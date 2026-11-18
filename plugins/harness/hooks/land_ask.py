#!/usr/bin/env python3
"""Retired PreToolUse hook (was matcher: Bash, #330): a silent stub; nothing asks before a land.

land_ask.py asked before a session landed a risk:high ticket's PR. Epic #383 (ticket #385) retired it:
a PR lands on its own once harness-pr land's gates pass, and hooks.json no longer wires this file.

It is kept only for sessions that started before the change. Hooks load at session start, so such a
session still runs `python3 .../land_ask.py`; with the file gone python3 would exit 2, and a PreToolUse
exit 2 blocks every Bash call in that session. So on any stdin it reads the event, drops it, prints
nothing and exits 0: no decision, no gh or git call, no harness_lib import.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import sys


def main() -> int:
    # Drain the event so Claude Code's write never meets a closed pipe; whatever it holds, it is not read.
    try:
        sys.stdin.buffer.read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
