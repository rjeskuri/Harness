"""Tests for harness-red: the red proof, its record, and the test-breaker's scratch copy.

    python3 -m unittest discover -s plugins/harness/tests -v

Every test builds, with real git under a temporary folder, a bare "origin", a clone with `main`, and the
branch t/12-slug in a linked worktree at <clone>/.claude/worktrees/t-12-slug. HOME is temporary, so the
records land under it, never in the real ~/.claude. The fixture's test command is
`python3 -m unittest {files}` (from the clone's .claude/settings.json), with a `python3` on PATH that runs
the interpreter running these tests.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

sys.dont_write_bytecode = True      # importing the bin script must not leave a __pycache__ in bin/

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
TOOL = os.path.join(BIN, "harness-red")
GIT = shutil.which("git")
BRANCH, SLUG = "t/12-slug", "t-12-slug"
RECORD_KEYS = {"branch", "base_sha", "test_files", "run_files", "test_hash", "cmd", "exit", "kind", "tail", "at"}
GREEN = "Tests pass before the implementation; they do not test this change."

CALC = "def add(a, b):\n    return None\n\n\ndef sub(a, b):\n    return a - b\n"
CHEAT = "def add(a, b):\n    return 5\n"
TEST_ADD = ("import unittest\n\nimport calc\n\n\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
TEST_SUB = ("import unittest\n\nimport calc\n\n\nclass SubTest(unittest.TestCase):\n"
            "    def test_sub(self):\n        self.assertEqual(calc.sub(5, 3), 2)\n")
TEST_IMPORT = ("import unittest\n\nfrom slugify_text import slugify\n\n\nclass SlugTest(unittest.TestCase):\n"
               "    def test_slug(self):\n        self.assertEqual(slugify('A b'), 'a-b')\n")
TEST_HANG = ("import time\nimport unittest\n\n\nclass HangTest(unittest.TestCase):\n"
             "    def test_hang(self):\n        time.sleep(60)\n")
TEST_SYNTAX = "import unittest\n\nimport calc\n\n\ndef test_add(:\n    assert calc.add(2, 3) == 5\n"

# What pytest 7 and unittest print (trimmed) when nothing proves red, and for the usual first red: the module
# under test does not exist yet, which pytest reports as a collection error.
PYTEST_NONE = "collected 0 items\n\n=================== no tests ran in 0.00s ===================\n"
UNITTEST_NONE = "\n--------------------------------------------------\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n"
PYTEST_MISSING = ("collected 0 items / 1 error\n\n=================== ERRORS ===================\n"
                  "______________________ ERROR collecting tests/test_slug.py ______________________\n"
                  "ImportError while importing test module '/w/tests/test_slug.py'.\n"
                  "E   ModuleNotFoundError: No module named 'slugify_text'\n"
                  "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
                  "=============================== 1 error in 0.05s ===============================\n")
PYTEST_SYNTAX = ("______________________ ERROR collecting tests/test_bad.py ______________________\n"
                 "E     File \"/w/tests/test_bad.py\", line 1\nE       def broken(:\nE                  ^\n"
                 "E   SyntaxError: invalid syntax\n"
                 "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n")
PYTEST_NAME_ERROR = ("collected 0 items / 1 error\n_____ ERROR collecting tests/test_x.py _____\n"
                     "E   NameError: name 'fixture' is not defined\nInterrupted: 1 error during collection\n")


def load_red():
    """Import bin/harness-red (no .py suffix) by path, as a fresh module."""
    loader = SourceFileLoader("harness_red", TOOL)
    spec = importlib.util.spec_from_loader("harness_red", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def expected_hash(root, files):
    h = hashlib.sha256()
    for f in sorted(files):
        with open(os.path.join(root, f), "rb") as fh:
            h.update(f.encode("utf-8") + b"\n" + fh.read())
    return h.hexdigest()


@unittest.skipUnless(GIT, "git is not installed")
class RedTest(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-red-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.home)
        python3 = write(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(python3, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_")) and k not in ("CLAUDE_PROJECT_DIR", "PYTHONPATH")}
        env.update(HOME=self.home, PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1",
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

    def record(self):
        with open(self.record_path, encoding="utf-8") as fh:
            return json.load(fh)

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)

    def check_red(self):
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def fake_runner(self, output, code):
        """Environment for a test command that prints `output` and exits `code` (outside the worktree)."""
        script = write(os.path.join(self.tmp, "runner.py"),
                       "import os, sys\nsys.stdout.write(os.environ['RUNNER_OUTPUT'])\n"
                       "sys.exit(int(os.environ['RUNNER_EXIT']))\n")
        return {"HARNESS_TEST_CMD": f"python3 {shlex.quote(script)} {{files}}", "RUNNER_OUTPUT": output,
                "RUNNER_EXIT": str(code)}

    # ------------------------------------------------------------ check
    def test_red_by_assertion(self):
        self.put("tests/test_calc.py", TEST_ADD)               # untracked: found through git status
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 1 test files, exit 1")
        self.assertIn("AssertionError: None != 5", p.stdout)

        rec = self.record()                                     # keyed by the main checkout, named by the branch
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["branch"], BRANCH)
        self.assertEqual(rec["base_sha"], self.git(self.repo, "rev-parse", "origin/main"))
        self.assertEqual(rec["test_files"], ["tests/test_calc.py"])
        self.assertEqual(rec["run_files"], ["tests/test_calc.py"])
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, ["tests/test_calc.py"]))
        self.assertEqual(rec["cmd"], "python3 -m unittest tests/test_calc.py")
        self.assertEqual((rec["exit"], rec["kind"]), (1, "assertion"))
        self.assertIn("AssertionError: None != 5", rec["tail"])
        self.assertLessEqual(len(rec["tail"].splitlines()), 40)
        self.assertNotIn("", rec["tail"].splitlines())         # non-empty lines only
        self.assertIsNotNone(dt.datetime.fromisoformat(rec["at"]).tzinfo)
        for _, dirs, _ in os.walk(self.wt):                     # PYTHONDONTWRITEBYTECODE reached the tests
            self.assertNotIn("__pycache__", dirs)

    def test_red_by_import(self):
        self.put("tests/test_slug.py", TEST_IMPORT)            # committed on the branch: found through git diff
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "tests for slugify")
        p = self.red("check", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rec = self.record()
        self.assertEqual(json.loads(p.stdout), rec)
        self.assertEqual((rec["kind"], rec["exit"]), ("import", 1))
        self.assertEqual(rec["test_files"], ["tests/test_slug.py"])
        self.assertIn("No module named 'slugify_text'", rec["tail"])

    def test_red_by_import_when_pytest_reports_a_collection_error(self):
        self.put("tests/test_slug.py", TEST_IMPORT)
        p = self.red("check", **self.fake_runner(PYTEST_MISSING, 2))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (import): 1 test files, exit 2")
        self.assertEqual((self.record()["kind"], self.record()["exit"]), ("import", 2))

    def test_pytest_no_tests_ran_refused(self):
        self.put("tests/test_calc.py", "import unittest\n")
        p = self.red("check", **self.fake_runner(PYTEST_NONE, 5))
        self.assertOneLineError(p, "no red run recorded: no tests ran (exit 5)")
        self.assertEqual(p.stdout.splitlines()[0], "no tests ran: 1 test files, exit 5")
        self.assertFalse(os.path.exists(self.record_path))
        p = self.red("check", "--json", **self.fake_runner(PYTEST_NONE, 5))
        self.assertOneLineError(p, "no red run recorded: no tests ran")
        refused = json.loads(p.stdout)
        self.assertEqual((refused["refused"], refused["exit"]), ("no tests ran", 5))
        self.assertIn("no tests ran in 0.00s", refused["tail"])
        self.assertFalse(os.path.exists(self.record_path))

    def test_unittest_no_tests_ran_refused(self):
        self.put("tests/test_calc.py", "import unittest\n")
        for code in (5, 1):                                     # 3.12+ exits 5; the text alone also refuses
            p = self.red("check", **self.fake_runner(UNITTEST_NONE, code))
            self.assertOneLineError(p, "no red run recorded: no tests ran")
            self.assertFalse(os.path.exists(self.record_path))

    def test_syntax_error_in_a_test_file_refused(self):
        self.put("tests/test_calc.py", TEST_SYNTAX)             # the real unittest run, exit 1
        p = self.red("check")
        self.assertOneLineError(p, "no red run recorded: tests failed to load (exit 1)")
        self.assertIn("SyntaxError", p.stdout)
        self.assertFalse(os.path.exists(self.record_path))
        p = self.red("check", **self.fake_runner(PYTEST_SYNTAX, 2))
        self.assertOneLineError(p, "no red run recorded: tests failed to load (exit 2)")
        self.assertFalse(os.path.exists(self.record_path))

    def test_only_runnable_files_run_but_all_are_hashed(self):
        self.put("tests/fixtures/data.json", '{"a": 2, "b": 3}\n')
        self.put("tests/conftest.py", "import sys\n")
        self.put("tests/test_x.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 3 test files, exit 1")
        rec = self.record()
        everything = ["tests/conftest.py", "tests/fixtures/data.json", "tests/test_x.py"]
        self.assertEqual(rec["test_files"], everything)
        self.assertEqual(rec["run_files"], ["tests/test_x.py"])
        self.assertEqual(rec["cmd"], "python3 -m unittest tests/test_x.py")
        self.assertEqual(rec["test_hash"], expected_hash(self.wt, everything))
        self.assertIn("run files: tests/test_x.py", self.red("status").stdout)

        self.put("tests/fixtures/data.json", '{"a": 2, "b": 4}\n')        # a fixture edit stales the proof
        self.assertEqual(self.red("status").stdout.splitlines()[-1], "current: no")

        self.assertEqual(self.red("scratch", "create").returncode, 0)        # every test file is copied in
        self.assertTrue(os.path.isfile(os.path.join(self.scratch, "tests", "fixtures", "data.json")))
        self.put("calc.py", CHEAT, root=self.scratch)
        p = self.red("scratch", "run")                                        # the fixture is not passed to the runner
        self.assertEqual((p.returncode, p.stdout.splitlines()[0]), (0, "cheat passed"), p.stdout + p.stderr)

    def test_fixture_only_refused(self):
        self.put("tests/fixtures/data.json", "{}\n")
        p = self.red("check")
        self.assertOneLineError(p, "No runnable test files among the test changes: tests/fixtures/data.json")
        self.assertFalse(os.path.exists(self.record_path))

    def test_green_refused_but_recorded(self):
        self.put("tests/test_sub.py", TEST_SUB)
        p = self.red("check")
        self.assertOneLineError(p, GREEN)
        rec = self.record()                                     # written anyway: a refactor ticket relies on it
        self.assertEqual((rec["kind"], rec["exit"]), ("green", 0))
        self.assertEqual(rec["test_files"], ["tests/test_sub.py"])

    def test_source_changed_refused(self):
        self.put("tests/test_calc.py", TEST_ADD)
        self.put("calc.py", CALC.replace("return None", "return a + b"))      # modified source
        self.put("util.py", "X = 1\n")                                         # new source
        self.put("docs/notes.md", "exempt\n")
        p = self.red("check")
        self.assertOneLineError(p, "Source already changed before the red run: calc.py, util.py. "
                                   "Revert them or move them to the coder step.")
        self.assertFalse(os.path.exists(self.record_path))

    def test_no_tests_refused(self):
        p = self.red("check")
        self.assertOneLineError(p, "No test changes on this branch; write the tests first.")
        self.put("README.md", "# calc, documented\n")                          # exempt changes only
        self.put("docs/usage.md", "usage\n")
        p = self.red("check")
        self.assertOneLineError(p, "No test changes on this branch; write the tests first.")
        self.assertFalse(os.path.exists(self.record_path))

    def test_base_falls_back_to_main(self):
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.check_red()
        self.assertEqual(self.record()["base_sha"], self.git(self.repo, "rev-parse", "main"))
        self.assertOneLineError(self.red("check", "--base", "nope"), "unknown base 'nope'")

    def test_timeout_stops_the_tests(self):
        self.put("tests/test_hang.py", TEST_HANG)
        started = time.monotonic()
        p = self.red("check", HARNESS_TEST_TIMEOUT="1")        # the environment wins over the settings
        self.assertLess(time.monotonic() - started, 20)
        self.assertOneLineError(p, "timed out after 1s", "no red run recorded")
        self.assertFalse(os.path.exists(self.record_path))

    def test_bad_invocations(self):
        self.assertOneLineError(self.red("check", cwd=self.tmp), "not inside a git working tree")
        self.assertOneLineError(self.red("bogus"), "harness-red: error:")
        self.assertOneLineError(self.red(), "choose a subcommand")
        detached = os.path.join(self.tmp, "detached")
        self.git(self.repo, "worktree", "add", "-q", "--detach", detached, "main")
        self.assertOneLineError(self.red("check", "--worktree", detached), "HEAD is detached")

    # ------------------------------------------------------------ status
    def test_status_current(self):
        self.assertOneLineError(self.red("status"), f"No red run recorded for {BRANCH}")
        self.check_red()
        p = self.red("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("kind: assertion (exit 1)", p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "current: yes")

        self.put("tests/test_calc.py", TEST_ADD + "\n# edited\n")
        p = self.red("status", "--worktree", self.wt, cwd=self.tmp)
        self.assertEqual(p.stdout.splitlines()[-1], "current: no")
        status = json.loads(self.red("status", "--json").stdout)
        self.assertEqual(status, dict(self.record(), current=False))

        os.remove(os.path.join(self.wt, "tests", "test_calc.py"))
        self.assertEqual(self.red("status").stdout.splitlines()[-1], "current: no")

    # ------------------------------------------------------------ scratch
    def test_scratch_create_and_run(self):
        self.check_red()                                        # the test file is not committed
        p = self.red("scratch", "create")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), self.scratch)
        self.assertEqual(read(os.path.join(self.scratch, "tests", "test_calc.py")), TEST_ADD)
        self.assertEqual(read(os.path.join(self.scratch, "calc.py")), CALC)
        self.assertEqual(self.git(self.scratch, "rev-parse", "HEAD"), self.git(self.wt, "rev-parse", "HEAD"))
        self.assertNotEqual(subprocess.run([GIT, "-C", self.scratch, "symbolic-ref", "-q", "HEAD"],
                                           env=self.env, capture_output=True).returncode, 0)   # detached

        p = self.red("scratch", "run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "cheat failed")
        self.assertIn("AssertionError", p.stdout)

        self.put("calc.py", CHEAT, root=self.scratch)           # the cheapest pass: a constant
        p = self.red("scratch", "run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "cheat passed")
        self.assertEqual(read(os.path.join(self.wt, "calc.py")), CALC)       # the ticket worktree is untouched

        self.assertOneLineError(self.red("scratch", "create"), "already exists")

    def test_scratch_remove(self):
        self.assertOneLineError(self.red("scratch", "create"), "No red run recorded")
        self.check_red()
        self.assertEqual(self.red("scratch", "create").returncode, 0)
        p = self.red("scratch", "remove")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"removed {self.scratch}")
        self.assertFalse(os.path.exists(self.scratch))
        self.assertNotIn(self.scratch, self.git(self.repo, "worktree", "list", "--porcelain"))
        p = self.red("scratch", "remove")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, f"no scratch copy at {self.scratch}"))
        self.assertOneLineError(self.red("scratch", "run"), "No scratch copy for t/12-slug")


class RedHelpers(unittest.TestCase):
    """The pure parts, imported from the script."""

    @classmethod
    def setUpClass(cls):
        cls.red = load_red()

    def test_glob_matcher(self):
        cases = [
            ("tests/**", "tests/test_x.py", True), ("tests/**", "tests/a/b/c.json", True),
            ("tests/**", "tests", True), ("tests/**", "src/tests/x.py", False),
            ("**/test_*.py", "test_x.py", True), ("**/test_*.py", "pkg/sub/test_x.py", True),
            ("**/test_*.py", "pkg/x_test.py", False), ("**/*.md", "README.md", True),
            ("docs/**", "src/docs/x.md", False), ("spec/**", "myspec/x.rb", False),
            ("src/*/x.py", "src/a/x.py", True), ("src/*/x.py", "src/a/b/x.py", False),
            ("a/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True), ("a/**/b", "a/x/y/c", False),
            (".gitignore", "sub/.gitignore", True), ("LICENSE*", "LICENSE.txt", True),
            ("*.toml", "pyproject.toml", True), ("", "x", False),
        ]
        for pattern, path, want in cases:
            self.assertEqual(self.red.glob_match(pattern, path), want, (pattern, path))

    def test_default_classification(self):
        with mock.patch.dict(os.environ, {"HARNESS_TEST_PATTERNS": "", "HARNESS_SOURCE_EXEMPT": ""}):
            tests = self.red.globs("HARNESS_TEST_PATTERNS", {}, self.red.TEST_PATTERNS)
            exempt = self.red.globs("HARNESS_SOURCE_EXEMPT", {}, self.red.SOURCE_EXEMPT)
            for path, want in [("tests/test_x.py", "test"), ("pkg/conftest.py", "test"), ("web/a.test.ts", "test"),
                               ("spec/a_spec.rb", "test"), ("docs/x.py", "exempt"), ("pyproject.toml", "exempt"),
                               (".claude/settings.json", "exempt"), ("LICENSE", "exempt"), ("src/pkg/a.py", "source"),
                               ("calc.py", "source")]:
                got = ("test" if any(self.red.glob_match(g, path) for g in tests) else
                       "exempt" if any(self.red.glob_match(g, path) for g in exempt) else "source")
                self.assertEqual(got, want, path)

    def test_runnable_files(self):
        files = ["tests/conftest.py", "tests/helpers.py", "tests/fixtures/a.json", "tests/test_a.py", "pkg/b_test.py",
                 "web/c.test.js", "web/d_test.js", "web/e.test.ts", "web/f_test.ts", "web/g.test.mjs",
                 "web/h.spec.js", "web/i.spec.ts", "spec/j_spec.rb"]
        self.assertEqual(self.red.runnable(files), ["tests/test_a.py", "pkg/b_test.py", "web/c.test.js", "web/d_test.js",
                                                    "web/e.test.ts", "web/f_test.ts", "web/g.test.mjs", "web/h.spec.js",
                                                    "web/i.spec.ts"])
        with self.assertRaises(self.red.Problem):
            self.red.runnable(["tests/conftest.py", "tests/fixtures/a.json"])

    def test_settings_precedence(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="harness-red-settings-"))
        self.addCleanup(shutil.rmtree, root, True)
        write(os.path.join(root, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_TEST_CMD": "shared {files}", "HARNESS_TEST_TIMEOUT": 30}}))
        write(os.path.join(root, ".claude", "settings.local.json"), json.dumps({"env": {"HARNESS_TEST_CMD": "local"}}))
        file_env = self.red.settings_env(root)
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CMD": "", "HARNESS_TEST_TIMEOUT": ""}):
            self.assertEqual(self.red.setting("HARNESS_TEST_CMD", file_env), "local")
            self.assertEqual(self.red.test_timeout(file_env), 30.0)
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CMD": "from env", "HARNESS_TEST_TIMEOUT": "junk"}):
            self.assertEqual(self.red.setting("HARNESS_TEST_CMD", file_env), "from env")
            self.assertEqual(self.red.test_timeout(file_env), 300.0)

    def test_runs_that_prove_nothing(self):
        cases = [
            (5, "", "no tests ran"), (5, PYTEST_NONE, "no tests ran"), (1, UNITTEST_NONE, "no tests ran"),
            (4, "collected 0 items\n", "no tests ran"),
            (2, PYTEST_SYNTAX, "tests failed to load"),
            (1, "IndentationError: unexpected indent\n", "tests failed to load"),
            (2, PYTEST_NAME_ERROR, "tests failed to load"),
            (2, PYTEST_MISSING, None),                                         # the usual first red
            (1, "ImportError: cannot import name 'slugify' from 'text'\n", None),
            (1, "ModuleNotFoundError: No module named 'calc2'\n", None),
            (1, "AssertionError: SyntaxError not raised\nFAILED (failures=1)\n", None),
            (1, "AssertionError: None != 5\n", None),
            (0, "Ran 0 tests in 0.000s\n\nOK\n", None),          # green: refused as green elsewhere
        ]
        for code, output, want in cases:
            self.assertEqual(self.red.unproven(code, output), want, (code, output))

    def test_detected_test_command(self):
        def detect(files, **project):
            root = tempfile.mkdtemp(prefix="harness-red-detect-")
            self.addCleanup(shutil.rmtree, root, True)
            for name, text in project.items():
                write(os.path.join(root, name.replace("_", ".")), text)
            return self.red.detect_test_cmd(root, files)

        py = ["tests/test_x.py"]
        self.assertEqual(detect(py, uv_lock=""), "uv run pytest -q {files}")
        self.assertEqual(detect(py, pyproject_toml="[tool.pytest.ini_options]\n"), "python3 -m pytest -q {files}")
        self.assertEqual(detect(py, pytest_ini="[pytest]\n"), "python3 -m pytest -q {files}")
        self.assertEqual(detect(py, pyproject_toml="[project]\nname = 'x'\n"), "python3 -m unittest {files}")
        self.assertEqual(detect(["pkg/x_test.py"]), "python3 -m unittest {files}")
        self.assertEqual(detect(["tests/a.test.js"], package_json='{"scripts": {"test": "node --test"}}'),
                         "npm test -- {files}")
        self.assertEqual(detect(["tests/a.test.js"], package_json='{"scripts": {}}'), "node --test {files}")
        self.assertIsNone(detect(["spec/a_spec.rb"]))


if __name__ == "__main__":
    unittest.main()
