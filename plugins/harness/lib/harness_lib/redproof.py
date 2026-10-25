"""Whether a red record still proves the tests the branch has now, for harness-red (status) and the hook
tests_first.py (source edits). See docs/design.md (red record).

The record holds `test_hash`, over the test files as they were at the red run, and `base_sha`, the merge
base with main then. harness-red check also keeps those files' bytes in a side file,
red-files/<branch-slug>.json beside red/ (`{"test_hash", "files": {path: base64}}`), which counts only when
its files hash to the record's `test_hash`: an edited or stale side file proves nothing.

A record is current when the files hash to `test_hash` now, or, after main has been merged (or the branch
rebased) so the merge base moved on from `base_sha`, when each file is exactly the red-time file with main's
changes merged in: `git merge-file -p --ours <red-time> <old base> <new base>` gives the file as it is. The
inputs are fixed (the red-time bytes proven by the hash, two commits of main), so the result pins every byte
and a test change of the branch's own, made after the red run or in a merge resolution that changes its
lines, leaves the record stale. `--ours` keeps the branch's side of a conflicting hunk: a resolution that
keeps the branch's own lines verbatim and drops main's edit of them keeps the record, as taking the
branch's whole file always did through the hash. When the default (Myers) result differs, histogram (what
newer gits merge with) is tried where merge-file takes --diff-algorithm; a git without it gives a false
"not current", never a false "current".

The new merge base is that of origin/main, then main (the refs harness-red check defaults to), with HEAD,
and with MERGE_HEAD too while a merge is in progress (the base the merge commit will have). Only a base
that descends from `base_sha` counts: an older one (a stale local main, a record made against a stacked
parent) would let the branch undo main's changes. Without a valid side file, or with a `base_sha` that is
not a commit here (a hand-written "0" * 40), only the whole-file comparison applies.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import subprocess
import tempfile
import time

from harness_lib.text import read_json, write_json

BASE_REFS = ("origin/main", "main")
SHA = re.compile(r"^[0-9a-f]{40}$")
ALGORITHMS = (None, "histogram")  # merge-file's default (Myers), then what ort merges with


def tests_hash(contents: dict) -> str:
    """sha256 over, for each path in sorted order, the path, a newline and its bytes (`contents`: path -> bytes)."""
    h = hashlib.sha256()
    for name in sorted(contents):
        h.update(name.encode("utf-8", "surrogateescape") + b"\n")
        h.update(contents[name])
    return h.hexdigest()


def read_files(root: str, files) -> dict:
    """path -> bytes of each file under `root`. Raises OSError."""
    contents = {}
    for name in files:
        with open(os.path.join(root, name), "rb") as fh:
            contents[name] = fh.read()
    return contents


def red_files_path(record_path: str) -> str:
    """The side file of the record at `record_path`: red-files/<slug>.json beside its red/ folder."""
    state = os.path.dirname(os.path.dirname(record_path))
    return os.path.join(state, "red-files", os.path.basename(record_path))


def save_red_files(record_path: str, contents: dict) -> None:
    """Keep the red-time bytes of the test files for the record at `record_path`. Raises OSError."""
    files = {name: base64.b64encode(data).decode("ascii") for name, data in sorted(contents.items())}
    write_json(red_files_path(record_path), {"test_hash": tests_hash(contents), "files": files}, prefix=".red-")


def load_red_files(record_path: str, record: dict):
    """path -> red-time bytes of the record's test files, or None unless the side file holds exactly those
    files and they hash to the record's test_hash."""
    side = read_json(red_files_path(record_path))
    files = side.get("files") if isinstance(side, dict) else None
    if not isinstance(files, dict) or set(files) != set(record.get("test_files") or ()):
        return None
    try:
        contents = {name: base64.b64decode(text, validate=True) for name, text in files.items()}
    except (TypeError, ValueError, binascii.Error):
        return None
    return contents if tests_hash(contents) == record.get("test_hash") else None


def record_current(root: str, record: dict, record_path: str, timeout: float = 30) -> bool:
    """True when the test files under `root` are the ones `record` (read from `record_path`) proved red, as
    above. All its git calls together get `timeout` seconds (a hook passes its own budget); any failure (a
    missing file, git failing, the budget running out) counts as not current."""
    files = record.get("test_files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        return False
    try:
        contents = read_files(root, files)
    except OSError:
        return False
    if tests_hash(contents) == record.get("test_hash"):
        return True
    old = record.get("base_sha")
    if not isinstance(old, str) or not SHA.match(old):
        return False
    red = load_red_files(record_path, record)
    if red is None:
        return False
    deadline = time.monotonic() + timeout
    try:
        return any(
            replayed(root, contents, red, old, new, deadline) for new in moved_bases(root, old, deadline)
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def git(root: str, args, deadline: float, stdin: bytes = b"") -> subprocess.CompletedProcess:
    """git in `root`, held to what is left before `deadline` (time.monotonic()). Raises TimeoutExpired."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired(["git", *args], 0)
    return subprocess.run(["git", "-C", root, *args], input=stdin, capture_output=True, timeout=left)


def moved_bases(root: str, old: str, deadline: float) -> list:
    """The merge bases with origin/main and main (of HEAD, and of MERGE_HEAD too during a merge) that
    descend from `old` and differ from it."""
    merging = git(root, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], deadline).returncode == 0
    heads = ["HEAD", "MERGE_HEAD"] if merging else ["HEAD"]
    found = []
    for ref in BASE_REFS:
        out = git(root, ["merge-base", ref, *heads], deadline)  # ref with a merge of the heads
        new = out.stdout.decode("ascii", "replace").strip()
        if out.returncode != 0 or not SHA.match(new) or new == old or new in found:
            continue
        if git(root, ["merge-base", "--is-ancestor", old, new], deadline).returncode == 0:
            found.append(new)
    return found


def file_blobs(root: str, wanted, deadline: float):
    """(commit, path) -> the file's bytes at that commit, None when it has no such file there (or it is not a
    blob); None for all when git fails. One `git cat-file --batch` call."""
    query = b"".join(f"{c}:{f}\n".encode("utf-8", "surrogateescape") for c, f in wanted)
    out = git(root, ["cat-file", "--batch"], deadline, query)
    if out.returncode != 0:
        return None
    data, at, blobs = out.stdout, 0, {}
    for c, f in wanted:
        end = data.index(b"\n", at)  # ValueError on output cut short: the caller's "not current"
        header = data[at:end].split()
        at = end + 1
        blobs[(c, f)] = None
        if len(header) == 3 and header[-1].isdigit():  # <sha> <type> <size>, then the object and a newline
            size = int(header[2])
            if header[1] == b"blob":
                blobs[(c, f)] = data[at:at + size]
            at += size + 1
    return blobs


def merge_file(root: str, ours: bytes, base: bytes, theirs: bytes, algorithm, deadline: float):
    """`git merge-file -p --ours ours base theirs` (with --diff-algorithm when given): a conflicting hunk keeps
    ours. None when git fails, a git without --diff-algorithm included."""
    with tempfile.TemporaryDirectory(prefix="harness-red-") as tmp:
        names = []
        for label, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, label)
            with open(path, "wb") as fh:
                fh.write(text)
            names.append(path)
        extra = [f"--diff-algorithm={algorithm}"] if algorithm else []
        out = git(root, ["merge-file", "-p", "-q", "--ours", *extra, *names], deadline)
    return out.stdout if out.returncode == 0 else None


def replayed(root: str, contents: dict, red: dict, old: str, new: str, deadline: float) -> bool:
    """True when every file of `contents` is its red-time bytes (`red`) with main's change from commit `old`
    to commit `new` merged in, a conflicting hunk keeping the branch's side."""
    blobs = file_blobs(root, [(c, f) for c in (old, new) for f in sorted(contents)], deadline)
    if blobs is None:
        return False
    for f, now in contents.items():
        before, after = blobs[(old, f)], blobs[(new, f)]
        # main did not touch it, or deleted it (the branch's side of that conflict is its whole file)
        if before == after or after is None:
            if now != red[f]:
                return False
            continue
        if not any(merge_file(root, red[f], before or b"", after, a, deadline) == now for a in ALGORITHMS):
            return False
    return True
