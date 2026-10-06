#!/usr/bin/env python3
"""Stop hook: before Claude finishes a turn in which it edited Python, lint the files it
edited and run the fast tests. If either fails, block the stop and hand Claude the
failures, so it fixes them now instead of telling you it's done.

When it runs
  Only if format_python.py recorded Python edits in this session, and only if the code
  changed since the last green check. Question-and-answer turns cost nothing. It also
  skips turns that end while background tasks are still running.

What it checks
  ruff: only the files Claude edited this session, so your own work-in-progress elsewhere
  in the tree doesn't get dragged in. Tests: the fast suite (or CLAUDE_VERIFY_CMD).

Loop safety
  Blocks at most MAX_CONSECUTIVE_BLOCKS times in a row, then lets Claude stop and warns
  you instead. (Claude Code itself force-ends a turn after 8 consecutive Stop-hook
  blocks.) An error inside this script never blocks Claude.

Customize with environment variables, e.g. in the "env" block of .claude/settings.json:
  CLAUDE_VERIFY_CMD      test command run through the shell ("uv run pytest -q -x tests/unit")
  CLAUDE_VERIFY_TIMEOUT  seconds before the test run is abandoned (default 300)
  CLAUDE_VERIFY_LINT     "0" skips the ruff check

Standard library only; works with the python3 from Xcode's command line tools (3.9) or
anything newer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_CONSECUTIVE_BLOCKS = 2
PY_SUFFIXES = (".py", ".pyi")
TAIL_LINES = 60


def git(root: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def repo_root(start: Path) -> Path | None:
    try:
        out = git(start, "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def state_dir(root: Path) -> Path:
    """Per-repo scratch directory shared with format_python.py (outside the repo)."""
    key = os.path.normcase(str(root.resolve())).encode("utf-8")
    d = (
        Path(tempfile.gettempdir())
        / "claude-hooks"
        / hashlib.sha1(key).hexdigest()[:12]
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_ruff(root: Path) -> list[str] | None:
    venv_ruff = root / ".venv" / "bin" / "ruff"
    if venv_ruff.is_file():
        return [str(venv_ruff)]
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    uvx = shutil.which("uvx")
    return [uvx, "ruff"] if uvx else None


def tail(text: str, n: int = TAIL_LINES) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join([f"... ({len(lines) - n} earlier lines omitted)", *lines[-n:]])


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def child_env() -> dict:
    # PYTHONDONTWRITEBYTECODE: no .pyc files that a same-second edit could leave stale.
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


# ---------- what changed ----------


def edited_files(dirty_flag: Path) -> list[Path]:
    """Python files format_python.py saw Claude edit this session (that still exist)."""
    try:
        lines = dirty_flag.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return sorted({Path(p) for p in lines if p.strip() and Path(p).is_file()})


def changed_python_files(root: Path) -> list[Path]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if out.returncode != 0:
        return []
    entries, files, i = out.stdout.split("\0"), [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # in -z output the rename/copy source path follows as its own field
        if rel.lower().endswith(PY_SUFFIXES) and (root / rel).is_file():
            files.append(root / rel)
    return files


def fingerprint(root: Path, files: list[Path]) -> str:
    """Identifies the current code state: HEAD plus the contents of changed files."""
    h = hashlib.sha256(git(root, "rev-parse", "HEAD").stdout.strip().encode())
    for f in sorted(set(files)):
        h.update(str(f).encode("utf-8"))
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()


# ---------- checks ----------


def run_lint(root: Path, files: list[Path]) -> tuple[str | None, bool]:
    """Return (problem report or None, whether ruff ran cleanly)."""
    if os.environ.get("CLAUDE_VERIFY_LINT") == "0" or not files:
        return None, False
    ruff = find_ruff(root)
    if ruff is None:
        return None, False
    cmd = [*ruff, "check", "--output-format", "concise", "--quiet", *map(str, files)]
    try:
        r = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    # Exit 1 means violations; 2 means ruff itself failed (bad config, very old ruff).
    if r.returncode == 1:
        return "ruff check reported:\n" + tail(r.stdout + r.stderr), False
    return None, r.returncode == 0


def has_tests(root: Path) -> bool:
    if any((root / p).exists() for p in ("tests", "test", "pytest.ini", "conftest.py")):
        return True
    pyproject = root / "pyproject.toml"
    try:
        return pyproject.is_file() and "[tool.pytest" in pyproject.read_text("utf-8")
    except OSError:
        return False


def test_command(root: Path):
    custom = os.environ.get("CLAUDE_VERIFY_CMD")
    if custom:
        return custom  # a string, run through the shell
    if not has_tests(root):
        return None
    args = ["pytest", "-q", "-x", "--tb=short"]
    uv = shutil.which("uv")
    if uv and (root / "uv.lock").is_file():
        return [uv, "run", "--frozen", *args]
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python), "-m", *args]
    return [sys.executable, "-m", *args]


def run_tests(root: Path) -> tuple[str | None, str | None, bool]:
    """Return (problem report or None, note for the user or None, whether tests passed)."""
    cmd = test_command(root)
    if cmd is None:
        return None, None, False
    timeout = int(os.environ.get("CLAUDE_VERIFY_TIMEOUT", "300"))
    try:
        r = subprocess.run(
            cmd,
            cwd=root,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        note = (
            f"verify_on_stop: tests took longer than {timeout}s and were skipped. "
            "Point CLAUDE_VERIFY_CMD at a faster subset."
        )
        return None, note, False
    except OSError as exc:
        return None, f"verify_on_stop: couldn't start the test command ({exc}).", False
    output = r.stdout + r.stderr
    if r.returncode in (0, 5):  # 5 = pytest collected no tests
        return None, None, r.returncode == 0
    if "No module named pytest" in output or "Failed to spawn: `pytest`" in output:
        return (
            None,
            "verify_on_stop: pytest isn't installed here; tests skipped.",
            False,
        )
    label = cmd if isinstance(cmd, str) else " ".join([Path(cmd[0]).name, *cmd[1:]])
    return f"`{label}` failed:\n" + tail(output), None, False


# ---------- main ----------


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        return 0
    sdir = state_dir(root)
    session = str(data.get("session_id") or "nosession")
    dirty_flag = sdir / f"dirty-{session}"
    if not dirty_flag.exists() or data.get("background_tasks"):
        return 0

    state_file = sdir / f"verify-{session}.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not data.get("stop_hook_active"):
        state["blocks"] = 0  # a fresh stop, not a continuation some Stop hook forced

    edited = edited_files(dirty_flag)
    fp = fingerprint(root, edited + changed_python_files(root))
    if fp == state.get("green"):
        return 0

    problems, notes, passed = [], [], []
    lint_problem, lint_ok = run_lint(root, edited)
    if lint_problem:
        problems.append(lint_problem)
    elif lint_ok:
        passed.append("ruff")
    test_problem, note, tests_ok = run_tests(root)
    if test_problem:
        problems.append(test_problem)
    elif tests_ok:
        passed.append("pytest")
    if note:
        notes.append(note)

    def save() -> None:
        try:
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    if not problems:
        state.update(green=fp, blocks=0)
        save()
        try:
            dirty_flag.unlink()
        except OSError:
            pass
        if passed:
            summary = f"verify_on_stop: {' and '.join(passed)} passed."
        else:
            summary = "verify_on_stop: nothing to run (no ruff, no tests found)."
        emit({"systemMessage": " ".join([summary, *notes])})
        return 0

    if state.get("blocks", 0) >= MAX_CONSECUTIVE_BLOCKS:
        state["blocks"] = 0
        save()
        warning = (
            f"verify_on_stop: still failing after {MAX_CONSECUTIVE_BLOCKS} automatic "
            "retries. Review the failures before trusting this turn."
        )
        emit({"systemMessage": warning})
        return 0

    state["blocks"] = state.get("blocks", 0) + 1
    save()
    reason = (
        "Before you finish: these checks fail after your edits.\n\n"
        + "\n\n".join(problems)
        + "\n\nFix the root cause and re-run the checks. Don't delete, skip or loosen "
        "tests to make them pass. If a failure predates your change or is out of scope, "
        "say so explicitly in your reply instead of working around it."
    )
    emit({"decision": "block", "reason": reason})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail open: a broken hook must never trap the session
        sys.stderr.write(f"verify_on_stop crashed: {exc}\n")
        sys.exit(0)
