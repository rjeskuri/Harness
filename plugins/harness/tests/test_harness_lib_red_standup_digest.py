"""Tests for ticket #81: harness-red, harness-standup and harness-digest import the state-key
helpers from plugins/harness/lib/harness_lib/paths.py instead of carrying copies.

Companion to tests/test_harness_lib.py (the lib itself and the hooks, ticket #79); the same
checks, for these three bin tools only. The other three tools (harness-board, harness-pr,
harness-work, ticket #80) are not covered here.

- The tools follow the lib (broad, behavioural): each tool, copied with the rest of the plugin into
  a temporary tree whose lib/ is instrumented (its keys are a md5 spelling no sha1 copy produces),
  keys its state by the lib's answers: harness-red records the red run under the lib's
  adopt_old_state key, harness-standup and harness-digest name a run's project from a run file
  named by the lib's key. Launched directly and through a symlink that sits next to a broken lib/
  (the lib is found from the tool's real path).
- Missing or broken lib: a tool prints one line `<tool>: cannot load harness_lib from <lib>: <error>`
  to stderr and exits 1 (docs/design.md: "A bin tool that imports the lib will print one line and
  exit 1 the same way"), whatever it was asked to do.
- Static backstops: the tools define none of the five helpers, carry no renamed copy, compute no
  sha1 key, and bind the names they use in their own namespace from harness_lib.paths. Against a
  copy kept as dead code or split across defs: every def in a tool is live, no def cuts a path on
  os.sep and matches a part's spelling, and no glob lists a directory's names.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_red_standup_digest.py" -v
"""

from __future__ import annotations

import ast
import builtins
import datetime as dt
import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.dont_write_bytecode = True  # loading the tools and the lib must not leave __pycache__ in the checkout

import test_harness_lib as thl  # noqa: E402  (helpers: AST checks, tool loaders, the lib's paths)

PLUGIN = thl.PLUGIN
BIN = PLUGIN / "bin"
PATHS_PY = thl.PATHS_PY
FIVE = thl.FIVE
FOUR = ("canonical_path", "same_entry", "path_key", "repo_key")
# The names each tool binds from harness_lib.paths, per the ticket: harness-standup and
# harness-digest import four and no adopt_old_state; harness-red imports what it uses (it
# canonicalises the main tree and adopts old state) plus repo_key, which the unchanged
# tests/test_repo_key.py reads from every tool (CanonicalPathNormalisation, STATE_KEY_FILES).
USES = {
    "harness-red": ("canonical_path", "repo_key", "adopt_old_state"),
    "harness-standup": FOUR,
    "harness-digest": FOUR,
}
TOOL_NAMES = tuple(USES)
BROKEN = ("missing", "syntax", "raises", "incomplete")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UTC = dt.timezone.utc
BRANCH, SLUG = "t/12-fix-login", "t-12-fix-login"
GIT = shutil.which("git")

# A lib that imports cleanly and exports every name, with keys no sha1 of a path gives:
#   path_key(p)        = "lib" + md5(p)[:9]        (12 characters, like a real key)
#   adopt_old_state(…) = "adopt-" + md5(main)[:6]  (so it is not path_key's answer)
SENTINEL_PATHS = '''from __future__ import annotations
import hashlib
import os


def canonical_path(path):
    # Not realpath: a "mirror-" directory is folded onto its sibling, and the answer carries a
    # "/." tail, so only a tool that asks this function gets the keys computed from it.
    head, tail = os.path.split(os.path.realpath(str(path)))
    if tail.startswith("mirror-"):
        tail = tail[len("mirror-"):]
    return os.path.join(head, tail) + "/."


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    return "lib" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:9]


def repo_key(path):
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    return "adopt-" + hashlib.md5(str(main).encode("utf-8")).hexdigest()[:6]
'''


def lib_key(path) -> str:
    """The sentinel lib's path_key, computed here."""
    return "lib" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:9]


def adopt_key(main) -> str:
    """The sentinel lib's adopt_old_state answer, computed here."""
    return "adopt-" + hashlib.md5(str(main).encode("utf-8")).hexdigest()[:6]


def canon(path) -> str:
    """The sentinel lib's canonical_path, computed here (a mirror- directory folds onto its sibling)."""
    head, tail = os.path.split(os.path.realpath(str(path)))
    if tail.startswith("mirror-"):
        tail = tail[len("mirror-"):]
    return os.path.join(head, tail) + "/."


def sha12(path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def stamp(seconds_ago: float) -> str:
    return dt.datetime.fromtimestamp(time.time() - seconds_ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


_SHARED: dict = {}


def setUpModule() -> None:
    if not GIT:
        raise unittest.SkipTest("git is not installed")
    base = Path(tempfile.mkdtemp(prefix="harness-lib-tools-")).resolve()
    fakebin = base / "fakebin"
    write(fakebin / "claude", '#!/bin/sh\nif [ "$1" = agents ]; then echo "[]"; fi\nexit 0\n', True)
    for name in ("terminal-notifier", "osascript"):
        write(fakebin / name, "#!/bin/sh\nexit 0\n", True)
    _SHARED.update(base=base, fakebin=fakebin, pycache=str(base / "pycache"))


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


class ToolCase(unittest.TestCase):
    """Runs the bin tools from a temporary plugin tree: the checkout's plugin copied (without its
    tests), with lib/ real, instrumented, missing or broken."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="Tools-", dir=str(_SHARED["base"]))).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.token = "boom-" + uuid.uuid4().hex[:8]
        self.env = dict(
            thl.GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp),
            PATH=os.pathsep.join([str(_SHARED["fakebin"]), os.path.dirname(GIT), SYSTEM_PATH]),
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
            HARNESS_LAUNCHCTL=str(self.tmp / "no-launchctl"),
            HARNESS_TEST_CMD=f"{sys.executable} -m unittest {{files}}",
        )
        self.cases = 0

    # ---- plugin trees

    def write_lib(self, root: Path, files: dict) -> None:
        pkg = root / "lib" / "harness_lib"
        pkg.mkdir(parents=True)
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")

    def plugin(self, lib: str) -> Path:
        """A plugin tree under tmp. lib is 'real' (the checkout's), 'sentinel' (SENTINEL_PATHS),
        'missing', 'syntax' (paths.py does not parse), 'raises' (__init__.py raises RuntimeError)
        or 'incomplete' (paths.py lacks the names)."""
        root = self.tmp / f"plugin-{lib}"
        if root.exists():
            return root
        shutil.copytree(
            PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__", "tests", "lib")
        )
        init = "from __future__ import annotations\n"
        if lib == "real":
            self.assertTrue(PATHS_PY.is_file(), f"{PATHS_PY} does not exist")
            shutil.copytree(thl.LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        elif lib == "sentinel":
            self.write_lib(root, {"__init__.py": init, "paths.py": SENTINEL_PATHS})
        elif lib == "syntax":
            self.write_lib(root, {"__init__.py": init, "paths.py": "def canonical_path(:\n    return\n"})
        elif lib == "raises":
            self.write_lib(
                root, {"__init__.py": f"raise RuntimeError({self.token!r})\n", "paths.py": ""}
            )
        elif lib == "incomplete":
            self.write_lib(
                root, {"__init__.py": init, "paths.py": "def path_key(path):\n    return 'x'\n"}
            )
        else:
            self.assertEqual(lib, "missing")
        return root

    def launcher(self, root: Path, tool: str, how: str) -> Path:
        """The path a tool is run by: 'direct' (root/bin/<tool>) or 'symlink' (a link next to a
        lib/ that raises, so only the link's real path leads to the working lib)."""
        target = root / "bin" / tool
        if how == "direct":
            return target
        self.links = getattr(self, "links", 0) + 1
        elsewhere = self.tmp / f"elsewhere{self.links}"
        self.write_lib(
            elsewhere, {"__init__.py": f"raise RuntimeError({self.token!r})\n", "paths.py": ""}
        )
        link = elsewhere / "bin" / tool
        link.parent.mkdir(parents=True)
        os.symlink(target, link)
        return link

    def run_tool(self, script: Path, *args: str, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(cwd or self.tmp),
            timeout=120,
        )

    # ---- fixtures

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True, env=self.env,
        )
        return out.stdout.strip()

    def make_repo(self, name: str) -> Path:
        """A git repo on main with one commit at <tmp>/case<n>/<name> (no remote)."""
        self.cases += 1
        repo = self.tmp / f"case{self.cases}" / name
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True, capture_output=True, env=self.env,
        )
        write(repo / "README.md", "hello\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo.resolve()

    def ticket_worktree(self, name: str) -> tuple[Path, Path]:
        """(main checkout, a linked worktree on t/12-fix-login with a failing new test)."""
        repo = self.make_repo(name)
        wt = repo / ".claude" / "worktrees" / SLUG
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        write(
            wt / "tests" / "test_login.py",
            "import unittest\n\n\nclass LoginTest(unittest.TestCase):\n"
            "    def test_login(self):\n        self.assertEqual(1 + 1, 3)\n",
        )
        return repo, wt.resolve()

    def state_dirs(self) -> list:
        repos = self.home / ".claude" / "harness" / "repos"
        return sorted(os.listdir(repos)) if repos.is_dir() else []

    def run_record(self, project: Path, ticket: int, name: str, **extra) -> None:
        """What harness-work writes for a headless run, named by `name` (a key) with no repo field."""
        record = dict(
            extra, id=f"{name}-{ticket}", ticket=ticket, venue="headless", outcome="error",
            exit=2, started_at=stamp(7200), ended_at=stamp(3600), worktree=None,
            branch=f"t/{ticket}-x",
        )
        write(self.home / ".claude" / "harness" / "runs" / f"{name}-{ticket}-20261013070000.json",
              json.dumps(record))

    def register(self, *projects: tuple) -> None:
        write(
            self.home / ".claude" / "harness" / "projects.json",
            json.dumps({"version": 1, "projects": [
                {"path": str(path), "name": name, "billable": False, "client": "", "notes": ""}
                for name, path in projects
            ]}),
        )


# ================================================================ the tools follow the lib


@unittest.skipUnless(GIT, "git is not installed")
class ToolsFollowTheLibTests(ToolCase):
    """Each tool, from a tree whose lib is instrumented, does what that lib's answers imply:
    the tool's behaviour comes from the lib's code, not from a copy of its own."""

    HOWS = ("direct", "symlink")

    def test_red_check_records_under_the_libs_adopt_old_state_key(self):
        root = self.plugin("sentinel")
        expected = []
        for how in self.HOWS:
            for name in ("alpha", "bravo"):  # the key follows the path, so it is not a constant
                with self.subTest(how=how, repo=name):
                    main, wt = self.ticket_worktree(f"{name}-{how}")
                    key = adopt_key(canon(main))   # keyed on the lib's canonical_path of the main tree
                    self.assertNotIn(key, (sha12(main), lib_key(main), adopt_key(main)))
                    r = self.run_tool(
                        self.launcher(root, "harness-red", how), "check", "--base", "main", cwd=wt
                    )
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    record = self.home / ".claude/harness/repos" / key / "red" / f"{SLUG}.json"
                    self.assertTrue(record.is_file(), f"no record at {record}: {self.state_dirs()}")
                    self.assertEqual(json.loads(record.read_text())["kind"], "assertion")
                    self.assertNotIn(sha12(main), self.state_dirs())  # no key of the tool's own
                    expected.append(key)
        self.assertEqual(len(set(expected)), len(expected))
        self.assertEqual(self.state_dirs(), sorted(expected))

    def run_names(self, tool: str, how: str, projects: tuple, runs: list) -> list:
        """(project, ticket) of each run `tool --json` lists, for run files named by `runs`' keys."""
        root = self.plugin("sentinel")
        self.register(*projects)
        for path, ticket, name, extra in runs:
            self.run_record(path, ticket, name, **extra)
        r = self.run_tool(self.launcher(root, tool, how), "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return sorted((run["project"], run["ticket"]) for run in json.loads(r.stdout)["runs"])

    def check_run_project(self, tool: str):
        """A run is matched to its project by keys the lib computes: the file's key against
        repo_key (which goes through the lib's canonical_path: here a marked, mirror-folding spelling,
        not realpath) and path_key of the real path, and a `repo` field against repo_key."""
        for how in self.HOWS:
            with self.subTest(how=how):
                shutil.rmtree(self.home / ".claude", ignore_errors=True)
                one = self.make_repo(f"widget-{how}")
                two = self.make_repo(f"gadget-{how}")
                mirror_one = one.parent / f"mirror-widget-{how}"      # no such directory: another spelling
                mirror_two = two.parent / f"mirror-gadget-{how}"
                stray = one.parent / f"mirror-unknown-{how}"
                self.assertNotEqual(lib_key(canon(one)), lib_key(one))
                projects = (("Widget Shop", one), ("Gadget Shop", two))
                runs = [
                    (one, 12, lib_key(canon(one)), {}),      # file key = repo_key of project one
                    (two, 5, lib_key(canon(two)), {}),       # of project two: another key, another name
                    (two, 6, lib_key(canon(one)), {}),       # the key says "one" whatever the ticket is
                    (one, 8, lib_key(one), {}),              # file key = path_key of the real path
                    (one, 7, sha12(one), {}),                # sha1 of the path: the lib's key it is not
                    # the repo field, its file key unknown: matched by repo_key, never by name or realpath
                    (one, 9, "stray0000009", {"repo": str(mirror_one)}),
                    (two, 10, "stray0000010", {"repo": str(mirror_two)}),
                    (two, 11, "stray0000011", {"repo": str(stray)}),   # no project: the repo's own name
                ]
                got = self.run_names(tool, how, projects, runs)
                self.assertEqual(
                    got,
                    sorted([
                        ("Widget Shop", 12), ("Gadget Shop", 5), ("Widget Shop", 6), ("Widget Shop", 8),
                        (sha12(one), 7), ("Widget Shop", 9), ("Gadget Shop", 10), (stray.name, 11),
                    ]),
                )

    def test_standup_names_the_project_of_a_run_by_the_libs_key(self):
        self.check_run_project("harness-standup")

    def test_digest_names_the_project_of_a_run_by_the_libs_key(self):
        self.check_run_project("harness-digest")


# ================================================================ missing or broken lib


@unittest.skipUnless(GIT, "git is not installed")
class LibFailureTests(ToolCase):
    def assert_one_line_exit_1(self, r, tool: str, lib: str, root: Path) -> None:
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        line = lines[0]
        self.assertTrue(line.startswith(f"{tool}: cannot load harness_lib from "), line)
        self.assertIn(str(root / "lib"), line)
        if lib == "raises":
            self.assertIn(self.token, line)  # the error itself, not a fixed message
        self.assertEqual(r.stdout, "")

    def test_a_missing_or_broken_lib_prints_one_line_and_exits_1(self):
        real = self.plugin("real")  # control: the same command starts with the real lib
        for tool in TOOL_NAMES:
            r = self.run_tool(real / "bin" / tool, "--help")
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            self.assertIn("usage", r.stdout.lower())
        for lib in BROKEN:
            root = self.plugin(lib)
            for tool in TOOL_NAMES:
                with self.subTest(lib=lib, tool=tool):
                    r = self.run_tool(root / "bin" / tool, "--help")
                    self.assert_one_line_exit_1(r, tool, lib, root)

    def test_a_broken_lib_stops_the_tool_whatever_it_is_asked_to_do(self):
        """The failure is at import, before any work: no run directory, no state is written."""
        root = self.plugin("missing")
        main, wt = self.ticket_worktree("proj")
        calls = (
            ("harness-red", ("check", "--base", "main"), wt),
            ("harness-standup", ("--json",), self.tmp),
            ("harness-digest", ("--json",), self.tmp),
        )
        for tool, args, cwd in calls:
            with self.subTest(tool=tool):
                r = self.run_tool(root / "bin" / tool, *args, cwd=cwd)
                self.assert_one_line_exit_1(r, tool, "missing", root)
        self.assertEqual(self.state_dirs(), [])
        self.assertFalse((self.home / ".claude" / "harness" / "digest").exists())


# ================================================================ the tools import, not copy


def tool_tree(name: str):
    return ast.parse((BIN / name).read_text(encoding="utf-8"), filename=name)


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

# The shape of a copy the checks below must catch (canonical_path's walk split over two dead
# defs, the directory listed by glob), beside live code they must not flag: an argparse
# override, a dunder, a method and a nested def that are called, the remote-URL parser, a
# file glob. The checks are run on it first, so a check that finds nothing fails too.
CHEAT = '''import argparse, glob, os, re


def _dir_names(done):
    import glob as g
    return [os.path.basename(p) for p in g.glob(os.path.join(g.escape(done), "*"))]

def _list(done):
    return glob.glob(os.path.join(glob.escape(done), "*"))


def _canon(path):
    done = os.sep
    for part in [p for p in os.path.realpath(path).split(os.sep) if p]:
        for name in _dir_names(done):
            if name.casefold() == part.casefold() and same_entry(name, part):
                part = name
        done = os.path.join(done, part)
    return _canon(done)


def remote(url):
    m = re.match(r"(?:https://|git@)([^/:]+)[/:](.+)", url)
    if not m or url.lower().startswith("file:"):
        return None
    host, parts = m.group(1), [p for p in m.group(2).split("/") if p]
    return host.lower(), parts


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit(message)


class Batch:
    def __init__(self):
        self.calls = []

    def results(self):
        def one(call):
            return call
        return [one(c) for c in self.calls]

    def _unused_method(self, path):
        return path


def runs():
    return sorted(glob.glob(os.path.join(glob.escape(os.path.expanduser("~")), "*.json")))


def main():
    Parser().parse_args([])
    return remote("https://github.com/a/b"), Batch().results(), runs(), _list


if __name__ == "__main__":
    main()
'''


def _shallow_refs(stmts) -> set:
    """Every name read and attribute named in `stmts`, not looking inside the body of a def
    (its decorators and defaults count: they run where it is defined). A class body runs where
    the class is defined, so it is looked into (its methods' bodies are not). Strings do not count."""
    found = set()
    stack = list(stmts)
    while stack:
        node = stack.pop()
        if isinstance(node, FUNCS):
            stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _owned(stmts) -> list:
    """(def or class, the class it is a method of or None) for each def and class defined by
    `stmts` themselves: not inside another def's body."""
    out = []
    stack = [(s, None) for s in stmts]
    while stack:
        node, cls = stack.pop()
        if isinstance(node, FUNCS):
            out.append((node, cls))
        elif isinstance(node, ast.ClassDef):
            out.append((node, cls))
            stack.extend((s, node) for s in node.body)
        else:
            stack.extend((c, cls) for c in ast.iter_child_nodes(node))
    return out


def _overrides(cls, name: str, imported: set) -> bool:
    """True when a base of `cls` that the tool imports from the standard library (argparse's
    ArgumentParser, Exception) defines `name`: a method the base class calls, such as
    Parser.error, which nothing in the tool names."""
    for base in cls.bases:
        obj = None
        if isinstance(base, ast.Name) and hasattr(builtins, base.id):
            obj = getattr(builtins, base.id)
        elif (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id in imported
        ):
            try:
                obj = getattr(importlib.import_module(base.value.id), base.attr, None)
            except ImportError:
                obj = None
        if obj is not None and hasattr(obj, name):
            return True
    return False


def dead_defs(tree) -> list[str]:
    """Each def and class that nothing live in the module refers to: code that never runs,
    such as a copy of a helper kept beside the import. Live: the module's own statements; a
    def or class whose name live code in its enclosing scope reads (its own body aside, so
    recursion does not count, and two dead defs naming each other stay dead); a method of a
    live class that live code names anywhere, that is a dunder, or that overrides a method of a
    standard-library base. Names are matched by spelling, which can only call more defs live."""
    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    entries = []  # (node, enclosing scope, class or None)
    scopes = [tree]
    while scopes:
        scope = scopes.pop()
        for node, cls in _owned(scope.body):
            entries.append((node, scope, cls))
            if isinstance(node, FUNCS):
                scopes.append(node)
    inside = {id(tree): {id(tree)}}  # scope -> the scopes within it (itself included)
    for node, scope, _ in entries:
        if isinstance(node, FUNCS):
            inside.setdefault(id(node), {id(node)})
    parent = {id(node): scope for node, scope, _ in entries if isinstance(node, FUNCS)}
    for key in list(inside):
        up = parent.get(key)
        while up is not None:
            inside.setdefault(id(up), {id(up)}).add(key)
            up = parent.get(id(up))
        inside[id(tree)].add(key)
    units = {id(tree): _shallow_refs(tree.body)}
    units.update({id(node): _shallow_refs(node.body) for node, _, _ in entries if isinstance(node, FUNCS)})
    live = {id(tree)}
    changed = True
    while changed:
        changed = False
        for node, scope, cls in entries:
            if id(node) in live or id(scope) not in live or (cls is not None and id(cls) not in live):
                continue
            if cls is not None and isinstance(node, FUNCS):
                name = node.name
                ok = (
                    (name.startswith("__") and name.endswith("__"))
                    or _overrides(cls, name, imported)
                    or any(name in units.get(u, ()) for u in live if u != id(node))
                )
            else:
                ok = any(
                    node.name in units.get(u, ()) for u in live & inside[id(scope)] if u != id(node)
                )
            if ok:
                live.add(id(node))
                changed = True
    return [
        f"{'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name} (line {node.lineno})"
        for node, _, _ in sorted(entries, key=lambda e: e[0].lineno)
        if id(node) not in live
    ]


def _is_sep(expr) -> bool:
    """os.sep, os.altsep, os.path.sep or os.path.altsep."""
    return isinstance(expr, ast.Attribute) and expr.attr in ("sep", "altsep") and (
        (isinstance(expr.value, ast.Name) and expr.value.id == "os")
        or (isinstance(expr.value, ast.Attribute) and expr.value.attr == "path")
    )


SPLITS = ("split", "rsplit", "partition", "rpartition")
# Folding or matching a name the way canonical_path does: strongly (casefold, NFC, the
# same-entry test) or weakly (lower, upper: also how URLs and host names are compared).
STRONG = {"casefold", "normcase", "normalize", "unicodedata", "samefile", "samestat", "same_entry"}
WEAK = {"lower", "upper", "swapcase"}


def part_matches(tree) -> list[str]:
    """Each def (with what is nested in it) that cuts a path into components and matches a
    component's spelling: canonical_path's walk, whatever lists the directory. Cutting is
    .split/.rsplit/.partition on os.sep (or "/" with a strong match), Path.parts or
    os.path.split; matching is casefold, normcase, unicodedata, samefile/samestat, same_entry,
    or (with os.sep) lower/upper. harness-standup's and harness-digest's remote-URL parser
    (split on "/" and lower the host) is neither."""
    found = []
    for fn in thl.defs(tree):
        sep_cut = slash_cut = False
        strong, weak = set(), set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func, args = node.func, node.args
                if func.attr in SPLITS and args and _is_sep(args[0]):
                    sep_cut = True
                elif func.attr in SPLITS and args and isinstance(args[0], ast.Constant) and args[0].value == "/":
                    slash_cut = True
                elif func.attr == "split" and isinstance(func.value, ast.Attribute) and func.value.attr == "path":
                    sep_cut = True  # os.path.split
            if isinstance(node, ast.Attribute) and node.attr == "parts":
                sep_cut = True
            word = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if word in STRONG:
                strong.add(word)
            elif word in WEAK:
                weak.add(word)
        if (sep_cut and (strong or weak)) or (slash_cut and strong):
            found.append(f"{fn.name} (line {fn.lineno}): {sorted(strong | weak)}")
    return found


GLOB_PATTERN = re.compile(r"\*\.[A-Za-z0-9]+")


def glob_listings(tree) -> list[str]:
    """Each glob that could list a directory's names: anything but
    glob.glob(os.path.join(glob.escape(<dir>), "*.<ext>")), the form the tools use to find their
    own files (session logs, run records, digests). iglob, Path.glob and rglob included."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name not in ("glob", "iglob", "rglob"):
            continue
        ok = (
            name == "glob"
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "glob"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Attribute)
            and node.args[0].func.attr == "join"
            and len(node.args[0].args) == 2
            and isinstance(node.args[0].args[0], ast.Call)
            and isinstance(node.args[0].args[0].func, ast.Attribute)
            and node.args[0].args[0].func.attr == "escape"
            and isinstance(node.args[0].args[1], ast.Constant)
            and isinstance(node.args[0].args[1].value, str)
            and GLOB_PATTERN.fullmatch(node.args[0].args[1].value) is not None
        )
        if not ok:
            found.append(f"{ast.unparse(node) if hasattr(ast, 'unparse') else name} (line {node.lineno})")
    return found


class ToolsImportTheLibTests(unittest.TestCase):
    # Static backstops against a copy of the helpers in a tool, built on the checks of
    # tests/test_harness_lib.py (see the notes there: each bans only a shape a copy needs; when a
    # later change has a legitimate use of a banned shape, revisit the check rather than work
    # around it). The instrumented-lib runs above are the main guard.

    def test_no_tool_computes_a_sha1(self):
        """Guards against a tool computing a state key of its own (path_key's sha1): no tool spells
        sha1 or _hashlib outside its docstrings, reaches hashlib by a computed name, or cuts a
        digest to [:12]. (harness-red's sha256 test hash is neither.)"""
        for name in TOOL_NAMES:
            tree = tool_tree(name)
            with self.subTest(tool=name):
                self.assertEqual(thl.spelled(tree, ("sha1", "_hashlib")), [])
                self.assertEqual(thl.hashlib_by_computed_name(tree), [])
                self.assertEqual(thl.state_key_slices(tree), [])

    def test_no_tool_walks_a_directory_to_match_a_spelling(self):
        """Guards against a copy of canonical_path's component walk: no def lists a directory
        (listdir, scandir) and also compares spellings (casefold, samefile)."""
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(thl.spelling_walks(tool_tree(name)), [])

    def test_no_tool_lists_a_directory_to_canonicalise(self):
        """Guards against canonical_path's component walk split across several defs: only the
        walk lists directories to match spellings, so no tool lists one (os.listdir, os.scandir,
        Path.iterdir, spelled or reached by getattr) and none uses unicodedata (the NFC fold). The one
        legitimate listing, harness-standup's latest_spec (newest file of docs/agent/specs), is left
        out of that tool's tree; a new legitimate listing means revisiting this check."""
        for name in TOOL_NAMES:
            tree = tool_tree(name)
            if name == "harness-standup":
                tree.body = [
                    node for node in tree.body
                    if not (isinstance(node, ast.FunctionDef) and node.name == "latest_spec")
                ]
            with self.subTest(tool=name):
                self.assertEqual(
                    thl.spelled(tree, ("listdir", "scandir", "iterdir", "unicodedata", "walk")), []
                )
                self.assertEqual(glob_listings(tree), [])

    def test_every_def_in_a_tool_is_live(self):
        """Guards against a copy kept as dead code beside the import (the helpers renamed, split
        across defs, never called): every def and class in a tool is reached from its module
        code. A def the refactor leaves with no caller goes with the copies."""
        self.assertEqual(
            [d.split(" (")[0] for d in dead_defs(ast.parse(CHEAT))],
            ["def _dir_names", "def _canon", "def _unused_method"],
        )
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(dead_defs(tool_tree(name)), [])

    def test_no_tool_cuts_a_path_and_matches_its_parts(self):
        """Guards against canonical_path's walk with its listing moved elsewhere: no def cuts a
        path on os.sep (or Path.parts, os.path.split) and matches a part's spelling."""
        self.assertEqual([d.split(" (")[0] for d in part_matches(ast.parse(CHEAT))], ["_canon"])
        listed = glob_listings(ast.parse(CHEAT))
        self.assertEqual(sorted(int(d.rsplit("line ", 1)[1].rstrip(")")) for d in listed), [6, 9], listed)
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(part_matches(tool_tree(name)), [])

    def test_no_tool_carries_a_renamed_copy(self):
        """Guards against a renamed copy: no def in a tool has the body (names and docstring
        aside) of canonical_path, same_entry, path_key or adopt_old_state as the lib spells it."""
        refs = thl.reference_bodies()
        self.assertEqual(sorted(set(refs.values())), sorted(thl.COMPARED), "reference bodies missing")
        for name in TOOL_NAMES:
            copies = [
                f"{fn.name} (line {fn.lineno}) = {refs[thl.normalized_body(fn)]}"
                for fn in thl.defs(tool_tree(name))
                if thl.normalized_body(fn) in refs
            ]
            with self.subTest(tool=name):
                self.assertEqual(copies, [])

    def test_no_tool_defines_any_of_the_five(self):
        """Guards against a copy under the lib's own name, which would shadow the import."""
        for name in TOOL_NAMES:
            defined = {fn.name for fn in thl.defs(tool_tree(name))}
            with self.subTest(tool=name):
                self.assertEqual(sorted(defined & set(FIVE)), [])

    def check_bound_from_lib(self, module: types.ModuleType, tool: str) -> None:
        paths = os.path.realpath(str(PATHS_PY))
        for fn in USES[tool]:
            with self.subTest(tool=tool, name=fn):
                bound = module.__dict__.get(fn)  # in the tool's own namespace
                self.assertTrue(callable(bound), f"{tool} does not bind {fn}")
                self.assertEqual(bound.__module__, "harness_lib.paths")
                self.assertEqual(os.path.realpath(inspect.getsourcefile(bound)), paths)

    def test_names_bound_from_the_lib_with_source_file_loader(self):
        for tool in TOOL_NAMES:
            self.check_bound_from_lib(thl.load_with_loader(BIN / tool), tool)

    def test_names_bound_from_the_lib_in_a_bare_module(self):
        for tool in TOOL_NAMES:
            self.check_bound_from_lib(thl.load_with_exec(BIN / tool), tool)


if __name__ == "__main__":
    unittest.main()
