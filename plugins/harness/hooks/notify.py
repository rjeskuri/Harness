#!/usr/bin/env python3
"""Notification hook: a macOS notification naming the repo when Claude Code needs you.

Matcher (hooks.json): permission_prompt|idle_prompt|agent_needs_input|agent_completed.
With several sessions open, the notification says which repo is waiting (the basename of
the session's cwd), so there is no need to poll windows.

How it notifies, in order:
  1. terminal-notifier, if it is on PATH (`brew install terminal-notifier`). Clicking the
     notification brings HARNESS_NOTIFY_APP to the front (default com.microsoft.VSCode;
     iTerm2 is com.googlecode.iterm2, Terminal is com.apple.Terminal). A newer notification
     from the same session replaces the older one (-group claude-code-<session_id>).
  2. AppleScript's `display notification`. macOS files these under Script Editor and shows
     nothing until Script Editor may notify: run
         osascript -e 'display notification "test"'
     once, then allow Script Editor in System Settings > Notifications.
  3. The terminal bell, which Claude Code writes on this hook's behalf.

HARNESS_NOTIFY_SOUND names a sound from /System/Library/Sounds (default Glass). Setting it,
or HARNESS_NOTIFY_APP, to an empty string turns that part off.

Always exits 0. Try it with:
    echo '{"message": "test", "cwd": "'"$PWD"'"}' | python3 hooks/notify.py

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.hookio import emit, read_input, run_fail_open
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"notify.py: cannot load harness_lib from {_LIB}: {exc}\n")
    sys.exit(0)

DEFAULT_TITLE = "Claude Code"
DEFAULT_MESSAGE = "Claude Code needs your attention"
DEFAULT_APP = "com.microsoft.VSCode"
DEFAULT_SOUND = "Glass"
MAX_TEXT = 300  # characters; notification banners show far less


def env_setting(name: str, default: str) -> str:
    """The variable's value, or the default when it is unset. An empty value means off."""
    value = os.environ.get(name)
    return default if value is None else value.strip()


def clean(text: object) -> str:
    """One line of plain text, trimmed to a length a notification can show."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= MAX_TEXT else line[: MAX_TEXT - 1] + "\u2026"


def succeeded(cmd: list[str]) -> bool:
    done = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=10
    )
    return done.returncode == 0


def via_terminal_notifier(
    title: str, subtitle: str, message: str, group: str, app: str, sound: str
) -> bool:
    exe = shutil.which("terminal-notifier")
    if not exe:
        return False
    cmd = [exe, "-title", title]
    if subtitle:
        cmd += ["-subtitle", subtitle]
    cmd += ["-message", message, "-group", group]
    if app:
        cmd += ["-activate", app]
    if sound:
        cmd += ["-sound", sound]
    return succeeded(cmd)


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def via_osascript(title: str, subtitle: str, message: str, sound: str) -> bool:
    exe = shutil.which("osascript")
    if not exe:
        return False
    script = (
        f"display notification {applescript_string(message)}"
        f" with title {applescript_string(title)}"
    )
    if subtitle:
        script += f" subtitle {applescript_string(subtitle)}"
    if sound:
        script += f" sound name {applescript_string(sound)}"
    return succeeded([exe, "-e", script])


def main() -> int:
    data = read_input()
    title = clean(data.get("title")) or DEFAULT_TITLE
    message = clean(data.get("message")) or DEFAULT_MESSAGE
    cwd = data.get("cwd")
    subtitle = clean(Path(cwd).name) if isinstance(cwd, str) and cwd else ""
    group = "claude-code-" + (clean(data.get("session_id")) or "session")
    app = env_setting("HARNESS_NOTIFY_APP", DEFAULT_APP)
    sound = env_setting("HARNESS_NOTIFY_SOUND", DEFAULT_SOUND)

    shown = False
    try:
        shown = via_terminal_notifier(title, subtitle, message, group, app, sound)
    except (OSError, subprocess.SubprocessError):
        shown = False
    if not shown:
        try:
            shown = via_osascript(title, subtitle, message, sound)
        except (OSError, subprocess.SubprocessError):
            shown = False
    if not shown:
        emit({"terminalSequence": "\u0007"})
    return 0


if __name__ == "__main__":
    # fail open: a broken hook never gets in the way
    sys.exit(run_fail_open("notify.py", main))
