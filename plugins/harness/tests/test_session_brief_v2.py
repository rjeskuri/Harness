"""Tests for the v2 additions to session_brief.py: the ticket lines on a t/<N>-... branch
(from the ticket cache and red record only), the board line from gh, and the budget line.

The hook runs as a subprocess, fed SessionStart JSON, under a temporary HOME, in
temporary git repos made with `git init -b main`. gh is fakegh.py copied onto a private
PATH as `gh`, driven by a scenario file and logging every call; nothing touches the
network or ~/.claude.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_session_brief_v2.py" -v
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
HOOK = TESTS.parent / "hooks" / "session_brief.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
BRANCH = "t/12-fix-login"
DOD = [f"Criterion {n} holds" for n in range(1, 9)]
ISSUES = [
    {"labels": [{"name": "task"}, {"name": "status:ready"}]},
    {"labels": [{"name": "bug"}, {"name": "status:ready"}, {"name": "priority:high"}]},
    {"labels": [{"name": "epic"}, {"name": "status:ready"}]},
    {"labels": [{"name": "task"}, {"name": "status:in-progress"}]},
    {"labels": [{"name": "task"}, {"name": "status:needs-input"}]},
    {"labels": [{"name": "task"}, {"name": "status:in-review"}]},
    {"labels": []},
]
PRS = [
    {"isDraft": False, "author": {"login": "rjeskuri", "is_bot": False}},
    {"isDraft": True, "author": {"login": "rjeskuri", "is_bot": False}},
    {"isDraft": False, "author": {"login": "app/dependabot", "is_bot": True}},
]
BOARD = "Board: 2 ready, 1 in progress, 1 awaiting your review, 1 needs input"

_SHARED: dict = {}


def setUpModule() -> None:
    """One copy of each fake gh for every test: a new executable costs time on macOS."""
    base = Path(tempfile.mkdtemp(prefix="harness-brief2-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")
    for folder, source in (
        ("bin-fake", None),
        ("bin-slow", "#!/bin/sh\nexec sleep 30\n"),
    ):
        (base / folder).mkdir()
        gh = base / folder / "gh"
        if source is None:
            shutil.copyfile(TESTS / "fakegh.py", gh)
        else:
            gh.write_text(source)
        gh.chmod(0o755)
    (base / "bin-none").mkdir()


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


class BriefV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-brief2-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        self.log = self.tmp / "gh.log"
        self.scenario = self.tmp / "scenario.json"
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=f"{_SHARED['base'] / 'bin-fake'}:{SYSTEM_PATH}",
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
            FAKEGH_SCENARIO=str(self.scenario),
            FAKEGH_LOG=str(self.log),
        )
        self.gh_scenario(ISSUES, PRS)
        self.repo = self.make_repo()
        key = hashlib.sha1(str(self.repo).encode()).hexdigest()[:12]
        self.state = self.home / ".claude/harness/repos" / key

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

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_repo(self, name: str = "proj") -> Path:
        repo = self.tmp / name
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
            env=self.env,
        )
        self.write(repo / "README.md", "hello\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def gh_scenario(self, issues, prs, exit_code: int = 0) -> None:
        calls = [
            {"match": ["issue", "list"], "stdout_json": issues, "exit": exit_code},
            {"match": ["pr", "list"], "stdout_json": prs, "exit": exit_code},
        ]
        self.scenario.write_text(json.dumps({"calls": calls}))

    def gh_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(ln)["argv"] for ln in self.log.read_text().splitlines()]

    def ticket(self, **fields) -> None:
        ticket = {
            "number": 12,
            "title": "Fix login",
            "type": "task",
            "status": "in-progress",
            "tier": "opus",
            "flags": [],
            "labels": ["task", "status:in-progress", "size:M"],
            "sections": {"definition_of_done": DOD, "tests": [], "request": "r"},
        }
        ticket.update(fields)
        self.write(self.state / "tickets/12.json", json.dumps(ticket))

    def red(self, kind: str = "assertion") -> None:
        record = {
            "branch": BRANCH,
            "test_files": ["tests/test_app.py"],
            "test_hash": "0" * 64,
            "exit": 1,
            "kind": kind,
            "at": "2026-10-13T10:00:00Z",
        }
        self.write(self.state / "red/t-12-fix-login.json", json.dumps(record))

    def budgets(self, data: dict) -> None:
        self.write(self.home / ".claude/harness/budgets.json", json.dumps(data))

    # ---- running the hook

    def lines(self, cwd: Path, env: dict | None = None) -> list[str]:
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            cwd=str(self.tmp),
            timeout=60,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        return out["additionalContext"].splitlines()

    # ---- ticket lines

    def test_ticket_lines_from_the_cache_in_a_worktree(self):
        wt = self.tmp / "wt"
        self.git(self.repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        self.ticket()
        self.red()
        lines = self.lines(wt)
        self.assertRegex(lines[0], rf"^## Session brief: wt on {BRANCH}, ")
        self.assertEqual(
            lines[1:9],
            [
                "Ticket #12: Fix login (in-progress, tier opus)",
                *(f"- {d}" for d in DOD[:6]),
                "Red run: assertion at 2026-10-13T10:00:00Z",
            ],
        )
        self.assertEqual(len(lines), 9)
        self.assertEqual(self.gh_calls(), [])  # no network on a ticket branch

    def test_ticket_lines_in_the_main_checkout_and_without_a_red_run(self):
        self.git(self.repo, "switch", "-q", "-c", BRANCH)
        dod = ["- [ ] Login works", "- [x] Logout works", "[ ] Third"]
        self.ticket(status=None, sections={"definition_of_done": dod})
        self.assertEqual(
            self.lines(self.repo)[1:],
            [
                "Ticket #12: Fix login (status unknown, tier opus)",
                "- [ ] Login works",
                "- [x] Logout works",
                "- [ ] Third",
                "Red run: none",
            ],
        )

    def test_ticket_branch_without_a_cache(self):
        self.git(self.repo, "switch", "-q", "-c", BRANCH)
        self.red("green")
        self.assertEqual(
            self.lines(self.repo)[1:],
            [
                "Ticket #12: not cached; harness-board show 12 fetches it.",
                "Red run: green at 2026-10-13T10:00:00Z",
            ],
        )

    # ---- board line

    def test_board_line_from_gh(self):
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        lines = self.lines(self.repo)
        self.assertEqual(lines[1:], [BOARD])
        self.assertEqual(
            sorted(self.gh_calls()),
            [
                ["issue", "list", "--json", "labels", "--limit", "200"],
                ["pr", "list", "--json", "isDraft,author", "--limit", "50"],
            ],
        )

    def test_board_line_through_harness_gh(self):
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        env = {
            "PATH": f"{_SHARED['base'] / 'bin-none'}:{SYSTEM_PATH}",
            "HARNESS_GH": str(_SHARED["base"] / "bin-fake" / "gh"),
        }
        self.assertEqual(self.lines(self.repo, env)[1:], [BOARD])

    def test_no_board_line_without_origin_gh_or_an_answer(self):
        self.assertEqual(self.lines(self.repo)[1:], [])  # no origin
        self.assertEqual(self.gh_calls(), [])
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        no_gh = {"PATH": f"{_SHARED['base'] / 'bin-none'}:{SYSTEM_PATH}"}
        self.assertEqual(self.lines(self.repo, no_gh)[1:], [])
        self.gh_scenario(ISSUES, PRS, exit_code=1)  # gh fails, e.g. not logged in
        self.assertEqual(self.lines(self.repo)[1:], [])
        self.scenario.write_text(json.dumps({"default": {"stdout": "not json"}}))
        self.assertEqual(self.lines(self.repo)[1:], [])

    def test_no_board_line_without_a_github_origin(self):
        bare = self.tmp / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)],
            check=True,
            capture_output=True,
            env=self.env,
        )
        self.git(self.repo, "remote", "add", "origin", str(bare))
        for url in (str(bare), "https://gitlab.com/o/r.git"):
            self.git(self.repo, "remote", "set-url", "origin", url)
            self.gh_scenario(ISSUES, PRS)  # gh would answer
            self.assertEqual(self.lines(self.repo)[1:], [])
            self.gh_scenario(ISSUES, PRS, exit_code=4)  # gh would fail, e.g. no token
            self.assertEqual(self.lines(self.repo)[1:], [])
        self.assertEqual(self.gh_calls(), [])  # gh never runs

    def test_slow_gh_is_cut_off_after_three_seconds(self):
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        slow = {"PATH": f"{_SHARED['base'] / 'bin-slow'}:{SYSTEM_PATH}"}
        started = time.monotonic()
        lines = self.lines(self.repo, slow)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(lines[1:], [])

    # ---- budget line

    def test_budget_line_for_an_exhausted_budget(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=20)).replace(
            microsecond=0
        )
        earlier = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.budgets(
            {
                "api": {
                    "exhausted_until": later.isoformat(),
                    "reason": "credit balance",
                },
                "cloud": {"exhausted_until": earlier.isoformat(), "reason": "old"},
            }
        )
        self.assertEqual(
            self.lines(self.repo)[1:],
            [f"Budget: api exhausted until {later.isoformat()} (credit balance)"],
        )
        self.budgets(
            {
                "api": {"exhausted_until": None, "reason": ""},
                "cloud": {"exhausted_until": "2999-01-01T00:00:00Z", "reason": ""},
            }
        )
        self.assertEqual(
            self.lines(self.repo)[1:],
            ["Budget: cloud exhausted until 2999-01-01T00:00:00Z"],
        )
        self.budgets({"api": {"exhausted_until": "soon"}})
        self.assertEqual(self.lines(self.repo)[1:], [])

    # ---- the cap

    def test_a_full_brief_stays_within_forty_lines(self):
        wt = self.tmp / "wt"
        self.git(self.repo, "worktree", "add", "-q", "-b", BRANCH, str(wt))
        self.ticket()
        self.red()
        self.write(
            wt / "docs/agent/handoff.md",
            "# Handoff\n" + "".join(f"- note {n}\n" for n in range(30)),
        )
        self.write(wt / "docs/agent/specs/2026-10-13-v2.md", "spec\n")
        features = [{"description": f"F{n}", "passes": False} for n in range(5)]
        self.write(wt / "docs/plans/feature_list.json", json.dumps(features))
        today = dt.date.today().isoformat()
        self.write(wt / "worklog/worklog.csv", f"date,hours\n{today},1.5\n")
        self.budgets(
            {
                "api": {"exhausted_until": "2999-01-01T00:00:00Z", "reason": "credits"},
                "cloud": {
                    "exhausted_until": "2999-01-01T00:00:00Z",
                    "reason": "credits",
                },
            }
        )
        lines = self.lines(wt)
        self.assertLessEqual(len(lines), 40)
        self.assertEqual(lines[1], "Ticket #12: Fix login (in-progress, tier opus)")
        self.assertIn("Red run: assertion at 2026-10-13T10:00:00Z", lines)
        self.assertIn(
            "Budget: api exhausted until 2999-01-01T00:00:00Z (credits)", lines
        )
        self.assertEqual(lines[-1], "Hours this month: 1.50")
        # many budget entries: the cap still holds, the heading and ticket come first
        self.budgets(
            {
                f"b{n}": {"exhausted_until": "2999-01-01T00:00:00Z", "reason": "x"}
                for n in range(40)
            }
        )
        lines = self.lines(wt)
        self.assertEqual(len(lines), 40)
        self.assertTrue(lines[0].startswith("## Session brief: wt on t/12-fix-login"))
        self.assertEqual(lines[1], "Ticket #12: Fix login (in-progress, tier opus)")


if __name__ == "__main__":
    unittest.main()
