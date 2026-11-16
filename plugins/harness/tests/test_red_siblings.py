"""Tests for harness-red and the test modules' own support modules (ticket #346).

    python3 -m unittest discover -s plugins/harness/tests -p test_red_siblings.py -v

A test module that imports a sibling (`tests/x_support.py` beside `tests/test_x.py`) must be run with its directory
on the import path, as `unittest discover -s <dir>` does. With no HARNESS_TEST_CMD, `harness-red check` runs the
detected default on the test files, and that run must reach the tests and report their real failures. A run whose
failure is only that a module that exists in the worktree could not be imported proves nothing about the change and
is never recorded as red; a module that does not exist yet (the code under test, the usual first red) still is.

Every case builds, with real git under a temporary folder, a bare "origin" and a clone whose `main` has calc.py (its
`add` not built), a package `pkg` (pkg/mod.py and the subpackage pkg/sub) and no settings at all, so the detected
default applies; each case gets its own ticket branch t/<n>-case in a linked worktree (a case that needs source of
its own commits it first and runs check and run with --base at that commit). HOME is temporary, so the
records land under it. `python3` on PATH runs the interpreter running these tests; the environment carries no
HARNESS_* and no PYTHONPATH of the session's own. This module keeps off test_red.py and imports nothing beside it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
TOOL = os.path.join(BIN, "harness-red")
GIT = shutil.which("git")

CALC = "def add(a, b):\n    return None\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_BUILT = CALC.replace("return None", "return a + b")
MAIN_FILES = {"calc.py": CALC, "README.md": "# calc\n", "pkg/__init__.py": "", "pkg/mod.py": "VALUE = 1\n",
              "pkg/sub/__init__.py": ""}
REFUSED = "refused"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def mark():
    """A value no implementation can know: it reaches the output only when the support module was imported."""
    return f"support-{uuid.uuid4().hex[:10]}"


def new_dir(prefix):
    """A test directory name no implementation can know (a valid identifier: unittest imports it as a package)."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def path_dirs(cmd):
    """The directories a command's `PYTHONPATH=<dirs>` names, normalised; [] when it sets none."""
    found = re.search(r"PYTHONPATH=((?:'[^']*'|\"[^\"]*\"|[^\s$'\"])+)", cmd)
    if not found:
        return []
    return [os.path.normpath(d) for d in "".join(shlex.split(found.group(1))).split(os.pathsep) if d]


def module_text(imports, mark_expr, a, b):
    """A unittest module that imports calc and `imports`, and fails on calc.add(a, b) with `mark_expr` as the
    message: `None != <a + b> : <mark>` while add is not built."""
    return ("import unittest\n\nimport calc\n" + "".join(f"{line}\n" for line in imports) +
            "\n\nclass AddTest(unittest.TestCase):\n"
            f"    def test_add(self):\n        self.assertEqual(calc.add({a}, {b}), {a + b}, {mark_expr})\n")


def unittest_import_error(test_name, missing):
    """What `python3 -m unittest` prints for a test module whose import fails on `No module named '<missing>'`."""
    rule, thin = "=" * 70, "-" * 70
    return (f"E\n{rule}\nERROR: {test_name} (unittest.loader._FailedTest.{test_name})\n{thin}\n"
            f"ImportError: Failed to import test module: {test_name}\n"
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3.12/unittest/loader.py", line 137, in loadTestsFromName\n'
            "    module = __import__(module_name)\n"
            f'  File "/w/tests/{test_name}.py", line 3, in <module>\n'
            f"    import {missing}\n"
            f"ModuleNotFoundError: No module named '{missing}'\n\n\n{thin}\n"
            "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")


def pytest_import_error(test_path, missing):
    """What pytest prints when collecting a test module whose import fails (exit 2)."""
    return ("collected 0 items / 1 error\n\n=================== ERRORS ===================\n"
            f"______________________ ERROR collecting {test_path} ______________________\n"
            f"ImportError while importing test module '/w/{test_path}'.\n"
            "Hint: make sure your test modules/packages have valid Python names.\n"
            f"Traceback:\n{test_path}:3: in <module>\n    import {missing}\n"
            f"E   ModuleNotFoundError: No module named '{missing}'\n"
            "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
            "=============================== 1 error in 0.05s ===============================\n")


def unittest_mixed(missing):
    """`python3 -m unittest tests/test_x.py tests/test_y.py`: test_x fails to import on `No module named
    '<missing>'`, test_y runs and fails on a real assertion."""
    rule, thin = "=" * 70, "-" * 70
    return (f"EF\n{rule}\nERROR: test_x (unittest.loader._FailedTest.test_x)\n{thin}\n"
            "ImportError: Failed to import test module: test_x\nTraceback (most recent call last):\n"
            '  File "/w/tests/test_x.py", line 3, in <module>\n'
            f"    import {missing}\nModuleNotFoundError: No module named '{missing}'\n\n"
            f"{rule}\nFAIL: test_add (tests.test_y.AddTest.test_add)\n{thin}\nTraceback (most recent call last):\n"
            '  File "/w/tests/test_y.py", line 8, in test_add\n'
            "    self.assertEqual(calc.add(2, 3), 5)\nAssertionError: None != 5\n\n"
            f"{thin}\nRan 2 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n")


# The module under test exists, the name it is asked for does not: the usual first red, never refused.
CANNOT_IMPORT_NAME = ("E\n" + "=" * 70 + "\nERROR: test_x (unittest.loader._FailedTest.test_x)\n" + "-" * 70 + "\n"
                      "ImportError: Failed to import test module: test_x\nTraceback (most recent call last):\n"
                      '  File "/w/tests/test_x.py", line 3, in <module>\n    from calc import mul\n'
                      "ImportError: cannot import name 'mul' from 'calc' (/w/calc.py)\n\n\n" + "-" * 70 + "\n"
                      "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")


@unittest.skipUnless(GIT, "git is not installed")
class SiblingImports(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-red-siblings-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.home)
        python3 = write(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(python3, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_", "CLAUDE_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(HOME=self.home, PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        self.env = env

        origin, self.repo = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "repo")
        self.git(self.tmp, "init", "--bare", "-q", origin)
        self.git(self.tmp, "clone", "-q", origin, self.repo)
        self.git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        for rel, text in MAIN_FILES.items():
            write(os.path.join(self.repo, rel), text)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "calc and pkg")
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".claude/worktrees/\n")
        key = hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12]
        self.state = os.path.join(self.home, ".claude", "harness", "repos", key)
        self.cases = 11
        self.bases = {}

    # ------------------------------------------------------------ helpers
    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", cwd, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                               *args], env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def red(self, wt, *args, **extra):
        """harness-red in `wt`; check and run get --base for a worktree made with source of its own (worktree)."""
        if wt in self.bases and args and args[0] in ("check", "run"):
            args = (args[0], "--base", self.bases[wt]) + args[1:]
        return subprocess.run([sys.executable, TOOL, *args], cwd=wt, env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=120)

    def worktree(self, files, commit=False, base=None):
        """A fresh ticket worktree with `files` (repo-relative path -> text) written, committed on the branch when
        `commit`. `base` (path -> text): source the branch starts from, committed first on a ref that `red` passes
        as --base, so it is not a change of the branch's. Returns (worktree, record path, side file path)."""
        self.cases += 1
        slug = f"t-{self.cases}-case"
        wt = os.path.join(self.repo, ".claude", "worktrees", slug)
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", f"t/{self.cases}-case", "origin/main")
        if base:
            for rel, text in base.items():
                write(os.path.join(wt, rel), text)
            self.git(wt, "add", "-A")
            self.git(wt, "commit", "-q", "-m", "the source the branch starts from")
            self.bases[wt] = f"base-{self.cases}"
            self.git(wt, "branch", self.bases[wt])
        for rel, text in files.items():
            write(os.path.join(wt, rel), text)
        if commit:
            self.git(wt, "add", "-A")
            self.git(wt, "commit", "-q", "-m", "tests")
        state = self.state
        return wt, os.path.join(state, "red", slug + ".json"), os.path.join(state, "red-files", slug + ".json")

    def fake_runner(self, output, code):
        """Environment for a test command (outside every worktree) that prints `output` and exits `code`."""
        script = write(os.path.join(self.tmp, "runner.py"),
                       "import os, sys\nsys.stdout.write(os.environ['RUNNER_OUTPUT'])\n"
                       "sys.exit(int(os.environ['RUNNER_EXIT']))\n")
        return {"HARNESS_TEST_CMD": f"python3 {shlex.quote(script)} {{files}}", "RUNNER_OUTPUT": output,
                "RUNNER_EXIT": str(code)}

    @staticmethod
    def load(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def assertRefused(self, p, record, side, said):
        """Refused like every run that proves nothing: exit 1, one line on stderr, nothing recorded."""
        self.assertEqual(p.returncode, 1, said)
        self.assertNotIn("Traceback", p.stderr, said)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, said)
        self.assertTrue(p.stderr.startswith("harness-red: "), said)
        self.assertFalse(p.stdout.startswith("red"), said)
        self.assertNotIn('"kind"', p.stdout, said)
        self.assertFalse(os.path.exists(record), f"recorded as red: {said}")
        self.assertFalse(os.path.exists(side), f"side file written: {said}")

    # ------------------------------------------------------------ DoD 1: the detected default reaches the tests
    def test_the_default_command_imports_sibling_support_modules_and_reports_the_real_failures(self):
        ext = os.path.join(self.tmp, "extlib")                        # outside the worktree, on an inherited PYTHONPATH
        cases = []
        m = mark()
        cases.append(("a sibling module, untracked", False, {}, {
            "tests/test_x.py": module_text(["import x_support"], "x_support.MARK", 2, 3),
            "tests/x_support.py": f"MARK = {m!r}\n"}, [(m, 5)]))
        m = mark()
        cases.append(("a sibling module, committed on the branch", True, {}, {
            "tests/test_x.py": module_text(["import x_support"], "x_support.MARK", 4, 4),
            "tests/x_support.py": f"MARK = {m!r}\n"}, [(m, 8)]))
        m, d = mark(), new_dir("unit")
        cases.append(("a sibling in a nested test directory", False, {}, {
            f"tests/{d}/test_y.py": module_text(["import y_support"], "y_support.MARK", 6, 1),
            f"tests/{d}/y_support.py": f"MARK = {m!r}\n"}, [(m, 7)]))
        m, n, d = mark(), mark(), new_dir("models")
        cases.append(("two test directories in one run, each with its own sibling", False, {}, {
            "tests/test_x.py": module_text(["import x_support"], "x_support.MARK", 1, 2),
            "tests/x_support.py": f"MARK = {m!r}\n",
            f"spec/{d}/test_m.py": module_text(["import m_support"], "m_support.MARK", 10, 20),
            f"spec/{d}/m_support.py": f"MARK = {n!r}\n"}, [(m, 3), (n, 30)]))
        m = mark()
        cases.append(("a sibling package", False, {}, {
            "tests/test_x.py": module_text(["from helpers.db import MARK"], "MARK", 3, 9),
            "tests/helpers/__init__.py": "",
            "tests/helpers/db.py": f"MARK = {m!r}\n"}, [(m, 12)]))
        m, n = mark(), mark()
        write(os.path.join(ext, "ext_lib.py"), f"EXT = {n!r}\n")
        cases.append(("an inherited PYTHONPATH still reaches the tests", False, {"PYTHONPATH": ext}, {
            "tests/test_x.py": module_text(["import ext_lib", "import x_support"],
                                           "x_support.MARK + ' ' + ext_lib.EXT", 5, 6),
            "tests/x_support.py": f"MARK = {m!r}\n"}, [(f"{m} {n}", 11)]))

        for name, commit, extra, files, failures in cases:
            with self.subTest(name):
                wt, record, _ = self.worktree(files, commit)
                tests = sorted(files)
                run_files = sorted(f for f in files if os.path.basename(f).startswith("test_"))
                p = self.red(wt, "check", **extra)
                said = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, said)
                self.assertEqual(p.stdout.splitlines()[0], f"red (assertion): {len(tests)} test files, exit 1", said)
                rec = self.load(record)
                self.assertEqual((rec["kind"], rec["exit"]), ("assertion", 1), said)
                self.assertEqual((rec["test_files"], rec["run_files"]), (tests, run_files))
                self.assertNotIn("No module named", rec["tail"], said)
                for value, want in failures:                   # each test ran, with its sibling imported
                    self.assertIn(f"None != {want} : {value}", rec["tail"], said)
                self.assertIn("-m unittest", rec["cmd"])
                for f in run_files:
                    self.assertIn(f, rec["cmd"])

                # The same command serves `run`: once add is built, the branch's own tests pass.
                write(os.path.join(wt, "calc.py"), CALC_BUILT)
                p = self.red(wt, "run", **extra)
                said = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, said)
                self.assertIn(f"own tests: {len(run_files)} files, exit 0", p.stdout.splitlines(), said)
                self.assertNotIn("No module named", said)

    def test_the_default_command_puts_only_the_test_files_directories_on_the_path(self):
        # A directory that holds no test file being run stays off the import path: a module there is not the
        # tests' sibling, and a module there named like the standard library's must not shadow it.
        m, scripts = mark(), new_dir("scripts")
        wt, record, _ = self.worktree(
            {"tests/test_x.py": module_text(["import x_support"], "x_support.MARK", 2, 3)},
            base={f"{scripts}/x_support.py": f"MARK = {m!r}\n"})
        with self.subTest("a module of that name only in a directory without tests"):
            p = self.red(wt, "check")
            said = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, said)              # not beside the test, not at the root: a valid red
            self.assertEqual(p.stdout.splitlines()[0], "red (import): 1 test files, exit 1", said)
            rec = self.load(record)
            self.assertEqual(rec["kind"], "import", said)
            self.assertIn("No module named 'x_support'", rec["tail"], said)
            self.assertNotIn(m, rec["tail"], said)               # the module elsewhere was never imported

        m, tools = mark(), new_dir("tools")
        wt, record, _ = self.worktree(
            {"tests/test_x.py": module_text(["import fractions", "import x_support"],
                                            "str(fractions.Fraction(7, 2)) + ' ' + x_support.MARK", 4, 5),
             "tests/x_support.py": f"MARK = {m!r}\n"},
            base={f"{tools}/fractions.py": "raise RuntimeError('a directory without tests is on the path')\n"})
        with self.subTest("a standard library name in a directory without tests"):
            p = self.red(wt, "check")
            said = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, said)
            self.assertEqual(p.stdout.splitlines()[0], "red (assertion): 2 test files, exit 1", said)
            rec = self.load(record)
            self.assertEqual(rec["kind"], "assertion", said)
            self.assertIn(f"None != 9 : 7/2 {m}", rec["tail"], said)     # the real fractions, and the sibling
            self.assertNotIn("a directory without tests is on the path", rec["tail"], said)
            write(os.path.join(wt, "calc.py"), CALC_BUILT)
            p = self.red(wt, "run")
            said = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, said)
            self.assertIn("own tests: 1 files, exit 0", p.stdout.splitlines(), said)

    def test_the_default_command_never_puts_a_package_directory_on_the_path(self):
        # The directory put on the path for a run file is the first one upward without an `__init__.py` (pytest's
        # rootdir rule). A package directory's modules would shadow the standard library (`pkg/json.py` over json)
        # and are reachable by their package path anyway; a test in a package under tests/ gets tests/ itself, so
        # a support module in that parent directory imports by its bare name (PR #371 review).
        m, n, d = mark(), mark(), new_dir("unit")
        shadow = "raise RuntimeError('the package directory is on the path')\n"
        cases = [
            # (name, the branch's files, source the branch starts from, failures, directories never on the path)
            ("a test in a package at the root: the standard library and a sibling by package path",
             {"pkg/test_x.py": module_text(["import json", "from pkg import mod, x_support"],
                                           "json.dumps([mod.VALUE]) + ' ' + x_support.MARK", 2, 3)},
             {"pkg/json.py": shadow, "pkg/x_support.py": f"MARK = {m!r}\n"}, [(f"[1] {m}", 5)], ["pkg"]),
            ("a test in a package under tests/: a support module of the parent directory",
             {f"tests/{d}/__init__.py": "",
              f"tests/{d}/test_y.py": module_text(["import x_support"], "x_support.MARK", 4, 6)},
             {"tests/x_support.py": f"MARK = {n!r}\n"}, [(n, 10)], [f"tests/{d}"]),
        ]
        for name, files, base, failures, never in cases:
            with self.subTest(name):
                wt, record, _ = self.worktree(files, False, base)
                tests = sorted(files)
                run_files = sorted(f for f in files if os.path.basename(f).startswith("test_"))
                p = self.red(wt, "check")
                said = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, said)
                self.assertEqual(p.stdout.splitlines()[0], f"red (assertion): {len(tests)} test files, exit 1", said)
                rec = self.load(record)
                self.assertEqual((rec["kind"], rec["exit"]), ("assertion", 1), said)
                self.assertEqual((rec["test_files"], rec["run_files"]), (tests, run_files))
                self.assertNotIn("No module named", rec["tail"], said)
                self.assertNotIn("the package directory is on the path", rec["tail"], said)
                for value, want in failures:                   # the real standard library, the support module
                    self.assertIn(f"None != {want} : {value}", rec["tail"], said)
                self.assertIn("-m unittest", rec["cmd"])
                for f in run_files:
                    self.assertIn(f, rec["cmd"])
                for directory in never:
                    self.assertNotIn(os.path.normpath(directory), path_dirs(rec["cmd"]), rec["cmd"])

                write(os.path.join(wt, "calc.py"), CALC_BUILT)
                p = self.red(wt, "run")
                said = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, said)
                self.assertIn(f"own tests: {len(run_files)} files, exit 0", p.stdout.splitlines(), said)
                self.assertNotIn("the package directory is on the path", said)

    # ------------------------------------------------------------ DoD 2: never red on a module the worktree has
    def test_a_real_run_failing_on_an_existing_sibling_is_never_recorded_as_red(self):
        # The reported case, with a command the user set (so no default of harness-red's own applies): plain
        # `python3 -m unittest` cannot import the sibling. Refused, or, when harness-red puts the test directories
        # on the path for every command, red on the real assertions; never a red whose failure is that import,
        # even when another test file of the run fails on a real assertion (that file's tests never ran).
        m, n = mark(), mark()
        sibling = {"tests/test_x.py": module_text(["import x_support"], "x_support.MARK", 2, 3),
                   "tests/x_support.py": f"MARK = {m!r}\n"}
        mixed = dict(sibling, **{"tests/test_y.py": module_text([], repr(n), 4, 4)})
        on_base = {"tests/x_support.py": sibling["tests/x_support.py"]}        # #337: pr_support.py was on main
        o, sd = mark(), new_dir("models")                                      # a test directory not named tests/
        for name, files, commit, base, failures in (
                ("a sibling, untracked", sibling, False, None, [(m, 5)]),
                ("a sibling, committed on the branch", sibling, True, None, [(m, 5)]),
                ("a sibling on the base, the branch adds the test", {"tests/test_x.py": sibling["tests/test_x.py"]},
                 False, on_base, [(m, 5)]),
                ("a sibling under spec/ on the base, the branch adds the test",
                 {f"spec/{sd}/test_x.py": module_text(["import x_support"], "x_support.MARK", 10, 20)}, False,
                 {f"spec/{sd}/x_support.py": f"MARK = {o!r}\n"}, [(o, 30)]),
                ("a sibling in one file, a real assertion in another", mixed, False, None, [(m, 5), (n, 8)])):
            with self.subTest(name):
                wt, record, side = self.worktree(files, commit, base)
                p = self.red(wt, "check", HARNESS_TEST_CMD="python3 -m unittest {files}")
                said = p.stdout + p.stderr
                if os.path.exists(record):
                    rec = self.load(record)
                    self.assertNotIn("No module named 'x_support'", rec["tail"], said)
                    self.assertEqual(rec["kind"], "assertion", said)
                    for value, want in failures:
                        self.assertIn(f"None != {want} : {value}", rec["tail"], said)
                    self.assertEqual(p.returncode, 0, said)
                else:
                    self.assertRefused(p, record, side, said)

    def test_a_missing_module_is_judged_by_whether_the_worktree_has_it(self):
        # The run's output names the module it could not import; the worktree's files decide. A module that exists
        # there by its whole dotted path, beside a test file or at the repository root (a module or a package),
        # proves nothing, even beside another file's real assertion failure; one that does not exist there yet is
        # the usual first red, wherever else a file of its last part's name lies.
        x_support = unittest_import_error("test_x", "x_support")
        d, scripts, other, md = new_dir("unit"), new_dir("scripts"), new_dir("other"), new_dir("models")
        test_y = module_text([], "'y'", 2, 3)
        cases = [
            # (name, test file, other files, committed, output, exit, verdict[, source the branch starts from])
            ("a sibling module, untracked", "tests/test_x.py", {"tests/x_support.py": "MARK = 1\n"}, False,
             x_support, 1, REFUSED),
            ("a sibling module, committed on the branch", "tests/test_x.py", {"tests/x_support.py": "MARK = 1\n"},
             True, x_support, 1, REFUSED),
            ("a sibling in a nested test directory", f"tests/{d}/test_y.py", {f"tests/{d}/y_support.py": "M = 1\n"},
             False, unittest_import_error("test_y", "y_support"), 1, REFUSED),
            ("a sibling package", "tests/test_x.py", {"tests/helpers/__init__.py": ""}, False,
             unittest_import_error("test_x", "helpers"), 1, REFUSED),
            ("dotted: a sibling package's module", "tests/test_x.py",
             {"tests/helpers/__init__.py": "", "tests/helpers/db.py": "M = 1\n"}, False,
             unittest_import_error("test_x", "helpers.db"), 1, REFUSED),
            ("a module at the repository root", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "calc"), 1, REFUSED),
            ("dotted: a module of a package at the root", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "pkg.mod"), 1, REFUSED),
            ("dotted: a subpackage of a package at the root", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "pkg.sub"), 1, REFUSED),
            ("pytest's collection error on a sibling", "tests/test_x.py", {"tests/x_support.py": "MARK = 1\n"},
             False, pytest_import_error("tests/test_x.py", "x_support"), 2, REFUSED),
            # the test directory is not tests/: the run files' own directories decide, not a directory name
            ("a sibling under spec/, untracked", f"spec/{md}/test_m.py", {f"spec/{md}/m_support.py": "M = 1\n"},
             False, unittest_import_error("test_m", "m_support"), 1, REFUSED),
            ("a sibling under spec/ on the base, the branch adds the test", f"spec/{md}/test_m.py", {}, False,
             unittest_import_error("test_m", "m_support"), 1, REFUSED, {f"spec/{md}/m_support.py": "M = 1\n"}),
            # the sibling is already on main and the branch adds only the test (the reported bug, #337): the
            # worktree has it just the same, whether or not the branch changed it
            ("a sibling module on the base, the branch adds the test", "tests/test_x.py", {}, False, x_support, 1,
             REFUSED, {"tests/x_support.py": "MARK = 1\n"}),
            ("a sibling in a nested test directory on the base", f"tests/{d}/test_y.py", {}, False,
             unittest_import_error("test_y", "y_support"), 1, REFUSED, {f"tests/{d}/y_support.py": "M = 1\n"}),
            ("dotted: a sibling package's module on the base", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "helpers.db"), 1, REFUSED,
             {"tests/helpers/__init__.py": "", "tests/helpers/db.py": "M = 1\n"}),
            # a module in an ancestor directory of the run file, between it and the root (PR #371 review): the
            # worktree has it, whether or not the test's own directory is a package
            ("a module in the parent tests directory, the test in a package", f"tests/{d}/test_y.py",
             {f"tests/{d}/__init__.py": "", "tests/y_support.py": "M = 1\n"}, False,
             unittest_import_error("test_y", "y_support"), 1, REFUSED),
            ("a module in the parent tests directory, the test in a plain directory", f"tests/{d}/test_y.py",
             {"tests/y_support.py": "M = 1\n"}, False, unittest_import_error("test_y", "y_support"), 1, REFUSED),
            ("a module in the parent tests directory on the base, the test in a package", f"tests/{d}/test_y.py",
             {f"tests/{d}/__init__.py": ""}, False, unittest_import_error("test_y", "y_support"), 1, REFUSED,
             {"tests/y_support.py": "M = 1\n"}),
            # ruling: a bare-name sibling inside a package directory (pkg/x_support.py beside pkg/test_x.py, which
            # the right command reaches only as pkg.x_support) exists in the worktree, so the run proves nothing
            ("a sibling inside a package directory at the root, by its bare name", "pkg/test_x.py", {}, False,
             x_support, 1, REFUSED, {"pkg/x_support.py": "MARK = 1\n"}),
            ("the module under test, not written yet", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "slugify_text"), 1, "import"),
            ("the sibling's name, but no such file", "tests/test_x.py", {}, False, x_support, 1, "import"),
            ("another sibling exists, not the one named", "tests/test_x.py", {"tests/x_support.py": "MARK = 1\n"},
             False, unittest_import_error("test_x", "y_support"), 1, "import"),
            ("dotted: a new module of a package at the root", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "pkg.newmod"), 1, "import"),
            ("dotted: a new module of a subpackage at the root", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "pkg.sub.newmod"), 1, "import"),
            ("dotted: a new module of a sibling package", "tests/test_x.py", {"tests/helpers/__init__.py": ""},
             False, unittest_import_error("test_x", "helpers.newmod"), 1, "import"),
            ("a file of that name that is no module", "tests/test_x.py", {"tests/slugify_text.json": "{}\n"}, False,
             unittest_import_error("test_x", "slugify_text"), 1, "import"),
            ("pytest's collection error on the module under test", "tests/test_x.py", {}, False,
             pytest_import_error("tests/test_x.py", "slugify_text"), 2, "import"),
            ("a name the existing module lacks", "tests/test_x.py", {}, False, CANNOT_IMPORT_NAME, 1, "import"),
            # the whole dotted path decides, and where it lies: a file of the last part's name elsewhere is not it
            ("dotted: only a module of its last part's name, beside the test", "tests/test_x.py",
             {"tests/db.py": "M = 1\n"}, False, unittest_import_error("test_x", "helpers.db"), 1, "import"),
            ("dotted: a new module of a root package, its name beside the test", "tests/test_x.py",
             {"tests/newmod.py": "M = 1\n"}, False, unittest_import_error("test_x", "pkg.newmod"), 1, "import"),
            ("dotted: a new module of a root package, its name a package elsewhere", "tests/test_x.py", {}, False,
             unittest_import_error("test_x", "pkg.newmod"), 1, "import", {f"{other}/newmod/__init__.py": ""}),
            ("the sibling's name only in a directory without tests", "tests/test_x.py", {}, False, x_support, 1,
             "import", {f"{scripts}/x_support.py": "MARK = 1\n"}),
            # ruling: an existing module that failed to load voids the run, whatever else failed in it
            ("an existing sibling missing in one file, a real assertion in another", "tests/test_x.py",
             {"tests/test_y.py": test_y, "tests/x_support.py": "MARK = 1\n"}, False, unittest_mixed("x_support"), 1,
             REFUSED),
            ("a module not written yet missing in one file, a real assertion in another", "tests/test_x.py",
             {"tests/test_y.py": test_y}, False, unittest_mixed("slugify_text"), 1, "import"),
        ]
        body = module_text(["import x_support"], "x_support.MARK", 2, 3)   # never run: the runner is canned
        for name, test_file, others, commit, output, code, verdict, *base in cases:
            with self.subTest(name):
                files = dict(others, **{test_file: body})
                wt, record, side = self.worktree(files, commit, base[0] if base else None)
                for args in ((), ("--json",)):
                    p = self.red(wt, "check", *args, **self.fake_runner(output, code))
                    said = f"{name} {args}: {p.stdout}{p.stderr}"
                    if verdict == REFUSED:
                        self.assertRefused(p, record, side, said)
                        continue
                    self.assertEqual(p.returncode, 0, said)
                    rec = self.load(record)
                    self.assertEqual((rec["kind"], rec["exit"]), (verdict, code), said)
                    self.assertEqual(rec["test_files"], sorted(files))
                    if not args:
                        self.assertEqual(p.stdout.splitlines()[0],
                                         f"red ({verdict}): {len(files)} test files, exit {code}", said)
                    else:
                        self.assertEqual(json.loads(p.stdout), rec)
                    self.assertTrue(os.path.exists(side), said)


if __name__ == "__main__":
    unittest.main()
