"""Tests for plugins/harness/lib/harness_lib/sessionstate.py (ticket #88): the per-session
state after_edit.py and verify_on_stop.py share, named once.

- SessionState.for_repo(root, data) through its public interface, checked against what the
  two hooks' own copies do today: .dir ($TMPDIR/claude-harness/<first 12 hex of the sha1 of
  the resolved root>, created lazily: reading, naming and clearing never create it, the
  first write does), .session (the session id with anything outside [A-Za-z0-9._-] turned
  into "_", "nosession" when absent or empty), .dirty / .verify_file / .stamp(name) (the
  paths dirty-<s>, verify-<s>.json, <name>-<s>), record(paths) (append "<path>\\n", skip a
  path with a newline in it, keep duplicates and order, swallow OSError), dirty_files()
  (sorted unique Paths, [] when absent), clear_dirty().
- porcelain_status(root, timeout) on real temp git repos: `git status --porcelain=v1 -z
  --untracked-files=all` bytes (checked against git itself), b"" for a clean tree and for
  a None root, None outside a repo, on a timeout and when git is missing; the index is
  left alone (--no-optional-locks).
- status_paths(root, out) on -z output: renames and copies in either column skip their
  second field, short entries are skipped, spaces, unicode and non-UTF-8 bytes survive.
- after_edit.py and verify_on_stop.py carry no copy of any of it (no def of the names, no
  renamed copy of today's bodies, none of the inlined forms) and bind SessionState,
  porcelain_status and status_paths from harness_lib.sessionstate; after_edit.py and
  notify.py bind their hook I/O helpers from harness_lib.hookio (#87) the same way.
- End to end, today's behavior the refactor must keep: verify_on_stop's empty dirty list
  after a clean-tree stop, and the fail-open stderr line of after_edit.py and notify.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_sessionstate.py" -v
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the hooks and the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
SESSIONSTATE_PY = LIB / "harness_lib" / "sessionstate.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
SHARED_NAMES = ("SessionState", "porcelain_status", "status_paths")
SIX = ("read_input", "deny", "emit", "hook_cwd", "edited_path", "run_fail_open")
# Names each hook must bind from the lib after the refactor.
SESSION_USES = {
    "after_edit.py": SHARED_NAMES,
    "verify_on_stop.py": SHARED_NAMES,
}
HOOKIO_USES = {
    "after_edit.py": ("read_input", "hook_cwd", "edited_path", "run_fail_open"),
    "notify.py": ("read_input", "emit", "run_fail_open"),
}
# Helpers the two session hooks copy today (by name or renamed).
COPY_NAMES = (
    "status_paths",
    "porcelain_status",
    "git_status",
    "worktree_status",
    "state_dir",
    "session_key",
    "dirty_files",
)
# Today's copies, verbatim: a renamed one left in a hook is still a copy.
TODAY = '''
def state_dir(root):
    key = os.path.normcase(str(root.resolve())).encode("utf-8", "surrogateescape")
    d = (
        Path(tempfile.gettempdir())
        / "claude-harness"
        / hashlib.sha1(key).hexdigest()[:12]
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_key(data):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(data.get("session_id") or "nosession"))


def record(root, paths, data):
    lines = "".join(f"{p}\\n" for p in paths if "\\n" not in str(p))
    if not lines:
        return
    try:
        flag = state_dir(root) / f"dirty-{session_key(data)}"
        with flag.open("a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(lines)
    except OSError:
        pass


def git_status(root):
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


def status_paths(root, out):
    paths = []
    fields = out.split(b"\\0")
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


def dirty_files(flag):
    try:
        lines = flag.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return []
    return sorted({Path(p) for p in lines if p.strip()})


def record(flag, files):
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        with flag.open("a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write("".join(f"{f}\\n" for f in files if "\\n" not in str(f)))
    except OSError:
        pass


def worktree_status(git_root):
    if git_root is None:
        return b""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(git_root), "status"]
            + ["--porcelain=v1", "-z", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=STATUS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None
'''

_SHARED: dict = {}


def setUpModule() -> None:
    _SHARED["base"] = Path(tempfile.mkdtemp(prefix="harness-sessionstate-shared-")).resolve()


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def clean_env(tmp: Path) -> dict:
    """Built from scratch, so nothing leaks in from a Claude Code session running the
    tests (CLAUDE_PROJECT_DIR, CLAUDE_VERIFY_CMD, HARNESS_*)."""
    home = tmp / "home"
    tmpdir = tmp / "tmp"
    home.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    return dict(
        GIT_IDENTITY,
        HOME=str(home),
        TMPDIR=str(tmpdir),
        PATH=SYSTEM_PATH,
        LANG="en_US.UTF-8",
        PYTHONDONTWRITEBYTECODE="1",
        GIT_CEILING_DIRECTORIES=str(tmp),
    )


def load_sessionstate() -> types.ModuleType:
    """harness_lib.sessionstate imported from the checkout's lib/, as a hook's prologue
    would; the import error while the module is missing is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.sessionstate")


def load_with_loader(path: Path) -> types.ModuleType:
    modname = path.name.replace("-", "_").replace(".py", "") + "_sessionstate_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    module = types.ModuleType("harness_hook_sessionstate_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def expected_dir(tmpdir: Path, root: Path) -> Path:
    """The state directory from first principles (not from the code under test)."""
    key = os.path.normcase(str(Path(root).resolve())).encode("utf-8", "surrogateescape")
    return Path(tmpdir) / "claude-harness" / hashlib.sha1(key).hexdigest()[:12]


_OMIT = object()  # a payload with no session_id key at all


class StateCase(unittest.TestCase):
    """A temp TMPDIR the lib sees through tempfile.gettempdir(), and a temp git env."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-sessionstate-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.tmpdir = Path(self.env["TMPDIR"])
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmpdir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load_sessionstate()

    def state(self, root: Path, session_id="sess-1", **extra):
        data = dict(extra)
        if session_id is not _OMIT:
            data["session_id"] = session_id
        return self.mod.SessionState.for_repo(Path(root), data)

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=self.env
        )

    def make_repo(self, name: str = "proj") -> Path:
        repo = self.tmp / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("hello\n")
        (repo / "app.py").write_text("x = 1\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def reference_status(self, repo: Path) -> bytes:
        """git's own answer, asked directly: the reference porcelain_status must match."""
        out = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        return out.stdout


# ================================================================ SessionState


class SessionStateTests(StateCase):
    def test_the_module_exposes_the_three_names(self):
        for name in SHARED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(self.mod, name, None)))

    def test_dir_is_claude_harness_under_tmpdir_keyed_on_the_resolved_root(self):
        roots = []
        for name in ("proj", "other repo", "café"):
            root = self.tmp / name
            root.mkdir()
            roots.append(root)
            with self.subTest(root=name):
                st = self.state(root)
                self.assertIsInstance(st.dir, Path)
                self.assertEqual(st.dir, expected_dir(self.tmpdir, root))
                self.assertRegex(st.dir.name, r"^[0-9a-f]{12}$")
                self.assertEqual(st.dir.parent, self.tmpdir / "claude-harness")
        self.assertEqual(len({self.state(r).dir for r in roots}), 3)  # each root its own

    def test_dir_follows_tempfile_gettempdir(self):
        root = self.tmp / "proj"
        root.mkdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        before = self.state(root).dir
        with mock.patch.object(tempfile, "tempdir", str(elsewhere)):
            after = self.state(root).dir
        self.assertEqual(before.name, after.name)
        self.assertEqual(after, elsewhere / "claude-harness" / after.name)
        self.assertEqual(before, self.tmpdir / "claude-harness" / before.name)

    def test_a_symlinked_root_shares_the_directory_of_the_real_one(self):
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        self.assertEqual(self.state(link).dir, self.state(real).dir)
        self.assertEqual(self.state(link).dir, expected_dir(self.tmpdir, real))

    def test_session_key_replaces_anything_outside_the_safe_set(self):
        table = {
            "sess-1": "sess-1",
            "A.b_c-9": "A.b_c-9",
            "a/b c": "a_b_c",
            "../etc/passwd": ".._etc_passwd",
            "café": "caf_",
            "x\ny": "x_y",
            "id:with*odd?chars": "id_with_odd_chars",
            12345: "12345",
            None: "nosession",
            "": "nosession",
            0: "nosession",
        }
        root = self.tmp / "proj"
        root.mkdir()
        for raw, expected in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.state(root, session_id=raw).session, expected)
        # No session_id key at all.
        self.assertEqual(self.state(root, session_id=_OMIT).session, "nosession")
        # Other keys in the payload do not matter.
        self.assertEqual(self.state(root, session_id="s9", cwd="/x").session, "s9")

    def test_named_paths_sit_in_the_dir_and_follow_the_session(self):
        root = self.tmp / "proj"
        root.mkdir()
        for session in ("sess-1", "other.2", "a b"):
            st = self.state(root, session_id=session)
            key = "a_b" if session == "a b" else session
            with self.subTest(session=session):
                self.assertEqual(st.dirty, st.dir / f"dirty-{key}")
                self.assertEqual(st.verify_file, st.dir / f"verify-{key}.json")
                self.assertEqual(st.stamp("bash-scan"), st.dir / f"bash-scan-{key}")
                self.assertEqual(st.stamp("other"), st.dir / f"other-{key}")

    def test_naming_reading_and_clearing_create_nothing(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        st.dir, st.session, st.dirty, st.verify_file, st.stamp("bash-scan")
        self.assertEqual(st.dirty_files(), [])
        st.clear_dirty()
        st.record([])
        self.assertFalse(st.dir.exists())
        self.assertFalse((self.tmpdir / "claude-harness").exists())

    def test_the_first_write_creates_the_dir_and_appends_lines(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        a, b = root / "a.py", root / "sub dir" / "b c.py"
        st.record([a, b])
        self.assertTrue(st.dir.is_dir())
        self.assertEqual(st.dirty.read_text(encoding="utf-8"), f"{a}\n{b}\n")
        st.record([a])  # appended, duplicates kept, order kept
        self.assertEqual(st.dirty.read_text(encoding="utf-8"), f"{a}\n{b}\n{a}\n")
        self.assertEqual(
            sorted(p.name for p in st.dir.iterdir()), ["dirty-sess-1"]
        )  # nothing else written

    def test_record_skips_paths_with_a_newline_and_writes_nothing_if_none_are_left(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        st.record([Path(str(root / "bad\nname.py"))])
        st.record([])
        self.assertFalse(st.dir.exists())
        self.assertFalse(st.dirty.exists())
        good = root / "good.py"
        st.record([Path(str(root / "x\ny.py")), good, Path("a\nb")])
        self.assertEqual(st.dirty.read_text(encoding="utf-8"), f"{good}\n")

    def test_sessions_and_roots_keep_separate_lists(self):
        one, two = self.tmp / "one", self.tmp / "two"
        one.mkdir()
        two.mkdir()
        f = one / "f.py"
        self.state(one, "s1").record([f])
        self.state(one, "s2").record([one / "g.py", one / "h.py"])
        self.state(two, "s1").record([two / "z.py"])
        self.assertEqual(self.state(one, "s1").dirty_files(), [f])
        self.assertEqual(self.state(one, "s2").dirty_files(), [one / "g.py", one / "h.py"])
        self.assertEqual(self.state(two, "s1").dirty_files(), [two / "z.py"])
        self.assertEqual(self.state(two, "s2").dirty_files(), [])

    def test_dirty_files_are_the_sorted_unique_paths_without_blank_lines(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        st.dir.mkdir(parents=True)
        st.dirty.write_text(
            f"{root}/b.py\n\n   \n{root}/a.py\n{root}/b.py\n{root}/c d.py\n", encoding="utf-8"
        )
        got = st.dirty_files()
        self.assertEqual(
            got, [Path(f"{root}/a.py"), Path(f"{root}/b.py"), Path(f"{root}/c d.py")]
        )
        self.assertTrue(all(isinstance(p, Path) for p in got))

    def test_dirty_files_roundtrip_a_path_that_is_not_utf8(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        odd = root / os.fsdecode(b"bad\xff.txt")
        st.record([odd, root / "ok.txt"])
        self.assertEqual(st.dirty_files(), sorted({odd, root / "ok.txt"}))

    def test_clear_dirty_removes_this_sessions_list_only(self):
        root = self.tmp / "proj"
        root.mkdir()
        mine, theirs = self.state(root, "mine"), self.state(root, "theirs")
        mine.record([root / "a.py"])
        theirs.record([root / "b.py"])
        mine.clear_dirty()
        self.assertFalse(mine.dirty.exists())
        self.assertEqual(mine.dirty_files(), [])
        self.assertEqual(theirs.dirty_files(), [root / "b.py"])
        mine.clear_dirty()  # nothing left: no error
        mine.record([root / "c.py"])  # and it starts over
        self.assertEqual(mine.dirty_files(), [root / "c.py"])

    def test_the_state_files_beside_the_dirty_list_are_left_alone_by_clear_dirty(self):
        root = self.tmp / "proj"
        root.mkdir()
        st = self.state(root)
        st.record([root / "a.py"])
        st.verify_file.write_text('{"blocks": 0}', encoding="utf-8")
        st.stamp("bash-scan").write_text("1.0\n", encoding="utf-8")
        st.clear_dirty()
        self.assertEqual(st.verify_file.read_text(encoding="utf-8"), '{"blocks": 0}')
        self.assertEqual(st.stamp("bash-scan").read_text(encoding="utf-8"), "1.0\n")

    def test_a_state_dir_that_cannot_be_made_is_not_an_error(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where TMPDIR should be\n")
        root = self.tmp / "proj"
        root.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(blocker)):
            st = self.state(root)
            st.record([root / "a.py"])  # OSError swallowed, like both hooks' copies
            self.assertEqual(st.dirty_files(), [])
            st.clear_dirty()


# ================================================================ porcelain_status


class PorcelainStatusTests(StateCase):
    def test_clean_repo_gives_empty_bytes(self):
        repo = self.make_repo()
        out = self.mod.porcelain_status(repo, 8)
        self.assertEqual(out, b"")
        self.assertIsInstance(out, bytes)

    def test_matches_git_for_a_spread_of_changes(self):
        repo = self.make_repo()
        (repo / "app.py").write_text("x = 2\n")  # modified
        (repo / "new.py").write_text("n = 1\n")  # staged add
        self.git(repo, "add", "new.py")
        (repo / "docs").mkdir()
        (repo / "docs" / "deep dir").mkdir()
        (repo / "docs" / "deep dir" / "note with spaces.md").write_text("n\n")  # untracked, listed per file
        (repo / "café.txt").write_text("c\n")
        self.git(repo, "mv", "README.md", "GUIDE.md")  # rename
        out = self.mod.porcelain_status(repo, 8)
        self.assertIsInstance(out, bytes)
        self.assertEqual(out, self.reference_status(repo))
        # And the facts, so an echo of an empty reference cannot pass.
        fields = [f for f in out.split(b"\0") if f]
        self.assertIn(b" M app.py", fields)
        self.assertIn(b"A  new.py", fields)
        self.assertIn(b"R  GUIDE.md", fields)
        self.assertIn(b"README.md", fields)
        self.assertIn(b"?? docs/deep dir/note with spaces.md", fields)
        self.assertTrue(any(f.startswith(b"?? ") and "café.txt".encode() in f for f in fields))

    def test_follows_the_repo_it_is_given(self):
        a, b = self.make_repo("a"), self.make_repo("b")
        (a / "only_in_a.py").write_text("1\n")
        (b / "only_in_b.py").write_text("1\n")
        (b / "second_in_b.py").write_text("2\n")
        self.assertEqual(self.mod.porcelain_status(a, 8), b"?? only_in_a.py\0")
        self.assertEqual(
            sorted(self.mod.porcelain_status(b, 8).split(b"\0")),
            [b"", b"?? only_in_b.py", b"?? second_in_b.py"],
        )

    def test_a_none_root_is_outside_git_and_gives_empty_bytes(self):
        self.assertEqual(self.mod.porcelain_status(None, 8), b"")

    def test_outside_a_repo_gives_none(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            self.assertIsNone(self.mod.porcelain_status(plain, 8))

    def test_does_not_rewrite_the_index(self):
        repo = self.make_repo()
        target = repo / "app.py"
        os.utime(target, (time.time() - 500, time.time() - 500))  # stat data now stale
        index = repo / ".git" / "index"
        before = index.read_bytes()
        self.assertEqual(self.mod.porcelain_status(repo, 8), b"")
        self.assertEqual(index.read_bytes(), before)

    def fake_git(self, body: str) -> Path:
        bindir = self.tmp / "fakebin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "git"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        return bindir

    def test_a_git_that_outlasts_the_timeout_gives_none(self):
        repo = self.make_repo()
        bindir = self.fake_git("exec sleep 20\n")
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{SYSTEM_PATH}"}):
            started = time.monotonic()
            out = self.mod.porcelain_status(repo, 0.5)
            took = time.monotonic() - started
        self.assertIsNone(out)
        self.assertLess(took, 15)

    def test_a_failing_git_gives_none_even_with_output(self):
        repo = self.make_repo()
        bindir = self.fake_git('printf "?? ghost.py\\0"\nexit 3\n')
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{SYSTEM_PATH}"}):
            self.assertIsNone(self.mod.porcelain_status(repo, 8))

    def test_a_missing_git_gives_none(self):
        repo = self.make_repo()
        empty = self.tmp / "emptybin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(self.mod.porcelain_status(repo, 8))

    def test_the_command_is_the_one_the_hooks_run_today(self):
        """The fake git records its argv: status with the porcelain v1 -z form listing every
        untracked file, run in the root it was given, never waiting on stdin."""
        repo = self.make_repo()
        log = self.tmp / "argv.log"
        bindir = self.fake_git(f'printf "%s\\n" "$@" > {log}\nprintf " M x.py\\0"\n')
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{SYSTEM_PATH}"}):
            out = self.mod.porcelain_status(repo, 8)
        self.assertEqual(out, b" M x.py\0")
        argv = log.read_text().splitlines()
        self.assertIn(str(repo), argv)
        for arg in ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            self.assertIn(arg, argv)
        self.assertIn("--no-optional-locks", argv)


# ================================================================ status_paths


class StatusPathsTests(StateCase):
    def test_one_path_per_entry_under_the_root(self):
        for root in (Path("/repo"), Path("/tmp/other root"), Path("rel")):
            out = b" M a.py\0A  src/b.py\0?? d/e/f.txt\0MM both.py\0 D gone.py\0"
            with self.subTest(root=str(root)):
                self.assertEqual(
                    self.mod.status_paths(root, out),
                    [root / n for n in ("a.py", "src/b.py", "d/e/f.txt", "both.py", "gone.py")],
                )

    def test_nothing_in_nothing_out(self):
        self.assertEqual(self.mod.status_paths(Path("/repo"), b""), [])
        self.assertEqual(self.mod.status_paths(Path("/repo"), b"\0\0"), [])

    def test_a_rename_or_copy_in_either_column_skips_its_source_field(self):
        root = Path("/r")
        cases = {
            b"R  new.py\0old.py\0": ["new.py"],
            b" R new.py\0old.py\0": ["new.py"],
            b"C  copy.py\0orig.py\0": ["copy.py"],
            b" C copy.py\0orig.py\0": ["copy.py"],
            b"RM moved.py\0was.py\0": ["moved.py"],
            b"MR moved.py\0was.py\0": ["moved.py"],
            b"RC both.py\0src.py\0": ["both.py"],
        }
        for out, expected in cases.items():
            with self.subTest(out=out):
                self.assertEqual(self.mod.status_paths(root, out), [root / n for n in expected])

    def test_the_source_field_is_skipped_even_when_it_looks_like_an_entry(self):
        root = Path("/r")
        out = b"R  new.py\0 M looks-like-an-entry.py\0A  after.py\0"
        self.assertEqual(self.mod.status_paths(root, out), [root / "new.py", root / "after.py"])
        # Without a rename that same field is an entry.
        self.assertEqual(
            self.mod.status_paths(root, b"A  new.py\0 M looks-like-an-entry.py\0"),
            [root / "new.py", root / "looks-like-an-entry.py"],
        )

    def test_entries_shorter_than_four_bytes_are_skipped(self):
        root = Path("/r")
        out = b"\0 M\0?? \0 M a.py\0ab\0"
        self.assertEqual(self.mod.status_paths(root, out), [root / "a.py"])

    def test_spaces_unicode_and_bytes_that_are_not_utf8_survive(self):
        root = Path("/r")
        raw = b"bad\xffname.txt"
        out = (
            b"?? with space.txt\0?? "
            + "café/über.py".encode("utf-8")
            + b"\0 M "
            + raw
            + b"\0"
        )
        self.assertEqual(
            self.mod.status_paths(root, out),
            [root / "with space.txt", root / "café/über.py", root / os.fsdecode(raw)],
        )

    def test_agrees_with_a_real_repo_after_a_rename_and_new_files(self):
        repo = self.make_repo()
        self.git(repo, "mv", "README.md", "GUIDE.md")
        (repo / "app.py").write_text("x = 3\n")
        (repo / "a b").mkdir()
        (repo / "a b" / "c d.txt").write_text("1\n")
        out = self.mod.porcelain_status(repo, 8)
        got = self.mod.status_paths(repo, out)
        self.assertEqual(
            sorted(got), sorted([repo / "GUIDE.md", repo / "app.py", repo / "a b" / "c d.txt"])
        )
        self.assertNotIn(repo / "README.md", got)
        self.assertEqual(self.mod.status_paths(repo, self.mod.porcelain_status(repo, 8)), got)


# ================================================================ the hooks carry no copies


def hook_tree(name: str):
    return ast.parse((HOOKS / name).read_text(encoding="utf-8"), filename=name)


def defs(tree) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def normalized_body(fn) -> str:
    """The body of a def with its docstring dropped and every name and argument renamed,
    so a renamed copy (or one calling renamed helpers) compares equal to the original."""
    body = list(fn.body)
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        body = body[1:]
    module = ast.Module(body=copy.deepcopy(body), type_ignores=[])
    for node in ast.walk(module):
        if isinstance(node, ast.Name):
            node.id = "_"
        elif isinstance(node, ast.arg):
            node.arg = "_"
    return ast.dump(module, annotate_fields=False)


def body_size(fn) -> int:
    return sum(1 for stmt in fn.body for _ in ast.walk(stmt))


def docstring_ids(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def string_constants(tree) -> list[tuple[int, str]]:
    """Every string literal (f-string parts included) that is not a docstring."""
    docs = docstring_ids(tree)
    return [
        (getattr(n, "lineno", 0), n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def reference_bodies() -> dict:
    """{normalised body: name} for today's copies and the lib's own functions and
    methods; bodies too small to tell apart from ordinary code are left out."""
    sources = [ast.parse(TODAY)]
    if SESSIONSTATE_PY.is_file():
        sources.append(ast.parse(SESSIONSTATE_PY.read_text(encoding="utf-8")))
    found = {}
    for tree in sources:
        for fn in defs(tree):
            if body_size(fn) >= 12:
                found[normalized_body(fn)] = fn.name
    return found


# Spelled by the helpers, so a hook that still spells one does their job itself.
INLINE_MARKERS = {
    "claude-harness": "state dir",
    "--porcelain": "porcelain status",
    "dirty-": "dirty list name",
    "bash-scan-": "stamp name",
    "verify-": "verify state name",
    "[^A-Za-z0-9._-]": "session key",
}


class HooksUseSessionStateTests(unittest.TestCase):
    def test_no_hook_defines_the_shared_helpers(self):
        for hook in SESSION_USES:
            defined = sorted({fn.name for fn in defs(hook_tree(hook))} & set(COPY_NAMES))
            with self.subTest(hook=hook):
                self.assertEqual(defined, [])

    def test_no_hook_carries_a_renamed_copy(self):
        self.assertTrue(SESSIONSTATE_PY.is_file(), f"{SESSIONSTATE_PY} does not exist")
        refs = reference_bodies()
        for hook in SESSION_USES:
            copies = [
                f"{fn.name} (line {fn.lineno}) = {refs[normalized_body(fn)]}"
                for fn in defs(hook_tree(hook))
                if normalized_body(fn) in refs
            ]
            with self.subTest(hook=hook):
                self.assertEqual(copies, [])

    def test_no_hook_spells_the_state_names_or_the_status_command_itself(self):
        for hook in SESSION_USES:
            found = [
                f"{text!r} (line {line}): {why}"
                for line, text in string_constants(hook_tree(hook))
                for marker, why in INLINE_MARKERS.items()
                if marker in text
            ]
            with self.subTest(hook=hook):
                self.assertEqual(found, [])

    def test_status_paths_and_porcelain_status_exist_once_in_the_plugin(self):
        sources = [p for p in PLUGIN.rglob("*.py") if "tests" not in p.relative_to(PLUGIN).parts]
        sources += [p for p in (PLUGIN / "bin").iterdir() if p.is_file() and not p.suffix]
        for name in ("status_paths", "porcelain_status"):
            homes = []
            for path in sources:
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                homes += [str(path.relative_to(PLUGIN)) for fn in defs(tree) if fn.name == name]
            with self.subTest(name=name):
                self.assertEqual(homes, ["lib/harness_lib/sessionstate.py"])

    def check_bound(self, module: types.ModuleType, hook: str) -> None:
        self.assertTrue(SESSIONSTATE_PY.is_file(), f"{SESSIONSTATE_PY} does not exist")
        home = os.path.realpath(str(SESSIONSTATE_PY))
        for name in SESSION_USES[hook]:
            with self.subTest(hook=hook, name=name):
                bound = module.__dict__.get(name)
                self.assertTrue(callable(bound), f"{hook} does not bind {name}")
                self.assertEqual(bound.__module__, "harness_lib.sessionstate")
                self.assertEqual(os.path.realpath(inspect.getsourcefile(bound)), home)

    def test_names_bound_from_sessionstate_with_source_file_loader(self):
        for hook in SESSION_USES:
            self.check_bound(load_with_loader(HOOKS / hook), hook)

    def test_names_bound_from_sessionstate_in_a_bare_module(self):
        for hook in SESSION_USES:
            self.check_bound(load_with_exec(HOOKS / hook), hook)


# ================================================================ after_edit and notify adopt hookio


STD_STREAMS = ("stdin", "stdout", "__stdin__", "__stdout__")


def lib_load_handler_ids(tree) -> set[int]:
    """Node ids inside the except handlers of the module-level `try:` whose body is only
    `from harness_lib... import ...`. That handler runs when hookio itself failed to load,
    so it cannot use hookio's helpers (notify.py asks for the bell there, by hand)."""
    ids: set[int] = set()
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Try)
            and stmt.body
            and all(
                isinstance(b, ast.ImportFrom)
                and (b.module == "harness_lib" or (b.module or "").startswith("harness_lib."))
                for b in stmt.body
            )
        ):
            for handler in stmt.handlers:
                ids.update(id(n) for n in ast.walk(handler))
    return ids


def stream_uses(tree) -> list[str]:
    """Direct uses of the standard streams outside the lib-load failure handler."""
    exempt = lib_load_handler_ids(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr in STD_STREAMS
        ):
            found.append(f"sys.{node.attr} (line {node.lineno}): read_input / emit")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            found += [f"from sys import {a.name}" for a in node.names if a.name in STD_STREAMS]
    return found


def main_block(tree):
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name)
            and stmt.test.left.id == "__name__"
        ):
            return stmt
    return None


def catches_exception(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type
    if kinds is None:
        return True
    names = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def calls_run_fail_open(stmt) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "run_fail_open")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "run_fail_open")
        )
        for n in ast.walk(stmt)
    )


class AfterEditAndNotifyUseHookioTests(unittest.TestCase):
    def test_neither_hook_defines_any_of_the_six(self):
        for hook in HOOKIO_USES:
            defined = sorted({fn.name for fn in defs(hook_tree(hook))} & set(SIX))
            with self.subTest(hook=hook):
                self.assertEqual(defined, [])

    def test_neither_hook_touches_the_standard_streams_or_spells_the_helpers(self):
        for hook in HOOKIO_USES:
            tree = hook_tree(hook)
            found = stream_uses(tree)
            for line, text in string_constants(tree):
                for marker, why in (("PROJECT_DIR", "hook_cwd"), ("notebook", "edited_path")):
                    if marker in text:
                        found.append(f"{text!r} (line {line}): {why}")
            with self.subTest(hook=hook):
                self.assertEqual(found, [])

    def test_only_the_lib_load_failure_handler_may_touch_the_streams(self):
        """The exemption above covers the except of the module-level harness_lib import
        and nothing else: the same stream use anywhere else is still reported."""
        handler = (
            "import json, sys\n"
            "try:\n"
            "    from harness_lib.hookio import emit\n"
            "    from harness_lib.notify import notify\n"
            "except Exception:\n"
            "    sys.stdout.write(json.dumps({'terminalSequence': '\\u0007'}))\n"
            "    sys.stdout.flush()\n"
            "    sys.exit(0)\n"
        )
        self.assertEqual(stream_uses(ast.parse(handler)), [])
        flagged = {
            "in the try body": handler.replace(
                "    from harness_lib.notify import notify\n",
                "    from harness_lib.notify import notify\n    sys.stdout.flush()\n",
            ),
            "after the try": handler + "sys.stdout.write('x')\n",
            "in main": handler + "def main():\n    return sys.stdin.read()\n",
            "in __main__": handler + "if __name__ == '__main__':\n    sys.stdout.flush()\n",
            "a try of a non-lib import": (
                "import sys\ntry:\n    import json\nexcept Exception:\n    sys.stdout.write('x')\n"
            ),
            "a nested try of the lib": (
                "import sys\ndef main():\n    try:\n        from harness_lib.hookio import emit\n"
                "    except Exception:\n        sys.stdout.write('x')\n"
            ),
            "from sys import stdout": handler + "from sys import stdout\n",
        }
        for where, source in flagged.items():
            with self.subTest(where=where):
                self.assertNotEqual(stream_uses(ast.parse(source)), [])

    def test_the_main_block_runs_run_fail_open_and_has_no_tail_of_its_own(self):
        for hook in HOOKIO_USES:
            block = main_block(hook_tree(hook))
            with self.subTest(hook=hook):
                self.assertIsNotNone(block, "no __main__ block")
                self.assertTrue(calls_run_fail_open(block), "__main__ does not call run_fail_open")
                own_tail = [
                    n.lineno
                    for n in ast.walk(block)
                    if isinstance(n, ast.ExceptHandler) and catches_exception(n)
                ]
                self.assertEqual(own_tail, [])

    def check_bound(self, module: types.ModuleType, hook: str) -> None:
        for name in HOOKIO_USES[hook]:
            with self.subTest(hook=hook, name=name):
                bound = module.__dict__.get(name)
                self.assertTrue(callable(bound), f"{hook} does not bind {name}")
                self.assertEqual(bound.__module__, "harness_lib.hookio")

    def test_names_bound_from_hookio(self):
        for hook in HOOKIO_USES:
            self.check_bound(load_with_loader(HOOKS / hook), hook)
            self.check_bound(load_with_exec(HOOKS / hook), hook)


# ================================================================ today's behavior, end to end


# Runs a hook as __main__ with json.loads raising, so main() crashes inside its first
# read; the hook's tail decides what is printed.
CRASH_DRIVER = r'''
import json, os, runpy, sys
token = os.environ["SESSIONSTATE_TEST_TOKEN"]

def loads(*args, **kwargs):
    raise RuntimeError(token)

json.loads = loads
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
'''


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-sessionstate-e2e-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.tmpdir = Path(self.env["TMPDIR"])

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=self.env
        )

    def make_repo(self) -> Path:
        repo = self.tmp / "proj"
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("hello\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def stop(self, repo: Path, cmd: str, active: bool = False):
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": active,
        }
        r = subprocess.run(
            [sys.executable, "-B", str(HOOKS / "verify_on_stop.py")],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=dict(self.env, CLAUDE_VERIFY_CMD=cmd),
            cwd=str(self.tmp),
            timeout=120,
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_a_stop_after_the_tree_went_clean_leaves_an_empty_dirty_list(self):
        """Guard for the merge of the two record() copies: verify_on_stop creates the dirty
        list even when git lists nothing, so the unchanged failing check is reported (a
        silent stop would mean the list was not kept). Green before the refactor too."""
        repo = self.make_repo()
        (repo / "new.txt").write_text("x\n")
        self.assertIsNone(self.stop(repo, "false"))  # first stop only records the tree
        self.git(repo, "add", "new.txt")
        self.git(repo, "commit", "-q", "-m", "Add new")
        first = self.stop(repo, "false")  # tree changed (now clean): runs, fails, blocks
        self.assertEqual(first["decision"], "block")
        dirty = expected_dir(self.tmpdir, repo) / "dirty-sess-1"
        self.assertTrue(dirty.is_file())
        self.assertEqual(dirty.read_bytes(), b"")
        self.assertEqual(
            self.stop(repo, "false"),
            {
                "systemMessage": "verify_on_stop: false still failing and nothing changed "
                "since the last check; fix it or run it by hand."
            },
        )

    def test_after_edit_and_notify_fail_open_with_todays_line(self):
        repo = self.make_repo()
        payload = {
            "session_id": "sess-1",
            "cwd": str(repo),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / "README.md")},
            "message": "hi",
        }
        for hook in HOOKIO_USES:
            token = "crash-" + uuid.uuid4().hex[:8]
            with self.subTest(hook=hook):
                r = subprocess.run(
                    [sys.executable, "-B", "-c", CRASH_DRIVER, str(HOOKS / hook)],
                    input=json.dumps(payload).encode("utf-8"),
                    capture_output=True,
                    env=dict(self.env, SESSIONSTATE_TEST_TOKEN=token),
                    cwd=str(repo),
                    timeout=60,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, b"")
                self.assertEqual(r.stderr.decode("utf-8"), f"{hook}: {token}\n")


# ================================================================ static backstops against hidden copies

SESSION_HOOKS = ("after_edit.py", "verify_on_stop.py")
ALL_FOUR = ("after_edit.py", "verify_on_stop.py", "notify.py")


def literal_str(node) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def has_status_constant(node) -> bool:
    return any(
        isinstance(n, ast.Constant) and n.value == "status" for n in ast.walk(node)
    )


class NoDisguisedCopiesTests(unittest.TestCase):
    """The lexical checks above are beaten by splitting a literal or renaming a name;
    these close the common disguises. The runtime tests below are the main guard."""

    def test_session_hooks_do_not_make_the_state_dir_or_run_git_status_themselves(self):
        for hook in SESSION_HOOKS:
            tree = hook_tree(hook)
            found = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found += [f"import {a.name}" for a in node.names if a.name == "tempfile"]
                elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
                    found.append("from tempfile import ...")
                elif isinstance(node, ast.Name) and node.id == "tempfile":
                    found.append(f"tempfile (line {node.lineno}): state dir")
                elif isinstance(node, ast.Attribute) and node.attr == "sha1":
                    found.append(f".sha1 (line {node.lineno}): state dir key")
                elif isinstance(node, ast.Constant) and node.value == "sha1":
                    found.append(f"'sha1' (line {node.lineno})")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and has_status_constant(node)
                ):
                    found.append(f"subprocess with 'status' (line {node.lineno}): porcelain_status")
            with self.subTest(hook=hook):
                self.assertEqual(found, [])

    def test_no_hook_builds_a_string_from_literals_or_reaches_for_sys_by_name(self):
        """Guards against disguises: "a" + "b", "x" + f"-{y}", getattr(sys, "std" + "in"),
        __import__, eval and exec. Green today, and must stay so."""
        for hook in ALL_FOUR:
            found = []
            for node in ast.walk(hook_tree(hook)):
                if (
                    isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Add)
                    and literal_str(node.left)
                    and literal_str(node.right)
                ):
                    found.append(f"literal + literal (line {node.lineno})")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in ("__import__", "eval", "exec", "compile"):
                        found.append(f"{node.func.id}() (line {node.lineno})")
                    elif (
                        node.func.id == "getattr"
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in ("sys", "os", "subprocess")
                    ):
                        found.append(f"getattr({node.args[0].id}, ...) (line {node.lineno})")
                elif isinstance(node, ast.Import):
                    found += [f"import {a.name}" for a in node.names if a.name == "importlib"]
            with self.subTest(hook=hook):
                self.assertEqual(found, [])


# ================================================================ the hooks go through the lib at runtime


# harness_lib.sessionstate stand-in: calls the real module, records every name the hook
# touches, and steers: the state dir is keyed on $SESSIONSTATE_TEST_ROOT when set,
# porcelain_status answers with the bytes in $SESSIONSTATE_TEST_STATUS_FILE when set, and
# status_paths adds $SESSIONSTATE_TEST_EXTRA (relative to the root it was given). A hook
# doing any of these jobs with a private copy shows it in where its files land.
TRACED_SESSIONSTATE = '''from __future__ import annotations

import os as _os
from pathlib import Path as _Path

from harness_lib import _real_sessionstate as _real


def _record(name):
    path = _os.environ.get("SESSIONSTATE_TEST_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(name + "\\n")


class _Traced:
    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        _record("SessionState." + name)
        return getattr(object.__getattribute__(self, "_real"), name)


class SessionState:
    @classmethod
    def for_repo(cls, root, data):
        _record("SessionState.for_repo")
        steer = _os.environ.get("SESSIONSTATE_TEST_ROOT")
        return _Traced(_real.SessionState.for_repo(_Path(steer) if steer else root, data))


def porcelain_status(root, timeout):
    _record("porcelain_status")
    canned = _os.environ.get("SESSIONSTATE_TEST_STATUS_FILE")
    if canned:
        with open(canned, "rb") as fh:
            return fh.read()
    return _real.porcelain_status(root, timeout)


def status_paths(root, out):
    _record("status_paths")
    got = _real.status_paths(root, out)
    extra = _os.environ.get("SESSIONSTATE_TEST_EXTRA")
    return got + [_Path(root) / extra] if extra else got
'''

# harness_lib.hookio stand-in, as in tests/test_hookio.py: records the calls and steers
# the answers (input, cwd, edited path), marks emit and the fail-open name.
TRACED_HOOKIO = '''from __future__ import annotations

import json as _json
import os as _os

from harness_lib import _real_hookio as _real

MARK = _os.environ.get("HOOKIO_TEST_MARK", "")


def _record(name):
    path = _os.environ.get("HOOKIO_TEST_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(name + "\\n")


def read_input():
    _record("read_input")
    data = _real.read_input()
    crash = _os.environ.get("HOOKIO_TEST_CRASH")
    if crash:
        raise RuntimeError(crash)
    swap = _os.environ.get("HOOKIO_TEST_DATA")
    return _json.loads(swap) if swap else data


def hook_cwd(data):
    _record("hook_cwd")
    got = _real.hook_cwd(data)
    return _os.environ.get("HOOKIO_TEST_CWD") or got


def edited_path(data):
    _record("edited_path")
    got = _real.edited_path(data)
    return _os.environ.get("HOOKIO_TEST_EDITED") or got


def deny(reason):
    _record("deny")
    return _real.deny("[" + MARK + "] " + reason)


def emit(obj):
    _record("emit")
    return _real.emit(dict(obj, hookio_mark=MARK))


def run_fail_open(name, main):
    _record("run_fail_open")
    return _real.run_fail_open(name + "<" + MARK + ">", main)
'''

AWS_KEY = "AKIA" + "Q7R2M5K9T3W8Z4B6"  # split so this file holds no whole key


class TracedCase(unittest.TestCase):
    """Hooks run as hooks.json runs them, next to a lib whose sessionstate and hookio
    record what the hook calls and can steer what it gets back."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-sessionstate-traced-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.tmpdir = Path(self.env["TMPDIR"])
        self.mark = "mark-" + uuid.uuid4().hex[:8]
        self.cases = 0
        self.decoy = self.tmp / "decoy"
        self.decoy.mkdir()

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=self.env
        )

    def make_repo(self) -> Path:
        self.cases += 1
        repo = self.tmp / f"case{self.cases}" / "proj"
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("hello\n")
        (repo / "app.py").write_text("x = 1\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def traced_plugin(self) -> Path:
        root = self.tmp / "plugin-traced"
        if root.is_dir():
            return root
        self.assertTrue(SESSIONSTATE_PY.is_file(), f"{SESSIONSTATE_PY} does not exist")
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        pkg = root / "lib" / "harness_lib"
        shutil.copyfile(pkg / "hookio.py", pkg / "_real_hookio.py")
        shutil.copyfile(SESSIONSTATE_PY, pkg / "_real_sessionstate.py")
        (pkg / "hookio.py").write_text(TRACED_HOOKIO, encoding="utf-8")
        (pkg / "sessionstate.py").write_text(TRACED_SESSIONSTATE, encoding="utf-8")
        return root

    def run_traced(self, hook: str, stdin: dict, cwd: Path, env: dict | None = None):
        """(result, the lib names the hook called)."""
        trace = self.tmp / f"trace-{uuid.uuid4().hex[:8]}"
        extra = dict(
            env or {},
            HOOKIO_TEST_MARK=self.mark,
            HOOKIO_TEST_TRACE=str(trace),
            SESSIONSTATE_TEST_TRACE=str(trace),
        )
        r = subprocess.run(
            [sys.executable, "-B", str(self.traced_plugin() / "hooks" / hook)],
            input=json.dumps(stdin).encode("utf-8"),
            capture_output=True,
            env=dict(self.env, **extra),
            cwd=str(cwd),
            timeout=120,
        )
        called = trace.read_text(encoding="utf-8").split() if trace.is_file() else []
        return r, set(called)

    def tool_event(self, repo: Path, tool: str, session: str = "sess-1", **tool_input) -> dict:
        return {
            "session_id": session,
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_response": {"success": True},
        }

    def stop_event(self, repo: Path, session: str = "sess-1") -> dict:
        return {
            "session_id": session,
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }

    def steer_root(self) -> Path:
        """A directory that is not the repo: state keyed on it proves the hook asked the
        lib for its state dir."""
        root = self.tmp / "steer-root"
        root.mkdir(exist_ok=True)
        return root


class HooksCallSessionStateTests(TracedCase):
    def assert_called(self, called: set, names) -> None:
        self.assertEqual(sorted(set(names) - called), [], f"called: {sorted(called)}")

    def test_an_edit_is_recorded_through_the_lib_in_the_dir_it_names(self):
        repo = self.make_repo()
        steer = self.steer_root()
        r, called = self.run_traced(
            "after_edit.py",
            self.tool_event(repo, "Edit", file_path=str(repo / "app.py")),
            repo,
            {"SESSIONSTATE_TEST_ROOT": str(steer)},
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertEqual(
            (expected_dir(self.tmpdir, steer) / "dirty-sess-1").read_text(),
            f"{repo / 'app.py'}\n",
        )
        self.assertFalse(expected_dir(self.tmpdir, repo).exists())  # no private state dir
        self.assert_called(called, ("SessionState.for_repo", "SessionState.record"))

    def test_a_bash_call_scans_what_the_libs_status_lists_and_keeps_its_stamp_there(self):
        repo = self.make_repo()
        steer = self.steer_root()
        readme, extra = repo / "README.md", repo / "listed-by-the-lib.txt"
        extra.write_text("e\n")
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "listed-by-the-lib.txt\n")  # git never lists it
        now = time.time()
        os.utime(readme, (now - 30, now - 30))  # tracked and unchanged: git never lists it
        os.utime(extra, (now - 5, now - 5))
        status = self.tmp / "status.bin"
        status.write_bytes(b" M README.md\0")
        self.assertEqual(self.reference_status(repo), b"")
        r, called = self.run_traced(
            "after_edit.py",
            self.tool_event(repo, "Bash", command="sh gen.sh"),
            repo,
            {
                "SESSIONSTATE_TEST_ROOT": str(steer),
                "SESSIONSTATE_TEST_STATUS_FILE": str(status),
                "SESSIONSTATE_TEST_EXTRA": "listed-by-the-lib.txt",
            },
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        sdir = expected_dir(self.tmpdir, steer)
        self.assertEqual(  # newest first
            (sdir / "dirty-sess-1").read_text().splitlines(), [str(extra), str(readme)]
        )
        self.assertGreater(float((sdir / "bash-scan-sess-1").read_text()), now)
        self.assertFalse(expected_dir(self.tmpdir, repo).exists())
        self.assert_called(
            called,
            (
                "SessionState.for_repo",
                "SessionState.record",
                "SessionState.stamp",
                "porcelain_status",
                "status_paths",
            ),
        )

    def reference_status(self, repo: Path) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            capture_output=True,
            env=self.env,
        ).stdout

    def test_the_stop_hook_reads_the_dirty_list_and_keeps_its_state_in_the_libs_dir(self):
        repo = self.make_repo()
        steer = self.steer_root()
        env = {"SESSIONSTATE_TEST_ROOT": str(steer)}
        r, _ = self.run_traced(
            "after_edit.py", self.tool_event(repo, "Edit", file_path=str(repo / "app.py")), repo, env
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        status = self.tmp / "status.bin"
        status.write_bytes(b" M README.md\0")
        tag = uuid.uuid4().hex[:6]
        r, called = self.run_traced(
            "verify_on_stop.py",
            self.stop_event(repo),
            repo,
            dict(
                env,
                SESSIONSTATE_TEST_STATUS_FILE=str(status),
                CLAUDE_VERIFY_CMD=f"echo nope-{tag}; exit 1",
            ),
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        out = json.loads(r.stdout.decode("utf-8"))  # blocked: it found the lib's dirty list
        self.assertEqual(out["decision"], "block", out)
        self.assertEqual(out["hookio_mark"], self.mark)
        self.assertIn(f"nope-{tag}", out["reason"])
        sdir = expected_dir(self.tmpdir, steer)
        state = json.loads((sdir / "verify-sess-1.json").read_text())
        self.assertEqual(state["blocks"], 1)
        self.assertFalse(expected_dir(self.tmpdir, repo).exists())
        self.assertTrue(
            {
                "SessionState.for_repo",
                "SessionState.dirty_files",
                "SessionState.verify_file",
                "porcelain_status",
                "status_paths",
            }
            <= called,
            sorted(called),
        )

    def test_a_changed_tree_is_recorded_and_a_green_run_clears_it_through_the_lib(self):
        repo = self.make_repo()
        steer = self.steer_root()
        sdir = expected_dir(self.tmpdir, steer)
        status = self.tmp / "status.bin"
        env = {
            "SESSIONSTATE_TEST_ROOT": str(steer),
            "SESSIONSTATE_TEST_STATUS_FILE": str(status),
            "SESSIONSTATE_TEST_EXTRA": "via-status-paths.py",
        }
        # Source paths throughout: a docs-only set would be skipped (#163), not run.
        status.write_bytes(b" M app.py\0")
        r, _ = self.run_traced(
            "verify_on_stop.py",
            self.stop_event(repo),
            repo,
            dict(env, CLAUDE_VERIFY_CMD="exit 1"),
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))  # first stop: tree only
        self.assertFalse((sdir / "dirty-sess-1").exists())
        self.assertIn("tree", json.loads((sdir / "verify-sess-1.json").read_text()))

        status.write_bytes(b" M app.py\0?? other.py\0")  # the tree changed
        r, called = self.run_traced(
            "verify_on_stop.py",
            self.stop_event(repo),
            repo,
            dict(env, CLAUDE_VERIFY_CMD="exit 1"),
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(json.loads(r.stdout.decode("utf-8"))["decision"], "block")
        self.assertEqual(
            (sdir / "dirty-sess-1").read_text().splitlines(),
            [str(repo / "app.py"), str(repo / "other.py"), str(repo / "via-status-paths.py")],
        )
        self.assertIn("SessionState.record", called)

        r, called = self.run_traced(
            "verify_on_stop.py",
            self.stop_event(repo),
            repo,
            dict(env, CLAUDE_VERIFY_CMD="true"),
        )
        self.assertEqual(
            json.loads(r.stdout.decode("utf-8"))["systemMessage"], "verify_on_stop: true passed."
        )
        self.assertFalse((sdir / "dirty-sess-1").exists())
        self.assertIn("SessionState.clear_dirty", called)
        self.assertFalse(expected_dir(self.tmpdir, repo).exists())


class HooksWriteWhereSessionStateSaysTests(StateCase):
    """With the real lib, the three files land exactly at the paths SessionState names."""

    def run_hook(self, hook: str, payload: dict, cwd: Path, env: dict | None = None):
        return subprocess.run(
            [sys.executable, "-B", str(HOOKS / hook)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=dict(self.env, **(env or {})),
            cwd=str(cwd),
            timeout=120,
        )

    def test_dirty_list_stamp_and_verify_state_are_at_the_named_paths(self):
        repo = self.make_repo()
        data = {"session_id": "odd/session id"}
        st = self.mod.SessionState.for_repo(repo, data)
        event = dict(data, cwd=str(repo), permission_mode="default")
        edited = repo / "app.py"
        r = self.run_hook(
            "after_edit.py",
            dict(event, tool_name="Edit", tool_input={"file_path": str(edited)}),
            repo,
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(st.dirty.read_text(), f"{edited}\n")
        self.assertEqual(st.dirty_files(), [edited])

        self.git(repo, "mv", "README.md", "GUIDE.md")
        fresh = repo / "fresh file.txt"
        fresh.write_text("f\n")
        r = self.run_hook(
            "after_edit.py", dict(event, tool_name="Bash", tool_input={"command": "x"}), repo
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertGreater(float(st.stamp("bash-scan").read_text()), time.time() - 600)
        self.assertEqual(
            set(st.dirty_files()), {edited, repo / "GUIDE.md", fresh}
        )  # the rename's new name, not its source

        r = self.run_hook(
            "verify_on_stop.py",
            dict(event, hook_event_name="Stop", stop_hook_active=False),
            repo,
            {"CLAUDE_VERIFY_CMD": "true"},
        )
        self.assertEqual(
            json.loads(r.stdout.decode("utf-8")), {"systemMessage": "verify_on_stop: true passed."}
        )
        state = json.loads(st.verify_file.read_text())
        self.assertEqual(state["blocks"], 0)
        self.assertFalse(st.dirty.exists())
        self.assertEqual(
            sorted(p.name for p in st.dir.iterdir()),
            ["bash-scan-odd_session_id", "verify-odd_session_id.json"],
        )


class HooksCallHookioTests(TracedCase):
    """after_edit.py and notify.py act on hookio's steered answers at runtime."""

    def steered_edit(self, repo: Path, edited: Path, extra: dict | None = None):
        # stdin: another session, an event with no path, in a decoy directory; the data
        # hookio's read_input hands back names the real session
        decoy_event = self.tool_event(self.decoy, "Edit", "sess-other")
        data = self.tool_event(self.decoy, "Edit", "sess-1", file_path=str(self.decoy / "nothing"))
        data.pop("cwd")
        env = dict(
            HOOKIO_TEST_DATA=json.dumps(data),
            HOOKIO_TEST_CWD=str(repo),
            HOOKIO_TEST_EDITED=str(edited),
        )
        env.update(extra or {})
        return self.run_traced("after_edit.py", decoy_event, self.decoy, env)

    def test_after_edit_takes_its_input_cwd_and_path_from_hookio(self):
        repo = self.make_repo()
        r, called = self.steered_edit(repo, repo / "app.py")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        sdir = expected_dir(self.tmpdir, repo)
        self.assertEqual((sdir / "dirty-sess-1").read_text(), f"{repo / 'app.py'}\n")
        self.assertFalse((sdir / "dirty-sess-other").exists())
        self.assertEqual(sorted(p.name for p in sdir.iterdir()), ["dirty-sess-1"])
        for name in ("read_input", "hook_cwd", "edited_path", "run_fail_open"):
            self.assertIn(name, called)

    def test_after_edit_still_exits_2_on_a_secret_through_run_fail_open(self):
        repo = self.make_repo()
        leaky = repo / "leaky.py"
        leaky.write_text(f'KEY = "{AWS_KEY}"\n')
        r, called = self.steered_edit(repo, leaky)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stdout, b"")
        self.assertTrue(
            r.stderr.decode("utf-8").startswith(
                f"SECRET pattern in {leaky}:1 (AWS access key id): AKIA"
            ),
            r.stderr,
        )
        self.assertIn("run_fail_open", called)
        self.assertEqual(  # recorded first, as always
            (expected_dir(self.tmpdir, repo) / "dirty-sess-1").read_text(), f"{leaky}\n"
        )

    def test_both_hooks_fail_open_through_hookios_run_fail_open(self):
        token = "crash-" + uuid.uuid4().hex[:8]
        repo = self.make_repo()
        for hook in HOOKIO_USES:
            with self.subTest(hook=hook):
                r, called = self.run_traced(
                    hook,
                    self.tool_event(repo, "Edit", file_path=str(repo / "app.py")),
                    repo,
                    {"HOOKIO_TEST_CRASH": token},
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, b"")
                self.assertEqual(r.stderr.decode("utf-8"), f"{hook}<{self.mark}>: {token}\n")
                self.assertIn("run_fail_open", called)

    def fake_notifier(self) -> tuple[Path, Path]:
        bindir = self.tmp / "bin-notifier"
        bindir.mkdir(exist_ok=True)
        log = self.tmp / "terminal-notifier.args"
        script = bindir / "terminal-notifier"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n')
        script.chmod(0o755)
        return bindir, log

    def test_notify_reads_its_event_from_hookio_and_rings_the_bell_through_emit(self):
        tag = uuid.uuid4().hex[:6]
        decoy_event = {"session_id": "sess-other", "cwd": "/x/decoy-repo", "message": "decoy"}
        data = {
            "session_id": "sess-1",
            "cwd": f"/x/steered-{tag}",
            "message": f"steered message {tag}",
            "title": "Steered",
        }
        bindir, log = self.fake_notifier()
        r, called = self.run_traced(
            "notify.py",
            decoy_event,
            self.decoy,
            {"HOOKIO_TEST_DATA": json.dumps(data), "PATH": str(bindir)},
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        args = log.read_text().splitlines()
        self.assertEqual(
            dict(zip(args[::2], args[1::2])),
            {
                "-title": "Steered",
                "-subtitle": f"steered-{tag}",
                "-message": f"steered message {tag}",
                "-group": "claude-code-sess-1",
                "-activate": "com.microsoft.VSCode",
                "-sound": "Glass",
            },
        )
        none = self.tmp / "bin-none"
        none.mkdir()
        r, called2 = self.run_traced(
            "notify.py", decoy_event, self.decoy, {"HOOKIO_TEST_DATA": json.dumps(data), "PATH": str(none)}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(  # written by hookio.emit: it adds the mark
            json.loads(r.stdout.decode("utf-8")),
            {"terminalSequence": "\u0007", "hookio_mark": self.mark},
        )
        for name in ("read_input", "run_fail_open"):
            self.assertIn(name, called)
        self.assertIn("emit", called2)


if __name__ == "__main__":
    unittest.main()
