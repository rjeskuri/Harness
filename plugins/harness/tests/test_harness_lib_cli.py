"""Tests for harness_lib/cli.py and harness_lib/text.py (ticket #82, refactor 2 of
docs/proposals/refactor.md), and their adoption by harness-board and harness-pr.

- cli.py: Problem, Parser (a bad invocation is one line `<prog>: error: <message>`, exit 1,
  as every tool does today, subcommands included), run_main (Problem -> `<prog>: <one line>`
  exit 1; any other Exception -> `<prog>: <TypeName>: <one line>` exit 1; KeyboardInterrupt
  -> 130; BrokenPipeError -> exit 1 quietly; SystemExit passes through), warn, emit, table,
  issue_number. run_main and Parser run in a subprocess, the way the tools run.
- text.py: one_line, first_line, last_line, as_list, read_text (raising cli.Problem),
  read_json, write_json and write_text (temporary file in the same folder, renamed over the
  target; OSError reaches the caller), home, harness_home, now_iso, parse_iso (aware, naive
  means UTC), parse_iso_seconds (epoch float, naive means local time), age_text, same_path,
  session_folder. Expected values are computed here from first principles, matching what
  the copies in the tools return today.
- harness-board and harness-pr define none of the moved helpers (under any name, padding or
  dead code: the source is checked by live shape and by what a body calls), import them from
  harness_lib, use every imported name from code that runs, and end in `run_main("<tool>",
  main)`. For every lib name a tool takes, a real tool function runs with the tool's binding
  of that name replaced by a recorder: a private copy leaves the recorder silent.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_cli.py" -v
"""

from __future__ import annotations

import argparse
import ast
import calendar
import contextlib
import copy
import datetime as dt
import functools
import importlib
import importlib.util
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
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tools must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
LIB = PLUGIN / "lib"
TEMPLATES = PLUGIN / "templates"
CLI_PY = LIB / "harness_lib" / "cli.py"
TEXT_PY = LIB / "harness_lib" / "text.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UTC = dt.timezone.utc

CLI_NAMES = ("Problem", "Parser", "run_main", "emit", "table", "issue_number", "warn")
TEXT_NAMES = (
    "one_line", "first_line", "last_line", "as_list", "read_text", "read_json", "write_json",
    "write_text", "home", "harness_home", "now_iso", "parse_iso", "parse_iso_seconds",
    "age_text", "same_path", "session_folder",
)
# The helpers the ticket moves out of harness-board and harness-pr.
MOVED = (
    "Problem", "Parser", "one_line", "first_line", "last_line", "home", "now_iso", "parse_iso",
    "as_list", "read_text", "write_json", "write_file", "table", "emit", "issue_number",
)
# What each tool uses today, by the name it will bind from the lib, and the lib module it comes from.
USED = {
    "harness-board": {
        "Problem": "cli", "Parser": "cli", "table": "cli", "emit": "cli", "issue_number": "cli", "warn": "cli",
        "one_line": "text", "first_line": "text", "home": "text", "now_iso": "text",
        "parse_iso": "text", "as_list": "text", "read_text": "text", "write_json": "text",
        "read_json": "text", "harness_home": "text",
    },
    "harness-pr": {
        "Problem": "cli", "Parser": "cli", "table": "cli", "warn": "cli",
        "one_line": "text", "first_line": "text", "last_line": "text", "home": "text",
        "parse_iso": "text", "as_list": "text", "read_text": "text", "write_text": "text",
        "read_json": "text", "harness_home": "text", "write_json": "text",
    },
}
TOOLS = tuple(USED)


def lib_module(name: str):
    """harness_lib.<name> imported from the checkout's lib/, as a tool's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib." + name)


def load_tool(path: Path):
    """A tool imported by path as a fresh module, without running its main()."""
    modname = path.name.replace("-", "_") + "_cli_lib_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def capture(fn, *args, **kwargs):
    """(return value, stdout, stderr) of an in-process call."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        value = fn(*args, **kwargs)
    return value, out.getvalue(), err.getvalue()


class SubprocessCase(unittest.TestCase):
    """Runs a small script that imports the lib from the checkout, in a clean environment."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {
            "PATH": SYSTEM_PATH,
            "HOME": self.tmp,
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    def script(self, body: str) -> str:
        path = os.path.join(self.tmp, f"driver_{len(os.listdir(self.tmp))}.py")
        prologue = f"import sys\nsys.path.insert(0, {str(LIB)!r})\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(prologue + textwrap.dedent(body))
        return path

    def run_script(self, body: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.script(body), *argv],
            capture_output=True, text=True, env=self.env, cwd=self.tmp, timeout=60,
        )

    def assertOneLine(self, p, code, line):
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stderr.splitlines(), [line], p.stderr)


# ================================================================ the modules exist


class LibModules(unittest.TestCase):
    def test_cli_and_text_are_modules_of_the_lib(self):
        self.assertTrue(CLI_PY.is_file(), CLI_PY)
        self.assertTrue(TEXT_PY.is_file(), TEXT_PY)
        cli, text = lib_module("cli"), lib_module("text")
        self.assertEqual(os.path.realpath(cli.__file__), os.path.realpath(str(CLI_PY)))
        self.assertEqual(os.path.realpath(text.__file__), os.path.realpath(str(TEXT_PY)))
        for module, names in ((cli, CLI_NAMES), (text, TEXT_NAMES)):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(callable(getattr(module, name, None)), name)
        self.assertTrue(issubclass(cli.Problem, Exception))
        self.assertTrue(issubclass(cli.Parser, argparse.ArgumentParser))

    def test_the_lib_is_standard_library_only(self):
        allowed = set(sys.stdlib_module_names) | {"harness_lib", "__future__"} if hasattr(
            sys, "stdlib_module_names") else None
        for path in (CLI_PY, TEXT_PY):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module.split(".")[0]]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        if allowed is not None:
                            self.assertIn(name, allowed)


# ================================================================ cli.py: run_main


class RunMain(SubprocessCase):
    """run_main(prog, main): the `__main__` tail every tool carried, one line and never a traceback."""

    def run_main(self, prog: str, body: str) -> subprocess.CompletedProcess:
        script = (
            "from harness_lib.cli import run_main, Problem\n"
            "def main():\n" + textwrap.indent(textwrap.dedent(body), "    ") + "\n"
            f"run_main({prog!r}, main)\n"
        )
        return self.run_script(script)

    def test_a_problem_is_the_prog_and_its_message_on_one_line_exit_1(self):
        cases = [
            ("harness-board", "no such ticket #12", "harness-board: no such ticket #12"),
            ("harness-pr", "gh failed:\n  HTTP 502\n\tretry later", "harness-pr: gh failed: HTTP 502 retry later"),
            ("tool-x", "  padded   words  ", "tool-x: padded words"),
        ]
        for prog, message, line in cases:
            with self.subTest(prog=prog, message=message):
                p = self.run_main(prog, f"print('before')\nraise Problem({message!r})")
                self.assertOneLine(p, 1, line)
                self.assertEqual(p.stdout, "before\n")

    def test_any_other_exception_is_prog_type_name_and_one_line_exit_1(self):
        cases = [
            ("harness-board", "raise ValueError('bad\\nvalue  here')", "harness-board: ValueError: bad value here"),
            ("harness-pr", "raise KeyError('number')", "harness-pr: KeyError: 'number'"),
            ("tool-y", "1 / 0","tool-y: ZeroDivisionError: division by zero"),
            ("tool-z", "raise OSError(28, 'No space left on device')",
             "tool-z: OSError: [Errno 28] No space left on device"),
            ("harness-board", "import json; json.loads('{')",
             "harness-board: JSONDecodeError: Expecting property name enclosed in double quotes: "
             "line 1 column 2 (char 1)"),
        ]
        for prog, body, line in cases:
            with self.subTest(prog=prog, body=body):
                self.assertOneLine(self.run_main(prog, body), 1, line)

    def test_keyboard_interrupt_exits_130_quietly(self):
        for prog in ("harness-board", "harness-pr"):
            with self.subTest(prog=prog):
                p = self.run_main(prog, "raise KeyboardInterrupt")
                self.assertEqual(p.returncode, 130, p.stderr)
                self.assertEqual(p.stderr, "")

    def test_a_broken_pipe_raised_by_main_exits_1_quietly(self):
        p = self.run_main("harness-pr", "raise BrokenPipeError(32, 'Broken pipe')")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stderr, "")

    def test_a_reader_that_goes_away_stops_the_tool_with_exit_1_and_no_noise(self):
        """`tool | head -1`: stdout closes under a tool that keeps writing."""
        script = self.script(
            "from harness_lib.cli import run_main\n"
            "def main():\n"
            "    for i in range(2000000):\n"
            "        print(f'line {i} ' + 'x' * 60)\n"
            "run_main('harness-board', main)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env, cwd=self.tmp,
        )
        first = proc.stdout.readline()
        proc.stdout.close()
        try:
            code = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        self.assertTrue(first.startswith(b"line 0 "), first)
        self.assertEqual(code, 1, err)
        self.assertEqual(err, "")

    def test_system_exit_from_main_passes_through(self):
        cases = [
            ("import sys; sys.exit(4)", 4, ""),
            ("import sys; sys.exit(0)", 0, ""),
            ("import sys; sys.exit('harness-board: 2 problems found')", 1, "harness-board: 2 problems found\n"),
            ("print('ok')", 0, ""),
        ]
        for body, code, stderr in cases:
            with self.subTest(body=body):
                p = self.run_main("harness-board", body)
                self.assertEqual(p.returncode, code, p.stderr)
                self.assertEqual(p.stderr, stderr)
        self.assertEqual(self.run_main("harness-pr", "print('ok')").stdout, "ok\n")


# ================================================================ cli.py: Parser and issue_number


class ParserAndIssueNumber(SubprocessCase):
    PARSER = """
        from harness_lib.cli import Parser, issue_number
        ap = Parser(prog={prog!r})
        ap.add_argument("--json", action="store_true")
        sub = ap.add_subparsers(dest="cmd")
        sub.required = True
        show = sub.add_parser("show")
        show.add_argument("N", type=issue_number)
        sub.add_parser("board")
        args = ap.parse_args()
        print("parsed", args.cmd, getattr(args, "N", None), args.json)
    """

    def parse(self, prog, *argv):
        return self.run_script(self.PARSER.format(prog=prog), *argv)

    def test_a_bad_invocation_is_one_line_and_exit_1_like_the_tools_today(self):
        cases = [
            ("harness-board", ("show", "abc"), "harness-board show: error: argument N: not an issue number: 'abc'"),
            ("harness-pr", ("show", "#0"), "harness-pr show: error: argument N: not an issue number: '#0'"),
            ("harness-board", ("show",), "harness-board show: error: the following arguments are required: N"),
            ("tool-q", ("nope",), "tool-q: error: argument cmd: invalid choice: 'nope'"),
            ("harness-pr", ("--frobnicate", "board"), "harness-pr: error: unrecognized arguments: --frobnicate"),
        ]
        for prog, argv, line in cases:
            with self.subTest(prog=prog, argv=argv):
                p = self.parse(prog, *argv)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(len(p.stderr.splitlines()), 1, p.stderr)
                # argparse's wording after an invalid choice differs across Python versions: a prefix here
                self.assertTrue(p.stderr.startswith(line), p.stderr)
                if "invalid choice" not in line:
                    self.assertEqual(p.stderr, line + "\n")
                self.assertEqual(p.stdout, "")
                self.assertNotIn("usage:", p.stderr)

    def test_a_good_invocation_parses(self):
        cases = [
            (("show", "#41"), "parsed show 41 False\n"),
            (("--json", "show", "7"), "parsed show 7 True\n"),
            (("board",), "parsed board None False\n"),
        ]
        for argv, out in cases:
            with self.subTest(argv=argv):
                p = self.parse("harness-board", *argv)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout, out)
                self.assertEqual(p.stderr, "")

    def test_issue_number_accepts_positive_numbers_with_or_without_hashes(self):
        cli = lib_module("cli")
        for given, want in (("1", 1), ("#1", 1), ("42", 42), ("#907", 907), ("0007", 7), ("##5", 5),
                            ("#123456", 123456)):
            with self.subTest(given=given):
                self.assertEqual(cli.issue_number(given), want)

    def test_issue_number_rejects_everything_else_with_the_board_message(self):
        cli = lib_module("cli")
        for given in ("0", "#0", "-1", "abc", "", "#", "1.5", " 3", "3a", "#-2", "+4"):
            with self.subTest(given=given):
                with self.assertRaises(argparse.ArgumentTypeError) as ctx:
                    cli.issue_number(given)
                self.assertEqual(str(ctx.exception), f"not an issue number: {given!r}")


# ================================================================ cli.py: warn, emit, table


class CliOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = lib_module("cli")

    def test_warn_is_prog_warning_and_one_line_on_stderr(self):
        cases = [
            ("harness-board", "the Projects board acme/3 was not updated: show",
             "harness-board: warning: the Projects board acme/3 was not updated: show\n"),
            ("harness-pr", "could not post\n  the comment\t(502)",
             "harness-pr: warning: could not post the comment (502)\n"),
            ("tool-w", "  café  ", "tool-w: warning: café\n"),
        ]
        for prog, message, line in cases:
            with self.subTest(prog=prog):
                _, out, err = capture(self.cli.warn, prog, message)
                self.assertEqual(out, "")
                self.assertEqual(err, line)

    def test_emit_prints_indented_unescaped_json_or_the_text(self):
        data = {"z": 1, "name": "café", "items": [1, "two"]}
        want_json = '{\n  "z": 1,\n  "name": "café",\n  "items": [\n    1,\n    "two"\n  ]\n}\n'
        _, out, err = capture(self.cli.emit, True, data, "ignored text")
        self.assertEqual(out, want_json)
        self.assertEqual(err, "")
        _, out, _ = capture(self.cli.emit, False, data, "#12 Fix login (task, S, low)")
        self.assertEqual(out, "#12 Fix login (task, S, low)\n")
        _, out, _ = capture(self.cli.emit, True, [], "nothing")
        self.assertEqual(out, "[]\n")
        _, out, _ = capture(self.cli.emit, False, [], "line one\nline two")
        self.assertEqual(out, "line one\nline two\n")

    def test_table_left_aligns_all_but_the_last_column_two_spaces_apart(self):
        cases = [
            ([("#1", "ready", "Fix login"), ("#23", "in-progress", "Add the thing")],
             "#1   ready        Fix login\n#23  in-progress  Add the thing"),
            ([("a", "bb", ""), ("ccc", "d", "x")],
             "a    bb\nccc  d   x"),
            ([("k", "a very long last column"), ("key", "v")],
             "k    a very long last column\nkey  v"),
            ([("only",), ("two",)], "only\ntwo"),
            ([("NAME", "AGE"), ("harness", "3d"), ("jotform-sync", "just now")],
             "NAME          AGE\nharness       3d\njotform-sync  just now"),
            ([("solo", "row", "here")], "solo  row  here"),
        ]
        for rows, want in cases:
            with self.subTest(rows=rows):
                self.assertEqual(self.cli.table(rows), want)


# ================================================================ text.py: line pickers and lists


class TextLines(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = lib_module("text")

    def test_one_line_collapses_all_whitespace(self):
        cases = [
            ("a\n b\t c", "a b c"),
            ("  padded  ", "padded"),
            ("", ""),
            ("\n\n", ""),
            ("one", "one"),
            (42, "42"),
            (None, "None"),
            (ValueError("bad\r\nthing"), "bad thing"),
            ("café\u00a0au lait", "café au lait"),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(self.text.one_line(given), want)

    def test_first_and_last_line_are_the_outer_non_blank_lines_stripped(self):
        cases = [
            ("\n\n  first  \nmiddle\n  last \n\n", "first", "last"),
            ("only", "only", "only"),
            ("", "", ""),
            (None, "", ""),
            ("   \n\t\n", "", ""),
            ("a\nb\nc", "a", "c"),
            ("\r\nwin\r\nlines\r\n", "win", "lines"),
        ]
        for given, first, last in cases:
            with self.subTest(given=given):
                self.assertEqual(self.text.first_line(given), first)
                self.assertEqual(self.text.last_line(given), last)

    def test_as_list_takes_a_list_or_nodes_and_keeps_only_dicts(self):
        a, b = {"number": 1}, {"number": 2}
        cases = [
            ([a, b], [a, b]),
            ([a, "x", 3, None, b, []], [a, b]),
            ({"nodes": [b, a]}, [b, a]),
            ({"nodes": [a, 7]}, [a]),
            ({"nodes": None}, []),
            ({"edges": [a]}, []),
            ({}, []),
            (None, []),
            ("text", []),
            (5, []),
            ([], []),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(self.text.as_list(given), want)


# ================================================================ text.py: files


class TextFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = lib_module("cli")
        cls.text = lib_module("text")

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-text-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def put(self, rel: str, data) -> str:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def raw(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    # ---- read_text

    def test_read_text_returns_the_contents_and_expands_the_home(self):
        body = "## Request\nFix the login — now.\n"
        path = self.put("notes/body.md", body)
        self.assertEqual(self.text.read_text(path, "body file"), body)
        self.put("bad.txt", b"ok \xff\xfe end")
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertEqual(self.text.read_text("~/notes/body.md", "body file"), body)
            self.assertEqual(self.text.read_text("~/bad.txt", "evidence file"), "ok \ufffd\ufffd end")

    def test_read_text_raises_the_one_problem_naming_what_and_the_path_as_given(self):
        os.mkdir(os.path.join(self.tmp, "adir"))
        cases = [
            (os.path.join(self.tmp, "missing.md"), "body file", "No such file or directory"),
            (os.path.join(self.tmp, "adir"), "evidence file", "Is a directory"),
            ("~/nowhere/x.json", "findings", "No such file or directory"),
        ]
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for path, what, reason in cases:
                with self.subTest(path=path):
                    with self.assertRaises(self.cli.Problem) as ctx:
                        self.text.read_text(path, what)
                    self.assertEqual(str(ctx.exception), f"cannot read {what} {path}: {reason}")

    # ---- read_json

    def test_read_json_returns_any_value_or_none(self):
        cases = [
            ("a.json", '{"b": [1, 2], "n": "café"}', {"b": [1, 2], "n": "café"}),
            ("b.json", "[3, 4]", [3, 4]),
            ("c.json", "7", 7),
            ("d.json", '"s"', "s"),
            ("e.json", "{", None),
            ("f.json", "", None),
        ]
        for rel, body, want in cases:
            with self.subTest(rel=rel):
                path = self.put(rel, body)
                self.assertEqual(self.text.read_json(path), want)
                self.assertEqual(self.text.read_json(Path(path)), want)
        self.assertIsNone(self.text.read_json(os.path.join(self.tmp, "missing.json")))
        self.assertIsNone(self.text.read_json(self.tmp))

    # ---- write_json and write_text

    def test_write_json_writes_indented_unescaped_json_with_a_newline_creating_folders(self):
        cases = [
            ("tickets/12.json", {"number": 12, "title": "Fix login — café", "labels": ["a", "b"]},
             '{\n  "number": 12,\n  "title": "Fix login — café",\n  "labels": [\n    "a",\n    "b"\n  ]\n}\n'),
            ("deep/er/x.json", [1, {"k": None}], '[\n  1,\n  {\n    "k": null\n  }\n]\n'),
            ("flat/empty.json", {}, "{}\n"),
        ]
        for rel, data, want in cases:
            with self.subTest(rel=rel):
                path = os.path.join(self.tmp, rel)
                self.text.write_json(path, data, prefix=".ticket-")
                self.assertEqual(self.raw(path), want.encode("utf-8"))
                self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])

    def test_write_json_replaces_through_a_prefixed_temporary_file_in_the_same_folder(self):
        for prefix in (".ticket-", ".projects-"):
            with self.subTest(prefix=prefix):
                folder = os.path.join(self.tmp, "w" + prefix.strip(".-"))
                path = self.put(os.path.join(folder, "state.json"), '{"old": true}\n')
                seen = []

                class Spy(dict):
                    def items(self):
                        with open(path, encoding="utf-8") as fh:
                            seen.append((sorted(os.listdir(folder)), fh.read()))
                        return super().items()

                self.text.write_json(path, Spy(new=1), prefix=prefix)
                self.assertEqual(len(seen), 1, seen)
                names, during = seen[0]
                self.assertEqual(during, '{"old": true}\n', "the target changed before the rename")
                others = [n for n in names if n != "state.json"]
                self.assertIn("state.json", names)
                self.assertEqual(len(others), 1, names)
                self.assertTrue(others[0].startswith(prefix), others)
                self.assertEqual(self.raw(path), b'{\n  "new": 1\n}\n')
                self.assertEqual(os.listdir(folder), ["state.json"])

    def test_write_json_that_fails_mid_write_leaves_the_old_file_and_no_temporary(self):
        path = self.put("keep/state.json", '{"old": true}\n')
        with self.assertRaises(TypeError):
            self.text.write_json(path, {"a": 1, "b": object()}, prefix=".ticket-")
        self.assertEqual(self.raw(path), b'{"old": true}\n')
        self.assertEqual(os.listdir(os.path.dirname(path)), ["state.json"])

    def test_write_text_writes_the_text_as_given_creating_folders(self):
        cases = [
            ("pr/body.md", "## Summary\nline — café"),
            ("pr/body.md", "replaced\n\n"),
            ("a/b/c/empty.txt", ""),
        ]
        for rel, body in cases:
            with self.subTest(rel=rel, body=body):
                path = os.path.join(self.tmp, rel)
                self.text.write_text(path, body, prefix=".harness-pr-")
                self.assertEqual(self.raw(path), body.encode("utf-8"))
                self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])

    def test_the_writers_raise_oserror_and_leave_no_temporary(self):
        blocker = self.put("plain-file", "x")
        os.mkdir(os.path.join(self.tmp, "target-is-dir"))
        os.mkdir(os.path.join(self.tmp, "target-is-dir", "out.json"))
        for name, call in (
            ("write_json", lambda p: self.text.write_json(p, {"a": 1}, prefix=".t-")),
            ("write_text", lambda p: self.text.write_text(p, "abc", prefix=".t-")),
        ):
            with self.subTest(writer=name, case="folder is a file"):
                with self.assertRaises(OSError):
                    call(os.path.join(blocker, "sub", "out.json"))
            with self.subTest(writer=name, case="target is a directory"):
                folder = os.path.join(self.tmp, "target-is-dir")
                with self.assertRaises(OSError):
                    call(os.path.join(folder, "out.json"))
                self.assertEqual(os.listdir(folder), ["out.json"])
                self.assertTrue(os.path.isdir(os.path.join(folder, "out.json")))
        self.assertEqual(self.raw(blocker), b"x")

    # ---- home and harness_home

    def test_home_and_harness_home_join_under_the_home_directory(self):
        for home in (os.path.join(self.tmp, "h1"), os.path.join(self.tmp, "Other Home")):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"HOME": home}):
                self.assertEqual(self.text.home(), home)
                self.assertEqual(self.text.home(".claude"), os.path.join(home, ".claude"))
                self.assertEqual(self.text.home(".claude", "settings.json"),
                                 os.path.join(home, ".claude", "settings.json"))
                self.assertEqual(self.text.harness_home(), os.path.join(home, ".claude", "harness"))
                self.assertEqual(self.text.harness_home("projects.json"),
                                 os.path.join(home, ".claude", "harness", "projects.json"))
                self.assertEqual(self.text.harness_home("repos", "abc123", "tickets"),
                                 os.path.join(home, ".claude", "harness", "repos", "abc123", "tickets"))

    # ---- same_path and session_folder

    def test_same_path_compares_real_absolute_paths(self):
        a = os.path.join(self.tmp, "Proj")
        b = os.path.join(self.tmp, "Other")
        os.makedirs(a)
        os.makedirs(b)
        link = os.path.join(self.tmp, "link")
        os.symlink(a, link)
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            cases = [
                (a, a, True),
                (a, a + os.sep, True),
                (a, link, True),
                (a, "~/Proj", True),
                ("~/link", a, True),
                (os.path.join(b, "..", "Proj"), a, True),
                (a, b, False),
                ("~/Other", a, False),
                (os.path.join(self.tmp, "missing"), os.path.join(self.tmp, "missing"), True),
                (os.path.join(self.tmp, "missing"), os.path.join(self.tmp, "missing2"), False),
            ]
            for x, y, want in cases:
                with self.subTest(x=x, y=y):
                    self.assertIs(self.text.same_path(x, y), want)
            cwd = os.getcwd()
            os.chdir(self.tmp)
            try:
                self.assertTrue(self.text.same_path("Proj", a))
                self.assertFalse(self.text.same_path("Other", a))
            finally:
                os.chdir(cwd)

    def test_session_folder_is_the_path_with_every_non_alphanumeric_as_a_dash(self):
        cases = [
            ("/Users/ryaneskuri/portfolio_projects/Harness", "-Users-ryaneskuri-portfolio-projects-Harness"),
            ("/tmp/a.b c_d", "-tmp-a-b-c-d"),
            ("/x/café", "-x-caf-"),
            ("/a/.claude/worktrees/t-82-x", "-a--claude-worktrees-t-82-x"),
            ("Rel9/ok", "Rel9-ok"),
            ("", ""),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(self.text.session_folder(given), want)


# ================================================================ text.py: time


class TextTime(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = lib_module("text")

    def set_tz(self, zone: str) -> None:
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = zone
        time.tzset()

    def test_now_iso_is_the_current_utc_second_with_a_z(self):
        before = int(time.time())
        stamp = self.text.now_iso()
        after = int(time.time())
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        seconds = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(before - 1 <= seconds <= after + 1, (before, stamp, after))

    def test_parse_iso_is_aware_and_naive_means_utc(self):
        plus2 = dt.timezone(dt.timedelta(hours=2))
        minus7 = dt.timezone(dt.timedelta(hours=-7, minutes=-30))
        cases = [
            ("2026-10-16T12:34:56Z", dt.datetime(2026, 10, 16, 12, 34, 56, tzinfo=UTC), dt.timedelta(0)),
            ("2026-10-16T12:34:56z", dt.datetime(2026, 10, 16, 12, 34, 56, tzinfo=UTC), dt.timedelta(0)),
            ("  2025-01-02T03:04:05Z\n", dt.datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC), dt.timedelta(0)),
            ("2026-10-16T12:34:56+02:00", dt.datetime(2026, 10, 16, 12, 34, 56, tzinfo=plus2),
             dt.timedelta(hours=2)),
            ("2026-03-01T00:00:00-07:30", dt.datetime(2026, 3, 1, 0, 0, 0, tzinfo=minus7),
             dt.timedelta(hours=-7, minutes=-30)),
            ("2026-10-16T12:34:56", dt.datetime(2026, 10, 16, 12, 34, 56, tzinfo=UTC), dt.timedelta(0)),
            ("2026-10-16T12:34:56.250Z", dt.datetime(2026, 10, 16, 12, 34, 56, 250000, tzinfo=UTC),
             dt.timedelta(0)),
            ("2026-10-16", dt.datetime(2026, 10, 16, tzinfo=UTC), dt.timedelta(0)),
        ]
        for zone in ("Asia/Tokyo", "America/New_York"):
            self.set_tz(zone)
            for given, want, offset in cases:
                with self.subTest(zone=zone, given=given):
                    got = self.text.parse_iso(given)
                    self.assertIsInstance(got, dt.datetime)
                    self.assertIsNotNone(got.tzinfo)
                    self.assertEqual(got, want)
                    self.assertEqual(got.utcoffset(), offset)
        # the instant itself, checked against calendar arithmetic
        self.assertEqual(self.text.parse_iso("2026-10-16T12:00:00+02:00").timestamp(),
                         calendar.timegm((2026, 10, 16, 10, 0, 0)))

    def test_parse_iso_is_none_for_anything_else(self):
        for given in (None, "", "   ", 42, 1.5, [], "not a date", "2026-13-01T00:00:00Z", "2026-02-30",
                      "yesterday"):
            with self.subTest(given=given):
                self.assertIsNone(self.text.parse_iso(given))

    def test_parse_iso_seconds_is_epoch_seconds_and_naive_means_local_time(self):
        aware = [
            ("2026-10-16T12:00:00Z", calendar.timegm((2026, 10, 16, 12, 0, 0))),
            (" 2026-10-16T12:00:00Z ", calendar.timegm((2026, 10, 16, 12, 0, 0))),
            ("2026-10-16T12:00:00+02:00", calendar.timegm((2026, 10, 16, 10, 0, 0))),
            ("2024-02-29T23:59:59-05:00", calendar.timegm((2024, 3, 1, 4, 59, 59))),
            ("2026-10-16T12:00:00.500Z", calendar.timegm((2026, 10, 16, 12, 0, 0)) + 0.5),
        ]
        # naive: local wall time; offsets from the zone rules (New York: EST -5 in January, EDT -4 in July)
        naive = {
            "America/New_York": [("2026-01-15T12:00:00", calendar.timegm((2026, 1, 15, 17, 0, 0))),
                                 ("2026-07-15T12:00:00", calendar.timegm((2026, 7, 15, 16, 0, 0)))],
            "Asia/Tokyo": [("2026-01-15T12:00:00", calendar.timegm((2026, 1, 15, 3, 0, 0))),
                           ("2026-07-15T12:00:00", calendar.timegm((2026, 7, 15, 3, 0, 0)))],
            "UTC": [("2026-01-15T12:00:00", calendar.timegm((2026, 1, 15, 12, 0, 0)))],
        }
        for zone, local_cases in naive.items():
            self.set_tz(zone)
            for given, want in aware + local_cases:
                with self.subTest(zone=zone, given=given):
                    got = self.text.parse_iso_seconds(given)
                    self.assertIsInstance(got, float)
                    self.assertEqual(got, want)

    def test_parse_iso_seconds_is_none_for_anything_else(self):
        for given in (None, "", "  ", 7, "garbage", "2026-02-30T00:00:00", "2026-10-16T25:00:00Z"):
            with self.subTest(given=given):
                self.assertIsNone(self.text.parse_iso_seconds(given))

    def test_age_text_buckets_the_seconds_between_then_and_now(self):
        cases = [
            (0, "just now"), (59, "just now"), (59.9, "just now"), (-3600, "just now"),
            (60, "1m ago"), (61, "1m ago"), (125, "2m ago"), (3599, "59m ago"),
            (3600, "1h ago"), (7199, "1h ago"), (7200, "2h ago"), (86399, "23h ago"),
            (86400, "1d ago"), (86400 * 3 + 5, "3d ago"), (86400 * 60 - 1, "59d ago"),
            (86400 * 60, "2mo ago"), (86400 * 95, "3mo ago"), (86400 * 730 - 1, "24mo ago"),
            (86400 * 730, "2y ago"), (86400 * 365 * 5 + 10, "5y ago"),
        ]
        for now in (1_800_000_000.0, 1_760_000_123.25):
            for seconds, want in cases:
                with self.subTest(now=now, seconds=seconds):
                    self.assertEqual(self.text.age_text(now - seconds, now), want)


# ================================================================ the tools adopt the lib

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ENDS_BLOCK = (ast.Return, ast.Raise, ast.Continue, ast.Break)
MAIN_GUARD = ("__name__ == '__main__'", '__name__ == "__main__"')
# Every export of cli.py and text.py is a reference for the copy check (plus the private helper write_json and
# write_text share), and so are the moved helpers under the names the tools carried them by.
EXPORTS = CLI_NAMES + TEXT_NAMES
LIB_REFERENCE_NAMES = set(MOVED) | set(EXPORTS) | {"_replace"}
# Functions the tools define today that nothing in them references (a leftover of the state-key move, #79).
UNREFERENCED_TODAY = {"repo_key"}


def top_level_names(path: Path) -> set:
    """Names a module defines at its top level: def, class, and plain assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


# The moved helpers as harness-board and harness-pr carried them before this ticket. A def, class or lambda in a
# tool whose shape (live statements only, docstring dropped, every name and argument renamed in order of
# appearance) equals one of these, or one of the lib's own, is a copy under another name; so is one that calls
# the same names on the same constants.
REFERENCE_COPIES = '''
class Problem(Exception):
    """A bad invocation, a failed precondition or a failed gh call: printed as one line, exit 1."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.exit(f"{self.prog}: error: {message}")


def one_line(text):
    return " ".join(str(text).split())


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def warn(message):
    print(f"harness-board: warning: {one_line(message)}", file=sys.stderr)


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def as_list(value):
    if isinstance(value, dict):
        value = value.get("nodes")
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def write_json(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ticket-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_file(path, text):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".harness-pr-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path, what):
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as err:
        raise Problem(f"cannot read {what} {path}: {err.strerror or err}") from None


def emit(args, data, text):
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else text)


def table(rows):
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return "\\n".join("  ".join([c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]).rstrip() for r in rows)


def issue_number(text):
    digits = text.lstrip("#")
    if not digits.isdigit() or int(digits) <= 0:
        raise argparse.ArgumentTypeError(f"not an issue number: {text!r}")
    return int(digits)
'''
# Names in today's tools that contain a moved name and are not copies of one.
LEGIT_NAMES = {"short_home"}


def is_main_guard(node) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) in MAIN_GUARD


def constant_truth(test):
    """The truth of a test that is a constant, or `not` a constant; None for a real test."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Constant):
        return not test.operand.value
    return None


def live(stmts: list) -> list:
    """`stmts` without what never runs or does nothing: pass, bare constants (docstrings, `...`), an assert of a
    true constant, the dead branch of an `if` on a constant, a `while` on a false constant, and everything after
    a return, raise, continue or break."""
    out = []
    for s in stmts:
        if isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)):
            continue
        if isinstance(s, ast.Assert) and constant_truth(s.test) is True:
            continue
        if isinstance(s, ast.If) and constant_truth(s.test) is not None:
            out.extend(live(s.body if constant_truth(s.test) else s.orelse))
            continue
        if isinstance(s, ast.While) and constant_truth(s.test) is False:
            out.extend(live(s.orelse))
            continue
        out.append(s)
        if isinstance(s, ENDS_BLOCK):
            break
    return out


def pruned(node):
    """A copy of `node` with every statement list reduced to its live statements."""
    node = copy.deepcopy(node)

    def prune(n):
        for field, value in ast.iter_fields(n):
            if isinstance(value, list):
                if value and isinstance(value[0], ast.stmt):
                    value = live(value)
                    setattr(n, field, value)
                for item in value:
                    if isinstance(item, ast.AST):
                        prune(item)
            elif isinstance(value, ast.AST):
                prune(value)
        return n

    return prune(node)


def as_def(lam, name: str):
    """A lambda as the def it abbreviates."""
    node = ast.parse(f"def _({ast.unparse(lam.args)}):\n    return {ast.unparse(lam.body)}").body[0]
    node.name, node.lineno = name, getattr(lam, "lineno", 0)
    return node


def is_partial(node) -> bool:
    """A call of functools.partial (or a bare `partial`) with something to bind."""
    return isinstance(node, ast.Call) and call_name(node) == "partial" and bool(node.args)


def partial_as_def(call, name: str):
    """`partial(f, a, k=v)` as the def it stands for: `def _(*rest, **kw): return f(a, *rest, k=v, **kw)`."""
    bound = [ast.unparse(a) for a in call.args[1:]] + ["*_rest"]
    bound += [f"{k.arg}={ast.unparse(k.value)}" if k.arg else f"**{ast.unparse(k.value)}" for k in call.keywords]
    node = ast.parse(f"def _(*_rest, **_kw):\n    return {ast.unparse(call.args[0])}({', '.join(bound + ['**_kw'])})"
                     ).body[0]
    node.name, node.lineno = name, getattr(call, "lineno", 0)
    return node


def defs_in(tree) -> list:
    """Every def and class in `tree`, nested ones included, and every lambda and functools.partial as a def named
    for what it is assigned to."""
    named = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and (isinstance(node.value, ast.Lambda)
                                                              or is_partial(node.value)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            named[id(node.value)] = names[0] if names else "<lambda>"
    found = []
    for node in ast.walk(tree):
        if isinstance(node, DEFS):
            found.append(node)
        elif isinstance(node, ast.Lambda):
            found.append(as_def(node, named.get(id(node), "<lambda>")))
        elif is_partial(node):
            found.append(partial_as_def(node, named.get(id(node), "<partial>")))
    return found


def shape(node) -> str:
    """A def or class reduced to its live statements, every name and argument renamed in order of appearance,
    nested defs unnamed, decorators dropped: what a copy keeps however it is renamed or padded."""
    node = pruned(node)
    node.name = "_"
    node.decorator_list = []
    if hasattr(node, "returns"):
        node.returns = None                     # annotations are not behavior: a copy may drop or add them
    if not node.body:
        node.body = [ast.Pass()]
    names: dict = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
            sub.annotation = None
            sub.arg = names.setdefault(sub.arg, f"v{len(names)}")
        elif isinstance(sub, ast.Name):
            sub.id = names.setdefault(sub.id, f"v{len(names)}")
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            sub.name = names.setdefault(sub.name, f"v{len(names)}")
        elif isinstance(sub, DEFS) and sub is not node:
            sub.name = "_"
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def fingerprint(node) -> tuple:
    """(the names called, the constants used) of a def or class, each sorted, over its live statements: what a
    copy keeps when its statements are reordered, split or wrapped."""
    node = pruned(node)
    calls, consts = [], []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            calls.append(f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "?")
        elif isinstance(sub, ast.Constant):
            consts.append(repr(sub.value))
    return tuple(sorted(calls)), tuple(sorted(consts))


def reference_defs() -> list:
    """(origin, node) for the moved helpers as the tools carried them and as the lib defines them."""
    found = [("before", node) for node in ast.parse(REFERENCE_COPIES).body]
    for path in (CLI_PY, TEXT_PY):
        if path.is_file():
            found += [(path.name, node) for node in ast.parse(path.read_text(encoding="utf-8")).body
                      if isinstance(node, DEFS) and node.name in LIB_REFERENCE_NAMES]
    return [(origin, node) for origin, node in found if isinstance(node, DEFS)]


@functools.lru_cache(maxsize=None)
def reference_shapes() -> dict:
    shapes = {}
    for origin, node in reference_defs():
        shapes.setdefault(shape(node), f"{origin}:{node.name}")
    return shapes


@functools.lru_cache(maxsize=None)
def reference_fingerprints() -> dict:
    """The references with at least three calls and constants between them: an empty class matches nothing."""
    prints = {}
    for origin, node in reference_defs():
        calls, consts = fingerprint(node)
        if len(calls) + len(consts) >= 3:
            prints.setdefault((calls, consts), f"{origin}:{node.name}")
    return prints


def used_names(tree) -> set:
    """Names called, raised, caught, passed as a keyword value (type=...) or subclassed, in code that can run:
    inside a def or class or under the `__main__` guard; never in a dead branch, after a return, or at module
    level."""
    tree = pruned(tree)
    roots = [n for n in tree.body if isinstance(n, DEFS) or is_main_guard(n)]
    used = set()
    for root in roots:
        for node in ast.walk(root):
            targets = []
            if isinstance(node, ast.Call):
                targets.append(node.func)
                targets += [k.value for k in node.keywords]
            elif isinstance(node, ast.Raise) and node.exc is not None:
                targets.append(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
            elif isinstance(node, ast.ExceptHandler) and node.type is not None:
                targets += node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            elif isinstance(node, ast.ClassDef):
                targets += node.bases
            used.update(t.id for t in targets if isinstance(t, ast.Name))
    return used


def referenced_names(tree) -> set:
    """Every name loaded and attribute taken in live code, the module's assignments included (a command table
    counts as a reference)."""
    found = set()
    for node in ast.walk(pruned(tree)):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def live_tree(tree):
    """`tree` pruned to its live statements, once: a tree that came out of live_tree is returned as it is."""
    if getattr(tree, "_live", False):
        return tree
    tree = pruned(tree)
    tree._live = True
    return tree


def stmt_lists(tree) -> list:
    """Every list of statements in `tree` (module, def, class, loop, branch, try, handler and with bodies), over the
    live statements only."""
    found = []
    for node in ast.walk(live_tree(tree)):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
                found.append(stmts)
    return found


def stmts_shape(stmts: list) -> str:
    """The shape of a run of statements as the body of one argument-less function, names renamed in order of
    appearance: an inline body and the lib function it was lifted from have the same shape."""
    empty = ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
                          defaults=[])
    return shape(ast.FunctionDef(name="_", args=empty, body=copy.deepcopy(stmts), decorator_list=[], returns=None,
                                 type_comment=None))


@functools.lru_cache(maxsize=None)
def reference_windows() -> dict:
    """{shape of a lib function's whole body: (origin, number of statements, their types)} for the references with at least
    three calls and constants between them: what the same statements look like pasted inside another function."""
    windows = {}
    for origin, node in reference_defs():
        body = live(node.body) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if not body:
            continue
        calls, consts = fingerprint(ast.Module(body=copy.deepcopy(body), type_ignores=[]))
        if len(calls) + len(consts) >= 3:
            windows.setdefault(stmts_shape(body), (f"{origin}:{node.name}", len(body),
                                                   tuple(type(s).__name__ for s in body)))
    return windows


def window_findings(tree, windows: dict) -> list:
    """(line, origin) for each run of statements anywhere in `tree` shaped like the whole body of a reference."""
    sizes = sorted({n for _, n, _ in windows.values()})
    kinds = {kind for _, _, kind in windows.values()}       # statement types in order: cheap to compare first
    found = []
    for stmts in stmt_lists(tree):
        for n in sizes:
            for i in range(0, len(stmts) - n + 1):
                if tuple(type(s).__name__ for s in stmts[i:i + n]) not in kinds:
                    continue
                key = stmts_shape(stmts[i:i + n])
                if key in windows:
                    found.append((stmts[i].lineno, windows[key][0]))
    return found


def is_json_call(node, attr: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr \
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "json"


def call_name(node) -> str:
    """The last name of what a call calls: `open`, `write_text`, `read`; "" for anything else."""
    f = node.func if isinstance(node, ast.Call) else None
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def inline_read_findings(tree) -> list:
    """Lines of a try that reads a file (open, read_text, .read()) and parses it as JSON (json.load, json.loads)
    and whose handlers all swallow the error (none raises): read_json written out inside another function, however
    the file is opened and the result kept. A try that turns the error into a Problem is a different, reporting
    read and is not a copy."""
    found = []
    for node in ast.walk(live_tree(tree)):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        inside = [sub for stmt in node.body for sub in ast.walk(stmt)]
        reads = any(call_name(sub) in ("open", "read_text", "read") for sub in inside)
        parses = any(is_json_call(sub, "load") or is_json_call(sub, "loads") for sub in inside)
        raises = any(isinstance(sub, ast.Raise) for h in node.handlers for sub in ast.walk(h))
        if reads and parses and not raises:
            found.append(node.lineno)
    return found


def write_mode(call) -> bool:
    """Whether an open() call can write: a mode that is not a plain read constant (a variable counts)."""
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False
    return not (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and not set(mode.value) & set("wax+"))


def inline_write_findings(tree) -> list:
    """Lines that write a file by hand instead of through write_json or write_text, however the text is held:
    a temporary file renamed over the target (mkstemp, fdopen, os.replace), open() in a write mode, Path.write_text
    or write_bytes, json.dump, and json.dumps with indent or ensure_ascii (the pretty JSON write_json writes; emit
    is the lib's, so the tools print none)."""
    found = []
    for node in ast.walk(live_tree(tree)):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        base = node.func.value if isinstance(node.func, ast.Attribute) else None
        if name in ("mkstemp", "fdopen", "write_bytes", "NamedTemporaryFile") \
                or (name == "replace" and isinstance(base, ast.Name) and base.id == "os") \
                or (name == "write_text" and isinstance(node.func, ast.Attribute)) \
                or (name == "open" and write_mode(node)):
            found.append(node.lineno)
        elif is_json_call(node, "dump"):
            found.append(node.lineno)
        elif is_json_call(node, "dumps") and {k.arg for k in node.keywords} & {"indent", "ensure_ascii"}:
            found.append(node.lineno)
    return found


def stderr_warning_findings(tree) -> list:
    """Lines that print or write a `... warning: ...` line of their own: the lib's warn is the one spelling. The
    call's string pieces are read in source order and joined, so "warn" + "ing: " is a warning too."""
    found = []
    for node in ast.walk(live_tree(tree)):
        if isinstance(node, ast.Call) and (call_name(node) == "print" or call_name(node) == "write"):
            pieces = sorted((sub.lineno, sub.col_offset, sub.value) for sub in ast.walk(node)
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                            and hasattr(sub, "lineno"))
            if "warning" in "".join(p for _, _, p in pieces).lower():
                found.append(node.lineno)
    return found


ASCII = [chr(c) for c in range(128)]
SLUGGED = [not (ch.isascii() and ch.isalnum()) for ch in ASCII]   # what session_folder turns into a dash


def slug_pattern_findings(tree) -> list:
    """Lines of a string constant that, as a regular expression, matches exactly the ASCII characters that
    session_folder replaces (every non-alphanumeric): r"[^A-Za-z0-9]", r"[\\W_]" or r"\\W|_", however it is used
    (re.sub, a module-level re.compile, a functools.partial)."""
    found = []
    for node in ast.walk(live_tree(tree)):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or len(node.value) > 60 \
                or not set(node.value) & set("[\\|"):
            continue
        try:
            pattern = re.compile(node.value)
        except (re.error, RecursionError, OverflowError):
            continue
        if [pattern.fullmatch(ch) is not None for ch in ASCII] == SLUGGED:
            found.append(node.lineno)
    return found


def translate_findings(tree) -> list:
    """Lines that translate characters (str.translate, str.maketrans): session_folder written another way."""
    return [node.lineno for node in ast.walk(live_tree(tree))
            if isinstance(node, ast.Call) and call_name(node) in ("translate", "maketrans")]


def copy_findings(tree) -> list:
    """Everything in `tree` that is a copy of a cli.py or text.py export: a def, class or lambda shaped like one
    or calling what one calls on the same constants, a run of statements shaped like one's whole body, a file read
    or written the way read_json, write_json or write_text do, a warning line of its own. One string per finding."""
    shapes, prints, windows = reference_shapes(), reference_fingerprints(), reference_windows()
    tree = live_tree(tree)
    found = []
    for node in defs_in(tree):
        if shape(node) in shapes:
            found.append(f"{node.name}:{node.lineno} is a copy of {shapes[shape(node)]}")
        elif fingerprint(node) in prints:
            found.append(f"{node.name}:{node.lineno} calls what {prints[fingerprint(node)]} calls")
    found += [f"line {line}: statements shaped like the body of {origin}" for line, origin in
              window_findings(tree, windows)]
    found += [f"line {line}: an inline read_json" for line in inline_read_findings(tree)]
    found += [f"line {line}: a file written by hand (write_json and write_text do that)"
              for line in inline_write_findings(tree)]
    found += [f"line {line}: a warning line of its own (the lib's warn)" for line in stderr_warning_findings(tree)]
    found += [f"line {line}: session_folder's pattern (every non-alphanumeric)" for line in slug_pattern_findings(tree)]
    found += [f"line {line}: a character translation (session_folder does that)" for line in translate_findings(tree)]
    return found


@functools.lru_cache(maxsize=None)
def tool_findings(tool: str) -> tuple:
    return tuple(copy_findings(ast.parse((BIN / tool).read_text(encoding="utf-8"))))


class ToolsAdoptTheLib(unittest.TestCase):
    """The source of harness-board and harness-pr: no copy of a moved helper under any name or disguise, every
    lib name imported and used from code that runs, no dead code to hide a use in, the run_main tail, and the
    names bound are the lib's objects."""

    def tree(self, tool: str):
        return ast.parse((BIN / tool).read_text(encoding="utf-8"))

    def test_board_and_pr_define_none_of_the_moved_helpers(self):
        for tool in TOOLS:
            defined = top_level_names(BIN / tool)
            for name in sorted(set(MOVED) | set(EXPORTS)):
                with self.subTest(tool=tool, name=name):
                    self.assertNotIn(name, defined, f"{tool} still defines {name}")

    def test_no_def_class_or_lambda_in_board_or_pr_is_a_moved_helper_under_any_name(self):
        """The whole tree, nested blocks, methods and lambdas included: nothing named like a moved helper, nothing
        shaped like one (today's copies or the lib's own; names, docstrings, padding and dead code aside), and
        nothing that calls the same names on the same constants as one."""
        shapes, prints = reference_shapes(), reference_fingerprints()
        self.assertGreaterEqual(len(shapes), 16)
        self.assertGreaterEqual(len(prints), 12)
        for tool in TOOLS:
            for node in defs_in(self.tree(tool)):
                with self.subTest(tool=tool, name=node.name, line=node.lineno):
                    if node.name not in LEGIT_NAMES:
                        for name in MOVED:
                            self.assertNotIn(name, node.name, f"{tool}:{node.lineno} {node.name} is named for {name}")
                    found = shapes.get(shape(node))
                    self.assertIsNone(found, f"{tool}:{node.lineno} {node.name} is a copy of {found}")
                    found = prints.get(fingerprint(node))
                    self.assertIsNone(found, f"{tool}:{node.lineno} {node.name} calls what {found} calls on the "
                                             "same constants")

    def test_board_and_pr_subclass_neither_argument_parser_nor_exception(self):
        """One Parser and one Problem: the tools define no parser or exception class of their own."""
        for tool in TOOLS:
            for node in defs_in(self.tree(tool)):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    with self.subTest(tool=tool, cls=node.name, base=ast.unparse(base)):
                        text = ast.unparse(base)
                        self.assertNotIn("ArgumentParser", text)
                        self.assertNotRegex(text, r"(Exception|Error)$")

    def test_each_name_taken_from_the_lib_is_used_from_code_that_runs(self):
        """Called, raised, caught or passed inside a function, a class or the `__main__` guard: not under
        `if False:`, not after a return, not at module level."""
        for tool in TOOLS:
            used = used_names(self.tree(tool))
            for name in sorted(USED[tool]) + ["run_main"]:
                with self.subTest(tool=tool, name=name):
                    self.assertIn(name, used, f"{tool} imports {name} but no code that runs calls, raises or passes it")

    def test_board_and_pr_carry_no_dead_code(self):
        """No `if` on a constant, no `while` on a false constant, nothing after a return, raise, break or
        continue, no bare expression at module level but the docstring, and no module-level call of a function
        the tool defines: a helper is used from inside functions, or it is not used."""
        for tool in TOOLS:
            tree = self.tree(tool)
            defined = {n.name for n in tree.body if isinstance(n, DEFS)}
            with self.subTest(tool=tool, rule="module level"):
                exprs = [s.lineno for s in tree.body[1:] if isinstance(s, ast.Expr)]
                self.assertEqual(exprs, [], f"{tool}: bare expressions at module level, lines {exprs}")
                calls = sorted(f"{n.func.id}:{n.lineno}" for s in tree.body if not isinstance(s, DEFS)
                               and not is_main_guard(s) for n in ast.walk(s)
                               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in defined)
                self.assertEqual(calls, [], f"{tool}: module-level calls of its own functions: {calls}")
            for node in ast.walk(tree):
                if isinstance(node, (ast.If, ast.While)) and not is_main_guard(node):
                    truth = constant_truth(node.test)
                    if truth is not None and not (isinstance(node, ast.While) and truth):
                        with self.subTest(tool=tool, line=node.lineno):
                            self.fail(f"{tool}:{node.lineno} {type(node).__name__} on a constant")
                for field in ("body", "orelse", "finalbody"):
                    stmts = getattr(node, field, None)
                    if isinstance(stmts, list):
                        for s in stmts[:-1]:
                            if isinstance(s, ENDS_BLOCK):
                                with self.subTest(tool=tool, line=s.lineno):
                                    self.fail(f"{tool}:{s.lineno} code after {type(s).__name__}")

    def test_every_function_board_and_pr_define_is_referenced_from_live_code(self):
        """A def nothing references is a dead copy or a leftover: none beyond the one leftover of #79."""
        for tool in TOOLS:
            tree = self.tree(tool)
            defined = {n.name for n in ast.walk(pruned(tree)) if isinstance(n, DEFS) and not n.name.startswith("__")}
            unused = sorted(defined - referenced_names(tree) - UNREFERENCED_TODAY)
            with self.subTest(tool=tool):
                self.assertEqual(unused, [], f"{tool} defines {unused} and nothing that runs references them")

    def test_board_and_pr_import_from_cli_and_text(self):
        for tool in TOOLS:
            tree = self.tree(tool)
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in ("harness_lib.cli", "harness_lib.text"):
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.module.split(".")[1]
            for name, module in sorted(USED[tool].items()) + [("run_main", "cli")]:
                with self.subTest(tool=tool, name=name):
                    self.assertEqual(imported.get(name), module, f"{tool} does not import {name} from {module}")

    def test_the_main_tail_is_run_main_with_the_tool_name(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                tails = [n for n in self.tree(tool).body if is_main_guard(n)]
                self.assertEqual(len(tails), 1)
                self.assertEqual([ast.unparse(s) for s in tails[0].body], [f"run_main('{tool}', main)"])

    def test_the_names_a_tool_binds_are_the_lib_objects(self):
        libs = {"cli": lib_module("cli"), "text": lib_module("text")}
        for tool in TOOLS:
            module = load_tool(BIN / tool)
            for name, where in sorted(USED[tool].items()) + [("run_main", "cli")]:
                with self.subTest(tool=tool, name=name):
                    self.assertIs(getattr(module, name, None), getattr(libs[where], name))


class ToolsHaveNoCopyOfAnyExport(unittest.TestCase):
    """The whole of cli.py and text.py is the reference, not just what #82 moved: read_json, harness_home,
    same_path, session_folder, parse_iso_seconds, age_text, run_main, warn and the rest are checked by shape, by
    what they call, as a run of statements pasted inside another function, and as a file read or written by
    hand."""

    def test_every_public_def_of_cli_and_text_is_a_reference(self):
        """A new export of either module must be added to the copy check: it cannot be left out silently."""
        for path in (CLI_PY, TEXT_PY):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            public = {n.name for n in tree.body if isinstance(n, DEFS) and not n.name.startswith("_")}
            with self.subTest(file=path.name):
                self.assertLessEqual(public, set(EXPORTS), f"{path.name} exports {sorted(public - set(EXPORTS))} "
                                                           "that the copy check does not know")
        referenced = {node.name for origin, node in reference_defs() if origin != "before"}
        self.assertEqual(sorted(set(EXPORTS) - referenced), [])

    def test_board_and_pr_contain_no_copy_of_any_export_under_any_name_or_inline(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(list(tool_findings(tool)), [], f"{tool} carries a copy of a harness_lib function")

    def enclosing_calls(self, tool: str, name: str) -> list:
        """The names of the functions in `tool` that call `name(...)`, one entry per call."""
        tree = ast.parse((BIN / tool).read_text(encoding="utf-8"))
        found = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found += [fn.name for sub in ast.walk(fn) if isinstance(sub, ast.Call)
                          and isinstance(sub.func, ast.Name) and sub.func.id == name]
        return found

    def test_write_text_is_for_the_ultrareview_output_only_and_the_budgets_file_is_write_json(self):
        """Pretty JSON and a newline is write_json (the owner's note on the #82 review: the budgets file is written by
        write_json with the same bytes); write_text is for text that is not JSON. However the JSON text is held
        (a variable, +=, an f-string), the only write_text call in either tool is cmd_ultra's, and the only
        write_json call in harness-pr is mark_cloud_exhausted's."""
        self.assertEqual(self.enclosing_calls("harness-board", "write_text"), [])
        self.assertEqual(self.enclosing_calls("harness-pr", "write_text"), ["cmd_ultra"])
        self.assertEqual(self.enclosing_calls("harness-pr", "write_json"), ["mark_cloud_exhausted"])

    def test_board_and_pr_write_no_file_by_hand(self):
        """No write-mode open, Path.write_text, mkstemp, os.replace, json.dump or indented json.dumps in either
        tool: files are written by the lib, and the tools have no write that could stand in for it."""
        for tool in TOOLS:
            tree = ast.parse((BIN / tool).read_text(encoding="utf-8"))
            with self.subTest(tool=tool):
                self.assertEqual(inline_write_findings(tree), [])


# Where each tool touches stderr itself today (sys.stderr, by enclosing function, with the count): the lines that
# are not warnings. harness-board: `project check`'s missing-options line and `project sync`'s two failure lines;
# harness-pr: board()'s pass-through of harness-board's warnings (a write and a flush) and the failed-checks line.
STDERR_TODAY = {
    "harness-board": {"cmd_project_check": 1, "cmd_project_sync": 2},
    "harness-pr": {"board": 2, "cmd_checks": 1},
}
BAD_PROGS = ("harness_pr", "harness-pr:", "harness-pr ", "harness-board", "harness_board", "Harness-pr", "pr", "")


def warn_call_is_spelt(call, tool: str) -> bool:
    """`warn("<tool>", message)`: the constant tool name first, exactly two positional arguments, no keywords."""
    return len(call.args) == 2 and not call.keywords and not any(isinstance(a, ast.Starred) for a in call.args) \
        and isinstance(call.args[0], ast.Constant) and call.args[0].value == tool


def warn_spelling_findings(tree, tool: str) -> list:
    """Every use of `warn` in a tool is a call spelt as warn_call_is_spelt says. `warn` passed, aliased, wrapped in
    a partial or rebound is a finding too: then a call site could spell the prog name any way."""
    names, calls = [], {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == "warn":
            names.append(n)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "warn":
            calls[id(n.func)] = n
    found = []
    for node in names:
        call = calls.get(id(node))
        if call is None:
            found.append(f"line {node.lineno}: warn used other than called")
        elif not warn_call_is_spelt(call, tool):
            found.append(f"line {call.lineno}: {ast.unparse(call)[:80]} is not warn({tool!r}, message)")
    return found


def warn_call_sites(tree) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "warn"]


def stderr_uses(tree) -> dict:
    """{enclosing top-level-or-nested function name (or "<module>"): count} of the places a tool reaches stderr by
    itself: sys.stderr or sys.__stderr__, getattr on sys, `from sys import`, os.write, /dev/stderr or /dev/fd/2."""
    counts: dict = {}

    def visit(node, where):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, DEFS) else where
            hit = (isinstance(child, ast.Attribute) and child.attr in ("stderr", "__stderr__")
                   and isinstance(child.value, ast.Name) and child.value.id == "sys") \
                or (isinstance(child, ast.ImportFrom) and child.module == "sys") \
                or (isinstance(child, ast.Call) and call_name(child) == "getattr" and child.args
                    and isinstance(child.args[0], ast.Name) and child.args[0].id == "sys") \
                or (isinstance(child, ast.Call) and call_name(child) == "write" and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == "os") \
                or (isinstance(child, ast.Constant) and child.value in ("/dev/stderr", "/dev/fd/2"))
            if hit:
                counts[inner] = counts.get(inner, 0) + 1
            visit(child, inner)

    visit(tree, "<module>")       # dead code included: the tools carry none (test_board_and_pr_carry_no_dead_code)
    return counts


class ToolsWarnAndReachStderrOnlyTheirOwnWay(unittest.TestCase):
    """Every warning a tool gives is the lib's warn with the tool's own name, at every call site (not only the two
    the run tests drive), and the tool writes to stderr by itself only where it does today: a warning assembled
    from pieces anywhere else has nowhere to hide."""

    def tree(self, tool: str):
        return ast.parse((BIN / tool).read_text(encoding="utf-8"))

    def test_every_warn_call_in_board_and_pr_names_the_tool(self):
        for tool in TOOLS:
            tree = self.tree(tool)
            with self.subTest(tool=tool):
                self.assertGreaterEqual(len(warn_call_sites(tree)), 5, f"{tool} gives fewer warnings than it does today")
                self.assertEqual(warn_spelling_findings(tree, tool), [])

    def test_a_misspelt_prog_at_any_one_warn_call_site_is_found(self):
        """Each call site in turn, in an in-memory copy of the tool, gets each wrong spelling: the site's call is
        refused, and the whole-tool check finds exactly that one site (the first, a middle and the last site run
        through the whole check; every site through the per-call rule)."""
        for tool in TOOLS:
            tree = self.tree(tool)
            sites = warn_call_sites(tree)
            whole = {0, len(sites) // 2, len(sites) - 1}
            for i, call in enumerate(sites):
                original = call.args[0]
                for bad in BAD_PROGS:
                    if bad == tool:
                        continue
                    with self.subTest(tool=tool, site=i, line=call.lineno, prog=bad):
                        call.args[0] = ast.Constant(bad)
                        try:
                            self.assertFalse(warn_call_is_spelt(call, tool))
                            if i in whole:
                                found = warn_spelling_findings(tree, tool)
                                self.assertEqual(len(found), 1, found)
                                self.assertIn(f"line {call.lineno}:", found[0])
                        finally:
                            call.args[0] = original
                self.assertTrue(warn_call_is_spelt(call, tool), f"{tool}:{call.lineno}")

    def test_warn_rebound_wrapped_or_given_a_variable_prog_is_found(self):
        plants = (
            'say = warn\n\ndef f():\n    say("harness_x", "m")\n',
            'say = functools.partial(warn, "{tool}")\n',
            'PROG = "{tool}"\n\ndef f():\n    warn(PROG, "m")\n',
            'def f():\n    warn("{tool}", "m", file=sys.stdout)\n',
            'def f():\n    warn("{tool}", "a", "b")\n',
            'def f():\n    warn(*("{tool}", "m"))\n',
            'def f():\n    warn(f"{tool}", "m")\n',
        )
        for tool in TOOLS:
            source = (BIN / tool).read_text(encoding="utf-8")
            for plant in plants:
                with self.subTest(tool=tool, plant=plant):
                    tree = ast.parse(source + "\n\n" + plant.replace("{tool}", tool))
                    self.assertNotEqual(warn_spelling_findings(tree, tool), [])

    def test_board_and_pr_reach_stderr_by_themselves_only_where_they_do_today(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(stderr_uses(self.tree(tool)), STDERR_TODAY[tool])

    def test_a_stderr_write_added_anywhere_is_found(self):
        """A warn built from pieces in a new function, at module level, or one more stderr line inside a function
        that already writes there: each changes the inventory."""
        plants = (
            'def caution(tool, msg):\n    print(tool + ": " + "warn" + "ing: " + msg, file=sys.stderr)\n',
            'caution = functools.partial(print, file=sys.stderr)\n',
            'from sys import stderr as err\n',
            'def caution(msg):\n    print(msg, file=getattr(sys, "stderr"))\n',
            'def caution(msg):\n    os.write(2, msg.encode())\n',
            'def caution(msg):\n    with open("/dev/stderr", "a") as fh:\n        fh.write(msg)\n',
        )
        for tool in TOOLS:
            source = (BIN / tool).read_text(encoding="utf-8")
            for plant in plants:
                with self.subTest(tool=tool, plant=plant):
                    self.assertNotEqual(stderr_uses(ast.parse(source + "\n\n" + plant)), STDERR_TODAY[tool])
            for where in STDERR_TODAY[tool]:
                with self.subTest(tool=tool, inside=where):
                    tree = self.tree(tool)
                    fn = next(n for n in ast.walk(tree) if isinstance(n, DEFS) and n.name == where)
                    fn.body.insert(0, ast.parse('print("harness-x: warn" "ing: m", file=sys.stderr)').body[0])
                    self.assertNotEqual(stderr_uses(tree), STDERR_TODAY[tool])


# Renamed, independently written copies of every export, as a tool might paste them: names, parameters and
# locals changed, annotations and docstrings dropped.
PLANTED_DEFS = {
    "Problem": r'''
class Trouble(Exception):
    """a failed precondition"""
''',
    "Parser": r'''
class Options(argparse.ArgumentParser):
    def error(self, msg):
        sys.exit(f"{self.prog}: error: {msg}")
''',
    "run_main": r'''
def execute(label, entry):
    try:
        entry()
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
    except Trouble as e:
        sys.exit(f"{label}: {squash(e)}")
    except Exception as e:
        sys.exit(f"{label}: {type(e).__name__}: {squash(e)}")
    sys.exit(0)
''',
    "emit": r'''
def show(flag, payload, words):
    print(json.dumps(payload, indent=2, ensure_ascii=False) if flag else words)
''',
    "table": r'''
def grid(lines):
    cols = [max(len(l[k]) for l in lines) for k in range(len(lines[0]) - 1)]
    return "\n".join("  ".join([c.ljust(w) for c, w in zip(l, cols)] + [l[-1]]).rstrip() for l in lines)
''',
    "issue_number": r'''
def ticket_arg(raw):
    num = raw.lstrip("#")
    if not num.isdigit() or int(num) <= 0:
        raise argparse.ArgumentTypeError(f"not an issue number: {raw!r}")
    return int(num)
''',
    "warn": r'''
def caution(tool, msg):
    print(f"{tool}: warning: {squash(msg)}", file=sys.stderr)
''',
    "one_line": r'''
def squash(value):
    return " ".join(str(value).split())
''',
    "first_line": r'''
def head_line(blob):
    for row in (blob or "").splitlines():
        if row.strip():
            return row.strip()
    return ""
''',
    "last_line": r'''
def tail_line(blob):
    rows = [row.strip() for row in (blob or "").splitlines() if row.strip()]
    return rows[-1] if rows else ""
''',
    "as_list": r'''
def records(val):
    if isinstance(val, dict):
        val = val.get("nodes")
    return [v for v in val if isinstance(v, dict)] if isinstance(val, list) else []
''',
    "read_text": r'''
def slurp(where, label):
    try:
        with open(os.path.expanduser(where), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as e:
        raise Trouble(f"cannot read {label} {where}: {e.strerror or e}") from None
''',
    "read_json": r'''
def load_blob(where):
    try:
        with open(where, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
''',
    "write_json": r'''
def save_blob(where, blob, *, pre):
    def dump(handle):
        json.dump(blob, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    _swap(where, pre, ".json", dump)
''',
    "write_text": r'''
def save_text(where, body, *, pre):
    _swap(where, pre, "", lambda handle: handle.write(body))
''',
    "home": r'''
def under_home(*bits):
    return os.path.join(os.path.expanduser("~"), *bits)
''',
    "harness_home": r'''
def state_path(*bits):
    return under_home(".claude", "harness", *bits)
''',
    "now_iso": r'''
def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
''',
    "parse_iso": r'''
def to_datetime(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
''',
    "parse_iso_seconds": r'''
def to_epoch(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.astimezone()).timestamp()
''',
    "age_text": r'''
def ago(past, present):
    secs = max(0, present - past)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 60:
        return f"{int(secs // 86400)}d ago"
    if secs < 86400 * 730:
        return f"{int(secs // (86400 * 30))}mo ago"
    return f"{int(secs // (86400 * 365))}y ago"
''',
    "same_path": r'''
def same_place(x, y):
    return os.path.realpath(os.path.abspath(os.path.expanduser(x))) == os.path.realpath(
        os.path.abspath(os.path.expanduser(y)))
''',
    "session_folder": r'''
def folder_slug(where):
    return re.sub(r"[^A-Za-z0-9]", "-", where)
''',
}
# Statements lifted out of an export and pasted inside another function, and the same job written out by hand.
PLANTED_INLINE = {
    "read_json, result kept in a variable": r'''
def settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    return data
''',
    "read_json, one handler for each error": r'''
def settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        data = {}
    except ValueError:
        data = {}
    return data
''',
    "read_json body verbatim in a branch": r'''
def settings(use_file):
    if use_file:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return {}
''',
    "one_line body verbatim": r'''
def describe(err):
    print("failed")
    return " ".join(str(err).split())
''',
    "first_line body verbatim": r'''
def title(text):
    seen = 0
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
''',
    "write_json spelled out for write_text": r'''
def save(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", prefix=".mine-")
''',
    "json.dump to a handle": r'''
def save(handle, data):
    json.dump(data, handle, indent=2, ensure_ascii=False)
''',
    "temporary file renamed over the target": r'''
def save(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
''',
    "pretty JSON held in a variable, newline added with +=, written with open": r'''
def save(path, data):
    body = json.dumps(data, indent=2, ensure_ascii=False)
    body += "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
''',
    "pretty JSON in an f-string": r'''
def save(path, data):
    body = f"{json.dumps(data, indent=2)}\n"
    write_text(path, body, prefix=".x-")
''',
    "indent only": r'''
def save(path, data):
    write_text(path, json.dumps(data, indent=2), prefix=".x-")
''',
    "Path.write_text": r'''
def save(path, data):
    Path(path).write_text(str(data), encoding="utf-8")
''',
    "open for append, mode as a keyword": r'''
def save(path, line):
    with open(path, mode="a") as fh:
        fh.write(line)
''',
    "open with a mode held in a variable": r'''
def save(path, line, how):
    with open(path, how) as fh:
        fh.write(line)
''',
    "read_json with Path.read_text and json.loads": r'''
def settings(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
''',
    "read_json with read() and json.loads, result kept": r'''
def settings(path):
    data = None
    try:
        fh = open(path, encoding="utf-8")
        data = json.loads(fh.read())
    except Exception:
        pass
    return data
''',
    "a warning line of its own": r'''
def complain(message):
    print(f"harness-x: warning: {message}", file=sys.stderr)
''',
    "a warning line written to stderr": r'''
def complain(message):
    sys.stderr.write("warning: " + message + "\n")
''',
    "a warning line built from string pieces, printed to stderr": r'''
def caution(tool, msg):
    print(tool + ": " + "warn" + "ing: " + " ".join(str(msg).split()), file=sys.stderr)
''',
    "a warning line built from f-string pieces, written to stderr": r'''
def caution(tool, msg):
    sys.stderr.write(f"{tool}: WARN" f"ING: {msg}" + "\n")
''',
    "session_folder through str.translate": r'''
def folder_slug(where):
    return where.translate({c: "-" for c in range(128) if not chr(c).isalnum()})
''',
    "session_folder through str.maketrans": r'''
SLUG = str.maketrans(dict.fromkeys(string.punctuation + " ", "-"))
''',
    "session_folder through a module-level compiled pattern": r'''
SESS_RE = re.compile(r"[^A-Za-z0-9]")


def folder_slug(where):
    return SESS_RE.sub("-", where)
''',
    "session_folder with the pattern spelled [\\W_]": r'''
def folder_slug(where):
    return re.sub(r"[\W_]", "-", where)
''',
    "session_folder with the pattern spelled \\W|_ in a constant": r'''
NON_ALNUM = r"\W|_"
''',
    "session_folder as a module-level functools.partial": r'''
folder_slug = functools.partial(re.sub, r"[^A-Za-z0-9]", "-")
''',
    "home as a module-level functools.partial": r'''
under_home = functools.partial(os.path.join, os.path.expanduser("~"))
''',
    "harness_home as a partial imported from functools, inside a function": r'''
def state(*bits):
    state_path = partial(home, ".claude", "harness")
    return state_path(*bits)
''',
}
# Reads and writes that are not copies of an export: they report, or are not what the exports do.
NOT_COPIES = {
    "a read that reports a bad file as a Problem": r'''
def load_projects():
    path = harness_home("projects.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except ValueError as err:
        raise Problem(f"{path} is not valid JSON ({err})") from None
    except OSError as err:
        raise Problem(f"cannot read {path}: {err.strerror or err}") from None
    return data["projects"]
''',
    "json.dumps on one line for gh": r'''
def post(call, payload):
    return call(stdin=json.dumps(payload))
''',
    "print of compact JSON": r'''
def show(data):
    print(json.dumps(data, sort_keys=True))
''',
    "a read-mode open, mode given": r'''
def first_bytes(path):
    with open(path, "rb") as fh:
        return fh.read(4)
''',
    "a read-mode open with encoding only": r'''
def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
''',
    "a JSON parse of text already in hand": r'''
def parsed(out):
    try:
        return json.loads(out)
    except ValueError:
        return None
''',
    "a stderr line that is not a warning": r'''
def failed(number, names):
    sys.stderr.write(f"harness-pr: checks failed on PR #{number}: {', '.join(names)}\n")
''',
    "a plain function": r'''
def ticket(number):
    return {"number": number, "title": "x" * number}
''',
    "a pattern that keeps the dash": r'''
def slug(name):
    return re.sub(r"[^A-Za-z0-9-]", "-", name)
''',
    "a pattern of the alphanumerics themselves": r'''
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
''',
    "a pattern of the non-word characters (the underscore kept)": r'''
def words(text):
    return re.split(r"\W+", text)
''',
    "a functools.partial of something no export does": r'''
run = functools.partial(subprocess.run, capture_output=True, text=True)
''',
}


class CopyChecksCatchPlants(unittest.TestCase):
    """The copy check can fail: a renamed copy of any export, in a tool-shaped source, is found; so is the same
    code nested in a function, and an inline body. Code that merely resembles one is left alone."""

    def test_the_plants_cover_every_export(self):
        self.assertEqual(sorted(PLANTED_DEFS), sorted(EXPORTS))

    def test_a_renamed_copy_of_each_export_is_found(self):
        for name, source in PLANTED_DEFS.items():
            with self.subTest(export=name):
                self.assertNotEqual(copy_findings(ast.parse(source)), [], f"a renamed {name} was not found")

    def test_a_renamed_copy_nested_in_a_function_or_class_is_found(self):
        for name, source in PLANTED_DEFS.items():
            for outer in ("def outer():\n", "class Outer:\n"):
                with self.subTest(export=name, inside=outer.split()[0]):
                    nested = outer + textwrap.indent(source, "    ")
                    self.assertNotEqual(copy_findings(ast.parse(nested)), [])

    def test_a_copy_padded_with_dead_code_and_annotations_is_found(self):
        padded = r'''
def read_it(where: str) -> object:
    """Read the file."""
    if False:
        print("never")
    try:
        with open(where, encoding="utf-8") as handle:
            return json.load(handle)
        print("after the return")
    except (OSError, ValueError):
        return None
    pass
'''
        self.assertNotEqual(copy_findings(ast.parse(padded)), [])

    def test_an_inline_body_is_found(self):
        for what, source in PLANTED_INLINE.items():
            with self.subTest(what=what):
                self.assertNotEqual(copy_findings(ast.parse(source)), [], what)

    def test_code_that_only_resembles_an_export_is_not_a_copy(self):
        for what, source in NOT_COPIES.items():
            with self.subTest(what=what):
                self.assertEqual(copy_findings(ast.parse(source)), [], what)

    def test_a_tool_with_a_planted_copy_would_fail_the_tool_check(self):
        """The real tools' source plus one planted copy is flagged, with the finding naming the plant."""
        for tool in TOOLS:
            source = (BIN / tool).read_text(encoding="utf-8")
            before = len(tool_findings(tool))
            for export in ("read_json", "age_text"):
                with self.subTest(tool=tool, export=export):
                    planted = ast.parse(source + "\n\n" + PLANTED_DEFS[export])
                    self.assertGreater(len(copy_findings(planted)), before,
                                       f"{tool} + a renamed {export} passed the copy check")


class ToolsRunTheLib(unittest.TestCase):
    """The tools' own code paths reach the lib's objects. In a freshly loaded tool, every global bound to a lib
    function is replaced by a recorder that delegates to it; then a tool function that needs that helper runs on
    inputs whose right answer depends on it, and the recorder must have fired. A private copy under another name
    leaves the recorder silent. At least one such test per lib name each tool takes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = lib_module("cli")
        cls.text = lib_module("text")

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-tools-lib-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls: list = []

    def spy(self, module, lib_obj, label: str, required: bool = True) -> None:
        """Every global of `module` bound to `lib_obj` records its calls as (label, args, kwargs)."""
        bound = [name for name, value in vars(module).items() if value is lib_obj]
        if required:
            self.assertTrue(bound, f"{module.__name__} binds no name to the lib's {label}")
        calls = self.calls

        def recorder(*args, **kwargs):
            calls.append((label, args, kwargs))
            return lib_obj(*args, **kwargs)

        for name in bound:
            setattr(module, name, recorder)

    def called(self, label: str) -> list:
        return [(args, kwargs) for name, args, kwargs in self.calls if name == label]

    def given(self, label: str) -> list:
        """The first argument of each recorded call of `label`, in order."""
        return [args[0] for args, _ in self.called(label)]

    def homed(self) -> int:
        """Calls that went to the lib for a path under HOME: home, or harness_home for ~/.claude/harness/..."""
        return len(self.called("home")) + len(self.called("harness_home"))

    def put(self, rel: str, text: str) -> str:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def fake_exe(self, name: str, script: str) -> str:
        """A small sh executable in the temporary folder."""
        path = self.put(os.path.join("fakebin", name), "#!/bin/sh\n" + textwrap.dedent(script))
        os.chmod(path, 0o755)
        return path

    # ---- both tools: Problem

    def test_board_and_pr_raise_and_catch_the_one_problem(self):
        """A precondition failure in either tool is the lib's Problem, and each tool's `except Problem` catches an
        instance of the lib's class."""
        board, pr = load_tool(BIN / "harness-board"), load_tool(BIN / "harness-pr")
        missing = os.path.join(self.tmp, "nowhere")
        cases = [
            ("harness-board", lambda: board.open_repo(missing), f"not a directory: {missing}"),
            ("harness-board", lambda: board.blocker_numbers(["3,x"]), "--blocked-by: not an issue number: 'x'"),
            ("harness-board", lambda: board.gh_json(None, ["issue", "view", "1"]),
             "gh issue view 1 printed something other than JSON: nope"),
            ("harness-pr", lambda: pr.open_repo(missing), f"not a directory: {missing}"),
            ("harness-pr", lambda: pr.sibling("harness-no-such-tool"), None),
            ("harness-pr", lambda: pr.load_findings(os.path.join(self.tmp, "none.json")), None),
        ]
        board.gh = lambda repo, args, stdin=None: "nope\n"
        for tool, call, message in cases:
            with self.subTest(tool=tool, message=message):
                with self.assertRaises(self.cli.Problem) as ctx:
                    call()
                if message:
                    self.assertEqual(str(ctx.exception), message)
        # caught: an instance of the lib's class, raised by a call the tool guards
        board = load_tool(BIN / "harness-board")

        def failing(repo, args):
            raise self.cli.Problem("gh issue view 3 failed: HTTP 404")

        board.gh_json = failing
        self.assertEqual(board.task_resolutions(None, [{"number": 3, "state": "CLOSED"}]), {})
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ""}):
                os.environ.pop("CLAUDE_PROJECT_DIR")
                with self.assertRaises(self.cli.Problem) as ctx:
                    pr.open_repo()
        finally:
            os.chdir(cwd)
        self.assertEqual(str(ctx.exception),
                         "not in a git repository (run it from the project or its ticket worktree)")

    # ---- harness-board

    def test_board_builds_its_parser_from_the_lib_with_issue_number(self):
        board = load_tool(BIN / "harness-board")
        ap = board.parser()
        self.assertIs(type(ap), self.cli.Parser)
        self.spy(board, self.cli.issue_number, "issue_number")
        for given in ("#5", "17"):
            board.parser().parse_args(["show", given])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            board.parser().parse_args(["show", "zero"])
        self.assertEqual(self.given("issue_number"), ["#5", "17", "zero"])

    def test_board_warns_through_the_lib_with_its_own_spelling_byte_for_byte(self):
        value = "acme/x y"
        message = f"HARNESS_PROJECT={value!r} is not OWNER/NUMBER; the Projects board is not updated"
        board = load_tool(BIN / "harness-board")
        with mock.patch.dict(os.environ, {"HARNESS_PROJECT": value, "HOME": self.tmp}):
            got, out, err = capture(board.project_setting)
        self.assertIsNone(got)
        self.assertEqual(out, "")
        self.assertEqual(err, f"harness-board: warning: {message}\n")
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.cli.warn, "warn")
        with mock.patch.dict(os.environ, {"HARNESS_PROJECT": value, "HOME": self.tmp}):
            capture(board.project_setting)
        self.assertEqual([args for args, _ in self.called("warn")], [("harness-board", message)])

    def test_board_emits_through_the_lib_with_a_bool(self):
        for as_json in (True, False):
            with self.subTest(as_json=as_json):
                self.calls.clear()
                board = load_tool(BIN / "harness-board")
                board.gh = lambda *args, **kwargs: ""
                self.spy(board, self.cli.emit, "emit")
                _, out, _ = capture(board.cmd_labels_ensure, None, argparse.Namespace(json=as_json))
                calls = self.called("emit")
                self.assertEqual(len(calls), 1, calls)
                args, _ = calls[0]
                self.assertIs(args[0], as_json)
                self.assertEqual(args[1]["count"], len(board.LABELS))
                if as_json:
                    self.assertEqual(json.loads(out)["labels"], [label[0] for label in board.LABELS])
                else:
                    self.assertEqual(out, f"{len(board.LABELS)} labels created or updated.\n")

    def test_board_ready_reads_lists_and_prints_a_table_through_the_lib(self):
        items = [
            {"number": 5, "title": "Beta", "labels": [{"name": "task"}, {"name": "status:ready"}],
             "blockedBy": {"nodes": [{"state": "CLOSED"}]}},
            {"number": 3, "title": "Alpha", "labels": [{"name": "bug"}, {"name": "status:ready"}],
             "blockedBy": []},
            {"number": 9, "title": "Waiting", "labels": [{"name": "task"}, {"name": "status:ready"}],
             "blockedBy": {"nodes": [{"state": "OPEN"}]}},
        ]
        board = load_tool(BIN / "harness-board")
        board.gh_json = lambda *args, **kwargs: copy.deepcopy(items)
        self.spy(board, self.cli.table, "table")
        self.spy(board, self.text.as_list, "as_list")
        _, out, _ = capture(board.cmd_ready, None, argparse.Namespace(json=False))
        self.assertEqual(len(self.called("table")), 1)
        self.assertGreaterEqual(len(self.called("as_list")), 3)
        self.assertIn("Alpha", out)
        self.assertIn("Beta", out)
        self.assertNotIn("Waiting", out)
        self.assertLess(out.index("Alpha"), out.index("Beta"))

    def test_board_writes_its_cache_through_the_lib(self):
        board = load_tool(BIN / "harness-board")
        folder = os.path.join(self.tmp, "repos", "k", "tickets")
        repo = argparse.Namespace(ticket_path=lambda number: os.path.join(folder, f"{number}.json"))
        self.spy(board, self.text.write_json, "write_json")
        for number in (12, 40):
            board.save_record(repo, {"number": number, "title": f"T{number} — café"})
        self.assertEqual(self.given("write_json"), [os.path.join(folder, "12.json"), os.path.join(folder, "40.json")])
        with open(os.path.join(folder, "40.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["title"], "T40 — café")

    def test_board_reads_a_comment_body_file_through_the_lib(self):
        """cmd_comment --body-file: read_text reads it, the comment reaches gh with the marker, first_line picks
        the URL gh printed, emit prints it as text or JSON."""
        path = self.put("notes/body.md", "Fixed — see the PR.\n\nDetails here.\n")
        url = "https://github.com/o/r/issues/12#issuecomment-99"
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                self.calls.clear()
                board = load_tool(BIN / "harness-board")
                seen = []

                def gh(repo, args, stdin=None):
                    seen.append((list(args), stdin))
                    return f"\n{url}\n"

                board.gh = gh
                self.spy(board, self.text.read_text, "read_text")
                self.spy(board, self.text.first_line, "first_line")
                self.spy(board, self.cli.emit, "emit")
                ns = argparse.Namespace(number=12, body=None, body_file=path, json=as_json)
                _, out, err = capture(board.cmd_comment, None, ns)
                self.assertEqual(seen, [(["issue", "comment", "12", "--body-file", "-"],
                                         "Fixed — see the PR.\n\nDetails here.\n\n<!-- harness -->")])
                self.assertEqual([args for args, _ in self.called("read_text")], [(path, "body file")])
                self.assertEqual(self.given("first_line"), [url])
                self.assertEqual(len(self.called("emit")), 1)
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out) if as_json else out,
                                 {"number": 12, "url": url} if as_json else url + "\n")

    def test_board_refuses_a_missing_evidence_file_with_the_lib_problem(self):
        board = load_tool(BIN / "harness-board")
        board.gh = board.gh_json = lambda *args, **kwargs: self.fail("gh ran before the evidence file was read")
        self.spy(board, self.text.read_text, "read_text")
        missing = os.path.join(self.tmp, "evidence", "red.txt")
        ns = argparse.Namespace(number=12, question="Which login flow?", evidence_file=missing, json=False)
        with self.assertRaises(self.cli.Problem) as ctx:
            board.cmd_needs_input(None, ns)
        self.assertEqual(str(ctx.exception), f"cannot read evidence file {missing}: No such file or directory")
        self.assertEqual([args for args, _ in self.called("read_text")], [(missing, "evidence file")])

    def test_board_finds_its_files_under_home_through_the_lib(self):
        """settings_project, load_projects and Repo.ticket_path: every path under HOME comes from the lib."""
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.text.home, "home")
        self.spy(board, self.text.harness_home, "harness_home", required=False)
        for home in (os.path.join(self.tmp, "h1"), os.path.join(self.tmp, "Other Home")):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"HOME": home}):
                rel = os.path.relpath(home, self.tmp)
                self.put(os.path.join(rel, ".claude", "settings.json"),
                         json.dumps({"env": {"HARNESS_PROJECT": "acme/7"}}))
                self.put(os.path.join(rel, ".claude", "harness", "projects.json"), json.dumps(
                    {"version": 1, "projects": [{"path": "/p/a"}, {"path": " "}, {"path": "/p/b"}, "x"]}))
                self.calls.clear()
                self.assertEqual(board.settings_project(), "acme/7")
                self.assertGreaterEqual(len(self.called("home")), 1, "settings_project found the file without the lib")
                self.calls.clear()
                self.assertEqual(board.load_projects(), ["/p/a", "/p/b"])
                self.assertGreaterEqual(self.homed(), 1, "load_projects found the registry without the lib")
                repo = object.__new__(board.Repo)
                repo.key = "0123456789ab"
                self.calls.clear()
                self.assertEqual(repo.ticket_path(12), os.path.join(
                    home, ".claude", "harness", "repos", "0123456789ab", "tickets", "12.json"))
                self.assertGreaterEqual(self.homed(), 1, "ticket_path built the path without the lib")

    def test_board_stamps_a_fetched_ticket_through_the_lib_now_iso(self):
        issue = {"number": 7, "title": "Fix login", "url": "https://x/issues/7", "state": "OPEN",
                 "body": "## Request\nFix it.\n\n## Definition of done\n- [ ] it works\n",
                 "labels": [{"name": "task"}, {"name": "status:ready"}, {"name": "size:S"}, {"name": "risk:low"}],
                 "parent": None, "blockedBy": [],
                 "comments": [{"author": {"login": "ryan"}, "createdAt": "2026-10-16T10:00:00Z", "body": "go"}]}
        board = load_tool(BIN / "harness-board")
        board.gh_json = lambda repo, args: copy.deepcopy(issue)
        self.spy(board, self.text.now_iso, "now_iso")
        before = int(time.time())
        record = board.fetch_record(None, 7)
        after = int(time.time())
        self.assertEqual(len(self.called("now_iso")), 1)
        stamp = record["fetched_at"]
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        seconds = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(before - 1 <= seconds <= after + 1, (before, stamp, after))
        self.assertEqual((record["type"], record["status"], record["tier"]), ("task", "ready", "sonnet"))
        self.assertEqual(record["sections"]["definition_of_done"], ["- [ ] it works"])
        self.assertEqual(record["comments"], [{"author": "ryan", "created_at": "2026-10-16T10:00:00Z", "body": "go"}])

    def test_board_answers_compares_comment_times_through_the_lib_parse_iso(self):
        """cmd_answers keeps the owner's replies newer than `since` (--since, else the newest needs-input
        question), comparing aware times: an offset counts."""
        comments = [
            {"author": {"login": "Ryan"}, "body": "older reply", "createdAt": "2026-10-14T10:00:00Z", "url": "u0"},
            {"author": {"login": "Ryan"}, "body": "**Needs your input**\nWhich one?\n\n<!-- harness -->",
             "createdAt": "2026-10-15T10:00:00Z", "url": "u1"},
            {"author": {"login": "ryan"}, "body": "the second", "createdAt": "2026-10-16T09:30:00+02:00", "url": "u2"},
            {"author": {"login": "someone-else"}, "body": "drive-by", "createdAt": "2026-10-16T12:00:00Z", "url": "u3"},
        ]
        cases = [
            (None, ["the second"]),                       # since the needs-input question, 10-15 10:00Z
            ("2026-10-13T00:00:00Z", ["older reply", "the second"]),
            ("2026-10-16T07:00:00Z", ["the second"]),     # 09:30+02:00 is 07:30Z: newer
            ("2026-10-16T07:45:00Z", []),                 # and older than this
        ]
        for since, want in cases:
            with self.subTest(since=since):
                self.calls.clear()
                board = load_tool(BIN / "harness-board")
                board.gh = lambda repo, args, stdin=None: "Ryan\n"
                board.gh_json = lambda repo, args: {"comments": copy.deepcopy(comments)}
                self.spy(board, self.text.parse_iso, "parse_iso")
                _, out, _ = capture(board.cmd_answers, None, argparse.Namespace(number=12, since=since))
                self.assertEqual([c["body"] for c in json.loads(out)], want)
                parsed = set(self.given("parse_iso"))
                self.assertTrue({c["createdAt"] for c in comments[:3]} <= parsed, parsed)
                if since is not None:
                    self.assertIn(since, parsed)
        self.calls.clear()
        board = load_tool(BIN / "harness-board")
        board.gh = board.gh_json = lambda *args, **kwargs: self.fail("gh ran before --since was checked")
        self.spy(board, self.text.parse_iso, "parse_iso")
        with self.assertRaises(self.cli.Problem) as ctx:
            board.cmd_answers(None, argparse.Namespace(number=12, since="yesterday"))
        self.assertEqual(str(ctx.exception), "--since: not an ISO-8601 time: 'yesterday'")
        self.assertEqual(self.given("parse_iso"), ["yesterday"])

    def test_board_names_gh_failures_by_their_first_line_through_the_lib(self):
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.text.first_line, "first_line")
        out = "\n\n  not json at all \nsecond\n"
        board.gh = lambda repo, args, stdin=None: out
        with self.assertRaises(self.cli.Problem) as ctx:
            board.gh_json(None, ["issue", "view", "7", "--json", "labels"])
        self.assertEqual(str(ctx.exception), "gh issue view 7 printed something other than JSON: not json at all")
        created = "\n   no url here\nhttps://x/not-an-issue\n"
        with self.assertRaises(self.cli.Problem) as ctx:
            board.created_issue(created)
        self.assertEqual(str(ctx.exception), "gh issue create printed no issue URL: no url here")
        # a gh that fails: the first line of its stderr names the failure
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.text.first_line, "first_line")
        fake = self.fake_exe("gh", "printf '\\n  HTTP 502: bad gateway\\nretry later\\n' >&2\nexit 1\n")
        with mock.patch.dict(os.environ, {"HARNESS_GH": fake}):
            with self.assertRaises(self.cli.Problem) as ctx:
                board.gh(None, ["issue", "view", "7", "--json", "labels"])
        self.assertEqual(str(ctx.exception), "gh issue view 7 failed: HTTP 502: bad gateway")
        self.assertEqual(self.given("first_line"), [out, created, "\n  HTTP 502: bad gateway\nretry later\n"])

    def test_board_project_sync_reports_a_failed_write_on_one_line_through_the_lib(self):
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.text.one_line, "one_line")
        repo = argparse.Namespace(main="/p/main", root="/p/main", name="main")
        board.open_repo = lambda path=None: repo
        board.gh_json = lambda r, args: {"items": [], "totalCount": 0}
        board.open_issues = lambda r: [{"url": "https://x/issues/1", "labels": [{"name": "status:ready"}]}]

        def gh(r, args, stdin=None):
            raise board.Problem("gh project item-add failed:\n   HTTP 502\n\tBad Gateway")

        board.gh = gh
        with mock.patch.dict(os.environ, {"HARNESS_PROJECT": "acme/3"}):
            code, out, err = capture(board.cmd_project_sync, argparse.Namespace(all_projects=False, dry_run=False))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.splitlines()[0], "harness-board: https://x/issues/1: gh project item-add failed: "
                                              "HTTP 502 Bad Gateway; its Status and Priority are not set")
        self.assertTrue(any(isinstance(given, Exception) and "HTTP 502" in str(given)
                            for given in self.given("one_line")), self.calls)

    def test_board_main_ends_a_problem_as_one_line(self):
        """main() is the zero-argument callable run_main takes; a Problem under it ends as `harness-board: <one
        line>` either in main (through the lib's one_line) or by reaching run_main as the lib's Problem."""
        board = load_tool(BIN / "harness-board")
        self.spy(board, self.text.one_line, "one_line")
        missing = os.path.join(self.tmp, "no such\nrepo")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", ["harness-board", "show", "5", "--repo", missing]):
            with self.assertRaises((SystemExit, self.cli.Problem)) as ctx:
                board.main()
        self.assertEqual(out.getvalue(), "")
        if isinstance(ctx.exception, SystemExit):
            self.assertEqual(ctx.exception.code, f"harness-board: not a directory: {' '.join(missing.split())}")
            self.assertTrue(any(isinstance(given, self.cli.Problem) for given in self.given("one_line")), self.calls)
        else:
            self.assertEqual(str(ctx.exception), f"not a directory: {missing}")

    # ---- harness-pr

    def test_pr_builds_its_parser_from_the_lib(self):
        pr = load_tool(BIN / "harness-pr")
        self.assertIs(type(pr.parser()), self.cli.Parser)

    def test_pr_writes_the_budgets_file_through_write_json_with_the_same_bytes(self):
        """mark_cloud_exhausted is write_json(path, data, prefix=".harness-pr-"): the file is indented JSON, non-ASCII
        kept, one newline at the end, other keys kept, and write_text is not the way it is written."""
        pr = load_tool(BIN / "harness-pr")
        budgets = os.path.join(self.tmp, ".claude", "harness", "budgets.json")
        os.makedirs(os.path.dirname(budgets))
        with open(budgets, "w", encoding="utf-8") as fh:
            json.dump({"api": {"exhausted_until": "2030-01-01T00:00:00Z"}, "note": "café"}, fh)
        self.spy(pr, self.text.read_json, "read_json")
        self.spy(pr, self.text.write_json, "write_json")
        self.spy(pr, self.text.write_text, "write_text")
        untils = []
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for reason in ("weekly limit", "quota (token-7) ☃"):
                until = pr.mark_cloud_exhausted(reason)
                untils.append(until)
                want = {"api": {"exhausted_until": "2030-01-01T00:00:00Z"}, "note": "café",
                        "cloud": {"exhausted_until": until, "reason": reason}}
                with open(budgets, "rb") as fh:
                    raw = fh.read()
                self.assertEqual(raw, (json.dumps(want, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                self.assertRegex(until, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.given("write_json"), [budgets, budgets])
        self.assertEqual([kwargs for _, kwargs in self.called("write_json")], [{"prefix": ".harness-pr-"}] * 2)
        # the data it was given is the whole budgets document, not a stand-in
        self.assertEqual([args[1] for args, _ in self.called("write_json")], [
            {"api": {"exhausted_until": "2030-01-01T00:00:00Z"}, "note": "café",
             "cloud": {"exhausted_until": untils[0], "reason": "weekly limit"}},
            {"api": {"exhausted_until": "2030-01-01T00:00:00Z"}, "note": "café",
             "cloud": {"exhausted_until": untils[1], "reason": "quota (token-7) ☃"}}])
        self.assertEqual(self.called("write_text"), [], "the budgets file is JSON: write_json, not write_text")
        self.assertEqual(self.given("read_json"), [budgets, budgets])
        self.assertEqual([n for n in os.listdir(os.path.dirname(budgets)) if n.startswith(".harness-pr-")], [])

    def test_pr_budgets_file_is_whatever_write_json_leaves_and_nothing_after_it(self):
        """With write_json replaced by a stand-in that writes a marker, the file holds the marker and only the marker
        after mark_cloud_exhausted: nothing else writes the file before, after or instead of write_json, and
        write_json is handed the real document (not an empty one) and the return value is its `exhausted_until`."""
        for reason in ("weekly limit", "plan limit ☃"):
            with self.subTest(reason=reason):
                pr = load_tool(BIN / "harness-pr")
                folder = os.path.join(self.tmp, reason[:4], ".claude", "harness")
                budgets = os.path.join(folder, "budgets.json")
                os.makedirs(folder)
                with open(budgets, "w", encoding="utf-8") as fh:
                    json.dump({"keep": [1, 2]}, fh)
                given = []

                def stand_in(path, data, **kwargs):
                    given.append((path, copy.deepcopy(data), kwargs))
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("MARKER\n")

                with mock.patch.dict(os.environ, {"HOME": os.path.dirname(os.path.dirname(folder))}), \
                        mock.patch.object(pr, "write_json", stand_in, create=True):
                    until = pr.mark_cloud_exhausted(reason)
                with open(budgets, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "MARKER\n")
                self.assertEqual(given, [(budgets, {"keep": [1, 2], "cloud": {"exhausted_until": until,
                                                                              "reason": reason}},
                                          {"prefix": ".harness-pr-"})])
                self.assertEqual(os.listdir(folder), ["budgets.json"])

    def test_pr_writes_the_ultrareview_output_through_write_text(self):
        """Text that is not (known to be) JSON is written as given through write_text, in a `.harness-pr-` temporary
        file: cmd_ultra saves claude's stdout byte for byte."""
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.write_text, "write_text")
        saved = os.path.join(self.tmp, "state", "ultra", "9.json")
        out = '{"findings": [], "note": "\u2603"}\n\n'
        claude = self.fake_exe("claude", f"printf '%s' '{out}'\n")
        repo = mock.Mock(root=self.tmp)
        repo.state = lambda *parts: os.path.join(self.tmp, "state", *parts)
        args = argparse.Namespace(number=9, post=False)
        with mock.patch.dict(os.environ, {"HARNESS_CLAUDE": claude, "HOME": self.tmp}), \
                mock.patch.object(pr, "open_repo", lambda: repo):
            _, printed, _ = capture(pr.cmd_ultra, args)
        self.assertIn("no findings", printed)
        self.assertEqual(self.given("write_text"), [saved])
        self.assertEqual(self.called("write_text")[0][1], {"prefix": ".harness-pr-"})
        with open(saved, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), out)

    def test_pr_warns_through_the_lib_with_its_own_spelling_byte_for_byte(self):
        """`harness-pr: warning: <message on one line>`: the prog name is harness-pr and the message is the tool's
        own words; a different prog, a different separator or a second line fails it."""
        pr = load_tool(BIN / "harness-pr")
        pr.open_repo = lambda: mock.Mock(root=self.tmp)
        pr.pr_view = lambda repo, number, fields: {"statusCheckRollup": []}
        args = argparse.Namespace(number=41, wait=False, json=False, timeout=0)
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "HARNESS_POLL_SECONDS": "0"}):
            code, out, err = capture(pr.cmd_checks, args)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "harness-pr: warning: no checks registered on PR #41\n")
        pr = load_tool(BIN / "harness-pr")
        pr.open_repo = lambda: mock.Mock(root=self.tmp)
        pr.pr_view = lambda repo, number, fields: {"statusCheckRollup": []}
        self.spy(pr, self.cli.warn, "warn")
        args.number = 7
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "HARNESS_POLL_SECONDS": "0"}):
            capture(pr.cmd_checks, args)
        self.assertEqual([a for a, _ in self.called("warn")], [("harness-pr", "no checks registered on PR #7")])

    def test_pr_warning_with_a_message_on_several_lines_is_one_line(self):
        """warn("harness-pr", ...) is `one_line`d by the lib: an `awaiting --all-projects` project that cannot be
        read is one `harness-pr: warning: <name>: <problem>` line, never two."""
        pr = load_tool(BIN / "harness-pr")
        home = os.path.join(self.tmp, "h")
        os.makedirs(os.path.join(home, ".claude", "harness"))
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        with open(os.path.join(home, ".claude", "harness", "projects.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": [{"name": "demo", "path": project}]}, fh)
        pr.git = lambda path, *a: mock.Mock(returncode=0)

        def broken(repo, project=None):
            raise self.cli.Problem("gh pr list failed:\n  HTTP 502\n  Bad Gateway")

        pr.awaiting_in = broken
        pr.Repo = lambda path: object()
        args = argparse.Namespace(all_projects=True, json=False)
        with mock.patch.dict(os.environ, {"HOME": home}):
            code, out, err = capture(pr.cmd_awaiting, args)
        self.assertEqual(code, 0)
        self.assertEqual(err, "harness-pr: warning: demo: gh pr list failed: HTTP 502 Bad Gateway\n")

    def test_pr_reads_files_through_the_lib_and_raises_its_problem(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.read_text, "read_text")
        missing = os.path.join(self.tmp, "findings-missing.json")
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.load_findings(missing)
        self.assertIn(missing, str(ctx.exception))
        self.assertEqual(self.given("read_text"), [missing])
        path = self.put("findings.json", '[{"path": "./a.py", "line": "7", "severity": "Blocker", "body": " x "}]')
        self.assertEqual(pr.load_findings(path), [{"path": "a.py", "line": 7, "severity": "high", "body": "x"}])
        self.assertEqual(self.given("read_text"), [missing, path])

    def test_pr_renders_its_body_from_the_template_through_the_lib_read_text(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.read_text, "read_text")
        ticket = {"title": "Fix login", "risk": "low", "labels": ["task", "size:S"], "flags": [],
                  "sections": {"request": "Fix the login.\nNow.\n\nSecond paragraph.",
                               "definition_of_done": ["- [ ] a", "- [ ] b"], "tests": ["test_x", "test_y"]}}
        body = pr.pr_body(12, ticket, None, "Ran 3 tests\n\nOK\n", None, None)
        self.assertEqual([args for args, _ in self.called("read_text")], [(str(TEMPLATES / "pr_body.md"), "template")])
        self.assertIn("Closes #12\n", body)
        self.assertIn("## Summary\nFix the login. Now.\n", body)
        self.assertIn("- [ ] a\n- [ ] b", body)
        self.assertIn("- test_x\n- test_y", body)
        self.assertIn("```\nRan 3 tests\n\nOK\n```", body)
        self.assertIn("No review attached.", body)
        self.assertIn("None noted.", body)

    def test_pr_ages_through_the_lib_parse_iso(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.parse_iso, "parse_iso")
        then = (dt.datetime.now(UTC) - dt.timedelta(days=3, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(pr.age(then), "3d")
        self.assertEqual(pr.age("not a time"), "?")
        self.assertEqual(self.given("parse_iso"), [then, "not a time"])

    def test_pr_picks_the_latest_harness_review_through_the_lib_parse_iso(self):
        """latest_harness_review compares aware times (an offset counts; a naive time is UTC; an unparsable one
        is skipped), and `newer` does the same against --since."""
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.parse_iso, "parse_iso")
        reviews = [
            {"id": 1, "body": "r1 <!-- harness-review -->", "submitted_at": "2026-10-15T10:00:00Z"},
            {"id": 2, "body": "r2 <!-- harness-review -->", "submitted_at": "2026-10-15T13:00:00+02:00"},  # 11:00Z
            {"id": 3, "body": "r3 <!-- harness-review -->", "submitted_at": "not a time"},
            {"id": 4, "body": "no marker", "submitted_at": "2026-10-16T00:00:00Z"},
            {"id": 5, "body": "r5 <!-- harness-review -->", "submitted_at": "2026-10-15T10:30:00"},
        ]
        self.assertEqual(pr.latest_harness_review(reviews)["id"], 2)
        self.assertEqual(pr.latest_harness_review(reviews[2:])["id"], 5)
        self.assertIsNone(pr.latest_harness_review(reviews[2:4]))
        self.assertIsNone(pr.latest_harness_review([]))
        parsed = set(self.given("parse_iso"))
        self.assertTrue({r["submitted_at"] for r in reviews if "harness-review" in r["body"]} <= parsed, parsed)
        since = self.text.parse_iso("2026-10-16T00:00:00Z")
        self.calls.clear()
        cases = [
            ({"createdAt": "2026-10-16T01:00:00Z"}, since, True),
            ({"created_at": "2026-10-16T03:00:00+02:00"}, since, True),      # 01:00Z
            ({"createdAt": "2026-10-16T01:00:00+02:00"}, since, False),     # 23:00Z the day before
            ({"createdAt": "garbage"}, since, False),
            ({"createdAt": "garbage"}, None, True),
        ]
        for node, bound, want in cases:
            with self.subTest(node=node, since=bound):
                self.assertIs(pr.newer(node, bound), want)
        self.assertEqual(self.given("parse_iso"), [next(iter(node.values())) for node, _, _ in cases])

    def test_pr_reads_gh_lists_through_the_lib_as_list(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.as_list, "as_list")
        closing = [
            ({"closingIssuesReferences": [{"number": 12}, {"number": 12}, {"number": 7}, "x", {"number": "8"}]},
             [12, 7]),
            ({"closingIssuesReferences": {"nodes": [{"number": 3}, {"number": 1}]}}, [3, 1]),
            ({"closingIssuesReferences": None}, []),
            ({}, []),
        ]
        for view, want in closing:
            with self.subTest(view=view):
                self.assertEqual(pr.closing_numbers(view), want)
        rollup = {"nodes": [
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "u1"},
            {"__typename": "StatusContext", "context": "lint", "state": "FAILURE", "targetUrl": "u2"},
            {"__typename": "CheckRun", "name": "slow", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": ""},
            "garbage",
        ]}
        entries = pr.check_entries(rollup)
        self.assertEqual(entries, [
            {"name": "ci", "state": "SUCCESS", "bucket": "pass", "url": "u1"},
            {"name": "lint", "state": "FAILURE", "bucket": "fail", "url": "u2"},
            {"name": "slow", "state": "IN_PROGRESS", "bucket": "pending", "url": ""},
        ])
        self.assertEqual(pr.overall(entries), "failure")
        files = [{"path": "a.py"}, {"path": ""}, {"nope": 1}, {"path": "b/c.md"}]
        self.assertEqual(pr.view_files({"files": files}), ["a.py", "b/c.md"])
        self.assertEqual(self.given("as_list"),
                         [v.get("closingIssuesReferences") for v, _ in closing] + [rollup, files])

    def test_pr_finds_its_state_files_under_home_through_the_lib(self):
        """mark_cloud_exhausted, registry_projects and Repo.state: every path under HOME comes from the lib."""
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.home, "home")
        self.spy(pr, self.text.harness_home, "harness_home", required=False)
        for home in (os.path.join(self.tmp, "h1"), os.path.join(self.tmp, "Other Home")):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"HOME": home}):
                rel = os.path.relpath(home, self.tmp)
                self.put(os.path.join(rel, ".claude", "harness", "projects.json"), json.dumps({"projects": [
                    {"name": "harness", "path": "~/portfolio/Harness"}, {"path": "/x/y/"}, {"nope": 1}]}))
                self.calls.clear()
                self.assertEqual(pr.registry_projects(), [("harness", os.path.join(home, "portfolio", "Harness")),
                                                          ("y", "/x/y/")])
                self.assertGreaterEqual(self.homed(), 1, "registry_projects found the registry without the lib")
                self.calls.clear()
                before = dt.datetime.now(UTC)
                until = pr.mark_cloud_exhausted("weekly limit")
                self.assertGreaterEqual(self.homed(), 1, "mark_cloud_exhausted built its path without the lib")
                expected = before + dt.timedelta(hours=24)
                self.assertLessEqual(abs((self.text.parse_iso(until) - expected).total_seconds()), 5, until)
                with open(os.path.join(home, ".claude", "harness", "budgets.json"), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["cloud"], {"exhausted_until": until, "reason": "weekly limit"})
                repo = object.__new__(pr.Repo)
                repo.key = "0123456789ab"
                self.calls.clear()
                self.assertEqual(repo.state("ultra", "9.json"), os.path.join(
                    home, ".claude", "harness", "repos", "0123456789ab", "ultra", "9.json"))
                self.assertGreaterEqual(self.homed(), 1, "Repo.state built the path without the lib")

    def test_pr_shortens_text_and_tool_failures_through_the_lib_one_line(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.one_line, "one_line")
        cases = [("a\n  b\t c", 100, "a b c"), ("  " + "x" * 120, 100, "x" * 97 + "..."), ("one  two", 5, "on..."),
                 ("", 10, "")]
        for text, n, want in cases:
            with self.subTest(text=text):
                self.assertEqual(pr.short(text, n), want)
        self.assertEqual(self.given("one_line"), [text for text, _, _ in cases])
        # a sibling tool that fails: its stderr, on one line, names the failure
        fake = self.fake_exe("harness-board", "printf 'harness-board: no such\\n   ticket #9\\n' >&2\nexit 1\n")
        pr.sibling = lambda name: fake
        self.calls.clear()
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.tool("harness-board", ["show", "9", "--json"], self.tmp)
        self.assertEqual(str(ctx.exception), "harness-board show 9 failed: harness-board: no such ticket #9")
        self.assertEqual(self.given("one_line")[:1], ["harness-board: no such\n   ticket #9\n"])

    def test_pr_names_gh_failures_by_their_first_line_through_the_lib(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.first_line, "first_line")
        stderr = "\n  GraphQL: Could not resolve to a PullRequest\nsecond line\n"
        pr.gh_run = lambda args, cwd, stdin=None, timeout=60: subprocess.CompletedProcess(list(args), 1, "", stderr)
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.gh(["pr", "view", "9", "--json", "url"], self.tmp)
        self.assertEqual(str(ctx.exception), "gh pr view 9 failed: GraphQL: Could not resolve to a PullRequest")
        out = "\n\n <html>oops\nmore\n"
        pr.gh = lambda args, cwd, stdin=None, timeout=60: out
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.gh_json(["pr", "list", "--head", "t/1-x"], self.tmp)
        self.assertEqual(str(ctx.exception), "gh pr list printed something other than JSON: <html>oops")
        created = "\n   Creating pull request for t/1-x into main\n"
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.created_pr(created)
        self.assertEqual(str(ctx.exception),
                         "gh pr create printed no pull request URL: Creating pull request for t/1-x into main")
        self.assertEqual(self.given("first_line"), [stderr, out, created])

    def test_pr_names_git_failures_and_the_verify_result_by_their_last_line_through_the_lib(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.last_line, "last_line")
        stderr = "warning: something\nfatal: not a git repository\n\n"
        pr.git = lambda path, *args, timeout=60: subprocess.CompletedProcess(list(args), 128, "", stderr)
        with self.assertRaises(self.cli.Problem) as ctx:
            pr.git_ok("/p/wt", "status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(str(ctx.exception), "git status --porcelain=v1 failed in /p/wt: fatal: not a git repository")
        self.assertEqual(self.given("last_line"), [stderr])
        bodies = [
            ("Red run: exit 1, 12 failed\n\nVerify:\n```\nRan 40 tests in 2.1s\n\nOK\n```\n",
             ("Red run: exit 1, 12 failed", "OK"), "Ran 40 tests in 2.1s\n\nOK"),
            ("## Verify\n```\n$ ./verify.sh\nFAILED (failures=2)\n  \n```\n",
             (None, "FAILED (failures=2)"), "$ ./verify.sh\nFAILED (failures=2)\n  "),
            ("Nothing here.\n", (None, None), None),
        ]
        for body, want, block in bodies:
            with self.subTest(body=body):
                self.calls.clear()
                self.assertEqual(pr.evidence(body), want)
                self.assertEqual(self.given("last_line"), [block] if block is not None else [])

    def test_pr_awaiting_prints_its_table_through_the_lib(self):
        pr = load_tool(BIN / "harness-pr")
        rows = [{"number": n, "title": f"Title {n}", "ticket": n + 100, "url": f"https://x/pull/{n}",
                 "created_at": "", "age": "2h", "checks": "green", "unresolved": n % 3} for n in (7, 31)]
        pr.open_repo = lambda *args, **kwargs: None
        pr.awaiting_in = lambda repo, project=None: copy.deepcopy(rows)
        self.spy(pr, self.cli.table, "table")
        _, out, _ = capture(pr.cmd_awaiting, argparse.Namespace(all_projects=False, json=False))
        self.assertEqual(len(self.called("table")), 1)
        self.assertIn("#7", out)
        self.assertIn("#131", out)


# ================================================================ the tools without the lib


class ToolsWithoutTheLib(unittest.TestCase):
    """A copy of the tool under a temporary bin/ finds its lib at ../lib (next to bin/). Without it, or with a lib
    that will not import, the tool says so in one line and exits 1: `<tool>: cannot load harness_lib from <lib>:
    <error>`, never a traceback, in the style of the hooks' fail-open line."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-nolib-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"PATH": SYSTEM_PATH, "HOME": self.tmp, "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8"}

    def install(self, tool: str, name: str) -> tuple:
        """(path of the tool, path of its lib) for a copy of `tool` in <tmp>/<name>/bin, lib not created."""
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "bin"))
        path = os.path.join(root, "bin", tool)
        shutil.copy(BIN / tool, path)
        os.chmod(path, 0o755)
        return path, os.path.join(root, "lib")

    def run_tool(self, path: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", path, *argv], capture_output=True, text=True, env=self.env,
                              cwd=self.tmp, timeout=60)

    def test_a_tool_with_no_lib_beside_it_says_so_in_one_line_and_exits_1(self):
        for tool in TOOLS:
            for argv in ((), ("--help",), ("board",), ("show", "5")):
                with self.subTest(tool=tool, argv=argv):
                    path, lib = self.install(tool, f"{tool}-{len(os.listdir(self.tmp))}")
                    p = self.run_tool(path, *argv)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertEqual(p.stdout, "")
                    self.assertEqual(p.stderr, f"{tool}: cannot load harness_lib from {lib}: "
                                               "No module named 'harness_lib'\n")

    def test_the_lib_path_in_the_line_is_the_one_beside_the_real_tool_not_the_symlink(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                path, lib = self.install(tool, f"real-{tool}")
                link_dir = os.path.join(self.tmp, f"link-{tool}")
                os.makedirs(link_dir)
                link = os.path.join(link_dir, tool)
                os.symlink(path, link)
                p = self.run_tool(link, "--help")
                self.assertEqual((p.returncode, p.stdout), (1, ""))
                self.assertEqual(p.stderr, f"{tool}: cannot load harness_lib from {lib}: "
                                           "No module named 'harness_lib'\n")

    def test_a_lib_that_fails_to_import_is_one_line_with_the_error_on_that_line(self):
        for tool in TOOLS:
            for error in ("boom", "first line\n   second\tline  \n", "café ☃"):
                with self.subTest(tool=tool, error=error):
                    path, lib = self.install(tool, f"{tool}-{len(os.listdir(self.tmp))}")
                    os.makedirs(os.path.join(lib, "harness_lib"))
                    for name, body in (("__init__.py", ""), ("paths.py", f"raise RuntimeError({error!r})\n")):
                        with open(os.path.join(lib, "harness_lib", name), "w", encoding="utf-8") as fh:
                            fh.write(body)
                    p = self.run_tool(path, "--help")
                    self.assertEqual((p.returncode, p.stdout), (1, ""))
                    self.assertEqual(p.stderr, f"{tool}: cannot load harness_lib from {lib}: "
                                               f"{' '.join(error.split())}\n")

    def lib_imports(self, tool: str) -> dict:
        """{module under harness_lib: [names]} for every harness_lib import anywhere in the tool's source."""
        found: dict = {}
        for node in ast.walk(ast.parse((BIN / tool).read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("harness_lib."):
                found.setdefault(node.module.split(".", 1)[1], []).extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("harness_lib."):
                        found.setdefault(alias.name.split(".", 1)[1], [])
        return found

    def stub_lib(self, lib: str, imports: dict, broken: str, body: str) -> None:
        """A harness_lib of stubs, no module importing another: each binds the names the tool takes from it, except
        `broken`, whose source is `body`. Breaking one module therefore breaks that one import line alone."""
        os.makedirs(os.path.join(lib, "harness_lib"))
        with open(os.path.join(lib, "harness_lib", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("")
        for module, names in imports.items():
            with open(os.path.join(lib, "harness_lib", f"{module}.py"), "w", encoding="utf-8") as fh:
                fh.write(body if module == broken else "".join(f"{n} = None\n" for n in names))

    def test_each_lib_module_the_tool_imports_failing_alone_is_the_one_line(self):
        """paths, cli, text and notify (and any harness_lib module the tool imports): with only that module broken,
        the tool still says `<tool>: cannot load harness_lib from <lib>: <error>` in one line and exits 1. An import
        moved out of the guarded block, or under a narrower except, leaves a traceback instead."""
        errors = (
            (RuntimeError, "broken {module}\n   on  two\tlines "),
            (KeyError, "{module} ☃"),
            (ValueError, ""),
        )
        for tool in TOOLS:
            imports = self.lib_imports(tool)
            self.assertLessEqual({"paths", "cli", "text", "notify"}, set(imports), f"{tool} imports {sorted(imports)}")
            for module in sorted(imports):
                variants = [(kind, text.format(module=module)) for kind, text in errors] + [("missing", "")]
                for kind, text in variants:
                    label = kind if isinstance(kind, str) else kind.__name__
                    with self.subTest(tool=tool, module=module, error=label):
                        path, lib = self.install(tool, f"{tool}-{len(os.listdir(self.tmp))}")
                        if kind == "missing":            # the module loads but lacks a name the tool takes from it
                            if not imports[module]:
                                continue
                            name = imports[module][0]
                            body = "".join(f"{n} = None\n" for n in imports[module] if n != name)
                            where = os.path.join(lib, "harness_lib", f"{module}.py")
                            error = f"cannot import name {name!r} from 'harness_lib.{module}' ({where})"
                        else:
                            body = f"raise {kind.__name__}({text!r})\n"
                            error = " ".join(str(kind(text)).split())
                        self.stub_lib(lib, imports, module, body)
                        p = self.run_tool(path, "--help")
                        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
                        self.assertEqual(p.stderr, f"{tool}: cannot load harness_lib from {lib}: {error}\n")

    def test_the_stub_lib_with_nothing_broken_gets_past_the_load_line(self):
        """The control for the stubs: with every module loading, the cannot-load line is not printed, so the line
        above comes from the one broken module."""
        for tool in TOOLS:
            with self.subTest(tool=tool):
                path, lib = self.install(tool, f"stub-ok-{tool}")
                self.stub_lib(lib, self.lib_imports(tool), broken="", body="")
                p = self.run_tool(path, "--help")
                self.assertNotIn("cannot load harness_lib", p.stderr)

    def test_the_same_copy_with_the_lib_beside_it_runs(self):
        """The control: the line above is about the missing lib and nothing else about the copy."""
        for tool in TOOLS:
            with self.subTest(tool=tool):
                path, lib = self.install(tool, f"ok-{tool}")
                os.symlink(str(LIB), lib)
                p = self.run_tool(path, "--help")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"usage: {tool}", p.stdout)
                self.assertEqual(p.stderr, "")


if __name__ == "__main__":
    unittest.main()
