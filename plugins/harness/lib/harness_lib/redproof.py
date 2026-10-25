"""Whether a red record still proves the tests the branch has now, for harness-red (status) and the hook
tests_first.py (source edits). See docs/design.md (red record).

The record holds `test_hash`, over the test files as they were at the red run, and `base_sha`, the merge
base with main then. It is current when the files hash to `test_hash` now, or, after main has been merged
(or the branch rebased) so the merge base moved, when the files are exactly the red-time files with main's
changes since `base_sha` merged in: for each file, `git merge-file --ours` of the red-time text (ours), the
old base's (base) and the new base's (theirs) gives the file as it is. `--ours` keeps the branch's side of
a conflicting hunk, so a resolution that keeps the branch's own lines verbatim and drops main's edit of
them keeps the record, as a whole-file "ours" always did through the hash. The red-time text is not
stored: it is recovered by merging main's change backwards out of the file (ours: the file now, base: the
new base, theirs: the old base) and proven by `test_hash`, then the forward merge is checked as above. A
test change of the branch's own, made after the red run or in a merge resolution that changes its lines,
leaves no such pair, so the record stops being current.

The new merge base is that of HEAD with origin/main, then with main (the refs harness-red check defaults
to), each tried. A base_sha that does not name a commit here (a hand-written "0" * 40) gets the whole-file
comparison only.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time

BASE_REFS = ("origin/main", "main")
SHA = re.compile(r"^[0-9a-f]{40}$")


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


def record_current(root: str, record: dict, timeout: float = 30) -> bool:
    """True when the test files under `root` are the ones `record` proved red, as above. All its git calls
    together get `timeout` seconds (a hook passes its own budget); any failure (a missing file, git failing,
    the budget running out) counts as not current."""
    files = record.get("test_files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        return False
    try:
        contents = read_files(root, files)
    except OSError:
        return False
    want = record.get("test_hash")
    if tests_hash(contents) == want:
        return True
    old = record.get("base_sha")
    if not isinstance(old, str) or not SHA.match(old):
        return False
    try:
        return merged_current(root, contents, old, want, time.monotonic() + timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def git(root: str, args, deadline: float, stdin: bytes = b"") -> subprocess.CompletedProcess:
    """git in `root`, held to what is left before `deadline` (time.monotonic()). Raises TimeoutExpired."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired(["git", *args], 0)
    return subprocess.run(["git", "-C", root, *args], input=stdin, capture_output=True, timeout=left)


def merged_current(root: str, contents: dict, old: str, want, deadline: float) -> bool:
    """True when, against some moved merge base, `contents` is the red-time files (hashing to `want`) with
    main's changes since `old` merged in."""
    if git(root, ["cat-file", "-e", old + "^{commit}"], deadline).returncode != 0:
        return False  # the old base is not here: nothing to replay against
    tried = {old}
    for ref in BASE_REFS:
        out = git(root, ["merge-base", "HEAD", ref], deadline)
        new = out.stdout.decode("ascii", "replace").strip()
        if out.returncode != 0 or not SHA.match(new) or new in tried:
            continue
        tried.add(new)
        blobs = file_blobs(root, [(c, f) for c in (old, new) for f in sorted(contents)], deadline)
        if blobs is None:
            continue
        at_old = {f: blobs[(old, f)] for f in contents}
        at_new = {f: blobs[(new, f)] for f in contents}
        if replayed(root, contents, at_old, at_new, want, deadline):
            return True
    return False


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


def merge_file(root: str, ours: bytes, base: bytes, theirs: bytes, deadline: float):
    """`git merge-file -p --ours ours base theirs`: a conflicting hunk keeps ours. None when git fails."""
    with tempfile.TemporaryDirectory(prefix="harness-red-") as tmp:
        names = []
        for label, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, label)
            with open(path, "wb") as fh:
                fh.write(text)
            names.append(path)
        out = git(root, ["merge-file", "-p", "-q", "--ours", *names], deadline)
    return out.stdout if out.returncode == 0 else None


def replayed(root: str, contents: dict, old: dict, new: dict, want, deadline: float) -> bool:
    """True when `contents` is the files hashing to `want` with the change from `old` to `new` (path ->
    bytes or None) merged in, a conflicting hunk keeping the branch's side."""
    red = {}
    for f, now in contents.items():
        # main did not touch it, or deleted it (the branch's side of that conflict is its whole file): the
        # branch's file must be the red-time one
        if old[f] == new[f] or new[f] is None:
            red[f] = now
            continue
        back = merge_file(root, now, new[f], old[f] or b"", deadline)
        if back is None or merge_file(root, back, old[f] or b"", new[f], deadline) != now:
            return False
        red[f] = back
    return tests_hash(red) == want
