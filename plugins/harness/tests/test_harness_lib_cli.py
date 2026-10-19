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
import importlib
import importlib.util
import io
import json
import os
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
    },
    "harness-pr": {
        "Problem": "cli", "Parser": "cli", "table": "cli",
        "one_line": "text", "first_line": "text", "last_line": "text", "home": "text",
        "parse_iso": "text", "as_list": "text", "read_text": "text", "write_text": "text",
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
# The lib's defs that are references for the copy check: the moved helpers under their lib names.
LIB_REFERENCE_NAMES = set(MOVED) | {"write_text", "warn"}
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


def defs_in(tree) -> list:
    """Every def and class in `tree`, nested ones included, and every lambda as a def named for what it is
    assigned to."""
    named = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Lambda):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            named[id(node.value)] = names[0] if names else "<lambda>"
    found = []
    for node in ast.walk(tree):
        if isinstance(node, DEFS):
            found.append(node)
        elif isinstance(node, ast.Lambda):
            found.append(as_def(node, named.get(id(node), "<lambda>")))
    return found


def shape(node) -> str:
    """A def or class reduced to its live statements, every name and argument renamed in order of appearance,
    nested defs unnamed, decorators dropped: what a copy keeps however it is renamed or padded."""
    node = pruned(node)
    node.name = "_"
    node.decorator_list = []
    if not node.body:
        node.body = [ast.Pass()]
    names: dict = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
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


def reference_shapes() -> dict:
    shapes = {}
    for origin, node in reference_defs():
        shapes.setdefault(shape(node), f"{origin}:{node.name}")
    return shapes


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


class ToolsAdoptTheLib(unittest.TestCase):
    """The source of harness-board and harness-pr: no copy of a moved helper under any name or disguise, every
    lib name imported and used from code that runs, no dead code to hide a use in, the run_main tail, and the
    names bound are the lib's objects."""

    def tree(self, tool: str):
        return ast.parse((BIN / tool).read_text(encoding="utf-8"))

    def test_board_and_pr_define_none_of_the_moved_helpers(self):
        for tool in TOOLS:
            defined = top_level_names(BIN / tool)
            for name in MOVED:
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

    def test_pr_writes_files_through_the_lib(self):
        pr = load_tool(BIN / "harness-pr")
        self.spy(pr, self.text.write_text, "write_text")
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for reason in ("weekly limit", "quota (token-7)"):
                pr.mark_cloud_exhausted(reason)
        budgets = os.path.join(self.tmp, ".claude", "harness", "budgets.json")
        self.assertEqual(self.given("write_text"), [budgets, budgets])
        with open(budgets, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cloud"]["reason"], "quota (token-7)")

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


if __name__ == "__main__":
    unittest.main()
