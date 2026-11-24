"""Tests for #475: scripts/mutate.py, the standard-library mutation runner kept outside the plugin.

    python3 -m unittest discover -s plugins/harness/tests -p test_mutate_script.py

Each test builds a tiny fixture tree in its own temporary directory, laid out like Harness (a git repository with
one commit holding `plugins/harness/lib/harness_lib/`, `plugins/harness/bin/` and `plugins/harness/tests/`), and
runs `python3 scripts/mutate.py run PATH ... --workers 2 [--tests PATTERN] [--json]` as a process with its working
directory at the fixture root. HARNESS_TEST_MAP and PYTHONDONTWRITEBYTECODE are removed from its environment, and
TMPDIR is a scratch directory of the test's own.

The test modules of a fixture live in `plugins/harness/tests` and run under unittest the way the suite runs them
(`python3 -m unittest discover -s plugins/harness/tests -p <module>`). `--tests PATTERN` names the test module(s)
by file name; without it, `lib/harness_lib/<name>.py` and `bin/harness-<name>` map to `test_<name>.py`. Each
calc fixture also holds a `test_other.py` that always fails, so a run that runs more than the mapped or named
module has a failing baseline. The fixture tests find their code through `Path(__file__).resolve()`.

The `--json` shape these tests pin (stdout is that one JSON object):

    {
      "killed": int,        # every mutant whose tests failed or timed out (timeouts included)
      "survived": int,      # every mutant whose tests passed
      "timed_out": int,     # the killed mutants that timed out
      "files": [            # one entry per file with a site, path relative to the repository root, POSIX
        {"path": str, "killed": int, "survived": int, "timed_out": int,
         "functions": {"<function name>": {"killed": int, "survived": int, "timed_out": int}}}
      ],
      "survivors": [        # one entry per surviving mutant
        {"path": str, "line": int, "change": str, "function": str}
      ]
    }

`line` is the 1-based line of the mutated node, `function` the plain name of the function holding it, `change`
names the operator replaced and its replacement (e.g. "* -> /"; only the two tokens are pinned). Exit codes: 0 when
nothing survived, 2 when the baseline fails (no mutant runs), 3 when a mutant survived.

#502: `--changed REF` keeps only the sites on lines that are new or changed in `git diff REF` (a fixture with a
second commit, run with `--changed HEAD~1`); with `--lines` as well, a site must be on both.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest

sys.dont_write_bytecode = True

from gitrepo_support import GIT  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(REPO, "scripts", "mutate.py")
IDENTITY = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
LIB = "plugins/harness/lib/harness_lib"
BIN = "plugins/harness/bin"
TESTS = "plugins/harness/tests"
RUN_TIMEOUT = 150

# The test module header every fixture test uses: its code is found through Path(__file__).resolve().
TEST_HEAD = '''\
from __future__ import annotations
import json, os, subprocess, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
'''

# calc.py: is_big's one site (> to >=) is caught by test_calc; untested, scale and same are never called, so
# their one site each survives. The docstring and the annotations on `same` hold operators and 0/1 literals
# that are not sites.
CALC = '''\
"""Calc: 1 + 1 == 2 and 0 < 1 are words here, not code."""
from __future__ import annotations


def is_big(x: int) -> bool:
    return x > 10


def untested(a, b):
    return a + b


def scale(a):
    return a * 3


def same(a: list[0], b: dict[1, 1]) -> list[1]:
    return a == b
'''

# Probe: with MUTATE_PROBE set, each import of calc by the test appends the real path and the source of the
# module it imported, so a test can see which file a mutant run loaded.
TEST_CALC = TEST_HEAD + '''\
from harness_lib import calc

PROBE = os.environ.get("MUTATE_PROBE")
if PROBE:
    with open(PROBE, "a") as handle:
        handle.write(json.dumps({"file": os.path.realpath(calc.__file__),
                                 "source": Path(calc.__file__).read_text()}) + "\\n")


class CalcTest(unittest.TestCase):
    def test_is_big(self):
        self.assertTrue(calc.is_big(11))
        self.assertFalse(calc.is_big(10))
        self.assertFalse(calc.is_big(3))
'''

TEST_CALC_FAILING = TEST_HEAD + '''\
from harness_lib import calc

PROBE = os.environ.get("MUTATE_PROBE")
if PROBE:
    with open(PROBE, "a") as handle:
        handle.write(json.dumps({"file": os.path.realpath(calc.__file__),
                                 "source": Path(calc.__file__).read_text()}) + "\\n")


class CalcTest(unittest.TestCase):
    def test_is_big(self):
        self.assertTrue(calc.is_big(5))
'''

TEST_OTHER = '''\
import unittest


class OtherTest(unittest.TestCase):
    def test_fails(self):
        self.fail("test_other must not run when another module is named or mapped")
'''

# loop.py: `i = 0` to 1 and `<` to `<=` are caught; `i += 1` to `i += 0` (and `-=`, if += is a site) never ends.
LOOP = '''\
from __future__ import annotations


def count_up(n):
    i = 0
    while i < n:
        i += 1
    return i
'''

TEST_LOOP = TEST_HEAD + '''\
from harness_lib import loop


class LoopTest(unittest.TestCase):
    def test_count_up(self):
        self.assertEqual(loop.count_up(3), 3)
        self.assertEqual(loop.count_up(0), 0)
'''

# A suffix-less bin tool with a python3 shebang, which its test runs as a process through its real path; every
# site in it is caught. The test module and the support module carry functions nobody calls, so a site mutated
# there would survive.
GREET = '''\
#!/usr/bin/env python3
from __future__ import annotations

import sys


def double(x):
    return x + x


if __name__ == "__main__":
    print(double(int(sys.argv[1])))
'''

TEST_GREET = TEST_HEAD + '''\
TOOL = ROOT / "bin" / "harness-greet"


def never_called(a):
    return a * 2 + 1


class GreetTest(unittest.TestCase):
    def test_double(self):
        out = subprocess.run([sys.executable, str(TOOL), "4"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "8")
        out = subprocess.run([sys.executable, str(TOOL), "5"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "10")
'''

UNCALLED = '''\
from __future__ import annotations


def unused(a, b):
    return a == b and a > 1
'''

SHELL_TOOL = '#!/bin/sh\necho $((1 + 1))\n'
NO_SHEBANG = 'x = 1 + 1\nprint(x == 2)\n'


def line_of(source: str, text: str) -> int:
    lines = [n for n, line in enumerate(source.splitlines(), 1) if text in line]
    assert len(lines) == 1, (text, lines)
    return lines[0]


def snapshot(root: str) -> dict:
    """Every file and directory under root but .git, with its bytes and mtime."""
    seen = {}
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not (here == root and d == ".git")]
        for name in dirs:
            seen[os.path.relpath(os.path.join(here, name), root) + "/"] = None
        for name in files:
            path = os.path.join(here, name)
            with open(path, "rb") as handle:
                seen[os.path.relpath(path, root)] = (handle.read(), os.stat(path).st_mtime_ns)
    return seen


class MutateScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mutate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "repo")
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch)
        self.probe = os.path.join(self.tmp, "probe.jsonl")

    def build(self, files: dict) -> None:
        for rel, (text, mode) in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(text)
            os.chmod(path, mode)
        env = dict(os.environ, **IDENTITY)
        for args in (("init", "-q"), ("config", "gc.auto", "0"), ("config", "maintenance.auto", "false"),
                     ("add", "-A"), ("commit", "-q", "-m", "fixture")):
            subprocess.run([GIT, "-C", self.root, *args], check=True, capture_output=True, env=env)

    def calc_tree(self, test_calc: str = TEST_CALC) -> None:
        self.build({f"{LIB}/__init__.py": ("", 0o644), f"{LIB}/calc.py": (CALC, 0o644),
                    f"{TESTS}/test_calc.py": (test_calc, 0o644), f"{TESTS}/test_other.py": (TEST_OTHER, 0o644)})

    def run_mutate(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, TMPDIR=self.scratch, MUTATE_PROBE=self.probe)
        env.pop("HARNESS_TEST_MAP", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        return subprocess.run([sys.executable, SCRIPT, "run", *args, "--workers", "2"], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=RUN_TIMEOUT)

    def report(self, proc: subprocess.CompletedProcess) -> dict:
        try:
            return json.loads(proc.stdout)
        except ValueError:
            self.fail(f"--json printed no JSON object (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")

    def file_entry(self, report: dict, path: str) -> dict:
        entries = [f for f in report["files"] if f["path"] == path]
        self.assertEqual(len(entries), 1, report["files"])
        return entries[0]

    def git_clean(self) -> None:
        status = subprocess.run([GIT, "-C", self.root, "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", "the run left changes in the checkout")

    # ---------------------------------------------------------------------------------------------- DoD 1, 5

    def test_caught_mutant_killed_uncaught_survives_json_lists_survivors(self):
        self.calc_tree()
        before = snapshot(self.root)
        proc = self.run_mutate(f"{LIB}/calc.py", "--tests", "test_calc.py", "--json")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        report = self.report(proc)
        path = f"{LIB}/calc.py"
        expected = {(path, line_of(CALC, "a + b"), "untested"), (path, line_of(CALC, "a * 3"), "scale"),
                    (path, line_of(CALC, "a == b"), "same")}
        got = {(s["path"], s["line"], s["function"]) for s in report["survivors"]}
        self.assertEqual(got, expected, report["survivors"])
        changes = {s["function"]: s["change"] for s in report["survivors"]}
        self.assertIn("+", changes["untested"])
        self.assertIn("*", changes["scale"])
        self.assertIn("/", changes["scale"])
        self.assertIn("==", changes["same"])
        self.assertIn("!=", changes["same"])
        self.assertEqual((report["killed"], report["survived"], report["timed_out"]), (1, 3, 0))
        entry = self.file_entry(report, path)
        self.assertEqual((entry["killed"], entry["survived"]), (1, 3))
        self.assertEqual(entry["functions"]["is_big"]["killed"], 1)
        self.assertEqual(entry["functions"]["is_big"]["survived"], 0)
        for name in ("untested", "scale", "same"):
            self.assertEqual((entry["functions"][name]["killed"], entry["functions"][name]["survived"]), (0, 1))
        self.assertEqual(snapshot(self.root), before)
        self.git_clean()

        # The text report names each survivor's function and file, with the same exit.
        text = self.run_mutate(f"{LIB}/calc.py", "--tests", "test_calc.py")
        self.assertEqual(text.returncode, 3, text.stdout + text.stderr)
        for name in ("untested", "scale", "same", "calc.py"):
            self.assertIn(name, text.stdout)

    def test_timeout_counts_as_killed_and_exit_0_when_nothing_survives(self):
        self.build({f"{LIB}/__init__.py": ("", 0o644), f"{LIB}/loop.py": (LOOP, 0o644),
                    f"{TESTS}/test_loop.py": (TEST_LOOP, 0o644)})
        proc = self.run_mutate(f"{LIB}/loop.py", "--tests", "test_loop.py", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = self.report(proc)
        self.assertEqual(report["survivors"], [])
        self.assertEqual(report["survived"], 0)
        self.assertGreaterEqual(report["timed_out"], 1)
        # `i = 0` and `<` are killed by a failure, the loop's increment by the timeout.
        self.assertGreaterEqual(report["killed"], report["timed_out"] + 2)
        counts = self.file_entry(report, f"{LIB}/loop.py")["functions"]["count_up"]
        self.assertEqual(counts["survived"], 0)
        self.assertGreaterEqual(counts["timed_out"], 1)
        self.git_clean()

    # ---------------------------------------------------------------------------------------------- DoD 2

    def test_shebang_tool_mutated_test_and_support_files_never(self):
        self.build({
            f"{BIN}/harness-greet": (GREET, 0o755),
            f"{BIN}/harness-shell": (SHELL_TOOL, 0o755),
            f"{BIN}/notes": (NO_SHEBANG, 0o644),
            f"{LIB}/test_inline.py": (UNCALLED, 0o644),
            f"{LIB}/inline_support.py": (UNCALLED, 0o644),
            f"{TESTS}/test_greet.py": (TEST_GREET, 0o644),
            f"{TESTS}/greet_support.py": (UNCALLED, 0o644),
            f"{TESTS}/plain.py": (UNCALLED, 0o644),
        })
        # The directory walk, with test_greet.py found by the bin/harness-<name> convention.
        proc = self.run_mutate("plugins/harness", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = self.report(proc)
        # Only harness-greet has mutants: a test or support file mutated would show its uncalled sites surviving.
        self.assertEqual({f["path"] for f in report["files"] if f["killed"] or f["survived"]},
                         {f"{BIN}/harness-greet"})
        self.assertEqual(report["survivors"], [])
        entry = self.file_entry(report, f"{BIN}/harness-greet")
        self.assertEqual(entry["functions"]["double"]["killed"], 1)
        self.assertGreaterEqual(entry["killed"], 3)  # x + x, == "__main__", argv[1]
        self.git_clean()

    def test_lib_module_maps_to_its_own_test_module(self):
        self.calc_tree()
        # No --tests: lib/harness_lib/calc.py runs test_calc.py only (test_other.py would fail the baseline).
        proc = self.run_mutate(f"{LIB}/calc.py", "--json")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        counts = self.file_entry(self.report(proc), f"{LIB}/calc.py")["functions"]
        self.assertEqual((counts["is_big"]["killed"], counts["is_big"]["survived"]), (1, 0))

    # ---------------------------------------------------------------------------------------------- DoD 3

    def test_resolved_path_imports_the_mutated_copy_not_the_checkout(self):
        self.calc_tree()
        proc = self.run_mutate(f"{LIB}/calc.py", "--tests", "test_calc.py", "--json")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        with open(self.probe) as handle:
            loads = [json.loads(line) for line in handle if line.strip()]
        mutated = [load for load in loads if load["source"] != CALC]
        # One import per mutant run at least: four sites, each run in a copy holding its mutant.
        self.assertGreaterEqual(len(mutated), 4, loads)
        for load in mutated:
            self.assertFalse(load["file"].startswith(self.root + os.sep), load["file"])
            self.assertTrue(load["file"].endswith("/" + LIB + "/calc.py"), load["file"])
            self.assertFalse(os.path.exists(load["file"]), "a worker copy was left behind: " + load["file"])
        self.assertEqual(len({load["source"] for load in mutated}), 4)

    # ---------------------------------------------------------------------------------------------- DoD 4

    def test_failing_baseline_exits_2_and_leaves_checkout_unchanged(self):
        self.calc_tree(TEST_CALC_FAILING)
        before = snapshot(self.root)
        proc = self.run_mutate(f"{LIB}/calc.py", "--tests", "test_calc.py", "--json")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        # The baseline ran (it imported calc) and no mutant did (every import saw the unchanged source).
        self.assertTrue(os.path.exists(self.probe), "the baseline tests never ran")
        with open(self.probe) as handle:
            sources = [json.loads(line)["source"] for line in handle if line.strip()]
        self.assertTrue(sources)
        self.assertEqual(set(sources), {CALC})
        self.assertEqual(snapshot(self.root), before)
        self.git_clean()

    # ------------------------------------------------------------------------------- #502: --changed by line

    def commit(self, files: dict) -> None:
        for rel, text in files.items():
            with open(os.path.join(self.root, rel), "w") as handle:
                handle.write(text)
        env = dict(os.environ, **IDENTITY)
        for args in (("add", "-A"), ("commit", "-q", "-m", "change")):
            subprocess.run([GIT, "-C", self.root, *args], check=True, capture_output=True, env=env)

    def test_changed_ref_mutates_only_the_sites_on_changed_lines(self):
        """`--changed REF` keeps a site only when its line is new or changed in `git diff REF`: a line that merely
        moved (code inserted above it) or a file whose changes hold no site gives no mutant. With `--lines` too,
        a site must be on both."""
        added = CALC.replace("def untested(a, b):", "def added(a):\n    return a - 1\n\n\ndef untested(a, b):")
        two = CALC.replace("return a + b", "return a + b  # two").replace("return a * 3", "return a * 3  # two")
        rows = [
            # name, new calc.py, extra args, expected exit, expected {(line, function)} survivors, killed
            ("one changed line in scale", CALC.replace("return a * 3", "return a * 4"), (), 3,
             {(line_of(CALC, "a * 3"), "scale")}, 0),
            ("a function inserted above three others", added, (), 3,
             {(line_of(added, "a - 1"), "added")}, 0),
            ("the changed line's site is caught", CALC.replace("return x > 10", "return x > 10  # big"), (), 0,
             set(), 1),
            ("only the docstring changed", CALC.replace("are words here", "are only words here"), (), 0,
             set(), 0),
            ("two changed lines, --lines picks one", two, ("--lines", str(line_of(two, "a * 3"))), 3,
             {(line_of(two, "a * 3"), "scale")}, 0),
        ]
        for i, (name, new, extra, code, survivors, killed) in enumerate(rows):
            with self.subTest(name):
                self.root = os.path.join(self.tmp, f"repo{i}")
                self.calc_tree()
                self.commit({f"{LIB}/calc.py": new})
                proc = self.run_mutate(f"{LIB}/calc.py", "--changed", "HEAD~1", "--tests", "test_calc.py",
                                       *extra, "--json")
                self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                report = self.report(proc)
                got = {(s["line"], s["function"]) for s in report["survivors"]}
                self.assertEqual(got, survivors, report["survivors"])
                self.assertEqual(report["killed"], killed, report)
                if not survivors:
                    self.assertEqual(report["survived"], 0, report)
                if not survivors and not killed:  # no changed line holds a site: nothing was mutated
                    self.assertEqual([f for f in report["files"] if f["killed"] or f["survived"]], [])
                self.git_clean()

    # ---------------------------------------------------------------------------------------------- DoD 6

    def test_standard_library_only_and_39_compatible(self):
        with open(SCRIPT) as handle:
            source = handle.read()
        tree = ast.parse(source, feature_version=(3, 9))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                            and any(a.name == "annotations" for a in node.names) for node in tree.body))
        stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        for name in sorted(names - {"__future__"}):
            spec = importlib.util.find_spec(name)
            self.assertIsNotNone(spec, name)
            origin = spec.origin or ""
            if origin in ("built-in", "frozen"):
                continue
            origin = os.path.realpath(origin)
            self.assertTrue(origin.startswith(stdlib + os.sep) and "site-packages" not in origin,
                            f"{name} is not in the standard library: {origin}")


if __name__ == "__main__":
    unittest.main()
