"""Tests for the per-repository state key: ~/.claude/harness/repos/<key>/ (ticket #7).

    python3 -m unittest discover -s plugins/harness/tests -p "test_repo_key.py" -v

The key is the first 12 hex digits of the sha1 of the main working tree's real path with its case
canonicalised through the file system (the on-disk spelling). A repository reached through another
spelling of its path on a case-insensitive file system (macOS) gets the same key, and state written under
the old spelling-based key is moved once to the canonical key.

Every test builds, with real git under a temporary folder, a bare `origin`, a main checkout at <tmp>/<name>
pushed to it, and the branch t/12-fix-login in a linked worktree at <tmp>/<name>/.claude/worktrees/. The
name is new each run and mixed case ("Rk" + 6 upper hex + "q"), or all lowercase in the *LowercaseOnDisk
variants, so no fixed table of names can stand in for asking the file system. Git reports the main tree in
the spelling the worktree's `.git` file names, so rewriting that file with every component below the system
temp root case-swapped gives a worktree whose `git rev-parse --git-common-dir` is a non-canonical spelling:
the way a second spelling reaches every tool and hook. HOME is temporary. `fakebin`, first on PATH and written
once for the module, holds gh (fakegh.py), claude (answers ultrareview and `agents --json`), silent
terminal-notifier and osascript, and python3.

Two spellings of one path exist only on a case-insensitive file system, so those tests are skipped on a
case-sensitive one (CI on Linux); the tests that the canonical spelling keeps its key run everywhere.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from importlib.machinery import SourceFileLoader

sys.dont_write_bytecode = True      # importing the bin scripts must not leave a __pycache__ in bin/

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
BIN = os.path.join(PLUGIN, "bin")
HOOKS = os.path.join(PLUGIN, "hooks")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
BRANCH, SLUG = "t/12-fix-login", "t-12-fix-login"
UTC = dt.timezone.utc
W = {}

FAILING_TEST = ("import unittest\n\n\nclass LoginTest(unittest.TestCase):\n"
                "    def test_login(self):\n        self.assertEqual(1 + 1, 3)\n")


def sha12(path):
    """The key of a path string, computed here from first principles."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def swap(name):
    return name.swapcase()


def spelled(path, depth=1):
    """`path` with the case of its last `depth` components swapped."""
    head, parts = path, []
    for _ in range(depth):
        head, tail = os.path.split(head)
        parts.insert(0, swap(tail))
    return os.path.join(head, *parts)


TEMP_ROOT = os.path.realpath(tempfile.gettempdir())


def spelled_below(path, base=TEMP_ROOT):
    """`path` with the case of every component below `base` swapped (the fixture's own folders included)."""
    rel = os.path.relpath(path, base)
    assert not rel.startswith(os.pardir), (path, base)
    return os.path.join(base, *[swap(part) for part in rel.split(os.sep)])


def mixed_name(prefix):
    """A directory name new each run, in mixed case: prefix + 6 upper hex digits + "q"."""
    return prefix + uuid.uuid4().hex[:6].upper() + "q"


def lower_name(prefix):
    """A directory name new each run, all lowercase on disk."""
    return prefix.lower() + uuid.uuid4().hex[:6] + "q"


def on_disk(path):
    """True when every component of the absolute `path` is spelled as its parent directory lists it."""
    parent = os.sep
    for part in path.strip(os.sep).split(os.sep):
        try:
            if part not in os.listdir(parent):
                return False
        except OSError:
            return False
        parent = os.path.join(parent, part)
    return True


def _probe_case_insensitive():
    base = os.path.realpath(tempfile.mkdtemp(prefix="harness-key-probe-"))
    try:
        probe = os.path.join(base, "CaseProbe")
        os.mkdir(probe)
        other = os.path.join(base, "cASEpROBE")
        return os.path.exists(other) and os.path.samefile(probe, other) and on_disk(base)
    finally:
        shutil.rmtree(base, True)


CASE_INSENSITIVE = _probe_case_insensitive()
needs_case_insensitive = unittest.skipUnless(
    CASE_INSENSITIVE, "two spellings of one path need a case-insensitive file system")


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def read_json(path):
    if not os.path.isfile(path):                                  # a failure, not an error, when not written
        raise AssertionError(f"{path} was not written")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def load_tool(name):
    """Import bin/<name> (no .py suffix) by path, as a fresh module."""
    modname = name.replace("-", "_") + "_key_test"
    loader = SourceFileLoader(modname, os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def z(seconds_ago):
    return dt.datetime.fromtimestamp(time.time() - seconds_ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def setUpModule():
    if not GIT:
        raise unittest.SkipTest("git is not installed")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-key-"))
    fakebin = os.path.join(root, "fakebin")
    py = sys.executable
    executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "claude"),
               '#!/bin/sh\nif [ "$1" = agents ]; then echo "[]"; exit 0; fi\necho \'{"findings": []}\'\n')
    executable(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec "{py}" "$@"\n')
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), "#!/bin/sh\nexit 0\n")
    W.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(W.get("root", ""), True)


@unittest.skipUnless(GIT, "git is not installed")
class KeyFixture(unittest.TestCase):

    def main_name(self):
        return mixed_name("Rk")

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="KeyTest-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.tmp, "tmpdir"))
        self.scenario = os.path.join(self.tmp, "scenario.json")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_", "CLAUDE_", "FAKEGH_"))
               and k not in ("PYTHONPATH", "PWD")}
        env.update(HOME=self.home, TMPDIR=os.path.join(self.tmp, "tmpdir"), LANG="en_US.UTF-8",
                   PATH=os.pathsep.join([W["fakebin"], os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   FAKEGH_SCENARIO=self.scenario, FAKEGH_LOG=os.path.join(self.tmp, "gh.log"),
                   HARNESS_CLAUDE=os.path.join(W["fakebin"], "claude"))
        self.env = env

        origin = os.path.join(self.tmp, "origin.git")
        self.main = os.path.join(self.tmp, self.main_name())      # the on-disk spelling
        self.git(self.tmp, "init", "--bare", "-q", origin)
        self.git(self.tmp, "clone", "-q", origin, self.main)
        self.git(self.main, "symbolic-ref", "HEAD", "refs/heads/main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, "src", "app.py"), "def login():\n    return None\n")
        write(os.path.join(self.main, "tests", "test_app.py"), "def test_app():\n    pass\n")
        write(os.path.join(self.main, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_TEST_CMD": "python3 -m unittest {files}"}}))
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "Initial commit")
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        with open(os.path.join(self.main, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".claude/worktrees/\n")
        self.wt = os.path.join(self.main, ".claude", "worktrees", SLUG)
        self.git(self.main, "worktree", "add", "-q", self.wt, "-b", BRANCH, "origin/main")

        self.repos = os.path.join(self.home, ".claude", "harness", "repos")
        self.canon_key = sha12(self.main)
        self.canon = os.path.join(self.repos, self.canon_key)

    # ------------------------------------------------------------ helpers
    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", cwd, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                               *args], env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def spell_worktree(self):
        """Point the worktree's .git file at the main tree with every component below the system temp root
        case-swapped; return that spelling of the main tree."""
        other = spelled_below(self.main)
        dotgit = os.path.join(self.wt, ".git")
        with open(dotgit, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(self.main + os.sep, text)
        write(dotgit, text.replace(self.main + os.sep, other + os.sep))
        common = self.git(self.wt, "rev-parse", "--git-common-dir")
        if common != os.path.join(other, ".git"):
            self.skipTest(f"git normalised the common dir to {common}")
        self.old_key = sha12(other)
        self.old = os.path.join(self.repos, self.old_key)
        self.assertNotEqual(self.old_key, self.canon_key)
        return other

    def tool(self, name, *args, cwd=None, **extra):
        return subprocess.run([sys.executable, os.path.join(BIN, name), *args], cwd=cwd or self.wt,
                              env=dict(self.env, **extra), capture_output=True, text=True, timeout=120)

    def ok(self, name, *args, **kw):
        p = self.tool(name, *args, **kw)
        self.assertEqual(p.returncode, 0, f"{name} {' '.join(args)}: {p.stdout}{p.stderr}")
        return p

    def hook(self, name, payload):
        r = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env, cwd=self.tmp, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r

    def gh_issue(self, number=12, title="Fix login"):
        data = {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}",
                "body": "## Request\nFix it.\n\n## Definition of done\n- [ ] it works\n",
                "labels": [{"name": n} for n in ("task", "status:in-progress", "size:M", "risk:low")],
                "state": "OPEN", "parent": None, "blockedBy": [], "comments": []}
        write(self.scenario, json.dumps({"calls": [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "view", str(number)], "stdout_json": data}]}))

    def put_ticket(self, state, number=12, **fields):
        ticket = {"number": number, "title": "Fix login", "type": "task", "status": "in-progress",
                  "tier": "opus", "flags": [], "labels": ["task", "status:in-progress", "size:M"],
                  "sections": {"definition_of_done": ["it works"], "tests": [], "request": "r"},
                  "attempts": []}
        ticket.update(fields)
        return write(os.path.join(state, "tickets", f"{number}.json"), json.dumps(ticket))

    def put_red(self, state, kind="assertion", at="2026-10-13T10:00:00Z", files=("tests/test_app.py",),
                branch=BRANCH, top=None):
        h = hashlib.sha256()
        for name in sorted(files):
            with open(os.path.join(top or self.wt, name), "rb") as fh:
                h.update(name.encode("utf-8") + b"\n" + fh.read())
        record = {"branch": branch, "base_sha": "0" * 40, "test_files": list(files),
                  "run_files": list(files), "test_hash": h.hexdigest(),
                  "cmd": "python3 -m unittest tests/test_app.py", "exit": 0 if kind == "green" else 1,
                  "kind": kind, "tail": "AssertionError", "at": at}
        return write(os.path.join(state, "red", branch.replace("/", "-") + ".json"), json.dumps(record))

    def state_dirs(self):
        return sorted(os.listdir(self.repos)) if os.path.isdir(self.repos) else []

    def edit_decision(self, path, cwd=None):
        """tests_first.py on an Edit of `path`: the deny reason, or None when allowed."""
        payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": cwd or self.wt,
                   "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": "Edit",
                   "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"},
                   "tool_use_id": "toolu_01"}
        out = self.hook("tests_first.py", payload).stdout
        if not out.strip():
            return None
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]

    def brief(self, cwd):
        payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": cwd, "permission_mode": "default",
                   "hook_event_name": "SessionStart", "source": "startup"}
        out = json.loads(self.hook("session_brief.py", payload).stdout)
        return out["hookSpecificOutput"]["additionalContext"].splitlines()

    def run_every_tool(self):
        """Each tool's state-touching command, from the worktree."""
        self.put_ticket(self.canon)
        write(os.path.join(self.wt, "tests", "test_login.py"), FAILING_TEST)
        self.ok("harness-red", "check")
        self.ok("harness-work", "attempt", "12", "tests", "pass")
        self.gh_issue()
        self.ok("harness-board", "show", "12")
        self.ok("harness-pr", "ultra", "34")


# ================================================================ the canonical spelling keeps its key
class CanonicalSpellingKeepsItsKey(KeyFixture):
    """Runs on every file system: a repository reached by its on-disk spelling keeps sha1(realpath)."""

    def test_every_tool_writes_under_the_sha1_of_the_real_path(self):
        self.run_every_tool()
        self.assertEqual(self.state_dirs(), [self.canon_key])
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "red", SLUG + ".json")))
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "ultra", "34.json")))
        cache = read_json(os.path.join(self.canon, "tickets", "12.json"))
        self.assertEqual(cache["title"], "Fix login")
        self.assertEqual([(a["stage"], a["result"]) for a in cache["attempts"]], [("tests", "pass")])

    def test_a_symlink_to_the_checkout_gives_the_same_key(self):
        link = os.path.join(self.tmp, "link-to-proj")
        os.symlink(self.main, link)
        self.put_ticket(self.canon)
        self.ok("harness-work", "attempt", "12", "code", "fail", "--repo", link, cwd=self.tmp)
        self.assertEqual(self.state_dirs(), [self.canon_key])
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"]), 1)

    def test_hooks_read_the_canonical_key(self):
        self.put_ticket(self.canon, title="Canonical ticket")
        self.put_red(self.canon, at="2026-10-01T09:30:00Z")
        lines = self.brief(self.wt)
        self.assertIn("Ticket #12: Canonical ticket (in-progress, tier opus)", lines)
        self.assertIn("Red run: assertion at 2026-10-01T09:30:00Z", lines)
        self.assertIsNone(self.edit_decision(os.path.join(self.wt, "src", "app.py")))

    def test_two_checkouts_keep_two_keys(self):
        other = os.path.join(self.tmp, mixed_name("Ot"))
        self.git(self.tmp, "clone", "-q", os.path.join(self.tmp, "origin.git"), other)
        self.put_ticket(self.canon, attempts=[])
        self.put_ticket(os.path.join(self.repos, sha12(other)), attempts=[])
        self.ok("harness-work", "attempt", "12", "code", "fail", cwd=other)
        self.assertEqual(len(read_json(os.path.join(self.repos, sha12(other), "tickets", "12.json"))["attempts"]), 1)
        self.assertEqual(read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"], [])


# ================================================================ another spelling, one key
@needs_case_insensitive
class OtherSpellingOneKey(KeyFixture):
    """The worktree's git names the main tree in another spelling; every tool and hook still uses sha1 of the
    on-disk spelling."""

    def setUp(self):
        super().setUp()
        self.spell_worktree()

    def test_harness_red_records_under_the_canonical_key(self):
        write(os.path.join(self.wt, "tests", "test_login.py"), FAILING_TEST)
        self.ok("harness-red", "check")
        self.assertEqual(self.state_dirs(), [self.canon_key])
        record = read_json(os.path.join(self.canon, "red", SLUG + ".json"))
        self.assertEqual((record["branch"], record["kind"]), (BRANCH, "assertion"))

    def test_harness_work_reads_and_writes_the_canonical_cache(self):
        self.put_ticket(self.canon, tier="sonnet")
        p = self.ok("harness-work", "attempt", "12", "code", "fail")
        self.assertEqual(p.stdout.strip(), "retry")
        attempts = read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"]
        self.assertEqual([(a["stage"], a["tier"], a["result"]) for a in attempts], [("code", "sonnet", "fail")])
        self.assertEqual(self.state_dirs(), [self.canon_key])

    def test_harness_board_writes_the_canonical_cache(self):
        self.gh_issue(title="Board title")
        self.ok("harness-board", "show", "12")
        self.assertEqual(self.state_dirs(), [self.canon_key])
        self.assertEqual(read_json(os.path.join(self.canon, "tickets", "12.json"))["title"], "Board title")

    def test_harness_pr_saves_under_the_canonical_key(self):
        self.ok("harness-pr", "ultra", "34")
        self.assertEqual(self.state_dirs(), [self.canon_key])
        self.assertEqual(read_json(os.path.join(self.canon, "ultra", "34.json")), {"findings": []})

    def test_every_tool_shares_one_state_directory(self):
        self.run_every_tool()
        self.assertEqual(self.state_dirs(), [self.canon_key])

    def test_session_brief_finds_the_canonical_ticket_and_red_record(self):
        self.put_ticket(self.canon, title="Spelled ticket", tier="fable")
        self.put_red(self.canon, at="2026-10-02T08:15:00Z")
        lines = self.brief(self.wt)
        self.assertIn("Ticket #12: Spelled ticket (in-progress, tier fable)", lines)
        self.assertIn("Red run: assertion at 2026-10-02T08:15:00Z", lines)

    def test_tests_first_finds_the_canonical_red_record(self):
        source = os.path.join(self.wt, "src", "app.py")
        self.assertIsNotNone(self.edit_decision(source))          # nothing recorded yet: refused
        self.put_ticket(self.canon)
        self.put_red(self.canon)
        self.assertIsNone(self.edit_decision(source))
        self.put_red(self.canon, kind="green")                    # the canonical record decides
        self.assertTrue(self.edit_decision(source).startswith("The red run on t/12 passed"))


@needs_case_insensitive
class MainCheckoutOtherSpelling(KeyFixture):
    """The main checkout itself reached in another spelling (cwd, --repo, CLAUDE_PROJECT_DIR)."""

    def test_tools_and_brief_from_the_other_spelling_use_the_canonical_key(self):
        other = spelled_below(self.main)
        self.git(self.main, "switch", "-q", "-c", "t/13-main-ticket")
        self.put_ticket(self.canon, number=13, title="Main ticket")
        self.ok("harness-work", "attempt", "13", "code", "fail", cwd=other)
        self.ok("harness-work", "attempt", "13", "code", "fail", "--repo", other, cwd=self.tmp)
        self.ok("harness-work", "attempt", "13", "code", "fail", cwd=self.tmp, CLAUDE_PROJECT_DIR=other)
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "13.json"))["attempts"]), 3)
        self.assertIn("Ticket #13: Main ticket (in-progress, tier opus)", self.brief(other))
        self.assertEqual(self.state_dirs(), [self.canon_key])

    # ------------------------------------------------ migration of state under the other spelling's key
    def old_main_state(self, **fields):
        """The main checkout on t/13-main-ticket, a ticket cache under sha1 of its other spelling."""
        self.other = spelled_below(self.main)
        self.old = os.path.join(self.repos, sha12(self.other))
        self.assertNotEqual(sha12(self.other), self.canon_key)
        self.git(self.main, "switch", "-q", "-c", "t/13-main-ticket")
        attempts = [{"stage": "tests", "tier": "opus", "result": "pass", "at": "2026-10-12T00:00:00Z"}]
        self.put_ticket(self.old, number=13, **dict(dict(title="Old main ticket", attempts=attempts), **fields))

    def assertMainMoved(self):
        self.assertFalse(os.path.exists(self.old), "the old key's directory is still there")
        self.assertEqual(self.state_dirs(), [self.canon_key])

    def test_harness_work_with_repo_in_the_other_spelling_moves_the_old_cache(self):
        self.old_main_state()
        self.assertEqual(self.ok("harness-work", "attempt", "13", "code", "fail", "--repo", self.other,
                                 cwd=self.tmp).stdout.strip(), "retry")
        self.assertMainMoved()
        attempts = read_json(os.path.join(self.canon, "tickets", "13.json"))["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [("tests", "pass"), ("code", "fail")])

    def test_harness_work_with_project_dir_in_the_other_spelling_moves_the_old_cache(self):
        self.old_main_state(tier="sonnet")
        self.assertEqual(self.ok("harness-work", "attempt", "13", "code", "fail", cwd=self.tmp,
                                 CLAUDE_PROJECT_DIR=self.other).stdout.strip(), "retry")
        self.assertMainMoved()
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "13.json"))["attempts"]), 2)

    def test_session_brief_from_the_other_spelling_moves_and_shows_the_old_state(self):
        self.old_main_state(title="Old main brief", tier="fable")
        self.put_red(self.old, at="2026-10-04T06:00:00Z", branch="t/13-main-ticket", top=self.main)
        lines = self.brief(self.other)
        self.assertIn("Ticket #13: Old main brief (in-progress, tier fable)", lines)
        self.assertIn("Red run: assertion at 2026-10-04T06:00:00Z", lines)
        self.assertMainMoved()
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "red", "t-13-main-ticket.json")))
        self.assertIn("Ticket #13: Old main brief (in-progress, tier fable)", self.brief(self.main))
        self.assertMainMoved()

    def test_tests_first_on_a_path_in_the_other_spelling_moves_the_old_red_record(self):
        self.old_main_state()
        self.put_red(self.old, branch="t/13-main-ticket", top=self.main)
        self.assertIsNone(self.edit_decision(os.path.join(self.other, "src", "app.py"), cwd=self.other))
        self.assertMainMoved()
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "red", "t-13-main-ticket.json")))
        self.put_red(self.canon, kind="green", branch="t/13-main-ticket", top=self.main)
        self.assertTrue(self.edit_decision(os.path.join(self.main, "src", "app.py"), cwd=self.main)
                        .startswith("The red run on t/13 passed"))


@needs_case_insensitive
class WorktreeReachedInAnotherSpelling(KeyFixture):
    """The worktree itself reached through another spelling (cwd, payload cwd, edited path)."""

    def check_from_spelled_worktree(self):
        spelled_wt = spelled_below(self.wt)
        self.assertFalse(on_disk(spelled_wt))
        self.put_ticket(self.canon, title="Spelled worktree ticket", tier="sonnet")
        write(os.path.join(self.wt, "tests", "test_login.py"), FAILING_TEST)
        self.ok("harness-red", "check", cwd=spelled_wt)
        self.assertEqual(read_json(os.path.join(self.canon, "red", SLUG + ".json"))["kind"], "assertion")
        self.assertEqual(self.ok("harness-work", "attempt", "12", "code", "fail", cwd=spelled_wt).stdout.strip(),
                         "retry")
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"]), 1)
        self.assertIn("Ticket #12: Spelled worktree ticket (in-progress, tier sonnet)", self.brief(spelled_wt))
        source = os.path.join(spelled_wt, "src", "app.py")
        self.put_red(self.canon, kind="green")
        reason = self.edit_decision(source, cwd=spelled_wt)
        self.assertIsNotNone(reason, "a green red run must refuse the source edit")
        self.assertTrue(reason.startswith("The red run on t/12 passed"), reason)
        self.put_red(self.canon)
        self.assertIsNone(self.edit_decision(source, cwd=spelled_wt))
        self.assertEqual(self.state_dirs(), [self.canon_key])

    def test_tools_and_hooks_from_a_spelled_worktree_use_the_canonical_key(self):
        self.check_from_spelled_worktree()

    def test_spelled_worktree_whose_git_file_names_another_spelling(self):
        self.spell_worktree()
        self.check_from_spelled_worktree()


@needs_case_insensitive
class WorktreeOutsideTheMainTree(KeyFixture):
    """A linked worktree outside <main>/.claude/worktrees/, its .git file naming the main tree in another
    spelling: the key comes from the file system, not from stripping a path suffix."""

    BRANCH2, SLUG2 = "t/14-outside", "t-14-outside"

    def setUp(self):
        super().setUp()
        self.wt2 = os.path.join(self.tmp, mixed_name("Wt"))
        self.git(self.main, "worktree", "add", "-q", self.wt2, "-b", self.BRANCH2, "origin/main")
        other = spelled_below(self.main)
        dotgit = os.path.join(self.wt2, ".git")
        with open(dotgit, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(self.main + os.sep, text)
        write(dotgit, text.replace(self.main + os.sep, other + os.sep))
        if self.git(self.wt2, "rev-parse", "--git-common-dir") != os.path.join(other, ".git"):
            self.skipTest("git normalised the common dir")
        self.old = os.path.join(self.repos, sha12(other))

    def test_tools_and_hooks_use_the_canonical_key(self):
        self.put_ticket(self.canon, number=14, title="Outside ticket")
        write(os.path.join(self.wt2, "tests", "test_login.py"), FAILING_TEST)
        self.ok("harness-red", "check", cwd=self.wt2)
        self.assertEqual(read_json(os.path.join(self.canon, "red", self.SLUG2 + ".json"))["branch"], self.BRANCH2)
        self.ok("harness-work", "attempt", "14", "code", "fail", cwd=self.wt2)
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "14.json"))["attempts"]), 1)
        self.ok("harness-pr", "ultra", "35", cwd=self.wt2)
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "ultra", "35.json")))
        lines = self.brief(self.wt2)
        self.assertIn("Ticket #14: Outside ticket (in-progress, tier opus)", lines)
        self.assertTrue(any(line.startswith("Red run: assertion at ") for line in lines), lines)
        self.assertIsNone(self.edit_decision(os.path.join(self.wt2, "src", "app.py"), cwd=self.wt2))
        self.assertEqual(self.state_dirs(), [self.canon_key])

    def test_old_state_moves_to_the_canonical_key(self):
        self.put_ticket(self.old, number=14, title="Outside old ticket",
                        attempts=[{"stage": "tests", "tier": "opus", "result": "pass", "at": "2026-10-12T00:00:00Z"}])
        self.assertIn("Ticket #14: Outside old ticket (in-progress, tier opus)", self.brief(self.wt2))
        self.ok("harness-work", "attempt", "14", "code", "fail", cwd=self.wt2)
        self.assertFalse(os.path.exists(self.old), "the old key's directory is still there")
        self.assertEqual(self.state_dirs(), [self.canon_key])
        attempts = read_json(os.path.join(self.canon, "tickets", "14.json"))["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [("tests", "pass"), ("code", "fail")])


# ================================================================ migration from the old key
@needs_case_insensitive
class Migration(KeyFixture):
    """State under sha1 of the non-canonical spelling is moved once to the canonical key."""

    def setUp(self):
        super().setUp()
        self.spell_worktree()
        # another repository on disk, its state under the key of another spelling: not ours to move
        elsewhere = os.path.join(self.tmp, mixed_name("El"))
        os.makedirs(elsewhere)
        self.unrelated = os.path.join(self.repos, sha12(spelled_below(elsewhere)))
        self.put_ticket(self.unrelated, title="Not ours")

    def assertMoved(self):
        self.assertFalse(os.path.exists(self.old), "the old key's directory is still there")
        self.assertTrue(os.path.isdir(self.canon))
        self.assertEqual(read_json(os.path.join(self.unrelated, "tickets", "12.json"))["title"], "Not ours")
        self.assertEqual(self.state_dirs(), sorted([self.canon_key, os.path.basename(self.unrelated)]))

    def test_harness_work_moves_the_old_cache(self):
        self.put_ticket(self.old, tier="sonnet", branch=BRANCH,
                        attempts=[{"stage": "tests", "tier": "sonnet", "result": "pass", "at": "2026-10-12T00:00:00Z"}])
        self.assertEqual(self.ok("harness-work", "attempt", "12", "code", "fail").stdout.strip(), "retry")
        self.assertMoved()
        cache = read_json(os.path.join(self.canon, "tickets", "12.json"))
        self.assertEqual(cache["branch"], BRANCH)
        self.assertEqual([(a["stage"], a["result"]) for a in cache["attempts"]], [("tests", "pass"), ("code", "fail")])
        # a second run works on the moved state and does not bring the old directory back
        self.assertEqual(self.ok("harness-work", "attempt", "12", "code", "fail").stdout.strip(), "escalate opus")
        self.assertMoved()
        self.assertEqual(len(read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"]), 3)

    def test_harness_red_moves_the_old_state(self):
        self.put_ticket(self.old, title="Old cached title")
        write(os.path.join(self.old, "red", "t-9-older.json"), json.dumps({"branch": "t/9-older", "kind": "import"}))
        write(os.path.join(self.wt, "tests", "test_login.py"), FAILING_TEST)
        self.ok("harness-red", "check")
        self.assertMoved()
        self.assertEqual(read_json(os.path.join(self.canon, "tickets", "12.json"))["title"], "Old cached title")
        self.assertEqual(read_json(os.path.join(self.canon, "red", "t-9-older.json"))["kind"], "import")
        self.assertEqual(read_json(os.path.join(self.canon, "red", SLUG + ".json"))["kind"], "assertion")
        self.ok("harness-red", "check")
        self.assertMoved()
        self.assertEqual(read_json(os.path.join(self.canon, "red", "t-9-older.json"))["kind"], "import")

    def test_harness_board_keeps_the_old_cache_fields(self):
        attempts = [{"stage": "tests", "tier": "opus", "result": "fail", "at": "2026-10-12T00:00:00Z"}]
        self.put_ticket(self.old, title="Stale title", branch=BRANCH, worktree=self.wt,
                        started_at="2026-10-12T00:00:00Z", attempts=attempts)
        self.gh_issue(title="Fresh title")
        self.ok("harness-board", "show", "12")
        self.assertMoved()
        cache = read_json(os.path.join(self.canon, "tickets", "12.json"))
        self.assertEqual((cache["title"], cache["branch"], cache["worktree"], cache["attempts"]),
                         ("Fresh title", BRANCH, self.wt, attempts))

    def test_harness_pr_moves_the_old_state(self):
        self.put_ticket(self.old, title="Old for pr")
        self.ok("harness-pr", "ultra", "34")
        self.assertMoved()
        self.assertEqual(read_json(os.path.join(self.canon, "tickets", "12.json"))["title"], "Old for pr")
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "ultra", "34.json")))

    def test_session_brief_moves_and_shows_the_old_state(self):
        self.put_ticket(self.old, title="Old brief ticket")
        self.put_red(self.old, at="2026-10-03T07:00:00Z")
        lines = self.brief(self.wt)
        self.assertIn("Ticket #12: Old brief ticket (in-progress, tier opus)", lines)
        self.assertIn("Red run: assertion at 2026-10-03T07:00:00Z", lines)
        self.assertMoved()
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "red", SLUG + ".json")))
        self.assertIn("Ticket #12: Old brief ticket (in-progress, tier opus)", self.brief(self.wt))
        self.assertMoved()

    def test_tests_first_moves_and_honours_the_old_red_record(self):
        self.put_ticket(self.old)
        self.put_red(self.old)
        source = os.path.join(self.wt, "src", "app.py")
        self.assertIsNone(self.edit_decision(source))
        self.assertMoved()
        self.assertTrue(os.path.isfile(os.path.join(self.canon, "red", SLUG + ".json")))
        self.assertIsNone(self.edit_decision(source))
        self.assertMoved()

    def test_both_keys_present_leaves_both_and_the_canonical_wins(self):
        old_cache = self.put_ticket(self.old, tier="fable",
                                    attempts=[{"stage": "code", "tier": "fable", "result": "fail", "at": "x"}])
        with open(old_cache, encoding="utf-8") as fh:
            old_text = fh.read()
        self.put_ticket(self.canon, tier="sonnet", attempts=[])
        self.assertEqual(self.ok("harness-work", "attempt", "12", "code", "fail").stdout.strip(), "retry")
        attempts = read_json(os.path.join(self.canon, "tickets", "12.json"))["attempts"]
        self.assertEqual([(a["tier"], a["result"]) for a in attempts], [("sonnet", "fail")])
        with open(old_cache, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), old_text)                  # untouched, not merged
        # the guard reads the canonical record, not the old one
        self.put_red(self.old, kind="assertion")
        self.put_red(self.canon, kind="green")
        self.assertTrue(self.edit_decision(os.path.join(self.wt, "src", "app.py")).startswith("The red run on t/12"))
        self.assertTrue(os.path.isdir(self.old))
        self.assertEqual(read_json(os.path.join(self.old, "red", SLUG + ".json"))["kind"], "assertion")


# ================================================================ the same, with a lowercase name on disk
class CanonicalSpellingKeepsItsKeyLowercaseOnDisk(CanonicalSpellingKeepsItsKey):
    """Runs on every file system: a checkout whose on-disk name is lowercase keeps sha1 of that spelling."""

    def main_name(self):
        return lower_name("rk")

    def test_the_lowercase_name_is_what_git_and_the_parent_list(self):
        self.assertTrue(on_disk(self.main))
        self.assertEqual(self.git(self.wt, "rev-parse", "--git-common-dir"), os.path.join(self.main, ".git"))


class OtherSpellingOneKeyLowercaseOnDisk(OtherSpellingOneKey):
    """A lowercase checkout reached through an uppercase spelling still keys by the lowercase on-disk name."""

    def main_name(self):
        return lower_name("rk")


class MigrationLowercaseOnDisk(Migration):
    """Old state under the uppercase spelling's key moves to the lowercase on-disk name's key."""

    def main_name(self):
        return lower_name("rk")


# ================================================================ harness-digest and harness-standup
class RegisteredPathKey(unittest.TestCase):
    """repo_key(path) in harness-digest and harness-standup, which map run files to registered projects."""

    @classmethod
    def setUpClass(cls):
        cls.tools = {name: load_tool(name) for name in ("harness-digest", "harness-standup")}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="KeyFn-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.outer = os.path.join(self.tmp, mixed_name("Ou"))
        self.proj = os.path.join(self.outer, mixed_name("Rk"))
        self.other = os.path.join(self.outer, mixed_name("Ot"))
        self.lower = os.path.join(self.tmp, lower_name("ou"), lower_name("rk"))     # lowercase on disk
        for path in (self.proj, self.other, self.lower):
            os.makedirs(path)

    def test_on_disk_spelling_symlink_and_missing_path_keep_sha1_of_the_real_path(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.proj, link)
        missing = os.path.join(self.tmp, "Gone", "Away")
        for name, mod in self.tools.items():
            for given, expected in ((self.proj, self.proj), (self.other, self.other), (link, self.proj),
                                    (missing, missing), (self.proj + os.sep, self.proj),
                                    (self.lower, self.lower), (self.lower + os.sep, self.lower)):
                with self.subTest(tool=name, path=given):
                    self.assertEqual(mod.repo_key(given), sha12(expected))

    @needs_case_insensitive
    def test_every_spelling_of_a_path_gives_its_canonical_key(self):
        link = os.path.join(self.tmp, "Link")
        os.symlink(self.proj, link)
        outer, proj, other = (os.path.basename(p) for p in (self.outer, self.proj, self.other))
        lower_outer, lower_proj = self.lower.split(os.sep)[-2:]
        cases = [(p, self.proj) for p in (spelled(self.proj), spelled(self.proj, 2),
                                          os.path.join(self.tmp, swap(outer), proj),
                                          os.path.join(self.tmp, outer.upper(), proj.upper()),
                                          os.path.join(self.tmp, outer.lower(), proj.lower()),
                                          spelled(link), spelled_below(self.proj))]
        cases += [(p, self.other) for p in (spelled(self.other), spelled_below(self.other),
                                            os.path.join(self.tmp, outer.lower(), other.upper()))]
        cases += [(p, self.lower) for p in (spelled(self.lower), spelled(self.lower, 2), spelled_below(self.lower),
                                            os.path.join(self.tmp, lower_outer.upper(), lower_proj))]
        # every component swapped, the system folders too, where this file system resolves that spelling
        for path in (self.proj, self.lower):
            everything = path.swapcase()
            if os.path.exists(everything) and os.path.samefile(everything, path):
                cases.append((everything, path))
        for given, _ in cases:
            self.assertFalse(on_disk(given), given)                  # each case really is another spelling
        for name, mod in self.tools.items():
            for given, canonical in cases:
                with self.subTest(tool=name, path=given):
                    self.assertEqual(mod.repo_key(given), sha12(canonical))
        self.assertNotEqual(sha12(self.proj), sha12(self.other))


@needs_case_insensitive
@unittest.skipUnless(GIT, "git is not installed")
class RunsOfAProjectRegisteredInAnotherSpelling(unittest.TestCase):
    """A run file named by the tools' (canonical) key belongs to the project registered in another spelling."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="KeyRuns-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".claude", "harness")
        self.proj = os.path.join(self.tmp, mixed_name("Ws"))
        self.other = os.path.join(self.tmp, lower_name("ot"))          # lowercase on disk, registered otherwise
        os.makedirs(self.other)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_", "CLAUDE_", "FAKEGH_")) and k != "PYTHONPATH"}
        env.update(HOME=self.home, LANG="en_US.UTF-8", GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1",
                   PATH=os.pathsep.join([W["fakebin"], os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_GH=os.path.join(W["fakebin"], "gh"),
                   FAKEGH_SCENARIO=os.path.join(self.tmp, "scenario.json"),
                   FAKEGH_LOG=os.path.join(self.tmp, "gh.log"),
                   HARNESS_LAUNCHCTL=os.path.join(self.tmp, "no-launchctl"))
        self.env = env
        subprocess.run([GIT, "init", "-q", self.proj], env=env, check=True, capture_output=True)
        write(os.path.join(self.proj, "README.md"), "x\n")
        subprocess.run([GIT, "-C", self.proj, "add", "-A"], env=env, check=True, capture_output=True)
        subprocess.run([GIT, "-C", self.proj, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
                       env=env, check=True, capture_output=True)
        write(os.path.join(self.state, "projects.json"), json.dumps({"version": 1, "projects": [
            {"path": spelled_below(self.proj), "name": "Widget Shop", "billable": False, "client": "", "notes": ""},
            {"path": spelled_below(self.other), "name": "Other Shop", "billable": False, "client": "", "notes": ""}]}))
        # what harness-work writes: id and file named by the key, repo the main tree as git reports it
        for path, ticket, extra in ((self.proj, 12, {"repo": self.proj}), (self.other, 5, {})):
            k = sha12(path)
            record = dict(extra, id=f"{k}-{ticket}", ticket=ticket, venue="headless", outcome="error", exit=2,
                          started_at=z(2 * 3600), ended_at=z(3600), worktree=None, branch=f"t/{ticket}-x")
            write(os.path.join(self.state, "runs", f"{k}-{ticket}-20261013070000.json"), json.dumps(record))

    def add_old_runs(self):
        """Run files written before the change, without a repo field: named by sha1 of the registered (other)
        spelling, the key the tools used then. Plus one of an unregistered repository in another spelling,
        which belongs to no project. Returns that run's key."""
        stray = os.path.join(self.tmp, mixed_name("Zz"))
        os.makedirs(stray)
        for path, ticket in ((spelled_below(self.proj), 13), (spelled_below(self.other), 6), (spelled_below(stray), 7)):
            k = sha12(path)
            record = dict(id=f"{k}-{ticket}", ticket=ticket, venue="headless", outcome="error", exit=2,
                          started_at=z(2 * 3600), ended_at=z(3000), worktree=None, branch=f"t/{ticket}-old")
            write(os.path.join(self.state, "runs", f"{k}-{ticket}-20261012070000.json"), json.dumps(record))
        self.assertNotIn(sha12(spelled_below(self.proj)), (sha12(self.proj), sha12(self.other)))
        return sha12(spelled_below(stray))

    def expected_with_old_runs(self, stray_key):
        return sorted([("Other Shop", 5), ("Other Shop", 6), ("Widget Shop", 12), ("Widget Shop", 13),
                       (stray_key, 7)])

    def test_standup_names_the_project_of_runs_under_the_old_key(self):
        stray_key = self.add_old_runs()
        runs = self.run_tool("harness-standup", "--json")["runs"]
        self.assertEqual(sorted((r["project"], r["ticket"]) for r in runs), self.expected_with_old_runs(stray_key))

    def test_digest_names_the_project_of_runs_under_the_old_key(self):
        stray_key = self.add_old_runs()
        runs = self.run_tool("harness-digest", "--json")["runs"]
        self.assertEqual(sorted((r["project"], r["ticket"]) for r in runs), self.expected_with_old_runs(stray_key))

    def run_tool(self, name, *args):
        p = subprocess.run([sys.executable, os.path.join(BIN, name), *args], cwd=self.tmp, env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def test_standup_names_the_project(self):
        runs = self.run_tool("harness-standup", "--json")["runs"]
        self.assertEqual(sorted((r["project"], r["ticket"]) for r in runs), [("Other Shop", 5), ("Widget Shop", 12)])

    def test_digest_names_the_project(self):
        runs = self.run_tool("harness-digest", "--json")["runs"]
        self.assertEqual(sorted((r["project"], r["ticket"]) for r in runs), [("Other Shop", 5), ("Widget Shop", 12)])


if __name__ == "__main__":
    unittest.main()
