"""Whether a red record still proves the tests the branch has now, for harness-red (status) and the hook
tests_first.py (source edits). See docs/design.md (red record).

The record holds `test_hash`, over the test files as they were at the red run, and `base_sha`, the merge
base with main then. harness-red check also keeps those files' bytes in a side file,
red-files/<branch-slug>.json beside red/ (`{"test_hash", "files": {path: base64}}`), which counts only when
its files hash to the record's `test_hash`: an edited or stale side file proves nothing.

A record is current when the files hash to `test_hash` now, or, after main has been merged (or the branch
rebased) so the merge base moved on from `base_sha`, when each file is exactly the red-time file with main's
changes merged in. The red-time bytes come from the side file; for a record without one (written before
it existed) they are recovered by merging main's change backwards out of each file (`git merge-file -p
--ours <now> <new base> <old base>`), and either way they must hash to `test_hash`. The forward replay
takes, per file, `git merge-file -p --ours <red-time> <old base> <new base>` (Myers; a conflicting hunk
keeps the branch's side, so a resolution that keeps the branch's own lines verbatim keeps the record) or,
when that differs, what `git merge` itself (ort) gives: `git merge-tree --write-tree` of a synthetic commit
(the old base's tree with the red-time blobs, its parent the old base) and the new base, its objects
written to a temporary object directory, never the repository's. The inputs are fixed (bytes proven by
the hash, two commits of main), so a match pins every byte of the file now, and a test change of the
branch's own, made after the red run or in a merge resolution that changes its lines, leaves the record
stale. A replay that differs from git's real merge can only give a false "not current".

The new merge base is that of origin/main, then main (the refs harness-red check defaults to, spelled in
full), with HEAD, and with MERGE_HEAD too while a merge is in progress (the base the merge commit will
have). Each ref counts at the commit its newest reflog entry set it to, and only when that entry is an
update the harness trusts (`trusted_update`: a fetch or pull from origin, a push, a clone, and for main a
commit, merge, rebase or reset), so a ref moved by `git update-ref`, `git fetch . x:<ref>` or a write to its
file names no base. Only a base that descends from `base_sha` counts: an older one (a stale local main, a
record made against a stacked parent) would let the branch undo main's changes. A `base_sha` that is not a
commit id here (a hand-written "0" * 40; 40 hex digits in SHA-1 repositories, 64 in SHA-256 ones) gets the
whole-file comparison only.

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

# In full, so a tag or another ref spelled origin/main or main cannot stand in for them.
BASE_REFS = ("refs/remotes/origin/main", "refs/heads/main")
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")  # a commit id: SHA-1, or SHA-256 (--object-format=sha256)
# A reflog entry git fetch or pull wrote: `fetch <args>: <note>` (`pull --rebase (finish): ...` for a rebase).
FETCH = re.compile(r"^(?:fetch|pull)((?: [^ ]+)*?)(?: \([a-z]+\))?: ")
# The other updates that move these refs legitimately; main is the user's own checkout.
TRUSTED = {
    "refs/remotes/origin/main": ("update by push", "clone: from "),
    "refs/heads/main": ("commit: ", "commit (", "merge ", "rebase", "reset: moving to ", "branch: Created from ",
                        "clone: from "),
}
# The synthetic commit's identity and date: fixed, so the replay never reads the user's config for them.
SYNTHETIC = {
    "GIT_AUTHOR_NAME": "harness-red", "GIT_AUTHOR_EMAIL": "harness-red@localhost",
    "GIT_COMMITTER_NAME": "harness-red", "GIT_COMMITTER_EMAIL": "harness-red@localhost",
    "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000",
}


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
    above. All its git calls together get `timeout` seconds; any failure (a missing file, git failing, the
    budget running out) counts as not current."""
    return record_state(root, record, record_path, timeout) == "current"


def record_state(root: str, record: dict, record_path: str, timeout: float = 30) -> str:
    """record_current's answer as "current", "stale", or "unknown" when its git calls ran out of `timeout`
    seconds (a hook passes what is left of its own deadline) before it could tell."""
    files = record.get("test_files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        return "stale"
    try:
        contents = read_files(root, files)
    except OSError:
        return "stale"
    want = record.get("test_hash")
    if tests_hash(contents) == want:
        return "current"
    old = record.get("base_sha")
    if not isinstance(old, str) or not SHA.match(old):
        return "stale"
    kept = load_red_files(record_path, record)
    deadline = time.monotonic() + timeout
    try:
        for new in moved_bases(root, old, deadline):
            blobs = file_blobs(root, [(c, f) for c in (old, new) for f in sorted(contents)], deadline)
            if blobs is None:
                continue
            before = {f: blobs[(old, f)] for f in contents}
            after = {f: blobs[(new, f)] for f in contents}
            red = kept if kept is not None else recovered(root, contents, before, after, deadline)
            if red is not None and tests_hash(red) == want and replayed(
                root, contents, red, old, new, before, after, deadline
            ):
                return "current"
    except subprocess.TimeoutExpired:
        return "unknown"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return "stale"


def git(root: str, args, deadline: float, stdin: bytes = b"", env=None) -> subprocess.CompletedProcess:
    """git in `root`, held to what is left before `deadline` (time.monotonic()), with `env` added to the
    environment. Raises TimeoutExpired."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired(["git", *args], 0)
    full = dict(os.environ, **env) if env else None
    return subprocess.run(["git", "-C", root, *args], input=stdin, capture_output=True, timeout=left, env=full)


def moved_bases(root: str, old: str, deadline: float) -> list:
    """The merge bases with origin/main and main, each at its trusted tip (trusted_tip), of HEAD (and of
    MERGE_HEAD too during a merge) that descend from `old` and differ from it."""
    merging = git(root, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], deadline).returncode == 0
    heads = ["HEAD", "MERGE_HEAD"] if merging else ["HEAD"]
    found = []
    for ref in BASE_REFS:
        tip = trusted_tip(root, ref, deadline)
        if tip is None:
            continue
        out = git(root, ["merge-base", tip, *heads], deadline)  # the tip with a merge of the heads
        new = out.stdout.decode("ascii", "replace").strip()
        if out.returncode != 0 or not SHA.match(new) or new == old or new in found:
            continue
        if git(root, ["merge-base", "--is-ancestor", old, new], deadline).returncode == 0:
            found.append(new)
    return found


def trusted_tip(root: str, ref: str, deadline: float):
    """The commit `ref`'s newest reflog entry set it to, when trusted_update trusts that entry; else None (no
    reflog, or a ref moved some other way). A ref file written without a reflog entry is not seen."""
    out = git(root, ["log", "-g", "-1", "--format=%H%x00%gs", ref, "--"], deadline)
    sha, _, subject = out.stdout.decode("utf-8", "replace").rstrip("\n").partition("\0")
    return sha if out.returncode == 0 and SHA.match(sha) and trusted_update(ref, subject) else None


def trusted_update(ref: str, subject: str) -> bool:
    """True when the reflog entry `subject` of `ref` (one of BASE_REFS) is an update the harness trusts: a
    fetch or pull from origin (named, or the default) whose refspecs name no destination or take main to one,
    or one of TRUSTED[ref]. A fetch from `.` or another remote, `git update-ref` (no message, or one git
    itself never writes) and `git branch -f` are not."""
    m = FETCH.match(subject)
    if m is None:
        return subject.startswith(TRUSTED.get(ref, ()))
    words = [w for w in m.group(1).split() if not w.startswith("-")]
    if words and words[0] != "origin":
        return False
    return all(w.lstrip("+").split(":", 1)[0] in ("main", "refs/heads/main") for w in words[1:] if ":" in w)


def file_blobs(root: str, wanted, deadline: float, env=None):
    """(object, path) -> the file's bytes in that commit or tree, None when it has no such file there (or it
    is not a blob); None for all when git fails. One `git cat-file --batch` call."""
    query = b"".join(f"{c}:{f}\n".encode("utf-8", "surrogateescape") for c, f in wanted)
    out = git(root, ["cat-file", "--batch"], deadline, query, env)
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


def recovered(root: str, contents: dict, before: dict, after: dict, deadline: float):
    """The red-time files guessed from `contents` by taking main's change (`before` -> `after`) back out, a
    conflicting hunk keeping the file now; None when a merge fails. The caller proves it by test_hash."""
    red = {}
    for f, now in contents.items():
        if before[f] == after[f] or after[f] is None:
            red[f] = now
            continue
        back = merge_file(root, now, after[f], before[f] or b"", deadline)
        if back is None:
            return None
        red[f] = back
    return red


def replayed(root, contents, red, old, new, before, after, deadline) -> bool:
    """True when every file of `contents` is its red-time bytes (`red`) with main's change from commit `old`
    (`before`, path -> bytes or None) to commit `new` (`after`) merged in: by merge-file --ours, else by
    git's own merge (ort)."""
    rest = []
    for f, now in contents.items():
        # main did not touch it, or deleted it (the branch's side of that conflict is its whole file)
        if before[f] == after[f] or after[f] is None:
            if now != red[f]:
                return False
        elif merge_file(root, red[f], before[f] or b"", after[f], deadline) != now:
            rest.append(f)
    if not rest:
        return True
    merged = ort_merge(root, red, old, new, deadline)
    return merged is not None and all(merged.get(f) == contents[f] for f in rest)


def ort_merge(root: str, red: dict, old: str, new: str, deadline: float):
    """path -> bytes of the red-time files after `git merge-tree --write-tree` (git 2.38+) of a synthetic
    commit (`old`'s tree with the `red` blobs, parent `old`) with `new`; None when it conflicts or git fails.
    Every object it writes goes to a temporary object directory that reads the repository's through
    GIT_ALTERNATE_OBJECT_DIRECTORIES, so the repository is left as it was. Eight git calls."""
    common = git(root, ["rev-parse", "--git-common-dir"], deadline)
    if common.returncode != 0:
        return None
    objects = os.path.join(root, common.stdout.decode("utf-8", "surrogateescape").strip(), "objects")
    with tempfile.TemporaryDirectory(prefix="harness-red-") as tmp:
        env = dict(SYNTHETIC, GIT_OBJECT_DIRECTORY=os.path.join(tmp, "objects"),
                   GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.abspath(objects),
                   GIT_INDEX_FILE=os.path.join(tmp, "index"))
        os.makedirs(env["GIT_OBJECT_DIRECTORY"])
        names = sorted(red)
        paths = []
        for k, name in enumerate(names):
            path = os.path.join(tmp, f"blob{k}")
            with open(path, "wb") as fh:
                fh.write(red[name])
            paths.append(path)
        stdin = "".join(p + "\n" for p in paths).encode("utf-8", "surrogateescape")
        out = git(root, ["hash-object", "-w", "--no-filters", "--stdin-paths"], deadline, stdin, env)
        blobs = out.stdout.decode("ascii", "replace").split()
        if out.returncode != 0 or len(blobs) != len(names):
            return None
        if git(root, ["read-tree", old], deadline, env=env).returncode != 0:
            return None
        info = b"".join(f"100644 {b}\t".encode() + n.encode("utf-8", "surrogateescape") + b"\0"
                        for b, n in zip(blobs, names))
        if git(root, ["update-index", "-z", "--index-info"], deadline, info, env).returncode != 0:
            return None
        tree = git(root, ["write-tree"], deadline, env=env)
        commit = git(root, ["commit-tree", "-p", old, "-m", "red-time tests",
                            tree.stdout.decode("ascii", "replace").strip()], deadline, env=env)
        if tree.returncode != 0 or commit.returncode != 0:
            return None
        synthetic = commit.stdout.decode("ascii", "replace").strip()
        out = git(root, ["merge-tree", "--write-tree", synthetic, new], deadline, env=env)
        if out.returncode != 0:  # 1: conflicts; anything else: no merge-tree --write-tree here
            return None
        result = out.stdout.decode("ascii", "replace").split()[:1]
        merged = file_blobs(root, [(result[0], f) for f in names], deadline, env) if result else None
    return None if merged is None else {f: merged[(result[0], f)] for f in names}
