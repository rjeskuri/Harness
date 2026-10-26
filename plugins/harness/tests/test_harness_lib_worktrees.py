"""Tests for the worktree parser in plugins/harness/lib/harness_lib/repo.py (ticket #93, refactor T15):
the one `git worktree list --porcelain` parser harness-work, harness-pr, harness-digest and
harness-standup share.

- Worktree(path, branch, head, bare, locked, prunable) and worktree_entries(main, git) -> list[Worktree],
  on real temporary git repositories: the main tree first, linked worktrees with the branch's short name
  and the head commit, a detached worktree (head, no branch), a locked one (with and without a reason),
  a prunable one (folder removed by hand), a bare repository's own entry, paths with spaces, worktrees
  listed from a linked worktree.
- The same parser on canned porcelain text (blank lines between blocks, a missing final newline, branches
  outside refs/heads, unknown keys, empty output) and the contract with the caller's git: it is called with
  `main` and the arguments `worktree list --porcelain`, and what it raises (the tool's own error text)
  reaches the caller untouched. The git callable may return the text, or an object that is the text and
  carries .stdout/.returncode; the tests pass an object that is both.
- One copy, by behavior: the four tools bind the lib's worktree_entries, and a stand-in put in a tool's
  namespace decides what every worktree-reading path sees (harness-work worktrees, worktree remove and
  start's reuse; harness-pr worktree_for; the stale-worktree lookup of digest and standup), so no path keeps
  a parser of its own. A recording git on PATH shows each listing runs `git worktree list --porcelain`
  exactly once; digest and standup hand the lib their own git (deadline, --no-optional-locks) and keep their
  `Problem` text `git worktree list failed: <first stderr line | exit N>`; what a tool's git raises reaches
  the caller. A token scan of the tools' source is deliberately not used: literals can be split, and it
  false-positives on honest text.
- A missing or broken harness_lib/repo.py: harness-digest and harness-standup exit 1 with one line, as
  harness-work and harness-pr do (test_harness_lib_repo.py).

The land, remove and stale-worktree outputs stay pinned by test_pr, test_work, test_digest and
test_standup_v2, which do not change.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_worktrees.py" -v
"""

from __future__ import annotations

import ast
import atexit
import contextlib
import importlib
import importlib.util
import inspect
import io
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

sys.dont_write_bytecode = True  # loading the tools and the lib must not leave __pycache__ in the checkout

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
TOOLS = ("harness-work", "harness-pr", "harness-digest", "harness-standup")
DAY = 86400

_COUNTER = [0]
_REAL_TMP = tempfile.gettempdir()   # taken before any test points TMPDIR into its own folder
_FAKE_GIT = []


# ================================================================ helpers


def lib_repo() -> types.ModuleType:
    """harness_lib.repo imported from the checkout's lib/, as a tool's prologue would; the import error
    (or the missing names) while the parser is not there yet is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.repo")


def load_tool(path: Path) -> types.ModuleType:
    _COUNTER[0] += 1
    modname = path.name.replace("-", "_") + f"_worktrees_test_{_COUNTER[0]}"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Out(str):
    """What a caller's git may hand back: the text itself, and a CompletedProcess-like view of it."""

    def __new__(cls, text, returncode=0, stderr=""):
        obj = super().__new__(cls, text)
        obj.stdout, obj.returncode, obj.stderr = text, returncode, stderr
        return obj


def flat(args):
    out = []
    for a in args:
        out.extend(flat(a) if isinstance(a, (list, tuple)) else [str(a)])
    return out


def real_git_callable(path, *args, **_ignored):
    """git itself in `path`; raises on a failing command, as the tools' own git helpers do."""
    p = subprocess.run(["git", "-C", str(path), *flat(args)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(flat(args))} failed: {p.stderr.strip()}")
    return Out(p.stdout, p.returncode, p.stderr)


def canned(text, calls=None):
    """A caller's git that always answers with `text`, noting how it was called."""

    def git(path, *args, **_ignored):
        if calls is not None:
            calls.append((path, flat(args)))
        return Out(text)

    return git


def row(w):
    """The comparable shape of a Worktree."""
    return (w.path, w.branch, w.head, bool(w.bare), bool(w.locked), bool(w.prunable))


def tree_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def fake_git_dir() -> str:
    """A folder with a `git` that logs its arguments to $FAKEGIT_LOG and runs the real one ($REAL_GIT);
    written once for the module (macOS checks a new executable on its first run)."""
    if not _FAKE_GIT:
        folder = tempfile.mkdtemp(prefix="harness-fakegit-", dir=_REAL_TMP)
        atexit.register(shutil.rmtree, folder, True)
        script = os.path.join(folder, "git")
        with open(script, "w") as fh:
            fh.write('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKEGIT_LOG"\nexec "$REAL_GIT" "$@"\n')
        os.chmod(script, 0o755)
        _FAKE_GIT.append(folder)
    return _FAKE_GIT[0]


def completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def defined_functions(tree: ast.Module) -> set:
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def called_names(tree: ast.Module) -> set:
    return {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def is_from_lib(fn) -> bool:
    try:
        source = inspect.getsourcefile(fn)
    except TypeError:
        return False
    return (getattr(fn, "__module__", None) == "harness_lib.repo" and source is not None
            and os.path.realpath(source) == os.path.realpath(str(REPO_PY)))


# ================================================================ fixtures


class GitCase(unittest.TestCase):
    """A realpath'd temp dir, a temp HOME, and a hermetic process environment for git and the lib."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-worktrees-lib-"))
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

    def git(self, cwd, *args, env=None) -> str:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           env=dict(self.env, **(env or {})), timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def make_repo(self, name: str = "proj", remote: bool = False) -> str:
        repo = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(repo, "src"))
        self.git(self.tmp, "init", "-q", "-b", "main", repo)
        with open(os.path.join(repo, "README.md"), "w") as fh:
            fh.write("hello\n")
        with open(os.path.join(repo, "src", "app.py"), "w") as fh:
            fh.write("x = 1\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        if remote:
            self.git(repo, "remote", "add", "origin", f"https://example.invalid/{name}.git")
        return repo

    def add_worktree(self, repo: str, where: str, branch: str = None, detach: bool = False) -> str:
        os.makedirs(os.path.dirname(where), exist_ok=True)
        if detach:
            self.git(repo, "worktree", "add", "-q", "--detach", where)
        else:
            self.git(repo, "worktree", "add", "-q", "-b", branch, where)
        return where

    def commit(self, where: str, name: str, when: int = None) -> str:
        with open(os.path.join(where, name), "w") as fh:
            fh.write(name + "\n")
        self.git(where, "add", "-A")
        env = {}
        if when is not None:
            env = {"GIT_AUTHOR_DATE": f"{when} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}
        self.git(where, "commit", "-q", "-m", f"add {name}", env=env)
        return self.git(where, "rev-parse", "HEAD")

    def head(self, where: str) -> str:
        return self.git(where, "rev-parse", "HEAD")


class LibCase(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.lib = lib_repo()

    def entries(self, main, git=real_git_callable):
        result = self.lib.worktree_entries(main, git)
        self.assertIsInstance(result, list)
        return result


# ================================================================ the module


class ModuleTests(unittest.TestCase):
    def test_repo_py_defines_the_worktree_type_and_the_parser(self):
        self.assertTrue(REPO_PY.is_file(), f"{REPO_PY} does not exist")
        lib = lib_repo()
        self.assertTrue(callable(getattr(lib, "worktree_entries", None)), "no worktree_entries in harness_lib.repo")
        self.assertTrue(callable(getattr(lib, "Worktree", None)), "no Worktree in harness_lib.repo")
        self.assertEqual(list(inspect.signature(lib.worktree_entries).parameters)[:2], ["main", "git"])

    def test_a_worktree_is_built_from_path_branch_head_bare_locked_prunable(self):
        lib = lib_repo()
        w = lib.Worktree("/some/where", "t/9-x", "abc123", False, True, False)
        self.assertEqual((w.path, w.branch, w.head, w.bare, w.locked, w.prunable),
                         ("/some/where", "t/9-x", "abc123", False, True, False))
        other = lib.Worktree("/else", None, None, True, False, True)
        self.assertEqual((other.path, other.branch, other.head, other.bare, other.locked, other.prunable),
                         ("/else", None, None, True, False, True))


# ================================================================ real repositories


@unittest.skipUnless(GIT, "git is not installed")
class RealRepositoryTests(LibCase):
    def test_a_fresh_repository_lists_only_its_main_tree_on_its_branch(self):
        repo = self.make_repo()
        (only,) = self.entries(repo)
        self.assertEqual(row(only), (repo, "main", self.head(repo), False, False, False))

    def test_the_branch_of_the_main_tree_follows_the_checkout(self):
        repo = self.make_repo()
        for branch in ("trunk", "feature/deep/name", "t/93-one-parser"):
            with self.subTest(branch=branch):
                self.git(repo, "checkout", "-q", "-B", branch)
                (only,) = self.entries(repo)
                self.assertEqual((only.path, only.branch), (repo, branch))

    def test_linked_worktrees_come_after_the_main_tree_with_short_branch_names_and_their_own_heads(self):
        repo = self.make_repo()
        trees = os.path.join(repo, ".claude", "worktrees")
        branches = {"t/12-fix-login": "t-12-fix-login", "feature/x": "feature-x", "plain": "plain"}
        paths = {b: self.add_worktree(repo, os.path.join(trees, d), b) for b, d in branches.items()}
        heads = {b: self.commit(p, f"{d}.txt") for (b, p), d in zip(paths.items(), branches.values())}
        got = self.entries(repo)
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0].path, repo)
        self.assertEqual(got[0].branch, "main")
        self.assertEqual(got[0].head, self.head(repo))
        by_path = {w.path: w for w in got[1:]}
        self.assertEqual(set(by_path), set(paths.values()))
        for branch, path in paths.items():
            with self.subTest(branch=branch):
                self.assertEqual(row(by_path[path]), (path, branch, heads[branch], False, False, False))
        self.assertEqual(len({w.head for w in got}), 4, "four different commits, four different heads")

    def test_the_list_is_the_same_from_a_linked_worktree_and_the_main_tree_stays_first(self):
        repo = self.make_repo()
        a = self.add_worktree(repo, os.path.join(self.tmp, "wt-a"), "t/1-a")
        b = self.add_worktree(repo, os.path.join(self.tmp, "wt-b"), "t/2-b")
        from_main = self.entries(repo)
        for start in (a, b):
            with self.subTest(start=start):
                from_linked = self.entries(start)
                self.assertEqual(from_linked[0].path, repo)
                self.assertEqual(sorted(map(row, from_linked)), sorted(map(row, from_main)))
        self.assertEqual({w.path for w in from_main}, {repo, a, b})

    def test_a_detached_worktree_has_its_head_and_no_branch(self):
        repo = self.make_repo()
        det = self.add_worktree(repo, os.path.join(self.tmp, "wt-detached"), detach=True)
        sha = self.commit(det, "d.txt")
        got = {w.path: w for w in self.entries(repo)}
        self.assertEqual(row(got[det]), (det, None, sha, False, False, False))
        self.assertEqual(got[repo].branch, "main")
        self.assertNotEqual(got[repo].head, sha)

    def test_a_worktree_path_with_spaces_is_kept_whole(self):
        repo = self.make_repo()
        spaced = self.add_worktree(repo, os.path.join(self.tmp, "my work trees", "t 7 spaced"), "t/7-spaced")
        got = {w.path: w for w in self.entries(repo)}
        self.assertIn(spaced, got)
        self.assertEqual(got[spaced].branch, "t/7-spaced")

    def test_a_locked_worktree_is_flagged_with_or_without_a_reason(self):
        repo = self.make_repo()
        plain = self.add_worktree(repo, os.path.join(self.tmp, "wt-plain"), "t/1-plain")
        reason = self.add_worktree(repo, os.path.join(self.tmp, "wt-reason"), "t/2-reason")
        silent = self.add_worktree(repo, os.path.join(self.tmp, "wt-silent"), "t/3-silent")
        self.git(repo, "worktree", "lock", "--reason", "on a usb stick", reason)
        self.git(repo, "worktree", "lock", silent)
        got = {w.path: w for w in self.entries(repo)}
        self.assertEqual([bool(got[p].locked) for p in (repo, plain, reason, silent)], [False, False, True, True])
        self.assertEqual(got[reason].branch, "t/2-reason")
        self.assertFalse(any(got[p].prunable or got[p].bare for p in got))

    def test_a_worktree_whose_folder_was_removed_by_hand_is_prunable(self):
        repo = self.make_repo()
        gone = self.add_worktree(repo, os.path.join(self.tmp, "wt-gone"), "t/4-gone")
        kept = self.add_worktree(repo, os.path.join(self.tmp, "wt-kept"), "t/5-kept")
        shutil.rmtree(gone)
        if "prunable" not in self.git(repo, "worktree", "list", "--porcelain"):
            self.skipTest("this git does not report prunable worktrees")
        got = {w.path: w for w in self.entries(repo)}
        self.assertTrue(got[gone].prunable)
        self.assertEqual(got[gone].branch, "t/4-gone")
        self.assertFalse(got[kept].prunable)
        self.assertFalse(got[repo].prunable)
        self.assertEqual(sorted(w.path for w in got.values() if w.prunable), [gone])

    def test_a_bare_repository_lists_itself_bare_with_no_branch_or_head(self):
        repo = self.make_repo()
        bare = os.path.join(self.tmp, "bare.git")
        self.git(self.tmp, "clone", "-q", "--bare", repo, bare)
        linked = os.path.join(self.tmp, "from-bare")
        self.git(bare, "worktree", "add", "-q", linked, "main")
        got = self.entries(bare)
        self.assertEqual(got[0].path, bare)
        self.assertIs(got[0].bare, True)
        self.assertIsNone(got[0].branch)
        self.assertIsNone(got[0].head)
        (wt,) = got[1:]
        self.assertEqual(row(wt), (linked, "main", self.head(repo), False, False, False))

    def test_the_git_callable_is_called_with_main_and_the_porcelain_arguments(self):
        repo = self.make_repo()
        self.add_worktree(repo, os.path.join(self.tmp, "wt"), "t/1-x")
        calls = []

        def spy(path, *args, **kw):
            calls.append((path, flat(args)))
            return real_git_callable(path, *args, **kw)

        got = self.entries(repo, spy)
        self.assertEqual(len(got), 2)
        self.assertTrue(calls)
        for path, args in calls:
            self.assertEqual(path, repo)
            self.assertEqual(args[-3:], ["worktree", "list", "--porcelain"])


# ================================================================ canned porcelain


class CannedPorcelainTests(unittest.TestCase):
    """The parser on text, independent of any repository."""

    def setUp(self) -> None:
        self.lib = lib_repo()

    def parse(self, text, main="/m"):
        calls = []
        got = self.lib.worktree_entries(main, canned(text, calls))
        self.assertIsInstance(got, list)
        self.assertEqual([c[0] for c in calls], [main] * len(calls))
        self.assertEqual(calls[0][1][-3:], ["worktree", "list", "--porcelain"])
        return [row(w) for w in got]

    def test_a_table_of_blocks(self):
        sha = [("%040x" % (n * 7919 + 1)) for n in range(8)]
        cases = [
            (
                "main and one linked, blank-line separated",
                f"worktree /r/main\nHEAD {sha[0]}\nbranch refs/heads/main\n\n"
                f"worktree /r/.claude/worktrees/t-1-a\nHEAD {sha[1]}\nbranch refs/heads/t/1-a\n\n",
                [("/r/main", "main", sha[0], False, False, False),
                 ("/r/.claude/worktrees/t-1-a", "t/1-a", sha[1], False, False, False)],
            ),
            (
                "no final newline and no trailing blank line",
                f"worktree /r/main\nHEAD {sha[2]}\nbranch refs/heads/trunk\n\nworktree /r/w\nHEAD {sha[3]}\nbranch refs/heads/f/x",
                [("/r/main", "trunk", sha[2], False, False, False), ("/r/w", "f/x", sha[3], False, False, False)],
            ),
            (
                "detached has a head and no branch",
                f"worktree /r/main\nHEAD {sha[0]}\nbranch refs/heads/main\n\nworktree /r/d\nHEAD {sha[4]}\ndetached\n",
                [("/r/main", "main", sha[0], False, False, False), ("/r/d", None, sha[4], False, False, False)],
            ),
            (
                "locked with a reason, locked without, prunable with a reason",
                f"worktree /r/main\nHEAD {sha[0]}\nbranch refs/heads/main\n\n"
                f"worktree /r/l1\nHEAD {sha[1]}\nbranch refs/heads/l1\nlocked on a usb stick\n\n"
                f"worktree /r/l2\nHEAD {sha[2]}\nbranch refs/heads/l2\nlocked\n\n"
                f"worktree /r/p\nHEAD {sha[3]}\ndetached\nprunable gitdir file points to non-existent location\n",
                [("/r/main", "main", sha[0], False, False, False),
                 ("/r/l1", "l1", sha[1], False, True, False),
                 ("/r/l2", "l2", sha[2], False, True, False),
                 ("/r/p", None, sha[3], False, False, True)],
            ),
            (
                "bare has neither head nor branch",
                f"worktree /r/bare.git\nbare\n\nworktree /r/w\nHEAD {sha[5]}\nbranch refs/heads/main\n",
                [("/r/bare.git", None, None, True, False, False), ("/r/w", "main", sha[5], False, False, False)],
            ),
            (
                "spaces in the path, a ref outside refs/heads kept as given, unknown keys ignored",
                f"worktree /r/a b/c  d\nHEAD {sha[6]}\nbranch refs/remotes/origin/x\nfuture-key some value\n\n"
                f"worktree /r/e\nHEAD {sha[7]}\nbranch refs/heads/refs/heads/odd\n",
                [("/r/a b/c  d", "refs/remotes/origin/x", sha[6], False, False, False),
                 ("/r/e", "refs/heads/odd", sha[7], False, False, False)],
            ),
            (
                "several blank lines between blocks",
                f"worktree /r/one\nHEAD {sha[1]}\nbranch refs/heads/one\n\n\n\nworktree /r/two\nHEAD {sha[2]}\nbranch refs/heads/two\n\n\n",
                [("/r/one", "one", sha[1], False, False, False), ("/r/two", "two", sha[2], False, False, False)],
            ),
        ]
        for label, text, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.parse(text), expected)

    def test_the_branch_depends_on_the_text_not_on_a_fixed_name(self):
        for ref, short in (("refs/heads/a", "a"), ("refs/heads/t/93-one-parser", "t/93-one-parser"),
                           ("refs/heads/x/y/z", "x/y/z")):
            with self.subTest(ref=ref):
                (got,) = self.parse(f"worktree /w\nHEAD {'ab' * 20}\nbranch {ref}\n")
                self.assertEqual((got[0], got[1], got[2]), ("/w", short, "ab" * 20))

    def test_empty_output_is_no_entries(self):
        self.assertEqual(self.parse(""), [])
        self.assertEqual(self.parse("\n\n"), [])

    def test_an_error_from_the_callers_git_reaches_the_caller_untouched(self):
        class Boom(Exception):
            pass

        boom = Boom("git worktree list failed: fatal: not a git repository")

        def failing(path, *args, **kw):
            raise boom

        with self.assertRaises(Boom) as caught:
            self.lib.worktree_entries("/m", failing)
        self.assertIs(caught.exception, boom)

    def test_each_call_asks_git_again(self):
        answers = ["worktree /one\nHEAD aa\nbranch refs/heads/one\n", "worktree /two\nHEAD bb\nbranch refs/heads/two\n"]

        def git(path, *args, **kw):
            return Out(answers.pop(0))

        self.assertEqual([w.path for w in self.lib.worktree_entries("/m", git)], ["/one"])
        self.assertEqual([w.path for w in self.lib.worktree_entries("/m", git)], ["/two"])


# ================================================================ one copy


class OneCopyTests(unittest.TestCase):
    def test_the_parser_is_defined_in_the_lib_and_in_no_tool(self):
        self.assertTrue(REPO_PY.is_file(), f"{REPO_PY} does not exist")
        defined = defined_functions(tree_of(REPO_PY))
        self.assertIn("worktree_entries", defined)
        for tool in TOOLS:
            with self.subTest(tool=tool):
                own = defined_functions(tree_of(BIN / tool))
                self.assertNotIn("worktree_entries", own)
                self.assertNotIn("worktrees", own)

    def test_land_reads_the_branchs_worktree_through_worktree_for(self):
        tree = tree_of(BIN / "harness-pr")
        # #142: cmd_land's body is land_pr (autoland lands through it too); cmd_land calls it
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("land_pr", called_names(functions["cmd_land"]))
        self.assertIn("worktree_for", called_names(functions["land_pr"]))

    def test_the_tools_bind_worktree_entries_from_the_lib(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                mod = load_tool(BIN / tool)
                self.assertTrue(is_from_lib(getattr(mod, "worktree_entries", None)), tool)

    def test_each_tool_calls_the_lib_parser(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertIn("worktree_entries", called_names(tree_of(BIN / tool)))


# ================================================================ the callers build on the lib


@unittest.skipUnless(GIT, "git is not installed")
class CallersBuildOnTheLibTests(GitCase):
    """A stand-in put in the tool's namespace decides which worktrees the tool sees."""

    def setUp(self) -> None:
        super().setUp()
        self.lib = lib_repo()

    def W(self, path, branch=None, head=None, bare=False, locked=False, prunable=False):
        return self.lib.Worktree(path, branch, head, bare, locked, prunable)

    def test_pr_worktree_for_finds_the_path_of_the_branchs_worktree(self):
        mod = load_tool(BIN / "harness-pr")
        main = "/nowhere/main"
        listing = [self.W(main, "main", "a" * 40), self.W("/nowhere/wt/one", "t/12-fix-login", "b" * 40),
                   self.W("/nowhere/wt/two", "t/12-fix", "c" * 40), self.W("/nowhere/wt/det", None, "d" * 40)]
        seen = []

        def stand_in(path, git, *rest, **kw):
            seen.append(path)
            return list(listing)

        with mock.patch.object(mod, "worktree_entries", stand_in):
            self.assertEqual(mod.worktree_for(main, "t/12-fix-login"), "/nowhere/wt/one")
            self.assertEqual(mod.worktree_for(main, "t/12-fix"), "/nowhere/wt/two")
            self.assertEqual(mod.worktree_for(main, "main"), main)
            self.assertIsNone(mod.worktree_for(main, "t/12"))
            self.assertIsNone(mod.worktree_for(main, "t/99-other"))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {main})

    def test_work_make_worktree_reuses_the_worktree_the_parser_lists_for_the_ticket(self):
        mod = load_tool(BIN / "harness-work")
        repo_path = self.make_repo(f"work-{self.token}", remote=True)
        repo = mod.Repo(repo_path)
        existing = os.path.join(self.tmp, "listed-by-the-stand-in")
        other = os.path.join(self.tmp, "another-ticket")
        os.makedirs(existing)
        os.makedirs(other)
        listing = [self.W(repo_path, "main", "a" * 40), self.W(other, "t/70-other", "b" * 40),
                   self.W(existing, "t/7-stand-in-branch", "c" * 40)]
        with mock.patch.object(mod, "worktree_entries", lambda *a, **kw: list(listing)):
            branch, path, created = mod.make_worktree(repo, 7, "Stand in branch")
        self.assertEqual((branch, path, created), ("t/7-stand-in-branch", os.path.realpath(existing), False))

    def stale_fixture(self, name):
        repo = self.make_repo(f"{name}-{self.token}")
        now = int(time.time())
        old = self.commit(repo, "old.txt", now - 12 * DAY)
        self.git(repo, "branch", "t/9-old-branch", old)
        fresh = self.commit(repo, "fresh.txt", now - 3600)
        return repo, now, old, fresh

    def check_old_worktrees(self, tool):
        mod = load_tool(BIN / tool)
        repo, now, old, fresh = self.stale_fixture(tool)
        trees = os.path.join(repo, ".claude", "worktrees")
        listing = [
            self.W(repo, "main", fresh),
            self.W(os.path.join(trees, "ghost-old"), None, old),                      # stale, by its head
            self.W(os.path.join(trees, "ghost-branch"), "t/9-old-branch", None),      # stale, by its branch
            self.W(os.path.join(trees, "ghost-fresh"), "t/10-fresh", fresh),          # recent
            self.W(os.path.join(self.tmp, "elsewhere"), "t/11-side", old),            # not a ticket worktree
            self.W(os.path.join(trees, "ghost-nothing"), None, None),                 # nothing to look up
        ]
        calls = []

        def stand_in(path, git, *rest, **kw):
            calls.append(path)
            return list(listing)

        with mock.patch.object(mod, "worktree_entries", stand_in):
            found = mod.old_worktrees(repo, now, time.monotonic() + 30)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {repo})
        got = sorted((f["relative"], f["branch"], f["path"]) for f in found)
        self.assertEqual(got, sorted([
            (os.path.join(".claude", "worktrees", "ghost-old"), None, os.path.join(trees, "ghost-old")),
            (os.path.join(".claude", "worktrees", "ghost-branch"), "t/9-old-branch",
             os.path.join(trees, "ghost-branch")),
        ]))

    def test_digest_old_worktrees_come_from_the_lib_parser(self):
        self.check_old_worktrees("harness-digest")

    def test_standup_old_worktrees_come_from_the_lib_parser(self):
        self.check_old_worktrees("harness-standup")

    def work_repo(self, name):
        mod = load_tool(BIN / "harness-work")
        return mod, mod.Repo(self.make_repo(f"{name}-{self.token}", remote=True))

    def run_quiet(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def test_work_worktrees_shows_what_the_parser_lists_and_nothing_else(self):
        mod, repo = self.work_repo("wk-list")
        trees = repo.worktrees_dir
        live, detached, bare_dir = (os.path.join(trees, d) for d in ("t-1-live", "t-3-detached", "t-4-bare"))
        gone = os.path.join(trees, "t-2-gone")                       # no folder: a prunable entry
        outside = os.path.join(self.tmp, "outside")
        for d in (live, detached, bare_dir, outside):
            os.makedirs(d)
        listing = [self.W(repo.main, "main", "a" * 40), self.W(live, "t/1-live", "b" * 40),
                   self.W(gone, "t/2-gone", "c" * 40, prunable=True), self.W(detached, None, "d" * 40),
                   self.W(bare_dir, None, None, bare=True), self.W(outside, "t/5-out", "e" * 40)]
        asked, heads = [], []

        def stand_in(path, git, *rest, **kw):
            asked.append(path)
            return list(listing)

        def gh_json(repo_, args):
            heads.append(args[args.index("--head") + 1])
            return []

        with mock.patch.object(mod, "worktree_entries", stand_in), mock.patch.object(mod, "gh_json", gh_json):
            as_json = json.loads(self.run_quiet(mod.cmd_worktrees, repo, types.SimpleNamespace(stale=7, json=True)))
            as_text = self.run_quiet(mod.cmd_worktrees, repo, types.SimpleNamespace(stale=7, json=False))
        self.assertEqual(set(asked), {repo.main})
        self.assertEqual([(r["path"], r["branch"], r["missing"]) for r in as_json],
                         [(os.path.realpath(live), "t/1-live", False), (os.path.realpath(gone), "t/2-gone", True),
                          (os.path.realpath(detached), None, False)])
        self.assertEqual(sorted(set(heads)), ["t/1-live", "t/2-gone"], "a PR lookup per branch, none for detached/bare")
        for shown in ("t/1-live", "t/2-gone", "(detached)", "missing"):
            self.assertIn(shown, as_text)
        for hidden in ("t-4-bare", "t/5-out", "outside"):
            self.assertNotIn(hidden, as_text)

    def test_work_worktrees_with_only_the_main_tree_listed_has_no_ticket_worktrees(self):
        mod, repo = self.work_repo("wk-none")
        real = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-8-real"), "t/8-real")
        self.assertTrue(os.path.isdir(real))
        with mock.patch.object(mod, "worktree_entries", lambda *a, **kw: [self.W(repo.main, "main", "a" * 40)]), \
                mock.patch.object(mod, "gh_json", lambda *a, **kw: []):
            self.assertEqual(self.run_quiet(mod.cmd_worktrees, repo, types.SimpleNamespace(stale=7, json=False)),
                             "No ticket worktrees.\n")

    def test_work_remove_finds_the_branchs_worktree_only_through_the_parser(self):
        mod, repo = self.work_repo("wk-rm")
        real = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-20-real"), "t/20-real")
        listing = [self.W(repo.main, "main", "a" * 40), self.W(real, "t/99-aliased", "b" * 40)]
        asked = []

        def stand_in(path, git, *rest, **kw):
            asked.append(path)
            return list(listing)

        with mock.patch.object(mod, "worktree_entries", stand_in):
            out = self.run_quiet(mod.cmd_worktree_remove, repo,
                                 types.SimpleNamespace(branch="t/99-aliased", discard=False))
        self.assertEqual(out, f"removed worktree {os.path.realpath(real)}\n")
        self.assertFalse(os.path.exists(real))
        self.assertEqual(set(asked), {repo.main})
        self.git(repo.main, "rev-parse", "--verify", "refs/heads/t/20-real")        # its own branch is untouched

    def test_work_remove_leaves_a_worktree_the_parser_does_not_list(self):
        mod, repo = self.work_repo("wk-rm-hidden")
        real = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-51-hidden"), "t/51-hidden")
        with mock.patch.object(mod, "worktree_entries", lambda *a, **kw: [self.W(repo.main, "main", "a" * 40)]):
            with self.assertRaises(mod.Problem):
                self.run_quiet(mod.cmd_worktree_remove, repo,
                               types.SimpleNamespace(branch="t/51-hidden", discard=True))
        self.assertTrue(os.path.isdir(real), "the worktree was removed although the parser did not list it")

    def test_work_remove_refuses_a_branch_the_parser_puts_in_the_main_checkout(self):
        mod, repo = self.work_repo("wk-rm-main")
        before = self.git(repo.main, "worktree", "list", "--porcelain")
        with mock.patch.object(mod, "worktree_entries",
                               lambda *a, **kw: [self.W(repo.main, "t/40-in-main", "a" * 40)]):
            with self.assertRaises(mod.Problem) as caught:
                self.run_quiet(mod.cmd_worktree_remove, repo, types.SimpleNamespace(branch="t/40-in-main", discard=False))
        self.assertIn("checked out in the main checkout", str(caught.exception))
        self.assertEqual(self.git(repo.main, "worktree", "list", "--porcelain"), before)


# ================================================================ one listing, by the lib, through the tool's git


@unittest.skipUnless(GIT, "git is not installed")
class OneListingPerReadTests(GitCase):
    """A recording git on PATH: every worktree-reading path runs `git worktree list --porcelain` once."""

    def setUp(self) -> None:
        super().setUp()
        self.log = os.path.join(self.tmp, "git.log")
        patcher = mock.patch.dict(os.environ, {"PATH": f"{fake_git_dir()}:{self.env['PATH']}",
                                               "FAKEGIT_LOG": self.log, "REAL_GIT": GIT})
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset_log(self):
        with open(self.log, "w"):
            pass

    def list_calls(self):
        with open(self.log) as fh:
            return [ln.strip() for ln in fh if "worktree list --porcelain" in ln]

    def work_repo(self, name):
        mod = load_tool(BIN / "harness-work")
        return mod, mod.Repo(self.make_repo(f"{name}-{self.token}", remote=True))

    def test_pr_worktree_for_runs_one_listing_and_answers_from_it(self):
        mod = load_tool(BIN / "harness-pr")
        repo = self.make_repo(f"pr-{self.token}")
        trees = os.path.join(repo, ".claude", "worktrees")
        a = self.add_worktree(repo, os.path.join(trees, "t-1-a"), "t/1-a")
        b = self.add_worktree(repo, os.path.join(trees, "t-2-b"), "t/2-b")
        for branch, expected in (("t/1-a", a), ("t/2-b", b), ("main", repo), ("t/3-none", None)):
            with self.subTest(branch=branch):
                self.reset_log()
                self.assertEqual(mod.worktree_for(repo, branch), expected)
                self.assertEqual(len(self.list_calls()), 1)

    def test_work_worktrees_runs_one_listing(self):
        mod, repo = self.work_repo("wk-once")
        a = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-1-a"), "t/1-a")
        b = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-2-b"), "t/2-b")
        self.reset_log()
        out = io.StringIO()
        with mock.patch.object(mod, "gh_json", lambda *a_, **kw: []), contextlib.redirect_stdout(out):
            mod.cmd_worktrees(repo, types.SimpleNamespace(stale=7, json=True))
        self.assertEqual(sorted(r["path"] for r in json.loads(out.getvalue())),
                         sorted([os.path.realpath(a), os.path.realpath(b)]))
        self.assertEqual(len(self.list_calls()), 1)

    def test_work_remove_runs_one_listing(self):
        mod, repo = self.work_repo("wk-rm-once")
        wt = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-3-c"), "t/3-c")
        self.reset_log()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_worktree_remove(repo, types.SimpleNamespace(branch="t/3-c", discard=False))
        self.assertEqual(out.getvalue(), f"removed worktree {os.path.realpath(wt)}\ndeleted branch t/3-c\n")
        self.assertEqual(len(self.list_calls()), 1)

    def test_work_make_worktree_runs_one_listing_and_reuses_the_ticket_worktree(self):
        mod, repo = self.work_repo("wk-start-once")
        wt = self.add_worktree(repo.main, os.path.join(repo.worktrees_dir, "t-7-reuse-me"), "t/7-reuse-me")
        self.reset_log()
        self.assertEqual(mod.make_worktree(repo, 7, "Reuse me"), ("t/7-reuse-me", os.path.realpath(wt), False))
        self.assertEqual(len(self.list_calls()), 1)

    def test_digest_and_standup_stale_lookup_runs_one_listing_with_their_own_git(self):
        for tool in ("harness-digest", "harness-standup"):
            with self.subTest(tool=tool):
                mod = load_tool(BIN / tool)
                repo = self.make_repo(f"{tool}-{self.token}")
                now = int(time.time())
                old = self.commit(repo, "old.txt", now - 12 * DAY)
                self.commit(repo, "fresh.txt", now - 3600)
                trees = os.path.join(repo, ".claude", "worktrees")
                stale = self.add_worktree(repo, os.path.join(trees, "ancient"), "t/9-old")
                self.git(stale, "reset", "-q", "--hard", old)
                self.reset_log()
                found = mod.old_worktrees(repo, now, time.monotonic() + 60)
                self.assertEqual([(f["path"], f["branch"], f["relative"]) for f in found],
                                 [(stale, "t/9-old", os.path.join(".claude", "worktrees", "ancient"))])
                (call,) = self.list_calls()
                self.assertIn("--no-optional-locks", call, "the tool's own git (no optional locks) runs the listing")


# ================================================================ digest and standup adapters


class StaleLookupAdapterTests(unittest.TestCase):
    """digest and standup run the listing through their own git(path, args, deadline); the tool's git is
    patched here, so nothing else can have produced the answer."""

    MAIN = "/nowhere/proj"

    def setUp(self) -> None:
        self.lib = lib_repo()
        self.now = 1_900_000_000
        self.old, self.fresh, self.other = "1" * 40, "2" * 40, "3" * 40
        t = f"{self.MAIN}/.claude/worktrees"
        self.text = (
            f"worktree {self.MAIN}\nHEAD {self.fresh}\nbranch refs/heads/main\n\n"
            f"worktree {t}/ancient one\nHEAD {self.old}\nbranch refs/heads/t/9-old\n\n"
            f"worktree {t}/recent\nHEAD {self.fresh}\nbranch refs/heads/t/10-new\n\n"
            f"worktree {t}/gone\nHEAD {self.old}\ndetached\nprunable gitdir file points to non-existent location\n\n"
            f"worktree {t}/the-bare\nbare\n\n"
            f"worktree /nowhere/elsewhere\nHEAD {self.other}\nbranch refs/heads/t/11-side\n"
        )
        self.when = {self.old: self.now - 12 * DAY, self.fresh: self.now - 3600, self.other: self.now - 99 * DAY}

    def fake(self, calls, list_result=None):
        def git(path, args, deadline=None, *rest, **kw):
            calls.append((path, list(args), deadline))
            if list(args[:2]) == ["worktree", "list"]:
                return list_result if list_result is not None else completed(0, self.text)
            if "log" in args:
                return completed(0, f"{self.when[args[-1]]}\n")
            return completed(0, "")
        return git

    def test_the_listing_goes_through_the_tools_git_with_its_deadline_and_is_parsed(self):
        for tool in ("harness-digest", "harness-standup"):
            for now_shift, expected_branches in ((0, ["t/9-old", None]), (10 * DAY, ["t/9-old", "t/10-new", None])):
                with self.subTest(tool=tool, shift=now_shift):
                    mod = load_tool(BIN / tool)
                    calls, deadline = [], time.monotonic() + 123.0
                    with mock.patch.object(mod, "git", self.fake(calls)):
                        found = mod.old_worktrees(self.MAIN, self.now + now_shift, deadline)
                    listings = [c for c in calls if flat(c[1])[:2] == ["worktree", "list"]]
                    self.assertEqual(len(listings), 1)
                    self.assertEqual(flat(listings[0][1]), ["worktree", "list", "--porcelain"])
                    self.assertEqual((listings[0][0], listings[0][2]), (self.MAIN, deadline))
                    self.assertEqual([f["branch"] for f in found], expected_branches)
                    self.assertEqual(found[0]["path"], f"{self.MAIN}/.claude/worktrees/ancient one")
                    self.assertEqual(found[0]["relative"], os.path.join(".claude", "worktrees", "ancient one"))
                    asked = {flat(c[1])[-1] for c in calls if "log" in flat(c[1])}
                    self.assertEqual(asked, {self.old, self.fresh})

    def test_a_failing_listing_is_the_tools_problem_with_the_first_stderr_line_or_the_exit_code(self):
        cases = [(128, "fatal: not a git repository (or any of the parent directories): .git\nhint: more\n",
                  "fatal: not a git repository (or any of the parent directories): .git"),
                 (7, "error: boom\nsecond line\n", "error: boom"), (1, "", "exit 1"), (3, "", "exit 3")]
        for tool in ("harness-digest", "harness-standup"):
            for code, stderr, why in cases:
                with self.subTest(tool=tool, code=code):
                    mod = load_tool(BIN / tool)
                    calls = []
                    with mock.patch.object(mod, "git", self.fake(calls, completed(code, "", stderr))):
                        with self.assertRaises(mod.Problem) as caught:
                            mod.old_worktrees(self.MAIN, self.now, time.monotonic() + 30)
                    self.assertEqual(str(caught.exception), f"git worktree list failed: {why}")
                    self.assertFalse([c for c in calls if "log" in flat(c[1])], "nothing is looked up after a failure")

    def test_what_the_tools_git_raises_reaches_the_caller_untouched(self):
        class Boom(Exception):
            pass

        for tool in ("harness-digest", "harness-standup"):
            with self.subTest(tool=tool):
                mod = load_tool(BIN / tool)
                boom = Boom(f"{tool}: git worktree timed out")

                def timing_out(path, args, deadline=None, *rest, **kw):
                    raise boom

                with mock.patch.object(mod, "git", timing_out):
                    with self.assertRaises(Boom) as caught:
                        mod.old_worktrees(self.MAIN, self.now, time.monotonic() + 30)
                self.assertIs(caught.exception, boom)


@unittest.skipUnless(GIT, "git is not installed")
class StaleLookupOutsideARepositoryTests(GitCase):
    def test_digest_and_standup_report_git_failing_in_a_plain_folder(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        for tool in ("harness-digest", "harness-standup"):
            with self.subTest(tool=tool):
                mod = load_tool(BIN / tool)
                with self.assertRaises(mod.Problem) as caught:
                    mod.old_worktrees(plain, int(time.time()), time.monotonic() + 30)
                self.assertTrue(str(caught.exception).startswith("git worktree list failed: fatal: not a git repository"),
                                str(caught.exception))
                self.assertEqual(len(str(caught.exception).splitlines()), 1)


# ================================================================ without the lib's repo.py


@unittest.skipUnless(GIT, "git is not installed")
class WithoutRepoPyTests(GitCase):
    """A plugin copy whose lib/harness_lib/repo.py is missing or raises on import: digest and standup now
    need it (work and pr already did, test_harness_lib_repo.py)."""

    def plugin(self, lib: str) -> Path:
        root = Path(self.tmp) / f"plugin-{lib}"
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(BIN, root / "bin", ignore=ignore)
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

    def run_tool(self, root: Path, tool: str):
        return subprocess.run([sys.executable, str(root / "bin" / tool), "--help"], capture_output=True, text=True,
                              env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL, timeout=120)

    def test_the_intact_lib_loads(self):
        root = self.plugin("real")
        for tool in ("harness-digest", "harness-standup"):
            with self.subTest(tool=tool):
                p = self.run_tool(root, tool)
                self.assertEqual((p.returncode, p.stderr), (0, ""), p.stdout)

    def test_digest_and_standup_exit_1_with_one_line(self):
        for lib, error_has in (("raises", "broken repo " + self.token), ("missing", "repo")):
            root = self.plugin(lib)
            for tool in ("harness-digest", "harness-standup"):
                with self.subTest(lib=lib, tool=tool):
                    p = self.run_tool(root, tool)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertEqual(p.stdout, "")
                    self.assertNotIn("Traceback", p.stderr)
                    lines = p.stderr.splitlines()
                    self.assertEqual(len(lines), 1, p.stderr)
                    prefix = f"{tool}: cannot load harness_lib from {root / 'lib'}: "
                    self.assertTrue(lines[0].startswith(prefix), f"{lines[0]!r} does not start with {prefix!r}")
                    self.assertIn(error_has, lines[0][len(prefix):])


if __name__ == "__main__":
    unittest.main()
