"""The backup store: the snapshot a destructive command leaves of what it destroys, and its restore.
A snapshot is <store>/<id>/: files/<k>, one copy per item (a file, a whole folder tree, or a symlink
kept as a link and never followed), and manifest.json. The store is ~/.claude/harness/backups/, or
<git common dir>/harness-backups/ for a project with any HARNESS_SENSITIVE_PATHS pattern, so that
project's copies never leave it. harness-work's `backup list` and `backup restore` read it. See
docs/design.md (State, harness-work).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import datetime as dt
import errno
import itertools
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from harness_lib.paths import canonical_path
from harness_lib.settings import glob_match, patterns, setting, setting_from, settings_env
from harness_lib.text import harness_home, read_json, write_json

SETTING = "HARNESS_SENSITIVE_PATHS"
PROJECT_STORE = "harness-backups"           # the in-project store's folder in the git common dir
ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
ID_TRIES = 100                              # suffixes tried for one second before giving up
GIT_TIMEOUT = 10                            # seconds one git call may take
PRIVATE = 0o700                             # the store folders' mode


class BackupError(Exception):
    """A snapshot that cannot be taken, found or restored: one line for the caller to print."""


# --------------------------------------------------------------- stores
def sensitive_patterns(main):
    """HARNESS_SENSITIVE_PATHS from the environment and from `env` in the main checkout's tracked
    .claude/settings.json, together, each pattern once (none for `main` None but the environment's)."""
    found = patterns(setting(SETTING, {}) or "")
    if main:
        found += patterns(setting_from(SETTING, settings_env(main, ("settings.json",))) or "")
    return list(dict.fromkeys(found))


def global_store():
    return Path(harness_home("backups"))


def store_dir(main):
    """Where a snapshot of `main`'s project (the main checkout, or None) goes: <git common dir>/harness-backups/
    when the project has any sensitive pattern, else ~/.claude/harness/backups/."""
    return _store(main, sensitive_patterns(main))


def _store(main, pats):
    if main and pats:
        common = _common_dir(main)
        if common is None:
            raise BackupError(f"{main} marks paths sensitive but git names no git dir for it; no snapshot taken")
        return Path(common, PROJECT_STORE)
    return global_store()


def _git(cwd, *args):
    """git's stripped stdout, or None when it fails, prints nothing or cannot run."""
    try:
        done = subprocess.run(["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def _common_dir(main):
    top = _git(main, "rev-parse", "--show-toplevel")
    common = _git(top, "rev-parse", "--git-common-dir") if top else None
    return os.path.normpath(os.path.join(top, common)) if common else None


def _stores(main):
    """The project store `main`'s snapshots can be in (its git dir's, whatever the patterns say now), then the
    global one."""
    common = _common_dir(main) if main else None
    return ([os.path.join(common, PROJECT_STORE)] if common else []) + [str(global_store())]


def _load(folder, snapshot_id):
    manifest = read_json(os.path.join(folder, "manifest.json"))
    if isinstance(manifest, dict) and manifest.get("id") == snapshot_id and isinstance(manifest.get("items"), list):
        return manifest
    return None


def _manifests(store):
    try:
        names = os.listdir(store)
    except OSError:
        return []
    found = (_load(os.path.join(store, name), name) for name in names if ID_RE.fullmatch(name))
    return [m for m in found if m is not None]


def snapshots(main, *, everything=False):
    """This repository's snapshots, newest first: every one in its project store and those in the global store whose
    `repo` is `main` (None: those taken outside any repository); `everything` adds the rest of the global store."""
    repo = canonical_path(main) if main else None
    *project, common = _stores(main)
    found = {}
    for store in project:
        for manifest in _manifests(store):
            found.setdefault(manifest["id"], manifest)
    for manifest in _manifests(common):
        if everything or manifest.get("repo") == repo:
            found.setdefault(manifest["id"], manifest)
    return sorted(found.values(), key=lambda m: (str(m.get("at") or ""), m["id"]), reverse=True)


def _locate(main, snapshot_id):
    if isinstance(snapshot_id, str) and ID_RE.fullmatch(snapshot_id):
        for store in _stores(main):
            folder = os.path.join(store, snapshot_id)
            manifest = _load(folder, snapshot_id)
            if manifest is not None:
                return manifest, folder
    return None


def find(main, snapshot_id):
    """The manifest of snapshot `snapshot_id` in `main`'s project store or the global store; None for an unknown
    id or anything that is not an id (a path, `..`)."""
    found = _locate(main, snapshot_id)
    return found[0] if found else None


# --------------------------------------------------------------- copies
def _copy(src, dst, skip=None):
    """Copy the file, folder tree or symlink `src` to the new path `dst`: links copied as links, never followed;
    bytes, modes and times kept, a folder's own mode set after its contents. On macOS `cp -c` clones where it
    can; elsewhere, or when cp fails, the copy is made here. `skip` holds (st_dev, st_ino) of folders left out."""
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
    elif not (sys.platform == "darwin" and not skip and _cp(src, dst, stat.S_ISDIR(st.st_mode))):
        _copy_here(src, dst, skip or set())


def _cp(src, dst, tree):
    if not os.path.isfile("/bin/cp"):
        return False
    try:
        done = subprocess.run(["/bin/cp", "-c", "-p"] + (["-R", "-P"] if tree else []) + ["--", src, dst],
                              stdin=subprocess.DEVNULL, capture_output=True)
    except OSError:
        return False
    if done.returncode != 0:
        _remove(dst)                        # a partial copy; the copy is made again here
    return done.returncode == 0


def _copy_here(src, dst, skip):
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISDIR(st.st_mode):
        if (st.st_dev, st.st_ino) in skip:
            return
        os.mkdir(dst, PRIVATE)
        for entry in os.scandir(src):
            _copy_here(entry.path, os.path.join(dst, entry.name), skip)
        shutil.copystat(src, dst, follow_symlinks=False)
    elif stat.S_ISREG(st.st_mode):
        shutil.copy2(src, dst, follow_symlinks=False)
    # a socket, fifo or device inside a tree is left out


def _remove(path):
    """Delete the file, link or folder tree at `path`, never following a link; read-only folders are opened first."""
    if not os.path.lexists(path):
        return
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        os.unlink(path)
        return
    try:
        shutil.rmtree(path)
    except OSError:
        _open_up(path)
        shutil.rmtree(path)


def _open_up(path):
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
        for entry in os.scandir(path):
            _open_up(entry.path)


def _inside(folder):
    """(path relative to `folder` with / separators, lstat) of every entry under it, links never followed."""
    for parent, dirs, files in os.walk(folder):
        for name in dirs + files:
            full = os.path.join(parent, name)
            yield os.path.relpath(full, folder).replace(os.sep, "/"), os.lstat(full)


def _within(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _own_relative(path, tops):
    """`path` relative to the working tree holding it, as git names that tree from the nearest existing folder above
    it, with / separators; None outside any (and for the tree's own root). `tops` caches git's answers."""
    folder = os.path.realpath(os.path.dirname(path))
    probe = folder
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if probe not in tops:
        top = _git(probe, "rev-parse", "--show-toplevel")
        tops[probe] = os.path.realpath(top) if top else None
    if tops[probe] is None:
        return None
    rel = os.path.relpath(os.path.join(folder, os.path.basename(path)), tops[probe])
    return None if rel == os.curdir or rel.split(os.sep)[0] == os.pardir else rel.replace(os.sep, "/")


def _sensitive(path, copy, pats, tops):
    """Whether the item's path relative to its own working tree (its basename outside one), or a path inside a
    folder item, matches a pattern."""
    if not pats:
        return False
    rel = _own_relative(path, tops) or os.path.basename(path)
    inner = (rel + "/" + sub for sub, _st in _inside(copy)) if stat.S_ISDIR(os.lstat(copy).st_mode) else ()
    return any(glob_match(p, name) for name in itertools.chain([rel], inner) for p in pats)


def _bytes(copy):
    st = os.lstat(copy)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    return sum(s.st_size for _sub, s in _inside(copy) if not stat.S_ISDIR(s.st_mode))


# --------------------------------------------------------------- create
def _existing(paths, cwd):
    """(path, kind) of each existing file, folder and symlink of `paths` (relative ones from `cwd`), once each and
    in order; a path inside a folder item is left out, the folder's copy holding it."""
    found = {}
    for given in paths:
        path = os.path.normpath(os.path.join(cwd, given))
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        kind = "link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else None
        if kind:
            found.setdefault(path, kind)
    dirs = [p for p, kind in found.items() if kind == "dir"]
    return [(p, kind) for p, kind in found.items() if not any(p != d and _within(p, d) for d in dirs)]


def _new_folder(store, when):
    """<store>/<id>/, made with an exclusive mkdir (a clash takes a new suffix); the store made private too."""
    os.makedirs(store, PRIVATE, exist_ok=True)
    if stat.S_IMODE(os.stat(store).st_mode) != PRIVATE:
        os.chmod(store, PRIVATE)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    for _ in range(ID_TRIES):
        folder = os.path.join(store, f"{stamp}-{secrets.token_hex(3)}")
        try:
            os.mkdir(folder, PRIVATE)
        except FileExistsError:
            continue
        os.chmod(folder, PRIVATE)           # whatever the umask
        return folder
    raise BackupError(f"no free snapshot id in {store} for {stamp}")


def _take(store, repo, paths, pats, *, cwd, command, session, now):
    """The snapshot of `paths` in `store` with `repo` as its repository; its manifest. A failure leaves no folder."""
    cwd = os.path.abspath(cwd)
    when = now or dt.datetime.now(dt.timezone.utc)
    when = (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    doing, folder = f"create the store {store}", None
    try:
        items = _existing(paths, cwd)
        folder = _new_folder(os.fspath(store), when)
        os.mkdir(os.path.join(folder, "files"), PRIVATE)
        own = os.lstat(folder)
        tops, listed, sensitive = {}, [], False
        for k, (path, kind) in enumerate(items):
            doing, copy = f"copy {path}", os.path.join(folder, "files", str(k))
            # A folder holding the store (rm -rf ~/.claude) is copied without this snapshot's own folder.
            _copy(path, copy, {(own.st_dev, own.st_ino)} if _within(os.path.realpath(folder), os.path.realpath(path))
                  else None)
            listed.append({"path": path, "copy": f"files/{k}", "kind": kind, "bytes": _bytes(copy)})
            sensitive = sensitive or _sensitive(path, copy, pats, tops)
        manifest = {"id": os.path.basename(folder), "at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": repo,
                    "cwd": cwd, "command": command, "session": session, "sensitive": sensitive, "items": listed,
                    "refs": []}
        doing = f"write {folder}/manifest.json"
        write_json(os.path.join(folder, "manifest.json"), manifest, prefix=".harness-backup-")
    except BaseException as err:
        if folder:
            try:
                _remove(folder)
            except OSError:
                pass
        if isinstance(err, OSError):
            raise BackupError(f"cannot {doing}: {err.strerror or err}; no snapshot taken") from None
        raise
    return manifest


def create(paths, *, main, cwd, command, session=None, now=None):
    """Snapshot each existing path of `paths` (a relative one taken from `cwd`, a missing one skipped) into the
    store of `main` (the main checkout, or None outside a repository); writes and returns the manifest
    {id, at, repo, cwd, command, session, sensitive, items: [{path, copy, kind, bytes}], refs}."""
    pats = sensitive_patterns(main)
    return _take(_store(main, pats), canonical_path(main) if main else None, paths, pats, cwd=cwd,
                 command=command, session=session, now=now)


# --------------------------------------------------------------- restore
def _source(item, folder, snapshot_id):
    """(original path, copy in the snapshot) of a manifest item; BackupError for one that names no copy of its own."""
    path, copy = (item.get("path"), item.get("copy")) if isinstance(item, dict) else (None, None)
    files = os.path.join(folder, "files")
    source = os.path.normpath(os.path.join(folder, copy)) if isinstance(copy, str) else ""
    if not (isinstance(path, str) and os.path.isabs(path) and _within(source, files) and source != files
            and os.path.lexists(source)):
        raise BackupError(f"backup {snapshot_id} is damaged: no copy of {path}")
    return os.path.normpath(path), source


def _target(path, repo, root, tops):
    """Where an item goes: its own path, or under `root` (--to) at its path relative to the snapshot's repository,
    else to its own working tree, else at its basename."""
    if root is None:
        return path
    rel = None
    for candidate in (path, os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))):
        if repo and candidate != repo and _within(candidate, repo):
            rel = os.path.relpath(candidate, repo)
            break
    return os.path.join(root, rel or _own_relative(path, tops) or os.path.basename(path))


def _check(plan, root, snapshot_id):
    """BackupError unless every target can be written as it stands: with --to each under `root`, no two at or
    under each other, and no symlink or non-folder among the folders between; without, the nearest existing
    folder above each a folder."""
    for i, (target, path, _source_) in enumerate(plan):
        if root is not None:
            if target == root or not _within(target, root):
                raise BackupError(f"backup {snapshot_id}: {path} would restore outside {root}")
            for other, other_path, _ in plan[:i]:
                if _within(target, other):
                    raise BackupError(f"backup {snapshot_id}: {other_path} and {path} would both restore to {other}")
            folders, folder = [root], root
            rel = os.path.relpath(os.path.dirname(target), root)
            for part in rel.split(os.sep) if rel != os.curdir else ():
                folder = os.path.join(folder, part)
                folders.append(folder)
            for folder in folders:          # DIR itself may be a link the caller chose; nothing below it may
                if folder != root and os.path.islink(folder):
                    raise BackupError(f"cannot restore {target}: {folder} is a symlink and --to writes only under DIR")
                if os.path.lexists(folder) and not os.path.isdir(folder):
                    raise BackupError(f"cannot restore {target}: {folder} is not a folder")
        else:
            folder = os.path.dirname(target)
            while not os.path.lexists(folder):
                folder = os.path.dirname(folder)
            if not os.path.isdir(folder):
                raise BackupError(f"cannot restore {target}: {folder} is not a folder")


def _put(source, target, replace):
    """Copy `source` to a temporary name beside `target`, then rename it into place; an existing target (only when
    `replace`) is moved aside first and deleted once the copy is in."""
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)

    def spare(kind):
        while True:
            name = os.path.join(parent, f".harness-{kind}-{secrets.token_hex(4)}")
            if not os.path.lexists(name):
                return name

    tmp, aside = spare("restore"), None
    _copy(source, tmp)
    try:
        if os.path.lexists(target):
            if not replace:
                raise FileExistsError(errno.EEXIST, "it appeared during the restore")
            aside = spare("replaced")
            os.rename(target, aside)
        try:
            os.rename(tmp, target)
        except OSError:
            if aside:
                os.rename(aside, target)
            raise
    except BaseException:
        _remove(tmp)
        raise
    if aside:
        _remove(aside)


def restore(main, snapshot_id, *, only=None, to=None, force=False, cwd=None, command="", session=None,
            progress=None):
    """Put snapshot `snapshot_id`'s items back: each at its original path, or under `to` (see _target); with `only`,
    the items at or under it. `only` and `to` are taken from `cwd` (default: the process's). Every target is checked
    before anything is written; an existing one refuses the restore unless `force`, which first snapshots the
    existing targets with the snapshot's repo: into its project store when the snapshot sat in one, else into the
    store that repo's snapshots go to now (store_dir). `progress(kind, value)` hears ("saved", that
    snapshot's manifest) and ("restored", target) as each happens. Returns {id, restored, replaced, saved}."""
    found = _locate(main, snapshot_id)
    if found is None:
        raise BackupError(f"no backup {snapshot_id}")
    manifest, folder = found
    sid = manifest["id"]
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), str) else None
    cwd = os.path.abspath(cwd or os.getcwd())
    items = manifest["items"]
    if only is not None:
        base = os.path.normpath(os.path.join(cwd, only))
        items = [item for item in items if isinstance(item, dict) and isinstance(item.get("path"), str)
                 and _within(os.path.normpath(item["path"]), base)]
        if not items:
            raise BackupError(f"backup {sid} has no item at or under {base}")
    items = [_source(item, folder, sid) for item in items]
    root = os.path.normpath(os.path.join(cwd, to)) if to is not None else None
    tops = {}
    plan = sorted((_target(path, repo, root, tops), path, source) for path, source in items)
    _check(plan, root, sid)
    existing = [target for target, _path, _source_ in plan if os.path.lexists(target)]
    replace = set(existing)
    if existing and not force:
        more = f" (and {len(existing) - 1} more)" if len(existing) > 1 else ""
        raise BackupError(f"{existing[0]} exists{more}; backup {sid} restores over it only with --force")
    saved = None
    if existing:
        # A project store's snapshot keeps it there; one from the global store goes where `repo`'s go now.
        pats, store = sensitive_patterns(repo), os.path.dirname(folder)
        if store == str(global_store()):
            store = _store(repo, pats)
        taken = _take(store, repo, existing, pats, cwd=cwd, command=command, session=session, now=None)
        saved = taken["id"]
        if progress:
            progress("saved", taken)
    restored = []
    for target, _path, source in plan:
        try:
            _put(source, target, target in replace)
        except OSError as err:
            kept = f"; what it replaced is in backup {saved}" if saved else ""
            raise BackupError(f"cannot restore {target}: {err.strerror or err} ({len(restored)} of {len(plan)} "
                              f"restored{kept})") from None
        restored.append(target)
        if progress:
            progress("restored", target)
    return {"id": sid, "restored": restored, "replaced": existing, "saved": saved}
