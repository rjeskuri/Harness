"""Ticket #116 (epic #78): the default test and source-exempt patterns have one definition.

- plugins/harness/lib/harness_lib/settings.py assigns DEFAULT_TEST_PATTERNS and
  DEFAULT_SOURCE_EXEMPT exactly once each (the module used to assign the first twice).
- No other tool, hook or lib source carries a copy of either default: no string constant,
  list/tuple/set, join or concatenation outside settings.py holds three or more of the
  default patterns (however it is split across lines or renamed). harness-scaffold's
  TEST_FILE_PATTERNS is a different list of single basenames and is left alone.
- Drift: with the lib's two defaults patched to sentinels before a fresh load of tests_first
  and harness-red, both classify by the sentinels (a private copy under another name cannot).
- harness-red re-exports the lib's values as TEST_PATTERNS and SOURCE_EXEMPT (tests/test_red.py
  reads those names), and tests_first takes both defaults from the lib: its source has no
  assignment of them, and classify() with no settings behaves as the lib's patterns say.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_default_patterns_once.py" -v
"""

from __future__ import annotations

import ast
import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
LIB = os.path.join(PLUGIN, "lib")
SETTINGS_PY = os.path.join(LIB, "harness_lib", "settings.py")
RED = os.path.join(PLUGIN, "bin", "harness-red")
TESTS_FIRST = os.path.join(PLUGIN, "hooks", "tests_first.py")

if LIB not in sys.path:
    sys.path.insert(0, LIB)
from harness_lib import settings  # noqa: E402

DEFAULTS = ("DEFAULT_TEST_PATTERNS", "DEFAULT_SOURCE_EXEMPT")


def parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), path)


def assigned_names(tree):
    """Every name bound by an assignment anywhere in the tree, with its line."""
    found = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    found.append((sub.id, node.lineno))
    return found


def source_files():
    """Every python source of the tools, hooks and lib (bin/ scripts without .py included)."""
    paths = glob.glob(os.path.join(PLUGIN, "bin", "*"))
    paths += glob.glob(os.path.join(PLUGIN, "hooks", "*.py"))
    paths += glob.glob(os.path.join(LIB, "harness_lib", "*.py"))
    out = []
    for p in sorted(paths):
        if not os.path.isfile(p):
            continue
        try:
            out.append((p, parse(p)))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue  # a shell script
    return out


def load_module(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class OneCopyInTheLib(unittest.TestCase):
    def test_lib_assigns_each_default_exactly_once(self):
        found = assigned_names(parse(SETTINGS_PY))
        for name in DEFAULTS:
            lines = [ln for n, ln in found if n == name]
            self.assertEqual(len(lines), 1, f"{name} is assigned at lines {lines} of settings.py")

    def test_lib_defaults_are_the_documented_lists(self):
        tests = settings.patterns(settings.DEFAULT_TEST_PATTERNS)
        exempt = settings.patterns(settings.DEFAULT_SOURCE_EXEMPT)
        for p in ("tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/*_test.js",
                  "**/*.test.js", "**/*.test.ts", "**/*_test.ts", "spec/**"):
            self.assertIn(p, tests)
        for p in ("docs/**", "**/*.md", ".claude/**", ".github/**", ".githooks/**", "**/*.toml", "**/*.cfg",
                  "**/*.ini", "**/*.json", "**/*.yaml", "**/*.yml", "**/*.txt", "**/*.lock", ".gitignore",
                  ".worktreeinclude", "LICENSE*"):
            self.assertIn(p, exempt)
        self.assertEqual(len(tests), len(set(tests)))
        self.assertEqual(len(exempt), len(set(exempt)))

    def test_no_other_source_carries_a_copy_of_either_default(self):
        defaults = {name: set(settings.patterns(getattr(settings, name))) for name in DEFAULTS}
        containers = (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.BinOp, ast.JoinedStr)
        copies = []
        for path, tree in source_files():
            if os.path.realpath(path) == os.path.realpath(SETTINGS_PY):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, containers):
                    continue
                # every string literal inside this node: a list, join or concatenation of pieces
                # counts as one copy
                listed = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        listed |= set(settings.patterns(sub.value))
                for name, pats in defaults.items():
                    if len(listed & pats) >= 3:
                        copies.append(f"{os.path.relpath(path, PLUGIN)}:{node.lineno} repeats {name}")
        self.assertEqual(copies, [])

    def test_scaffolds_own_list_is_not_confused_with_the_defaults(self):
        tree = parse(os.path.join(PLUGIN, "bin", "harness-scaffold"))
        names = [n for n, _ in assigned_names(tree)]
        self.assertIn("TEST_FILE_PATTERNS", names)
        for name in DEFAULTS:
            self.assertNotIn(name, names)


class ToolsUseTheLibsObjects(unittest.TestCase):
    def test_harness_red_re_exports_the_lib_values(self):
        red = load_module("harness_red_for_default_patterns", RED)
        self.assertIs(red.TEST_PATTERNS, settings.DEFAULT_TEST_PATTERNS)
        self.assertIs(red.SOURCE_EXEMPT, settings.DEFAULT_SOURCE_EXEMPT)

    def test_harness_red_does_not_assign_the_defaults_itself(self):
        names = [n for n, _ in assigned_names(parse(RED))]
        self.assertNotIn("SOURCE_EXEMPT", names)
        self.assertNotIn("TEST_PATTERNS", names)

    def test_tests_first_does_not_assign_the_defaults_itself(self):
        names = [n for n, _ in assigned_names(parse(TESTS_FIRST))]
        for name in DEFAULTS:
            self.assertNotIn(name, names)

    def test_tests_first_binds_both_defaults_from_the_lib(self):
        imported = set()
        for node in ast.walk(parse(TESTS_FIRST)):
            if isinstance(node, ast.ImportFrom) and node.module == "harness_lib.settings":
                imported |= {a.name for a in node.names}
        for name in DEFAULTS:
            self.assertIn(name, imported)

    def test_tests_first_module_holds_the_lib_objects(self):
        tf = load_module("tests_first_for_default_patterns", TESTS_FIRST)
        self.assertIs(tf.DEFAULT_TEST_PATTERNS, settings.DEFAULT_TEST_PATTERNS)
        self.assertIs(tf.DEFAULT_SOURCE_EXEMPT, settings.DEFAULT_SOURCE_EXEMPT)

    def test_classify_defaults_follow_the_lib_patterns(self):
        # Behaviour guard (passes before the change too): the hook's classification with no
        # settings, for paths chosen from the documented defaults, not from the code.
        tf = load_module("tests_first_for_classify", TESTS_FIRST)
        cases = {
            "tests/test_a.py": "test", "pkg/test_b.py": "test", "pkg/b_test.py": "test",
            "web/app.test.ts": "test", "spec/x.rb": "test", "pkg/conftest.py": "test",
            "docs/guide.html": "exempt", "README.md": "exempt", "pkg/config.yaml": "exempt",
            "package-lock.lock": "exempt", "LICENSE-MIT": "exempt", ".gitignore": "exempt",
            "src/app.py": "source", "bin/tool": "source", "lib/mod.js": "source",
        }
        for rel, kind in cases.items():
            self.assertEqual(tf.classify(rel, {}), kind, rel)


SENTINEL_EXEMPT = "zzz/**"
SENTINEL_TEST = "yyy/**"


class Drift(unittest.TestCase):
    """Change the lib's defaults, load the consumers afresh: any private copy shows."""

    def setUp(self):
        self.saved = {n: getattr(settings, n) for n in DEFAULTS}
        self.env = {k: os.environ.pop(k) for k in ("HARNESS_TEST_PATTERNS", "HARNESS_SOURCE_EXEMPT")
                    if k in os.environ}
        settings.DEFAULT_SOURCE_EXEMPT = SENTINEL_EXEMPT
        settings.DEFAULT_TEST_PATTERNS = SENTINEL_TEST
        self.tmp = tempfile.mkdtemp(prefix="default-patterns-")

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(settings, n, v)
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tests_first_classify_follows_the_lib(self):
        tf = load_module("tests_first_drift", TESTS_FIRST)
        self.assertEqual(tf.classify("zzz/a", {}), "exempt")
        self.assertEqual(tf.classify("yyy/a", {}), "test")
        self.assertEqual(tf.classify("docs/a.md", {}), "source")
        self.assertEqual(tf.classify("tests/test_a.py", {}), "source")
        self.assertEqual(tf.classify("src/app.py", {}), "source")

    def test_harness_red_classify_follows_the_lib(self):
        red = load_module("harness_red_drift", RED)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "-c", "init.defaultBranch=main", "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "t/1-drift"], check=True)
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp
        try:
            r = red.Repo(repo)
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
        self.assertEqual(r.classify("zzz/a"), "exempt")
        self.assertEqual(r.classify("yyy/a"), "test")
        self.assertEqual(r.classify("docs/a.md"), "source")
        self.assertEqual(r.classify("tests/test_a.py"), "source")
        self.assertEqual(r.classify("src/app.py"), "source")


if __name__ == "__main__":
    unittest.main()
