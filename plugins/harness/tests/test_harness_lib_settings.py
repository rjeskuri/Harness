"""Tests for plugins/harness/lib/harness_lib/settings.py (ticket #89, epic #78): one copy of
settings_env, setting, patterns and glob_match for harness-work, harness-red, branch_guard,
verify_on_stop and tests_first.

- The lib's four functions, imported from lib/ and checked against values derived here:
  settings_env (settings.json then settings.local.json by default, the later file winning
  key by key; only the `env` object; missing, unreadable or broken files skipped; the
  `names` parameter, root as str or Path), setting (the environment first when not blank,
  then str/int/float from the file, never bool; blank means unset), patterns (comma list,
  stripped, empties dropped) and glob_match (`**` takes zero or more segments, other
  segments fnmatchcase, a pattern without / matches the basename, an empty pattern
  matches nothing), the last also against a reference matcher written here.
- One copy: none of the five files defines any of them (or classification_env, or the
  segment matchers) or carries a renamed copy, and each binds the names it uses from
  harness_lib.settings in its own namespace; harness-red so re-exports what test_red reads.
- tests_first reads only the main checkout's tracked settings.json: every settings_env
  call in it names ("settings.json",), the security comment is kept, and from a tree whose
  settings_env is instrumented the hook classifies (Edit and a wholesale git reset) with
  what that settings_env returns for the main checkout and ("settings.json",) alone.
  branch_guard, from the same tree, takes HARNESS_ALLOW_MAIN from the lib's settings_env.
- Loading: the three hooks fail open (exit 0, one stderr line naming harness_lib) and the
  two tools stop (exit 1, one line) when lib/ is missing or settings.py is missing,
  broken or raises; the tools find the lib from their real path.
- harness-scaffold's TEST_FILE_PATTERNS is untouched (this one passes before the change
  too: it guards against the refactor reaching it).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_settings.py" -v
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import importlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import types
import unittest
import uuid
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the hooks, tools and lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
BIN = PLUGIN / "bin"
LIB = PLUGIN / "lib"
SETTINGS_PY = LIB / "harness_lib" / "settings.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
BRANCH = "t/12-fix-login"
DEFAULT_NAMES = ("settings.json", "settings.local.json")
SHARED = ("settings_env", "setting", "patterns", "glob_match")
# The old private copies' names: none may survive in the five files.
COPIES = SHARED + ("classification_env", "match_segments", "_segments_match")
# The files that carried copies, and the names each uses (red re-exports glob_match,
# settings_env and setting, which tests/test_red.py reads by attribute).
USES = {
    HOOKS / "tests_first.py": ("settings_env", "setting", "patterns", "glob_match"),
    HOOKS / "branch_guard.py": ("settings_env", "setting"),
    HOOKS / "verify_on_stop.py": ("settings_env", "setting"),
    BIN / "harness-red": ("settings_env", "setting", "glob_match"),
    BIN / "harness-work": ("settings_env", "setting"),
}
FIVE_FILES = tuple(USES)
HOOK_FILES = tuple(p for p in FIVE_FILES if p.parent == HOOKS)
TOOLS = ("harness-red", "harness-work")
AUTHOR_ON_SOURCE = (
    "You write tests only on this ticket. Report the source change the implementation "
    "needs; the coder makes it."
)
CODER_ON_TEST = (
    "Tests are fixed for this ticket. If a test is wrong, stop and report it; the "
    "orchestrator sends it back to the test-author."
)

_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-lib-settings-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def load_settings():
    """harness_lib.settings imported from the checkout's lib/, as a file's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.settings")


def load_with_loader(path: Path) -> types.ModuleType:
    """A hook or tool loaded by path, the way tests/test_red.py loads harness-red."""
    modname = path.name.replace("-", "_").replace(".py", "") + "_settings_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_guards.py loads it: a bare module with __file__."""
    module = types.ModuleType("harness_hook_settings_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def parse(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def defs(tree) -> list:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def normalized_body(fn) -> str:
    """The body of a def without its docstring, every name and argument spelled `_`, so a
    renamed copy compares equal to the original."""
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


def distinctive(fn) -> bool:
    """A body worth comparing: more than one statement, or a loop or comprehension. A
    one-line wrapper (`return f(x)`) normalises to a shape too common to call a copy."""
    body = [
        s
        for s in fn.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    if len(body) > 1:
        return True
    kinds = (ast.For, ast.While, ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)
    return any(isinstance(n, kinds) for s in body for n in ast.walk(s))


# ================================================================ reference matcher


def reference_match(pattern: str, path: str) -> bool:
    """glob_match's contract, written here from first principles: an empty (or blank)
    pattern matches nothing; a pattern without / is matched, case-sensitively, against
    the basename; otherwise pattern and path are split on /, a `**` segment takes zero or
    more path segments and every other segment must match one path segment."""
    pattern = pattern.strip()
    if not pattern:
        return False

    def seg(pat: str, part: str) -> bool:
        return re.fullmatch(fnmatch.translate(pat), part) is not None

    if "/" not in pattern:
        return seg(pattern, path.split("/")[-1])
    pats, parts = pattern.split("/"), path.split("/")
    # reachable[j]: pats[:i] can consume parts[:j]
    reachable = [True] + [False] * len(parts)
    for pat in pats:
        nxt = [False] * (len(parts) + 1)
        for j in range(len(parts) + 1):
            if not reachable[j]:
                continue
            if pat == "**":
                for k in range(j, len(parts) + 1):
                    nxt[k] = True
            elif j < len(parts) and seg(pat, parts[j]):
                nxt[j + 1] = True
        reachable = nxt
    return reachable[len(parts)]


# ================================================================ the lib itself


class SettingsModuleTests(unittest.TestCase):
    def test_the_module_is_in_lib_and_stdlib_only(self):
        self.assertTrue(SETTINGS_PY.is_file(), f"{SETTINGS_PY} does not exist")
        lib = load_settings()
        self.assertEqual(os.path.realpath(lib.__file__), os.path.realpath(str(SETTINGS_PY)))
        for name in SHARED:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(lib, name, None)), name)
        tree = parse(SETTINGS_PY)
        futures = [
            n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"
        ]
        self.assertTrue(
            any(a.name == "annotations" for n in futures for a in n.names),
            "settings.py lacks `from __future__ import annotations`",
        )
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:  # 3.9 has no list to check against
            return
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        imported -= {"__future__", "harness_lib"}
        self.assertEqual(sorted(imported - set(stdlib)), [])


class SettingsEnvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_settings()

    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="harness-settings-env-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        os.mkdir(os.path.join(self.root, ".claude"))
        tag = uuid.uuid4().hex[:8].upper()
        self.k = [f"K{i}_{tag}" for i in range(8)]

    def put(self, name: str, data) -> None:
        path = os.path.join(self.root, ".claude", name)
        if isinstance(data, bytes):
            with open(path, "wb") as fh:
                fh.write(data)
        elif isinstance(data, str):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)

    def env_of(self, *args, **kwargs) -> dict:
        got = self.lib.settings_env(*args, **kwargs)
        self.assertIsInstance(got, dict)
        return got

    def test_default_merges_local_over_shared_key_by_key(self):
        k = self.k
        shared = {k[0]: "a", k[1]: 30, k[2]: True, k[3]: {"n": 1}, k[4]: "shared"}
        local = {k[4]: "local", k[5]: "", k[3]: {"m": 2}, k[6]: 2.5}
        self.put("settings.json", {"env": shared, "model": "x"})
        self.put("settings.local.json", {"env": local})
        expected = dict(shared)
        expected.update(local)  # whole values replaced, not merged
        for root in (self.root, Path(self.root)):
            with self.subTest(root=type(root).__name__):
                self.assertEqual(self.env_of(root), expected)
        # values come back as the file has them: no stringifying here
        got = self.env_of(self.root)
        self.assertIs(got[k[2]], True)
        self.assertEqual((got[k[1]], got[k[6]]), (30, 2.5))
        self.assertEqual(got[k[3]], {"m": 2})

    def test_one_file_alone(self):
        k = self.k
        self.put("settings.json", {"env": {k[0]: "only shared", k[1]: 7}})
        self.assertEqual(self.env_of(self.root), {k[0]: "only shared", k[1]: 7})
        os.remove(os.path.join(self.root, ".claude", "settings.json"))
        self.put("settings.local.json", {"env": {k[2]: "only local"}})
        self.assertEqual(self.env_of(self.root), {k[2]: "only local"})

    def test_nothing_there_gives_an_empty_dict(self):
        self.assertEqual(self.env_of(self.root), {})
        bare = os.path.join(self.root, "no-claude-dir")
        os.mkdir(bare)
        self.assertEqual(self.env_of(bare), {})
        self.assertEqual(self.env_of(os.path.join(self.root, "does", "not", "exist")), {})

    def test_only_the_env_object_counts(self):
        k = self.k
        local = {k[7]: "kept"}
        self.put("settings.local.json", {"env": local})
        not_an_env = [
            {"permissions": {"allow": ["Bash"]}, k[0]: "top-level key"},
            {"env": [k[0], "x"]},
            {"env": f"{k[0]}=x"},
            {"env": None},
            {"env": 5},
            [{"env": {k[0]: "inside a list"}}],
            "a string",
            42,
            None,
        ]
        for data in not_an_env:
            with self.subTest(settings_json=data):
                self.put("settings.json", data)
                self.assertEqual(self.env_of(self.root), local)
        # and the other way round: the shared file still counts when local is junk
        self.put("settings.json", {"env": {k[1]: "shared"}, "hooks": {}})
        for data in not_an_env:
            with self.subTest(settings_local_json=data):
                self.put("settings.local.json", data)
                self.assertEqual(self.env_of(self.root), {k[1]: "shared"})

    def test_broken_files_are_skipped(self):
        k = self.k
        good = {k[3]: "good", k[4]: 1}
        broken = {
            "invalid json": "{",
            "truncated": '{"env": {"%s": 1}' % k[0],
            "empty": "",
            "trailing junk": '{"env": {"%s": 1}} x' % k[0],
            "not utf-8": b'\xff\xfe{"env": {}}',
        }
        for broken_name, good_name in (
            ("settings.json", "settings.local.json"),
            ("settings.local.json", "settings.json"),
        ):
            for label, text in broken.items():
                with self.subTest(broken=broken_name, how=label):
                    for name in DEFAULT_NAMES:
                        path = os.path.join(self.root, ".claude", name)
                        if os.path.isdir(path):
                            os.rmdir(path)
                    self.put(good_name, {"env": good})
                    self.put(broken_name, text)
                    self.assertEqual(self.env_of(self.root), good)
            with self.subTest(broken=broken_name, how="a directory"):
                path = os.path.join(self.root, ".claude", broken_name)
                os.remove(path)
                os.mkdir(path)
                self.assertEqual(self.env_of(self.root), good)
                os.rmdir(path)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads anything")
    def test_an_unreadable_file_is_skipped(self):
        k = self.k
        self.put("settings.json", {"env": {k[0]: "shared"}})
        self.put("settings.local.json", {"env": {k[0]: "local", k[1]: "x"}})
        path = os.path.join(self.root, ".claude", "settings.local.json")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        self.assertEqual(self.env_of(self.root), {k[0]: "shared"})

    def test_names_choose_the_files_and_the_last_wins(self):
        k = self.k
        self.put("settings.json", {"env": {k[0]: "shared", k[1]: "shared only"}})
        self.put("settings.local.json", {"env": {k[0]: "local", k[2]: "local only"}})
        self.put("other.json", {"env": {k[0]: "other", k[3]: "other only"}})
        cases = [
            (("settings.json",), {k[0]: "shared", k[1]: "shared only"}),
            (("settings.local.json",), {k[0]: "local", k[2]: "local only"}),
            (
                ("settings.local.json", "settings.json"),
                {k[0]: "shared", k[1]: "shared only", k[2]: "local only"},
            ),
            (
                ("settings.json", "settings.local.json"),
                {k[0]: "local", k[1]: "shared only", k[2]: "local only"},
            ),
            (
                ("settings.json", "other.json"),
                {k[0]: "other", k[1]: "shared only", k[3]: "other only"},
            ),
            (
                ("other.json", "settings.local.json", "settings.json"),
                {k[0]: "shared", k[1]: "shared only", k[2]: "local only", k[3]: "other only"},
            ),
            (("missing.json", "settings.json"), {k[0]: "shared", k[1]: "shared only"}),
            ((), {}),
        ]
        for names, expected in cases:
            with self.subTest(names=names):
                self.assertEqual(self.env_of(self.root, names), expected)
                self.assertEqual(self.env_of(Path(self.root), names=names), expected)
        # the default is the two project files, settings.local.json last
        self.assertEqual(self.env_of(self.root), self.env_of(self.root, DEFAULT_NAMES))
        self.assertEqual(self.env_of(self.root)[k[0]], "local")


class SettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_settings()

    def setUp(self) -> None:
        self.name = "HARNESS_T89_" + uuid.uuid4().hex[:10].upper()

    def value(self, environ, file_env: dict):
        """setting(self.name, file_env) with the variable set to `environ` (None: unset)."""
        patch = {} if environ is None else {self.name: environ}
        with mock.patch.dict(os.environ, patch):
            if environ is None:
                os.environ.pop(self.name, None)
            return self.lib.setting(self.name, file_env)

    def test_file_values(self):
        cases = [
            ("uv run pytest", "uv run pytest"),
            ("  padded value \t", "padded value"),
            ("", None),
            ("   ", None),
            (30, "30"),
            (0, "0"),
            (-7, "-7"),
            (2.5, "2.5"),
            (1e20, str(1e20)),
            (True, None),
            (False, None),
            (None, None),
            ([1, 2], None),
            ({"a": 1}, None),
        ]
        for given, expected in cases:
            with self.subTest(file_value=given):
                self.assertEqual(self.value(None, {self.name: given}), expected)
                # another variable's value is not this one's
                self.assertIsNone(self.value(None, {self.name + "_OTHER": given}))
        self.assertIsNone(self.value(None, {}))

    def test_the_environment_wins_when_not_blank(self):
        for environ, expected in [
            ("from env", "from env"),
            ("  spaced  ", "spaced"),
            ("0", "0"),
            ("1", "1"),
        ]:
            for file_value in ("from file", 99, True, None):
                with self.subTest(environ=environ, file_value=file_value):
                    file_env = {} if file_value is None else {self.name: file_value}
                    self.assertEqual(self.value(environ, file_env), expected)

    def test_a_blank_environment_falls_through_to_the_file(self):
        for environ in ("", "   ", "\t\n"):
            for file_value, expected in [("from file", "from file"), (12, "12"), (" ", None), (False, None)]:
                with self.subTest(environ=environ, file_value=file_value):
                    self.assertEqual(self.value(environ, {self.name: file_value}), expected)
            with self.subTest(environ=environ, file_value="absent"):
                self.assertIsNone(self.value(environ, {}))

    def test_settings_env_and_setting_together(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="harness-setting-"))
        self.addCleanup(shutil.rmtree, root, True)
        os.mkdir(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
            json.dump({"env": {self.name: 45, "OTHER_" + self.name: "keep"}}, fh)
        with open(os.path.join(root, ".claude", "settings.local.json"), "w") as fh:
            json.dump({"env": {self.name: " local "}}, fh)
        both = self.lib.settings_env(root)
        tracked = self.lib.settings_env(root, ("settings.json",))
        self.assertEqual(self.value(None, both), "local")
        self.assertEqual(self.value(None, tracked), "45")
        self.assertEqual(self.value("env", tracked), "env")


class ProjectSettingTests(unittest.TestCase):
    """Ticket #205: a session started in another project carries that project's settings in its
    environment; other_project tells it apart from this repository's own sessions, and then
    project_setting reads the settings files alone (setting_from)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_settings()

    def setUp(self) -> None:
        self.name = "HARNESS_T205_" + uuid.uuid4().hex[:10].upper()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-project-setting-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.git_env = dict(os.environ, **GIT_IDENTITY)

    def git(self, cwd: str, *args: str) -> None:
        subprocess.run(["git", "-C", cwd, *args], env=self.git_env, check=True, capture_output=True)

    def repository(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        self.git(path, "init", "-q")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "init")
        return path

    def foreign(self, project, main: str) -> bool:
        """other_project(main) with CLAUDE_PROJECT_DIR set to `project` (None: unset)."""
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project or ""}):
            if project is None:
                del os.environ["CLAUDE_PROJECT_DIR"]
            return self.lib.other_project(main)

    def test_setting_from_reads_the_file_alone(self):
        with mock.patch.dict(os.environ, {self.name: "from env"}):
            self.assertEqual(self.lib.setting_from(self.name, {self.name: " from file "}), "from file")
            self.assertIsNone(self.lib.setting_from(self.name, {}))
            self.assertIsNone(self.lib.setting_from(self.name, {self.name: True}))
            self.assertEqual(self.lib.setting_from(self.name, {self.name: 30}), "30")

    def test_this_repositorys_own_sessions_are_not_another_projects(self):
        main = self.repository("main")
        wt = os.path.join(self.tmp, "wt")
        self.git(main, "worktree", "add", "-q", wt, "-b", "t/1-x")
        os.makedirs(os.path.join(main, "sub"))
        link = os.path.join(self.tmp, "link")
        os.symlink(main, link)
        for name, project in (("unset", None), ("blank", "  "), ("main", main), ("symlink", link),
                              ("subdirectory", os.path.join(main, "sub")), ("worktree", wt)):
            with self.subTest(name):
                self.assertFalse(self.foreign(project, main))

    def test_another_repository_or_no_repository_is_another_project(self):
        main = self.repository("main")
        other = self.repository("OkinawaDream")
        plain = os.path.join(self.tmp, "notes")
        os.makedirs(plain)
        for name, project in (("another repository", other), ("no repository", plain), ("the parent", self.tmp),
                              ("missing", os.path.join(self.tmp, "gone"))):
            with self.subTest(name):
                self.assertTrue(self.foreign(project, main))

    def test_project_setting(self):
        with mock.patch.dict(os.environ, {self.name: "from env"}):
            self.assertEqual(self.lib.project_setting(self.name, {self.name: "from file"}, False), "from env")
            self.assertEqual(self.lib.project_setting(self.name, {self.name: "from file"}, True), "from file")
            self.assertIsNone(self.lib.project_setting(self.name, {}, True))


class PatternsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_settings()

    def test_patterns(self):
        cases = [
            ("", []),
            (" ", []),
            (",", []),
            (" , ,, ", []),
            ("tests/**", ["tests/**"]),
            ("a,b", ["a", "b"]),
            (" a , b ,c ", ["a", "b", "c"]),
            ("a,,b,", ["a", "b"]),
            ("b,a,b", ["b", "a", "b"]),  # order and repeats kept
            ("checks/**, **/*_check.py", ["checks/**", "**/*_check.py"]),
            ("\tx y ,z\n", ["x y", "z"]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.lib.patterns(text), expected)
        default = (
            "tests/**,test/**,**/test_*.py,**/*_test.py,**/conftest.py,**/*_test.js,"
            "**/*.test.js,**/*.test.ts,**/*_test.ts,spec/**"
        )
        self.assertEqual(self.lib.patterns(default), default.split(","))


class GlobMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_settings()

    def test_table(self):
        cases = [
            ("tests/**", "tests/test_x.py", True),
            ("tests/**", "tests/a/b/c.json", True),
            ("tests/**", "tests", True),
            ("tests/**", "src/tests/x.py", False),
            ("tests/**", "testsuite/x.py", False),
            ("**/test_*.py", "test_x.py", True),
            ("**/test_*.py", "pkg/sub/test_x.py", True),
            ("**/test_*.py", "pkg/x_test.py", False),
            ("**/*.md", "README.md", True),
            ("docs/**", "src/docs/x.md", False),
            ("spec/**", "myspec/x.rb", False),
            ("src/*/x.py", "src/a/x.py", True),
            ("src/*/x.py", "src/a/b/x.py", False),
            ("a/**/b", "a/b", True),
            ("a/**/b", "a/x/y/b", True),
            ("a/**/b", "a/x/y/c", False),
            ("a/**/**/b", "a/b", True),
            ("a/*/c", "a/c", False),
            ("a/b", "a/b", True),
            ("a/b", "a/b/c", False),
            ("a/b", "x/a/b", False),
            ("src/*", "src/a", True),
            ("src/*", "src/a/b", False),
            ("src/*", "src", False),
            ("src/*.py", "lib/src/a.py", False),
            ("docs/*.md", "docs/sub/a.md", False),
            (".claude/**", ".claude/settings.json", True),
            # no / in the pattern: the basename
            (".gitignore", "sub/.gitignore", True),
            ("LICENSE*", "LICENSE.txt", True),
            ("LICENSE*", "sub/LICENSE", True),
            ("*.toml", "pyproject.toml", True),
            ("*.toml", "pyproject.toml.bak", False),
            ("conftest.py", "a/b/conftest.py", True),
            ("*.test.ts", "web/a.test.ts", True),
            ("*.test.ts", "web/a.test.tsx", False),
            ("*.md", "docs/.hidden.md", True),
            ("**", "any/where/at/all.py", True),
            ("test_?.py", "pkg/test_a.py", True),
            ("test_?.py", "pkg/test_ab.py", False),
            ("[ab].py", "x/a.py", True),
            ("[ab].py", "x/c.py", False),
            ("[!ab].py", "c.py", True),
            # case matters (fnmatchcase), whatever the file system
            ("*.PY", "a.py", False),
            ("*.py", "A.PY", False),
            ("Tests/**", "tests/x.py", False),
            ("**/TEST_*.py", "pkg/test_a.py", False),
            ("README.MD", "README.md", False),
            ("readme.md", "README.md", False),
            # an empty pattern matches nothing
            ("", "x", False),
            ("", "", False),
            ("   ", "x", False),
        ]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(bool(self.lib.glob_match(pattern, path)), want)
                self.assertEqual(reference_match(pattern, path), want)  # the table agrees with the contract

    def test_against_the_reference_matcher(self):
        pats = [
            "tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/*.test.js",
            "spec/**", "docs/**", "**/*.md", ".claude/**", "*.toml", "LICENSE*", ".gitignore",
            "src/*/x.py", "a/**/b", "a/**/**/b", "**/b/**", "a/*", "*/b", "**", "*", "a/b/c",
            "**/[Tt]est?.py", "x/**/y/**/z", "Docs/**", "*.Md",
        ]
        paths = [
            "tests/test_x.py", "tests", "test/a/b.py", "pkg/test_a.py", "pkg/a_test.py", "conftest.py",
            "web/a.test.js", "spec/a_spec.rb", "docs/x.md", "README.md", ".claude/settings.json",
            "pyproject.toml", "LICENSE", "sub/.gitignore", "src/a/x.py", "src/a/b/x.py", "a/b", "a/x/b",
            "a/b/c", "b", "x/b/y", "x/y/z", "x/1/y/2/z", "Test1.py", "q/test2.py", "Docs/a", "A.Md",
            "a.md", "src/app.py",
        ]
        mismatches = [
            (p, q, reference_match(p, q))
            for p in pats
            for q in paths
            if bool(self.lib.glob_match(p, q)) != reference_match(p, q)
        ]
        self.assertEqual(mismatches, [])
        # the grid is not lopsided: both answers occur, so a constant cannot pass
        answers = [reference_match(p, q) for p in pats for q in paths]
        self.assertGreater(answers.count(True), 40)
        self.assertGreater(answers.count(False), 40)

    def test_the_pattern_is_stripped(self):
        """harness-red's variant: surrounding whitespace in a pattern does not count."""
        for pattern, path, want in [
            (" tests/** ", "tests/a.py", True),
            ("\t*.md\n", "docs/a.md", True),
            (" a/b ", "a/b", True),
            (" a/b ", "a/c", False),
        ]:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(bool(self.lib.glob_match(pattern, path)), want)

    def test_patterns_and_glob_match_classify_as_the_defaults_say(self):
        lib = self.lib
        tests = lib.patterns(
            "tests/**,test/**,**/test_*.py,**/*_test.py,**/conftest.py,**/*_test.js,"
            "**/*.test.js,**/*.test.ts,**/*_test.ts,spec/**"
        )
        exempt = lib.patterns(
            "docs/**,**/*.md,.claude/**,.github/**,.githooks/**,**/*.toml,**/*.cfg,**/*.ini,"
            "**/*.json,**/*.yaml,**/*.yml,**/*.txt,**/*.lock,.gitignore,.worktreeinclude,LICENSE*"
        )
        for path, want in [
            ("tests/test_x.py", "test"), ("pkg/conftest.py", "test"), ("web/a.test.ts", "test"),
            ("spec/a_spec.rb", "test"), ("docs/x.py", "exempt"), ("pyproject.toml", "exempt"),
            (".claude/settings.json", "exempt"), ("LICENSE", "exempt"), ("src/pkg/a.py", "source"),
            ("calc.py", "source"), ("Tests/test_x.py", "test"), ("src/Docs/x.py", "source"),
        ]:
            got = (
                "test" if any(lib.glob_match(g, path) for g in tests)
                else "exempt" if any(lib.glob_match(g, path) for g in exempt)
                else "source"
            )
            with self.subTest(path=path):
                self.assertEqual(got, want)


# ================================================================ one copy


def string_compares(tree, literal: str) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and side.value == literal:
                    found.append(f"compare with {literal!r} (line {node.lineno})")
    return found


def comma_split_comprehensions(tree) -> list[str]:
    """[x.strip() for x in <y>.split(",") if x.strip()] and its generator form: the body of
    patterns()."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            continue
        for gen in node.generators:
            it = gen.iter
            if (
                isinstance(it, ast.Call)
                and isinstance(it.func, ast.Attribute)
                and it.func.attr == "split"
                and it.args
                and isinstance(it.args[0], ast.Constant)
                and it.args[0].value == ","
                and any(
                    isinstance(n, ast.Attribute) and n.attr == "strip" for n in ast.walk(node)
                )
            ):
                found.append(f"comma-split comprehension (line {node.lineno})")
    return found


class OneCopyTests(unittest.TestCase):
    def test_no_file_defines_its_own_copy(self):
        for path in FIVE_FILES:
            defined = {fn.name for fn in defs(parse(path))}
            with self.subTest(file=path.name):
                self.assertEqual(sorted(defined & set(COPIES)), [])

    def test_no_file_carries_a_renamed_copy(self):
        """No def in the five files has the body (names and docstring aside) of a def in
        settings.py. One-line wrappers are not compared (see distinctive)."""
        self.assertTrue(SETTINGS_PY.is_file(), f"{SETTINGS_PY} does not exist")
        refs = {
            normalized_body(fn): fn.name for fn in defs(parse(SETTINGS_PY)) if distinctive(fn)
        }
        self.assertTrue(refs, "settings.py has no def to compare")
        for path in FIVE_FILES:
            copies = [
                f"{fn.name} (line {fn.lineno}) = {refs[normalized_body(fn)]}"
                for fn in defs(parse(path))
                if normalized_body(fn) in refs
            ]
            with self.subTest(file=path.name):
                self.assertEqual(copies, [])

    def test_no_file_matches_segments_or_splits_patterns_itself(self):
        """The two shapes a hidden copy needs: comparing a segment with "**" (glob_match's
        recursion) and the comma-split comprehension (patterns, and harness-red's globs)."""
        for path in FIVE_FILES:
            tree = parse(path)
            with self.subTest(file=path.name):
                self.assertEqual(string_compares(tree, "**"), [])
                self.assertEqual(comma_split_comprehensions(tree), [])

    def check_bound(self, module: types.ModuleType, path: Path) -> None:
        lib = os.path.realpath(str(SETTINGS_PY))
        for name in USES[path]:
            with self.subTest(file=path.name, name=name):
                bound = module.__dict__.get(name)  # in the file's own namespace
                self.assertTrue(callable(bound), f"{path.name} does not bind {name}")
                self.assertEqual(bound.__module__, "harness_lib.settings")
                self.assertEqual(os.path.realpath(inspect.getsourcefile(bound)), lib)

    def test_names_bound_from_the_lib_with_source_file_loader(self):
        for path in FIVE_FILES:
            self.check_bound(load_with_loader(path), path)

    def test_hook_names_bound_from_the_lib_in_a_bare_module(self):
        for path in HOOK_FILES:
            self.check_bound(load_with_exec(path), path)

    def test_harness_red_reexports_what_test_red_reads(self):
        red = load_with_loader(BIN / "harness-red")
        lib = load_settings()
        for name in ("glob_match", "settings_env", "setting"):
            with self.subTest(name=name):
                self.assertEqual(getattr(red, name).__module__, "harness_lib.settings")
                self.assertEqual(
                    os.path.realpath(inspect.getsourcefile(getattr(red, name))),
                    os.path.realpath(inspect.getsourcefile(getattr(lib, name))),
                )
        for name in ("globs", "TEST_PATTERNS", "SOURCE_EXEMPT", "Problem", "test_timeout"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(red, name), name)


# ================================================================ tests_first: main's settings.json only


def module_tuples(tree) -> dict:
    """Module-level NAME = (<string literals>,) assignments."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Tuple):
                if all(isinstance(e, ast.Constant) for e in value.elts):
                    found[target.id] = tuple(e.value for e in value.elts)
    return found


class TestsFirstSpellsItOutTests(unittest.TestCase):
    def test_every_settings_env_call_names_only_settings_json(self):
        path = HOOKS / "tests_first.py"
        tree = parse(path)
        constants = module_tuples(tree)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "settings_env"
        ]
        self.assertTrue(calls, "tests_first.py never calls settings_env")
        for call in calls:
            with self.subTest(line=call.lineno):
                names = call.args[1] if len(call.args) > 1 else None
                for kw in call.keywords:
                    if kw.arg == "names":
                        names = kw.value
                self.assertIsNotNone(names, "settings_env called without names: that reads settings.local.json")
                if isinstance(names, ast.Name):
                    value = constants.get(names.id)
                else:
                    try:
                        value = ast.literal_eval(names)
                    except ValueError:
                        value = None
                self.assertEqual(tuple(value) if value is not None else None, ("settings.json",))

    def test_the_security_comment_is_kept(self):
        """The reason for reading one file stays next to the code, as a comment."""
        text = (HOOKS / "tests_first.py").read_text(encoding="utf-8")
        comments = [
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT
        ]
        self.assertTrue(
            [c for c in comments if "settings.local.json" in c],
            "no comment in tests_first.py says why settings.local.json is not read",
        )


# ================================================================ running the files from a plugin tree


# Appended to a copy of the real settings.py: settings_env answers with patterns that name
# the root it was given (its basename) and the files it was asked for, and allows main
# exactly when asked for the two default files.
SENTINEL = '''

import os as _sentinel_os

_sentinel_real_settings_env = settings_env


def settings_env(root, names=("settings.json", "settings.local.json")):
    env = dict(_sentinel_real_settings_env(root, names))
    base = _sentinel_os.path.basename(_sentinel_os.path.normpath(str(root)))
    tag = base + "--" + "+".join(n.replace(".", "_") for n in names)
    env["HARNESS_TEST_PATTERNS"] = tag + "/**"
    env["HARNESS_ALLOW_MAIN"] = "1" if tuple(names) == ("settings.json", "settings.local.json") else "0"
    return env
'''

BROKEN = ("missing", "no-settings", "settings-syntax", "settings-raises")


class TreeTestCase(unittest.TestCase):
    """Runs hooks and tools from a temporary plugin tree: hooks/ and the two tools copied
    from the checkout, and lib/ real, missing, instrumented or with settings.py missing,
    broken or raising."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-lib-settings-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )
        self.token = "boom-" + uuid.uuid4().hex[:8]
        self.trees = 0

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=self.env
        )
        return out.stdout.strip()

    def make_repo(self, name: str = "proj") -> Path:
        self.cases = getattr(self, "cases", 0) + 1
        repo = self.tmp / f"case{self.cases}" / name
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=self.env
        )
        (repo / "src").mkdir()
        (repo / "tests").mkdir()
        (repo / "README.md").write_text("hello\n")
        (repo / "src/app.py").write_text("def login():\n    return None\n")
        (repo / "tests/test_app.py").write_text("def test_login():\n    assert False\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def ticket_worktree(self) -> tuple[Path, Path]:
        repo = self.make_repo()
        wt = repo.parent / "wt"
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        return repo, wt

    def plugin(self, lib: str) -> Path:
        self.trees += 1
        root = self.tmp / f"plugin{self.trees}-{lib}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        (root / "bin").mkdir()
        for tool in TOOLS:
            shutil.copy2(BIN / tool, root / "bin" / tool)
        if lib == "missing":
            return root
        shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        settings = root / "lib" / "harness_lib" / "settings.py"
        if lib in ("real", "sentinel"):
            self.assertTrue(SETTINGS_PY.is_file(), f"{SETTINGS_PY} does not exist")
            if lib == "sentinel":
                settings.write_text(SETTINGS_PY.read_text(encoding="utf-8") + SENTINEL, encoding="utf-8")
        elif lib == "no-settings":
            if settings.exists():
                settings.unlink()
        elif lib == "settings-syntax":
            settings.write_text("def settings_env(:\n    return {}\n", encoding="utf-8")
        elif lib == "settings-raises":
            settings.write_text(f"raise RuntimeError({self.token!r})\n", encoding="utf-8")
        else:
            self.fail(f"unknown lib {lib}")
        return root

    def payload(self, tool: str, tool_input: dict, cwd: Path, agent=None) -> dict:
        data = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_01",
        }
        if agent is not None:
            data.update(agent_id="agent-1", agent_type=agent)
        return data

    def stop_payload(self, cwd: Path) -> dict:
        return {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }

    def run_script(self, script: Path, args=(), payload=None, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(script), *args],
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(cwd or self.tmp),
            timeout=60,
        )

    def deny_reason(self, r) -> str | None:
        if not r.stdout.strip():
            return None
        d = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        return d["permissionDecisionReason"]

    def edit(self, root: Path, hook: str, path: Path, cwd: Path, agent=None) -> str | None:
        tool_input = {"file_path": str(path), "old_string": "a", "new_string": "b"}
        r = self.run_script(root / "hooks" / hook, payload=self.payload("Edit", tool_input, cwd, agent), cwd=cwd)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return self.deny_reason(r)

    def bash(self, root: Path, command: str, cwd: Path, agent=None) -> str | None:
        tool_input = {"command": command, "description": "Run it"}
        r = self.run_script(
            root / "hooks" / "tests_first.py", payload=self.payload("Bash", tool_input, cwd, agent), cwd=cwd
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return self.deny_reason(r)


class TestsFirstFollowsTheLibTests(TreeTestCase):
    """tests_first, from a tree whose settings_env is instrumented, classifies with what
    that settings_env returns for the main checkout and ("settings.json",) alone."""

    def test_an_edit_is_classified_from_the_main_checkouts_settings_json_only(self):
        sentinel = self.plugin("sentinel")
        real = self.plugin("real")
        repo, wt = self.ticket_worktree()
        main_only = wt / f"{repo.name}--settings_json" / "check.py"
        with_local = wt / f"{repo.name}--settings_json+settings_local_json" / "check.py"
        of_worktree = wt / f"{wt.name}--settings_json" / "check.py"
        author = "harness:test-author"
        # the lib's answer for (main, ("settings.json",)) makes it a test
        self.assertIsNone(self.edit(sentinel, "tests_first.py", main_only, wt, author))
        self.assertEqual(self.edit(sentinel, "tests_first.py", main_only, wt, "harness:coder"), CODER_ON_TEST)
        # any other root or names would have named another directory
        self.assertEqual(self.edit(sentinel, "tests_first.py", with_local, wt, author), AUTHOR_ON_SOURCE)
        self.assertEqual(self.edit(sentinel, "tests_first.py", of_worktree, wt, author), AUTHOR_ON_SOURCE)
        # and the default tests/** no longer counts: the patterns came from the lib
        self.assertEqual(
            self.edit(sentinel, "tests_first.py", wt / "tests" / "test_app.py", wt, author), AUTHOR_ON_SOURCE
        )
        # with the real lib the same directory is plain source
        self.assertEqual(self.edit(real, "tests_first.py", main_only, wt, author), AUTHOR_ON_SOURCE)
        self.assertIsNone(self.edit(real, "tests_first.py", wt / "tests" / "test_app.py", wt, author))

    def test_a_wholesale_rewrite_is_judged_from_the_main_checkouts_settings_json_only(self):
        sentinel = self.plugin("sentinel")
        coder = "harness:coder"
        for subdir, refused in [
            ("{main}--settings_json", True),
            ("{main}--settings_json+settings_local_json", False),
            ("{wt}--settings_json", False),
        ]:
            with self.subTest(subdir=subdir):
                repo, wt = self.ticket_worktree()
                where = wt / subdir.format(main=repo.name, wt=wt.name)
                where.mkdir()
                (where / "check.py").write_text("x = 1\n")
                got = self.bash(sentinel, "git reset --hard", wt, coder)
                if refused:
                    self.assertEqual(got, f'Refused "git reset --hard": {CODER_ON_TEST}')
                else:
                    self.assertIsNone(got)  # tests/test_app.py is not a test under the lib's patterns


class BranchGuardFollowsTheLibTests(TreeTestCase):
    def test_allow_main_comes_from_the_libs_settings_env(self):
        """The sentinel settings_env sets HARNESS_ALLOW_MAIN=1 when asked for the two
        project files: branch_guard then allows an edit on main that the real lib denies."""
        sentinel = self.plugin("sentinel")
        real = self.plugin("real")
        repo = self.make_repo()
        target = repo / "src" / "app.py"
        denied = self.edit(real, "branch_guard.py", target, repo)
        self.assertIsNotNone(denied)
        self.assertTrue(denied.startswith("On main in proj."), denied)
        self.assertIsNone(self.edit(sentinel, "branch_guard.py", target, repo))


class LoadingTests(TreeTestCase):
    def assert_one_line(self, r, prefix: str, lib: str, code: int) -> None:
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith(prefix + ":"), lines[0])
        self.assertIn("harness_lib", lines[0])
        if lib == "settings-raises":
            self.assertIn(self.token, lines[0])

    def guard_event(self, hook: str):
        """(payload, cwd) for an edit `hook` denies with a working lib."""
        if hook == "branch_guard.py":
            repo = self.make_repo()
            tool_input = {"file_path": str(repo / "src/app.py"), "old_string": "a", "new_string": "b"}
            return self.payload("Edit", tool_input, repo), repo
        if hook == "tests_first.py":
            _, wt = self.ticket_worktree()
            tool_input = {"file_path": str(wt / "src/app.py"), "old_string": "a", "new_string": "b"}
            return self.payload("Edit", tool_input, wt, "harness:test-author"), wt
        repo = self.make_repo()
        return self.stop_payload(repo), repo

    def test_with_the_real_lib_the_hooks_work(self):
        root = self.plugin("real")
        for hook in ("branch_guard.py", "tests_first.py"):
            with self.subTest(hook=hook):
                payload, cwd = self.guard_event(hook)
                r = self.run_script(root / "hooks" / hook, payload=payload, cwd=cwd)
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                self.assertIsNotNone(self.deny_reason(r))
        payload, cwd = self.guard_event("verify_on_stop.py")
        r = self.run_script(root / "hooks" / "verify_on_stop.py", payload=payload, cwd=cwd)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_a_missing_or_broken_settings_lib_fails_the_hooks_open_with_one_line(self):
        for lib in BROKEN:
            root = self.plugin(lib)
            for hook in ("branch_guard.py", "tests_first.py", "verify_on_stop.py"):
                with self.subTest(lib=lib, hook=hook):
                    payload, cwd = self.guard_event(hook)
                    r = self.run_script(root / "hooks" / hook, payload=payload, cwd=cwd)
                    self.assert_one_line(r, hook, lib, 0)
                    self.assertEqual(r.stdout.strip(), "")  # no decision: allowed

    def test_with_the_real_lib_the_tools_run(self):
        root = self.plugin("real")
        for tool in TOOLS:
            with self.subTest(tool=tool):
                r = self.run_script(root / "bin" / tool, ["--help"])
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                self.assertIn("usage:", r.stdout)

    def test_a_missing_or_broken_settings_lib_stops_the_tools_with_one_line(self):
        for lib in BROKEN:
            root = self.plugin(lib)
            for tool in TOOLS:
                with self.subTest(lib=lib, tool=tool):
                    r = self.run_script(root / "bin" / tool, ["--help"])
                    self.assert_one_line(r, tool, lib, 1)
                    self.assertNotIn("usage:", r.stdout)

    def test_the_tools_find_the_lib_from_their_real_path(self):
        """A symlink to the checkout's tool, next to a lib whose settings.py raises, still
        loads the checkout's lib; a copy next to it does not."""
        self.assertTrue(SETTINGS_PY.is_file(), f"{SETTINGS_PY} does not exist")
        elsewhere = self.plugin("settings-raises")
        for tool in TOOLS:
            with self.subTest(tool=tool):
                link = elsewhere / "bin" / (tool + "-link")
                os.symlink(BIN / tool, link)
                r = self.run_script(link, ["--help"])
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                self.assertIn("usage:", r.stdout)
                copied = self.run_script(elsewhere / "bin" / tool, ["--help"])
                self.assert_one_line(copied, tool, "settings-raises", 1)


# ================================================================ harness-scaffold untouched


class ScaffoldUntouchedTests(unittest.TestCase):
    def test_test_file_patterns_is_its_own_unchanged_tuple(self):
        """harness-scaffold keeps its hand-copied subset, as a literal of its own: deriving
        it from the shared default would change own_layout() detection."""
        tree = parse(BIN / "harness-scaffold")
        assigned = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TEST_FILE_PATTERNS" for t in node.targets)
        ]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(
            ast.literal_eval(assigned[0]),
            ("test_*.py", "*_test.py", "conftest.py", "*_test.js", "*.test.js", "*.test.ts", "*_test.ts"),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("harness_lib"):
                self.assertNotIn("TEST_FILE_PATTERNS", [a.name for a in node.names])


if __name__ == "__main__":
    unittest.main()
