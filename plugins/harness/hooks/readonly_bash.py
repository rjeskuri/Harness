#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): keeps the reviewer and researcher agents read-only.

Plugin subagents ignore the `hooks` frontmatter field, so this guard ships in the plugin's
hooks/hooks.json and runs before every Bash call, but acts only when the hook input's
`agent_type` is harness:reviewer or harness:researcher (or reviewer or researcher, for
copies of those agents outside the plugin). The field is absent in the main session, so
the main session and every other agent pass through untouched.

For a guarded agent it splits the command into simple commands (on &&, ||, ;, |, &,
newlines and parentheses, and inside $(...), backticks, sh -c strings and find -exec) and
refuses the call when one of them would change files, git state, packages or processes:
  - an output redirection (>, >>, &>) to anything but /dev/null;
  - rm, rmdir, unlink, mv, cp, tee, touch, chmod, chown, chgrp, mkdir, ln, truncate, dd,
    kill, pkill or killall; sed -i or perl -i; find -delete;
  - git, except diff, log, show, status, blame, ls-files, rev-parse, describe, cat-file,
    grep, shortlog, ls-tree, merge-base, rev-list, show-ref, for-each-ref, diff-tree,
    range-diff, version and help, and the listing forms of branch, tag, remote, config,
    worktree and stash (list, show); --output is refused everywhere;
  - install, add, remove, uninstall, update, upgrade, sync, lock, venv, link or publish
    with pip, uv, npm, pnpm, yarn, bun, brew, cargo, poetry, gem, conda or pipx, and
    python -m pip install;
  - python -c, or a python program read from a heredoc, that calls open() with a w, a, x
    or + mode or a pathlib, os or shutil function that writes, moves or deletes;
  - make with no target, or a target other than check, test or lint.
Leading VAR=value assignments and wrappers such as env, xargs, nohup and timeout are
looked through; a heredoc body is data unless a shell or python reads it as its program.
Everything else runs, test runners included.

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0; Claude
sees the reason, which names the part and the rule. This guards against mistakes; it is
not a sandbox, and a test or a script can still write files.

Fails open: any error of its own exits 0, which allows the call.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import sys

GUARDED = {
    "harness:planner",
    "harness:reviewer",
    "harness:researcher",
    "planner",
    "reviewer",
    "researcher",
}
MAX_SHOWN = 160
MAX_DEPTH = 8

WRITERS = set(
    "rm rmdir unlink mv cp tee touch chmod chown chgrp mkdir ln truncate dd".split()
)
KILLERS = {"kill", "pkill", "killall"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "nice", "timeout", "xargs"}
KEYWORDS = set("! { } if then elif else fi do done while until time".split())

GIT_READERS = set(
    "diff log show status blame ls-files rev-parse describe cat-file grep shortlog "
    "ls-tree merge-base rev-list show-ref for-each-ref diff-tree range-diff version "
    "help".split()
)
GIT_VALUE_OPTIONS = set("-C -c --git-dir --work-tree --namespace --config-env".split())
BRANCH_WRITES = set(
    "-d -D --delete -m -M --move -c -C --copy -f --force -u --set-upstream-to "
    "--unset-upstream --edit-description -t --track --no-track --create-reflog".split()
)
TAG_WRITES = set(
    "-d --delete -a --annotate -s --sign -f --force -m --message -F --file -u "
    "--local-user".split()
)
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}

PACKAGE_TOOLS = set(
    "pip pip3 uv npm pnpm yarn bun brew cargo poetry gem conda pipx".split()
)
PACKAGE_WRITES = set(
    "install reinstall i add remove rm uninstall un update upgrade sync lock venv ci "
    "link unlink publish".split()
)
# Groups whose next word is the verb: uv pip install, uv tool install, uv self update.
PACKAGE_GROUPS = {"pip", "tool", "python", "self"}

MAKE_TARGETS = {"check", "test", "lint"}
MAKE_VALUE_OPTIONS = set(
    "-C -f -I -o -W --directory --file --makefile --include-dir --old-file "
    "--new-file --what-if --assume-old --assume-new".split()
)
MAKE_COUNT_OPTIONS = {"-j", "-l", "--jobs", "--load-average"}

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
NUMBER = re.compile(r"\d+(\.\d+)?[smhd]?$")
PYTHON = re.compile(r"python[0-9.]*$")
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
OPEN_CALL = re.compile(r"\bopen\s*\(")
WRITE_MODE = re.compile(r"""(?:,|\bmode\s*=)\s*(["'])([rwxabt+]{1,4})\1""")
PY_WRITERS = re.compile(
    r"\.(write_text|write_bytes|unlink|rmdir|mkdir|touch|symlink_to)\s*\("
    r"|\b(os|shutil)\.(remove|unlink|rmdir|removedirs|rename|renames|replace|makedirs"
    r"|mkdir|truncate|chmod|chown|symlink|link|rmtree|move|copy|copy2|copyfile"
    r"|copytree)\s*\("
)


def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------- splitting a command ----------


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


# ---------- judging one simple command ----------


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
    """True when a shell or python given `args` reads its program from stdin."""
    for a in args:
        if a == "-":
            return True
        if not a.startswith("-") or a.startswith(("-c", "-m")):
            return False
    return True


def git_lists(sub: str, rest: list[str]) -> bool:
    """The listing forms of git subcommands that can also write."""
    options = {a.split("=", 1)[0] for a in rest if a.startswith("-")}
    plain = [a for a in rest if not a.startswith("-")]
    if sub == "branch":
        listing = "--list" in options or "-l" in options or not plain
        return listing and not options & BRANCH_WRITES
    if sub == "tag":
        listing = "--list" in options or "-l" in options or not rest
        return listing and not options & TAG_WRITES
    if sub == "stash":
        return plain[:1] in (["list"], ["show"])
    if sub == "remote":
        return not plain or plain[0] in ("show", "get-url")
    if sub == "config":
        return bool(options & CONFIG_READS)
    if sub == "worktree":
        return plain[:1] == ["list"]
    return False


def git_refusal(args: list[str]) -> str | None:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if args[k] in GIT_VALUE_OPTIONS else 1
    if k >= len(args):
        return None
    sub, rest = args[k], args[k + 1 :]
    if any(a == "--output" or a.startswith("--output=") for a in rest):
        return f"git {sub} --output writes a file"
    if sub in GIT_READERS or git_lists(sub, rest):
        return None
    return f"git {sub} can change files or git state"


def package_refusal(name: str, args: list[str], depth: int) -> str | None:
    plain = [a for a in args if not a.startswith("-")]
    if plain[:1] in (["run"], ["exec"]):  # uv run pytest: judge the command it runs
        rest = args[args.index(plain[0]) + 1 :]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        words = command_words(rest)
        return words_refusal(words, depth + 1) if words else None
    verbs = plain[1:2] if plain[:1] and plain[0] in PACKAGE_GROUPS else plain[:1]
    if verbs and verbs[0] in PACKAGE_WRITES:
        return f"{name} {' '.join(plain[: plain.index(verbs[0]) + 1])} changes packages"
    return None


def code_refusal(code: str) -> str | None:
    """Why a python program writes files, or None."""
    for m in OPEN_CALL.finditer(code):
        call = code[m.end() - 1 : closing(code, m.end() - 1) + 1]
        for mode in WRITE_MODE.finditer(call):
            if set(mode.group(2)) & set("wax+"):
                return "python opens a file for writing"
    if PY_WRITERS.search(code):
        return "python writes, moves or deletes files"
    return None


def python_refusal(args: list[str]) -> str | None:
    for k, a in enumerate(args):
        if a == "-c":
            return code_refusal(args[k + 1]) if k + 1 < len(args) else None
        if a.startswith("-c"):
            return code_refusal(a[2:])
        if a == "-m":
            module, verb = args[k + 1 : k + 2], args[k + 2 : k + 3]
            if module == ["pip"] and verb and verb[0] in PACKAGE_WRITES:
                return f"python -m pip {verb[0]} changes packages"
            return None
        if not a.startswith("-"):
            return None  # a script: what it does is up to the script
    return None


def make_refusal(args: list[str]) -> str | None:
    targets, k = [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a in MAKE_VALUE_OPTIONS:
            k += 1
        elif a in MAKE_COUNT_OPTIONS:
            if k < len(args) and NUMBER.match(args[k]):
                k += 1
        elif not (a.startswith("-") or ASSIGNMENT.match(a)):
            targets.append(a)
    if not targets:
        return "make without a target runs the default build"
    other = [t for t in targets if t not in MAKE_TARGETS]
    return f"make {other[0]} is not check, test or lint" if other else None


def find_refusal(args: list[str], depth: int) -> str | None:
    for k, a in enumerate(args):
        if a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
            return f"find {a} deletes or writes files"
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            sub = []
            for b in args[k + 1 :]:
                if b in (";", "+"):
                    break
                sub.append(b)
            words = command_words(sub)
            why = words_refusal(words, depth + 1) if words else None
            if why:
                return why
    return None


def words_refusal(words: list[str], depth: int) -> str | None:
    """Why the command `words` (name first) would change something, or None."""
    name, args = os.path.basename(words[0]), words[1:]
    if name in WRITERS:
        return f"{name} changes files"
    if name in KILLERS:
        return f"{name} stops processes"
    if name == "sed" and any(SED_IN_PLACE.match(a) for a in args):
        return "sed -i edits files in place"
    if name == "perl" and any(PERL_IN_PLACE.match(a) for a in args):
        return "perl -i edits files in place"
    if name == "find":
        return find_refusal(args, depth)
    if name == "git":
        return git_refusal(args)
    if name in PACKAGE_TOOLS:
        return package_refusal(name, args, depth)
    if PYTHON.match(name):
        return python_refusal(args)
    if name == "make":
        return make_refusal(args)
    if name in SHELLS or name == "eval":
        if name == "eval":
            script: str | None = " ".join(args)
        else:
            flags = [
                k
                for k, a in enumerate(args)
                if a.startswith("-") and not a.startswith("--") and "c" in a[1:]
            ]
            script = args[flags[0] + 1] if flags and flags[0] + 1 < len(args) else None
        found = refusal(script, depth + 1) if script is not None else None
        return found[1] if found else None
    return None


def part_refusal(part: Part, depth: int) -> str | None:
    for target in part.targets:
        if target != "/dev/null":
            return f"it writes to {target or 'a file'} through a redirection"
    words = command_words(part.words)
    if not words:
        return None
    why = words_refusal(words, depth)
    name, args = os.path.basename(words[0]), words[1:]
    for body in part.heredocs:
        if why or not stdin_program(args):
            break
        if name in SHELLS:
            found = refusal(body, depth + 1)
            why = found[1] if found else None
        elif PYTHON.match(name):
            why = code_refusal(body)
    return why


def refusal(command: str, depth: int = 0) -> tuple[str, str] | None:
    """The first simple command in `command` that would change something, and why."""
    if depth > MAX_DEPTH:
        return None
    for part in split_command(command):
        why = part_refusal(part, depth)
        if why:
            return part.text, why
    return None


# ---------- main ----------


def main() -> int:
    data = read_input()
    agent = data.get("agent_type")
    if not isinstance(agent, str) or agent not in GUARDED:
        return 0  # the main session, or an agent that may write
    if data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    found = refusal(command)
    if found is None:
        return 0
    text, why = found
    if len(text) > MAX_SHOWN:
        text = text[: MAX_SHOWN - 1] + "\u2026"
    role = agent.split(":")[-1]
    reason = (
        f'Refused "{text}": {why}. The {role} only reads, searches, diffs and runs '
        "tests, so a hook refuses Bash commands that would change files or git state; "
        "describe the change instead of making it."
    )
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    sys.stdout.write(json.dumps({"hookSpecificOutput": decision}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open: a broken guard must never trap the agent
        sys.stderr.write(f"readonly_bash.py: {exc}\n")
    sys.exit(0)
