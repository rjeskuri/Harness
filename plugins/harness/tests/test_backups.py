"""Tests for harness_lib/backups.py and `harness-work backup list|restore` (ticket #326, epic #322): the store a
destructive command's snapshot goes into, and the commands that list and restore snapshots.

- create(paths, *, main, cwd, command, session=None, now=None) copies each existing file, directory tree and
  symlink (kept as a link, never followed) under <store>/<id>/files/, skips missing paths, writes
  <store>/<id>/manifest.json and returns it; the id is the UTC second of `now` plus 6 hex digits, a clash retried.
- The store: ~/.claude/harness/backups/, or <git common dir>/harness-backups/ for a project with any
  HARNESS_SENSITIVE_PATHS pattern (the environment's and the main checkout's tracked .claude/settings.json `env`),
  from the main checkout and from a linked worktree; the global store then stays empty. Store folders are 0700.
- `harness-work backup list [--all] [--json] [--repo PATH]`: this repository's snapshots (both stores, worktrees
  included), newest first, a line each with id, age, item count, size and command; `--json` the manifests;
  `--all` adds every snapshot in the global store; outside a repository it acts as `--all`; none is `No backups.`.
- `harness-work backup restore ID [--path P] [--to DIR] [--force]`: every item back at its path, `restored <path>`
  each; an unknown id, or an existing path without --force, is one line, exit 1 and nothing written; --force
  snapshots what it replaces first and prints that id; --path picks the items at or under P; --to restores under
  DIR at the path relative to the snapshot's repository (the basename without one), never its cwd and never
  outside DIR, and with --force snapshots what it replaces there. Bytes, file and folder modes (empty folders
  too) and link targets come back.

Fixtures: a temporary HOME per test (the module's own one for the whole process, so nothing here can reach the
real ~/.claude), real temporary git repositories and a linked worktree made with `git worktree add`, random file
contents, umask 022 and modes with group and other bits a umask would drop. GIT_CEILING_DIRECTORIES keeps git
from finding a repository above the test's folder. create runs in process (harness_lib from plugins/harness/lib,
the environment replaced for the call); list and restore run harness-work as a subprocess.

Choices the ticket leaves open, fixed here:
- `at` and the id's time are `now`'s UTC second when `now` is given; a relative path is taken from `cwd`, and an
  item's path is the path given, made absolute, never a symlink's target.
- A dangling symlink is an existing symlink (copied as a link); a symlink at an original path, dangling or not,
  is an existing path for the restore refusal.
- The manifest's `sensitive` (ruling on #326): true when at least one item's path, taken relative to its own
  working tree (not the cwd, not the main checkout for a worktree file), matches a pattern; false otherwise, also
  in a project with patterns, whose every snapshot still goes in its own store. The snapshot restore --force
  takes is the snapshot's repository's like any other: repo is the main checkout, in that project's store.
- `--json` with no snapshots prints []. The unknown-id line names the id; an id that climbs out of the store
  (`..`, an absolute path) is unknown.
- --force snapshots only the item paths that exist (a missing one is not replaced); a replaced directory comes
  back exactly as snapshotted (files added since go, kept in the --force snapshot); its id is printed before the
  first `restored` line. A relative --path is taken from the cwd.
- A snapshot taken outside any repository is restored with --to from outside any repository.
- list's age is checked by its number and unit (`3h`, `1d`), its size only for totals near 1.2 MB and 1.9 MB
  (one decimal, read the same in MB and MiB); the header line and column widths are not pinned.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_backups.py" -v
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
LIB = os.path.join(PLUGIN, "lib")
TOOL = os.path.join(PLUGIN, "bin", "harness-work")
GIT = shutil.which("git") or "/usr/bin/git"
PATH = os.pathsep.join([os.path.dirname(GIT), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
MANIFEST_KEYS = {"id", "at", "repo", "cwd", "command", "session", "sensitive", "items", "refs"}
ITEM_KEYS = {"path", "copy", "kind", "bytes"}
UNKNOWN_ID = "20990101T000000Z-abcdef"

_SAVED = {}


def setUpModule():
    """One umask and a module HOME for the whole process: nothing below can reach the real ~/.claude."""
    _SAVED["umask"] = os.umask(0o022)
    _SAVED["home"] = os.environ.get("HOME")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-backups-module-"))
    _SAVED["root"] = root
    os.makedirs(os.path.join(root, "home"))
    os.environ["HOME"] = os.path.join(root, "home")


def tearDownModule():
    os.umask(_SAVED.get("umask", 0o022))
    if _SAVED.get("home") is not None:
        os.environ["HOME"] = _SAVED["home"]
    shutil.rmtree(_SAVED.get("root", ""), True)


# ================================================================ helpers
def tree(path):
    """Every entry at and under `path`, links never followed: relative path -> ("dir", mode),
    ("file", sha256, size, mode) or ("link", target)."""
    out = {}

    def walk(p, rel):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            out[rel] = ("link", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            for name in sorted(os.listdir(p)):
                walk(os.path.join(p, name), name if rel == "." else rel + "/" + name)
        else:
            with open(p, "rb") as fh:
                data = fh.read()
            out[rel] = ("file", hashlib.sha256(data).hexdigest(), len(data), stat.S_IMODE(st.st_mode))

    walk(path, ".")
    return out


def holds(root, data):
    """Whether any regular file at or under `root` (links not followed) contains `data`."""
    if not os.path.lexists(root):
        return False
    if os.path.isfile(root) and not os.path.islink(root):
        with open(root, "rb") as fh:
            return data in fh.read()
    for folder, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            p = os.path.join(folder, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                if data in fh.read():
                    return True
    return False


def files_under(root):
    """Every non-directory entry at or under `root` (links not followed)."""
    if not os.path.isdir(root):
        return []
    found = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        found += [os.path.join(folder, n) for n in files]
        found += [os.path.join(folder, n) for n in dirs if os.path.islink(os.path.join(folder, n))]
    return found


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def real(path):
    return os.path.realpath(str(path))


def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-backups-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        seed = int.from_bytes(os.urandom(8), "big")
        self.rng = random.Random(seed)
        self.seed = seed                    # named in the failure messages that depend on random choices
        self.now = dt.datetime.now(dt.timezone.utc)

    # ----------------------------------------------------------- files and repositories
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def word(self):
        return "%08x" % self.rng.getrandbits(32)

    def put(self, path, size=None, mode=0o644, data=None):
        """A file of `data` (else `size` random bytes, else 1 to 5000) with `mode`; returns its bytes."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if data is None:
            data = os.urandom(self.rng.randint(1, 5000) if size is None else size)
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, mode)
        return data

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "HARNESS_", "GIT_"))}
        env.update(HOME=self.home, PATH=PATH, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   GIT_CEILING_DIRECTORIES=os.path.dirname(self.tmp),
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@test.invalid",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@test.invalid")
        env.update(extra)
        return env

    def git(self, cwd, *args):
        p = subprocess.run([GIT, "-C", cwd, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def make_repo(self, name):
        path = self.mk(name)
        self.git(path, "init", "-q", "-b", "main")
        self.put(os.path.join(path, "README.md"), data=f"# {name}\n".encode())
        self.put(os.path.join(path, ".gitignore"), data=b".claude/worktrees/\n")
        self.git(path, "add", "README.md", ".gitignore")
        self.git(path, "commit", "-q", "-m", "First")
        return path

    def commit_settings(self, main, env):
        self.put(os.path.join(main, ".claude", "settings.json"), data=json.dumps({"env": env}).encode())
        self.git(main, "add", ".claude/settings.json")
        self.git(main, "commit", "-q", "-m", "Settings")

    def add_worktree(self, main, name):
        path = os.path.join(main, ".claude", "worktrees", name)
        self.git(main, "worktree", "add", "-q", "-b", "t/" + name, path)
        return os.path.realpath(path)

    def global_store(self):
        return os.path.join(self.home, ".claude", "harness", "backups")

    def project_store(self, main):
        return os.path.join(main, ".git", "harness-backups")

    def assertGlobalStoreEmpty(self):
        self.assertEqual(files_under(self.global_store()), [], f"written under the global store (seed {self.seed})")

    # ----------------------------------------------------------- the lib, in process
    @contextlib.contextmanager
    def lib(self, env=None):
        """harness_lib.backups with this test's environment (HOME included) for the duration; the import
        error while the module does not exist is the red state."""
        with mock.patch.dict(os.environ, self.env(**(env or {})), clear=True):
            if LIB not in sys.path:
                sys.path.insert(0, LIB)
            importlib.invalidate_caches()
            yield importlib.import_module("harness_lib.backups")

    def create(self, paths, *, main, cwd, command=None, session=None, now=None, env=None):
        kwargs = {"main": main, "cwd": cwd, "command": command or f"rm -rf {self.word()}"}
        if session is not None:
            kwargs["session"] = session
        if now is not None:
            kwargs["now"] = now
        with self.lib(env) as backups:
            manifest = backups.create(list(paths), **kwargs)
        self.assertIsInstance(manifest, dict)
        self.assertRegex(manifest.get("id", ""), r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
        return manifest

    def ago(self, **delta):
        return self.now - dt.timedelta(**delta)

    # ----------------------------------------------------------- the command
    def work(self, *args, cwd, env=None):
        p = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd, env=env or self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def ok(self, *args, cwd, env=None):
        p = self.work(*args, cwd=cwd, env=env)
        self.assertEqual(p.returncode, 0, f"harness-work {' '.join(args)}: {p.stdout}{p.stderr} (seed {self.seed})")
        return p

    def ok_json(self, *args, cwd, env=None):
        return json.loads(self.ok(*args, "--json", cwd=cwd, env=env).stdout)

    def assertRefused(self, p, *needles):
        """Exit 1, one stderr line naming each needle, not a crash's `<Type>Error:` line, nothing restored."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        lines = p.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertIsNone(re.match(r"^harness-work: \w+(Error|Exception)\b", lines[0]), lines[0])
        for needle in needles:
            self.assertIn(needle, lines[0])
        self.assertNotIn("restored ", p.stdout)

    @staticmethod
    def restored(p):
        return {line[len("restored "):].strip() for line in p.stdout.splitlines() if line.startswith("restored ")}

    @staticmethod
    def rows(text):
        """[(id, line)] for every line of `text` that holds a snapshot id, in order."""
        out = []
        for line in text.splitlines():
            found = ID_RE.search(line)
            if found:
                out.append((found.group(0), line))
        return out


# ================================================================ create
class Create(BackupTest):
    def test_create_copies_and_writes_the_manifest(self):
        main = self.make_repo("proj")
        outside = self.mk("outside")
        secret = self.put(os.path.join(outside, "target", "secret.bin"), size=4096 + self.rng.randint(0, 999))
        a1, a2 = os.path.join(main, "notes", "a.txt"), os.path.join(main, "other", "a.txt")
        d1, d2 = self.put(a1, mode=0o664), self.put(a2, mode=0o600)
        cache = os.path.join(main, "cache")
        sizes = [len(self.put(os.path.join(cache, rel), mode=m))
                 for rel, m in (("x.bin", 0o640), ("sub/y.bin", 0o775), ("sub/deep/z.bin", 0o600))]
        os.symlink(os.path.join(outside, "target"), os.path.join(cache, "sub", "out"))   # inside the tree
        link = os.path.join(main, "target-link")
        os.symlink(os.path.join(outside, "target"), link)                              # a link to a folder
        tz = dt.timezone(dt.timedelta(hours=self.rng.choice([-7, -3, 2, 5, 9]), minutes=self.rng.choice([0, 30])))
        when = dt.datetime(self.rng.randint(2025, 2027), self.rng.randint(1, 12), self.rng.randint(1, 28),
                           self.rng.randint(0, 23), self.rng.randint(0, 59), self.rng.randint(0, 59), tzinfo=tz)
        utc = when.astimezone(dt.timezone.utc)
        session, command = "sess-" + self.word(), "rm -rf notes other cache target-link " + self.word()
        before = {p: tree(p) for p in (a1, a2, cache, link)}

        m = self.create(["notes/a.txt", a2, cache, link], main=main, cwd=main, command=command, session=session,
                        now=when)

        self.assertEqual(set(m), MANIFEST_KEYS)
        self.assertTrue(m["id"].startswith(utc.strftime("%Y%m%dT%H%M%SZ-")), (m["id"], utc))
        self.assertEqual(m["at"], utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual([m["repo"], m["cwd"], m["command"], m["session"], m["sensitive"], m["refs"]],
                         [main, main, command, session, False, []])
        items = {i["path"]: i for i in m["items"]}
        self.assertEqual(set(items), {a1, a2, cache, link})
        for item in m["items"]:
            self.assertEqual(set(item), ITEM_KEYS)
            self.assertTrue(item["copy"].startswith("files/"), item)
            self.assertIsInstance(item["bytes"], int)
            self.assertNotIsInstance(item["bytes"], bool)
        self.assertEqual(len({i["copy"] for i in m["items"]}), 4, "two items share a copy")
        self.assertEqual({p: i["kind"] for p, i in items.items()},
                         {a1: "file", a2: "file", cache: "dir", link: "link"})
        self.assertEqual((items[a1]["bytes"], items[a2]["bytes"]), (len(d1), len(d2)))
        self.assertGreaterEqual(items[cache]["bytes"], sum(sizes))
        self.assertLess(items[cache]["bytes"], sum(sizes) + 65536)

        snap = os.path.join(self.global_store(), m["id"])
        with open(os.path.join(snap, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), m)
        for path, item in items.items():
            self.assertEqual(tree(os.path.join(snap, item["copy"])), before[path], path)
        self.assertTrue(os.path.islink(os.path.join(snap, items[link]["copy"])))
        self.assertFalse(holds(snap, secret), "a symlink was followed into the store")
        self.assertEqual({p: tree(p) for p in before}, before, "create changed an original")

        again = self.create([a1], main=main, cwd=main, now=when)      # the same second: a new suffix
        self.assertNotEqual(again["id"], m["id"])
        self.assertEqual(again["id"][:17], m["id"][:17])
        self.assertIsNone(again["session"])
        self.assertTrue(os.path.isfile(os.path.join(self.global_store(), again["id"], "manifest.json")))
        with open(os.path.join(snap, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), m, "the clash overwrote the first snapshot")
        with self.lib() as backups:
            self.assertEqual(backups.find(main, m["id"]), m)
            self.assertEqual(backups.find(main, again["id"]), again)
            self.assertIsNone(backups.find(main, UNKNOWN_ID))

    def test_missing_paths_are_skipped(self):
        main = self.make_repo("proj")
        keep = os.path.join(main, "keep.txt")
        self.put(keep, mode=0o664)
        keepdir = os.path.join(main, "keepdir")
        self.put(os.path.join(keepdir, "inner", "f.bin"), mode=0o600)
        gone, ghost_child, ghost_rel = (os.path.join(main, "gone.txt"), os.path.join(main, "no-such", "child.txt"),
                                        "never-" + self.word())
        before = {p: tree(p) for p in (keep, keepdir)}

        m = self.create([gone, keep, ghost_child, keepdir, ghost_rel], main=main, cwd=main)

        self.assertEqual(sorted(i["path"] for i in m["items"]), sorted([keep, keepdir]))
        files = os.path.join(self.global_store(), m["id"], "files")
        copies = sorted(i["copy"][len("files/"):].split("/")[0] for i in m["items"])
        self.assertEqual(sorted(os.listdir(files)), copies, "a missing path was copied")
        for p in (keep, keepdir):
            remove(p)
        p = self.ok("backup", "restore", m["id"], cwd=main)
        self.assertEqual(self.restored(p), {keep, keepdir})
        self.assertEqual({q: tree(q) for q in (keep, keepdir)}, before)
        for missing in (gone, ghost_child, os.path.join(main, "no-such"), os.path.join(main, ghost_rel)):
            self.assertFalse(os.path.lexists(missing), missing)


# ================================================================ restore
class Restore(BackupTest):
    def test_create_then_restore_round_trip(self):
        main = self.make_repo("proj")
        outside = self.mk("outside")
        self.put(os.path.join(outside, "target", "secret.bin"))
        target_before = tree(os.path.join(outside, "target"))
        notes = os.path.join(main, "notes.txt")
        self.put(notes, mode=0o664)
        cache = os.path.join(main, "build-cache")
        self.put(os.path.join(cache, "a.bin"), mode=0o600)
        self.put(os.path.join(cache, "sub", "b.txt"), mode=0o775)
        self.put(os.path.join(cache, "sub", "deeper", "c.dat"), mode=0o640)
        os.makedirs(os.path.join(cache, "sub", "empty"))
        os.makedirs(os.path.join(cache, "open-empty"))
        os.symlink(os.path.join(outside, "target"), os.path.join(cache, "sub", "out"))
        os.symlink("deeper/c.dat", os.path.join(cache, "sub", "rel"))
        for rel, m_ in (("sub/empty", 0o750), ("sub/deeper", 0o700), ("sub", 0o750), ("open-empty", 0o775),
                        (".", 0o700)):                       # folders come back with their own modes
            os.chmod(os.path.join(cache, rel), m_)
        report = os.path.join(main, "gen", "out", "x", "report.txt")     # its folders go with it
        self.put(report, mode=0o660)
        latest = os.path.join(main, "latest")
        os.symlink("notes.txt", latest)
        dirlink = os.path.join(main, "dirlink")
        os.symlink(os.path.join(outside, "target"), dirlink)
        dangling = os.path.join(main, "dangling")
        os.symlink("nowhere/missing-" + self.word(), dangling)
        paths = [notes, cache, report, latest, dirlink, dangling]
        before = {p: tree(p) for p in paths}

        m = self.create(paths, main=main, cwd=main, command="rm -rf build-cache gen notes.txt latest dirlink")
        self.assertEqual({i["path"]: i["kind"] for i in m["items"]},
                         {notes: "file", cache: "dir", report: "file", latest: "link", dirlink: "link",
                          dangling: "link"})
        for p in paths:
            remove(p)
        shutil.rmtree(os.path.join(main, "gen"))

        p = self.ok("backup", "restore", m["id"], cwd=main)
        self.assertEqual(self.restored(p), set(paths))
        self.assertEqual({q: tree(q) for q in paths}, before, f"seed {self.seed}")
        self.assertEqual(tree(os.path.join(outside, "target")), target_before, "the restore wrote through a link")

    def test_restore_refuses_an_existing_path_without_force(self):
        main = self.make_repo("proj")
        a = os.path.join(main, "a.txt")
        self.put(a)
        blink = os.path.join(main, "b-link")
        os.symlink("a.txt", blink)
        zz = os.path.join(main, "zz-dir")                  # last in the order given and in sorted order
        self.put(os.path.join(zz, "keep.bin"))
        m = self.create([a, blink, zz], main=main, cwd=main)
        remove(a)
        remove(blink)
        self.put(os.path.join(zz, "keep.bin"))             # changed since the snapshot
        self.put(os.path.join(zz, "new.bin"))
        zz_now = tree(zz)
        store_before = {q: tree(q) for q in [self.global_store()]}

        p = self.work("backup", "restore", m["id"], cwd=main)
        self.assertRefused(p, zz)
        self.assertFalse(os.path.lexists(a), "restored part of a refused snapshot")
        self.assertFalse(os.path.lexists(blink), "restored part of a refused snapshot")
        self.assertEqual(tree(zz), zz_now)
        self.assertEqual({q: tree(q) for q in [self.global_store()]}, store_before, "the refusal wrote a snapshot")

        # A symlink at the original path exists too, dangling or not; nothing is written through it.
        q = os.path.join(main, "q.txt")
        self.put(q)
        mq = self.create([q], main=main, cwd=main)
        remove(q)
        never = os.path.join(self.mk("elsewhere"), "never-" + self.word())
        os.symlink(never, q)
        p = self.work("backup", "restore", mq["id"], cwd=main)
        self.assertRefused(p, q)
        self.assertEqual(os.readlink(q), never)
        self.assertFalse(os.path.lexists(never), "the restore wrote through a dangling link")

    def test_restore_force_snapshots_what_it_replaces(self):
        main = self.make_repo("proj")
        f = os.path.join(main, "f.txt")
        self.put(f, mode=0o664)
        d = os.path.join(main, "d")
        self.put(os.path.join(d, "one.bin"), mode=0o640)
        self.put(os.path.join(d, "two", "three.bin"), mode=0o775)
        gone = os.path.join(main, "gone.txt")
        self.put(gone, mode=0o600)
        lnk = os.path.join(main, "lnk")
        os.symlink("f.txt", lnk)
        paths = [f, d, gone, lnk]
        original = {p: tree(p) for p in paths}
        m = self.create(paths, main=main, cwd=main)

        self.put(f, mode=0o600)                              # what --force replaces
        self.put(os.path.join(d, "one.bin"), mode=0o664)
        self.put(os.path.join(d, "added-later.bin"))
        remove(gone)                                         # missing: nothing to replace
        remove(lnk)
        os.symlink("d", lnk)
        replaced = {p: tree(p) for p in (f, d, lnk)}

        p = self.ok("backup", "restore", m["id"], "--force", cwd=main)
        new_ids = [i for i in ID_RE.findall(p.stdout) if i != m["id"]]
        self.assertEqual(len(set(new_ids)), 1, p.stdout)
        new_id = new_ids[0]
        first_restored = p.stdout.find("restored ")
        self.assertGreaterEqual(first_restored, 0, p.stdout)
        self.assertLess(p.stdout.find(new_id), first_restored, "the --force snapshot's id comes first")
        self.assertEqual(self.restored(p), set(paths))
        self.assertEqual({q: tree(q) for q in paths}, original, f"seed {self.seed}")

        with self.lib() as backups:
            taken = backups.find(main, new_id)
        self.assertIsNotNone(taken, "the --force snapshot cannot be found")
        self.assertEqual(taken["repo"], main)
        self.assertIn(new_id, [x["id"] for x in self.ok_json("backup", "list", cwd=main)])
        self.assertEqual(sorted(i["path"] for i in taken["items"]), sorted([f, d, lnk]))
        snap = os.path.join(self.global_store(), new_id)
        for item in taken["items"]:
            self.assertEqual(tree(os.path.join(snap, item["copy"])), replaced[item["path"]], item["path"])

        # The --force snapshot is a snapshot like any other: restoring it brings the replaced state back.
        p = self.ok("backup", "restore", new_id, "--force", cwd=main)
        self.assertEqual({q: tree(q) for q in (f, d, lnk)}, replaced)
        self.assertEqual(tree(gone), original[gone])

    def test_restore_only_one_path(self):
        main = self.make_repo("proj")
        a = os.path.join(main, "notes", "a.txt")
        b = os.path.join(main, "notes", "deep", "b.txt")
        notes2 = os.path.join(main, "notes2.txt")           # a string prefix of notes/, not under it
        cache = os.path.join(main, "cache")
        other = os.path.join(main, "other.txt")
        for path, m_ in ((a, 0o664), (b, 0o600), (notes2, 0o644), (other, 0o775)):
            self.put(path, mode=m_)
        self.put(os.path.join(cache, "c.bin"), mode=0o640)
        paths = [a, b, notes2, cache, other]
        before = {p: tree(p) for p in paths}
        m = self.create(paths, main=main, cwd=main)
        for path in paths:
            remove(path)

        p = self.ok("backup", "restore", m["id"], "--path", os.path.join(main, "notes"), cwd=main)
        self.assertEqual(self.restored(p), {a, b})
        self.assertEqual({q: tree(q) for q in (a, b)}, {q: before[q] for q in (a, b)})
        for q in (notes2, cache, other):
            self.assertFalse(os.path.lexists(q), q)

        # Only the chosen items are checked for existing paths: notes/ is back, cache still restores.
        p = self.ok("backup", "restore", m["id"], "--path", cache, cwd=main)
        self.assertEqual(self.restored(p), {cache})
        self.assertEqual(tree(cache), before[cache])
        self.assertFalse(os.path.lexists(notes2))
        self.assertFalse(os.path.lexists(other))

        p = self.ok("backup", "restore", m["id"], "--path", "other.txt", cwd=main)     # relative to the cwd
        self.assertEqual(self.restored(p), {other})
        self.assertEqual(tree(other), before[other])
        self.assertFalse(os.path.lexists(notes2))

        p = self.work("backup", "restore", m["id"], "--path", os.path.join(main, "notes"), cwd=main)
        self.assertRefused(p)
        self.assertTrue(a in p.stderr or b in p.stderr, p.stderr)

    def test_restore_to_another_folder(self):
        main = self.make_repo("proj")
        src = os.path.join(main, "src", "a.txt")
        self.put(src, mode=0o664)
        lnk = os.path.join(main, "lnk")
        os.symlink("src/a.txt", lnk)
        data = os.path.join(main, "data")
        self.put(os.path.join(data, "x.bin"), mode=0o600)
        self.put(os.path.join(data, "deep", "y.bin"), mode=0o775)
        os.makedirs(os.path.join(data, "empty"))
        for rel, m_ in (("empty", 0o700), ("deep", 0o750), (".", 0o750)):
            os.chmod(os.path.join(data, rel), m_)
        paths = [src, lnk, data]
        before = {p: tree(p) for p in paths}
        m = self.create(paths, main=main, cwd=main)          # the originals stay where they are

        out = self.mk("out")
        self.ok("backup", "restore", m["id"], "--to", out, cwd=main)
        self.assertEqual({rel: tree(os.path.join(out, rel)) for rel in ("src/a.txt", "lnk", "data")},
                         {"src/a.txt": before[src], "lnk": before[lnk], "data": before[data]})
        self.assertEqual({p: tree(p) for p in paths}, before, "--to touched an original")

        # Relative to the snapshot's repository, not its cwd; nothing lands outside DIR.
        m_src = self.create([src, data], main=main, cwd=os.path.join(main, "src"))
        out_src = self.mk("out-src")
        def worktree_files():
            return {k: v for k, v in tree(main).items() if k != ".git" and not k.startswith(".git/")}

        tmp_before, main_before = sorted(os.listdir(self.tmp)), worktree_files()
        self.ok("backup", "restore", m_src["id"], "--to", out_src, cwd=main)
        self.assertEqual(sorted(os.listdir(out_src)), ["data", "src"])
        self.assertEqual({rel: tree(os.path.join(out_src, rel)) for rel in ("src/a.txt", "data")},
                         {"src/a.txt": before[src], "data": before[data]})
        self.assertEqual(sorted(os.listdir(self.tmp)), tmp_before, "--to wrote outside DIR")
        self.assertEqual(worktree_files(), main_before, "--to wrote into the repository")

        # An existing path under DIR refuses the whole restore.
        out3 = self.mk("out3")
        blocker = os.path.join(out3, "data")
        blocker_bytes = self.put(blocker)
        blocker_before = tree(blocker)
        p = self.work("backup", "restore", m["id"], "--to", out3, cwd=main)
        self.assertRefused(p, blocker)
        self.assertEqual(sorted(os.listdir(out3)), ["data"])
        self.assertEqual(tree(blocker), blocker_before)

        # --to with --force snapshots the blocker first, at its path under DIR, then restores.
        p = self.ok("backup", "restore", m["id"], "--to", out3, "--force", cwd=main)
        new_ids = [i for i in ID_RE.findall(p.stdout) if i not in (m["id"], m_src["id"])]
        self.assertEqual(len(set(new_ids)), 1, p.stdout)
        new_id = new_ids[0]
        first_restored = p.stdout.find("restored ")
        self.assertGreaterEqual(first_restored, 0, p.stdout)
        self.assertLess(p.stdout.find(new_id), first_restored, "the --force snapshot's id comes first")
        snap_dirs = [os.path.join(s, new_id) for s in (self.global_store(), self.project_store(main))
                     if os.path.isfile(os.path.join(s, new_id, "manifest.json"))]
        self.assertEqual(len(snap_dirs), 1, f"no store holds {new_id}")
        with open(os.path.join(snap_dirs[0], "manifest.json"), encoding="utf-8") as fh:
            taken = json.load(fh)
        self.assertEqual([i["path"] for i in taken["items"]], [blocker])
        self.assertEqual(tree(os.path.join(snap_dirs[0], taken["items"][0]["copy"])), blocker_before)
        self.assertTrue(holds(snap_dirs[0], blocker_bytes))
        self.assertEqual({rel: tree(os.path.join(out3, rel)) for rel in ("src/a.txt", "lnk", "data")},
                         {"src/a.txt": before[src], "lnk": before[lnk], "data": before[data]})

        # With --path, only the chosen items go under DIR.
        out5 = self.mk("out5")
        self.ok("backup", "restore", m["id"], "--path", data, "--to", out5, cwd=main)
        self.assertEqual(sorted(os.listdir(out5)), ["data"])
        self.assertEqual(tree(os.path.join(out5, "data")), before[data])

        # A snapshot taken outside any repository goes under DIR by basename.
        loose = self.mk("loose")
        one = os.path.join(loose, "one.txt")
        self.put(one, mode=0o660)
        two = os.path.join(loose, "nested", "two")
        self.put(os.path.join(two, "t.bin"), mode=0o640)
        loose_before = {"one.txt": tree(one), "two": tree(two)}
        mn = self.create([one, two], main=None, cwd=loose)
        self.assertIsNone(mn["repo"])
        out4 = self.mk("out4")
        self.ok("backup", "restore", mn["id"], "--to", out4, cwd=self.mk("nowhere"))
        self.assertEqual({rel: tree(os.path.join(out4, rel)) for rel in loose_before}, loose_before)
        self.assertEqual(sorted(os.listdir(out4)), ["one.txt", "two"])

    def test_unknown_id_is_one_line_exit_1(self):
        main = self.make_repo("proj")
        f = os.path.join(main, "f.txt")
        self.put(f)
        real_snap = self.create([f], main=main, cwd=main)
        remove(f)
        pwned = os.path.join(main, "pwned.txt")
        evil = os.path.join(self.home, ".claude", "harness", "evil")     # a manifest outside the store
        self.put(os.path.join(evil, "files", "0"), data=b"bad\n")
        with open(os.path.join(evil, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "../evil", "at": "2026-11-04T10:15:00Z", "repo": main, "cwd": main,
                       "command": "rm pwned.txt", "session": None, "sensitive": False,
                       "items": [{"path": pwned, "copy": "files/0", "kind": "file", "bytes": 4}], "refs": []}, fh)

        for bad in (UNKNOWN_ID, "nonsense", "../evil", evil, real_snap["id"] + "/../../evil"):
            p = self.work("backup", "restore", bad, cwd=main)
            self.assertRefused(p, bad)
            self.assertEqual(p.stdout.strip(), "", bad)
            self.assertFalse(os.path.lexists(pwned), f"{bad!r} restored a manifest outside the store")
            self.assertFalse(os.path.lexists(f), bad)
        self.ok("backup", "restore", real_snap["id"], cwd=main)
        self.assertTrue(os.path.isfile(f))


# ================================================================ stores
class Stores(BackupTest):
    def test_sensitive_project_keeps_snapshots_inside_its_git_dir(self):
        main = self.make_repo("vault")
        wt = self.add_worktree(main, "t-1-wt")             # from the first commit: no settings file in it
        # The last pattern matches a worktree file only when its path is taken relative to the main checkout.
        self.commit_settings(main, {"HARNESS_SENSITIVE_PATHS": "secrets/**, *.pem, .claude/worktrees/*/inner/**"})
        self.assertFalse(os.path.exists(os.path.join(wt, ".claude", "settings.json")))
        store = self.project_store(main)
        mine = sorted([".claude/worktrees/*/inner/**", "*.pem", "secrets/**"])
        with self.lib() as backups:
            self.assertEqual(sorted(backups.sensitive_patterns(main)), mine)
            self.assertEqual(real(backups.store_dir(main)), real(store))
        with self.lib({"HARNESS_SENSITIVE_PATHS": "extra/**"}) as backups:
            self.assertEqual(sorted(backups.sensitive_patterns(main)), sorted(mine + ["extra/**"]))

        key, notes = os.path.join(main, "secrets", "key.txt"), os.path.join(main, "notes.txt")
        key_bytes = self.put(key, size=64, mode=0o600)
        notes_bytes = self.put(notes)
        m1 =self.create([key, notes], main=main, cwd=main, now=self.ago(hours=2))
        # secrets/** matches token.txt only relative to the worktree's own root; plain.txt matches nothing.
        token, plain_txt = os.path.join(wt, "secrets", "token.txt"), os.path.join(wt, "plain.txt")
        token_bytes = self.put(token, size=64, mode=0o600)
        plain_bytes = self.put(plain_txt, size=80, mode=0o640)
        wt_before = {p: tree(p) for p in (token, plain_txt)}
        m2 = self.create([token, plain_txt], main=main, cwd=wt, now=self.ago(hours=1))
        readme = os.path.join(main, "README.md")           # matches no pattern: the project's store all the same
        m3 = self.create([readme], main=main, cwd=main, now=self.ago(minutes=30))

        for m in (m1, m2, m3):
            with open(os.path.join(store, m["id"], "manifest.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), m)
            self.assertEqual(m["repo"], main)
        self.assertEqual(m2["cwd"], wt)

        # `sensitive` (ruling on #326): some item's path, relative to its own working tree, matches a pattern.
        loose_wt = os.path.join(wt, "loose.txt")                         # matches nothing
        inner = os.path.join(wt, "inner", "data.txt")                    # matches relative to main only
        deep = os.path.join(wt, "secrets", "deep", "t2.txt")             # relative to the cwd it would not match
        main_secret = os.path.join(main, "secrets", "x.txt")             # its own tree is main, not the cwd's
        for path in (loose_wt, inner, deep, main_secret):
            self.put(path)
        m4 = self.create([loose_wt], main=main, cwd=wt, now=self.ago(hours=3))
        m5 = self.create([inner], main=main, cwd=wt, now=self.ago(hours=4))
        m6 = self.create([deep], main=main, cwd=os.path.join(wt, "secrets"), now=self.ago(hours=5))
        m7 = self.create([main_secret], main=main, cwd=wt, now=self.ago(hours=6))
        cases = (("m1 main secrets/key.txt + notes.txt", m1, True),
                 ("m2 worktree secrets/token.txt + plain.txt", m2, True),
                 ("m3 main README.md", m3, False),
                 ("m4 worktree loose.txt", m4, False),
                 ("m5 worktree inner/data.txt", m5, False),
                 ("m6 worktree secrets/deep/t2.txt, cwd secrets/", m6, True),
                 ("m7 main secrets/x.txt, cwd the worktree", m7, True))
        self.assertEqual({label: m["sensitive"] for label, m, _ in cases},
                         {label: want for label, _, want in cases})
        for m in (m4, m5, m6, m7):                                       # the project's store either way
            self.assertTrue(os.path.isfile(os.path.join(store, m["id"], "manifest.json")), m["id"])

        wt_git_dir = os.path.join(wt, self.git(wt, "rev-parse", "--git-dir"))
        self.assertFalse(os.path.lexists(os.path.join(wt_git_dir, "harness-backups")))
        self.assertGlobalStoreEmpty()
        for secret in (key_bytes, token_bytes, plain_bytes):
            self.assertFalse(holds(self.home, secret), "a sensitive project's copy left the project")

        older = [m["id"] for m in (m3, m2, m1, m4, m5, m6, m7)]
        listed = self.ok_json("backup", "list", cwd=wt)
        self.assertEqual([x["id"] for x in listed], older)
        remove(token)
        remove(plain_txt)
        p = self.ok("backup", "restore", m2["id"], cwd=wt)
        self.assertEqual(self.restored(p), {token, plain_txt})
        self.assertEqual({q: tree(q) for q in (token, plain_txt)}, wt_before)
        self.assertGlobalStoreEmpty()

        # restore --force from the main checkout and from the worktree: what it replaces stays in the project.
        forced, new_bytes = [], []
        for snap, cwd, paths, originals in ((m1, main, (key, notes), (key_bytes, notes_bytes)),
                                            (m2, wt, (token, plain_txt), (token_bytes, plain_bytes))):
            fresh = [self.put(path, size=96, mode=0o600) for path in paths]
            new_bytes += fresh
            p = self.ok("backup", "restore", snap["id"], "--force", cwd=cwd)
            new_ids = [i for i in ID_RE.findall(p.stdout) if i != snap["id"]]
            self.assertEqual(len(set(new_ids)), 1, p.stdout)
            new_id = new_ids[0]
            forced.append(new_id)
            with open(os.path.join(store, new_id, "manifest.json"), encoding="utf-8") as fh:
                taken = json.load(fh)
            self.assertEqual((taken["id"], taken["repo"], taken["sensitive"]), (new_id, main, True), cwd)
            self.assertEqual(sorted(i["path"] for i in taken["items"]), sorted(paths))
            for data in fresh:
                self.assertTrue(holds(os.path.join(store, new_id), data), "the --force snapshot lacks a replaced file")
            for path, data in zip(paths, originals):
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), data, path)
        self.assertGlobalStoreEmpty()
        for data in new_bytes:
            self.assertFalse(holds(self.home, data), "restore --force copied a sensitive project's file out of it")
        ids = [x["id"] for x in self.ok_json("backup", "list", cwd=main)]
        self.assertEqual(set(ids[:2]), set(forced), ids)
        self.assertEqual(ids[2:], older)

        # A linked worktree outside the main checkout: git, not the folder layout, names an item's working tree.
        elsewhere = os.path.join(self.tmp, "elsewhere-wt")
        self.git(main, "worktree", "add", "-q", "-b", "t/elsewhere", elsewhere)
        far = os.path.join(elsewhere, "secrets", "t.txt")
        far_bytes = self.put(far, size=64, mode=0o600)
        m8 = self.create([far], main=main, cwd=os.path.join(elsewhere, "secrets"))
        self.assertEqual((m8["repo"], m8["sensitive"]), (main, True))
        with open(os.path.join(store, m8["id"], "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), m8)
        self.assertTrue(holds(os.path.join(store, m8["id"]), far_bytes), "the copy is not in the project's store")
        self.assertGlobalStoreEmpty()
        self.assertFalse(holds(self.home, far_bytes))

        # The pattern in the environment does the same for a project with no settings file.
        other = self.make_repo("envproj")
        idfile = os.path.join(other, "id.key")
        id_bytes = self.put(idfile, size=64)
        mo = self.create([idfile], main=other, cwd=other, env={"HARNESS_SENSITIVE_PATHS": "*.key"})
        self.assertTrue(os.path.isfile(os.path.join(self.project_store(other), mo["id"], "manifest.json")))
        self.assertTrue(mo["sensitive"])
        self.assertGlobalStoreEmpty()
        self.assertFalse(holds(self.home, id_bytes))

        # A project without patterns, and no repository at all, use the global store.
        plain = self.make_repo("plain")
        with self.lib() as backups:
            self.assertEqual(backups.sensitive_patterns(plain), [])
            self.assertEqual(real(backups.store_dir(plain)), real(self.global_store()))
            self.assertEqual(real(backups.store_dir(None)), real(self.global_store()))

    def test_store_folders_are_private(self):
        main = self.make_repo("proj")
        f = os.path.join(main, "f.txt")
        self.put(f)
        m = self.create([f], main=main, cwd=main)
        self.assertEqual(oct(mode(self.global_store())), oct(0o700))
        self.assertEqual(oct(mode(os.path.join(self.global_store(), m["id"]))), oct(0o700))

        other = self.make_repo("secretive")
        g = os.path.join(other, "g.txt")
        self.put(g)
        mo = self.create([g], main=other, cwd=other, env={"HARNESS_SENSITIVE_PATHS": "*.txt"})
        self.assertEqual(oct(mode(self.project_store(other))), oct(0o700))
        self.assertEqual(oct(mode(os.path.join(self.project_store(other), mo["id"]))), oct(0o700))

        # The snapshot restore --force takes is private too.
        self.put(f)
        p = self.ok("backup", "restore", m["id"], "--force", cwd=main)
        new_ids = [i for i in ID_RE.findall(p.stdout) if i != m["id"]]
        self.assertEqual(len(set(new_ids)), 1, p.stdout)
        self.assertEqual(oct(mode(os.path.join(self.global_store(), new_ids[0]))), oct(0o700))


# ================================================================ list
class List(BackupTest):
    def five(self):
        """alpha's snapshots from its worktree (3 h ago, 3 items, ~1.9 MB), in its own store (1 d ago, 1 item) and
        from its main checkout (2 d ago, 2 items, ~1.2 MB); beta's (1 h ago); one outside any repository (4 h ago).
        Made in an order that is neither newest nor oldest first."""
        a = self.make_repo("alpha")
        wt = self.add_worktree(a, "t-7-wt")
        b = self.make_repo("beta")
        loose = self.mk("loose")
        s = {"a": a, "wt": wt, "b": b, "loose": loose, "cmd": {}, "m": {}}

        def snap(name, paths, main, cwd, **kw):
            cmd = f"rm -rf {name}-{self.word()}"
            s["cmd"][name] = cmd
            s["m"][name] = self.create(paths, main=main, cwd=cwd, command=cmd, **kw)

        big = os.path.join(wt, "big.bin")
        self.put(big, size=1_940_000)
        self.put(os.path.join(wt, "w1.txt"), size=1000)
        self.put(os.path.join(wt, "w2.txt"), size=1000)
        snap("wt", [big, os.path.join(wt, "w1.txt"), os.path.join(wt, "w2.txt")], a, wt, now=self.ago(hours=3))
        self.put(os.path.join(b, "q.txt"))
        snap("b", [os.path.join(b, "q.txt")], b, b, now=self.ago(hours=1))
        self.put(os.path.join(a, "big.bin"), size=1_226_000)
        self.put(os.path.join(a, "small", "s1.bin"), size=1000)
        self.put(os.path.join(a, "small", "s2.bin"), size=1000)
        snap("main", [os.path.join(a, "big.bin"), os.path.join(a, "small")], a, a, now=self.ago(days=2))
        self.put(os.path.join(loose, "r.txt"))
        snap("null", [os.path.join(loose, "r.txt")], None, loose, now=self.ago(hours=4))
        self.put(os.path.join(a, "creds.secret"))
        snap("sens", [os.path.join(a, "creds.secret")], a, a, now=self.ago(days=1),
             env={"HARNESS_SENSITIVE_PATHS": "*.secret"})
        self.assertTrue(os.path.isdir(os.path.join(self.project_store(a), s["m"]["sens"]["id"])))
        return s

    def ids(self, s, *names):
        return [s["m"][n]["id"] for n in names]

    def test_list_shows_this_repos_snapshots_newest_first(self):
        s = self.five()
        expected = {"wt": ("3h", 3, r"1\.9\s*M"), "sens": ("1d", 1, None), "main": ("2d", 2, r"1\.2\s*M")}
        runs = [("cwd", s["a"], self.env(), ()), ("worktree", s["wt"], self.env(), ()),
                ("--repo", s["loose"], self.env(), ("--repo", s["a"])),
                ("CLAUDE_PROJECT_DIR", s["loose"], self.env(CLAUDE_PROJECT_DIR=s["a"]), ())]
        for how, cwd, env, extra in runs:
            with self.subTest(how=how):
                p = self.ok("backup", "list", *extra, cwd=cwd, env=env)
                rows = self.rows(p.stdout)
                self.assertEqual([r[0] for r in rows], self.ids(s, "wt", "sens", "main"), p.stdout)
                for (sid, line), name in zip(rows, ("wt", "sens", "main")):
                    age, count, size = expected[name]
                    cmd = s["cmd"][name]
                    found = re.match(rf"^\s*{re.escape(sid)}\s+(?P<age>.+?)\s+(?P<items>\d+)\s+(?P<size>.+?)\s+"
                                     rf"{re.escape(cmd)}\s*$", line)
                    self.assertIsNotNone(found, f"not id, age, items, size, command: {line!r}")
                    self.assertRegex(found.group("age"), rf"^{age}\b", line)
                    self.assertEqual(int(found.group("items")), count, line)
                    if size:
                        self.assertRegex(found.group("size"), size, line)
                for name in ("b", "null"):
                    self.assertNotIn(s["m"][name]["id"], p.stdout)
                    self.assertNotIn(s["cmd"][name], p.stdout)

        with self.lib() as backups:
            self.assertEqual([x["id"] for x in backups.snapshots(s["a"])], self.ids(s, "wt", "sens", "main"))
            self.assertEqual([x["id"] for x in backups.snapshots(s["a"], everything=True)],
                             self.ids(s, "b", "wt", "null", "sens", "main"))

    def test_list_json_all_and_empty(self):
        gamma = self.make_repo("gamma")
        nowhere = self.mk("nowhere")
        for cwd, extra in ((gamma, ()), (gamma, ("--all",)), (nowhere, ())):
            p = self.ok("backup", "list", *extra, cwd=cwd)
            self.assertEqual(p.stdout.strip(), "No backups.")
            self.assertEqual(self.ok_json("backup", "list", *extra, cwd=cwd), [])

        s = self.five()
        p = self.ok("backup", "list", cwd=gamma)                       # the others' are left out
        self.assertEqual(p.stdout.strip(), "No backups.")
        self.assertEqual(self.ok_json("backup", "list", cwd=gamma), [])

        listed = self.ok_json("backup", "list", cwd=s["a"])
        self.assertEqual([x["id"] for x in listed], self.ids(s, "wt", "sens", "main"))
        for printed, name in zip(listed, ("wt", "sens", "main")):
            for k, v in s["m"][name].items():
                self.assertEqual(printed.get(k), v, f"{name}: {k}")

        everything = self.ids(s, "b", "wt", "null", "sens", "main")
        global_only = self.ids(s, "b", "wt", "null", "main")
        self.assertEqual([x["id"] for x in self.ok_json("backup", "list", "--all", cwd=s["a"])], everything)
        self.assertEqual([r[0] for r in self.rows(self.ok("backup", "list", "--all", cwd=s["a"]).stdout)], everything)
        self.assertEqual([x["id"] for x in self.ok_json("backup", "list", "--all", cwd=gamma)], global_only)
        self.assertEqual([x["id"] for x in self.ok_json("backup", "list", cwd=nowhere)], global_only)
        self.assertEqual([r[0] for r in self.rows(self.ok("backup", "list", cwd=nowhere).stdout)], global_only)
        by_id = {x["id"]: x for x in self.ok_json("backup", "list", "--all", cwd=s["a"])}
        self.assertIsNone(by_id[s["m"]["null"]["id"]]["repo"])
        self.assertEqual(by_id[s["m"]["b"]["id"]]["repo"], s["b"])


if __name__ == "__main__":
    unittest.main()
