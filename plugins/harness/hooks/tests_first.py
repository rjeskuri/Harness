#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit|Bash): tests first, proven.

On a ticket branch (t/<N>-<slug>) every edited file is classified by its repo-relative
path: a test (HARNESS_TEST_PATTERNS), exempt (HARNESS_SOURCE_EXEMPT), or source. For the
role rules the files that decide which tests run and how (.claude/settings.json,
.claude/settings.local.json, pytest.ini, pyproject.toml, setup.cfg, tox.ini,
conftest.py, jest.config.*, vitest.config.*, package.json) count as tests. Then:
  - the test-author may not edit source; the coder and the test-breaker may not edit
    tests or that configuration (the part of `agent_type` after the last ":" names the
    role);
  - a source edit, by anyone, needs a red run: ~/.claude/harness/repos/<key>/red/<slug>.json
    written by harness-red check. A ticket flagged no-tests needs none; a refactor needs a
    record of any kind; any other ticket needs a red record (kind assertion, import or
    error) whose test files still hash to its test_hash, or are those files with main's
    changes merged in since (harness_lib/redproof.py). Without a ticket cache
    (tickets/<N>.json) the ticket counts as a task without flags;
  - test and exempt edits pass.
Outside a repo, on a detached HEAD or on any other branch it allows everything, except
that the role rules also apply in the test-breaker's scratch worktree
(~/.claude/harness/repos/<key>/scratch/<slug>, detached by design).

Bash: branch_guard.py's walker splits the command and follows its directory (cd, pushd,
subshells, git -C); every path a part writes (a redirection, tee, cp, mv, install, ln,
rsync, dd, rm, touch, truncate, chmod, sed/perl/awk in place, ed, patch with a named
file or -o, read inside its -d directory, git checkout or restore of paths, git rm,
git mv, git stash push and git clean of paths) is judged as an Edit of that path, except
that an untracked path git ignores (a cache, build output, a log) passes unless it is
test configuration. A git form that rewrites the working tree wholesale (checkout or
restore of a directory, a glob or pathspec magic, reset --hard or --merge, stash push,
pop, apply, drop and the others but list, show, create and store, and clean, unless
stash push or clean name only files) touches every test file of its worktree: each of
them (git ls-files, tracked or untracked and not ignored) and each piece of test
configuration goes through the role rules, so the coder and the test-breaker are
refused and the test-author is not; a worktree without either passes. For the
test-author, the coder and the test-breaker on a ticket branch, writes whose paths
cannot be seen are refused: an inline python or node program (-c, -e or a heredoc) that
writes files, patch without a file argument or -o (the diff names the files), and find
with -delete or an -exec, -execdir, -ok or -okdir command that writes files (as the
walker judges a part), runs git on paths, or is a shell or an interpreter.

<key> is the first 12 hex digits of the sha1 of the real path of the main working tree
with each component spelled as its parent directory lists it, so a linked worktree
shares its main checkout's state and every spelling of the path gives one key. HARNESS_TEST_PATTERNS and
HARNESS_SOURCE_EXEMPT come from the environment first, then `env` in the main
checkout's tracked .claude/settings.json; never from the worktree or from a
settings.local.json, which an agent could rewrite (a gitignored file is editable on main).

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0. Allowing
prints nothing. Fails open: any error of its own goes to stderr in one line, exit 0. But
the whole run keeps one deadline (HOOK_BUDGET, inside hooks.json's timeout), and when the
red proof, the check-ignore of a refused path or the file list of a wholesale rewrite
runs out of it, the edit is refused and says why; only the repo lookup fails open then.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.hookio import deny, edited_path, hook_cwd, read_input, run_fail_open
    from harness_lib.paths import adopt_old_state, canonical_path, path_key, repo_key, same_entry
    from harness_lib.redproof import record_state
    from harness_lib.repo import main_checkout
    from harness_lib.settings import DEFAULT_SOURCE_EXEMPT, DEFAULT_TEST_PATTERNS, glob_match, patterns, setting, settings_env
    from harness_lib.text import read_json
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"tests_first.py: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
    sys.exit(0)

GIT_TIMEOUT = 3
# One deadline for the whole run: hooks.json gives the hook 10 s, a hook killed at it prints nothing (the
# write goes through), and interpreter start and imports come out of the same 10 s. Every git call gets
# at most GIT_TIMEOUT of what is left, the red proof all of it.
HOOK_BUDGET = 8.5
DEADLINE = time.monotonic() + HOOK_BUDGET
TICKET_BRANCH = re.compile(r"^t/(\d+)-")
ROLES = {"test-author", "coder", "test-breaker"}
RED_KINDS = {"assertion", "import", "error"}
# Configuration that decides which tests run and how: tests, for the role rules.
TEST_CONFIG = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "conftest.py",
    "jest.config.*",
    "vitest.config.*",
    "package.json",
)
AUTHOR_ON_SOURCE = (
    "You write tests only on this ticket. Report the source change the implementation "
    "needs; the coder makes it."
)
CODER_ON_TEST = (
    "Tests are fixed for this ticket. If a test is wrong, stop and report it; the "
    "orchestrator sends it back to the test-author."
)
BREAKER_ON_TEST = (
    "Tests are fixed for this ticket: find the cheapest source change that passes them, "
    "and if there is none, report that."
)
CODER_ON_CONFIG = (
    "{rel} decides which tests run and how, so it is fixed for this ticket like the "
    "tests. If it has to change, stop and ask the orchestrator."
)
CHANGED = "Tests changed since the red run; run harness-red check again."
UNVERIFIED = (
    "Could not check the red record for t/{number} in time (git was too slow); the edit is "
    "refused, not judged. Retry it; harness-red status checks with a longer budget."
)
IGNORE_UNKNOWN = "(git did not say in time whether it ignores {rel}.)"
# git stash subcommands that leave the working tree alone.
STASH_READS = {"list", "show", "create", "store"}
# find's primaries that run a command for each file, and its options before the paths.
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_OPTIONS = {"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"}
# Interpreters, besides the shells, python and node: run by find -exec, they count as
# writers, since what their program writes cannot be seen.
INTERPRETERS = {"perl", "ruby", "php", "awk", "gawk", "lua", "Rscript", "osascript"}
INLINE = "Write files with Edit or Write so the tests-first rules can see the path."


# ---------- globs ----------


def classify(rel: str, file_env: dict) -> str:
    """ "test", "exempt" or "source"."""
    tests = patterns(
        setting("HARNESS_TEST_PATTERNS", file_env) or DEFAULT_TEST_PATTERNS
    )
    if any(glob_match(p, rel) for p in tests):
        return "test"
    exempt = setting("HARNESS_SOURCE_EXEMPT", file_env) or DEFAULT_SOURCE_EXEMPT
    if any(glob_match(p, rel) for p in patterns(exempt)):
        return "exempt"
    return "source"


# ---------- the repo holding the file ----------


def time_left() -> float:
    """Seconds left before the hook's DEADLINE."""
    return DEADLINE - time.monotonic()


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """git in `cwd`, held to GIT_TIMEOUT or what is left of the deadline. Raises TimeoutExpired."""
    left = min(GIT_TIMEOUT, time_left())
    if left <= 0:
        raise subprocess.TimeoutExpired(["git", "-C", cwd, *args], 0)
    return subprocess.run(
        ["git", "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=left,
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


def repo_facts(directory: str) -> dict | None:
    """top (the worktree's top-level) and main (the main working tree, main_checkout's,
    which moves state kept under the spelling git reports), both canonical paths, branch
    (None when detached) and spelled (`directory`, for adopt_old_state); None outside a
    repo. One git call gives top and HEAD's branch, main_checkout's lookup is held to one more
    GIT_TIMEOUT (or what is left of the deadline): at worst 2 x GIT_TIMEOUT, as on main. A
    lookup that runs out of time allows, like any failure of the hook's own (the branch is
    unknown). Only an unborn branch (that call exits 1 with the top alone) costs a third,
    symbolic-ref, call. The branch is spelled as
    `symbolic-ref --short HEAD` (harness-red's name) spells it: `--abbrev-ref=loose` shortens
    the same way, heads/<branch> when a tag or refs/<branch> shares the name; plain
    `--abbrev-ref` is strict, and also says heads/ for a clash with refs/remotes/<branch>."""
    out = git(directory, "rev-parse", "--show-toplevel", "--abbrev-ref=loose", "--verify", "-q", "HEAD")
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    if out.returncode not in (0, 1) or not lines or not lines[0]:
        return None
    top = lines[0]
    main = main_checkout(directory, timeout=min(GIT_TIMEOUT, time_left()))
    if main is None:
        return None
    if out.returncode == 0:  # the short branch name, or HEAD when detached
        ref = lines[1] if len(lines) > 1 else ""
        branch = "" if ref == "HEAD" else ref
    else:  # an unborn branch: HEAD names a branch with no commit yet
        head = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
        branch = head.stdout.strip() if head.returncode == 0 else ""
    return {
        "top": Path(canonical_path(top)),
        "main": Path(main),
        "branch": branch or None,
        "spelled": (directory,),
    }


def harness_home() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "harness"


def in_scratch(top: Path) -> bool:
    """True for a worktree under ~/.claude/harness/repos/<key>/scratch/."""
    repos = Path(canonical_path(harness_home() / "repos"))
    try:
        rel = top.relative_to(repos).parts
    except ValueError:
        return False
    return len(rel) >= 3 and rel[1] == "scratch"


def relative_path(path: str, top: Path) -> str | None:
    """`path` relative to the worktree `top`, with / separators; None when outside it.
    The existing part is spelled canonically, as `top` is, so another spelling of the
    worktree is not outside it."""
    base = existing_dir(os.path.dirname(path))
    tail = os.path.relpath(path, base)
    full = os.path.normpath(os.path.join(canonical_path(base), tail))
    rel = os.path.relpath(full, str(top))
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None
    return rel.replace(os.sep, "/")


# ---------- the rules ----------


def role_kind(rel: str, kind: str) -> str:
    """`kind` for the role rules: test configuration counts as "config"."""
    if kind != "test" and any(glob_match(p, rel) for p in TEST_CONFIG):
        return "config"
    return kind


def role_refusal(role: str, kind: str, rel: str = "") -> str | None:
    if role == "test-author" and kind == "source":
        return AUTHOR_ON_SOURCE
    if role == "coder" and kind == "test":
        return CODER_ON_TEST
    if role == "coder" and kind == "config":
        return CODER_ON_CONFIG.format(rel=rel)
    if role == "test-breaker" and kind in ("test", "config"):
        return BREAKER_ON_TEST
    return None


def source_refusal(facts: dict, number: str) -> str | None:
    """Why a source edit on ticket `number` must wait, or None when the proof is there."""
    repos = harness_home() / "repos"
    state = repos / adopt_old_state(str(repos), str(facts["main"]), *facts["spelled"])
    branch = facts["branch"]
    ticket = read_json(state / "tickets" / f"{number}.json")
    if not isinstance(ticket, dict):  # valid JSON that is not an object counts as absent
        ticket = None
    flags = []
    if ticket:
        for key in ("flags", "labels"):
            if isinstance(ticket.get(key), list):
                flags.extend(str(f) for f in ticket[key])
    if "no-tests" in flags:
        return None
    record_path = state / "red" / f"{branch.replace('/', '-')}.json"
    record = read_json(record_path)
    if not isinstance(record, dict):  # likewise: a red record that is not an object is no red run
        record = None
    if record is None:
        reason = (
            f"Tests first on t/{number}: write the tests, then run harness-red check. "
            "(No red run recorded.)"
        )
        if ticket is None:
            reason += f" There is no ticket cache either; harness-work start {number} writes it."
        return reason
    if ticket and ticket.get("type") == "refactor":
        return None
    if record.get("kind") == "green":
        return (
            f"The red run on t/{number} passed before any source change, so it proves "
            "nothing: make the tests fail against the current code, then run harness-red "
            "check again."
        )
    if record.get("kind") not in RED_KINDS:
        return (
            f"The red record for t/{number} does not say how the tests failed; run "
            "harness-red check again."
        )
    # The files hash to test_hash, or are the red-time files (kept in red-files/) with main's
    # merged changes (redproof.py), with what is left of the deadline. Fails closed: a proof that
    # runs out of time, or breaks, refuses the edit.
    try:
        state = record_state(str(facts["top"]), record, str(record_path), time_left())
    except Exception as exc:
        return f"Could not check the red record for t/{number} ({' '.join(str(exc).split())}); the edit is refused."
    if state == "current":
        return None
    return CHANGED if state == "stale" else UNVERIFIED.format(number=number)


def facts_at(directory: str, cache: dict) -> dict | None:
    if directory not in cache:
        cache[directory] = repo_facts(directory)
    return cache[directory]


def guarded(facts: dict | None):
    """The ticket-branch match, or True in the breaker's scratch worktree, when the
    rules apply in the repo of `facts`; None otherwise."""
    if facts is None:
        return None
    branch = facts["branch"]
    ticket = TICKET_BRANCH.match(branch) if branch else None
    if ticket is not None:
        return ticket
    return True if branch is None and in_scratch(facts["top"]) else None


def path_refusal(
    path: str, role: str, cache: dict, skip_ignored: bool = False
) -> str | None:
    """Why `role` may not write the absolute `path` now, or None. With `skip_ignored`, a
    path git ignores (untracked, matched by an ignore rule: caches, build output, logs)
    passes unless it is test configuration; when git cannot tell in time, the refusal stands."""
    facts = facts_at(existing_dir(os.path.dirname(path)), cache)
    ticket = guarded(facts)
    if ticket is None:
        return None
    rel = relative_path(path, facts["top"])
    if rel is None:
        return None
    # What a test is comes from the main checkout's tracked .claude/settings.json only:
    # worktree files and settings.local.json are agent-editable.
    kind = classify(rel, settings_env(facts["main"], ("settings.json",)))
    kind = role_kind(rel, kind)
    why = role_refusal(role, kind, rel)
    if why is None and kind == "source" and ticket is not True:
        why = source_refusal(facts, str(int(ticket.group(1))))
    if why and skip_ignored and kind != "config":
        try:
            out = git(str(facts["top"]), "check-ignore", "-q", "--", rel)
        except (OSError, subprocess.SubprocessError):
            return f"{why} {IGNORE_UNKNOWN.format(rel=rel)}"
        if out.returncode == 0:
            return None
    return why


def branch_guard():
    """branch_guard.py from this directory, for its command walker and write targets."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import branch_guard as guard

    return guard


def wide(guard, path: str, where: str | None) -> bool:
    """True for a pathspec that reaches files it does not name: a directory (on disk,
    relative to `where`), a glob or pathspec magic."""
    if path.startswith(":") or any(c in path for c in "*?["):
        return True
    full = guard.absolute(path, where)
    return full is not None and os.path.isdir(full)


def bulk_form(guard, sub: str, rest: list[str], where: str | None) -> bool:
    """True when git `sub` `rest` rewrites the working tree wholesale: checkout or
    restore of a directory, reset --hard or --merge, stash (push, pop, apply, drop and
    the rest that change files) and clean, unless they name only files."""
    if sub == "reset":
        return bool({"--hard", "--merge"} & set(rest))
    if sub == "stash" and guard.stash_action(rest) != "push":
        return guard.stash_action(rest) not in STASH_READS
    if sub == "clean" and guard.dry_run(rest):
        return False
    paths = guard.git_paths(sub, rest, where)
    if sub in ("stash", "clean") and not paths:
        return True
    return any(wide(guard, p, where) for p in paths)


def bulk_refusal(directory: str | None, role: str, cache: dict) -> str | None:
    """Why `role` may not rewrite every file of the worktree at `directory`: the role
    rules for each of its test files (tracked, or untracked and not ignored) and its
    test configuration. None off the guarded branches or without such files."""
    if role not in ROLES or directory is None:
        return None
    facts = facts_at(directory, cache)
    if guarded(facts) is None:
        return None
    top = str(facts["top"])
    try:
        out = git(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except (OSError, subprocess.SubprocessError):
        return f"git did not list the files of {top} in time, so this rewrite is refused; retry it."
    # The main checkout's tracked settings.json only, not settings.local.json (see path_refusal).
    file_env = settings_env(facts["main"], ("settings.json",))
    found = None
    for rel in sorted(set(out.stdout.split("\0")) - {""}):
        kind = role_kind(rel, classify(rel, file_env))
        if kind not in ("test", "config"):
            continue
        why = role_refusal(role, kind, rel)
        if why and kind == "test":
            return why
        found = found or why
    return found


def find_roots(args: list[str]) -> list[str]:
    """The starting points `find args` searches: the words before the expression."""
    roots, k = [], 0
    while k < len(args) and (args[k] in FIND_OPTIONS or args[k].startswith(("-O", "-D"))):
        k += 2 if args[k] == "-D" else 1  # -D takes a value, -O3 does not
    for a in args[k:]:
        if a.startswith("-") or a in ("(", "!"):
            break
        roots.append(a)
    return roots or ["."]


def exec_writes(guard, words: list[str], cwd: str | None) -> str | None:
    """Why the command find runs for each file, `words`, writes files, or None: a shell,
    an interpreter, or a command branch_guard sees writing."""
    inner = guard.command_words(words)
    if not inner:
        return None
    name, args = os.path.basename(inner[0]), inner[1:]
    if (
        name in guard.SHELLS
        or name == "eval"
        or guard.PYTHON.match(name)
        or name in guard.NODES
        or name in INTERPRETERS
    ):
        return f"{name}, which can write any file"
    part = guard.Part()
    part.words = words
    if guard.writes(part, name, args, cwd):
        return f"{name}, which writes files"
    if name == "git":
        where, sub, rest = guard.git_split(args, cwd)
        if guard.git_paths(sub, rest, where) or bulk_form(guard, sub, rest, where):
            return f"git {sub}, which writes files"
    if name == "find":
        return find_writes(guard, args, cwd)
    return None


def find_writes(guard, args: list[str], cwd: str | None) -> str | None:
    """Why `find args` writes files whose paths cannot be seen: -delete, or an -exec,
    -execdir, -ok or -okdir whose command (up to ; or a + after {}) writes files."""
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "-delete":
            return "find -delete removes the files it finds"
        if a not in FIND_EXEC:
            continue
        words: list[str] = []
        while k < len(args):
            w = args[k]
            k += 1
            if w == ";" or (w == "+" and words[-1:] == ["{}"]):
                break
            words.append(w)
        why = exec_writes(guard, words, cwd)
        if why:
            return f"find {a} runs {why}"
    return None


def unseen_writes(guard, part, name: str, args: list[str], here: str | None):
    """(why, the directories it writes in, None for one that cannot be resolved) when
    `part` writes files whose paths cannot be seen: an inline program, patch taking the
    files from its diff, find -exec; (None, []) otherwise. An inline program writes
    where it runs: no directories."""
    if name == "patch":
        found = guard.writes(part, name, args, here)
        hidden = [(p, why) for _, p, why, exact in found if not exact]
        if hidden:
            return hidden[0][1], [p for p, _ in hidden]
    if name == "find":
        why = find_writes(guard, args, here)
        if why:
            return why, [guard.absolute(r, here) for r in find_roots(args)]
    return guard.code_writes(name, args, part), []


def check_bash(command: str, cwd: str, role: str) -> None:
    guard = branch_guard()
    state = {"cwd": cwd, "repos": {}}
    cache: dict = {}
    for part, name, args in guard.walk(state, command):
        here = state["cwd"]
        paths = [p for _, p, _, exact in guard.writes(part, name, args, here) if exact]
        bulk, where = False, here
        if name == "git":
            where, sub, rest = guard.git_split(args, here)
            for p in guard.git_paths(sub, rest, where):
                paths.append(guard.absolute(p, where))
            bulk = bulk_form(guard, sub, rest, where)
        for path in paths:
            if path is None:
                continue
            why = path_refusal(path, role, cache, skip_ignored=True)
            if why:
                deny(f'Refused "{guard.shown(part.text)}": {why}')
                return
        if bulk:
            why = bulk_refusal(where, role, cache)
            if why:
                deny(f'Refused "{guard.shown(part.text)}": {why}')
                return
        if role in ROLES:
            why, dirs = unseen_writes(guard, part, name, args, here)
            places = [existing_dir(d) for d in dirs if d is not None]
            # where it runs, when that is where it writes or a directory is unknown
            if (not dirs or None in dirs) and here is not None and os.path.isdir(here):
                places.append(here)
            if why and any(guarded(facts_at(p, cache)) is not None for p in places):
                deny(f'Refused "{guard.shown(part.text)}": {why}. {INLINE}')
                return


def check(data: dict) -> None:
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    agent = data.get("agent_type")
    role = agent.rsplit(":", 1)[-1] if isinstance(agent, str) else ""
    cwd = hook_cwd(data)
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            check_bash(command, cwd, role)
        return
    if tool not in ("Edit", "Write", "NotebookEdit"):
        return
    raw = edited_path(data)
    if not isinstance(raw, str) or not raw.strip():
        return
    path = os.path.join(cwd, os.path.expanduser(raw))
    why = path_refusal(path, role, {})
    if why:
        deny(why)


def main() -> int:
    check(read_input())
    return 0


if __name__ == "__main__":
    # fail open: a broken guard must never trap the agent
    sys.exit(run_fail_open("tests_first.py", main))
