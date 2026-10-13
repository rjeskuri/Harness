#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit|Bash): never on main.

GitHub Free does not protect branches on private repos, so this hook is the only block
against work landing on main or master outside a pull request.

Edit, Write, NotebookEdit: the repo is the one holding the file (its nearest existing
directory). When that repo is on main or master and has a commit, the edit is refused,
except for the main checkout's own notes: a path under docs/agent/ (the handoff note and
specs the session brief reads) or a path git ignores (`git check-ignore`: .env,
.claude/settings.local.json, scratch files).
Bash: the repo is the one at `cwd`, followed through `cd <dir>` and `git -C <dir>` inside
the command. When that repo is on main or master with a commit, the command is split on
;, &&, ||, |, & and newlines (and inside $(...), sh -c and eval) and refused when a part
commits, merges (except --ff-only), rebases, cherry-picks, reverts, applies mail, pops or
applies a stash, resets, pushes main (no refspec, or one naming main or master), deletes a
remote branch, deletes main or master, edits in place (sed -i, perl -i), or writes into the
repo (a redirection or tee to it, cp or mv into it, mv out of it, touch or rm in it; a path
is in the repo when it is relative or starts with the repo root). A force push (--force,
-f, --force-with-lease or a +refspec) is refused on every branch, in every repo. Reading,
git fetch, pull, switch, checkout, worktree, gh, harness-* and the project's
CLAUDE_VERIFY_CMD run.

Allowed always: a linked worktree, a detached HEAD, a repo with no commits, a path outside
any repo, and any repo whose HARNESS_ALLOW_MAIN is 1 (environment first, then `env` in the
repo's .claude/settings.json, then .claude/settings.local.json).

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0. Allowing
prints nothing. Fails open: any error of its own goes to stderr in one line, exit 0.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_SHOWN = 160
MAX_DEPTH = 8
GIT_TIMEOUT = 3
PROTECTED = {"main", "master"}
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
FORCE_ADVICE = (
    "Force pushes are refused on every branch; push new commits instead, or ask the user "
    "to rewrite history by hand."
)

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "nice", "timeout", "xargs"}
KEYWORDS = set("! { } if then elif else fi do done while until time".split())
GIT_VALUE_OPTIONS = set("-C -c --git-dir --work-tree --namespace --config-env".split())
PUSH_VALUE_OPTIONS = set("-o --push-option --repo --receive-pack --exec".split())
TOUCH_VALUE_OPTIONS = {"-t", "-d", "-r", "-A"}

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
NUMBER = re.compile(r"\d+(\.\d+)?[smhd]?$")
SHORT_CLUSTER = re.compile(r"-[A-Za-z0-9]+$")
SED_IN_PLACE = re.compile(r"-[nrsuzE]*i|--in-place(=|$)")
PERL_IN_PLACE = re.compile(r"-[aclnpsw]*i")
REDIRECT = re.compile(r"&>>|&>|>>|>\||>&|>|<<<|<<-|<<|<>|<&|<")
REDIRECTS = {
    "&>>": "out",
    "&>": "out",
    ">>": "out",
    ">|": "out",
    ">": "out",
    ">&": "dup",
    "<<<": "in",
    "<<-": "heredoc-",
    "<<": "heredoc",
    "<>": "out",
    "<&": "in",
    "<": "in",
}


def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def deny(reason: str) -> None:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    sys.stdout.write(json.dumps({"hookSpecificOutput": decision}))
    sys.stdout.flush()


def shown(text: str) -> str:
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "\u2026"


# ---------- settings ----------


def settings_env(root: Path) -> dict:
    """`env` from the project's settings files; settings.local.json wins over settings.json."""
    merged: dict = {}
    for name in ("settings.json", "settings.local.json"):
        try:
            data = json.loads((root / ".claude" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, dict):
            merged.update(env)
    return merged


def setting(name: str, file_env: dict) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    value = file_env.get(name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def allow_main(root: Path | None) -> bool:
    """HARNESS_ALLOW_MAIN is 1 in the environment or in `root`'s project settings."""
    file_env = settings_env(root) if root is not None else {}
    return setting("HARNESS_ALLOW_MAIN", file_env) == "1"


# ---------- the repo at a directory ----------


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT,
    )


def existing_dir(path: str) -> str:
    """`path` itself when it is a directory, else its nearest existing ancestor."""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def toplevel(directory: str) -> Path | None:
    """The top-level of the working tree holding `directory`, or None outside a repo."""
    out = git(directory, "rev-parse", "--show-toplevel")
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def protected_repo(directory: str) -> tuple[Path, str] | None:
    """(top-level, branch) when `directory` is in the main working tree of a repo that is
    on main or master and has a commit; None otherwise (not a repo, a linked worktree,
    detached, another branch, no commits yet)."""
    out = git(
        directory,
        "rev-parse",
        "--show-toplevel",
        "--absolute-git-dir",
        "--git-common-dir",
    )
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    if out.returncode != 0 or len(lines) < 3 or not all(lines[:3]):
        return None
    top, git_dir, common = lines[:3]
    common_dir = os.path.join(directory, common)  # unchanged when already absolute
    if os.path.realpath(git_dir) != os.path.realpath(common_dir):
        return None  # a linked worktree: its branch is never main
    head = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    branch = head.stdout.strip()
    if head.returncode != 0 or branch not in PROTECTED:
        return None  # detached, or another branch
    if git(directory, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return None  # no commits yet: the first commit has to land somewhere
    return Path(top), branch


# ---------- Edit, Write, NotebookEdit ----------

NOTES_DIR = "docs/agent/"


def repo_relative(path: str, top: Path) -> str | None:
    """`path` relative to the repo root `top`, with / separators; None when outside it.
    The nearest existing directory is resolved, so symlinked spellings still match."""
    base = existing_dir(os.path.dirname(path))
    tail = os.path.relpath(os.path.abspath(path), base)
    full = os.path.normpath(os.path.join(os.path.realpath(base), tail))
    rel = os.path.relpath(full, os.path.realpath(str(top)))
    if rel in (".", "..") or rel.startswith("../"):
        return None
    return rel.replace(os.sep, "/")


def notes_or_ignored(path: str, top: Path) -> bool:
    """True for a path under docs/agent/, or one git ignores (untracked and matched by
    an ignore rule)."""
    rel = repo_relative(path, top)
    if rel is None:
        return False
    if rel.startswith(NOTES_DIR):
        return True
    return git(str(top), "check-ignore", "-q", "--", rel).returncode == 0


def check_edit(data: dict, cwd: str) -> None:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw.strip():
        return
    path = os.path.join(cwd, os.path.expanduser(raw))
    found = protected_repo(existing_dir(os.path.dirname(path)))
    if found is None:
        return
    top, branch = found
    if allow_main(top) or notes_or_ignored(path, top):
        return
    deny(f"On {branch} in {top.name}. {ADVICE}")


# ---------- splitting a command (as in readonly_bash.py) ----------


class Part:
    """One simple command: its words without quotes, where its output goes, the bodies
    of its heredocs, and its text as written."""

    def __init__(self) -> None:
        self.words: list[str] = []
        self.targets: list[str] = []
        self.heredocs: list[str] = []
        self.text = ""


def closing(text: str, i: int) -> int:
    """Index of the ")" that closes the "(" at text[i], or len(text); quotes skipped."""
    depth, quote, j = 0, "", i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\" and quote == '"':
                j += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            j += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(text)


def substitution(text: str, i: int, nested: list[str]) -> int:
    """Keeps the command inside the $(...) or `...` at text[i]; returns the index after it.
    Arithmetic, $((...)), holds no command."""
    if text.startswith("$((", i):
        return closing(text, i + 1) + 1
    if text[i] == "`":
        j = text.find("`", i + 1)
        j = len(text) if j < 0 else j
        nested.append(text[i + 1 : j])
        return j + 1
    j = closing(text, i + 1)
    nested.append(text[i + 2 : j])
    return j + 1


def split_command(command: str, depth: int = 0) -> list[Part]:
    """The simple commands in `command`; those inside substitutions come last."""
    parts: list[Part] = []
    nested: list[str] = []
    heredocs: list[tuple[str, bool, Part]] = []
    part, start = Part(), 0
    word: str | None = None
    redirect = ""  # what the next word is: "out", "dup", "in", "heredoc" or "heredoc-"
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal word, redirect
        if word is None:
            return
        if redirect == "out" or (
            redirect == "dup" and not (word.isdigit() or word == "-")
        ):
            part.targets.append(word)
        elif redirect.startswith("heredoc"):
            heredocs.append((word, redirect == "heredoc-", part))
        elif not redirect:
            part.words.append(word)
        word, redirect = None, ""

    def end_part(end: int) -> None:
        nonlocal part, start, redirect
        end_word()
        if redirect in ("out", "dup"):
            part.targets.append("")  # a redirection with no target
        redirect = ""
        part.text = command[start:end].strip()
        if part.words or part.targets:
            parts.append(part)
        part, start = Part(), end + 1

    while i < n:
        c = command[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            end_part(i)
            i += 1
            for delimiter, tabs, owner in heredocs:  # their bodies follow this line
                body = []
                while i < n:
                    j = command.find("\n", i)
                    j = n if j < 0 else j
                    line, i = command[i:j], j + 1
                    if (line.lstrip("\t") if tabs else line) == delimiter:
                        break
                    body.append(line)
                owner.heredocs.append("\n".join(body))
            heredocs.clear()
            start = i
        elif c == "#" and word is None:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            if command[i + 1 : i + 2] != "\n":
                word = (word or "") + command[i + 1 : i + 2]
            i += 2
        elif c == "'":
            j = command.find("'", i + 1)
            j = n if j < 0 else j
            word = (word or "") + command[i + 1 : j]
            i = j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n:
                    if command[j + 1] in '"\\$`':
                        buf.append(command[j + 1])
                    elif command[j + 1] != "\n":
                        buf.append(command[j : j + 2])
                    j += 2
                elif command[j] == "`" or command.startswith("$(", j):
                    k = substitution(command, j, nested)
                    buf.append(command[j:k])
                    j = k
                else:
                    buf.append(command[j])
                    j += 1
            word = (word or "") + "".join(buf)
            i = j + 1
        elif c == "`" or command.startswith("$(", i):
            k = substitution(command, i, nested)
            word = (word or "") + command[i:k]
            i = k
        elif command.startswith("((", i) and word is None:  # arithmetic, no command
            i = closing(command, i) + 1
        elif c in "<>" and command.startswith("(", i + 1):  # process substitution
            end_word()
            k = closing(command, i + 1)
            nested.append(command[i + 2 : k])
            i = k + 1
        elif c in "<>" or command.startswith("&>", i):
            if word is not None and word.isdigit() and c != "&":
                word = None  # a file descriptor number, as in 2>
            end_word()
            op = REDIRECT.match(command, i).group(0)
            redirect = REDIRECTS[op]
            i += len(op)
        elif c in ";|&()":
            end_part(i)
            i += 1
        else:
            word = (word or "") + c
            i += 1
    end_part(n)
    if depth < MAX_DEPTH:
        for inner in nested:
            parts.extend(split_command(inner, depth + 1))
    return parts


def command_words(words: list[str]) -> list[str]:
    """`words` from the command's name on: assignments, keywords and wrappers skipped."""
    k = 0
    while k < len(words):
        w = words[k]
        if ASSIGNMENT.match(w) or w in KEYWORDS:
            k += 1
        elif os.path.basename(w) in WRAPPERS:
            k += 1
            while k < len(words) and (
                words[k].startswith("-")
                or ASSIGNMENT.match(words[k])
                or NUMBER.match(words[k])
            ):
                k += 1
        else:
            break
    return words[k:]


def stdin_program(args: list[str]) -> bool:
    """True when a shell given `args` reads its program from stdin."""
    for a in args:
        if a == "-":
            return True
        if not a.startswith("-") or a.startswith(("-c", "-m")):
            return False
    return True


# ---------- judging one simple command ----------


def plain_args(
    args: list[str], value_options: frozenset | set = frozenset()
) -> list[str]:
    """The non-option arguments; everything after `--` counts."""
    found, k = [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            found.extend(args[k:])
            break
        if a in value_options:
            k += 1
        elif not a.startswith("-") or a == "-":
            found.append(a)
    return found


def short_flag(args: list[str], letter: str) -> bool:
    return any(
        SHORT_CLUSTER.match(a) and not a.startswith("--") and letter in a[1:]
        for a in args
    )


def git_split(args: list[str], cwd: str | None) -> tuple[str | None, str, list[str]]:
    """(directory git runs in, subcommand, its arguments); the directory is None when
    --git-dir, --work-tree or an unexpanded -C hides the repo."""
    k = 0
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a in GIT_VALUE_OPTIONS:
            value = args[k + 1] if k + 1 < len(args) else ""
            if a == "-C":
                cwd = resolve_dir(cwd, value)
            elif a in ("--git-dir", "--work-tree"):
                cwd = None
            k += 2
        else:
            if a.startswith(("--git-dir=", "--work-tree=")):
                cwd = None
            k += 1
    if k >= len(args):
        return cwd, "", []
    return cwd, args[k], args[k + 1 :]


def push_refusal(rest: list[str], branch: str) -> str | None:
    """Why `git push <rest>` would update or delete what it must not, on `branch`."""
    plain = plain_args(rest, PUSH_VALUE_OPTIONS)
    if "--delete" in rest or short_flag(rest, "d"):
        return "git push --delete deletes a remote branch"
    refspecs = plain[1:]  # the first plain argument is the remote
    if not refspecs:
        return f"git push without a refspec pushes {branch}"
    for spec in refspecs:
        spec = spec.lstrip("+")
        if spec.startswith(":"):
            return "a git push refspec starting with : deletes a remote branch"
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        name = dst[len("refs/heads/") :] if dst.startswith("refs/heads/") else dst
        if name in PROTECTED or (":" not in spec and spec in ("HEAD", "@")):
            return f"git push {spec} updates {name if name in PROTECTED else branch}"
    return None


def is_force_push(sub: str, rest: list[str]) -> bool:
    if sub != "push":
        return False
    for a in rest:
        if a in ("--force", "--force-with-lease") or a.startswith(
            "--force-with-lease="
        ):
            return True
    if short_flag(rest, "f"):
        return True
    return any(a.startswith("+") for a in plain_args(rest, PUSH_VALUE_OPTIONS)[1:])


def git_refusal(sub: str, rest: list[str], branch: str) -> str | None:
    """Why git `sub` `rest` would change `branch` (main or master), or None."""
    plain = plain_args(rest)
    if sub in ("commit", "rebase", "cherry-pick", "revert", "am", "reset"):
        return f"git {sub} changes {branch}"
    if sub == "merge" and "--ff-only" not in rest:
        return f"git merge without --ff-only changes {branch}"
    if sub == "stash" and plain[:1] in (["pop"], ["apply"]):
        return f"git stash {plain[0]} changes files on {branch}"
    if sub == "push":
        return push_refusal(rest, branch)
    if sub == "branch" and (
        "--delete" in rest or short_flag(rest, "d") or short_flag(rest, "D")
    ):
        hit = [b for b in plain if b in PROTECTED]
        if hit:
            return f"git branch -D {hit[0]} deletes {hit[0]}"
    return None


def resolve_dir(cwd: str | None, target: str) -> str | None:
    """The directory `cd target` or `git -C target` lands in; None when unknown."""
    if cwd is None:
        return None
    target = os.path.expandvars(os.path.expanduser(target))
    if not target or "$" in target or "`" in target or target == "-":
        return None
    return os.path.normpath(os.path.join(cwd, target))


def inside(path: str, top: Path) -> bool:
    """Relative paths, and absolute ones under the repo root, are inside the repo."""
    if not path:
        return False
    path = os.path.expandvars(os.path.expanduser(path))
    if "$" in path or "`" in path:
        return False  # unexpanded: not established
    if not os.path.isabs(path):
        return True
    root = str(top).rstrip("/") + "/"
    candidates = {os.path.normpath(path)}
    try:
        candidates.add(os.path.realpath(path))
    except OSError:
        pass
    return any(c == root[:-1] or c.startswith(root) for c in candidates)


def file_refusal(name: str, args: list[str], top: Path) -> str | None:
    """Why `name args` writes into the repo at `top`, or None."""
    if name == "sed" and any(SED_IN_PLACE.match(a) for a in args):
        return "sed -i edits files in place"
    if name == "perl" and any(PERL_IN_PLACE.match(a) for a in args):
        return "perl -i edits files in place"
    if name == "tee":
        paths = plain_args(args)
    elif name in ("rm", "mv"):
        paths = plain_args(args)
    elif name == "touch":
        paths = plain_args(args, TOUCH_VALUE_OPTIONS)
    elif name == "cp":
        paths = plain_args(args, {"-t", "-S"})[-1:]
        if "-t" in args and args.index("-t") + 1 < len(args):
            paths = [args[args.index("-t") + 1]]
    else:
        return None
    for path in paths:
        if inside(path, top):
            return f"{name} changes {path} inside the repo"
    return None


# ---------- Bash ----------


def repo_at(state: dict, directory: str | None) -> tuple[Path, str] | None:
    """protected_repo(directory), cached per command; None for an unknown directory."""
    if directory is None or not os.path.isdir(directory):
        return None
    repos = state["repos"]
    if directory not in repos:
        repos[directory] = protected_repo(directory)
    return repos[directory]


def shell_script(name: str, args: list[str], part: Part) -> str | None:
    """The program a shell, or eval, runs: its -c string or the heredoc it reads."""
    if name == "eval":
        return " ".join(args)
    if name not in SHELLS:
        return None
    flags = [
        k
        for k, a in enumerate(args)
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]
    ]
    if flags:
        return args[flags[0] + 1] if flags[0] + 1 < len(args) else None
    if stdin_program(args) and part.heredocs:
        return "\n".join(part.heredocs)
    return None


def part_refusal(
    state: dict, part: Part, depth: int
) -> tuple[str, str, Path | None, str | None] | None:
    words = command_words(part.words)
    name = os.path.basename(words[0]) if words else ""
    args = words[1:]
    if name in ("cd", "pushd"):
        plain = plain_args(args)
        state["cwd"] = resolve_dir(state["cwd"], plain[0] if plain else "~")
        return None
    where, sub, rest = (
        git_split(args, state["cwd"]) if name == "git" else (None, "", [])
    )
    if name == "git" and is_force_push(sub, rest):
        top = toplevel(where) if where and os.path.isdir(where) else None
        return part.text, "a force push rewrites history", top, None
    here = repo_at(state, state["cwd"])
    if here is not None:
        top, branch = here
        for target in part.targets:
            if inside(target, top):
                why = f"it writes to {target} through a redirection"
                return part.text, why, top, branch
        why = file_refusal(name, args, top) if name else None
        if why:
            return part.text, why, top, branch
    if name == "git":
        there = repo_at(state, where)
        why = git_refusal(sub, rest, there[1]) if there is not None else None
        if why:
            return part.text, why, there[0], there[1]
    script = shell_script(name, args, part)
    if script is None:
        return None
    saved = state["cwd"]
    found = refusal(state, script, depth + 1)
    state["cwd"] = saved  # a child shell's cd stays in the child
    return found


def refusal(
    state: dict, command: str, depth: int = 0
) -> tuple[str, str, Path | None, str | None] | None:
    """The first part of `command` to refuse, walking parts with the directory each runs
    in (state["cwd"], moved by cd); None when every part may run."""
    if depth > MAX_DEPTH:
        return None
    for part in split_command(command):
        found = part_refusal(state, part, depth)
        if found:
            return found
    return None


def verify_command(top: Path) -> str | None:
    return setting("CLAUDE_VERIFY_CMD", settings_env(top))


def check_bash(data: dict, cwd: str) -> None:
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return
    found = refusal({"cwd": cwd, "repos": {}}, command)
    if found is None:
        return
    text, why, top, branch = found
    if allow_main(top):
        return
    if branch is None:
        deny(f'Refused "{shown(text)}": {why}. {FORCE_ADVICE}')
        return
    verify = verify_command(top) if top is not None else None
    if verify and command.strip() == verify:
        return
    name = top.name if top is not None else "this repo"
    deny(f'On {branch} in {name}. Refused "{shown(text)}": {why}. {ADVICE}')


# ---------- main ----------


def main() -> int:
    if os.environ.get("HARNESS_ALLOW_MAIN", "").strip() == "1":
        return 0
    data = read_input()
    cwd = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    tool = data.get("tool_name")
    if tool in ("Edit", "Write", "NotebookEdit"):
        check_edit(data, cwd)
    elif tool == "Bash":
        check_bash(data, cwd)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open: a broken guard must never trap the agent
        sys.stderr.write(f"branch_guard.py: {exc}\n")
    sys.exit(0)
