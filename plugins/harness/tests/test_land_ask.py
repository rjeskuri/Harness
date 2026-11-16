"""Tests for hooks/land_ask.py (ticket #330): the PreToolUse Bash hook that turns `harness-pr land P` into a
prompt (permissionDecision "ask") unless every ticket the PR closes is labeled risk:low.

The hook runs as a subprocess, the way Claude Code runs it: PreToolUse JSON on stdin, a temporary HOME, its own
working directory outside the repos, and temporary git repos made with `git init -b main`. gh is HARNESS_GH, a
small wrapper written once per module: it logs every call (argv and the directory it ran in), slows each gh
subcommand as the scenario's pace says, and answers as fakegh.py does from the scenario of the directory it runs
in. Any
directory without a scenario of its own (a linked worktree, a subdirectory, the hook's working directory) gets
a decoy in which every PR closes a risk:low ticket, so a hook that runs gh anywhere but the main checkout stays
silent where it must ask. Nothing touches the network or ~/.claude.

The contract these tests read from the ticket:
  - ask, the reason naming the ticket and risk:high, for a PR closing a risk:high ticket; nothing at all (no
    stdout, no stderr, exit 0) when every ticket is risk:low;
  - ask, each with its own reason, for a ticket without a risk label, a PR with no ticket (it closes nothing and
    its head is not t/<N>-), a failing gh, a gh slower than 3 s and gh calls past 8 s in all (inside the 10 s
    hook timeout); the 8 s are one deadline for the whole command, however many lands it holds; the ticket
    comes from a t/<N>- head only when the PR closes nothing;
  - ask, naming the flag, with no gh call, for --accept-findings or --discard;
  - every `harness-pr land P` in a command is judged, each in the main checkout of its own directory (a cd
    carries over to the parts after it, as in the shell, and a cd inside a ( ... ) subshell ends with it): one
    land that asks makes the command ask, in any position, and an override flag in any part asks with no gh
    call; harness-pr by its path ($CLAUDE_PLUGIN_ROOT/bin/harness-pr, an absolute path) is the same command;
  - `cd <dir> && harness-pr land P` is judged in the main checkout of <dir>; `harness-pr ready P`,
    `harness-pr land --help`, `harness-pr land` without a number and commands without harness-pr print nothing
    and call no gh;
  - without its lib, or on an error of its own: one stderr line, exit 0, no decision;
  - hooks.json runs it before every Bash call, with 10 s.

Fixtures live in this module, not in a sibling support module (harness-red's default command cannot import one,
#346).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_land_ask.py" -v
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
NAME = "land_ask.py"
HOOK = HOOKS / NAME
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
HOOK_TIMEOUT = 10  # what hooks.json gives the hook
# A gh that hangs is cut off after 3 s; 6 s leaves room for a loaded machine, not for an 8 s call.
ONE_CALL_LIMIT = 6.0
# gh calls share 8 s in all: the hook answers inside the 10 s Claude Code gives it.
ALL_CALLS_LIMIT = 9.5
# Slow gh calls that are each still in time: the fake's own start adds about 0.03 s a call on an idle Mac and up to
# 0.8 s on a loaded one, so none of these comes near the 3 s cut-off.
CALL_DELAY = 1.5  # every call
ISSUE_DELAY = 1.8  # issue views only, the PR's view at once
PR_DELAY = 2.0  # the PR's view only, the issue views at once
IN_FLIGHT_DELAY = 2.4  # every call: the fourth starts inside 8 s and would end well past it
HANG = 30  # a gh that never answers in time

# The fake gh. It logs the call and the directory it ran in, then answers as fakegh.py does from the scenario
# LANDASK_DIRS maps this directory to ("*" for any other). A scenario's "pace" ({"pr": 2.0, "issue": 30}: seconds
# for each gh subcommand, by its first argument) slows it: each call waits its turn (calls made at once queue, so
# the total is the same however the hook makes them), then sleeps its subcommand's seconds. The pace sits in the
# scenario file, beside the answers, and nowhere in the hook's environment: the hook cannot read the timing it is
# judged on.
WRAPPER = r'''#!@PYTHON@
import json
import os
import runpy
import sys
import time

FAKEGH = @FAKEGH@
cwd = os.path.realpath(os.getcwd())
with open(os.environ["LANDASK_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": cwd}) + "\n")
with open(os.environ["LANDASK_DIRS"], encoding="utf-8") as fh:
    dirs = json.load(fh)
scenario = dirs.get(cwd, dirs["*"])
with open(scenario, encoding="utf-8") as fh:
    pace = json.load(fh).get("pace")
if pace:
    import fcntl
    turn = open(scenario + ".lock", "a")  # held until this gh exits or is killed
    fcntl.flock(turn, fcntl.LOCK_EX)
    time.sleep(float(pace.get(sys.argv[1] if len(sys.argv) > 1 else "", 0)))
os.environ["FAKEGH_SCENARIO"] = scenario
os.environ.pop("FAKEGH_LOG", None)
sys.argv[0] = FAKEGH
runpy.run_path(FAKEGH, run_name="__main__")
'''

# Every PR closes #1, and every issue is risk:low: what gh answers outside a directory with a scenario.
DECOY = {
    "calls": [],
    "default": {
        "stdout_json": {
            "closingIssuesReferences": [{"number": 1}],
            "headRefName": "t/1-decoy",
            "labels": [{"name": "risk:low"}],
        }
    },
}

_SHARED: dict = {}


def plugin_copy(root: Path, with_lib: bool = True) -> Path:
    """hooks/ (and lib/) copied under `root`; the hook's lib is the copy's."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(HOOKS, root / "hooks", ignore=ignore)
    if with_lib:
        shutil.copytree(PLUGIN / "lib", root / "lib", ignore=ignore)
    return root


def setUpModule() -> None:
    """The fake gh and the broken plugin copies, once for every test: a new executable costs time on macOS."""
    base = Path(tempfile.mkdtemp(prefix="harness-landask-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")
    gh = base / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        WRAPPER.replace("@PYTHON@", sys.executable).replace("@FAKEGH@", repr(str(TESTS / "fakegh.py"))),
        encoding="utf-8",
    )
    gh.chmod(0o755)
    _SHARED["gh"] = gh

    token = uuid.uuid4().hex[:10]
    _SHARED["token"] = token
    # No lib at all: harness_lib cannot be imported.
    _SHARED["no lib"] = plugin_copy(base / "no-lib", with_lib=False)
    # A lib that raises on import, with line breaks in the message.
    broken = plugin_copy(base / "broken-lib")
    message = f"first-{token} line\nsecond-{token}\tline\r\n"
    (broken / "lib" / "harness_lib" / "__init__.py").write_text(f"raise RuntimeError({message!r})\n", encoding="utf-8")
    _SHARED["broken lib"] = broken
    _SHARED["broken message"] = " ".join(message.split())
    # The hook's own error: the shell walker it splits commands with raises when called.
    own = plugin_copy(base / "own-error")
    with open(own / "lib" / "harness_lib" / "shellwalk.py", "a", encoding="utf-8") as fh:
        fh.write(
            "\n\ndef split_command(*args, **kwargs):\n"
            f"    raise RuntimeError('walker broke {token}')\n"
            "\n\ndef command_words(*args, **kwargs):\n"
            f"    raise RuntimeError('walker broke {token}')\n"
        )
    _SHARED["own error"] = own


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def names_number(text: str, number: int) -> bool:
    """`number` appears in `text` as a whole number: 40 is not found in 140 or 401."""
    return re.search(rf"(?<!\d){number}(?!\d)", text) is not None


def pr_call(number: int, closing=(), head: str = "feature/login") -> dict:
    """gh pr view `number` answering the issues it closes and its head branch, in gh's own shape."""
    refs = [
        {
            "id": f"I_kwDO{n:04d}",
            "number": n,
            "url": f"https://github.com/o/proj/issues/{n}",
            "repository": {"id": "R_kgDO1", "name": "proj", "owner": {"id": "U_1", "login": "o"}},
        }
        for n in closing
    ]
    return {"contains": ["pr", "view", str(number)], "stdout_json": {"closingIssuesReferences": refs,
                                                                     "headRefName": head}}


def issue_call(number: int, *labels: str) -> dict:
    """gh issue view `number` answering its labels, in gh's own shape."""
    return {
        "contains": ["issue", "view", str(number)],
        "stdout_json": {
            "labels": [
                {"id": f"LA_{number}_{i}", "name": name, "description": "", "color": "ededed"}
                for i, name in enumerate(labels)
            ]
        },
    }


class LandAskTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-landask-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        self.outside = self.tmp / "outside"  # the hook process's own working directory: no repo
        self.outside.mkdir()
        self.log = self.tmp / "gh.log"
        self.dirs_file = self.tmp / "gh-dirs.json"
        self.scenarios = 0
        decoy = self.tmp / "scenario-decoy.json"
        decoy.write_text(json.dumps(DECOY), encoding="utf-8")
        self.dirs = {"*": str(decoy)}
        self.save_dirs()
        # Built from scratch, so nothing leaks in from a Claude Code session running the tests.
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
            HARNESS_GH=str(_SHARED["gh"]),
            LANDASK_LOG=str(self.log),
            LANDASK_DIRS=str(self.dirs_file),
        )
        self.repo = self.make_repo("proj")

    # ---- fixtures

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                             env=self.env)
        return out.stdout.strip()

    def make_repo(self, name: str) -> Path:
        repo = self.tmp / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True,
                       env=self.env)
        (repo / "src").mkdir()
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        (repo / "src" / "app.py").write_text("def login():\n    return None\n", encoding="utf-8")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        self.git(repo, "remote", "add", "origin", f"https://github.com/o/{name}.git")
        return repo

    def save_dirs(self) -> None:
        self.dirs_file.write_text(json.dumps(self.dirs), encoding="utf-8")

    def scenario(self, *calls: dict, where: Path | None = None, pace: dict | None = None) -> None:
        """gh run in `where` (the repo by default) answers `calls`, one at a time and slowed by `pace` (seconds per
        subcommand) when it is given; anything else there fails as gh does."""
        self.scenarios += 1
        path = self.tmp / f"scenario-{self.scenarios}.json"
        path.write_text(json.dumps(dict({"calls": list(calls)}, **({"pace": pace} if pace else {}))),
                        encoding="utf-8")
        self.dirs[os.path.realpath(str(where or self.repo))] = str(path)
        self.save_dirs()

    def gh_calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def pr_views(self) -> set:
        """(PR, directory) of every gh pr view the last hook run made."""
        return {
            (int(arg), call["cwd"])
            for call in self.gh_calls()
            if "pr" in call["argv"] and "view" in call["argv"]
            for arg in call["argv"]
            if arg.isdigit()
        }

    # ---- running the hook

    def event(self, command: str, cwd: Path | None = None) -> dict:
        return {
            "session_id": "s1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd or self.repo),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_use_id": "toolu_01",
        }

    def run_hook(self, command: str, cwd: Path | None = None, env: dict | None = None, hook: Path = HOOK):
        """(exit, stdout, stderr, seconds) of the hook judging `command` run in `cwd`; the gh log starts empty."""
        if self.log.exists():
            self.log.unlink()
        started = time.monotonic()
        r = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(self.event(command, cwd)),
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            cwd=str(self.outside),
            timeout=60,
        )
        return r.returncode, r.stdout, r.stderr, time.monotonic() - started

    def ask(self, command: str, cwd: Path | None = None, env: dict | None = None) -> str:
        """The reason of the ask the hook must answer `command` with."""
        code, out, err, _ = self.run_hook(command, cwd, env)
        return self.ask_reason(command, code, out, err)

    def ask_reason(self, command: str, code: int, out: str, err: str) -> str:
        self.assertEqual((code, err), (0, ""), f"{command!r}: stdout {out!r}")
        self.assertTrue(out.strip(), f"{command!r}: no decision, an ask was due")
        data = json.loads(out)
        self.assertEqual(list(data), ["hookSpecificOutput"], out)
        decision = data["hookSpecificOutput"]
        self.assertEqual(decision.get("hookEventName"), "PreToolUse", out)
        self.assertEqual(decision.get("permissionDecision"), "ask", f"{command!r}: {out}")
        reason = decision.get("permissionDecisionReason")
        self.assertIsInstance(reason, str, out)
        self.assertTrue(reason.strip(), out)
        return reason

    def assert_silent(self, command: str, cwd: Path | None = None, env: dict | None = None) -> None:
        code, out, err, _ = self.run_hook(command, cwd, env)
        self.assertEqual((code, out, err), (0, "", ""), f"{command!r} must get no output")

    # ---- risk:high asks, risk:low passes

    def test_risk_high_ticket_asks(self):
        cases = [
            # (PR, closing tickets with their labels, command, the risk:high ticket the reason names)
            (12, {40: ("task", "risk:high", "size:M")}, "harness-pr land 12", 40),
            (12, {40: ("risk:high",)}, "harness-pr land '#12'", 40),
            (12, {40: ("task", "risk:high", "status:in-review")}, "harness-pr land 12 --yes", 40),
            # one risk:high ticket among risk:low ones, first or last
            (57, {8: ("task", "risk:low"), 93: ("bug", "risk:high", "size:S")}, "harness-pr land 57", 93),
            (204, {61: ("risk:high", "size:L"), 3: ("risk:low",)}, "harness-pr land --yes 204", 61),
        ]
        for pr, tickets, command, high in cases:
            with self.subTest(command=command, tickets=sorted(tickets)):
                self.scenario(pr_call(pr, list(tickets), "feature/x"),
                              *(issue_call(n, *labels) for n, labels in tickets.items()))
                reason = self.ask(command)
                self.assertTrue(names_number(reason, high), f"the reason does not name #{high}: {reason}")
                self.assertIn("risk:high", reason)

    def test_risk_low_ticket_passes_silently(self):
        cases = [
            # the same PR and ticket numbers the risk:high cases ask for, labeled risk:low here
            (12, {40: ("task", "risk:low", "size:S", "status:in-review")}, "harness-pr land 12"),
            (12, {40: ("risk:low",)}, "harness-pr land '#12'"),
            (204, {61: ("risk:low", "size:L"), 3: ("risk:low", "bug")}, "harness-pr land 204 --yes"),
            (57, {8: ("task", "risk:low"), 93: ("risk:low",), 94: ("risk:low", "chore")}, "harness-pr land --yes 57"),
        ]
        for pr, tickets, command in cases:
            with self.subTest(command=command, tickets=sorted(tickets)):
                self.scenario(pr_call(pr, list(tickets), "feature/x"),
                              *(issue_call(n, *labels) for n, labels in tickets.items()))
                self.assert_silent(command)

    # ---- what cannot be told is asked

    def test_unknown_risk_asks(self):
        reasons = {}  # PR 12's reason for each cause: each its own

        # A ticket without a risk label, alone or beside a risk:low one.
        for pr, tickets, unlabeled in (
            (12, {40: ("task", "size:M")}, 40),
            (88, {15: ()}, 15),
            (12, {40: ("risk:low",), 41: ("task", "status:in-review")}, 41),
        ):
            with self.subTest(cause="no risk label", pr=pr, tickets=sorted(tickets)):
                self.scenario(pr_call(pr, list(tickets), "feature/x"),
                              *(issue_call(n, *labels) for n, labels in tickets.items()))
                reason = self.ask(f"harness-pr land {pr}")
                self.assertTrue(names_number(reason, unlabeled), f"#{unlabeled} not named: {reason}")
                if (pr, unlabeled) == (12, 40):
                    reasons["no risk label"] = reason

        # No ticket: the PR closes nothing and its head is not t/<N>-.
        for pr, head in ((12, "feature/login"), (318, "main"), (12, "t/abc-not-a-number")):
            with self.subTest(cause="no ticket", pr=pr, head=head):
                self.scenario(pr_call(pr, (), head))
                reason = self.ask(f"harness-pr land {pr}")
                self.assertTrue(names_number(reason, pr), f"PR #{pr} not named: {reason}")
                if (pr, head) == (12, "feature/login"):
                    reasons["no ticket"] = reason

        # A failing gh: pr view or issue view exits non-zero, answers something other than JSON, or gh is missing.
        failing = [
            ("pr view fails", [{"contains": ["pr", "view", "12"], "exit": 1,
                                "stderr": "GraphQL: Could not resolve to a PullRequest with the number of 12.\n"}],
             None),
            ("issue view fails", [pr_call(12, [40]), {"contains": ["issue", "view", "40"], "exit": 1,
                                                       "stderr": "HTTP 403: API rate limit exceeded\n"}], None),
            ("not JSON", [{"contains": ["pr", "view", "12"], "stdout": "<!DOCTYPE html><html>502</html>"}], None),
            ("gh missing", [pr_call(12, [40]), issue_call(40, "risk:low")],
             {"HARNESS_GH": str(self.tmp / "no-such-dir" / "gh")}),
        ]
        for cause, calls, env in failing:
            with self.subTest(cause=cause):
                self.scenario(*calls)
                reason = self.ask("harness-pr land 12", env=env)
                self.assertRegex(reason, r"\bgh\b", f"{cause}: the reason does not say gh failed")
                if cause == "pr view fails":
                    reasons["gh failing"] = reason

        # A gh slower than 3 s: on the PR's view, or on every issue view of a PR closing four tickets (a hook that
        # goes on past a cut-off call still answers inside 8 s in all).
        tickets = (21, 22, 23, 24)
        for hang, limit in (("pr", ONE_CALL_LIMIT), ("issue", ALL_CALLS_LIMIT)):
            with self.subTest(cause="gh slow", hang=hang):
                self.scenario(pr_call(12, tickets), *(issue_call(n, "risk:low") for n in tickets), pace={hang: HANG})
                code, out, err, seconds = self.run_hook("harness-pr land 12")
                reason = self.ask_reason("harness-pr land 12", code, out, err)
                self.assertLess(seconds, limit, f"a hanging gh {hang} call took {seconds:.1f} s to answer")
                self.assertRegex(reason, r"\bgh\b", "the reason does not say gh was too slow")
                if hang == "pr":
                    reasons["gh slow"] = reason

        # gh in time on every call, slow in all: past 8 s, ask, naming gh, inside the 10 s Claude Code gives the
        # hook (a hook killed there lets the land run unasked). The 8 s are one deadline for every call: calls cut
        # off at 3 s each, a deadline looked at only between calls, or a guess at the total from the PR's view all
        # run past 10 s on one of these.
        many = (21, 22, 23, 24, 25, 26)
        slow_in_all = [
            # seven calls at 1.5 s each: 10.5 s or more
            ("every call slow", many, {"pr": CALL_DELAY, "issue": CALL_DELAY}),
            # the PR's view at once, then six issue views at 1.8 s each: 10.8 s or more, the first call fast
            ("issue views slow", many, {"issue": ISSUE_DELAY}),
            # four calls at 2.4 s each: the fourth starts near 7.3 s, inside 8 s, and would end near 9.8 s, so only
            # a cut-off at 8 s (not 3 s after the call starts) answers in time
            ("call in flight at 8 s", many[:3], {"pr": IN_FLIGHT_DELAY, "issue": IN_FLIGHT_DELAY}),
        ]
        for cause, few, pace in slow_in_all:
            with self.subTest(cause="gh slow in all", calls=cause, tickets=few, pace=pace):
                self.scenario(pr_call(12, few), *(issue_call(n, "risk:low") for n in few), pace=pace)
                code, out, err, seconds = self.run_hook("harness-pr land 12")
                reason = self.ask_reason("harness-pr land 12", code, out, err)
                self.assertLess(seconds, ALL_CALLS_LIMIT, f"{cause}: gh took {seconds:.1f} s to answer")
                self.assertRegex(reason, r"\bgh\b", f"{cause}: the reason does not say gh ran out of time")
        # What those asks are not: the six tickets answered at once pass; so do two of them at 1.5 s a call (three
        # calls, about 5 s); and so does a PR's view at 2 s followed by four issue views at once (about 2.3 s), so
        # neither the tickets, nor slow calls inside 8 s, nor a slow first call alone asks.
        in_time = [
            ("at once", many, None),
            ("every call slow", many[:2], {"pr": CALL_DELAY, "issue": CALL_DELAY}),
            ("PR's view slow", many[:4], {"pr": PR_DELAY}),
        ]
        for cause, few, pace in in_time:
            with self.subTest(cause="gh in time", calls=cause, tickets=few, pace=pace):
                self.scenario(pr_call(12, few), *(issue_call(n, "risk:low") for n in few), pace=pace)
                self.assert_silent("harness-pr land 12")

        # P that is not a PR number.
        self.scenario(pr_call(12, [40]), issue_call(40, "risk:low"))
        for command in ("harness-pr land abc", "harness-pr land 12x", "harness-pr land '#x'"):
            with self.subTest(cause="not a number", command=command):
                self.ask(command)

        self.assertEqual(sorted(reasons), ["gh failing", "gh slow", "no risk label", "no ticket"])
        self.assertEqual(len(set(reasons.values())), 4, f"the causes share a reason: {reasons}")

    def test_gh_deadline_is_shared_across_lands(self):
        # Two lands in one command, each closing three risk:low tickets, gh at 1.5 s a call: eight calls, 12 s or
        # more. The 8 s are one deadline for the whole command: the hook asks, naming gh, inside the 10 s Claude
        # Code gives it. A hook that gives each land its own 8 s finishes both (6 s each) near 12 s: Claude Code
        # has killed it by then, and both lands run unasked. Here that hook is silent (every ticket is risk:low)
        # and over ALL_CALLS_LIMIT.
        self.scenario(pr_call(12, [21, 22, 23]), pr_call(7, [31, 32, 33]),
                      *(issue_call(n, "risk:low") for n in (21, 22, 23, 31, 32, 33)),
                      pace={"pr": CALL_DELAY, "issue": CALL_DELAY})
        command = "harness-pr land 12 && harness-pr land 7"
        code, out, err, seconds = self.run_hook(command)
        reason = self.ask_reason(command, code, out, err)
        self.assertLess(seconds, ALL_CALLS_LIMIT, f"gh took {seconds:.1f} s to answer across two lands")
        self.assertRegex(reason, r"\bgh\b", "the reason does not say gh ran out of time")

    def test_ticket_from_the_head_branch_when_nothing_closes(self):
        cases = [
            # (PR, head, ticket labels on gh, expected: the ticket the ask names, or None for silence)
            (12, "t/77-fix-login", {77: ("task", "risk:high")}, 77),
            (5, "t/123-big-change", {123: ("risk:low", "size:S")}, None),
            (31, "t/9-x", {9: ("task",)}, 9),
            (31, "t/9-x", {9: ("risk:low",)}, None),
            (12, "t/77-fix-login", {77: ("risk:low",)}, None),
        ]
        for pr, head, tickets, asked in cases:
            with self.subTest(pr=pr, head=head, labels=tickets):
                self.scenario(pr_call(pr, (), head), *(issue_call(n, *labels) for n, labels in tickets.items()))
                command = f"harness-pr land {pr}"
                if asked is None:
                    self.assert_silent(command)
                else:
                    reason = self.ask(command)
                    self.assertTrue(names_number(reason, asked), f"#{asked} not named: {reason}")
        # The head is the fallback only: a PR that closes a risk:low ticket is judged by it, whatever its head.
        with self.subTest(closes=40, head="t/77-fix-login"):
            self.scenario(pr_call(12, [40], "t/77-fix-login"), issue_call(40, "risk:low"),
                          issue_call(77, "risk:high"))
            self.assert_silent("harness-pr land 12")

    # ---- overrides

    def test_override_flags_ask_without_gh(self):
        # Every ticket is risk:low, so the plain land passes: the flag alone is what asks.
        self.scenario(pr_call(12, [40]), issue_call(40, "risk:low"), pr_call(7, [3]), issue_call(3, "risk:low"))
        self.assert_silent("harness-pr land 12")
        cases = [
            (self.repo, "harness-pr land 12 --accept-findings", "accept-findings", "discard"),
            (self.repo, "harness-pr land --accept-findings 7", "accept-findings", "discard"),
            (self.repo, "harness-pr land 12 --discard", "discard", "accept-findings"),
            (self.repo, "harness-pr land --yes --discard 7", "discard", "accept-findings"),
            (self.outside, f"cd {shlex.quote(str(self.repo))} && harness-pr land 12 --discard", "discard",
             "accept-findings"),
        ]
        for cwd, command, flag, other in cases:
            with self.subTest(command=command):
                reason = self.ask(command, cwd=cwd)
                self.assertIn(flag, reason)
                self.assertNotIn(other, reason, "the reason names a flag the command does not give")
                self.assertEqual(self.gh_calls(), [], "an override flag must ask without calling gh")
        with self.subTest(command="both flags"):
            reason = self.ask("harness-pr land 12 --discard --accept-findings")
            self.assertTrue("discard" in reason or "accept-findings" in reason, reason)
            self.assertEqual(self.gh_calls(), [])

    # ---- where it is judged, and what it leaves alone

    def test_cd_prefix_and_other_commands(self):
        # PR 12 closes #40: risk:high in this repo, risk:low in the other one. PR 7 (closing #3) and PR 9 (closing
        # #5) are risk:low and exist in this repo only. gh anywhere else answers the decoy.
        other = self.make_repo("other")
        self.scenario(pr_call(12, [40]), issue_call(40, "risk:high"), pr_call(7, [3]), issue_call(3, "risk:low"),
                      pr_call(9, [5]), issue_call(5, "task", "risk:low"))
        self.scenario(pr_call(12, [40]), issue_call(40, "risk:low"), where=other)
        worktree = self.tmp / "wt"
        self.git(self.repo, "worktree", "add", "-q", "-b", "t/40-fix-login", str(worktree))
        repo, there = shlex.quote(str(self.repo)), shlex.quote(str(other))
        judged = [
            # (the session's cwd, the command, whether it asks: the main checkout of the part's directory decides)
            (self.repo, "harness-pr land 12", True),
            (other, "harness-pr land 12", False),
            (other, f"cd {repo} && harness-pr land 12", True),
            (self.repo, f"cd {there} && harness-pr land 12", False),
            (self.outside, f"cd {repo}; harness-pr land 12", True),
            (self.tmp, "cd proj && harness-pr land 12", True),
            (self.tmp, "cd other && harness-pr land 12", False),
            (other, "cd ../proj && harness-pr land 12", True),
            (other, f"cd {shlex.quote(str(self.repo / 'src'))} && harness-pr land 12", True),
            (worktree, "harness-pr land 12", True),
            (other, f"cd {shlex.quote(str(worktree))} && harness-pr land 12", True),
            (self.repo, "git fetch -q && harness-pr land 12 2>&1 | tail -5", True),
            (self.repo, "harness-pr ready 12 && harness-pr land 12", True),
            # harness-pr by its path: the plugin's bin by variable or absolute path, as a session off PATH runs
            # it (the allow rule names the bare word only, but auto mode can let a path through)
            (self.repo, '"$CLAUDE_PLUGIN_ROOT/bin/harness-pr" land 12', True),
            (self.repo, f"{shlex.quote(str(PLUGIN / 'bin' / 'harness-pr'))} land 12", True),
            (self.repo, f"cd {there} && {shlex.quote(str(PLUGIN / 'bin' / 'harness-pr'))} land 12", False),
            # in a subshell
            (self.repo, "(harness-pr land 12)", True),
            (self.repo, f"(cd {there} && harness-pr land 12)", False),
        ]
        for cwd, command, asks in judged:
            with self.subTest(cwd=cwd.name, command=command):
                if asks:
                    reason = self.ask(command, cwd=cwd)
                    self.assertTrue(names_number(reason, 40), reason)
                    self.assertIn("risk:high", reason)
                else:
                    self.assert_silent(command, cwd=cwd)

        # Every land in a command is judged, each in the main checkout of its own directory, a cd carrying over to
        # the parts after it: one risk:high PR anywhere in the chain asks, in either order, and a chain of risk:low
        # lands passes. Judging only the first land, only the last, or every land in one directory (the session's,
        # the first cd's or the last cd's) gets one of these wrong; so does asking on any chain.
        home, away = os.path.realpath(str(self.repo)), os.path.realpath(str(other))
        chains = [
            # (the session's cwd, the command, the risk:high ticket the ask names or None for silence, (PR,
            # directory) views gh must have answered)
            (self.repo, "harness-pr land 7 && harness-pr land 12", 40, {(12, home)}),
            (self.repo, "harness-pr land 12 && harness-pr land 7", 40, {(12, home)}),
            (self.repo, "harness-pr land 7; harness-pr land 12", 40, {(12, home)}),
            (self.repo, "harness-pr land 7 || harness-pr land 12", 40, {(12, home)}),
            (self.repo, "harness-pr land 9\nharness-pr land '#12'", 40, {(12, home)}),
            (self.repo, "harness-pr land 7 && harness-pr land 12 && harness-pr land 9", 40, {(12, home)}),
            (self.repo, "harness-pr land 9 2>&1 | tail -1; harness-pr land 12 --yes", 40, {(12, home)}),
            (self.repo, "harness-pr land 7 && harness-pr land 9", None, {(7, home), (9, home)}),
            (self.repo, "harness-pr land 9; harness-pr land '#7' --yes", None, {(7, home), (9, home)}),
            (self.repo, "harness-pr ready 12 && harness-pr land 7", None, {(7, home)}),
            (self.repo, "harness-pr land 7 && harness-pr land --help", None, {(7, home)}),
            # across the two repos: PR 12 is risk:high in this one only, PR 7 exists in this one only
            (self.tmp, f"cd {there} && harness-pr land 12; cd {repo} && harness-pr land 12", 40, {(12, home)}),
            (self.tmp, f"cd {repo} && harness-pr land 12; cd {there} && harness-pr land 12", 40, {(12, home)}),
            (other, f"harness-pr land 12 && cd {repo} && harness-pr land 12", 40, {(12, home)}),
            (self.tmp, "cd other && harness-pr land 12; cd ../proj && harness-pr land 12", 40, {(12, home)}),
            (self.tmp, f"cd {there} && harness-pr land 12 && cd {repo} && harness-pr land 7", None,
             {(12, away), (7, home)}),
            (self.repo, f"harness-pr land 7 && cd {there} && harness-pr land 12", None, {(7, home), (12, away)}),
            (self.tmp, "cd proj && harness-pr land 9; cd ../other && harness-pr land 12", None,
             {(9, home), (12, away)}),
            # a cd inside ( ... ) ends with the subshell: the land after the ) runs where the command started (or
            # where the last cd outside the parentheses left it), so it is judged there, risk:high in this repo.
            # A hook that carries the cd past the ) judges both lands in the other repo and stays silent on the
            # first two; one that goes back to the session's directory at the ) misses the cd before the ( on
            # the third.
            (self.repo, f"(cd {there} && harness-pr land 12) && harness-pr land 12", 40, {(12, away), (12, home)}),
            (self.tmp, "(cd other && harness-pr land 12); cd proj && harness-pr land 12", 40,
             {(12, away), (12, home)}),
            (self.tmp, "cd proj && (cd ../other && harness-pr land 12) && harness-pr land 12", 40,
             {(12, away), (12, home)}),
            (other, f"(cd {repo} && harness-pr land 7); harness-pr land 12", None, {(7, home), (12, away)}),
        ]
        for cwd, command, high, views in chains:
            with self.subTest(cwd=cwd.name, command=command):
                if high is None:
                    self.assert_silent(command, cwd=cwd)
                else:
                    reason = self.ask(command, cwd=cwd)
                    self.assertTrue(names_number(reason, high), f"the reason does not name #{high}: {reason}")
                    self.assertIn("risk:high", reason)
                made = self.pr_views()
                self.assertLessEqual(views, made, "a land was not judged in the main checkout of its directory")
                self.assertLessEqual({where for _, where in made}, {home, away}, "gh ran outside a main checkout")
        # Not a PR number in any part asks.
        for command in ("harness-pr land 7 && harness-pr land abc", "harness-pr land 9; harness-pr land '#x'"):
            with self.subTest(command=command):
                self.ask(command)
        # An override flag in any part asks, naming it, before any gh call: every land but PR 12 in this repo is
        # risk:low, so the flag alone is what asks, wherever it stands.
        flagged = [
            (self.repo, "harness-pr land 7 && harness-pr land 9 --discard", "discard", "accept-findings"),
            (self.repo, "harness-pr land 9 --discard && harness-pr land 7", "discard", "accept-findings"),
            (self.repo, "harness-pr land 7 && harness-pr land 12 --discard", "discard", "accept-findings"),
            (self.repo, "harness-pr land 7; harness-pr land --accept-findings 9; harness-pr land 7", "accept-findings",
             "discard"),
            (self.tmp, f"cd {there} && harness-pr land 12; cd {repo} && harness-pr land 7 --accept-findings",
             "accept-findings", "discard"),
        ]
        for cwd, command, flag, unnamed in flagged:
            with self.subTest(cwd=cwd.name, command=command):
                reason = self.ask(command, cwd=cwd)
                self.assertIn(flag, reason)
                self.assertNotIn(unnamed, reason, "the reason names a flag the command does not give")
                self.assertEqual(self.gh_calls(), [], "an override flag in any part must ask without calling gh")

        left_alone = [
            "harness-pr ready 12",
            "harness-pr land --help",
            "harness-pr land",
            "harness-pr --help",
            "harness-pr gate 12 --json",
            f"cd {there} && harness-pr ready 12",
            "git status",
            "ls -la",
            "gh pr merge 12 --squash",
            "echo 'harness-pr land 12'",
            "grep -rn 'harness-pr land 12' docs",
        ]
        for command in left_alone:
            with self.subTest(command=command):
                self.assert_silent(command)
                self.assertEqual(self.gh_calls(), [], f"{command!r} must not call gh")

    # ---- failing open

    def test_fails_open_without_the_lib(self):
        # The working hook asks here, so the silence below is the break's, not the case's.
        self.scenario(pr_call(12, [40]), issue_call(40, "risk:high"))
        self.ask("harness-pr land 12")
        token = _SHARED["token"]
        cases = [
            ("no lib", "harness_lib"),
            ("broken lib", _SHARED["broken message"]),
            ("own error", token),
        ]
        for copy, needle in cases:
            with self.subTest(copy=copy):
                code, out, err, _ = self.run_hook("harness-pr land 12", hook=_SHARED[copy] / "hooks" / NAME)
                self.assertEqual(code, 0, err)
                self.assertEqual(out, "", "a hook failing open makes no decision")
                self.assertEqual(err.count("\n"), 1, repr(err))
                self.assertEqual(len(err.splitlines()), 1, repr(err))
                self.assertTrue(err.startswith(f"{NAME}: "), repr(err))
                self.assertIn(needle, err)
                self.assertNotIn("Traceback", err)

    # ---- registration

    def test_registered_in_hooks_json(self):
        data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        found = []
        for event, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    words = [hook.get("command", "")] + [str(a) for a in hook.get("args", [])]
                    if any(w.endswith("/hooks/" + NAME) for w in words):
                        found.append((event, entry.get("matcher"), hook.get("timeout")))
        self.assertEqual(len(found), 1, f"hooks.json must run {NAME} once: {found}")
        event, matcher, timeout = found[0]
        self.assertEqual(event, "PreToolUse")
        self.assertTrue(matcher and re.fullmatch(matcher, "Bash"), f"matcher {matcher!r} does not match Bash")
        self.assertEqual(timeout, HOOK_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
