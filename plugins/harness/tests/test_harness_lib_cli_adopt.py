"""harness-work, harness-red, harness-scaffold and harness-projects adopt harness_lib/cli.py and text.py
(ticket #83, T5 of docs/proposals/refactor.md; section 2 names the differences each tool keeps).

- Every tool finds the lib from its own real path: loaded through a symlink in an unrelated folder, by an
  isolated interpreter, its Problem and Parser are the lib's classes from this checkout.
- None of them defines a moved helper, by name or as a copy under another name (shape and fingerprint, the
  machinery of test_harness_lib_cli.py), and none subclasses ArgumentParser or Exception. Each binds the lib's
  objects for the helpers it uses, uses them from code that runs, and ends in `run_main("<tool>", main)`.
- What each tool keeps, per the ticket: harness-work's `write_json` wrapper (the lib's writer, OSError as
  `Problem("cannot write <path>: <reason>")`), its own `warn` (`harness-work: <one line>`, no "warning:"),
  and `harness_home` where it called `home`; its `age_text(seconds)` is a different function from the lib's
  and stays. harness-red's `read_text(path)` (returns "" on OSError) and `load_record`'s validation, over the
  shared writer, with `Problem` re-exported as the lib's class. harness-scaffold's `one_line` becomes
  `last_line_collapsed`. harness-projects may keep its own `emit` and registry writer (`ensure_ascii`, item 3),
  so neither is asserted here.
- Through the tools' own functions, with the tool's binding of a lib name replaced by a recorder that
  delegates: a private copy leaves the recorder silent.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_cli_adopt.py" -v
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import errno
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import test_harness_lib_cli as base  # noqa: E402  the #82 adoption machinery: shapes, fingerprints, loaders

BIN, LIB, CLI_PY, TEXT_PY = base.BIN, base.LIB, base.CLI_PY, base.TEXT_PY
GIT = shutil.which("git")

TOOLS = ("harness-work", "harness-red", "harness-scaffold", "harness-projects")

# The helpers each tool carries today that the lib now holds, by the name the tool gives them.
MOVED = {
    "harness-work": ("Problem", "Parser", "one_line", "last_line", "home", "parse_iso", "read_json", "emit",
                     "table", "issue_number"),
    "harness-red": ("Problem", "Parser"),
    "harness-scaffold": ("Problem", "Parser", "one_line"),
    "harness-projects": ("Problem", "Parser", "table", "same_path", "encode", "age"),
}
# Kept by name, per the ticket: these are not checked against the reference shapes.
KEEP = {
    "harness-work": {"write_json", "warn", "age_text"},
    "harness-red": {"read_text", "load_record"},
    "harness-scaffold": {"last_line_collapsed"},
    "harness-projects": {"emit", "save"},
}
# The lib names each tool takes, by the name it binds, and the lib module each comes from.
USED = {
    "harness-work": {
        "Problem": "cli", "Parser": "cli", "emit": "cli", "table": "cli", "issue_number": "cli",
        "one_line": "text", "last_line": "text", "harness_home": "text", "parse_iso": "text", "read_json": "text",
    },
    "harness-red": {"Problem": "cli", "Parser": "cli", "write_json": "text"},
    "harness-scaffold": {"Problem": "cli", "Parser": "cli"},
    "harness-projects": {
        "Problem": "cli", "Parser": "cli", "table": "cli",
        "same_path": "text", "session_folder": "text", "age_text": "text",
    },
}

# The copies in these four tools as they are before this ticket, beyond the ones test_harness_lib_cli.py and the
# lib already supply: a def anywhere in a tool shaped like one of these is a copy under another name.
TODAY_COPIES = '''
def home(*parts):
    return os.path.join(os.path.expanduser("~"), ".claude", "harness", *parts)


def write_json(path, data):
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".harness-work-", suffix=".json", dir=folder)
    except OSError as err:
        raise Problem(f"cannot write {path}: {err.strerror or err}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\\n")
        os.replace(tmp, path)
    except OSError as err:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise Problem(f"cannot write {path}: {err.strerror or err}") from None


def emit(args, data, text):
    print(json.dumps(data, indent=2, ensure_ascii=False) if getattr(args, "json", False) else text)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_record(path, record):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".red-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
            fh.write("\\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def same_path(a, b):
    return os.path.realpath(absolute(a)) == os.path.realpath(absolute(b))


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


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
'''
# What an atomic writer calls: a def in harness-work or harness-red that calls one of these writes a file itself.
WRITER_CALLS = {"mkstemp", "fdopen", "os.replace", "os.rename", "NamedTemporaryFile"}


def tree_of(tool: str):
    return ast.parse((BIN / tool).read_text(encoding="utf-8"))


def all_references() -> list:
    """(origin, node): today's copies in board and pr, today's copies in these four, and every top-level def and
    class of cli.py and text.py."""
    found = [("board/pr before #82", n) for n in ast.parse(base.REFERENCE_COPIES).body]
    found += [("before #83", n) for n in ast.parse(TODAY_COPIES).body]
    for path in (CLI_PY, TEXT_PY):
        found += [(path.name, n) for n in ast.parse(path.read_text(encoding="utf-8")).body]
    return [(origin, n) for origin, n in found if isinstance(n, base.DEFS)]


def reference_shapes() -> dict:
    shapes = {}
    for origin, node in all_references():
        shapes.setdefault(base.shape(node), f"{origin}:{node.name}")
    return shapes


def reference_fingerprints() -> dict:
    prints = {}
    for origin, node in all_references():
        calls, consts = base.fingerprint(node)
        if len(calls) + len(consts) >= 3:
            prints.setdefault((calls, consts), f"{origin}:{node.name}")
    return prints


def calls_in(node) -> set:
    """The calls in a def's live code: `os.<name>` for os's functions, the bare attribute or name otherwise (so a
    str.replace is not os.replace)."""
    names = set()
    for sub in ast.walk(base.pruned(node)):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                owner = f.value.id if isinstance(f.value, ast.Name) else ""
                names.add(f"os.{f.attr}" if owner == "os" else f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def capture(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        value = fn(*args, **kwargs)
    return value, out.getvalue(), err.getvalue()


# ================================================================ the source


class ToolsFindTheLibFromTheirRealPath(unittest.TestCase):
    """Reached through a symlink in another folder, by an interpreter that adds nothing to sys.path (-I), each
    tool loads harness_lib.cli from this checkout and binds its Problem and Parser."""

    DRIVER = textwrap.dedent('''
        import importlib.util, json, os, sys
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("tool_under_test", sys.argv[1])
        spec = importlib.util.spec_from_loader("tool_under_test", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        cli = sys.modules.get("harness_lib.cli")
        print(json.dumps({
            "cli": os.path.realpath(cli.__file__) if cli else None,
            "problem": cli is not None and getattr(module, "Problem", None) is cli.Problem,
            "parser": cli is not None and getattr(module, "Parser", None) is cli.Parser,
        }))
    ''')

    def test_each_tool_loaded_through_a_symlink_binds_the_lib_from_this_checkout(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-link-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        driver = os.path.join(tmp, "driver.py")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(self.DRIVER)
        links = os.path.join(tmp, "elsewhere", "bin")
        os.makedirs(links)
        env = {"PATH": base.SYSTEM_PATH, "HOME": tmp, "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        for tool in TOOLS:
            link = os.path.join(links, tool)
            os.symlink(str(BIN / tool), link)
            with self.subTest(tool=tool):
                p = subprocess.run([sys.executable, "-I", driver, link], capture_output=True, text=True,
                                   env=env, cwd=tmp, timeout=60)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                got = json.loads(p.stdout)
                self.assertEqual(got, {"cli": os.path.realpath(str(CLI_PY)), "problem": True, "parser": True})


class ToolsDefineNoMovedHelper(unittest.TestCase):

    def test_none_of_the_four_defines_a_moved_helper_by_name(self):
        for tool in TOOLS:
            defined = {n.name for n in base.defs_in(tree_of(tool))} | base.top_level_names(BIN / tool)
            for name in MOVED[tool]:
                with self.subTest(tool=tool, name=name):
                    self.assertNotIn(name, defined, f"{tool} still defines {name}")

    def test_no_def_class_or_lambda_is_a_copy_of_a_moved_or_lib_helper_under_another_name(self):
        """Every def outside the kept ones, nested and lambdas included: not shaped like a reference (today's copies,
        board's and pr's before #82, the lib's own) and not calling the same names on the same constants."""
        shapes, prints = reference_shapes(), reference_fingerprints()
        for tool in TOOLS:
            for node in base.defs_in(tree_of(tool)):
                if node.name in KEEP[tool]:
                    continue
                with self.subTest(tool=tool, name=node.name, line=node.lineno):
                    found = shapes.get(base.shape(node))
                    self.assertIsNone(found, f"{tool}:{node.lineno} {node.name} is a copy of {found}")
                    found = prints.get(base.fingerprint(node))
                    self.assertIsNone(found, f"{tool}:{node.lineno} {node.name} calls what {found} calls on the "
                                             "same constants")

    def test_none_subclasses_argument_parser_or_exception(self):
        for tool in TOOLS:
            for node in base.defs_in(tree_of(tool)):
                if isinstance(node, ast.ClassDef):
                    for b in node.bases:
                        with self.subTest(tool=tool, cls=node.name, base=ast.unparse(b)):
                            self.assertNotIn("ArgumentParser", ast.unparse(b))
                            self.assertNotRegex(ast.unparse(b), r"(Exception|Error)$")

    def test_work_and_red_write_no_file_themselves(self):
        """The one atomic writer is the lib's: no def in harness-work or harness-red makes a temporary file or
        renames one over a target (the wrapper and red's record saving go through text.write_json)."""
        for tool in ("harness-work", "harness-red"):
            for node in base.defs_in(tree_of(tool)):
                with self.subTest(tool=tool, name=node.name, line=node.lineno):
                    self.assertEqual(calls_in(node) & WRITER_CALLS, set(),
                                     f"{tool}:{node.lineno} {node.name} writes a file through a temporary itself")

    def test_work_calls_harness_home_not_home(self):
        tree = tree_of("harness-work")
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn("home", called, "harness-work still calls home(...); the ticket renames it harness_home")
        self.assertIn("harness_home", called)

    def test_scaffold_names_its_line_picker_last_line_collapsed(self):
        names = {n.name for n in tree_of("harness-scaffold").body if isinstance(n, base.DEFS)}
        self.assertIn("last_line_collapsed", names)
        self.assertNotIn("one_line", names)

    def test_each_tool_imports_what_it_uses_from_cli_and_text(self):
        for tool in TOOLS:
            imported = {}
            for node in ast.walk(tree_of(tool)):
                if isinstance(node, ast.ImportFrom) and node.module in ("harness_lib.cli", "harness_lib.text"):
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.module.split(".")[1]
            for name, module in sorted(USED[tool].items()) + [("run_main", "cli")]:
                with self.subTest(tool=tool, name=name):
                    self.assertEqual(imported.get(name), module, f"{tool} does not import {name} from {module}")

    def test_each_name_taken_from_the_lib_is_used_from_code_that_runs(self):
        for tool in TOOLS:
            used = base.used_names(tree_of(tool))
            for name in sorted(USED[tool]) + ["run_main"]:
                with self.subTest(tool=tool, name=name):
                    self.assertIn(name, used, f"{tool} imports {name} but no code that runs calls, raises or passes it")

    def test_the_main_tail_is_run_main_with_the_tool_name(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                tails = [n for n in tree_of(tool).body if base.is_main_guard(n)]
                self.assertEqual(len(tails), 1)
                self.assertEqual([ast.unparse(s) for s in tails[0].body], [f"run_main('{tool}', main)"])

    def test_the_names_each_tool_binds_are_the_lib_objects(self):
        libs = {"cli": base.lib_module("cli"), "text": base.lib_module("text")}
        for tool in TOOLS:
            module = base.load_tool(BIN / tool)
            for name, where in sorted(USED[tool].items()) + [("run_main", "cli")]:
                with self.subTest(tool=tool, name=name):
                    self.assertIs(getattr(module, name, None), getattr(libs[where], name))


# ================================================================ the tools run the lib


class Spying(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = base.lib_module("cli")
        cls.text = base.lib_module("text")

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-adopt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls: list = []

    def spy(self, module, lib_module, name: str, required: bool = True) -> None:
        """Every global of `module` bound to lib_module.<name>, and the lib's own attribute, record their calls."""
        lib_obj = getattr(lib_module, name)
        bound = [n for n, v in vars(module).items() if v is lib_obj]
        if required:
            self.assertTrue(bound, f"{module.__name__} binds no name to the lib's {name}")
        calls = self.calls

        def recorder(*args, **kwargs):
            calls.append((name, args, kwargs))
            return lib_obj(*args, **kwargs)

        for n in bound:
            setattr(module, n, recorder)
        patcher = mock.patch.object(lib_module, name, recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def called(self, name: str) -> list:
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]

    def home_env(self, **extra):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        patcher = mock.patch.dict(os.environ, dict({"HOME": home}, **extra))
        patcher.start()
        self.addCleanup(patcher.stop)
        return home


class WorkKeepsItsWrappers(Spying):
    def setUp(self) -> None:
        super().setUp()
        self.work = base.load_tool(BIN / "harness-work")

    def test_write_json_writes_through_the_lib(self):
        cases = [
            (os.path.join(self.tmp, "a", "b.json"), {"api": {"reason": "café", "n": 1}}),
            (os.path.join(self.tmp, "deep", "er", "c.json"), [1, "two", None]),
            (os.path.join(self.tmp, "d.json"), {}),
        ]
        self.spy(self.work, self.text, "write_json", required=False)
        self.assertIsNot(self.work.write_json, self.text.write_json)
        for path, data in cases:
            with self.subTest(path=path):
                before = len(self.called("write_json"))
                self.work.write_json(path, data)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                got = self.called("write_json")[before:]
                self.assertEqual(len(got), 1, "harness-work's write_json does not call the lib's")
                self.assertEqual((got[0][0][0], got[0][0][1]), (path, data))
                self.assertEqual(sorted(n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")), [])

    def test_a_failed_write_is_the_lib_problem_cannot_write_path_reason(self):
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        target_dir = os.path.join(self.tmp, "is-a-dir.json")
        os.makedirs(os.path.join(target_dir, "inside"))
        cases = [
            (os.path.join(blocker, "sub", "x.json"), os.strerror(errno.ENOTDIR)),     # makedirs fails
            (os.path.join(target_dir), os.strerror(errno.EISDIR)),                    # the rename fails
        ]
        if os.geteuid() != 0:
            cases.append((os.path.join(locked, "y.json"), os.strerror(errno.EACCES)))  # mkstemp fails
        for path, reason in cases:
            with self.subTest(path=path):
                with self.assertRaises(self.cli.Problem) as caught:
                    self.work.write_json(path, {"k": "v"})
                self.assertEqual(str(caught.exception), f"cannot write {path}: {reason}")
                self.assertIs(type(caught.exception), self.cli.Problem)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".")], [])

    def test_a_failed_write_from_the_command_line_is_one_line_naming_the_path(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", "harness"), "w", encoding="utf-8") as fh:
            fh.write("not a folder")
        env = {"PATH": base.SYSTEM_PATH, "HOME": home, "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, str(BIN / "harness-work"), "budget", "exhaust", "api", "--reason", "x"],
                           capture_output=True, text=True, env=env, cwd=self.tmp, timeout=60)
        path = os.path.join(home, ".claude", "harness", "budgets.json")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        # makedirs of ~/.claude/harness, a regular file: EEXIST
        self.assertEqual(p.stderr.splitlines(), [f"harness-work: cannot write {path}: {os.strerror(errno.EEXIST)}"])
        # and in-process the same failure is the lib's Problem
        with mock.patch.dict(os.environ, {"HOME": home}):
            with self.assertRaises(self.cli.Problem):
                self.work.write_json(path, {})

    def test_warn_keeps_the_harness_work_prefix_and_collapses_through_the_lib(self):
        self.spy(self.work, self.text, "one_line")
        self.assertIsNot(self.work.warn, self.cli.warn)
        cases = [
            ("no origin remote", "harness-work: no origin remote\n"),
            ("git fetch\n  failed\t(exit 128)", "harness-work: git fetch failed (exit 128)\n"),
            ("  padded   words ", "harness-work: padded words\n"),
        ]
        for message, line in cases:
            with self.subTest(message=message):
                before = len(self.called("one_line"))
                _, out, err = capture(self.work.warn, message)
                self.assertEqual((out, err), ("", line))
                self.assertNotIn("warning:", err)
                self.assertGreater(len(self.called("one_line")), before, "warn does not collapse through the lib")

    def test_state_paths_are_under_harness_home_through_the_lib(self):
        home = self.home_env()
        self.spy(self.work, self.text, "harness_home")
        self.assertEqual(self.work.budgets_path(), os.path.join(home, ".claude", "harness", "budgets.json"))
        self.assertEqual(self.work.runs_dir(), os.path.join(home, ".claude", "harness", "runs"))
        self.assertEqual([args for args, _ in self.called("harness_home")], [("budgets.json",), ("runs",)])

    def test_budgets_read_and_parse_through_the_lib(self):
        home = self.home_env()
        self.spy(self.work, self.text, "read_json")
        self.spy(self.work, self.text, "parse_iso")
        path = os.path.join(home, ".claude", "harness", "budgets.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"api": {"exhausted_until": "2999-03-04T05:06:07Z", "reason": "cap"}}, fh)
        budgets = self.work.load_budgets()
        self.assertEqual(self.called("read_json"), [((path,), {})])
        self.assertEqual(budgets["api"]["reason"], "cap")
        until = self.work.exhausted_until(budgets["api"])
        self.assertEqual(until, dt.datetime(2999, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc))
        self.assertIsNone(self.work.exhausted_until({"exhausted_until": "2001-01-01T00:00:00"}))
        self.assertEqual([args[0] for args, _ in self.called("parse_iso")],
                         ["2999-03-04T05:06:07Z", "2001-01-01T00:00:00"])

    def test_budget_status_emits_through_the_lib_with_a_bool(self):
        self.home_env()
        self.spy(self.work, self.cli, "emit")
        _, out, _ = capture(self.work.cmd_budget, argparse.Namespace(action=None, json=True))
        self.assertEqual(sorted(json.loads(out)), sorted(self.work.BUDGETS))
        _, out, _ = capture(self.work.cmd_budget, argparse.Namespace(action="status"))   # no --json attribute
        self.assertEqual(out.splitlines(), [f"{name}: available" for name in self.work.BUDGETS])
        flags = [args[0] for args, _ in self.called("emit")]
        self.assertEqual(flags, [True, False])
        self.assertTrue(all(type(f) is bool for f in flags), flags)


class RedKeepsItsOwn(Spying):
    def setUp(self) -> None:
        super().setUp()
        self.red = base.load_tool(BIN / "harness-red")

    def test_problem_is_the_lib_class_and_load_record_keeps_its_validation(self):
        self.assertIs(self.red.Problem, self.cli.Problem)
        record_path = os.path.join(self.tmp, "red", "t-9-x.json")
        repo = SimpleNamespace(record_path=record_path, branch="t/9-x")
        with self.assertRaises(self.cli.Problem) as caught:
            self.red.load_record(repo)
        self.assertEqual(str(caught.exception), "No red run recorded for t/9-x; run harness-red check.")
        os.makedirs(os.path.dirname(record_path))
        bad = [
            ("{not json", f"{record_path} is not valid JSON ("),
            (json.dumps({"test_files": []}), f"{record_path} has no test_files; run harness-red check again"),
            (json.dumps({"test_files": ["a.py", ""]}), f"{record_path} has no test_files; run harness-red check again"),
            (json.dumps({"test_files": "a.py"}), f"{record_path} has no test_files; run harness-red check again"),
            (json.dumps(["a.py"]), f"{record_path} has no test_files; run harness-red check again"),
        ]
        for text, message in bad:
            with self.subTest(text=text):
                with open(record_path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                with self.assertRaises(self.cli.Problem) as caught:
                    self.red.load_record(repo)
                self.assertTrue(str(caught.exception).startswith(message), str(caught.exception))
        good = {"test_files": ["tests/test_a.py", "tests/test_b.py"], "kind": "assertion"}
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        self.assertEqual(self.red.load_record(repo), good)
        # its own read_text: "" for what cannot be read, not the lib's Problem
        self.assertIsNot(self.red.read_text, self.text.read_text)
        self.assertEqual(self.red.read_text(os.path.join(self.tmp, "missing.toml")), "")
        self.assertEqual(self.red.read_text(record_path), json.dumps(good))

    @unittest.skipUnless(GIT, "git is not installed")
    def test_check_saves_its_record_through_the_lib_writer(self):
        repo = os.path.join(self.tmp, "repo")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        env = {"PATH": os.pathsep.join([os.path.dirname(GIT), base.SYSTEM_PATH]), "HOME": home,
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "GIT_CEILING_DIRECTORIES": self.tmp,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@localhost",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@localhost",
               "HARNESS_TEST_CMD": f"{shlex.quote(sys.executable)} -m unittest {{files}}"}

        def git(*args):
            subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                           env=env, capture_output=True, text=True, check=True)

        os.makedirs(repo)
        git("init", "-q")
        with open(os.path.join(repo, "calc.py"), "w", encoding="utf-8") as fh:
            fh.write("def add(a, b):\n    return None\n")
        git("add", "-A")
        git("commit", "-q", "-m", "calc")
        git("checkout", "-q", "-b", "t/7-add")
        os.makedirs(os.path.join(repo, "tests"))
        with open(os.path.join(repo, "tests", "test_add.py"), "w", encoding="utf-8") as fh:
            fh.write("import unittest\n\nimport calc\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")

        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.spy(self.red, self.text, "write_json")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["harness-red", "check", "--worktree", repo]):
            with self.assertRaises(SystemExit) as caught:
                capture(self.red.main)
        self.assertIn(caught.exception.code, (0, None))
        saved = self.called("write_json")
        self.assertEqual(len(saved), 1, "harness-red check does not save its record through the lib's write_json")
        path, data = saved[0][0][0], saved[0][0][1]
        self.assertTrue(path.startswith(os.path.join(home, ".claude", "harness", "repos") + os.sep), path)
        self.assertTrue(path.endswith(os.path.join("red", "t-7-add.json")), path)
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, data)
        self.assertEqual((on_disk["branch"], on_disk["test_files"]), ("t/7-add", ["tests/test_add.py"]))


class ScaffoldRenamesItsPicker(Spying):
    def setUp(self) -> None:
        super().setUp()
        self.scaffold = base.load_tool(BIN / "harness-scaffold")

    def test_last_line_collapsed_is_the_last_non_blank_line_with_whitespace_collapsed(self):
        cases = [
            ("a\n  x   y  \n\n", "x y"),
            ("only", "only"),
            ("first line\nsecond\tline  here", "second line here"),
            ("  \n\t\n", ""),
            ("", ""),
            (None, ""),
            ("error: one\nhint:   two\n   ", "hint: two"),
        ]
        picker = getattr(self.scaffold, "last_line_collapsed", None)
        self.assertTrue(callable(picker), "harness-scaffold has no last_line_collapsed")
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(picker(text), want)
        lib_one_line = getattr(self.scaffold, "one_line", self.text.one_line)
        self.assertIs(lib_one_line, self.text.one_line, "harness-scaffold's one_line is not the lib's")

    def test_a_failed_command_is_the_lib_problem_naming_its_last_stderr_line(self):
        cases = [
            ("early\\n  late   words \\n", 3, "late words"),
            ("one\\ntwo\\nthree\\n", 1, "three"),
            ("", 4, "exit 4"),
        ]
        for stderr, code, why in cases:
            with self.subTest(stderr=stderr):
                cmd = [sys.executable, "-c", f"import sys; sys.stderr.write('{stderr}'); sys.exit({code})"]
                with self.assertRaises(self.cli.Problem) as caught:
                    self.scaffold.run(cmd)
                self.assertEqual(str(caught.exception), f"{' '.join(cmd[:3])} failed: {why}")


class ProjectsRunTheLib(Spying):
    def setUp(self) -> None:
        super().setUp()
        self.projects = base.load_tool(BIN / "harness-projects")

    def test_find_matches_a_path_through_the_lib_same_path(self):
        self.spy(self.projects, self.text, "same_path")
        real = os.path.join(self.tmp, "Work", "alpha")
        os.makedirs(real)
        link = os.path.join(self.tmp, "alias")
        os.symlink(real, link)
        projects = [{"name": "beta", "path": os.path.join(self.tmp, "beta")}, {"name": "alpha", "path": real}]
        self.assertEqual(self.projects.find(projects, link), 1)
        self.assertTrue(self.called("same_path"), "find does not compare paths through the lib")
        with self.assertRaises(self.cli.Problem):
            self.projects.find(projects, os.path.join(self.tmp, "nowhere"))

    def test_recover_encodes_entries_through_the_lib_session_folder(self):
        self.spy(self.projects, self.text, "session_folder")
        target = os.path.join(self.tmp, "My_Proj.v2", "sub dir")
        os.makedirs(target)
        os.makedirs(os.path.join(self.tmp, "other"))
        self.assertEqual(self.projects.recover("-My-Proj-v2-sub-dir", root=self.tmp), target)
        self.assertIsNone(self.projects.recover("-Nothing-here", root=self.tmp))
        self.assertIn("My_Proj.v2", [args[0] for args, _ in self.called("session_folder")])

    def test_discover_ages_and_tabulates_through_the_lib(self):
        home = self.home_env()
        self.spy(self.projects, self.text, "age_text")
        self.spy(self.projects, self.cli, "table")
        project = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(project, ".git"))
        folder = os.path.join(home, ".claude", "projects", self.text.session_folder(project))
        os.makedirs(folder)
        transcript = os.path.join(folder, "s.jsonl")
        with open(transcript, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        then = time.time() - 3 * 3600 - 30
        os.utime(transcript, (then, then))
        _, out, _ = capture(self.projects.cmd_discover, {"version": 1, "projects": []},
                            argparse.Namespace(json=False))
        self.assertIn(project, out)
        self.assertIn("(3h ago)", out)
        self.assertTrue(self.called("age_text"), "discover does not age through the lib")
        self.assertTrue(self.called("table"), "discover does not print its table through the lib")


if __name__ == "__main__":
    unittest.main()
