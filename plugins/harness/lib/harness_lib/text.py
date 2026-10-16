"""The text, time and file helpers the harness tools share: line pickers, gh list shapes, reading
and atomically writing files, paths under HOME, and the two ISO-8601 parsers (an aware datetime
where a naive time means UTC; epoch seconds where a naive time means local time).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile


# --------------------------------------------------------------- lines and lists
def one_line(text):
    """`text` (anything str() takes) with every run of whitespace collapsed to one space."""
    return " ".join(str(text).split())


def first_line(text):
    """The first non-blank line, stripped; "" when there is none."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def last_line(text):
    """The last non-blank line, stripped; "" when there is none."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def as_list(value):
    """gh lists come as a JSON list or as {"nodes": [...]}; anything else is empty."""
    if isinstance(value, dict):
        value = value.get("nodes")
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


# --------------------------------------------------------------- files
def read_text(path, what):
    """The file's text (`~` expanded, undecodable bytes replaced); Problem naming `what` and the path."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as err:
        from harness_lib.cli import Problem     # here, not at the top: cli imports this module
        raise Problem(f"cannot read {what} {path}: {err.strerror or err}") from None


def read_json(path):
    """The parsed JSON value of the file, or None when it cannot be read or parsed."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _replace(path, prefix, suffix, write):
    """Call write(fh) on a temporary file in the target's folder, then rename it over the target.
    On any failure the temporary file is removed and the error (OSError for the file system) raised."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data, *, prefix):
    """`data` as indented JSON (non-ASCII kept) and a newline, through a `prefix`ed temporary file."""

    def dump(fh):
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    _replace(path, prefix, ".json", dump)


def write_text(path, text, *, prefix):
    """`text` as given, through a `prefix`ed temporary file."""
    _replace(path, prefix, "", lambda fh: fh.write(text))


# --------------------------------------------------------------- paths
def home(*parts):
    """~/<parts>."""
    return os.path.join(os.path.expanduser("~"), *parts)


def harness_home(*parts):
    """~/.claude/harness/<parts>."""
    return home(".claude", "harness", *parts)


def same_path(a, b):
    """Whether two paths (`~` expanded, relative to the cwd) name the same real path."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(a))) == os.path.realpath(
        os.path.abspath(os.path.expanduser(b)))


def session_folder(path):
    """Claude Code's folder name for a project: its path with every non-alphanumeric as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# --------------------------------------------------------------- time
def now_iso():
    """The current UTC second, as 2026-10-16T12:34:56Z."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """A timezone-aware datetime from ISO-8601 (a trailing Z and a naive time both mean UTC), or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def parse_iso_seconds(text):
    """Seconds since the epoch for an ISO-8601 time (Z allowed; naive means local), or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        when = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.astimezone()).timestamp()


def age_text(then, now):
    """'just now', '5m ago', '3h ago', '2d ago', '4mo ago' or '2y ago'."""
    s = max(0, now - then)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    if s < 86400 * 60:
        return f"{int(s // 86400)}d ago"
    if s < 86400 * 730:
        return f"{int(s // (86400 * 30))}mo ago"
    return f"{int(s // (86400 * 365))}y ago"
