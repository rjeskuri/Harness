"""Tests for harness-work: start (branch and worktree from origin/main, an existing remote branch reused, the
exclude line, the worktree setup, .worktreeinclude copies, refusals), attempt's retry, escalate and
needs-input sequence, dispatch (dry-run plan, headless launch billed through apiKeyHelper, the bg fallback
while the api budget is exhausted, re-dispatch of a run that ended without a PR), runs --sweep's outcome
classification and the budget it marks, budget status, exhaust and reset, worktrees and worktree remove.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME, a bare `origin` and a clone of it (`proj`, the main checkout) made with
real git. A `fakebin` folder holds `gh` (a wrapper around fakegh.py) and `claude` (a fake that logs its
argv, cwd and environment, answers `agents --json` from $FAKECLAUDE_AGENTS, prints `backgrounded · abc123`
for --bg, and for -p sleeps briefly, then prints a canned JSON result). harness-board is the real one,
found next to harness-work. The fake executables are written once for the module: macOS checks a new
executable on its first run, which costs about half a second each time.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-work")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
TITLE = "Add the board tool"
BRANCH = "t/12-add-the-board-tool"
SECRET = "fake-key-value-7f3a9c"   # what the key command prints; must never reach argv or env
DEFAULT_KEY_CMD = "security find-generic-password -s harness-anthropic -w"
TEN_DAYS_AGO = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+0000")
MARK = "\n\n<!-- harness -->"      # the last line of every comment harness-board posts
QUESTION = "**Needs your input**\n\nWhich base?\n\nReply in a comment; /harness:work 12 picks it up." + MARK
CREDENTIALS = {"ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_AUTH_TOKEN": "fake-token",
               "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth"}
# What claude 2.1.296 printed for `claude -p ... --output-format json` with an invalid ANTHROPIC_API_KEY (exit 1;
# stderr held only a connectors warning). subtype says success even so.
API_401 = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
           "api_error_status": 401, "result": "Invalid API key \u00b7 Fix external API key", "num_turns": 1,
           "total_cost_usd": 0, "duration_api_ms": 0, "stop_reason": "stop_sequence",
           "usage": {"fallback_credit": None}}
# The results of two live headless probes on 2026-10-13: a stored key not scoped to a workspace (400), and an
# apiKeyHelper that could not produce a key (401). Neither credential can ever work, so both are `auth`.
API_400_WORKSPACE = dict(API_401, api_error_status=400, result=(
    "API Error: 400 This API key is not scoped to a workspace, so this request must include the "
    "anthropic-workspace-id header to identify the workspace it is for."))
API_401_HELPER = dict(API_401, api_error_status=401, result=(
    "API Error: 401 Your apiKeyHelper script is failing: command exited with status 1 · Fix the "
    "apiKeyHelper setting"))
CONNECTORS_WARNING = "Warning: some connectors could not be loaded"
BODY = """## Request
Add the board tool.

## Definition of done
- [ ] `harness-board ready` lists unblocked tickets
"""

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
argv = sys.argv[1:]
log = os.environ.get("FAKECLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\\n")
if argv[:1] == ["agents"]:
    sys.stdout.write(os.environ.get("FAKECLAUDE_AGENTS", "[]"))
    sys.exit(0)
if "--bg" in argv:
    print("backgrounded \\u00b7 " + os.environ.get("FAKECLAUDE_BG_ID", "abc123") + " \\u00b7 " + argv[argv.index("-n") + 1])
    sys.exit(0)
if "-p" in argv:
    time.sleep(float(os.environ.get("FAKECLAUDE_SLEEP", "0.2")))
    if os.environ.get("FAKECLAUDE_MODE") == "billing":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
                          "api_error_status": 400, "result": "Credit balance is too low"}}))
        sys.exit(1)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "PR #7 ready"}}))
    sys.exit(0)
sys.exit(2)
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


BINS = {}


def setUpModule():
    """fakebin: gh and claude; toolbin: git and python3 only (symlinks), so PATH holds nothing else of ours."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-bin-"))
    fakebin, toolbin = os.path.join(root, "fakebin"), os.path.join(root, "toolbin")
    executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "claude"), FAKE_CLAUDE.format(python=sys.executable))
    os.makedirs(toolbin)
    os.symlink(GIT, os.path.join(toolbin, "git"))
    os.symlink(sys.executable, os.path.join(toolbin, "python3"))
    BINS.update(root=root, fakebin=fakebin, toolbin=toolbin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def issue(number=12, labels=("task", "status:ready", "size:S", "risk:low"), blocked_by=(), state="OPEN",
          title=TITLE, comments=()):
    return {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}", "body": BODY,
            "labels": [{"name": n} for n in labels], "state": state, "parent": None,
            "blockedBy": list(blocked_by), "comments": list(comments)}


def comment(login, when, body):
    return {"author": {"login": login}, "createdAt": when, "body": body,
            "url": f"https://github.com/o/r/issues/12#issuecomment-{abs(hash(body)) % 100000}"}


def ready_row(number, title=TITLE, labels=("task", "status:ready", "size:S", "risk:low")):
    return {"number": number, "title": title, "labels": [{"name": n} for n in labels], "blockedBy": []}


def wait_for(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def iso_ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class WorkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.claude_log = os.path.join(self.tmp, "claude.log")
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.repo = self.mk("proj")
        self.git(self.repo, "init", "-q", "-b", "main")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, ".gitignore"), ".env\n*.log\n")
        write(os.path.join(self.repo, ".worktreeinclude"), "# copied into new worktrees\n.env\n.env.*\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "First", env=dict(GIT_AUTHOR_DATE=TEN_DAYS_AGO,
                                                                    GIT_COMMITTER_DATE=TEN_DAYS_AGO))
        self.git(self.repo, "remote", "add", "origin", self.origin)
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.key = hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12]
        self.scenario()

    # ----------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_", "FAKECLAUDE_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_GH=os.path.join(BINS["fakebin"], "gh"),
                   HARNESS_CLAUDE=os.path.join(BINS["fakebin"], "claude"),
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log, FAKECLAUDE_LOG=self.claude_log)
        env.update(extra)
        return env

    def git(self, repo, *args, env=None):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args),
                           env=dict(self.env(), **(env or {})), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def work(self, *args, env=None, cwd=None):
        p = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.repo, env=env or self.env(),
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def ok(self, *args, env=None):
        p = self.work(*args, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def ok_json(self, *args, env=None):
        return json.loads(self.ok(*args, env=env).stdout)

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)

    def scenario(self, issues=(), ready=(), prs=(), sub_issues=()):
        calls = [{"match": ["issue", "view", str(i["number"])], "stdout_json": i} for i in issues]
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": list(ready)},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": list(prs)},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": list(sub_issues)},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def gh_calls(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def claude_calls(self):
        if not os.path.exists(self.claude_log):
            return []
        with open(self.claude_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def cache_path(self, number=12):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key, "tickets", f"{number}.json")

    def read_json(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def runs_dir(self):
        return os.path.join(self.home, ".claude", "harness", "runs")

    def run_records(self):
        folder = self.runs_dir()
        if not os.path.isdir(folder):
            return []
        return [self.read_json(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if n.endswith(".json")]

    def budgets_path(self):
        return os.path.join(self.home, ".claude", "harness", "budgets.json")

    def write_budget(self, name, hours, reason="test"):
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write(self.budgets_path(), json.dumps({name: {"exhausted_until": until, "reason": reason}}))

    def write_run(self, ticket, **fields):
        rid = fields.pop("id", f"{self.key}-{ticket}-20261013120000")
        record = {"id": rid, "ticket": ticket, "repo": os.path.realpath(self.repo),
                  "worktree": os.path.join(self.repo, ".claude", "worktrees", f"t-{ticket}-x"),
                  "branch": f"t/{ticket}-x", "venue": "headless", "log": os.path.join(self.runs_dir(), rid + ".log"),
                  "started_at": iso_ago(), "ended_at": None, "exit": None, "outcome": None}
        record.update(fields)
        write(os.path.join(self.runs_dir(), rid + ".json"), json.dumps(record))
        return record

    def dead_pid(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def other_clone(self):
        other = os.path.join(self.tmp, "other")
        self.git(self.tmp, "clone", "-q", self.origin, other)
        return other

    def worktree_path(self, branch=BRANCH):
        return os.path.join(os.path.realpath(self.repo), ".claude", "worktrees", branch.replace("/", "-"))

    def iso_close_to(self, text, delta, slack=300):
        when = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        expected = dt.datetime.now(dt.timezone.utc) + delta
        self.assertLess(abs((when - expected).total_seconds()), slack, text)


# =========================================================================== start
class Start(WorkTest):
    def test_start_creates_branch_and_worktree_from_origin_main(self):
        other = self.other_clone()                        # origin/main moves on; proj's main is behind
        write(os.path.join(other, "NEW.md"), "new\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "Second")
        self.git(other, "push", "-q", "origin", "main")
        head = self.git(other, "rev-parse", "HEAD")
        write(os.path.join(self.repo, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_WORKTREE_SETUP": "echo shared > .setup-ran"}}))
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_WORKTREE_SETUP": "echo local > .setup-ran"}}))
        self.scenario(issues=[issue()])

        data = self.ok_json("start", "12", "--json")
        self.assertEqual(set(data), {"number", "branch", "worktree", "tier", "previous_status", "waiting", "answers",
                                     "context"})
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("ready", False, []))
        wt = self.worktree_path()
        self.assertEqual((data["number"], data["branch"], data["worktree"], data["tier"]), (12, BRANCH, wt, "sonnet"))
        self.assertTrue(data["context"].startswith("# #12 Add the board tool"), data["context"][:80])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(wt, "branch", "--show-current"), BRANCH)
        with open(os.path.join(wt, ".setup-ran"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "local")      # settings.local.json wins over settings.json

        exclude = os.path.join(self.repo, ".git", "info", "exclude")
        with open(exclude, encoding="utf-8") as fh:
            self.assertEqual([ln.strip() for ln in fh].count(".claude/worktrees/"), 1)

        cache = self.read_json(self.cache_path())
        self.assertEqual((cache["branch"], cache["worktree"]), (BRANCH, wt))
        self.assertTrue(cache["started_at"])
        calls = self.gh_calls()
        edit = next(c for c in calls if c[:2] == ["issue", "edit"])
        self.assertIn("status:in-progress", edit)
        comment = next(c for c in calls if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.startswith(f"Started on {BRANCH} ("), body)
        self.assertTrue(body.splitlines()[0].endswith(", interactive)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)           # harness-board marks its comments

        again = self.ok("start", "12")                       # the existing worktree is reused
        self.assertEqual(again.stdout.splitlines()[0], wt)
        self.assertIn("# #12 Add the board tool", again.stdout)
        with open(exclude, encoding="utf-8") as fh:
            self.assertEqual([ln.strip() for ln in fh].count(".claude/worktrees/"), 1)
        self.assertEqual(self.read_json(self.cache_path())["started_at"], cache["started_at"])

    def test_start_reuses_an_existing_remote_branch(self):
        other = self.other_clone()
        self.git(other, "switch", "-q", "-c", "t/12-older-title")
        write(os.path.join(other, "WIP.md"), "work in progress\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "WIP")
        self.git(other, "push", "-q", "origin", "t/12-older-title")
        head = self.git(other, "rev-parse", "HEAD")
        self.scenario(issues=[issue()])

        data = self.ok_json("start", "12", "--json")
        wt = self.worktree_path("t/12-older-title")
        self.assertEqual((data["branch"], data["worktree"]), ("t/12-older-title", wt))
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(wt, "rev-parse", "--abbrev-ref", "@{u}"), "origin/t/12-older-title")
        self.assertFalse(os.path.exists(self.worktree_path()))

    def test_start_copies_gitignored_files_listed_in_worktreeinclude(self):
        write(os.path.join(self.repo, ".env"), "KEY=1\n")              # ignored and included: copied
        write(os.path.join(self.repo, "sub", ".env"), "SUB=1\n")       # same, deeper
        write(os.path.join(self.repo, "debug.log"), "noise\n")         # ignored, not included
        write(os.path.join(self.repo, ".env.sample"), "KEY=\n")        # included, not ignored
        self.scenario(issues=[issue()])
        wt = self.ok_json("start", "12", "--json")["worktree"]
        with open(os.path.join(wt, ".env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "KEY=1\n")
        self.assertTrue(os.path.isfile(os.path.join(wt, "sub", ".env")))
        self.assertFalse(os.path.exists(os.path.join(wt, "debug.log")))
        self.assertFalse(os.path.exists(os.path.join(wt, ".env.sample")))

    def test_start_leaves_an_in_review_ticket_in_review(self):
        self.scenario(issues=[issue(labels=("task", "status:in-review", "size:S", "risk:low"))])
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("in-review", False, []))
        self.assertEqual(data["worktree"], self.worktree_path())
        self.assertTrue(os.path.isdir(data["worktree"]))
        calls = self.gh_calls()
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertFalse([c for c in calls if c[-2:] == ["--json", "comments"]])   # no answers read

    def needs_input(self, *replies):
        comments = [comment("rjeskuri", iso_ago(hours=3), QUESTION)] + list(replies)
        self.scenario(issues=[issue(labels=("task", "status:needs-input", "size:S", "risk:low"), comments=comments)])

    def test_start_on_needs_input_returns_the_owners_answer_and_moves_on(self):
        self.needs_input(comment("RJeskuri", iso_ago(hours=2), "Use main."),
                         comment("rjeskuri", iso_ago(hours=1), "Also add a flag."))
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"]), ("needs-input", False))
        self.assertEqual([a["body"] for a in data["answers"]], ["Use main.", "Also add a flag."])
        calls = self.gh_calls()
        read = calls.index(["issue", "view", "12", "--json", "comments"])
        edit = next(i for i, c in enumerate(calls) if c[:2] == ["issue", "edit"])
        self.assertLess(read, edit)
        self.assertIn("status:in-progress", calls[edit])
        self.assertIn("status:needs-input", calls[edit])                  # removed
        first_show = next(i for i, c in enumerate(calls) if c[:3] == ["issue", "view", "12"]
                          and c[-2:] not in (["--json", "labels"], ["--json", "comments"]))
        self.assertLess(read, first_show)                                  # answers before show refreshes the cache

        text = self.ok("start", "12").stdout                               # text mode shows the answer too
        self.assertIn("Use main.", text)

    def test_start_on_needs_input_without_an_answer_waits(self):
        self.needs_input()
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("needs-input", True, []))
        calls = self.gh_calls()
        self.assertIn(["issue", "view", "12", "--json", "comments"], calls)
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertIn("no answer yet", self.ok("start", "12").stdout)

    def test_start_refuses_an_epic(self):
        self.scenario(issues=[issue(labels=("epic", "status:ready"), title="The board")],
                      sub_issues=[{"number": 13, "title": "A task", "state": "open", "labels": []},
                                  {"number": 14, "title": "Done", "state": "closed", "labels": []}])
        p = self.work("start", "12")
        self.assertOneLineError(p, "#12 is an epic", "#13")
        self.assertNotIn("#14", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_start_refuses_blocked_and_closed_tickets(self):
        cases = [
            (issue(labels=("task", "status:blocked")), ["status:blocked"], []),
            (issue(blocked_by=[{"number": 9, "state": "OPEN"}, {"number": 8, "state": "CLOSED"}]),
             ["blocked by open #9"], ["#8"]),
            (issue(state="CLOSED"), ["#12 is closed"], []),
        ]
        for record, needles, absent in cases:
            with self.subTest(needles=needles):
                self.scenario(issues=[record])
                p = self.work("start", "12", "--json")
                self.assertOneLineError(p, *needles)
                for text in absent:
                    self.assertNotIn(text, p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])


# =========================================================================== attempt
class Attempt(WorkTest):
    def test_retry_escalate_needs_input_and_tier_progression(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["code", "fail"], "retry"),
            (["code", "fail"], "escalate opus"),
            (["code", "fail"], "needs-input"),
            (["code", "fail"], "needs-input"),
            (["tests", "fail"], "retry"),                    # each stage counts its own failures
            (["code", "pass"], "done"),
            (["code", "fail"], "retry"),                     # a pass ends the stage's run of failures
            (["review", "fail", "--tier", "opus"], "retry"),
            (["review", "fail", "--tier", "opus"], "escalate fable"),
            (["ci", "fail", "--tier", "fable"], "retry"),
            (["ci", "fail", "--tier", "fable"], "escalate fable"),   # Fable stays Fable
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "result", "at"})
        self.assertEqual((attempts[0]["stage"], attempts[0]["tier"], attempts[0]["result"]), ("code", "sonnet", "fail"))
        self.assertEqual(attempts[-1]["tier"], "fable")

        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"]), ("retry", 1, "sonnet"))

    def test_attempt_without_a_cache_says_to_start(self):
        self.assertOneLineError(self.work("attempt", "12", "code", "fail"), "harness-work start 12")


# =========================================================================== dispatch
class Dispatch(WorkTest):
    def test_dry_run_prints_the_plan_and_starts_nothing(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertIn("venue headless, 1 live, 1 of 2 slots free", p.stdout)
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch headless", p.stdout)
        self.assertIn(f"skip #13: live run {self.key}-13-", p.stdout)
        self.assertIn("skip #14: over --max 2", p.stdout)
        self.assertNotIn("would start #13", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_headless_launch_bills_through_api_key_helper_and_keeps_the_key_off_argv(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent",
                       **CREDENTIALS)
        p = self.ok("dispatch", env=env)
        self.assertIn(f"#12 headless on {BRANCH} (pid ", p.stdout)

        [record] = self.run_records()
        wt = self.worktree_path()
        self.assertRegex(record["id"], rf"^{self.key}-12-\d{{14}}$")
        self.assertEqual((record["ticket"], record["repo"], record["worktree"], record["branch"], record["venue"]),
                         (12, os.path.realpath(self.repo), wt, BRANCH, "headless"))
        self.assertIsInstance(record["pid"], int)
        self.assertNotIn("bg_id", record)
        self.assertEqual((record["ended_at"], record["exit"], record["outcome"]), (None, None, None))
        self.assertEqual(record["log"], os.path.join(self.runs_dir(), record["id"] + ".log"))
        self.assertTrue(record["started_at"].endswith("Z"))

        self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(record["log"])))
        [call] = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(len(argv), 8)
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        self.assertNotIn(SECRET, json.dumps(call))          # neither argv nor the environment carries the key
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", call["env"])
        for name, value in CREDENTIALS.items():                # an exported key would bill instead of the helper
            self.assertNotIn(name, call["env"])
            self.assertNotIn(value, json.dumps(call))
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", headless)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual(swept["outcome"], "ok")
        self.assertTrue(swept["ended_at"])
        self.assertIn("api: available", self.ok("budget").stdout)

    def test_headless_default_api_key_command(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
        argv = self.claude_calls()[0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"apiKeyHelper": DEFAULT_KEY_CMD})
        self.assertTrue(wait_for(lambda: pid_gone(self.run_records()[0]["pid"])))

    def test_falls_back_to_bg_while_the_api_budget_is_exhausted(self):
        self.write_budget("api", 1, "billing: credit balance is too low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"])
        for name in CREDENTIALS:                               # the subscription login bills, not an exported key
            self.assertNotIn(name, call["env"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["ended_at"]), ("bg", "abc123", None))
        self.assertNotIn("pid", record)
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        # The session is still working: the next dispatch leaves the ticket alone.
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working", "cwd": self.worktree_path()}])
        again = self.ok("dispatch", "--dry-run", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertIn("skip #12: live run", again.stdout)
        self.assertIn("venue bg", again.stdout)

    def test_redispatches_an_in_progress_ticket_whose_run_ended_without_a_pr(self):
        self.write_run(12, pid=self.dead_pid(), ended_at=iso_ago(hours=1), outcome="billing")
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Add the board tool (again after billing) and launch headless", p.stdout)
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))],
                      prs=[{"number": 7}])
        p = self.ok("dispatch", "--dry-run")
        self.assertNotIn("would start", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)

    def test_skips_a_ticket_after_three_ended_runs(self):
        in_progress = issue(labels=("task", "status:in-progress", "size:S", "risk:low"))
        for hours in (3, 2):
            self.write_run(12, id=f"{self.key}-12-2026101309{hours:02d}00", pid=self.dead_pid(),
                           started_at=iso_ago(hours=hours, minutes=30), ended_at=iso_ago(hours=hours), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # two ended runs: a third is launched
        self.assertIn("would start #12 Add the board tool (again after error)", p.stdout)

        self.write_run(12, id=f"{self.key}-12-20261013100000", pid=self.dead_pid(),
                       started_at=iso_ago(hours=1, minutes=30), ended_at=iso_ago(hours=1), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # three: the fourth dispatch skips it
        self.assertNotIn("would start", p.stdout)
        self.assertIn("skip #12: 3 runs ended without a pull request in 7 days, the last error", p.stdout)
        self.assertIn("harness-board status 12 ready", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["plan"], [])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])

        p = self.ok("dispatch")                                # for real: nothing started, the skip said
        self.assertIn("skip #12: 3 runs ended", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 3)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])


# =========================================================================== runs
class Runs(WorkTest):
    def test_sweep_classifies_billing_and_marks_the_api_budget(self):
        record = self.write_run(12, pid=self.dead_pid())
        low = dict(API_401, api_error_status=400, result="Credit balance is too low")   # told by the result text
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(low) + "\n")
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual((swept["id"], swept["outcome"]), (record["id"], "billing"))
        self.assertTrue(swept["ended_at"])
        self.assertEqual(self.run_records()[0]["outcome"], "billing")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertIn("credit balance", api["reason"].lower())
        self.assertTrue(api["reason"].startswith("billing: "))
        self.assertIn("api: exhausted until", self.ok("budget", "status").stdout)

        # The next dispatch falls back to a background session.
        self.scenario(issues=[issue()], ready=[ready_row(13, "Next")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Next (tier sonnet) and launch bg", p.stdout)

    def test_sweep_classifies_ok_auth_and_error(self):
        logs = {
            12: json.dumps({"type": "result", "is_error": False, "result": "Fixed the billing page; see 401 and 402."}),
            13: "Invalid API key · Please run /login",                  # no JSON result: the API-error lines
            14: "Traceback: something else broke",
            15: "warning: a hook printed 402 Payment Required",           # not an API-error line
        }
        for n, text in logs.items():
            record = self.write_run(n, pid=self.dead_pid())
            write(record["log"], text + "\n")
        swept = {r["ticket"]: r["outcome"] for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual(swept, {12: "ok", 13: "auth", 14: "error", 15: "error"})
        api = self.read_json(self.budgets_path())["api"]
        self.assertTrue(api["reason"].startswith("auth: Invalid API key"), api["reason"])

    def sweep_one(self, n, result):
        record = self.write_run(n, pid=self.dead_pid())
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        return next(r for r in self.ok_json("runs", "--sweep", "--json") if r["ticket"] == n)

    def test_sweep_reads_the_api_error_fields_of_the_result(self):
        # Other API errors and the model's own words never exhaust a budget.
        overloaded = dict(API_401, api_error_status=529, result="Overloaded")
        own_words = {k: v for k, v in API_401.items() if k not in ("terminal_reason", "api_error_status")}
        own_words["result"] = "Fix the billing page: 401 handling"
        for n, result in ((12, overloaded), (13, own_words)):
            with self.subTest(result=result["result"]):
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "error")
                self.assertIsNone(swept["exit"])
                self.assertFalse(os.path.exists(self.budgets_path()))

        # The real invalid-key result: auth, and the api budget is marked.
        self.assertEqual(self.sweep_one(14, API_401)["outcome"], "auth")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertEqual(api["reason"], "auth: API error 401: Invalid API key \u00b7 Fix external API key")

        # The same result with 402: billing.
        self.ok("budget", "reset", "api")
        self.assertEqual(self.sweep_one(15, dict(API_401, api_error_status=402))["outcome"], "billing")
        self.assertTrue(self.read_json(self.budgets_path())["api"]["reason"].startswith("billing: API error 402"))

    def test_sweep_classifies_an_unscoped_api_key_as_auth_and_falls_back_to_bg(self):
        # The real 400 of 2026-10-13: a 400 is no auth status, the result text names the key.
        swept = self.sweep_one(12, API_400_WORKSPACE)
        self.assertEqual(swept["outcome"], "auth")
        self.assertEqual(self.run_records()[0]["outcome"], "auth")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertTrue(api["reason"].startswith("auth: API error 400: API Error: 400 This API key is not scoped"),
                        api["reason"])
        self.assertIn("anthropic-workspace-id", api["reason"])
        self.assertIn("api: exhausted until", self.ok("budget", "status").stdout)

        # The next dispatch falls back to a background session instead of retrying the key headless.
        self.scenario(issues=[issue()], ready=[ready_row(13, "Next")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Next (tier sonnet) and launch bg", p.stdout)

    def test_sweep_classifies_key_and_helper_failures_as_auth_whatever_the_4xx(self):
        # (result, the status the reason must name, a fragment of the result the reason must carry)
        cases = [
            (API_400_WORKSPACE, "400", "not scoped to a workspace"),
            (API_401_HELPER, "401", "apiKeyHelper script is failing"),
            # The helper text under a status that is not an auth status: the text decides.
            (dict(API_401_HELPER, api_error_status=400,
                  result="API Error: 400 Your apiKeyHelper script is failing: timed out after 10s"),
             "400", "timed out after 10s"),
            (dict(API_401_HELPER, api_error_status=400,
                  result="Your apiKeyHelper script is failing: security: item not found"),
             "400", "security: item not found"),
            # Any other 4xx naming the API key.
            (dict(API_401, api_error_status=404,
                  result="API Error: 404 This API key does not have access to model claude-opus-5-5"),
             "404", "does not have access to model"),
            (dict(API_401, api_error_status=400,
                  result="API Error: 400 Your API key belongs to an archived workspace"),
             "400", "archived workspace"),
            # The workspace header named without the words "API key".
            (dict(API_401, api_error_status=400,
                  result="API Error: 400 Missing anthropic-workspace-id header for an organization credential"),
             "400", "organization credential"),
        ]
        for n, (result, status, fragment) in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                self.ok("budget", "reset", "api")
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "auth")
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(f"auth: API error {status}: "), api["reason"])
                self.assertIn(fragment, api["reason"])

        # terminal_reason api_error without a status: the text still decides, and the reason names no status.
        self.ok("budget", "reset", "api")
        no_status = {k: v for k, v in API_400_WORKSPACE.items() if k != "api_error_status"}
        self.assertEqual(self.sweep_one(40, no_status)["outcome"], "auth")
        reason = self.read_json(self.budgets_path())["api"]["reason"]
        self.assertTrue(reason.startswith("auth: API error: API Error: 400 This API key"), reason)

    def test_sweep_keeps_rate_limits_overloads_and_other_errors_as_error(self):
        # Guard against over-matching: transient statuses and the model's own words never exhaust a budget,
        # even when their text names an API key, the workspace header or the helper.
        own_words = {k: v for k, v in API_400_WORKSPACE.items() if k not in ("terminal_reason", "api_error_status")}
        cases = [
            dict(API_401, api_error_status=529, result="Overloaded"),
            dict(API_401, api_error_status=529, result="API Error: 529 Overloaded"),
            dict(API_401, api_error_status=429, result=(
                "API Error: 429 Number of request tokens has exceeded your per-minute rate limit for this API key")),
            dict(API_401, api_error_status=500,
                 result="API Error: 500 Internal server error while validating the API key"),
            dict(API_401, api_error_status=503,
                 result="API Error: 503 Service unavailable; API key checks are degraded"),
            dict(API_401, api_error_status=400,
                 result="API Error: 400 prompt is too long: 212000 tokens > 200000 maximum"),
            own_words,
            dict(own_words, result="Your apiKeyHelper script is failing, so I added a retry to it."),
        ]
        for n, result in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "error")
                self.assertFalse(os.path.exists(self.budgets_path()))

        # A run that succeeded is ok whatever its text says.
        ok = {"type": "result", "is_error": False, "result": API_400_WORKSPACE["result"]}
        self.assertEqual(self.sweep_one(30, ok)["outcome"], "ok")
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_sweep_closes_a_bg_run_on_any_state_that_is_not_live(self):
        for n, bg_id in ((12, "s-blocked"), (13, "s-idle"), (14, "s-done"), (15, "s-archived"), (16, "s-gone"),
                         (17, "s-nostate")):
            self.write_run(n, venue="bg", bg_id=bg_id)
        agents = json.dumps([{"id": "s-blocked", "kind": "background", "state": "blocked"},
                             {"id": "s-idle", "kind": "background", "state": "idle"},
                             {"id": "s-done", "kind": "background", "state": "done", "exitCode": 0},
                             {"id": "s-archived", "kind": "background", "state": "archived"},
                             {"id": "s-nostate", "kind": "background"}])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                     env=self.env(FAKECLAUDE_AGENTS=agents))}
        self.assertEqual({n: rows[n]["outcome"] for n in rows},
                         {12: None, 13: None, 14: "ok", 15: "archived", 16: "unknown", 17: None})
        self.assertEqual(rows[14]["exit"], 0)
        self.assertIsNone(rows[15]["exit"])
        self.assertEqual((rows[12]["state"], rows[13]["state"], rows[17]["state"]), ("blocked", "idle", "live"))

    def test_sweep_leaves_a_live_run_alone(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 12"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(12, pid=live.pid)
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertIsNone(row["ended_at"])
        self.assertEqual(row["state"], f"alive (pid {live.pid})")

    def test_sweep_treats_a_recycled_pid_as_ended(self):
        later = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(later.wait)
        self.addCleanup(later.kill)
        self.write_run(12, pid=later.pid, started_at=iso_ago(hours=1))   # the pid now belongs to a younger process
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertTrue(row["ended_at"])
        self.assertEqual(row["outcome"], "error")

    def test_runs_lists_recent_runs_and_all_with_all(self):
        self.write_run(12, ended_at=iso_ago(days=3), outcome="ok", pid=1)
        self.write_run(13, ended_at=iso_ago(hours=1), outcome="error", pid=1)
        self.assertEqual([r["ticket"] for r in self.ok_json("runs", "--json")], [13])
        self.assertEqual(sorted(r["ticket"] for r in self.ok_json("runs", "--all", "--json")), [12, 13])
        text = self.ok("runs").stdout
        self.assertIn("RUN", text)
        self.assertIn("proj #13", text)
        self.assertIn("ended error", text)


# =========================================================================== budget
class Budget(WorkTest):
    def test_status_exhaust_and_reset(self):
        self.assertEqual(self.ok("budget").stdout.splitlines(), ["api: available", "cloud: available"])
        p = self.ok("budget", "exhaust", "cloud", "--reason", "ultrareview: out of credits", "--hours", "2")
        self.assertRegex(p.stdout.strip(), r"^cloud: exhausted until \S+Z \(ultrareview: out of credits\)$")
        saved = self.read_json(self.budgets_path())
        self.iso_close_to(saved["cloud"]["exhausted_until"], dt.timedelta(hours=2))
        self.assertEqual(saved["cloud"]["reason"], "ultrareview: out of credits")

        status = self.ok_json("budget", "status", "--json")
        self.assertTrue(status["cloud"]["exhausted"])
        self.assertFalse(status["api"]["exhausted"])

        self.assertEqual(self.ok("budget", "reset", "cloud").stdout.strip(), "cloud: available")
        self.assertIsNone(self.read_json(self.budgets_path())["cloud"]["exhausted_until"])

        self.write_budget("api", -1)                         # a time already past means available
        self.assertIn("api: available", self.ok("budget").stdout)
        p = self.work("budget", "exhaust", "api")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--reason", p.stderr)


# =========================================================================== worktrees
class Worktrees(WorkTest):
    def add_worktree(self, branch):
        path = self.worktree_path(branch)
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, path, "origin/main")
        return path

    def test_lists_ticket_worktrees_with_age_pr_state_and_stale_flag(self):
        wt = self.add_worktree("t/12-x")
        self.git(self.repo, "worktree", "add", "-q", "-b", "elsewhere", os.path.join(self.tmp, "elsewhere"), "main")

        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual((row["path"], row["branch"], row["pr"], row["dirty"]), (wt, "t/12-x", None, False))
        self.assertGreater(row["age_days"], 9.9)
        self.assertTrue(row["stale"])
        self.assertIn(["pr", "list", "--head", "t/12-x", "--state", "all", "--json", "number,state,isDraft",
                       "--limit", "1"], self.gh_calls())
        self.assertFalse(self.ok_json("worktrees", "--stale", "30", "--json")[0]["stale"])
        text = self.ok("worktrees").stdout
        self.assertIn("t/12-x", text)
        self.assertIn("stale", text)

        self.scenario(prs=[{"number": 7, "state": "OPEN", "isDraft": True}])
        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual(row["pr"], {"number": 7, "state": "OPEN", "draft": True})
        self.assertFalse(row["stale"])

        self.scenario(prs=[{"number": 7, "state": "MERGED", "isDraft": False}])
        self.assertTrue(self.ok_json("worktrees", "--json")[0]["stale"])

    def test_remove_refuses_a_dirty_worktree_without_discard(self):
        clean = self.add_worktree("t/12-x")
        dirty = self.add_worktree("t/13-y")
        write(os.path.join(clean, ".env"), "KEY=1\n")       # ignored files do not make a tree dirty
        write(os.path.join(dirty, "README.md"), "changed\n")

        p = self.ok("worktree", "remove", "t/12-x")
        self.assertIn(f"removed worktree {clean}", p.stdout)
        self.assertIn("deleted branch t/12-x", p.stdout)
        self.assertFalse(os.path.exists(clean))

        p = self.work("worktree", "remove", "t/13-y")
        self.assertOneLineError(p, "README.md", "--discard")
        self.assertTrue(os.path.isfile(os.path.join(dirty, "README.md")))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/13-y"), "t/13-y")

        p = self.ok("worktree", "remove", "t/13-y", "--discard")
        self.assertIn("deleted branch t/13-y", p.stdout)
        self.assertFalse(os.path.exists(dirty))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/*"), "")

    def test_remove_refuses_main(self):
        self.assertOneLineError(self.work("worktree", "remove", "main"), "refusing")


if __name__ == "__main__":
    unittest.main()
