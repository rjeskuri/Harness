"""Tests for plugins/harness/lib/harness_lib/repo.py (ticket #92, refactor T14): the Repo core
harness-board, harness-pr, harness-work and harness-red share, and the main-checkout lookup of
harness-worklog and the hooks session_brief.py and tests_first.py.

- repo_facts(path, git, pick_line) -> (root, common_dir, main, key), on real temporary git
  repositories: the toplevel git reports, the common git dir, the canonical main working tree
  (from the main checkout, a subdirectory or a linked worktree, under any spelling), the state
  key (the first 12 hex of the sha1 of the canonical main tree), the one-time move of state kept
  under an earlier spelling's key; and its errors: `not a directory: <path>`, `not a git
  repository: <path>` (both with `~` expanded and made absolute), and `git rev-parse
  --git-common-dir failed in <root>: <pick_line(stderr)>`, built from the `git` and `pick_line`
  the caller passes.
- main_checkout(path): the canonical main working tree of whatever repository `path` is in;
  outside a repository, or with git missing, it returns (None or the path as given) rather
  than raising.
- One copy: the Repo classes of the four tools carry none of the shared lines and build on
  repo_facts with their own git and line picker (board and pr the first line, work the last);
  session_brief.py, tests_first.py and harness-worklog derive the main tree with main_checkout
  only; each binds the lib's name in its own namespace.
- Today's behavior, kept, through the tools as subprocesses: board's no-remote refusal, the
  common-dir error's first line (board, pr) and last line (work), red's `not inside a git
  working tree` and its own git error, worklog export outside a repository.
- A missing or broken harness_lib/repo.py: each tool exits 1 with one line, each hook fails
  open with one line.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_repo.py" -v
"""

from __future__ import annotations

import ast
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
import time
import types
import unittest
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the tools, hooks and lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
REPO_PY = LIB / "harness_lib" / "repo.py"
GIT = shutil.which("git")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
BRANCH = "t/12-fix-login"
TOOLS = ("harness-board", "harness-pr", "harness-work", "harness-red")
# Each tool's Repo: the attribute holding the toplevel, and the line its common-dir error uses.
TOOL_SPEC = {
    "harness-board": {"root": "root", "pick": "first"},
    "harness-pr": {"root": "root", "pick": "first"},
    "harness-work": {"root": "root", "pick": "last"},
    "harness-red": {"root": "top", "pick": None},  # red's own git raises before the picker applies
}
MAIN_CHECKOUT_USERS = {
    "session_brief.py": HOOKS / "session_brief.py",
    "tests_first.py": HOOKS / "tests_first.py",
    "harness-worklog": BIN / "harness-worklog",
}

_COUNTER = [0]


# ================================================================ helpers


def sha12(text: str) -> str:
    """The state key of a canonical main tree, from first principles."""
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def first_nonblank(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def last_nonblank(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


PICKERS = {"first": first_nonblank, "last": last_nonblank}


def lib_repo() -> types.ModuleType:
    """harness_lib.repo imported from the checkout's lib/, as a tool's prologue would; the
    import error while the module is missing is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.repo")


def load_with_loader(path: Path) -> types.ModuleType:
    _COUNTER[0] += 1
    modname = path.name.replace("-", "_").replace(".py", "") + f"_repo_test_{_COUNTER[0]}"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    _COUNTER[0] += 1
    module = types.ModuleType(f"harness_repo_exec_{path.stem}_{_COUNTER[0]}".replace("-", "_"))
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def real_git(path, *args, **_ignored) -> subprocess.CompletedProcess:
    """git itself, in `path`, with the (patched) process environment."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def completed(args, code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", *args], code, out, err)


def is_from_lib(fn) -> bool:
    """`fn` is the lib's harness_lib.repo function (by module and by file)."""
    try:
        source = inspect.getsourcefile(fn)
    except TypeError:
        return False
    return (getattr(fn, "__module__", None) == "harness_lib.repo" and source is not None
            and os.path.realpath(source) == os.path.realpath(str(REPO_PY)))


def tree_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"no class {name}")


def strings_in(node) -> list:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def names_in(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def defined_functions(tree: ast.Module) -> set:
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def swap_case(name: str) -> str:
    return "".join(c.lower() if c.isupper() else c.upper() for c in name)


# ================================================================ fixtures


class RepoCase(unittest.TestCase):
    """A realpath'd temp dir, a temp HOME, and a hermetic process environment for git and the lib."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-repo-lib-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.tmp, "tmp"))
        self.env = dict(
            GIT_IDENTITY,
            HOME=self.home,
            TMPDIR=os.path.join(self.tmp, "tmp"),
            PATH=f"{os.path.dirname(GIT)}:{SYSTEM_PATH}" if GIT else SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONDONTWRITEBYTECODE="1",
            GIT_CEILING_DIRECTORIES=self.tmp,
        )
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = uuid.uuid4().hex[:10]
        self.repos = os.path.join(self.home, ".claude", "harness", "repos")

    # ---- git fixtures

    def git(self, cwd, *args) -> str:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=self.env,
                           timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def make_repo(self, name: str = "proj", remote: bool = False) -> str:
        repo = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(repo, "src", "deep"))
        os.makedirs(os.path.join(repo, "tests"))
        self.git(self.tmp, "init", "-q", "-b", "main", repo)
        with open(os.path.join(repo, "README.md"), "w") as fh:
            fh.write("hello\n")
        with open(os.path.join(repo, "src", "app.py"), "w") as fh:
            fh.write("def login():\n    return None\n")
        with open(os.path.join(repo, "src", "deep", "x.py"), "w") as fh:
            fh.write("x = 1\n")
        with open(os.path.join(repo, "tests", "test_app.py"), "w") as fh:
            fh.write("def test_login():\n    assert False\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        if remote:
            self.git(repo, "remote", "add", "origin", f"https://example.invalid/{name}.git")
        return repo

    def add_worktree(self, repo: str, where: str, branch: str) -> str:
        os.makedirs(os.path.dirname(where), exist_ok=True)
        self.git(repo, "worktree", "add", "-q", "-b", branch, where)
        return where

    def toplevel(self, path: str) -> str:
        """What git itself reports as the toplevel of `path`."""
        return self.git(path, "rev-parse", "--show-toplevel")

    def plain_dir(self, name: str = "plain") -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def case_insensitive(self) -> bool:
        probe = os.path.join(self.tmp, "CaseProbe")
        os.makedirs(probe, exist_ok=True)
        return os.path.isdir(os.path.join(self.tmp, "caseprobe"))


class LibCase(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.lib = lib_repo()

    def facts(self, path, git=real_git, pick_line=first_nonblank):
        result = self.lib.repo_facts(path, git, pick_line)
        result = tuple(result)
        self.assertEqual(len(result), 4, result)
        return result

    def raises(self, path, git=real_git, pick_line=first_nonblank) -> str:
        with self.assertRaises(Exception) as caught:
            self.lib.repo_facts(path, git, pick_line)
        return str(caught.exception)


# ================================================================ the module


class ModuleTests(unittest.TestCase):
    def test_repo_py_is_a_module_of_the_lib_with_both_names(self):
        self.assertTrue(REPO_PY.is_file(), f"{REPO_PY} does not exist")
        lib = lib_repo()
        for name in ("repo_facts", "main_checkout"):
            self.assertTrue(callable(getattr(lib, name, None)), name)
        params = list(inspect.signature(lib.repo_facts).parameters)
        self.assertEqual(params[:3], ["path", "git", "pick_line"])
        params = inspect.signature(lib.main_checkout).parameters
        self.assertEqual(list(params)[0], "path")
        self.assertIn("timeout", params, "main_checkout takes no timeout (finding 2 of PR #126)")
        self.assertIn(params["timeout"].kind,
                      (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD))
        for name, param in list(params.items())[1:]:
            self.assertIsNot(param.default, inspect.Parameter.empty, f"main_checkout(path) needs {name}")

    def test_repo_py_is_standard_library_only_and_39_compatible(self):
        self.assertTrue(REPO_PY.is_file(), f"{REPO_PY} does not exist")
        text = REPO_PY.read_text(encoding="utf-8")
        tree = ast.parse(text)
        self.assertIn("from __future__ import annotations", text)
        stdlib = set(getattr(sys, "stdlib_module_names", ())) or {
            "os", "subprocess", "hashlib", "sys", "unicodedata", "typing", "pathlib", "collections"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for mod in mods:
                self.assertTrue(mod in stdlib or mod in ("__future__", "harness_lib"), mod)


# ================================================================ repo_facts


@unittest.skipUnless(GIT, "git is not installed")
class RepoFactsTests(LibCase):
    def test_from_the_main_checkout_and_its_subdirectories(self):
        for name in ("alpha", "Beta Space", "gamma-" + self.token):
            repo = self.make_repo(name)
            for start in (repo, os.path.join(repo, "src"), os.path.join(repo, "src", "deep")):
                with self.subTest(name=name, start=start):
                    root, common_dir, main, key = self.facts(start)
                    self.assertEqual(root, self.toplevel(start))
                    self.assertTrue(os.path.isabs(common_dir), common_dir)
                    self.assertEqual(os.path.realpath(common_dir), os.path.join(repo, ".git"))
                    self.assertEqual(main, repo)
                    self.assertEqual(key, sha12(repo))

    def test_a_linked_worktree_gives_its_own_root_and_the_main_trees_main_and_key(self):
        repo = self.make_repo("north")
        inside = self.add_worktree(repo, os.path.join(repo, ".claude", "worktrees", "t-7-a"), "t/7-a")
        beside = self.add_worktree(repo, os.path.join(self.tmp, "elsewhere", "side"), "t/8-side")
        for wt in (inside, beside, os.path.join(beside, "src", "deep")):
            with self.subTest(wt=wt):
                root, common_dir, main, key = self.facts(wt)
                self.assertEqual(root, self.toplevel(wt))
                self.assertNotEqual(root, repo)
                self.assertEqual(os.path.realpath(common_dir), os.path.join(repo, ".git"))
                self.assertEqual(main, repo)
                self.assertEqual(key, sha12(repo))

    def test_two_repositories_have_two_keys(self):
        one, two = self.make_repo("one"), self.make_repo("two")
        k1, k2 = self.facts(one)[3], self.facts(two)[3]
        self.assertEqual((k1, k2), (sha12(one), sha12(two)))
        self.assertNotEqual(k1, k2)

    def test_another_spelling_gives_the_canonical_main(self):
        repo = self.make_repo("MixedCase")
        link = os.path.join(self.tmp, "link-to-repo")
        os.symlink(repo, link)
        spellings = [link, os.path.join(link, "src")]
        if self.case_insensitive():
            spellings += [os.path.join(self.tmp, "mixedcase"), os.path.join(self.tmp, "MIXEDCASE", "SRC", "Deep")]
        for start in spellings:
            with self.subTest(start=start):
                _root, _common, main, key = self.facts(start)
                self.assertEqual(main, repo)
                self.assertEqual(key, sha12(repo))

    def test_the_home_is_expanded_and_a_relative_path_made_absolute(self):
        os.rename(self.make_repo("homed"), os.path.join(self.home, "homed"))
        repo = os.path.join(self.home, "homed")
        self.assertEqual(self.facts("~/homed/src")[2:], (repo, sha12(repo)))
        other = self.make_repo("relative-" + self.token)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.tmp)
        self.assertEqual(self.facts(os.path.basename(other))[2:], (other, sha12(other)))

    def test_state_under_an_earlier_spelling_moves_to_the_key(self):
        if not self.case_insensitive():
            self.skipTest("needs a case-insensitive file system")
        repo = self.make_repo("Proj")
        spelled = os.path.join(self.tmp, "proj")
        old = os.path.join(self.repos, sha12(spelled))
        other = os.path.join(self.repos, sha12(os.path.join(self.tmp, "unrelated")))
        for d in (old, other):
            os.makedirs(os.path.join(d, "tickets"))
        with open(os.path.join(old, "tickets", "12.json"), "w") as fh:
            fh.write(json.dumps({"number": 12, "token": self.token}))
        with open(os.path.join(other, "tickets", "9.json"), "w") as fh:
            fh.write("{}")
        _root, _common, main, key = self.facts(os.path.join(spelled, "src"))
        self.assertEqual((main, key), (repo, sha12(repo)))
        moved = os.path.join(self.repos, key, "tickets", "12.json")
        self.assertTrue(os.path.isfile(moved), os.listdir(self.repos))
        with open(moved) as fh:
            self.assertEqual(json.load(fh)["token"], self.token)
        self.assertFalse(os.path.lexists(old))
        self.assertTrue(os.path.isfile(os.path.join(other, "tickets", "9.json")))

    def test_existing_state_under_the_key_is_kept_and_the_old_left_alone(self):
        if not self.case_insensitive():
            self.skipTest("needs a case-insensitive file system")
        repo = self.make_repo("Kept")
        spelled = os.path.join(self.tmp, "kept")
        old = os.path.join(self.repos, sha12(spelled))
        new = os.path.join(self.repos, sha12(repo))
        os.makedirs(old)
        os.makedirs(new)
        for d, what in ((old, "old"), (new, "new")):
            with open(os.path.join(d, "mark"), "w") as fh:
                fh.write(what + self.token)
        self.assertEqual(self.facts(spelled)[3], sha12(repo))
        for d, what in ((old, "old"), (new, "new")):
            with open(os.path.join(d, "mark")) as fh:
                self.assertEqual(fh.read(), what + self.token)

    def test_without_earlier_state_nothing_is_created(self):
        repo = self.make_repo("fresh")
        self.facts(repo)
        self.assertFalse(os.path.lexists(os.path.join(self.repos, sha12(repo))))

    # ---- errors

    def test_a_path_that_is_not_a_directory(self):
        missing = os.path.join(self.tmp, "missing-" + self.token)
        afile = os.path.join(self.tmp, "afile-" + self.token)
        with open(afile, "w") as fh:
            fh.write("x")
        for given, shown in ((missing, missing), (afile, afile),
                             ("~/nowhere-" + self.token, os.path.join(self.home, "nowhere-" + self.token))):
            with self.subTest(given=given):
                self.assertEqual(self.raises(given), f"not a directory: {shown}")
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.tmp)
        self.assertEqual(self.raises("gone-" + self.token), f"not a directory: {os.path.join(self.tmp, 'gone-' + self.token)}")

    def test_a_directory_outside_git(self):
        plain = self.plain_dir("plain-" + self.token)
        sub = self.plain_dir(os.path.join("plain-" + self.token, "inner"))
        os.makedirs(os.path.join(self.home, "loose"))
        for given, shown in ((plain, plain), (sub, sub), ("~/loose", os.path.join(self.home, "loose"))):
            with self.subTest(given=given):
                self.assertEqual(self.raises(given), f"not a git repository: {shown}")

    def test_the_passed_git_decides_the_toplevel(self):
        repo = self.make_repo("real")
        other = self.make_repo("other-" + self.token)

        def failing(code, out):
            def git(path, *args, **kw):
                if "--show-toplevel" in args:
                    return completed(args, code, out, "fatal: nope\n")
                return real_git(path, *args)
            return git

        for code, out in ((128, ""), (0, ""), (0, "   \n")):
            with self.subTest(code=code, out=out):
                self.assertEqual(self.raises(repo, git=failing(code, out)), f"not a git repository: {repo}")

        def elsewhere(path, *args, **kw):
            if "--show-toplevel" in args:
                return completed(args, 0, other + "\n")
            return real_git(path, *args)

        root, common_dir, main, key = self.facts(repo, git=elsewhere)
        self.assertEqual((root, main, key), (other, other, sha12(other)))
        self.assertEqual(os.path.realpath(common_dir), os.path.join(other, ".git"))

    def test_a_failing_common_dir_names_the_root_and_the_callers_picked_line(self):
        repo = self.make_repo("commonfail")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "wt-" + self.token), "t/3-x")
        stderrs = [
            f"\n  fatal: first {self.token}  \nmiddle\n  last {self.token}\n\n",
            f"only one {self.token[::-1]}\n",
            "",
        ]
        pickers = {
            "first": first_nonblank,
            "last": last_nonblank,
            "count": lambda s: f"<{len(s.splitlines())} lines>",
        }
        for start in (repo, os.path.join(wt, "src")):
            root = self.toplevel(start)
            for err in stderrs:
                for code, out in ((128, ""), (0, "")):
                    def git(path, *args, _err=err, _code=code, _out=out, **kw):
                        if "--git-common-dir" in args and "--show-toplevel" not in args:
                            return completed(args, _code, _out, _err)
                        return real_git(path, *args)

                    for label, pick in pickers.items():
                        with self.subTest(start=start, err=err, code=code, pick=label):
                            self.assertEqual(
                                self.raises(start, git=git, pick_line=pick),
                                f"git rev-parse --git-common-dir failed in {root}: {pick(err)}",
                            )


# ================================================================ main_checkout


@unittest.skipUnless(GIT, "git is not installed")
class MainCheckoutTests(LibCase):
    def check(self, given, expected: str) -> None:
        result = self.lib.main_checkout(given)
        self.assertIsNotNone(result, given)
        self.assertEqual(os.fspath(result), expected, given)

    def test_inside_a_repository_it_is_the_main_tree(self):
        for name in ("east", "west " + self.token):
            repo = self.make_repo(name)
            wt = self.add_worktree(repo, os.path.join(repo, ".claude", "worktrees", "t-1-x"), f"t/1-{name[:4]}")
            side = self.add_worktree(repo, os.path.join(self.tmp, "sides", name), f"t/2-{name[:4]}")
            for start in (repo, os.path.join(repo, "src", "deep"), wt, os.path.join(wt, "src"), side,
                          os.path.join(side, "tests")):
                with self.subTest(start=start):
                    self.check(start, repo)

    def test_a_path_object_works_too(self):
        repo = self.make_repo("pathobj")
        self.check(Path(repo) / "src", repo)

    def test_it_returns_the_canonical_spelling(self):
        repo = self.make_repo("Canon")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "canon-wt"), "t/4-canon")
        link = os.path.join(self.tmp, "canon-link")
        os.symlink(repo, link)
        wlink = os.path.join(self.tmp, "wt-link")
        os.symlink(wt, wlink)
        spellings = [link, os.path.join(link, "src"), wlink]
        if self.case_insensitive():
            spellings += [os.path.join(self.tmp, "canon"), os.path.join(self.tmp, "CANON", "Src"),
                          os.path.join(self.tmp, "Canon-WT")]
        for start in spellings:
            with self.subTest(start=start):
                self.check(start, repo)

    def test_outside_a_repository_it_returns_rather_than_raises(self):
        plain = self.plain_dir("plain-" + self.token)
        afile = os.path.join(plain, "f.txt")
        with open(afile, "w") as fh:
            fh.write("x")
        for given in (plain, os.path.join(self.tmp, "missing-" + self.token), afile):
            with self.subTest(given=given):
                result = self.lib.main_checkout(given)
                self.assertTrue(result is None or os.fspath(result) == given, result)

    def test_without_git_it_returns_rather_than_raises(self):
        repo = self.make_repo("nogit")
        empty = self.plain_dir("empty-bin")
        with mock.patch.dict(os.environ, {"PATH": empty}):
            for given in (repo, os.path.join(repo, "src")):
                with self.subTest(given=given):
                    result = self.lib.main_checkout(given)
                    self.assertTrue(result is None or os.fspath(result) == given, result)


# ================================================================ one copy


class OneCopyTests(unittest.TestCase):
    def test_the_repo_classes_carry_none_of_the_shared_lines(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                cls = class_node(tree_of(BIN / tool), "Repo")
                strings = " ".join(strings_in(cls))
                for flag in ("--show-toplevel", "--git-common-dir"):
                    self.assertFalse(flag in strings, f"{tool}'s Repo still runs git rev-parse {flag}")
                names = names_in(cls)
                for name in ("canonical_path", "adopt_old_state"):
                    self.assertNotIn(name, names)
                self.assertIn("repo_facts", names)

    def test_board_pr_and_work_no_longer_spell_the_shared_messages(self):
        for tool in ("harness-board", "harness-pr", "harness-work"):
            text = (BIN / tool).read_text(encoding="utf-8")
            for message in ("not a git repository:", "not a directory:", "--git-common-dir failed in"):
                with self.subTest(tool=tool, message=message):
                    self.assertFalse(message in text, f"{tool} still spells {message!r}")

    def test_no_tool_or_hook_names_the_common_dir_outside_the_lib_for_these_callers(self):
        for label, path in list(MAIN_CHECKOUT_USERS.items()) + [(t, BIN / t) for t in TOOLS]:
            with self.subTest(file=label):
                if label in MAIN_CHECKOUT_USERS:
                    text = path.read_text(encoding="utf-8")
                else:
                    text = " ".join(strings_in(class_node(tree_of(path), "Repo")))
                self.assertFalse("--git-common-dir" in text, f"{label} still runs git rev-parse --git-common-dir")

    def test_worklog_and_the_hooks_define_no_main_checkout_of_their_own(self):
        for label, path in MAIN_CHECKOUT_USERS.items():
            with self.subTest(file=label):
                self.assertNotIn("main_checkout", defined_functions(tree_of(path)))
        self.assertTrue(REPO_PY.is_file(), f"{REPO_PY} does not exist")
        self.assertIn("main_checkout", defined_functions(tree_of(REPO_PY)))
        self.assertIn("repo_facts", defined_functions(tree_of(REPO_PY)))

    def test_the_tools_bind_repo_facts_from_the_lib(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                mod = load_with_loader(BIN / tool)
                self.assertTrue(is_from_lib(getattr(mod, "repo_facts", None)), tool)

    def test_worklog_and_the_hooks_bind_main_checkout_from_the_lib(self):
        for label, path in MAIN_CHECKOUT_USERS.items():
            loaders = (load_with_loader, load_with_exec) if label.endswith(".py") else (load_with_loader,)
            for load in loaders:
                with self.subTest(file=label, load=load.__name__):
                    mod = load(path)
                    self.assertTrue(is_from_lib(getattr(mod, "main_checkout", None)), label)


@unittest.skipUnless(GIT, "git is not installed")
class CallersBuildOnTheLibTests(RepoCase):
    """The callers take their facts from the lib's functions: a stand-in put in the tool's or
    hook's namespace decides what the Repo, the state dir or the repo found are."""

    def test_each_repo_class_takes_its_facts_from_repo_facts(self):
        lib = lib_repo()
        for tool, spec in TOOL_SPEC.items():
            with self.subTest(tool=tool):
                repo = self.make_repo(f"{tool}-{self.token}", remote=True)
                start = os.path.join(repo, "src")
                mod = load_with_loader(BIN / tool)
                self.assertTrue(hasattr(mod, "repo_facts"), f"{tool} has no repo_facts")
                calls = []
                common = os.path.join(self.tmp, f"common-{tool}-{self.token}")
                key = f"key{uuid.uuid4().hex[:9]}"

                def stand_in(path, git, pick_line, _calls=calls, _common=common, _key=key):
                    _calls.append((path, git, pick_line))
                    root, _c, main, _k = lib.repo_facts(path, git, pick_line)
                    return root, _common, main, _key

                with mock.patch.object(mod, "repo_facts", stand_in):
                    obj = mod.Repo(start)
                self.assertEqual(len(calls), 1, calls)
                self.assertEqual(getattr(obj, spec["root"]), self.toplevel(repo))
                self.assertEqual(obj.main, repo)
                self.assertEqual(obj.key, key)
                if tool == "harness-work":
                    self.assertEqual(obj.common_dir, common)
                    self.assertEqual(obj.worktrees_dir, os.path.join(repo, ".claude", "worktrees"))
                if tool == "harness-red":
                    self.assertTrue(obj.record_path.startswith(os.path.join(self.repos, key) + os.sep),
                                    obj.record_path)
                _path, git, pick_line = calls[0]
                ran = git(repo, "rev-parse", "--show-toplevel")
                self.assertEqual((ran.returncode, ran.stdout.strip()), (0, self.toplevel(repo)))
                if spec["pick"]:
                    sample = f"\n  one {self.token} \n two\n three {self.token}  \n\n"
                    self.assertEqual(pick_line(sample), PICKERS[spec["pick"]](sample))

    def other_main(self, name: str) -> str:
        return self.make_repo(name)

    def test_session_brief_keys_its_state_by_main_checkout(self):
        repo = self.make_repo("brief")
        mod = load_with_loader(HOOKS / "session_brief.py")
        self.assertEqual(mod.LIB_PROBLEM, "")
        self.assertTrue(hasattr(mod, "main_checkout"), "session_brief.py has no main_checkout")
        for n in range(2):
            target = self.other_main(f"brief-target-{n}-{self.token}")
            calls = []
            with mock.patch.object(mod, "main_checkout", lambda p, _t=target, _c=calls, **_kw: _c.append(p) or _t):
                state = mod.repo_state_dir(Path(repo), Path(repo, "src"))
            with self.subTest(n=n):
                self.assertTrue(calls)
                self.assertEqual(os.fspath(state), os.path.join(self.repos, sha12(target)))

    def test_tests_first_takes_the_main_tree_from_main_checkout(self):
        repo = self.make_repo("guarded")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "guarded-wt"), BRANCH)
        mod = load_with_loader(HOOKS / "tests_first.py")
        self.assertTrue(hasattr(mod, "main_checkout"), "tests_first.py has no main_checkout")
        for n in range(2):
            target = self.other_main(f"tf-target-{n}-{self.token}")
            calls = []
            with mock.patch.object(mod, "main_checkout", lambda p, _t=target, _c=calls, **_kw: _c.append(p) or _t):
                facts = mod.repo_facts(os.path.join(wt, "src"))
            with self.subTest(n=n):
                self.assertTrue(calls)
                self.assertEqual(facts["main"], Path(target))
                self.assertEqual(facts["top"], Path(wt))
                self.assertEqual(facts["branch"], BRANCH)

    def test_worklog_finds_the_repo_with_main_checkout(self):
        """Inside a linked worktree the main tree is main_checkout's (a stand-in decides it); a
        plain subdirectory of the main checkout is kept as given, as before the refactor."""
        repo = self.make_repo("logged")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "logged-wt"), "t/6-logged")
        mod = load_with_loader(BIN / "harness-worklog")
        self.assertTrue(is_from_lib(getattr(mod, "main_checkout", None)), "harness-worklog binds no lib main_checkout")
        for n in range(2):
            target = self.other_main(f"wl-target-{n}-{self.token}")
            for start in (wt, os.path.join(wt, "src")):
                with mock.patch.object(mod, "main_checkout", lambda p, _t=target, **_kw: _t):
                    with self.subTest(n=n, start=start):
                        self.assertEqual(os.fspath(mod.find_repo(start)), target)
        for start in (os.path.join(repo, "src"), os.path.join(repo, "src", "deep")):
            with self.subTest(start=start):
                self.assertEqual(os.fspath(mod.find_repo(start)), start)


# ================================================================ the tools, kept as they are


@unittest.skipUnless(GIT, "git is not installed")
class PreservedBehaviorTests(RepoCase):
    """Today's messages, through the tools as subprocesses (regression guards for the refactor)."""

    def run_tool(self, tool: str, *args: str, cwd: str, path_prefix: str = "", **extra):
        env = dict(self.env, HARNESS_GH=os.path.join(self.tmp, "no-gh"), **extra)
        if path_prefix:
            env["PATH"] = f"{path_prefix}:{env['PATH']}"
        return subprocess.run([sys.executable, str(BIN / tool), *args], capture_output=True, text=True,
                              env=env, cwd=cwd, stdin=subprocess.DEVNULL, timeout=120)

    def assert_one_line(self, p, line: str, exact: bool = True) -> None:
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stderr.splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        if exact:
            self.assertEqual(lines[0], line)
        else:
            self.assertTrue(lines[0].startswith(line), f"{lines[0]!r} does not start with {line!r}")

    def fake_git_dir(self, first: str, last: str) -> str:
        """A `git` that fails `rev-parse --git-common-dir` with a multi-line stderr and runs
        the real git for everything else."""
        d = os.path.join(self.tmp, "fakebin-" + uuid.uuid4().hex[:6])
        os.makedirs(d)
        script = os.path.join(d, "git")
        with open(script, "w") as fh:
            fh.write(f"""#!/bin/sh
common=0; top=0
for a in "$@"; do
  case "$a" in --git-common-dir) common=1;; --show-toplevel) top=1;; esac
done
if [ $common = 1 ] && [ $top = 0 ]; then
  printf '%s\\n' '' '   {first}  ' 'middle line' '  {last} ' '' >&2
  exit 128
fi
exec '{GIT}' "$@"
""")
        os.chmod(script, 0o755)
        return d

    def test_board_refuses_a_repository_without_a_remote(self):
        repo = self.make_repo("noremote")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "noremote-wt"), "t/5-x")
        for cwd in (repo, os.path.join(repo, "src"), wt):
            with self.subTest(cwd=cwd):
                p = self.run_tool("harness-board", "ready", cwd=cwd)
                self.assertEqual(p.stdout, "")
                self.assert_one_line(p, f"harness-board: {repo} has no git remote", exact=False)
        self.git(repo, "remote", "add", "origin", "https://example.invalid/noremote.git")
        p = self.run_tool("harness-board", "ready", cwd=repo)
        self.assertNotIn("no git remote", p.stderr)

    def test_the_common_dir_error_uses_the_first_line_in_board_and_pr_and_the_last_in_work(self):
        for n in range(2):
            first, last = f"FIRST-{n}-{uuid.uuid4().hex[:6]}", f"LAST-{n}-{uuid.uuid4().hex[:6]}"
            fake = self.fake_git_dir(first, last)
            repo = self.make_repo(f"cd-{n}-{self.token}", remote=True)
            root = self.toplevel(repo)
            runs = {
                "harness-board": (("ready",), first),
                "harness-pr": (("open", "5", "--worktree", repo), first),
                "harness-work": (("worktrees",), last),
            }
            for tool, (args, line) in runs.items():
                with self.subTest(n=n, tool=tool):
                    p = self.run_tool(tool, *args, cwd=repo, path_prefix=fake)
                    self.assert_one_line(p, f"{tool}: git rev-parse --git-common-dir failed in {root}: {line}")

    def test_red_outside_a_working_tree_and_with_a_failing_common_dir(self):
        for name in ("plain-a", os.path.join("plain-b", "deeper")):
            d = self.plain_dir(name + "-" + self.token)
            with self.subTest(cwd=d):
                p = self.run_tool("harness-red", "check", cwd=d)
                self.assert_one_line(p, f"harness-red: not inside a git working tree: {d}")
        last = "LAST-" + self.token
        fake = self.fake_git_dir("FIRST-" + self.token, last)
        repo = self.make_repo("redcd")
        p = self.run_tool("harness-red", "check", cwd=repo, path_prefix=fake)
        self.assert_one_line(p, f"harness-red: git rev-parse --git-common-dir failed: {last}")

    def test_worklog_export_outside_a_repository_still_succeeds(self):
        d = self.plain_dir("outside-" + self.token)
        for args in (("export", "--month", "2026-09"), ("--repo", d, "export", "--month", "2026-08")):
            with self.subTest(args=args):
                p = self.run_tool("harness-worklog", *args, cwd=d)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn("Total", p.stdout)


# ================================================================ worklog: the repo it works on (PR #126 finding 1)


def encoded(path: str) -> str:
    """Claude Code's session-folder name for a project path: every non-alphanumeric as '-'."""
    return "".join(c if ("a" <= c <= "z" or "A" <= c <= "Z" or "0" <= c <= "9") else "-" for c in path)


@unittest.skipUnless(GIT, "git is not installed")
class WorklogRepoTests(RepoCase):
    """harness-worklog keeps the path it is given (--repo, $CLAUDE_PROJECT_DIR) unless that path is
    inside a linked worktree, which stands for its main checkout; with neither, the current
    directory's repository is its main checkout. As before the refactor (main's own main_checkout
    in harness-worklog)."""

    def worklog_add(self, description: str, cwd: str, repo_arg: str = "", project_dir: str = ""):
        env = dict(self.env)
        if project_dir:
            env["CLAUDE_PROJECT_DIR"] = project_dir
        args = (["--repo", repo_arg] if repo_arg else []) + [
            "add", "--date", "2026-09-03", "--hours", "1.5", "--description", description]
        p = subprocess.run([sys.executable, str(BIN / "harness-worklog"), *args], capture_output=True, text=True,
                           env=env, cwd=cwd, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def logs_holding(self, description: str, under: str) -> list:
        """Every worklog/worklog.csv below `under` whose rows carry `description`."""
        found = []
        for dirpath, dirnames, filenames in os.walk(under):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if os.path.basename(dirpath) == "worklog" and "worklog.csv" in filenames:
                path = os.path.join(dirpath, "worklog.csv")
                with open(path, encoding="utf-8") as fh:
                    if description in fh.read():
                        found.append(os.path.realpath(path))
        return found

    def test_the_log_is_written_in_the_given_path_unless_it_is_in_a_linked_worktree(self):
        repo = self.make_repo("billed")
        inside = self.add_worktree(repo, os.path.join(repo, ".claude", "worktrees", "t-3-b"), "t/3-b")
        beside = self.add_worktree(repo, os.path.join(self.tmp, "billed-side"), "t/4-b")
        log_of = lambda d: os.path.realpath(os.path.join(d, "worklog", "worklog.csv"))  # noqa: E731
        cases = []
        for given, owner in ((os.path.join(repo, "src"), os.path.join(repo, "src")),
                             (os.path.join(repo, "src", "deep"), os.path.join(repo, "src", "deep")),
                             (os.path.join(repo, "tests"), os.path.join(repo, "tests")),
                             (repo, repo),
                             (inside, repo), (os.path.join(inside, "src"), repo),
                             (beside, repo), (os.path.join(beside, "src", "deep"), repo)):
            cases.append(("--repo", given, owner))
            cases.append(("CLAUDE_PROJECT_DIR", given, owner))
        # Neither: the current directory's repository, its main checkout (git's top level before).
        for cwd in (os.path.join(repo, "src"), os.path.join(repo, "tests"), os.path.join(beside, "src")):
            cases.append(("cwd", cwd, repo))
        for n, (source, given, owner) in enumerate(cases):
            description = f"row {n} {uuid.uuid4().hex[:8]}"
            with self.subTest(source=source, given=given):
                if source == "--repo":
                    self.worklog_add(description, cwd=self.tmp, repo_arg=given)
                elif source == "CLAUDE_PROJECT_DIR":
                    self.worklog_add(description, cwd=self.tmp, project_dir=given)
                else:
                    self.worklog_add(description, cwd=given)
                self.assertEqual(self.logs_holding(description, self.tmp), [log_of(owner)])

    def test_find_repo_keeps_the_spelling_given_outside_a_linked_worktree(self):
        """The session folders Claude Code keeps are named after the spelling of the project path,
        so a path outside a linked worktree is kept as given (made absolute, `~` expanded), not
        resolved; a linked worktree under any spelling gives the canonical main checkout."""
        repo = self.make_repo("Spelled")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "spelled-wt"), "t/9-sp")
        link = os.path.join(self.tmp, "spelled-link")
        os.symlink(repo, link)
        wlink = os.path.join(self.tmp, "spelled-wt-link")
        os.symlink(wt, wlink)
        os.rename(self.make_repo("homed"), os.path.join(self.home, "homed"))
        homed = os.path.join(self.home, "homed")
        mod = load_with_loader(BIN / "harness-worklog")
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(repo)
        cases = [
            (os.path.join(link, "src"), os.path.join(link, "src")),
            (link, link),
            (os.path.join(repo, "src", "deep"), os.path.join(repo, "src", "deep")),
            ("src", os.path.join(repo, "src")),
            (os.path.join("src", "deep"), os.path.join(repo, "src", "deep")),
            ("~/homed/tests", os.path.join(homed, "tests")),
            (wlink, repo),
            (os.path.join(wlink, "src"), repo),
            (os.path.join(wt, "tests"), repo),
        ]
        if self.case_insensitive():
            cases += [(os.path.join(self.tmp, "SPELLED", "Src"), os.path.join(self.tmp, "SPELLED", "Src")),
                      (os.path.join(self.tmp, "Spelled-WT", "src"), repo)]
        projects = os.path.join(self.home, ".claude", "projects")
        for given, expected in cases:
            with self.subTest(given=given):
                found = os.fspath(mod.find_repo(given))
                self.assertEqual(found, expected)
                self.assertEqual(mod.transcripts_dirs(found)[0], os.path.join(projects, encoded(expected)))


# ================================================================ timeouts sized to the hooks (PR #126 finding 2)


SLACK = 0.6  # seconds a lookup may run past its timeout: process start-up and the kill


def hooks_json_timeout(hook: str) -> float:
    """The timeout hooks.json gives the hook whose command runs `hook`."""
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    found = [h["timeout"] for groups in data["hooks"].values() for group in groups for h in group["hooks"]
             if any(hook in str(a) for a in [h.get("command", "")] + list(h.get("args", [])))]
    if not found:
        raise AssertionError(f"hooks.json runs no {hook}")
    return float(min(found))


class SlowGitMixin:
    def slow_git(self, top: float = 0.0, common: float = 0.0, other: float = 0.0) -> str:
        """A directory holding a `git` that sleeps (seconds) before running the real git:
        `common` for rev-parse --git-common-dir, else `top` for rev-parse --show-toplevel, else
        `other`. One process (it execs git), so a timeout's kill stops it."""
        d = os.path.join(self.tmp, "slowbin-" + uuid.uuid4().hex[:6])
        os.makedirs(d)
        script = os.path.join(d, "git")
        with open(script, "w") as fh:
            fh.write(f"""#!{sys.executable}
import os, sys, time
args = sys.argv[1:]
if "--git-common-dir" in args:
    time.sleep({common!r})
elif "--show-toplevel" in args:
    time.sleep({top!r})
else:
    time.sleep({other!r})
os.execv({GIT!r}, [{GIT!r}] + args)
""")
        os.chmod(script, 0o755)
        return d

    def on_path(self, directory: str):
        patcher = mock.patch.dict(os.environ, {"PATH": f"{directory}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)


@unittest.skipUnless(GIT, "git is not installed")
class MainCheckoutTimeoutTests(SlowGitMixin, LibCase):
    def test_a_git_slower_than_the_timeout_makes_it_return_within_the_timeout(self):
        """The whole lookup, both git calls, is held to `timeout`: a fast-enough first call
        followed by a hung one returns at about `timeout`, not one timeout per call."""
        repo = self.make_repo("slowpoke")
        start = os.path.join(repo, "src")
        for t in (1.5, 3.0):
            with self.subTest(timeout=t):
                fake = self.slow_git(top=0.8 * t, common=60.0)
                with mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ['PATH']}"}):
                    began = time.monotonic()
                    result = self.lib.main_checkout(start, timeout=t)
                    took = time.monotonic() - began
                self.assertTrue(result is None or os.fspath(result) == start, result)
                self.assertLess(took, t + SLACK, f"main_checkout(timeout={t}) took {took:.2f} s")

    def test_a_hung_git_from_the_first_call_returns_within_the_timeout(self):
        repo = self.make_repo("hung")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "hung-wt"), "t/5-hung")
        fake = self.slow_git(top=60.0, common=60.0, other=60.0)
        self.on_path(fake)
        for t, start in ((1.0, repo), (2.0, os.path.join(wt, "src"))):
            with self.subTest(timeout=t, start=start):
                began = time.monotonic()
                result = self.lib.main_checkout(start, timeout=t)
                took = time.monotonic() - began
                self.assertTrue(result is None or os.fspath(result) == start, result)
                self.assertLess(took, t + SLACK, f"main_checkout(timeout={t}) took {took:.2f} s")

    def test_a_generous_timeout_lets_a_slow_git_answer(self):
        """The timeout is the caller's: a git taking 1.5 s a call still answers within 6 s."""
        repo = self.make_repo("patient")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "patient-wt"), "t/6-patient")
        self.on_path(self.slow_git(top=1.5, common=1.5, other=1.5))
        for start in (os.path.join(repo, "src"), os.path.join(wt, "tests")):
            with self.subTest(start=start):
                result = self.lib.main_checkout(start, timeout=6)
                self.assertIsNotNone(result, start)
                self.assertEqual(os.fspath(result), repo)


@unittest.skipUnless(GIT, "git is not installed")
class HookTimeoutTests(SlowGitMixin, RepoCase):
    """session_brief.py and tests_first.py hold the main-checkout lookup to their own git
    timeouts (session_brief its EXTRA_GIT_TIMEOUT, the old single rev-parse's; tests_first its
    GIT_TIMEOUT), inside the timeout hooks.json gives each hook."""

    def recorder(self, target: str, calls: list):
        def stand_in(path, *args, **kwargs):
            calls.append((path, args, kwargs))
            return target
        return stand_in

    def test_session_brief_passes_its_own_timeout_to_main_checkout(self):
        repo = self.make_repo("brief-t")
        mod = load_with_loader(HOOKS / "session_brief.py")
        self.assertEqual(mod.LIB_PROBLEM, "")
        expected = mod.EXTRA_GIT_TIMEOUT
        self.assertLess(expected, hooks_json_timeout("session_brief.py"))
        calls = []
        with mock.patch.object(mod, "main_checkout", self.recorder(repo, calls)):
            mod.repo_state_dir(Path(repo), Path(repo, "src"))
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("timeout", calls[0][2], f"session_brief calls main_checkout without a timeout: {calls}")
        self.assertEqual(calls[0][2]["timeout"], expected)

    def test_tests_first_passes_its_own_timeout_to_main_checkout(self):
        repo = self.make_repo("guard-t")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "guard-t-wt"), BRANCH)
        mod = load_with_loader(HOOKS / "tests_first.py")
        expected = mod.GIT_TIMEOUT
        self.assertLess(expected, hooks_json_timeout("tests_first.py"))
        calls = []
        with mock.patch.object(mod, "main_checkout", self.recorder(repo, calls)):
            mod.repo_facts(os.path.join(wt, "src"))
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("timeout", calls[0][2], f"tests_first calls main_checkout without a timeout: {calls}")
        self.assertEqual(calls[0][2]["timeout"], expected)

    def test_a_hung_common_dir_holds_each_hook_to_its_own_timeout(self):
        """With the lib's real main_checkout and a git whose --git-common-dir hangs, the lookup
        each hook makes gives up after the hook's own timeout (the old single call's)."""
        repo = self.make_repo("hang-hooks")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "hang-hooks-wt"), BRANCH)
        self.on_path(self.slow_git(common=60.0))
        brief = load_with_loader(HOOKS / "session_brief.py")
        guard = load_with_loader(HOOKS / "tests_first.py")
        runs = (
            ("session_brief.py", brief.EXTRA_GIT_TIMEOUT,
             lambda: brief.repo_state_dir(Path(repo), Path(repo, "src"))),
            ("tests_first.py", guard.GIT_TIMEOUT, lambda: guard.repo_facts(os.path.join(wt, "src"))),
        )
        for hook, own, lookup in runs:
            with self.subTest(hook=hook):
                began = time.monotonic()
                result = lookup()
                took = time.monotonic() - began
                self.assertIsNone(result)
                self.assertLess(took, min(own + SLACK, hooks_json_timeout(hook)),
                                f"{hook}'s main-checkout lookup took {took:.2f} s against its {own} s")


# ================================================================ without the lib's repo.py


@unittest.skipUnless(GIT, "git is not installed")
class WithoutRepoPyTests(RepoCase):
    """A plugin copy whose lib/harness_lib/repo.py is missing or raises on import."""

    TOOL_ARGS = {
        "harness-board": ("ready",),
        "harness-pr": ("awaiting",),
        "harness-work": ("worktrees",),
        "harness-red": ("check",),
        "harness-worklog": ("export", "--month", "2026-09"),
    }

    def plugin(self, lib: str) -> Path:
        root = Path(self.tmp) / f"plugin-{lib}"
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(BIN, root / "bin", ignore=ignore)
        shutil.copytree(HOOKS, root / "hooks", ignore=ignore)
        shutil.copytree(LIB, root / "lib", ignore=ignore)
        repo_py = root / "lib" / "harness_lib" / "repo.py"
        if lib == "raises":
            repo_py.write_text(f"raise RuntimeError({('broken repo ' + self.token)!r})\n", encoding="utf-8")
        elif lib == "missing":
            if repo_py.exists():
                repo_py.unlink()
        else:
            self.assertEqual(lib, "real")
        return root

    def test_each_tool_exits_1_with_one_line(self):
        cwd = self.make_repo("tooled", remote=True)
        for lib, error_has in (("raises", "broken repo " + self.token), ("missing", "repo")):
            root = self.plugin(lib)
            for tool, args in self.TOOL_ARGS.items():
                with self.subTest(lib=lib, tool=tool):
                    env = dict(self.env, HARNESS_GH=os.path.join(self.tmp, "no-gh"))
                    p = subprocess.run([sys.executable, str(root / "bin" / tool), *args], capture_output=True,
                                       text=True, env=env, cwd=cwd, stdin=subprocess.DEVNULL, timeout=120)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertEqual(p.stdout, "")
                    self.assertNotIn("Traceback", p.stderr)
                    lines = p.stderr.splitlines()
                    self.assertEqual(len(lines), 1, p.stderr)
                    prefix = f"{tool}: cannot load harness_lib from {root / 'lib'}: "
                    self.assertTrue(lines[0].startswith(prefix), f"{lines[0]!r} does not start with {prefix!r}")
                    self.assertIn(error_has, lines[0][len(prefix):])

    def hook(self, root: Path, name: str, payload: dict, cwd: str):
        return subprocess.run([sys.executable, str(root / "hooks" / name)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env, cwd=cwd, timeout=60)

    def edit_payload(self, wt: str) -> dict:
        return {
            "session_id": "sess-1", "transcript_path": "/dev/null", "cwd": wt, "permission_mode": "default",
            "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_use_id": "toolu_01",
            "tool_input": {"file_path": os.path.join(wt, "src", "app.py"), "old_string": "a", "new_string": "b"},
        }

    def brief_payload(self, project: str) -> dict:
        return {"session_id": "sess-1", "transcript_path": "/dev/null", "cwd": project,
                "permission_mode": "default", "hook_event_name": "SessionStart", "source": "startup"}

    def assert_fail_open(self, r, hook: str, root: Path, error_has: str) -> None:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        prefix = f"{hook}: cannot load harness_lib from {root / 'lib'}: "
        self.assertTrue(lines[0].startswith(prefix), f"{lines[0]!r} does not start with {prefix!r}")
        self.assertIn(error_has, lines[0][len(prefix):])

    def test_tests_first_fails_open_with_one_line(self):
        repo = self.make_repo("guard")
        wt = self.add_worktree(repo, os.path.join(self.tmp, "guard-wt"), BRANCH)
        real = self.plugin("real")
        r = self.hook(real, "tests_first.py", self.edit_payload(wt), wt)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        decision = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].startswith("Tests first on t/12"))
        for lib, error_has in (("raises", "broken repo " + self.token), ("missing", "repo")):
            with self.subTest(lib=lib):
                root = self.plugin(lib)
                r = self.hook(root, "tests_first.py", self.edit_payload(wt), wt)
                self.assert_fail_open(r, "tests_first.py", root, error_has)
                self.assertEqual(r.stdout.strip(), "")  # no decision: the call is allowed

    def test_session_brief_fails_open_with_one_line(self):
        project = self.make_repo("briefed")
        real = self.plugin("real")
        r = self.hook(real, "session_brief.py", self.brief_payload(project), project)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        for lib, error_has in (("raises", "broken repo " + self.token), ("missing", "repo")):
            with self.subTest(lib=lib):
                root = self.plugin(lib)
                r = self.hook(root, "session_brief.py", self.brief_payload(project), project)
                self.assert_fail_open(r, "session_brief.py", root, error_has)
                context = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(len([ln for ln in context.splitlines() if "harness_lib" in ln]), 1, context)


if __name__ == "__main__":
    unittest.main()
