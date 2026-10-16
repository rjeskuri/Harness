"""Tests for plugins/harness/lib/harness_lib/notify.py (ticket #86): one notification
function for harness-board, harness-pr, harness-digest and hooks/notify.py.

- The lib's notify(message, group, title="Harness", subtitle="") -> bool, run in a
  subprocess with only fake notifiers on PATH: the terminal-notifier argv in the order
  board uses (-title, -subtitle only when non-empty, -message, -group, -activate, -sound),
  HARNESS_NOTIFY_APP / HARNESS_NOTIFY_SOUND (unset: com.microsoft.VSCode / Glass; empty
  or blank: that part off), and the osascript fallback as hooks/notify.py does it (when
  terminal-notifier is missing, fails, or cannot be run), never raising.
- env_setting, succeeded and applescript_string, DEFAULT_APP and DEFAULT_SOUND.
- One copy: the three bin tools and hooks/notify.py define none of the helpers or an
  argv builder of their own, spell none of the notifier names, flags or settings, and
  bind the lib's notify from harness_lib.notify.
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

# The four places that carried a copy, per the ticket.
COPIES = {
    "harness-board": BIN / "harness-board",
    "harness-pr": BIN / "harness-pr",
    "harness-digest": BIN / "harness-digest",
    "hooks/notify.py": HOOKS / "notify.py",
}
HELPERS = ("env_setting", "succeeded", "applescript_string")
# The argv builders: the tools' notify, the hook's two via_ functions.
BUILDERS = ("notify", "via_terminal_notifier", "via_osascript")

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


def tearDownModule() -> None:
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


def notifier_strings(tree) -> list[str]:
    docs = docstring_ids(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            text = node.value
            for word in NAMES_IN_STRINGS:
                if word.lower() in text.lower():
                    found.append(f"{word} in {text!r} (line {node.lineno})")
            if text in FLAGS:
                found.append(f"{text!r} (line {node.lineno})")
    return found


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
    def test_no_copy_defines_the_helpers_or_a_builder(self):
        for label, path in COPIES.items():
            tree = parse(path)
            defined = sorted(
                {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                & set(HELPERS + BUILDERS)
            )
            with self.subTest(file=label):
                self.assertEqual(defined, [])

    def test_no_copy_spells_the_notifiers_flags_or_settings(self):
        """Outside docstrings, no tool or the hook names terminal-notifier, osascript, the
        AppleScript command, the two settings or the default app, or uses a notifier flag
        as a string: only the lib builds the argv."""
        for label, path in COPIES.items():
            with self.subTest(file=label):
                self.assertEqual(notifier_strings(parse(path)), [])

    def test_no_copy_assigns_the_defaults(self):
        for label, path in COPIES.items():
            tree = parse(path)
            assigned = sorted(
                {
                    target.id
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                    if isinstance(target, ast.Name)
                }
                & {"DEFAULT_APP", "DEFAULT_SOUND"}
            )
            with self.subTest(file=label):
                self.assertEqual(assigned, [])

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
        notify.py replaced by SENTINEL_NOTIFY) or 'missing'."""
        root = self.tmp / f"plugin-{lib}"
        shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "sentinel":
            shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "lib" / "harness_lib" / "notify.py").write_text(SENTINEL_NOTIFY, encoding="utf-8")
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
        root = self.plugin("missing")
        r = self.run_hook(root, self.PAYLOAD)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("notify.py:"), lines[0])
        self.assertIn("harness_lib", lines[0])


if __name__ == "__main__":
    unittest.main()
