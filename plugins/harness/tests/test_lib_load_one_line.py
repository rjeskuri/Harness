"""Tests for ticket #117: a hook or bin tool that cannot load harness_lib reports it on
exactly one line, whatever the exception's text (docs/design.md, "Fail open").

The plugin (bin/, hooks/, lib/) is copied into a temporary tree whose
lib/harness_lib/__init__.py raises a RuntimeError with a message full of line breaks and
other whitespace (\\n, \\r\\n, \\r, \\t, \\v, \\f, U+2028, and, per run, every character for
which str.isspace() is true, shuffled into random runs, so that no fixed table of
characters can pass). Each script is then run as a subprocess, the way Claude Code or a
shell runs it:

- harness-standup (and, as a guard, harness-board, harness-pr, harness-work, harness-red
  and harness-digest, which already collapse it): stderr is exactly
  `<tool>: cannot load harness_lib from <lib>: <error>` plus one newline, the error's
  whitespace runs collapsed to single spaces (`' '.join(str(exc).split())`), nothing on
  stdout, exit 1. An all-whitespace error collapses to the empty string.
- The seven hooks: stderr is exactly `<hook>.py: cannot load harness_lib from <lib>:
  <error>` on one line, exit 0. notify.py also prints the bell on stdout; session_brief
  puts the error into one `Warning:` line of its brief (and, in a project with its own
  SessionStart hook, that one line is the whole additionalContext).

The message carries a fresh token per run and the expected line is built here from the
message, so the error words must survive (collapsed, not dropped or replaced). The random
seed is printed in failure messages.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_lib_load_one_line.py" -v
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
BEL = "\u0007"

STANDUP = "harness-standup"
# Already collapse today: a guard that the shared contract holds for every bin tool.
COLLAPSING_TOOLS = ("harness-board", "harness-pr", "harness-work", "harness-red", "harness-digest")
HOOKS = (
    "branch_guard.py",
    "tests_first.py",
    "readonly_bash.py",
    "verify_on_stop.py",
    "after_edit.py",
    "notify.py",
    "session_brief.py",
)
# Hooks whose stdout is empty on a lib-load failure (notify and session_brief have their own).
QUIET_HOOKS = tuple(h for h in HOOKS if h not in ("notify.py", "session_brief.py"))
# Every whitespace character Python knows (\x1c-\x1f, U+0085, U+00A0, U+2029, U+3000, ...).
ALL_WHITESPACE = tuple(c for c in map(chr, range(0x110000)) if c.isspace())
# The ones a fixed table is most likely to miss; each must be among ALL_WHITESPACE.
MUST_COVER = ("\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0", " ", " ", "　")
# How many messages the shuffled whitespace is spread over: each stays short enough that
# session_brief's warning line (clipped at 300 characters) still holds it whole.
WHITESPACE_CHUNKS = 3


def collapse(text: str) -> str:
    """The expected error text, derived independently: every whitespace run one space."""
    out, word = [], []
    for ch in text:
        if ch.isspace():
            if word:
                out.append("".join(word))
                word = []
        else:
            word.append(ch)
    if word:
        out.append("".join(word))
    return " ".join(out)


def messages(token: str) -> dict:
    """Named exception messages, each with line breaks; the token, on more than one of each
    message's lines, makes each run's differ and shows any piece that spills onto a line."""
    return {
        "newlines": f"first-{token} line\nsecond line\nthird-{token} line\n",
        "mixed": f"\n  alpha-{token}\r\nbravo-{token}\rcharlie\t\tdelta\x0becho\x0cfoxtrot golf-{token}  hotel\n\n",
        "traceback-like": (
            f"Traceback (most recent call last):\n  File \"x.py\", line 3\n"
            f"    boom({token})\nValueError: kaput {token}"
        ),
    }


def every_whitespace_messages(token: str, rng: random.Random) -> dict:
    """Messages whose words are separated by random runs of whitespace; across them every
    character of ALL_WHITESPACE leads at least one run, in a fresh order per run, and the
    messages start and end with a run (which must vanish, not become a space)."""
    chars = list(ALL_WHITESPACE)
    rng.shuffle(chars)
    out = {}
    for n in range(WHITESPACE_CHUNKS):
        leads = chars[n::WHITESPACE_CHUNKS]
        runs = ["".join([lead] + rng.choices(ALL_WHITESPACE, k=rng.randint(0, 2))) for lead in leads]
        words = [f"w{n}{i}" for i in range(len(runs) - 1)]
        words[0] = f"{words[0]}-{token}"
        words[-1] = f"{words[-1]}-{token}"
        out[f"every-whitespace-{n}"] = runs[0] + "".join(w + r for w, r in zip(words, runs[1:]))
    return out


def plant(root: Path, message: str) -> None:
    """A copy of the plugin whose harness_lib package raises `message` on import."""
    for part in ("bin", "hooks", "lib"):
        shutil.copytree(PLUGIN / part, root / part, ignore=shutil.ignore_patterns("__pycache__"))
    # repr escapes every non-printable character, so the source line stays one ASCII line.
    (root / "lib" / "harness_lib" / "__init__.py").write_text(
        f"raise RuntimeError({message!r})\n", encoding="utf-8"
    )


class LibLoadOneLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="harness-libline-")).resolve()
        # Short, so that session_brief's warning, clipped at 300 characters, still holds it all.
        cls.token = uuid.uuid4().hex[:10]
        cls.seed = random.randrange(2**32)
        rng = random.Random(cls.seed)
        all_messages = dict(messages(cls.token))
        all_messages.update(every_whitespace_messages(cls.token, rng))
        cls.cases = {}
        for name, message in all_messages.items():
            root = cls.tmp / f"plugin-{name}"
            plant(root, message)
            cls.cases[name] = (root, message)
        # Only whitespace: every whitespace character, shuffled, some repeated.
        only = list(ALL_WHITESPACE) + rng.choices(ALL_WHITESPACE, k=8)
        rng.shuffle(only)
        cls.whitespace_message = "".join(only)
        whitespace = cls.tmp / "plugin-whitespace-only"
        plant(whitespace, cls.whitespace_message)
        cls.whitespace_root = whitespace
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        (cls.tmp / "tmpdir").mkdir()
        cls.project = cls.tmp / "project"  # not a git repo
        cls.project.mkdir()
        cls.own_start = cls.tmp / "own-start"  # declares its own SessionStart hook
        (cls.own_start / ".claude").mkdir(parents=True)
        (cls.own_start / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self) -> dict:
        return {
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp / "tmpdir"),
            "PATH": SYSTEM_PATH,
            "LANG": "en_US.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(self.tmp / "pycache"),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run_script(self, path: Path, args=(), stdin: str = "", cwd: Path | None = None):
        r = subprocess.run(
            [sys.executable, str(path), *args],
            input=stdin.encode("utf-8"),
            capture_output=True,
            env=self.env(),
            cwd=str(cwd or self.project),
            timeout=60,
        )
        return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")

    def hook_payload(self, hook: str, cwd: Path) -> str:
        data = {"session_id": "sess-117", "cwd": str(cwd), "permission_mode": "default"}
        if hook == "session_brief.py":
            data.update(hook_event_name="SessionStart", source="startup")
        elif hook == "notify.py":
            data.update(hook_event_name="Notification", message="needs you")
        elif hook == "verify_on_stop.py":
            data.update(hook_event_name="Stop", stop_hook_active=False)
        elif hook == "readonly_bash.py":
            data.update(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "rm -rf x"},
                        agent_type="harness:reviewer")
        else:
            data.update(hook_event_name="PreToolUse" if hook != "after_edit.py" else "PostToolUse",
                        tool_name="Edit", tool_input={"file_path": str(cwd / "src" / "app.py")})
        return json.dumps(data)

    def expected_line(self, name: str, root: Path, message: str) -> str:
        return f"{name}: cannot load harness_lib from {root / 'lib'}: {collapse(message)}\n"

    def assert_one_line(self, err: str) -> None:
        self.assertEqual(err.count("\n"), 1, f"seed {self.seed}: {err!r}")
        # str.splitlines also breaks on \v, \f, \x1c-\x1e, U+0085, U+2028 and U+2029.
        self.assertEqual(len(err.splitlines()), 1, f"seed {self.seed}: {err!r}")

    def assert_no_odd_whitespace(self, text: str) -> None:
        odd = sorted({hex(ord(c)) for c in text if c.isspace() and c != " "})
        self.assertEqual(odd, [], f"seed {self.seed}: {text!r}")
        self.assertNotIn("  ", text, f"seed {self.seed}: {text!r}")

    def test_the_fixtures_use_every_whitespace_character(self):
        # Guards the fixtures, not the code: every whitespace character is in some message.
        for ch in MUST_COVER:
            self.assertIn(ch, ALL_WHITESPACE)
        for name in ("every-whitespace-0", "every-whitespace-1", "every-whitespace-2"):
            used = set(self.cases[name][1])
            self.assertTrue(used & set(ALL_WHITESPACE), name)
        used = set("".join(m for name, (_, m) in self.cases.items() if name.startswith("every-")))
        self.assertEqual(sorted(hex(ord(c)) for c in set(ALL_WHITESPACE) - used), [])
        self.assertEqual(set(self.whitespace_message), set(ALL_WHITESPACE))

    # ------------------------------------------------------------ the bin tools

    def check_tool(self, tool: str) -> None:
        for case, (root, message) in self.cases.items():
            with self.subTest(tool=tool, message=case):
                code, out, err = self.run_script(root / "bin" / tool, ["--help"])
                self.assertEqual(code, 1, err)
                self.assertEqual(out, "")
                self.assert_one_line(err)
                self.assertEqual(err, self.expected_line(tool, root, message), f"seed {self.seed}")

    def test_standup_prints_a_multiline_lib_error_on_one_line_and_exits_1(self):
        self.check_tool(STANDUP)

    def test_the_other_bin_tools_print_a_multiline_lib_error_on_one_line_and_exit_1(self):
        for tool in COLLAPSING_TOOLS:
            self.check_tool(tool)

    def test_a_whitespace_only_lib_error_is_still_one_line_for_every_bin_tool(self):
        root = self.whitespace_root
        for tool in (STANDUP,) + COLLAPSING_TOOLS:
            with self.subTest(tool=tool):
                code, out, err = self.run_script(root / "bin" / tool, ["--help"])
                self.assertEqual((code, out), (1, ""), err)
                self.assert_one_line(err)
                self.assertTrue(err.endswith("\n"), repr(err))
                self.assertTrue(err.startswith(f"{tool}: cannot load harness_lib from {root / 'lib'}:"), repr(err))
                self.assertNotIn("\r", err)
                self.assertNotIn("\t", err)
                # An all-whitespace message collapses to the empty string: nothing after ": ".
                self.assertEqual(err, f"{tool}: cannot load harness_lib from {root / 'lib'}: \n",
                                 f"seed {self.seed}")

    def test_a_whitespace_only_lib_error_is_still_one_line_for_every_stderr_hook(self):
        root = self.whitespace_root
        for hook in QUIET_HOOKS + ("notify.py",):
            with self.subTest(hook=hook):
                code, out, err = self.run_script(root / "hooks" / hook, stdin=self.hook_payload(hook, self.project))
                self.assertEqual(code, 0, err)
                if hook == "notify.py":
                    self.assertEqual(json.loads(out or "null"), {"terminalSequence": BEL}, out)
                else:
                    self.assertEqual(out, "")
                self.assert_one_line(err)
                self.assertTrue(err.endswith("\n"), repr(err))
                self.assertTrue(err.startswith(f"{hook}: cannot load harness_lib from {root / 'lib'}:"), repr(err))
                self.assertNotIn("\r", err)
                self.assertNotIn("\t", err)
                self.assertEqual(err, f"{hook}: cannot load harness_lib from {root / 'lib'}: \n",
                                 f"seed {self.seed}")

    # ------------------------------------------------------------ the hooks

    def test_each_quiet_hook_prints_a_multiline_lib_error_on_one_line_and_exits_0(self):
        for hook in QUIET_HOOKS:
            for case, (root, message) in self.cases.items():
                with self.subTest(hook=hook, message=case):
                    code, out, err = self.run_script(
                        root / "hooks" / hook, stdin=self.hook_payload(hook, self.project))
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out, "")
                    self.assert_one_line(err)
                    self.assertEqual(err, self.expected_line(hook, root, message), f"seed {self.seed}")

    def test_notify_prints_a_multiline_lib_error_on_one_line_and_still_rings_the_bell(self):
        for case, (root, message) in self.cases.items():
            with self.subTest(message=case):
                code, out, err = self.run_script(
                    root / "hooks" / "notify.py", stdin=self.hook_payload("notify.py", self.project))
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out or "null"), {"terminalSequence": BEL}, out)
                self.assert_one_line(err)
                self.assertEqual(err, self.expected_line("notify.py", root, message), f"seed {self.seed}")

    def brief(self, root: Path, cwd: Path):
        code, out, err = self.run_script(
            root / "hooks" / "session_brief.py", stdin=self.hook_payload("session_brief.py", cwd), cwd=cwd)
        self.assertEqual(code, 0, err)
        parsed = json.loads(out)  # a crash instead of the fail-open brief leaves no JSON
        return parsed["hookSpecificOutput"]["additionalContext"], err

    def check_brief_stderr(self, err: str, root: Path, message: str) -> None:
        # The brief is where the error must reach; stderr, when written at all, is one line too.
        if err:
            self.assert_one_line(err)
            self.assertEqual(err, self.expected_line("session_brief.py", root, message), f"seed {self.seed}")

    def test_session_brief_puts_a_multiline_lib_error_into_one_warning_line(self):
        for case, (root, message) in self.cases.items():
            with self.subTest(message=case):
                context, err = self.brief(root, self.project)
                lines = context.split("\n")
                error = f"cannot load harness_lib from {root / 'lib'}: {collapse(message)}"
                warnings = [ln for ln in lines if ln.startswith("Warning:")]
                self.assertEqual(len(warnings), 1, f"seed {self.seed}: {context!r}")
                self.assertIn(error, warnings[0], f"seed {self.seed}")
                # No fragment of the error spills onto another line of the brief.
                spill = [ln for ln in lines if not ln.startswith("Warning:") and self.token in ln]
                self.assertEqual(spill, [], context)
                for ch in ("\r", "\t", "\x0b", "\x0c", "  "):
                    self.assertNotIn(ch, warnings[0])
                self.assert_no_odd_whitespace(warnings[0])
                self.check_brief_stderr(err, root, message)

    def test_session_brief_warning_alone_is_one_line_in_a_project_with_its_own_session_start(self):
        for case, (root, message) in self.cases.items():
            with self.subTest(message=case):
                context, err = self.brief(root, self.own_start)
                error = f"cannot load harness_lib from {root / 'lib'}: {collapse(message)}"
                self.assertTrue(context.startswith("Warning: "), context)
                self.assertIn(error, context, f"seed {self.seed}")
                self.assertNotIn("\n", context)
                self.assertEqual(len(context.splitlines()), 1, f"seed {self.seed}: {context!r}")
                for ch in ("\r", "\t", "\x0b", "\x0c", "  "):
                    self.assertNotIn(ch, context)
                self.assert_no_odd_whitespace(context)
                self.check_brief_stderr(err, root, message)

    def test_a_whitespace_only_lib_error_is_one_warning_line_in_session_brief(self):
        root = self.whitespace_root
        lib = root / "lib"
        context, err = self.brief(root, self.project)
        warnings = [ln for ln in context.split("\n") if ln.startswith("Warning:")]
        self.assertEqual(len(warnings), 1, f"seed {self.seed}: {context!r}")
        self.assertIn(f"cannot load harness_lib from {lib}:", warnings[0])
        self.assert_no_odd_whitespace(warnings[0])
        if err:
            self.assertEqual(err, f"session_brief.py: cannot load harness_lib from {lib}: \n", f"seed {self.seed}")
        context, _ = self.brief(root, self.own_start)
        self.assertTrue(context.startswith(f"Warning: cannot load harness_lib from {lib}:"), repr(context))
        self.assertEqual(len(context.splitlines()), 1, f"seed {self.seed}: {context!r}")
        self.assert_no_odd_whitespace(context)


if __name__ == "__main__":
    unittest.main()
