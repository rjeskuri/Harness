"""Tests for the harness hooks: notify, session_brief, after_edit, verify_on_stop,
readonly_bash.

Every hook runs end to end as a subprocess of the interpreter running the tests, fed sample
stdin JSON, under a temporary HOME and TMPDIR and in temporary git repositories. Nothing
here touches ~/.claude, the real hook state directory or the notification centre: the
notifiers are fake scripts on a private PATH. Finer checks (each secret pattern, each
handoff format) load the hook's source by path and call its functions directly.

Secret-shaped samples, and the allow marker, are assembled at run time, so this file never
trips the secret scan itself.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_hooks*.py" -v
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Secret-shaped samples, split so that no line of this file matches a pattern.
AWS_KEY = "AKIA" + "QWERTYUIOP234567"
SECRET_SAMPLES = [
    ("AWS access key id", 'aws_key_id = "' + AWS_KEY + '"'),
    ("GitHub token", "GH=" + "ghp" + "_" + "a1B2c3D4" * 4 + "e5F6"),
    (
        "GitHub fine-grained token",
        "pat: " + "github" + "_pat_" + "11AAQQ7Y0" + "Zq8Lm4Np2Rs6Tv",
    ),
    ("Slack token", "SLACK=" + "xo" + "xb-" + "2401375-8812-QwErTy"),
    ("Google API key", "maps: " + "AI" + "za" + "SyD4" + "k9" * 15 + "Q"),
    ("Anthropic API key", "ANTHROPIC=" + "sk" + "-ant-" + "api03-" + "Zz9Yy8" * 5),
    ("OpenAI API key", "OPENAI=" + "sk" + "-proj-" + "Ab1Cd2Ef3" * 3),
    ("OpenAI API key", "key: " + "sk" + "-" + "T3BlbkFJ" * 6),
    (
        "Stripe live secret key",
        "stripe = " + "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    ),
    ("private key block", "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"),
    (
        "Jotform-style apiKey",
        "url = https://api.jotform.com/user?api" + "Key=" + "0123abcd" * 4,
    ),
    ("generic secret assignment", "password" + ' = "' + "Pa55w0rdQ" * 3 + '"'),
    ("generic secret assignment", "client_secret" + ": '" + "Zq8Lm4Np2Rs6Tv" * 2 + "'"),
]
PLACEHOLDER_LINES = [
    'aws = "' + AWS_KEY + '"  # example',
    "AWS_KEY=<" + AWS_KEY + ">",
    "key: ${" + AWS_KEY + "}",
    "your_key = " + AWS_KEY,
    "KEY = " + AWS_KEY + "  # Placeholder",
    "KEY = " + AWS_KEY + "  # dummy",
    "KEY = " + AWS_KEY + "  # REDACTED",
    "KEY = " + AWS_KEY + "  # XXXX",
    "AKIA" + "IOSFODNN7EXAMPLE",
]
ALLOW_MARKER = "harness: " + "allow-secrets"
SECRET_ADVICE = (
    " - remove it before continuing; read secrets from the environment or a file that is "
    "gitignored."
)
GUIDANCE = (
    "Fix the root cause and re-run. Don't delete, skip or loosen tests. If a failure "
    "predates your change or is out of scope, say so explicitly."
)

ELLIPSIS = chr(0x2026)
BEL = chr(7)
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
FAKE_NOTIFIER = """#!/bin/sh
name=${0##*/}
for a in "$@"; do printf '%s\\n' "$a"; done > "$FAKE_LOG_DIR/$name.args"
case " $FAKE_FAIL " in *" $name "*) exit 1 ;; esac
exit 0
"""

_SHARED: dict = {}


def setUpModule() -> None:
    """Fixtures built once, because each costs real time on macOS.

    - One bytecode cache for every hook subprocess: the temporary HOME hides python3's
      usual cache, and recompiling the standard library on each run is slow.
    - One fake notifier script, linked under each name: the first exec of a new
      executable costs about 150 ms while macOS assesses it.
    - One committed template repository, copied for each test instead of re-created.
    """
    base = Path(tempfile.mkdtemp(prefix="harness-hooks-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")
    fake = base / "fake-notifier"
    fake.write_text(FAKE_NOTIFIER)
    fake.chmod(0o755)
    for folder, names in (
        ("bin-both", ("terminal-notifier", "osascript")),
        ("bin-osascript", ("osascript",)),
        ("bin-none", ()),
    ):
        (base / folder).mkdir()
        for name in names:
            os.symlink(fake, base / folder / name)
    # A git that works except for `rev-parse --git-common-dir`, so the main checkout
    # cannot be found while the repository itself still answers.
    real_git = shutil.which("git", path=SYSTEM_PATH) or "/usr/bin/git"
    (base / "bin-git-no-common").mkdir()
    no_common = base / "bin-git-no-common" / "git"
    no_common.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--git-common-dir" ]; then echo "fatal: simulated" >&2; exit 128; fi\n'
        "done\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    no_common.chmod(0o755)
    template = base / "template"
    template.mkdir()
    env = dict(GIT_IDENTITY, HOME=str(base), PATH=SYSTEM_PATH)
    for args in (
        ("init", "-q"),
        ("symbolic-ref", "HEAD", "refs/heads/main"),
        ("add", "README.md"),
        ("commit", "-q", "-m", "Initial commit"),
    ):
        if args[0] == "add":
            (template / "README.md").write_text("hello\n")
        subprocess.run(
            ["git", "-C", str(template), *args],
            check=True,
            capture_output=True,
            env=env,
        )
    _SHARED["template"] = template


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def load_hook(name: str) -> types.ModuleType:
    """Import a hook script by path without running its main() or writing bytecode."""
    path = HOOKS / f"{name}.py"
    module = types.ModuleType(f"harness_hook_{name}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-hooks-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.tmpdir = self.tmp / "tmp"
        for d in (self.home, self.tmpdir):
            d.mkdir()
        # Built from scratch, so nothing leaks in from a Claude Code session running the
        # tests (CLAUDE_PROJECT_DIR, CLAUDE_VERIFY_CMD, HARNESS_*).
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmpdir),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )

    def run_hook(
        self, name: str, payload, env: dict | None = None, timeout: float = 60
    ):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(HOOKS / name)],
            input=stdin,
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            cwd=str(self.tmp),
            timeout=timeout,
        )

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=self.env,
        )

    def make_repo(self, name: str = "proj", commit: bool = True) -> Path:
        """A git repo on branch main: README.md in one commit "Initial commit", or empty."""
        repo = self.tmp / name
        if commit:
            shutil.copytree(_SHARED["template"], repo, symlinks=True)
        else:
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q")
            self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        return repo

    def state_dir(self, root: Path) -> Path:
        key = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
        return self.tmpdir / "claude-harness" / hashlib.sha1(key).hexdigest()[:12]

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


# ---------------------------------------------------------------- notify.py


class NotifyTests(HookTestCase):
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

    def notify(self, payload=None, notifiers: str = "none", **env: str):
        """Run the hook with only the fake notifiers in bin-<notifiers> on PATH."""
        env.setdefault("PATH", str(_SHARED["base"] / f"bin-{notifiers}"))
        env.setdefault("FAKE_LOG_DIR", str(self.tmp))
        r = self.run_hook(
            "notify.py", self.PAYLOAD if payload is None else payload, env
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def args_of(self, name: str) -> list[str]:
        return (self.tmp / f"{name}.args").read_text().splitlines()

    def test_terminal_notifier_names_repo_and_uses_defaults(self):
        r = self.notify(notifiers="both")
        self.assertEqual(r.stdout, "")
        self.assertFalse((self.tmp / "osascript.args").exists())
        args = self.args_of("terminal-notifier")
        self.assertEqual(
            dict(zip(args[::2], args[1::2])),
            {
                "-title": "Permission needed",
                "-subtitle": "myrepo",
                "-message": "Claude needs your permission to use Bash",
                "-group": "claude-code-abc123",
                "-activate": "com.microsoft.VSCode",
                "-sound": "Glass",
            },
        )

    def test_app_and_sound_come_from_environment_and_empty_disables(self):
        self.notify(
            notifiers="both",
            HARNESS_NOTIFY_APP="com.googlecode.iterm2",
            HARNESS_NOTIFY_SOUND="Ping",
        )
        args = self.args_of("terminal-notifier")
        self.assertEqual(args[args.index("-activate") + 1], "com.googlecode.iterm2")
        self.assertEqual(args[args.index("-sound") + 1], "Ping")

        self.notify(notifiers="both", HARNESS_NOTIFY_APP="", HARNESS_NOTIFY_SOUND="")
        args = self.args_of("terminal-notifier")
        self.assertNotIn("-activate", args)
        self.assertNotIn("-sound", args)

    def test_osascript_when_terminal_notifier_is_missing(self):
        payload = dict(self.PAYLOAD, message='Say "hi"\nnow')
        del payload["title"]
        r = self.notify(payload, notifiers="osascript")
        self.assertEqual(r.stdout, "")
        self.assertEqual(
            self.args_of("osascript"),
            [
                "-e",
                'display notification "Say \\"hi\\" now" with title "Claude Code" '
                'subtitle "myrepo" sound name "Glass"',
            ],
        )

    def test_failed_terminal_notifier_falls_back_to_osascript(self):
        r = self.notify(notifiers="both", FAKE_FAIL="terminal-notifier")
        self.assertEqual(r.stdout, "")
        self.assertTrue((self.tmp / "terminal-notifier.args").exists())
        self.assertEqual(self.args_of("osascript")[0], "-e")

    def test_terminal_bell_when_nothing_can_notify(self):
        r = self.notify()
        self.assertEqual(json.loads(r.stdout), {"terminalSequence": BEL})

    def test_bad_stdin_still_exits_zero(self):
        r = self.notify("this is not json")
        self.assertEqual(json.loads(r.stdout), {"terminalSequence": BEL})


# ---------------------------------------------------------------- session_brief.py


class SessionBriefTests(HookTestCase):
    def brief(self, cwd: Path, source: str = "startup"):
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": source,
        }
        r = self.run_hook("session_brief.py", payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def output(self, cwd: Path, source: str = "startup") -> dict:
        data = json.loads(self.brief(cwd, source).stdout)
        self.assertEqual(list(data), ["hookSpecificOutput"])
        out = data["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIsInstance(out["additionalContext"], str)
        return out

    def test_full_brief_shape_and_sections(self):
        repo = self.make_repo()
        (repo / "README.md").write_text("changed\n")
        self.write(repo / "notes.txt", "untracked\n")
        steps = "".join(f"{n}. Step {n}\n" for n in range(1, 15))
        self.write(
            repo / "docs/agent/handoff.md",
            "# Handoff\n\n## Goal\nShip the hooks.\n\n## Next steps\n\n"
            + steps
            + "\n## Decisions\n- Decided something\n",
        )
        self.write(repo / "docs/PROGRESS.md", "## Today\nNext: from progress\n")
        newest = self.write(repo / "docs/agent/specs/2026-10-01-a.md", "spec a\n")
        older = self.write(repo / "docs/agent/specs/2026-10-07-b.md", "spec b\n")
        self.write(repo / "docs/agent/specs/.DS_Store", "")
        os.utime(older, (time.time() - 3600, time.time() - 3600))
        os.utime(newest, None)
        features = [
            {"description": "Login works", "passes": True},
            {"description": "Export CSV", "passes": False},
            {"name": "Dark mode", "passes": False},
            {"description": "Undo", "passes": False},
            {"description": "Sync", "passes": False},
        ]
        self.write(repo / "docs/plans/feature_list.json", json.dumps(features))
        today = dt.date.today()
        last_month = today.replace(day=1) - dt.timedelta(days=1)
        self.write(
            repo / "worklog/worklog.csv",
            "date,hours,blocks,description,source,zone\n"
            f"{last_month.isoformat()},9.00,,old,auto,\n"
            f'{today.strftime("%Y-%m")}-01,1.50,"10:00-11:30","Built, tested",auto,+02:00\n'
            f"{today.isoformat()},0.75,,Call,manual,\n",
        )

        out = self.output(repo)
        self.assertEqual(out["sessionTitle"], "proj")
        lines = out["additionalContext"].splitlines()
        self.assertLessEqual(len(lines), 40)
        self.assertRegex(
            lines[0],
            r"^## Session brief: proj on main, 4 uncommitted, last commit .+: Initial commit$",
        )
        start = lines.index("Next steps (docs/agent/handoff.md):")
        self.assertEqual(
            lines[start + 1 : start + 13], [f"{n}. Step {n}" for n in range(1, 13)]
        )
        self.assertNotIn("13. Step 13", lines)
        self.assertNotIn("- Decided something", lines)
        self.assertNotIn(
            "Next: from progress", lines
        )  # the handoff wins over PROGRESS.md
        self.assertIn("Latest spec: docs/agent/specs/2026-10-01-a.md", lines)
        start = lines.index("Not passing yet (docs/plans/feature_list.json, 4 of 5):")
        self.assertEqual(
            lines[start + 1 : start + 4], ["- Export CSV", "- Dark mode", "- Undo"]
        )
        self.assertEqual(lines[-1], "Hours this month: 2.25")

    def test_progress_next_lines_without_a_handoff(self):
        repo = self.make_repo()
        self.write(repo / "docs/PROGRESS.md", "## 2026-10-05\n- Did B\nNext: ship it\n")
        lines = self.output(repo)["additionalContext"].splitlines()
        self.assertEqual(lines[1:], ["docs/PROGRESS.md (2026-10-05):", "Next: ship it"])

    def test_session_title_only_on_startup(self):
        repo = self.make_repo()
        for source in ("resume", "compact"):
            out = self.output(repo, source)
            self.assertNotIn("sessionTitle", out)
            self.assertTrue(
                out["additionalContext"].startswith("## Session brief: proj on main")
            )

    def test_skips_project_with_its_own_session_start_hook(self):
        repo = self.make_repo()
        self.write(repo / "docs/agent/handoff.md", "## Next steps\n1. Something\n")
        settings = repo / ".claude/settings.json"
        self.write(settings, json.dumps({"hooks": {"PreToolUse": [{"hooks": []}]}}))
        self.assertIn("1. Something", self.output(repo)["additionalContext"])
        own = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]
            }
        }
        self.write(settings, json.dumps(own))
        self.assertEqual(self.brief(repo).stdout, "")

    def test_not_a_git_repo_reads_sections_from_the_directory(self):
        plain = self.tmp / "plain"
        self.write(plain / "docs/agent/handoff.md", "## Next steps\n1. Something\n")
        out = self.output(plain)
        self.assertEqual(
            out["additionalContext"].splitlines(),
            [
                "## Session brief: plain",  # no git facts
                "Next steps (docs/agent/handoff.md):",
                "1. Something",
            ],
        )
        self.assertEqual(out["sessionTitle"], "plain")

    def test_repo_without_commits_and_from_a_subdirectory(self):
        repo = self.make_repo(commit=False)
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        self.assertEqual(
            self.output(sub)["additionalContext"],
            "## Session brief: proj on main, 0 uncommitted, no commits yet",
        )

    def test_bad_stdin_fails_open(self):
        r = self.run_hook(
            "session_brief.py", "{not json", {"CLAUDE_PROJECT_DIR": str(self.tmp)}
        )
        self.assertEqual(r.returncode, 0)


class BriefSourcesTests(unittest.TestCase):
    """The brief's file readers, called directly on a scratch directory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("session_brief")

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="harness-brief-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_progress_uses_the_last_block_and_at_most_six_next_lines(self):
        self.write(
            "docs/PROGRESS.md",
            "# Progress\n\n## 2026-09-30\n- Did A\nNext: an old item\n\n"
            "## 2026-10-05\n- Did B\n- Next: first new item\nNext up: second\n"
            "Next: 3\nNext: 4\nNext: 5\nNext: 6\nNext: 7\nNot next\n",
        )
        self.assertEqual(
            self.hook.progress_part(self.root),
            [
                "docs/PROGRESS.md (2026-10-05):",
                "- Next: first new item",
                "Next up: second",
                "Next: 3",
                "Next: 4",
                "Next: 5",
                "Next: 6",
            ],
        )

    def test_progress_ignores_lines_inside_code_fences(self):
        self.write(
            "docs/PROGRESS.md",
            "## 2026-10-05\n- Did B\n```sh\n## not a heading\nNext: inside a fence\n```\n"
            "Next: outside\n~~~\nNext: inside a tilde fence\n~~~\n",
        )
        self.assertEqual(
            self.hook.progress_part(self.root),
            ["docs/PROGRESS.md (2026-10-05):", "Next: outside"],
        )

    def test_progress_without_next_lines_adds_nothing(self):
        self.write("docs/PROGRESS.md", "## 2026-10-05\n- Did B\n")
        self.assertEqual(self.hook.progress_part(self.root), [])

    def test_handoff_without_next_steps_shows_its_first_twenty_lines(self):
        self.write(
            "docs/agent/handoff.md", "".join(f"line {n}\n\n" for n in range(1, 26))
        )
        self.assertEqual(
            self.hook.handoff_part(self.root),
            ["docs/agent/handoff.md (no Next steps section; its first lines):"]
            + [f"line {n}" for n in range(1, 21)],
        )

    def test_missing_handoff_is_none(self):
        self.assertIsNone(self.hook.handoff_part(self.root))

    def test_next_steps_as_bold_label(self):
        self.write(
            "docs/agent/handoff.md",
            "- **Goal**: ship it\n- **Next steps**:\n  1. First thing\n  2. Second thing\n"
            "- **Decisions**: keep it small\n",
        )
        self.assertEqual(
            self.hook.handoff_part(self.root),
            [
                "Next steps (docs/agent/handoff.md):",
                "  1. First thing",
                "  2. Second thing",
            ],
        )

    def test_next_steps_label_with_inline_text(self):
        self.write(
            "docs/agent/handoff.md",
            "**Next steps:** run the suite\n\n**Gotchas**: none\n",
        )
        self.assertEqual(
            self.hook.handoff_part(self.root),
            ["Next steps (docs/agent/handoff.md):", "run the suite"],
        )

    def test_next_steps_section_keeps_code_fences_whole(self):
        self.write(
            "docs/agent/handoff.md",
            "## Next steps\n1. Run:\n```sh\n# rebuild everything\nmake\n```\n2. Check\n"
            "## Decisions\n- none\n",
        )
        self.assertEqual(
            self.hook.handoff_part(self.root),
            [
                "Next steps (docs/agent/handoff.md):",
                "1. Run:",
                "```sh",
                "# rebuild everything",
                "make",
                "```",
                "2. Check",
            ],
        )

    def test_feature_list_shapes(self):
        rel = "docs/plans/feature_list.json"
        self.write(
            rel, json.dumps({"features": [{"description": "X", "passes": False}]})
        )
        self.assertEqual(self.hook.pending_features(self.root), [])
        self.write(rel, json.dumps([{"description": "X"}, {"description": "Y"}]))
        self.assertEqual(self.hook.pending_features(self.root), [])
        self.write(rel, json.dumps([{"description": "X", "passes": True}]))
        self.assertEqual(self.hook.pending_features(self.root), [])
        self.write(rel, "not json")
        self.assertEqual(self.hook.pending_features(self.root), [])
        self.write(rel, json.dumps([{"id": 7, "steps": [], "passes": False}]))
        self.assertEqual(
            self.hook.pending_features(self.root),
            ["Not passing yet (docs/plans/feature_list.json, 1 of 1):", "- 7"],
        )

    def test_latest_spec_ignores_hidden_files_and_empty_folders(self):
        (self.root / "docs/agent/specs").mkdir(parents=True)
        self.assertEqual(self.hook.latest_spec(self.root), [])
        self.write("docs/agent/specs/.DS_Store", "")
        self.assertEqual(self.hook.latest_spec(self.root), [])
        self.write("docs/agent/specs/2026-10-06-x.md", "x\n")
        self.assertEqual(
            self.hook.latest_spec(self.root),
            ["Latest spec: docs/agent/specs/2026-10-06-x.md"],
        )

    def test_hours_this_month_tolerates_bom_and_bad_rows(self):
        month = dt.date.today().strftime("%Y-%m")
        self.write(
            "worklog/worklog.csv",
            chr(0xFEFF) + "date,hours,blocks,description,source,zone\n"
            f"{month}-01,1.25,,a,auto,\n{month}-02,,,b,manual,\n{month}-03,oops,,c,manual,\n",
        )
        self.assertEqual(
            self.hook.hours_this_month(self.root), ["Hours this month: 1.25"]
        )


# ---------------------------------------------------------------- after_edit.py


class AfterEditTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()

    def edit(
        self, path, tool: str = "Edit", session: str = "sess-1", cwd=None, env=None
    ):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        payload = {
            "session_id": session,
            "transcript_path": "/dev/null",
            "cwd": str(cwd or self.repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": {key: str(path)},
            "tool_response": {"success": True},
        }
        return self.run_hook("after_edit.py", payload, env)

    def dirty_lines(self, session: str = "sess-1", root=None) -> list[str]:
        flag = self.state_dir(root or self.repo) / f"dirty-{session}"
        return flag.read_text().splitlines()

    def test_records_every_edit_for_the_stop_hook(self):
        a = self.write(self.repo / "a.py", "x = 1\n")
        nb = self.write(self.repo / "nb.ipynb", "{}\n")
        c = self.write(self.repo / "src/c.txt", "c\n")
        for path, tool in ((a, "Edit"), (nb, "NotebookEdit"), ("src/c.txt", "Write")):
            r = self.edit(path, tool)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.dirty_lines(), [str(a), str(nb), str(c)])
        self.edit(a, session="sess-2")
        self.assertEqual(self.dirty_lines("sess-2"), [str(a)])

    def test_outside_a_git_repo_state_is_keyed_on_cwd(self):
        plain = self.tmp / "plain"
        f = self.write(plain / "notes.md", "hello\n")
        self.assertEqual(self.edit(f, cwd=plain).returncode, 0)
        self.assertEqual(self.dirty_lines(root=plain), [str(f)])

    def test_secret_hit_exits_2_with_location(self):
        f = self.write(
            self.repo / "settings.py",
            'DEBUG = True\nAWS_KEY = "'
            + AWS_KEY
            + '"\nNAME = "x"\n'
            + SECRET_SAMPLES[1][1]
            + "\n",
        )
        r = self.edit(f)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(
            r.stderr.splitlines(),
            [
                f"SECRET pattern in {f}:2 (AWS access key id): AKIA"
                + ELLIPSIS
                + SECRET_ADVICE,
                f"SECRET pattern in {f}:4 (GitHub token): ghp_"
                + ELLIPSIS
                + SECRET_ADVICE,
            ],
        )
        self.assertNotIn(AWS_KEY, r.stderr)
        self.assertEqual(
            self.dirty_lines(), [str(f)]
        )  # recorded even though the scan objected

    def test_placeholder_lines_are_skipped(self):
        f = self.write(self.repo / "conf.txt", "\n".join(PLACEHOLDER_LINES) + "\n")
        r = self.edit(f)
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_clean_file_is_silent(self):
        f = self.write(
            self.repo / "app.py",
            'import os\n\nTOKEN = os.environ["API_TOKEN"]\n'
            "# see docs/agent/specs/2026-10-06-task-management-dashboard-v2.md\n",
        )
        r = self.edit(f)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_scan_can_be_switched_off(self):
        f = self.write(
            self.repo / "fixture.py",
            "# " + ALLOW_MARKER + "\nKEY = '" + AWS_KEY + "'\n",
        )
        self.assertEqual(self.edit(f).returncode, 0)
        g = self.write(self.repo / "other.py", "KEY = '" + AWS_KEY + "'\n")
        self.assertEqual(self.edit(g, env={"HARNESS_SECRET_SCAN": "0"}).returncode, 0)
        self.assertEqual(self.edit(g).returncode, 2)
        self.assertEqual(self.dirty_lines(), [str(f), str(g), str(g)])

    def test_missing_file_is_recorded_and_not_scanned(self):
        gone = self.repo / "deleted.py"
        r = self.edit(gone)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(self.dirty_lines(), [str(gone)])

    def test_bad_input_fails_open(self):
        for stdin in ("{not json", json.dumps({"tool_input": {}})):
            with self.subTest(stdin=stdin):
                r = self.run_hook("after_edit.py", stdin)
                self.assertEqual((r.returncode, r.stdout), (0, ""))

    def bash(self, cwd=None):
        """The hook after a Bash call, which names no file."""
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd or self.repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "sh generate.sh", "description": "Generate"},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
        }
        return self.run_hook("after_edit.py", payload)

    def test_bash_call_scans_the_files_git_lists(self):
        # Written through the shell, as sed -i or a redirect would: a tracked file changed
        # and an untracked one created, neither named in the Bash call.
        readme = self.write(
            self.repo / "README.md", "hello\n" + SECRET_SAMPLES[1][1] + "\n"
        )
        script = self.write(
            self.repo / "deploy/env.sh", "export AWS_KEY=" + AWS_KEY + "\n"
        )
        now = time.time()
        os.utime(readme, (now - 60, now - 60))
        os.utime(script, (now - 30, now - 30))
        r = self.bash()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(
            r.stderr.splitlines(),
            [  # newest first
                f"SECRET pattern in {script}:1 (AWS access key id): AKIA"
                + ELLIPSIS
                + SECRET_ADVICE,
                f"SECRET pattern in {readme}:2 (GitHub token): ghp_"
                + ELLIPSIS
                + SECRET_ADVICE,
            ],
        )
        self.assertEqual(self.dirty_lines(), [str(script), str(readme)])

    def test_bash_scan_skips_files_older_than_the_last_one(self):
        stale = self.write(self.repo / "old.py", 'KEY = "' + AWS_KEY + '"\n')
        hour_ago = time.time() - 3600
        os.utime(stale, (hour_ago, hour_ago))  # outside the first scan's 10 minutes
        f = self.write(self.repo / "settings.py", 'AWS_KEY = "' + AWS_KEY + '"\n')
        r = self.bash()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(
            r.stderr.splitlines(),
            [
                f"SECRET pattern in {f}:1 (AWS access key id): AKIA"
                + ELLIPSIS
                + SECRET_ADVICE
            ],
        )
        self.assertEqual(self.dirty_lines(), [str(f)])

        # The same file, older than the stored scan time: not scanned or recorded again.
        stamp = self.state_dir(self.repo) / "bash-scan-sess-1"
        scanned = float(stamp.read_text())
        os.utime(f, (scanned - 5, scanned - 5))
        r = self.bash()
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(self.dirty_lines(), [str(f)])

        # Changed after that scan, it is.
        later = float(stamp.read_text()) + 1
        os.utime(f, (later, later))
        self.assertEqual(self.bash().returncode, 2)
        self.assertEqual(self.dirty_lines(), [str(f), str(f)])

    def test_bash_scan_follows_a_rename(self):
        self.git(self.repo, "mv", "README.md", "GUIDE.md")
        guide = self.write(
            self.repo / "GUIDE.md", "hello\n" + SECRET_SAMPLES[0][1] + "\n"
        )
        r = self.bash()
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"SECRET pattern in {guide}:2 (AWS access key id)", r.stderr)
        self.assertEqual(self.dirty_lines(), [str(guide)])

    def test_bash_outside_a_git_repo_does_nothing(self):
        plain = self.tmp / "plain"
        self.write(plain / "keys.txt", "KEY = " + AWS_KEY + "\n")
        r = self.bash(cwd=plain)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(self.state_dir(plain).exists())  # no dirty list, no scan time


class SecretPatternTests(unittest.TestCase):
    """after_edit.py's scanner, called directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("after_edit")

    def test_every_pattern_is_detected_by_name(self):
        for name, line in SECRET_SAMPLES:
            with self.subTest(line=line):
                hits = self.hook.scan("first line\n" + line + "\n")
                self.assertEqual([(n, p) for n, p, _ in hits], [(2, name)])

    def test_placeholder_lines_are_skipped(self):
        for line in PLACEHOLDER_LINES:
            with self.subTest(line=line):
                self.assertEqual(self.hook.scan(line), [])

    def test_generic_assignment_in_any_case_provider_patterns_exact(self):
        value = "Zq8Lm4Np2Rs6Tv" * 2
        for line in (
            "API_KEY" + ' = "' + value + '"',
            "Secret" + ": '" + value + "'",
            "apiKey" + ': "' + value + '"',
        ):
            with self.subTest(line=line):
                hits = self.hook.scan(line + "\n")
                self.assertEqual(
                    [(n, p) for n, p, _ in hits], [(1, "generic secret assignment")]
                )
        lowercase_aws = "aws_key_id = " + '"' + "akia" + "abcdefghijklmnop" + '"'
        self.assertEqual(self.hook.scan(lowercase_aws + "\n"), [])

    def test_words_ending_in_sk_are_not_keys(self):
        text = (
            "see docs/agent/specs/2026-10-06-task-management-dashboard-v2.md\n"
            "flask-sqlalchemy-integration-guide, risk-assessment-and-mitigation-plan\n"
            "the_desk_live_streaming_feature_flag_enabled\n"
        )
        self.assertEqual(self.hook.scan(text), [])

    def test_line_numbers_count_newlines_only(self):
        text = "a\x0cb\nc" + chr(0x2028) + "d\n" + SECRET_SAMPLES[0][1] + "\n"
        self.assertEqual([n for n, _, _ in self.hook.scan(text)], [3])

    def test_large_and_non_utf8_files_are_not_scanned(self):
        tmp = Path(tempfile.mkdtemp(prefix="harness-scan-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        line = ("KEY = " + AWS_KEY + "\n").encode()
        small, big, binary = tmp / "small.txt", tmp / "big.txt", tmp / "blob.bin"
        small.write_bytes(line)
        big.write_bytes(line + b"a" * (2 * 1024 * 1024))
        binary.write_bytes(b"\xff\xfe\x00" + line)
        with mock.patch.dict(os.environ):
            os.environ.pop("HARNESS_SECRET_SCAN", None)
            self.assertEqual(len(self.hook.secret_hits(small)), 1)
            self.assertEqual(self.hook.secret_hits(big), [])
            self.assertEqual(self.hook.secret_hits(binary), [])
            self.assertEqual(self.hook.secret_hits(tmp), [])


class BashScanHelpersTests(unittest.TestCase):
    """after_edit.py's git status parsing and file selection, called directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("after_edit")

    def test_status_paths_skip_the_source_of_a_rename_or_copy(self):
        root = Path("/repo")
        out = (
            b" M a.py\0R  new name.py\0old.py\0C  copy.py\0orig.py\0"
            b"RM moved.py\0was.py\0?? d/e/f.txt\0"
        )
        self.assertEqual(
            self.hook.status_paths(root, out),
            [
                root / n
                for n in ("a.py", "new name.py", "copy.py", "moved.py", "d/e/f.txt")
            ],
        )
        self.assertEqual(self.hook.status_paths(root, b""), [])

    def test_recent_files_are_regular_small_newest_first_and_capped(self):
        tmp = Path(tempfile.mkdtemp(prefix="harness-recent-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        now = time.time()
        files = []
        for n in range(105):
            f = tmp / f"f{n:03}.txt"
            f.write_text("x")
            os.utime(f, (now - 500 + n, now - 500 + n))
            files.append(f)
        old, big, folder = tmp / "old.txt", tmp / "big.bin", tmp / "folder"
        old.write_text("x")
        os.utime(old, (now - 5000, now - 5000))
        big.write_bytes(b"a" * (2 * 1024 * 1024))
        folder.mkdir()
        link = tmp / "link.txt"
        os.symlink(files[-1], link)
        candidates = files + [old, big, folder, link, tmp / "missing.txt"]
        self.assertEqual(
            self.hook.recent_files(candidates, now - 1000), files[::-1][:100]
        )


# ---------------------------------------------------------------- verify_on_stop.py


class VerifyOnStopTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.src = self.write(self.repo / "app.py", "x = 1\n")
        self.marker = self.tmp / "ran"

    @property
    def dirty(self) -> Path:
        return self.state_dir(self.repo) / "dirty-sess-1"

    def edit(self, path: Path | None = None) -> None:
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(self.repo),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(path or self.src)},
        }
        r = self.run_hook("after_edit.py", payload)
        self.assertEqual(r.returncode, 0, r.stderr)

    def stop(self, cmd=None, active: bool = False, cwd=None, env=None, **extra):
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd or self.repo),
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": active,
        }
        payload.update(extra)
        env = dict(env or {})
        if cmd is not None:
            env["CLAUDE_VERIFY_CMD"] = cmd
        r = self.run_hook("verify_on_stop.py", payload, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def touch(self, path: Path) -> str:
        return "touch " + shlex.quote(str(path))

    def unchanged(self, cmd: str) -> dict:
        """What a fresh stop says instead of re-running a check that nothing has changed."""
        return {
            "systemMessage": f"verify_on_stop: {cmd} still failing and nothing changed "
            "since the last check; fix it or run it by hand."
        }

    def test_silent_without_edits_or_without_a_command(self):
        self.assertIsNone(self.stop(self.touch(self.marker)))  # no dirty file yet
        self.assertFalse(self.marker.exists())
        self.edit()
        self.assertIsNone(self.stop())  # no verify command
        self.assertTrue(self.dirty.exists())

    def test_waits_for_background_tasks(self):
        self.edit()
        busy = [{"id": "t1", "type": "bash", "status": "running"}]
        self.assertIsNone(self.stop(self.touch(self.marker), background_tasks=busy))
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.dirty.exists())
        self.assertEqual(
            self.stop(self.touch(self.marker), background_tasks=[]),
            {"systemMessage": f"verify_on_stop: {self.touch(self.marker)} passed."},
        )

    def test_pass_records_green_and_clears_the_dirty_list(self):
        self.edit()
        self.assertEqual(
            self.stop("true"), {"systemMessage": "verify_on_stop: true passed."}
        )
        self.assertFalse(self.dirty.exists())
        state = json.loads(
            (self.state_dir(self.repo) / "verify-sess-1.json").read_text()
        )
        self.assertEqual(state["blocks"], 0)
        self.assertRegex(state["green"], r"^[0-9a-f]{64}$")

        self.edit()  # the same content again: already verified, so nothing runs
        self.assertIsNone(self.stop(self.touch(self.marker) + "; false"))
        self.assertFalse(self.marker.exists())

        self.git(self.repo, "add", "app.py")  # a new HEAD is a new state
        self.git(self.repo, "commit", "-q", "-m", "Add app")
        self.assertEqual(self.stop("false")["decision"], "block")

        self.src.write_text("x = 2\n")  # and so is new content
        self.edit()
        self.assertEqual(
            self.stop("true"), {"systemMessage": "verify_on_stop: true passed."}
        )

    def test_failure_blocks_with_the_tail_of_the_output(self):
        script = self.write(
            self.tmp / "noisy-check.sh",
            'i=1\nwhile [ $i -le 70 ]; do echo "line $i"; echo; i=$((i+1)); done\n'
            "echo 'boom' >&2\nexit 1\n",
        )
        cmd = "sh " + shlex.quote(str(script))
        self.edit()
        result = self.stop(cmd)
        self.assertEqual(list(result), ["decision", "reason"])
        self.assertEqual(result["decision"], "block")
        lines = result["reason"].split("\n")
        self.assertEqual(lines[0], f"{cmd} failed:")
        self.assertEqual(lines[1], "... (11 earlier lines omitted)")
        self.assertEqual(lines[2], "line 12")
        self.assertEqual(lines[60], "line 70")
        self.assertEqual(lines[61:], ["boom", "", GUIDANCE])
        self.assertTrue(self.dirty.exists())

    def test_at_most_two_blocks_in_a_row(self):
        self.edit()
        expected = "false failed:\n(no output; exit status 1)\n\n" + GUIDANCE
        self.assertEqual(self.stop("false"), {"decision": "block", "reason": expected})
        # A fresh stop (stop_hook_active false) after an edit starts the count again ...
        self.src.write_text("x = 2\n")
        self.edit()
        self.assertEqual(self.stop("false")["decision"], "block")
        # ... so this is the second block in a row, and the next stop is let through.
        self.assertEqual(self.stop("false", active=True)["decision"], "block")
        self.assertEqual(
            self.stop("false", active=True),
            {
                "systemMessage": "verify_on_stop: still failing after 2 automatic retries; "
                "review before trusting this turn."
            },
        )
        self.src.write_text("x = 3\n")
        self.edit()
        self.assertEqual(
            self.stop("false")["decision"], "block"
        )  # the next turn, after an edit, tries again
        self.assertTrue(self.dirty.exists())

    def test_fresh_stop_does_not_rerun_an_unchanged_failure(self):
        cmd = self.touch(self.marker) + "; exit 1"
        self.edit()
        self.assertEqual(self.stop(cmd)["decision"], "block")
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        # The next turn edits nothing (a question and its answer): one line, no run.
        self.assertEqual(self.stop(cmd), self.unchanged(cmd))
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.dirty.exists())
        # Another command is another check, so it runs.
        self.assertEqual(self.stop("false")["decision"], "block")

    def test_an_edit_after_a_failure_runs_the_command_again(self):
        cmd = self.touch(self.marker) + "; grep -q ok app.py"
        self.edit()
        self.assertEqual(self.stop(cmd)["decision"], "block")
        self.marker.unlink()
        self.src.write_text("x = 2\n")  # a new fingerprint
        self.edit()
        self.assertEqual(self.stop(cmd)["decision"], "block")
        self.assertTrue(self.marker.exists())
        self.src.write_text("x = 'ok'\n")
        self.edit()
        self.assertEqual(
            self.stop(cmd), {"systemMessage": f"verify_on_stop: {cmd} passed."}
        )
        # The green run forgot the failure, so the failing content is checked again.
        self.marker.unlink()
        self.src.write_text("x = 2\n")
        self.edit()
        self.assertEqual(self.stop(cmd)["decision"], "block")
        self.assertTrue(self.marker.exists())

    def test_a_timeout_is_remembered_like_a_failure(self):
        cmd = self.touch(self.marker) + "; sleep 8"
        env = {"CLAUDE_VERIFY_TIMEOUT": "1"}
        self.edit()
        message = self.stop(cmd, env=env)["systemMessage"]
        self.assertTrue(message.startswith(f"verify_on_stop: {cmd} timed out after 1s"))
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        self.assertEqual(self.stop(cmd, env=env), self.unchanged(cmd))
        self.assertFalse(self.marker.exists())

    def test_command_from_project_settings_env(self):
        a, b, c = (self.tmp / name for name in ("a", "b", "c"))
        settings = {"env": {"CLAUDE_VERIFY_CMD": self.touch(a) + "; exit 1"}}
        self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.edit()
        self.assertEqual(self.stop()["decision"], "block")
        self.assertTrue(a.exists())

        local = {"env": {"CLAUDE_VERIFY_CMD": self.touch(b) + "; exit 1"}}
        self.write(self.repo / ".claude/settings.local.json", json.dumps(local))
        self.assertEqual(self.stop()["decision"], "block")
        self.assertTrue(b.exists())

        self.assertEqual(self.stop(self.touch(c) + "; exit 1")["decision"], "block")
        self.assertTrue(c.exists())

    def test_skips_project_with_its_own_stop_hook(self):
        settings = self.repo / ".claude/settings.json"
        self.edit()
        # An empty Stop list declares no hook, so the failure still blocks ...
        self.write(settings, json.dumps({"hooks": {"Stop": []}}))
        self.assertEqual(self.stop("false")["decision"], "block")
        # ... but a project that runs its own Stop hook is not verified twice.
        own = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}
        self.write(settings, json.dumps(own))
        self.assertIsNone(self.stop(self.touch(self.marker) + "; exit 1"))
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.dirty.exists())

    def test_first_stop_records_the_tree_and_runs_nothing(self):
        cmd = self.touch(self.marker) + "; false"
        # No edit recorded, and app.py (untracked) predates the session's first stop.
        self.assertIsNone(self.stop(cmd))
        self.assertFalse(self.marker.exists())
        state = json.loads(
            (self.state_dir(self.repo) / "verify-sess-1.json").read_text()
        )
        self.assertRegex(state["tree"], r"^[0-9a-f]{64}$")
        self.assertFalse(self.dirty.exists())
        self.assertIsNone(self.stop(cmd))  # the same tree again
        self.assertFalse(self.marker.exists())

    def test_a_change_no_edit_recorded_runs_the_command(self):
        cmd = self.touch(self.marker) + "; false"
        self.assertIsNone(self.stop(cmd))  # the first stop records the tree
        # Changed through the shell (sed -i, a redirect), so after_edit.py saw nothing.
        readme = self.repo / "README.md"
        readme.write_text("changed through the shell\n")
        self.assertEqual(self.stop(cmd)["decision"], "block")
        self.assertTrue(self.marker.exists())
        # It now counts as an edit until a green run: an unchanged tree does not run the
        # failing check again, ...
        self.assertEqual(
            sorted(self.dirty.read_text().splitlines()), [str(readme), str(self.src)]
        )
        self.marker.unlink()
        self.assertEqual(self.stop(cmd), self.unchanged(cmd))
        self.assertFalse(self.marker.exists())
        # ... a pass clears it, and after that the unchanged tree runs nothing.
        self.assertEqual(
            self.stop("true"), {"systemMessage": "verify_on_stop: true passed."}
        )
        self.assertFalse(self.dirty.exists())
        self.assertIsNone(self.stop(cmd))
        self.assertFalse(self.marker.exists())

    def test_the_fingerprint_covers_every_file_git_lists(self):
        readme = self.repo / "README.md"
        readme.write_text("first\n")  # already modified when the check passes
        self.edit()
        self.assertEqual(
            self.stop("true"), {"systemMessage": "verify_on_stop: true passed."}
        )
        # Changed again through the shell: git status reads the same and the recorded
        # edit's content is what passed, but README.md is not what was checked.
        readme.write_text("second\n")
        self.edit()
        self.assertEqual(self.stop("false")["decision"], "block")

    def test_runs_from_the_repo_root(self):
        sub = self.repo / "src"
        sub.mkdir()
        self.edit()
        self.assertEqual(
            self.stop("test -f README.md", cwd=sub),
            {"systemMessage": "verify_on_stop: test -f README.md passed."},
        )

    def test_timeout_is_reported_and_stops_the_whole_command(self):
        self.edit()
        started = time.monotonic()
        result = self.stop("sleep 8; echo late", env={"CLAUDE_VERIFY_TIMEOUT": "0.5"})
        self.assertLess(
            time.monotonic() - started, 4
        )  # the shell's child was killed too
        self.assertEqual(list(result), ["systemMessage"])
        self.assertTrue(
            result["systemMessage"].startswith(
                "verify_on_stop: sleep 8; echo late timed out after 0.5s"
            )
        )
        self.assertTrue(self.dirty.exists())

    def test_command_that_cannot_start_is_reported_not_blocked(self):
        self.edit()
        cmd = "echo preparing; echo; harness-no-such-command"
        result = self.stop(cmd)
        self.assertEqual(list(result), ["systemMessage"])
        message = result["systemMessage"]
        self.assertTrue(message.startswith(f"verify_on_stop: could not start {cmd}: "))
        # The shell's own words: "sh: harness-no-such-command: command not found" from
        # bash or zsh as sh (macOS), "sh: 1: harness-no-such-command: not found" from dash
        # (Debian and Ubuntu).
        last = message[len(f"verify_on_stop: could not start {cmd}: ") :]
        self.assertIn("harness-no-such-command", last)
        self.assertTrue(last.endswith("not found"), last)
        self.assertNotIn("\n", message)
        self.assertTrue(self.dirty.exists())
        # Remembered like a failure: the next unchanged turn does not try again.
        self.assertEqual(self.stop(cmd), self.unchanged(cmd))

    def test_cancelled_hook_takes_the_command_down(self):
        self.edit()
        pidfile = self.tmp / "verify.pid"
        payload = {
            "session_id": "sess-1",
            "cwd": str(self.repo),
            "stop_hook_active": False,
        }
        env = dict(
            self.env,
            CLAUDE_VERIFY_CMD=f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep 30",
        )
        hook = subprocess.Popen(
            [sys.executable, str(HOOKS / "verify_on_stop.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(self.tmp),
        )
        self.addCleanup(hook.kill)
        hook.stdin.write(json.dumps(payload).encode())
        hook.stdin.close()
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text().strip()):
            self.assertLess(
                time.monotonic(), deadline, "the verify command never started"
            )
            time.sleep(0.02)
        pid = int(pidfile.read_text())
        hook.terminate()  # what Claude Code does to a hook it gives up on
        self.assertEqual(hook.wait(timeout=10), 0)
        self.assertEqual(hook.stdout.read(), b"")
        hook.stdout.close()
        hook.stderr.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            self.assertLess(
                time.monotonic(), deadline, "the verify command outlived its hook"
            )
            time.sleep(0.02)

    # ---- docs-and-config-only turns (#163)

    def skipped(self, n: int) -> dict:
        return {
            "systemMessage": f"verify_on_stop: only docs and config changed ({n} files); "
            "verify skipped."
        }

    def passed(self) -> dict:
        return {"systemMessage": f"verify_on_stop: {self.touch(self.marker)} passed."}

    def baseline(self, name: str | None = None, settings: dict | None = None) -> None:
        """Point the test at a repo with app.py (and the project settings, if any)
        committed, so git status lists nothing until the test changes something. A new
        name is a new repo, hence a new session state; the marker is reset."""
        if name is not None:
            self.repo = self.make_repo(name)
            self.src = self.write(self.repo / "app.py", "x = 1\n")
        if settings is not None:
            self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "Baseline")
        if self.marker.exists():
            self.marker.unlink()

    def change(self, *rels: str, record: bool = True) -> None:
        """Write (or append to) each repo-relative path; record=True also reports each
        write through after_edit.py, as the Edit tool would."""
        for rel in rels:
            path = self.repo / rel
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if rel.endswith(".json"):
                text = json.dumps({"env": {}, "changed": len(old)})
            else:
                text = old + "changed\n"
            self.write(path, text)
            if record:
                self.edit(path)

    def verify_state(self) -> dict:
        try:
            return json.loads(
                (self.state_dir(self.repo) / "verify-sess-1.json").read_text()
            )
        except (OSError, ValueError):
            return {}

    DOCS_ONLY = [
        (("docs/guide/a.md", "docs/b.txt", "CHANGELOG.md"), 3),
        ((".github/workflows/ci.yml",), 1),
        (("notes/todo.txt", ".gitignore", "config/app.yaml", "LICENSE"), 4),
        ((".claude/agents/helper.md", "docs/x.md"), 2),
    ]

    def test_docs_only_turn_skips_the_command(self):
        cmd = self.touch(self.marker) + "; false"
        self.baseline()
        self.change("docs/x.md", "README.md")  # a new doc and a tracked one
        self.assertEqual(self.stop(cmd), self.skipped(2))
        self.assertFalse(self.marker.exists())
        # Varied sets under the default HARNESS_SOURCE_EXEMPT, each in a repo of its own.
        for i, (rels, n) in enumerate(self.DOCS_ONLY):
            with self.subTest(rels=rels):
                self.baseline(f"docs-only-{i}")
                self.change(*rels)
                self.assertEqual(self.stop(cmd), self.skipped(n))
                self.assertFalse(self.marker.exists())

    def test_docs_only_change_through_the_shell_skips_the_command(self):
        cmd = self.touch(self.marker) + "; false"
        self.baseline()
        self.assertIsNone(self.stop(cmd))  # the first stop records the tree
        # Nothing reported through after_edit.py: the paths come from git status alone.
        self.change("docs/x.md", "docs/y.md", "README.md", record=False)
        self.assertEqual(self.stop(cmd), self.skipped(3))
        self.assertFalse(self.marker.exists())

    def test_a_skipped_turn_records_nothing_as_green(self):
        self.baseline()
        self.change("docs/x.md")
        self.assertEqual(self.stop(self.touch(self.marker)), self.skipped(1))
        self.assertFalse(self.marker.exists())
        state = self.verify_state()
        self.assertNotIn("green", state)
        self.assertNotIn("attempted", state)  # not a failure either
        self.assertTrue(self.dirty.exists())
        # A later source change in the same session runs the command, over both files.
        self.change("app.py")
        self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
        self.assertTrue(self.marker.exists())

    def test_source_path_runs_the_command(self):
        cases = [
            ("README.md", "app.py"),
            ("docs/x.md", "src/lib/util.py"),
            ("docs/x.md", "scripts/build.sh"),
            ("CHANGELOG.md", "Makefile"),
        ]
        for i, rels in enumerate(cases):
            with self.subTest(rels=rels):
                self.baseline(f"source-{i}")
                self.change(*rels)
                self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
                self.assertTrue(self.marker.exists())

    def test_source_change_through_the_shell_beside_a_recorded_doc_runs(self):
        self.baseline()
        self.assertIsNone(self.stop(self.touch(self.marker)))  # records the tree
        self.change("README.md")  # reported through after_edit.py
        self.change("app.py", record=False)  # only git status sees this one
        self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
        self.assertTrue(self.marker.exists())

    def test_test_file_runs_the_command(self):
        cases = [
            ("docs/x.md", "tests/test_app.py"),
            ("README.md", "pkg/test_util.py"),
            ("docs/x.md", "web/a.test.js"),
            # Exempt by extension, but a test by HARNESS_TEST_PATTERNS: a test wins.
            ("docs/x.md", "tests/fixtures/data.json"),
            ("tests/README.md",),
            ("spec/notes.txt",),
        ]
        for i, rels in enumerate(cases):
            with self.subTest(rels=rels):
                self.baseline(f"test-file-{i}")
                self.change(*rels)
                self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
                self.assertTrue(self.marker.exists())

    def test_test_configuration_runs_the_command(self):
        # Each is exempt under the default HARNESS_SOURCE_EXEMPT, and still never skipped.
        configs = [
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            "pytest.ini",
            "package.json",
            "jest.config.json",
            ".claude/settings.json",
            ".claude/settings.local.json",
            # A TEST_CONFIG entry without a slash matches the basename at any depth.
            "web/package.json",
            "pkg/setup.cfg",
            "sub/pyproject.toml",
            "vitest.config.json",
            "lib/conftest.py",
        ]
        for i, rel in enumerate(configs):
            with self.subTest(rel=rel):
                self.baseline(f"config-{i}")
                self.change("docs/x.md", rel)
                self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
                self.assertTrue(self.marker.exists())

    def test_project_exempt_setting_is_honoured(self):
        cmd = self.touch(self.marker)
        own = {"env": {"HARNESS_SOURCE_EXEMPT": "assets/**,**/*.md"}}
        # (settings, environment, changed paths, files when skipped or None when it runs)
        cases = [
            # The project's list makes assets/ exempt, which the default does not ...
            (own, {}, ("assets/logo.svg", "README.md"), 2),
            (own, {}, ("assets/icons/a.png",), 1),
            # ... and replaces the default, so docs/notes.txt is no longer exempt.
            (own, {}, ("docs/notes.txt",), None),
            (own, {}, ("README.md", "app.py"), None),
            # The project's HARNESS_TEST_PATTERNS likewise: checks/ holds tests, tests/
            # no longer does.
            ({"env": {"HARNESS_TEST_PATTERNS": "checks/**"}}, {}, ("checks/notes.md",), None),
            ({"env": {"HARNESS_TEST_PATTERNS": "checks/**"}}, {}, ("tests/notes.md",), 1),
            # The environment wins over the project's settings.
            (own, {"HARNESS_SOURCE_EXEMPT": "media/**,**/*.md"}, ("media/clip.mp4",), 1),
            (own, {"HARNESS_SOURCE_EXEMPT": "media/**,**/*.md"}, ("assets/logo.svg",), None),
            (
                {"env": {"HARNESS_TEST_PATTERNS": "checks/**"}},
                {"HARNESS_TEST_PATTERNS": "probes/**"},
                ("probes/notes.md",),
                None,
            ),
            (
                {"env": {"HARNESS_TEST_PATTERNS": "checks/**"}},
                {"HARNESS_TEST_PATTERNS": "probes/**"},
                ("checks/notes.md",),
                1,
            ),
        ]
        for i, (settings, env, rels, n) in enumerate(cases):
            with self.subTest(settings=settings, env=env, rels=rels):
                self.baseline(f"project-{i}", settings=settings)
                self.change(*rels)
                result = self.stop(cmd, env=env)
                if n is None:
                    self.assertEqual(result, self.passed())
                    self.assertTrue(self.marker.exists())
                else:
                    self.assertEqual(result, self.skipped(n))
                    self.assertFalse(self.marker.exists())

    def test_local_settings_do_not_set_the_patterns(self):
        # As in tests_first: the environment, then the main checkout's settings.json, never
        # settings.local.json (gitignored here, so it is not itself a dirty path).
        cmd = self.touch(self.marker)
        local = {"env": {"HARNESS_SOURCE_EXEMPT": "assets/**,**/*.md"}}
        self.write(self.repo / ".gitignore", ".claude/settings.local.json\n")
        self.write(self.repo / ".claude/settings.local.json", json.dumps(local))
        self.baseline()
        self.change("assets/logo.svg", "README.md")
        self.assertEqual(self.stop(cmd), self.passed())
        self.assertTrue(self.marker.exists())
        # The same setting in settings.json is honoured.
        self.baseline("local-vs-shared", settings=local)
        self.change("assets/logo.svg", "README.md")
        self.assertEqual(self.stop(cmd), self.skipped(2))
        self.assertFalse(self.marker.exists())

    def test_a_worktree_reads_the_main_checkouts_settings(self):
        # The ticket branch forked before the main checkout's settings.json gained
        # HARNESS_SOURCE_EXEMPT, so the worktree's own copy has no such setting.
        cmd = self.touch(self.marker)
        self.baseline()
        self.git(self.repo, "branch", "t/1-ticket")
        own = {"env": {"HARNESS_SOURCE_EXEMPT": "assets/**,**/*.md"}}
        self.baseline(settings=own)
        worktree = self.tmp / "proj-t1"
        self.git(self.repo, "worktree", "add", "-q", str(worktree), "t/1-ticket")
        self.assertFalse((worktree / ".claude/settings.json").exists())
        self.repo, self.src = worktree, worktree / "app.py"
        self.change("assets/logo.svg")
        self.assertEqual(self.stop(cmd), self.skipped(1))
        self.assertFalse(self.marker.exists())
        # A change only the default would exempt is not exempt under the project's list.
        self.change("docs/notes.txt")
        self.assertEqual(self.stop(cmd), self.passed())
        self.assertTrue(self.marker.exists())

    def test_a_staged_rename_counts_its_source(self):
        # git status lists only the new name of a rename; the old one counts too.
        cmd = self.touch(self.marker)
        cases = [
            # A test fixture moved to an exempt name: a test changed, so it runs.
            ("tests/fixtures/expected.json", "data/expected.json", None),
            # Source renamed to an exempt name: source changed, so it runs.
            ("app.py", "notes.txt", None),
            # Between two exempt paths: still docs only, both names counted.
            ("docs/a.md", "docs/b.md", 2),
        ]
        for i, (old, new, n) in enumerate(cases):
            with self.subTest(old=old, new=new):
                self.baseline(f"rename-{i}")
                if old != "app.py":
                    self.write(self.repo / old, "kept\n")
                    self.baseline()
                self.assertIsNone(self.stop(cmd))  # the first stop records the tree
                (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
                self.git(self.repo, "mv", old, new)
                result = self.stop(cmd)
                if n is None:
                    self.assertEqual(result, self.passed())
                    self.assertTrue(self.marker.exists())
                else:
                    self.assertEqual(result, self.skipped(n))
                    self.assertFalse(self.marker.exists())

    def test_an_unchanged_docs_only_turn_is_silent_after_the_first_skip(self):
        cmd = self.touch(self.marker)
        self.baseline()
        self.change("docs/x.md")
        self.assertEqual(self.stop(cmd), self.skipped(1))
        # The next turn changes nothing (a question and its answer): no output at all.
        self.assertIsNone(self.stop(cmd))
        self.assertIsNone(self.stop(cmd))
        # New content in the same file is a change: the line again, then silence.
        self.change("docs/x.md")
        self.assertEqual(self.stop(cmd), self.skipped(1))
        self.assertIsNone(self.stop(cmd))
        # Another doc, through the shell this time.
        self.change("docs/y.md", record=False)
        self.assertEqual(self.stop(cmd), self.skipped(2))
        self.assertIsNone(self.stop(cmd))
        self.assertFalse(self.marker.exists())
        # Nothing was recorded as green along the way: a source change runs it.
        self.change("app.py")
        self.assertEqual(self.stop(cmd), self.passed())
        self.assertTrue(self.marker.exists())

    def test_patterns_fall_back_when_the_main_checkout_cannot_be_found(self):
        # git answers everything except --git-common-dir, so main_checkout fails while the
        # repo works: the project's settings.json cannot be read, and the environment and
        # then the defaults decide. The hook still exits 0 with nothing on stderr.
        cmd = self.touch(self.marker)
        own = {"env": {"HARNESS_SOURCE_EXEMPT": "assets/**,**/*.md"}}
        no_common = str(_SHARED["base"] / "bin-git-no-common") + ":" + SYSTEM_PATH
        # (environment, changed paths, files when skipped or None when it runs)
        cases = [
            ({}, ("docs/notes.txt",), 1),  # the default exempts it, the project would not
            ({}, ("assets/logo.svg",), None),  # the project exempts it, the default does not
            ({"HARNESS_SOURCE_EXEMPT": "assets/**"}, ("assets/logo.svg",), 1),
            ({"HARNESS_SOURCE_EXEMPT": "assets/**"}, ("docs/notes.txt",), None),
        ]
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        for i, (env, rels, n) in enumerate(cases):
            with self.subTest(env=env, rels=rels):
                self.baseline(f"no-main-{i}", settings=own)
                self.change(*rels)
                r = self.run_hook(
                    "verify_on_stop.py",
                    dict(payload, cwd=str(self.repo)),
                    dict(env, PATH=no_common, CLAUDE_VERIFY_CMD=cmd),
                )
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                result = json.loads(r.stdout) if r.stdout.strip() else None
                if n is None:
                    self.assertEqual(result, self.passed())
                    self.assertTrue(self.marker.exists())
                else:
                    self.assertEqual(result, self.skipped(n))
                    self.assertFalse(self.marker.exists())

    def test_empty_dirty_set_runs_the_command_as_today(self):
        self.baseline()
        readme = self.repo / "README.md"
        readme.write_text("changed through the shell\n")
        self.assertIsNone(self.stop(self.touch(self.marker)))  # records the tree
        # The tree goes clean: a change since the last stop, but git lists nothing and
        # nothing was recorded. Not a docs-only turn, so the command runs as before.
        self.git(self.repo, "checkout", "--", "README.md")
        self.assertEqual(self.stop(self.touch(self.marker)), self.passed())
        self.assertTrue(self.marker.exists())

    def test_bad_stdin_fails_open(self):
        r = self.run_hook(
            "verify_on_stop.py", "{not json", {"CLAUDE_VERIFY_CMD": "false"}
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class VerifyHelpersTests(unittest.TestCase):
    """verify_on_stop.py's settings lookup and output tail, called directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("verify_on_stop")

    def test_environment_wins_then_local_then_shared_settings(self):
        root = Path(tempfile.mkdtemp(prefix="harness-verify-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_text(
            json.dumps(
                {"env": {"CLAUDE_VERIFY_CMD": "shared", "CLAUDE_VERIFY_TIMEOUT": 90}}
            )
        )
        with mock.patch.dict(os.environ):
            for name in ("CLAUDE_VERIFY_CMD", "CLAUDE_VERIFY_TIMEOUT"):
                os.environ.pop(name, None)
            env = self.hook.settings_env(root)
            self.assertEqual(self.hook.setting("CLAUDE_VERIFY_CMD", env), "shared")
            self.assertEqual(self.hook.verify_timeout(env), 90)
            (root / ".claude/settings.local.json").write_text(
                json.dumps({"env": {"CLAUDE_VERIFY_CMD": "local"}})
            )
            env = self.hook.settings_env(root)
            self.assertEqual(self.hook.setting("CLAUDE_VERIFY_CMD", env), "local")
            os.environ["CLAUDE_VERIFY_CMD"] = "from env"
            self.assertEqual(self.hook.setting("CLAUDE_VERIFY_CMD", env), "from env")

    def test_timeout_default_and_cap(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_VERIFY_TIMEOUT", None)
            self.assertEqual(self.hook.verify_timeout({}), 300)
            for raw, expected in (
                ("45", 45),
                ("2.5", 2.5),
                ("0", 300),
                ("soon", 300),
                ("9999", 400),
            ):
                with self.subTest(raw=raw):
                    os.environ["CLAUDE_VERIFY_TIMEOUT"] = raw
                    self.assertEqual(self.hook.verify_timeout({}), expected)

    def test_tail_keeps_the_last_non_empty_lines(self):
        text = "\n".join(f"l{n}\n  " for n in range(1, 66))
        self.assertEqual(
            self.hook.tail(text).splitlines(),
            ["... (5 earlier lines omitted)"] + [f"l{n}" for n in range(6, 66)],
        )
        self.assertEqual(self.hook.tail("a\n\nb\n"), "a\nb")


# ---------------------------------------------------------------- readonly_bash.py


class ReadonlyBashTests(HookTestCase):
    DENIED = [
        "sed -i 's/a/b/' x.py",
        "echo hi > x.txt",
        "git checkout -- x",
        "rm -rf build",
    ]
    ALLOWED = [
        "git diff --cached",
        "pytest -q",
        "grep -rn foo src",
        "cat x.py | head",
        "make check",
        "ls -la",
    ]

    def guard(self, command: str, agent="harness:reviewer"):
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(self.tmp),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "Run it"},
            "tool_use_id": "toolu_01",
        }
        if agent is not None:  # present only inside a subagent (or with --agent)
            payload.update(agent_id="agent-1", agent_type=agent)
        r = self.run_hook("readonly_bash.py", payload)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return json.loads(r.stdout) if r.stdout.strip() else None

    def assertRefused(self, out, command: str, role: str = "reviewer") -> None:
        self.assertEqual(list(out), ["hookSpecificOutput"])
        decision = out["hookSpecificOutput"]
        self.assertEqual(
            [decision["hookEventName"], decision["permissionDecision"]],
            ["PreToolUse", "deny"],
        )
        reason = decision["permissionDecisionReason"]
        self.assertTrue(reason.startswith(f'Refused "{command}": '), reason)
        self.assertIn(
            f"The {role} only reads, searches, diffs and runs tests, so a hook refuses "
            "Bash commands that would change files or git state",
            reason,
        )

    def test_reviewer_is_refused_commands_that_change_things(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertRefused(self.guard(command), command)

    def test_reviewer_may_read_search_diff_and_test(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command))

    def test_only_the_read_only_agents_are_guarded(self):
        command = "echo hi > x.txt"
        for agent in (None, "harness:coder", "general-purpose"):
            with self.subTest(agent=agent):
                self.assertIsNone(self.guard(command, agent))
        for agent, role in (
            ("harness:researcher", "researcher"),
            ("researcher", "researcher"),
            ("reviewer", "reviewer"),
        ):
            with self.subTest(agent=agent):
                self.assertRefused(self.guard(command, agent), command, role)

    def test_bad_input_fails_open(self):
        for stdin in (
            "{not json",
            json.dumps({"agent_type": "harness:reviewer", "tool_name": "Bash"}),
            json.dumps({"agent_type": ["harness:reviewer"], "tool_input": {}}),
        ):
            with self.subTest(stdin=stdin):
                r = self.run_hook("readonly_bash.py", stdin)
                self.assertEqual((r.returncode, r.stdout), (0, ""))


class ReadonlyRulesTests(unittest.TestCase):
    """readonly_bash.py's command splitting and rules, called directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("readonly_bash")

    def test_every_part_of_a_command_is_checked(self):
        for command, part in (
            ("ls & rm x", "rm x"),
            (
                "cd src && grep -n x a.py; sed -i.bak 's/x/y/' a.py",
                "sed -i.bak 's/x/y/' a.py",
            ),
            ("git status || git stash", "git stash"),
            ("ls\nrm x", "rm x"),
            ('echo "$(rm x)"', "rm x"),
            ("echo `touch y`", "touch y"),
            ("(cd sub && mkdir out)", "mkdir out"),
            ("FOO=1 rm x", "FOO=1 rm x"),
            ("find . -name '*.pyc' | xargs -n 1 rm", "xargs -n 1 rm"),
            ("bash -c 'git stash'", "bash -c 'git stash'"),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.hook.refusal(command)[0], part)

    def test_rules(self):
        for command, why in (
            ("pytest -q > out.txt", "it writes to out.txt through a redirection"),
            ("echo x &> log.txt", "it writes to log.txt through a redirection"),
            ("perl -pi -e 's/a/b/' f", "perl -i edits files in place"),
            ("chmod +x run.sh", "chmod changes files"),
            ("kill 123", "kill stops processes"),
            ("find . -name '*.pyc' -delete", "find -delete deletes or writes files"),
            ("find . -name '*.pyc' -exec rm {} ;", "rm changes files"),
            ("git branch topic", "git branch can change files or git state"),
            ("git -C repo commit -m x", "git commit can change files or git state"),
            ("git diff --output=p.diff", "git diff --output writes a file"),
            ("uv add requests", "uv add changes packages"),
            ("uv pip install x", "uv pip install changes packages"),
            ("npm install", "npm install changes packages"),
            ("python3 -m pip install x", "python -m pip install changes packages"),
            (
                "python3 -c \"open('x', 'w').write('1')\"",
                "python opens a file for writing",
            ),
            (
                "python3 -c \"import pathlib; pathlib.Path('x').write_text('y')\"",
                "python writes, moves or deletes files",
            ),
            (
                "python3 - <<'EOF'\nlog = open('out.txt', mode='a')\nEOF",
                "python opens a file for writing",
            ),
            ("bash <<EOF\nrm -rf build\nEOF", "rm changes files"),
            ("make", "make without a target runs the default build"),
            ("make -C sub install", "make install is not check, test or lint"),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.hook.refusal(command)[1], why)

    def test_reading_searching_diffing_and_testing_pass(self):
        for command in (
            "pytest -q 2>&1 | tail -20",
            "grep -rn foo src 2>/dev/null",
            "echo done >&2",
            "grep -rn 'a > b' src",
            "git --no-pager log --oneline -5",
            "git -C repo show HEAD:a.py | diff - a.py",
            "git diff $(git merge-base main HEAD)",
            "diff <(git show HEAD:a.py) a.py",
            "git branch -a",
            "git stash list",
            "uv run pytest -q",
            "npm test",
            "make -j 4 test lint",
            "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests",
            "python3 -c \"print(open('a.json').read())\"",
            "python3 - <<'EOF'\nimport json\nprint(json.load(open('x.json')))\nEOF",
            "cat <<'EOF'\nrm -rf build\nEOF",
            "ls  # rm build",
            "echo $((1 > 0))",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.hook.refusal(command))


if __name__ == "__main__":
    unittest.main()
