#!/usr/bin/env python3
"""PostToolUse hook (matcher: Edit|Write): tidy a Python file right after Claude edits it.

What it does
  1. Runs `ruff format` and the safe `ruff check --fix` autofixes on the edited file only.
     Unused-import removal (F401) is excluded, because Claude often adds an import one
     edit before the code that uses it.
  2. Records the edited path for this session, so verify_on_stop.py knows the turn
     needs a lint + test check before Claude reports it's done, and which files to lint.

What it deliberately doesn't do
  It never blocks or sends lint errors mid-edit. Half-finished code trips lint rules,
  and interrupting Claude between edits derails it. Lint problems are reported once,
  at the end of the turn, by verify_on_stop.py.

Ruff lookup order: the project's .venv, then `ruff` on PATH (Homebrew or `uv tool install
ruff`), then `uvx ruff`. Fails open: if ruff is missing or anything goes wrong, it exits 0
and does nothing. Standard library only; works with the python3 from Xcode's command line
tools (3.9) or anything newer.
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

PY_SUFFIXES = (".py", ".pyi")


def repo_root(start: Path) -> Path:
    """Git top-level for `start`, or `start` itself outside a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def state_dir(root: Path) -> Path:
    """Per-repo scratch directory shared with verify_on_stop.py (outside the repo)."""
    key = hashlib.sha1(
        os.path.normcase(str(root.resolve())).encode("utf-8")
    ).hexdigest()[:12]
    d = Path(tempfile.gettempdir()) / "claude-hooks" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_ruff(start: Path) -> list[str] | None:
    for d in (start, *start.parents):
        candidate = d / ".venv" / "bin" / "ruff"
        if candidate.is_file():
            return [str(candidate)]
        if (d / ".git").exists():
            break
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "ruff"]
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_input = data.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or ""
    if not raw_path.lower().endswith(PY_SUFFIXES):
        return 0

    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    if not path.is_file():
        return 0

    # Record the edit first, so the end-of-turn check still runs if ruff is unavailable.
    session = str(data.get("session_id") or "nosession")
    try:
        flag = state_dir(repo_root(cwd)) / f"dirty-{session}"
        with flag.open("a", encoding="utf-8") as fh:
            fh.write(str(path.resolve()) + "\n")
    except OSError:
        pass

    ruff = find_ruff(path.parent)
    if ruff is None:
        return 0

    try:
        subprocess.run(
            [*ruff, "format", "--quiet", str(path)], capture_output=True, timeout=45
        )
        subprocess.run(
            [*ruff, "check", "--fix", "--quiet", "--unfixable", "F401", str(path)],
            capture_output=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
