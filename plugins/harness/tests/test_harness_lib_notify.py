"""Tests for plugins/harness/lib/harness_lib/notify.py (ticket #86): one notification
function for harness-board, harness-pr, harness-digest and hooks/notify.py.

- The lib's notify(message, group, title="Harness", subtitle="") -> bool, run in a
  subprocess with only fake notifiers on PATH: the terminal-notifier argv in the order
  board uses (-title, -subtitle only when non-empty, -message, -group, -activate, -sound),
  HARNESS_NOTIFY_APP / HARNESS_NOTIFY_SOUND (unset: com.microsoft.VSCode / Glass; empty
  or blank: that part off), and the osascript fallback as hooks/notify.py does it (when
  terminal-notifier is missing, fails, or cannot be run), never raising.
- env_setting, succeeded and applescript_string, DEFAULT_APP and DEFAULT_SOUND.
- One copy: every Python file under the plugin but the lib's notify.py (the bin tools,
  the hooks, the rest of the lib, the templates; tests aside) defines none of the helpers
  or an argv builder of its own, spells none of the notifier names, flags or settings
  (with string pieces folded, so "ter" + "minal-notifier" counts), and assigns neither
  default; the three bin tools and hooks/notify.py bind and use the lib's notify from
  harness_lib.notify. The test-breaker's round 2 parked the old copy in a second lib
  module as a fallback the tools called when the lib said False: the scan covers every
  file for that.
- Each bin tool's notifying path (board needs-input, pr ready, the digest), driven with
  its own test module's fixtures from a plugin copy whose lib notify is instrumented,
  hands the lib its message, group and title exactly once, whether the lib answers True
  or False, runs no notifier of its own either way, and reports the lib's answer where it
  reports one (board --json and the digest --json "notified").
- hooks/notify.py, from a plugin tree whose lib is instrumented, hands the lib's notify
  its message, group, title and subtitle, and rings the bell only when notify says False;
  with the lib missing it fails open with one stderr line, as the #79 hooks do.

The fake notifier logs as tests/test_board.py's does (one JSON argv per line in
$NOTIFY_LOG); tests/test_board.py, test_pr.py, test_digest.py and test_hooks.py are the
unchanged guard of what each tool sends.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_notify.py" -v
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import os
import re
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

sys.dont_write_bytecode = True  # loading the tools and the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
NOTIFY_PY = LIB / "harness_lib" / "notify.py"
BEL = "\u0007"

if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import test_board as tb  # noqa: E402  the guard modules: their fixtures drive each tool's notifying path
import test_digest as td  # noqa: E402
import test_pr as tp  # noqa: E402

# The four places that carried a copy, per the ticket.
COPIES = {
    "harness-board": BIN / "harness-board",
    "harness-pr": BIN / "harness-pr",
    "harness-digest": BIN / "harness-digest",
    "hooks/notify.py": HOOKS / "notify.py",
}
HELPERS = ("env_setting", "succeeded", "applescript_string")
# Functions that are a copy by their name alone: the helpers and the hook's two via_
# builders. Any other function, whatever its name (old_notify, ping, notify), is a builder
# when it does what one does: it runs a process (subprocess.*, shutil.which, os.exec*,
# os.spawn*, os.system, os.popen, directly or through a function of the same file that
# does) and its body spells a notifier name or flag (folded). A notify_on_merge that runs
# gh and then calls the lib's notify is not one.
NAMED_BUILDERS = HELPERS + ("via_terminal_notifier", "via_osascript")
RUNNER_MODULES = ("subprocess", "shutil", "os")
# Strings that spell a notifier name legitimately, outside any notification block, as
# (file relative to the plugin) -> the exact whole strings allowed there: the tests-first
# hook lists osascript among the interpreters `find -exec` may run. Nothing else today.
ALLOWED_SPELLINGS = {"hooks/tests_first.py": {"osascript"}}

FAKE_NOTIFIER = """#!{python}
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["NOTIFY_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([name] + sys.argv[1:]) + "\\n")
sys.exit(1 if name in os.environ.get("FAKE_FAIL", "").split(",") else 0)
"""

# Exits 0 only when its stdin is empty; writes to both streams.
STDIN_PROBE = """#!{python}
import sys
data = sys.stdin.read()
sys.stdout.write("LEAK-OUT\\n")
sys.stderr.write("LEAK-ERR\\n")
sys.exit(0 if data == "" else 3)
"""

# An executable whose interpreter does not exist: running it raises OSError.
UNRUNNABLE = "#!/nonexistent/harness-test-interpreter\n"

DRIVER = """
import json, sys
sys.path.insert(0, {lib!r})
import harness_lib.notify as lib
spec = json.loads(sys.argv[1])
fn = getattr(lib, spec["fn"])
result = fn(*spec.get("args", []), **spec.get("kwargs", {{}}))
sys.stdout.write("RESULT " + json.dumps(result) + "\\n")
"""

_SHARED: dict = {}


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def setUpModule() -> None:
    """Fake notifiers written once and linked under each name into one bin per PATH."""
    base = Path(tempfile.mkdtemp(prefix="harness-notify-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")
    fake = executable(base / "fake-notifier", FAKE_NOTIFIER.format(python=sys.executable))
    probe = executable(base / "stdin-probe", STDIN_PROBE.format(python=sys.executable))
    bad = executable(base / "unrunnable", UNRUNNABLE)
    layouts = {
        "both": {"terminal-notifier": fake, "osascript": fake},
        "tn": {"terminal-notifier": fake},
        "osascript": {"osascript": fake},
        "none": {},
        "bad-tn": {"terminal-notifier": bad, "osascript": fake},
        "bad-tn-only": {"terminal-notifier": bad},
        "bad-both": {"terminal-notifier": bad, "osascript": bad},
        "probe": {"stdin-probe": probe, "fake-ok": fake},
    }
    for folder, names in layouts.items():
        (base / f"bin-{folder}").mkdir()
        for name, target in names.items():
            os.symlink(target, base / f"bin-{folder}" / name)


    # the guard modules' fixtures (fake gh, recorders, the digest's repositories), which
    # the *NotifiesThroughTheLib classes reuse
    for module in (tb, tp, td):
        module.setUpModule()


def tearDownModule() -> None:
    for module in (tb, tp, td):
        module.tearDownModule()
    _SHARED.pop("sentinel_tree", None)
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def quoted(text: str) -> str:
    """An AppleScript string literal, built here character by character: backslash and
    double quote escaped with a backslash, everything else as is, in double quotes."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def expected_tn(message, group, title="Harness", subtitle="", app="com.microsoft.VSCode", sound="Glass"):
    """The terminal-notifier argv (after the program name), as board orders it."""
    argv = ["-title", title]
    if subtitle:
        argv += ["-subtitle", subtitle]
    argv += ["-message", message, "-group", group]
    if app:
        argv += ["-activate", app]
    if sound:
        argv += ["-sound", sound]
    return ["terminal-notifier"] + argv


def expected_osa(message, title="Harness", subtitle="", sound="Glass"):
    script = f"display notification {quoted(message)} with title {quoted(title)}"
    if subtitle:
        script += f" subtitle {quoted(subtitle)}"
    if sound:
        script += f" sound name {quoted(sound)}"
    return ["osascript", "-e", script]


def load_lib():
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.notify")


# ================================================================ the lib, run with fake notifiers


class LibTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-notify-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "notify.log"

    def call(self, fn: str, *args, bins: str = "both", env: dict | None = None, stdin: str = "", **kwargs):
        """Run harness_lib.notify.<fn>(*args, **kwargs) in a fresh python3 whose PATH holds
        only bin-<bins>; returns (result, completed process)."""
        run_env = {
            "PATH": str(_SHARED["base"] / f"bin-{bins}"),
            "HOME": str(self.tmp),
            "LANG": "en_US.UTF-8",
            "NOTIFY_LOG": str(self.log),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": _SHARED["pycache"],
        }
        run_env.update(env or {})
        spec = json.dumps({"fn": fn, "args": list(args), "kwargs": kwargs})
        r = subprocess.run(
            [sys.executable, "-c", DRIVER.format(lib=str(LIB)), spec],
            input=stdin,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        self.assertEqual(len(lines), 1, r.stdout)
        return json.loads(lines[0][len("RESULT "):]), r

    def logged(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]

    def reset_log(self) -> None:
        if self.log.exists():
            self.log.unlink()


class LibNotifyArgvTests(LibTestCase):
    """notify() through terminal-notifier: the argv board sends, plus -subtitle when given."""

    CASES = [
        # (args, kwargs, env, expected terminal-notifier argv)
        (
            ["Build done", "harness-repo-12"],
            {},
            {},
            expected_tn("Build done", "harness-repo-12"),
        ),
        (
            ['Say "hi" \\ there', "claude-code-abc123"],
            {"title": "Permission needed", "subtitle": "myrepo"},
            {"HARNESS_NOTIFY_APP": "com.googlecode.iterm2", "HARNESS_NOTIFY_SOUND": "Ping"},
            expected_tn('Say "hi" \\ there', "claude-code-abc123", "Permission needed", "myrepo",
                        "com.googlecode.iterm2", "Ping"),
        ),
        (
            ["third message", "g-3"],
            {"title": "T3", "subtitle": ""},
            {"HARNESS_NOTIFY_APP": "", "HARNESS_NOTIFY_SOUND": ""},
            expected_tn("third message", "g-3", "T3", "", "", ""),
        ),
        (
            ["padded settings", "harness-digest"],
            {"subtitle": "proj"},
            {"HARNESS_NOTIFY_APP": "  com.apple.Terminal \n"},
            expected_tn("padded settings", "harness-digest", "Harness", "proj", "com.apple.Terminal", "Glass"),
        ),
        (
            ["blank sound", "harness-x-pr-7"],
            {"title": "Claude Code"},
            {"HARNESS_NOTIFY_SOUND": "   "},
            expected_tn("blank sound", "harness-x-pr-7", "Claude Code", "", "com.microsoft.VSCode", ""),
        ),
        (
            ["café ✓ done", "grp-ü"],
            {"title": "Ünï", "subtitle": "naïve repo"},
            {"HARNESS_NOTIFY_SOUND": " Submarine "},
            expected_tn("café ✓ done", "grp-ü", "Ünï", "naïve repo",
                        "com.microsoft.VSCode", "Submarine"),
        ),
    ]

    def test_terminal_notifier_argv_over_a_table_of_inputs(self):
        for args, kwargs, env, expected in self.CASES:
            with self.subTest(args=args, kwargs=kwargs, env=env):
                self.reset_log()
                result, _ = self.call("notify", *args, env=env, **kwargs)
                self.assertIs(result, True)
                self.assertEqual(self.logged(), [expected])  # osascript not reached

    def test_title_and_subtitle_positionally(self):
        result, _ = self.call("notify", "pos msg", "pos-group", "Pos Title", "pos-sub")
        self.assertIs(result, True)
        self.assertEqual(self.logged(), [expected_tn("pos msg", "pos-group", "Pos Title", "pos-sub")])

    def test_subtitle_only_when_given(self):
        """-subtitle sits between the title and -message when non-empty, and is absent
        otherwise; every other flag keeps its place."""
        for subtitle in ("", "repo-a", "repo b"):
            with self.subTest(subtitle=subtitle):
                self.reset_log()
                self.call("notify", "m", "g", subtitle=subtitle)
                (argv,) = self.logged()
                flags = [a for a in argv[1:] if a.startswith("-")]
                if subtitle:
                    self.assertEqual(flags, ["-title", "-subtitle", "-message", "-group", "-activate", "-sound"])
                    self.assertEqual(argv[argv.index("-subtitle") + 1], subtitle)
                else:
                    self.assertEqual(flags, ["-title", "-message", "-group", "-activate", "-sound"])

    def test_only_terminal_notifier_on_path(self):
        result, _ = self.call("notify", "alone", "g-alone", bins="tn")
        self.assertIs(result, True)
        self.assertEqual(self.logged(), [expected_tn("alone", "g-alone")])


class LibNotifyFallbackTests(LibTestCase):
    """notify() through osascript, as hooks/notify.py does it, and the False cases."""

    CASES = [
        # (args, kwargs, env)
        (['Say "hi" \\ path', "g1"], {}, {}),
        (["needs input", "g2"], {"title": "Claude Code", "subtitle": "myrepo"}, {"HARNESS_NOTIFY_SOUND": "Ping"}),
        (["quiet", "g3"], {"title": 'Ti"tle', "subtitle": "s\\b"}, {"HARNESS_NOTIFY_SOUND": "", "HARNESS_NOTIFY_APP": "x.y"}),
        (["café", "g4"], {"subtitle": ""}, {"HARNESS_NOTIFY_SOUND": "  Hero  "}),
    ]

    @staticmethod
    def sound_of(env: dict) -> str:
        value = env.get("HARNESS_NOTIFY_SOUND")
        return "Glass" if value is None else value.strip()

    def test_osascript_when_terminal_notifier_is_missing(self):
        for args, kwargs, env in self.CASES:
            with self.subTest(args=args, kwargs=kwargs, env=env):
                self.reset_log()
                result, _ = self.call("notify", *args, bins="osascript", env=env, **kwargs)
                self.assertIs(result, True)
                expected = expected_osa(
                    args[0], kwargs.get("title", "Harness"), kwargs.get("subtitle", ""), self.sound_of(env)
                )
                self.assertEqual(self.logged(), [expected])

    def test_failed_terminal_notifier_falls_back_to_osascript(self):
        for args, kwargs, env in self.CASES:
            with self.subTest(args=args, kwargs=kwargs, env=env):
                self.reset_log()
                env = dict(env, FAKE_FAIL="terminal-notifier")
                result, _ = self.call("notify", *args, bins="both", env=env, **kwargs)
                self.assertIs(result, True)
                app = env.get("HARNESS_NOTIFY_APP")
                app = "com.microsoft.VSCode" if app is None else app.strip()
                title, subtitle = kwargs.get("title", "Harness"), kwargs.get("subtitle", "")
                self.assertEqual(
                    self.logged(),
                    [
                        expected_tn(args[0], args[1], title, subtitle, app, self.sound_of(env)),
                        expected_osa(args[0], title, subtitle, self.sound_of(env)),
                    ],
                )

    def test_unrunnable_terminal_notifier_falls_back_without_raising(self):
        result, r = self.call("notify", "odd exe", "g-odd", bins="bad-tn", subtitle="sub")
        self.assertIs(result, True)
        self.assertEqual(self.logged(), [expected_osa("odd exe", "Harness", "sub")])

    def test_false_when_nothing_can_notify(self):
        cases = [
            ("none", {}, []),
            ("both", {"FAKE_FAIL": "terminal-notifier,osascript"}, ["terminal-notifier", "osascript"]),
            ("osascript", {"FAKE_FAIL": "osascript"}, ["osascript"]),
            ("tn", {"FAKE_FAIL": "terminal-notifier"}, ["terminal-notifier"]),
            ("bad-tn-only", {}, []),
            ("bad-both", {}, []),
        ]
        for bins, env, tried in cases:
            with self.subTest(bins=bins, env=env):
                self.reset_log()
                result, r = self.call("notify", "nobody", "g-none", bins=bins, env=env)
                self.assertIs(result, False)
                self.assertEqual([argv[0] for argv in self.logged()], tried)


class LibHelperTests(LibTestCase):
    """env_setting, succeeded, applescript_string and the two defaults."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = load_lib()

    def test_module_is_in_lib_with_its_names(self):
        self.assertTrue(NOTIFY_PY.is_file(), f"{NOTIFY_PY} does not exist")
        self.assertEqual(os.path.realpath(self.lib.__file__), os.path.realpath(str(NOTIFY_PY)))
        for name in HELPERS + ("notify",):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(self.lib, name, None)))
        self.assertEqual(self.lib.DEFAULT_APP, "com.microsoft.VSCode")
        self.assertEqual(self.lib.DEFAULT_SOUND, "Glass")
        params = list(inspect.signature(self.lib.notify).parameters.values())
        self.assertEqual([p.name for p in params], ["message", "group", "title", "subtitle"])
        self.assertEqual([p.default for p in params[2:]], ["Harness", ""])

    def test_env_setting(self):
        name = "HARNESS_NOTIFY_TEST_" + uuid.uuid4().hex[:8].upper()
        cases = [
            # (value or None for unset, default, expected)
            (None, "fallback-a", "fallback-a"),
            (None, "", ""),
            ("value", "fallback-b", "value"),
            ("  padded\t\n", "fallback-c", "padded"),
            ("", "fallback-d", ""),
            ("   ", "fallback-e", ""),
            ("in side", "x", "in side"),
        ]
        for value, default, expected in cases:
            with self.subTest(value=value, default=default):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(name, None)
                    if value is not None:
                        os.environ[name] = value
                    self.assertEqual(self.lib.env_setting(name, default), expected)

    def test_applescript_string(self):
        cases = ["", "plain", 'a"b', "a\\b", '\\"', '"\\', "café ✓", "two\nlines", "'single'"]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.lib.applescript_string(text), quoted(text))
        self.assertEqual(self.lib.applescript_string('x"y'), '"x\\"y"')
        self.assertEqual(self.lib.applescript_string("x\\y"), '"x\\\\y"')

    def test_succeeded_by_exit_status(self):
        cases = [
            (["fake-ok"], {}, True),
            (["fake-ok", "-a", "b"], {}, True),
            (["fake-ok"], {"FAKE_FAIL": "fake-ok"}, False),
        ]
        for cmd, env, expected in cases:
            with self.subTest(cmd=cmd, env=env):
                result, _ = self.call("succeeded", cmd, bins="probe", env=env)
                self.assertIs(result, expected)

    def test_succeeded_closes_stdin_and_captures_output(self):
        """The command gets no stdin (the probe exits 3 if it reads any) and its output
        does not reach the caller's stdout or stderr."""
        result, r = self.call("succeeded", ["stdin-probe"], bins="probe", stdin="data on stdin\n")
        self.assertIs(result, True)
        self.assertNotIn("LEAK", r.stdout)
        self.assertNotIn("LEAK", r.stderr)


# ================================================================ one copy


def parse(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


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


# Names a copy of the notifier needs, matched inside any string that is not a docstring.
NAMES_IN_STRINGS = (
    "terminal-notifier",
    "osascript",
    "display notification",
    "HARNESS_NOTIFY_APP",
    "HARNESS_NOTIFY_SOUND",
    "com.microsoft.VSCode",
)
# The argv flags, matched as whole strings.
FLAGS = ("-title", "-subtitle", "-message", "-group", "-activate", "-sound")


UNKNOWN = "\0"  # a piece of a string the fold cannot know


class Folder:
    """Folds string expressions to their text, so a name split across pieces is still
    seen: "ter" + "minal-notifier", "-" + "title", "display" + f" notification {x}",
    "%s-notifier" % "terminal", "".join(["os", "ascript"]), and names bound once (anywhere
    in the file) to such an expression. A piece it cannot know becomes UNKNOWN."""

    def __init__(self, tree) -> None:
        self.docs = docstring_ids(tree)
        assigned: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned.setdefault(node.targets[0].id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                assigned.setdefault(node.target.id, []).append(node.value)
        self.assigned = {name: values[0] for name, values in assigned.items() if len(values) == 1}

    def fold(self, node, depth: int = 0):
        """The text of `node`, or None when it is not a string expression at all."""
        if depth > 20:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                text = self.fold(value, depth + 1) if isinstance(value, ast.Constant) else None
                parts.append(text if text is not None else UNKNOWN)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.fold(node.left, depth + 1), self.fold(node.right, depth + 1)
            if left is None and right is None:
                return None
            return (left if left is not None else UNKNOWN) + (right if right is not None else UNKNOWN)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = self.fold(node.left, depth + 1)
            if left is None:
                return None
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            texts = [self.fold(a, depth + 1) for a in args]
            out = left
            for text in texts:
                out = out.replace("%s", text if text is not None else UNKNOWN, 1)
            return out
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
            and isinstance(node.args[0], (ast.List, ast.Tuple))
        ):
            sep = self.fold(node.func.value, depth + 1)
            if sep is None:
                return None
            pieces = [self.fold(e, depth + 1) for e in node.args[0].elts]
            return sep.join(p if p is not None else UNKNOWN for p in pieces)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.assigned:
            return self.fold(self.assigned[node.id], depth + 1)
        return None


def notifier_strings(tree, allow=frozenset()) -> list[str]:
    """The notifier names, flags and settings spelled in `tree`'s strings (folded), each
    with its line; a folded string that is exactly one of `allow` is passed over."""
    return notifier_hits(ast.walk(tree), Folder(tree), allow)


def notifier_hits(nodes, folder: Folder, allow=frozenset()) -> list[str]:
    found = []
    for node in nodes:
        if not isinstance(node, ast.expr) or id(node) in folder.docs:
            continue
        text = folder.fold(node)
        if text is None or text in allow:
            continue
        line = getattr(node, "lineno", "?")
        for word in NAMES_IN_STRINGS:
            if word.lower() in text.lower():
                found.append(f"{word} in {text!r} (line {line})")
        if text in FLAGS:
            found.append(f"{text!r} (line {line})")
    return sorted(set(found))


def scanned_files(root: Path = PLUGIN) -> dict:
    """Every Python file under the plugin, by its path relative to the plugin: each .py
    file and each extensionless file whose shebang names python (the bin tools), under
    bin, hooks, lib, skills, templates and whatever else is there, except the tests, any
    __pycache__ and the lib's notify.py, the one place the notifier may live."""
    found = {}
    skip = root / "lib" / "harness_lib" / "notify.py"
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == skip:
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts:
            continue
        if path.suffix == ".py":
            found[rel.as_posix()] = path
        elif path.suffix == "":
            with open(path, "rb") as fh:
                first = fh.readline()
            if first.startswith(b"#!") and b"python" in first:
                found[rel.as_posix()] = path
    return found


PLACEHOLDER = re.compile(r"\{\{\s*\w+\s*\}\}")


def parsed(path: Path):
    """The file's AST: as it stands, or else with each {{placeholder}} (a template's)
    replaced by a name. None when it is still not Python: the checks then read its text."""
    text = path.read_text(encoding="utf-8")
    for source in (text, PLACEHOLDER.sub("harness_placeholder", text)):
        try:
            return ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
    return None


def runner_imports(tree):
    """(alias -> module) for `import subprocess [as sp]` and the like, and (bare name ->
    "module.attr") for `from subprocess import run [as r]` and the like."""
    modules, bare = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in RUNNER_MODULES:
                    modules[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module in RUNNER_MODULES:
            for a in node.names:
                bare[a.asname or a.name] = f"{node.module}.{a.name}"
    return modules, bare


def is_runner(qualified: str) -> bool:
    module, _, attr = qualified.partition(".")
    if module == "subprocess":
        return True
    if module == "shutil":
        return attr == "which"
    if module == "os":
        return attr.startswith(("exec", "spawn", "posix_spawn", "system", "popen"))
    return False


def builders_in(tree, allow=frozenset()) -> list:
    """The functions in `tree` that are a notifier copy: named as a helper or via_ builder,
    or running a process (directly or through a same-file function that does) while
    their body, or a module-level value it reads, spells a notifier name or flag."""
    folder = Folder(tree)
    modules, bare = runner_imports(tree)
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    runs, called, spells = {}, {}, {}
    for fn in fns:
        direct, names = False, set()
        nodes = list(ast.walk(fn))
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
                if isinstance(func.value, ast.Name) and func.value.id in modules:
                    direct = direct or is_runner(f"{modules[func.value.id]}.{func.attr}")
            elif isinstance(func, ast.Name):
                names.add(func.id)
                if func.id in bare:
                    direct = direct or is_runner(bare[func.id])
        reads = [n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]
        for name in reads:
            if name in folder.assigned:
                nodes.extend(ast.walk(folder.assigned[name]))
        runs[id(fn)], called[id(fn)] = direct, names
        spells[id(fn)] = bool(notifier_hits(nodes, folder, allow))
    changed = True
    while changed:  # a function that calls a same-file function that runs a process runs one
        running = {fn.name for fn in fns if runs[id(fn)]}
        changed = False
        for fn in fns:
            if not runs[id(fn)] and called[id(fn)] & running:
                runs[id(fn)] = changed = True
    return sorted({fn.name for fn in fns if fn.name in NAMED_BUILDERS or (runs[id(fn)] and spells[id(fn)])})


RUNNER_TEXT = re.compile(r"\b(?:subprocess\.|shutil\.which|os\.(?:exec|spawn|posix_spawn|system|popen))")


def defined_builders(label: str, path: Path) -> list:
    tree = parsed(path)
    allow = ALLOWED_SPELLINGS.get(label, set())
    if tree is not None:
        return builders_in(tree, allow)
    text = path.read_text(encoding="utf-8")
    names = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", text, re.M)
    found = {n for n in names if n in NAMED_BUILDERS}
    if RUNNER_TEXT.search(text) and spelled_names(label, path):
        found.add("(runs a process and spells a notifier, read as text)")
    return sorted(found)


def spelled_names(label: str, path: Path) -> list:
    tree = parsed(path)
    allow = ALLOWED_SPELLINGS.get(label, set())
    if tree is not None:
        return notifier_strings(tree, allow)
    text = path.read_text(encoding="utf-8")
    found = [f"{w} in the text" for w in NAMES_IN_STRINGS if w.lower() in text.lower() and w not in allow]
    found += [f"{f!r} in the text" for f in FLAGS if re.search(r"""["']""" + re.escape(f) + r"""["']""", text)]
    return sorted(found)


def assigned_defaults(path: Path) -> list:
    tree = parsed(path)
    if tree is None:
        names = re.findall(r"^\s*(DEFAULT_APP|DEFAULT_SOUND)\s*(?::[^=]*)?=", path.read_text(encoding="utf-8"), re.M)
    else:
        names = [
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        ]
    return sorted(set(names) & {"DEFAULT_APP", "DEFAULT_SOUND"})


def lib_bindings(tree) -> set:
    """The names a file binds from harness_lib.notify (or to it, or to harness_lib)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "harness_lib.notify":
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "harness_lib":
            names.update(a.asname or a.name for a in node.names if a.name == "notify")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "harness_lib.notify":
                    names.add(a.asname or "harness_lib")
    return names


def loaded_names(tree) -> set:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def load_with_loader(path: Path) -> types.ModuleType:
    modname = path.name.replace("-", "_").replace(".py", "") + "_notify_test"
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_with_exec(path: Path) -> types.ModuleType:
    module = types.ModuleType("harness_notify_exec_" + path.stem.replace("-", "_"))
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class OneCopyTests(unittest.TestCase):
    """One copy, in lib/harness_lib/notify.py: every other Python file under the plugin
    defines no helper or builder, spells no notifier name, flag or setting, and assigns
    neither default, so no second copy can sit in a sibling module (a `_legacy.py`) as the
    fallback a tool calls when the lib says False. The four former copies, besides, bind
    and use the lib's notify."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = scanned_files()

    def test_the_scan_covers_the_plugin(self):
        """The scan reaches the four former copies, every other bin tool and hook, the rest
        of the lib and the templates, and leaves out the tests and the lib's notify.py."""
        former = [path.relative_to(PLUGIN).as_posix() for path in COPIES.values()]
        for label in former + ["bin/harness-work", "hooks/tests_first.py", "lib/harness_lib/paths.py",
                               "lib/harness_lib/__init__.py", "templates/test_smoke.py"]:
            with self.subTest(file=label):
                self.assertIn(label, self.files)
        self.assertNotIn("lib/harness_lib/notify.py", self.files)
        self.assertFalse([f for f in self.files if f.startswith("tests/")])
        self.assertFalse([f for f in self.files if "__pycache__" in f])
        for label, path in self.files.items():
            self.assertTrue(path.is_file(), label)

    def test_no_file_defines_the_helpers_or_a_builder(self):
        for label, path in self.files.items():
            with self.subTest(file=label):
                self.assertEqual(defined_builders(label, path), [], f"{label} defines a notifier helper or builder")

    def test_no_file_spells_the_notifiers_flags_or_settings(self):
        """Outside docstrings, no file but the lib's notify.py names terminal-notifier,
        osascript, the AppleScript command, the two settings or the default app, or uses a
        notifier flag as a string: only the lib builds the argv. The one allowance is the
        tests-first hook's interpreter list (ALLOWED_SPELLINGS)."""
        for label, path in self.files.items():
            with self.subTest(file=label):
                self.assertEqual(spelled_names(label, path), [], f"{label} spells the notifier")

    def test_no_file_assigns_the_defaults(self):
        for label, path in self.files.items():
            with self.subTest(file=label):
                self.assertEqual(assigned_defaults(path), [], f"{label} assigns a notifier default")

    def test_the_allowance_is_exact_and_builders_are_found_by_what_they_do(self):
        """The tests-first allowance passes "osascript" as a whole string there and nothing
        more. A function is a builder by a helper's or via_ builder's name, or by running a
        process while spelling a notifier, whatever it is called; a function that has
        "notif" in its name but only calls the lib's notify (or runs gh) is not one."""
        allowed = ast.parse('INTERPRETERS = {"perl", "osascript"}\n')
        self.assertEqual(notifier_strings(allowed, {"osascript"}), [])
        self.assertEqual(len(notifier_strings(allowed)), 1)
        fallback = ast.parse('exe = shutil.which("osascript")\nscript = "display notification " + x\n')
        found = notifier_strings(fallback, {"osascript"})  # the AppleScript command still fires, osascript not
        self.assertTrue(found and all(f.startswith("display notification in") for f in found), found)

        for name in NAMED_BUILDERS:
            with self.subTest(named=name):
                self.assertEqual(builders_in(ast.parse(f"def {name}(x):\n    return x\n")), [name])

        legitimate = '''
import os
import subprocess
from harness_lib.notify import notify

def notify_on_merge(pr):
    subprocess.run(["gh", "pr", "merge", str(pr)], check=False)
    return notify(f"merged #{pr}", "harness-merge")

def cmd_notifications(args):
    return notify("proj needs input", "harness-proj-12", "Harness")

def notifications_enabled():
    return os.environ.get("HARNESS_QUIET") != "1"

def spell_only():
    return "terminal-notifier is optional"  # spells, runs nothing (the spelling check's job)

def main():
    return cmd_notifications([]) or notify_on_merge(3)
'''
        self.assertEqual(builders_in(ast.parse(legitimate)), [])

        # The test-breaker's round 1, (a): renamed, string-split copies in the bin tools,
        # in the shapes it can take.
        renamed = {
            "direct": '''
import shutil, subprocess
def ping(message, group, title="Harness"):
    exe = shutil.which("ter" + "minal-notifier")
    if exe:
        return subprocess.run([exe, "-" + "title", title, "-" "message", message, "-group", group]).returncode == 0
    return False
def cmd_needs_input(args):
    return ping("proj #1 needs input", "harness-proj-1")
''',
            "through a same-file runner": '''
import subprocess
from subprocess import run as _r
A = "os"
def _ok(cmd):
    return _r(cmd, stdin=subprocess.DEVNULL, capture_output=True).returncode == 0
def _alert(message, title):
    return _ok(["".join([A, "ascript"]), "-e", "display" + f" notification {message} with title {title}"])
''',
            "module-level argv and an aliased module": '''
import subprocess as sp
FLAGS = ["-ti" "tle", "-message"]
TN = "%s-notifier" % "terminal"
def send(message, title):
    return sp.call([TN] + [FLAGS[0], title, FLAGS[1], message]) == 0
''',
            "os.exec": '''
import os
def bell(message):
    os.execvp("osa" + "script", ["osa" + "script", "-e", "display notification " + message])
''',
        }
        expected = {"direct": ["ping"], "through a same-file runner": ["_alert"],
                    "module-level argv and an aliased module": ["send"], "os.exec": ["bell"]}
        for shape, source in renamed.items():
            with self.subTest(cheat="a", shape=shape):
                tree = ast.parse(source)
                self.assertEqual(builders_in(tree), expected[shape])
                self.assertTrue(notifier_strings(tree))

    def test_a_copy_parked_in_a_sibling_lib_module_is_caught(self):
        """The test-breaker's round 2, (b): old_notify moved to lib/harness_lib/_legacy.py
        and called as `notify(...) or _legacy.old_notify(...)`: the scan reaches the module,
        and the builder and spelling checks both name it there."""
        root = Path(tempfile.mkdtemp(prefix="harness-notify-scan-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "lib" / "harness_lib").mkdir(parents=True)
        (root / "bin").mkdir()
        (root / "tests").mkdir()
        (root / "lib" / "harness_lib" / "notify.py").write_text('X = "terminal-notifier"\n', encoding="utf-8")
        (root / "lib" / "harness_lib" / "_legacy.py").write_text('''
import shutil
import subprocess

def _run(cmd):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True).returncode == 0

def old_notify(message, group, title="Harness"):
    tn = shutil.which("terminal-" + "notifier")
    if tn and _run([tn, "-title", title, "-message", message, "-group", group]):
        return True
    osa = shutil.which("osa" "script")
    return bool(osa) and _run([osa, "-e", "display notification " + repr(message)])
''', encoding="utf-8")
        executable(root / "bin" / "harness-pr", '''#!/usr/bin/env python3
from harness_lib.notify import notify
from harness_lib import _legacy

def cmd_ready(n):
    return notify(f"PR #{n} ready", f"harness-pr-{n}") or _legacy.old_notify(f"PR #{n} ready", f"harness-pr-{n}")
''')
        (root / "tests" / "test_x.py").write_text('def old_notify():\n    return "osascript"\n', encoding="utf-8")
        files = scanned_files(root)
        self.assertEqual(sorted(files), ["bin/harness-pr", "lib/harness_lib/_legacy.py"])
        legacy = files["lib/harness_lib/_legacy.py"]
        self.assertEqual(defined_builders("lib/harness_lib/_legacy.py", legacy), ["old_notify"])
        self.assertTrue(spelled_names("lib/harness_lib/_legacy.py", legacy))
        self.assertEqual(defined_builders("bin/harness-pr", files["bin/harness-pr"]), [])

    def test_the_scan_reads_a_file_whether_or_not_it_parses(self):
        """A file is checked as Python as it stands, as a template with its {{placeholders}}
        filled, or, when neither parses, as text: a copy is found in each, and a clean file
        of each kind passes."""
        tmp = Path(tempfile.mkdtemp(prefix="harness-notify-parse-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = '''import subprocess
import {{pkg}}

def alert(message):
    return subprocess.run(["terminal-notifier", "-message", message]).returncode == 0
'''
        clean = '''"""The smoke test: the package imports."""

import {{pkg}}


def test_package_imports():
    assert {{pkg}}.__version__
'''
        cases = [
            # (name, text, parses after filling, builders, spells)
            ("plain-copy.py", copy.replace("{{pkg}}", "pkg"), True, ["alert"], True),
            ("template-copy.py", copy, True, ["alert"], True),
            ("broken-copy.py", copy + "def oops(:\n", False, None, True),
            ("plain-clean.py", clean.replace("{{pkg}}", "pkg"), True, [], False),
            ("template-clean.py", clean, True, [], False),
            ("broken-clean.py", clean + "def oops(:\n", False, [], False),
        ]
        for name, text, parses, builders, spells in cases:
            with self.subTest(file=name):
                path = tmp / name
                path.write_text(text, encoding="utf-8")
                self.assertEqual(parsed(path) is not None, parses)
                found = defined_builders(f"templates/{name}", path)
                if builders is None:
                    self.assertTrue(found, "a copy read as text is not found")
                else:
                    self.assertEqual(found, builders)
                self.assertEqual(bool(spelled_names(f"templates/{name}", path)), spells)

    def test_each_copy_imports_harness_lib_notify(self):
        for label, path in COPIES.items():
            imports = False
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.ImportFrom):
                    if node.module == "harness_lib.notify":
                        imports = True
                    if node.module == "harness_lib" and any(a.name == "notify" for a in node.names):
                        imports = True
                elif isinstance(node, ast.Import):
                    if any(a.name == "harness_lib.notify" for a in node.names):
                        imports = True
            with self.subTest(file=label):
                self.assertTrue(imports, f"{label} does not import harness_lib.notify")

    def test_the_folder_sees_split_names(self):
        """The spelling check folds the ways a name can be split (the test-breaker's
        round 1 hid the copies this way)."""
        source = '''
A = "ter"
B = A + "minal-notifier"
def f(x):
    one = "-" + "title"
    two = "display" + f" notification {x}"
    three = "".join(["os", "ascript"])
    four = "%s.microsoft.VSCode" % "com"
    five = "HARNESS_" "NOTIFY_APP"
    return [B, one, two, three, four, five, "-" "sound"]
'''
        found = " | ".join(notifier_strings(ast.parse(source)))
        for word in ("terminal-notifier", "'-title'", "display notification", "osascript",
                     "com.microsoft.VSCode", "HARNESS_NOTIFY_APP", "'-sound'"):
            with self.subTest(word=word):
                self.assertIn(word, found)
        self.assertEqual(notifier_strings(ast.parse('"""terminal-notifier in a docstring"""\nX = "--title"\n')), [])

    def test_the_lib_binding_is_used(self):
        """The names bound from harness_lib.notify are read somewhere, not only imported."""
        for label, path in COPIES.items():
            tree = parse(path)
            bound = lib_bindings(tree)
            with self.subTest(file=label):
                self.assertTrue(bound, f"{label} binds nothing from harness_lib.notify")
                self.assertTrue(bound & loaded_names(tree), f"{label} imports {sorted(bound)} but never uses them")

    def check_binds_the_lib(self, module: types.ModuleType, label: str) -> None:
        lib = sys.modules.get("harness_lib.notify")
        self.assertIsNotNone(lib, f"loading {label} did not import harness_lib.notify")
        self.assertEqual(os.path.realpath(lib.__file__), os.path.realpath(str(NOTIFY_PY)))
        bound = [
            name
            for name, value in module.__dict__.items()
            if value is lib.notify
            or value is lib
            or (isinstance(value, types.ModuleType) and getattr(value, "notify", None) is lib)
        ]  # `from harness_lib.notify import notify`, `from harness_lib import notify` or `import harness_lib.notify`
        self.assertTrue(bound, f"{label} binds neither harness_lib.notify nor its notify")
        local = module.__dict__.get("notify")
        if local is not None and not isinstance(local, types.ModuleType):
            self.assertIs(local, lib.notify, f"{label}'s notify is not the lib's")

    def test_tools_bind_the_libs_notify(self):
        for label, path in COPIES.items():
            with self.subTest(file=label):
                self.check_binds_the_lib(load_with_loader(path), label)

    def test_hook_binds_the_libs_notify_in_a_bare_module(self):
        self.check_binds_the_lib(load_with_exec(COPIES["hooks/notify.py"]), "hooks/notify.py")


# ================================================================ the hook runs the lib's notify


# A lib whose notify records its arguments and answers $SENTINEL_RESULT; the helpers and
# defaults are present so whatever the hook imports exists.
SENTINEL_NOTIFY = '''from __future__ import annotations
import json
import os
import subprocess

DEFAULT_APP = "sentinel.app"
DEFAULT_SOUND = "SentinelSound"


def env_setting(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip()


def succeeded(cmd):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=10).returncode == 0


def applescript_string(text):
    return '"' + text.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"') + '"'


def notify(message, group, title="Harness", subtitle=""):
    with open(os.environ["SENTINEL_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"message": message, "group": group, "title": title, "subtitle": subtitle}) + "\\n")
    return os.environ.get("SENTINEL_RESULT") == "true"
'''


class HookUsesTheLibTests(unittest.TestCase):
    PAYLOAD = {
        "session_id": "abc123",
        "transcript_path": "/dev/null",
        "cwd": "/Users/someone/projects/myrepo",
        "permission_mode": "default",
        "hook_event_name": "Notification",
        "message": "Claude needs your permission to use Bash",
        "title": "Permission needed",
        "notification_type": "permission_prompt",
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-notify-hook-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sentinel_log = self.tmp / "sentinel.log"
        self.notify_log = self.tmp / "notify.log"

    def plugin(self, lib: str) -> Path:
        """hooks/ copied from the checkout, with lib/ 'sentinel' (the checkout's lib, its
        notify.py replaced by SENTINEL_NOTIFY), 'broken' (its notify.py raising on import)
        or 'missing'."""
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        if lib in ("sentinel", "broken"):
            shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
            text = SENTINEL_NOTIFY if lib == "sentinel" else 'raise RuntimeError("broken lib")\n'
            (root / "lib" / "harness_lib" / "notify.py").write_text(text, encoding="utf-8")
        else:
            self.assertEqual(lib, "missing")
        return root

    def run_hook(self, root: Path, payload, result: str = "true"):
        env = {
            "PATH": str(_SHARED["base"] / "bin-both"),  # real-looking notifiers: the hook must not call them itself
            "HOME": str(self.tmp),
            "LANG": "en_US.UTF-8",
            "NOTIFY_LOG": str(self.notify_log),
            "SENTINEL_LOG": str(self.sentinel_log),
            "SENTINEL_RESULT": result,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": _SHARED["pycache"],
        }
        r = subprocess.run(
            [sys.executable, str(root / "hooks" / "notify.py")],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.tmp),
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return r

    def calls(self) -> list:
        if not self.sentinel_log.exists():
            return []
        return [json.loads(ln) for ln in self.sentinel_log.read_text(encoding="utf-8").splitlines()]

    def test_hook_hands_its_fields_to_the_libs_notify(self):
        root = self.plugin("sentinel")
        token = uuid.uuid4().hex[:8]
        cases = [
            (
                self.PAYLOAD,
                {
                    "message": "Claude needs your permission to use Bash",
                    "group": "claude-code-abc123",
                    "title": "Permission needed",
                    "subtitle": "myrepo",
                },
            ),
            (
                {"session_id": f"s-{token}", "cwd": f"/tmp/other-{token}", "message": f"  two\n  lines {token} "},
                {
                    "message": f"two lines {token}",
                    "group": f"claude-code-s-{token}",
                    "title": "Claude Code",
                    "subtitle": f"other-{token}",
                },
            ),
            (
                {},
                {
                    "message": "Claude Code needs your attention",
                    "group": "claude-code-session",
                    "title": "Claude Code",
                    "subtitle": "",
                },
            ),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                if self.sentinel_log.exists():
                    self.sentinel_log.unlink()
                r = self.run_hook(root, payload)
                self.assertEqual(self.calls(), [expected])
                self.assertEqual(r.stdout, "")  # notify said True: no bell
                self.assertEqual(r.stderr, "")
                self.assertFalse(self.notify_log.exists(), "the hook ran a notifier itself")

    def test_bell_only_when_the_libs_notify_says_false(self):
        root = self.plugin("sentinel")
        r = self.run_hook(root, self.PAYLOAD, result="false")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(json.loads(r.stdout), {"terminalSequence": BEL})
        self.assertFalse(self.notify_log.exists(), "the hook ran a notifier itself")

    def test_missing_lib_fails_open_with_one_line(self):
        """With harness_lib missing or failing to import: one stderr line, exit 0, and the
        bell on stdout. A Notification hook's stderr reaches no one, so without the bell a
        broken lib would make every prompt silent."""
        for lib in ("missing", "broken"):
            with self.subTest(lib=lib):
                root = self.plugin(lib)
                r = self.run_hook(root, self.PAYLOAD)
                lines = r.stderr.splitlines()
                self.assertEqual(len(lines), 1, r.stderr)
                self.assertTrue(lines[0].startswith("notify.py:"), lines[0])
                self.assertIn("harness_lib", lines[0])
                self.assertEqual(json.loads(r.stdout or "null"), {"terminalSequence": BEL}, r.stdout)
                self.assertFalse(self.notify_log.exists(), "the hook ran a notifier itself")


# ================================================================ the tools without the lib


class ToolsWithoutTheLibTests(unittest.TestCase):
    """harness-board, harness-pr and harness-digest run from a plugin copy whose lib/ is
    missing, or whose harness_lib/notify.py raises on import: one stderr line
    `<tool>: cannot load harness_lib from <lib>: <error>`, no traceback, nothing on stdout,
    exit 1, and no notifier run."""

    TOOLS = {"harness-board": ["board"], "harness-pr": ["ready", "34"], "harness-digest": ["--json"]}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-notify-nolib-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.notify_log = self.tmp / "notify.log"

    def plugin(self, lib: str, token: str = "") -> Path:
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(BIN, root / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "broken":
            shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "lib" / "harness_lib" / "notify.py").write_text(
                f'raise RuntimeError("broken lib {token}")\n', encoding="utf-8")
        else:
            self.assertEqual(lib, "missing")
        return root

    def run_tool(self, root: Path, tool: str):
        env = {
            "PATH": str(_SHARED["base"] / "bin-both"),
            "HOME": str(self.tmp),
            "LANG": "en_US.UTF-8",
            "NOTIFY_LOG": str(self.notify_log),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": _SHARED["pycache"],
        }
        return subprocess.run([sys.executable, str(root / "bin" / tool)] + self.TOOLS[tool], capture_output=True,
                              text=True, env=env, cwd=str(self.tmp), timeout=60)

    def check(self, root: Path, tool: str, error_has: str) -> None:
        r = self.run_tool(root, tool)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertNotIn("Traceback", r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        prefix = f"{tool}: cannot load harness_lib from {root / 'lib'}: "
        self.assertTrue(lines[0].startswith(prefix), f"{lines[0]!r} does not start with {prefix!r}")
        self.assertIn(error_has, lines[0][len(prefix):])
        self.assertFalse(self.notify_log.exists(), f"{tool} ran a notifier itself")

    def test_missing_lib_is_one_line_and_exit_1(self):
        root = self.plugin("missing")
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.check(root, tool, "harness_lib")

    def test_a_lib_that_fails_to_import_is_one_line_with_its_error(self):
        token = uuid.uuid4().hex[:10]
        root = self.plugin("broken", token)
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.check(root, tool, f"broken lib {token}")


# ================================================================ the tools run the lib's notify
#
# Each tool's notifying path (board needs-input, pr ready, the digest), driven with the
# fixtures of its own test module, but run from a copy of the plugin whose
# lib/harness_lib/notify.py is SENTINEL_NOTIFY: the sentinel must receive the tool's
# message, group and title, and the fake terminal-notifier and osascript on PATH (which
# log to $NOTIFY_LOG) must never run, so the tool has no notifier of its own.


def sentinel_tree() -> Path:
    tree = _SHARED.get("sentinel_tree")
    if tree is None:
        tree = _SHARED["base"] / "plugin-sentinel"
        def ignore(folder, names):
            skip = {"__pycache__"} | ({"tests"} if Path(folder).resolve() == PLUGIN else set())
            return [n for n in names if n in skip]

        shutil.copytree(PLUGIN, tree, ignore=ignore)
        (tree / "lib" / "harness_lib" / "notify.py").write_text(SENTINEL_NOTIFY, encoding="utf-8")
        _SHARED["sentinel_tree"] = tree
    return tree


def own_tests_only(cls):
    """Turn off the test methods a fixture class inherits from the guard modules, so only
    the ones defined here run (unittest skips non-callable test attributes)."""
    for name in dir(cls):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


class SentinelLog:
    def sentinel_env(self, result: str = "true") -> dict:
        self.sentinel_log = Path(self.tmp) / f"sentinel-{uuid.uuid4().hex[:6]}.log"
        return {"SENTINEL_LOG": str(self.sentinel_log), "SENTINEL_RESULT": result}

    def sentinel_calls(self) -> list:
        if not self.sentinel_log.exists():
            return []
        return [json.loads(ln) for ln in self.sentinel_log.read_text(encoding="utf-8").splitlines()]

    def assert_no_notifier_ran(self, log) -> None:
        self.assertFalse(os.path.exists(log) and Path(log).read_text().strip(), "the tool ran a notifier itself")


@own_tests_only
class BoardNotifiesThroughTheLib(SentinelLog, tb.Status):
    def board(self, *args, cwd=None, **env):
        tool = str(sentinel_tree() / "bin" / "harness-board")
        return subprocess.run([sys.executable, tool] + list(args), cwd=cwd or self.repo, env=self.env(**env),
                              capture_output=True, text=True, timeout=60)

    def test_needs_input_hands_its_message_and_group_to_the_lib(self):
        for number in ("12", "47"):
            for result in ("true", "false"):
                with self.subTest(number=number, result=result):
                    self.scenario([{"match": ["issue", "view", number], "stdout_json": {"labels": []}}],
                                  default={"stdout": ""})
                    if os.path.exists(self.notify_log):
                        os.unlink(self.notify_log)
                    env = self.sentinel_env(result)
                    p = self.ok("needs-input", number, "--question", "Ship it?", "--json", **env)
                    self.assertEqual(self.sentinel_calls(), [{
                        "message": f"proj #{number} needs input",
                        "group": f"harness-{self.key()}-{number}",
                        "title": "Harness",
                        "subtitle": "",
                    }])
                    self.assertIs(json.loads(p.stdout)["notified"], result == "true")  # the lib's answer
                    self.assert_no_notifier_ran(self.notify_log)


@own_tests_only
class PrNotifiesThroughTheLib(SentinelLog, tp.Ready):
    def pr(self, *args, cwd=None, stubs=True, **env):
        tool = str(sentinel_tree() / "bin" / "harness-pr")
        return subprocess.run([sys.executable, tool] + list(args), cwd=cwd or self.main,
                              env=self.env(stubs=stubs, **env), capture_output=True, text=True, timeout=120)

    def test_ready_hands_its_message_and_group_to_the_lib(self):
        """Whether the lib's notify answers True or False: the sentinel is called exactly
        once with ready's message and group, no notifier on PATH runs (a tool with a
        fallback of its own would run one when the lib says False), and ready's exit code
        and output, which carry no notified value, are the same either way."""
        for result in ("true", "false"):
            with self.subTest(result=result):
                self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
                if os.path.exists(self.notify_log):
                    os.unlink(self.notify_log)
                p = self.ok("ready", "34", **self.sentinel_env(result))
                self.assertEqual(p.stdout, f"PR #34 ready for review: {tp.PR_URL}\n")
                self.assertEqual(self.sentinel_calls(), [{
                    "message": "proj PR #34 ready for review: Add the widget (#12)",
                    "group": f"harness-{self.key()}-pr-34",
                    "title": "Harness",
                    "subtitle": "",
                }])
                self.assert_no_notifier_ran(self.notify_log)


@own_tests_only
class DigestNotifiesThroughTheLib(SentinelLog, td.Digest):
    def run_tool(self, *args, **extra):
        tool = str(sentinel_tree() / "bin" / "harness-digest")
        return subprocess.run([sys.executable, tool] + list(args), cwd=self.tmp, env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def test_digest_hands_its_message_and_group_to_the_lib(self):
        """Whether the lib's notify answers True or False, printing Markdown or --json: the
        sentinel is called exactly once with the digest's message and group, no notifier
        on PATH runs, the digest exits 0, and --json's "notified" is the lib's answer, not
        that of a fallback of the digest's own."""
        self.world()
        expected = {
            "message": "Harness digest: 1 PRs to review, 2 need input, 2 stale",
            "group": "harness-digest",
            "title": "Harness",
            "subtitle": "",
        }
        for result in ("true", "false"):
            for args in ((), ("--json",)):
                with self.subTest(result=result, args=args):
                    if os.path.exists(self.notifylog):
                        os.unlink(self.notifylog)
                    p = self.run_tool(*args, **self.sentinel_env(result))
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertNotIn("Traceback", p.stderr)
                    self.assertEqual(self.sentinel_calls(), [expected])
                    self.assert_no_notifier_ran(self.notifylog)
                    if args:
                        data = json.loads(p.stdout)
                        self.assertEqual(data["counts"], {"prs": 1, "needs_input": 2, "stale": 2})
                        self.assertIs(data["notified"], result == "true")  # the lib's answer
                    else:
                        self.assertTrue(p.stdout.startswith("# Harness digest, "), p.stdout[:80])


if __name__ == "__main__":
    unittest.main()
