#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit|Bash): never on main.

GitHub Free does not protect branches on private repos, so this hook is the only block
against work landing on main or master outside a pull request.

Edit, Write, NotebookEdit: the repo is the one holding the file (its nearest existing
directory). When that repo is on main or master and has a commit, the edit is refused,
except for the main checkout's own notes: a path under docs/agent/ (the handoff note and
specs the session brief reads) or a path git ignores (`git check-ignore`: .env,
.claude/settings.local.json, scratch files).

Bash: the command is split on ;, &&, ||, |, & and newlines (and inside (...), $(...),
sh -c, eval, a heredoc a shell reads and a git !alias), and each part is judged in the
directory it runs in: the hook's cwd, moved by cd and pushd (an empty target, or one that
is not a directory, leaves it where it was, unless && follows, since a failed cd stops
the chain), restored after a subshell or a child shell, and moved for git by -C,
--git-dir and --work-tree. Then:
  - every file a part writes is judged by the repo holding it, as an Edit is: a
    redirection or tee to it, cp, mv, install, ln, rsync or dd into it, mv, rm, touch,
    truncate or chmod of it, and in-place edits (sed or gsed -i, perl -i, awk -i inplace,
    ed, patch). A path under <top>/.claude/worktrees/ or in a linked worktree belongs to
    that worktree, and a path outside any repo is never refused;
  - when the repo git runs in is on main or master with a commit: git commit, merge
    (except --ff-only), rebase, cherry-pick, revert, am, apply, reset, rm, mv, clean,
    restore, stash pop|apply, checkout of paths or -B main, switch -C main, branch -f, -m
    or -C onto main, -D main, update-ref and symbolic-ref writes to HEAD or main, and a
    push with no refspec, of HEAD, or deleting a remote branch. Aliases given with
    -c alias.<name>= are expanded first;
  - when the part runs in such a repo: python -c, node -e, or a program either reads from
    a heredoc, that writes files;
  - in every repo, on every branch: a force push (--force, -f, --force-with-lease or a
    +refspec), and a push that names main or master as the destination (main,
    HEAD:main, refs/heads/main, :main), --mirror, --all, or a matching (:) or wildcard
    refspec that reaches main.
Reading, mkdir, git fetch, pull, switch, checkout <branch>, worktree, gh, harness-* and
the project's CLAUDE_VERIFY_CMD run.

Allowed always: a linked worktree, a detached HEAD, a repo with no commits, a path outside
any repo, and any repo whose HARNESS_ALLOW_MAIN is 1 (environment first, then `env` in the
repo's .claude/settings.json, then .claude/settings.local.json).

tests_first.py imports this module for walk(), writes(), git_split(), git_paths() and
code_writes(); importing it has no side effects.

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0. Allowing
prints nothing. Fails open: any error of its own goes to stderr in one line, exit 0.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

MAX_SHOWN = 160
MAX_DEPTH = 8
GIT_TIMEOUT = 3
PROTECTED = {"main", "master"}
WORKTREES = ".claude/worktrees/"
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
FORCE_ADVICE = (
    "Force pushes are refused on every branch; push new commits instead, or ask the user "
    "to rewrite history by hand."
)
PUSH_ADVICE = (
    "main and master change only through a pull request: push the ticket branch and "
    "open one (harness-pr open <issue#>)."
)

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
NODES = {"node", "nodejs", "bun"}
NODE_VALUE_OPTIONS = set(
    "-r --require --import --loader --input-type -C --conditions".split()
)
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "nice", "timeout", "xargs"}
KEYWORDS = set("! { } if then elif else fi do done while until time".split())
GIT_VALUE_OPTIONS = set("-C -c --git-dir --work-tree --namespace --config-env".split())
PUSH_VALUE_OPTIONS = set("-o --push-option --repo --receive-pack --exec".split())
PUSH_EVERYTHING = {"--mirror", "--all", "--branches"}
CHECKOUT_VALUE_OPTIONS = {"-b", "-B", "--orphan"}
TOUCH_VALUE_OPTIONS = {"-t", "-d", "-r", "-A"}
COPY_VALUE_OPTIONS = {"-t", "-S", "--target-directory", "--suffix"}
INSTALL_VALUE_OPTIONS = set(
    "-m -o -g -t -S -B -f -M --mode --owner --group --target-directory --suffix".split()
)
TRUNCATE_VALUE_OPTIONS = {"-s", "-r", "--size", "--reference"}
RSYNC_VALUE_OPTIONS = set(
    "-e -f -T -B -M --rsh --filter --exclude --include --exclude-from --include-from "
    "--files-from --temp-dir --partial-dir --backup-dir --suffix --chmod --chown "
    "--log-file --password-file --compare-dest --copy-dest --link-dest --timeout "
    "--port --remote-option --rsync-path".split()
)
PATCH_VALUE_OPTIONS = set(
    "-p -d -i -o -r -B -D -F -V -z -Y --strip --directory --input --output "
    "--reject-file --prefix --ifdef --fuzz --version-control --suffix "
    "--basename-prefix".split()
)
AWK_VALUE_OPTIONS = set(
    "-i -f -v -F -l -e -E --include --file --assign --field-separator --load --source "
    "--exec".split()
)
AWK_PROGRAM_OPTIONS = {"-f", "-e", "-E", "--file", "--source", "--exec"}
CHMOD_OPTION_LETTERS = set("fhvRHLPcECNiI")
# Commands whose files, when xargs supplies them, are unknown: the directory they run
# in is what they change.
XARGS_WRITERS = set("rm touch chmod truncate tee ed".split())

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
NUMBER = re.compile(r"\d+(\.\d+)?[smhd]?$")
SHORT_CLUSTER = re.compile(r"-[A-Za-z0-9]+$")
PYTHON = re.compile(r"python[0-9.]*$")
SED_IN_PLACE = re.compile(r"-[nrsuzE]*i|--in-place(=|$)")
REMOTE_PATH = re.compile(r"[^/]*:")
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
# Inline programs that write files (as in readonly_bash.py; creating a directory is
# allowed, as mkdir is).
OPEN_CALL = re.compile(r"\bopen\s*\(")
WRITE_MODE = re.compile(r"""(?:,|\bmode\s*=)\s*(["'])([rwxabt+]{1,4})\1""")
PATH_OPEN = re.compile(
    r"""\.open\s*\(\s*(?:mode\s*=\s*)?(["'])[rbt]*[wax+][rwxabt+]*\1"""
)
PY_WRITERS = re.compile(
    r"\.(write_text|write_bytes|unlink|rmdir|touch|symlink_to)\s*\("
    r"|\b(os|shutil)\.(remove|unlink|rmdir|removedirs|rename|renames|replace"
    r"|truncate|chmod|chown|symlink|link|rmtree|move|copy|copy2|copyfile"
    r"|copytree)\s*\("
)
JS_WRITERS = re.compile(
    r"\b(writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream"
    r"|copyFile|copyFileSync|cpSync)\s*\("
    r"|\.(cp|rename|renameSync|rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync|truncate"
    r"|truncateSync|symlink|symlinkSync)\s*\("
    r"|\bBun\.write\s*\("
)


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
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "…"


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


def toplevel(directory: str | None) -> Path | None:
    """The top-level of the working tree holding `directory`, or None outside a repo."""
    if not directory or not os.path.isdir(directory):
        return None
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


def resolved(path: str) -> str:
    """The absolute `path` with its nearest existing directory's symlinks resolved."""
    base = existing_dir(os.path.dirname(os.path.abspath(path)))
    tail = os.path.relpath(os.path.abspath(path), base)
    return os.path.normpath(os.path.join(os.path.realpath(base), tail))


def repo_relative(path: str, top: Path) -> str | None:
    """`path` relative to the repo root `top`, with / separators; None when outside it.
    The nearest existing directory is resolved, so symlinked spellings still match."""
    rel = os.path.relpath(resolved(path), os.path.realpath(str(top)))
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
    of its heredocs, its text as written and the operator after it (;, &&, ||, |, &, or
    "" at a newline or the end). A marker part, with no words, opens ("(") or closes
    (")") a subshell."""

    def __init__(self, marker: str = "") -> None:
        self.words: list[str] = []
        self.targets: list[str] = []
        self.heredocs: list[str] = []
        self.text = ""
        self.sep = ""
        self.marker = marker


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
    """The simple commands in `command`, with marker parts around subshells; those inside
    substitutions come last, each in its own subshell."""
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

    def end_part(end: int, op: str = "") -> None:
        nonlocal part, start, redirect
        end_word()
        if redirect in ("out", "dup"):
            part.targets.append("")  # a redirection with no target
        redirect = ""
        part.text = command[start:end].strip()
        part.sep = op
        if part.words or part.targets:
            parts.append(part)
        part, start = Part(), end + max(len(op), 1)

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
            op = command[i : i + 2] if command.startswith(("&&", "||"), i) else c
            end_part(i, op)
            i += len(op)
            if c in "()":
                parts.append(Part(marker=c))
        else:
            word = (word or "") + c
            i += 1
    end_part(n)
    if depth < MAX_DEPTH:
        for inner in nested:
            parts.append(Part(marker="("))
            parts.extend(split_command(inner, depth + 1))
            parts.append(Part(marker=")"))
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


# ---------- where each part runs ----------


def resolve_dir(cwd: str | None, target: str) -> str | None:
    """The directory `git -C target` (or --work-tree) names: `cwd` for an empty target,
    as git treats it; None when unknown (unexpanded variables)."""
    if cwd is None:
        return None
    target = os.path.expandvars(os.path.expanduser(target))
    if "$" in target or "`" in target:
        return None
    if not target:
        return cwd
    return os.path.normpath(os.path.join(cwd, target))


def cd_target(cwd: str | None, target: str, sep: str) -> str | None:
    """Where `cd target` leaves the shell. An empty target is a no-op, and one that is not
    a directory (yet) makes cd fail, which leaves the shell where it was, unless && follows:
    then nothing after a failed cd runs, and the commands that do run are in `target`
    (made earlier in the command). None when unknown (`cd -`, unexpanded variables)."""
    if target == "-":
        return None
    path = resolve_dir(cwd, target)
    if path is None or path == cwd or os.path.isdir(path):
        return path
    return path if sep == "&&" else cwd


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


def walk(state: dict, command: str, depth: int = 0):
    """Yields (part, name, args) for each simple command in `command` in the order the
    shell runs it, with state["cwd"] set to the directory it runs in (None when unknown).
    cd and pushd move it; a subshell, a substitution and a child shell (sh -c, eval, a
    heredoc a shell reads, a git !alias) put it back when they end."""
    if depth > MAX_DEPTH:
        return
    saved: list = []
    for part in split_command(command):
        if part.marker == "(":
            saved.append(state["cwd"])
            continue
        if part.marker == ")":
            if saved:
                state["cwd"] = saved.pop()
            continue
        words = command_words(part.words)
        name = os.path.basename(words[0]) if words else ""
        args = words[1:]
        yield part, name, args
        if name in ("cd", "pushd"):
            plain = plain_args(args)
            target = plain[0] if plain else "~"
            state["cwd"] = cd_target(state["cwd"], target, part.sep)
            continue
        script = shell_script(name, args, part)
        if script is None and name == "git":
            _, sub, rest = git_split(args, state["cwd"])
            if sub.startswith("!"):
                script = " ".join([sub[1:]] + rest)
        if script is not None:
            before = state["cwd"]
            yield from walk(state, script, depth + 1)
            state["cwd"] = before  # a child shell's cd stays in the child


# ---------- arguments ----------


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


def option_value(args: list[str], names: tuple) -> str | None:
    """The value given to the first of the options `names` (`-B x`, `--long=x`)."""
    for k, a in enumerate(args):
        if a == "--":
            return None
        if a in names:
            return args[k + 1] if k + 1 < len(args) else ""
        for name in names:
            if name.startswith("--") and a.startswith(name + "="):
                return a[len(name) + 1 :]
    return None


def absolute(path: str, cwd: str | None) -> str | None:
    """`path` made absolute against `cwd` and normalized; None when it cannot be known."""
    if not path:
        return None
    path = os.path.expandvars(os.path.expanduser(path))
    if "$" in path or "`" in path:
        return None  # unexpanded: not established
    if not os.path.isabs(path):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


# ---------- git ----------


def git_split(args: list[str], cwd: str | None) -> tuple[str | None, str, list[str]]:
    """(directory git runs in, subcommand, its arguments). -C, --work-tree and --git-dir
    move the directory; an alias given with -c alias.<name>=<value> is expanded (a !alias
    comes back as the subcommand "!<command>"). The directory is None when unknown."""
    k = 0
    git_dir = work_tree = None
    aliases: dict = {}
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a in GIT_VALUE_OPTIONS:
            value = args[k + 1] if k + 1 < len(args) else ""
            k += 2
        elif a.startswith(("--git-dir=", "--work-tree=")):
            a, value = a.split("=", 1)
            k += 1
        else:
            k += 1
            continue
        if a == "-C":
            cwd = resolve_dir(cwd, value)
        elif a == "--git-dir":
            git_dir = resolve_dir(cwd, value)
        elif a == "--work-tree":
            work_tree = resolve_dir(cwd, value)
        elif a == "-c":
            key, _, expansion = value.partition("=")
            if key.lower().startswith("alias."):
                aliases[key[len("alias.") :].lower()] = expansion
    where = cwd
    if work_tree is not None:
        where = work_tree
    elif git_dir is not None:
        where = (
            os.path.dirname(git_dir) if os.path.basename(git_dir) == ".git" else git_dir
        )
    if k >= len(args):
        return where, "", []
    sub, rest = args[k], args[k + 1 :]
    expansion = aliases.get(sub.lower())
    if expansion is not None:
        if expansion.startswith("!"):
            return where, expansion, rest
        try:
            words = shlex.split(expansion)
        except ValueError:
            words = expansion.split()
        if words:
            sub, rest = words[0], words[1:] + rest
    return where, sub, rest


def branch_name(ref: str) -> str:
    """The branch a ref names: refs/heads/main and heads/main are main."""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def push_target_refusal(rest: list[str]) -> str | None:
    """Why `git push <rest>` would update or delete main or master, from any branch."""
    for a in rest:
        option = a.split("=", 1)[0]
        if option in PUSH_EVERYTHING:
            return f"git push {option} pushes every branch, main included"
    deleting = "--delete" in rest or short_flag(rest, "d")
    for spec in plain_args(rest, PUSH_VALUE_OPTIONS)[1:]:  # the first is the remote
        spec = spec.lstrip("+")
        if spec == ":":
            return "git push : pushes every matching branch, main included"
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        name = branch_name(dst)
        if name in PROTECTED:
            verb = "deletes" if deleting or spec.startswith(":") else "updates"
            return f"git push {spec} {verb} {name}"
        if "*" in dst and any(
            fnmatch.fnmatchcase(ref, dst)
            for ref in ("main", "master", "refs/heads/main", "refs/heads/master")
        ):
            return f"git push {spec} reaches main"
    return None


def push_refusal(rest: list[str], branch: str) -> str | None:
    """Why `git push <rest>` would update or delete what it must not, on `branch`."""
    why = push_target_refusal(rest)
    if why:
        return why
    if "--delete" in rest or short_flag(rest, "d"):
        return "git push --delete deletes a remote branch"
    refspecs = plain_args(rest, PUSH_VALUE_OPTIONS)[1:]
    if not refspecs:
        return f"git push without a refspec pushes {branch}"
    for spec in refspecs:
        spec = spec.lstrip("+")
        if spec.startswith(":"):
            return "a git push refspec starting with : deletes a remote branch"
        if spec in ("HEAD", "@"):
            return f"git push {spec} updates {branch}"
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


def dry_run(rest: list[str]) -> bool:
    return "--dry-run" in rest or short_flag(rest, "n")


def index_only(rest: list[str]) -> bool:
    """git restore that touches only the index (--staged without --worktree)."""
    staged = "--staged" in rest or short_flag(rest, "S")
    return staged and not ("--worktree" in rest or short_flag(rest, "W"))


def checkout_paths(rest: list[str], where: str | None) -> list[str]:
    """The paths `git checkout <rest>` restores: after --, after the tree-ish, or a lone
    argument that names an existing file or directory."""
    if "--" in rest:
        return rest[rest.index("--") + 1 :]
    plain = plain_args(rest, CHECKOUT_VALUE_OPTIONS)
    if len(plain) >= 2:
        return plain[1:]
    if (
        len(plain) == 1
        and plain[0] != "-"
        and where is not None
        and os.path.exists(os.path.join(where, plain[0]))
    ):
        return plain
    return []


def git_paths(sub: str, rest: list[str], where: str | None) -> list[str]:
    """The working-tree paths git `sub` `rest` writes, relative to `where`: checkout and
    restore of paths, rm and mv (dry runs and index-only forms write none)."""
    if sub == "checkout":
        return checkout_paths(rest, where)
    if sub == "restore" and not index_only(rest):
        return plain_args(rest, {"-s", "--source"})
    if sub == "rm" and not dry_run(rest) and "--cached" not in rest:
        return plain_args(rest)
    if sub == "mv" and not dry_run(rest):
        return plain_args(rest)
    return []


def branch_refusal(rest: list[str]) -> str | None:
    plain = plain_args(rest)
    if "--delete" in rest or short_flag(rest, "d") or short_flag(rest, "D"):
        hit = [b for b in plain if b in PROTECTED]
        return f"git branch -D {hit[0]} deletes {hit[0]}" if hit else None
    if "--move" in rest or short_flag(rest, "m") or short_flag(rest, "M"):
        hit = [b for b in plain[:2] if b in PROTECTED]
        if hit:
            return f"git branch -m renames or overwrites {hit[0]}"
    if "--copy" in rest or short_flag(rest, "c") or short_flag(rest, "C"):
        if plain and plain[-1] in PROTECTED:
            return f"git branch -C overwrites {plain[-1]}"
    if ("--force" in rest or short_flag(rest, "f")) and plain[:1]:
        if plain[0] in PROTECTED:
            return f"git branch -f {plain[0]} moves {plain[0]}"
    return None


def checkout_refusal(rest: list[str], branch: str, where: str | None) -> str | None:
    reset = option_value(rest, ("-B",))
    if reset is not None and branch_name(reset) in PROTECTED:
        return f"git checkout -B {reset} resets {reset}"
    if "-p" in rest or "--patch" in rest or checkout_paths(rest, where):
        return f"git checkout of paths changes files on {branch}"
    return None


def touches_head(ref: str) -> bool:
    return ref == "HEAD" or branch_name(ref) in PROTECTED


def git_refusal(
    sub: str, rest: list[str], branch: str, where: str | None
) -> str | None:
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
    if sub == "branch":
        return branch_refusal(rest)
    if sub == "apply":
        reads = {"--check", "--stat", "--numstat", "--summary"}
        if not (reads & set(rest)) or "--apply" in rest:
            return f"git apply changes files on {branch}"
    if sub in ("rm", "mv", "clean") and not dry_run(rest):
        if not (sub == "rm" and "--cached" in rest):
            return f"git {sub} changes files on {branch}"
    if sub == "restore" and not index_only(rest):
        return f"git restore changes files on {branch}"
    if sub == "checkout":
        return checkout_refusal(rest, branch, where)
    if sub == "switch":
        reset = option_value(rest, ("-C", "--force-create"))
        if reset is not None and branch_name(reset) in PROTECTED:
            return f"git switch -C {reset} resets {reset}"
    if sub == "update-ref":
        refs = plain_args(rest, {"-m"})
        if "--stdin" in rest:
            return f"git update-ref --stdin can move {branch}"
        if refs and touches_head(refs[0]):
            return f"git update-ref {refs[0]} moves {branch}"
    if sub == "symbolic-ref":
        refs = plain_args(rest, {"-m"})
        writes = len(refs) >= 2 or "-d" in rest or "--delete" in rest
        if writes and refs and touches_head(refs[0]):
            return f"git symbolic-ref {refs[0]} repoints {branch}"
    return None


# ---------- files a part writes ----------


def sed_files(args: list[str]) -> list[str]:
    """The files `sed args` reads (and, with -i, rewrites): the plain arguments after the
    script, which is the first plain argument unless -e or -f gives it. A lone -i
    followed by '' or .ext takes that word as BSD's backup suffix."""
    files: list[str] = []
    script, k = False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files.extend(args[k:])
            break
        if a in ("-e", "--expression", "-f", "--file"):
            script, k = True, k + 1
        elif a.startswith(("--expression=", "--file=")):
            script = True
        elif a in ("-l", "--line-length"):
            k += 1
        elif a.startswith("-") and len(a) > 1 and not a.startswith("--"):
            letters = a[1:]
            for j, ch in enumerate(letters):
                last = j == len(letters) - 1
                if ch in "ef":
                    script = True
                    k += 1 if last else 0
                    break
                if ch == "i":
                    if last and k < len(args) and (
                        args[k] == "" or args[k].startswith(".")
                    ):
                        k += 1
                    break
                if ch == "l":
                    k += 1 if last else 0
                    break
        elif not a.startswith("--") and a != "-":
            files.append(a)
    return files if script else files[1:]


def perl_files(args: list[str]) -> tuple[bool, list[str]]:
    """(whether `perl args` edits in place, the files it is given)."""
    in_place = code = False
    files: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files = args[k:]
            break
        if not a.startswith("-") or a == "-":
            files = args[k - 1 :]
            break
        letters, j = a[1:], 0
        while j < len(letters):
            ch = letters[j]
            last = j == len(letters) - 1
            if ch == "i":
                in_place = True  # the rest of the word is the backup suffix
                break
            if ch in "eE":
                code = True
                k += 1 if last else 0
                break
            if ch in "IMm":
                k += 1 if last else 0
                break
            if ch in "xdDCFV":
                break  # the rest of the word is its value
            j += 1
            if ch in "0l":
                while j < len(letters) and letters[j].isdigit():
                    j += 1
    return in_place, (files if code else files[1:])


def awk_files(args: list[str]) -> tuple[bool, list[str]]:
    """(whether `awk args` loads gawk's inplace extension, the files it is given)."""
    in_place = program = False
    plain: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            plain.extend(args[k:])
            break
        if a in AWK_VALUE_OPTIONS:
            value = args[k] if k < len(args) else ""
            k += 1
            if a in ("-i", "--include") and value in ("inplace", "inplace.awk"):
                in_place = True
            program = program or a in AWK_PROGRAM_OPTIONS
        elif a in ("-iinplace", "-iinplace.awk", "--include=inplace"):
            in_place = True
        elif a.startswith(("--file=", "--source=", "--exec=")) or (
            len(a) > 2 and a[:2] in ("-f", "-e", "-E")
        ):
            program = True
        elif not a.startswith("-") or a == "-":
            plain.append(a)
    files = plain if program else plain[1:]
    return in_place, [f for f in files if not ASSIGNMENT.match(f)]


def chmod_files(args: list[str]) -> list[str]:
    """The files `chmod args` changes: the plain arguments after the mode (which may look
    like an option, as in chmod -x)."""
    files: list[str] = []
    mode = False
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            rest = args[k:]
            files.extend(rest if mode else rest[1:])
            break
        if a == "--reference":
            mode, k = True, k + 1
        elif a.startswith("--"):
            mode = mode or a.startswith("--reference=")
        elif a.startswith("-") and len(a) > 1 and set(a[1:]) <= CHMOD_OPTION_LETTERS:
            continue
        elif not mode:
            mode = True
        else:
            files.append(a)
    return files


def copy_writes(name: str, args: list[str], cwd: str | None, options: set) -> list:
    """(destination as written, path written) for cp, install, ln and mv: the
    destination, or the name each source gets inside it when it is a directory."""
    target = option_value(args, ("-t", "--target-directory"))
    plain = plain_args(args, options)
    if target is not None:
        dest, sources, into = target, plain, True
    elif name == "ln" and len(plain) == 1:
        dest, sources, into = ".", plain, True
    elif len(plain) >= 2:
        dest, sources = plain[-1], plain[:-1]
        full = absolute(dest, cwd)
        into = (
            dest.endswith("/")
            or len(sources) > 1
            or (full is not None and os.path.isdir(full))
        )
    else:
        return []
    if not into:
        return [(dest, dest)]
    return [
        (dest, os.path.join(dest, os.path.basename(s.rstrip("/")) or s))
        for s in sources
    ]


def rsync_dest(args: list[str]) -> list[str]:
    plain: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            plain.extend(args[k:])
            break
        if a in RSYNC_VALUE_OPTIONS or (
            SHORT_CLUSTER.match(a) and not a.startswith("--") and a.endswith("e")
        ):
            k += 1
        elif not a.startswith("-"):
            plain.append(a)
    if len(plain) < 2:
        return []
    if REMOTE_PATH.match(plain[-1]) and not plain[-1].startswith("/"):
        return []  # host:path
    return plain[-1:]


def edits_in_place(name: str, args: list[str]) -> bool:
    if name in ("sed", "gsed"):
        return any(SED_IN_PLACE.match(a) for a in args)
    if name == "perl":
        return perl_files(args)[0]
    if name in ("awk", "gawk"):
        return awk_files(args)[0]
    return False


def file_writes(name: str, args: list[str], cwd: str | None) -> list:
    """(as written, path, why, exact) for each file `name args` creates, changes or
    removes; `exact` is False when the path is only the directory the files are in."""

    def changes(paths: list[str]) -> list:
        return [(p, p, f"{name} changes {p} inside the repo", True) for p in paths]

    if name in ("sed", "gsed"):
        if not edits_in_place(name, args):
            return []
        why = f"{name} -i edits files in place"
        return [(f, f, why, True) for f in sed_files(args)]
    if name == "perl":
        in_place, files = perl_files(args)
        why = "perl -i edits files in place"
        return [(f, f, why, True) for f in files] if in_place else []
    if name in ("awk", "gawk"):
        in_place, files = awk_files(args)
        why = f"{name} -i inplace edits files in place"
        return [(f, f, why, True) for f in files] if in_place else []
    if name in ("ed", "red"):
        return changes(plain_args(args, {"-p"}))
    if name in ("tee", "rm"):
        return changes(plain_args(args))
    if name == "touch":
        return changes(plain_args(args, TOUCH_VALUE_OPTIONS))
    if name == "truncate":
        return changes(plain_args(args, TRUNCATE_VALUE_OPTIONS))
    if name == "chmod":
        return changes(chmod_files(args))
    if name == "dd":
        return changes([a[3:] for a in args if a.startswith("of=")])
    if name == "rsync":
        return changes(rsync_dest(args))
    if name in ("cp", "mv", "ln", "install"):
        if name == "install" and short_flag(args, "d"):
            return []  # creates directories, as mkdir does
        options = INSTALL_VALUE_OPTIONS if name == "install" else COPY_VALUE_OPTIONS
        found = []
        if name == "mv":  # the sources go away
            plain = plain_args(args, options)
            target = option_value(args, ("-t", "--target-directory"))
            moved = plain if target is not None else plain[:-1]
            found.extend(changes(moved))
        for dest, path in copy_writes(name, args, cwd, options):
            found.append((dest, path, f"{name} changes {dest} inside the repo", True))
        return found
    if name == "patch":
        if {"--dry-run", "--check", "-C"} & set(args):
            return []
        out = option_value(args, ("-o", "--output"))
        if out is not None:
            return changes([out])
        plain = plain_args(args, PATCH_VALUE_OPTIONS)
        if plain:
            return changes(plain[:1])
        where = option_value(args, ("-d", "--directory")) or "."
        return [(where, where, f"patch changes files in {place(where)}", False)]
    return []


def place(directory: str) -> str:
    return "the current directory" if directory in (".", "./") else directory


def writes(part: Part, name: str, args: list[str], cwd: str | None) -> list:
    """(as written, absolute path or None, why, exact) for everything `part` writes:
    its redirections and the files its command changes."""
    found = [
        (t, t, f"it writes to {t} through a redirection", True) for t in part.targets
    ]
    if name:
        found.extend(file_writes(name, args, cwd))
        wrappers = part.words[: max(len(part.words) - len(args) - 1, 0)]
        if any(os.path.basename(w) == "xargs" for w in wrappers) and (
            name in XARGS_WRITERS or edits_in_place(name, args)
        ):
            why = f"xargs {name} changes files in {place('.')}"
            found.append((".", ".", why, False))
    return [(text, absolute(p, cwd), why, exact) for text, p, why, exact in found]


def python_writes(code: str) -> str | None:
    """Why a python program writes files, or None."""
    for m in OPEN_CALL.finditer(code):
        call = code[m.end() - 1 : closing(code, m.end() - 1) + 1]
        for mode in WRITE_MODE.finditer(call):
            if set(mode.group(2)) & set("wax+"):
                return "python opens a file for writing"
    if PATH_OPEN.search(code):
        return "python opens a file for writing"
    if PY_WRITERS.search(code):
        return "python writes, moves or deletes files"
    return None


def inline_programs(name: str, args: list[str], part: Part) -> list[str]:
    """The programs python or node runs from the command line (-c, -e) or a heredoc."""
    programs: list[str] = []
    python = bool(PYTHON.match(name))
    if not python and name not in NODES:
        return programs
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if python and a in ("-W", "-X"):
            k += 1
        elif not python and a in NODE_VALUE_OPTIONS:
            k += 1
        elif python and a.startswith("-c"):
            programs.append(a[2:] or (args[k] if k < len(args) else ""))
            break
        elif not python and a in ("-e", "--eval", "-p", "--print"):
            programs.append(args[k] if k < len(args) else "")
            break
        elif not python and a.startswith(("--eval=", "--print=")):
            programs.append(a.split("=", 1)[1])
            break
        elif a == "-m" or a == "-" or not a.startswith("-"):
            break
    if not programs and stdin_program(args):
        programs.extend(part.heredocs)
    return programs


def code_writes(name: str, args: list[str], part: Part) -> str | None:
    """Why the python or node program `part` runs inline writes files, or None."""
    for code in inline_programs(name, args, part):
        if PYTHON.match(name):
            why = python_writes(code)
        else:
            why = None
            if JS_WRITERS.search(code):
                why = "node writes, moves or deletes files"
        if why:
            return why
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


def holder(state: dict, path: str | None) -> tuple[Path, str] | None:
    """The protected repo whose main working tree holds the absolute `path`; None when
    no such repo does: outside any repo, in a linked worktree, under
    <top>/.claude/worktrees/ (ticket worktrees, judged by their own branch)."""
    if path is None or path == "/dev" or path.startswith("/dev/"):
        return None
    directory = path if os.path.isdir(path) else existing_dir(os.path.dirname(path))
    names = resolved(path).split(os.sep)
    if ".git" in names:  # git's own files belong to the working tree around them
        directory = os.sep.join(names[: names.index(".git")]) or os.sep
    found = repo_at(state, directory)
    if found is None:
        return None
    rel = os.path.relpath(resolved(path), os.path.realpath(str(found[0])))
    if rel == ".." or rel.startswith("../"):
        return None
    if rel.replace(os.sep, "/").startswith(WORKTREES):
        return None
    return found


def part_refusal(state: dict, part: Part, name: str, args: list[str]) -> tuple | None:
    """(text, why, top, branch, advice) when `part` must not run; branch is None for the
    rules that hold on every branch."""
    cwd = state["cwd"]
    where, sub, rest = git_split(args, cwd) if name == "git" else (None, "", [])
    if name == "git" and is_force_push(sub, rest):
        why = "a force push rewrites history"
        return part.text, why, toplevel(where), None, FORCE_ADVICE
    for _, path, why, _ in writes(part, name, args, cwd):
        found = holder(state, path)
        if found is not None:
            return part.text, why, found[0], found[1], ADVICE
    if name == "git":
        there = repo_at(state, where)
        if there is not None:
            why = git_refusal(sub, rest, there[1], where)
            if why:
                return part.text, why, there[0], there[1], ADVICE
        elif sub == "push":
            why = push_target_refusal(rest)
            if why:
                return part.text, why, toplevel(where), None, PUSH_ADVICE
    here = repo_at(state, cwd)
    if here is not None:
        why = code_writes(name, args, part)
        if why:
            return part.text, why, here[0], here[1], ADVICE
    return None


def refusal(state: dict, command: str) -> tuple | None:
    """The first part of `command` to refuse, walking parts with the directory each runs
    in; None when every part may run."""
    for part, name, args in walk(state, command):
        found = part_refusal(state, part, name, args)
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
    text, why, top, branch, advice = found
    if allow_main(top):
        return
    if branch is None:
        deny(f'Refused "{shown(text)}": {why}. {advice}')
        return
    verify = verify_command(top) if top is not None else None
    if verify and command.strip() == verify:
        return
    name = top.name if top is not None else "this repo"
    deny(f'On {branch} in {name}. Refused "{shown(text)}": {why}. {advice}')


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
