"""Tests for `harness-red reprove` (ticket #275): the tool-kept recovery when a fix round's tests stale the red record.

    python3 -m unittest discover -s plugins/harness/tests -p 'test_red_reprove.py' -v

A fix round that adds coverage tests for behaviour the branch already implements commits test changes that pass
at HEAD: the red record goes stale, tests_first refuses every later source edit, `check` refuses (source already
changed) and `check --base HEAD` sees nothing committed, or answers green. `reprove` re-proves the branch's test
files as they are now against main's code at the merge base, in a temporary detached worktree outside the ticket
worktree, and writes the side file and the record exactly as `check` does; a run that passes there, or proves
nothing, is refused and the old record is kept byte for byte.

#309: a branch brought home from a cloud session (no harness hooks there) carries its tests and its source in the
same commits and has no red record, so tests_first refuses every source edit, a merge-conflict resolution included,
and `check` refuses (source already changed). `reprove` no longer needs an earlier record: red at the merge base
writes the record and the side file as above; green, or a run that proves nothing, leaves no record and no side
file. A corrupt record does not block it either: a red run replaces it.

Every case builds, with real git under a temporary folder, a bare "origin", a clone with `main` (calc.py, whose
`add` is not built yet), and the branch t/12-slug in a linked worktree at <clone>/.claude/worktrees/t-12-slug.
HOME and TMPDIR are temporary, so records and temporary worktrees land under them. The test command is
`python3 -m unittest {files}` (from the clone's .claude/settings.json), with a `python3` on PATH that runs the
interpreter running these tests. tests_first.py is fed the PreToolUse payload Claude Code sends, `agent_type`
naming the coder. Expected hashes, side files and merge bases are computed here from the files and git, never by
the tool.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(PLUGIN, "bin", "harness-red")
HOOK = os.path.join(PLUGIN, "hooks", "tests_first.py")
DESIGN = os.path.join(os.path.dirname(os.path.dirname(PLUGIN)), "docs", "design.md")
WORK_SKILL = os.path.join(PLUGIN, "skills", "work", "SKILL.md")
GIT = shutil.which("git")
BRANCH, SLUG = "t/12-slug", "t-12-slug"
RECORD_KEYS = {"branch", "base_sha", "test_files", "run_files", "test_hash", "cmd", "exit", "kind", "tail", "at"}
GREEN = "Tests pass before the implementation; they do not test this change."
NO_RECORD = f"No red run recorded for {BRANCH}; run harness-red check."
NO_TEST_CHANGES = "No test changes on this branch; write the tests first."
# tests_first's denial of a source edit while the record is stale (#275 adds the reprove path to it).
CHANGED = ("Tests changed since the red run; run harness-red check again, or harness-red reprove once the "
           "branch has source changes.")
OLD_CHANGED = "Tests changed since the red run; run harness-red check again."

CALC = "def add(a, b):\n    return None\n\n\ndef sub(a, b):\n    return a - b\n"      # main's: add is not built
CALC_BUILT = CALC.replace("return None", "return a + b")                              # the coder's
TEST_ADD = ("import unittest\n\nimport calc\n\n\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
# Coverage a fix round adds for behaviour the branch already has: it passes at HEAD, fails on main's code.
TEST_ADD_COVERED = TEST_ADD + "\n    def test_add_zero(self):\n        self.assertEqual(calc.add(0, 9), 9)\n"
TEST_MORE = ("import unittest\n\nimport calc\n\n\nclass MoreTest(unittest.TestCase):\n"
             "    def test_add_more(self):\n        self.assertEqual(calc.add(7, 8), 15)\n")
# Passes on main's code too: sub is there.
TEST_SUB = ("import unittest\n\nimport calc\n\n\nclass SubTest(unittest.TestCase):\n"
            "    def test_sub(self):\n        self.assertEqual(calc.sub(5, 3), 2)\n")
TEST_HANG = ("import time\nimport unittest\n\n\nclass HangTest(unittest.TestCase):\n"
             "    def test_hang(self):\n        time.sleep(60)\n")
TEST_SYNTAX = "import unittest\n\nimport calc\n\n\ndef test_add(:\n    assert calc.add(2, 3) == 5\n"
# #309: passes against main's code only (add is not built there), so it fails at HEAD once add is built.
TEST_ADD_UNBUILT = ("import unittest\n\nimport calc\n\n\nclass UnbuiltTest(unittest.TestCase):\n"
                    "    def test_add_is_not_built(self):\n        self.assertIsNone(calc.add(2, 3))\n")
# Main's later edit of add's own line: a branch that built add conflicts with it in calc.py.
CALC_MAIN_EDIT = CALC.replace("    return None\n", '    """The sum of a and b."""\n    return None  # not built\n')
CALC_RESOLVED = CALC.replace("    return None\n", '    """The sum of a and b."""\n    return a + b\n')
# tests_first's denial of a source edit on a ticket branch with no red record (its cache present).
NO_RED_HOOK = "Tests first on t/{n}: write the tests, then run harness-red check. (No red run recorded.)"
TESTS_FIXED = ("Tests are fixed for this ticket. If a test is wrong, stop and report it; the orchestrator sends it "
               "back to the test-author.")

# harness-red reads some phrases in a run's output as a run that proves nothing (bin/harness-red: NO_TESTS_RE,
# COLLECTED_NONE_RE, BAD_SYNTAX_RE, LOAD_ERROR_RE), and `harness-red check` runs this module before reprove exists.
# So its red-time output must never carry them: the texts holding them are built from pieces (a traceback prints
# source lines), assertions on them name the case instead of echoing them, and any output put into a failure
# message goes through quiet().
EMPTY_RUN = "no tests" + " ran"
PYTEST_NONE = "collected 0" + " items\n\n=================== " + EMPTY_RUN + " in 0.00s ===================\n"
QUIET = re.compile(r"(?i)no\s+tests\s+ran|collected\s+0\s+items|error\s+during\s+collection|error\s+collecting|"
                   r"(?:Syntax|Indentation|Tab)Error")


def quiet(text):
    """`text` with every phrase harness-red reads as an empty or unloaded run masked, for failure messages."""
    return QUIET.sub("<masked>", str(text))

# A test command that reports where and on what it ran, then fails as an assertion would.
PROBE = '''import json, os, subprocess, sys


def git(*args):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


files = {}
for name in json.loads(os.environ["PROBE_NAMES"]):
    try:
        with open(name, encoding="utf-8") as fh:
            files[name] = fh.read()
    except FileNotFoundError:
        files[name] = None
seen = {"cwd": os.getcwd(), "argv": sys.argv[1:], "head": git("rev-parse", "HEAD")[1],
        "top": git("rev-parse", "--show-toplevel")[1], "on_branch": git("symbolic-ref", "-q", "HEAD")[0] == 0,
        "files": files}
with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as fh:
    json.dump(seen, fh)
print("AssertionError: the probe ran at " + seen["head"])
sys.exit(1)
'''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def bytes_of(path):
    """The file's bytes, or None when it does not exist."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def expected_hash(root, files):
    h = hashlib.sha256()
    for f in sorted(files):
        with open(os.path.join(root, f), "rb") as fh:
            h.update(f.encode("utf-8") + b"\n" + fh.read())
    return h.hexdigest()


def expected_side_file(root, files):
    """The side file check writes for these files: {"test_hash", "files": {path: base64 of the bytes}}."""
    encoded = {}
    for f in sorted(files):
        with open(os.path.join(root, f), "rb") as fh:
            encoded[f] = base64.b64encode(fh.read()).decode("ascii")
    return {"test_hash": expected_hash(root, files), "files": encoded}


def tree(root):
    """({relative path: bytes} of every file under `root`, sorted directories), without a linked worktree's .git."""
    files, dirs = {}, []
    for here, subdirs, names in os.walk(root):
        rel = os.path.relpath(here, root)
        if rel == ".":
            subdirs[:] = [d for d in subdirs if d != ".git"]
            names = [n for n in names if n != ".git"]
        dirs.extend(os.path.normpath(os.path.join(rel, d)) for d in subdirs)
        for name in names:
            with open(os.path.join(here, name), "rb") as fh:
                files[os.path.normpath(os.path.join(rel, name))] = fh.read()
    return files, sorted(dirs)


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


@unittest.skipUnless(GIT, "git is not installed")
class Fixture(unittest.TestCase):
    """The repository, the ticket worktree and the helpers; no tests of its own."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-reprove-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        temp = os.path.join(self.tmp, "tmp")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.home)
        os.makedirs(temp)
        python3 = write(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(python3, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_", "CLAUDE_")) and k not in ("PYTHONPATH",)}
        env.update(HOME=self.home, TMPDIR=temp,
                   PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        self.env = env

        origin, self.repo = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "repo")
        self.git(self.tmp, "init", "--bare", "-q", origin)
        self.git(self.tmp, "clone", "-q", origin, self.repo)
        self.git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        write(os.path.join(self.repo, "calc.py"), CALC)
        write(os.path.join(self.repo, "README.md"), "# calc\n")
        write(os.path.join(self.repo, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_TEST_CMD": "python3 -m unittest {files}"}}))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "calc")
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".claude/worktrees/\n")
        self.wt = os.path.join(self.repo, ".claude", "worktrees", SLUG)
        self.git(self.repo, "worktree", "add", "-q", self.wt, "-b", BRANCH, "origin/main")

        key = hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12]
        self.state = os.path.join(self.home, ".claude", "harness", "repos", key)
        self.record_path = os.path.join(self.state, "red", SLUG + ".json")
        self.side_path = os.path.join(self.state, "red-files", SLUG + ".json")
        self.scratch = os.path.join(self.state, "scratch", SLUG)

    # ------------------------------------------------------------ helpers
    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", cwd, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                               *args], env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def red(self, *args, cwd=None, **extra):
        return subprocess.run([sys.executable, TOOL, *args], cwd=cwd or self.wt, env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=120)

    def put(self, rel, text, root=None):
        return write(os.path.join(root or self.wt, rel), text)

    def commit(self, message, root=None):
        self.git(root or self.wt, "add", "-A")
        self.git(root or self.wt, "commit", "-q", "-m", message)

    def record(self):
        with open(self.record_path, encoding="utf-8") as fh:
            return json.load(fh)

    def kept(self):
        """The record's and the side file's bytes (None for a missing one)."""
        return bytes_of(self.record_path), bytes_of(self.side_path)

    def head(self):
        return self.git(self.wt, "rev-parse", "HEAD")

    def merge_base(self, ref="origin/main"):
        return self.git(self.wt, "merge-base", "HEAD", ref)

    def worktrees(self):
        return self.git(self.repo, "worktree", "list", "--porcelain")

    def status_line(self):
        p = self.red("status")
        self.assertEqual(p.returncode, 0, quiet(p.stdout + p.stderr))
        return p.stdout.splitlines()[-1]

    def assertOneLineError(self, p, *needles):
        said = quiet(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 1, said)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, said)
        self.assertFalse("Traceback" in p.stderr, said)
        for needle in needles:
            self.assertTrue(needle in p.stderr, f"stderr lacks {quiet(needle)!r}: {quiet(p.stderr)}")

    def assertRed(self, p, n_files):
        said = quiet(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, said)
        lines = p.stdout.splitlines()
        self.assertTrue(lines and lines[0] == f"red (assertion): {n_files} test files, exit 1",
                        f"not red (assertion) with {n_files} test files: {said}")

    def assertInText(self, needle, text, what):
        """`needle` is in `text` (a run's tail or output), said without echoing a trigger phrase."""
        self.assertTrue(needle in text, f"{what} lacks {quiet(needle)!r}: {quiet(text)}")

    def cache(self, number=12, flags=()):
        write(os.path.join(self.state, "tickets", f"{number}.json"),
              json.dumps({"number": number, "title": "A ticket", "type": "task", "flags": list(flags),
                          "labels": ["task", "status:in-progress"] + list(flags)}))

    def hook(self, rel, agent="harness:coder", root=None):
        """tests_first.py's deny reason for an Edit of `rel` in the ticket worktree, or None when it allows."""
        root = root or self.wt
        data = {"session_id": "sess-1", "transcript_path": "/dev/null", "cwd": root,
                "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": os.path.join(root, rel), "old_string": "a", "new_string": "b"},
                "tool_use_id": "toolu_01"}
        if agent is not None:                                   # present only inside a subagent
            data.update(agent_id="agent-1", agent_type=agent)
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(data), capture_output=True, text=True,
                           env=self.env, cwd=self.tmp, timeout=60)
        self.assertEqual((p.returncode, quiet(p.stderr)), (0, ""), quiet(p.stdout))
        if not p.stdout.strip():
            return None
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
        return out["permissionDecisionReason"]

    def unittest_exit(self, root, *files):
        """The exit status of `python3 -m unittest files` in `root`, as the test command runs them."""
        return subprocess.run([sys.executable, "-m", "unittest", *files], cwd=root, env=self.env,
                              capture_output=True, text=True, timeout=120).returncode

    def on_main(self, changes, message="main moves on"):
        """Commit literal texts on main in the clone and push them to origin."""
        for rel, text in changes.items():
            write(os.path.join(self.repo, rel), text)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", message)
        self.git(self.repo, "push", "-q", "origin", "main")

    def marker_env(self):
        """(environment for a test command that only leaves a marker file, the marker's path)."""
        marker = os.path.join(self.tmp, "ran.marker")
        script = write(os.path.join(self.tmp, "mark.py"), f"open({marker!r}, 'w').close()\nprint('AssertionError')\n"
                                                          "raise SystemExit(1)\n")
        return {"HARNESS_TEST_CMD": f"python3 {shlex.quote(script)} {{files}}"}, marker

    def fake_runner(self, output, code):
        """Environment for a test command that prints `output` and exits `code` (outside the worktree)."""
        script = write(os.path.join(self.tmp, "runner.py"),
                       "import os, sys\nsys.stdout.write(os.environ['RUNNER_OUTPUT'])\n"
                       "sys.exit(int(os.environ['RUNNER_EXIT']))\n")
        return {"HARNESS_TEST_CMD": f"python3 {shlex.quote(script)} {{files}}", "RUNNER_OUTPUT": output,
                "RUNNER_EXIT": str(code)}

    def red_ticket_with_source(self):
        """The usual ticket so far: the test-author's commit, a red `check`, the coder's commit that makes it pass."""
        self.cache()
        self.put("tests/test_calc.py", TEST_ADD)
        self.commit("tests for add")
        p = self.red("check")
        self.assertRed(p, 1)
        self.put("calc.py", CALC_BUILT)
        self.commit("add")
        self.assertEqual(self.status_line(), "current: yes")

    def fix_round(self):
        """A fix round's test-only commit of coverage that passes at HEAD: a test added to the recorded file
        (which stales the record) and a new test file."""
        self.put("tests/test_calc.py", TEST_ADD_COVERED)
        self.put("tests/test_more.py", TEST_MORE)
        self.commit("coverage for add")

    def snapshot(self):
        """Everything reprove must leave alone in the ticket worktree and the repository."""
        return {
            "head": self.head(),
            "branch": self.git(self.wt, "symbolic-ref", "HEAD"),
            "status": self.git(self.wt, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"),
            "index": self.git(self.wt, "ls-files", "--stage"),
            "staged": self.git(self.wt, "diff", "--cached"),
            "tree": tree(self.wt),
            "main head": self.git(self.repo, "rev-parse", "HEAD"),
            "main status": self.git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "worktrees": self.worktrees(),
            "refs": self.git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            "stash": self.git(self.repo, "stash", "list"),
        }

    def cloud_commit(self, files, root=None, message="tests and source, from a cloud session"):
        """What a cloud session pushes (#309): the tests and the source in one commit, no harness-red run, no
        record."""
        for rel, text in files.items():
            self.put(rel, text, root=root)
        self.commit(message, root=root)

    def ticket_worktree(self, number, slug):
        """(worktree, record path, side file path) of a further ticket branch t/<number>-<slug> off origin/main,
        with its ticket cache."""
        self.cache(number)
        wt = os.path.join(self.repo, ".claude", "worktrees", f"t-{number}-{slug}")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", f"t/{number}-{slug}", "origin/main")
        return (wt, os.path.join(self.state, "red", f"t-{number}-{slug}.json"),
                os.path.join(self.state, "red-files", f"t-{number}-{slug}.json"))

    def assertNoRecordAnywhere(self):
        """No red record and no side file under any name: nothing tests_first could accept."""
        for folder in ("red", "red-files"):
            path = os.path.join(self.state, folder)
            left = [n for n in os.listdir(path) if not n.startswith(".")] if os.path.isdir(path) else []
            self.assertEqual(left, [], f"{folder}/ holds {left}")


class ReproveTest(Fixture):

    # ------------------------------------------------------------ the headline: PR #274's shape
    def test_a_fix_round_commit_of_tests_that_pass_at_head_is_reproved_and_unlocks_source(self):
        self.red_ticket_with_source()
        self.assertIsNone(self.hook("calc.py"))
        self.fix_round()
        # The premise: the new tests pass at HEAD, so no red run of them at HEAD is possible...
        self.assertEqual(self.unittest_exit(self.wt, "tests/test_calc.py", "tests/test_more.py"), 0)
        # ...the record is stale and every source edit is refused...
        self.assertEqual(self.status_line(), "current: no")
        for agent in ("harness:coder", None):
            with self.subTest(agent=agent):
                self.assertEqual(self.hook("calc.py", agent), CHANGED)
        # ...and check cannot help: the branch has source, and --base HEAD sees nothing uncommitted.
        self.assertOneLineError(self.red("check"), "Source already changed before the red run: calc.py.")
        self.assertOneLineError(self.red("check", "--base", "HEAD"), NO_TEST_CHANGES)

        base = self.merge_base()
        p = self.red("reprove")
        self.assertRed(p, 2)
        files = ["tests/test_calc.py", "tests/test_more.py"]
        rec = self.record()
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["branch"], BRANCH)
        self.assertEqual(rec["base_sha"], base)
        self.assertEqual(rec["test_files"], files)
        self.assertEqual(rec["run_files"], files)
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, files))
        self.assertEqual(rec["cmd"], "python3 -m unittest tests/test_calc.py tests/test_more.py")
        self.assertEqual((rec["exit"], rec["kind"]), (1, "assertion"))
        for failure in ("None != 5", "None != 9", "None != 15"):    # main's add: every one of them fails there
            self.assertInText(failure, rec["tail"], "the tail")
        self.assertLessEqual(len(rec["tail"].splitlines()), 40)
        self.assertTrue(rec["tail"] in p.stdout, "the tail is not printed after the red line")
        self.assertIsNotNone(dt.datetime.fromisoformat(rec["at"]).tzinfo)
        self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, files))

        self.assertEqual(self.status_line(), "current: yes")
        for agent in ("harness:coder", None):
            with self.subTest(agent=agent, after="reprove"):
                self.assertIsNone(self.hook("calc.py", agent))
        self.assertIsNone(self.hook("util.py"))                    # a new source file too
        self.assertEqual(self.hook("tests/test_more.py"),           # the role rules still hold
                         "Tests are fixed for this ticket. If a test is wrong, stop and report it; the "
                         "orchestrator sends it back to the test-author.")

    def test_uncommitted_coverage_tests_after_a_green_check_at_head_are_reproved(self):
        # The revise step: the owner asks for a test of behaviour the branch already has; the test-author leaves
        # it uncommitted, and `check --base HEAD` answers green and records it, which refuses every source edit.
        self.red_ticket_with_source()
        self.put("tests/test_more.py", TEST_MORE)
        self.assertOneLineError(self.red("check", "--base", "HEAD"), GREEN)
        self.assertEqual((self.record()["kind"], self.record()["test_files"]), ("green", ["tests/test_more.py"]))
        self.assertIn("passed before any source change", self.hook("calc.py") or "")

        base = self.merge_base()
        self.assertNotEqual(base, self.head())
        p = self.red("reprove", "--worktree", self.wt, cwd=self.tmp)
        self.assertRed(p, 2)
        files = ["tests/test_calc.py", "tests/test_more.py"]       # the committed test file and the new one
        rec = self.record()
        self.assertEqual(rec["base_sha"], base)
        self.assertEqual((rec["test_files"], rec["run_files"]), (files, files))
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, files))
        self.assertEqual(rec["kind"], "assertion")
        self.assertInText("None != 5", rec["tail"], "the tail")
        self.assertInText("None != 15", rec["tail"], "the tail")
        self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, files))
        self.assertEqual(self.status_line(), "current: yes")
        self.assertIsNone(self.hook("calc.py"))
        self.assertEqual(self.git(self.wt, "status", "--porcelain"), "?? tests/test_more.py")   # still uncommitted

    # ------------------------------------------------------------ #309: a branch brought home from a cloud session
    def test_a_branch_brought_home_with_tests_and_source_and_no_record_is_reproved_and_unlocks_source(self):
        self.cache()
        self.cloud_commit({"tests/test_calc.py": TEST_ADD, "calc.py": CALC_BUILT})
        self.assertEqual(self.git(self.wt, "show", "--name-only", "--format=", "HEAD").split(),
                         ["calc.py", "tests/test_calc.py"])                 # one commit holds both
        branch_point = self.merge_base()
        # Main edits add's own line meanwhile, so merging main conflicts in calc.py: a source file.
        self.on_main({"calc.py": CALC_MAIN_EDIT})
        merge = subprocess.run([GIT, "-C", self.wt, "merge", "--no-edit", "origin/main"], env=self.env,
                               capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0, "the merge of main must conflict")
        self.assertEqual(self.git(self.wt, "diff", "--name-only", "--diff-filter=U"), "calc.py")
        # The premise: no record, so the resolution cannot be written; check refuses, status has nothing.
        self.assertEqual(self.kept(), (None, None))
        for agent in ("harness:coder", None):
            with self.subTest(agent=agent, before="reprove"):
                self.assertEqual(self.hook("calc.py", agent), NO_RED_HOOK.format(n=12))
        self.assertOneLineError(self.red("check"), "Source already changed before the red run: calc.py.")
        self.assertOneLineError(self.red("status"), NO_RECORD)

        p = self.red("reprove")
        self.assertRed(p, 1)
        files = ["tests/test_calc.py"]
        rec = self.record()
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["branch"], BRANCH)
        self.assertEqual(rec["base_sha"], branch_point)                     # the merge base, mid-merge
        self.assertNotEqual(rec["base_sha"], self.git(self.repo, "rev-parse", "origin/main"))
        self.assertEqual((rec["test_files"], rec["run_files"]), (files, files))
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, files))
        self.assertEqual(rec["cmd"], "python3 -m unittest tests/test_calc.py")
        self.assertEqual((rec["exit"], rec["kind"]), (1, "assertion"))
        self.assertInText("None != 5", rec["tail"], "the tail")              # main's add, not the branch's
        self.assertTrue(rec["tail"] in p.stdout, "the tail is not printed after the red line")
        self.assertIsNotNone(dt.datetime.fromisoformat(rec["at"]).tzinfo)
        self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, files))
        self.assertEqual(self.status_line(), "current: yes")

        # The conflict resolution in source is allowed now, for the coder and the main session alike...
        for agent in ("harness:coder", None):
            with self.subTest(agent=agent, after="reprove"):
                self.assertIsNone(self.hook("calc.py", agent))
        self.put("calc.py", CALC_RESOLVED)
        self.git(self.wt, "add", "calc.py")
        self.git(self.wt, "commit", "-q", "--no-edit")
        self.assertEqual(len(self.git(self.wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual(self.unittest_exit(self.wt, "tests/test_calc.py"), 0)
        # ...and so is a later fix, while the role rules still hold.
        self.assertEqual(self.status_line(), "current: yes")
        self.assertIsNone(self.hook("calc.py"))
        self.assertIsNone(self.hook("util.py"))
        self.assertEqual(self.hook("tests/test_calc.py"), TESTS_FIXED)

    def test_branches_brought_home_in_other_shapes_get_their_own_red_record(self):
        # Each its own ticket branch, worktree and record: (name, ticket, the cloud commit, main's move, merged?,
        # test files, run files, failures in the tail, failures that must not be there).
        shapes = [
            ("two modules, a fixture and new source in one commit", 21,
             {"tests/test_calc.py": TEST_ADD_COVERED, "tests/test_more.py": TEST_MORE,
              "tests/fixtures/data.json": '{"n": 2}\n', "calc.py": CALC_BUILT, "util.py": "HELPER = 1\n"},
             None, False,
             ["tests/fixtures/data.json", "tests/test_calc.py", "tests/test_more.py"],
             ["tests/test_calc.py", "tests/test_more.py"], ["None != 5", "None != 9", "None != 15"], []),
            ("main merged in since", 22,
             {"tests/test_more.py": TEST_MORE, "calc.py": CALC_BUILT},
             {"mathx.py": "def double(x):\n    return 2 * x\n"}, True,
             ["tests/test_more.py"], ["tests/test_more.py"], ["None != 15"], ["None != 5"]),
            ("main moved on, not merged", 23,
             {"tests/test_calc.py": TEST_ADD, "calc.py": CALC_BUILT},
             {"README.md": "# calc, moved on\n"}, False,
             ["tests/test_calc.py"], ["tests/test_calc.py"], ["None != 5"], ["None != 15"]),
        ]
        for name, number, commit, main_move, merged, files, run_files, failures, absent in shapes:
            with self.subTest(name):
                wt, record_path, side_path = self.ticket_worktree(number, "cloud")
                self.cloud_commit(commit, root=wt)
                if main_move:
                    self.on_main(main_move)
                if merged:
                    self.git(wt, "merge", "-q", "--no-edit", "origin/main")
                base = self.git(wt, "merge-base", "HEAD", "origin/main")
                tip = self.git(self.repo, "rev-parse", "origin/main")
                self.assertEqual(base == tip, merged or not main_move, "the shape's merge base")
                self.assertFalse(os.path.exists(record_path) or os.path.exists(side_path))
                self.assertEqual(self.hook("calc.py", root=wt), NO_RED_HOOK.format(n=number))

                p = self.red("reprove", cwd=wt)
                self.assertRed(p, len(files))
                with open(record_path, encoding="utf-8") as fh:
                    rec = json.load(fh)
                self.assertEqual(set(rec), RECORD_KEYS)
                self.assertEqual(rec["branch"], f"t/{number}-cloud")
                self.assertEqual(rec["base_sha"], base)
                self.assertEqual((rec["test_files"], rec["run_files"]), (files, run_files))
                self.assertEqual(rec["test_hash"], expected_hash(wt, files))
                self.assertEqual(rec["cmd"], "python3 -m unittest " + " ".join(run_files))
                self.assertEqual((rec["exit"], rec["kind"]), (1, "assertion"))
                for failure in failures:
                    self.assertInText(failure, rec["tail"], "the tail")
                for failure in absent:
                    self.assertFalse(failure in rec["tail"], quiet(rec["tail"]))
                self.assertEqual(json.loads(read(side_path)), expected_side_file(wt, files))
                status = self.red("status", cwd=wt)
                self.assertEqual(status.returncode, 0, quiet(status.stdout + status.stderr))
                self.assertEqual(status.stdout.splitlines()[-1], "current: yes")
                self.assertIsNone(self.hook("calc.py", root=wt))
                self.assertIsNone(self.hook("util.py", root=wt))
        self.assertEqual(self.kept(), (None, None))                # t/12-slug was never touched

    def test_a_corrupt_record_does_not_block_reprove_and_a_red_run_replaces_it(self):
        self.cache()
        self.cloud_commit({"tests/test_calc.py": TEST_ADD, "calc.py": CALC_BUILT})
        files = ["tests/test_calc.py"]
        corrupt = [
            ("not JSON", "{not json\n"),
            ("no test_files", json.dumps({"branch": BRANCH, "kind": "assertion", "exit": 1}) + "\n"),
            ("empty test_files", json.dumps({"branch": BRANCH, "test_files": []}) + "\n"),
            ("test_files not a list", json.dumps({"branch": BRANCH, "test_files": "tests/test_calc.py"}) + "\n"),
        ]
        for name, text in corrupt:
            with self.subTest(name):
                write(self.record_path, text)
                if os.path.exists(self.side_path):
                    os.remove(self.side_path)
                self.assertIsNotNone(self.hook("calc.py"))           # the corrupt record allows nothing
                p = self.red("reprove")
                self.assertRed(p, 1)
                rec = self.record()
                self.assertEqual(set(rec), RECORD_KEYS)
                self.assertEqual(rec["base_sha"], self.merge_base())
                self.assertEqual((rec["test_files"], rec["kind"]), (files, "assertion"))
                self.assertEqual(rec["test_hash"], expected_hash(self.wt, files))
                self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, files))
                self.assertEqual(self.status_line(), "current: yes")
                self.assertIsNone(self.hook("calc.py"))

    # ------------------------------------------------------------ where the run happens
    def test_the_run_is_at_the_merge_base_with_the_test_files_as_they_are_now(self):
        # Main has an older test module and a fixture; the test-author deletes the module, edits the fixture and
        # adds a test; the coder builds add and a new source file.
        self.on_main({"tests/test_old.py": TEST_SUB, "tests/fixtures/data.json": '{"n": 1}\n'})
        self.git(self.wt, "merge", "-q", "--ff-only", "origin/main")
        os.remove(os.path.join(self.wt, "tests", "test_old.py"))
        self.put("tests/fixtures/data.json", '{"n": 2}\n')
        self.put("tests/test_calc.py", TEST_ADD)
        self.commit("tests for add")
        self.assertRed(self.red("check"), 2)
        self.put("calc.py", CALC_BUILT)
        self.put("util.py", "HELPER = 1\n")
        self.commit("add")
        # The fix round, not committed: the test module edited, a new one, the fixture edited again.
        self.put("tests/test_calc.py", TEST_ADD_COVERED)
        self.put("tests/test_more.py", TEST_MORE)
        self.put("tests/fixtures/data.json", '{"n": 3}\n')
        base = self.merge_base()
        self.assertEqual(base, self.git(self.repo, "rev-parse", "origin/main"))
        worktrees = self.worktrees()

        probe = write(os.path.join(self.tmp, "probe.py"), PROBE)
        out = os.path.join(self.tmp, "probe.json")
        names = ["calc.py", "util.py", "tests/test_calc.py", "tests/test_more.py", "tests/fixtures/data.json"]
        p = self.red("reprove", HARNESS_TEST_CMD=f"python3 {shlex.quote(probe)} {{files}}", PROBE_OUT=out,
                     PROBE_NAMES=json.dumps(names))
        self.assertRed(p, 3)
        seen = json.loads(read(out))
        cwd = os.path.realpath(seen["cwd"])
        self.assertFalse(inside(cwd, self.wt), cwd)                 # never in the ticket worktree
        self.assertNotEqual(cwd, os.path.realpath(self.repo))
        self.assertEqual(os.path.realpath(seen["top"]), cwd)        # the top of a worktree of its own
        self.assertEqual(seen["head"], base)                         # main's code at the merge base
        self.assertFalse(seen["on_branch"], "the temporary worktree is detached")
        self.assertEqual(seen["argv"], ["tests/test_calc.py", "tests/test_more.py"])   # runnable files only
        self.assertEqual(seen["files"], {
            "calc.py": CALC,                                         # main's, not the coder's
            "util.py": None,                                         # the branch's source is not there
            "tests/test_calc.py": TEST_ADD_COVERED,                  # the test files' bytes on disk now
            "tests/test_more.py": TEST_MORE,
            "tests/fixtures/data.json": '{"n": 3}\n',
        })
        self.assertFalse(os.path.exists(seen["cwd"]), "the temporary worktree was not removed")
        self.assertEqual(self.worktrees(), worktrees)

        files = ["tests/fixtures/data.json", "tests/test_calc.py", "tests/test_more.py"]   # the deleted one is not
        rec = self.record()
        self.assertEqual(rec["base_sha"], base)
        self.assertEqual(rec["test_files"], files)
        self.assertEqual(rec["run_files"], ["tests/test_calc.py", "tests/test_more.py"])
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, files))
        self.assertEqual(rec["cmd"], f"python3 {shlex.quote(probe)} tests/test_calc.py tests/test_more.py")
        self.assertInText("AssertionError: the probe ran at " + base, rec["tail"], "the tail")
        self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, files))
        self.assertEqual(self.status_line(), "current: yes")

    def test_the_merge_base_is_the_one_now_not_the_old_records(self):
        # Main moves on after the red run (a new module) and the branch merges it; the fix round's test uses the
        # new module. At the merge base it imports and fails by assertion; at the old record's base_sha it would
        # not even import.
        self.red_ticket_with_source()
        old_base = self.record()["base_sha"]
        self.on_main({"mathx.py": "def double(x):\n    return 2 * x\n"})
        self.git(self.wt, "merge", "-q", "--no-edit", "origin/main")
        self.put("tests/test_calc.py", TEST_ADD.replace("import calc\n", "import calc\nimport mathx\n")
                 + "\n    def test_double_of_a_difference(self):\n"
                   "        self.assertEqual(mathx.double(calc.sub(5, 3)), 4)\n")
        self.commit("coverage for double")
        self.assertEqual(self.unittest_exit(self.wt, "tests/test_calc.py"), 0)
        self.assertEqual(self.status_line(), "current: no")
        base = self.merge_base()
        self.assertEqual(base, self.git(self.repo, "rev-parse", "origin/main"))
        self.assertNotEqual(base, old_base)

        p = self.red("reprove", "--json")
        self.assertEqual(p.returncode, 0, quiet(p.stdout + p.stderr))
        rec = self.record()
        self.assertTrue(json.loads(p.stdout) == rec, f"--json does not print the record: {quiet(p.stdout)}")
        self.assertEqual(rec["base_sha"], base)
        self.assertEqual(rec["kind"], "assertion")
        self.assertFalse("No module named" in rec["tail"], f"the run could not import: {quiet(rec['tail'])}")
        self.assertInText("None != 5", rec["tail"], "the tail")
        self.assertEqual(rec["test_files"], ["tests/test_calc.py"])
        self.assertEqual(self.status_line(), "current: yes")
        self.assertIsNone(self.hook("calc.py"))

    def test_test_files_follow_the_test_patterns(self):
        # Under HARNESS_TEST_PATTERNS=specs/** a file in tests/ is source: not proven, and no refusal either.
        self.env["HARNESS_TEST_PATTERNS"] = "specs/**"
        self.cache()
        self.put("specs/test_calc.py", TEST_ADD)
        self.commit("tests for add")
        self.assertRed(self.red("check"), 1)
        self.put("calc.py", CALC_BUILT)
        self.put("tests/test_extra.py", TEST_MORE)
        self.commit("add")
        self.put("specs/test_calc.py", TEST_ADD_COVERED)
        self.commit("coverage for add")
        self.assertEqual(self.status_line(), "current: no")
        p = self.red("reprove")
        self.assertRed(p, 1)
        rec = self.record()
        self.assertEqual((rec["test_files"], rec["run_files"]), (["specs/test_calc.py"], ["specs/test_calc.py"]))
        self.assertEqual(rec["cmd"], "python3 -m unittest specs/test_calc.py")
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, ["specs/test_calc.py"]))
        self.assertInText("None != 9", rec["tail"], "the tail")
        self.assertFalse("None != 15" in rec["tail"], quiet(rec["tail"]))   # tests/test_extra.py did not run
        self.assertEqual(self.status_line(), "current: yes")

    def test_the_base_option_names_the_ref_and_falls_back_to_main(self):
        self.red_ticket_with_source()
        author_commit = self.git(self.wt, "rev-parse", "HEAD~1")    # the test-author's commit
        self.fix_round()
        kept = self.kept()
        self.assertOneLineError(self.red("reprove", "--base", "nope"), "unknown base 'nope'")
        self.assertEqual(self.kept(), kept)

        self.git(self.wt, "branch", "stack", author_commit)        # a stacked parent: the merge base moves to it
        p = self.red("reprove", "--base", "stack")
        self.assertRed(p, 2)
        self.assertEqual(self.record()["base_sha"], author_commit)

        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        p = self.red("reprove")
        self.assertRed(p, 2)
        self.assertEqual(self.record()["base_sha"], self.git(self.repo, "rev-parse", "main"))
        self.assertNotEqual(self.record()["base_sha"], author_commit)

    # ------------------------------------------------------------ refusals keep the old record
    def test_tests_that_pass_against_mains_code_are_refused_and_the_red_record_kept(self):
        # A fix round rewrites the recorded test into one main's code passes too: nothing proves it can fail.
        self.red_ticket_with_source()
        self.put("tests/test_calc.py", TEST_SUB)
        self.commit("the test now checks sub")
        self.assertEqual(self.unittest_exit(self.wt, "tests/test_calc.py"), 0)
        kept, snapshot = self.kept(), self.snapshot()
        p = self.red("reprove")
        self.assertOneLineError(p, "merge base", "prove nothing")
        self.assertEqual(self.kept(), kept)                          # no green record, unlike check
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(self.status_line(), "current: no")
        self.assertEqual(self.hook("calc.py"), CHANGED)

    def test_tests_that_pass_against_mains_code_are_refused_and_a_green_record_kept(self):
        self.cache()
        self.put("tests/test_sub.py", TEST_SUB)
        self.commit("tests for sub")
        self.assertOneLineError(self.red("check"), GREEN)            # check records green
        kept, worktrees = self.kept(), self.worktrees()
        self.assertIsNotNone(kept[1])
        p = self.red("reprove")
        self.assertOneLineError(p, "merge base", "prove nothing")
        self.assertEqual(self.kept(), kept)
        self.assertEqual(self.worktrees(), worktrees)
        self.assertEqual(self.record()["kind"], "green")

    def test_runs_that_prove_nothing_are_refused_and_the_record_kept(self):
        self.red_ticket_with_source()
        self.fix_round()
        kept, snapshot = self.kept(), self.snapshot()
        cases = [
            ("empty run", {}, self.fake_runner(PYTEST_NONE, 5), [f"no red run recorded: {EMPTY_RUN} (exit 5)"]),
            ("failed to load", {"tests/test_bad.py": TEST_SYNTAX}, {},
             ["no red run recorded: tests failed to load (exit 1)"]),
            ("timeout", {"tests/test_hang.py": TEST_HANG}, {"HARNESS_TEST_TIMEOUT": "1"},
             ["timed out after 1s", "no red run recorded"]),
            ("cannot start", {}, {"HARNESS_TEST_CMD": "no-such-test-runner-275 {files}"}, ["could not start"]),
        ]
        for name, files, env, needles in cases:
            with self.subTest(name):
                for rel, text in files.items():
                    self.put(rel, text)
                try:
                    started = time.monotonic()
                    p = self.red("reprove", **env)
                    self.assertLess(time.monotonic() - started, 30)
                    self.assertOneLineError(p, *needles)
                    self.assertEqual(self.kept(), kept)
                    self.assertEqual(self.worktrees(), snapshot["worktrees"])   # removed whatever the outcome
                finally:
                    for rel in files:
                        os.remove(os.path.join(self.wt, rel))
        self.assertEqual(self.snapshot(), snapshot)
        p = self.red("reprove", "--json", **self.fake_runner(PYTEST_NONE, 5))
        self.assertOneLineError(p, f"no red run recorded: {EMPTY_RUN}")
        refused = json.loads(p.stdout)
        self.assertTrue(refused.get("refused") == EMPTY_RUN and refused.get("exit") == 5,
                        f"the --json refusal is not the empty-run one: {quiet(refused)}")
        self.assertEqual(self.kept(), kept)
        self.assertEqual(self.status_line(), "current: no")

    def test_without_a_red_record_it_runs_and_a_red_run_writes_one(self):
        # #309 changed this on purpose: without an earlier record reprove used to be refused with NO_RECORD and run
        # nothing; now it runs, and red writes the record. Tests and source in two commits, no check between.
        self.cache()
        self.put("tests/test_calc.py", TEST_ADD)
        self.commit("tests for add")
        self.put("calc.py", CALC_BUILT)
        self.commit("add")
        env, marker = self.marker_env()
        worktrees = self.worktrees()
        self.assertEqual(self.kept(), (None, None))
        p = self.red("reprove", **env)
        self.assertRed(p, 1)
        self.assertTrue(os.path.exists(marker), "the test command did not run")
        rec = self.record()
        self.assertEqual(rec["base_sha"], self.merge_base())
        self.assertEqual(rec["test_files"], ["tests/test_calc.py"])
        self.assertEqual(rec["cmd"], env["HARNESS_TEST_CMD"].replace("{files}", "tests/test_calc.py"))
        self.assertEqual(json.loads(read(self.side_path)), expected_side_file(self.wt, ["tests/test_calc.py"]))
        self.assertEqual(self.worktrees(), worktrees)
        self.assertEqual(self.status_line(), "current: yes")
        self.assertIsNone(self.hook("calc.py"))

    def test_without_a_red_record_tests_that_pass_against_mains_code_leave_no_record(self):
        # The cloud shape whose tests prove nothing at the merge base: no record, not even a green one, and no
        # side file, so tests-first still refuses source. The second case fails at HEAD and passes only against
        # main's code: the judging is of the merge base's run.
        self.cache()
        cases = [
            ("sub, which main has", {"tests/test_calc.py": TEST_SUB, "calc.py": CALC_BUILT}, 0),
            ("add unbuilt, which only main's code passes", {"tests/test_calc.py": TEST_ADD_UNBUILT}, 1),
            ("two more files, all passing there",
             {"tests/test_sub.py": TEST_SUB, "tests/test_unbuilt.py": TEST_ADD_UNBUILT}, 1),
        ]
        for name, commit, exit_at_head in cases:
            with self.subTest(name):
                self.cloud_commit(commit)
                tests = sorted(p for p in commit if p.startswith("tests/"))
                self.assertEqual(self.unittest_exit(self.wt, *tests) != 0, bool(exit_at_head))
                snapshot = self.snapshot()
                p = self.red("reprove")
                self.assertOneLineError(p, "merge base", "prove nothing")
                self.assertEqual(self.kept(), (None, None))
                self.assertNoRecordAnywhere()
                self.assertEqual(self.snapshot(), snapshot)
                self.assertOneLineError(self.red("status"), NO_RECORD)
                self.assertEqual(self.hook("calc.py"), NO_RED_HOOK.format(n=12))
        p = self.red("reprove", "--json")
        self.assertOneLineError(p, "merge base", "prove nothing")
        self.assertEqual(json.loads(p.stdout).get("refused"), "green")
        self.assertEqual(self.kept(), (None, None))

    def test_without_a_red_record_runs_that_prove_nothing_leave_no_record(self):
        self.cache()
        self.cloud_commit({"tests/test_calc.py": TEST_ADD, "calc.py": CALC_BUILT})
        snapshot = self.snapshot()
        cases = [
            ("empty run", {}, self.fake_runner(PYTEST_NONE, 5), [f"no red run recorded: {EMPTY_RUN} (exit 5)"]),
            ("failed to load", {"tests/test_bad.py": TEST_SYNTAX}, {},
             ["no red run recorded: tests failed to load (exit 1)"]),
            ("timeout", {"tests/test_hang.py": TEST_HANG}, {"HARNESS_TEST_TIMEOUT": "1"},
             ["timed out after 1s", "no red run recorded"]),
            ("cannot start", {}, {"HARNESS_TEST_CMD": "no-such-test-runner-309 {files}"}, ["could not start"]),
        ]
        for name, files, env, needles in cases:
            with self.subTest(name):
                for rel, text in files.items():
                    self.put(rel, text)
                try:
                    started = time.monotonic()
                    p = self.red("reprove", **env)
                    self.assertLess(time.monotonic() - started, 30)
                    self.assertOneLineError(p, *needles)
                    self.assertEqual(self.kept(), (None, None))
                    self.assertNoRecordAnywhere()
                    self.assertEqual(self.worktrees(), snapshot["worktrees"])   # removed whatever the outcome
                finally:
                    for rel in files:
                        os.remove(os.path.join(self.wt, rel))
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(self.hook("calc.py"), NO_RED_HOOK.format(n=12))
        # The same branch with a run that proves something: red, so the refusals above were the runs'.
        self.assertRed(self.red("reprove"), 1)
        self.assertEqual(self.status_line(), "current: yes")

    def test_without_a_red_record_or_test_changes_it_is_refused_as_check_refuses(self):
        self.cache()
        self.cloud_commit({"calc.py": CALC_BUILT}, message="source only, from a cloud session")
        self.put("docs/notes.md", "notes\n")                        # exempt, uncommitted
        env, marker = self.marker_env()
        worktrees = self.worktrees()
        p = self.red("reprove", **env)
        self.assertEqual(p.returncode, 1, quiet(p.stdout + p.stderr))
        self.assertEqual(quiet(p.stderr.strip()), "harness-red: " + NO_TEST_CHANGES)
        self.assertFalse(os.path.exists(marker), "the test command ran without test changes")
        self.assertEqual(self.kept(), (None, None))
        self.assertEqual(self.worktrees(), worktrees)

    def test_without_test_changes_it_is_refused(self):
        self.cache()
        self.put("tests/test_calc.py", TEST_ADD)                   # proven red, then dropped
        self.assertRed(self.red("check"), 1)
        os.remove(os.path.join(self.wt, "tests", "test_calc.py"))
        self.put("calc.py", CALC_BUILT)
        self.commit("add")
        self.put("docs/notes.md", "notes\n")                        # exempt, uncommitted
        env, marker = self.marker_env()
        kept, worktrees = self.kept(), self.worktrees()
        p = self.red("reprove", **env)
        self.assertEqual(p.returncode, 1, quiet(p.stdout + p.stderr))
        self.assertEqual(quiet(p.stderr.strip()), "harness-red: " + NO_TEST_CHANGES)
        self.assertFalse(os.path.exists(marker), "the test command ran without test changes")
        self.assertEqual(self.kept(), kept)
        self.assertEqual(self.worktrees(), worktrees)

    # ------------------------------------------------------------ the ticket worktree is left alone
    def test_the_ticket_worktree_and_the_worktree_list_are_untouched_and_a_scratch_copy_survives(self):
        self.red_ticket_with_source()
        self.put("tests/test_calc.py", TEST_ADD_COVERED)            # committed coverage
        self.commit("coverage for add")
        self.put("tests/test_more.py", TEST_MORE)                   # staged
        self.git(self.wt, "add", "tests/test_more.py")
        self.put("util.py", "HELPER = 1\n")                          # the coder's work in progress
        self.put("README.md", "# calc, edited\n")                   # tracked, modified
        p = self.red("scratch", "create")
        self.assertEqual((p.returncode, quiet(p.stdout.strip())), (0, self.scratch), quiet(p.stderr))
        self.put("calc.py", "def add(a, b):\n    return 5\n", root=self.scratch)     # the breaker's cheat
        scratch_head = self.git(self.scratch, "rev-parse", "HEAD")
        scratch_tree = tree(self.scratch)
        before = self.snapshot()
        self.assertIn(self.scratch, before["worktrees"])

        p = self.red("reprove")
        self.assertRed(p, 2)
        self.assertEqual(self.record()["test_files"], ["tests/test_calc.py", "tests/test_more.py"])
        after = self.snapshot()
        for key in before:
            with self.subTest(key=key):
                self.assertEqual(after[key], before[key])
        self.assertTrue(os.path.isdir(self.scratch))
        self.assertEqual(self.git(self.scratch, "rev-parse", "HEAD"), scratch_head)
        self.assertEqual(tree(self.scratch), scratch_tree)
        self.assertEqual(read(os.path.join(self.wt, "calc.py")), CALC_BUILT)
        self.assertEqual(self.status_line(), "current: yes")


class ReproveNoTests(Fixture):
    """A ticket whose cache carries `no-tests` needs no red run: reprove answers exactly as check and status do."""

    def message(self, number):
        return f"ticket {number} is no-tests: no red run needed"

    def test_a_no_tests_ticket_answers_like_check_and_status(self):
        self.cache(12, ["no-tests"])
        self.put("tests/test_calc.py", TEST_ADD)                   # tests that would prove red are there
        env, marker = self.marker_env()
        p = self.red("reprove", **env)
        self.assertEqual(p.returncode, 0, quiet(p.stdout + p.stderr))
        self.assertInText(self.message(12), p.stdout, "stdout")
        self.assertFalse("Traceback" in p.stderr, quiet(p.stderr))
        self.assertFalse(os.path.exists(marker), "the test command ran on a no-tests ticket")
        self.assertEqual(self.kept(), (None, None))

        p = self.red("reprove", "--json", **env)
        self.assertEqual(p.returncode, 0, quiet(p.stdout + p.stderr))
        self.assertTrue(json.loads(p.stdout) == {"no_tests": True, "ticket": 12, "message": self.message(12)},
                        quiet(p.stdout))
        self.assertEqual(len(p.stdout.strip().splitlines()), 1, quiet(p.stdout))
        self.assertFalse(os.path.exists(marker))

        write(self.record_path, '{"branch": "t/12-slug", "kept": true}\n')     # an older record stays as it was
        self.assertEqual(self.red("reprove", **env).returncode, 0)
        self.assertEqual(read(self.record_path), '{"branch": "t/12-slug", "kept": true}\n')

        # The number is the ticket's: another ticket, another worktree.
        self.cache(47, ["no-tests"])
        wt = os.path.join(self.repo, ".claude", "worktrees", "t-47-other-docs")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "t/47-other-docs", "origin/main")
        p = self.red("reprove", "--worktree", wt, cwd=self.tmp, **env)
        self.assertEqual(p.returncode, 0, quiet(p.stdout + p.stderr))
        self.assertInText(self.message(47), p.stdout, "stdout")
        self.assertFalse("ticket 12" in p.stdout, quiet(p.stdout))
        self.assertFalse(os.path.exists(os.path.join(self.state, "red", "t-47-other-docs.json")))


# ---------------------------------------------------------------- docs/design.md
def design_lines():
    text = re.sub(r"<!--.*?-->", "", read(DESIGN), flags=re.DOTALL)     # a comment documents nothing
    return text.splitlines()


def design_section(lines, title_line):
    """The lines under the one heading `title_line`, up to the next heading of its level or higher."""
    level = len(title_line) - len(title_line.lstrip("#"))
    fenced, start = False, None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"^(#{1,6}) ", line)
        if start is None and line.rstrip() == title_line:
            start = i + 1
        elif start is not None and m and len(m.group(1)) <= level:
            return lines[start:i]
    if start is None:
        raise AssertionError(f"no heading {title_line!r} in docs/design.md")
    return lines[start:]


def paragraph_starting(lines, opening):
    """The paragraph (up to a blank line) whose first line starts with `opening`."""
    starts = [i for i, line in enumerate(lines) if line.startswith(opening)]
    if len(starts) != 1:
        raise AssertionError(f"want one paragraph starting {opening!r} in docs/design.md, found {len(starts)}")
    out = []
    for line in lines[starts[0]:]:
        if not line.strip():
            break
        out.append(line)
    return "\n".join(out)


# #309: what the docs must say about a branch brought home from a cloud session, checked one sentence at a time so
# prose that turns such a branch away cannot pass on its key words. A sentence ends at . ! or ? before a space and a
# capital, a backtick, a bold or a bracket (a period inside a code span such as `harness-red check.` does not end one).
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*(\[])")
# The positive statement: reprove writes the record ("writes the record", "writes its red record", "writes a record").
WRITES_RECORD = re.compile(r"\bwrites (?:the|its|a) (?:red )?record\b", re.IGNORECASE)
# "red" as a word of its own, not the "red" of `harness-red`.
RED_WORD = re.compile(r"(?<![-\w])red\b", re.IGNORECASE)
# Words that turn the branch away; none may stand in a sentence about a cloud branch and its record.
CLOUD_NEGATIONS = re.compile(r"refus|turned away|turns? away|cannot|can not|n't\b|\bnot\b|\bnever\b|rebuil|"
                             r"needs-input|needs input|\binstead\b", re.IGNORECASE)
# A source edit: a merge-conflict resolution, or any other edit of source.
SOURCE_EDIT = re.compile(r"conflict|source edit|\bedit(?:s|ing)? (?:to |in |of )?(?:the )?source", re.IGNORECASE)


def sentences(paragraph):
    return [s for s in SENTENCE_END.split(paragraph) if s.strip()]


def about_a_cloud_record(sentence):
    """A sentence that speaks of a cloud branch together with its record or reprove."""
    low = sentence.lower()
    return "cloud" in low and ("record" in low or "reprove" in low)


def proves_a_cloud_branch(sentence):
    """The sentence says a branch from a cloud session with no record gets its record when red at the merge base."""
    low = sentence.lower()
    return (all(p in low for p in ("cloud session", "no record", "merge base"))
            and bool(WRITES_RECORD.search(sentence)) and bool(RED_WORD.search(sentence)))


def turned_away(sentences_):
    """(negation, sentence) for each sentence about a cloud branch's record that holds a negation."""
    out = []
    for s in sentences_:
        m = CLOUD_NEGATIONS.search(s) if about_a_cloud_record(s) else None
        if m:
            out.append((m.group(0), s))
    return out


class ReproveDocs(unittest.TestCase):

    def test_the_harness_red_section_documents_reprove(self):
        section = design_section(design_lines(), "### harness-red")
        entries = [line for line in section if re.match(r"^- `reprove\b", line)]
        self.assertEqual(len(entries), 1, "want one `- `reprove ...`` entry in the harness-red section")
        self.assertTrue("merge base" in entries[0], "the reprove entry does not say it runs at the merge base")

    def test_the_recovery_from_a_false_not_current_names_reprove(self):
        para = paragraph_starting(design_lines(), 'Recovering from a false "not current"')
        self.assertTrue("harness-red reprove" in para, "the recovery paragraph does not name harness-red reprove")

    def test_the_hooks_stale_denial_is_the_new_text(self):
        text = "\n".join(design_lines())
        self.assertTrue(CHANGED in text, f"docs/design.md does not state tests_first's denial {CHANGED!r}")
        self.assertFalse(OLD_CHANGED in text, f"docs/design.md still states the old denial {OLD_CHANGED!r}")

    # #309: the phrases each doc must carry, kept few so the prose is the coder's. The proving sentence holds them
    # all at once ("cloud session", "no record", "merge base", "writes the record" or "writes its red record", and
    # "red" as a word); no sentence about a cloud branch and its record holds a word of CLOUD_NEGATIONS.
    PROVING = ("one sentence holding 'cloud session', 'no record', 'writes the record' (or 'writes its red record'), "
               "'red' and 'merge base'")

    def test_the_reprove_entry_needs_no_earlier_record_and_serves_a_branch_from_a_cloud_session(self):
        section = design_section(design_lines(), "### harness-red")
        entries = [line for line in section if re.match(r"^- `reprove\b", line)]
        self.assertEqual(len(entries), 1, "want one `- `reprove ...`` entry in the harness-red section")
        entry = entries[0]
        for refusal in ("Without a record it is refused", "No red run recorded for <branch>"):
            self.assertFalse(refusal in entry, f"the reprove entry still says it is refused without a record: "
                                               f"{refusal!r}")
        for phrase in ("cloud session", "no record"):
            self.assertTrue(phrase in entry, f"the reprove entry does not say {phrase!r}")
        found = turned_away(sentences(entry))
        self.assertEqual(found, [], "a sentence of the reprove entry about a cloud branch and its record turns it "
                                    "away; reprove writes its record instead")
        self.assertTrue([s for s in sentences(entry) if proves_a_cloud_branch(s)],
                        f"the reprove entry does not say a branch from a cloud session with no record gets its record "
                        f"when red at the merge base: want {self.PROVING}")

    def test_the_work_skill_reproves_a_branch_from_a_cloud_session_before_a_source_edit(self):
        text = re.sub(r"<!--.*?-->", "", read(WORK_SKILL), flags=re.DOTALL)
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text      # past the front matter
        all_sentences = [s for line in body.splitlines() for s in sentences(line)]
        found = turned_away(all_sentences)
        self.assertEqual(found, [], "a sentence of the work skill about a cloud branch and its record turns it "
                                    "away; harness-red reprove writes its record instead")
        proving = [s for s in all_sentences if proves_a_cloud_branch(s) and "harness-red reprove" in s]
        self.assertTrue(proving, f"the work skill does not say harness-red reprove gives a branch from a cloud "
                                 f"session with no record its record: want {self.PROVING}, and 'harness-red reprove'")

        def reprove_first(s):
            at = s.find("harness-red reprove")
            edit = SOURCE_EDIT.search(s)
            return edit is not None and edit.start() > at and "conflict" in s[at:].lower()

        self.assertTrue([s for s in proving if reprove_first(s)],
                        "in the work skill's sentence on a branch from a cloud session, harness-red reprove does not "
                        f"come before the merge-conflict resolution (want 'harness-red reprove', then 'conflict', "
                        f"and no source edit named before it): {proving!r}")


if __name__ == "__main__":
    unittest.main()
