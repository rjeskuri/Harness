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
    error) whose test files still hash to its test_hash. Without a ticket cache
    (tickets/<N>.json) the ticket counts as a task without flags;
  - test and exempt edits pass.
Outside a repo, on a detached HEAD or on any other branch it allows everything, except
that the role rules also apply in the test-breaker's scratch worktree
(~/.claude/harness/repos/<key>/scratch/<slug>, detached by design).

Bash: branch_guard.py's walker splits the command and follows its directory (cd, pushd,
subshells, git -C); every path a part writes (a redirection, tee, cp, mv, install, ln,
rsync, dd, rm, touch, truncate, chmod, sed/perl/awk in place, ed, patch with a named
file, git checkout or restore of paths, git rm, git mv) is judged as an Edit of that
path, except that an untracked path git ignores (a cache, build output, a log) passes
unless it is test configuration. For the test-author, the coder and the test-breaker on
a ticket branch, an inline python or node program (-c, -e or a heredoc) that writes
files is refused, since its paths cannot be seen.

<key> is the first 12 hex digits of the sha1 of the real path of the main working tree,
so a linked worktree shares its main checkout's state. HARNESS_TEST_PATTERNS and
HARNESS_SOURCE_EXEMPT come from the environment first, then `env` in the main
checkout's tracked .claude/settings.json; never from the worktree or from a
settings.local.json, which an agent could rewrite (a gitignored file is editable on main).

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0. Allowing
prints nothing. Fails open: any error of its own goes to stderr in one line, exit 0.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT = 3
TICKET_BRANCH = re.compile(r"^t/(\d+)-")
DEFAULT_TEST_PATTERNS = (
    "tests/**,test/**,**/test_*.py,**/*_test.py,**/conftest.py,**/*_test.js,"
    "**/*.test.js,**/*.test.ts,**/*_test.ts,spec/**"
)
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
DEFAULT_SOURCE_EXEMPT = (
    "docs/**,**/*.md,.claude/**,.github/**,.githooks/**,**/*.toml,**/*.cfg,**/*.ini,"
    "**/*.json,**/*.yaml,**/*.yml,**/*.txt,**/*.lock,.gitignore,.worktreeinclude,"
    "LICENSE*"
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
INLINE = "Write files with Edit or Write so the tests-first rules can see the path."


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


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------- globs ----------


def glob_match(pattern: str, path: str) -> bool:
    """`path` (repo-relative, / separators) against `pattern`: split on /, `**` consumes
    zero or more segments, other segments use fnmatch; a pattern without / matches the
    basename."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return match_segments(pattern.split("/"), path.split("/"))


def match_segments(pats: list[str], segs: list[str]) -> bool:
    if not pats:
        return not segs
    if pats[0] == "**":
        return any(match_segments(pats[1:], segs[k:]) for k in range(len(segs) + 1))
    return bool(segs) and (
        fnmatch.fnmatchcase(segs[0], pats[0]) and match_segments(pats[1:], segs[1:])
    )


def patterns(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


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


# ---------- settings ----------


def classification_env(main_tree: Path) -> dict:
    """`env` from the main checkout's tracked .claude/settings.json, the only file that
    may say what a test is: worktree files and settings.local.json are agent-editable."""
    data = read_json(main_tree / ".claude" / "settings.json")
    env = data.get("env") if data else None
    return env if isinstance(env, dict) else {}


def setting(name: str, file_env: dict) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    value = file_env.get(name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


# ---------- the repo holding the file ----------


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


def repo_facts(directory: str) -> dict | None:
    """top (the worktree's top-level), main (the main working tree), branch (None when
    detached); None outside a repo."""
    out = git(directory, "rev-parse", "--show-toplevel", "--git-common-dir")
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    if out.returncode != 0 or len(lines) < 2 or not all(lines[:2]):
        return None
    common = os.path.realpath(os.path.join(directory, lines[1]))
    head = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    branch = head.stdout.strip() if head.returncode == 0 else ""
    return {
        "top": Path(os.path.realpath(lines[0])),
        "main": Path(os.path.dirname(common)),
        "branch": branch or None,
    }


def repo_key(main_tree: Path) -> str:
    raw = str(main_tree).encode("utf-8", "surrogateescape")
    return hashlib.sha1(raw).hexdigest()[:12]


def harness_home() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "harness"


def in_scratch(top: Path) -> bool:
    """True for a worktree under ~/.claude/harness/repos/<key>/scratch/."""
    repos = Path(os.path.realpath(harness_home() / "repos"))
    try:
        rel = top.relative_to(repos).parts
    except ValueError:
        return False
    return len(rel) >= 3 and rel[1] == "scratch"


def relative_path(path: str, top: Path) -> str | None:
    """`path` relative to the worktree `top`, with / separators; None when outside it."""
    base = existing_dir(os.path.dirname(path))
    tail = os.path.relpath(path, base)
    full = os.path.normpath(os.path.join(os.path.realpath(base), tail))
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


def tests_hash(top: Path, files: list[str]) -> str | None:
    """sha256 over each file's path, a newline and its bytes, in sorted path order; None
    when a file cannot be read."""
    h = hashlib.sha256()
    for name in sorted(files):
        try:
            content = (top / name).read_bytes()
        except OSError:
            return None
        h.update(name.encode("utf-8", "surrogateescape") + b"\n")
        h.update(content)
    return h.hexdigest()


def source_refusal(facts: dict, number: str) -> str | None:
    """Why a source edit on ticket `number` must wait, or None when the proof is there."""
    state = harness_home() / "repos" / repo_key(facts["main"])
    branch = facts["branch"]
    ticket = read_json(state / "tickets" / f"{number}.json")
    flags = []
    if ticket:
        for key in ("flags", "labels"):
            if isinstance(ticket.get(key), list):
                flags.extend(str(f) for f in ticket[key])
    if "no-tests" in flags:
        return None
    record = read_json(state / "red" / f"{branch.replace('/', '-')}.json")
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
    files = record.get("test_files")
    if not isinstance(files, list) or not files:
        return CHANGED
    current = tests_hash(facts["top"], [str(f) for f in files])
    return None if current and current == record.get("test_hash") else CHANGED


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
    passes unless it is test configuration."""
    facts = facts_at(existing_dir(os.path.dirname(path)), cache)
    ticket = guarded(facts)
    if ticket is None:
        return None
    rel = relative_path(path, facts["top"])
    if rel is None:
        return None
    kind = classify(rel, classification_env(facts["main"]))
    kind = role_kind(rel, kind)
    why = role_refusal(role, kind, rel)
    if why is None and kind == "source" and ticket is not True:
        why = source_refusal(facts, str(int(ticket.group(1))))
    if why and skip_ignored and kind != "config":
        out = git(str(facts["top"]), "check-ignore", "-q", "--", rel)
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


def check_bash(command: str, cwd: str, role: str) -> None:
    guard = branch_guard()
    state = {"cwd": cwd, "repos": {}}
    cache: dict = {}
    for part, name, args in guard.walk(state, command):
        here = state["cwd"]
        paths = [p for _, p, _, exact in guard.writes(part, name, args, here) if exact]
        if name == "git":
            where, sub, rest = guard.git_split(args, here)
            for p in guard.git_paths(sub, rest, where):
                paths.append(guard.absolute(p, where))
        for path in paths:
            if path is None:
                continue
            why = path_refusal(path, role, cache, skip_ignored=True)
            if why:
                deny(f'Refused "{guard.shown(part.text)}": {why}')
                return
        if role in ROLES and here is not None and os.path.isdir(here):
            why = guard.code_writes(name, args, part)
            if why and guarded(facts_at(here, cache)) is not None:
                deny(f'Refused "{guard.shown(part.text)}": {why}. {INLINE}')
                return


def check(data: dict) -> None:
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    agent = data.get("agent_type")
    role = agent.rsplit(":", 1)[-1] if isinstance(agent, str) else ""
    cwd = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            check_bash(command, cwd, role)
        return
    if tool not in ("Edit", "Write", "NotebookEdit"):
        return
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
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
    try:
        main()
    except Exception as exc:  # fail open: a broken guard must never trap the agent
        sys.stderr.write(f"tests_first.py: {exc}\n")
    sys.exit(0)
