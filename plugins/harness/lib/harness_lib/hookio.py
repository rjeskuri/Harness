"""The hook I/O helpers the hooks share: the event read from stdin, the directory and the
edited path it names, the PreToolUse deny and any other JSON answer on stdout, and the
fail-open tail every hook's __main__ block runs. See docs/design.md (Layout).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import sys


def read_input() -> dict:
    """The hook event on stdin as a dict: {} for empty, invalid or non-object input."""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def hook_cwd(data: dict) -> str:
    """The event's cwd, else CLAUDE_PROJECT_DIR, else the process's own directory."""
    return str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def edited_path(data: dict):
    """The path an Edit, Write or NotebookEdit names (file_path, else notebook_path), as
    given; None when tool_input is not an object or names neither."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def emit(obj: dict) -> None:
    """Write `obj` to stdout as JSON (json.dumps defaults, no newline) and flush."""
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def deny(reason: str) -> None:
    """Refuse the PreToolUse call with `reason`, which Claude sees."""
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    emit({"hookSpecificOutput": decision})


def run_fail_open(name: str, main) -> int:
    """main()'s exit status (0 for None). An exception from main is one stderr line,
    "<name>: <error>", and status 0: a broken hook never blocks Claude."""
    try:
        code = main()
    except Exception as exc:
        sys.stderr.write(f"{name}: {exc}\n")
        return 0
    return 0 if code is None else code
