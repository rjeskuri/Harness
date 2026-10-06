#!/usr/bin/env python3
"""Notification hook (user settings): a macOS notification when Claude Code is waiting on you.

Useful once you run several sessions at a time: the notification names the repo or worktree
that needs a permission decision or has gone idle, so you don't have to poll terminals.

How it notifies, in order:
  1. terminal-notifier, if installed (`brew install terminal-notifier`). Clicking the
     notification brings ACTIVATE_APP (VS Code by default) to the front.
  2. AppleScript's `display notification`. macOS posts these as Script Editor, and shows
     nothing until Script Editor is allowed to notify: run
         osascript -e 'display notification "test"'
     once, then turn on Script Editor under System Settings > Notifications.
  3. The terminal bell, emitted by Claude Code on this hook's behalf.

Always exits 0. Test it by piping a payload in:
    echo '{"message": "test", "cwd": "'"$PWD"'"}' | python3 ~/.claude/hooks/notify.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SOUND = "Glass"  # a name from /System/Library/Sounds, or "" for silence
ACTIVATE_APP = "com.microsoft.VSCode"  # iTerm2: com.googlecode.iterm2, Terminal: com.apple.Terminal


def via_terminal_notifier(title: str, subtitle: str, message: str, group: str) -> bool:
    exe = shutil.which("terminal-notifier")
    if not exe:
        return False
    cmd = [
        exe,
        "-title",
        title,
        "-message",
        message,
        "-group",
        group,
        "-activate",
        ACTIVATE_APP,
    ]
    if subtitle:
        cmd += ["-subtitle", subtitle]
    if SOUND:
        cmd += ["-sound", SOUND]
    return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def via_osascript(title: str, subtitle: str, message: str) -> bool:
    exe = shutil.which("osascript")
    if not exe:
        return False
    script = f"display notification {applescript_string(message)} with title {applescript_string(title)}"
    if subtitle:
        script += f" subtitle {applescript_string(subtitle)}"
    if SOUND:
        script += f" sound name {applescript_string(SOUND)}"
    return (
        subprocess.run([exe, "-e", script], capture_output=True, timeout=10).returncode
        == 0
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    title = data.get("title") or "Claude Code"
    message = data.get("message") or "Claude Code needs your attention"
    subtitle = Path(data["cwd"]).name if data.get("cwd") else ""
    group = f"claude-code-{data.get('session_id') or 'session'}"

    try:
        shown = via_terminal_notifier(title, subtitle, message, group) or via_osascript(
            title, subtitle, message
        )
    except (OSError, subprocess.SubprocessError):
        shown = False
    if not shown:
        sys.stdout.write(json.dumps({"terminalSequence": "\u0007"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
