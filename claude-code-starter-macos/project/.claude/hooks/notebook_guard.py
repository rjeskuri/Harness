#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit): steer notebook edits to the paired .py.

If Claude tries to edit `analysis.ipynb` and a jupytext-paired `analysis.py` (or
`analysis.pct.py`) sits next to it, the edit is denied with a message telling Claude to
edit the .py and sync. Notebook JSON is noisy to edit, drags cell outputs into Claude's
context, and produces unreadable diffs; the paired .py is plain code.

If there's no paired file, the hook does nothing, so it's harmless in repos that don't
use jupytext. Standard library only; Python 3.8+.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HEAD_BYTES = 4096


def jupytext_configured(start: Path) -> bool:
    for d in (start, *start.parents):
        for name in ("jupytext.toml", ".jupytext.toml", ".jupytext"):
            if (d / name).is_file():
                return True
        pyproject = d / "pyproject.toml"
        if pyproject.is_file():
            try:
                if "[tool.jupytext]" in pyproject.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
        if (d / ".git").exists():
            break
    return False


def is_paired(py_file: Path) -> bool:
    try:
        with py_file.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return False
    # jupytext writes a metadata header at the top of paired files; without one, fall back
    # to percent-format cell markers plus a jupytext config in the project.
    if "jupytext" in head:
        return True
    return "# %%" in head and jupytext_configured(py_file.parent)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_input = data.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw.lower().endswith(".ipynb"):
        return 0

    nb = Path(raw)
    if not nb.is_absolute():
        nb = Path(data.get("cwd") or ".") / nb

    for candidate in (nb.with_suffix(".py"), nb.with_name(nb.stem + ".pct.py")):
        if candidate.is_file() and is_paired(candidate):
            reason = (
                f"{nb.name} is paired with {candidate.name} via jupytext. Edit {candidate.name} "
                f'instead, then run `uv run jupytext --sync "{candidate.as_posix()}"` to update '
                "the notebook. Keep logic in src/ and import it from the notebook."
            )
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
            return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail open
        sys.exit(0)
