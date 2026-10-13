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
        self.assertIn("skip #12: 3 runs ended without a ready pull request in 7 days (last: error at ", p.stdout)
        self.assertIn("run it in this session with /harness:work 12, or wait for the window", p.stdout)
        self.assertNotIn("harness-board status 12 ready", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["plan"], [])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])

        p = self.ok("dispatch")                                # for real: nothing started, the skip said
        self.assertIn("skip #12: 3 runs ended", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 3)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])


IN_PROGRESS = ("task", "status:in-progress", "size:S", "risk:low")


class RedispatchCap(WorkTest):
    """Ticket #29: the redispatch-cap skip names the way forward (finish it in this session), carries its numbers
    in --json, and `runs` marks the capped ticket's newest ended record. The time in the skip line is the last
    run's `ended_at` exactly as the record holds it (ISO-8601 UTC, `...Z`), which is how harness-work prints every
    other time (budget reasons, run records). The cap itself (3 ended runs in 7 days) is unchanged."""

    def ended(self, ticket, outcomes, hours_ago):
        """One ended headless run per outcome, oldest first; hours_ago[i] is when run i ended. Returns the records."""
        records = []
        for i, (outcome, hours) in enumerate(zip(outcomes, hours_ago)):
            records.append(self.write_run(
                ticket, id=f"{self.key}-{ticket}-2026100{i + 1}090000", pid=self.dead_pid(),
                started_at=iso_ago(hours=hours + 1), ended_at=iso_ago(hours=hours), outcome=outcome))
        return records

    def skip_why(self, ticket, runs, last):
        return (f"{runs} runs ended without a ready pull request in 7 days (last: {last['outcome']} at "
                f"{last['ended_at']}); run it in this session with /harness:work {ticket}, or wait for the window")

    def capped_pair(self):
        """#12: three ended runs, the last ok; #15: four, the last billing. Both in progress without a PR."""
        first = self.ended(12, ["error", "error", "ok"], [30, 20, 10])
        second = self.ended(15, ["error", "ok", "error", "billing"], [60, 50, 40, 5])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        return first[-1], second[-1]

    def test_the_skip_names_the_in_session_path_and_the_window(self):
        last12, last15 = self.capped_pair()
        why12, why15 = self.skip_why(12, 3, last12), self.skip_why(15, 4, last15)

        p = self.ok("dispatch", "--dry-run")                   # the dry run prints the new line, per ticket
        self.assertIn("skip #12: " + why12, p.stdout.splitlines())
        self.assertIn("skip #15: " + why15, p.stdout.splitlines())
        self.assertNotIn("would start", p.stdout)
        self.assertNotIn("harness-board status", p.stdout)
        self.assertNotIn("by hand", p.stdout)

        data = self.ok_json("dispatch", "--dry-run", "--json")   # --json carries the numbers and the advice
        self.assertEqual(data["plan"], [])
        by_number = {s["number"]: s for s in data["skipped"]}
        self.assertEqual(sorted(by_number), [12, 15])
        self.assertEqual(by_number[12], {"number": 12, "why": why12, "runs": 3, "window_days": 7,
                                         "advice": "/harness:work 12"})
        self.assertEqual(by_number[15], {"number": 15, "why": why15, "runs": 4, "window_days": 7,
                                         "advice": "/harness:work 15"})

        p = self.ok("dispatch")                                # for real: nothing started, the same lines said
        self.assertIn("skip #12: " + why12, p.stdout.splitlines())
        self.assertIn("skip #15: " + why15, p.stdout.splitlines())
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 7)
        real = self.ok_json("dispatch", "--json")
        self.assertEqual({s["number"]: (s["why"], s["runs"], s["window_days"], s["advice"]) for s in real["skipped"]},
                         {12: (why12, 3, 7, "/harness:work 12"), 15: (why15, 4, 7, "/harness:work 15")})
        self.assertEqual(real["launched"], [])
        self.assertEqual(self.claude_calls(), [])

    def test_the_skip_text_follows_the_last_run_not_the_first(self):
        records = self.ended(12, ["billing", "error", "auth"], [40, 30, 20])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + self.skip_why(12, 3, records[-1]), p.stdout.splitlines())
        for earlier in records[:-1]:
            self.assertNotIn(earlier["ended_at"], p.stdout)

    def test_a_ticket_under_the_cap_is_redispatched_with_no_skip(self):
        self.ended(13, ["error", "ok"], [30, 10])                       # two ended runs
        self.ended(16, ["error", "error", "ok"], [24 * 8, 20, 10])      # three, one outside the 7 days
        self.scenario(issues=[issue(13, labels=IN_PROGRESS, title="Two"),
                              issue(16, labels=IN_PROGRESS, title="Straddling")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Two (again after ok) and launch headless", p.stdout)
        self.assertIn("would start #16 Straddling (again after ok) and launch headless", p.stdout)
        self.assertNotIn("skip #", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["skipped"], [])
        self.assertEqual([row["number"] for row in data["plan"]], [13, 16])

    def test_other_skips_carry_no_cap_fields(self):
        self.ended(12, ["error", "error", "ok"], [30, 20, 10])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13), issue(14)],
                      ready=[ready_row(13, "Second"), ready_row(14, "Third")])
        data = self.ok_json("dispatch", "--dry-run", "--max", "1", "--json")
        by_number = {s["number"]: s for s in data["skipped"]}
        self.assertEqual(by_number[14]["why"], "over --max 1")
        self.assertEqual(sorted(by_number[12]), ["advice", "number", "runs", "why", "window_days"])
        self.assertNotIn("advice", by_number[14])
        self.assertNotIn("runs", by_number[14])

    def row_for(self, text, record):
        [row] = [line for line in text.splitlines() if record["id"] in line]
        return row

    def test_runs_marks_the_capped_ticket(self):
        capped = self.ended(12, ["error", "error", "ok"], [30, 20, 10])      # newest ended run is the third
        also = self.ended(15, ["error", "ok", "error", "billing"], [60, 50, 40, 5])
        two = self.ended(13, ["error", "ok"], [30, 10])                      # under the cap
        straddle = self.ended(16, ["error", "error", "ok"], [24 * 8, 20, 10])  # only two inside the 7 days
        stale = self.ended(17, ["error", "error", "ok"], [24 * 10, 24 * 9, 24 * 8])  # the window has passed
        self.scenario(issues=[issue(n, labels=IN_PROGRESS) for n in (12, 13, 15, 16, 17)])

        text = self.ok("runs", "--all").stdout
        self.assertIn("STATE", text.splitlines()[0])
        self.assertIn("capped", self.row_for(text, capped[-1]))
        self.assertIn("capped", self.row_for(text, also[-1]))
        for record in capped[:-1] + also[:-1] + two + straddle + stale:   # only the newest of a capped ticket
            self.assertNotIn("capped", self.row_for(text, record))
        self.assertEqual(text.count("capped"), 2)
        # the mark is added to the state, it does not replace the outcome
        self.assertIn("ok", self.row_for(text, capped[-1]))
        self.assertIn("billing", self.row_for(text, also[-1]))

        recent = self.ok("runs").stdout                                  # the default 24 h view marks it too
        self.assertIn("capped", self.row_for(recent, capped[-1]))
        self.assertEqual(recent.count("capped"), 2)

    def test_runs_marks_nothing_when_no_ticket_is_capped(self):
        records = self.ended(12, ["error", "ok"], [30, 10]) + self.ended(13, ["ok"], [5])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS)])
        text = self.ok("runs", "--all").stdout
        for record in records:
            self.assertIn(record["id"], text)
        self.assertNotIn("capped", text)

    # ---- what "the cap holds" means: exactly the condition under which dispatch emits the skip
    def run_at(self, ticket, outcome, started, ended, repo=None):
        """One ended headless run of `ticket`, started/ended that many hours ago, in `repo` (default: this one)."""
        self.seq = getattr(self, "seq", 0) + 1
        repo = repo or os.path.realpath(self.repo)
        prefix = self.key if repo == os.path.realpath(self.repo) else sha12(repo)
        return self.write_run(ticket, id=f"{prefix}-{ticket}-20261001{self.seq:06d}", repo=repo,
                              pid=self.dead_pid(), started_at=iso_ago(hours=started), ended_at=iso_ago(hours=ended),
                              outcome=outcome)

    def three(self, ticket, repo=None):
        """Three ended runs inside the window, oldest first."""
        return [self.run_at(ticket, o, s, e, repo) for o, s, e in (("error", 31, 30), ("error", 21, 20),
                                                                    ("ok", 11, 10))]

    def with_prs(self, *branches):
        """Add to the scenario, ahead of its generic `pr list` answer: a pull request for each of these branches."""
        data = self.read_json(self.scenario_path)
        data["calls"] = [{"match": ["pr", "list", "--head", b], "stdout_json": [{"number": 99}]}
                         for b in branches] + data["calls"]
        write(self.scenario_path, json.dumps(data))

    def live_run_of(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return self.write_run(ticket, id=f"{self.key}-{ticket}-20261001999999", pid=live.pid)

    def sibling(self, name="proj2"):
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.origin, path)
        return os.path.realpath(path)

    def marked(self, *extra):
        """{ticket: [ids of its records marked capped]} from `runs --all` (plus `extra` flags), the text table and
        --json both, which must say the same thing."""
        records = {r["id"]: r for r in self.run_records()}
        in_text = {}
        for line in self.ok("runs", "--all", *extra).stdout.splitlines()[1:]:
            ids = [rid for rid in records if rid in line]
            self.assertEqual(len(ids), 1, line)
            if "capped" in line:
                in_text.setdefault(records[ids[0]]["ticket"], []).append(ids[0])
        in_json = {}
        for row in self.ok_json("runs", "--all", *extra, "--json"):
            if row.get("capped"):
                self.assertIs(row["capped"], True)
                in_json.setdefault(row["ticket"], []).append(row["id"])
        self.assertEqual(in_text, in_json)
        return in_text

    def skipped_by_cap(self):
        """(sorted tickets dispatch skips for the cap, the whole dry-run --json)."""
        data = self.ok_json("dispatch", "--dry-run", "--json")
        return sorted(s["number"] for s in data["skipped"] if "advice" in s), data

    def test_a_ticket_with_a_pull_request_is_not_capped_and_not_skipped(self):
        self.three(12)                                       # a pull request exists for t/12-x
        control = self.three(15)                             # none for t/15-x
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.with_prs("t/12-x")
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])
        self.assertNotIn(12, [row["number"] for row in data["plan"]])

    def test_a_ticket_with_a_live_run_is_not_capped_and_not_skipped(self):
        self.three(12)
        live = self.live_run_of(12)
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual(data["live"], [live["id"]])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])

    def test_a_ticket_not_in_progress_or_not_open_is_not_capped_and_not_skipped(self):
        for n in (12, 13, 14):
            self.three(n)
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=("task", "status:ready", "size:S", "risk:low")),
                              issue(13, labels=IN_PROGRESS, state="CLOSED"),
                              issue(14, labels=("task", "status:needs-input", "size:S", "risk:low")),
                              issue(15, labels=IN_PROGRESS, title="Control")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([s["number"] for s in data["skipped"]], [15])

    def test_runs_of_the_same_ticket_number_in_another_repo_do_not_cap_this_repos_ticket(self):
        other = self.sibling()
        self.run_at(12, "error", 31, 30), self.run_at(12, "ok", 21, 20)    # two of ours ...
        self.run_at(12, "error", 11, 10, repo=other)                       # ... and a foreign one, the newest
        self.run_at(13, "error", 51, 50, repo=other)                       # a foreign one, the oldest ...
        self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 21, 20)    # ... and two of ours
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([row["number"] for row in data["plan"]], [12, 13])   # two of ours: redispatched

    def test_the_mark_goes_to_the_latest_started_run_as_dispatch_picks_it(self):
        # #12's runs overlap: the second started before the third but was swept later, so it ENDED newest while the
        # third STARTED newest. Dispatch reads the record that started last (its outcome is "last: ..."); so does `runs`.
        self.run_at(12, "error", 50, 40)
        self.run_at(12, "ok", 45, 2)                                      # newest ended_at
        newest_started = self.run_at(12, "billing", 30, 20)               # newest started_at
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        self.assertEqual(self.marked(), {12: [newest_started["id"]]})
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + self.skip_why(12, 3, newest_started), p.stdout.splitlines())

    def test_runs_json_adds_a_capped_flag_and_leaves_the_state_alone(self):
        records = self.three(12)
        under = [self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 11, 10)]
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS, title="Two")])
        rows = {r["id"]: r for r in self.ok_json("runs", "--all", "--json")}
        self.assertIs(rows[records[-1]["id"]].get("capped"), True)
        self.assertEqual(rows[records[-1]["id"]]["state"], "ended ok")        # the state is not mangled
        for record in records[:-1] + under:
            self.assertFalse(rows[record["id"]].get("capped"), record["id"])
            self.assertNotIn("capped", rows[record["id"]]["state"])
        text = self.ok("runs", "--all").stdout                                  # the table says it in STATE
        self.assertIn("ended ok capped", self.row_for(text, records[-1]))

    def test_runs_and_dispatch_agree_on_which_tickets_are_capped(self):
        other = self.sibling()
        cap12 = self.three(12)
        cap16 = [self.run_at(16, o, s, e) for o, s, e in (("error", 100, 90), ("error", 80, 70),
                                                          ("billing", 60, 50), ("auth", 11, 5))]
        self.three(13)                                                          # has a pull request
        self.three(14)                                                          # and a live run
        self.live_run_of(14)
        self.run_at(17, "error", 31, 30), self.run_at(17, "ok", 21, 20)         # two of ours ...
        self.run_at(17, "error", 11, 10, repo=other)                            # ... and one foreign
        self.run_at(18, "error", 24 * 8 + 1, 24 * 8)                            # one outside the window, two inside
        self.run_at(18, "error", 21, 20), self.run_at(18, "ok", 11, 10)
        for n in (19, 20, 21):
            self.three(n)                                                       # ready, closed, needs-input
        self.run_at(22, "error", 31, 30), self.run_at(22, "ok", 11, 10)         # under the cap
        self.scenario(issues=[
            issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS), issue(14, labels=IN_PROGRESS),
            issue(16, labels=IN_PROGRESS), issue(17, labels=IN_PROGRESS), issue(18, labels=IN_PROGRESS),
            issue(19, labels=("task", "status:ready", "size:S", "risk:low")),
            issue(20, labels=IN_PROGRESS, state="CLOSED"),
            issue(21, labels=("task", "status:needs-input", "size:S", "risk:low")),
            issue(22, labels=IN_PROGRESS)])
        self.with_prs("t/13-x")
        marked = self.marked()
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 16])
        self.assertEqual(sorted(marked), capped)                  # the same tickets, in `runs` and in `dispatch`
        self.assertEqual(marked, {12: [cap12[-1]["id"]], 16: [cap16[-1]["id"]]})   # one mark each: its newest run

    def test_a_stale_latest_run_means_no_skip_and_no_mark_whatever_else_ended_in_the_window(self):
        # #12's latest-started run ended before the 7-day cutoff; three older-started runs of it (overlapping it, swept
        # late) ended inside the window. Dispatch judges the latest record and moves on: no skip, so no mark either.
        for outcome, started, ended in (("error", 24 * 10 + 3, 30), ("error", 24 * 10 + 2, 20), ("ok", 24 * 10 + 1, 10)):
            self.run_at(12, outcome, started, ended)
        self.run_at(12, "error", 24 * 9, 24 * 8)                          # started last, ended outside the window
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])

    def test_a_dead_unended_run_is_judged_by_its_process_after_the_sweep_as_dispatch_does(self):
        # `runs` without --sweep leaves a dead process's record un-ended, so the two can only agree once swept:
        # `runs --sweep` ends it (as dispatch does first) and it counts. #12: two ended runs and a dead un-ended one
        # make three; #13: two ended and a LIVE un-ended one stay under the cap; #15: three ended is the control.
        self.run_at(12, "error", 31, 30), self.run_at(12, "error", 21, 20)
        dead = self.write_run(12, id=f"{self.key}-12-20261001555555", pid=self.dead_pid(), started_at=iso_ago(hours=1))
        write(dead["log"], "claude exited before printing a result\n")
        self.run_at(13, "error", 31, 30), self.run_at(13, "error", 21, 20)
        live = self.live_run_of(13)
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        marked = self.marked("--sweep")
        self.assertEqual(marked, {12: [dead["id"]], 15: [control[-1]["id"]]})   # the swept run is #12's newest
        swept = {r["id"]: r for r in self.run_records()}
        self.assertTrue(swept[dead["id"]]["ended_at"])
        self.assertIsNone(swept[live["id"]]["ended_at"])
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 15])
        self.assertEqual(sorted(marked), capped)
        self.assertEqual(data["live"], [live["id"]])

    def test_a_ticket_whose_gh_check_fails_is_neither_skipped_nor_marked(self):
        # dispatch warns and moves on when it cannot tell whether #12 (pr list) or #13 (issue view) still lacks a pull
        # request; `runs` must not claim they are capped, and must not crash either. #15 is the control.
        self.three(12), self.three(13)
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        data = self.read_json(self.scenario_path)
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        data["calls"] = [dict(gone, contains=["pr", "list", "--head", "t/12-x"]),
                         dict(gone, match=["issue", "view", "13"])] + data["calls"]
        write(self.scenario_path, json.dumps(data))
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([row["number"] for row in data["plan"]], [])
        p = self.ok("dispatch", "--dry-run")
        for n in (12, 13):
            self.assertIn(f"#{n}: cannot check whether its ended run needs another", p.stderr)
            self.assertNotIn(f"skip #{n}:", p.stdout)

    def asked_about(self, ticket):
        """The gh calls that look the ticket up: its `issue view` and its branch's `pr list`."""
        return [c for c in self.gh_calls()
                if c[:3] == ["issue", "view", str(ticket)] or (c[:2] == ["pr", "list"] and f"t/{ticket}-x" in c)]

    def test_runs_asks_gh_only_about_tickets_at_the_cap(self):
        # #13 (two ended runs) and #16 (three, one outside the window: two inside) are under the cap, so `runs`
        # has nothing to mark for them and must not spend gh calls on them; #12 is the capped control.
        self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 11, 10)
        self.run_at(16, "error", 24 * 8 + 1, 24 * 8), self.run_at(16, "error", 21, 20), self.run_at(16, "ok", 11, 10)
        control = self.three(12)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS, title="Two"),
                              issue(16, labels=IN_PROGRESS, title="Straddling")])
        self.assertEqual(self.marked(), {12: [control[-1]["id"]]})
        self.assertTrue(self.asked_about(12))                       # the capped ticket is still checked
        self.assertEqual(self.asked_about(13), [])
        self.assertEqual(self.asked_about(16), [])

        os.unlink(self.gh_log)                                      # dispatch still asks about them: candidates
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [13, 16])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])
        self.assertTrue(self.asked_about(13))
        self.assertTrue(self.asked_about(16))

    def test_runs_outside_a_repository_lists_the_records_marks_nothing_and_asks_gh_nothing(self):
        records = self.three(12)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        plain = self.mk("not-a-repo")
        self.assertNotEqual(subprocess.run([GIT, "-C", plain, "rev-parse", "--git-dir"], env=self.env(),
                                           capture_output=True).returncode, 0)
        p = self.work("runs", "--all", cwd=plain)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for record in records:
            self.assertIn(record["id"], p.stdout)
        self.assertNotIn("capped", p.stdout)
        rows = json.loads(self.work("runs", "--all", "--json", cwd=plain).stdout)
        self.assertEqual(sorted(r["id"] for r in rows), sorted(r["id"] for r in records))
        self.assertFalse([r for r in rows if r.get("capped")])
        self.assertEqual(self.gh_calls(), [])
        # the same records, from inside the repository, are marked: the difference is the cwd alone
        self.assertEqual(self.marked(), {12: [records[-1]["id"]]})


def sha12(path):
    """A path string's key, from first principles: the first 12 hex digits of its sha1."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


class DispatchRunsByRepoField(WorkTest):
    """Which run records dispatch counts as this repository's (ticket #7): a record whose `repo` field is
    another spelling of the main tree is ours even when its id carries another key (written before the
    key was canonical); a record of another repository is not ours, even when its path shares a prefix
    with ours or differs from it only in case. Every record here has an id that does NOT start with this
    repository's key, so only the repo field can decide."""

    def spelled_repo(self):
        """The main checkout in another case spelling; the test is skipped where that is another path."""
        spelled = os.path.join(self.tmp, "PROJ")
        if not (os.path.exists(spelled) and os.path.samefile(spelled, self.repo)):
            self.skipTest("two spellings of one path need a case-insensitive file system")
        self.assertNotEqual(sha12(spelled), self.key)
        return spelled

    def sibling(self, name="proj2"):
        """Another repository next to ours whose path starts with ours."""
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.origin, path)
        self.assertNotEqual(sha12(path), self.key)
        return path

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def live_run(self, ticket, repo):
        rid = f"{sha12(repo)}-{ticket}-20261013110000"
        return self.write_run(ticket, id=rid, repo=repo, pid=self.sleeper(ticket),
                              worktree=os.path.join(repo, ".claude", "worktrees", f"t-{ticket}-x"))

    def ended_runs(self, ticket, repo, count=3):
        for i in range(count):
            hours = count - i
            self.write_run(ticket, id=f"{sha12(repo)}-{ticket}-202610130{i}0000", repo=repo, pid=self.dead_pid(),
                           started_at=iso_ago(hours=hours, minutes=30), ended_at=iso_ago(hours=hours),
                           outcome="error")

    def test_a_live_run_recorded_in_another_spelling_is_this_repos_and_takes_a_slot(self):
        spelled = self.spelled_repo()
        other = self.sibling()
        ours = self.live_run(13, spelled)
        theirs = self.live_run(14, other)
        self.scenario(issues=[issue(), issue(13), issue(14), issue(15)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third"),
                             ready_row(15, "Fourth")])
        p = self.ok("dispatch", "--dry-run", "--max", "3")
        self.assertIn("venue headless, 1 live, 2 of 3 slots free", p.stdout)
        self.assertIn(f"skip #13: live run {ours['id']}", p.stdout)
        self.assertNotIn("would start #13", p.stdout)
        # the sibling's live run on #14 is not ours: #14 is planned here, and the sibling's run is not named
        self.assertIn("would start #12 Add the board tool (tier sonnet)", p.stdout)
        self.assertIn("would start #14 Third (tier sonnet)", p.stdout)
        self.assertNotIn(theirs["id"], p.stdout)
        self.assertIn("skip #15: over --max 3", p.stdout)

        data = self.ok_json("dispatch", "--dry-run", "--max", "3", "--json")
        self.assertEqual(data["live"], [ours["id"]])
        self.assertEqual([row["number"] for row in data["plan"]], [12, 14])
        self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                         [(13, f"live run {ours['id']}"), (15, "over --max 3")])

    def test_the_cap_counts_only_this_repos_live_runs_in_any_spelling(self):
        spelled = self.spelled_repo()
        first, second = self.live_run(13, spelled), self.live_run(14, self.repo + os.sep)
        for name in ("proj2", "proj-old"):                     # two live runs elsewhere: not ours
            self.live_run(20 if name == "proj2" else 21, self.sibling(name))
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        p = self.ok("dispatch", "--max", "2")                  # for real: both slots are taken by our runs
        self.assertIn("2 live runs; --max 2 reached, nothing launched.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        data = self.ok_json("dispatch", "--dry-run", "--max", "3", "--json")
        self.assertEqual(sorted(data["live"]), sorted([first["id"], second["id"]]))
        self.assertEqual([row["number"] for row in data["plan"]], [12])

    def test_ended_runs_recorded_in_another_spelling_count_toward_the_redispatch_cap(self):
        spelled = self.spelled_repo()
        other = self.sibling()
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.ended_runs(12, spelled)                            # ours: three ended, the fourth is skipped
        self.ended_runs(14, other)                              # the sibling's: never ours to redispatch or cap
        self.scenario(issues=[issue(labels=in_progress), issue(14, labels=in_progress, title="Theirs")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: 3 runs ended without a ready pull request in 7 days (last: error at ", p.stdout)
        self.assertIn("run it in this session with /harness:work 12, or wait for the window", p.stdout)
        self.assertNotIn("would start #12", p.stdout)
        self.assertNotIn("#14", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)

    def test_two_ended_runs_in_another_spelling_redispatch_the_ticket(self):
        spelled = self.spelled_repo()
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.ended_runs(12, spelled, count=2)
        self.scenario(issues=[issue(labels=in_progress)])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Add the board tool (again after error) and launch headless", p.stdout)

    def test_a_run_of_a_directory_differing_only_in_case_is_not_ours_on_a_case_sensitive_file_system(self):
        upper = os.path.join(self.tmp, "PROJ")
        if os.path.exists(upper):
            self.skipTest("PROJ is another spelling of proj on this (case-insensitive) file system")
        self.git(self.tmp, "clone", "-q", self.origin, upper)
        theirs = self.live_run(13, upper)
        self.ended_runs(12, upper)
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.scenario(issues=[issue(labels=in_progress), issue(13)], ready=[ready_row(13, "Second")])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["live"], [])
        self.assertEqual([row["number"] for row in data["plan"]], [13])
        self.assertEqual(data["skipped"], [])
        self.assertNotIn(theirs["id"], json.dumps(data))


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
            # An explicit 429 or 5xx stays transient even when the text names the helper or the workspace header.
            dict(API_401, api_error_status=503, result=API_401_HELPER["result"].replace("401", "503", 1)),
            dict(API_401, api_error_status=429, result=API_400_WORKSPACE["result"].replace("400", "429", 1)),
            # Other 4xx: only the text tells auth from error, and these name neither key, helper nor header.
            dict(API_401, api_error_status=400,
                 result="API Error: 400 prompt is too long: 212000 tokens > 200000 maximum"),
            dict(API_401, api_error_status=400,
                 result="API Error: 400 messages.0.content: Input should be a valid list"),
            dict(API_401, api_error_status=404, result="API Error: 404 model: claude-foo not found"),
            dict(API_401, api_error_status=413, result="API Error: 413 Request too large"),
            dict(API_401, api_error_status=400,
                 result="API Error: 400 invalid_request_error: tool_use ids must be unique"),
            dict(API_401, api_error_status=408, result="API Error: 408 Request timeout"),
            dict(API_401, api_error_status=409, result="API Error: 409 Conflict"),
            # terminal_reason api_error without a status, and nothing about a key in the text.
            {k: v for k, v in dict(API_401, result="API Error: Connection error.").items()
             if k != "api_error_status"},
            {k: v for k, v in dict(API_401, result="API Error: Request timed out.").items()
             if k != "api_error_status"},
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
        self.assertEqual(self.sweep_one(50, ok)["outcome"], "ok")
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_sweep_reads_key_and_workspace_in_their_credential_sense_only(self):
        # The words "key" and "workspace" on their own decide nothing. A 4xx about an unexpected key in the request
        # schema, a missing workspace file or a keychain read is `error`; the API key named as the credential, the
        # apiKeyHelper or the anthropic-workspace-id header is `auth`. Interleaved and worded unlike the real probes, so
        # neither a substring match on either word nor a list of the probes' phrases passes.
        no_status = {k: v for k, v in API_401.items() if k != "api_error_status"}   # terminal_reason api_error only
        cases = [
            # (result, outcome, the start of the api budget's reason when auth, a fragment of the text it must carry)
            (dict(API_401, api_error_status=400, result="API Error: 400 tools.0.input_schema: unexpected key 'foo'"),
             "error", None, None),
            (dict(API_401, api_error_status=400, result="API Error: 400 The provided API key has been disabled"),
             "auth", "auth: API error 400: ", "has been disabled"),
            (dict(API_401, api_error_status=400, result="API Error: 400 metadata: key user_id must be a string"),
             "error", None, None),
            (dict(API_401, api_error_status=404, result="API Error: 404 workspace file not found: /tmp/x"),
             "error", None, None),
            (dict(API_401, api_error_status=404, result="API Error: 404 No API key found for this organization"),
             "auth", "auth: API error 404: ", "No API key found"),
            (dict(API_401, api_error_status=422,
                  result="API Error: 422 The anthropic-workspace-id header names a deleted workspace"),
             "auth", "auth: API error 422: ", "names a deleted workspace"),
            (dict(API_401, api_error_status=400, result="API Error: 400 cache_control: unexpected key 'ttl'"),
             "error", None, None),
            # terminal_reason api_error without a status: the same words, the same split.
            (dict(no_status, result="API Error: Connection error while reading keychain"), "error", None, None),
            (dict(no_status, result="API Error: 400 Unexpected key 'workspace' in request body"), "error", None, None),
            (dict(no_status, result="apiKeyHelper script is failing: produced no output · Fix the apiKeyHelper "
                                    "setting"),
             "auth", "auth: API error: apiKeyHelper script is failing", "produced no output"),
        ]
        for n, (result, outcome, prefix, fragment) in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                self.ok("budget", "reset", "api")
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], outcome)
                self.assertEqual(next(r for r in self.run_records() if r["ticket"] == n)["outcome"], outcome)
                status = self.ok("budget", "status").stdout
                if outcome == "error":
                    self.assertIn("api: available", status)
                    continue
                self.assertIn("api: exhausted until", status)
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(prefix), api["reason"])
                self.assertIn(fragment, api["reason"])

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


# =========================================================================== runs: ended early
# The result of the headless run of #7 (2026-10-13): the orchestrator launched the test-author in the background and
# ended its turn; `claude -p` has no next turn, so the process exited `ok` with no red record and no PR.
EARLY_RESULT = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "duration_ms": 70214,
                "result": "I've started ticket #7 and the test-author is writing the tests now in the background. "
                          "Next comes the red check, then the breaker and the coder.",
                "total_cost_usd": 0.41, "stop_reason": "end_turn"}
# The result of the headless run of #31 (2026-10-13 20:43Z, #42): it ended mid-review with the round-2 fix uncommitted
# in the worktree and PR #41 still a draft.
ROUND2_RESULT = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 5520143,
                 "result": "The coder is on fix round 2; waiting for its report.", "stop_reason": "end_turn"}
IN_PROGRESS = ("task", "status:in-progress", "size:S", "risk:low")


class EndedEarly(WorkTest):
    def write_scenario(self, tickets, branches=None, states=None, failing=None):
        """tickets: {n: (labels, open_prs, all_prs)}. `pr list --head <branch>` (t/<n>-x unless branches[n] says
        otherwise) gives open_prs; with --state all it gives all_prs (merged and closed included), as real gh does.
        Any other pr list gives []. states[n] is the issue's state when it is not OPEN. failing[n] names the gh call
        that fails for ticket n ("issue": `issue view n`; "pr": every `pr list --head <its branch>`): exit 1 with a
        message on stderr and nothing on stdout, as gh does when the network or the token is gone."""
        branches, states, failing = branches or {}, states or {}, failing or {}
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        calls = []
        for n, (labels, open_prs, all_prs) in tickets.items():
            if failing.get(n) == "issue":
                calls.append(dict(gone, match=["issue", "view", str(n)]))
            else:
                calls.append({"match": ["issue", "view", str(n)],
                              "stdout_json": issue(n, labels=labels, state=states.get(n, "OPEN"))})
        for n, (labels, open_prs, all_prs) in tickets.items():
            branch = branches.get(n, f"t/{n}-x")
            if failing.get(n) == "pr":
                calls.append(dict(gone, contains=["pr", "list", "--head", branch]))
                continue
            calls.append({"contains": ["pr", "list", "--head", branch, "--state", "all"], "stdout_json": all_prs})
            calls.append({"contains": ["pr", "list", "--head", branch], "stdout_json": open_prs})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": []},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def ended_run(self, n, result, warning=True, **fields):
        """A headless run of ticket n whose process is gone, with `result` (a dict, or raw text) as its log, after the
        connectors warning unless warning=False."""
        record = self.write_run(n, pid=self.dead_pid(), **fields)
        text = json.dumps(result) if isinstance(result, dict) else result
        write(record["log"], (CONNECTORS_WARNING + "\n" if warning else "") + text + "\n")
        return record

    def fresh(self):
        """Forget every run record and gh call, for the next pass of a test."""
        shutil.rmtree(self.runs_dir(), True)
        if os.path.exists(self.gh_log):
            os.unlink(self.gh_log)

    def run_lines(self):
        """{ticket: its line} of `harness-work runs` (no sweep)."""
        return {int(m.group(1)): line for line in self.ok("runs").stdout.splitlines()
                for m in [re.search(r"proj #(\d+)\b", line)] if m}

    def sweep_pair(self, **scenario):
        """Two ended runs with the same log, both tickets in progress with no PR; the scenario keywords make #8
        differ. Returns {ticket: row} of the sweep after checking that both runs were ended."""
        for n in (7, 8):
            self.ended_run(n, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, [], [])}, **scenario)
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual(sorted(rows), [7, 8])
        for n in (7, 8):
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertIsNone(rows[n]["exit"])
        return rows

    def assertEarlyAndOk(self, rows):
        """#7 ended early, #8 ended ok: in the sweep's rows, the saved records and the runs listing."""
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {7: "early", 8: "ok"})
        self.assertEqual((rows[7]["state"], rows[8]["state"]), ("ended early (no PR)", "ended ok"))
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {7: "early", 8: "ok"})
        lines = self.run_lines()
        self.assertIn("ended early (no PR)", lines[7])
        self.assertIn("ended ok", lines[8])
        self.assertNotIn("early", lines[8])
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_a_closed_issue_still_labelled_in_progress_is_ok_not_early(self):
        # Same labels, same log, no PR either way: only the issue's state differs. A closed ticket is never worked
        # again (the redispatch skips issues that are not OPEN), so its run did not end early.
        self.assertEarlyAndOk(self.sweep_pair(states={8: "CLOSED"}))

    def test_a_failing_issue_view_leaves_the_run_ok(self):
        # gh cannot say what the board holds for #8: the sweep still ends its run, as ok (not early: the facts that
        # make early are missing), exits 0, and classifies #7 all the same. A warning on stderr is fine.
        self.assertEarlyAndOk(self.sweep_pair(failing={8: "issue"}))

    def test_a_failing_pr_list_leaves_the_run_ok(self):
        # The board says in-progress, but gh cannot list the branch's PRs: the same, ok, exit 0, #7 still early.
        self.assertEarlyAndOk(self.sweep_pair(failing={8: "pr"}))

    def test_ok_run_on_an_in_progress_ticket_without_a_pr_is_ended_early(self):
        record = self.ended_run(7, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], [])})

        text = self.ok("runs", "--sweep").stdout
        self.assertIn("proj #7", text)
        self.assertIn("ended early (no PR)", text)
        self.assertNotIn("ended ok", text)

        [saved] = self.run_records()
        self.assertEqual((saved["id"], saved["outcome"], saved["exit"]), (record["id"], "early", None))
        self.assertTrue(saved["ended_at"])

        [row] = self.ok_json("runs", "--json")             # no sweep: the saved outcome alone gives the state
        self.assertEqual((row["outcome"], row["state"]), ("early", "ended early (no PR)"))
        self.assertIn("ended early (no PR)", self.ok("runs").stdout)
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

        # The redispatch says why it runs the ticket again.
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #7 Add the board tool (again after early)", p.stdout)

    def test_sweep_json_reports_early_on_the_sweep_itself(self):
        self.ended_run(7, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], [])})
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual((row["ticket"], row["outcome"], row["state"]), (7, "early", "ended early (no PR)"))
        self.assertTrue(row["ended_at"])

    def test_sweep_labels_early_only_for_ok_runs_on_in_progress_tickets_without_any_pr(self):
        own_words = {k: v for k, v in API_401.items() if k not in ("terminal_reason", "api_error_status")}
        own_words["result"] = "I could not finish: the tests do not import."
        pr_open = [{"number": 40, "state": "OPEN", "isDraft": True}]
        pr_merged = [{"number": 41, "state": "MERGED", "isDraft": False}]
        # n: (log result, labels, open PRs, PRs of any state, outcome)
        cases = {
            12: (EARLY_RESULT, IN_PROGRESS, [], [], "early"),
            13: ({"type": "result", "is_error": False, "result": "PR #31 is ready for review."},
                 IN_PROGRESS, [], [], "early"),                           # the text claims a PR; there is none
            14: (EARLY_RESULT, IN_PROGRESS, pr_open, pr_open, "early"),   # only a draft PR: still early (#42)
            15: (EARLY_RESULT, IN_PROGRESS, [], pr_merged, "ok"),          # a merged PR counts (--state all)
            16: (EARLY_RESULT, ("task", "status:in-review", "size:S", "risk:low"), [], [], "ok"),
            17: (EARLY_RESULT, ("task", "status:needs-input", "size:S", "risk:low"), [], [], "ok"),
            18: (EARLY_RESULT, ("task", "status:ready", "size:S", "risk:low"), [], [], "ok"),
            19: ("Traceback: something else broke", IN_PROGRESS, [], [], "error"),
            20: (dict(API_401, api_error_status=402), IN_PROGRESS, [], [], "billing"),
            21: (API_401, IN_PROGRESS, [], [], "auth"),
            22: (own_words, IN_PROGRESS, [], [], "error"),
            23: ({"type": "result", "is_error": False, "result": "Done."}, IN_PROGRESS, [], [], "early"),
            24: (EARLY_RESULT, IN_PROGRESS, [], [{"number": 42, "state": "CLOSED", "isDraft": False}], "ok"),
            25: (EARLY_RESULT, IN_PROGRESS, [], [], "ok"),                # the issue itself is CLOSED (below)
        }
        for n, (result, *_rest) in cases.items():
            self.ended_run(n, result)
        self.write_scenario({n: (labels, open_prs, all_prs) for n, (_r, labels, open_prs, all_prs, _o)
                             in cases.items()}, states={25: "CLOSED"})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        expected = {n: c[-1] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        saved = {r["ticket"]: r["outcome"] for r in self.run_records()}
        self.assertEqual(saved, expected)
        states = {n: ("ended early (draft PR #40)" if n == 14 else "ended early (no PR)") if outcome == "early"
                  else f"ended {outcome}" for n, outcome in expected.items()}
        for n, state in states.items():
            with self.subTest(ticket=n):
                self.assertEqual(rows[n]["state"], state)

        lines = self.run_lines()
        for n, outcome in expected.items():
            with self.subTest(ticket=n, text=True):
                self.assertEqual("ended early" in lines[n], outcome == "early", lines[n])
                if outcome == "early":
                    self.assertIn(states[n], lines[n])

    def test_only_the_board_and_the_pr_decide_early_never_the_ticket_number_or_the_log(self):
        # Every ticket goes through every label, issue and PR state across the passes (a rotation), each run's log is
        # one of three byte-identical texts whatever its state (with and without the connectors warning), and each
        # branch has a slug the ticket number does not give. So no list of ticket numbers, no reading of the log and
        # no branch built from the number can tell early from ok: only `issue view` (labels and state) and
        # `pr list --head <branch> --state all`.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        states = [   # (labels, open PRs, PRs of any state, issue state, outcome)
            (IN_PROGRESS, [], [], "OPEN", "early"),
            (IN_PROGRESS, [{"number": 60, "state": "OPEN", "isDraft": True}],     # only a draft: early (#42)
             [{"number": 60, "state": "OPEN", "isDraft": True}], "OPEN", "early"),
            (IN_PROGRESS, [{"number": 61, "state": "OPEN", "isDraft": False}],
             [{"number": 61, "state": "OPEN", "isDraft": False}], "OPEN", "ok"),
            (IN_PROGRESS, [], [{"number": 62, "state": "MERGED", "isDraft": False}], "OPEN", "ok"),
            (IN_PROGRESS, [], [{"number": 63, "state": "CLOSED", "isDraft": False}], "OPEN", "ok"),  # closed unmerged
            (IN_PROGRESS, [], [], "CLOSED", "ok"),                        # the issue is closed, label or not
            (in_review, [], [], "OPEN", "ok"),
            (("task", "status:needs-input", "size:S", "risk:low"), [], [], "OPEN", "ok"),
            (("task", "status:ready", "size:S", "risk:low"), [], [], "OPEN", "ok"),
        ]
        logs = [(EARLY_RESULT, True), (EARLY_RESULT, False),
                ({"type": "result", "is_error": False, "result": "PR #7 ready"}, False)]
        tickets = [7, 12, 13, 23, 58, 104, 311, 9, 77]
        branches = {n: f"t/{n}-sweep-{chr(97 + i)}{n * 7}" for i, n in enumerate(tickets)}
        for k in range(len(states)):
            self.fresh()
            plan = {n: states[(j + k) % len(states)] for j, n in enumerate(tickets)}
            for j, n in enumerate(tickets):
                result, warning = logs[(j + 2 * k) % len(logs)]
                self.ended_run(n, result, warning=warning, branch=branches[n],
                               worktree=os.path.join(self.repo, ".claude", "worktrees", branches[n].replace("/", "-")))
            self.write_scenario({n: s[:3] for n, s in plan.items()}, branches=branches,
                                states={n: s[3] for n, s in plan.items() if s[3] != "OPEN"})

            rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
            expected = {n: s[4] for n, s in plan.items()}
            with self.subTest(rotation=k):
                self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
                self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, expected)
                for n, outcome in expected.items():
                    early = "ended early (draft PR #60)" if plan[n][1] else "ended early (no PR)"
                    self.assertEqual(rows[n]["state"], early if outcome == "early" else "ended ok")
                # Early needs both facts: the ticket's labels and every PR of its branch, merged and closed included.
                calls = self.gh_calls()
                for n in (n for n, o in expected.items() if o == "early"):
                    self.assertTrue(any(a[:3] == ["issue", "view", str(n)] for a in calls), (n, calls))
                    self.assertTrue(any(a[:2] == ["pr", "list"] and "--head" in a
                                        and a[a.index("--head") + 1] == branches[n]
                                        and "--state" in a and a[a.index("--state") + 1] == "all"
                                        for a in calls), (n, calls))

    def test_a_run_without_a_repo_stays_ok_and_asks_gh_nothing(self):
        # A record with no repo (the key absent, empty or null) cannot say which repository's issue it ran: the sweep
        # must not read issue N of whatever repository it runs in (here proj, where #7..#9 are in progress with no
        # PR, so a guess would label them early). The control #10 has its repo and ends early as before. The ids
        # carry a key no repository here has, so nothing can tie the records to proj.
        for n, repo in ((7, "absent"), (8, ""), (9, None)):
            record = self.ended_run(n, EARLY_RESULT, id=f"0123456789ab-{n}-20261013120000")
            if repo == "absent":
                record.pop("repo")
            else:
                record["repo"] = repo
            write(os.path.join(self.runs_dir(), record["id"] + ".json"), json.dumps(record))
        self.ended_run(10, EARLY_RESULT)
        self.write_scenario({n: (IN_PROGRESS, [], []) for n in (7, 8, 9, 10)})

        p = self.work("runs", "--sweep", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {7: "ok", 8: "ok", 9: "ok", 10: "early"})
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {7: "ok", 8: "ok", 9: "ok", 10: "early"})
        for n in (7, 8, 9):
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertEqual(rows[n]["state"], "ended ok")
        calls = self.gh_calls()
        for n in (7, 8, 9):
            with self.subTest(ticket=n):
                self.assertFalse([a for a in calls if a[:3] == ["issue", "view", str(n)]], calls)
                self.assertFalse([a for a in calls if a[:2] == ["pr", "list"] and f"t/{n}-x" in a], calls)
        self.assertTrue([a for a in calls if a[:3] == ["issue", "view", "10"]], calls)

    def test_dispatch_asks_gh_about_a_freshly_ended_run_once(self):
        # Three headless runs ended ok since the last sweep: #7 in progress with no PR (early, dispatched again), #8 in
        # progress with an open ready PR, #9 in review. One dispatch closes them (the sweep) and picks what to run again
        # (the redispatch); both need `issue view` and `pr list --head` for the same ticket and branch, and the
        # answer is the same, so each is asked at most once per ticket in one invocation.
        pr_open = [{"number": 40, "state": "OPEN", "isDraft": False}]
        for n in (7, 8, 9):
            self.ended_run(n, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, pr_open, pr_open),
                             9: (("task", "status:in-review", "size:S", "risk:low"), [], [])})

        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #7 Add the board tool (again after early)", p.stdout)
        self.assertNotIn("would start #8", p.stdout)
        self.assertNotIn("would start #9", p.stdout)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {7: "early", 8: "ok", 9: "ok"})
        calls = self.gh_calls()
        for n in (7, 8, 9):
            with self.subTest(ticket=n):
                views = [a for a in calls if a[:3] == ["issue", "view", str(n)]]
                heads = [a for a in calls if a[:2] == ["pr", "list"] and "--head" in a
                         and a[a.index("--head") + 1] == f"t/{n}-x"]
                self.assertLessEqual(len(views), 1, calls)
                self.assertLessEqual(len(heads), 1, calls)
                if n == 7:                                  # early needs both facts: asked exactly once each
                    self.assertEqual((len(views), len(heads)), (1, 1), calls)

    # ---- #42: a draft pull request is not a ready one
    def test_a_draft_pull_request_does_not_make_an_early_end_ok(self):
        # The run of #31 on 2026-10-13 ended `success` mid-review with its fix uncommitted and PR #41 still a draft.
        # Several tickets, each with its own draft PR number (and two different logs), so neither the PR number
        # nor the ticket can be a constant; #8 has no PR at all (the old early), #12 a ready PR (ok).
        cases = {   # n: (log, open PRs = PRs of any state, outcome, state)
            7: (ROUND2_RESULT, draft(41), "early", "ended early (draft PR #41)"),
            13: (EARLY_RESULT, draft(58), "early", "ended early (draft PR #58)"),
            31: (ROUND2_RESULT, draft(9), "early", "ended early (draft PR #9)"),
            8: (ROUND2_RESULT, [], "early", "ended early (no PR)"),
            12: (ROUND2_RESULT, ready_pr(77), "ok", "ended ok"),
        }
        for n, (result, *_rest) in cases.items():
            self.ended_run(n, result)
        self.write_scenario({n: (IN_PROGRESS, prs, prs) for n, (_r, prs, _o, _s) in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({n: rows[n]["state"] for n in rows}, {n: c[3] for n, c in cases.items()})
        for n in cases:
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertIsNone(rows[n]["exit"])
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {n: c[2] for n, c in cases.items()})

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")},
                         {n: c[3] for n, c in cases.items()})
        lines = self.run_lines()
        for n, (_r, prs, outcome, state) in cases.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                for other in (c[1][0]["number"] for m, c in cases.items() if m != n and c[1]):
                    self.assertNotIn(f"PR #{other})", lines[n])
        self.assertNotIn("draft", lines[8] + lines[12])
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

    def test_a_ready_pull_request_or_a_waiting_ticket_is_a_normal_end(self):
        # The same log for every run; only the board and the branch's PRs differ. A ready PR, a ticket the run moved
        # to in-review or needs-input (draft or not), a merged PR and a closed issue are normal ends: `ok`, never
        # dispatched again. #26 (in progress, draft only) is the control that does end early.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        needs_input = ("task", "status:needs-input", "size:S", "risk:low")
        merged = [{"number": 74, "state": "MERGED", "isDraft": False}]
        cases = {   # n: (labels, open PRs, PRs of any state, outcome)
            20: (IN_PROGRESS, ready_pr(70), ready_pr(70), "ok"),
            21: (in_review, draft(71), draft(71), "ok"),
            22: (needs_input, draft(72), draft(72), "ok"),
            23: (in_review, ready_pr(73), ready_pr(73), "ok"),
            24: (IN_PROGRESS, [], merged, "ok"),
            25: (IN_PROGRESS, draft(75), draft(75), "ok"),             # the issue is CLOSED (below)
            27: (needs_input, [], [], "ok"),
            26: (IN_PROGRESS, draft(76), draft(76), "early"),
        }
        for n in cases:
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({n: c[:3] for n, c in cases.items()}, states={25: "CLOSED"})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        expected = {n: c[3] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, expected)
        lines = self.run_lines()
        for n, outcome in expected.items():
            with self.subTest(ticket=n):
                if outcome == "ok":
                    self.assertEqual(rows[n]["state"], "ended ok")
                    self.assertNotIn("early", lines[n])
        self.assertEqual(rows[26]["state"], "ended early (draft PR #76)")

        p = self.ok("dispatch", "--dry-run")
        started = [line for line in p.stdout.splitlines() if line.startswith("would start #")]
        self.assertEqual(len(started), 1, p.stdout)
        self.assertTrue(started[0].startswith("would start #26 "), p.stdout)
        self.assertIn("#76", started[0])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [26])
        self.assertEqual(data["skipped"], [])


def draft(number):
    """`pr list` rows: one open draft pull request."""
    return [{"number": number, "state": "OPEN", "isDraft": True}]


def ready_pr(number):
    """`pr list` rows: one open pull request marked ready for review."""
    return [{"number": number, "state": "OPEN", "isDraft": False}]


# =========================================================================== #42: redispatch of a draft-PR run
class RedispatchDraft(WorkTest):
    """A run that ended with its ticket in progress and only a draft PR is dispatched again: the plan's `again`
    reason, the start comment and the prompt name the PR, and the prompt says the worktree may hold uncommitted
    work. The cap (3 ended runs in 7 days) holds for it, with the "ended without a ready pull request" line."""

    def setUp(self):
        super().setUp()
        # The ticket branch exists, pushed, with its worktree and the previous run's uncommitted fix in it.
        self.wt = self.worktree_path()
        self.git(self.repo, "worktree", "add", "-q", "-b", BRANCH, self.wt, "main")
        write(os.path.join(self.wt, "board.py"), "def ready():\n    return []\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Round 1")
        self.git(self.wt, "push", "-q", "-u", "origin", BRANCH)
        self.dirty = write(os.path.join(self.wt, "board.py"), "def ready():\n    return ['round 2']\n")

    def write_scenario(self, drafts):
        """drafts: {ticket: (branch, its draft PR number)}; every ticket OPEN and status:in-progress."""
        calls = [{"match": ["issue", "view", str(n)],
                  "stdout_json": issue(n, labels=IN_PROGRESS, title=TITLE if n == 12 else f"Ticket {n}")}
                 for n in drafts]
        for n, (branch, pr) in drafts.items():
            calls.append({"contains": ["pr", "list", "--head", branch], "stdout_json": draft(pr)})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": []},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def ended_run(self, n, branch, worktree, result=None, **fields):
        record = self.write_run(n, pid=self.dead_pid(), branch=branch, worktree=worktree, **fields)
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result or ROUND2_RESULT) + "\n")
        return record

    def comment_bodies(self, n):
        """The text of every `gh issue comment n` call: its --body, else what came on stdin (--body-file -)."""
        bodies = []
        with open(self.gh_log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                argv = entry["argv"]
                if argv[:3] == ["issue", "comment", str(n)]:
                    bodies.append(argv[argv.index("--body") + 1] if "--body" in argv else entry.get("stdin") or "")
        return bodies

    def test_dispatch_picks_the_ticket_up_again_and_names_the_pull_request(self):
        other = os.path.join(self.repo, ".claude", "worktrees", "t-15-x")
        self.ended_run(12, BRANCH, self.wt)                    # the #31 case: draft PR #41, fix uncommitted
        self.ended_run(15, "t/15-x", other, result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", 58)})

        # Dry run: both tickets come back, each `again` reason naming its own draft PR.
        p = self.ok("dispatch", "--dry-run")
        lines = p.stdout.splitlines()
        starts = {n: [ln for ln in lines if ln.startswith(f"would start #{n} ")] for n in (12, 15)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {12: 1, 15: 1}, p.stdout)
        [line12], [line15] = starts[12], starts[15]
        self.assertIn("again after early", line12)
        self.assertIn("#41", line12)
        self.assertNotIn("#58", line12)
        self.assertIn("again after early", line15)
        self.assertIn("#58", line15)
        self.assertNotIn("#41", line15)
        self.assertNotIn("No ready tickets.", p.stdout)
        self.assertNotIn("skip #", p.stdout)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {12: "early", 15: "early"})
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [12, 15])
        self.assertEqual(self.claude_calls(), [])

        # For real, one slot: #12 is launched in its existing worktree, the uncommitted work left alone.
        p = self.ok("dispatch", "--max", "1")
        self.assertIn(f"#12 headless on {BRANCH} (pid ", p.stdout)
        launched = [r for r in self.run_records() if r.get("ended_at") is None]
        self.assertEqual([r["ticket"] for r in launched], [12])
        self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(launched[0]["log"])))
        self.assertTrue(wait_for(lambda: pid_gone(launched[0]["pid"])))
        [call] = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[0], "-p")
        prompt = argv[1]
        self.assertTrue(prompt.startswith("/harness:work 12"), prompt)
        self.assertIn("#41", prompt)
        self.assertNotIn("#58", prompt)
        self.assertIn("uncommitted", prompt.lower())
        self.assertIn("git status", prompt)
        self.assertEqual(os.path.realpath(call["cwd"]), self.wt)
        self.assertEqual(read_text(self.dirty), "def ready():\n    return ['round 2']\n")

        bodies = self.comment_bodies(12)
        self.assertTrue([b for b in bodies if "#41" in b and "uncommitted" in b.lower()], bodies)
        self.assertFalse([b for b in bodies if "#58" in b], bodies)
        self.assertEqual(self.comment_bodies(15), [])

        # The cap is unchanged: three ended runs of #12 in the window, so the next dispatch skips it with the capped
        # line; #15 (one ended run) still comes back.
        shutil.rmtree(self.runs_dir(), True)
        os.unlink(self.gh_log)
        runs12 = [self.ended_run(12, BRANCH, self.wt, id=f"{self.key}-12-2026100{i + 1}090000",
                                 started_at=iso_ago(hours=hours + 1), ended_at=iso_ago(hours=hours), outcome=outcome)
                  for i, (outcome, hours) in enumerate((("error", 30), ("early", 20), ("early", 10)))]
        self.ended_run(15, "t/15-x", other, result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", 58)})
        last = runs12[-1]
        why = (f"3 runs ended without a ready pull request in 7 days (last: early at {last['ended_at']}); "
               f"run it in this session with /harness:work 12, or wait for the window")
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + why, p.stdout.splitlines())
        self.assertFalse([ln for ln in p.stdout.splitlines() if ln.startswith("would start #12")], p.stdout)
        self.assertTrue([ln for ln in p.stdout.splitlines() if ln.startswith("would start #15 ")], p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [15])
        self.assertEqual(data["skipped"], [{"number": 12, "why": why, "runs": 3, "window_days": 7,
                                            "advice": "/harness:work 12"}])
        table = self.ok("runs", "--all").stdout.splitlines()
        [row] = [ln for ln in table if last["id"] in ln]
        self.assertIn("capped", row)
        self.assertEqual(sum("capped" in ln for ln in table), 1)
        sessions = [c for c in self.claude_calls() if "-p" in c["argv"] or "--bg" in c["argv"]]
        self.assertEqual(len(sessions), 1)                     # nothing launched after the one run above


# =========================================================================== the work skill
SKILL = os.path.join(os.path.dirname(HERE), "skills", "work", "SKILL.md")


def skill_text():
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


def skill_front():
    text = skill_text()
    return text.split("\n---", 1)[0] if text.startswith("---") else ""


def skill_body():
    text = skill_text()
    if text.startswith("---"):
        text = text.split("\n---", 1)[1].split("\n", 1)[1]           # drop the frontmatter
    return text


def blocks(text):
    """Blank-line-separated blocks of non-empty lines."""
    out, cur = [], []
    for line in text.splitlines():
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def is_preamble(block):
    """Headings, the Arguments line and the injected ready-tickets listing come before the instructions."""
    lines = [ln for ln in block if not ln.lstrip().startswith("#")]
    return not lines or all(ln.startswith(("Arguments:", "Ready tickets:", "!`")) for ln in lines)


def first_paragraph():
    """The first instruction paragraph of the skill, lower-cased, heading lines dropped."""
    first = next(b for b in blocks(skill_body()) if not is_preamble(b))
    return " ".join(ln for ln in first if not ln.lstrip().startswith("#")).lower()


LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
HEADING = re.compile(r"(?m)^(?=#{1,6} )")
NEGATION = {"never", "not", "no", "none", "nothing", "nor", "neither", "without", "cannot", "don't", "doesn't",
            "isn't", "aren't", "won't", "shouldn't", "mustn't", "can't"}
CLAUSE_END = re.compile(r"[.;:!?,()]")
WORD = re.compile(r"[a-z][a-z'_-]*")


def negated(text, at, window=8):
    """True when a negation word stands within `window` words before text[at], in the same clause."""
    clause = CLAUSE_END.split(text[:at])[-1]
    return any(w in NEGATION for w in WORD.findall(clause)[-window:])


# Wording that turns a rule into an option: an inverted skill reads as a list of things the orchestrator may leave
# out or do when it likes. A prohibition is not an option ("never skip a stage", "do not ignore a red check", "is not
# optional"), and "may" bounded by "only" in its clause ("the turn may end only when ...") is a rule; "not only" is
# always an opening ("may end ..., not only at X").
OPTION = re.compile(r"\b(?:optional(?:ly)?|skip(?:s|ped|ping)?|ignor(?:e|es|ed|ing)|omit(?:s|ted|ting)?"
                    r"|whenever you (?:like|want|wish|choose|prefer|see fit)|as you (?:like|see fit)"
                    r"|if you (?:have time|like|want|prefer)|at your discretion|up to you)\b")
NOT_ONLY = re.compile(r"\bnot only\b")
MAY = re.compile(r"\bmay\b")


def permissive_words(text):
    """The words in lower-cased `text` that leave a rule to choice: [] for a text that only commands and forbids."""
    found = [m.group(0) for m in NOT_ONLY.finditer(text)]
    found += [m.group(0) for m in OPTION.finditer(text) if not negated(text, m.start())]
    for m in MAY.finditer(text):
        rest = CLAUSE_END.split(text[m.end():])[0]
        if negated(text, m.start()) or re.match(r"\s+(?:not|never)\b", rest) or re.search(r"\bonly\b", rest):
            continue
        found.append("may" + rest[:40])
    return found


def first_paragraph_problems(para):
    """What the one-turn paragraph (lower-cased) gets wrong; [] when it states the rule."""
    problems = []
    if not re.search(r"\b(?:one|single) turn\b", para):
        problems.append("no one turn")
    # Agents run in the foreground: said as a directive, not negated, and no "background" anywhere in the
    # paragraph that is not negated ("never/not/no ... in the background").
    if not directive_foreground(para):
        problems.append("no directive to run agents in the foreground")
    fore, back_negated, _ = foreground_stance(para)
    if not fore:
        problems.append("no foreground that is not negated")
    if not back_negated:
        problems.append("a background that is not negated")
    # The orchestrator waits for each agent's result: a "wait" that is not "never wait".
    if not any(not negated(para, m.start()) for m in re.finditer(r"\bwait", para)):
        problems.append("no wait")
    for needle in ("harness-pr ready", "needs-input", "harness-work attempt"):
        if needle not in para:
            problems.append(f"no {needle}")
    if not re.search(r"\bfinal\b[^.;]*\bfailure\b", para):
        problems.append("no final failure")
    # "Only" (or "until") governs the end states: in the same sentence as the first of them.
    if not re.search(r"\b(?:only|until)\b(?:(?!\.\s).)*?(?:harness-pr ready|needs-input|harness-work attempt)", para):
        problems.append("the end states are not the only ones")
    words = permissive_words(para)
    if words:
        problems.append(f"permissive: {words}")
    return problems


def sentences_of(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def findings_rule_problems(para):
    """What the first paragraph (lower-cased) gets wrong about review findings; [] when it says that every finding
    on the ticket's files is fixed before `harness-pr ready` whatever its severity, that only a finding outside them
    becomes a new ticket, and that the PR body names it and says why."""
    problems = []
    sentences = sentences_of(para)

    def fixes_all(s):
        fixed = [m for m in re.finditer(r"\bfix(?:e[ds])?\b|\bfixing\b", s) if not negated(s, m.start())]
        return (re.search(r"\bfindings?\b", s) and fixed
                and re.search(r"\b(?:whatever|regardless of|of any|any|every|all)\b(?:\s+[a-z']+){0,2}\s+severit", s)
                and re.search(r"\b(?:files?|touch\w*|chang\w*|diff)\b", s)
                and re.search(r"\bbefore\b[^.!?]*harness-pr ready|harness-pr ready[^.!?]*\bafter\b", s))
    if not any(fixes_all(s) for s in sentences):
        problems.append("no sentence: every finding on the ticket's files is fixed before harness-pr ready, "
                        "whatever its severity")
    outside = [i for i, s in enumerate(sentences)
               if re.search(r"\b(?:outside|out of scope|beyond|other files|not (?:on|in) the (?:ticket's )?files)\b", s)
               and re.search(r"\b(?:new|follow-up|separate) (?:ticket|issue)\b", s)
               and re.search(r"\b(?:only|except|exception)\b", s)]
    if not outside:
        problems.append("no sentence: only a finding outside the ticket's files becomes a new ticket")

    def names_why(s):
        return re.search(r"\b(?:pr|pull request) (?:body|description)\b", s) and re.search(r"\b(?:why|reasons?)\b", s)
    if not any(names_why(s) for i in outside for s in sentences[i:i + 2]):
        problems.append("no sentence: the PR body names the out-of-scope finding and says why")
    return problems


def foreground_stance(text):
    """(a 'foreground' that is not negated, every 'background' negated, a 'background' at all) in lower-cased text.
    `run_in_background: false` is one word and never counts as 'background'."""
    fore = [not negated(text, m.start()) for m in re.finditer(r"\bforeground\b", text)]
    back = [negated(text, m.start()) for m in re.finditer(r"\bbackground\b", text)]
    return any(fore), all(back), bool(back)


def directive_foreground(text):
    """A sentence that names agents (itself, or the sentence before it: "run them in the foreground") and says, not
    negated, that they are 'in the foreground' (or are 'foreground agents'): the rule as an instruction, not a
    mention."""
    sentences = re.split(r"(?<=[.!?])\s", text)
    for i, sentence in enumerate(sentences):
        if not any(re.search(r"\bagents?\b", s) for s in sentences[max(0, i - 1):i + 1]):
            continue
        for m in re.finditer(r"\b(?:in the foreground|foreground agents?)\b", sentence):
            if not negated(sentence, m.start()):
                return True
    return False


def closing_lines():
    """The lines of the last section of the skill: its heading and everything after it."""
    return HEADING.split(skill_body())[-1].splitlines()


def closing_parts(lines):
    """(heading and lead-in text, the list items) of a closing section's lines, lower-cased."""
    items, item, prose = [], None, []
    for ln in lines:
        if LIST_ITEM.match(ln):
            item = [LIST_ITEM.sub("", ln, count=1)]
            items.append(item)
        elif item is not None and ln.startswith((" ", "\t")) and ln.strip():
            item.append(ln.strip())                                 # a wrapped item continues
        else:
            item = None
            prose.append(ln)
    return " ".join(prose).lower(), [" ".join(i).lower() for i in items]


def closing_problems(lines):
    """What a closing section gets wrong about being a checklist with nothing left to choice; [] when right."""
    problems = []
    if "checklist" not in lines[0].lower():
        problems.append(f"the heading is not a checklist: {lines[0]}")
    prose, items = closing_parts(lines)
    for where, text in [("closing prose", prose)] + [(f"item {i + 1}", t) for i, t in enumerate(items)]:
        words = permissive_words(text)
        if words:
            problems.append(f"{where} is permissive: {words}")
    return problems


class WorkSkill(unittest.TestCase):
    def closing_lines(self):
        return closing_lines()

    def closing_heading(self):
        return self.closing_lines()[0].lower()

    def closing_section(self):
        return closing_parts(self.closing_lines())

    def revise_step(self):
        """The revise step: from the numbered step that runs `harness-pr comments ... --unresolved` up to the next
        top-level numbered step, heading, or unindented paragraph after a blank line (indented sub-items and
        continuation lines included)."""
        lines = skill_body().splitlines()
        start = next((i for i, ln in enumerate(lines) if re.match(r"^\d+[.)]\s", ln)
                      and "harness-pr comments" in ln and "--unresolved" in ln), None)
        self.assertIsNotNone(start, "no numbered step runs `harness-pr comments <P> --unresolved`")
        end = next((j for j in range(start + 1, len(lines))
                    if re.match(r"^\d+[.)]\s", lines[j]) or lines[j].startswith("#")
                    or (lines[j].strip() and not lines[j][0].isspace() and not lines[j - 1].strip())), len(lines))
        return "\n".join(lines[start:end])

    def test_the_revise_step_runs_the_red_check_against_the_branch_tip_for_new_tests(self):
        # The branch already carries source changes when the owner's comments need new tests, so the default base
        # refuses ("Source already changed before the red run"); the revise step says to check with --base HEAD.
        step = self.revise_step()
        self.assertIn("harness-red check", step, step)
        self.assertIn("--base HEAD", step, step)
        self.assertGreater(step.index("--base HEAD"), step.index("harness-red check"), step)
        self.assertRegex(step.lower(), r"\btests?\b|test-author", step)

    def loop_steps(self):
        """{n: text} of the loop's numbered steps (a step runs to the next numbered line, a heading, or the blank
        line before an unindented paragraph)."""
        lines = skill_body().splitlines()
        steps, number = {}, None
        for i, line in enumerate(lines):
            m = re.match(r"^(\d+)[.)]\s+\*\*", line)
            if m:
                number = int(m.group(1))
                steps[number] = [line]
            elif number is not None and (line.startswith("#") or (line.strip() and not line[0].isspace()
                                                                  and not lines[i - 1].strip())):
                number = None
            elif number is not None:
                steps[number].append(line)
        return {n: "\n".join(v) for n, v in steps.items()}

    def test_a_no_tests_ticket_skips_the_test_author_red_check_and_breaker(self):
        steps = self.loop_steps()
        for n in (1, 2, 3, 4, 5):
            self.assertIn(n, steps, f"no numbered step {n} in the loop")
        start, tests, breaker = steps[1], steps[2], steps[3]
        # Step 1 reads the flag after `harness-work start` and says the ticket is no-tests.
        self.assertIn("no-tests", start, start)
        self.assertRegex(start, r"\bflags\b", start)
        # Step 2 names the skip and what still runs: the coder, the verify command, the review.
        low = tests.lower()
        self.assertIn("no-tests", low, tests)
        self.assertRegex(low, r"\bskip", tests)
        for word in ("coder", "verify", "review"):
            self.assertIn(word, low, f"step 2 does not say the {word} still runs:\n{tests}")
        # The breaker is skipped too: a no-tests sentence in steps 1-3 names it (or step 3).
        sentences = re.split(r"(?<=[.;])\s+|\n", "\n".join((start, tests, breaker)).lower())
        self.assertTrue(any("no-tests" in s and re.search(r"breaker|step 3", s) for s in sentences),
                        "no sentence says a no-tests ticket skips the breaker")
        self.assertTrue(any("no-tests" in s and re.search(r"test-author|red check|step 2", s) for s in sentences),
                        "no sentence says a no-tests ticket skips the test-author and the red check")
        # Every other ticket keeps the tests-first path, unchanged.
        self.assertIn("harness:test-author", tests)
        self.assertIn("harness-red check", tests)
        self.assertIn("Red: continue", tests)
        self.assertIn("harness:test-breaker", breaker)
        self.assertIn("harness-red scratch create", breaker)
        self.assertIn("harness:coder", steps[4])
        self.assertIn("harness:reviewer", steps[5])

    def test_step_one_reads_flags_from_the_board_show_json_not_the_start_json(self):
        # `harness-work start --json` carries no `flags` key; the flags come from `harness-board show N --json`.
        start = self.loop_steps()[1]
        sentences = re.split(r"(?<=[.;])\s+|\n", start)
        flag_sentences = [s for s in sentences if re.search(r"\bflags\b", s) and "no-tests" in s]
        self.assertTrue(flag_sentences, f"no sentence in step 1 reads flags for no-tests:\n{start}")
        for sentence in flag_sentences:
            self.assertIn("harness-board show", sentence)
            self.assertIn("--json", sentence)
        self.assertNotRegex(start.lower(), r"flags[^.]*start json")

    def test_the_design_doc_reads_flags_from_the_board_show_json_not_the_start_json(self):
        design = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "docs", "design.md")
        with open(design, encoding="utf-8") as fh:
            text = fh.read()
        lines = [ln for ln in text.splitlines()
                 if "skips the test-author" in ln and "no-tests" in ln and "flags" in ln]
        self.assertTrue(lines, "design.md has no sentence saying a no-tests ticket skips the test-author")
        for line in lines:
            self.assertIn("harness-board show", line)
            self.assertNotIn("start JSON", line)

    def test_the_first_paragraph_says_one_turn_foreground_agents_and_when_the_turn_ends(self):
        para = first_paragraph()
        self.assertEqual(first_paragraph_problems(para), [], para)

    def test_the_skill_ends_with_the_closing_checklist(self):
        body = skill_body()
        last = blocks(body)[-1]
        items = [ln for ln in last if LIST_ITEM.match(ln)]
        self.assertGreaterEqual(len(items), 3, last)
        self.assertTrue(LIST_ITEM.match(last[-1]), last[-1])          # the file ends on a checklist item
        prose, checklist = self.closing_section()
        self.assertGreaterEqual(len(checklist), 4, checklist)          # foreground agents and the three end states
        joined = " ".join(checklist)
        self.assertIn("harness-pr ready", joined)
        self.assertIn("harness-board needs-input", joined)
        self.assertIn("harness-work attempt", joined)
        self.assertRegex(joined, r"\b(?:foreground|background)\b", joined)

    def test_the_closing_checklist_is_named_as_one_and_tied_to_the_last_message(self):
        prose, checklist = self.closing_section()
        # The heading calls it a checklist; the heading or its lead-in says it holds before the final message.
        self.assertIn("checklist", self.closing_heading())
        self.assertRegex(prose, r"\bbefore\b.*\b(?:(?:last|final|closing)\b.*\b(?:message|reply|response|report)"
                                r"|end(?:ing|s)?\b.*\bturn)", prose)

    def test_the_one_turn_rule_and_the_checklist_are_unconditional(self):
        # Neither the first paragraph nor the closing section (heading, lead-in, every item) leaves anything to
        # choice: no may, optional, skip, ignore, whenever, "not only" or "if you have time".
        self.assertEqual(permissive_words(first_paragraph()), [], first_paragraph())
        self.assertEqual(closing_problems(self.closing_lines()), [], self.closing_lines())

    def test_each_end_state_and_the_foreground_rule_is_its_own_checklist_item_naming_its_command(self):
        prose, checklist = self.closing_section()

        def holding(pattern):
            return [i for i, item in enumerate(checklist) if re.search(pattern, item)]

        found = {
            "ready": holding(r"harness-pr ready"),
            "needs-input": holding(r"harness-board needs-input"),
            "final failure": holding(r"harness-work attempt"),
            "foreground agents": holding(r"\bagents?\b.*\b(?:foreground|background)\b"
                                         r"|\b(?:foreground|background)\b.*\bagents?\b"),
        }
        for what, where in found.items():
            with self.subTest(item=what):
                self.assertTrue(where, (what, checklist))
        # One item per end state: the three commands sit in three different items.
        end_states = [found["ready"], found["needs-input"], found["final failure"]]
        self.assertTrue(any(len({a, b, c}) == 3 for a in end_states[0] for b in end_states[1] for c in end_states[2]),
                        checklist)
        # The foreground item takes the right side: a foreground that is not negated, or a background that is, and
        # no background that is not ("agents ran in the background" is the inverse of the rule).
        stances = {i: foreground_stance(checklist[i]) for i in found["foreground agents"]}
        self.assertTrue(any(back_ok and (fore or back) for fore, back_ok, back in stances.values()),
                        (stances, checklist))
        # An item is a sentence the orchestrator can check, not a bare command or keyword.
        for item in checklist:
            with self.subTest(item=item):
                self.assertGreaterEqual(len(re.findall(r"[a-z][a-z'-]*", item)), 5, item)

    def test_the_one_turn_rule_leads_and_dispatch_the_loop_and_the_checklist_follow_it(self):
        # The rule is the first paragraph, before the dispatch section; the dispatch section, the loop with its
        # agents and tools, and the report guidance all survive; the checklist is the last section.
        body = skill_body()
        self.assertIn("foreground", first_paragraph())
        first = next(b for b in blocks(body) if not is_preamble(b))
        first_at = body.index(next(ln for ln in first if not ln.lstrip().startswith("#")))
        headings = [m.start() for m in re.finditer(r"(?m)^#{1,6} ", body)]
        dispatch = [m.start() for m in re.finditer(r"(?im)^#{1,6} .*\bdispatch\b.*$", body)]
        loop = [m.start() for m in re.finditer(r"(?im)^#{1,6} .*\bloop\b.*$", body)]
        self.assertTrue(dispatch, body)
        self.assertTrue(loop, body)
        self.assertLess(first_at, dispatch[0], "the one-turn rule comes before the dispatch section")
        self.assertLess(dispatch[0], loop[0], "dispatch comes before the loop")
        self.assertLess(loop[0], headings[-1], "the loop comes before the closing checklist")
        self.assertIn("checklist", body[headings[-1]:].splitlines()[0].lower())
        for needle in ("harness-work dispatch", "harness-work start", "harness-red check", "harness-pr open",
                       "harness-pr ready", "harness:test-author", "harness:test-breaker", "harness:coder",
                       "harness:reviewer"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        self.assertGreaterEqual(len(re.findall(r"(?m)^\s*\d+[.)]\s", body)), 6)     # the loop's numbered stages
        self.assertRegex(body[loop[0]:], r"(?i)\breport\b")                          # the report guidance
        front = skill_front()
        self.assertRegex(front, r"(?m)^name: work\s*$")
        self.assertRegex(front, r"(?m)^description: .*\bticket\b.*\bdispatch\b")
        self.assertIn("/harness:work", front)

    def test_the_first_paragraph_says_every_finding_on_the_tickets_files_is_fixed_before_ready(self):
        # The owner's scope addition (11:59): in the same first paragraph, every review finding on the files the
        # ticket touched is fixed before `harness-pr ready`, whatever its severity; only a finding outside the
        # ticket's files becomes a new ticket, and the PR body names it and says why.
        para = first_paragraph()
        self.assertEqual(findings_rule_problems(para), [], para)
        self.assertEqual(first_paragraph_problems(para), [], para)          # and the one-turn rule still holds


# The breaker's inverted skill: agents in the background, no waiting, the turn ends when it likes, the checks optional.
INVERTED_FIRST = ("every run of this skill is one turn. launch agents in the background with `run_in_background: "
                  "true`, never wait for their results, and keep going. the turn may end whenever you like, not only "
                  "at `harness-pr ready`, `harness-board needs-input` or the final failure that `harness-work "
                  "attempt` records.")
INVERTED_CLOSING = ["## Optional checks", "Run these if you have time.", "",
                    "- You may skip `harness-pr ready` when CI is slow.",
                    "- Ignore `harness-work attempt` and keep going after a failure.",
                    "- `harness-board needs-input` is optional when the question seems small."]
# Rules in the ticket outline's own words and plain prohibitions: none of them is an option.
RULE_SENTENCES = ["With a ticket number the turn may end only when `harness-pr ready` has run.",
                  "Never skip a stage, and do not ignore a red check.",
                  "The turn may not end before one of the end states holds; the checklist is not optional.",
                  "Don't skip `harness-red check` and never omit the evidence file."]
OPTION_SENTENCES = ["You may skip `harness-pr ready`.",
                    "Ignore `harness-work attempt`.",
                    "The turn may end whenever you like, not only at these points.",
                    "The review is optional.",
                    "Run the breaker if you have time.",
                    "You may end the turn after the coder."]


class SkillWording(unittest.TestCase):
    """The wording checks themselves, on text swapped in at test time: a rule in the outline's words or a plain
    prohibition passes, the breaker's inverted skill and each option sentence fails."""

    def test_rules_in_the_outlines_words_and_plain_prohibitions_pass(self):
        para = first_paragraph()
        for sentence in RULE_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertEqual(permissive_words(sentence.lower()), [])
                self.assertEqual(first_paragraph_problems((para + " " + sentence).lower()), [])
                lines = closing_lines()
                self.assertEqual(closing_problems(lines + ["- " + sentence]), [])        # as one more item
                self.assertEqual(closing_problems(lines[:1] + [sentence] + lines[1:]), [])   # in the lead-in

    def test_the_inverted_skill_and_every_option_sentence_fail(self):
        problems = first_paragraph_problems(INVERTED_FIRST)
        self.assertTrue(any(p.startswith("permissive") for p in problems), problems)
        self.assertIn("a background that is not negated", problems)
        self.assertIn("no directive to run agents in the foreground", problems)
        problems = closing_problems(INVERTED_CLOSING)
        self.assertTrue(any(p.startswith("the heading is not a checklist") for p in problems), problems)
        for where in ("closing prose", "item 1", "item 2", "item 3"):
            with self.subTest(where=where):
                self.assertTrue(any(p.startswith(where + " is permissive") for p in problems), problems)
        para, lines = first_paragraph(), closing_lines()
        for sentence in OPTION_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertNotEqual(permissive_words(sentence.lower()), [])
                self.assertNotEqual(first_paragraph_problems((para + " " + sentence).lower()), [])
                self.assertNotEqual(closing_problems(lines + ["- " + sentence]), [])

    def test_the_findings_rule_check_takes_any_wording_of_the_rule_and_rejects_a_weaker_one(self):
        rules = [
            "Every review finding on the files the ticket touched is fixed before `harness-pr ready`, whatever its "
            "severity; only a finding outside the ticket's files becomes a new ticket, and the PR body names it and "
            "says why.",
            "Fix all review findings in the files this ticket changed before `harness-pr ready`, whatever their "
            "severity. Only findings outside those files go to a new ticket. The PR body lists each one and the "
            "reason.",
            "`harness-pr ready` runs only after every finding on the touched files is fixed, regardless of severity. "
            "A finding out of scope is the one exception: it becomes a new issue, and the pull request body names it "
            "and says why it was left.",
        ]
        for text in rules:
            with self.subTest(rule=text):
                self.assertEqual(findings_rule_problems(text.lower()), [])
        weaker = [
            "Fix the high and medium review findings on the ticket's files before `harness-pr ready`; only a finding "
            "outside the ticket's files becomes a new ticket, and the PR body names it and says why.",
            "Fix all high-severity findings on the touched files before `harness-pr ready`. Only a finding outside "
            "the ticket's files becomes a new ticket, and the PR body names it and says why.",
            "Findings on the ticket's files need not be fixed before `harness-pr ready`, whatever their severity; "
            "only a finding outside the ticket's files becomes a new ticket, and the PR body names it and says why.",
            "Every review finding on the files the ticket touched is fixed before `harness-pr ready`, whatever its "
            "severity, and any finding you would rather not fix becomes a new ticket.",
            "Every review finding on the files the ticket touched is fixed before `harness-pr ready`, whatever its "
            "severity; only a finding outside the ticket's files becomes a new ticket.",
        ]
        for text in weaker:
            with self.subTest(weaker=text):
                self.assertNotEqual(findings_rule_problems(text.lower()), [])


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
