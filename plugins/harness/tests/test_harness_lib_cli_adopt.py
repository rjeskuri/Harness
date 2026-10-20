"""Tests for ticket #84 (refactor T6 of docs/proposals/refactor.md): harness-standup,
harness-digest, harness-worklog and the hooks session_brief.py and tests_first.py adopt
harness_lib/cli.py and harness_lib/text.py. Companion to test_harness_lib_cli.py (#82, the lib
itself and its adoption by harness-board and harness-pr); the AST helpers come from there.

What each file takes from the lib (the names it uses today, by their lib names):
- harness-standup: Problem, Parser, run_main; home, session_folder (today `encode`), same_path,
  age_text (today `age`), first_line, parse_iso_seconds (today `parse_iso`: epoch seconds, naive
  means local time; NOT the lib's datetime `parse_iso`), read_json.
- harness-digest: the same but session_folder, plus harness_home.
- harness-worklog: Parser and run_main, and session_folder (today `encode_path`, byte-for-byte the
  lib's body: the definition of done allows no copy); it gains `from __future__ import annotations`.
- session_brief.py: read_json and one_line. Only it runs on without the lib (test_harness_lib.py),
  so a lib-less fallback inside the except branch of its harness_lib import is allowed, as in
  test_hookio.py; its budget line must survive a missing lib, as it does today.
- tests_first.py: read_json, keeping today's dict check at the call sites (a ticket cache or red
  record that is valid JSON but not an object still counts as absent: the guard denies, it never
  crashes open).

Broad first: each tool, run as a subprocess from a plugin tree whose cli.py is instrumented,
reports a bad invocation and a Problem or crash in one line, exit 1, through the lib's Parser,
Problem and run_main. Then, in process, for every lib name a file takes, a real function of the
file runs with the file's binding of that name replaced by a recorder, on inputs whose right
answer depends on it, and the recorder must fire. Last, the source: no copy of a cli.py/text.py
helper under any name (by shape and by what a body calls), the names imported from the right
lib module and used from code that runs, the run_main tail, worklog's __future__ import.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_cli_adopt.py" -v
"""

from __future__ import annotations

import ast
import calendar
import contextlib
import copy
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.dont_write_bytecode = True  # loading the lib, the tools and the hooks must not leave __pycache__

import test_harness_lib_cli as thc  # noqa: E402  (AST helpers, the tool loader, the lib loader)

PLUGIN = thc.PLUGIN
BIN = thc.BIN
LIB = thc.LIB
HOOKS = PLUGIN / "hooks"
CLI_PY = thc.CLI_PY
SYSTEM_PATH = thc.SYSTEM_PATH
UTC = dt.timezone.utc
LIB_NAMES = tuple(thc.CLI_NAMES) + tuple(thc.TEXT_NAMES)

# The names each file binds from the lib, and the lib module each comes from.
TAKES = {
    "harness-standup": {
        "Problem": "cli", "Parser": "cli", "run_main": "cli",
        "home": "text", "session_folder": "text", "same_path": "text", "age_text": "text",
        "first_line": "text", "parse_iso_seconds": "text", "read_json": "text",
    },
    "harness-digest": {
        "Problem": "cli", "Parser": "cli", "run_main": "cli",
        "home": "text", "harness_home": "text", "same_path": "text", "age_text": "text",
        "first_line": "text", "parse_iso_seconds": "text", "read_json": "text",
    },
    "harness-worklog": {"Parser": "cli", "run_main": "cli", "session_folder": "text"},
    "session_brief.py": {"read_json": "text", "one_line": "text"},
    "tests_first.py": {"read_json": "text"},
}
TOOLS = ("harness-standup", "harness-digest", "harness-worklog")
HOOK_FILES = ("session_brief.py", "tests_first.py")
FILES = TOOLS + HOOK_FILES
# Only session_brief may keep a lib-less fallback, and only inside the except branch of its
# harness_lib import (the brief must still reach the session without the lib).
FALLBACK_ALLOWED = ("session_brief.py",)
# The private names today's copies go by: none may survive under them either.
TODAY_NAMES = ("encode", "encode_path", "age")
# Names a hook may define although the lib exports them: functions that are not the lib's
# (session_brief's read_text reads at most READ_LIMIT bytes; the hooks' harness_home() takes no
# parts and returns a Path). The proposal keeps them; the ticket does not move them.
HOOK_OWN = {"read_text", "harness_home"}


def path_of(name: str) -> Path:
    return (BIN / name) if name in TOOLS else (HOOKS / name)


def tree_of(name: str):
    return ast.parse(path_of(name).read_text(encoding="utf-8"))


def load_hook(name: str) -> types.ModuleType:
    """A hook imported by path, as test_guards.py does, without running its main()."""
    path = HOOKS / name
    module = types.ModuleType("adopt_hook_" + name.replace(".", "_"))
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def load(name: str):
    return thc.load_tool(BIN / name) if name in TOOLS else load_hook(name)


def imports_harness_lib(stmt) -> bool:
    return any(isinstance(n, ast.ImportFrom) and (n.module or "").startswith("harness_lib")
               for n in ast.walk(stmt))


def fallback_ids(tree, name: str) -> set:
    """id() of every node inside the except branch of a top-level try that imports from
    harness_lib, for the one hook allowed a lib-less fallback; empty for every other file."""
    if name not in FALLBACK_ALLOWED:
        return set()
    ids = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Try) and any(imports_harness_lib(s) for s in stmt.body):
            for handler in stmt.handlers:
                ids.update(id(n) for n in ast.walk(handler))
    return ids


def without_fallback(tree, name: str):
    """`tree` with the allowed fallback's handlers emptied (to `pass`)."""
    if name not in FALLBACK_ALLOWED:
        return tree
    for stmt in tree.body:
        if isinstance(stmt, ast.Try) and any(imports_harness_lib(s) for s in stmt.body):
            for handler in stmt.handlers:
                handler.body = [ast.Pass()]
    return tree


# Today's copies, verbatim (what this ticket removes): a renamed one left behind is still a copy.
TODAY_COPIES = '''
class Problem(Exception):
    """A bad invocation or an unreadable registry (one line, exit 1), or a failed gh call (one board line)."""


class Parser(argparse.ArgumentParser):
    """argparse that reports a bad invocation in one line and exits 1."""

    def error(self, message):
        sys.exit(f"{self.prog}: error: {message}")


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def harness_home(*parts):
    return home(".claude", "harness", *parts)


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def same_path(a, b):
    return os.path.realpath(os.path.abspath(os.path.expanduser(a))) == os.path.realpath(os.path.abspath(os.path.expanduser(b)))


def age(then, now):
    s = max(0, now - then)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    if s < 86400 * 60:
        return f"{int(s // 86400)}d ago"
    if s < 86400 * 730:
        return f"{int(s // (86400 * 30))}mo ago"
    return f"{int(s // (86400 * 365))}y ago"


def first_line(text):
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def parse_iso(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        when = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.astimezone()).timestamp()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_json_path(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def one_line(value) -> str:
    return " ".join(str(value).split())
'''


def reference_defs(skip=()) -> list:
    """(origin, node) for today's copies and the lib's own defs of every cli.py/text.py export."""
    found = [("today", node) for node in ast.parse(TODAY_COPIES).body if isinstance(node, thc.DEFS)]
    for path in (thc.CLI_PY, thc.TEXT_PY):
        found += [(path.name, node) for node in ast.parse(path.read_text(encoding="utf-8")).body
                  if isinstance(node, thc.DEFS) and node.name in LIB_NAMES and node.name not in skip]
    return found


def reference_shapes(skip=()) -> dict:
    shapes = {}
    for origin, node in reference_defs(skip):
        shapes.setdefault(thc.shape(node), f"{origin}:{node.name}")
    return shapes


def reference_fingerprints(skip=()) -> dict:
    prints = {}
    for origin, node in reference_defs(skip):
        calls, consts = thc.fingerprint(node)
        if len(calls) + len(consts) >= 3:
            prints.setdefault((calls, consts), f"{origin}:{node.name}")
    return prints


def stamp_utc(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_expected(seconds: float) -> str:
    """The age buckets, from their definition (docs/design.md; the tools' tests pin them)."""
    s = max(0, seconds)
    for limit, unit, label in ((60, None, "just now"), (3600, 60, "m"), (86400, 3600, "h"),
                               (86400 * 60, 86400, "d"), (86400 * 730, 86400 * 30, "mo")):
        if s < limit:
            return label if unit is None else f"{int(s // unit)}{label} ago"
    return f"{int(s // (86400 * 365))}y ago"


def folder_name(path: str) -> str:
    """Claude Code's session folder for a project path: every character but A-Z, a-z, 0-9 is '-'."""
    return "".join(c if (c.isascii() and c.isalnum()) else "-" for c in path)


# Marks for FilesRunTheLib.spy: what a marked recorder returns in place of the lib's answer.
def tagged(tag: str):
    """A non-empty text result r becomes TAG<r> (empty stays empty, so `x or fallback` keeps its meaning)."""
    return lambda result, *args, **kwargs: f"{tag}<{result}>" if result else result


def prefixed(prefix: str):
    return lambda result, *args, **kwargs: prefix + result


def shifted(seconds: float):
    """Epoch seconds moved by `seconds` (None stays None)."""
    return lambda result, *args, **kwargs: None if result is None else result + seconds


def rooted(root: str, *base: str):
    """A home()/harness_home() that lives under `root` instead of $HOME."""
    return lambda result, *parts, **kwargs: os.path.join(root, *base, *parts)


def crafted(basename: str, value):
    """read_json that answers `value` for a file named `basename`, whatever is on disk."""
    return lambda result, path, *args, **kwargs: (copy.deepcopy(value) if os.path.basename(os.fspath(path))
                                                  == basename else result)


def also_same(a: str, b: str):
    """same_path that also holds for the pair (a, b), in either order."""
    return lambda result, x, y, *args, **kwargs: result or {os.fspath(x), os.fspath(y)} == {a, b}


def exactly_same(a: str, b: str):
    """same_path that holds for the pair (a, b) only, in either order, whatever the lib says (so a path
    compared with its own spelling is no longer the same)."""
    return lambda result, x, y, *args, **kwargs: {os.fspath(x), os.fspath(y)} == {a, b}


# ISO-8601 texts and their epoch seconds, derived by hand: Z, offsets east and west, padding, a space for the
# T, fractional seconds, and naive local times (whose epoch is the local clock's, as time.mktime gives it).
ISO_CASES = [
    ("2025-10-09T08:53:20Z", calendar.timegm((2025, 10, 9, 8, 53, 20))),
    ("2025-10-01T00:00:00+02:00", calendar.timegm((2025, 9, 30, 22, 0, 0))),
    (" 2024-01-01T00:00:00Z ", calendar.timegm((2024, 1, 1, 0, 0, 0))),
    ("2025-06-15T12:30:00", time.mktime((2025, 6, 15, 12, 30, 0, 0, 0, -1))),
    ("2025-06-15T12:30:00.500000+05:30", calendar.timegm((2025, 6, 15, 7, 0, 0)) + 0.5),
    ("2023-02-28 23:59:59-07:00", calendar.timegm((2023, 3, 1, 6, 59, 59))),
    ("2024-12-31T23:00:00", time.mktime((2024, 12, 31, 23, 0, 0, 0, 0, -1))),
]
# Project paths for the session-folder tests: punctuation, a space, a dash, non-ASCII, and paths of letters and
# slashes only (whose folder is just the slashes turned to dashes).
SESSION_PATHS = ("/Users/me/My Project.v2", "/tmp/a_b-c", "/Users/me/café", "/x", "/Users/me/app", "/w/my-repo",
                 "/w/rx")
# Repos for worklog's transcripts_dirs: punctuation and a space, an underscore, plain letters and slashes, a dash.
WORKLOG_REPOS = ("/Users/me/Client Work/app.v2", "/w/r_x", "/w/rx", "/Users/me/app", "/w/my-repo")
# Definition-of-done items and their brief lines: messy whitespace, an int, and clean single-spaced strings
# (where collapsing whitespace changes nothing), with and without a checkbox or a bullet.
DOD_ITEMS = (("[ ] Do  the\nthing", "- [ ] Do the thing"), ("  - [x]   shipped ", "- [x] shipped"), (42, "- 42"),
             ("plain\ttext", "- plain text"), ("Ship it", "- Ship it"), ("[x] done", "- [x] done"),
             ("- [ ] Write the tests", "- [ ] Write the tests"), ("one", "- one"))
# Clocks and ages for the age tests: whole and fractional seconds, an int and a float now, every bucket.
AGE_NOWS = (1_760_000_000, 1_761_234_567.75, 1_700_000_123.5)
AGE_SECONDS = (0, 0.5, 59, 59.9, 61, 3599, 7300.25, 86399, 86400 * 3 + 5, 86400 * 61, 86400 * 800, 86400 * 400.5)


def spy_parser(result, *args, **kwargs):
    """A Parser built from the same arguments whose error says `SPY <prog>: <message>` (subparsers inherit it)."""
    def error(self, message):
        sys.exit(f"SPY {self.prog}: {message}")

    return type("SpyParser", (type(result),), {"error": error})(*args, **kwargs)


# ================================================================ through the lib, end to end


INSTRUMENT = '''

# ---- instrumentation added by test_harness_lib_cli_adopt.py: records which lib objects ran
import os as _adopt_os


def _adopt_mark(what):
    path = _adopt_os.environ.get("HARNESS_CLI_MARK")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(what + "\\n")


_adopt_error = Parser.error


def _adopt_parser_error(self, message):
    _adopt_mark("Parser.error " + self.prog)
    return _adopt_error(self, message)


Parser.error = _adopt_parser_error
_adopt_problem_init = Problem.__init__


def _adopt_problem(self, *args):
    _adopt_mark("Problem")
    _adopt_problem_init(self, *args)


Problem.__init__ = _adopt_problem
_adopt_run_main = run_main


def run_main(prog, main):
    _adopt_mark("run_main " + prog)
    return _adopt_run_main(prog, main)
'''


class ToolsFailThroughTheLib(unittest.TestCase):
    """The three bin tools, copied with lib/ into a temporary plugin whose cli.py records each use of
    its Parser, Problem and run_main: a bad invocation is one line `<prog>: error: ...` exit 1, a
    Problem `<tool>: <message>` exit 1 and a crash `<tool>: <Type>: <message>` exit 1, exactly as
    today, and the lib's objects are the ones that ran."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-plugin-")))
        (cls.root / "bin").mkdir()
        for tool in TOOLS:
            shutil.copy2(BIN / tool, cls.root / "bin" / tool)
        shutil.copytree(LIB, cls.root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        cli = cls.root / "lib" / "harness_lib" / "cli.py"
        cli.write_text(cli.read_text(encoding="utf-8") + INSTRUMENT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, True)

    def setUp(self) -> None:
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.marks = self.tmp / "marks.txt"
        self.env = {
            "PATH": SYSTEM_PATH, "HOME": str(self.home), "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "GIT_CEILING_DIRECTORIES": str(self.tmp), "HARNESS_CLI_MARK": str(self.marks),
            "HARNESS_GH": str(self.tmp / "no-gh"), "TMPDIR": str(self.tmp),
        }

    def run_tool(self, tool: str, *argv: str) -> subprocess.CompletedProcess:
        if self.marks.exists():
            self.marks.unlink()
        return subprocess.run([sys.executable, str(self.root / "bin" / tool), *argv], capture_output=True,
                              text=True, env=self.env, cwd=str(self.tmp), timeout=60)

    def marked(self) -> list:
        return self.marks.read_text(encoding="utf-8").splitlines() if self.marks.exists() else []

    def assertOneLine(self, p, line: str, prefix: bool = False) -> None:
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.splitlines()), 1, p.stderr)
        if prefix:
            self.assertTrue(p.stderr.startswith(line), p.stderr)
        else:
            self.assertEqual(p.stderr, line + "\n")

    def registry(self, text: str) -> str:
        path = self.home / ".claude" / "harness" / "projects.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_bad_invocation_is_one_line_exit_1_through_the_lib_parser_and_run_main(self):
        cases = [
            ("harness-standup", ("--bogus",), "harness-standup: error: unrecognized arguments: --bogus",
             "harness-standup", False),
            ("harness-standup", ("a", "b"), "harness-standup: error: unrecognized arguments: b",
             "harness-standup", False),
            ("harness-digest", ("--install-launchd", "--uninstall-launchd"),
             "harness-digest: error: argument --uninstall-launchd: not allowed with argument --install-launchd",
             "harness-digest", False),
            ("harness-digest", ("--frobnicate",), "harness-digest: error: unrecognized arguments: --frobnicate",
             "harness-digest", False),
            ("harness-worklog", ("export",),
             "harness-worklog export: error: the following arguments are required: --month",
             "harness-worklog export", False),
            ("harness-worklog", ("nope",), "harness-worklog: error: argument cmd: invalid choice: 'nope'",
             "harness-worklog", True),
            ("harness-worklog", ("describe", "--date", "29/09/2026", "--text", "x"),
             "harness-worklog describe: error: argument --date: '29/09/2026': give a date like 2026-09-28",
             "harness-worklog describe", False),
        ]
        for tool, argv, line, prog, prefix in cases:
            with self.subTest(tool=tool, argv=argv):
                p = self.run_tool(tool, *argv)
                self.assertOneLine(p, line, prefix)
                self.assertEqual(p.stdout, "")
                marks = self.marked()
                self.assertIn(f"Parser.error {prog}", marks, f"{tool}'s parser is not the lib's Parser")
                self.assertIn(f"run_main {tool}", marks, f"{tool} does not end in the lib's run_main")

    def test_a_problem_is_one_line_exit_1_raised_as_the_lib_problem(self):
        for tool in ("harness-standup", "harness-digest"):
            for text in ("{not json", "", "[1, 2"):
                with self.subTest(tool=tool, registry=text):
                    path = self.registry(text)
                    try:
                        json.loads(text)
                    except ValueError as err:
                        why = str(err)
                    p = self.run_tool(tool)
                    self.assertOneLine(p, f"{tool}: {path} is not valid JSON ({why}); fix it or remove it")
                    marks = self.marked()
                    self.assertIn("Problem", marks, f"{tool} raises a Problem that is not the lib's")
                    self.assertIn(f"run_main {tool}", marks)
            with self.subTest(tool=tool, registry="version 2"):
                path = self.registry('{"version": 2, "projects": []}')
                p = self.run_tool(tool)
                self.assertOneLine(p, f"{tool}: {path} is version 2; this tool reads version 1")
                self.assertIn("Problem", self.marked())

    def test_standup_names_an_unknown_project_through_the_lib_problem(self):
        self.registry(json.dumps({"version": 1, "projects": [{"name": "alpha", "path": str(self.tmp)}]}))
        for key in ("beta", "no such/place"):
            with self.subTest(key=key):
                p = self.run_tool("harness-standup", key)
                self.assertOneLine(p, f"harness-standup: no registered project named or at {key!r} "
                                      "(harness-projects list shows them)")
                self.assertIn("Problem", self.marked())
                self.assertIn("run_main harness-standup", self.marked())

    def test_a_crash_is_one_line_with_its_type_exit_1_through_the_lib_run_main(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        for month, name in (("2026-09", "a.csv"), ("2025-12", "b.csv")):
            out = str(self.tmp / "no" / "such" / month / name)
            with self.subTest(out=out):
                p = self.run_tool("harness-worklog", "--repo", str(repo), "export", "--month", month, "--out", out)
                self.assertOneLine(p, f"harness-worklog: FileNotFoundError: [Errno 2] No such file or directory: {out!r}")
                self.assertIn("run_main harness-worklog", self.marked())


# ================================================================ the files run the lib's code


class FilesRunTheLib(unittest.TestCase):
    """In a freshly loaded tool or hook, every global bound to a lib function is replaced by a recorder that
    delegates to it; then a function of the file that needs that helper runs on inputs whose answer depends on
    it, and the recorder must fire. A private copy under another name leaves the recorder silent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = thc.lib_module("cli")
        cls.text = thc.lib_module("text")
        cls.paths = thc.lib_module("paths")
        # #90 and #91 moved the registry (Project, load_registry, find_project) and GhBatch into these lib
        # modules: the tools bind them, and their text helpers (same_path, harness_home, first_line) are looked
        # up there. These modules are shared by every load, so a spy on them is undone (see spy()).
        cls.registry = thc.lib_module("registry")
        cls.ghlib = thc.lib_module("gh")

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home, "GIT_CEILING_DIRECTORIES": self.tmp,
                                               "LC_ALL": "C"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list = []

    # ---- helpers

    def bound_to(self, module, label: str, where: str) -> tuple:
        """(the lib's object `label`, the module's global names bound to it)."""
        lib_obj = getattr(self.cli if where == "cli" else self.text, label)
        return lib_obj, [name for name, value in vars(module).items() if value is lib_obj]

    def spy(self, module, label: str, where: str = "text", mark=None):
        """Replace every name the module binds to the lib's `label` by a recorder that runs the lib's code.
        With `mark`, the recorder returns mark(lib result, *args, **kwargs) instead of the lib's result: a
        value the lib would not give, which the function under test must pass on (calling the lib and
        discarding its answer for a private recomputation leaves the mark out of the output).
        Returns a function that puts the lib's object back; it also runs at cleanup (a shared lib module such
        as registry or gh must not keep a recorder past the test)."""
        lib_obj, bound = self.bound_to(module, label, where)
        self.assertTrue(bound, f"{module.__name__} binds no name to the lib's {label}")
        calls = self.calls

        def recorder(*args, **kwargs):
            calls.append((label, args, kwargs))
            result = lib_obj(*args, **kwargs)
            return result if mark is None else mark(result, *args, **kwargs)

        for name in bound:
            setattr(module, name, recorder)

        def restore():
            for name in bound:
                setattr(module, name, lib_obj)

        self.addCleanup(restore)
        return restore

    def run_main(self, module, argv: list):
        """module.main() with `argv`, its output swallowed."""
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def github_repo(self) -> str:
        """A git repository under the temp folder whose origin is on github.com (so the board is read)."""
        repo = os.path.join(self.tmp, "ghrepo")
        os.makedirs(repo, exist_ok=True)
        for args in (["init", "-q"], ["remote", "add", "origin", "https://github.com/owner/ghrepo.git"]):
            subprocess.run(["git", "-C", repo] + args, check=True, capture_output=True)
        return repo

    def given(self, label: str) -> list:
        return [args[0] if args else None for name, args, _ in self.calls if name == label]

    def put(self, rel: str, text: str) -> str:
        path = os.path.join(self.home, rel) if not os.path.isabs(rel) else rel
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def fake_exe(self, name: str, script: str) -> str:
        path = os.path.join(self.tmp, "fakebin", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + textwrap.dedent(script))
        os.chmod(path, 0o755)
        return path

    def problem(self, call, *args):
        """The exception `call(*args)` raises, which must be the lib's Problem."""
        with self.assertRaises(Exception) as ctx:
            call(*args)
        self.assertIsInstance(ctx.exception, self.cli.Problem, f"{ctx.exception!r} is not the lib's Problem")
        return ctx.exception

    def bad_invocation(self, tool: str, argv: list, line: str) -> None:
        """main() with `argv` exits through the lib's Parser with `line`; and when the lib's Parser is swapped
        for one whose error says `SPY <prog>: <message>`, that is what main() exits with (the parser that
        parses is the one the lib's Parser built, not a private one)."""
        prog, message = line.split(": error: ", 1)
        for mark, want in ((None, line), (spy_parser, f"SPY {prog}: {message}")):
            with self.subTest(tool=tool, marked=mark is not None):
                module = thc.load_tool(BIN / tool)
                self.calls.clear()
                self.spy(module, "Parser", "cli", mark)
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        module.main()
                self.assertEqual(ctx.exception.code, want)
                progs = [kwargs.get("prog", args[0] if args else None) for name, args, kwargs in self.calls
                         if name == "Parser"]
                self.assertEqual(progs[:1], [argv[0]], self.calls)

    def budgets(self, now: float) -> tuple:
        """budgets.json with entries ahead, behind and unreadable, in Z, offset and naive local spellings, with
        reasons messy and clean; (path, the (name, until, reason) ahead in file order). self.late holds what is
        ahead once parse_iso_seconds runs an hour late, in file order."""
        ahead = stamp_utc(now + 7200)
        naive_ahead = dt.datetime.fromtimestamp(now + 3 * 86400).strftime("%Y-%m-%dT%H:%M:%S")  # local
        clean = dt.datetime.fromtimestamp(now + 5400, dt.timezone(dt.timedelta(hours=-4))).isoformat()
        east = dt.datetime.fromtimestamp(now - 600, dt.timezone(dt.timedelta(hours=2))).isoformat()
        lagging = dt.datetime.fromtimestamp(now - 900).strftime("%Y-%m-%dT%H:%M:%S")  # local
        data = {
            "api": {"exhausted_until": ahead, "reason": "credit\n  balance"},
            "cloud": {"exhausted_until": stamp_utc(now - 60), "reason": "old"},
            "local": {"exhausted_until": " " + naive_ahead + " ", "reason": ""},
            "clean": {"exhausted_until": clean, "reason": "quota"},
            "junk": {"exhausted_until": "soon"},
            "none": "not a dict",
            "east": {"exhausted_until": east, "reason": "east"},
            "lagging": {"exhausted_until": lagging, "reason": "naive"},
        }
        path = self.put(os.path.join(".claude", "harness", "budgets.json"), json.dumps(data))
        self.late = [("api", ahead, "credit balance"), ("cloud", stamp_utc(now - 60), "old"),
                     ("local", naive_ahead, ""), ("clean", clean, "quota"), ("east", east, "east"),
                     ("lagging", lagging, "naive")]
        return path, [("api", ahead, "credit balance"), ("local", naive_ahead, ""), ("clean", clean, "quota")]

    # ================================================== harness-standup

    def test_standup_raises_the_lib_problem_and_parses_with_the_lib_parser(self):
        standup = thc.load_tool(BIN / "harness-standup")
        # The registry is the lib's (#91): the tool binds registry.py's own objects, no copies.
        self.assertIs(standup.find_project, self.registry.find_project)
        self.assertIs(standup.load_registry, self.registry.load_registry)
        registry = os.path.join(".claude", "harness", "projects.json")
        self.put(registry, json.dumps({"version": 1, "projects": [{"name": "alpha", "path": self.tmp},
                                                                  {"name": "Alpha", "path": self.home}]}))
        for key, message in (("gamma", "no registered project named or at 'gamma' (harness-projects list shows "
                                       "them)"),
                             ("ALPHA", "'ALPHA' matches 2 projects; give its path instead")):
            with self.subTest(key=key):
                err = self.problem(self.run_main, standup, ["harness-standup", key])
                self.assertEqual(str(err), message)
        self.put(registry, '{"version": 1}')
        self.assertRegex(str(self.problem(self.run_main, standup, ["harness-standup"])),
                         r'projects\.json: expected \{"version": 1, ')
        self.bad_invocation("harness-standup", ["harness-standup", "--nope"],
                            "harness-standup: error: unrecognized arguments: --nope")

    def test_standup_reads_the_registry_and_transcripts_under_home_through_the_lib(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.assertIs(standup.load_registry, self.registry.load_registry)
        self.spy(standup, "home")
        # The registry file is found by registry.py (#91), through the lib's harness_home it binds.
        self.spy(self.registry, "harness_home")
        self.put(os.path.join(".claude", "harness", "projects.json"), json.dumps(
            {"version": 1, "projects": [{"name": "w", "path": "/w/x", "billable": True}, {"path": "/y/z/"}]}))
        got = standup.load_registry()
        self.assertEqual([(p.name, p.path, p.billable) for p in got],
                         [("w", "/w/x", True), ("z", "/y/z/", False)])
        self.assertGreaterEqual(len(self.given("harness_home")), 1)
        # and the transcripts under the same HOME through the tool's own home()
        folder = os.path.join(self.home, ".claude", "projects", folder_name("/w/x"))
        self.touch(os.path.join(folder, "a.jsonl"), 1_700_000_000)
        self.assertEqual(standup.last_session("/w/x"), 1_700_000_000)
        self.assertGreaterEqual(len(self.given("home")), 1)

    def test_standup_finds_a_projects_sessions_through_the_lib_session_folder(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.spy(standup, "session_folder")
        self.spy(standup, "home")
        stamps = {}
        for i, path in enumerate(SESSION_PATHS):
            folder = os.path.join(self.home, ".claude", "projects", folder_name(path))
            when = 1_700_000_000 + 1000 * i
            for j, name in enumerate(("a.jsonl", "b.jsonl")):
                f = self.put(os.path.join(folder, name), "{}\n")
                os.utime(f, (when - 500 * j, when - 500 * j))
            stamps[path] = when
        for path, when in stamps.items():
            with self.subTest(path=path):
                self.assertEqual(standup.last_session(path), when)
        self.assertIsNone(standup.last_session("/never/opened"))
        self.assertEqual(self.given("session_folder"), list(stamps) + ["/never/opened"])
        self.assertGreaterEqual(len(self.given("home")), 5)

    def test_standup_picks_a_project_by_path_through_the_lib_same_path(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.assertIs(standup.find_project, self.registry.find_project)
        # find_project (#91) looks same_path up in registry.py: the lib's text.same_path, no copy.
        self.spy(self.registry, "same_path")
        a = os.path.join(self.tmp, "alpha")
        b = os.path.join(self.tmp, "beta")
        os.makedirs(a)
        os.makedirs(b)
        os.symlink(b, os.path.join(self.tmp, "link-to-beta"))
        projects = [self.registry.Project(path=a, name="one"), self.registry.Project(path=b, name="two")]
        for key, name in ((os.path.join(self.tmp, "beta", "..", "alpha"), "one"),
                          (os.path.join(self.tmp, "link-to-beta"), "two"),
                          (b + "/", "two")):
            with self.subTest(key=key):
                self.assertEqual(standup.find_project(projects, key).name, name)
        self.assertGreaterEqual(len(self.given("same_path")), 3)

    def test_standup_ages_through_the_lib_age_text(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.spy(standup, "age_text")
        count = 0
        for now in AGE_NOWS:
            for seconds in AGE_SECONDS:
                with self.subTest(now=now, seconds=seconds):
                    then = now - seconds
                    got = standup.stamp(then, now)
                    self.assertEqual(got["age"], age_expected(now - then))
                    count += 1
        self.assertEqual(len(self.given("age_text")), count)

    def test_standup_reads_budgets_through_the_lib_read_json_and_parse_iso_seconds(self):
        standup = thc.load_tool(BIN / "harness-standup")
        for name in ("read_json", "parse_iso_seconds"):
            self.spy(standup, name)
        now = time.time()
        path, ahead = self.budgets(now)
        got = standup.exhausted_budgets(now)
        self.assertEqual([(b["name"], b["until"], b["reason"]) for b in got], ahead)
        self.assertIn(path, [str(p) for p in self.given("read_json")])
        self.assertGreaterEqual(len(self.given("parse_iso_seconds")), 4)

    def test_standup_reads_run_records_through_the_lib_read_json(self):
        standup = thc.load_tool(BIN / "harness-standup")
        for name in ("read_json", "parse_iso_seconds", "age_text"):
            self.spy(standup, name)
        now = time.time()
        self.runs(now)
        got = standup.recent_runs([], now)
        self.assertEqual([(r["id"], r["project"], r["state"], r["age"]) for r in got], self.RUNS_SEEN)
        self.assertEqual(len(self.given("read_json")), 4)
        self.assertGreaterEqual(len(self.given("parse_iso_seconds")), 6)

    RUNS_SEEN = [("r1", "proj", "error", "1h ago"), ("r3", "naive", "ok", "2h ago"),
                 ("r4", "offset", "failed", "3h ago")]

    def runs(self, now: float) -> None:
        """Four run records: Z stamps, an unreadable one, naive local stamps, and offset stamps with fractions."""
        runs = os.path.join(self.home, ".claude", "harness", "runs")

        def local(seconds):
            return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S")

        def zone(seconds, hours):
            return dt.datetime.fromtimestamp(seconds, dt.timezone(dt.timedelta(hours=hours))).isoformat()

        self.put(os.path.join(runs, "aaaaaaaaaaaa-1.json"), json.dumps(
            {"id": "r1", "repo": "/somewhere/proj", "outcome": "error", "exit": 3,
             "started_at": stamp_utc(now - 4000), "ended_at": stamp_utc(now - 3600), "log": "/l/1"}))
        self.put(os.path.join(runs, "bbbbbbbbbbbb-2.json"), "[1, 2]")
        self.put(os.path.join(runs, "cccccccccccc-3.json"), json.dumps(
            {"id": "r3", "repo": "/x/naive", "outcome": "ok", "started_at": local(now - 9000),
             "ended_at": local(now - 7200)}))
        self.put(os.path.join(runs, "dddddddddddd-4.json"), json.dumps(
            {"id": "r4", "repo": "/y/offset", "outcome": "failed", "started_at": zone(now - 12000, 5.5),
             "ended_at": zone(now - 11000, -3)}))   # well inside its hour: microseconds round either way

    def test_standup_names_gh_failures_by_their_first_line_through_the_lib(self):
        standup = thc.load_tool(BIN / "harness-standup")
        # GhBatch is gh.py's (#90), and it names a failure by gh.py's binding of the lib's first_line.
        self.assertIs(standup.GhBatch, self.ghlib.GhBatch)
        self.spy(self.ghlib, "first_line")
        project = self.registry.Project(path=self.github_repo(), name="ghrepo")
        cases = [
            ("printf '\\n   HTTP 502: bad gateway  \\nretry later\\n' >&2; exit 1\n", "gh issue list failed: HTTP 502: bad gateway"),
            ("exit 4\n", "gh issue list failed: exit 4"),
            ("printf '  \\n<html>oops</html>\\n'; exit 0\n", "gh issue list printed something other than JSON: <html>oops</html>"),
            ("printf 'HTTP 404: Not Found' >&2; exit 1\n", "gh issue list failed: HTTP 404: Not Found"),
            ("printf 'not json\\n'; exit 0\n", "gh issue list printed something other than JSON: not json"),
        ]
        for i, (script, message) in enumerate(cases):
            with self.subTest(message=message):
                gh = self.fake_exe(f"gh{i}", script)
                with mock.patch.dict(os.environ, {"HARNESS_GH": gh}):
                    got = standup.board_report(project, time.time())
                # The lib's Problem, as the board's error line (any other exception is prefixed by its type).
                self.assertEqual(got["board_error"], message)
                self.assertIsNone(got["board"])
        self.assertGreaterEqual(len(self.given("first_line")), len(cases))

    # ================================================== harness-digest

    def test_digest_raises_the_lib_problem_and_parses_with_the_lib_parser(self):
        digest = thc.load_tool(BIN / "harness-digest")
        for text in ("{oops", '{"projects": {}}', '{"version": 9, "projects": []}'):
            with self.subTest(registry=text):
                self.put(os.path.join(".claude", "harness", "projects.json"), text)
                self.assertIn(os.path.join(self.home, ".claude", "harness", "projects.json"),
                              str(self.problem(digest.load_projects)))
        self.bad_invocation("harness-digest", ["harness-digest", "--json", "extra"],
                            "harness-digest: error: unrecognized arguments: extra")

    def test_digest_finds_its_files_under_home_through_the_lib(self):
        digest = thc.load_tool(BIN / "harness-digest")
        self.spy(digest, "home")
        self.spy(digest, "harness_home")
        self.assertEqual(digest.plist_path(), os.path.join(self.home, "Library", "LaunchAgents",
                                                           "com.ryaneskuri.harness-digest.plist"))
        self.assertGreaterEqual(len(self.given("home")), 1)
        # The registry is read by registry.py (#91), which finds it through its binding of the lib's
        # harness_home; digest's own harness_home serves its other files (budgets, runs, the digest folder).
        self.assertIs(digest.load_registry, self.registry.load_registry)
        self.calls.clear()
        self.spy(self.registry, "harness_home")
        self.put(os.path.join(".claude", "harness", "projects.json"), json.dumps(
            {"version": 1, "projects": [{"name": "n", "path": "/p/q"}, {"path": "/r/s/"}, {"path": " "}]}))
        self.assertEqual(digest.load_projects(), [{"name": "n", "path": "/p/q"}, {"name": "s", "path": "/r/s/"}])
        self.assertGreaterEqual(len(self.given("harness_home")), 1)

    def test_digest_names_launchctl_failures_by_their_first_line_through_the_lib(self):
        digest = thc.load_tool(BIN / "harness-digest")
        self.spy(digest, "first_line")
        cases = [
            ("printf '\\n  Boot-out failed: 3: No such process  \\nmore\\n' >&2; exit 3\n",
             (3, "Boot-out failed: 3: No such process")),
            ("printf '\\n\\n loaded ok\\n'; exit 0\n", (0, "loaded ok")),
            ("exit 5\n", (5, "exit 5")),
            ("printf 'Bootstrap failed: 5: Input/output error' >&2; exit 5\n",
             (5, "Bootstrap failed: 5: Input/output error")),
            ("printf 'ok\\n'; exit 0\n", (0, "ok")),
        ]
        for i, (script, want) in enumerate(cases):
            with self.subTest(want=want):
                exe = self.fake_exe(f"launchctl{i}", script)
                with mock.patch.dict(os.environ, {"HARNESS_LAUNCHCTL": exe}):
                    self.assertEqual(digest.launchctl(["bootout", "gui/1/x"]), want)
        self.assertGreaterEqual(len(self.given("first_line")), len(cases))

    def test_digest_matches_a_runs_repo_through_the_lib_same_path(self):
        digest = thc.load_tool(BIN / "harness-digest")
        self.spy(digest, "same_path")
        a = os.path.join(self.tmp, "proj-a")
        os.makedirs(a)
        os.symlink(a, os.path.join(self.tmp, "alias"))
        projects = [{"name": "Alpha", "path": a}]
        for repo, want in ((os.path.join(self.tmp, "alias"), "Alpha"), (a + "/.", "Alpha"),
                           ("/elsewhere/other-proj", "other-proj")):
            with self.subTest(repo=repo):
                got = digest.run_project({"repo": repo}, "/runs/zzzzzzzzzzzz-1.json", projects)
                self.assertEqual(got, want)
        self.assertGreaterEqual(len(self.given("same_path")), 3)

    def test_digest_ages_items_through_the_lib_parse_iso_seconds_and_age_text(self):
        digest = thc.load_tool(BIN / "harness-digest")
        self.spy(digest, "parse_iso_seconds")
        self.spy(digest, "age_text")
        now = 1_760_000_000.5
        for text, epoch in ISO_CASES:
            with self.subTest(text=text):
                created, when = digest.opened({"createdAt": text}, now)
                self.assertEqual(created, epoch)
                self.assertEqual(when, age_expected(now - epoch))
        for bad in (None, "", "yesterday", 5):
            with self.subTest(bad=bad):
                self.assertEqual(digest.opened({"createdAt": bad}, now), (None, "at an unknown time"))
        self.assertEqual(self.given("parse_iso_seconds"), [t for t, _ in ISO_CASES] + [None, "", "yesterday", 5])
        self.assertEqual(len(self.given("age_text")), len(ISO_CASES))

    def test_digest_reads_budgets_through_the_lib_read_json(self):
        digest = thc.load_tool(BIN / "harness-digest")
        for name in ("read_json", "parse_iso_seconds", "harness_home"):
            self.spy(digest, name)
        now = time.time()
        path, ahead = self.budgets(now)
        got = digest.exhausted_budgets(now)
        self.assertEqual([(b["name"], b["until"], b["reason"]) for b in got], ahead)
        self.assertIn(path, [str(p) for p in self.given("read_json")])

    # ================================================== harness-worklog

    def test_worklog_parses_with_the_lib_parser(self):
        self.bad_invocation("harness-worklog",["harness-worklog", "add", "--date", "2026-09-29"],
                            "harness-worklog add: error: the following arguments are required: --hours, "
                            "--description")

    def test_worklog_finds_the_session_folders_through_the_lib_session_folder(self):
        worklog = thc.load_tool(BIN / "harness-worklog")
        self.spy(worklog, "session_folder")
        projects = os.path.join(self.home, ".claude", "projects")
        for repo in WORKLOG_REPOS:
            with self.subTest(repo=repo):
                self.calls.clear()
                own = folder_name(repo)
                wt = folder_name(os.path.join(repo, ".claude", "worktrees")) + "-"
                for name in (wt + "t-1-a", wt + "t-2-b", own + "-other"):
                    os.makedirs(os.path.join(projects, name), exist_ok=True)
                got = worklog.transcripts_dirs(repo)
                self.assertEqual(got, [os.path.join(projects, own), os.path.join(projects, wt + "t-1-a"),
                                       os.path.join(projects, wt + "t-2-b")])
                # One lib call for the repo's own folder and one for the worktrees prefix, nothing private.
                self.assertEqual(sorted(self.given("session_folder")),
                                 sorted([repo, repo + "/.claude/worktrees"]))

    # ================================================== session_brief.py

    def test_session_brief_reads_budgets_through_the_lib_read_json_and_one_line(self):
        brief = load_hook("session_brief.py")
        self.spy(brief, "read_json")
        self.spy(brief, "one_line")
        now = time.time()
        path, ahead = self.budgets(now)
        want = [f"Budget: {n} exhausted until {u.strip()}" + (f" ({r})" if r else "") for n, u, r in ahead]
        self.assertEqual(brief.budget_part(), want)
        self.assertEqual([str(p) for p in self.given("read_json")], [path])
        self.assertIn("credit\n  balance", self.given("one_line"))

    def test_session_brief_writes_done_items_through_the_lib_one_line(self):
        brief = load_hook("session_brief.py")
        self.spy(brief, "one_line")
        for item, want in DOD_ITEMS:
            with self.subTest(item=item):
                self.assertEqual(brief.dod_line(item), want)
        self.assertEqual(self.given("one_line"), [item for item, _ in DOD_ITEMS])

    # ================================================== tests_first.py

    def test_tests_first_reads_the_state_through_the_lib_read_json_keeping_the_dict_check(self):
        """A ticket cache or red record that parses but is not an object counts as absent, as today: the
        guard still refuses with the right reason and never crashes open."""
        main = self.paths.canonical_path(os.path.join(self.tmp, "main"))
        os.makedirs(main, exist_ok=True)
        state = os.path.join(self.home, ".claude", "harness", "repos", self.paths.path_key(main))
        green = {"kind": "green", "test_files": ["t.py"], "test_hash": "0"}
        for number, branch in (("12", "t/12-fix-login"), ("907", "t/907-x")):
            no_red = f"Tests first on t/{number}: write the tests, then run harness-red check. (No red run recorded.)"
            no_cache = f" There is no ticket cache either; harness-work start {number} writes it."
            green_msg = (f"The red run on t/{number} passed before any source change, so it proves nothing: make "
                         "the tests fail against the current code, then run harness-red check again.")
            cases = [
                (None, None, no_red + no_cache),
                ('["no-tests"]', None, no_red + no_cache),
                ('"no-tests"', None, no_red + no_cache),
                ("null", None, no_red + no_cache),
                ("{}", None, no_red),
                (json.dumps({"flags": ["no-tests"]}), None, None),
                (json.dumps({"type": "task"}), "[1, 2]", no_red),
                (json.dumps({"type": "refactor"}), "7", no_red),
                ('["no-tests"]', json.dumps(green), green_msg),
                (json.dumps({"labels": ["no-tests"]}), "[]", None),
            ]
            for ticket, red, want in cases:
                with self.subTest(number=number, ticket=ticket, red=red):
                    shutil.rmtree(state, True)
                    if ticket is not None:
                        self.put(os.path.join(state, "tickets", f"{number}.json"), ticket)
                    if red is not None:
                        self.put(os.path.join(state, "red", branch.replace("/", "-") + ".json"), red)
                    hook = load_hook("tests_first.py")
                    self.spy(hook, "read_json")
                    facts = {"main": main, "spelled": (), "branch": branch, "top": Path(self.tmp)}
                    self.assertEqual(hook.source_refusal(facts, number), want)
                    read = [str(p) for p in self.given("read_json")]
                    self.assertIn(os.path.join(state, "tickets", f"{number}.json"), read)
                    self.calls.clear()

    # ================================================== marked: the lib's answer is the answer used
    # Each test below swaps a lib helper for a recorder whose answer differs from the lib's (see the marks
    # above spy_parser) and checks that the file's result moves with it. A file that calls the lib and then
    # recomputes the value privately gives the lib's answer and fails here.

    def touch(self, path: str, when: float) -> None:
        self.put(path, "{}\n")
        os.utime(path, (when, when))

    def test_marked_standup_reads_registry_and_transcripts_where_the_lib_home_points(self):
        alt = os.path.join(self.tmp, "spyhome")
        standup = thc.load_tool(BIN / "harness-standup")
        self.spy(standup, "home", mark=rooted(alt))
        # The registry's place is registry.py's answer (#91), through its binding of the lib's harness_home.
        self.spy(self.registry, "harness_home", mark=rooted(alt, ".claude", "harness"))
        self.put(os.path.join(".claude", "harness", "projects.json"),
                 json.dumps({"version": 1, "projects": [{"name": "real", "path": "/real/one"}]}))
        self.put(os.path.join(alt, ".claude", "harness", "projects.json"),
                 json.dumps({"version": 1, "projects": [{"name": "spy", "path": "/s/p"}, {"path": "/t/u"}]}))
        self.assertEqual([(p.name, p.path) for p in standup.load_registry()], [("spy", "/s/p"), ("u", "/t/u")])
        for path, real, spied in (("/w/q", 1_600_000_000, 1_650_000_000), ("/a b/c", 1_690_000_000, 1_610_000_000),
                                  ("/plain", 1_620_000_000, 1_630_000_000)):
            with self.subTest(path=path):
                self.touch(os.path.join(self.home, ".claude", "projects", folder_name(path), "a.jsonl"), real)
                self.touch(os.path.join(alt, ".claude", "projects", folder_name(path), "a.jsonl"), spied)
                self.assertEqual(standup.last_session(path), spied)

    def test_marked_standup_finds_sessions_in_the_folder_the_lib_session_folder_names(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.spy(standup, "session_folder", mark=prefixed("SF-"))
        projects = os.path.join(self.home, ".claude", "projects")
        for i, path in enumerate(SESSION_PATHS):
            spied = 1_700_000_000 + 1000 * i
            with self.subTest(path=path):
                self.touch(os.path.join(projects, "SF-" + folder_name(path), "a.jsonl"), spied)
                self.touch(os.path.join(projects, folder_name(path), "b.jsonl"), spied + 50_000)
                self.assertEqual(standup.last_session(path), spied)
        self.touch(os.path.join(projects, folder_name("/only/plain"), "a.jsonl"), 1_700_000_000)
        self.assertIsNone(standup.last_session("/only/plain"))

    def test_marked_standup_picks_the_project_the_lib_same_path_matches(self):
        a = os.path.join(self.tmp, "alpha")
        b = os.path.join(self.tmp, "beta")
        os.makedirs(a)
        os.makedirs(b)
        projects = [self.registry.Project(path=a, name="one"), self.registry.Project(path=b, name="two")]
        # find_project (#91) asks registry.py's binding of the lib's same_path; the tool picks through it.
        for key, path, name in (("@spy-key", b, "two"), ("@spy-key", a, "one"), ("other key", b, "two")):
            with self.subTest(key=key, name=name):
                standup = thc.load_tool(BIN / "harness-standup")
                restore = self.spy(self.registry, "same_path", mark=also_same(path, key))
                try:
                    self.assertEqual(standup.find_project(projects, key).name, name)
                finally:
                    restore()
        # A key that is an existing project's own spelling, where the lib's answer is plain string equality: the
        # answer still comes from the lib (here, one that matches only the other project).
        for key, path, name in ((a, b, "two"), (b, a, "one")):
            with self.subTest(key=key, name=name, exact=True):
                standup = thc.load_tool(BIN / "harness-standup")
                restore = self.spy(self.registry, "same_path", mark=exactly_same(path, key))
                try:
                    self.assertEqual(standup.find_project(projects, key).name, name)
                finally:
                    restore()

    def test_marked_standup_ages_with_the_lib_age_text(self):
        standup = thc.load_tool(BIN / "harness-standup")
        self.spy(standup, "age_text", mark=tagged("AGE"))
        for now in AGE_NOWS:
            for seconds in AGE_SECONDS:
                with self.subTest(now=now, seconds=seconds):
                    then = now - seconds
                    self.assertEqual(standup.stamp(then, now)["age"], f"AGE<{age_expected(now - then)}>")

    def check_marked_budgets(self, load_file, read) -> None:
        """exhausted_budgets(now) of a fresh file follows a late parse_iso_seconds and a crafted read_json."""
        now = time.time()
        path, ahead = self.budgets(now)
        file = load_file()
        self.spy(file, "parse_iso_seconds", mark=shifted(3600))  # an hour late: cloud is ahead again
        self.assertEqual(read(file.exhausted_budgets(now)), self.late)
        file = load_file()
        later = stamp_utc(now + 600)
        self.spy(file, "read_json", mark=crafted("budgets.json", {
            "spy": {"exhausted_until": later, "reason": "from  the\nspy"}, "api": "replaced"}))
        self.assertEqual(read(file.exhausted_budgets(now)), [("spy", later, "from the spy")])

    def test_marked_standup_budgets_follow_the_lib_read_json_and_parse_iso_seconds(self):
        self.check_marked_budgets(lambda: thc.load_tool(BIN / "harness-standup"),
                                  lambda got: [(b["name"], b["until"], b["reason"]) for b in got])

    def test_marked_standup_runs_follow_the_lib_read_json_parse_iso_seconds_and_age_text(self):
        now = time.time()
        self.runs(now)
        r1, r3, r4 = self.RUNS_SEEN
        made = {"id": "r2", "repo": "/spy/made", "outcome": "ok", "started_at": stamp_utc(now - 5500),
                "ended_at": stamp_utc(now - 5400)}
        nine = {"id": "r9", "repo": "/spy/nine", "outcome": "ok", "started_at": stamp_utc(now - 400),
                "ended_at": stamp_utc(now - 300)}
        for label, mark, want in (
                ("read_json", crafted("bbbbbbbbbbbb-2.json", made), [r1, ("r2", "made", "ok", "1h ago"), r3, r4]),
                ("read_json", crafted("aaaaaaaaaaaa-1.json", nine), [("r9", "nine", "ok", "5m ago"), r3, r4]),
                ("parse_iso_seconds", shifted(-3 * 3600),
                 [("r1", "proj", "error", "4h ago"), ("r3", "naive", "ok", "5h ago"), ("r4", "offset", "failed", "6h ago")]),
                ("age_text", tagged("AGE"), [r[:3] + (f"AGE<{r[3]}>",) for r in self.RUNS_SEEN])):
            with self.subTest(helper=label, want=want[0]):
                standup = thc.load_tool(BIN / "harness-standup")
                self.spy(standup, label, mark=mark)
                got = standup.recent_runs([], now)
                self.assertEqual([(r["id"], r["project"], r["state"], r["age"]) for r in got], want)

    def test_marked_standup_names_gh_failures_by_the_lib_first_line(self):
        cases = [
            ("printf '\\n   HTTP 502: bad gateway  \\nretry later\\n' >&2; exit 1\n",
             "gh issue list failed: FL<HTTP 502: bad gateway>"),
            ("exit 4\n", "gh issue list failed: exit 4"),
            ("printf '  \\n<html>oops</html>\\n'; exit 0\n",
             "gh issue list printed something other than JSON: FL<<html>oops</html>>"),
            ("printf 'HTTP 404: Not Found' >&2; exit 1\n", "gh issue list failed: FL<HTTP 404: Not Found>"),
            ("printf 'not json\\n'; exit 0\n", "gh issue list printed something other than JSON: FL<not json>"),
        ]
        project = self.registry.Project(path=self.github_repo(), name="ghrepo")
        for i, (script, message) in enumerate(cases):
            with self.subTest(message=message):
                standup = thc.load_tool(BIN / "harness-standup")
                # GhBatch is gh.py's (#90): the line it names is gh.py's first_line's answer.
                restore = self.spy(self.ghlib, "first_line", mark=tagged("FL"))
                gh = self.fake_exe(f"ghm{i}", script)
                try:
                    with mock.patch.dict(os.environ, {"HARNESS_GH": gh}):
                        got = standup.board_report(project, time.time())
                finally:
                    restore()
                self.assertEqual(got["board_error"], message)

    def test_marked_digest_finds_its_files_where_the_lib_home_and_harness_home_point(self):
        alt = os.path.join(self.tmp, "spyhome")
        digest = thc.load_tool(BIN / "harness-digest")
        self.spy(digest, "home", mark=rooted(alt))
        self.spy(digest, "harness_home", mark=rooted(alt, ".claude", "harness"))
        self.assertEqual(digest.plist_path(), os.path.join(alt, "Library", "LaunchAgents",
                                                           "com.ryaneskuri.harness-digest.plist"))
        # The registry's place is registry.py's answer (#91), through its binding of the lib's harness_home.
        self.spy(self.registry, "harness_home", mark=rooted(alt, ".claude", "harness"))
        self.put(os.path.join(".claude", "harness", "projects.json"),
                 json.dumps({"version": 1, "projects": [{"name": "real", "path": "/real/one"}]}))
        self.put(os.path.join(alt, ".claude", "harness", "projects.json"),
                 json.dumps({"version": 1, "projects": [{"name": "spy", "path": "/s/p"}, {"path": "/t/u/"}]}))
        self.assertEqual(digest.load_projects(), [{"name": "spy", "path": "/s/p"}, {"name": "u", "path": "/t/u/"}])

    def test_marked_digest_names_launchctl_results_by_the_lib_first_line(self):
        cases = [
            ("printf '\\n  Boot-out failed: 3: No such process  \\nmore\\n' >&2; exit 3\n",
             (3, "FL<Boot-out failed: 3: No such process>")),
            ("printf '\\n\\n loaded ok\\n'; exit 0\n", (0, "FL<loaded ok>")),
            ("exit 5\n", (5, "exit 5")),
            ("printf 'Bootstrap failed: 5: Input/output error' >&2; exit 5\n",
             (5, "FL<Bootstrap failed: 5: Input/output error>")),
            ("printf 'ok\\n'; exit 0\n", (0, "FL<ok>")),
        ]
        for i, (script, want) in enumerate(cases):
            with self.subTest(want=want):
                digest = thc.load_tool(BIN / "harness-digest")
                self.spy(digest, "first_line", mark=tagged("FL"))
                exe = self.fake_exe(f"launchctlm{i}", script)
                with mock.patch.dict(os.environ, {"HARNESS_LAUNCHCTL": exe}):
                    self.assertEqual(digest.launchctl(["bootout", "gui/1/x"]), want)

    def test_marked_digest_matches_a_runs_repo_by_the_lib_same_path(self):
        a = os.path.join(self.tmp, "proj-a")
        b = os.path.join(self.tmp, "proj-b")
        os.makedirs(a)
        os.makedirs(b)
        projects = [{"name": "Alpha", "path": a}, {"name": "Beta", "path": b}]
        repo = "/elsewhere/spy-repo"
        for path, want in ((b, "Beta"), (a, "Alpha"), ("/no/match", "spy-repo")):
            with self.subTest(path=path):
                digest = thc.load_tool(BIN / "harness-digest")
                self.spy(digest, "same_path", mark=also_same(path, repo))
                self.assertEqual(digest.run_project({"repo": repo}, "/runs/zzzzzzzzzzzz-1.json", projects), want)
        # A repo that is an existing project's own spelling, where the lib's answer is plain string equality: the
        # answer still comes from the lib (here, one that matches the project listed first instead).
        for first, repo, want in (({"name": "Beta", "path": b}, a, "Beta"), ({"name": "Alpha", "path": a}, b, "Alpha")):
            with self.subTest(repo=repo, exact=True):
                digest = thc.load_tool(BIN / "harness-digest")
                self.spy(digest, "same_path", mark=exactly_same(first["path"], repo))
                listed = [first] + [p for p in projects if p["name"] != first["name"]]
                self.assertEqual(digest.run_project({"repo": repo}, "/runs/zzzzzzzzzzzz-1.json", listed), want)

    def test_marked_digest_ages_items_with_the_lib_parse_iso_seconds_and_age_text(self):
        late = 86400 * 3 + 7
        for now, (text, epoch) in [(n, c) for n in AGE_NOWS for c in ISO_CASES]:
            with self.subTest(now=now, text=text, helper="parse_iso_seconds"):
                digest = thc.load_tool(BIN / "harness-digest")
                self.spy(digest, "parse_iso_seconds", mark=shifted(late))
                self.assertEqual(digest.opened({"createdAt": text}, now),
                                 (epoch + late, age_expected(now - epoch - late)))
            with self.subTest(now=now, text=text, helper="age_text"):
                digest = thc.load_tool(BIN / "harness-digest")
                self.spy(digest, "age_text", mark=tagged("AGE"))
                self.assertEqual(digest.opened({"createdAt": text}, now), (epoch, f"AGE<{age_expected(now - epoch)}>"))

    def test_marked_digest_budgets_follow_the_lib_read_json_and_parse_iso_seconds(self):
        self.check_marked_budgets(lambda: thc.load_tool(BIN / "harness-digest"),
                                  lambda got: [(b["name"], b["until"], b["reason"]) for b in got])

    def test_marked_worklog_lists_the_folders_the_lib_session_folder_names(self):
        worklog = thc.load_tool(BIN / "harness-worklog")
        self.spy(worklog, "session_folder", mark=prefixed("SF-"))
        projects = os.path.join(self.home, ".claude", "projects")
        for repo in WORKLOG_REPOS:
            with self.subTest(repo=repo):
                own = "SF-" + folder_name(repo)
                wt = "SF-" + folder_name(os.path.join(repo, ".claude", "worktrees")) + "-"
                plain = folder_name(os.path.join(repo, ".claude", "worktrees")) + "-"
                for name in (wt + "t-1-a", wt + "t-2-b", own + "-other", plain + "t-3-c", folder_name(repo)):
                    os.makedirs(os.path.join(projects, name), exist_ok=True)
                self.assertEqual(worklog.transcripts_dirs(repo),
                                 [os.path.join(projects, own), os.path.join(projects, wt + "t-1-a"),
                                  os.path.join(projects, wt + "t-2-b")])

    def test_marked_session_brief_budget_line_follows_the_lib_read_json_and_one_line(self):
        now = time.time()
        _, ahead = self.budgets(now)
        brief = load_hook("session_brief.py")
        later = stamp_utc(now + 600)
        self.spy(brief, "read_json", mark=crafted("budgets.json", {
            "spy": {"exhausted_until": later, "reason": "from  the\nspy"}, "api": "replaced"}))
        self.assertEqual(brief.budget_part(), [f"Budget: spy exhausted until {later} (from the spy)"])
        brief = load_hook("session_brief.py")
        self.spy(brief, "one_line", mark=tagged("OL"))
        lines = brief.budget_part()
        self.assertEqual(len(lines), len(ahead), lines)
        for line, (name, _, reason) in zip(lines, ahead):
            if reason:   # "credit\n  balance" collapses; "quota" is already one line, and still goes through the lib
                with self.subTest(budget=name):
                    self.assertIn("OL<", line)
                    self.assertIn(reason, line)

    def test_marked_session_brief_writes_done_items_with_the_lib_one_line(self):
        for item, want in DOD_ITEMS:
            words = want[2:].split("] ", 1)[-1]
            with self.subTest(item=item):
                brief = load_hook("session_brief.py")
                self.spy(brief, "one_line", mark=tagged("OL"))
                got = brief.dod_line(item)
                self.assertTrue(got.startswith("- "), got)
                self.assertIn("OL<", got)
                self.assertIn(words, got)

    def test_marked_tests_first_decides_on_what_the_lib_read_json_answers(self):
        """The ticket cache and red record count as what read_json answers, not as what is on disk. (A non-object
        answer counting as absent is pinned by the unmarked test above, on files that hold one.)"""
        main = self.paths.canonical_path(os.path.join(self.tmp, "main"))
        os.makedirs(main, exist_ok=True)
        state = os.path.join(self.home, ".claude", "harness", "repos", self.paths.path_key(main))
        number, branch = "12", "t/12-fix-login"
        red_name = branch.replace("/", "-") + ".json"
        green = {"kind": "green", "test_files": ["t.py"], "test_hash": "0"}
        green_msg = (f"The red run on t/{number} passed before any source change, so it proves nothing: make "
                     "the tests fail against the current code, then run harness-red check again.")
        red_one = {"kind": "red", "test_files": ["t.py"], "test_hash": "0"}
        cases = [
            ("{}", None, f"{number}.json", {"flags": ["no-tests"]}, None),
            ("{}", None, f"{number}.json", {"labels": ["no-tests"], "type": "task"}, None),
            (json.dumps({"type": "task"}), "[1, 2]", red_name, green, green_msg),
            (json.dumps({"flags": ["x"]}), json.dumps(red_one), red_name, green, green_msg),
        ]
        for ticket, red, basename, answer, want in cases:
            with self.subTest(ticket=ticket, red=red, answer=answer):
                shutil.rmtree(state, True)
                self.put(os.path.join(state, "tickets", f"{number}.json"), ticket)
                if red is not None:
                    self.put(os.path.join(state, "red", red_name), red)
                hook = load_hook("tests_first.py")
                self.spy(hook, "read_json", mark=crafted(basename, answer))
                facts = {"main": main, "spelled": (), "branch": branch, "top": Path(self.tmp)}
                self.assertEqual(hook.source_refusal(facts, number), want)


class SessionBriefWithoutTheLib(unittest.TestCase):
    """session_brief.py from a plugin tree without lib/ still briefs (test_harness_lib.py), and its budget line
    is part of that brief today: taking read_json and one_line from the lib must not lose it. (This passes
    today; it guards the behavior the adoption puts at risk.)"""

    def test_the_budget_line_survives_a_missing_lib(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-brief-")))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(HOOKS, tmp / "plugin" / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        home = tmp / "home"
        project = tmp / "plain"
        project.mkdir()
        later = (dt.datetime.now(UTC) + dt.timedelta(hours=20)).replace(microsecond=0).isoformat()
        budgets = home / ".claude" / "harness" / "budgets.json"
        budgets.parent.mkdir(parents=True)
        budgets.write_text(json.dumps({"api": {"exhausted_until": later, "reason": "credit\n balance"},
                                       "cloud": {"exhausted_until": "2001-01-01T00:00:00Z", "reason": "x"}}))
        payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(project),
                   "hook_event_name": "SessionStart", "source": "startup"}
        r = subprocess.run([sys.executable, str(tmp / "plugin" / "hooks" / "session_brief.py")],
                           input=json.dumps(payload), capture_output=True, text=True, cwd=str(project), timeout=60,
                           env={"PATH": SYSTEM_PATH, "HOME": str(home), "LANG": "en_US.UTF-8",
                                "PYTHONDONTWRITEBYTECODE": "1", "GIT_CEILING_DIRECTORIES": str(tmp),
                                "TMPDIR": str(tmp)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"].splitlines()
        self.assertIn(f"Budget: api exhausted until {later} (credit balance)", lines)
        self.assertEqual([ln for ln in lines if ln.startswith("Budget:")],
                         [f"Budget: api exhausted until {later} (credit balance)"])


# ================================================================ the source


class FilesAdoptTheLib(unittest.TestCase):
    """The five files' source: no copy of a cli.py/text.py helper under any name or disguise (the allowed
    session_brief fallback aside), every name they take imported from the right lib module and used from code
    that runs, bound to the lib's object, the run_main tail, and worklog's __future__ import."""

    def forbidden(self, name: str) -> set:
        own = HOOK_OWN if name in HOOK_FILES else set()
        return (set(LIB_NAMES) | set(TODAY_NAMES)) - own

    def test_no_file_defines_a_helper_by_its_lib_name_or_todays_private_name(self):
        for name in FILES:
            tree = tree_of(name)
            skip = fallback_ids(tree, name)
            found = {}
            for node in ast.walk(tree):
                if id(node) in skip:
                    continue
                if isinstance(node, thc.DEFS):
                    found.setdefault(node.name, node.lineno)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            found.setdefault(t.id, node.lineno)
            for helper in sorted(self.forbidden(name)):
                with self.subTest(file=name, helper=helper):
                    self.assertNotIn(helper, found, f"{name}:{found.get(helper)} defines {helper}")

    def test_no_def_class_or_lambda_is_a_helper_under_any_name(self):
        """Nothing shaped like one of today's copies or the lib's own defs (names, docstrings, padding and dead
        code aside), and nothing calling the same names on the same constants as one."""
        for name in FILES:
            own = HOOK_OWN if name in HOOK_FILES else set()
            shapes, prints = reference_shapes(own), reference_fingerprints(own)
            self.assertGreaterEqual(len(shapes), 20)
            tree = tree_of(name)
            skip = fallback_ids(tree, name)
            for node in thc.defs_in(tree):
                if id(node) in skip:
                    continue
                with self.subTest(file=name, name=node.name, line=node.lineno):
                    found = shapes.get(thc.shape(node))
                    self.assertIsNone(found, f"{name}:{node.lineno} {node.name} is a copy of {found}")
                    found = prints.get(thc.fingerprint(node))
                    self.assertIsNone(found, f"{name}:{node.lineno} {node.name} calls what {found} calls on the "
                                             "same constants")

    def test_no_file_subclasses_argument_parser_or_exception(self):
        for name in FILES:
            tree = tree_of(name)
            skip = fallback_ids(tree, name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and id(node) not in skip:
                    for base in node.bases:
                        with self.subTest(file=name, cls=node.name):
                            text = ast.unparse(base)
                            self.assertNotIn("ArgumentParser", text)
                            self.assertNotRegex(text, r"(Exception|Error)$")

    def test_each_name_is_imported_from_its_lib_module(self):
        for name in FILES:
            imported = {}
            for node in ast.walk(tree_of(name)):
                if isinstance(node, ast.ImportFrom) and node.module in ("harness_lib.cli", "harness_lib.text"):
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.module.split(".")[1]
            for helper, module in sorted(TAKES[name].items()):
                with self.subTest(file=name, helper=helper):
                    self.assertEqual(imported.get(helper), module, f"{name} does not import {helper} from {module}")

    def test_each_name_taken_is_used_from_code_that_runs(self):
        for name in FILES:
            used = thc.used_names(without_fallback(tree_of(name), name))
            for helper in sorted(TAKES[name]):
                with self.subTest(file=name, helper=helper):
                    self.assertIn(helper, used, f"{name} takes {helper} but no code that runs calls, raises or "
                                                "passes it")

    def test_the_names_bound_are_the_lib_objects(self):
        libs = {"cli": thc.lib_module("cli"), "text": thc.lib_module("text")}
        for name in FILES:
            module = load(name)
            for helper, where in sorted(TAKES[name].items()):
                with self.subTest(file=name, helper=helper):
                    self.assertIs(getattr(module, helper, None), getattr(libs[where], helper))

    def test_the_tools_main_tail_is_run_main_with_the_tool_name(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                tails = [n for n in tree_of(tool).body if thc.is_main_guard(n)]
                self.assertEqual(len(tails), 1)
                self.assertEqual([ast.unparse(s) for s in tails[0].body], [f"run_main('{tool}', main)"])

    def test_worklog_has_future_annotations(self):
        tree = tree_of("harness-worklog")
        body = tree.body[1:] if (tree.body and isinstance(tree.body[0], ast.Expr)
                                 and isinstance(tree.body[0].value, ast.Constant)) else tree.body
        self.assertTrue(body and isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__"
                        and [a.name for a in body[0].names] == ["annotations"],
                        "harness-worklog does not begin with `from __future__ import annotations`")

    def test_the_tools_load_the_lib_from_their_real_path(self):
        """The prologue every adopting tool carries: harness_lib found next to the tool's real path."""
        for tool in TOOLS:
            source = (BIN / tool).read_text(encoding="utf-8")
            with self.subTest(tool=tool):
                self.assertRegex(source, r"os\.path\.realpath\(__file__\)")
                self.assertRegex(source, re.escape(f"{tool}: cannot load harness_lib from"))

    def test_the_files_carry_no_dead_code_and_every_def_is_referenced(self):
        for name in FILES:
            tree = without_fallback(tree_of(name), name)
            with self.subTest(file=name, rule="module level"):
                defined = {n.name for n in tree.body if isinstance(n, thc.DEFS)}
                calls = sorted(f"{n.func.id}:{n.lineno}" for s in tree.body if not isinstance(s, thc.DEFS)
                               and not thc.is_main_guard(s) for n in ast.walk(s)
                               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in defined)
                self.assertEqual(calls, [], f"{name}: module-level calls of its own functions: {calls}")
            for node in ast.walk(tree):
                if isinstance(node, (ast.If, ast.While)) and not thc.is_main_guard(node):
                    truth = thc.constant_truth(node.test)
                    if truth is not None and not (isinstance(node, ast.While) and truth):
                        with self.subTest(file=name, line=node.lineno):
                            self.fail(f"{name}:{node.lineno} {type(node).__name__} on a constant")
                for field in ("body", "orelse", "finalbody"):
                    stmts = getattr(node, field, None)
                    if isinstance(stmts, list):
                        for s in stmts[:-1]:
                            if isinstance(s, thc.ENDS_BLOCK):
                                with self.subTest(file=name, line=s.lineno):
                                    self.fail(f"{name}:{s.lineno} code after {type(s).__name__}")
            defined = {n.name for n in ast.walk(thc.pruned(tree)) if isinstance(n, thc.DEFS)
                       and not n.name.startswith("__")}
            unused = sorted(defined - thc.referenced_names(tree))
            with self.subTest(file=name, rule="referenced"):
                self.assertEqual(unused, [], f"{name} defines {unused} and nothing that runs references them")


if __name__ == "__main__":
    unittest.main()
