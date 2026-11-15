"""Tests for plugins/harness/hooks/backup_gate.py (ticket #327, epic #322): back up, then allow, rm in the project.

The hook (PreToolUse, matcher Bash) snapshots what an rm, unlink or rmdir in the session's project would destroy,
with harness_lib/backups.py's create, and then answers allow; what it cannot back up gets ask (over the size cap)
or no decision at all (Claude Code's default). It never weakens branch_guard: a command branch_guard denies gets
no decision and no snapshot.

- No decision and no snapshot: plan mode; HARNESS_BACKUP=off (the environment, then the project's settings); a
  command branch_guard denies (HARNESS_ALLOW_MAIN honored); an unknown target ($, a backtick, xargs, an unknown
  directory, a glob matching nothing); a target outside the session's project (the main checkout of
  CLAUDE_PROJECT_DIR, else of the event's cwd, its worktrees included); the main checkout, a worktree root, .git
  or anything in it, $HOME, and anything under .claude/, .githooks/ or .github/ relative to its own working tree.
- Rebuildable (no copy): a path component named node_modules, .venv, venv, dist, build, __pycache__,
  .pytest_cache, .mypy_cache, .ruff_cache or .tox, unless the path is sensitive (HARNESS_SENSITIVE_PATHS, or a
  folder holding a path that matches) or secret-shaped (.env, .env.*, *.pem, *.key, credentials/ and under).
  Rebuildable folders inside a copied target are left out of the copy and of the size.
- Everything else is copied. Over HARNESS_BACKUP_CAP_MB (default 200): ask, no snapshot, the reason naming the
  size and the cap. Otherwise one snapshot, then allow when every other part is harmless (cd, pushd, popd, pwd,
  true, :, ls, echo, printf, test, [, with no redirect but to /dev/null or a descriptor), the reason naming the
  snapshot id; with any other part the snapshot stays and there is no decision. Nothing to copy (rebuildable or
  missing): allow on the same rule, no snapshot.
- A command without rm, unlink or rmdir prints nothing. Without the lib, or on its own error: one stderr line
  naming the hook, exit 0, no decision.
- hooks.json runs it on PreToolUse Bash after tests_first.py, with a 30 s timeout.

Fixtures: a temporary HOME per test (and one for the whole process, so nothing here can reach the real ~/.claude),
real git repositories (`git init -b main`, one commit holding README.md, src/, docs/, docs/agent/, .gitignore,
.claude/settings.json, .claude/agents/, .githooks/ and .github/workflows/) and linked worktrees made with
`git worktree add`, random names and contents. GIT_CEILING_DIRECTORIES keeps git from finding a repository above
the test's folder. The hook runs as hooks.json runs it: `python3 <hook>` with the event JSON on stdin and
CLAUDE_PROJECT_DIR in its environment, from a folder outside every fixture repository; its answer is read as
Claude Code reads it (stdout parsed as JSON, hookSpecificOutput.permissionDecision; nothing on stdout is no
decision). A snapshot is any new folder in the global store (<HOME>/.claude/harness/backups/) or in a fixture
repository's project store (<git common dir>/harness-backups/); the round trip removes the target itself, then runs
`harness-work backup restore <id>` and compares every entry (bytes, modes, link targets) with what was there.
backup_gate is run alone: tests_first.py, which judges rm on a ticket branch as a source edit, is another hook.

Choices the ticket leaves open, fixed here:
- Ruling 2 (Ryan, 2026-11-05, epic #322) supersedes the outline's "or `git check-ignore` reporting it" and the
  DoD's "rm of a git-ignored scratch file: allow, no snapshot": only a path component among the named build-output
  folders is rebuildable; any other git-ignored path (a scratch file, data/, .env) is copied under the cap.
- A word with a glob character (*, ? or [) is expanded from the directory its part runs in, as the shell does;
  each match is a target, and a glob matching nothing makes the command's targets unknown.
- A missing literal target is nothing to copy; an empty folder (rmdir) is an existing target and is copied.
- `.git` by name: a target named .git (a worktree's .git file too) or with a .git component gets nothing. The
  folders .claude, .githooks and .github themselves get nothing, like the paths under them.
- A command whose parts run no rm, unlink or rmdir (the word only as an argument: `echo rm -rf build`) gets no
  decision, even when every part is harmless.
- One snapshot per command, holding every copied target. The size is the apparent size (the sum of the regular
  files' st_size, links not followed, as the manifest's bytes), over all copied targets of the command; the sizes
  used are over or under the cap whether MB means 10^6 or 2^20 bytes. A reason names the size when it shows a
  number with a unit within 25% of the byte count (decimal or binary units), and the cap when it shows its number.
- HARNESS_BACKUP is read from the environment first, then `env` in the main checkout's tracked
  .claude/settings.json; an environment value other than off (on) wins over a settings off. Every
  permission_mode but plan (default, auto, acceptEdits, bypassPermissions) gets the same answer.
- The session's project is the main checkout of CLAUDE_PROJECT_DIR (a worktree's names its main checkout), else
  of the event's cwd; a worktree outside the main checkout's folder is inside the project.
- A store the snapshot cannot be written to is the hook's own error: one stderr line, exit 0, no decision.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_backup_gate.py -v
"""
from __future__ import annotations

import hashlib
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

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
HOOKS_DIR = os.path.join(PLUGIN, "hooks")
HOOK = os.path.join(HOOKS_DIR, "backup_gate.py")
HOOKS_JSON = os.path.join(HOOKS_DIR, "hooks.json")
TOOL = os.path.join(PLUGIN, "bin", "harness-work")
GIT = shutil.which("git") or "/usr/bin/git"
PATH = os.pathsep.join([os.path.dirname(GIT), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
REBUILDABLE = ("node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache",
               ".ruff_cache", ".tox")
GITIGNORE = (".claude/worktrees/\n.claude/settings.local.json\nnode_modules/\n*.tmp\ndata/\n.env\n.env.*\n*.pem\n"
             "*.key\ncredentials/\n")
FILE_MODES = (0o600, 0o640, 0o644, 0o664, 0o755)

# An internal error, as test_hookio.py makes one: every json.loads raises, then the hook runs as __main__.
CRASH_DRIVER = r'''
import json, os, runpy, sys
token = os.environ["BACKUP_GATE_TEST_TOKEN"]

def loads(*args, **kwargs):
    raise RuntimeError(token)

json.loads = loads
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
'''

_SAVED = {}


def setUpModule():
    """One umask and a module HOME for the whole process: nothing below can reach the real ~/.claude."""
    _SAVED["umask"] = os.umask(0o022)
    _SAVED["home"] = os.environ.get("HOME")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-gate-module-"))
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


def real(path):
    return os.path.realpath(str(path))


def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


# Sizes in a reason: a number, thousands separators allowed, and its unit.
AMOUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(bytes?\b|[kmg]i?b\b|[kmg]\b|b\b)?", re.I)
SCALES = {"b": (1,), "byte": (1,), "bytes": (1,), "kb": (1e3, 2 ** 10), "kib": (2 ** 10,), "k": (1e3, 2 ** 10),
          "mb": (1e6, 2 ** 20), "mib": (2 ** 20,), "m": (1e6, 2 ** 20), "gb": (1e9, 2 ** 30), "gib": (2 ** 30,),
          "g": (1e9, 2 ** 30)}


def amounts(text):
    return [(float(num.replace(",", "")), (unit or "").lower()) for num, unit in AMOUNT.findall(text)]


def names_size(text, size):
    """Does `text` show `size` bytes: a number with a unit within 25% of it, or the exact byte count?"""
    for value, unit in amounts(text):
        if not unit and value == size:
            return True
        if any(0.75 * size <= value * scale <= 1.25 * size for scale in SCALES.get(unit, ())):
            return True
    return False


def names_cap(text, cap_mb):
    """Does `text` show a cap of `cap_mb` MB: the number itself (bare or in MB), or its bytes in some unit?"""
    for value, unit in amounts(text):
        if unit in ("", "mb", "mib", "m") and value == cap_mb:
            return True
        for scale in SCALES.get(unit, ()):
            if any(abs(value * scale - cap_mb * mb) <= 0.01 * cap_mb * mb for mb in (1e6, 2 ** 20)):
                return True
    return False


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.neutral = self.mk("neutral")       # the hook process's own cwd: in no repository
        seed = int.from_bytes(os.urandom(8), "big")
        self.rng = random.Random(seed)
        self.seed = seed
        self.mains = []

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

    def fill(self, folder, files=3, total=None):
        """`folder` holding `files` random files at random depths and modes and one empty subfolder; with `total`,
        the files' sizes add up to it. Returns the folder."""
        os.makedirs(folder, exist_ok=True)
        sizes = [None] * files
        if total is not None:
            sizes = [total // files] * files
            sizes[0] += total - sum(sizes)
        for k, size in enumerate(sizes):
            parts = [self.word() for _ in range(self.rng.randint(0, 2))]
            self.put(os.path.join(folder, *parts, f"f{k}-{self.word()}.bin"), size=size,
                     mode=self.rng.choice(FILE_MODES))
        os.makedirs(os.path.join(folder, "empty-" + self.word()), exist_ok=True)
        return folder

    def env(self, **extra):
        env = {"HOME": self.home, "PATH": PATH, "TMPDIR": self.mk("tmpdir"), "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
               "GIT_CEILING_DIRECTORIES": self.tmp, "GIT_AUTHOR_NAME": "Test",
               "GIT_AUTHOR_EMAIL": "test@test.invalid", "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "test@test.invalid"}
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def git(self, cwd, *args):
        p = subprocess.run([GIT, "-C", cwd, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def make_project(self, name="proj", branch="main", settings=None):
        """A repository with one commit on main, then on `branch`; `settings` is the `env` of its tracked
        .claude/settings.json."""
        main = self.mk(name)
        self.git(main, "init", "-q", "-b", "main")
        files = {
            "README.md": f"# {name} {self.word()}\n",
            "src/app.py": f"def login():\n    return {self.word()!r}\n",
            "src/util.py": f"VALUE = {self.word()!r}\n",
            "docs/a.md": f"a {self.word()}\n",
            "docs/b.md": f"b {self.word()}\n",
            "docs/agent/old.md": f"note {self.word()}\n",
            ".gitignore": GITIGNORE,
            ".claude/settings.json": json.dumps({"env": settings or {}}) + "\n",
            ".claude/agents/helper.md": f"helper {self.word()}\n",
            ".githooks/pre-commit": "#!/bin/sh\nexit 0\n",
            ".github/workflows/ci.yml": "on: pull_request\n",
        }
        for rel, text in files.items():
            self.put(os.path.join(main, rel), data=text.encode(), mode=0o755 if rel.startswith(".githooks") else 0o644)
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "First")
        if branch != "main":
            self.git(main, "switch", "-q", "-c", branch)
        self.mains.append(main)
        return main

    def commit_settings(self, main, settings):
        self.put(os.path.join(main, ".claude", "settings.json"), data=(json.dumps({"env": settings}) + "\n").encode())
        self.git(main, "add", ".claude/settings.json")
        self.git(main, "commit", "-q", "-m", "Settings")

    def add_worktree(self, main, name, branch, path=None):
        path = path or os.path.join(main, ".claude", "worktrees", name)
        self.git(main, "worktree", "add", "-q", "-b", branch, path)
        return real(path)

    def ignored(self, repo, rel):
        p = subprocess.run([GIT, "-C", repo, "check-ignore", "-q", "--", rel], env=self.env(),
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode == 0

    def tracked(self, repo, rel):
        p = subprocess.run([GIT, "-C", repo, "ls-files", "--error-unmatch", "--", rel], env=self.env(),
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode == 0

    # ----------------------------------------------------------- the hook
    def event(self, command, cwd, mode="auto"):
        return {"session_id": "s1", "transcript_path": "/dev/null", "cwd": cwd, "permission_mode": mode,
                "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                "tool_use_id": "toolu_01"}

    def run_raw(self, stdin, project, env=None, hook=None, argv=None):
        """The hook run as hooks.json runs it: `python3 <hook>`, `stdin` on its stdin, CLAUDE_PROJECT_DIR set to
        `project` (None: unset), from a folder in no repository."""
        return subprocess.run(argv or [sys.executable, hook or HOOK], input=stdin, capture_output=True, text=True,
                              env=self.env(CLAUDE_PROJECT_DIR=project, **(env or {})), cwd=self.neutral,
                              timeout=90)

    def run_gate(self, command, cwd, project, mode="auto", env=None):
        """The hook's run on a Bash `command` in `cwd`; exit 0 and nothing on stderr."""
        p = self.run_raw(json.dumps(self.event(command, cwd, mode)), project, env)
        last = p.stderr.strip().splitlines()[-1:] or [""]
        self.assertEqual((p.returncode, p.stderr), (0, ""),
                         f"backup_gate.py on {command!r} exited {p.returncode}: {last[0]} (seed {self.seed})")
        return p

    def answer(self, p):
        """(decision, reason) of the hook's stdout; (None, None) when it printed nothing."""
        if not p.stdout.strip():
            return None, None
        out = json.loads(p.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"], p.stdout)
        d = out["hookSpecificOutput"]
        self.assertEqual(d.get("hookEventName"), "PreToolUse", p.stdout)
        self.assertIn(d.get("permissionDecision"), ("allow", "ask"), p.stdout)
        reason = d.get("permissionDecisionReason")
        self.assertIsInstance(reason, str, p.stdout)
        self.assertTrue(reason.strip(), p.stdout)
        return d["permissionDecision"], reason

    # ----------------------------------------------------------- the stores
    def global_store(self, home=None):
        return os.path.join(home or self.home, ".claude", "harness", "backups")

    def project_store(self, main):
        return os.path.join(main, ".git", "harness-backups")

    def snapshots(self):
        """Every folder in the global store and in each fixture repository's project store."""
        found = set()
        for store in [self.global_store()] + [self.project_store(m) for m in self.mains]:
            if os.path.isdir(store):
                found.update(os.path.join(store, n) for n in os.listdir(store)
                             if os.path.isdir(os.path.join(store, n)))
        return found

    def snapshot_made(self, before, store, paths, command):
        """The id of the one snapshot made since `before`, in `store`; its items are `paths` (unless None)."""
        new = self.snapshots() - before
        self.assertEqual(len(new), 1, f"{command!r}: snapshots made {sorted(new)} (seed {self.seed})")
        folder = new.pop()
        self.assertEqual(os.path.dirname(folder), store, f"{command!r}: snapshot in the wrong store")
        sid = os.path.basename(folder)
        self.assertRegex(sid, r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if paths is not None:
            self.assertEqual({real(i["path"]) for i in manifest["items"]}, {real(p) for p in paths},
                             f"{command!r}: what the snapshot holds (seed {self.seed})")
        return sid

    def expect_backup(self, p, before, store, paths, command):
        """Allow, one new snapshot in `store` holding `paths`, its id in the reason; returns the id."""
        decision, reason = self.answer(p)
        self.assertEqual(decision, "allow", f"{command!r}: {p.stdout!r} (seed {self.seed})")
        sid = self.snapshot_made(before, store, paths, command)
        self.assertEqual(set(ID_RE.findall(reason)), {sid}, f"{command!r}: the reason names another id: {reason}")
        return sid

    def expect_snapshot_only(self, p, before, store, paths, command):
        """No decision, but one new snapshot in `store` holding `paths`; returns its id."""
        self.assertEqual(p.stdout.strip(), "", f"{command!r}: expected no decision (seed {self.seed})")
        return self.snapshot_made(before, store, paths, command)

    def expect_allow_without_copy(self, p, before, command):
        decision, _reason = self.answer(p)
        self.assertEqual(decision, "allow", f"{command!r}: {p.stdout!r} (seed {self.seed})")
        self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made (seed {self.seed})")

    def expect_nothing(self, p, before, command):
        self.assertEqual(p.stdout.strip(), "", f"{command!r}: expected no decision (seed {self.seed})")
        self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made (seed {self.seed})")

    def restore(self, sid, cwd):
        p = subprocess.run([sys.executable, TOOL, "backup", "restore", sid], cwd=cwd, env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(p.returncode, 0, f"harness-work backup restore {sid}: {p.stdout}{p.stderr}")
        return p

    def round_trip(self, sid, targets, was, cwd):
        """Remove `targets` as the command would, restore `sid` and compare with `was`."""
        for target in targets:
            remove(target)
            self.assertFalse(os.path.lexists(target))
        self.restore(sid, cwd)
        self.assertEqual({t: tree(t) for t in targets}, was, f"restored from {sid} (seed {self.seed})")


# ================================================================ backed up and allowed
class BackedUp(GateTest):
    def test_rm_of_an_untracked_dir_is_backed_up_allowed_and_restorable(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        sub = os.path.join(wt, "sub")
        a = self.fill(os.path.join(wt, "build-cache-" + self.word()))
        b = self.fill(os.path.join(sub, "out-" + self.word()), files=4)
        c1 = self.fill(os.path.join(wt, "gen-" + self.word()), files=2)
        c2 = self.fill(os.path.join(wt, "tmpdata-" + self.word()))
        stem = "c" + self.word()
        g1, g2 = (self.fill(os.path.join(wt, f"{stem}-{k}"), files=2) for k in "ab")
        empty = os.path.join(wt, "empty-" + self.word())
        os.mkdir(empty)
        os.chmod(empty, 0o750)
        cases = (
            (f"rm -rf {os.path.basename(a)}", wt, main, [a]),
            (f"rm -r -f ./{os.path.basename(b)}/", sub, wt, [b]),                     # project: a worktree
            (f"/bin/rm -Rf -- {c1} {os.path.basename(c2)}", wt, main, [c1, c2]),
            (f"rm -rf {stem}-*", wt, main, [g1, g2]),                                  # a glob, expanded
            (f"rmdir {os.path.basename(empty)}", wt, main, [empty]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command):
                self.assertFalse(any(self.ignored(wt, os.path.relpath(t, wt)) for t in targets))
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_rm_of_a_tracked_file_on_a_ticket_branch_is_backed_up(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        other = self.make_project("proj2", branch="t/2-y")      # a main checkout on a ticket branch
        cases = (
            ("rm src/app.py", wt, main, [os.path.join(wt, "src", "app.py")]),
            ("rm -f a.md ../src/util.py", os.path.join(wt, "docs"), wt,
             [os.path.join(wt, "docs", "a.md"), os.path.join(wt, "src", "util.py")]),
            ("unlink docs/b.md", wt, main, [os.path.join(wt, "docs", "b.md")]),
            ("rm -r src", wt, main, [os.path.join(wt, "src")]),
            ("rm README.md", other, other, [os.path.join(other, "README.md")]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command, cwd=cwd):
                for t in targets:
                    top = other if t.startswith(other + os.sep) else wt
                    self.assertTrue(self.tracked(top, os.path.relpath(t, top)), t)
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_rebuildable_targets_are_allowed_without_a_copy(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.global_store()
        nm_marker = os.urandom(48)
        self.fill(os.path.join(wt, "node_modules"))
        self.put(os.path.join(wt, "node_modules", "left-pad", "marker.bin"), data=nm_marker)
        self.fill(os.path.join(wt, "dist"))                                     # not git-ignored: the name counts
        self.assertFalse(self.ignored(wt, "dist"))
        nothing_to_copy = ["rm -rf node_modules dist"]
        for k, name in enumerate(REBUILDABLE):
            rel = name if k % 2 == 0 else f"lib/{self.word()}/{name}"
            self.fill(os.path.join(wt, rel), files=2)
            nothing_to_copy.append(f"rm -rf {rel}")
        self.put(os.path.join(wt, "pkg", "__pycache__", "mod.cpython-39.pyc"))
        self.fill(os.path.join(wt, "web", "node_modules", ".cache"), files=2)
        nothing_to_copy += [
            "rm -f pkg/__pycache__/mod.cpython-39.pyc",                         # a file under a named folder
            "rm -rf web/node_modules/.cache",
            f"rm -f missing-{self.word()}.txt",                                 # missing: nothing to copy
            f"rm -rf node_modules missing-{self.word()}",
        ]
        for command in nothing_to_copy:
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_allow_without_copy(self.run_gate(command, wt, main), before, command)

        # Ruling 2: a git-ignored path that is not under a named folder is copied.
        scratch = os.path.join(wt, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        self.assertTrue(self.ignored(wt, os.path.basename(scratch)))
        # Names that only contain a rebuildable name are copied.
        lookalikes = [self.fill(os.path.join(wt, n), files=1)
                      for n in ("distro", "builds", "prebuild", "node_modules.bak", "venv2", "my_dist")]
        notes = self.fill(os.path.join(wt, "notes-" + self.word()))
        copied = (
            (f"rm -f {os.path.basename(scratch)}", [scratch], [scratch]),
            ("rm -rf " + " ".join(os.path.basename(p) for p in lookalikes), lookalikes, lookalikes),
            (f"rm -rf node_modules {os.path.basename(notes)}", [notes], [notes]),     # only notes is copied
        )
        for command, targets, held in copied:
            with self.subTest(command=command):
                before, was = self.snapshots(), {t: tree(t) for t in held}
                sid = self.expect_backup(self.run_gate(command, wt, main), before, store, targets, command)
                self.assertFalse(holds(store, nm_marker), f"{command!r}: node_modules went into the snapshot")
                self.round_trip(sid, held, was, wt)

        # A named folder inside a copied target is left out of the copy and of the size: 2.5 MB of
        # web/node_modules under a 1 MB cap is no reason to ask.
        app = os.path.join(wt, "app-" + self.word())
        self.put(os.path.join(app, "index.js"))
        self.put(os.path.join(app, "src", "view.js"), mode=0o640)
        big = self.put(os.path.join(app, "node_modules", "react", "big.bin"), size=2_500_000)
        command = f"rm -rf {os.path.basename(app)}"
        with self.subTest(command=command):
            before = self.snapshots()
            want = {k: v for k, v in tree(app).items() if k != "node_modules" and not k.startswith("node_modules/")}
            p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": "1"})
            sid = self.expect_backup(p, before, store, None, command)
            self.assertFalse(holds(os.path.join(store, sid), big[:4096]), "node_modules went into the snapshot")
            remove(app)
            self.restore(sid, wt)
            self.assertEqual(tree(app), want, f"restored from {sid} (seed {self.seed})")

    def test_sensitive_path_snapshot_stays_in_the_project(self):
        main = self.make_project("vault", settings={"HARNESS_SENSITIVE_PATHS": "data/**, *.db"})
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.project_store(main)
        wt_data = self.fill(os.path.join(wt, "data"), files=4)
        main_data = self.fill(os.path.join(main, "data"))
        self.assertTrue(self.ignored(wt, "data") and self.ignored(main, "data"))
        data_build = self.fill(os.path.join(wt, "data", "build"), files=2)       # sensitive beats rebuildable
        dist = self.fill(os.path.join(wt, "dist"), files=2)
        self.put(os.path.join(dist, "cache.db"))                                 # dist holds a sensitive path
        notes = self.fill(os.path.join(wt, "notes-" + self.word()), files=2)    # matches nothing: same store
        cases = (
            ("rm -rf data/build", wt, wt, [data_build]),
            ("rm -rf data", wt, main, [wt_data]),
            ("rm -rf data", main, main, [main_data]),                           # on main: git-ignored, so allowed
            ("rm -rf dist", wt, main, [dist]),
            (f"rm -rf {os.path.basename(notes)}", wt, main, [notes]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, store, targets, command)
                self.round_trip(sid, targets, was, cwd)
        self.fill(os.path.join(wt, "node_modules"), files=1)                   # neither sensitive nor copied
        before = self.snapshots()
        self.expect_allow_without_copy(self.run_gate("rm -rf node_modules", wt, main), before, "rm -rf node_modules")
        glob = self.global_store()
        self.assertFalse(os.path.lexists(glob) and not os.path.isdir(glob), glob)
        self.assertEqual(os.listdir(glob) if os.path.isdir(glob) else [], [], f"written under {glob}")

    def test_secret_shaped_ignored_file_is_copied(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.global_store()

        def at(rel, mode=0o600):
            path = os.path.join(wt, rel)
            self.put(path, mode=mode)
            return path

        env, env_local, env_prod = at(".env"), at(".env.local"), at("config/.env.production")
        pem, key = at("certs/server.pem"), at("keys/deploy.key")
        creds = self.fill(os.path.join(wt, "credentials"), files=2)
        token = at("credentials/token.json")
        build_env, nm_pem = at("build/.env"), at("node_modules/pkg/client.pem")
        dist_creds = self.fill(os.path.join(wt, "dist", "credentials"), files=1)
        for rel in (".env", ".env.local", "config/.env.production", "certs/server.pem", "keys/deploy.key",
                    "credentials", "build/.env", "node_modules/pkg/client.pem"):
            self.assertTrue(self.ignored(wt, rel), rel)
        cases = (
            ("rm .env", [env]),
            ("rm -f .env.local config/.env.production", [env_local, env_prod]),
            ("rm -f certs/server.pem keys/deploy.key", [pem, key]),
            ("rm -f credentials/token.json", [token]),
            ("rm -rf credentials", [creds]),
            ("rm -f build/.env", [build_env]),                                   # secret beats rebuildable
            ("rm -f node_modules/pkg/client.pem", [nm_pem]),
            ("rm -rf dist/credentials", [dist_creds]),
        )
        for command, targets in cases:
            with self.subTest(command=command):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                sid = self.expect_backup(self.run_gate(command, wt, main), before, store, targets, command)
                self.round_trip(sid, targets, was, wt)
        at("build/notes.txt")
        at("node_modules/pkg/index.js")
        for command in ("rm -f build/notes.txt", "rm -f node_modules/pkg/index.js"):
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_allow_without_copy(self.run_gate(command, wt, main), before, command)


# ================================================================ the cap
class Cap(GateTest):
    def test_over_the_cap_asks_without_a_snapshot(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        r = self.rng.randint

        def folder(prefix, total):
            return self.fill(os.path.join(wt, f"{prefix}-{self.word()}"), files=3, total=total)

        big1 = folder("big", r(2_300_000, 2_900_000))
        big3 = folder("huge", r(6_600_000, 8_200_000))
        data = self.fill(os.path.join(wt, "data"), files=3, total=r(1_600_000, 2_000_000))   # git-ignored
        self.assertTrue(self.ignored(wt, "data"))
        pair = [folder("half", r(580_000, 650_000)) for _ in range(2)]              # each under, both over
        sparse = os.path.join(wt, "disk-" + self.word() + ".img")
        with open(sparse, "wb") as fh:
            fh.truncate(215_000_000)                                                  # over 200 MB and 200 MiB

        def size(paths):
            """The regular files' st_size at and under `paths`, links not followed (never read: one is sparse)."""
            total = 0
            for p in paths:
                if not os.path.isdir(p):
                    total += os.lstat(p).st_size
                    continue
                for folder, _dirs, files in os.walk(p):
                    total += sum(os.lstat(os.path.join(folder, n)).st_size for n in files
                                 if stat.S_ISREG(os.lstat(os.path.join(folder, n)).st_mode))
            return total

        asks = (
            ("1", [big1]),
            ("3", [big3]),
            ("1", [data]),
            ("1", pair),
            (None, [sparse]),                                                         # the default cap
        )
        for cap, targets in asks:
            command = "rm -rf " + " ".join(os.path.basename(t) for t in targets)
            with self.subTest(command=command, cap=cap):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": cap})
                decision, reason = self.answer(p)
                self.assertEqual(decision, "ask", f"{command!r} cap {cap}: {p.stdout!r}")
                self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made")
                total = size(targets)
                self.assertTrue(names_size(reason, total), f"the reason does not name {total} bytes: {reason}")
                self.assertTrue(names_cap(reason, int(cap or 200)), f"the reason does not name the cap: {reason}")
        remove(sparse)

        under = folder("mid", r(1_800_000, 2_200_000))
        nm = self.fill(os.path.join(wt, "node_modules"), files=3, total=r(2_300_000, 2_900_000))
        plain = folder("plain", r(2_300_000, 2_900_000))
        for cap, target, copied in (("3", under, True), ("1", nm, False), (None, plain, True)):
            command = f"rm -rf {os.path.basename(target)}"
            with self.subTest(command=command, cap=cap):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": cap})
                if copied:
                    self.expect_backup(p, before, self.global_store(), [target], command)
                else:
                    self.expect_allow_without_copy(p, before, command)


# ================================================================ the other parts of a command
class Parts(GateTest):
    def test_harmless_parts_allow_other_parts_give_no_decision(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        os.makedirs(os.path.join(wt, "sub"))
        allowed = (
            ("cd sub && rm -rf {d}", "sub"),
            ("pushd sub && rm -rf {d} && popd", "sub"),
            ("(cd sub && rm -rf {d}); pwd", "sub"),
            ("pwd; rm -rf {d}; ls -la", ""),
            ("echo removing && rm -rf {d} && echo done", ""),
            ("test -d {d} && rm -rf {d}", ""),
            ("[ -d {d} ] && rm -rf {d}", ""),
            ("rm -rf {d} || true", ""),
            (": ; rm -rf {d}", ""),
            ("printf 'clean\\n'; rm -rf {d} 2>/dev/null", ""),
            ("rm -rf {d} >/dev/null 2>&1; echo ok 2>&1", ""),
            ("ls {d} > /dev/null && rm -rf {d}", ""),
        )
        undecided = (
            ("rm -rf {d} && python3 build.py", ""),
            ("rm -rf {d}; make", ""),
            ("rm -rf {d} && npm install", ""),
            ("echo hi > log-{w}.txt; rm -rf {d}", ""),
            ("ls > files-{w}.txt && rm -rf {d}", ""),
            ("cat {d}/keep.bin && rm -rf {d}", ""),
            ("cd sub && rm -rf {d} && git status", "sub"),
            ("rm -rf {d} && echo built >> out-{w}.log", ""),
        )
        store = self.global_store()
        for k, (shape, where) in enumerate(allowed + undecided):
            name = "x-" + self.word()
            target = self.fill(os.path.join(wt, where, name), files=2)
            self.put(os.path.join(target, "keep.bin"))
            command = shape.format(d=name, w=self.word())
            with self.subTest(command=command):
                before, was = self.snapshots(), {target: tree(target)}
                p = self.run_gate(command, wt, main)
                if k < len(allowed):
                    sid = self.expect_backup(p, before, store, [target], command)
                else:
                    sid = self.expect_snapshot_only(p, before, store, [target], command)
                if k in (0, len(allowed)):
                    self.round_trip(sid, [target], was, wt)


# ================================================================ what gets nothing
class Nothing(GateTest):
    def test_unknown_or_outside_targets_get_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        victim = self.fill(os.path.join(wt, "victim-" + self.word()))
        v = os.path.basename(victim)
        self.put(os.path.join(wt, "list.txt"), data=(v + "\n").encode())
        outside = self.fill(self.mk("outside", "dir-" + self.word()))
        in_home = self.fill(os.path.join(self.home, "scratch-" + self.word()))
        other = self.make_project("other", branch="dev")
        stuff = self.fill(os.path.join(other, "stuff-" + self.word()))
        # A $ in a target makes it unknown, whatever the hook's own environment holds: these name the real target.
        hook_env = {"DIR": victim, "TARGET": victim, "VICTIM": v}
        nothing = (
            ("rm -rf $DIR", wt, main),
            ('rm -rf "$TARGET"', wt, main),
            ("rm -rf ${VICTIM}", wt, main),
            (f"rm -rf $(echo {v})", wt, main),
            (f"rm -rf `echo {v}`", wt, main),
            (f"printf '%s\\n' {v} | xargs rm -rf", wt, main),
            ("xargs rm -rf < list.txt", wt, main),
            (f"ls -d {v} | xargs -0 rm -r", wt, main),
            (f"cd - && rm -rf {v}", wt, main),
            (f"cd $D && rm -rf {v}", wt, main),
            (f"rm -rf nomatch-{self.word()}-*", wt, main),
            (f"rm -rf {v} $DIR", wt, main),
            (f"find . -name {v} -exec rm -rf {{}} +", wt, main),
            (f"rm -rf {outside}", wt, main),
            (f"rm -rf {os.path.relpath(outside, wt)}", wt, main),
            (f"rm -rf ~/{os.path.basename(in_home)}", wt, main),
            (f"rm -rf {stuff}", wt, main),
            (f"rm -rf {os.path.basename(stuff)}", other, main),               # another repository's cwd
            (f"rm -rf {v} {outside}", wt, main),
        )
        for command, cwd, project in nothing:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, project, env=hook_env), before, command)

        stuff2 = self.fill(os.path.join(other, "stuff-" + self.word()))
        victim2 = self.fill(os.path.join(wt, "victim-" + self.word()))
        scratch = os.path.join(main, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        backed = (
            (f"rm -rf {os.path.basename(stuff2)}", other, None, [stuff2]),     # no CLAUDE_PROJECT_DIR: the cwd's
            (f"rm -rf {victim2}", os.path.dirname(outside), main, [victim2]),  # cwd in no repo, target inside
            (f"rm -f {scratch}", wt, wt, [scratch]),                           # the worktree's main checkout
        )
        for command, cwd, project, targets in backed:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project, env=hook_env)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, os.path.dirname(targets[0]))

    def test_roots_git_dirs_and_guard_files_get_nothing(self):
        main = self.make_project(branch="t/2-y")       # off main: branch_guard refuses nothing in this repository
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        wt2 = self.add_worktree(main, "t-3-z", "t/3-z", path=os.path.join(self.tmp, "proj-wt2"))
        self.fill(os.path.join(wt, ".githooks", "extra"), files=1)
        plain = self.fill(os.path.join(wt, "plain-" + self.word()), files=1)
        homedir = self.fill(os.path.join(main, "homedir-" + self.word()), files=1)
        nothing = (
            ("rm -rf .", main, main, None),
            (f"rm -rf {main}/", wt, main, None),
            ("rm -rf ../../..", wt, main, None),
            ("rm -rf .claude/worktrees/t-1-x", main, main, None),
            ("cd .. && rm -rf t-1-x", wt, main, None),
            (f"rm -rf {wt2}", main, main, None),
            ("rm -rf .", wt2, wt2, None),
            ("rm -rf .git", main, main, None),
            ("rm -rf .git/hooks", main, main, None),
            (f"rm -f {main}/.git/index", wt, main, None),
            ("rm -f .git", wt, main, None),
            ("rm .claude/settings.json", wt, main, None),
            ("cd .claude && rm -f settings.json", wt, main, None),
            ("rm -rf .claude", wt2, main, None),
            ("rm -rf .claude/agents", main, main, None),
            ("rm -rf .githooks", wt, main, None),
            ("rm .github/workflows/ci.yml", wt, main, None),
            (f"rm -rf {os.path.basename(plain)} .github", wt, main, None),
            ("rm -rf ~", main, main, {"HOME": homedir}),                       # a HOME inside the project
        )
        for command, cwd, project, env in nothing:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, project, env=env), before, command)
        self.assertFalse(os.path.lexists(os.path.join(homedir, ".claude")))

        in_wt = self.fill(os.path.join(wt, "cache-" + self.word()), files=2)
        lookalikes = [self.fill(os.path.join(wt, n + self.word()), files=1) for n in (".claude-old-", ".github-")]
        in_wt2 = self.fill(os.path.join(wt2, "cache-" + self.word()), files=2)
        backed = (
            (f"rm -rf .claude/worktrees/t-1-x/{os.path.basename(in_wt)}", main, main, [in_wt]),
            ("rm -rf " + " ".join(os.path.basename(p) for p in lookalikes), wt, main, lookalikes),
            (f"rm -rf {in_wt2}", main, main, [in_wt2]),
        )
        for command, cwd, project, targets in backed:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_branch_guard_denials_stay_denied_with_no_snapshot(self):
        main = self.make_project()                     # the main checkout on main
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        notes = self.fill(os.path.join(main, "notes-" + self.word()), files=1)    # untracked, not ignored
        scratch = os.path.join(main, f"scratch-{self.word()}.tmp")                 # git-ignored
        self.put(scratch)
        s = os.path.basename(scratch)
        victim = self.fill(os.path.join(wt, "victim-" + self.word()), files=1)
        denied = (
            ("rm src/app.py", main),
            ("rm -rf src", main),
            ("cd src && rm -f app.py", main),
            (f"rm -rf {os.path.basename(notes)}", main),
            (f"rm -f {s} && rm docs/a.md", main),
            (f"rm -f {main}/README.md", wt),
            (f"rm -rf {os.path.basename(victim)} && git push --force origin t/1-x", wt),
        )
        for command, cwd in denied:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, main), before, command)

        backed = (
            (f"rm -f {s}", None, [scratch]),                                    # branch_guard's ignored exemption
            ("rm -f docs/agent/old.md", None, [os.path.join(main, "docs", "agent", "old.md")]),
            ("rm src/app.py", {"HARNESS_ALLOW_MAIN": "1"}, [os.path.join(main, "src", "app.py")]),
        )
        for command, env, targets in backed:
            with self.subTest(command=command, env=env):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, main, main, env=env)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, main)
        self.commit_settings(main, {"HARNESS_ALLOW_MAIN": "1"})
        target = os.path.join(main, "docs", "b.md")
        with self.subTest(command="rm -f docs/b.md", settings="HARNESS_ALLOW_MAIN=1"):
            before, was = self.snapshots(), {target: tree(target)}
            p = self.run_gate("rm -f docs/b.md", main, main)
            sid = self.expect_backup(p, before, self.global_store(), [target], "rm -f docs/b.md")
            self.round_trip(sid, [target], was, main)

    def test_plan_mode_and_backup_off_get_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")

        def victim(where):
            return self.fill(os.path.join(where, "victim-" + self.word()), files=2)

        for mode in ("default", "auto", "acceptEdits", "bypassPermissions"):
            target = victim(wt)
            command = f"rm -rf {os.path.basename(target)}"
            with self.subTest(mode=mode):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, mode=mode)
                self.expect_backup(p, before, self.global_store(), [target], command)
        target = victim(wt)
        command = f"rm -rf {os.path.basename(target)}"
        for mode, env in (("plan", None), ("auto", {"HARNESS_BACKUP": "off"}), ("default", {"HARNESS_BACKUP": "off"})):
            with self.subTest(mode=mode, env=env):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, wt, main, mode=mode, env=env), before, command)

        off = self.make_project("proj-off", settings={"HARNESS_BACKUP": "off"})
        wt_off = self.add_worktree(off, "t-4-off", "t/4-off")
        target = victim(wt_off)
        scratch = os.path.join(off, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        for command, cwd in ((f"rm -rf {os.path.basename(target)}", wt_off), (f"rm -f {os.path.basename(scratch)}", off)):
            with self.subTest(command=command, settings="HARNESS_BACKUP=off"):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, off), before, command)
        command = f"rm -rf {os.path.basename(target)}"
        with self.subTest(command=command, settings="HARNESS_BACKUP=off", env="HARNESS_BACKUP=on"):
            before = self.snapshots()
            p = self.run_gate(command, wt_off, off, env={"HARNESS_BACKUP": "on"})
            self.expect_backup(p, before, self.global_store(), [target], command)

    def test_commands_without_rm_print_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        os.makedirs(os.path.join(wt, "sub"))
        self.fill(os.path.join(wt, "build"), files=1)
        commands = (
            "ls -la", "pwd", "cd sub && ls", "true", "echo hi", "git status", "python3 build.py", "make clean",
            "mv a.txt b.txt && cp b.txt c.txt", "cat rm.txt", "echo firm > rm.log", "npm run rmdir-helper",
            "rmx a", "trm a", "farm --charm", "unlinked x", "rmdirs y", "echo rm -rf build", "ls -l rmdir",
            "printf '%s\\n' unlink", "grep -rn 'rm -rf' docs",
        )
        for command in commands:
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, wt, main), before, command)
        edit = dict(self.event("", wt), tool_name="Edit",
                    tool_input={"file_path": os.path.join(wt, "src", "app.py"), "old_string": "a", "new_string": "b"})
        read = dict(self.event("", wt), tool_name="Read", tool_input={"file_path": os.path.join(wt, "README.md")})
        raw = (
            json.dumps(edit), json.dumps(read), json.dumps(self.event("", wt)), json.dumps(self.event("   ", wt)),
            json.dumps(dict(self.event("", wt), tool_input="rm -rf build")),
            json.dumps({k: v for k, v in self.event("", wt).items() if k != "tool_input"}),
            "not json at all, rm -rf build", "", "[]",
        )
        for stdin in raw:
            with self.subTest(stdin=stdin[:60]):
                before = self.snapshots()
                p = self.run_raw(stdin, main)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), stdin[:60])
                self.assertEqual(self.snapshots() - before, set())


# ================================================================ failing open
class FailOpen(GateTest):
    def plugin(self, lib):
        """hooks/ copied from the checkout next to a lib/ that is missing, raises at import, or is the real one."""
        root = os.path.join(self.tmp, f"plugin-{lib}")
        shutil.copytree(HOOKS_DIR, os.path.join(root, "hooks"), ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "raises":
            pkg = os.path.join(root, "lib", "harness_lib")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write(f"raise RuntimeError({self.token!r})\n")
        return os.path.join(root, "hooks", "backup_gate.py")

    def one_line(self, p, *needles):
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "", "a decision")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stderr.splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertTrue(lines[0].startswith("backup_gate.py:"), lines[0])
        for needle in needles:
            self.assertIn(needle, lines[0])

    def test_fails_open_without_the_lib(self):
        self.token = "gate-" + self.word()
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        target = self.fill(os.path.join(wt, "victim-" + self.word()))
        stdin = json.dumps(self.event(f"rm -rf {os.path.basename(target)}", wt))
        for lib, needles in (("missing", ("harness_lib",)), ("raises", ("harness_lib", self.token))):
            with self.subTest(lib=lib):
                before = self.snapshots()
                self.one_line(self.run_raw(stdin, main, hook=self.plugin(lib)), *needles)
                self.assertEqual(self.snapshots() - before, set())
        with self.subTest(error="an internal crash"):
            before = self.snapshots()
            p = self.run_raw(stdin, main, env={"BACKUP_GATE_TEST_TOKEN": self.token},
                             argv=[sys.executable, "-B", "-c", CRASH_DRIVER, HOOK])
            self.one_line(p, self.token)
            self.assertEqual(self.snapshots() - before, set())
        with self.subTest(error="a store that cannot be written"):
            store = self.global_store()
            self.put(store, data=b"not a folder\n")
            before = self.snapshots()
            self.one_line(self.run_raw(stdin, main))
            self.assertEqual(self.snapshots() - before, set())
            with open(store, "rb") as fh:
                self.assertEqual(fh.read(), b"not a folder\n")
            self.assertTrue(os.path.isdir(target))


# ================================================================ registration
class Registration(unittest.TestCase):
    def test_hooks_json_registers_it_with_a_30s_timeout(self):
        self.assertTrue(os.path.isfile(HOOK), HOOK)
        with open(HOOKS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        found = []
        for event, entries in data["hooks"].items():
            for entry in entries:
                for k, hook in enumerate(entry["hooks"]):
                    words = [str(hook.get("command", ""))] + [str(a) for a in hook.get("args", [])]
                    if any("backup_gate.py" in w for w in words):
                        found.append((event, entry.get("matcher"), k, hook, entry))
        self.assertEqual(len(found), 1, found)
        event, matcher, k, hook, entry = found[0]
        self.assertEqual((event, matcher), ("PreToolUse", "Bash"))
        self.assertEqual([hook.get("type"), hook.get("command"), hook.get("args"), hook.get("timeout")],
                         ["command", "python3", ["${CLAUDE_PLUGIN_ROOT}/hooks/backup_gate.py"], 30])
        names = [os.path.basename(str((h.get("args") or [""])[0])) for h in entry["hooks"]]
        self.assertIn("tests_first.py", names)
        self.assertGreater(k, names.index("tests_first.py"), names)


if __name__ == "__main__":
    unittest.main()
