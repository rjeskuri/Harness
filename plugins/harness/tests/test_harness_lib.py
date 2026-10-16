"""Tests for the shared module plugins/harness/lib/harness_lib (ticket #79): the state-key
helpers in harness_lib/paths.py, how the hooks load them, and what a hook does when the
lib is missing or broken.

- The lib's five functions (canonical_path, same_entry, path_key, repo_key,
  adopt_old_state), imported from lib/ and checked against keys computed here from first
  principles on real temporary directories.
- session_brief.py, tests_first.py and branch_guard.py define none of the five any more,
  and the names they use are bound in their own namespace from harness_lib.paths, both
  when loaded with SourceFileLoader (tests/test_repo_key.py) and when exec'd into a bare
  module (tests/test_guards.py).
- Fail open: each hook, copied with hooks/ into a temporary plugin tree whose lib/ is
  missing or broken, run as hooks.json runs it (`python3 <plugin>/hooks/<name>.py`, JSON
  on stdin, the project as cwd) on an event it would deny with a working lib: exit 0,
  one line on stderr naming harness_lib, no traceback, and no decision on stdout. The
  same tree with the real lib copied in denies, so the event is one that matters.
- The session warning: session_brief.py from a tree with a missing or broken lib still
  prints its SessionStart JSON, and the additionalContext carries one line naming
  harness_lib (stderr of a SessionStart hook that exits 0 reaches no one).
- The lib is found from the hook's real path: a symlink to branch_guard.py placed next to
  a broken lib still loads the checkout's lib and denies.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib.py" -v
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path

sys.dont_write_bytecode = True  # loading the hooks and the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
PATHS_PY = LIB / "harness_lib" / "paths.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
BRANCH = "t/12-fix-login"
FIVE = ("canonical_path", "same_entry", "path_key", "repo_key", "adopt_old_state")
# The names each hook uses, per the ticket: branch_guard had only canonical_path and same_entry.
USES = {
    "session_brief.py": FIVE,
    "tests_first.py": FIVE,
    "branch_guard.py": ("canonical_path", "same_entry"),
}
HOOK_NAMES = tuple(USES)

_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-lib-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def sha12(data) -> str:
    """The key of a path, computed here: sha1 of its bytes, first 12 hex digits."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha1(data).hexdigest()[:12]


def case_insensitive(directory: str) -> bool:
    probe = os.path.join(directory, "CaseProbe")
    os.mkdir(probe)
    try:
        return os.path.isdir(os.path.join(directory, "cASEpROBE"))
    finally:
        os.rmdir(probe)


def swapped_below(path: str, root: str) -> str:
    """`path` with every component below `root` case-swapped."""
    rel = os.path.relpath(path, root)
    return os.path.join(root, *[part.swapcase() for part in rel.split(os.sep)])


def load_lib():
    """harness_lib.paths imported from the checkout's lib/, as a hook's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.paths")


def load_with_loader(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_repo_key.py loads it."""
    modname = path.name.replace("-", "_").replace(".py", "") + "_lib_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_guards.py loads it: a bare module with __file__."""
    module = types.ModuleType("harness_hook_lib_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


# ================================================================ the lib itself


class LibPathsTests(unittest.TestCase):
    """The five helpers, imported from lib/harness_lib/paths.py, on real directories."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_lib()

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="LibKey-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tag = uuid.uuid4().hex[:6].upper()
        self.outer = os.path.join(self.tmp, f"Outer{tag}x")
        self.proj = os.path.join(self.outer, f"Proj{tag}q")
        self.other = os.path.join(self.outer, f"Other{tag}z")
        for path in (self.proj, self.other):
            os.makedirs(path)
        self.insensitive = case_insensitive(self.tmp)

    def test_the_package_and_module_are_in_lib(self):
        self.assertTrue((LIB / "harness_lib" / "__init__.py").is_file())
        self.assertTrue(PATHS_PY.is_file())
        self.assertEqual(
            os.path.realpath(self.lib.__file__), os.path.realpath(str(PATHS_PY))
        )
        for name in FIVE:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(self.lib, name, None)))

    def test_path_key_is_sha1_of_the_spelling(self):
        cases = [
            ("/", sha12("/")),
            ("/Users/a/Harness", sha12("/Users/a/Harness")),
            ("/Users/a/harness", sha12("/Users/a/harness")),
            ("/tmp/with space/x", sha12("/tmp/with space/x")),
            ("/tmp/café", sha12("/tmp/café".encode("utf-8"))),
            ("/tmp/café", sha12("/tmp/café".encode("utf-8"))),
            ("/tmp/\udcff-raw", sha12(b"/tmp/\xff-raw")),  # surrogateescape: the raw byte
            (self.proj, sha12(self.proj)),
        ]
        keys = set()
        for given, expected in cases:
            with self.subTest(path=given):
                self.assertEqual(self.lib.path_key(given), expected)
                self.assertRegex(expected, r"^[0-9a-f]{12}$")
                keys.add(expected)
        self.assertEqual(len(keys), len(cases))
        # a Path is keyed by its string
        self.assertEqual(self.lib.path_key(Path(self.other)), sha12(self.other))

    def test_on_disk_spelling_is_its_own_canonical_path_and_key(self):
        for path in (self.tmp, self.outer, self.proj, self.other):
            with self.subTest(path=path):
                self.assertEqual(self.lib.canonical_path(path), path)
                self.assertEqual(self.lib.repo_key(path), sha12(path))
        self.assertEqual(self.lib.canonical_path(self.proj + os.sep), self.proj)
        self.assertNotEqual(self.lib.repo_key(self.proj), self.lib.repo_key(self.other))

    def test_symlinks_resolve_and_missing_tails_stay_as_spelled(self):
        link = os.path.join(self.tmp, "LinkToProj")
        os.symlink(self.proj, link)
        self.assertEqual(self.lib.canonical_path(link), self.proj)
        self.assertEqual(self.lib.repo_key(link), sha12(self.proj))
        missing = os.path.join(self.proj, "NotThere", "DeepER")
        self.assertEqual(self.lib.canonical_path(missing), missing)
        self.assertEqual(
            self.lib.canonical_path(os.path.join(link, "NotThere")),
            os.path.join(self.proj, "NotThere"),
        )

    def test_other_case_spellings_give_the_on_disk_spelling(self):
        if not self.insensitive:
            self.skipTest("two spellings of one path need a case-insensitive file system")
        for canonical in (self.outer, self.proj, self.other):
            rel = os.path.relpath(canonical, self.tmp)
            for given in (
                swapped_below(canonical, self.tmp),
                os.path.join(self.tmp, rel.lower()),
                os.path.join(self.tmp, rel.upper()),
                os.path.join(os.path.dirname(canonical), os.path.basename(canonical).lower()),
            ):
                with self.subTest(given=given):
                    self.assertNotEqual(given, canonical)
                    self.assertEqual(self.lib.canonical_path(given), canonical)
                    self.assertEqual(self.lib.repo_key(given), sha12(canonical))
                    self.assertEqual(self.lib.path_key(given), sha12(given))
        # a missing tail below a respelled existing head: head canonical, tail as given
        given = os.path.join(swapped_below(self.proj, self.tmp), "NewDir")
        self.assertEqual(self.lib.canonical_path(given), os.path.join(self.proj, "NewDir"))

    def test_same_entry(self):
        a = os.path.join(self.proj, "a.txt")
        b = os.path.join(self.proj, "b.txt")
        for path in (a, b):
            Path(path).write_text("x")
        link = os.path.join(self.other, "to-a")
        os.symlink(a, link)
        self.assertTrue(self.lib.same_entry(a, a))
        self.assertTrue(self.lib.same_entry(a, link))
        self.assertTrue(self.lib.same_entry(self.proj, self.proj + os.sep))
        self.assertFalse(self.lib.same_entry(a, b))
        self.assertFalse(self.lib.same_entry(self.proj, self.other))
        self.assertFalse(self.lib.same_entry(a, os.path.join(self.proj, "gone")))
        self.assertFalse(self.lib.same_entry(os.path.join(self.proj, "gone"), a))

    def test_adopt_old_state_without_old_state_creates_nothing(self):
        repos = os.path.join(self.tmp, "repos")
        os.mkdir(repos)
        self.assertEqual(self.lib.adopt_old_state(repos, self.proj, self.proj, ""), sha12(self.proj))
        self.assertEqual(os.listdir(repos), [])

    def test_adopt_old_state_moves_the_old_spelling_once(self):
        if not self.insensitive:
            self.skipTest("an old spelling-based key needs a case-insensitive file system")
        repos = os.path.join(self.tmp, "repos")
        old_spelling = swapped_below(self.proj, self.tmp)
        old = os.path.join(repos, sha12(old_spelling))
        os.makedirs(os.path.join(old, "tickets"))
        Path(old, "tickets", "12.json").write_text('{"number": 12}')
        # another repository's old directory must not move
        other_old = os.path.join(repos, sha12(swapped_below(self.other, self.tmp)))
        os.makedirs(other_old)

        key = self.lib.adopt_old_state(repos, self.proj, "", old_spelling)
        self.assertEqual(key, sha12(self.proj))
        target = os.path.join(repos, key)
        self.assertEqual(Path(target, "tickets", "12.json").read_text(), '{"number": 12}')
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(other_old))
        # a second call finds the canonical directory and changes nothing
        self.assertEqual(self.lib.adopt_old_state(repos, self.proj, old_spelling), key)
        self.assertEqual(sorted(os.listdir(repos)), sorted([key, os.path.basename(other_old)]))

    def test_adopt_old_state_with_both_keeps_both(self):
        if not self.insensitive:
            self.skipTest("an old spelling-based key needs a case-insensitive file system")
        repos = os.path.join(self.tmp, "repos")
        old_spelling = swapped_below(self.proj, self.tmp)
        old = os.path.join(repos, sha12(old_spelling))
        new = os.path.join(repos, sha12(self.proj))
        os.makedirs(old)
        os.makedirs(new)
        self.assertEqual(self.lib.adopt_old_state(repos, self.proj, old_spelling), sha12(self.proj))
        self.assertTrue(os.path.isdir(old) and os.path.isdir(new))

    def test_adopt_old_state_from_a_path_inside_the_tree(self):
        """The start path may be deeper than the main tree (an edited file, a subdir)."""
        if not self.insensitive:
            self.skipTest("an old spelling-based key needs a case-insensitive file system")
        repos = os.path.join(self.tmp, "repos")
        sub = os.path.join(self.proj, "Src")
        os.mkdir(sub)
        old_spelling = swapped_below(self.proj, self.tmp)
        os.makedirs(os.path.join(repos, sha12(old_spelling)))
        key = self.lib.adopt_old_state(repos, self.proj, swapped_below(sub, self.tmp))
        self.assertEqual(key, sha12(self.proj))
        self.assertEqual(os.listdir(repos), [key])


# ================================================================ the hooks import, not copy


def normalized_body(fn) -> str:
    """The body of a def without its docstring, every name and argument spelled `_`, so a
    copy renamed (or calling renamed helpers) compares equal to the original."""
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


def defs(tree) -> list:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


# repo_key's body (`return path_key(canonical_path(path))`) normalises to a shape too
# common to compare; the instrumented-lib runs below cover it.
COMPARED = ("canonical_path", "same_entry", "path_key", "adopt_old_state")


def reference_bodies() -> dict:
    """Normalised bodies of the four helpers, from the lib and from every bin tool that
    still carries a copy: {normalised body: helper name}."""
    sources = [PATHS_PY] if PATHS_PY.is_file() else []
    sources += [p for p in sorted((PLUGIN / "bin").iterdir()) if p.is_file()]
    found = {}
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for fn in defs(tree):
            if fn.name in COMPARED:
                found[normalized_body(fn)] = fn.name
    return found


def state_key_slices(tree) -> list[str]:
    """Where a hook cuts a digest to 12 hex digits (`<x>.hexdigest()[:12]`): the shape
    of path_key's state key."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            value, piece = node.value, node.slice
            if isinstance(piece, ast.Index):  # Python 3.8
                piece = piece.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "hexdigest"
                and isinstance(piece, ast.Slice)
                and isinstance(piece.upper, ast.Constant)
                and piece.upper.value == 12
            ):
                found.append(f"hexdigest()[:12] (line {node.lineno})")
    return found


def docstring_ids(tree) -> set:
    """id() of every Constant that is a module, class or function docstring."""
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


def spelled(tree, words: tuple, docs: set | None = None) -> list[str]:
    """Where `tree` spells one of `words` (case ignored): as a name, an attribute, an
    imported module, or inside a string that is not a docstring (so hashlib.new("sha1")
    and getattr(os, "listdir") count; prose does not). `docs` are the docstring ids of
    the whole module when `tree` is one def inside it."""
    if docs is None:
        docs = docstring_ids(tree)
    found = []

    def hit(text: str, node) -> None:
        low = text.lower()
        for word in words:
            if word in low:
                found.append(f"{word} in {text!r} (line {node.lineno})")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            hit(node.id, node)
        elif isinstance(node, ast.Attribute):
            hit(node.attr, node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                hit(alias.name, node)
        elif isinstance(node, ast.ImportFrom):
            hit(node.module or "", node)
            for alias in node.names:
                hit(alias.name, node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                hit(node.value, node)
    return found


def _literal(expr) -> bool:
    return isinstance(expr, ast.Constant) and isinstance(expr.value, str)


def _on_hashlib(expr) -> bool:
    return isinstance(expr, ast.Name) and expr.id == "hashlib"


def hashlib_by_computed_name(tree) -> list[str]:
    """hashlib.new(<not a literal>) and getattr(hashlib, <not a literal>): the two ways
    to reach sha1 without spelling it. Any other computed name is allowed."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func, args = node.func, node.args
        if isinstance(func, ast.Attribute) and func.attr == "new" and _on_hashlib(func.value):
            if not (args and _literal(args[0])):
                found.append(f"hashlib.new with a computed name (line {node.lineno})")
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(args) > 1
            and _on_hashlib(args[0])
            and not _literal(args[1])
        ):
            found.append(f"getattr(hashlib, <computed>) (line {node.lineno})")
    return found


def spelling_walks(tree) -> list[str]:
    """Each def that both lists a directory (listdir, scandir) and compares spellings
    (casefold, samefile): canonical_path's component walk, or a copy of it. Either
    one alone is not reported."""
    docs = docstring_ids(tree)
    found = []
    for fn in defs(tree):
        lists = spelled(fn, ("listdir", "scandir"), docs)
        compares = spelled(fn, ("casefold", "samefile"), docs)
        if lists and compares:
            found.append(f"{fn.name} (line {fn.lineno}): {lists[0]}; {compares[0]}")
    return found


def hook_tree(name: str):
    return ast.parse((HOOKS / name).read_text(encoding="utf-8"), filename=name)


class HooksImportTheLibTests(unittest.TestCase):
    # Static backstops against a copy of the five helpers in a hook. They are narrow on
    # purpose: the instrumented-lib runs in HooksFollowTheLibTests are the main guard
    # (they catch a disguised copy by what the hook does), and each check here bans only
    # a shape a copy needs. When a later change has a legitimate use of a banned shape,
    # revisit the check and its docstring rather than work around it.

    def test_no_hook_computes_a_sha1(self):
        """Guards against a hook computing a state key of its own (path_key's sha1):
        no hook spells sha1 or _hashlib outside its docstrings (hashlib.sha1,
        hashlib.new("sha1"), getattr(hashlib, "sha1"), _sha1), reaches hashlib by a
        computed name, or cuts a digest to [:12]. A legitimate future sha1 in a hook (a
        content hash, say) means revisiting this check."""
        for name in HOOK_NAMES:
            tree = hook_tree(name)
            with self.subTest(hook=name):
                self.assertEqual(spelled(tree, ("sha1", "_hashlib")), [])
                self.assertEqual(hashlib_by_computed_name(tree), [])
                self.assertEqual(state_key_slices(tree), [])

    def test_no_hook_walks_a_directory_to_match_a_spelling(self):
        """Guards against a copy of canonical_path's component walk: no def in a hook
        both lists a directory (listdir, scandir) and compares spellings (casefold,
        samefile). Either one alone is allowed (str.casefold, os.scandir). A legitimate
        future def that needs both means revisiting this check."""
        for name in HOOK_NAMES:
            with self.subTest(hook=name):
                self.assertEqual(spelling_walks(hook_tree(name)), [])

    def test_no_hook_carries_a_renamed_copy(self):
        """Guards against a renamed copy: no def in a hook has the body (names and
        docstring aside) of canonical_path, same_entry, path_key or adopt_old_state as
        the lib or a bin tool spells it. A legitimate def that happens to normalise to
        one of those bodies means revisiting this check."""
        refs = reference_bodies()
        self.assertEqual(sorted(set(refs.values())), sorted(COMPARED), "reference bodies missing")
        for name in HOOK_NAMES:
            tree = ast.parse((HOOKS / name).read_text(encoding="utf-8"), filename=name)
            copies = [
                f"{fn.name} (line {fn.lineno}) = {refs[normalized_body(fn)]}"
                for fn in defs(tree)
                if normalized_body(fn) in refs
            ]
            with self.subTest(hook=name):
                self.assertEqual(copies, [])

    def test_no_hook_defines_any_of_the_five(self):
        """Guards against a copy under the lib's own name, which would shadow the import."""
        for name in HOOK_NAMES:
            tree = ast.parse((HOOKS / name).read_text(encoding="utf-8"), filename=name)
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            with self.subTest(hook=name):
                self.assertEqual(sorted(defined & set(FIVE)), [])

    def check_bound_from_lib(self, module: types.ModuleType, hook: str) -> None:
        paths = os.path.realpath(str(PATHS_PY))
        for fn in USES[hook]:
            with self.subTest(hook=hook, name=fn):
                bound = module.__dict__.get(fn)  # in the hook's own namespace
                self.assertTrue(callable(bound), f"{hook} does not bind {fn}")
                self.assertEqual(bound.__module__, "harness_lib.paths")
                self.assertEqual(os.path.realpath(inspect.getsourcefile(bound)), paths)

    def test_names_bound_from_the_lib_with_source_file_loader(self):
        for hook in HOOK_NAMES:
            self.check_bound_from_lib(load_with_loader(HOOKS / hook), hook)

    def test_names_bound_from_the_lib_in_a_bare_module(self):
        for hook in HOOK_NAMES:
            self.check_bound_from_lib(load_with_exec(HOOKS / hook), hook)


# ================================================================ fail open and the warning


class LoaderTestCase(unittest.TestCase):
    """Runs hooks from a temporary plugin tree: hooks/ copied from the checkout, and lib/
    real, missing or broken."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-lib-")).resolve()
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

    # ---- fixtures

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return out.stdout.strip()

    def make_repo(self, name: str = "proj") -> Path:
        """A repo on main at a fresh <tmp>/case<n>/<name>, with README.md and src/app.py."""
        self.cases = getattr(self, "cases", 0) + 1
        repo = self.tmp / f"case{self.cases}" / name
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
            env=self.env,
        )
        (repo / "src").mkdir()
        (repo / "tests").mkdir()
        (repo / "README.md").write_text("hello\n")
        (repo / "src/app.py").write_text("def login():\n    return None\n")
        (repo / "tests/test_app.py").write_text("def test_login():\n    assert False\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def write_lib(self, root: Path, files: dict) -> Path:
        pkg = root / "lib" / "harness_lib"
        pkg.mkdir(parents=True)
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")
        return root / "lib"

    def plugin(self, lib: str) -> Path:
        """A plugin tree under tmp: the checkout's hooks/ and a lib/ that is
        'real' (copied from the checkout), 'missing', 'syntax' (paths.py does not parse),
        'raises' (__init__.py raises RuntimeError) or 'incomplete' (paths.py lacks the names)."""
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(
            HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__")
        )
        init = "from __future__ import annotations\n"
        if lib == "real":
            self.assertTrue(PATHS_PY.is_file(), f"{PATHS_PY} does not exist")
            shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        elif lib == "syntax":
            self.write_lib(root, {"__init__.py": init, "paths.py": "def canonical_path(:\n    return\n"})
        elif lib == "raises":
            self.write_lib(
                root,
                {"__init__.py": f"raise RuntimeError({self.token!r})\n", "paths.py": ""},
            )
        elif lib == "incomplete":
            self.write_lib(
                root,
                {"__init__.py": init, "paths.py": "def path_key(path):\n    return 'x'\n"},
            )
        else:
            self.assertEqual(lib, "missing")
        return root

    def payload(self, tool: str, tool_input: dict, cwd: Path) -> dict:
        return {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_01",
        }

    def run_hook(self, script: Path, payload: dict, cwd: Path):
        return subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(cwd),
            timeout=60,
        )

    def deny_reason(self, r) -> str | None:
        if not r.stdout.strip():
            return None
        d = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        return d["permissionDecisionReason"]

    def assert_one_line_fail_open(self, r, hook: str, lib: str) -> str:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        line = lines[0]
        self.assertTrue(line.strip())
        self.assertTrue(line.startswith(f"{hook}:"), line)
        self.assertIn("harness_lib", line)
        if lib == "raises":
            self.assertIn(self.token, line)  # the error itself, not a fixed message
        return line

    # ---- the two guards' events, denied with a working lib

    def guard_event(self, hook: str):
        """(payload, cwd, expected deny prefix) for an edit `hook` denies."""
        repo = self.make_repo()
        if hook == "branch_guard.py":
            tool_input = {"file_path": str(repo / "src/app.py"), "old_string": "a", "new_string": "b"}
            return self.payload("Edit", tool_input, repo), repo, "On main in proj."
        wt = repo.parent / "wt"
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        tool_input = {"file_path": str(wt / "src/app.py"), "old_string": "a", "new_string": "b"}
        return self.payload("Edit", tool_input, wt), wt, "Tests first on t/12"


BROKEN = ("missing", "syntax", "raises", "incomplete")


class GuardFailOpenTests(LoaderTestCase):
    def test_with_the_real_lib_the_copied_guards_deny(self):
        root = self.plugin("real")
        for hook in ("branch_guard.py", "tests_first.py"):
            with self.subTest(hook=hook):
                payload, cwd, prefix = self.guard_event(hook)
                r = self.run_hook(root / "hooks" / hook, payload, cwd)
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                reason = self.deny_reason(r)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith(prefix), reason)
        # and allows agent notes on main, which the flattening lib below turns into a deny
        repo = self.make_repo()
        (repo / "docs" / "agent").mkdir(parents=True)
        tool_input = {"file_path": str(repo / "docs/agent/handoff.md"), "content": "x\n"}
        r = self.run_hook(root / "hooks" / "branch_guard.py", self.payload("Write", tool_input, repo), repo)
        self.assertEqual((r.returncode, r.stderr, r.stdout), (0, "", ""))

    def test_a_missing_or_broken_lib_allows_with_one_line(self):
        for lib in BROKEN:
            root = self.plugin(lib)
            for hook in ("branch_guard.py", "tests_first.py"):
                with self.subTest(lib=lib, hook=hook):
                    payload, cwd, _ = self.guard_event(hook)
                    r = self.run_hook(root / "hooks" / hook, payload, cwd)
                    self.assert_one_line_fail_open(r, hook, lib)
                    self.assertEqual(r.stdout.strip(), "")  # no decision: the call is allowed

    def test_the_lib_is_found_from_the_hooks_real_path(self):
        """A symlink to the checkout's branch_guard.py, next to a broken lib/, still loads
        the checkout's lib: the prologue resolves __file__ with realpath."""
        self.assertTrue(PATHS_PY.is_file(), f"{PATHS_PY} does not exist")
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "hooks").mkdir(parents=True)
        self.write_lib(
            elsewhere, {"__init__.py": f"raise RuntimeError({self.token!r})\n", "paths.py": ""}
        )
        link = elsewhere / "hooks" / "branch_guard.py"
        os.symlink(HOOKS / "branch_guard.py", link)
        payload, cwd, prefix = self.guard_event("branch_guard.py")
        r = self.run_hook(link, payload, cwd)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        reason = self.deny_reason(r)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith(prefix), reason)


class SessionBriefWarningTests(LoaderTestCase):
    def brief(self, root: Path, project: Path):
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(project),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        r = self.run_hook(root / "hooks" / "session_brief.py", payload, project)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(r.stdout.strip(), f"no SessionStart output; stderr: {r.stderr!r}")
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        return r, out["additionalContext"].splitlines()

    def test_the_real_lib_gives_a_brief_with_no_warning(self):
        project = self.make_repo()
        r, lines = self.brief(self.plugin("real"), project)
        self.assertEqual(r.stderr, "")
        self.assertTrue(lines and lines[0].startswith("## Session brief: proj"), lines)
        self.assertEqual([ln for ln in lines if "harness_lib" in ln], [])

    def test_a_missing_or_broken_lib_shows_one_warning_line(self):
        project = self.make_repo()
        for lib in BROKEN:
            with self.subTest(lib=lib):
                r, lines = self.brief(self.plugin(lib), project)
                warnings = [ln for ln in lines if "harness_lib" in ln]
                self.assertEqual(len(warnings), 1, lines)
                if lib == "raises":
                    self.assertIn(self.token, warnings[0])
                # and, like every hook, one line on stderr
                self.assert_one_line_fail_open(r, "session_brief.py", lib)

    def test_a_broken_lib_warns_outside_a_git_repo_too(self):
        project = self.tmp / "plain"
        project.mkdir()
        r, lines = self.brief(self.plugin("missing"), project)
        self.assertEqual(len([ln for ln in lines if "harness_lib" in ln]), 1, lines)

    def own_session_start(self, project: Path) -> None:
        """The project declares its own SessionStart hook (as in tests/test_hooks.py), and
        has a handoff whose next steps the full brief would show."""
        (project / ".claude").mkdir(parents=True, exist_ok=True)
        own = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}
        (project / ".claude" / "settings.json").write_text(json.dumps(own))
        (project / "docs" / "agent").mkdir(parents=True, exist_ok=True)
        (project / "docs/agent/handoff.md").write_text(
            f"# Handoff\n\n## Next steps\n- finish {self.token}\n"
        )

    def test_a_broken_lib_warns_even_when_the_project_has_its_own_session_start(self):
        """The project's own SessionStart hook replaces the brief, but not the warning:
        a broken or missing lib still gives exactly one line, the harness_lib warning,
        and nothing else of the brief (no heading, no handoff), plus one stderr line."""
        for lib in BROKEN:
            for where in ("repo", "plain"):
                with self.subTest(lib=lib, where=where):
                    if where == "repo":
                        project = self.make_repo()
                    else:
                        self.cases = getattr(self, "cases", 0) + 1
                        project = self.tmp / f"case{self.cases}" / "plain"
                        project.mkdir(parents=True)
                    self.own_session_start(project)
                    r, lines = self.brief(self.plugin_once(lib), project)
                    self.assertEqual(len(lines), 1, lines)
                    self.assertIn("harness_lib", lines[0])
                    self.assertFalse(lines[0].startswith("## Session brief"), lines)
                    self.assertNotIn(f"finish {self.token}", lines[0])  # not the handoff
                    if lib == "raises":
                        self.assertIn(self.token, lines[0])
                    self.assert_one_line_fail_open(r, "session_brief.py", lib)

    def test_the_real_lib_with_its_own_session_start_gives_no_output(self):
        """With the real lib, a project's own SessionStart hook still means no brief at
        all, as before: empty stdout, empty stderr, exit 0."""
        root = self.plugin("real")
        repo = self.make_repo()
        plain = self.tmp / "plain-own"
        plain.mkdir()
        for project in (repo, plain):
            with self.subTest(project=project.name):
                self.own_session_start(project)
                payload = {
                    "session_id": "sess-1",
                    "transcript_path": "/dev/null",
                    "cwd": str(project),
                    "permission_mode": "default",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                }
                r = self.run_hook(root / "hooks" / "session_brief.py", payload, project)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def plugin_once(self, lib: str) -> Path:
        root = self.tmp / f"plugin-{lib}"
        return root if root.is_dir() else self.plugin(lib)

    def test_a_broken_lib_gives_the_brief_without_ticket_state(self):
        """Without the lib there is no state key: the brief keeps its heading and notes
        and adds the warning, but shows no ticket or red-run line even though the ticket
        cache sits under the repo's real key (the hook computes no key of its own)."""
        for lib in BROKEN:
            with self.subTest(lib=lib):
                repo = self.make_repo()
                self.git(repo, "checkout", "-q", "-b", BRANCH)
                (repo / "docs" / "agent").mkdir(parents=True)
                (repo / "docs/agent/handoff.md").write_text(
                    f"# Handoff\n\n## Next steps\n- finish {self.token}\n"
                )
                state = self.home / ".claude" / "harness" / "repos" / sha12(str(repo))
                (state / "tickets").mkdir(parents=True, exist_ok=True)
                (state / "red").mkdir(exist_ok=True)
                (state / "tickets" / "12.json").write_text(
                    json.dumps({"number": 12, "title": "From a private key", "status": "in-progress"})
                )
                (state / "red" / "t-12-fix-login.json").write_text(
                    json.dumps({"kind": "assertion", "at": "2026-10-13T10:00:00Z"})
                )
                r, lines = self.brief(self.plugin(lib), repo)
                self.assertTrue(lines and lines[0].startswith("## Session brief: proj on t/12-fix-login"), lines)
                self.assertEqual([ln for ln in lines if ln.startswith(("Ticket #", "Red run:"))], [], lines)
                self.assertNotIn("private key", "\n".join(lines))
                self.assertIn(f"- finish {self.token}", lines)  # the rest of the brief is intact
                self.assertEqual(len([ln for ln in lines if "harness_lib" in ln]), 1, lines)


# ================================================================ the hooks run the lib's code


# A lib that imports cleanly and exports all five names, with a path_key no real key
# matches: TOKEN-<md5 of the path, 8 hex>. repo_key and adopt_old_state go through it.
SENTINEL_PATHS = '''from __future__ import annotations
import hashlib
import os

TOKEN = {token!r}


def canonical_path(path):
    return os.path.realpath(path)


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    return TOKEN + "-" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''

# canonical_path gives a new string on every call: git's two spellings of a main tree's
# .git never compare equal, so the main checkout looks like a linked worktree.
SPLIT_PATHS = '''from __future__ import annotations
import itertools
import os

_COUNT = itertools.count()


def canonical_path(path):
    return os.path.realpath(path) + "-{token}-" + str(next(_COUNT))


def same_entry(a, b):
    return False


def path_key(path):
    return "{token}"


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''

# canonical_path keeps only the last component under a fixed root: the main tree is still
# the main tree (.git == .git), but a file under docs/agent/ no longer sits under it.
FLAT_PATHS = '''from __future__ import annotations
import os


def canonical_path(path):
    return os.path.join("/{token}", os.path.basename(os.path.realpath(str(path))))


def same_entry(a, b):
    return False


def path_key(path):
    return "{token}"


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''

# canonical_path puts every path under /<token>: a spelling realpath never gives, so a
# hook that passes its own canonical_path(reported) to the key functions keys the state
# under a different directory than one that passes the lib's. path_key is the sentinel's.
PREFIX_PATHS = '''from __future__ import annotations
import hashlib
import os

TOKEN = {token!r}


def canonical_path(path):
    return "/" + TOKEN + os.path.realpath(str(path))


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    return TOKEN + "-" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''

# canonical_path swaps two path components (SWAP, filled in per test): a repo top keeps
# its spelling, but a file under one directory is spelled under the other, so the
# repo-relative path a hook classifies or exempts changes with the lib.
SWAP_TEMPLATE = '''from __future__ import annotations
import hashlib
import os

TOKEN = {token!r}
SWAP = @SWAP@


def canonical_path(path):
    real = os.path.realpath(str(path))
    return os.sep.join(SWAP.get(part, part) for part in real.split(os.sep))


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    return TOKEN + "-" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''


def swap_paths(mapping: dict) -> str:
    return SWAP_TEMPLATE.replace("@SWAP@", repr(mapping))


# canonical_path spells a repository top (a directory holding .git) as its parent
# directory and leaves every other path alone: the main tree is still the main tree
# (.git == .git), but every file in it is now one directory below the top, so what is
# relative to the top (notes, worktrees) is relative to the lib's spelling of the top.
UP_PATHS = '''from __future__ import annotations
import os


def canonical_path(path):
    real = os.path.realpath(str(path))
    if os.path.lexists(os.path.join(real, ".git")):
        return os.path.dirname(real)
    return real


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    return "{token}"


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return path_key(main)
'''


class HooksFollowTheLibTests(LoaderTestCase):
    """Each hook, from a tree whose lib is instrumented, does what that lib's answers
    imply: the hook's behaviour comes from the lib's code, not from a copy of its own."""

    def instrumented(self, label: str, paths_source: str) -> Path:
        root = self.tmp / f"plugin-{label}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        files = {
            "__init__.py": "from __future__ import annotations\n",
            "paths.py": paths_source.replace("{token!r}", repr(self.token)).replace(
                "{token}", self.token
            ),
        }
        # Only paths.py is instrumented; the rest of the lib (hookio.py, #87) is the real one.
        for extra in sorted((LIB / "harness_lib").glob("*.py")):
            if extra.name not in files:
                files[extra.name] = extra.read_text(encoding="utf-8")
        self.write_lib(root, files)
        return root

    def sentinel_key(self, main: Path) -> str:
        """SENTINEL's and PREFIX's path_key of the real path: what a hook that canonicalises
        the main tree itself (realpath) hands the lib's key functions."""
        return self.token + "-" + hashlib.md5(str(main).encode("utf-8")).hexdigest()[:8]

    def prefix_key(self, main: Path) -> str:
        """PREFIX's key of the main tree: path_key of its canonical_path, /<token><real>."""
        spelled = "/" + self.token + str(main)
        return self.token + "-" + hashlib.md5(spelled.encode("utf-8")).hexdigest()[:8]

    def session_payload(self, cwd: Path) -> dict:
        return {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }

    def state(self, key: str) -> Path:
        return self.home / ".claude" / "harness" / "repos" / key

    def write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def red_record(self, top: Path) -> dict:
        files = ["tests/test_app.py"]
        h = hashlib.sha256()
        for name in sorted(files):
            h.update(name.encode() + b"\n" + (top / name).read_bytes())
        return {
            "branch": BRANCH,
            "base_sha": "0" * 40,
            "test_files": files,
            "test_hash": h.hexdigest(),
            "cmd": "python3 -m unittest tests/test_app.py",
            "exit": 1,
            "kind": "assertion",
            "tail": "AssertionError",
            "at": "2026-10-13T10:00:00Z",
        }

    def ticket_worktree(self) -> tuple[Path, Path]:
        repo = self.make_repo()
        wt = repo.parent / "wt"
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        return repo, wt

    def source_edit(self, root: Path, wt: Path):
        tool_input = {"file_path": str(wt / "src/app.py"), "old_string": "a", "new_string": "b"}
        r = self.run_hook(root / "hooks" / "tests_first.py", self.payload("Edit", tool_input, wt), wt)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return self.deny_reason(r)

    def test_tests_first_reads_the_red_run_under_the_libs_key(self):
        root = self.instrumented("sentinel", SENTINEL_PATHS)
        # the red run only under the lib's key: the source edit is allowed
        repo, wt = self.ticket_worktree()
        self.assertNotEqual(self.sentinel_key(repo), sha12(str(repo)))
        self.write_json(self.state(self.sentinel_key(repo)) / "red/t-12-fix-login.json", self.red_record(wt))
        self.assertIsNone(self.source_edit(root, wt))
        # the red run only under the real key: the lib's key has none, so it is refused
        repo, wt = self.ticket_worktree()
        self.write_json(self.state(sha12(str(repo))) / "red/t-12-fix-login.json", self.red_record(wt))
        reason = self.source_edit(root, wt)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("Tests first on t/12"), reason)

    def test_session_brief_reads_the_ticket_under_the_libs_key(self):
        root = self.instrumented("sentinel", SENTINEL_PATHS)
        repo = self.make_repo()
        self.git(repo, "checkout", "-q", "-b", BRANCH)
        ticket = {"number": 12, "status": "in-progress", "tier": "opus", "type": "task"}
        self.write_json(
            self.state(self.sentinel_key(repo)) / "tickets/12.json",
            dict(ticket, title=f"From the lib {self.token}"),
        )
        self.write_json(
            self.state(sha12(str(repo))) / "tickets/12.json",
            dict(ticket, title="From a private copy"),
        )
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        r = self.run_hook(root / "hooks" / "session_brief.py", payload, repo)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        lines = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"].splitlines()
        ticket_lines = [ln for ln in lines if ln.startswith("Ticket #12:")]
        self.assertEqual(len(ticket_lines), 1, lines)
        self.assertIn(f"From the lib {self.token}", ticket_lines[0])
        self.assertNotIn("private copy", "\n".join(lines))

    def branch_guard_edit(self, root: Path, path: Path, repo: Path):
        tool_input = {"file_path": str(path), "content": "x\n"}
        r = self.run_hook(root / "hooks" / "branch_guard.py", self.payload("Write", tool_input, repo), repo)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return self.deny_reason(r)

    def test_branch_guard_judges_main_with_the_libs_canonical_path(self):
        # every call a new spelling: the main checkout is taken for a linked worktree
        root = self.instrumented("split", SPLIT_PATHS)
        repo = self.make_repo()
        self.assertIsNone(self.branch_guard_edit(root, repo / "src/app.py", repo))

    def test_branch_guard_places_notes_with_the_libs_canonical_path(self):
        # flattened paths: still main, but docs/agent/handoff.md is no longer under it
        root = self.instrumented("flat", FLAT_PATHS)
        repo = self.make_repo()
        (repo / "docs" / "agent").mkdir(parents=True)
        reason = self.branch_guard_edit(root, repo / "docs/agent/handoff.md", repo)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("On main in proj."), reason)

    # ---- the call sites the sentinel cannot tell apart: canonical_path == realpath there

    def test_tests_first_classifies_with_the_libs_canonical_path(self):
        """The repo-relative path tests_first classifies comes from the lib's
        canonical_path of the file's directory: under a lib that swaps src and tests, a
        file in tests/ is source (refused without a red run) and one in src/ is a test."""
        root = self.instrumented("swap", swap_paths({"src": "tests", "tests": "src"}))
        repo, wt = self.ticket_worktree()
        helper = {"file_path": str(wt / "tests/helpers.py"), "content": "x = 1\n"}
        r = self.run_hook(root / "hooks" / "tests_first.py", self.payload("Write", helper, wt), wt)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        reason = self.deny_reason(r)
        self.assertIsNotNone(reason, "tests/helpers.py, spelled src/helpers.py by the lib, is source")
        self.assertTrue(reason.startswith("Tests first on t/12"), reason)
        # and the source file, spelled tests/app.py by the lib, is a test: allowed
        self.assertIsNone(self.source_edit(root, wt))

    def test_tests_first_keys_the_state_by_the_libs_canonical_path(self):
        """The main tree handed to adopt_old_state is the lib's canonical_path(reported):
        under PREFIX that spelling is /<token><real>, so the ticket cache and red run are
        found only under its key, not under the key of the real path."""
        root = self.instrumented("prefix", PREFIX_PATHS)
        ticket = {"number": 12, "status": "in-progress", "tier": "opus", "type": "refactor"}
        # under the lib's key: a refactor ticket with a red run of any kind, allowed
        repo, wt = self.ticket_worktree()
        key = self.prefix_key(repo)
        self.assertNotEqual(key, self.sentinel_key(repo))
        self.write_json(self.state(key) / "tickets/12.json", ticket)
        self.write_json(self.state(key) / "red/t-12-fix-login.json", self.red_record(wt))
        self.assertIsNone(self.source_edit(root, wt))
        # the same files under the keys a hook computing its own spelling would use: refused
        repo, wt = self.ticket_worktree()
        for key in (self.sentinel_key(repo), sha12(str(repo))):
            self.write_json(self.state(key) / "tickets/12.json", ticket)
            self.write_json(self.state(key) / "red/t-12-fix-login.json", self.red_record(wt))
        reason = self.source_edit(root, wt)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("Tests first on t/12"), reason)

    def test_session_brief_keys_the_ticket_by_the_libs_canonical_path(self):
        """session_brief passes the lib's canonical_path(reported) to adopt_old_state:
        under PREFIX the ticket is found only under the key of /<token><real>."""
        root = self.instrumented("prefix", PREFIX_PATHS)
        repo = self.make_repo()
        self.git(repo, "checkout", "-q", "-b", BRANCH)
        ticket = {"number": 12, "status": "in-progress", "tier": "opus", "type": "task"}
        self.write_json(
            self.state(self.prefix_key(repo)) / "tickets/12.json",
            dict(ticket, title=f"From the lib {self.token}"),
        )
        self.write_json(self.state(self.prefix_key(repo)) / "red/t-12-fix-login.json", self.red_record(repo))
        for key in (self.sentinel_key(repo), sha12(str(repo))):
            self.write_json(self.state(key) / "tickets/12.json", dict(ticket, title="From a private spelling"))
            self.write_json(
                self.state(key) / "red/t-12-fix-login.json", dict(self.red_record(repo), kind="import")
            )
        r = self.run_hook(root / "hooks" / "session_brief.py", self.session_payload(repo), repo)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        lines = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"].splitlines()
        ticket_lines = [ln for ln in lines if ln.startswith("Ticket #12:")]
        self.assertEqual(len(ticket_lines), 1, lines)
        self.assertIn(f"From the lib {self.token}", ticket_lines[0])
        self.assertIn("Red run: assertion at 2026-10-13T10:00:00Z", lines)
        self.assertNotIn("private spelling", "\n".join(lines))
        self.assertEqual([ln for ln in lines if ln.startswith("Red run: import")], [])

    def test_branch_guard_resolves_the_edited_path_with_the_lib(self):
        """The edited path's directory is spelled by the lib's canonical_path: under a lib
        that swaps docs and src, docs/agent/handoff.md is src/agent/handoff.md (refused)
        and src/agent/notes.md is docs/agent/notes.md (the main checkout's own notes)."""
        root = self.instrumented("swap", swap_paths({"docs": "src", "src": "docs"}))
        repo = self.make_repo()
        (repo / "docs" / "agent").mkdir(parents=True)
        (repo / "src" / "agent").mkdir()
        reason = self.branch_guard_edit(root, repo / "docs/agent/handoff.md", repo)
        self.assertIsNotNone(reason, "docs/agent/handoff.md, spelled src/agent/... by the lib")
        self.assertTrue(reason.startswith("On main in proj."), reason)
        self.assertIsNone(self.branch_guard_edit(root, repo / "src/agent/notes.md", repo))

    def test_branch_guard_places_the_repo_top_with_the_lib(self):
        """The repo top an edit is made relative to is the lib's canonical_path(top):
        under UP the top is spelled as its parent, so docs/agent/handoff.md is
        proj/docs/agent/handoff.md, outside the notes directory, and refused."""
        root = self.instrumented("up", UP_PATHS)
        repo = self.make_repo()
        (repo / "docs" / "agent").mkdir(parents=True)
        reason = self.branch_guard_edit(root, repo / "docs/agent/handoff.md", repo)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("On main in proj."), reason)

    def test_branch_guard_judges_bash_writes_with_the_libs_canonical_path(self):
        """A Bash write is placed in its repo with the lib's canonical_path(top): a path
        under <top>/.claude/worktrees/ belongs to a worktree and passes, but under UP the
        top is its parent, so the same path is other/.claude/worktrees/... in the main
        tree of a repo on main, and refused, from another directory or from the repo."""
        root = self.instrumented("up", UP_PATHS)
        other = self.make_repo("other")
        (other / ".claude" / "worktrees" / "wt").mkdir(parents=True)  # a plain directory
        plain = self.tmp / "plain"
        plain.mkdir()
        target = other / ".claude/worktrees/wt/notes.py"
        cases = [
            (plain, f"echo x > {target}"),
            (other, "echo x > .claude/worktrees/wt/notes.py"),
            (plain, f"touch {target}"),
        ]
        for cwd, command in cases:
            with self.subTest(cwd=cwd.name, command=command.split()[0]):
                payload = self.payload("Bash", {"command": command}, cwd)
                r = self.run_hook(root / "hooks" / "branch_guard.py", payload, cwd)
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                reason = self.deny_reason(r)
                self.assertIsNotNone(reason, "a write into another repo on main")
                self.assertTrue(reason.startswith("On main in other."), reason)


if __name__ == "__main__":
    unittest.main()
