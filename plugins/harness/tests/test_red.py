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
# A test module main already has (#231): ten tests two lines apart, so edits to the first, the middle and the end
# are separate hunks that git merges cleanly. The branch inserts test_add before test_sub_5.
CALC_SUITE = ("import unittest\n\nimport calc\n\n\nclass CalcTest(unittest.TestCase):\n"
              + "\n".join(f"    def test_sub_{i}(self):\n        self.assertEqual(calc.sub({i + 2}, 2), {i})\n"
                          for i in range(10)))
SUB_5 = "    def test_sub_5(self):\n"
SUB_10 = "\n    def test_sub_10(self):\n        self.assertEqual(calc.sub(12, 2), 10)\n"
BRANCH_ADD = "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n"
MAIN_ADD = "    def test_add(self):\n        self.assertEqual(calc.add(1, 1), 2)\n\n"
THIRD_ADD = "    def test_add(self):\n        self.assertEqual(calc.add(2, 2), 4)\n\n"
ADD_ASSERT = "        self.assertEqual(calc.add(2, 3), 5)\n"     # the branch's own assert line


def sub_line(i):
    """The assert line of test_sub_i as main first wrote it."""
    return f"        self.assertEqual(calc.sub({i + 2}, 2), {i})\n"


# Repeated context (#231): ten identical test classes with PEP 8's two blank lines between them, so every gap has
# the same surroundings however many context lines a diff shows; only the position tells the gaps apart. The
# branch's own change is a whole new class inserted into gap 5.
SUITE_HEAD = "import unittest\n\nimport calc\n\n\n"
SAME_CASE = "class CaseTest(unittest.TestCase):\n    def test_case(self):\n        self.assertEqual(calc.sub(3, 1), 2)"
ADD_CASE = "class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)"
MAIN_ADD_CASE = ("class AddMainTest(unittest.TestCase):\n    def test_add(self):\n"
                 "        self.assertEqual(calc.add(1, 1), 2)")


def case_with(n):
    """SAME_CASE with its numbers changed: main's edit of one repeated class."""
    return SAME_CASE.replace("calc.sub(3, 1), 2)", f"calc.sub({n + 1}, 1), {n})")


def suite(classes):
    return SUITE_HEAD + "\n\n\n".join(classes) + "\n"


def suite_classes(text):
    assert text.startswith(SUITE_HEAD) and text.endswith("\n") and not text.endswith("\n\n"), text
    return text[len(SUITE_HEAD):-1].split("\n\n\n")


def class_at(text, gap, case):
    classes = suite_classes(text)
    classes.insert(gap, case)
    return suite(classes)


def class_moved(text, case, gap):
    classes = suite_classes(text)
    assert classes.count(case) == 1, text
    classes.remove(case)
    classes.insert(gap, case)
    return suite(classes)


def class_replaced(text, index, case):
    classes = suite_classes(text)
    classes[index] = case
    return suite(classes)


# A unique last class bounds the repeated run: main's clean merges edit it, where git's alignment is unambiguous.
LAST_CASE = "class LastTest(unittest.TestCase):\n    def test_last(self):\n        self.assertEqual(calc.sub(9, 9), 0)"
REPEATED_SUITE = suite([SAME_CASE] * 10 + [LAST_CASE])

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


def once(text, old, new):
    """`text` with its one occurrence of `old` replaced; fails loudly when there is not exactly one."""
    assert text.count(old) == 1, (old, text)
    return text.replace(old, new)


def swapped(text, a, b):
    """`text` with its one occurrence each of `a` and `b` swapped: the same lines, in another order."""
    marker = "\0swap\0"
    return once(once(once(text, a, marker), b, a), marker, b)


def moved_to_end(text, block):
    """`text` with `block` (a test method ending in its blank separator) moved after the last test: the same
    lines, in another order."""
    return once(text, block, "") + "\n" + block[:-1]


def line_moved_into(text, line, anchor):
    """`text` with `line` taken out of its test and put after `anchor`, in another one."""
    return once(once(text, line, ""), anchor, anchor + line)


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

    # ------------------------------------------------------------ which files are tests (ticket #6)
    # HARNESS_TEST_PATTERNS and HARNESS_SOURCE_EXEMPT come from the environment, then from `env` in the MAIN
    # checkout's .claude/settings.json, exactly like hooks/tests_first.py: never from the ticket worktree's
    # files, never from a settings.local.json (an agent can rewrite those). HARNESS_TEST_CMD stays as it was.
    def settings(self, root, env, local=False):
        """Write root/.claude/settings.json (or settings.local.json) with `env`; the test command is kept."""
        full = dict(env)
        if not local:
            full["HARNESS_TEST_CMD"] = "python3 -m unittest {files}"
        return write(os.path.join(root, ".claude", "settings.local.json" if local else "settings.json"),
                     json.dumps({"env": full}))

    def refused_sources(self, p):
        """The paths named by the 'Source already changed' refusal."""
        self.assertOneLineError(p, "Source already changed before the red run: ")
        line = p.stderr.strip().split("Source already changed before the red run: ", 1)[1]
        return line.split(". Revert them", 1)[0].split(", ")

    def test_patterns_from_the_main_checkout_apply_in_the_worktree(self):
        cases = [("specs/**", "specs/test_calc.py"), ("suite/**", "suite/deep/test_calc.py"),
                 ("pkg/test_*.py", "pkg/test_calc.py"), ("**/*_test.py", "deep/dir/calc_test.py"),
                 ("other/**, y/**", "y/test_calc.py"), ("**/test_*.py", "anywhere/test_calc.py")]
        for patterns, path in cases:
            with self.subTest(patterns=patterns):
                self.settings(self.repo, {"HARNESS_TEST_PATTERNS": patterns})
                self.put(path, TEST_ADD)
                try:
                    p = self.red("check")
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 1 test files, exit 1")
                    self.assertEqual(self.record()["test_files"], [path])
                    self.assertEqual(self.record()["run_files"], [path])
                    if patterns != "**/test_*.py":          # tests/ is then not a test folder: source
                        self.put("tests/test_other.py", TEST_SUB)
                        self.assertEqual(self.refused_sources(self.red("check")), ["tests/test_other.py"])
                finally:                                    # the next case starts from a clean worktree
                    for f in (path, "tests/test_other.py"):
                        if os.path.isfile(os.path.join(self.wt, f)):
                            os.remove(os.path.join(self.wt, f))

    def test_worktree_settings_json_cannot_redefine_the_patterns(self):
        self.settings(self.wt, {"HARNESS_TEST_PATTERNS": "checks/**"})          # main still has the defaults
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["tests/test_calc.py"])
        self.put("checks/helper.py", "X = 1\n")                                 # a test only by the worktree's patterns
        self.assertEqual(self.refused_sources(self.red("check")), ["checks/helper.py"])

    def test_settings_local_json_cannot_redefine_the_patterns(self):
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**"})
        self.settings(self.wt, {"HARNESS_TEST_PATTERNS": "checks/**"})          # worktree settings.json
        self.settings(self.wt, {"HARNESS_TEST_PATTERNS": "tests/**"}, local=True)   # worktree settings.local.json
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "extra/**"}, local=True)  # main settings.local.json
        self.put("specs/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["specs/test_calc.py"])
        for rel in ("checks/helper.py", "tests/helper.py", "extra/helper.py"):  # none of them says "test"
            self.put(rel, "X = 1\n")
        self.assertEqual(self.refused_sources(self.red("check")),
                         ["checks/helper.py", "extra/helper.py", "tests/helper.py"])

    def test_exempt_globs_from_the_main_checkout_only(self):
        self.settings(self.repo, {"HARNESS_SOURCE_EXEMPT": "notes/**,.claude/**"})   # .claude/**: the worktree's edited settings
        self.settings(self.wt, {"HARNESS_SOURCE_EXEMPT": "lib/**"})
        self.settings(self.wt, {"HARNESS_SOURCE_EXEMPT": "vendor/**"}, local=True)
        self.settings(self.repo, {"HARNESS_SOURCE_EXEMPT": "scratch/**"}, local=True)
        self.put("tests/test_calc.py", TEST_ADD)
        self.put("notes/data.py", "X = 1\n")                    # exempt only in the main checkout's settings
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["tests/test_calc.py"])
        for rel in ("lib/x.py", "vendor/y.py", "scratch/z.py", "docs/n.md"):    # docs/** is no longer exempt either
            self.put(rel, "X = 1\n")
        self.assertEqual(self.refused_sources(self.red("check")),
                         ["docs/n.md", "lib/x.py", "scratch/z.py", "vendor/y.py"])

    def test_environment_wins_over_the_main_checkout(self):
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**", "HARNESS_SOURCE_EXEMPT": "notes/**"})
        self.put("envtests/test_calc.py", TEST_ADD)
        self.put("notes/data.py", "X = 1\n")                    # exempt through the file: the environment sets only patterns
        p = self.red("check", HARNESS_TEST_PATTERNS="envtests/**")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["envtests/test_calc.py"])

        self.put("specs/test_calc.py", TEST_SUB)                # a test in the file, source for the environment
        self.put("other/data.py", "X = 1\n")
        p = self.red("check", HARNESS_TEST_PATTERNS="envtests/**", HARNESS_SOURCE_EXEMPT="other/**")
        self.assertEqual(self.refused_sources(p), ["notes/data.py", "specs/test_calc.py"])

        os.remove(os.path.join(self.wt, "other", "data.py"))      # exempt only through the environment just above
        p = self.red("check", HARNESS_TEST_PATTERNS="specs/**, envtests/**")     # the environment can widen it too
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["envtests/test_calc.py", "specs/test_calc.py"])

    def test_blank_environment_falls_back_to_the_main_checkout(self):
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**"})
        self.put("specs/test_calc.py", TEST_ADD)
        p = self.red("check", HARNESS_TEST_PATTERNS="  ", HARNESS_SOURCE_EXEMPT="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.record()["test_files"], ["specs/test_calc.py"])
        self.put("tests/test_other.py", TEST_SUB)               # not a test under the file's patterns
        p = self.red("check", HARNESS_TEST_PATTERNS="  ", HARNESS_SOURCE_EXEMPT="")
        self.assertEqual(self.refused_sources(p), ["tests/test_other.py"])

    def test_test_command_still_comes_from_the_worktree_settings(self):
        runner = write(os.path.join(self.tmp, "runner.py"), "import sys\nprint('ran', sys.argv[1:])\nsys.exit(1)\n")
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**"})
        write(os.path.join(self.wt, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_TEST_CMD": f"python3 {shlex.quote(runner)} {{files}}",
                                  "HARNESS_TEST_PATTERNS": "checks/**"}}))
        self.put("specs/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ran ['specs/test_calc.py']", p.stdout)
        self.assertEqual(self.record()["cmd"], f"python3 {shlex.quote(runner)} specs/test_calc.py")

    def test_unusable_main_settings_fall_back_to_the_defaults_not_the_worktree(self):
        main = os.path.join(self.repo, ".claude", "settings.json")
        cases = [("malformed json", "{not json"), ("empty file", ""), ("top level list", "[]"),
                 ("env is a list", json.dumps({"env": ["HARNESS_TEST_PATTERNS=specs/**"]})),
                 ("env is a string", json.dumps({"env": "HARNESS_TEST_PATTERNS=specs/**"})),
                 ("env is null", json.dumps({"env": None})), ("missing file", None)]
        for name, content in cases:
            with self.subTest(name):
                if content is None:
                    os.remove(main)
                else:
                    write(main, content)
                self.settings(self.wt, {"HARNESS_TEST_PATTERNS": "checks/**"})     # the decoy the worktree must not win with
                self.put("tests/test_calc.py", TEST_ADD)
                try:
                    p = self.red("check")
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertNotIn("Traceback", p.stderr)
                    self.assertEqual(self.record()["test_files"], ["tests/test_calc.py"])      # the defaults
                    self.put("checks/helper.py", "X = 1\n")
                    self.assertEqual(self.refused_sources(self.red("check")), ["checks/helper.py"])
                finally:
                    for f in ("tests/test_calc.py", "checks/helper.py"):
                        if os.path.isfile(os.path.join(self.wt, f)):
                            os.remove(os.path.join(self.wt, f))

    def test_main_setting_values_are_read_like_tests_first(self):
        # tests_first.setting(): str, int and float count (as text); bool, list and null do not (the defaults apply)
        self.put("tests/test_calc.py", TEST_ADD)
        for value in (5, 2.5):
            with self.subTest(value=value):
                self.settings(self.repo, {"HARNESS_TEST_PATTERNS": value})
                p = self.red("check")                                   # the pattern is the text "5": tests/ is no test
                self.assertOneLineError(p, "No test changes on this branch")
        for value in (True, False, ["specs/**"], None, {"a": 1}):
            with self.subTest(value=value):
                self.settings(self.repo, {"HARNESS_TEST_PATTERNS": value})
                p = self.red("check")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.record()["test_files"], ["tests/test_calc.py"])

    def test_run_from_the_main_checkout_itself(self):
        self.git(self.repo, "checkout", "-q", "-b", "t/13-main")
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**"})
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "extra/**"}, local=True)     # still never read
        self.put("specs/test_calc.py", TEST_ADD, root=self.repo)
        p = self.red("check", cwd=self.repo)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rec = json.loads(read(os.path.join(self.state, "red", "t-13-main.json")))
        self.assertEqual((rec["branch"], rec["test_files"]), ("t/13-main", ["specs/test_calc.py"]))
        for rel in ("extra/helper.py", "tests/test_other.py"):
            self.put(rel, "X = 1\n", root=self.repo)
        self.assertEqual(self.refused_sources(self.red("check", cwd=self.repo)),
                         ["extra/helper.py", "tests/test_other.py"])

    def test_test_timeout_still_comes_from_the_worktree_settings(self):
        self.settings(self.repo, {"HARNESS_TEST_PATTERNS": "specs/**", "HARNESS_TEST_TIMEOUT": 600})
        self.settings(self.wt, {"HARNESS_TEST_TIMEOUT": 1, "HARNESS_TEST_PATTERNS": "checks/**"})
        self.put("specs/test_hang.py", TEST_HANG)
        started = time.monotonic()
        p = self.red("check")
        self.assertLess(time.monotonic() - started, 20)
        self.assertOneLineError(p, "timed out after 1s", "no red run recorded")

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

    # ------------------------------------------------------------ a merge of main (#231)
    # main already has tests/test_calc.py; the test-author inserts test_add in its middle and commits, harness-red
    # check records red, the coder commits calc.add. Then main moves (pushed to origin) and the branch runs
    # `git merge origin/main`. Main's edits to that file in other hunks keep the record current; a test change the
    # branch makes itself, a merge resolution of its own lines included, does not.
    def red_ticket_with_source(self, suite=CALC_SUITE, author=None):
        """Main gets `suite` as tests/test_calc.py; the test-author applies `author` (default: test_add inserted
        before test_sub_5)."""
        author = author or (lambda t: once(t, SUB_5, BRANCH_ADD + SUB_5))
        write(os.path.join(self.repo, "tests", "test_calc.py"), suite)
        self.on_main({}, "the tests so far")
        self.git(self.wt, "merge", "-q", "--ff-only", "origin/main")
        path = os.path.join(self.wt, "tests", "test_calc.py")
        self.put("tests/test_calc.py", author(read(path)))                                # the test-author
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "tests for add")
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 1 test files, exit 1")
        self.put("calc.py", CALC.replace("return None", "return a + b"))                 # the coder
        self.git(self.wt, "commit", "-q", "-am", "add")
        self.assertEqual(self.current(), "current: yes")

    def on_main(self, changes, message="main moves on"):
        """Commit on main and push it: each path gets a function of its old text or a literal text."""
        for rel, change in changes.items():
            path = os.path.join(self.repo, rel)
            old = read(path) if os.path.exists(path) else None
            write(path, change(old) if callable(change) else change)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", message)
        self.git(self.repo, "push", "-q", "origin", "main")

    def merge_main(self):
        return subprocess.run([GIT, "-C", self.wt, "-c", "commit.gpgsign=false", "merge", "--no-edit", "origin/main"],
                              env=self.env, capture_output=True, text=True)

    def merge_cleanly(self):
        p = self.merge_main()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.git(self.wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)

    def current(self):
        p = self.red("status")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.splitlines()[-1]

    def test_status_stays_current_across_merges_of_main(self):
        self.red_ticket_with_source()
        for name, changes in (
            ("first-test", {"tests/test_calc.py": lambda t: once(t, "calc.sub(2, 2), 0)", "calc.sub(4, 4), 0)")}),
            ("inserted-above", {"tests/test_calc.py": lambda t: '"""Tests for calc."""\n' + t}),
            ("appended-and-more", {
                "tests/test_calc.py": lambda t: t + SUB_10,
                "util.py": "X = 1\n",                                   # source from main
                "tests/test_util.py": TEST_SUB,                         # a test file the branch never touched
            }),
        ):
            with self.subTest(main=name):
                self.on_main(changes)
                self.merge_cleanly()
                merged = read(os.path.join(self.wt, "tests", "test_calc.py"))
                self.assertEqual(merged, once(read(os.path.join(self.repo, "tests", "test_calc.py")),
                                              SUB_5, BRANCH_ADD + SUB_5))    # main's edits and the branch's own
                self.assertEqual(self.current(), "current: yes")
                self.assertIs(json.loads(self.red("status", "--json").stdout)["current"], True)

    def assertRecordHolds(self, before):
        """The proof is still good: the record written before, or a new red record whose test_hash is the files'
        content now; status says current either way."""
        if read(self.record_path) != before:
            rec = self.record()
            self.assertIn(rec["kind"], ("assertion", "import", "error"))   # calc.add works now: a re-run is green
            self.assertEqual(rec["test_hash"], expected_hash(self.wt, rec["test_files"]))
        self.assertEqual(self.current(), "current: yes")

    def test_check_after_a_merge_of_main_keeps_the_proof_and_never_a_stale_one(self):
        self.red_ticket_with_source()
        self.on_main({"tests/test_calc.py": lambda t: '"""Tests for calc."""\n' + t, "util.py": "X = 1\n"})
        self.merge_cleanly()
        before = read(self.record_path)
        p = self.red("check")
        self.assertNotIn("Traceback", p.stderr)
        self.assertRecordHolds(before)                          # untouched, or a new red record for these files
        # then a real branch-side test change that keeps every line: the branch's test_add moves to the end
        proven = read(self.record_path)
        path = os.path.join(self.wt, "tests", "test_calc.py")
        self.put("tests/test_calc.py", moved_to_end(read(path), BRANCH_ADD))
        self.git(self.wt, "commit", "-q", "-am", "the branch moves its test")
        self.assertEqual(self.current(), "current: no")
        p = self.red("check")
        self.assertNotIn("Traceback", p.stderr)
        after = read(self.record_path)
        rec = self.record()
        fresh = expected_hash(self.wt, rec["test_files"])
        if p.returncode == 0:                                   # a new red record for these files, never the stale one
            self.assertNotEqual(after, proven)
            self.assertIn(rec["kind"], ("assertion", "import", "error"))
            self.assertEqual(rec["test_hash"], fresh)
            self.assertEqual(self.current(), "current: yes")
        else:                                                   # refused, in one line that says why
            self.assertOneLineError(p, "harness-red: ")
            if after == proven:
                self.assertEqual(self.current(), "current: no")  # the stale record is still not proof
            else:
                self.assertEqual(rec["test_hash"], fresh)

    def test_status_stays_current_while_main_moves_unmerged(self):
        # a regression guard: it passes before #231 too
        self.red_ticket_with_source()
        self.on_main({"tests/test_calc.py": lambda t: once(t, sub_line(0), sub_line(0).replace("2, 2", "4, 4"))
                      + SUB_10, "util.py": "X = 1\n"})
        self.assertEqual(self.current(), "current: yes")

    def test_a_branch_test_edit_after_a_merge_of_main_is_not_current(self):
        self.red_ticket_with_source()
        self.on_main({"tests/test_calc.py": lambda t: once(t, "calc.sub(2, 2), 0)", "calc.sub(4, 4), 0)")})
        self.merge_cleanly()
        self.assertEqual(self.current(), "current: yes")
        path = os.path.join(self.wt, "tests", "test_calc.py")
        merged = read(path)
        head = self.git(self.wt, "rev-parse", "HEAD")
        for name, edited in (
            ("own-line", once(merged, "calc.add(2, 3), 5)", "calc.add(3, 3), 6)")),
            ("mains-line", once(merged, "calc.sub(4, 4), 0)", "calc.sub(5, 5), 0)")),
            ("appended", merged + SUB_10),
            # the same lines in another order
            ("own-test-moved", moved_to_end(merged, BRANCH_ADD)),
            ("asserts-swapped", swapped(merged, sub_line(1), sub_line(2))),
            ("own-line-swapped", swapped(merged, ADD_ASSERT, sub_line(6))),
            # the branch's own line moved out of test_add into test_sub_5
            ("own-line-into-sub-5", line_moved_into(merged, ADD_ASSERT, sub_line(5))),
        ):
            for committed in (False, True):
                with self.subTest(edit=name, committed=committed):
                    self.put("tests/test_calc.py", edited)
                    if committed:
                        self.git(self.wt, "commit", "-q", "-am", "the branch edits its tests")
                    self.assertEqual(self.current(), "current: no")
                    if committed:
                        self.git(self.wt, "reset", "-q", "--hard", head)
                    else:
                        self.git(self.wt, "checkout", "--", "tests/test_calc.py")
                    self.assertEqual(self.current(), "current: yes")

    def test_a_conflict_resolution_changing_the_branchs_test_lines_is_not_current(self):
        self.red_ticket_with_source()
        self.on_main({"tests/test_calc.py": lambda t: '"""Tests for calc."""\n' + t})
        self.merge_cleanly()                                    # a clean merge first: the record holds
        self.assertEqual(self.current(), "current: yes")
        self.on_main({"tests/test_calc.py": lambda t: once(t, SUB_5, MAIN_ADD + SUB_5)}, "main adds its own test_add")
        before = self.git(self.wt, "rev-parse", "HEAD")
        theirs = read(os.path.join(self.repo, "tests", "test_calc.py"))
        for name, resolved in (
            ("third-value", once(theirs, MAIN_ADD, THIRD_ADD)),
            ("mains-side", theirs),
            # main's test_add kept; the branch's assert line verbatim, but in test_sub_8
            ("branch-line-into-sub-8", once(theirs, sub_line(8), sub_line(8) + ADD_ASSERT)),
            # main's test_add dropped; the branch's test_add verbatim, but at the end of the file
            ("branch-test-at-end", moved_to_end(once(theirs, MAIN_ADD, BRANCH_ADD), BRANCH_ADD)),
        ):
            with self.subTest(resolution=name):
                p = self.merge_main()
                self.assertNotEqual(p.returncode, 0, "the merge must conflict")
                self.put("tests/test_calc.py", resolved)
                self.git(self.wt, "add", "tests/test_calc.py")
                self.git(self.wt, "commit", "-q", "--no-edit")
                self.assertEqual(len(self.git(self.wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
                self.assertEqual(self.current(), "current: no")
                self.git(self.wt, "reset", "-q", "--hard", before)

    # main's edits close to the branch's hunk: git conflicts on the line right next to it, so two lines away
    def test_status_stays_current_across_merges_of_main_close_to_the_branchs_hunk(self):
        self.red_ticket_with_source()
        main_path = os.path.join(self.repo, "tests", "test_calc.py")
        for name, change in (
            ("assert-two-lines-after", lambda t: once(t, sub_line(5), sub_line(5).replace("(7, 2), 5)", "(8, 3), 5)"))),
            ("assert-two-lines-before", lambda t: once(t, sub_line(4), sub_line(4).replace("(6, 2), 4)", "(9, 5), 4)"))),
        ):
            with self.subTest(main=name):
                self.on_main({"tests/test_calc.py": change})
                self.merge_cleanly()
                self.assertEqual(read(os.path.join(self.wt, "tests", "test_calc.py")),
                                 once(read(main_path), SUB_5, BRANCH_ADD + SUB_5))
                self.assertEqual(self.current(), "current: yes")

    def test_status_stays_current_across_merges_of_main_close_to_the_hunk_with_repeated_context(self):
        self.red_ticket_with_source(REPEATED_SUITE, lambda t: class_at(t, 5, ADD_CASE))
        main_path = os.path.join(self.repo, "tests", "test_calc.py")
        for name, change in (
            ("class-after-the-gap", lambda t: class_replaced(t, 5, case_with(5))),
            ("class-before-the-gap", lambda t: class_replaced(t, 4, case_with(4))),
        ):
            with self.subTest(main=name):
                self.on_main({"tests/test_calc.py": change})
                self.merge_cleanly()
                self.assertEqual(read(os.path.join(self.wt, "tests", "test_calc.py")),
                                 class_at(read(main_path), 5, ADD_CASE))
                self.assertEqual(self.current(), "current: yes")

    def repeated_red_ticket(self):
        """The repeated-context ticket after a clean merge of main that edits the last class and appends one;
        returns the merge commit."""
        self.red_ticket_with_source(REPEATED_SUITE, lambda t: class_at(t, 5, ADD_CASE))
        more = "class MoreTest(unittest.TestCase):\n    def test_more(self):\n        self.assertEqual(calc.sub(9, 1), 8)"
        last = LAST_CASE.replace("calc.sub(9, 9), 0)", "calc.sub(8, 8), 0)")
        self.on_main({"tests/test_calc.py": lambda t: suite(suite_classes(class_replaced(t, -1, last)) + [more])})
        self.merge_cleanly()
        self.assertEqual(read(os.path.join(self.wt, "tests", "test_calc.py")),
                         class_at(read(os.path.join(self.repo, "tests", "test_calc.py")), 5, ADD_CASE))
        self.assertEqual(self.current(), "current: yes")
        return self.git(self.wt, "rev-parse", "HEAD")

    def test_status_stays_current_across_merges_of_main_with_repeated_context(self):
        self.repeated_red_ticket()
        more = "class MoreTest(unittest.TestCase):\n    def test_more(self):\n        self.assertEqual(calc.sub(7, 1), 6)"
        self.on_main({"tests/test_calc.py": lambda t: class_replaced(t, -1, more)}, "main moves again")
        self.merge_cleanly()
        self.assertEqual(self.current(), "current: yes")

    def test_a_branch_moving_its_test_to_another_gap_is_not_current(self):
        merged = self.repeated_red_ticket()
        path = os.path.join(self.wt, "tests", "test_calc.py")
        for gap in (8, 2, 0, 6):
            for committed in (False, True):
                with self.subTest(gap=gap, committed=committed):
                    self.put("tests/test_calc.py", class_moved(read(path), ADD_CASE, gap))
                    if committed:
                        self.git(self.wt, "commit", "-q", "-am", "the branch moves its test")
                    self.assertEqual(self.current(), "current: no")
                    if committed:
                        self.git(self.wt, "reset", "-q", "--hard", merged)
                    else:
                        self.git(self.wt, "checkout", "--", "tests/test_calc.py")
                    self.assertEqual(self.current(), "current: yes")

    def test_a_conflict_resolution_moving_the_branchs_test_to_another_gap_is_not_current(self):
        before = self.repeated_red_ticket()
        self.on_main({"tests/test_calc.py": lambda t: class_at(t, 5, MAIN_ADD_CASE)}, "main adds its own AddTest")
        theirs = read(os.path.join(self.repo, "tests", "test_calc.py"))
        without_mains = suite([c for c in suite_classes(theirs) if c != MAIN_ADD_CASE])
        for name, resolved in (
            ("branch-test-later", class_at(theirs, 9, ADD_CASE)),          # main's class in gap 5, the branch's later
            ("branch-test-earlier", class_at(theirs, 2, ADD_CASE)),        # ... or earlier
            ("mains-dropped-branch-later", class_at(without_mains, 8, ADD_CASE)),
        ):
            with self.subTest(resolution=name):
                p = self.merge_main()
                self.assertNotEqual(p.returncode, 0, "the merge must conflict")
                self.put("tests/test_calc.py", resolved)
                self.git(self.wt, "add", "tests/test_calc.py")
                self.git(self.wt, "commit", "-q", "--no-edit")
                self.assertEqual(len(self.git(self.wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
                self.assertEqual(self.current(), "current: no")
                self.git(self.wt, "reset", "-q", "--hard", before)

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


@unittest.skipUnless(GIT, "git is not installed")
class NoTests(unittest.TestCase):
    """A ticket whose cache (<state>/tickets/<N>.json, as harness-board writes it) carries `no-tests` needs no red
    run: check and status say so and exit 0, and nothing is recorded. Every other ticket is unchanged. Same
    fixture as RedTest (borrowed, so its tests are not run again here)."""
    setUp = RedTest.setUp
    git = RedTest.git
    red = RedTest.red
    put = RedTest.put
    record = RedTest.record
    assertOneLineError = RedTest.assertOneLineError

    def cache(self, number=12, flags=("no-tests",)):
        write(os.path.join(self.state, "tickets", f"{number}.json"),
              json.dumps({"number": number, "title": "A ticket", "flags": list(flags),
                          "labels": ["task", "status:in-progress"] + list(flags)}))

    def second_worktree(self, number, slug):
        wt = os.path.join(self.repo, ".claude", "worktrees", f"t-{number}-{slug}")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", f"t/{number}-{slug}", "origin/main")
        return wt

    def message(self, number):
        return f"ticket {number} is no-tests: no red run needed"

    def test_check_on_a_no_tests_ticket_exits_zero_and_records_nothing(self):
        self.cache(12)
        self.put("docs/guide.md", "# Guide\n\nA docs-only change.\n")      # what such a ticket changes
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(self.message(12), p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(os.path.exists(self.record_path))
        self.assertFalse(os.path.isdir(os.path.dirname(self.record_path)))

        # The number in the message is the ticket's, not a constant: another ticket, another worktree.
        self.cache(47)
        wt = self.second_worktree(47, "other-docs")
        p = self.red("check", cwd=wt)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(self.message(47), p.stdout)
        self.assertNotIn("ticket 12", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.state, "red", "t-47-other-docs.json")))

        p = self.red("check", "--worktree", wt, cwd=self.tmp)
        self.assertEqual((p.returncode, self.message(47) in p.stdout), (0, True), p.stdout + p.stderr)

    def test_check_on_a_no_tests_ticket_does_not_run_the_test_command(self):
        # Tests that would prove red are on the branch, yet a no-tests ticket neither runs them nor records them.
        self.cache(12)
        marker = os.path.join(self.tmp, "ran.marker")
        script = write(os.path.join(self.tmp, "mark.py"), f"open({marker!r}, 'w').close()\n")
        env = {"HARNESS_TEST_CMD": f"python3 {shlex.quote(script)} {{files}}"}
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check", **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(self.message(12), p.stdout)
        self.assertFalse(os.path.exists(marker), "the test command ran on a no-tests ticket")
        self.assertFalse(os.path.exists(self.record_path))

        # An older record is left exactly as it was.
        write(self.record_path, '{"branch": "t/12-slug", "kept": true}\n')
        self.assertEqual(self.red("check", **env).returncode, 0)
        self.assertEqual(read(self.record_path), '{"branch": "t/12-slug", "kept": true}\n')

    def test_status_on_a_no_tests_ticket_says_the_same(self):
        self.cache(12)
        p = self.red("status")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(self.message(12), p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(os.path.exists(self.record_path))

        self.cache(47)
        wt = self.second_worktree(47, "other-docs")
        p = self.red("status", "--worktree", wt, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(self.message(47), p.stdout)

    def test_json_on_a_no_tests_ticket_prints_one_json_object_and_records_nothing(self):
        # --json keeps its contract: one parseable object, not the plain line.
        for sub in ("check", "status"):
            with self.subTest(sub=sub, ticket=12):
                self.cache(12)
                self.put("docs/guide.md", "# Guide\n\nA docs-only change.\n")
                p = self.red(sub, "--json")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(json.loads(p.stdout),
                                 {"no_tests": True, "ticket": 12, "message": self.message(12)})
                self.assertIsInstance(json.loads(p.stdout)["ticket"], int)
                self.assertEqual(len(p.stdout.strip().splitlines()), 1, p.stdout)
                self.assertFalse(os.path.exists(self.record_path))
        # Another ticket, another worktree.
        self.cache(47)
        wt = self.second_worktree(47, "other-docs")
        for sub in ("check", "status"):
            with self.subTest(sub=sub, ticket=47):
                p = self.red(sub, "--json", "--worktree", wt, cwd=self.tmp)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(json.loads(p.stdout),
                                 {"no_tests": True, "ticket": 47, "message": self.message(47)})
                self.assertFalse(os.path.exists(os.path.join(self.state, "red", "t-47-other-docs.json")))

    def test_json_on_an_ordinary_ticket_is_not_the_no_tests_object(self):
        self.cache(12, ())
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("no_tests", p.stdout)
        status = self.red("status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertNotIn("no_tests", json.loads(status.stdout))

    def test_a_ticket_without_the_flag_is_unchanged(self):
        # No cache at all, an empty flag list and other flags: today's behavior, record included.
        for flags in (None, (), ("needs-local", "priority:high")):
            with self.subTest(flags=flags):
                if flags is not None:
                    self.cache(12, flags)
                self.assertOneLineError(self.red("check"), "No test changes on this branch")
                self.assertOneLineError(self.red("status"), f"No red run recorded for {BRANCH}")
                self.assertFalse(os.path.exists(self.record_path))
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 1 test files, exit 1")
        self.assertNotIn("no-tests", p.stdout)
        self.assertEqual(set(self.record()), RECORD_KEYS)
        status = self.red("status")
        self.assertIn("kind: assertion (exit 1)", status.stdout)
        self.assertNotIn("no-tests", status.stdout)

    def test_another_tickets_no_tests_flag_does_not_exempt_this_one(self):
        self.cache(13)                                          # t/12-slug is the branch under test
        self.assertOneLineError(self.red("check"), "No test changes on this branch")
        self.assertOneLineError(self.red("status"), f"No red run recorded for {BRANCH}")
        self.put("tests/test_calc.py", TEST_ADD)
        p = self.red("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 1 test files, exit 1")
        self.assertTrue(os.path.isfile(self.record_path))


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
