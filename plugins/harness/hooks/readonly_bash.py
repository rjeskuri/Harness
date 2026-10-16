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
Everything else runs, test runners included. The splitting is harness_lib.shellwalk's, the
walker branch_guard.py uses too; its marker parts around a subshell or a substitution have
no words and no targets, so they refuse nothing and the commands inside them are judged.

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0; Claude
sees the reason, which names the part and the rule. This guards against mistakes; it is
not a sandbox, and a test or a script can still write files.

Fails open: any error of its own exits 0, which allows the call; a lib it cannot load says
so on stderr in one line first.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import sys

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.shellwalk import (
        ASSIGNMENT,
        GIT_VALUE_OPTIONS,
        MAX_DEPTH,
        NUMBER,
        PYTHON,
        SED_IN_PLACE,
        SHELLS,
        Part,
        closing,
        command_words,
        split_command,
        stdin_program,
    )
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"readonly_bash.py: cannot load harness_lib from {_LIB}: {exc}\n")
    sys.exit(0)

GUARDED = {
    "harness:planner",
    "harness:reviewer",
    "harness:researcher",
    "planner",
    "reviewer",
    "researcher",
}
MAX_SHOWN = 160

WRITERS = set(
    "rm rmdir unlink mv cp tee touch chmod chown chgrp mkdir ln truncate dd".split()
)
KILLERS = {"kill", "pkill", "killall"}

GIT_READERS = set(
    "diff log show status blame ls-files rev-parse describe cat-file grep shortlog "
    "ls-tree merge-base rev-list show-ref for-each-ref diff-tree range-diff version "
    "help".split()
)
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

PERL_IN_PLACE = re.compile(r"-[aclnpsw]*i")
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


# ---------- judging one simple command ----------


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
    """Why `part` would change something, or None: for a marker part (no words, no
    targets) always None."""
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
