#!/usr/bin/env python3
"""PostToolUse hook (matcher: Edit|Write|NotebookEdit|Bash): bookkeeping and a secret scan.

1. Records the edited file's absolute path in <state>/dirty-<session_id>, where <state> is
   $TMPDIR/claude-harness/<first 12 hex of the sha1 of the repo path>, so
   verify_on_stop.py knows a verify run is due and which files to fingerprint.
2. Scans the edited file (when it exists, is under 2 MB and decodes as UTF-8) for
   high-confidence secret patterns. On a hit it prints the location to stderr and exits 2,
   which Claude Code shows to Claude so it removes the secret right away. The edit has
   already happened; the scan makes sure it does not go unnoticed.

A Bash call can change files without naming them (sed -i, redirects, formatters, scripts),
so after one it does both for the files `git status` lists (modified, added, renamed, copied
or untracked) that are regular files under 2 MB modified since this hook's last Bash scan in
the session, whose start time it keeps in <state>/bash-scan-<session_id> (the first scan
looks back 10 minutes): newest first, at most 100. Outside a git repo, or when git fails, a
Bash call is left alone.

A line that looks like a placeholder (it contains example, placeholder, dummy, xxxx, <, ${,
your_ or REDACTED, in any case) is never reported. Turn the scan off with
HARNESS_SECRET_SCAN=0, or for one file by putting `harness: allow-secrets` anywhere in it
(test fixtures, documentation of key formats).

Fails open: any error of its own exits 0. Exit 2 means a confirmed hit, nothing else.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_REPORTED = 10
MAX_BASH_FILES = 100
FIRST_BASH_WINDOW = 600.0  # seconds a session's first Bash scan looks back
STATUS_TIMEOUT = 8
ALLOW_MARKER = "harness: allow-secrets"
SKIP_MARKERS = (
    "example",
    "placeholder",
    "dummy",
    "xxxx",
    "<",
    "${",
    "your_",
    "redacted",
)

# (name, pattern), most specific first; a line is reported once, under the first match.
# The sk- patterns may not follow a letter, digit, "_" or "-", so kebab-case words that
# end in "sk" ("task-", "risk-", "flask-") are not mistaken for keys. Only the generic
# assignment matches in any case (API_KEY = "...", apiKey: "..."); the provider formats
# are case-sensitive.
SECRET_PATTERNS = [
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("Slack token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
    ("Anthropic API key", r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI API key", r"(?<![A-Za-z0-9_-])sk-(proj-)?[A-Za-z0-9_-]{20,}"),
    ("Stripe live secret key", r"(?<![A-Za-z0-9_-])sk_live_[A-Za-z0-9]{16,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Jotform-style apiKey", r"apiKey=[0-9a-f]{32}"),
    (
        "generic secret assignment",
        r"""(api[_-]?key|secret|token|password)\s*[:=]\s*["'][A-Za-z0-9_\-]{24,}["']""",
    ),
]
IGNORE_CASE = {"generic secret assignment"}
COMPILED = [
    (name, re.compile(pattern, re.IGNORECASE if name in IGNORE_CASE else 0))
    for name, pattern in SECRET_PATTERNS
]


def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def git_toplevel(start: Path) -> Path | None:
    """Git top-level for `start`, or None outside a repo or when git fails."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def repo_root(start: Path) -> Path:
    """Git top-level for `start`, or `start` itself outside a repo."""
    return git_toplevel(start) or start


def state_dir(root: Path) -> Path:
    """Per-repo scratch directory shared with verify_on_stop.py (outside the repo)."""
    key = os.path.normcase(str(root.resolve())).encode("utf-8", "surrogateescape")
    d = (
        Path(tempfile.gettempdir())
        / "claude-harness"
        / hashlib.sha1(key).hexdigest()[:12]
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_key(data: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(data.get("session_id") or "nosession"))


def record_edit(cwd: Path, path: Path, data: dict) -> None:
    if "\n" in str(path):
        return
    record(repo_root(cwd), [path], data)


def record(root: Path, paths: list[Path], data: dict) -> None:
    """Append `paths` to <state>/dirty-<session_id>, skipping any with a newline in it."""
    lines = "".join(f"{p}\n" for p in paths if "\n" not in str(p))
    if not lines:
        return
    try:
        flag = state_dir(root) / f"dirty-{session_key(data)}"
        with flag.open("a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(lines)
    except OSError:
        pass


def scan(text: str) -> list[tuple[int, str, str]]:
    """(line number, pattern name, matched text) for each line holding a secret."""
    candidates = [(name, rx) for name, rx in COMPILED if rx.search(text)]
    if not candidates:
        return []
    hits = []
    for number, line in enumerate(text.split("\n"), start=1):
        lowered = line.lower()
        if any(marker in lowered for marker in SKIP_MARKERS):
            continue
        for name, rx in candidates:
            m = rx.search(line)
            if m:
                hits.append((number, name, m.group(0)))
                break
    return hits


def secret_hits(path: Path) -> list[tuple[int, str, str]]:
    if os.environ.get("HARNESS_SECRET_SCAN", "").strip() == "0":
        return []
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size >= MAX_SCAN_BYTES:
            return []
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if ALLOW_MARKER in text:
        return []
    return scan(text)


def report(shown: Path, hits: list[tuple[int, str, str]]) -> None:
    for number, name, found in hits[:MAX_REPORTED]:
        sys.stderr.write(
            f"SECRET pattern in {shown}:{number} ({name}): {found[:4]}\u2026 - remove it "
            "before continuing; read secrets from the environment or a file that is "
            "gitignored.\n"
        )
    if len(hits) > MAX_REPORTED:
        sys.stderr.write(f"... and {len(hits) - MAX_REPORTED} more lines in {shown}.\n")


# ---------- after a Bash call ----------


def git_status(root: Path) -> bytes | None:
    """`git status --porcelain=v1 -z --untracked-files=all` output, or None when git fails."""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "status"]
            + ["--porcelain=v1", "-z", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=STATUS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def status_paths(root: Path, out: bytes) -> list[Path]:
    """The paths in `git status --porcelain=v1 -z` output: one "XY path" field per entry,
    and for a rename or copy (R or C in X or Y) a second field, its source, skipped here."""
    paths = []
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(root / os.fsdecode(entry[3:]))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            i += 1
    return paths


def recent_files(paths: list[Path], since: float) -> list[Path]:
    """Regular files under 2 MB modified after `since`: newest first, at most 100."""
    found = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_size < MAX_SCAN_BYTES
            and info.st_mtime > since
        ):
            found.append((info.st_mtime, path))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:MAX_BASH_FILES]]


def last_bash_scan(stamp: Path, now: float) -> float:
    """When this session's previous Bash scan started; 10 minutes ago if there was none."""
    try:
        value = float(stamp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        value = math.nan
    return min(value, now) if math.isfinite(value) else now - FIRST_BASH_WINDOW


def after_bash(cwd: Path, data: dict) -> int:
    root = git_toplevel(cwd)
    if root is None:
        return 0
    started = time.time()
    out = git_status(root)
    if out is None:
        return 0
    stamp = state_dir(root) / f"bash-scan-{session_key(data)}"
    files = recent_files(status_paths(root, out), last_bash_scan(stamp, started))
    record(root, files, data)  # first, so the Stop hook runs even if the scan objects
    try:
        stamp.write_text(f"{started:.6f}\n", encoding="utf-8")
    except OSError:
        pass

    found = False
    for path in files:
        hits = secret_hits(path)
        if hits:
            report(path, hits)
            found = True
    sys.stderr.flush()
    return 2 if found else 0


def main() -> int:
    data = read_input()
    cwd = Path(
        str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    )
    if data.get("tool_name") == "Bash":
        return after_bash(cwd, data)
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw.strip():
        return 0

    shown = Path(raw) if os.path.isabs(raw) else cwd / raw
    path = shown.resolve()

    record_edit(
        cwd, path, data
    )  # first, so the Stop hook runs even if the scan objects

    hits = secret_hits(path)
    if not hits:
        return 0
    report(shown, hits)
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # fail open: only a confirmed secret may exit non-zero
        sys.stderr.write(f"after_edit.py: {exc}\n")
        code = 0
    sys.exit(code)
