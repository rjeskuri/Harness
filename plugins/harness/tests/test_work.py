"""Tests for harness-work: start (branch and worktree from origin/main, an existing remote branch reused, the
exclude line, the worktree setup, .worktreeinclude copies, refusals), attempt's retry, escalate and
needs-input sequence, dispatch (dry-run plan, bg on the subscription by default whatever the api budget says,
--venue headless billed through apiKeyHelper and refused while the api budget is exhausted, re-dispatch of a run
that ended without a PR), runs --sweep's outcome
classification and the budget it marks, the sweep stopping the bg session of a ticket that is in review, needs input or
closed (#123), budget status, exhaust and reset, worktrees and worktree remove.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME, a bare `origin` and a clone of it (`proj`, the main checkout) made with
real git. A `fakebin` folder holds `gh` (a wrapper around fakegh.py) and `claude` (a fake that logs its
argv, cwd and environment, answers `agents --json` from $FAKECLAUDE_AGENTS, prints `backgrounded · abc123`
for --bg, answers `stop <id>` with exit 0, or exit 1 for an id listed in $FAKECLAUDE_STOP_FAIL (comma-separated),
and for -p sleeps briefly, then prints a canned JSON result). harness-board is the real one,
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
if argv[:1] == ["stop"]:
    failing = [x for x in os.environ.get("FAKECLAUDE_STOP_FAIL", "").split(",") if x]
    named = [a for a in argv[1:] if not a.startswith("-")]
    if not named:
        sys.stderr.write("error: missing session id\\n")
        sys.exit(2)
    if any(a in failing for a in named):
        sys.stderr.write("Error: could not stop background session " + named[0] + ": supervisor not reachable\\n")
        sys.exit(1)
    print("Stopped " + named[0])
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


# #69: a harness-board that runs the real one and adds stderr chatter that is not a harness-board warning, before
# and after; its exit code is the real one's. With NOISY_WARN set, a `show` also ends with a warning line of its own,
# `harness-board: warning: <NOISY_WARN> from show <number> <format>`.
CHATTER = ("harness-board: note: chatter before the call", "warning: chatter without the harness-board prefix")
NOISY_BOARD = """#!/bin/sh
printf '%s\\n' "$*" >> "$NOISY_LOG"
printf '%s\\n' "{before}" >&2
"{{python}}" "{{board}}" "$@"
rc=$?
printf '%s\\n' "{after}" >&2
if [ "$1" = show ] && [ -n "$NOISY_WARN" ]; then
    printf 'harness-board: warning: %s from show %s %s\\n' "$NOISY_WARN" "$2" "$3" >&2
fi
exit $rc
""".format(before=CHATTER[0], after=CHATTER[1])
WARNING = "harness-board: warning: "


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def project_argv(argv):
    """(subcommand, positionals, {option: value}) of a `gh project ...` argv, whatever the flag order."""
    pos, opts, i = [], {}, 2
    while i < len(argv):
        name, eq, value = argv[i][2:].partition("=")
        if not argv[i].startswith("--"):
            pos.append(argv[i])
        else:
            if not eq:
                i += 1
                value = argv[i] if i < len(argv) else None
            opts[name] = [opts[name], value] if name in opts else value
        i += 1
    return argv[1], pos, opts


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
    noisybin = os.path.join(root, "noisybin")       # only on PATH when a test puts it there
    executable(os.path.join(noisybin, "harness-board"), NOISY_BOARD.format(
        python=sys.executable, board=os.path.join(os.path.dirname(TOOL), "harness-board")))
    BINS.update(root=root, fakebin=fakebin, toolbin=toolbin, noisybin=noisybin)


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

    def test_start_mirrors_in_progress_to_the_configured_project(self):
        """#64: with HARNESS_PROJECT set, the real harness-board's status change during start sets the project
        item's Status to "In progress", on the URL gh reports for the issue (not one built from the remote)."""
        url = "https://ghe.example.com/o/r/issues/12"
        self.scenario(issues=[dict(issue(), url=url)])
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, {"match": ["project"], "stdout": ""})
        write(self.scenario_path, json.dumps(data))
        p = self.ok("start", "12", "--json", env=self.env(HARNESS_PROJECT="acme-org/23"))
        self.assertEqual(json.loads(p.stdout)["previous_status"], "ready")
        projects = [c for c in self.gh_calls() if c[:1] == ["project"]]
        self.assertEqual(len(projects), 1, projects)
        argv = projects[0]
        self.assertEqual(project_argv(argv), ("item-edit", ["23"], {"owner": "acme-org", "url": url, "field": "Status",
                                                                     "value": "In progress"}))
        edit = next(c for c in self.gh_calls() if c[:2] == ["issue", "edit"])
        self.assertLess(self.gh_calls().index(edit), self.gh_calls().index(argv), "the label changes first")

    # ------------------------------------------------- #69: harness-board's Projects warning reaches the user
    def project_scenario(self, project_call, numbers=(12,)):
        """The issues, with `project_call` (the fake gh's answer to any `gh project ...`) ahead of the rest."""
        self.scenario(issues=[issue(n) for n in numbers])
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, dict(project_call, match=["project"]))
        write(self.scenario_path, json.dumps(data))

    def test_start_passes_the_board_warning_through_when_the_project_item_edit_fails(self):
        """A failing `gh project item-edit` during start: start still exits 0, and harness-board's one warning line,
        naming that board and gh's error, is on start's stderr exactly once (not on stdout). The board comes from
        the environment in one case and from ~/.claude/settings.json in the other; text and --json output alike."""
        cases = [(12, "acme-org/23", "HTTP 403: missing the project scope (token-a1)", "env", []),
                 (13, "other-owner/5", "item not found on the board (token-b2)", "settings", ["--json"])]
        for number, project, why, source, flags in cases:
            with self.subTest(number=number, source=source):
                self.project_scenario({"stderr": why + "\n", "exit": 1}, numbers=(12, 13))
                if source == "settings":
                    write(os.path.join(self.home, ".claude", "settings.json"),
                          json.dumps({"env": {"HARNESS_PROJECT": project}}))
                    env = self.env()
                else:
                    env = self.env(HARNESS_PROJECT=project)
                p = self.ok("start", str(number), *flags, env=env)
                owner, board_number = project.split("/")
                expected = (f"{WARNING}the Projects board {owner}/{board_number} was not updated: "
                            f"gh project item-edit {board_number} failed: {why}")
                self.assertEqual(p.stderr.splitlines(), [expected], p.stderr)
                self.assertNotIn(WARNING, p.stdout)
                if flags:
                    self.assertEqual(json.loads(p.stdout)["previous_status"], "ready")
                else:
                    self.assertTrue(p.stdout.startswith(self.worktree_path(f"t/{number}-add-the-board-tool")),
                                    p.stdout)
                self.assertIn(["issue", "edit", str(number), "--add-label", "status:in-progress",
                               "--remove-label", "status:ready"],
                              [c[:7] for c in self.gh_calls()], "the labels still changed")

    def test_start_keeps_other_board_stderr_hidden(self):
        """harness-board succeeds but writes other lines on stderr (a wrapper around the real one): only warning
        lines reach start's stderr, from every board call start makes (both `show`s as well as `status`), each
        once and in call order; with no warning anywhere, start's stderr stays empty as today. A ticket already in
        progress gets no status change, and its `show` warnings still come through."""
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        why = "HTTP 401: token expired (token-c3)"
        self.project_scenario({"stderr": why + "\n", "exit": 1}, numbers=(12, 13))
        log = os.path.join(self.tmp, "noisy.log")
        p = self.ok("start", "12", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log,
                                                NOISY_WARN="token-k1"))
        self.assertIn("status 12 in-progress", read_text(log), "the chattering board ran")
        self.assertEqual(p.stderr.splitlines(), [
            f"{WARNING}token-k1 from show 12 --json",
            f"{WARNING}the Projects board acme-org/31 was not updated: gh project item-edit 31 failed: {why}",
            f"{WARNING}token-k1 from show 12 --markdown"], p.stderr)
        for line in CHATTER:
            self.assertNotIn(line, p.stderr)
        self.project_scenario({"stdout": ""}, numbers=(12, 13))
        p = self.ok("start", "13", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log))
        self.assertIn("status 13 in-progress", read_text(log))
        self.assertEqual(p.stderr, "")
        self.assertTrue([c for c in self.gh_calls() if c[:2] == ["project", "item-edit"]], "the mirror ran")

        self.project_scenario({"stderr": "should never run\n", "exit": 1}, numbers=())
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, {"match": ["issue", "view", "14"], "stdout_json": issue(
            14, labels=("task", "status:in-progress", "size:S", "risk:low"))})
        write(self.scenario_path, json.dumps(data))
        p = self.ok("start", "14", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log,
                                                NOISY_WARN="token-m3"))
        self.assertNotIn("status 14", read_text(log), "no status change from in-progress")
        self.assertEqual(p.stderr.splitlines(), [f"{WARNING}token-m3 from show 14 --json",
                                                 f"{WARNING}token-m3 from show 14 --markdown"], p.stderr)

    def test_start_stderr_is_unchanged_without_the_project_setting(self):
        """No HARNESS_PROJECT anywhere: no project call, and start's stderr is empty as before #69, with the plain
        board and with one that chatters on stderr."""
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        self.project_scenario({"stderr": "should never run\n", "exit": 1}, numbers=(12, 13))
        log = os.path.join(self.tmp, "noisy.log")
        for number, env in ((12, self.env()), (13, self.env(PATH=noisy, NOISY_LOG=log))):
            with self.subTest(number=number):
                p = self.ok("start", str(number), env=env)
                self.assertEqual(p.stderr, "")
        self.assertIn("status 13 in-progress", read_text(log), "the chattering board ran")
        self.assertEqual([c for c in self.gh_calls() if c[:1] == ["project"]], [])


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

    def test_fix_stage_allows_two_rounds_then_needs_input(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["fix", "fail"], "retry"),
            (["fix", "fail"], "needs-input"),                # two rounds at most, never an escalation
            (["fix", "fail"], "needs-input"),
            (["fix", "pass"], "done"),
            (["fix", "fail"], "retry"),                      # a pass ends the run of fix failures
            (["fix", "fail", "--tier", "opus"], "needs-input"),
            (["fix", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "fable"], "retry"),
            (["fix", "fail", "--tier", "fable"], "needs-input"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "result", "at"})
        self.assertEqual([a["stage"] for a in attempts], ["fix"] * len(sequence))
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[3]["tier"], attempts[3]["result"]), ("sonnet", "pass"))
        self.assertEqual((attempts[5]["tier"], attempts[5]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-1]["tier"], attempts[-1]["result"]), ("fable", "fail"))

        # --json on a fresh run: the tier never moves and no next tier is named
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        for failures, action in ((1, "retry"), (2, "needs-input"), (3, "needs-input")):
            with self.subTest(json_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--json")
                self.assertEqual((data["stage"], data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 ("fix", action, failures, "sonnet", None))
        data = self.ok_json("attempt", "12", "fix", "pass", "--json")
        self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("done", 0, None))
        for failures, action in ((1, "retry"), (2, "needs-input")):
            with self.subTest(json_opus_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--tier", "opus", "--json")
                self.assertEqual((data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 (action, failures, "opus", None))

    def test_fix_and_review_failures_count_apart(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["review", "fail", "--tier", "opus"], "retry"),
            (["fix", "fail"], "retry"),                      # review's failure is not fix's
            (["review", "fail", "--tier", "opus"], "escalate fable"),   # fix's failure did not reset review
            (["fix", "fail"], "needs-input"),                # review's failures did not advance or reset fix
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["review", "fail", "--tier", "fable"], "needs-input"),
            (["fix", "pass"], "done"),
            (["review", "fail", "--tier", "fable"], "needs-input"),     # a fix pass does not end review's run
            (["fix", "fail", "--tier", "opus"], "retry"),
            (["review", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "opus"], "needs-input"),         # a review pass does not end fix's run
            (["review", "fail", "--tier", "opus"], "retry"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])

    def test_an_unknown_stage_is_refused(self):
        attempts = [{"stage": "code", "tier": "sonnet", "result": "fail", "at": "2026-10-01T00:00:00Z"}]
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": attempts}))
        for stage in ("deploy", "fixes", "Fix"):
            with self.subTest(stage=stage):
                p = self.work("attempt", "12", stage, "fail")
                self.assertOneLineError(p, stage, "fix")     # the refusal names the bad stage and the valid ones
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], attempts)


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
        self.assertIn("venue bg, 1 live, 1 of 2 slots free", p.stdout)
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)
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
        p = self.ok("dispatch", "--venue", "headless", env=env)
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
        self.ok("dispatch", "--venue", "headless")
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
        self.assertIn("would start #12 Add the board tool (again after billing) and launch bg", p.stdout)
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

    # ---- ticket #50: an exhausted api budget never launches headless, whatever --venue says
    def api_until(self):
        return self.read_json(self.budgets_path())["api"]["exhausted_until"]

    def assertRefusedHeadless(self, p, until):
        """Exit non-zero, the message names when the budget is exhausted until and how to launch headless again."""
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("harness-work budget reset api", out)
        self.assertIn(until, out)
        self.assertNotIn("would start", out)
        self.assertNotIn("launching headless anyway", out)

    def assertNothingStarted(self, records_before):
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])],
                         self.gh_calls())
        self.assertEqual(sorted(r["id"] for r in self.run_records()), sorted(records_before))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted(self):
        # #12 is ready on the board; #15 is in progress after a billing run (the #43 case): either would be launched.
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            with self.subTest(hours=hours):
                self.write_budget("api", hours, reason)
                until = self.api_until()
                self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)

                p = self.work("dispatch", "--venue", "headless", env=self.env(**CREDENTIALS))
                self.assertRefusedHeadless(p, until)
                self.assertNotIn("headless on", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--max", "5", "--dry-run")
                self.assertRefusedHeadless(p, until)                # the dry run says the same and plans nothing
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--dry-run", "--json")
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNothingStarted([ended["id"]])
                self.assertEqual(self.read_json(self.budgets_path())["api"]["exhausted_until"], until)

        # Without --venue the same board launches both on bg, as before: the refusal changed nothing.
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
            for name in CREDENTIALS:
                self.assertNotIn(name, call["env"])
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def unswept_failure(self, result):
        """The overnight #43 case from a clean slate: no budgets file; #15 in progress with a headless run whose
        process is gone, not yet swept, its log ending in `result`; #12 ready on the board. Returns the record."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.budgets_path(), self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)
        record = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(minutes=20))
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        self.scenario(issues=[issue(), issue(15, labels=IN_PROGRESS, title="Billing victim")],
                      ready=[ready_row(12)])
        return record

    def test_venue_headless_is_refused_when_dispatchs_own_sweep_exhausts_the_api_budget(self):
        # The budget is judged after dispatch's sweep: the sweep finds the billing (or auth) failure, marks the api
        # budget, and --venue headless is then refused, as it must have been at 23:55:51Z on 2026-10-13.
        low = dict(API_401, api_error_status=400, result="Credit balance is too low")
        cases = [("billing", low, ()), ("auth", API_401, ()), ("billing", low, ("--dry-run",)),
                 ("auth", API_401_HELPER, ("--dry-run",))]
        for outcome, result, extra in cases:
            with self.subTest(outcome=outcome, extra=extra):
                record = self.unswept_failure(result)
                p = self.work("dispatch", "--venue", "headless", "--max", "5", *extra, env=self.env(**CREDENTIALS))
                self.assertTrue(os.path.exists(self.budgets_path()), p.stdout + p.stderr)
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(outcome + ": "), api["reason"])
                self.assertRefusedHeadless(p, api["exhausted_until"])
                self.assertNotIn("headless on", p.stdout)
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([record["id"]])
                [swept] = self.run_records()
                self.assertEqual(swept["outcome"], outcome)
                self.assertTrue(swept["ended_at"])

        # The same board without --venue: the sweep exhausts the budget and both tickets go to bg, as before.
        record = self.unswept_failure(low)
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
        self.assertEqual({r["id"]: r["outcome"] for r in self.run_records() if r["id"] == record["id"]},
                         {record["id"]: "billing"})
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted_even_with_nothing_to_launch(self):
        self.write_budget("api", 5, "auth: invalid api key")
        until = self.api_until()
        self.scenario()                                                 # no ready ticket, no run on record
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                p = self.work("dispatch", "--venue", "headless", *extra)
                self.assertRefusedHeadless(p, until)
                self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg")                        # bg is still fine: nothing to do
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])

    def test_venue_headless_launches_headless_while_the_api_budget_is_available(self):
        # Four ways the api budget is not exhausted: no budgets file, its time has passed, only the cloud budget is
        # exhausted, and `harness-work budget reset api` after an exhaustion (the refusal's advice).
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        setups = [(12, "Add the board tool", no_file), (13, "Second ticket", lambda: self.write_budget("api", -2)),
                  (14, "Third ticket", lambda: self.write_budget("cloud", 5)), (16, "Fourth ticket", reset)]
        for n, title, setup in setups:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                if os.path.exists(self.claude_log):
                    os.unlink(self.claude_log)
                setup()
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", "--venue", "headless")
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn("budget reset", p.stdout + p.stderr)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                argv = self.claude_calls()[0]["argv"]
                self.assertEqual(argv[:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                            "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": DEFAULT_KEY_CMD})
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    # ---- ticket #57: without --venue, dispatch runs on the subscription (bg) whatever the api budget says
    def budget_states(self):
        """(label, setup) for every state of the api budget: no budgets file, an exhaustion whose time has passed,
        only the cloud budget exhausted, `budget reset api` after an exhaustion, and the api budget exhausted."""
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        return [("no budgets file", no_file), ("expired", lambda: self.write_budget("api", -2, "billing: old")),
                ("cloud only", lambda: self.write_budget("cloud", 5, "cloud: out")), ("reset", reset),
                ("api exhausted", lambda: self.write_budget("api", 3, "billing: credit balance is too low"))]

    def fresh_logs(self):
        for record in self.run_records():                  # a headless child still running would log into the next case
            if isinstance(record.get("pid"), int):
                wait_for(lambda: pid_gone(record["pid"]))
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)

    def test_default_venue_is_bg_whatever_the_api_budget_says(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        key_cmd = f"cat {keyfile}"
        tickets = [(12, "Add the board tool"), (13, "Second ticket"), (14, "Third ticket"), (16, "Fourth ticket"),
                   (17, "Fifth ticket")]
        for (label, setup), (n, title) in zip(self.budget_states(), tickets):
            with self.subTest(budget=label, ticket=n):
                self.fresh_logs()
                setup()
                budgets_before = read_text(self.budgets_path())
                status_before = self.ok("budget").stdout
                bg_id = f"bg{n}{label[:2]}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                env = self.env(HARNESS_API_KEY_CMD=key_cmd, FAKECLAUDE_BG_ID=bg_id, **CREDENTIALS)
                p = self.ok("dispatch", env=env)
                self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id {bg_id}\)$")
                self.assertNotIn("headless", p.stdout + p.stderr)
                self.assertNotIn("budget reset", p.stdout + p.stderr)

                [call] = self.claude_calls()                   # one session, on the subscription login
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {title}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                self.assertNotIn(key_cmd, json.dumps(call["argv"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name, value in CREDENTIALS.items():          # no exported key or token bills instead
                    self.assertNotIn(name, call["env"])
                    self.assertNotIn(value, json.dumps(call))
                [record] = self.run_records()
                self.assertTrue(record["branch"].startswith(f"t/{n}-"), record["branch"])
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"]),
                                 (n, "bg", bg_id, None))
                self.assertNotIn("pid", record)
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                body = comment[comment.index("--body") + 1]
                self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)

                # dispatch leaves the budgets as it found them: the default does not depend on them or change them
                self.assertEqual(read_text(self.budgets_path()), budgets_before)
                self.assertEqual(self.ok("budget").stdout, status_before)

    def test_dry_run_plan_names_bg_by_default_and_headless_only_on_request(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        board = dict(issues=[issue(), issue(13), issue(14, title="Third"), issue(15, title="Fourth")],
                     ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third"), ready_row(15, "Fourth")])
        for (label, setup), most in zip(self.budget_states()[:4], (2, 3, 4, 5)):    # the api budget is available
            with self.subTest(budget=label, max=most):
                setup()
                self.scenario(**board)
                free = most - 1
                planned = [n for n in (12, 14, 15)][:free]
                for venue, extra in (("bg", ()), ("bg", ("--venue", "bg")), ("headless", ("--venue", "headless"))):
                    p = self.ok("dispatch", "--dry-run", "--max", str(most), *extra)
                    self.assertIn(f"Dry run: venue {venue}, 1 live, {free} of {most} slots free.", p.stdout)
                    starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                    self.assertEqual(len(starts), len(planned), p.stdout)
                    for line in starts:
                        self.assertTrue(line.endswith(f"and launch {venue}"), line)
                    other = "headless" if venue == "bg" else "launch bg"
                    self.assertNotIn(other, p.stdout)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *extra)
                    self.assertEqual(data["venue"], venue)
                    self.assertEqual([row["number"] for row in data["plan"]], planned)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(len(self.run_records()), 1)
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_redispatched_ticket_launches_bg_by_default(self):
        # #12 ended early, #15 ended in an error, both still in progress with no PR; #16 is fresh from the board.
        # No budgets file: the api budget is available, and still every launch goes to the subscription.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="early")
        self.write_run(15, id=f"{self.key}-15-20261013080000", pid=self.dead_pid(), started_at=iso_ago(hours=5),
                       ended_at=iso_ago(hours=4), outcome="error")
        titles = {12: "Ended early", 15: "Errored", 16: "Fresh"}
        self.scenario(issues=[issue(12, labels=IN_PROGRESS, title=titles[12]),
                              issue(15, labels=IN_PROGRESS, title=titles[15]), issue(16, title=titles[16])],
                      ready=[ready_row(16, titles[16])])
        self.assertFalse(os.path.exists(self.budgets_path()))

        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Ended early (again after early) and launch bg", p.stdout.splitlines())
        self.assertIn("would start #15 Errored (again after error) and launch bg", p.stdout.splitlines())
        self.assertNotIn("headless", p.stdout)

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57", **CREDENTIALS))
        for n in (12, 15, 16):
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id redo57\)$")
        self.assertNotIn("headless", p.stdout)
        calls = {int(c["argv"][-1].split()[1]): c for c in self.claude_calls()}
        self.assertEqual(sorted(calls), [12, 15, 16])
        for n, call in calls.items():
            with self.subTest(ticket=n):
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {titles[n]}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual(sorted((r["ticket"], r["venue"], r.get("bg_id"), "pid" in r) for r in fresh),
                         [(12, "bg", "redo57", False), (15, "bg", "redo57", False), (16, "bg", "redo57", False)])

    # ---- ticket #57: `--api` is the CLI's own alias for `--venue headless`, the only other way onto the API key
    def test_api_launches_headless_on_the_api_key_while_the_budget_is_available(self):
        tickets = [(12, "Add the board tool", ("--api",)), (13, "Second ticket", ("--api", "--venue", "headless")),
                   (14, "Third ticket", ("--venue", "headless", "--api")), (16, "Fourth ticket", ("--api",))]
        for (label, setup), (n, title, flags) in zip(self.budget_states()[:4], tickets):
            with self.subTest(budget=label, ticket=n, flags=flags):
                self.fresh_logs()
                setup()
                keyfile = write(os.path.join(self.tmp, f"key{n}.txt"), SECRET + "\n")
                key_cmd = f"cat {keyfile}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", *flags, env=self.env(HARNESS_API_KEY_CMD=key_cmd, **CREDENTIALS))
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn(" bg on ", p.stdout)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertIsInstance(record["pid"], int)
                self.assertNotIn("bg_id", record)
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                [call] = self.claude_calls()
                self.assertEqual(call["argv"][:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                                    "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(call["argv"][7]), {"apiKeyHelper": key_cmd})
                self.assertEqual(len(call["argv"]), 8)
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                self.assertTrue(comment[comment.index("--body") + 1].splitlines()[0].endswith(", headless)"))
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_api_dry_run_plans_headless(self):
        self.scenario(issues=[issue(), issue(13), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        for most, flags in ((1, ("--api",)), (2, ("--api", "--venue", "headless")), (3, ("--api",))):
            with self.subTest(max=most, flags=flags):
                p = self.ok("dispatch", "--dry-run", "--max", str(most), *flags)
                self.assertIn(f"Dry run: venue headless, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for line in starts:
                    self.assertTrue(line.endswith("and launch headless"), line)
                self.assertNotIn("launch bg", p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *flags)
                self.assertEqual(data["venue"], "headless")
                self.assertEqual([row["number"] for row in data["plan"]], [12, 13, 14][:most])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_api_is_refused_while_the_api_budget_is_exhausted(self):
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            for flags in (("--api",), ("--api", "--venue", "headless")):
                for extra in ((), ("--dry-run",), ("--dry-run", "--json")):
                    with self.subTest(hours=hours, flags=flags, extra=extra):
                        self.write_budget("api", hours, reason)
                        until = self.api_until()
                        self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                        for path in (self.gh_log, self.claude_log):
                            if os.path.exists(path):
                                os.unlink(path)
                        p = self.work("dispatch", *flags, "--max", "5", *extra, env=self.env(**CREDENTIALS))
                        if "--json" in extra:
                            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                        else:
                            self.assertRefusedHeadless(p, until)
                            self.assertNotIn("launch headless", p.stdout)
                            self.assertNotIn("headless on", p.stdout)
                        self.assertNothingStarted([ended["id"]])
                        self.assertEqual(self.api_until(), until)
        # nothing to launch at all: still refused
        self.scenario()
        p = self.work("dispatch", "--api")
        self.assertRefusedHeadless(p, self.api_until())

    def test_api_with_venue_bg_is_an_error_and_starts_nothing(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])            # the api budget is available
        # --api alone is a known flag (so the error below is the conflict, not an unknown argument)
        p = self.ok("dispatch", "--api", "--dry-run")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch headless", p.stdout)
        for flags in (("--api", "--venue", "bg"), ("--venue", "bg", "--api")):
            for extra in ((), ("--dry-run",)):
                with self.subTest(flags=flags, extra=extra):
                    p = self.work("dispatch", *flags, *extra, env=self.env(**CREDENTIALS))
                    self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("--api", p.stderr)
                    self.assertNotIn("unrecognized", p.stderr)
                    self.assertNotIn("would start", p.stdout)
                    self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg", "--dry-run")              # the control: the same board plans bg
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)

    def test_dispatch_help_says_bg_is_the_default_venue(self):
        # --venue states bg as the default with no budget condition; --api says it bills the API key. The checker
        # itself is proven on right and wrong help texts in SkillWording.
        p = self.ok("dispatch", "--help")
        self.assertEqual(help_venue_problems(p.stdout), [], p.stdout)


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
        """#12: three ended runs, the last ok; #15: four, the last early. Both in progress without a PR. (No billing
        or auth run here: those do not count toward the cap, ticket #50, so the counts 3 and 4 are unambiguous.)"""
        first = self.ended(12, ["error", "error", "ok"], [30, 20, 10])
        second = self.ended(15, ["error", "ok", "error", "early"], [60, 50, 40, 5])
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
        records = self.ended(12, ["early", "error", "ok"], [40, 30, 20])      # billing/auth would not count (#50)
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
        self.assertIn("would start #13 Two (again after ok) and launch bg", p.stdout)
        self.assertIn("would start #16 Straddling (again after ok) and launch bg", p.stdout)
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
        newest_started = self.run_at(12, "early", 30, 20)                 # newest started_at
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
        cap16 = [self.run_at(16, o, s, e) for o, s, e in (("error", 100, 90), ("billing", 80, 70),
                                                          ("early", 60, 50), ("auth", 30, 25), ("error", 11, 5))]
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

    # ---- ticket #50: billing and auth runs are the budget's fault, not the ticket's: they do not count toward the cap
    def cap_lines(self, text):
        """{ticket: its skip line} for the cap skips in a dispatch's text output."""
        return {int(m.group(1)): line for line in text.splitlines()
                for m in [re.match(r"skip #(\d+): \d+ runs ended without a ready pull request", line)] if m}

    def assertCapSkip(self, line, n, last):
        self.assertTrue(line.startswith(f"skip #{n}: "), line)
        self.assertTrue(line.endswith(f"in 7 days (last: {last['outcome']} at {last['ended_at']}); run it in this "
                                      f"session with /harness:work {n}, or wait for the window"), line)

    def assertRelaunchedOnBg(self, failing):
        """`failing` holds, per ticket, outcomes that are all billing or auth. With the api budget exhausted each
        ticket is redispatched on bg with no cap skip and no `capped` mark; #15 (error, early, ok) is the capped
        control in the same dispatch."""
        self.write_budget("api", 12, "billing: credit balance is too low")
        titles = {n: f"Budget victim {n}" for n in failing}
        last = {}
        for n, outcomes in failing.items():
            hours = [10 * (len(outcomes) - i) + n % 7 for i in range(len(outcomes))]
            last[n] = self.ended(n, outcomes, hours)[-1]
        control = self.ended(15, ["error", "early", "ok"], [30, 20, 10])
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=titles[n]) for n in failing]
                      + [issue(15, labels=IN_PROGRESS, title="Control")])

        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})      # `runs` marks only the control

        p = self.ok("dispatch", "--dry-run", "--max", "9")
        self.assertIn("Dry run: venue bg,", p.stdout)
        for n in failing:
            self.assertIn(f"would start #{n} {titles[n]} (again after {last[n]['outcome']}) and launch bg",
                          p.stdout.splitlines())
            self.assertNotIn(f"skip #{n}:", p.stdout)
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        self.assertCapSkip(self.cap_lines(p.stdout)[15], 15, control[-1])
        data = self.ok_json("dispatch", "--dry-run", "--max", "9", "--json")
        self.assertEqual(data["venue"], "bg")
        self.assertEqual([row["number"] for row in data["plan"]], sorted(failing))
        self.assertEqual([s["number"] for s in data["skipped"]], [15])
        self.assertEqual(self.claude_calls(), [])

        p = self.ok("dispatch", "--max", "9", env=self.env(**CREDENTIALS))       # for real
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        for n in failing:
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id abc123\)$")
            self.assertNotIn(f"skip #{n}:", p.stdout)
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), sorted(f"/harness:work {n}" for n in failing))
        for call in calls:
            n = int(call["argv"][-1].split()[-1])
            self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {titles[n]}", "--permission-mode", "auto",
                                            f"/harness:work {n}"])
            for name in CREDENTIALS:
                self.assertNotIn(name, call["env"])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual(sorted((r["ticket"], r["venue"], r.get("bg_id")) for r in fresh),
                         [(n, "bg", "abc123") for n in sorted(failing)])

    def test_billing_runs_do_not_cap_a_ticket_and_it_is_redispatched_on_bg(self):
        self.assertRelaunchedOnBg({12: ["billing", "billing", "billing"],
                                   13: ["billing", "billing", "billing", "billing", "billing"]})

    def test_auth_runs_do_not_cap_a_ticket_and_it_is_redispatched_on_bg(self):
        self.assertRelaunchedOnBg({12: ["auth", "auth", "auth"],
                                   14: ["auth", "billing", "billing", "auth"],
                                   16: ["billing", "auth", "auth", "billing", "auth", "billing"]})

    def test_only_runs_other_than_billing_and_auth_count_toward_the_cap(self):
        # Only billing and auth are left out of the count: every other outcome counts, the bg ones (`stopped`,
        # `unknown`) and any state kept as is (`timeout`) included, not just ok, error and early.
        under = {12: ["early", "billing", "early", "billing"],          # fewer than three count: redispatched
                 13: ["billing", "billing", "early", "early"],
                 14: ["auth", "early", "billing", "error", "auth"],
                 17: ["error", "auth", "billing", "auth", "billing", "ok"],
                 19: ["early", "early"],
                 22: ["stopped", "auth", "billing"],
                 23: ["timeout", "billing", "unknown", "auth"]}
        capped = {16: ["early", "early", "early", "billing"],           # three or more count: capped
                  18: ["billing", "early", "error", "early"],
                  20: ["early", "auth", "error", "ok"],
                  21: ["auth", "error", "billing", "early", "billing", "ok", "auth"],
                  24: ["stopped", "unknown", "early", "billing"],
                  25: ["timeout", "billing", "timeout", "stopped", "auth", "early"],
                  26: ["unknown", "unknown", "auth", "unknown"]}
        counted = {16: 3, 18: 3, 20: 3, 21: 3, 24: 3, 25: 4, 26: 3}     # the runs that count, as the skip says
        last = {}
        for n, outcomes in sorted({**under, **capped}.items()):
            hours = [8 * (len(outcomes) - i) + n % 5 for i in range(len(outcomes))]
            last[n] = self.ended(n, outcomes, hours)[-1]
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"Mixed {n}") for n in sorted(last)])

        self.assertEqual(self.marked(), {n: [last[n]["id"]] for n in capped})

        p = self.ok("dispatch", "--dry-run", "--max", "9")
        self.assertIn("Dry run: venue bg,", p.stdout)
        for n in under:
            self.assertIn(f"would start #{n} Mixed {n} (again after {last[n]['outcome']}) and launch bg",
                          p.stdout.splitlines())
        lines = self.cap_lines(p.stdout)
        self.assertEqual(sorted(lines), sorted(capped))
        for n in capped:
            self.assertCapSkip(lines[n], n, last[n])
            self.assertEqual(lines[n], f"skip #{n}: " + self.skip_why(n, counted[n], last[n]))
            self.assertNotIn(f"would start #{n} ", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--max", "9", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], sorted(under))
        self.assertEqual(sorted(s["number"] for s in data["skipped"] if "advice" in s), sorted(capped))
        self.assertEqual({s["number"]: s for s in data["skipped"]},
                         {n: {"number": n, "why": self.skip_why(n, counted[n], last[n]), "runs": counted[n],
                              "window_days": 7, "advice": f"/harness:work {n}"} for n in capped})
        self.assertEqual(self.claude_calls(), [])

    def test_an_ended_run_without_an_outcome_counts_toward_the_cap(self):
        # Only billing and auth are left out: an ended record whose outcome is null still counts.
        none3 = self.ended(12, [None, None, None], [30, 20, 10])
        mixed = self.ended(13, [None, "early", "stopped", "billing"], [35, 25, 15, 5])
        self.ended(14, [None, "billing", None, "auth"], [36, 26, 16, 6])            # two count: redispatched
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"Null {n}") for n in (12, 13, 14)])
        self.assertEqual(self.marked(), {12: [none3[-1]["id"]], 13: [mixed[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 13])
        self.assertEqual([row["number"] for row in data["plan"]], [14])
        self.assertEqual({s["number"]: s["runs"] for s in data["skipped"]}, {12: 3, 13: 3})

    def test_billing_runs_do_not_cap_a_ticket_after_the_budget_is_reset_and_it_relaunches_headless(self):
        self.write_budget("api", 12, "billing: credit balance is too low")
        last = self.ended(12, ["billing", "billing", "billing"], [30, 20, 10])[-1]
        control = self.ended(15, ["unknown", "error", "stopped"], [31, 21, 11])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS, title="Credits added"),
                              issue(15, labels=IN_PROGRESS, title="Control")])
        self.ok("budget", "reset", "api")                                   # credits added
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        p = self.ok("dispatch", "--venue", "headless", "--dry-run")
        self.assertIn(f"would start #12 Credits added (again after {last['outcome']}) and launch headless",
                      p.stdout.splitlines())
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        p = self.ok("dispatch", "--venue", "headless")
        self.assertRegex(p.stdout, r"(?m)^#12 headless on t/12-\S+ \(pid \d+, log ")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"][:7], ["-p", "/harness:work 12", "--permission-mode", "auto",
                                            "--output-format", "json", "--settings"])
        [fresh] = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual((fresh["ticket"], fresh["venue"]), (12, "headless"))
        self.assertTrue(wait_for(lambda: pid_gone(fresh["pid"])))


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
        self.assertIn("venue bg, 1 live, 2 of 3 slots free", p.stdout)
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
        self.assertIn("would start #12 Add the board tool (again after error) and launch bg", p.stdout)

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

    def test_dispatch_asks_gh_about_a_freshly_ended_draft_run_once(self):
        # The draft case of the gh-once guarantee: #10 and #11 in progress with only a draft PR each (#43, #44),
        # #7 with no PR, #8 with a ready PR. One dispatch sweeps all four (early, early, early, ok) and redispatches
        # the three early ones; `issue view` and `pr list --head` are asked exactly once for each early ticket, the
        # draft's PR number included in that one answer.
        for n in (7, 8, 10, 11):
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, ready_pr(40), ready_pr(40)),
                             10: (IN_PROGRESS, draft(43), draft(43)), 11: (IN_PROGRESS, draft(44), draft(44))})

        p = self.ok("dispatch", "--dry-run")
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {7: "early", 8: "ok", 10: "early", 11: "early"})
        starts = {n: [ln for ln in p.stdout.splitlines() if ln.startswith(f"would start #{n} ")] for n in (7, 8, 10, 11)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {7: 1, 8: 0, 10: 1, 11: 1}, p.stdout)
        self.assertIn("#43", starts[10][0])
        self.assertNotIn("#44", starts[10][0])
        self.assertIn("#44", starts[11][0])
        self.assertNotIn("#43", starts[11][0])
        calls = self.gh_calls()
        for n in (7, 8, 10, 11):
            with self.subTest(ticket=n):
                views = [a for a in calls if a[:3] == ["issue", "view", str(n)]]
                heads = [a for a in calls if a[:2] == ["pr", "list"] and "--head" in a
                         and a[a.index("--head") + 1] == f"t/{n}-x"]
                self.assertLessEqual(len(views), 1, calls)
                self.assertLessEqual(len(heads), 1, calls)
                if n != 8:                                  # early needs both facts: asked exactly once each
                    self.assertEqual((len(views), len(heads)), (1, 1), calls)

    def test_every_pull_request_of_the_branch_counts_and_the_open_draft_is_named(self):
        # A branch can have several PRs (gh lists them newest first). Early means none of them, in any state, is
        # ready: an old closed or merged non-draft PR beside an open draft is a normal end, wherever it sits in the
        # list (a row without isDraft is not a draft). Only drafts, closed ones included, is early, and the state
        # names the open draft, the newest. gh is asked with --state all; the rows are what it would return.
        def pr(number, state, is_draft=None):
            row = {"number": number, "state": state}
            if is_draft is not None:
                row["isDraft"] = is_draft
            return row
        cases = {   # n: (PRs of any state, newest first; outcome, state)
            30: ([pr(81, "OPEN", True), pr(80, "CLOSED", False)], "ok", "ended ok"),
            31: ([pr(83, "OPEN", True), pr(82, "MERGED", False)], "ok", "ended ok"),
            32: ([pr(85, "OPEN", True), pr(84, "CLOSED", True)], "early", "ended early (draft PR #85)"),
            33: ([pr(87, "CLOSED", False), pr(86, "OPEN", True)], "ok", "ended ok"),
            34: ([pr(89, "OPEN", True), pr(88, "MERGED")], "ok", "ended ok"),
            35: ([pr(93, "OPEN", True), pr(92, "CLOSED", True), pr(91, "CLOSED", True)], "early",
                 "ended early (draft PR #93)"),
        }
        for n in cases:
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({n: (IN_PROGRESS, [r for r in prs if r["state"] == "OPEN"], prs)
                             for n, (prs, _o, _s) in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: c[1] for n, c in cases.items()})
        self.assertEqual({n: rows[n]["state"] for n in rows}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {n: c[1] for n, c in cases.items()})

        p = self.ok("dispatch", "--dry-run")
        started = {int(m.group(1)): line for line in p.stdout.splitlines()
                   for m in [re.match(r"would start #(\d+) ", line)] if m}
        self.assertEqual(sorted(started), [32, 35], p.stdout)
        self.assertIn("#85", started[32])
        self.assertNotIn("#84", started[32])
        self.assertIn("#93", started[35])
        self.assertNotIn("#92", started[35])
        self.assertNotIn("#91", started[35])

    # ---- #57: bg is the default venue, so a bg run that ends `done` is judged like a headless `ok`
    def bg_runs(self, cases):
        """cases: {n: (bg id, `claude agents` state)}. Writes an unended bg run record for each ticket (repo proj,
        branch t/<n>-x, no pid, no log) and returns the FAKECLAUDE_AGENTS listing that reports each state. The
        listing gives no exitCode, as `claude agents --json` documents none."""
        for n, (bg_id, _state) in cases.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        return json.dumps([{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo,
                            "name": f"proj #{n} {TITLE}"} for n, (bg_id, state) in cases.items()])

    def test_a_done_bg_run_on_an_in_progress_ticket_without_a_ready_pr_ended_early(self):
        # The same facts that make a headless `ok` early make a bg `done` early: the issue OPEN, status:in-progress,
        # and no ready PR of its branch in any state (none, or only drafts). The early run keeps the newest open
        # draft as draft_pr. Anything else that ends `done` stays `ok`; `failed` and `stopped` are never upgraded,
        # whatever the board says. gh failing for a done run leaves it `ok` (the facts are missing), as headless.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        needs_input = ("task", "status:needs-input", "size:S", "risk:low")
        merged = [{"number": 95, "state": "MERGED", "isDraft": False}]
        cases = {   # n: (bg id, agents state, labels, open PRs, PRs of any state, outcome, state shown)
            40: ("s-9f1", "done", IN_PROGRESS, [], [], "early", "ended early (no PR)"),
            41: ("s-0a7", "done", IN_PROGRESS, draft(91), draft(91), "early", "ended early (draft PR #91)"),
            42: ("s-c3e", "done", IN_PROGRESS, draft(57), draft(57), "early", "ended early (draft PR #57)"),
            43: ("s-b12", "done", IN_PROGRESS, ready_pr(92), ready_pr(92), "ok", "ended ok"),
            44: ("s-77d", "done", in_review, [], [], "ok", "ended ok"),
            45: ("s-e05", "done", in_review, draft(93), draft(93), "ok", "ended ok"),
            46: ("s-5aa", "done", IN_PROGRESS, [], [], "ok", "ended ok"),          # the issue is CLOSED (below)
            47: ("s-2d4", "done", IN_PROGRESS, [], merged, "ok", "ended ok"),
            51: ("s-6c0", "done", needs_input, [], [], "ok", "ended ok"),
            52: ("s-31f", "done", IN_PROGRESS, [], [], "ok", "ended ok"),          # gh issue view fails (below)
            48: ("s-d8b", "failed", IN_PROGRESS, [], [], "error", "ended error"),
            49: ("s-4e9", "failed", IN_PROGRESS, draft(94), draft(94), "error", "ended error"),
            50: ("s-a60", "stopped", IN_PROGRESS, [], [], "stopped", "ended stopped"),
        }
        agents = self.bg_runs({n: c[:2] for n, c in cases.items()})
        self.write_scenario({n: c[2:5] for n, c in cases.items()}, states={46: "CLOSED"}, failing={52: "issue"})

        p = self.work("runs", "--sweep", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        expected = {n: c[5] for n, c in cases.items()}
        shown = {n: c[6] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({n: rows[n]["state"] for n in rows}, shown)
        for n in cases:
            self.assertTrue(rows[n]["ended_at"], rows[n])

        saved = {r["ticket"]: r for r in self.run_records()}
        self.assertEqual({n: r["outcome"] for n, r in saved.items()}, expected)
        self.assertEqual({n: saved[n].get("draft_pr") for n in (40, 41, 42)}, {40: None, 41: 91, 42: 57})
        for n in (43, 44, 45, 46, 47, 48, 49, 50, 51, 52):         # only an early run names a draft
            self.assertIsNone(saved[n].get("draft_pr"), saved[n])

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")}, shown)
        lines = self.run_lines()
        for n, state in shown.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                self.assertEqual("ended early" in lines[n], expected[n] == "early", lines[n])
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

    def test_only_the_board_and_the_pr_decide_a_bg_early_never_the_ticket_or_the_session_id(self):
        # Each ticket keeps its bg id across the passes while its agents state, labels and PRs rotate, so neither the
        # ticket number nor the session id can tell early from ok; a done run's outcome follows the board and the
        # branch's PRs, a failed run stays error.
        states = [   # (agents state, labels, open PRs, PRs of any state, outcome, state shown)
            ("done", IN_PROGRESS, [], [], "early", "ended early (no PR)"),
            ("done", IN_PROGRESS, draft(66), draft(66), "early", "ended early (draft PR #66)"),
            ("done", IN_PROGRESS, ready_pr(67), ready_pr(67), "ok", "ended ok"),
            ("failed", IN_PROGRESS, [], [], "error", "ended error"),
            ("done", ("task", "status:in-review", "size:S", "risk:low"), draft(68), draft(68), "ok", "ended ok"),
        ]
        tickets = {9: "s-x1", 23: "s-k4", 58: "s-q7", 104: "s-m2", 311: "s-z9"}
        for k in range(len(states)):
            self.fresh()
            plan = {n: states[(j + k) % len(states)] for j, n in enumerate(tickets)}
            agents = self.bg_runs({n: (bg_id, plan[n][0]) for n, bg_id in tickets.items()})
            self.write_scenario({n: s[1:4] for n, s in plan.items()})
            rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                         env=self.env(FAKECLAUDE_AGENTS=agents))}
            with self.subTest(rotation=k):
                self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: s[4] for n, s in plan.items()})
                self.assertEqual({n: rows[n]["state"] for n in rows}, {n: s[5] for n, s in plan.items()})
                self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                                 {n: s[4] for n, s in plan.items()})

    def test_dispatch_runs_a_bg_early_ticket_again_after_early(self):
        # One dispatch sweeps four bg runs and plans the redispatch: #40 (done, no PR) and #41 (done, only draft #91)
        # go again after early, the draft named; #43 (done, ready PR) is not run again; #48 (failed) goes again after
        # error, not early.
        cases = {40: ("s-9f1", "done", []), 41: ("s-0a7", "done", draft(91)),
                 43: ("s-b12", "done", ready_pr(92)), 48: ("s-d8b", "failed", [])}
        agents = self.bg_runs({n: c[:2] for n, c in cases.items()})
        self.write_scenario({n: (IN_PROGRESS, c[2], c[2]) for n, c in cases.items()})

        p = self.ok("dispatch", "--dry-run", env=self.env(FAKECLAUDE_AGENTS=agents))
        starts = {int(m.group(1)): line for line in p.stdout.splitlines()
                  for m in [re.match(r"would start #(\d+) ", line)] if m}
        self.assertEqual(sorted(starts), [40, 41, 48], p.stdout)
        self.assertIn("would start #40 Add the board tool (again after early)", starts[40])
        self.assertIn("(again after early", starts[41])
        self.assertIn("#91", starts[41])
        self.assertIn("(again after error)", starts[48])
        self.assertNotIn("early", starts[48])
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {40: "early", 41: "early", 43: "ok", 48: "error"})
        self.assertEqual(next(r for r in self.run_records() if r["ticket"] == 41).get("draft_pr"), 91)

        data = self.ok_json("dispatch", "--dry-run", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual({row["number"]: row.get("again") for row in data["plan"]},
                         {40: "early", 41: "early", 48: "error"})

    def test_an_early_bg_run_shows_early_even_when_the_listing_gives_an_exit_code(self):
        # Should `claude agents` give an exitCode, the record keeps it, but an early end is what `runs` shows: the 0
        # of a done session says less than that its ticket is still in progress without a ready PR. Any other ended
        # run with an exit code still shows `exited N` (a done one with a ready PR, a failed one with or without
        # a draft), each with its own code.
        cases = {   # n: (bg id, agents state, exitCode, open PRs = PRs of any state, outcome, state shown)
            60: ("s-e01", "done", 0, [], "early", "ended early (no PR)"),
            61: ("s-e02", "done", 0, draft(88), "early", "ended early (draft PR #88)"),
            62: ("s-e03", "done", 0, draft(47), "early", "ended early (draft PR #47)"),
            63: ("s-e04", "done", 0, ready_pr(89), "ok", "exited 0"),
            64: ("s-e05", "failed", 3, [], "error", "exited 3"),
            65: ("s-e06", "failed", 1, draft(90), "error", "exited 1"),
        }
        self.bg_runs({n: c[:2] for n, c in cases.items()})
        agents = json.dumps([{"id": c[0], "kind": "background", "state": c[1], "exitCode": c[2], "cwd": self.repo,
                              "name": f"proj #{n} {TITLE}"} for n, c in cases.items()])
        self.write_scenario({n: (IN_PROGRESS, c[3], c[3]) for n, c in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                     env=self.env(FAKECLAUDE_AGENTS=agents))}
        expected = {n: c[4] for n, c in cases.items()}
        shown = {n: c[5] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({n: rows[n]["state"] for n in rows}, shown)
        saved = {r["ticket"]: r for r in self.run_records()}
        self.assertEqual({n: saved[n]["exit"] for n in saved}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({n: saved[n].get("draft_pr") for n in (60, 61, 62)}, {60: None, 61: 88, 62: 47})

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")}, shown)
        lines = self.run_lines()
        for n, state in shown.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                self.assertEqual("exited" in lines[n], expected[n] != "early", lines[n])


def draft(number):
    """`pr list` rows: one open draft pull request."""
    return [{"number": number, "state": "OPEN", "isDraft": True}]


def ready_pr(number):
    """`pr list` rows: one open pull request marked ready for review."""
    return [{"number": number, "state": "OPEN", "isDraft": False}]


# =========================================================================== #123: stop the session of a finished ticket
# The bg supervisor marks a finished session `blocked` (not `done`) when its summary of the final reply suggests a next
# step for the user (`/harness:land 121`), so the `claude agents` state cannot tell a finished run from a live one. The
# ticket decides: a session whose ticket is status:in-review, status:needs-input or closed has ended, whatever its
# state; the sweep closes its record and runs `claude stop <id>`. A session whose ticket is status:in-progress is never
# stopped, and a live-state one stays live.
IN_REVIEW = ("task", "status:in-review", "size:S", "risk:low")
NEEDS_INPUT = ("task", "status:needs-input", "size:S", "risk:low")
NO_STATUS = ("task", "size:S", "risk:low")
READY = ("task", "status:ready", "size:S", "risk:low")
BLOCKED_STATUS = ("task", "status:blocked", "size:S", "risk:low")
MERGED = [{"number": 140, "state": "MERGED", "isDraft": False}]
STOPPED_RE = re.compile(r"stopped session ([A-Za-z0-9_-]+)")


class StopFinishedSessions(WorkTest):
    def board_scenario(self, tickets, ready=(), failing=()):
        """tickets: {n: (labels, issue state, PRs of its branch t/<n>-x)}; `ready` the rows of `harness-board ready`.
        For a ticket in `failing`, `gh issue view n` exits 1 with a message on stderr, as gh does when the network or
        the token is gone."""
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        calls = [dict(gone, match=["issue", "view", str(n)]) if n in failing else
                 {"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels=labels, state=state)}
                 for n, (labels, state, _prs) in tickets.items()]
        for n, (_labels, _state, prs) in tickets.items():
            open_prs = [pr for pr in prs if pr["state"] == "OPEN"]
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x", "--state", "all"], "stdout_json": prs})
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x"], "stdout_json": open_prs})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": list(ready)},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def bg_sessions(self, cases):
        """cases: {n: (bg id, `claude agents` state)}. Writes an unended bg run record per ticket and returns the
        FAKECLAUDE_AGENTS listing that reports each state."""
        for n, (bg_id, _state) in cases.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        return json.dumps([{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo,
                            "name": f"proj #{n} {TITLE}"} for n, (bg_id, state) in cases.items()])

    def stops(self):
        """{session id: how many times `claude stop` was called with it}."""
        counts = {}
        for call in self.claude_calls():
            argv = call["argv"]
            if argv[:1] == ["stop"]:
                for arg in argv[1:]:
                    if not arg.startswith("-"):
                        counts[arg] = counts.get(arg, 0) + 1
        return counts

    def fresh(self):
        """Forget every run record, claude call and gh call, for the next pass of a test."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.claude_log, self.gh_log):
            if os.path.exists(path):
                os.unlink(path)

    def records(self):
        return {r["ticket"]: r for r in self.run_records()}

    def reported(self, p):
        """The session ids named on a `stopped session <id>` line of the command's output."""
        return sorted(re.findall(STOPPED_RE, p.stdout + p.stderr))

    def warnings(self, p):
        """The command's stderr lines other than a `stopped session <id>` report."""
        return [line for line in p.stderr.splitlines() if line.strip() and not re.search(STOPPED_RE, line)]

    # ---- broad: the ticket's status decides, across agents states, statuses and PR states
    def test_sweep_stops_the_session_of_every_finished_ticket_and_of_no_in_progress_one(self):
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, stopped, record closed, outcome)
            70: ("s-a11", "blocked", IN_REVIEW, "OPEN", ready_pr(120), True, True, None),
            71: ("s-b22", "blocked", NEEDS_INPUT, "OPEN", [], True, True, None),
            72: ("s-c33", "blocked", IN_REVIEW, "CLOSED", MERGED, True, True, None),
            73: ("s-d44", "done", IN_REVIEW, "OPEN", ready_pr(121), True, True, "ok"),
            74: ("s-e55", "done", NEEDS_INPUT, "OPEN", draft(122), True, True, "ok"),
            75: ("s-f66", "done", NO_STATUS, "CLOSED", MERGED, True, True, "ok"),
            76: ("s-g77", "idle", NEEDS_INPUT, "OPEN", [], True, True, None),
            77: ("s-h88", "blocked", IN_PROGRESS, "OPEN", [], False, False, None),
            78: ("s-i99", "blocked", IN_PROGRESS, "OPEN", ready_pr(123), False, False, None),
            79: ("s-j10", "idle", IN_PROGRESS, "OPEN", draft(124), False, False, None),
            80: ("s-k20", "running", IN_PROGRESS, "OPEN", [], False, False, None),
            81: ("s-l30", "done", IN_PROGRESS, "OPEN", ready_pr(125), False, True, "ok"),
            82: ("s-m40", "done", IN_PROGRESS, "OPEN", [], False, True, "early"),
        }
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        self.board_scenario({n: c[2:5] for n, c in cases.items()})

        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stopped = sorted(c[0] for c in cases.values() if c[5])
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in stopped}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), stopped, p.stdout + p.stderr)

        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, state=c[1], labels=c[2], issue=c[3]):
                self.assertEqual(bool(saved[n]["ended_at"]), c[6], saved[n])
                if not c[6]:
                    self.assertIsNone(saved[n]["outcome"], saved[n])
                if c[7] is not None:
                    self.assertEqual(saved[n]["outcome"], c[7], saved[n])

    def test_the_board_decides_the_stop_never_the_ticket_number_or_the_session_id(self):
        # Each ticket keeps its session id while its status rotates; every session reports `blocked` throughout. Only
        # the ticket's status and state can tell which sessions to stop and which records to close.
        statuses = [   # (labels, issue state, PRs, stop and close)
            (IN_REVIEW, "OPEN", ready_pr(150), True),
            (IN_PROGRESS, "OPEN", [], False),
            (NEEDS_INPUT, "OPEN", draft(151), True),
            (IN_PROGRESS, "OPEN", ready_pr(152), False),
            (IN_REVIEW, "CLOSED", MERGED, True),
            (READY, "OPEN", [], False),
            (IN_PROGRESS, "CLOSED", MERGED, True),          # closed wins over a leftover in-progress label
            (BLOCKED_STATUS, "OPEN", draft(153), False),
        ]
        tickets = {9: "s-x1", 23: "s-k4", 58: "s-q7", 104: "s-m2", 311: "s-z9", 412: "s-p3", 77: "s-w8"}
        for k in range(len(statuses)):
            self.fresh()
            plan = {n: statuses[(j + k) % len(statuses)] for j, n in enumerate(tickets)}
            agents = self.bg_sessions({n: (bg_id, "blocked") for n, bg_id in tickets.items()})
            self.board_scenario({n: s[:3] for n, s in plan.items()})
            p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
            with self.subTest(rotation=k):
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                expected = sorted(tickets[n] for n, s in plan.items() if s[3])
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in expected}, p.stdout + p.stderr)
                self.assertEqual(self.reported(p), expected, p.stdout + p.stderr)
                self.assertEqual({n: bool(r["ended_at"]) for n, r in self.records().items()},
                                 {n: s[3] for n, s in plan.items()})

    def test_only_in_review_needs_input_or_closed_end_a_session_and_an_unreadable_ticket_stays_live(self):
        # The finished set is exactly status:in-review, status:needs-input or a CLOSED issue (closed wins over any
        # leftover label, status:in-progress included). An OPEN ticket with status:ready, status:blocked or no status
        # label is not finished: its live session is never stopped and holds a slot. Nor is a ticket gh cannot read.
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, finished)
            120: ("s-rd0", "blocked", READY, "OPEN", [], False),
            121: ("s-bk1", "blocked", BLOCKED_STATUS, "OPEN", draft(190), False),
            122: ("s-ns2", "blocked", NO_STATUS, "OPEN", [], False),
            123: ("s-ci3", "blocked", IN_PROGRESS, "CLOSED", MERGED, True),
            124: ("s-gf4", "blocked", IN_REVIEW, "OPEN", ready_pr(191), False),     # gh issue view fails (below)
            125: ("s-ci5", "done", IN_PROGRESS, "CLOSED", MERGED, True),
            126: ("s-rv6", "blocked", IN_REVIEW, "OPEN", ready_pr(192), True),
            127: ("s-rd7", "idle", READY, "OPEN", [], False),
        }
        failing = {124}
        finished = sorted(c[0] for c in cases.values() if c[5])
        ready = [ready_row(n, f"Ready {n}") for n in range(140, 150)]

        # runs --sweep
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        self.board_scenario({n: c[2:5] for n, c in cases.items()}, failing=failing)
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), finished, p.stdout + p.stderr)
        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, labels=c[2], issue=c[3], gh_fails=n in failing):
                self.assertEqual(bool(saved[n]["ended_at"]), c[5], saved[n])
                if not c[5]:
                    self.assertIsNone(saved[n]["outcome"], saved[n])
        self.assertEqual(saved[125]["outcome"], "ok")
        shown = {r["ticket"]: r["state"] for r in self.ok_json("runs", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))}
        self.assertEqual({n: shown[n] for n, c in cases.items() if not c[5]},
                         {n: c[1] for n, c in cases.items() if not c[5]})

        # dispatch: the unfinished sessions are the live runs and each holds a slot; the finished ones hold none.
        live_tickets = sorted(n for n, c in cases.items() if not c[5])
        for most in (len(live_tickets), len(live_tickets) + 1, len(live_tickets) + 3):
            with self.subTest(max=most):
                self.fresh()
                agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
                ids = self.records()
                self.board_scenario({n: c[2:5] for n, c in cases.items()}, ready=ready, failing=failing)
                env = self.env(FAKECLAUDE_AGENTS=agents)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                self.assertEqual(sorted(data["live"]), sorted(ids[n]["id"] for n in live_tickets), data)
                self.assertEqual(len(data["plan"]), most - len(live_tickets), data)
                self.assertTrue({row["number"] for row in data["plan"]} <= set(range(140, 150)), data)
                self.work("runs", "--sweep", env=env)
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished})

    def issue_views(self, n):
        """How many times gh was asked for `issue view n`."""
        return sum(1 for argv in self.gh_calls() if argv[:3] == ["issue", "view", str(n)])

    # ---- review of the fix: a stopped session, an unreadable done one, one issue read per command
    def test_a_stopped_session_is_never_stopped_again_and_its_ticket_is_not_read(self):
        # `claude agents` says #200's session is already `stopped`: the sweep closes its record as `stopped` without a
        # `claude stop`, a report or a read of its ticket, though the ticket is in review. Beside it, #201's blocked
        # session of an in-review ticket is stopped as usual (its ticket read once).
        agents = self.bg_sessions({200: ("s-st0", "stopped"), 201: ("s-bl1", "blocked")})
        self.board_scenario({200: (IN_REVIEW, "OPEN", ready_pr(200)), 201: (IN_REVIEW, "OPEN", ready_pr(201))})
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-bl1": 1})
        self.assertEqual(self.reported(p), ["s-bl1"])
        self.assertEqual(self.issue_views(200), 0, self.gh_calls())
        self.assertEqual(self.issue_views(201), 1, self.gh_calls())
        saved = self.records()
        self.assertTrue(saved[200]["ended_at"])
        self.assertEqual(saved[200]["outcome"], "stopped")
        self.assertTrue(saved[201]["ended_at"])

    def test_a_done_session_whose_ticket_cannot_be_read_closes_ok_with_one_warning(self):
        # gh issue view fails for #210: its `done` session is not stopped, its record closes `ok` (not `early`: the
        # early check needs the same unreadable issue and is skipped), and the command warns exactly once, naming it.
        for labels in (IN_PROGRESS, IN_REVIEW):
            with self.subTest(labels=labels):
                self.fresh()
                agents = self.bg_sessions({210: ("s-dn0", "done")})
                self.board_scenario({210: (labels, "OPEN", [])}, failing={210})
                p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.stops(), {})
                self.assertEqual(self.reported(p), [])
                warnings = self.warnings(p)
                self.assertEqual(len(warnings), 1, p.stderr)
                self.assertIn("#210", warnings[0])
                saved = self.records()[210]
                self.assertTrue(saved["ended_at"], saved)
                self.assertEqual(saved["outcome"], "ok", saved)
                self.assertIsNone(saved.get("draft_pr"), saved)

    def test_one_issue_read_per_ticket_per_command_for_the_finished_and_early_checks(self):
        # Done sessions of in-progress tickets go through both the finished check and the early check (#220 ends
        # early, #221 has a ready PR and ends ok); #222's blocked in-progress session through the finished check only.
        # Each command reads each issue once.
        cases = {220: ("s-on0", "done", []), 221: ("s-on1", "done", ready_pr(221)), 222: ("s-on2", "blocked", [])}
        for command in (("runs", "--sweep"), ("dispatch", "--dry-run"), ("dispatch", "--dry-run", "--json")):
            with self.subTest(command=command):
                self.fresh()
                agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
                self.board_scenario({n: (IN_PROGRESS, "OPEN", c[2]) for n, c in cases.items()},
                                    ready=[ready_row(230, "Ready 230")])
                p = self.work(*command, env=self.env(FAKECLAUDE_AGENTS=agents))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual({n: self.issue_views(n) for n in cases}, {n: 1 for n in cases}, self.gh_calls())
                self.assertEqual(self.stops(), {})
                outcomes = {n: r["outcome"] for n, r in self.records().items()}
                self.assertEqual(outcomes, {220: "early", 221: "ok", 222: None})

    # ---- the four cases the ticket names
    def test_a_blocked_session_of_an_in_review_ticket_is_stopped_once_and_frees_its_dispatch_slot(self):
        # runs --sweep alone: one `claude stop <id>`, reported, and the record closed.
        agents = self.bg_sessions({90: ("s-rev1", "blocked")})
        self.board_scenario({90: (IN_REVIEW, "OPEN", ready_pr(160))})
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-rev1": 1})
        self.assertIn("stopped session s-rev1", p.stdout + p.stderr)
        self.assertTrue(self.records()[90]["ended_at"])

        # dispatch: the session does not hold a slot, so --max N plans N launches, for each finished status. Whichever
        # command stops the session, it is stopped once in all.
        ready = [ready_row(n, f"Ready {n}") for n in (91, 92, 93, 94)]
        for labels, state in ((IN_REVIEW, "OPEN"), (NEEDS_INPUT, "OPEN"), (IN_REVIEW, "CLOSED")):
            for most in (1, 2, 3):
                with self.subTest(labels=labels, state=state, max=most):
                    self.fresh()
                    agents = self.bg_sessions({90: ("s-rev1", "blocked")})
                    self.board_scenario({90: (labels, state, ready_pr(160))}, ready=ready)
                    env = self.env(FAKECLAUDE_AGENTS=agents)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                    self.assertEqual(data["live"], [])
                    self.assertEqual(len(data["plan"]), most, data)
                    self.assertTrue({row["number"] for row in data["plan"]} <= {91, 92, 93, 94}, data)
                    p = self.work("runs", "--sweep", env=env)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(self.stops(), {"s-rev1": 1})
                    self.assertTrue(self.records()[90]["ended_at"])

        # The dry-run text says the same.
        self.fresh()
        agents = self.bg_sessions({90: ("s-rev1", "blocked")})
        self.board_scenario({90: (IN_REVIEW, "OPEN", ready_pr(160))}, ready=ready)
        p = self.ok("dispatch", "--dry-run", "--max", "2", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertIn("Dry run: venue bg, 0 live, 2 of 2 slots free.", p.stdout)
        self.assertEqual(len(re.findall(r"^would start #", p.stdout, re.M)), 2, p.stdout)

    def test_a_done_session_of_a_closed_ticket_is_stopped_by_the_first_sweep_only(self):
        agents = self.bg_sessions({95: ("s-cl5", "done")})
        # A record a sweep closed before (#103's case: outcome ok, its session still listed): never stopped again.
        self.write_run(96, id=f"{self.key}-96-20261012120000", venue="bg", bg_id="s-old6", log=None,
                       ended_at=iso_ago(hours=2), outcome="ok")
        listing = json.loads(agents) + [{"id": "s-old6", "kind": "background", "state": "done", "cwd": self.repo,
                                         "name": f"proj #96 {TITLE}"}]
        env = self.env(FAKECLAUDE_AGENTS=json.dumps(listing))
        self.board_scenario({95: (IN_REVIEW, "CLOSED", MERGED), 96: (IN_REVIEW, "OPEN", ready_pr(161))})

        first = self.work("runs", "--sweep", env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.stops(), {"s-cl5": 1})
        self.assertEqual(self.reported(first), ["s-cl5"])
        saved = self.records()
        self.assertTrue(saved[95]["ended_at"])
        self.assertEqual(saved[95]["outcome"], "ok")

        # The listing still shows the session (as when a stop has not taken effect yet): the closed record is skipped.
        second = self.work("runs", "--sweep", env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.stops(), {"s-cl5": 1})
        self.assertEqual(self.reported(second), [])
        self.ok("dispatch", "--dry-run", env=env)
        self.assertEqual(self.stops(), {"s-cl5": 1})

    def test_a_blocked_session_of_an_in_progress_ticket_stays_live_and_counts_toward_max(self):
        # Beside it, a blocked session of an in-review ticket (#99), which holds no slot: --max N plans N - 1 launches,
        # the in-progress session is the one live run, and only #99's session is ever stopped, whatever #100's PRs.
        ready = [ready_row(n, f"Ready {n}") for n in (101, 102, 103, 104)]
        for prs in ([], draft(170), ready_pr(171)):
            for most in (1, 2, 3, 4):
                with self.subTest(prs=prs, max=most):
                    self.fresh()
                    agents = self.bg_sessions({100: ("s-ip0", "blocked"), 99: ("s-rv9", "blocked")})
                    rid = self.records()[100]["id"]
                    self.board_scenario({100: (IN_PROGRESS, "OPEN", prs), 99: (IN_REVIEW, "OPEN", ready_pr(172))},
                                        ready=ready)
                    env = self.env(FAKECLAUDE_AGENTS=agents)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                    self.assertEqual(data["live"], [rid])
                    self.assertEqual(len(data["plan"]), most - 1, data)
                    p = self.work("runs", "--sweep", env=env)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(self.stops(), {"s-rv9": 1})
                    self.assertNotIn("s-ip0", self.reported(p))
                    saved = self.records()[100]
                    self.assertIsNone(saved["ended_at"], saved)
                    self.assertIsNone(saved["outcome"], saved)
                    self.assertTrue(self.records()[99]["ended_at"])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--json", env=env)}
        self.assertEqual(rows[100]["state"], "blocked")

    def test_a_failed_stop_is_one_warning_leaves_exit_0_and_still_closes_the_record(self):
        agents = self.bg_sessions({110: ("s-bad0", "blocked"), 111: ("s-ok11", "done")})
        self.board_scenario({110: (IN_REVIEW, "OPEN", ready_pr(180)), 111: (NEEDS_INPUT, "OPEN", [])})
        env = self.env(FAKECLAUDE_AGENTS=agents, FAKECLAUDE_STOP_FAIL="s-bad0")

        p = self.work("runs", "--sweep", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-bad0": 1, "s-ok11": 1})
        warnings = self.warnings(p)
        self.assertEqual(len(warnings), 1, p.stderr)
        self.assertIn("s-bad0", warnings[0])
        self.assertTrue(warnings[0].startswith("harness-work: "), warnings[0])
        self.assertEqual(self.reported(p), ["s-ok11"])
        saved = self.records()
        self.assertTrue(saved[110]["ended_at"], saved[110])
        self.assertTrue(saved[111]["ended_at"], saved[111])

        # The same with --json: the JSON on stdout is whole, the one warning on stderr.
        self.fresh()
        agents = self.bg_sessions({110: ("s-bad0", "blocked")})
        self.board_scenario({110: (IN_REVIEW, "OPEN", ready_pr(180))})
        env = self.env(FAKECLAUDE_AGENTS=agents, FAKECLAUDE_STOP_FAIL="s-bad0")
        p = self.work("runs", "--sweep", "--json", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        self.assertTrue(rows[110]["ended_at"], rows[110])
        warnings = self.warnings(p)
        self.assertEqual(len(warnings), 1, p.stderr)
        self.assertIn("s-bad0", warnings[0])
        self.assertEqual(self.stops(), {"s-bad0": 1})
        self.assertTrue(self.records()[110]["ended_at"])


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

    def write_scenario(self, drafts, ready=()):
        """drafts: {ticket: (branch, its draft PR number, or None for no PR)}; every ticket OPEN and
        status:in-progress. ready: tickets on the board as status:ready (no run, no PR), listed by `harness-board
        ready`."""
        calls = [{"match": ["issue", "view", str(n)],
                  "stdout_json": issue(n, labels=IN_PROGRESS, title=TITLE if n == 12 else f"Ticket {n}")}
                 for n in drafts]
        calls += [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, title=f"Ticket {n}")} for n in ready]
        for n, (branch, pr) in drafts.items():
            calls.append({"contains": ["pr", "list", "--head", branch], "stdout_json": draft(pr) if pr else []})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": [ready_row(n, f"Ticket {n}") for n in ready]},
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
        p = self.ok("dispatch", "--venue", "headless", "--max", "1")
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
        self.assertIn("review", prompt.lower())                # it reads the PR's review comments first
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

    def test_only_a_draft_pull_request_puts_the_pr_and_uncommitted_work_in_the_prompt(self):
        # One dispatch launches three tickets: #12 after an early run with draft PR #41 (the #31 case), #15 after an
        # early run with no PR, #16 fresh from the board (status:ready, never run). Only #12's prompt and start
        # comment name a PR and the uncommitted work; the other two get the plain prompt and no such comment, so the
        # PR text cannot be a constant of every launch.
        self.ended_run(12, BRANCH, self.wt)
        self.ended_run(15, "t/15-x", os.path.join(self.repo, ".claude", "worktrees", "t-15-x"), result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", None)}, ready=[16])

        p = self.ok("dispatch", "--dry-run")
        starts = {n: [ln for ln in p.stdout.splitlines() if ln.startswith(f"would start #{n} ")] for n in (12, 15, 16)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {12: 1, 15: 1, 16: 1}, p.stdout)
        self.assertIn("#41", starts[12][0])
        self.assertIn("(again after early)", starts[15][0])    # no PR: the reason as before #42
        self.assertNotIn("#41", starts[15][0] + starts[16][0])

        p = self.ok("dispatch", "--venue", "headless", "--max", "3")
        launched = [r for r in self.run_records() if r.get("ended_at") is None]
        self.assertEqual(sorted(r["ticket"] for r in launched), [12, 15, 16], p.stdout)
        for record in launched:
            self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(record["log"])))
            self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        prompts = {}
        for call in self.claude_calls():
            argv = call["argv"]
            self.assertEqual(argv[0], "-p", argv)
            m = re.match(r"/harness:work (\d+)\b", argv[1])
            self.assertTrue(m, argv[1])
            prompts[int(m.group(1))] = argv[1]
        self.assertEqual(sorted(prompts), [12, 15, 16])

        pr_words = re.compile(r"\bPR\b|pull request", re.IGNORECASE)
        self.assertIn("#41", prompts[12])
        self.assertIn("uncommitted", prompts[12].lower())
        self.assertIn("git status", prompts[12])
        self.assertEqual(prompts[16], "/harness:work 16")     # a plain dispatch: the prompt is unchanged
        for n in (15, 16):
            with self.subTest(ticket=n):
                prompt = prompts[n]
                self.assertNotIn("uncommitted", prompt.lower())
                self.assertNotIn("git status", prompt)
                self.assertNotIn("review", prompt.lower())
                self.assertNotIn("#41", prompt)
                self.assertNotRegex(prompt, pr_words)

        self.assertTrue([b for b in self.comment_bodies(12) if "#41" in b and "uncommitted" in b.lower()],
                        self.comment_bodies(12))
        self.assertTrue(self.comment_bodies(16))               # the plain "Started on ..." comment
        for n in (15, 16):
            with self.subTest(ticket=n, comment=True):
                for body in self.comment_bodies(n):
                    self.assertNotIn("uncommitted", body.lower())
                    self.assertNotIn("#41", body)
                    self.assertNotRegex(body, pr_words)


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


def dispatch_section(body=None):
    """The text under the skill's dispatch heading, up to the next heading."""
    body = skill_body() if body is None else body
    m = re.search(r"(?im)^#{1,6} .*\bdispatch\b.*$", body)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"(?m)^#{1,6} ", rest)
    return rest[:nxt.start()] if nxt else rest


def argument_hint():
    m = re.search(r"(?m)^argument-hint:\s*(.*)$", skill_front())
    return m.group(1) if m else ""


def dispatch_venue_problems(section, hint):
    """What the dispatch section and the argument-hint get wrong about ticket #57's venues; [] when they say: the
    hint offers `--api` and `--venue`, the subscription is the default, `--api` reaches harness-work dispatch (as its
    own flag passed through, or as `--venue headless`) and bills the API key, and every mention of headless or the
    API key is tied to that opt-in."""
    problems = []
    if "--api" not in hint:
        problems.append("the argument-hint has no --api")
    if "--venue" not in hint:
        problems.append("the argument-hint has no --venue")
    sentences = sentences_of(" ".join(section.replace("`", "").split()).lower())
    clauses = [c for s in sentences for c in re.split(r";\s*", s)]
    if not any("harness-work dispatch" in s and ("$arguments" in s or "--api" in s or "--venue headless" in s)
               for s in sentences):
        problems.append("no sentence passes --api to harness-work dispatch")
    # The default is the subscription, unconditionally: a clause that says so and names no budget condition.
    if not any("subscription" in c and says_default(c) and not BUDGET_WORDS.search(c) for c in clauses):
        problems.append("no clause says the subscription is the default, unconditionally")
    for c in clauses:
        if "subscription" in c and BUDGET_CONDITION.search(c):
            problems.append(f"the subscription is tied to an exhausted budget: {c}")
        if says_default(c) and re.search(r"\bheadless\b|\bapi key\b", c):
            problems.append(f"headless or the api key as a default: {c}")
    if not any(opt_in_trigger(s) and "api key" in strip_parens(s) and re.search(r"\bbill", s) for s in sentences):
        problems.append("no sentence says --api bills the api key")
    for s in sentences:
        if re.search(r"\bheadless\b|\bapi key\b", s) and not opt_in_trigger(s):
            problems.append(f"headless or the api key without --api as its trigger: {s}")
        if re.search(r"--api\b[^.;]*\bimpl(?:ied|icit)", s):
            problems.append(f"--api as implied: {s}")
    return problems


# A budget as the condition of what precedes it ("once the api budget is exhausted", "only while the budget lasts");
# "whatever the budgets say" is not one.
BUDGET_WORDS = re.compile(r"\b(?:once|unless|only|when|while|if|until|after)\b[^.;]*\b(?:exhausted|budgets?)\b")
BUDGET_CONDITION = re.compile(r"\b(?:once|unless|only when|when|while|if|until|after)\b[^.;]*\bexhausted\b")
DEFAULT = re.compile(r"\bdefault\b")


def says_default(text):
    """A 'default' in lower-cased `text` that is not negated."""
    return any(not negated(text, m.start()) for m in DEFAULT.finditer(text))


def strip_parens(text):
    """`text` without its parenthetical asides, nested ones included."""
    while True:
        out = re.sub(r"\([^()]*\)", " ", text)
        if out == text:
            return out
        text = out


def opt_in_trigger(sentence):
    """True when lower-cased `sentence` names `--api` or `--venue headless` outside parentheses, not after
    'without', 'no', 'unless', 'never' or 'not': the opt-in is the trigger, not an aside."""
    text = strip_parens(sentence)
    for m in re.finditer(r"--api\b|--venue headless", text):
        if not set(WORD.findall(text[:m.start()])[-2:]) & {"without", "no", "unless", "never", "not"}:
            return True
    return False


def option_help(stdout, option):
    """The help text argparse prints for `option` (its metavar dropped), joined into one line; '' when absent."""
    lines = stdout.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^(?:options|optional arguments):", ln)), None)
    if start is None:
        return ""
    for i in range(start + 1, len(lines)):
        m = re.match(rf"^\s+{re.escape(option)}(?![\w-])(.*)$", lines[i])
        if not m:
            continue
        parts = [m.group(1)]
        for ln in lines[i + 1:]:
            if not ln[:1].isspace() or ln.strip().startswith("-") or not ln.strip():
                break
            parts.append(ln.strip())
        return re.sub(r"\{[^}]*\}", " ", " ".join(" ".join(parts).split())).strip()
    return ""


def help_venue_problems(stdout):
    """What `harness-work dispatch --help` gets wrong about ticket #57; [] when --venue states bg as the default
    with no budget condition on it, and --api says it bills the API key."""
    problems = []
    venue = option_help(stdout, "--venue").lower()
    if not venue:
        problems.append("no --venue entry")
    if not (re.search(r"\bdefault[: ]+(?:is )?(?:the )?`?bg\b", venue) or re.search(r"\bbg\b[^.;]*\(default\)", venue)):
        problems.append(f"bg is not the stated default: {venue}")
    if re.search(r"\bdefault[^.;)]*\b(?:exhausted|budget)\b", venue):
        problems.append(f"the default is tied to the budget: {venue}")
    for clause in re.split(r"[.;]", venue):
        if re.search(r"\bbg\b", clause) and BUDGET_CONDITION.search(clause):
            problems.append(f"bg is tied to an exhausted budget: {clause}")
    api = option_help(stdout, "--api").lower()
    if not api:
        problems.append("no --api entry")
    elif not (re.search(r"\bapi key\b", api) and re.search(r"\bbill", api)):
        problems.append(f"--api does not say it bills the api key: {api}")
    return problems


def design_venue_problems(text):
    """What docs/design.md gets wrong about ticket #57; [] when principle 5 says dispatched work runs on the
    subscription by default (no budget condition, headless never the default) and ties the API key to `--api` or
    `--venue headless`, the headless venue bullet names that opt-in and is never the default, and the bg bullet is the
    default, not a fallback and not conditioned on an exhausted budget."""
    problems = []
    lines = text.splitlines()
    principles = [ln for ln in lines if re.match(r"^5\. \*\*", ln)]
    if len(principles) != 1:
        return [f"{len(principles)} principle-5 lines"]
    p = principles[0].replace("`", "").lower()
    clauses = re.split(r"[.;]\s*", p)
    if not any("subscription" in c and "dispatch" in c and says_default(c) and not BUDGET_WORDS.search(c)
               for c in clauses):
        problems.append("principle 5 has no clause: dispatched work runs on the subscription by default")
    if re.search(r"\bdefaults? (?:to )?headless\b", p) or re.search(r"\bdefault only when\b", p):
        problems.append("principle 5 makes headless the default, or the default conditional")
    for c in clauses:
        if says_default(c) and re.search(r"\bheadless\b|\bapi key\b|\bapi\b(?! budget)", c):
            problems.append(f"principle 5 makes the api a default: {c}")
    if not any(re.search(r"\bapi\b", s) and ("--api" in s or "--venue headless" in s) for s in sentences_of(p)):
        problems.append("principle 5 does not tie the api key to --api or --venue headless")
    if re.search(r"dispatched ticket work runs headless and bills the api key", p):
        problems.append("principle 5 keeps the old headless default")
    headless = [ln for ln in lines if ln.lstrip().startswith("- venue `headless`")]
    bg = [ln for ln in lines if ln.lstrip().startswith("- venue `bg`")]
    if len(headless) != 1 or len(bg) != 1:
        return problems + [f"{len(headless)} headless and {len(bg)} bg venue bullets"]
    h, b = headless[0].replace("`", "").lower(), bg[0].replace("`", "").lower()
    if says_default(h):
        problems.append(f"the headless bullet is a default: {h[:120]}")
    if not ("--api" in h or "on request" in h or "only with --venue headless" in h):
        problems.append(f"the headless bullet names no opt-in: {h[:120]}")
    if not says_default(b):
        problems.append(f"the bg bullet is not the default: {b[:120]}")
    if "fallback" in b:
        problems.append(f"the bg bullet is a fallback: {b[:120]}")
    if re.search(r"\bdefault[^.;]*\bexhausted\b", b):
        problems.append(f"the bg default is conditioned on an exhausted budget: {b[:120]}")
    return problems


class WorkSkill(unittest.TestCase):
    def test_dispatch_says_the_subscription_is_the_default_and_api_opts_in_to_the_key(self):
        # Ticket #57: `/harness:work --api` reaches `harness-work dispatch` (its own `--api` flag, passed through, an
        # alias for `--venue headless`) and bills the API key; the subscription (bg) is the default. The hint keeps
        # `--venue headless|bg` beside `--api`.
        section = dispatch_section()
        self.assertTrue(section.strip(), "no dispatch section")
        self.assertIn("harness-work dispatch", section)
        self.assertEqual(dispatch_venue_problems(section, argument_hint()), [], (argument_hint(), section))

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

    def test_the_design_doc_says_dispatch_defaults_to_bg_and_headless_is_opt_in(self):
        # Ticket #57: principle 5 and the dispatch venue bullets say the subscription (bg) is the default and the API
        # key is spent only through `--api` / `--venue headless`. The checker is proven on right and wrong text in
        # SkillWording.
        design = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "docs", "design.md")
        with open(design, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(design_venue_problems(text), [])

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


PLUGIN = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(PLUGIN))


def read_repo_file(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+|\n", text) if s.strip()]


class ReviewModel(unittest.TestCase):
    """Ticket #67: the review runs on Opus 5.5; Fable only for a risk:high ticket or after the review stage has
    escalated by failure. The work skill takes the model from `harness-board tier N --stage review`."""

    def routing_paragraph(self):
        lines = [ln for ln in skill_body().splitlines() if ln.startswith("You orchestrate")]
        self.assertEqual(len(lines), 1, "no routing paragraph (the one that starts 'You orchestrate')")
        return lines[0]

    def review_step(self):
        lines = [ln for ln in skill_body().splitlines() if re.match(r"^5[.)]\s+\*\*Review", ln)]
        self.assertEqual(len(lines), 1, "no numbered step 5 named Review")
        return lines[0]

    def test_the_skill_takes_the_reviewers_model_from_tier_stage_review(self):
        body = skill_body()
        self.assertRegex(body, r"harness-board tier N --stage review")
        # ... in the routing paragraph or in the review step, where the orchestrator picks the model
        where = self.routing_paragraph() + "\n" + self.review_step()
        self.assertRegex(where, r"harness-board tier N --stage review", where)
        # the review step itself says which model the reviewer gets
        step = self.review_step()
        self.assertRegex(step, r"--stage review", step)

    def test_fable_is_for_risk_high_or_escalation_only(self):
        where = self.routing_paragraph() + "\n" + self.review_step()
        text = " ".join(where.split())
        self.assertRegex(text.lower(), r"risk:high", text)
        self.assertRegex(text.lower(), r"escalat", text)
        for sentence in sentences(where):
            low = sentence.lower()
            if "fable" in low and "review" in low:
                with self.subTest(sentence=sentence):
                    self.assertTrue("risk:high" in low or "escalat" in low or "--stage review" in low,
                                    "an unconditional Fable review: " + sentence)

    def test_no_unconditional_review_on_fable_anywhere_in_the_skill(self):
        body = skill_body()
        self.assertNotRegex(body.lower(), r"or reviewing on fable", "the old unconditional rule is still there")
        for sentence in sentences(body):
            low = sentence.lower()
            if "fable" in low and re.search(r"\breview(?:s|er|ing)?\b", low) and "ultrareview" not in low:
                with self.subTest(sentence=sentence):
                    self.assertTrue("risk:high" in low or "escalat" in low or "--stage review" in low,
                                    "an unconditional Fable review: " + sentence)

    def test_fable_effort_is_still_xhigh_when_fable_is_used(self):
        body = skill_body()
        self.assertRegex(body, r"model: fable.{0,40}effort: xhigh|effort: xhigh.{0,40}model: fable")

    def test_the_reviewer_agent_runs_on_opus(self):
        front = read_repo_file(PLUGIN, "agents", "reviewer.md").split("\n---", 1)[0]
        models = re.findall(r"(?m)^model:\s*(\S+)\s*$", front)
        self.assertEqual(models, ["opus"], front)
        self.assertNotRegex(front, r"(?m)^model:\s*fable")

    def test_the_reviewer_agent_takes_the_default_effort(self):
        # Opus runs at its default (high); xhigh is passed on the Agent call only when Fable is used.
        front = read_repo_file(PLUGIN, "agents", "reviewer.md").split("\n---", 1)[0]
        self.assertNotRegex(front, r"(?m)^effort:\s*xhigh", front)

    def test_no_sentence_naming_fable_says_always_or_every(self):
        # Fable is conditional (risk:high or escalation); "always"/"every" next to it makes it unconditional.
        def fable_sentences(text):
            return [s for s in re.split(r"(?<=[.!?;])\s+|\n", text) if re.search(r"fable", s, re.I)]
        for sentence in fable_sentences(skill_body()):
            with self.subTest(file="work/SKILL.md", sentence=sentence[:100]):
                self.assertIsNone(re.search(r"\b(always|every)\b", re.sub(r"every agent call", "", sentence, flags=re.I), re.I), sentence)
        for name in (("user", "CLAUDE.md"), ("README.md",), ("docs", "guide.md"), ("docs", "design.md")):
            for line in read_repo_file(REPO_ROOT, *name).splitlines():
                if re.search(r"reviewer", line, re.I):
                    for sentence in fable_sentences(line):
                        with self.subTest(file="/".join(name), sentence=sentence[:100]):
                            self.assertIsNone(re.search(r"\b(always|every)\b", re.sub(r"every agent call", "", sentence, flags=re.I), re.I), sentence)

    def test_the_reviewer_table_rows_name_fable_only_with_risk_high(self):
        # README, guide and design list the reviewer in a table; its model cell may say Fable only for risk:high.
        for name in (("README.md",), ("docs", "guide.md"), ("docs", "design.md")):
            rows = [ln for ln in read_repo_file(REPO_ROOT, *name).splitlines()
                    if ln.lstrip().startswith("|") and re.search(r"`reviewer`", ln)]
            with self.subTest(file="/".join(name)):
                self.assertTrue(rows, "no reviewer table row")
            for row in rows:
                cells = [c.strip() for c in row.strip().strip("|").split("|")][:-1]    # all but the description
                for cell in cells:
                    if re.search(r"fable", cell, re.I):
                        with self.subTest(file="/".join(name), cell=cell):
                            self.assertIn("risk:high", cell.lower(), row)

    def test_no_user_facing_doc_says_every_review_runs_on_fable(self):
        # DoD: nothing in the skills, agents or docs says every review runs on Fable.
        claude_md = read_repo_file(REPO_ROOT, "user", "CLAUDE.md")
        self.assertTrue("reviewer` (always)" not in claude_md, "user/CLAUDE.md still says Fable is for the reviewer (always)")
        self.assertTrue("Fable for judgment stages" not in claude_md, "user/CLAUDE.md still says Fable for judgment stages")
        for name in (("user", "CLAUDE.md"), ("README.md",), ("docs", "guide.md"), ("docs", "design.md")):
            for line in read_repo_file(REPO_ROOT, *name).splitlines():
                low = line.lower()
                if "reviewer" in low and "fable" in low and not low.lstrip().startswith("#"):
                    with self.subTest(file="/".join(name), line=line[:80]):
                        self.assertTrue("risk:high" in low or "escalat" in low or "opus" in low,
                                        "reviewer row names Fable unconditionally: " + line)


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

    def test_the_dispatch_venue_check_takes_any_wording_of_the_opt_in_and_rejects_the_old_default(self):
        hint = '"[issue# | --api | --max N | --venue headless|bg | --dry-run]"'
        right = [
            "Run `harness-work dispatch $ARGUMENTS`. Without `--api` every run is a background session on the "
            "subscription, the default; `--api` (the tool's alias for `--venue headless`) bills the API key and is "
            "refused while the API budget is exhausted.",
            "Run `harness-work dispatch` with the arguments, passing no `--venue`: every run is a background session "
            "on the subscription, the default. With `--api`, run `harness-work dispatch --venue headless` with the "
            "other arguments instead: headless runs billed to the API key, refused while the API budget is exhausted.",
            "The subscription is the default: without `--api` the dispatch passes no venue, so `harness-work dispatch "
            "--max N` launches background sessions. `/harness:work --api` bills the API key: it runs `harness-work "
            "dispatch --venue headless`, which is refused while the API budget is exhausted.",
        ]
        for text in right:
            with self.subTest(right=text):
                self.assertEqual(dispatch_venue_problems(text, hint), [])
        self.assertIn("the argument-hint has no --api",
                      dispatch_venue_problems(right[0], '"[issue# | --max N | --venue headless|bg | --dry-run]"'))
        self.assertIn("the argument-hint has no --venue",
                      dispatch_venue_problems(right[0], '"[issue# | --api | --max N | --dry-run]"'))
        wrong = {
            # the paragraph before #57: headless on the API key, the subscription only as the fallback
            "old": "Run `harness-work dispatch $ARGUMENTS`. It starts a worktree per ready ticket and launches a "
                   "headless run billed to the API key, or a background session on the subscription when the API "
                   "budget is exhausted.",
            "headless by default": "Run `harness-work dispatch $ARGUMENTS`; it launches headless runs by default, "
                                   "and `--api` is accepted too. The subscription is used when the API budget is "
                                   "exhausted.",
            "no mapping": "Run `harness-work dispatch` with no `--venue`: the subscription is the default. `--api` "
                          "bills the API key.",
            "untied": right[0] + " Dispatched runs are billed to the API key.",
            "api bills the subscription": "Run `harness-work dispatch` with no `--venue`: the subscription is the "
                                          "default. With `--api`, run `harness-work dispatch --venue headless`; it "
                                          "still bills the subscription.",
            "not the default": "The subscription is not the default: without `--api` dispatch passes no venue. With "
                               "`--api`, run `harness-work dispatch --venue headless`, billed to the API key.",
        }
        for name, text in wrong.items():
            with self.subTest(wrong=name):
                self.assertNotEqual(dispatch_venue_problems(text, hint), [])

        # Breaker round 1 (reconstructed from its report: the old meaning, headless on the API key by default and the
        # subscription only once the budget is exhausted, in words that passed the first checker). Each case is
        # right[1] or right[0] with one fault, and must fail for that fault.
        faults = {
            "the breaker's cheat": (
                "Run `harness-work dispatch $ARGUMENTS`. Dispatch launches a headless run on the API key by default "
                "(`--api` is implied) and bills the API key; the subscription is the default only when the API budget "
                "is exhausted.", "no clause says the subscription is the default, unconditionally"),
            "default once exhausted": (
                right[1].replace("on the subscription, the default.",
                                 "on the subscription, the default once the API budget is exhausted."),
                "no clause says the subscription is the default, unconditionally"),
            "subscription only when exhausted": (
                right[0] + " The subscription is used only when the API budget is exhausted.",
                "the subscription is tied to an exhausted budget"),
            "headless by default with --api": (
                right[0] + " Dispatch with `--api` runs headless by default and bills the API key.",
                "headless or the api key as a default"),
            "parenthetical opt-in": (
                right[0] + " Runs go headless on the API key (see `--api`).",
                "headless or the api key without --api as its trigger"),
            "implied opt-in": (
                right[0] + " Runs go headless (`--api` is implied).",
                "headless or the api key without --api as its trigger"),
            "--api is implied outside parentheses": (
                right[0] + " With `--api` implied, runs go headless.", "--api as implied"),
            "without --api is headless": (
                right[0] + " Without `--api`, runs go headless on the API key.",
                "headless or the api key without --api as its trigger"),
            "bills only in parentheses": (
                "Run `harness-work dispatch $ARGUMENTS`. Every run is a background session on the subscription, the "
                "default. Dispatch can also go headless with `--venue headless` (`--api` bills the API key).",
                "no sentence says --api bills the api key"),
        }
        for name, (text, fault) in faults.items():
            with self.subTest(fault=name):
                problems = dispatch_venue_problems(text, hint)
                self.assertTrue(any(p.startswith(fault) for p in problems), (fault, problems))

    def test_the_help_check_takes_bg_as_the_default_and_rejects_a_budget_condition(self):
        def fake_help(venue, api):
            return ("usage: harness-work dispatch [-h] [--max MAX] [--venue {headless,bg}] [--api] [--dry-run]\n\n"
                    "options:\n  -h, --help            show this help message and exit\n"
                    "  --max MAX             live runs at most (default 3)\n"
                    f"  --venue {{headless,bg}}\n                        {venue}\n"
                    f"  --api                 {api}\n"
                    "  --dry-run             print the plan; start and launch nothing\n")
        venue_ok = "default: bg (the subscription); headless bills the api key and is refused while the api " \
                   "budget is exhausted"
        api_ok = "alias for --venue headless: bills the API key"
        for venue, api in ((venue_ok, api_ok), ("bg, the subscription (default); headless only on request", api_ok),
                           ("the default is bg, whatever the budgets say", "run headless, billed to the API key")):
            with self.subTest(right=venue):
                self.assertEqual(help_venue_problems(fake_help(venue, api)), [])
        wrong = {
            "default: headless, or bg while the api budget is exhausted (headless is then refused)":
                "bg is not the stated default",
            "bg is the default once the api budget is exhausted; headless otherwise": "bg is not the stated default",
            "default: bg when the api budget is exhausted, else headless": "the default is tied to the budget",
            "headless; bg (default) only when the api budget is exhausted": "bg is tied to an exhausted budget",
        }
        for venue, fault in wrong.items():
            with self.subTest(wrong=venue):
                problems = help_venue_problems(fake_help(venue, api_ok))
                self.assertTrue(any(p.startswith(fault) for p in problems), (fault, problems))
        problems = help_venue_problems(fake_help(venue_ok, "same as --venue headless"))
        self.assertTrue(any(p.startswith("--api does not say it bills the api key") for p in problems), problems)
        no_api = fake_help(venue_ok, api_ok).replace(f"  --api                 {api_ok}\n", "")
        self.assertIn("no --api entry", help_venue_problems(no_api))

    def test_the_design_check_takes_bg_as_the_default_and_rejects_the_old_meaning(self):
        principle = ("5. **Spend the subscription first.** Interactive sessions and dispatched ticket work use the "
                     "Max subscription by default. The API key is spent only on request: `harness-work dispatch "
                     "--api` (or `--venue headless`) runs headless and bills it; high-risk pull requests get "
                     "ultrareview, which bills claude.ai usage credits.")
        headless = ("  - venue `headless` (only with `--api` or `--venue headless`; refused while `api` is "
                    "exhausted): in the worktree, `claude -p \"/harness:work N\"` ...")
        bg = ("  - venue `bg` (the default, whatever the budgets say): in the worktree, `claude --bg -n \"<repo> #N "
              "<title>\"` ...")

        def doc(p=principle, h=headless, b=bg):
            return "\n".join(["## Principles", "", p, "", "- `dispatch ...`: then launch:", h, b])

        self.assertEqual(design_venue_problems(doc()), [])
        self.assertEqual(design_venue_problems(doc(h=headless.replace("only with `--api` or `--venue headless`",
                                                                      "on request"))), [])
        old_principle = ("5. **Spend credits before the subscription.** Interactive sessions use the Max subscription. "
                         "Dispatched ticket work runs headless and bills the API key; high-risk pull requests get "
                         "ultrareview, which bills claude.ai usage credits. When a budget is exhausted the dispatcher "
                         "falls back one step and remembers for a day.")
        old_headless = ("  - venue `headless` (default when `budgets.json` shows `api` not exhausted; refused, as "
                        "above, while it is exhausted): in the worktree, `claude -p \"/harness:work N\"` ...")
        old_bg = ("  - venue `bg` (fallback, the default while `api` is exhausted): in the worktree, `claude --bg -n "
                  "\"<repo> #N <title>\"` ...")
        wrong = {
            "old principle": (dict(p=old_principle), "principle 5 keeps the old headless default"),
            "defaults to headless": (dict(p=principle.replace(
                "Interactive sessions and dispatched ticket work use the Max subscription by default.",
                "Dispatched ticket work defaults to headless on the API key.")),
                "principle 5 makes headless the default"),
            "default only when": (dict(p=principle.replace(
                "use the Max subscription by default.",
                "use the Max subscription by default only when the API budget is exhausted.")),
                "principle 5 has no clause: dispatched work runs on the subscription by default"),
            "api by default, subscription once exhausted (the breaker's meaning)": (dict(p=(
                "5. **Spend credits before the subscription.** Dispatched ticket work runs on the API key by default "
                "(`--api`); the subscription is the default dispatch venue once the API budget is exhausted.")),
                "principle 5 has no clause: dispatched work runs on the subscription by default"),
            "opt-in only when exhausted": (dict(p=(
                "5. **Spend the subscription first.** Dispatched ticket work runs on the subscription by default. "
                "The API key is spent only when the API budget is exhausted.")),
                "principle 5 does not tie the api key to --api or --venue headless"),
            "old headless bullet": (dict(h=old_headless), "the headless bullet is a default"),
            "headless bullet without opt-in": (dict(h=(
                "  - venue `headless` (refused while `api` is exhausted): in the worktree, `claude -p ...`")),
                "the headless bullet names no opt-in"),
            "old bg bullet": (dict(b=old_bg), "the bg bullet is a fallback"),
            "bg default once exhausted": (dict(b=(
                "  - venue `bg` (the default once `api` is exhausted): in the worktree, `claude --bg ...`")),
                "the bg default is conditioned on an exhausted budget"),
            "bg not the default": (dict(b="  - venue `bg`: in the worktree, `claude --bg ...`"),
                                   "the bg bullet is not the default"),
        }
        for name, (parts, fault) in wrong.items():
            with self.subTest(wrong=name):
                problems = design_venue_problems(doc(**parts))
                self.assertTrue(any(p.startswith(fault) for p in problems), (fault, problems))


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
