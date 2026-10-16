"""Tests for plugins/harness/lib/harness_lib/hookio.py (ticket #87): the hook I/O helpers
branch_guard, tests_first, readonly_bash, session_brief and verify_on_stop share.

- hookio's six functions through their public interface, checked against what the five
  hooks' own copies do today: read_input on empty, invalid and non-object stdin;
  hook_cwd's precedence (cwd, else CLAUDE_PROJECT_DIR, else os.getcwd()); edited_path
  (file_path, else notebook_path, from tool_input); deny and emit writing byte-identical
  JSON (json.dumps defaults: ", " and ": ", ASCII escapes, key order kept, no trailing
  newline) and nothing on stderr; run_fail_open returning main()'s int, 0 for None, and
  on an exception exit 0 with today's one stderr line "<name>: <exc>".
- The five hooks carry no copies: no def of the six names, no renamed copy of hookio's
  bodies or of today's copies, none of the inlined forms (sys.stdin, sys.stdout, the
  PreToolUse decision dict, the CLAUDE_PROJECT_DIR fallback, notebook_path, a
  try/except Exception in the __main__ block), and the names each hook uses are bound
  from harness_lib.hookio. session_brief alone must still brief without the lib
  (tests/test_harness_lib.py), so a fallback inside the except branch of its
  harness_lib import is exempt from the static checks.
- End to end, today's bytes from each hook: the guards' deny JSON, session_brief's and
  verify_on_stop's JSON, and each hook's fail-open line on an internal crash.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_hookio.py" -v
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
from unittest import mock

sys.dont_write_bytecode = True  # loading the hooks and the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
HOOKIO_PY = LIB / "harness_lib" / "hookio.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
BRANCH = "t/12-fix-login"
SIX = ("read_input", "deny", "emit", "hook_cwd", "edited_path", "run_fail_open")
# The names each hook uses today (its own copy, or the inlined form).
USES = {
    "branch_guard.py": ("read_input", "deny", "hook_cwd", "edited_path", "run_fail_open"),
    "tests_first.py": ("read_input", "deny", "hook_cwd", "edited_path", "run_fail_open"),
    "readonly_bash.py": ("read_input", "deny", "run_fail_open"),
    "session_brief.py": ("read_input", "emit", "hook_cwd", "run_fail_open"),
    "verify_on_stop.py": ("read_input", "emit", "hook_cwd", "run_fail_open"),
}
HOOK_NAMES = tuple(USES)
# Only session_brief runs on without the lib, so only it may keep a fallback, and only
# inside the except branch of its harness_lib import.
FALLBACK_ALLOWED = ("session_brief.py",)

# Today's copies, verbatim: a renamed one left in a hook is still a copy.
TODAY = '''
def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def deny(reason: str) -> None:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    sys.stdout.write(json.dumps({"hookSpecificOutput": decision}))
    sys.stdout.flush()


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def hook_cwd(data):
    return str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
'''

_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-hookio-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def clean_env(tmp: Path) -> dict:
    """Built from scratch, so nothing leaks in from a Claude Code session running the
    tests (CLAUDE_PROJECT_DIR, CLAUDE_VERIFY_CMD, HARNESS_*)."""
    home = tmp / "home"
    tmpdir = tmp / "tmp"
    home.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    return dict(
        GIT_IDENTITY,
        HOME=str(home),
        TMPDIR=str(tmpdir),
        PATH=SYSTEM_PATH,
        LANG="en_US.UTF-8",
        PYTHONPYCACHEPREFIX=_SHARED["pycache"],
        PYTHONDONTWRITEBYTECODE="1",
        GIT_CEILING_DIRECTORIES=str(tmp),
    )


def load_hookio() -> types.ModuleType:
    """harness_lib.hookio imported from the checkout's lib/, as a hook's prologue would."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.hookio")


def load_with_loader(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_repo_key.py loads it."""
    modname = path.name.replace("-", "_").replace(".py", "") + "_hookio_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    """A hook loaded the way tests/test_guards.py and test_hooks.py load it."""
    module = types.ModuleType("harness_hook_hookio_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


# A driver run as `python3 -B -c DRIVER <op>`: imports hookio from $HOOKIO_TEST_LIB and
# calls one function the way a hook would, so stdin, stdout, stderr and the exit status
# are the real ones.
DRIVER = r'''
import json, os, sys
sys.path.insert(0, os.environ["HOOKIO_TEST_LIB"])
from harness_lib import hookio
op = sys.argv[1]
if op == "read_input":
    got = hookio.read_input()
    sys.stdout.write(json.dumps({"type": type(got).__name__, "value": got}))
elif op == "deny":
    hookio.deny(os.environ["HOOKIO_TEST_ARG"])
elif op == "deny-hard-exit":
    hookio.deny(os.environ["HOOKIO_TEST_ARG"])
    os._exit(0)
elif op == "emit":
    hookio.emit(json.loads(os.environ["HOOKIO_TEST_ARG"]))
elif op == "run_fail_open":
    mode, name = sys.argv[2], sys.argv[3]

    def main():
        if mode == "none":
            return None
        if mode.startswith("int:"):
            return int(mode[4:])
        if mode.startswith("raise:"):
            sys.stdout.write("partial")
            raise RuntimeError(mode[6:])
        if mode == "raise-empty":
            raise ValueError()
        if mode == "keyerror":
            return {}["missing-key"]
        if mode.startswith("exit:"):
            raise SystemExit(int(mode[5:]))
        raise AssertionError(mode)

    sys.exit(hookio.run_fail_open(name, main))
else:
    raise AssertionError(op)
'''


def deny_bytes(reason: str) -> bytes:
    """Today's PreToolUse deny, from first principles: json.dumps with its defaults."""
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    return json.dumps({"hookSpecificOutput": decision}).encode("ascii")


class DriverCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-hookio-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.token = "boom-" + uuid.uuid4().hex[:8]

    def drive(self, *args: str, stdin: bytes = b"", arg: str | None = None):
        env = dict(self.env, HOOKIO_TEST_LIB=str(LIB))
        if arg is not None:
            env["HOOKIO_TEST_ARG"] = arg
        return subprocess.run(
            [sys.executable, "-B", "-c", DRIVER, *args],
            input=stdin,
            capture_output=True,
            env=env,
            cwd=str(self.tmp),
            timeout=60,
        )


# ================================================================ hookio itself


class HookioModuleTests(DriverCase):
    def test_hookio_is_in_the_lib_with_the_six_functions(self):
        self.assertTrue(HOOKIO_PY.is_file(), f"{HOOKIO_PY} does not exist")
        hookio = load_hookio()
        self.assertEqual(os.path.realpath(hookio.__file__), os.path.realpath(str(HOOKIO_PY)))
        for name in SIX:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(hookio, name, None)), name)

    def test_read_input_parses_an_object_and_gives_empty_for_anything_else(self):
        cases = [
            (b"", {}),
            (b"   \n", {}),
            (b"{bad", {}),
            (b'{"cwd": "/x"', {}),
            (b"null", {}),
            (b"[1, 2]", {}),
            (b'"text"', {}),
            (b"42", {}),
            (b"true", {}),
            (b"{}", {}),
            # decoded as utf-8, not utf-8-sig: a BOM is invalid JSON, as today
            (b'\xef\xbb\xbf{"k": 1}', {}),
            (
                b'{"cwd": "/w/a", "tool_name": "Edit", "n": 3}',
                {"cwd": "/w/a", "tool_name": "Edit", "n": 3},
            ),
            (
                b'{"tool_input": {"command": "ls", "list": [1, null, false]}}',
                {"tool_input": {"command": "ls", "list": [1, None, False]}},
            ),
            (b'{"k": "caf\xc3\xa9"}', {"k": "café"}),
            # invalid UTF-8 is replaced, not refused
            (b'{"k": "a\xffb"}', {"k": "a�b"}),
        ]
        for stdin, expected in cases:
            with self.subTest(stdin=stdin):
                r = self.drive("read_input", stdin=stdin)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, b"")
                got = json.loads(r.stdout)
                self.assertEqual(got, {"type": "dict", "value": expected})

    def test_hook_cwd_prefers_cwd_then_the_project_dir_then_the_process_cwd(self):
        hookio = load_hookio()
        here = os.path.realpath(tempfile.mkdtemp(prefix="hookio-cwd-"))
        self.addCleanup(shutil.rmtree, here, True)
        tag = uuid.uuid4().hex[:6]
        given, project = f"/given/{tag}/a", f"/project/{tag}/b"
        cases = [
            ({"cwd": given}, project, given),
            ({"cwd": given}, None, given),
            ({"cwd": given + "-2", "x": 1}, "", given + "-2"),
            ({"cwd": ""}, project, project),
            ({"cwd": None}, project, project),
            ({}, project, project),
            ({}, project + "-2", project + "-2"),
            ({}, "", here),
            ({}, None, here),
            ({"cwd": ""}, None, here),
            ({"other": given}, None, here),
            ({"cwd": 7}, project, "7"),  # str() of whatever cwd holds, as today
        ]
        old = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, old)
        for data, env_value, expected in cases:
            with self.subTest(data=data, project_dir=env_value):
                with mock.patch.dict(os.environ):
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                    if env_value is not None:
                        os.environ["CLAUDE_PROJECT_DIR"] = env_value
                    got = hookio.hook_cwd(data)
                self.assertIsInstance(got, str)
                self.assertEqual(got, expected)

    def test_edited_path_is_file_path_else_notebook_path_from_tool_input(self):
        hookio = load_hookio()
        tag = uuid.uuid4().hex[:6]
        src, nb = f"/r{tag}/src/a.py", f"/r{tag}/nb/b.ipynb"
        cases = [
            ({"tool_input": {"file_path": src}}, src),
            ({"tool_input": {"file_path": src + "x", "content": "y"}}, src + "x"),
            ({"tool_input": {"notebook_path": nb}}, nb),
            ({"tool_input": {"file_path": src, "notebook_path": nb}}, src),
            ({"tool_input": {"file_path": "", "notebook_path": nb}}, nb),
            ({"tool_input": {"file_path": None, "notebook_path": f"rel{tag}/n.ipynb"}}, f"rel{tag}/n.ipynb"),
            ({"tool_input": {"file_path": f"~/{tag}.py"}}, f"~/{tag}.py"),  # not expanded
            ({"tool_input": {}}, None),
            ({"tool_input": {"content": src}}, None),
            ({"tool_input": None}, None),
            ({"tool_input": src}, None),
            ({"tool_input": [src]}, None),
            ({"file_path": src}, None),  # only tool_input counts
            ({}, None),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(hookio.edited_path(data), expected)

    def test_deny_writes_todays_pretooluse_json_and_nothing_on_stderr(self):
        literal = {
            "no": b'{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
            b'"permissionDecision": "deny", "permissionDecisionReason": "no"}}',
            "café …": b'{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
            b'"permissionDecision": "deny", "permissionDecisionReason": "caf\\u00e9 \\u2026"}}',
        }
        reasons = list(literal) + [
            'Refused "rm -rf build": it deletes files.',
            "line one\nline two\ttabbed",
            'back\\slash and "quotes"',
            "emoji \U0001f600 and ✓",
            "x" * 500 + "…",
            f"On main in proj-{uuid.uuid4().hex[:6]}.",
        ]
        for reason in reasons:
            with self.subTest(reason=reason[:40]):
                r = self.drive("deny", arg=reason)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, b"")
                self.assertEqual(r.stdout, literal.get(reason, deny_bytes(reason)))
                self.assertFalse(r.stdout.endswith(b"\n"))

    def test_deny_is_flushed_before_the_hook_goes_on(self):
        reason = f"flushed {self.token}"
        r = self.drive("deny-hard-exit", arg=reason)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(r.stdout, deny_bytes(reason))

    def test_emit_writes_json_dumps_with_its_defaults(self):
        cases = [
            ({"decision": "block", "reason": "x"}, b'{"decision": "block", "reason": "x"}'),
            (
                {"systemMessage": "verify_on_stop: café passed."},
                b'{"systemMessage": "verify_on_stop: caf\\u00e9 passed."}',
            ),
            ({"b": 1, "a": [1, 2.5, None, True]}, b'{"b": 1, "a": [1, 2.5, null, true]}'),
            ({}, b"{}"),
            (
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "## Session brief: x\n- line → two",
                        "sessionTitle": "x",
                    }
                },
                b'{"hookSpecificOutput": {"hookEventName": "SessionStart", '
                b'"additionalContext": "## Session brief: x\\n- line \\u2192 two", '
                b'"sessionTitle": "x"}}',
            ),
        ]
        tag = uuid.uuid4().hex[:8]
        varied = {"decision": "block", "reason": f"{tag} failed:\n..."}
        cases.append((varied, json.dumps(varied).encode("ascii")))
        for obj, expected in cases:
            with self.subTest(obj=obj):
                r = self.drive("emit", arg=json.dumps(obj))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, b"")
                self.assertEqual(r.stdout, expected)

    def test_run_fail_open_exits_with_mains_int_and_0_for_none(self):
        for mode, code in (("int:0", 0), ("int:1", 1), ("int:2", 2), ("int:7", 7), ("none", 0)):
            with self.subTest(mode=mode):
                r = self.drive("run_fail_open", mode, "after_edit.py")
                self.assertEqual((r.returncode, r.stdout, r.stderr), (code, b"", b""))

    def test_run_fail_open_returns_mains_int_in_process(self):
        hookio = load_hookio()
        for value, expected in ((0, 0), (2, 2), (5, 5), (None, 0)):
            with self.subTest(value=value):
                try:
                    got = hookio.run_fail_open("x.py", lambda: value)
                except SystemExit as stop:  # exiting with the code is as good as returning it
                    got = 0 if stop.code is None else stop.code
                self.assertEqual(got, expected)

    def test_run_fail_open_on_an_exception_writes_todays_line_and_exits_0(self):
        cases = [
            (f"raise:{self.token}", "branch_guard.py", f"branch_guard.py: {self.token}\n", b"partial"),
            (f"raise:{self.token}-2", "verify_on_stop.py", f"verify_on_stop.py: {self.token}-2\n", b"partial"),
            ("raise:café", "session_brief.py", "session_brief.py: café\n", b"partial"),
            ("raise-empty", "tests_first.py", "tests_first.py: \n", b""),
            ("keyerror", "readonly_bash.py", "readonly_bash.py: 'missing-key'\n", b""),
        ]
        for mode, name, line, out in cases:
            with self.subTest(mode=mode, name=name):
                r = self.drive("run_fail_open", mode, name)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr.decode("utf-8"), line)
                self.assertEqual(r.stdout, out)  # what main wrote before the crash stays

    def test_run_fail_open_lets_a_system_exit_through(self):
        """Today's tail catches Exception, so a SystemExit from main keeps its code."""
        r = self.drive("run_fail_open", "exit:3", "x.py")
        self.assertEqual((r.returncode, r.stderr), (3, b""))


# ================================================================ the hooks carry no copies


def hook_tree(name: str):
    return ast.parse((HOOKS / name).read_text(encoding="utf-8"), filename=name)


def normalized_body(fn) -> str:
    """The body of a def with its docstring dropped and every name and argument renamed,
    so a copy renamed (or calling renamed helpers) compares equal to the original."""
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


def body_size(fn) -> int:
    return sum(1 for stmt in fn.body for _ in ast.walk(stmt))


def defs(tree) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def imports_harness_lib(node) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and (n.module or "").startswith("harness_lib")
        for n in ast.walk(node)
    )


def fallback_ids(tree, hook: str) -> set:
    """id() of every node inside the except branch of a top-level try that imports from
    harness_lib, for the one hook allowed a lib-less fallback; empty for the others."""
    if hook not in FALLBACK_ALLOWED:
        return set()
    ids = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Try) and any(imports_harness_lib(s) for s in stmt.body):
            for handler in stmt.handlers:
                ids.update(id(n) for n in ast.walk(handler))
    return ids


def docstring_ids(tree) -> set:
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


def main_block(tree):
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name)
            and stmt.test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in stmt.test.comparators)
        ):
            return stmt
    return None


def catches_exception(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type
    if kinds is None:
        return True
    names = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


STD_STREAMS = ("stdin", "stdout", "__stdin__", "__stdout__")


def prologue_handlers(tree) -> list:
    """The except branches of the top-level try that imports from harness_lib."""
    return [
        handler
        for stmt in tree.body
        if isinstance(stmt, ast.Try) and any(imports_harness_lib(s) for s in stmt.body)
        for handler in stmt.handlers
    ]


def sys_names(tree) -> set:
    names = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys" and alias.asname:
                    names.add(alias.asname)
    return names


def builds_hook_output(call: ast.Call) -> bool:
    """json.dumps(...) of a dict literal keyed hookSpecificOutput: deny's or emit's body."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "dumps"):
        return False
    for arg in call.args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "hookSpecificOutput" for k in node.keys
            ):
                return True
    return False


def calls_run_fail_open_with_main(stmt) -> bool:
    """`run_fail_open("<name>", main)`, `sys.exit(run_fail_open(...))` or
    `raise SystemExit(run_fail_open(...))` as one plain statement: no condition,
    short-circuit or lambda around it."""
    if not isinstance(stmt, (ast.Expr, ast.Raise)):
        return False
    if any(isinstance(n, (ast.BoolOp, ast.IfExp, ast.Lambda)) for n in ast.walk(stmt)):
        return False
    for node in ast.walk(stmt):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_fail_open"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == "main"
        ):
            return True
    return False


def inlined_forms(tree, hook: str) -> list[str]:
    """Where a hook still does one of the helpers' jobs itself, outside an allowed
    fallback: touches the standard streams, builds the deny or hook-output JSON, spells
    the CLAUDE_PROJECT_DIR fallback or notebook_path, prints, or keeps a fail-open tail
    of its own. Static backstops; HooksCallHookioTests is the main guard."""
    skip = fallback_ids(tree, hook)
    docs = docstring_ids(tree)
    sysn = sys_names(tree)
    prologue = {id(h) for h in prologue_handlers(tree)}
    top_defs = {
        s.name for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys" and alias.asname:
                    found.append(f"import sys as {alias.asname} (line {line})")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            for alias in node.names:
                if alias.name in STD_STREAMS:
                    found.append(f"from sys import {alias.name} (line {line})")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in sysn
            and node.attr in STD_STREAMS
        ):
            found.append(f"{node.value.id}.{node.attr} (line {line}): read_input / deny / emit")
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in sysn
            ):
                found.append(f"getattr(sys, ...) (line {line})")
            if isinstance(func, ast.Name) and func.id == "print" and not any(
                k.arg == "file" for k in node.keywords
            ):
                found.append(f"print to stdout (line {line}): emit")
            if builds_hook_output(node):
                found.append(f"json.dumps of a hookSpecificOutput dict (line {line}): deny / emit")
            for kw in node.keywords:
                if kw.arg == "permissionDecision":
                    found.append(f"permissionDecision= (line {line}): deny")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            text = node.value
            if "permissionDecision" in text:
                found.append(f"{text!r} (line {line}): deny")
            if "PROJECT_DIR" in text:
                found.append(f"{text!r} (line {line}): hook_cwd")
            if "notebook" in text:
                found.append(f"{text!r} (line {line}): edited_path")
        elif isinstance(node, ast.ExceptHandler) and id(node) not in prologue and catches_exception(node):
            writes_stderr = any(
                isinstance(n, ast.Attribute)
                and n.attr in ("stderr", "__stderr__")
                and isinstance(n.value, ast.Name)
                and n.value.id in sysn
                for n in ast.walk(node)
            )
            if writes_stderr:
                found.append(f"except Exception writing stderr (line {line}): run_fail_open")
    block = main_block(tree)
    if block is None:
        found.append("no __main__ block")
        return found
    for node in ast.walk(block):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.ExceptHandler) and catches_exception(node):
            found.append(f"except Exception in __main__ (line {line}): run_fail_open")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            found.append(f"a function defined in __main__ (line {line})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in top_defs:
            found.append(f"__main__ calls {node.func.id}() itself (line {line}): run_fail_open")
    if not any(calls_run_fail_open_with_main(stmt) for stmt in block.body):
        found.append("__main__ does not run run_fail_open(<name>, main) unconditionally")
    return found


def reference_bodies() -> dict:
    """{normalised body: name} for hookio's functions and today's copies; bodies too
    small to tell apart from ordinary code (a one-call return) are left out."""
    sources = [ast.parse(TODAY)]
    if HOOKIO_PY.is_file():
        sources.append(ast.parse(HOOKIO_PY.read_text(encoding="utf-8")))
    found = {}
    for tree in sources:
        for fn in defs(tree):
            if fn.name in SIX and body_size(fn) >= 12:
                found[normalized_body(fn)] = fn.name
    return found


class HooksUseHookioTests(unittest.TestCase):
    def test_no_hook_defines_any_of_the_six(self):
        for hook in HOOK_NAMES:
            tree = hook_tree(hook)
            skip = fallback_ids(tree, hook)
            defined = sorted(
                {fn.name for fn in defs(tree) if id(fn) not in skip} & set(SIX)
            )
            with self.subTest(hook=hook):
                self.assertEqual(defined, [])

    def test_no_hook_carries_a_renamed_copy(self):
        refs = reference_bodies()
        self.assertTrue(HOOKIO_PY.is_file(), f"{HOOKIO_PY} does not exist")
        for hook in HOOK_NAMES:
            tree = hook_tree(hook)
            skip = fallback_ids(tree, hook)
            copies = [
                f"{fn.name} (line {fn.lineno}) = {refs[normalized_body(fn)]}"
                for fn in defs(tree)
                if id(fn) not in skip and normalized_body(fn) in refs
            ]
            with self.subTest(hook=hook):
                self.assertEqual(copies, [])

    def test_no_hook_spells_the_helpers_inline(self):
        for hook in HOOK_NAMES:
            with self.subTest(hook=hook):
                self.assertEqual(inlined_forms(hook_tree(hook), hook), [])

    def check_bound_from_hookio(self, module: types.ModuleType, hook: str) -> None:
        self.assertTrue(HOOKIO_PY.is_file(), f"{HOOKIO_PY} does not exist")
        hookio = os.path.realpath(str(HOOKIO_PY))
        for name in USES[hook]:
            with self.subTest(hook=hook, name=name):
                bound = module.__dict__.get(name)  # in the hook's own namespace
                self.assertTrue(callable(bound), f"{hook} does not bind {name}")
                self.assertEqual(bound.__module__, "harness_lib.hookio")
                self.assertEqual(os.path.realpath(inspect.getsourcefile(bound)), hookio)

    def test_names_bound_from_hookio_with_source_file_loader(self):
        for hook in HOOK_NAMES:
            self.check_bound_from_hookio(load_with_loader(HOOKS / hook), hook)

    def test_names_bound_from_hookio_in_a_bare_module(self):
        for hook in HOOK_NAMES:
            self.check_bound_from_hookio(load_with_exec(HOOKS / hook), hook)


# ================================================================ today's bytes, end to end


# Runs a hook as __main__ with json.loads raising, so main() crashes inside its first
# read; the hook's tail decides what is printed.
CRASH_DRIVER = r'''
import json, os, runpy, sys
token = os.environ["HOOKIO_TEST_TOKEN"]

def loads(*args, **kwargs):
    raise RuntimeError(token)

json.loads = loads
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
'''


class HookRunCase(unittest.TestCase):
    """Fixtures for running the hooks as hooks.json runs them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-hookio-e2e-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_env(self.tmp)
        self.token = "crash-" + uuid.uuid4().hex[:8]
        self.cases = 0

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=self.env
        )

    def make_repo(self, name: str = "proj") -> Path:
        self.cases += 1
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

    def run_hook(self, hook: str, payload: dict, cwd: Path, env: dict | None = None, root: Path = HOOKS):
        return subprocess.run(
            [sys.executable, str(root / hook)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=dict(self.env, **(env or {})),
            cwd=str(cwd),
            timeout=120,
        )

    def pretool(self, tool: str, tool_input: dict, cwd: Path, agent: str | None = None) -> dict:
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

    def assert_exact_deny(self, r, prefix: str) -> str:
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertTrue(r.stdout, "no decision")
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertTrue(reason.startswith(prefix), reason)
        self.assertEqual(r.stdout, deny_bytes(reason))
        return reason


class HookBytesTests(HookRunCase):
    def test_the_guards_deny_with_todays_bytes(self):
        # branch_guard: an edit on main in a repo whose name is not ASCII
        repo = self.make_repo("café")
        tool_input = {"file_path": str(repo / "src/app.py"), "old_string": "a", "new_string": "b"}
        with self.subTest(hook="branch_guard.py"):
            r = self.run_hook("branch_guard.py", self.pretool("Edit", tool_input, repo), repo)
            self.assert_exact_deny(r, "On main in caf")
            self.assertTrue(r.stdout.isascii() and b"\\u" in r.stdout, r.stdout)
        # tests_first: a source edit on a ticket branch with no red run
        repo = self.make_repo()
        wt = repo.parent / "wt"
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        tool_input = {"file_path": str(wt / "src/app.py"), "old_string": "a", "new_string": "b"}
        with self.subTest(hook="tests_first.py"):
            r = self.run_hook("tests_first.py", self.pretool("Edit", tool_input, wt), wt)
            self.assert_exact_deny(r, "Tests first on t/12")
        # readonly_bash: a long write command from the reviewer, cut with an ellipsis
        command = "rm -rf build/" + "x" * 200
        tool_input = {"command": command, "description": "Run it"}
        with self.subTest(hook="readonly_bash.py"):
            r = self.run_hook(
                "readonly_bash.py", self.pretool("Bash", tool_input, self.tmp, "harness:reviewer"), self.tmp
            )
            reason = self.assert_exact_deny(r, 'Refused "rm -rf build/')
            self.assertIn("…", reason)
            self.assertIn(b"\\u2026", r.stdout)

    def test_session_brief_writes_todays_bytes(self):
        repo = self.make_repo("café")
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        r = self.run_hook("session_brief.py", payload, repo)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        parsed = json.loads(r.stdout)
        self.assertEqual(list(parsed), ["hookSpecificOutput"])
        out = parsed["hookSpecificOutput"]
        self.assertEqual(list(out), ["hookEventName", "additionalContext", "sessionTitle"])
        self.assertTrue(out["sessionTitle"].startswith("caf"), out)
        self.assertEqual(r.stdout, json.dumps(parsed).encode("ascii"))
        self.assertTrue(r.stdout.isascii() and b"\\u" in r.stdout, r.stdout)

    def test_verify_on_stop_writes_todays_bytes(self):
        repo = self.make_repo()
        src = repo / "app.py"
        src.write_text("x = 1\n")
        edit = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
        }
        r = self.run_hook("after_edit.py", edit, repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        stop = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        cmd = "printf 'caf\\303\\251 broke\\n'; exit 1"
        r = self.run_hook("verify_on_stop.py", stop, repo, {"CLAUDE_VERIFY_CMD": cmd})
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        parsed = json.loads(r.stdout)
        self.assertEqual(list(parsed), ["decision", "reason"])
        self.assertEqual(parsed["decision"], "block")
        self.assertIn("café broke", parsed["reason"])
        self.assertEqual(r.stdout, json.dumps(parsed).encode("ascii"))
        self.assertIn(b"caf\\u00e9 broke", r.stdout)

    def test_each_hook_fails_open_on_an_internal_crash_with_todays_line(self):
        repo = self.make_repo()
        payload = self.pretool("Edit", {"file_path": str(repo / "src/app.py")}, repo)
        for hook in HOOK_NAMES:
            with self.subTest(hook=hook):
                r = subprocess.run(
                    [sys.executable, "-B", "-c", CRASH_DRIVER, str(HOOKS / hook)],
                    input=json.dumps(payload).encode("utf-8"),
                    capture_output=True,
                    env=dict(self.env, HOOKIO_TEST_TOKEN=self.token),
                    cwd=str(repo),
                    timeout=60,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, b"")
                self.assertEqual(r.stderr.decode("utf-8"), f"{hook}: {self.token}\n")

    def plugin(self, lib: str) -> Path:
        """hooks/ copied from the checkout next to a lib/ that is missing or raises."""
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "raises":
            pkg = root / "lib" / "harness_lib"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(f"raise RuntimeError({self.token!r})\n")
        return root

    def test_readonly_bash_and_verify_on_stop_fail_open_without_the_lib(self):
        """The two hooks that gain the lib import keep the fail-open rule: exit 0, no
        traceback, and at most the one stderr line naming harness_lib."""
        plain = self.tmp / "plain"
        plain.mkdir()
        bash = self.pretool("Bash", {"command": "rm -rf build"}, plain, "harness:reviewer")
        stop = {"session_id": "sess-1", "cwd": str(plain), "hook_event_name": "Stop"}
        for lib in ("missing", "raises"):
            root = self.plugin(lib)
            for hook, payload in (("readonly_bash.py", bash), ("verify_on_stop.py", stop)):
                with self.subTest(lib=lib, hook=hook):
                    r = self.run_hook(hook, payload, plain, {"CLAUDE_VERIFY_CMD": "true"}, root / "hooks")
                    err = r.stderr.decode("utf-8")
                    self.assertEqual(r.returncode, 0, err)
                    self.assertNotIn("Traceback", err)
                    lines = err.splitlines()
                    self.assertLessEqual(len(lines), 1, err)
                    if lines:
                        self.assertTrue(lines[0].startswith(f"{hook}:"), lines[0])
                        self.assertIn("harness_lib", lines[0])


# ================================================================ the hooks use hookio at runtime


# The lib for HooksCallHookioTests: the real paths.py, the real hookio.py loaded as
# harness_lib._real_hookio, and in hookio's place this module, which records each call
# and steers it. read_input returns $HOOKIO_TEST_DATA instead of what stdin held (or
# raises $HOOKIO_TEST_CRASH), hook_cwd and edited_path return $HOOKIO_TEST_CWD and
# $HOOKIO_TEST_EDITED, deny marks the reason, emit adds a key, run_fail_open marks the
# name. A hook that does any of these jobs itself shows it in its output.
TRACED_HOOKIO = '''from __future__ import annotations

import json as _json
import os as _os

from harness_lib import _real_hookio as _real

MARK = _os.environ.get("HOOKIO_TEST_MARK", "")


def _record(name):
    path = _os.environ.get("HOOKIO_TEST_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(name + "\\n")


def read_input():
    _record("read_input")
    data = _real.read_input()
    crash = _os.environ.get("HOOKIO_TEST_CRASH")
    if crash:
        raise RuntimeError(crash)
    swap = _os.environ.get("HOOKIO_TEST_DATA")
    return _json.loads(swap) if swap else data


def hook_cwd(data):
    _record("hook_cwd")
    got = _real.hook_cwd(data)
    return _os.environ.get("HOOKIO_TEST_CWD") or got


def edited_path(data):
    _record("edited_path")
    got = _real.edited_path(data)
    return _os.environ.get("HOOKIO_TEST_EDITED") or got


def deny(reason):
    _record("deny")
    return _real.deny("[" + MARK + "] " + reason)


def emit(obj):
    _record("emit")
    return _real.emit(dict(obj, hookio_mark=MARK))


def run_fail_open(name, main):
    _record("run_fail_open")
    return _real.run_fail_open(name + "<" + MARK + ">", main)
'''


class HooksCallHookioTests(HookRunCase):
    """Each hook, run as hooks.json runs it next to a lib whose hookio steers every
    answer, does what hookio's answers imply: its input, cwd, edited path, output and
    fail-open tail come from hookio at runtime, not from a copy of its own.

    The event on stdin is always one the hook allows or ignores; only the data hookio's
    read_input hands back (with a decoy cwd, and for the guards a decoy path that is
    allowed) leads to a decision, and only hookio's hook_cwd and edited_path point it at
    the real repo and file."""

    def setUp(self) -> None:
        super().setUp()
        self.mark = "hookio-" + uuid.uuid4().hex[:8]
        self.decoy = self.tmp / f"decoy-{uuid.uuid4().hex[:6]}"
        self.decoy.mkdir()

    def traced_plugin(self) -> Path:
        root = self.tmp / "plugin-traced"
        if root.is_dir():
            return root
        self.assertTrue(HOOKIO_PY.is_file(), f"{HOOKIO_PY} does not exist")
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        pkg = root / "lib" / "harness_lib"
        shutil.copyfile(HOOKIO_PY, pkg / "_real_hookio.py")
        (pkg / "hookio.py").write_text(TRACED_HOOKIO, encoding="utf-8")
        return root

    def run_traced(self, hook: str, stdin: dict, data: dict | None, env: dict):
        """(result, the hookio names the hook called)."""
        trace = self.tmp / f"trace-{uuid.uuid4().hex[:8]}"
        extra = dict(env, HOOKIO_TEST_MARK=self.mark, HOOKIO_TEST_TRACE=str(trace))
        if data is not None:
            extra["HOOKIO_TEST_DATA"] = json.dumps(data)
        r = self.run_hook(hook, stdin, self.decoy, extra, self.traced_plugin() / "hooks")
        called = trace.read_text(encoding="utf-8").split() if trace.is_file() else []
        return r, set(called)

    def session_event(self, cwd: Path, source: str) -> dict:
        return {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": source,
        }

    def stop_event(self, session: str) -> dict:
        return {
            "session_id": session,
            "transcript_path": "/dev/null",
            "cwd": str(self.decoy),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }

    def steered(self, hook: str):
        """Run `hook` on its steered event; return (result, called, check), where
        check(result) asserts the decision the steered answers imply."""
        quiet = self.pretool("Read", {"file_path": str(self.decoy / "README.md")}, self.decoy)
        tag = uuid.uuid4().hex[:6]
        if hook == "branch_guard.py":
            repo = self.make_repo(f"bg{tag}")
            # the decoy path is an agent note: allowed even on main
            data = self.pretool("Edit", {"file_path": "docs/agent/handoff.md", "old_string": "a", "new_string": "b"}, self.decoy)
            env = {"HOOKIO_TEST_CWD": str(repo), "HOOKIO_TEST_EDITED": "src/app.py"}
            prefix = f"[{self.mark}] On main in bg{tag}."
        elif hook == "tests_first.py":
            repo = self.make_repo(f"tf{tag}")
            wt = repo.parent / "wt"
            self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
            # the decoy path is a test file: allowed before the red run
            data = self.pretool("Edit", {"file_path": "tests/test_app.py", "old_string": "a", "new_string": "b"}, self.decoy)
            env = {"HOOKIO_TEST_CWD": str(wt), "HOOKIO_TEST_EDITED": "src/app.py"}
            prefix = f"[{self.mark}] Tests first on t/12"
        elif hook == "readonly_bash.py":
            quiet = self.pretool("Bash", {"command": "ls"}, self.decoy)  # the main session
            data = self.pretool("Bash", {"command": f"rm -rf build{tag}"}, self.decoy, "harness:reviewer")
            env = {}
            prefix = f'[{self.mark}] Refused "rm -rf build{tag}"'
        if hook in ("branch_guard.py", "tests_first.py", "readonly_bash.py"):
            r, called = self.run_traced(hook, quiet, data, env)

            def check(r):
                self.assertEqual((r.returncode, r.stderr), (0, b""))
                self.assertTrue(r.stdout, "no decision: the hook did not act on hookio's answers")
                reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertTrue(reason.startswith(prefix), reason)

            return r, called, check
        if hook == "session_brief.py":
            project = self.make_repo(f"sb{tag}")
            # stdin says resume (no title); hookio's data says startup
            r, called = self.run_traced(
                hook,
                self.session_event(self.decoy, "resume"),
                self.session_event(self.decoy, "startup"),
                {"HOOKIO_TEST_CWD": str(project)},
            )

            def check(r):
                self.assertEqual((r.returncode, r.stderr), (0, b""))
                parsed = json.loads(r.stdout)
                self.assertEqual(parsed.get("hookio_mark"), self.mark, "output not written by hookio.emit")
                out = parsed["hookSpecificOutput"]
                self.assertEqual(out.get("sessionTitle"), f"sb{tag}")
                self.assertTrue(out["additionalContext"].startswith(f"## Session brief: sb{tag}"), out)

            return r, called, check
        self.assertEqual(hook, "verify_on_stop.py")
        repo = self.make_repo(f"vs{tag}")
        src = repo / "app.py"
        src.write_text("x = 1\n")
        edit = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(src)},
        }
        r = self.run_hook("after_edit.py", edit, repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        # stdin names another session, in the decoy dir; hookio's data names the edited one
        cmd = f"echo nope-{tag}; exit 1"
        r, called = self.run_traced(
            hook,
            self.stop_event("sess-other"),
            self.stop_event("sess-1"),
            {"HOOKIO_TEST_CWD": str(repo), "CLAUDE_VERIFY_CMD": cmd},
        )

        def check(r):
            self.assertEqual((r.returncode, r.stderr), (0, b""))
            self.assertTrue(r.stdout, "no output: the hook did not act on hookio's answers")
            parsed = json.loads(r.stdout)
            self.assertEqual(parsed.get("hookio_mark"), self.mark, "output not written by hookio.emit")
            self.assertEqual(parsed.get("decision"), "block")
            self.assertIn(f"nope-{tag}", parsed["reason"])

        return r, called, check

    def test_the_steered_lib_leaves_todays_hooks_their_own_answers(self):
        """Control: the stdin events alone are allowed or silent, so a decision in the
        tests below can only come from hookio's steered answers."""
        quiet = self.pretool("Read", {"file_path": str(self.decoy / "README.md")}, self.decoy)
        for hook in ("branch_guard.py", "tests_first.py"):
            with self.subTest(hook=hook):
                r = self.run_hook(hook, quiet, self.decoy)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        r = self.run_hook("readonly_bash.py", self.pretool("Bash", {"command": "ls"}, self.decoy), self.decoy)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_each_hook_acts_on_hookios_input_cwd_and_path_and_answers_through_it(self):
        for hook in HOOK_NAMES:
            with self.subTest(hook=hook):
                r, _, check = self.steered(hook)
                check(r)

    def test_each_hook_calls_every_hookio_name_it_uses(self):
        for hook in HOOK_NAMES:
            with self.subTest(hook=hook):
                _, called, _ = self.steered(hook)
                self.assertEqual(sorted(set(USES[hook]) - called), [])

    def test_each_hook_fails_open_through_hookios_run_fail_open(self):
        token = "crash-" + uuid.uuid4().hex[:8]
        quiet = self.pretool("Read", {"file_path": str(self.decoy / "README.md")}, self.decoy)
        for hook in HOOK_NAMES:
            with self.subTest(hook=hook):
                r, called = self.run_traced(hook, quiet, None, {"HOOKIO_TEST_CRASH": token})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, b"")
                self.assertEqual(r.stderr.decode("utf-8"), f"{hook}<{self.mark}>: {token}\n")
                self.assertIn("run_fail_open", called)


if __name__ == "__main__":
    unittest.main()
