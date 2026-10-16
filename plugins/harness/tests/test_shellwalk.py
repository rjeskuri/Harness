"""Tests for the shared shell walker plugins/harness/lib/harness_lib/shellwalk.py (ticket
#85): the walker both Bash guards, branch_guard.py and readonly_bash.py, split commands
with.

- The lib: Part, closing, substitution, split_command, command_words, stdin_program, the
  ten tables both guards defined identically (SHELLS, WRAPPERS, KEYWORDS,
  GIT_VALUE_OPTIONS, ASSIGNMENT, NUMBER, PYTHON, SED_IN_PLACE, REDIRECT, REDIRECTS) and
  the walker's depth cap MAX_DEPTH, imported from lib/ and driven through the public
  functions over pipelines and lists, quoting, substitutions, heredocs and redirections.
  The expected parts are written out by hand from the shell grammar as branch_guard's
  walker reads it: marker parts around subshells and substitutions, the operator after
  each part in `sep`, substitutions last.
- One walker: no def anywhere in the plugin but shellwalk.py's own shares half or more of
  its statements with the lib's walker (names, arguments and attributes normalised, so a
  renamed copy padded with no-op statements still matches); neither guard defines the
  walker's names, assigns or binds a copy of a table (judged by value, under any name),
  or writes a Part attribute; each guard's refusal walk calls split_command and
  command_words by those names, unconditionally (not inside a ternary or an and/or that
  could choose between the lib and something else); the names each guard uses are the
  lib's objects, both when loaded with SourceFileLoader (tests/test_repo_key.py) and when
  exec'd into a bare module (tests/test_guards.py, tests/test_hooks.py). Neither guard
  runs code it did not import: no exec, eval or compile, no dunder attribute, imports
  only from the standard library and harness_lib; readonly_bash reads nothing but stdin;
  no string constant in the plugin holds the walker's code; after loading, no object in
  a guard's namespace (one level into containers and classes) carries the walker's
  bytecode or a table's value.
- Fail open: each guard, copied with hooks/ into a temporary plugin tree whose lib/ lacks
  shellwalk.py or has a broken one, run as hooks.json runs it on a Bash call it denies
  with the real lib: exit 0, one stderr line naming harness_lib, the lib directory and
  (when only shellwalk.py is broken) shellwalk, nothing on stdout. A symlink to the
  checkout's hook next to a broken lib still denies (the lib is found from the hook's
  real path); a copy there fails open.
- The guards follow the lib: from a tree whose shellwalk.py is the checkout's with one
  behavioural edit inside a function body (split_command swaps ls and rm; command_words
  skips a wrapper word only it knows) and no other difference, both guards judge the
  command the lib hands them, so their splitting comes from the lib's code.
- Marker parts fall through readonly_bash's part_refusal as None, and its refusal still
  names the command inside a subshell or a substitution.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_shellwalk.py" -v
"""

from __future__ import annotations

import ast
import copy
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
SHELLWALK_PY = LIB / "harness_lib" / "shellwalk.py"
PATHS_PY = LIB / "harness_lib" / "paths.py"
INIT_PY = LIB / "harness_lib" / "__init__.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
READONLY_TAIL = (
    "The reviewer only reads, searches, diffs and runs tests, so a hook refuses Bash "
    "commands that would change files or git state; describe the change instead of "
    "making it."
)

GUARDS = ("branch_guard.py", "readonly_bash.py")
WALKER = ("Part", "closing", "substitution", "split_command", "command_words", "stdin_program")
FUNCTIONS = WALKER[1:]
TABLES = (
    "SHELLS",
    "WRAPPERS",
    "KEYWORDS",
    "GIT_VALUE_OPTIONS",
    "ASSIGNMENT",
    "NUMBER",
    "PYTHON",
    "SED_IN_PLACE",
    "REDIRECT",
    "REDIRECTS",
)
CONSTANTS = ("MAX_DEPTH",)  # the walker's depth cap: one value, in the lib
LIB_NAMES = WALKER + TABLES + CONSTANTS
# The attributes of a Part: the walker fills them in; a guard only reads them.
PART_ATTRIBUTES = ("words", "targets", "heredocs", "text", "sep", "marker")
# Whatever else a guard binds, its refusal walk splits the command with split_command and
# finds the command's name with command_words, calling them by those names.
DIRECT_CALLS = {
    "readonly_bash.py": {
        "refusal": ("split_command",),
        "part_refusal": ("command_words", "stdin_program"),
    },
    "branch_guard.py": {"walk": ("split_command", "command_words")},
}
BROKEN = ("missing", "no-shellwalk", "syntax", "raises", "incomplete")
# A def shares the walker's code when at least this share of its statements is the
# walker's and it has at least this many statements that count.
SHARED_FRACTION = 0.5
SHARED_MINIMUM = 4
SMALL_STATEMENT = 8  # AST nodes: `i += 1`, `end_word()`, `return j + 1` do not count
# Code a guard did not import can only run through these (a copy of the walker kept as a
# string, or fetched from a file); none of them has a use in a guard.
FORBIDDEN_CALLS = (
    "exec",
    "eval",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "open",
)
# The standard-library modules a guard may import besides harness_lib: what the hooks
# use today and the plain ones a hook could need. importlib, runpy, marshal, pickle,
# types, inspect and ctypes are not among them. A future legitimate need means adding
# the module here, with the reason.
ALLOWED_IMPORTS = frozenset(
    "__future__ collections csv datetime errno fnmatch functools glob hashlib io itertools "
    "json os pathlib re shlex shutil signal stat string subprocess sys tempfile textwrap "
    "time typing unicodedata".split()
)
STDIN = ("sys.stdin.buffer", "sys.stdin")  # all readonly_bash reads
LONG_STRING = 200  # a string constant this long that parses as Python with a def is code
MESSAGE = 120  # characters of source quoted in a failure message

_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-shellwalk-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def load_lib():
    """harness_lib.shellwalk imported from the checkout's lib/, as a hook's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.shellwalk")


def load_with_loader(path: Path, suffix: str = "_shellwalk_test") -> types.ModuleType:
    """A hook loaded the way tests/test_repo_key.py loads it."""
    modname = path.name.replace("-", "_").replace(".py", "") + suffix
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_guards.py loads it: a bare module with __file__."""
    module = types.ModuleType("harness_hook_shellwalk_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def flat(parts) -> list:
    """split_command's parts as plain data: a marker part is its marker, any other part
    is (words, targets, heredocs, text, sep)."""
    out: list = []
    for part in parts:
        if part.marker:
            out.append(part.marker)
        else:
            out.append((part.words, part.targets, part.heredocs, part.text, part.sep))
    return out


def parse(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def hook_tree(name: str):
    return parse(HOOKS / name)


# ================================================================ the lib itself

# Each table: (command, the parts split_command gives, as flat() spells them). Written
# by hand from the grammar: words without their quotes, output targets (a dup to a
# descriptor number or - is none; a bare redirection has target ""), heredoc bodies on
# the part that reads them, the part's text as written, and the operator after it.

LISTS = [
    (
        "cat x.py | head",
        [(["cat", "x.py"], [], [], "cat x.py", "|"), (["head"], [], [], "head", "")],
    ),
    (
        "ls && rm x || echo no; touch y & wait",
        [
            (["ls"], [], [], "ls", "&&"),
            (["rm", "x"], [], [], "rm x", "||"),
            (["echo", "no"], [], [], "echo no", ";"),
            (["touch", "y"], [], [], "touch y", "&"),
            (["wait"], [], [], "wait", ""),
        ],
    ),
    (
        "ls&&rm x||echo",
        [
            (["ls"], [], [], "ls", "&&"),
            (["rm", "x"], [], [], "rm x", "||"),
            (["echo"], [], [], "echo", ""),
        ],
    ),
    ("ls|rm x", [(["ls"], [], [], "ls", "|"), (["rm", "x"], [], [], "rm x", "")]),
    ("ls & rm x", [(["ls"], [], [], "ls", "&"), (["rm", "x"], [], [], "rm x", "")]),
    ("ls\nrm x", [(["ls"], [], [], "ls", ""), (["rm", "x"], [], [], "rm x", "")]),
    ("a; b\n", [(["a"], [], [], "a", ";"), (["b"], [], [], "b", "")]),
    ("ls;", [(["ls"], [], [], "ls", ";")]),
    ("", []),
    ("   ", []),
    (
        "cd src && grep -n x a.py; sed -i.bak 's/x/y/' a.py",
        [
            (["cd", "src"], [], [], "cd src", "&&"),
            (["grep", "-n", "x", "a.py"], [], [], "grep -n x a.py", ";"),
            (["sed", "-i.bak", "s/x/y/", "a.py"], [], [], "sed -i.bak 's/x/y/' a.py", ""),
        ],
    ),
    (
        "{ ls; rm x; }",
        [
            (["{", "ls"], [], [], "{ ls", ";"),
            (["rm", "x"], [], [], "rm x", ";"),
            (["}"], [], [], "}", ""),
        ],
    ),
    (
        "find . -name '*.pyc' | xargs -n 1 rm",
        [
            (["find", ".", "-name", "*.pyc"], [], [], "find . -name '*.pyc'", "|"),
            (["xargs", "-n", "1", "rm"], [], [], "xargs -n 1 rm", ""),
        ],
    ),
    (
        "git -C repo show HEAD:a.py | diff - a.py",
        [
            (
                ["git", "-C", "repo", "show", "HEAD:a.py"],
                [],
                [],
                "git -C repo show HEAD:a.py",
                "|",
            ),
            (["diff", "-", "a.py"], [], [], "diff - a.py", ""),
        ],
    ),
    (
        "FOO=1 BAR=2 rm x",
        [(["FOO=1", "BAR=2", "rm", "x"], [], [], "FOO=1 BAR=2 rm x", "")],
    ),
    (
        "sh -c 'ls; rm x' && echo done",
        [
            (["sh", "-c", "ls; rm x"], [], [], "sh -c 'ls; rm x'", "&&"),
            (["echo", "done"], [], [], "echo done", ""),
        ],
    ),
    ("eval 'rm x'", [(["eval", "rm x"], [], [], "eval 'rm x'", "")]),
]

QUOTING = [
    (
        'echo "a b" \'c d\' e\\ f',
        [(["echo", "a b", "c d", "e f"], [], [], 'echo "a b" \'c d\' e\\ f', "")],
    ),
    (
        'echo "\\$HOME \\"q\\" \\n"',
        [(["echo", '$HOME "q" \\n'], [], [], 'echo "\\$HOME \\"q\\" \\n"', "")],
    ),
    (
        "echo 'it''s' \"un\\closed",
        [(["echo", "its", "un\\closed"], [], [], "echo 'it''s' \"un\\closed", "")],
    ),
    ("ls \\\n -la", [(["ls", "-la"], [], [], "ls \\\n -la", "")]),
    ("echo a\\;b", [(["echo", "a;b"], [], [], "echo a\\;b", "")]),
    (
        "echo 'a;b' c|d",
        [(["echo", "a;b", "c"], [], [], "echo 'a;b' c", "|"), (["d"], [], [], "d", "")],
    ),
    (
        "grep -rn 'a > b' src",
        [(["grep", "-rn", "a > b", "src"], [], [], "grep -rn 'a > b' src", "")],
    ),
    (
        "echo $HOME ${X:-y}",
        [(["echo", "$HOME", "${X:-y}"], [], [], "echo $HOME ${X:-y}", "")],
    ),
    ("ls  # rm build", [(["ls"], [], [], "ls  # rm build", "")]),
    ("echo a#b", [(["echo", "a#b"], [], [], "echo a#b", "")]),
]

SUBSTITUTIONS = [
    (
        'echo "$(rm x)"',
        [
            (["echo", "$(rm x)"], [], [], 'echo "$(rm x)"', ""),
            "(",
            (["rm", "x"], [], [], "rm x", ""),
            ")",
        ],
    ),
    (
        "echo `touch y` done",
        [
            (["echo", "`touch y`", "done"], [], [], "echo `touch y` done", ""),
            "(",
            (["touch", "y"], [], [], "touch y", ""),
            ")",
        ],
    ),
    (
        "echo $(ls) `pwd`",
        [
            (["echo", "$(ls)", "`pwd`"], [], [], "echo $(ls) `pwd`", ""),
            "(",
            (["ls"], [], [], "ls", ""),
            ")",
            "(",
            (["pwd"], [], [], "pwd", ""),
            ")",
        ],
    ),
    (
        "echo $(ls $(pwd))",
        [
            (["echo", "$(ls $(pwd))"], [], [], "echo $(ls $(pwd))", ""),
            "(",
            (["ls", "$(pwd)"], [], [], "ls $(pwd)", ""),
            "(",
            (["pwd"], [], [], "pwd", ""),
            ")",
            ")",
        ],
    ),
    (
        'echo "$(rm x)" > `touch y`',
        [
            (["echo", "$(rm x)"], ["`touch y`"], [], 'echo "$(rm x)" > `touch y`', ""),
            "(",
            (["rm", "x"], [], [], "rm x", ""),
            ")",
            "(",
            (["touch", "y"], [], [], "touch y", ""),
            ")",
        ],
    ),
    (
        'echo "$(cat <<EOF\nbody\nEOF\n)"',
        [
            (
                ["echo", "$(cat <<EOF\nbody\nEOF\n)"],
                [],
                [],
                'echo "$(cat <<EOF\nbody\nEOF\n)"',
                "",
            ),
            "(",
            (["cat"], [], ["body"], "cat <<EOF", ""),
            ")",
        ],
    ),
    ("(ls)", ["(", (["ls"], [], [], "ls", ")"), ")"]),
    (
        "(cd sub && mkdir out)",
        [
            "(",
            (["cd", "sub"], [], [], "cd sub", "&&"),
            (["mkdir", "out"], [], [], "mkdir out", ")"),
            ")",
        ],
    ),
    (
        "(ls) && (git status)",
        [
            "(",
            (["ls"], [], [], "ls", ")"),
            ")",
            "(",
            (["git", "status"], [], [], "git status", ")"),
            ")",
        ],
    ),
    ("ls )", [(["ls"], [], [], "ls", ")"), ")"]),
    (
        "diff <(git show HEAD:a.py) a.py",
        [
            (["diff", "a.py"], [], [], "diff <(git show HEAD:a.py) a.py", ""),
            "(",
            (["git", "show", "HEAD:a.py"], [], [], "git show HEAD:a.py", ""),
            ")",
        ],
    ),
    ("echo $((1 > 0))", [(["echo", "$((1 > 0))"], [], [], "echo $((1 > 0))", "")]),
    ("(( i++ )); ls", [(["ls"], [], [], "ls", "")]),
]

HEREDOCS = [
    (
        "python3 - <<'EOF'\nimport sys\nprint(sys.argv)\nEOF",
        [(["python3", "-"], [], ["import sys\nprint(sys.argv)"], "python3 - <<'EOF'", "")],
    ),
    (
        "cat <<'EOF'\nrm -rf build\nEOF",
        [(["cat"], [], ["rm -rf build"], "cat <<'EOF'", "")],
    ),
    (
        "cat <<EOF | tee out\nrm -rf build\nEOF",
        [
            (["cat"], [], ["rm -rf build"], "cat <<EOF", "|"),
            (["tee", "out"], [], [], "tee out", ""),
        ],
    ),
    (
        "cat <<-EOF\n\t\tindented\n\tEOF\necho after",
        [
            (["cat"], [], ["\t\tindented"], "cat <<-EOF", ""),
            (["echo", "after"], [], [], "echo after", ""),
        ],
    ),
    ("cat <<A <<B\na1\nA\nb1\nb2\nB", [(["cat"], [], ["a1", "b1\nb2"], "cat <<A <<B", "")]),
    ("cat <<EOF\nline1\nline2", [(["cat"], [], ["line1\nline2"], "cat <<EOF", "")]),
    ("cat <<EOF\nEOF", [(["cat"], [], [""], "cat <<EOF", "")]),
]

REDIRECTIONS = [
    ("ls > out.txt 2>&1", [(["ls"], ["out.txt"], [], "ls > out.txt 2>&1", "")]),
    ("echo hi >> notes.txt", [(["echo", "hi"], ["notes.txt"], [], "echo hi >> notes.txt", "")]),
    (
        "cmd 2>err.txt >&2 >| f &>> g <> h",
        [(["cmd"], ["err.txt", "f", "g", "h"], [], "cmd 2>err.txt >&2 >| f &>> g <> h", "")],
    ),
    ("cmd &>/dev/null", [(["cmd"], ["/dev/null"], [], "cmd &>/dev/null", "")]),
    (
        "grep -rn foo src 2>/dev/null",
        [(["grep", "-rn", "foo", "src"], ["/dev/null"], [], "grep -rn foo src 2>/dev/null", "")],
    ),
    (
        'echo "x" >"my file.txt"',
        [(["echo", "x"], ["my file.txt"], [], 'echo "x" >"my file.txt"', "")],
    ),
    (
        "pytest -q 2>&1 | tail -20",
        [
            (["pytest", "-q"], [], [], "pytest -q 2>&1", "|"),
            (["tail", "-20"], [], [], "tail -20", ""),
        ],
    ),
    ("echo done >&2", [(["echo", "done"], [], [], "echo done >&2", "")]),
    ("cmd >&-", [(["cmd"], [], [], "cmd >&-", "")]),
    ("cmd 3>&1 1>&2 2>&3", [(["cmd"], [], [], "cmd 3>&1 1>&2 2>&3", "")]),
    ("echo x >", [(["echo", "x"], [""], [], "echo x >", "")]),
    ("> out.txt", [([], ["out.txt"], [], "> out.txt", "")]),
    ("cat < in.txt <<< here", [(["cat"], [], [], "cat < in.txt <<< here", "")]),
]


class LibShellwalkTests(unittest.TestCase):
    """The walker, imported from lib/harness_lib/shellwalk.py, through its public functions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_lib()

    def parts(self, command: str) -> list:
        return flat(self.lib.split_command(command))

    def check(self, table) -> None:
        for command, expected in table:
            with self.subTest(command=command):
                self.assertEqual(self.parts(command), expected)

    def test_the_module_is_in_lib_and_exposes_the_walker(self):
        self.assertTrue(INIT_PY.is_file())
        self.assertTrue(SHELLWALK_PY.is_file())
        self.assertEqual(
            os.path.realpath(self.lib.__file__), os.path.realpath(str(SHELLWALK_PY))
        )
        self.assertTrue(inspect.isclass(self.lib.Part))
        for name in FUNCTIONS:
            with self.subTest(name=name):
                self.assertTrue(inspect.isfunction(getattr(self.lib, name, None)), name)
        for name in TABLES + CONSTANTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(self.lib, name), name)

    def test_the_shared_tables(self):
        lib = self.lib
        self.assertEqual(lib.MAX_DEPTH, 8)
        self.assertEqual(lib.SHELLS, {"sh", "bash", "zsh", "dash", "ksh"})
        self.assertEqual(
            lib.WRAPPERS,
            {"env", "command", "builtin", "exec", "nohup", "nice", "timeout", "xargs"},
        )
        self.assertEqual(
            lib.KEYWORDS,
            {"!", "{", "}", "if", "then", "elif", "else", "fi", "do", "done", "while", "until", "time"},
        )
        self.assertEqual(
            lib.GIT_VALUE_OPTIONS,
            {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"},
        )
        self.assertEqual(
            lib.REDIRECTS,
            {
                "&>>": "out",
                "&>": "out",
                ">>": "out",
                ">|": "out",
                ">": "out",
                ">&": "dup",
                "<<<": "in",
                "<<-": "heredoc-",
                "<<": "heredoc",
                "<>": "out",
                "<&": "in",
                "<": "in",
            },
        )
        for op in lib.REDIRECTS:  # the longest operator at a position wins
            with self.subTest(op=op):
                self.assertEqual(lib.REDIRECT.match(op + " f").group(0), op)
        # the patterns, by what they match at the start of a word
        for word, matches in (
            ("FOO=1", True),
            ("_x=", True),
            ("a1_B=x y", True),
            ("1A=2", False),
            ("-x=1", False),
            ("a-b=1", False),
            ("ls", False),
        ):
            with self.subTest(pattern="ASSIGNMENT", word=word):
                self.assertEqual(bool(lib.ASSIGNMENT.match(word)), matches)
        for word, matches in (
            ("5", True),
            ("2.5", True),
            ("10m", True),
            ("5s", True),
            ("1h", True),
            ("3d", True),
            ("x5", False),
            ("5x", False),
            ("-1", False),
            ("", False),
        ):
            with self.subTest(pattern="NUMBER", word=word):
                self.assertEqual(bool(lib.NUMBER.match(word)), matches)
        for word, matches in (
            ("python", True),
            ("python3", True),
            ("python3.12", True),
            ("python2.7", True),
            ("pythonw", False),
            ("ipython", False),
            ("python3 ", False),
        ):
            with self.subTest(pattern="PYTHON", word=word):
                self.assertEqual(bool(lib.PYTHON.match(word)), matches)
        for word, matches in (
            ("-i", True),
            ("-i.bak", True),
            ("-ni", True),
            ("-Ei", True),
            ("--in-place", True),
            ("--in-place=.bak", True),
            ("-n", False),
            ("-e", False),
            ("--in-places", False),
            ("i", False),
        ):
            with self.subTest(pattern="SED_IN_PLACE", word=word):
                self.assertEqual(bool(lib.SED_IN_PLACE.match(word)), matches)

    def test_split_command_lists_and_pipelines(self):
        self.check(LISTS)

    def test_split_command_quoting_and_comments(self):
        self.check(QUOTING)

    def test_split_command_substitutions_and_subshells(self):
        self.check(SUBSTITUTIONS)

    def test_split_command_heredocs(self):
        self.check(HEREDOCS)

    def test_split_command_redirections(self):
        self.check(REDIRECTIONS)

    def test_substitutions_nest_eight_deep_and_no_further(self):
        for depth, found in ((1, True), (7, True), (8, True), (9, False), (12, False)):
            command = "echo " + "$(echo " * depth + f"rm{depth}" + ")" * depth
            with self.subTest(depth=depth):
                parts = self.parts(command)
                words = [p[0] for p in parts if isinstance(p, tuple)]
                self.assertEqual(["echo", f"rm{depth}"] in words, found)
                self.assertEqual(parts.count("("), min(depth, 8))
                self.assertEqual(parts.count(")"), min(depth, 8))
                self.assertEqual(len(words), min(depth, 8) + 1)

    def test_command_words(self):
        for words, expected in (
            (["FOO=1", "env", "-i", "X=1", "nohup", "timeout", "5s", "rm", "x"], ["rm", "x"]),
            (["env", "-i", "X=1", "nohup", "timeout", "5s", "rm", "x"], ["rm", "x"]),
            (["if", "!", "/usr/bin/env", "-u", "X", "sudo", "rm", "x"], ["X", "sudo", "rm", "x"]),
            (["xargs", "-n", "1", "rm"], ["rm"]),
            (["xargs", "-I", "{}", "rm", "{}"], ["{}", "rm", "{}"]),
            (["{", "ls", "}"], ["ls", "}"]),
            (["time", "pytest", "-q"], ["pytest", "-q"]),
            (["timeout", "2.5", "ls"], ["ls"]),
            (["timeout", "10m", "ls"], ["ls"]),
            (["nice", "-n", "5", "ls"], ["ls"]),
            (["ls", "env", "rm"], ["ls", "env", "rm"]),
            (["rm", "x"], ["rm", "x"]),
            (["env"], []),
            (["X=1"], []),
            ([], []),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.lib.command_words(words), expected)

    def test_stdin_program(self):
        for args, expected in (
            ([], True),
            (["-"], True),
            (["-s"], True),
            (["-e", "-u"], True),
            (["--norc"], True),
            (["-u", "-"], True),
            (["-", "arg"], True),
            (["-c", "x"], False),
            (["-cx"], False),
            (["-ec", "x"], False),
            (["-m", "mod"], False),
            (["-mjson.tool"], False),
            (["script.sh"], False),
            (["-s", "script.sh"], False),
            (["--", "x"], False),
        ):
            with self.subTest(args=args):
                self.assertIs(self.lib.stdin_program(args), expected)

    def test_closing(self):
        for text, i, expected in (
            ("(a(b)c)", 0, 6),
            ("f(x, ')', y) z", 1, 11),
            ('g("(" ) tail', 1, 6),
            ("(a\\) b) c", 0, 6),
            ('("\\"(" ) r', 0, 7),
            ("('\\') x) y", 0, 4),
            ("x(y)", 1, 3),
            ("(unclosed", 0, 9),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.lib.closing(text, i), expected)

    def test_substitution(self):
        for text, i, expected, inner in (
            ("$((1+2)) x", 0, 8, []),
            ("$(( (1+2) )) q", 0, 12, []),
            ("$(ls -la) y", 0, 9, ["ls -la"]),
            ("`pwd` z", 0, 5, ["pwd"]),
            ("$(a $(b) c) d", 0, 11, ["a $(b) c"]),
            ("x $(ls) y", 2, 7, ["ls"]),
            ("`unclosed", 0, 10, ["unclosed"]),
            ("$(unclosed", 0, 11, ["unclosed"]),
        ):
            with self.subTest(text=text):
                nested: list = []
                self.assertEqual(self.lib.substitution(text, i, nested), expected)
                self.assertEqual(nested, inner)

    def test_part_starts_empty_and_shares_nothing(self):
        a, b = self.lib.Part(), self.lib.Part(marker="(")
        self.assertEqual(
            (a.words, a.targets, a.heredocs, a.text, a.sep, a.marker), ([], [], [], "", "", "")
        )
        self.assertEqual(b.marker, "(")
        a.words.append("ls")
        a.targets.append("f")
        a.heredocs.append("h")
        self.assertEqual((b.words, b.targets, b.heredocs), ([], [], []))
        for part in self.lib.split_command("(ls)"):
            if part.marker:
                self.assertEqual((part.words, part.targets, part.heredocs), ([], [], []))


# ================================================================ one walker: the guards import it


def is_docstring(stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def normalized(node) -> str:
    """ast.dump of `node` with every name, argument, attribute and def name spelled `_`,
    so a renamed copy (renamed helpers, renamed Part attributes) compares equal."""
    node = copy.deepcopy(node)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            sub.id = "_"
        elif isinstance(sub, ast.arg):
            sub.arg = "_"
        elif isinstance(sub, ast.Attribute):
            sub.attr = "_"
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sub.name = "_"
    return ast.dump(node, annotate_fields=False)


def statements(node) -> list:
    """The normalised statements nested anywhere in `node` (a def or class), docstrings
    and small statements left out. A compound statement counts as a whole and each
    statement inside it counts again, so padding a copy with no-op statements changes
    the compound ones and leaves the rest matching."""
    found = []
    for sub in ast.walk(node):
        if sub is node or not isinstance(sub, ast.stmt) or is_docstring(sub):
            continue
        if sum(1 for _ in ast.walk(sub)) < SMALL_STATEMENT:
            continue
        found.append(normalized(sub))
    return found


def walker_statements() -> set:
    """Every statement of the lib's walker: Part and the five functions in shellwalk.py."""
    if not SHELLWALK_PY.is_file():
        return set()
    found: set = set()
    for node in parse(SHELLWALK_PY).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in WALKER:
            found.update(statements(node))
    return found


def defs_outside(node, skip: tuple):
    """Every def or class under `node`, nested ones included, except those named in `skip`
    and anything nested inside them (the walker's own end_word and end_part)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if child.name in skip:
                continue
            yield child
        yield from defs_outside(child, skip)


def shared_defs(tree, reference: set, skip: tuple = ()) -> list:
    """Each def or class in `tree` (nested ones included, `skip` and their insides left
    out) whose statements are at least half the walker's: 'name (line n): shared/total'."""
    found = []
    for node in defs_outside(tree, skip):
        own = statements(node)
        shared = sum(1 for s in own if s in reference)
        if len(own) >= SHARED_MINIMUM and shared >= SHARED_FRACTION * len(own):
            found.append(f"{node.name} (line {node.lineno}): {shared}/{len(own)}")
    return found


def python_files(root: Path) -> list:
    """Every file under `root` that parses as Python, tests/ and __pycache__ left out."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.is_relative_to(TESTS):
            continue
        try:
            tree = parse(path)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        found.append((path, tree))
    return found


def module_assignments(tree) -> set:
    """The names a module assigns at top level."""
    found = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
    return found


def loaded_names(tree) -> set:
    """The names a module reads, annotations left out: with `from __future__ import
    annotations` a name used only in an annotation need not be bound."""
    annotations = set()
    for node in ast.walk(tree):
        subtrees = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            subtrees.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            subtrees.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            subtrees.append(node.annotation)
        for sub in subtrees:
            annotations.update(id(n) for n in ast.walk(sub))
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in annotations
    }


def part_attribute_writes(tree) -> list:
    """Where `tree` fills in a Part: assigns to .words/.targets/.heredocs/.text/.sep/
    .marker, or appends to, extends or clears one of them. Only the walker does that."""
    found = []
    for node in ast.walk(tree):
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "extend", "insert", "clear", "pop", "remove")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr in PART_ATTRIBUTES
        ):
            found.append(f"{ast.unparse(node)} (line {node.lineno})")
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute) and sub.attr in PART_ATTRIBUTES:
                    found.append(f"{ast.unparse(node)} (line {node.lineno})")
    return found


def top_level_def(tree, name: str):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def called_names(fn) -> set:
    return {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def table_copies(module: types.ModuleType, lib) -> list:
    """Each module-level binding in `module` whose value equals one of the lib's ten tables
    without being the lib's own name bound to the lib's object: a copy under another name
    (`_SHELLS = {...}`), whether or not Python happens to hand back the same cached object
    (it does for an identical regex)."""
    found = []
    for name, value in vars(module).items():
        if name.startswith("__") or isinstance(value, types.ModuleType):
            continue
        for table in TABLES:
            reference = getattr(lib, table)
            if name == table and value is reference:
                continue  # the import itself
            try:
                equal = bool(value == reference)
            except Exception:
                equal = False
            if equal:
                found.append(f"{name} == shellwalk.{table}")
    return found


def quoted(node) -> str:
    text = ast.unparse(node)
    return text if len(text) <= MESSAGE else text[: MESSAGE - 1] + "…"


def forbidden_calls(tree) -> list:
    return [
        f"{quoted(node)} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in FORBIDDEN_CALLS
    ]


def dunder_attributes(tree) -> list:
    """`x.__code__`, `m.__file__`, `f.__globals__`: how code is inspected or reached
    behind the import. A hook's own `__file__` and `__name__` are names, not attributes."""
    return [
        f"{quoted(node)} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("__")
        and node.attr.endswith("__")
    ]


def foreign_imports(tree) -> list:
    """Imports of anything but ALLOWED_IMPORTS and harness_lib, and relative imports."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                found.append(f"{quoted(node)} (line {node.lineno})")
                continue
            tops = [node.module.split(".")[0]]
        else:
            continue
        if any(top != "harness_lib" and top not in ALLOWED_IMPORTS for top in tops):
            found.append(f"{quoted(node)} (line {node.lineno})")
    return found


def file_reads(tree) -> list:
    """Calls that read something: .read_text, .read_bytes, .open, .read, .readline(s) on
    anything but sys.stdin."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("read_text", "read_bytes", "open", "read", "readline", "readlines")
            and ast.unparse(node.func.value) not in STDIN
        ):
            found.append(f"{quoted(node)} (line {node.lineno})")
    return found


def code_strings(tree) -> list:
    """(line, parsed module) for each string constant of LONG_STRING characters or more
    that is not a docstring and parses as Python defining a def or class."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and is_docstring(node.body[0]):
                docs.add(id(node.body[0].value))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) >= LONG_STRING
            and id(node) not in docs
        ):
            try:
                inner = ast.parse(node.value)
            except (SyntaxError, ValueError):
                continue
            if any(
                isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for d in ast.walk(inner)
            ):
                found.append((node.lineno, inner))
    return found


def contents(value) -> list:
    """`value` and, one level down, the values of a dict, the items of a list, tuple or
    set, or the attributes of a class."""
    items = [value]
    if isinstance(value, dict):
        items.extend(value.values())
    elif isinstance(value, (list, tuple, set, frozenset)):
        items.extend(value)
    elif isinstance(value, type):
        items.extend(vars(value).values())
    return items


def walker_originals(lib) -> dict:
    """{id(function): name} of the lib's walker functions, Part.__init__ included."""
    found = {id(getattr(lib, name)): name for name in FUNCTIONS}
    found[id(vars(lib.Part)["__init__"])] = "Part.__init__"
    return found


def hidden_code(module: types.ModuleType, lib) -> list:
    """Functions in `module`'s namespace (one level into containers and classes) whose
    bytecode is a lib walker function's but which are not the lib's own objects: a copy
    exec'd from a string or loaded some other way."""
    originals = walker_originals(lib)
    codes = {}
    for name in FUNCTIONS:
        codes[getattr(lib, name).__code__.co_code] = name
    codes[vars(lib.Part)["__init__"].__code__.co_code] = "Part.__init__"
    found = []
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        for item in contents(value):
            if not isinstance(item, types.FunctionType) or id(item) in originals:
                continue
            match = codes.get(item.__code__.co_code)
            if match is not None:
                found.append(f"{name}: {item.__qualname__} has the bytecode of shellwalk.{match}")
    return found


def hidden_tables(module: types.ModuleType, lib) -> list:
    """Values one level inside a guard's containers that equal a lib table."""
    found = []
    for name, value in vars(module).items():
        if name.startswith("__") or isinstance(value, types.ModuleType):
            continue
        for item in contents(value)[1:]:
            for table in TABLES:
                try:
                    equal = bool(item == getattr(lib, table))
                except Exception:
                    equal = False
                if equal:
                    found.append(f"inside {name}: shellwalk.{table}")
    return found


def conditional_walker_calls(tree) -> list:
    """Walker calls inside a ternary, and split_command or command_words inside an and/or:
    a call site that could choose between the lib and something else."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp):
            names = set(FUNCTIONS)
        elif isinstance(node, ast.BoolOp):
            names = {"split_command", "command_words"}
        else:
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in names:
                found.append(f"{quoted(call)} inside {quoted(node)} (line {call.lineno})")
    return found


def iterates(fn, name: str) -> bool:
    """`for ... in name(...)` somewhere in `fn`."""
    return any(
        isinstance(node, ast.For)
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == name
        for node in ast.walk(fn)
    )


def assigns_from(fn, name: str) -> bool:
    """`x = name(...)` somewhere in `fn`."""
    return any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == name
        for node in ast.walk(fn)
    )


class GuardsImportTheWalkerTests(unittest.TestCase):
    """Neither guard carries the walker, a table or a copy of either; the names each uses
    are the lib's objects, called by their own names."""

    def test_no_guard_defines_the_walker(self):
        for name in GUARDS:
            tree = hook_tree(name)
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            with self.subTest(hook=name):
                self.assertEqual(sorted(defined & set(WALKER)), [])

    def test_no_guard_assigns_the_shared_tables(self):
        """The ten tables and MAX_DEPTH are bound by `from harness_lib.shellwalk import
        ...`, as the lib section of docs/design.md says, never assigned in a hook (a copy,
        or a rebinding that a copy could hide behind)."""
        banned = set(TABLES + CONSTANTS)
        for name in GUARDS:
            with self.subTest(hook=name):
                self.assertEqual(sorted(module_assignments(hook_tree(name)) & banned), [])

    def test_no_guard_binds_a_copy_of_a_table(self):
        """No module-level binding in a guard, under any name, has the value of one of the
        ten tables unless it is the lib's own name bound to the lib's object."""
        lib = load_lib()
        for name in GUARDS:
            with self.subTest(hook=name):
                self.assertEqual(table_copies(load_with_exec(HOOKS / name), lib), [])

    def test_no_guard_writes_a_part_attribute(self):
        """A guard reads a Part; only the walker fills one in."""
        for name in GUARDS:
            with self.subTest(hook=name):
                self.assertEqual(part_attribute_writes(hook_tree(name)), [])

    def test_no_file_but_shellwalk_carries_walker_code(self):
        """No def or class anywhere in the plugin (hooks, lib, bin, anything that parses as
        Python; tests/ aside) shares half or more of its statements with the lib's walker,
        names normalised; and shellwalk.py itself has no second copy beside Part and the
        five functions. A renamed copy padded with no-op statements, split in two or with
        its statements reordered still shares them."""
        reference = walker_statements()
        self.assertTrue(reference, f"{SHELLWALK_PY} defines no walker to compare against")
        for path, tree in python_files(PLUGIN):
            skip = WALKER if path == SHELLWALK_PY else ()
            with self.subTest(file=str(path.relative_to(PLUGIN))):
                self.assertEqual(shared_defs(tree, reference, skip), [])

    def test_guards_call_the_walker_by_its_names(self):
        """readonly_bash's refusal splits with split_command and its part_refusal calls
        command_words and stdin_program; branch_guard's walk calls split_command and
        command_words: direct calls by the lib's names, not through an alias."""
        for hook, wanted in DIRECT_CALLS.items():
            tree = hook_tree(hook)
            for caller, names in wanted.items():
                fn = top_level_def(tree, caller)
                with self.subTest(hook=hook, function=caller):
                    self.assertIsNotNone(fn, f"{hook} has no top-level def {caller}")
                    missing = sorted(set(names) - called_names(fn))
                    self.assertEqual(missing, [], f"{hook}:{caller} does not call {missing}")

    def test_guards_call_the_walker_unconditionally(self):
        """No walker call inside a ternary, no split_command or command_words inside an
        and/or (stdin_program legitimately sits in one); readonly_bash's refusal and
        branch_guard's walk iterate `split_command(...)` directly and assign
        `command_words(...)` directly (part_refusal for readonly_bash): nothing at the
        call site can pick another walker."""
        for hook in GUARDS:
            with self.subTest(hook=hook):
                self.assertEqual(conditional_walker_calls(hook_tree(hook)), [])
        for hook, loop, assign in (
            ("readonly_bash.py", "refusal", "part_refusal"),
            ("branch_guard.py", "walk", "walk"),
        ):
            tree = hook_tree(hook)
            with self.subTest(hook=hook, function=loop):
                fn = top_level_def(tree, loop)
                self.assertIsNotNone(fn)
                self.assertTrue(iterates(fn, "split_command"), f"{loop} has no `for ... in split_command(...)`")
            with self.subTest(hook=hook, function=assign):
                fn = top_level_def(tree, assign)
                self.assertIsNotNone(fn)
                self.assertTrue(assigns_from(fn, "command_words"), f"{assign} has no `... = command_words(...)`")

    def test_guards_run_no_code_they_did_not_import(self):
        """No exec, eval, compile, __import__, getattr/setattr/delattr, globals/locals/vars
        or open call, and no dunder attribute (`.__code__`, `.__file__`) in either guard:
        the ways to run or inspect code behind the import. The honest guards use none."""
        for hook in GUARDS:
            tree = hook_tree(hook)
            with self.subTest(hook=hook, check="calls"):
                self.assertEqual(forbidden_calls(tree), [])
            with self.subTest(hook=hook, check="dunder attributes"):
                self.assertEqual(dunder_attributes(tree), [])

    def test_guards_import_only_the_standard_library_and_the_lib(self):
        for hook in GUARDS:
            with self.subTest(hook=hook):
                self.assertEqual(foreign_imports(hook_tree(hook)), [])

    def test_readonly_bash_reads_only_stdin(self):
        """readonly_bash judges the hook input and nothing else: no file read of any kind
        (branch_guard legitimately reads .claude/settings*.json, so this is its rule only)."""
        self.assertEqual(file_reads(hook_tree("readonly_bash.py")), [])

    def test_no_code_string_carries_the_walker(self):
        """No string constant of 200+ characters anywhere in the plugin (tests aside) that
        parses as Python and defines a def or class shares the walker's statements; in a
        guard, no such string at all."""
        reference = walker_statements()
        self.assertTrue(reference, f"{SHELLWALK_PY} defines no walker to compare against")
        for path, tree in python_files(PLUGIN):
            strings = code_strings(tree)
            with self.subTest(file=str(path.relative_to(PLUGIN))):
                if path.parent == HOOKS and path.name in GUARDS:
                    self.assertEqual([line for line, _ in strings], [], "a code string in a guard")
                for line, inner in strings:
                    self.assertEqual(shared_defs(inner, reference), [], f"code string at line {line}")

    def test_no_guard_holds_the_walkers_bytecode_or_a_table(self):
        """After loading a guard as the suites do, no function in its namespace (one level
        into dicts, lists, tuples, sets and classes) has the bytecode of a lib walker
        function without being the lib's object, and no value one level inside a container
        equals a lib table."""
        lib = load_lib()
        for hook in GUARDS:
            module = load_with_exec(HOOKS / hook)
            with self.subTest(hook=hook, check="bytecode"):
                self.assertEqual(hidden_code(module, lib), [])
            with self.subTest(hook=hook, check="tables"):
                self.assertEqual(hidden_tables(module, lib), [])

    def check_bound_from_lib(self, module: types.ModuleType, hook: str) -> None:
        lib = load_lib()
        used = loaded_names(hook_tree(hook))
        for name in LIB_NAMES:
            if name not in used:
                continue
            with self.subTest(hook=hook, name=name):
                self.assertIn(name, module.__dict__, f"{hook} uses {name} but does not bind it")
                bound = module.__dict__[name]
                if name in CONSTANTS:
                    self.assertEqual(bound, getattr(lib, name))
                else:
                    self.assertIs(bound, getattr(lib, name))
        for name in FUNCTIONS:
            bound = module.__dict__.get(name)
            if bound is None:
                continue
            with self.subTest(hook=hook, name=name):
                self.assertEqual(bound.__module__, "harness_lib.shellwalk")
                self.assertEqual(
                    os.path.realpath(inspect.getsourcefile(bound)),
                    os.path.realpath(str(SHELLWALK_PY)),
                )

    def test_names_bound_from_the_lib_with_source_file_loader(self):
        for hook in GUARDS:
            self.check_bound_from_lib(load_with_loader(HOOKS / hook), hook)

    def test_names_bound_from_the_lib_in_a_bare_module(self):
        for hook in GUARDS:
            self.check_bound_from_lib(load_with_exec(HOOKS / hook), hook)


# ================================================================ running the guards from a tree


def attribute_names(module: types.ModuleType) -> set:
    return {name for name in vars(module) if not name.startswith("__")}


def instrumented_source(kind: str, token: str) -> str:
    """The checkout's shellwalk.py with one behavioural edit inside a function body and
    nothing else: no new module attribute, no changed signature.
    'swap': split_command, at depth 0, swaps a part's first word between ls and rm.
    'wrap': command_words drops a leading `token`, a wrapper word only this lib knows."""
    text = SHELLWALK_PY.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    name = "split_command" if kind == "swap" else "command_words"
    fn = top_level_def(ast.parse(text), name)
    if fn is None:
        raise AssertionError(f"{SHELLWALK_PY} has no top-level def {name} to instrument")
    params = [a.arg for a in fn.args.args]
    if kind == "swap":
        last = fn.body[-1]
        if not isinstance(last, ast.Return) or last.value is None:
            raise AssertionError("split_command does not end with `return <parts>`")
        indent = " " * last.col_offset
        value = ast.get_source_segment(text, last.value)
        condition = f"{params[1]} == 0" if len(params) > 1 else "True"
        inserted = (
            f"{indent}if {condition}:\n"
            f"{indent}    for _part in {value}:\n"
            f"{indent}        if _part.words and _part.words[0] in ('ls', 'rm'):\n"
            f"{indent}            _part.words[0] = 'rm' if _part.words[0] == 'ls' else 'ls'\n"
        )
        at = last.lineno - 1
    else:
        first = fn.body[1] if is_docstring(fn.body[0]) and len(fn.body) > 1 else fn.body[0]
        indent = " " * first.col_offset
        words = params[0]
        inserted = f"{indent}{words} = {words}[1:] if {words}[:1] == [{token!r}] else {words}\n"
        at = first.lineno - 1
    lines[at:at] = [inserted]
    return "".join(lines)


class WalkerLoaderTestCase(unittest.TestCase):
    """Runs the two guards from a temporary plugin tree: hooks/ copied from the checkout,
    and a lib/ that is real, missing, broken only in shellwalk.py, or instrumented."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-shellwalk-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        self.plain = self.tmp / "plain"  # a directory outside any repo
        self.plain.mkdir()
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )
        self.token = "walk" + uuid.uuid4().hex[:8]

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
        (repo / "src").mkdir(parents=True)
        (repo / "README.md").write_text("hello\n")
        (repo / "src/app.py").write_text("def login():\n    return None\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def write_lib(self, root: Path, files: dict) -> Path:
        pkg = root / "lib" / "harness_lib"
        pkg.mkdir(parents=True)
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")
        return root / "lib"

    def real_paths(self) -> dict:
        """The checkout's __init__.py and paths.py: the lib is intact but for shellwalk.py."""
        self.assertTrue(PATHS_PY.is_file(), f"{PATHS_PY} does not exist")
        return {
            "__init__.py": INIT_PY.read_text(encoding="utf-8"),
            "paths.py": PATHS_PY.read_text(encoding="utf-8"),
        }

    def plugin(self, lib: str) -> Path:
        """A plugin tree under tmp: the checkout's hooks/ and a lib/ that is 'real'
        (copied from the checkout), 'missing', or has the real paths.py and a shellwalk.py
        that is 'no-shellwalk' (absent), 'syntax' (does not parse), 'raises' (raises
        RuntimeError) or 'incomplete' (lacks split_command)."""
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "real":
            self.assertTrue(SHELLWALK_PY.is_file(), f"{SHELLWALK_PY} does not exist")
            shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        elif lib == "missing":
            pass
        else:
            files = self.real_paths()
            if lib == "syntax":
                files["shellwalk.py"] = "def split_command(:\n    return\n"
            elif lib == "raises":
                files["shellwalk.py"] = f"raise RuntimeError({self.token!r})\n"
            elif lib == "incomplete":
                files["shellwalk.py"] = "def closing(text, i):\n    return len(text)\n"
            else:
                self.assertEqual(lib, "no-shellwalk")
            self.write_lib(root, files)
        return root

    def instrumented(self, kind: str) -> Path:
        """A plugin tree whose lib is the checkout's, with shellwalk.py instrumented
        ('swap' or 'wrap'); the instrumented module has exactly the real one's attributes."""
        self.assertTrue(SHELLWALK_PY.is_file(), f"{SHELLWALK_PY} does not exist")
        root = self.tmp / f"plugin-{kind}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        files = self.real_paths()
        files["shellwalk.py"] = instrumented_source(kind, self.token)
        self.write_lib(root, files)
        patched = root / "lib" / "harness_lib" / "shellwalk.py"
        self.assertEqual(
            attribute_names(load_with_loader(patched, "_" + kind)),
            attribute_names(load_with_loader(SHELLWALK_PY, "_real_" + kind)),
            "the instrumented lib must not be told apart by its attributes",
        )
        return root

    def payload(self, command: str, cwd: Path, agent: str | None = None) -> dict:
        data = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "Run it"},
            "tool_use_id": "toolu_01",
        }
        if agent is not None:  # present only inside a subagent
            data.update(agent_id="agent-1", agent_type=agent)
        return data

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
        out = json.loads(r.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"])
        d = out["hookSpecificOutput"]
        self.assertEqual([d["hookEventName"], d["permissionDecision"]], ["PreToolUse", "deny"])
        return d["permissionDecisionReason"]

    def judged(self, script: Path, payload: dict, cwd: Path) -> str | None:
        """The deny reason, or None; the hook ran clean (exit 0, nothing on stderr)."""
        r = self.run_hook(script, payload, cwd)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return self.deny_reason(r)

    def assert_one_line_fail_open(self, r, hook: str, root: Path, lib: str) -> str:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        line = lines[0]
        self.assertTrue(line.startswith(f"{hook}:"), line)
        self.assertIn("harness_lib", line)
        self.assertIn(str(root / "lib"), line)  # names the lib directory it looked in
        if lib == "raises":
            self.assertIn(self.token, line)  # the error itself, not a fixed message
        elif lib != "missing":
            self.assertIn("shellwalk", line)  # paths.py is fine; the line says what is not
        self.assertEqual(r.stdout.strip(), "")  # no decision: the call is allowed
        return line

    # ---- the two guards' Bash events, denied with a working lib

    def bash_event(self, hook: str) -> tuple:
        """(payload, cwd, expected deny prefix) for a Bash call `hook` denies."""
        if hook == "readonly_bash.py":
            command = "echo hi > x.txt"
            prefix = f'Refused "{command}": it writes to x.txt through a redirection.'
            return self.payload(command, self.plain, "harness:reviewer"), self.plain, prefix
        repo = self.make_repo()
        prefix = 'On main in proj. Refused "rm -rf src": rm changes src inside the repo.'
        return self.payload("rm -rf src", repo), repo, prefix


class WalkerFailOpenTests(WalkerLoaderTestCase):
    def test_a_missing_or_broken_walker_lib_allows_with_one_line(self):
        real = self.plugin("real")
        for hook in GUARDS:  # the events matter: the real lib denies them
            with self.subTest(lib="real", hook=hook):
                payload, cwd, prefix = self.bash_event(hook)
                reason = self.judged(real / "hooks" / hook, payload, cwd)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith(prefix), reason)
        for lib in BROKEN:
            root = self.plugin(lib)
            for hook in GUARDS:
                with self.subTest(lib=lib, hook=hook):
                    payload, cwd, _ = self.bash_event(hook)
                    r = self.run_hook(root / "hooks" / hook, payload, cwd)
                    self.assert_one_line_fail_open(r, hook, root, lib)

    def test_the_lib_is_found_from_the_hooks_real_path(self):
        """A symlink to the checkout's guard, next to a broken lib/, still loads the
        checkout's lib and denies: the prologue resolves __file__ with realpath. A copy
        of the guard in the same place finds only the broken lib and fails open."""
        self.assertTrue(SHELLWALK_PY.is_file(), f"{SHELLWALK_PY} does not exist")
        for hook in GUARDS:
            elsewhere = self.tmp / f"elsewhere-{hook[:-3]}"
            (elsewhere / "hooks").mkdir(parents=True)
            files = self.real_paths()
            files["shellwalk.py"] = f"raise RuntimeError({self.token!r})\n"
            self.write_lib(elsewhere, files)
            link = elsewhere / "hooks" / hook
            os.symlink(HOOKS / hook, link)
            copied = elsewhere / "hooks" / ("copy_of_" + hook)
            shutil.copyfile(HOOKS / hook, copied)
            with self.subTest(hook=hook, loaded="symlink"):
                payload, cwd, prefix = self.bash_event(hook)
                reason = self.judged(link, payload, cwd)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith(prefix), reason)
            with self.subTest(hook=hook, loaded="copy"):
                payload, cwd, _ = self.bash_event(hook)
                r = self.run_hook(copied, payload, cwd)
                self.assert_one_line_fail_open(r, hook, elsewhere, "raises")


class GuardsFollowTheLibTests(WalkerLoaderTestCase):
    """Each guard, from a tree whose shellwalk.py is the checkout's with one behavioural
    edit inside a function body, judges the command that lib hands it: the splitting
    comes from the lib's code, not from a copy."""

    def readonly(self, root: Path, command: str) -> str | None:
        payload = self.payload(command, self.plain, "harness:reviewer")
        return self.judged(root / "hooks" / "readonly_bash.py", payload, self.plain)

    def on_main(self, root: Path, command: str, repo: Path) -> str | None:
        return self.judged(root / "hooks" / "branch_guard.py", self.payload(command, repo), repo)

    def refused(self, text: str, why: str) -> str:
        return f'Refused "{text}": {why}. {READONLY_TAIL}'

    def denied_on_main(self, text: str, why: str) -> str:
        return f'On main in proj. Refused "{text}": {why}. {ADVICE}'

    def test_readonly_bash_splits_commands_with_the_lib(self):
        real = self.plugin("real")
        self.assertIsNone(self.readonly(real, "ls -la"))
        self.assertEqual(
            self.readonly(real, "rm -rf build"), self.refused("rm -rf build", "rm changes files")
        )
        swap = self.instrumented("swap")
        self.assertEqual(self.readonly(swap, "ls -la"), self.refused("ls -la", "rm changes files"))
        self.assertIsNone(self.readonly(swap, "rm -rf build"))

    def test_readonly_bash_takes_the_command_name_from_the_lib(self):
        command = f"{self.token} rm x"
        real = self.plugin("real")
        self.assertIsNone(self.readonly(real, command))
        wrap = self.instrumented("wrap")
        self.assertEqual(self.readonly(wrap, command), self.refused(command, "rm changes files"))

    def test_branch_guard_splits_commands_with_the_lib(self):
        real = self.plugin("real")
        repo = self.make_repo()
        self.assertIsNone(self.on_main(real, "ls src/app.py", repo))
        self.assertEqual(
            self.on_main(real, "rm src/app.py", repo),
            self.denied_on_main("rm src/app.py", "rm changes src/app.py inside the repo"),
        )
        swap = self.instrumented("swap")
        repo = self.make_repo()
        self.assertEqual(
            self.on_main(swap, "ls src/app.py", repo),
            self.denied_on_main("ls src/app.py", "rm changes src/app.py inside the repo"),
        )
        self.assertIsNone(self.on_main(swap, "rm src/app.py", repo))

    def test_branch_guard_takes_the_command_name_from_the_lib(self):
        command = f"{self.token} rm src/app.py"
        real = self.plugin("real")
        repo = self.make_repo()
        self.assertIsNone(self.on_main(real, command, repo))
        wrap = self.instrumented("wrap")
        repo = self.make_repo()
        self.assertEqual(
            self.on_main(wrap, command, repo),
            self.denied_on_main(command, "rm changes src/app.py inside the repo"),
        )


# ================================================================ marker parts in readonly_bash


class MarkerPartsTests(unittest.TestCase):
    """readonly_bash.py, loaded as tests/test_hooks.py loads it, with the lib's walker:
    the marker parts around a subshell or a substitution refuse nothing, and the commands
    inside them are judged as before."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_lib()
        cls.hook = load_with_exec(HOOKS / "readonly_bash.py")

    def test_marker_parts_fall_through_part_refusal(self):
        for marker in ("(", ")"):
            for depth in (0, 3):
                with self.subTest(marker=marker, depth=depth):
                    part = self.lib.Part(marker=marker)
                    self.assertIsNone(self.hook.part_refusal(part, depth))

    def test_subshells_and_substitutions_are_judged_by_their_commands(self):
        for command, markers, found in (
            ("(ls)", ["(", "", ")"], None),
            ("(cd sub && mkdir out)", ["(", "", "", ")"], ("mkdir out", "mkdir changes files")),
            ('echo "$(rm x)"', ["", "(", "", ")"], ("rm x", "rm changes files")),
            ("echo `touch y`", ["", "(", "", ")"], ("touch y", "touch changes files")),
            ("diff <(git show HEAD:a.py) a.py", ["", "(", "", ")"], None),
            ("(ls) && (git status)", ["(", "", ")", "(", "", ")"], None),
            (
                "(ls) && (git stash)",
                ["(", "", ")", "(", "", ")"],
                ("git stash", "git stash can change files or git state"),
            ),
            ("echo $(ls) `pwd`", ["", "(", "", ")", "(", "", ")"], None),
            ("ls )", ["", ")"], None),
        ):
            with self.subTest(command=command):
                self.assertEqual([p.marker for p in self.hook.split_command(command)], markers)
                self.assertEqual(self.hook.refusal(command), found)


if __name__ == "__main__":
    unittest.main()
