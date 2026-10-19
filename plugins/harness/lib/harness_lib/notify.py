"""The one macOS notification every harness tool and hook sends: harness-board needs-input,
harness-pr ready, the digest and hooks/notify.py call notify() here.

How it notifies, in order:
  1. terminal-notifier, if it is on PATH (`brew install terminal-notifier`). Clicking the
     notification brings HARNESS_NOTIFY_APP to the front (default com.microsoft.VSCode;
     iTerm2 is com.googlecode.iterm2, Terminal is com.apple.Terminal). A newer notification
     with the same group replaces the older one.
  2. AppleScript's `display notification` through osascript. macOS files these under Script
     Editor and shows nothing until Script Editor may notify.
notify() answers whether either one showed the notification; the caller decides what to do
when neither did (hooks/notify.py rings the terminal bell).

HARNESS_NOTIFY_SOUND names a sound from /System/Library/Sounds (default Glass). Setting it,
or HARNESS_NOTIFY_APP, to an empty or blank string turns that part off.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_APP = "com.microsoft.VSCode"
DEFAULT_SOUND = "Glass"
TIMEOUT = 10  # seconds per notifier call


def env_setting(name: str, default: str) -> str:
    """The variable's value, stripped, or the default when it is unset. An empty value means off."""
    value = os.environ.get(name)
    return default if value is None else value.strip()


def succeeded(cmd: list[str]) -> bool:
    """Whether `cmd` exits 0, run with no stdin and its output captured."""
    done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    return done.returncode == 0


def applescript_string(text: str) -> str:
    """`text` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str, group: str, title: str = "Harness", subtitle: str = "") -> bool:
    """A macOS notification: terminal-notifier when on PATH, else osascript. Never raises;
    answers whether a notification was shown."""
    app = env_setting("HARNESS_NOTIFY_APP", DEFAULT_APP)
    sound = env_setting("HARNESS_NOTIFY_SOUND", DEFAULT_SOUND)
    try:
        exe = shutil.which("terminal-notifier")
        if exe:
            cmd = [exe, "-title", title]
            if subtitle:
                cmd += ["-subtitle", subtitle]
            cmd += ["-message", message, "-group", group]
            if app:
                cmd += ["-activate", app]
            if sound:
                cmd += ["-sound", sound]
            if succeeded(cmd):
                return True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        exe = shutil.which("osascript")
        if exe:
            script = f"display notification {applescript_string(message)} with title {applescript_string(title)}"
            if subtitle:
                script += f" subtitle {applescript_string(subtitle)}"
            if sound:
                script += f" sound name {applescript_string(sound)}"
            return succeeded([exe, "-e", script])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return False
