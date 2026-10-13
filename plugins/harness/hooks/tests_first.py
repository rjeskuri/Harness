#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit): tests first, proven.

On a ticket branch (t/<N>-<slug>) every edited file is classified by its repo-relative
path: a test (HARNESS_TEST_PATTERNS), exempt (HARNESS_SOURCE_EXEMPT), or source. Then:
  - the test-author may not edit source; the coder and the test-breaker may not edit
    tests (the part of `agent_type` after the last ":" names the role);
  - a source edit, by anyone, needs a red run: ~/.claude/harness/repos/<key>/red/<slug>.json
    written by harness-red check. A ticket flagged no-tests needs none; a refactor needs a
    record of any kind; any other ticket needs a red (not green) record whose test files
    still hash to its test_hash. Without a ticket cache (tickets/<N>.json) the ticket
    counts as a task without flags;
  - test and exempt edits pass.
Outside a repo, on a detached HEAD or on any other branch it allows everything, except
that the role rules also apply in the test-breaker's scratch worktree
(~/.claude/harness/repos/<key>/scratch/<slug>, detached by design).

<key> is the first 12 hex digits of the sha1 of the real path of the main working tree,
so a linked worktree shares its main checkout's state. Settings come from the
environment first, then `env` in the worktree's .claude/settings.local.json, then its
.claude/settings.json, as harness-red reads them, so the two agree on what a test is.

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
CHANGED = "Tests changed since the red run; run harness-red check again."


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


def settings_env(root: Path) -> dict:
    """`env` from the project's settings files; settings.local.json wins over settings.json."""
    merged: dict = {}
    for name in ("settings.json", "settings.local.json"):
        data = read_json(root / ".claude" / name)
        env = data.get("env") if data else None
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


def role_refusal(role: str, kind: str) -> str | None:
    if role == "test-author" and kind == "source":
        return AUTHOR_ON_SOURCE
    if role == "coder" and kind == "test":
        return CODER_ON_TEST
    if role == "test-breaker" and kind == "test":
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
    files = record.get("test_files")
    if not isinstance(files, list) or not files:
        return CHANGED
    current = tests_hash(facts["top"], [str(f) for f in files])
    return None if current and current == record.get("test_hash") else CHANGED


def check(data: dict) -> None:
    if data.get("tool_name") not in ("Edit", "Write", "NotebookEdit"):
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw.strip():
        return
    cwd = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    path = os.path.join(cwd, os.path.expanduser(raw))
    facts = repo_facts(existing_dir(os.path.dirname(path)))
    if facts is None:
        return
    branch = facts["branch"]
    ticket = TICKET_BRANCH.match(branch) if branch else None
    scratch = branch is None and in_scratch(facts["top"])
    if ticket is None and not scratch:
        return
    rel = relative_path(path, facts["top"])
    if rel is None:
        return
    kind = classify(rel, settings_env(facts["top"]))
    agent = data.get("agent_type")
    role = agent.rsplit(":", 1)[-1] if isinstance(agent, str) else ""
    why = role_refusal(role, kind)
    if why is None and kind == "source" and ticket is not None:
        why = source_refusal(facts, str(int(ticket.group(1))))
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
