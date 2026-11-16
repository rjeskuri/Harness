"""Tests for the v2 additions to session_brief.py: the ticket lines on a t/<N>-... branch
(from the ticket cache and red record only), the board line from gh, the budget line, and
the backup sweep at SessionStart (ticket #329).

The hook runs as a subprocess, fed SessionStart JSON, under a temporary HOME, in
temporary git repos made with `git init -b main`. gh is fakegh.py copied onto a private
PATH as `gh`, driven by a scenario file and logging every call; nothing touches the
network or ~/.claude. The sweep tests run the hook the way hooks.json wires it (its
command and args, ${CLAUDE_PLUGIN_ROOT} filled in, its timeout); the snapshots are folders
written in backups.py's layout with an id of the age wanted, the refs made with
`git update-ref`. A git that hangs on update-ref alone (the reads answer), with many old
refs to delete, stands in for a sweep that overruns: a timeout per call adds up, a
deadline does not. A ref under another git's lock, a store that is a file and a read-only
store stand in for a sweep that fails part way: the rest is still swept. The brief is
compared whole, its last commit dated long ago so that its age reads the same every run.
Rulings on #329: the sweep runs only where the brief runs (a project whose own
SessionStart hook replaces the brief is not tested here).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_session_brief_v2.py" -v
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
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

PLUGIN_ROOT = TESTS.parent
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
DAY = dt.timedelta(days=1)
LONG_AGO = "2015-06-15T12:00:00+0000"  # `last commit 11 years ago` on every run
SWEEP_BUDGET = 2  # seconds the brief gives the sweep (ticket #329)
TIMING_MARGIN = 3  # seconds on top, for a loaded CI machine
HANGING_REFS = 64  # old refs: a timeout per deletion, even 0.1 s, adds up past the margin
REAL_GIT = shutil.which("git", path=SYSTEM_PATH) or "/usr/bin/git"
# The real git for every call but update-ref (the sweep's deletions), which is logged and hangs:
# the sweep's reads (rev-parse, for-each-ref, worktree list) answer, so it has refs to delete.
HANGING_GIT = """#!/bin/sh
sub=""
skip=""
for a in "$@"; do
  if [ -n "$skip" ]; then skip=""; continue; fi
  case "$a" in
    -C|-c) skip=1 ;;
    -*) ;;
    *) sub="$a"; break ;;
  esac
done
if [ "$sub" = "update-ref" ]; then
  if [ -n "$HANG_LOG" ]; then echo "$*" >> "$HANG_LOG"; fi
  exec sleep 20
fi
exec "@GIT@" "$@"
"""

_SHARED: dict = {}


def setUpModule() -> None:
    """One copy of each fake gh and git for every test: a new executable costs time on macOS."""
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
    (base / "bin-hang").mkdir()
    git = base / "bin-hang" / "git"
    git.write_text(HANGING_GIT.replace("@GIT@", REAL_GIT))
    git.chmod(0o755)


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

    # ---- the backup sweep (#329)

    def session_start(
        self, cwd: Path, source: str = "startup", env: dict | None = None
    ) -> tuple[dict, float]:
        """The SessionStart hook run as hooks.json wires it for `source`, killed at its timeout;
        (hookSpecificOutput, seconds it took). It must exit 0 and print nothing on stderr."""
        config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        hooks = [
            hook
            for group in config["hooks"]["SessionStart"]
            if re.fullmatch(group.get("matcher") or ".*", source)
            for hook in group["hooks"]
            if any("session_brief.py" in arg for arg in hook.get("args", []))
        ]
        self.assertEqual(len(hooks), 1, f"hooks.json runs session_brief.py once on {source}")
        hook = hooks[0]
        argv = [hook["command"]] + [
            arg.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)) for arg in hook["args"]
        ]
        if argv[0] == "python3":
            argv[0] = sys.executable  # the suite's python3 stands in for the one on PATH
        payload = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": source,
        }
        run_env = dict(
            self.env, CLAUDE_PLUGIN_ROOT=str(PLUGIN_ROOT), CLAUDE_PROJECT_DIR=str(cwd)
        )
        run_env.update(env or {})
        started = time.monotonic()
        try:
            r = subprocess.run(
                argv,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=run_env,
                cwd=str(self.tmp),
                timeout=hook["timeout"],
            )
        except subprocess.TimeoutExpired:
            self.fail(f"the hook ran past the {hook['timeout']} s hooks.json gives it")
        took = time.monotonic() - started
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        return out, took

    def global_store(self) -> Path:
        return self.home / ".claude/harness/backups"

    @staticmethod
    def project_store(repo: Path) -> Path:
        return repo / ".git/harness-backups"

    @staticmethod
    def backup_id(when: dt.datetime) -> str:
        return f"{when.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(3).hex()}"

    def snapshot(self, store: Path, age: dt.timedelta, repo: Path | None) -> str:
        """A snapshot folder in backups.py's layout whose id is `age` old; its id."""
        when = dt.datetime.now(dt.timezone.utc) - age
        sid = self.backup_id(when)
        store.mkdir(parents=True, exist_ok=True)
        store.chmod(0o700)
        folder = store / sid
        (folder / "files").mkdir(mode=0o700, parents=True)
        (folder / "files" / "0").write_bytes(os.urandom(64))
        where = str(repo or self.tmp)
        manifest = {
            "id": sid,
            "at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo": str(repo) if repo else None,
            "cwd": where,
            "command": "rm -rf build",
            "session": "sess-0",
            "sensitive": False,
            "items": [
                {"path": f"{where}/build", "copy": "files/0", "kind": "file", "bytes": 64}
            ],
            "refs": [],
        }
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return sid

    def backup_ref(self, repo: Path, age: dt.timedelta) -> str:
        when = dt.datetime.now(dt.timezone.utc) - age
        name = f"refs/harness-backup/{self.backup_id(when)}/0"
        self.git(repo, "update-ref", name, "HEAD")
        return name

    def backup_refs(self, repo: Path, count: int, age: dt.timedelta) -> list[str]:
        """`count` refs under refs/harness-backup/, each id about `age` old, made in one git call."""
        sha = self.git(repo, "rev-parse", "HEAD")
        when = dt.datetime.now(dt.timezone.utc) - age
        names = [
            f"refs/harness-backup/{self.backup_id(when - dt.timedelta(seconds=k))}/0"
            for k in range(count)
        ]
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "--stdin"],
            input="".join(f"create {name} {sha}\n" for name in names),
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return names

    def refs(self, repo: Path) -> set[str]:
        return set(self.git(repo, "for-each-ref", "--format=%(refname)").splitlines())

    @staticmethod
    def listing(store: Path) -> set[str]:
        return set(os.listdir(store)) if store.is_dir() else set()

    def pin_commit_date(self, repo: Path) -> None:
        """The last commit dated long ago, so the heading's commit age reads the same every run."""
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--amend", "--no-edit", "-q"],
            check=True,
            capture_output=True,
            env=dict(self.env, GIT_AUTHOR_DATE=LONG_AGO, GIT_COMMITTER_DATE=LONG_AGO),
        )

    def test_session_start_sweeps_old_backups(self):
        other = self.make_repo("other")
        vault = self.make_repo("vault")  # a sensitive project: its snapshots stay in it
        self.write(
            vault / ".claude/settings.json",
            json.dumps({"env": {"HARNESS_SENSITIVE_PATHS": "secrets/**"}}),
        )
        self.git(vault, "add", ".claude/settings.json")
        self.git(vault, "commit", "-q", "-m", "Mark secrets")
        wt = self.tmp / "vault-wt"
        self.git(vault, "worktree", "add", "-q", "-b", "t/9-vault-work", str(wt))
        g, v = self.global_store(), self.project_store(vault)
        g_new = {
            self.snapshot(g, 13 * DAY, self.repo),
            self.snapshot(g, 13 * DAY, other),
        }
        for repo in (self.repo, other, None):  # every project's, in the global store
            self.snapshot(g, 15 * DAY, repo)
        v_old, v_new = self.snapshot(v, 15 * DAY, vault), self.snapshot(v, 13 * DAY, vault)
        mine_old = self.backup_ref(self.repo, 15 * DAY)
        self.backup_ref(self.repo, 13 * DAY)
        vault_old = self.backup_ref(vault, 15 * DAY)
        self.backup_ref(vault, 13 * DAY)
        self.backup_ref(other, 15 * DAY)
        self.backup_ref(other, 13 * DAY)
        mine, vaults, others = self.refs(self.repo), self.refs(vault), self.refs(other)

        # A new session in a plain project: the global store and this repository's refs.
        self.session_start(self.repo, "startup")
        self.assertEqual(self.listing(g), g_new)
        self.assertEqual(self.refs(self.repo), mine - {mine_old})
        self.assertEqual(self.listing(v), {v_old, v_new}, "another project's store was swept")
        self.assertEqual(self.refs(vault), vaults, "another repository's refs were swept")
        self.assertEqual(self.refs(other), others, "another repository's refs were swept")

        # A resumed session in the sensitive project's worktree: its own store and refs.
        self.session_start(wt, "resume")
        self.assertEqual(self.listing(v), {v_new})
        self.assertEqual(self.refs(vault), vaults - {vault_old})
        self.assertEqual(self.refs(other), others)
        self.assertEqual(self.refs(self.repo), mine - {mine_old})

        # Outside any repository the global store is swept all the same.
        loose = self.tmp / "loose"
        loose.mkdir()
        self.snapshot(g, 15 * DAY, None)
        self.session_start(loose, "compact")
        self.assertEqual(self.listing(g), g_new)
        self.assertEqual(self.listing(v), {v_new})
        self.assertEqual(self.refs(other), others)

    def test_brief_unchanged_by_the_sweep(self):
        self.pin_commit_date(self.repo)
        self.write(
            self.repo / "docs/agent/handoff.md",
            "# Handoff\n\n## Next steps\n- Finish the sweep\n- Land it\n",
        )
        self.budgets({"api": {"exhausted_until": "2999-01-01T00:00:00Z", "reason": "credits"}})
        clean, _ = self.session_start(self.repo)
        self.assertGreater(len(clean["additionalContext"].splitlines()), 2)
        g = self.global_store()
        self.snapshot(g, 15 * DAY, self.repo)
        new = self.snapshot(g, 13 * DAY, self.repo)
        old_ref = self.backup_ref(self.repo, 15 * DAY)
        new_ref = self.backup_ref(self.repo, 13 * DAY)

        out, _ = self.session_start(self.repo)
        self.assertEqual(out, clean)
        self.assertEqual(self.listing(g), {new}, "the sweep did not run")
        self.assertNotIn(old_ref, self.refs(self.repo), "the sweep did not run")
        self.assertIn(new_ref, self.refs(self.repo))
        self.assertEqual(self.session_start(self.repo)[0], clean)  # nothing left to sweep

        # A sweep that fails part way adds no line and still sweeps the rest: a ref git cannot
        # delete (another git holds its lock), between two old refs, beside an old snapshot,
        before = self.backup_ref(self.repo, 25 * DAY)  # ids sort by time: before, locked, after
        locked = self.backup_ref(self.repo, 20 * DAY)
        after = self.backup_ref(self.repo, 16 * DAY)
        stale = self.snapshot(g, 16 * DAY, self.repo)
        lock = self.repo / ".git" / (locked + ".lock")
        lock.write_text("")
        self.assertEqual(self.session_start(self.repo)[0], clean)
        self.assertIn(locked, self.refs(self.repo))
        self.assertEqual({before, after} & self.refs(self.repo), set(), "a locked ref stopped the rest")
        self.assertNotIn(stale, self.listing(g), "a locked ref stopped the store's sweep")
        lock.unlink()
        # a store that is not a folder (the refs still go),
        shutil.rmtree(g)
        g.write_text("not a folder\n")
        old = self.backup_ref(self.repo, 18 * DAY)
        self.assertEqual(self.session_start(self.repo)[0], clean)
        self.assertEqual(g.read_text(), "not a folder\n")
        self.assertNotIn(old, self.refs(self.repo), "a store that is a file stopped the refs' sweep")
        g.unlink()
        # an old snapshot that cannot be removed (permissions do not bind root; the refs still go).
        if os.geteuid() != 0:
            self.snapshot(g, 20 * DAY, self.repo)
            g.chmod(0o500)
            self.addCleanup(g.chmod, 0o700)
            old = self.backup_ref(self.repo, 18 * DAY)
            self.assertEqual(self.session_start(self.repo)[0], clean)
            self.assertNotIn(old, self.refs(self.repo), "a read-only store stopped the refs' sweep")

    def test_a_hanging_sweep_adds_no_delay_past_its_budget(self):
        self.pin_commit_date(self.repo)
        clean, took = self.session_start(self.repo)
        again, took_again = self.session_start(self.repo)
        self.assertEqual(again, clean)
        took = max(took, took_again)  # nothing to sweep: the brief's own time, twice
        # Many old refs, and a git whose every read answers but whose update-ref hangs.
        self.backup_refs(self.repo, HANGING_REFS, 15 * DAY)
        self.snapshot(self.global_store(), 15 * DAY, self.repo)
        hang_log = self.tmp / "hang.log"
        hang = {
            "PATH": f"{_SHARED['base'] / 'bin-hang'}:{_SHARED['base'] / 'bin-fake'}:{SYSTEM_PATH}",
            "HANG_LOG": str(hang_log),
        }
        out, hung = self.session_start(self.repo, env=hang)
        self.assertEqual(out, clean)
        self.assertTrue(
            hang_log.is_file() and hang_log.read_text().strip(),
            "the sweep tried to delete no ref",
        )
        self.assertLess(
            hung,
            took + SWEEP_BUDGET + TIMING_MARGIN,
            f"a hanging sweep held the brief {hung:.1f} s (a normal run took {took:.1f} s)",
        )


if __name__ == "__main__":
    unittest.main()
