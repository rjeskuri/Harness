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
and for -p sleeps briefly, then prints a canned JSON result; for --cloud it refuses unless its stdout is a terminal,
logs the origin's heads and prints the create-and-exit lines with \\r\\n ends and ANSI escapes, or the failure
$FAKECLAUDE_CLOUD names), and terminal-notifier and osascript that do nothing. harness-board is the real one,
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
entry = {{"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}}
if "--cloud" in argv:
    import subprocess
    entry["tty"] = os.isatty(1)
    entry["remote_heads"] = subprocess.run(["git", "ls-remote", "--heads", "origin"], capture_output=True,
                                           text=True, stdin=subprocess.DEVNULL).stdout
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")
if "--cloud" in argv:
    if not os.isatty(1):
        sys.stderr.write("Error: --cloud requires an interactive terminal. To start a new cloud session, run from "
                         "a TTY.\\n")
        sys.exit(1)
    mode = os.environ.get("FAKECLAUDE_CLOUD", "created")
    sid = os.environ.get("FAKECLAUDE_CLOUD_ID", "session_01FakeCloudXyz")
    esc = "\\x1b["
    if mode == "attached":
        out = ("Opening the cloud session here...\\r\\n" + esc + "33mNo cloud session was created or opened: it would "
               "open in this terminal and need the keyboard." + esc + "0m\\r\\n")
        code = 0
    elif mode == "fail":
        out = "Connecting...\\r\\n" + esc + "31mError: cloud environment unavailable (HTTP 503)" + esc + "0m\\r\\n"
        code = 1
    elif mode == "billing":
        out = "Connecting...\\r\\n" + esc + "31mError: your cloud session credits are used up" + esc + "0m\\r\\n"
        code = 1
    else:
        out = (esc + "?25l" + esc + "1mCreated cloud session:" + esc + "0m "
               + os.environ.get("FAKECLAUDE_TITLE", "work") + "\\r\\n" + esc + "2mView:" + esc + "0m " + esc + "4mhttps://claude.ai/code/" + sid + "?from=cli&m=0"
               + esc + "24m\\r\\n")
        if mode != "noresume":
            out += "Resume with: " + esc + "36mclaude --teleport " + sid + esc + "39m\\r\\n"
        out += esc + "?25h"
        code = 0
    sys.stdout.write(out)
    sys.stdout.flush()
    sys.exit(code)
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
    for name in ("terminal-notifier", "osascript"):     # harness-board needs-input (a cloud run's blocked marker)
        executable(os.path.join(fakebin, name), "#!/bin/sh\nexit 0\n")
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

    def test_refuses_a_proposed_ticket(self):
        """#160: a proposed ticket waits for harness-board promote; start makes no branch, worktree or status change."""
        self.scenario(issues=[issue(labels=("task", "status:proposed", "size:S", "risk:low"))])
        branches = self.git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads")
        p = self.work("start", "12", "--json")
        self.assertOneLineError(p, "#12 is status:proposed; it waits for harness-board promote 12")
        self.assertEqual(p.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads"), branches)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

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
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),     # #239: opus goes to opus xhigh
            (["ci", "fail", "--tier", "fable"], "retry"),
            (["ci", "fail", "--tier", "fable"], "needs-input"),    # #239: nothing above an explicit Fable
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # #239: effort recorded
        self.assertEqual((attempts[0]["stage"], attempts[0]["tier"], attempts[0]["result"]), ("code", "sonnet", "fail"))
        self.assertEqual(attempts[-1]["tier"], "fable")

        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"]), ("retry", 1, "sonnet"))

    def test_escalation_starts_from_the_overridden_tier(self):
        """#175: HARNESS_CODE_TIER in the main checkout's .claude/settings.json is the tier `start` reports and the
        cache holds, and `attempt` escalates from it, not from the labels' route (opus xhigh here). #239: the override
        replaces the labels' effort too, so opus escalates to opus xhigh and sonnet to opus."""
        hard = ("task", "status:ready", "size:L", "risk:high")
        self.scenario(issues=[issue(labels=hard), issue(number=13, labels=hard)])
        settings = os.path.join(self.repo, ".claude", "settings.json")
        for number, setting, escalated in ((12, "opus", "opus xhigh"), (13, "sonnet", "opus")):
            with self.subTest(setting=setting):
                write(settings, json.dumps({"env": {"HARNESS_CODE_TIER": setting}}))
                data = self.ok_json("start", str(number), "--json")
                self.assertEqual(data["tier"], setting)
                self.assertEqual(self.read_json(self.cache_path(number))["tier"], setting)
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(), "retry")
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(),
                                 f"escalate {escalated}")
                attempts = self.read_json(self.cache_path(number))["attempts"]
                self.assertEqual([(a["stage"], a["tier"], a["result"]) for a in attempts],
                                 [("code", setting, "fail")] * 2)

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
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
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

    def test_breaker_stage_allows_two_rounds_then_needs_input(self):
        """#252: the breaker follows the fix rule: retry on a run's first failure, needs-input on the second and every
        later one, at any tier; never an escalation. A breaker pass ends the run; other stages neither advance nor
        reset it (the tests stage passes between breaker rounds)."""
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["breaker", "fail"], "retry"),
            (["breaker", "fail"], "needs-input"),            # two rounds at most; sonnet does not escalate to opus
            (["breaker", "fail"], "needs-input"),
            (["breaker", "pass"], "done"),
            (["breaker", "fail"], "retry"),                  # a pass ends the run of breaker failures
            (["tests", "pass"], "done"),                     # the tightened tests do not end the breaker's run
            (["breaker", "fail", "--tier", "opus"], "needs-input"),     # opus does not escalate to opus xhigh
            (["code", "fail"], "retry"),                     # nor does the breaker's run count for code
            (["breaker", "pass", "--tier", "opus"], "done"),
            (["breaker", "fail", "--tier", "opus"], "retry"),
            (["breaker", "fail", "--tier", "opus", "--effort", "high"], "needs-input"),
            (["breaker", "pass", "--tier", "fable"], "done"),
            (["breaker", "fail", "--tier", "fable"], "retry"),
            (["breaker", "fail", "--tier", "fable"], "needs-input"),
            (["breaker", "fail", "--tier", "sonnet"], "needs-input"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # the record is unchanged
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[6]["tier"], attempts[6]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-2]["tier"], attempts[-2]["result"]), ("fable", "fail"))

        # --json on a fresh run, at each tier: the tier never moves and no next tier (or effort) is named
        for tier in ("sonnet", "opus", "fable"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            for failures, action in ((1, "retry"), (2, "needs-input"), (3, "needs-input")):
                with self.subTest(json_tier=tier, json_failures=failures):
                    data = self.ok_json("attempt", "12", "breaker", "fail", "--tier", tier, "--json")
                    self.assertEqual((data["stage"], data["result"], data["action"], data["failures"], data["tier"],
                                      data["next_tier"], data["next_effort"]),
                                     ("breaker", "fail", action, failures, tier, None, None))
            data = self.ok_json("attempt", "12", "breaker", "pass", "--tier", tier, "--json")
            self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("done", 0, None))
            data = self.ok_json("attempt", "12", "breaker", "fail", "--tier", tier, "--json")
            self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("retry", 1, None))
        # without --tier the cached sonnet is the tier, and the second failure still names no next tier
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        self.assertEqual(self.ok_json("attempt", "12", "breaker", "fail", "--json")["next_tier"], None)
        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"], data["next_tier"]),
                         ("needs-input", 2, "sonnet", None))

    def test_verify_stage_answers_like_ci_and_counts_apart(self):
        """#252: `verify` (the full verify run just before ready) is a stage of its own after ci, answered by the
        default rule as ci is: retry, escalate, needs-input; a pass ends its run. verify's failures and ci's count
        apart, and neither counts for code."""
        sequence = [
            (["fail"], "retry"),
            (["fail"], "escalate opus"),
            (["fail"], "needs-input"),
            (["fail"], "needs-input"),
            (["pass"], "done"),
            (["fail"], "retry"),
            (["fail", "--tier", "opus"], "escalate opus xhigh"),
            (["pass", "--tier", "opus"], "done"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "retry"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "needs-input"),
            (["pass", "--tier", "fable"], "done"),
            (["fail", "--tier", "fable"], "retry"),
            (["fail", "--tier", "fable"], "needs-input"),
        ]
        answers = {}
        for stage in ("ci", "verify"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            answers[stage] = []
            for argv, expected in sequence:
                with self.subTest(stage=stage, argv=argv):
                    out = self.ok("attempt", "12", stage, *argv).stdout.strip()
                    answers[stage].append(out)
                    self.assertEqual(out, expected)
            attempts = self.read_json(self.cache_path())["attempts"]
            self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(stage, argv[0]) for argv, _ in sequence])
            self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
        self.assertEqual(answers["verify"], answers["ci"])

        # --json: verify's escalation names the next tier as ci's does
        for stage in ("ci", "verify"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            first = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((first["stage"], first["action"], first["failures"], first["next_tier"]),
                             (stage, "retry", 1, None))
            second = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((second["stage"], second["action"], second["failures"], second["tier"],
                              second["next_tier"]), (stage, "escalate", 2, "sonnet", "opus"))

        # interleaved: each stage keeps its own run
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        interleaved = [
            (["verify", "fail"], "retry"),
            (["ci", "fail"], "retry"),                       # verify's failure is not ci's
            (["verify", "fail"], "escalate opus"),           # ci's failure did not reset verify
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["ci", "pass"], "done"),
            (["verify", "fail"], "needs-input"),             # a ci pass does not end verify's run
            (["ci", "fail"], "retry"),
            (["verify", "pass"], "done"),
            (["ci", "fail"], "escalate opus"),               # a verify pass does not end ci's run
            (["verify", "fail"], "retry"),
            (["ci", "fail"], "needs-input"),
            (["verify", "fail", "--tier", "opus"], "escalate opus xhigh"),
        ]
        for argv, expected in interleaved:
            with self.subTest(interleaved=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts],
                         [(argv[0], argv[1]) for argv, _ in interleaved])

    def test_fix_and_review_failures_count_apart(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["review", "fail", "--tier", "opus"], "retry"),
            (["fix", "fail"], "retry"),                      # review's failure is not fix's
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),   # fix's failure did not reset review
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


# =========================================================================== attempt: effort-aware escalation (#239)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def route_of(kind, size, risk):
    """#239's label route for the coding stages, from the ticket's words: S+low non-bug is sonnet with no effort;
    size:L or risk:high is opus at xhigh; everything else opus with no effort. Never fable."""
    tier = "sonnet" if size == "S" and risk == "low" and kind != "bug" else "opus"
    return tier, ("xhigh" if size == "L" or risk == "high" else None)


def escalation_of(tier, effort):
    """#239: where an escalation-worthy failure at (tier, effort) goes: sonnet to opus; opus below xhigh to opus at
    xhigh; opus at xhigh or above, and an explicit fable, have nothing above them (None: needs-input)."""
    if tier == "sonnet":
        return "opus", None
    if tier == "opus" and effort in (None, "low", "medium", "high"):
        return "opus", "xhigh"
    return None


def escalation_text(tier, effort):
    nxt = escalation_of(tier, effort)
    if nxt is None:
        return "needs-input"
    return " ".join(["escalate", nxt[0]] + ([nxt[1]] if nxt[1] else []))


def ticket_labels(kind, size, risk):
    return [kind, "status:in-progress"] + (["size:" + size] if size else []) + (["risk:" + risk] if risk else [])


class Keys(dict):
    """A JSON object whose missing key reads as a marker, so a missing field fails an assertion instead of raising."""

    def __missing__(self, key):
        return f"<no {key}>"


class AttemptEffort(WorkTest):
    """Ticket #239: `harness-work attempt` knows the effort a stage ran at (`--effort`, else the ticket's own route
    for that stage) and never escalates to Fable: sonnet goes to opus (`escalate opus`), opus to opus at xhigh
    (`escalate opus xhigh`), and a stage already at opus xhigh (or max), or on an explicit fable, goes to needs-input
    on its second failure. --json carries `effort` and `next_effort` beside `tier` and `next_tier`. WorkTest.env drops
    every HARNESS_* variable of the real environment."""

    SPREAD = [("task", "S", "low"), ("bug", "S", "low"), ("task", "M", "low"), ("chore", "M", "high"),
              ("task", "L", "low"), ("refactor", "L", "high"), ("task", "S", "high"), ("task", None, None),
              ("bug", "L", None)]

    def put_ticket(self, number, kind, size, risk, tier=None):
        """A ticket cache as `harness-board show` writes it: labels, type, size, risk and the effective tier."""
        write(self.cache_path(number), json.dumps({
            "number": number, "title": TITLE, "type": kind, "size": size, "risk": risk, "flags": [],
            "labels": ticket_labels(kind, size, risk), "tier": tier or route_of(kind, size, risk)[0],
            "attempts": []}))

    def attempt(self, number, *argv, env=None):
        return self.ok("attempt", str(number), *argv, env=env).stdout.strip()

    def attempt_json(self, number, *argv, env=None):
        return Keys(self.ok_json("attempt", str(number), *argv, "--json", env=env))

    def attempts(self, number):
        return [Keys(a) for a in self.read_json(self.cache_path(number))["attempts"]]

    def configure(self, **env):
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    # ----------------------------------------------------------- broad: the ticket's route decides the default
    def test_the_ticket_route_decides_the_tier_and_effort_of_each_failure(self):
        seen = set()
        for i, (kind, size, risk) in enumerate(self.SPREAD):
            tier, effort = route_of(kind, size, risk)
            seen.add(escalation_text(tier, effort))
            with self.subTest(kind=kind, size=size, risk=risk, output="text"):
                n = 100 + i
                self.put_ticket(n, kind, size, risk)
                answers = [self.attempt(n, "code", "fail") for _ in range(3)]
                self.assertEqual(answers, ["retry", escalation_text(tier, effort), "needs-input"])
                self.assertNotIn("fable", " ".join(answers))
                attempts = self.attempts(n)
                self.assertEqual([(a["stage"], a["tier"], a["effort"], a["result"]) for a in attempts],
                                 [("code", tier, effort, "fail")] * 3)
            with self.subTest(kind=kind, size=size, risk=risk, output="json"):
                n = 200 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual({k: first[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                        "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "retry", "failures": 1, "next_tier": None,
                                  "next_effort": None})
                second = self.attempt_json(n, "code", "fail")
                nxt = escalation_of(tier, effort)
                self.assertEqual({k: second[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                         "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "escalate" if nxt else "needs-input",
                                  "failures": 2, "next_tier": nxt[0] if nxt else None,
                                  "next_effort": nxt[1] if nxt else None})
                done = self.attempt_json(n, "code", "pass")
                self.assertEqual((done["action"], done["next_tier"], done["next_effort"]), ("done", None, None))
        # the spread reaches all three answers
        self.assertEqual(seen, {"escalate opus", "escalate opus xhigh", "needs-input"})

    def test_size_l_and_risk_high_run_at_opus_xhigh_and_go_to_needs_input(self):
        # DoD: a failure at opus xhigh gives needs-input; nothing prints fable. Every stage alike.
        for n, (kind, size, risk) in ((31, ("task", "L", "low")), (32, ("bug", "M", "high")),
                                      (33, ("chore", "S", "high"))):
            self.put_ticket(n, kind, size, risk)
            for stage in ("tests", "breaker", "code", "ci"):
                with self.subTest(ticket=n, stage=stage):
                    self.assertEqual(self.attempt(n, stage, "fail"), "retry")
                    data = self.attempt_json(n, stage, "fail")
                    self.assertEqual((data["tier"], data["effort"], data["action"], data["next_tier"],
                                      data["next_effort"]), ("opus", "xhigh", "needs-input", None, None))
                    self.assertEqual(self.attempt(n, stage, "fail"), "needs-input")

    def test_a_second_failure_at_opus_says_escalate_opus_xhigh(self):
        # DoD: an M/low ticket runs at opus with no effort; its second failure escalates to opus at xhigh
        for n, kind in ((41, "task"), (42, "bug"), (43, "refactor")):
            with self.subTest(kind=kind):
                self.put_ticket(n, kind, "M", "low")
                self.assertEqual(self.attempt(n, "code", "fail"), "retry")
                self.assertEqual(self.attempt(n, "code", "fail"), "escalate opus xhigh")
                # the escalated run, recorded at opus xhigh, fails again: needs-input
                self.assertEqual(self.attempt(n, "code", "fail", "--tier", "opus", "--effort", "xhigh"), "needs-input")

    # ----------------------------------------------------------- explicit --tier and --effort
    def test_every_explicit_tier_and_effort(self):
        n = 300
        for tier in ("sonnet", "opus", "fable"):
            for effort in (None,) + EFFORT_LEVELS:
                flags = ["--tier", tier] + (["--effort", effort] if effort else [])
                with self.subTest(tier=tier, effort=effort):
                    n += 1
                    self.put_ticket(n, "task", "M", "low")              # code route: opus, no effort
                    first = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((second["tier"], second["effort"]), (tier, effort))
                    nxt = escalation_of(tier, effort)
                    if tier == "sonnet":
                        self.assertEqual((second["action"], second["next_tier"]), ("escalate", "opus"))
                    else:
                        self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                         ("escalate" if nxt else "needs-input", nxt[0] if nxt else None,
                                          nxt[1] if nxt else None))
                    attempts = self.attempts(n)
                    self.assertEqual([(a["tier"], a["effort"]) for a in attempts], [(tier, effort)] * 2)
                    n += 1
                    self.put_ticket(n, "task", "M", "low")
                    self.assertEqual(self.attempt(n, "code", "fail", *flags), "retry")
                    text = self.attempt(n, "code", "fail", *flags)
                    self.assertNotIn("fable", text)
                    if tier == "sonnet":
                        self.assertTrue(text.startswith("escalate opus"), text)
                    else:
                        self.assertEqual(text, escalation_text(tier, effort))
                    if tier == "sonnet" and effort is None:
                        self.assertEqual(text, "escalate opus")

    def test_an_explicit_tier_that_matches_the_stage_route_takes_its_effort(self):
        # Without --effort, an attempt at the tier its stage routes to ran at that route's effort; at another tier it
        # ran at none. The review stage's route is the review's, not the coder's.
        cases = [
            # (labels, stage, flags, second answer)
            (("task", "L", "low"), "code", ["--tier", "opus"], "needs-input"),            # code route: opus xhigh
            (("task", "L", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),  # review route: opus
            (("task", "M", "high"), "review", ["--tier", "opus"], "needs-input"),        # review route: opus xhigh
            (("task", "M", "high"), "code", ["--tier", "opus"], "needs-input"),
            (("task", "M", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),
            (("task", "L", "low"), "code", ["--tier", "sonnet"], "escalate opus"),       # not the route's tier
            (("task", "L", "low"), "code", ["--tier", "opus", "--effort", "high"], "escalate opus xhigh"),
            (("task", "M", "high"), "review", ["--tier", "fable"], "needs-input"),
        ]
        for i, ((kind, size, risk), stage, flags, want) in enumerate(cases):
            with self.subTest(labels=(kind, size, risk), stage=stage, flags=flags):
                n = 400 + i
                self.put_ticket(n, kind, size, risk)
                self.assertEqual(self.attempt(n, stage, "fail", *flags), "retry")
                self.assertEqual(self.attempt(n, stage, "fail", *flags), want)

    def test_effort_takes_the_five_levels_and_refuses_anything_else(self):
        self.put_ticket(12, "task", "M", "low")
        for bad in ("extra", "XHIGHER", "5"):
            with self.subTest(bad=bad):
                p = self.work("attempt", "12", "code", "fail", "--effort", bad)
                self.assertOneLineError(p, bad)
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], [])
        for level in EFFORT_LEVELS:
            with self.subTest(level=level):
                data = self.attempt_json(12, "code", "pass", "--effort", level)
                self.assertEqual((data["effort"], data["action"]), (level, "done"))
        self.assertEqual([a["effort"] for a in self.attempts(12)], list(EFFORT_LEVELS))

    # ----------------------------------------------------------- settings and stale caches
    def test_a_pre_239_cache_that_says_fable_never_escalates_to_fable(self):
        # A cache written before #239 holds the old rubric's tier (fable for size:L or risk:high). The labels decide
        # now: opus at xhigh, and needs-input on the second failure.
        for n, (kind, size, risk) in ((51, ("task", "L", "high")), (52, ("task", "M", "high")),
                                      (53, ("bug", "L", "low"))):
            with self.subTest(kind=kind, size=size, risk=risk):
                self.put_ticket(n, kind, size, risk, tier="fable")
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), ("opus", "xhigh"))
                self.assertEqual(self.attempt(n, "code", "fail"), "needs-input")
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))

    def test_explicit_fable_from_the_settings_still_runs_fable_and_tops_out(self):
        for n, how in ((61, "settings.json"), (62, "environment")):
            with self.subTest(how=how):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="fable")
                else:
                    env = self.env(HARNESS_CODE_TIER="fable")
                self.put_ticket(n, "task", "M", "low", tier="fable")
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual(first["tier"], "fable")
                second = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((second["tier"], second["action"], second["next_tier"]),
                                 ("fable", "needs-input", None))

    def test_the_code_effort_setting_is_the_effort_a_stage_ran_at(self):
        # HARNESS_CODE_EFFORT wins over the labels' xhigh, as it does for `harness-board tier`
        cases = [("low", ("task", "L", "low"), "escalate opus xhigh"),
                 ("high", ("bug", "M", "high"), "escalate opus xhigh"),
                 ("max", ("task", "M", "low"), "needs-input"),
                 ("xhigh", ("task", "M", "low"), "needs-input"),
                 ("medium", ("task", "S", "low"), "escalate opus")]
        for i, (level, (kind, size, risk), want) in enumerate(cases):
            with self.subTest(level=level, labels=(kind, size, risk)):
                self.configure(HARNESS_CODE_EFFORT=level)
                n = 70 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (route_of(kind, size, risk)[0], level))
                self.assertEqual(self.attempt(n, "code", "fail"), want)

    def test_a_code_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_CODE_TIER replaces the labels' tier and their effort. A size:L ticket under
        # HARNESS_CODE_TIER=opus runs at opus with no effort, so its second failure escalates to opus xhigh.
        for n, how, (kind, size, risk) in ((91, "settings.json", ("task", "L", "low")),
                                           (92, "environment", ("bug", "L", "high")),
                                           (93, "settings.json", ("task", "M", "high"))):
            with self.subTest(how=how, labels=(kind, size, risk)):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="opus")
                else:
                    env = self.env(HARNESS_CODE_TIER="opus")
                self.put_ticket(n, kind, size, risk, tier="opus")       # the effective tier harness-board writes
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", None, "retry"))
                self.assertEqual(self.attempt(n, "code", "fail", env=env), "escalate opus xhigh")
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", None)] * 2)
                # HARNESS_CODE_EFFORT set beside it is the effort the stage ran at
                self.put_ticket(n + 10, kind, size, risk, tier="opus")
                env2 = self.env(HARNESS_CODE_TIER="opus", HARNESS_CODE_EFFORT="xhigh")
                self.assertEqual(self.attempt_json(n + 10, "code", "fail", env=env2)["effort"], "xhigh")
                self.assertEqual(self.attempt(n + 10, "code", "fail", env=env2), "needs-input")

    def test_a_review_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_REVIEW_TIER replaces the labels' review rule, their xhigh included. A risk:high review
        # recorded at `--tier opus` ran at opus xhigh by the labels (needs-input on the second failure), but at opus
        # with no effort under HARNESS_REVIEW_TIER=opus (escalate opus xhigh). HARNESS_REVIEW_TIER=fable runs at
        # fable xhigh and tops out.
        cases = [
            (None, "opus", "xhigh", "needs-input"),
            ({"HARNESS_REVIEW_TIER": "opus"}, "opus", None, "escalate opus xhigh"),
            ({"HARNESS_REVIEW_TIER": "fable"}, "fable", "xhigh", "needs-input"),
        ]
        for i, (settings, tier, effort, want) in enumerate(cases):
            for where in (("settings.json", "environment") if settings else (None,)):
                with self.subTest(settings=settings, where=where):
                    self.setUp()
                    env = None
                    if where == "settings.json":
                        self.configure(**settings)
                    elif where == "environment":
                        env = self.env(**settings)
                    n = 95 + i
                    self.put_ticket(n, "task", "M", "high")
                    first = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    nxt = escalation_of(tier, effort)
                    self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                     ("escalate" if nxt else "needs-input", nxt[0] if nxt else None,
                                      nxt[1] if nxt else None))
                    self.put_ticket(n + 10, "task", "M", "high")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), "retry")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), want)

    def test_a_cache_without_labels_that_says_fable_reads_as_opus_xhigh(self):
        # Agrees with `harness-board tier` on the same cache: a stored fable without labels is the pre-#239 rubric's
        # answer for size:L or risk:high, so the attempt runs at opus xhigh and the second failure is needs-input.
        for n, stage in ((85, "code"), (86, "tests"), (87, "ci")):
            with self.subTest(stage=stage):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": "fable", "attempts": []}))
                first = self.attempt_json(n, stage, "fail")
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", "xhigh", "retry"))
                second = self.attempt_json(n, stage, "fail")
                self.assertEqual((second["tier"], second["effort"], second["action"], second["next_tier"],
                                  second["next_effort"]), ("opus", "xhigh", "needs-input", None, None))
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", "xhigh")] * 2)
                self.assertNotIn("fable", json.dumps([dict(first), dict(second)]))
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))
                write(self.cache_path(n + 10), json.dumps({"number": n + 10, "title": TITLE, "tier": "fable",
                                                           "attempts": []}))
                self.assertEqual([self.attempt(n + 10, stage, "fail") for _ in range(2)], ["retry", "needs-input"])
        # the board agrees on the same cache
        board = os.path.join(os.path.dirname(TOOL), "harness-board")
        p = subprocess.run([sys.executable, board, "tier", "85", "--json"], cwd=self.repo, env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual((data["tier"], data["effort"]), ("opus", "xhigh"))
        # an explicit HARNESS_CODE_TIER=fable still gives fable on that cache
        write(self.cache_path(88), json.dumps({"number": 88, "title": TITLE, "tier": "fable", "attempts": []}))
        env = self.env(HARNESS_CODE_TIER="fable")
        self.assertEqual(self.attempt_json(88, "code", "fail", env=env)["tier"], "fable")
        self.assertEqual(self.attempt(88, "code", "fail", env=env), "needs-input")

    def test_a_cache_without_labels_keeps_its_tier(self):
        # Caches written by hand or by old tools carry no labels: the cached tier stands, with no effort.
        for n, tier, want in ((81, "sonnet", "escalate opus"), (82, "opus", "escalate opus xhigh")):
            with self.subTest(tier=tier):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": tier, "attempts": []}))
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (tier, None))
                self.assertEqual(self.attempt(n, "code", "fail"), want)


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



IN_PROGRESS = ("task", "status:in-progress", "size:S", "risk:low")


class DispatchDefaultMax(WorkTest):
    """Ticket #130: `harness-work dispatch` without `--max` allows ten live runs at once (it was three)."""

    def board(self, count):
        numbers = list(range(12, 12 + count))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        return numbers

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def test_dry_run_without_max_plans_ten_and_skips_the_rest_over_max_ten(self):
        for count in (12, 15):
            with self.subTest(ready=count):
                numbers = self.board(count)
                p = self.ok("dispatch", "--dry-run")
                self.assertIn("Dry run: venue bg, 0 live, 10 of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), 10, p.stdout)
                for n in numbers[:10]:
                    self.assertIn(f"would start #{n} Ticket {n} (tier sonnet) and launch bg", p.stdout.splitlines())
                for n in numbers[10:]:
                    self.assertIn(f"skip #{n}: over --max 10", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:10])
                self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                                 [(n, "over --max 10") for n in numbers[10:]])
                self.assertEqual(self.claude_calls(), [])

    def test_dry_run_with_ten_or_fewer_ready_tickets_plans_them_all_with_nothing_skipped(self):
        for count in (4, 10):                       # four is past the old default of three
            with self.subTest(ready=count):
                numbers = self.board(count)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers)
                self.assertEqual(data["skipped"], [])
                self.assertNotIn("skip #", self.ok("dispatch", "--dry-run").stdout)

    def test_the_free_slots_are_ten_minus_the_live_runs(self):
        for live_count in (1, 4):
            with self.subTest(live=live_count):
                if os.path.isdir(self.runs_dir()):
                    shutil.rmtree(self.runs_dir())
                live_numbers = list(range(40, 40 + live_count))
                for n in live_numbers:
                    self.write_run(n, pid=self.sleeper(n))
                numbers = list(range(12, 24))
                self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers + live_numbers],
                              ready=[ready_row(n, f"Ticket {n}") for n in numbers])
                free = 10 - live_count
                p = self.ok("dispatch", "--dry-run")
                self.assertIn(f"Dry run: venue bg, {live_count} live, {free} of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), free, p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:free])
                self.assertEqual([s["number"] for s in data["skipped"]], numbers[free:])
                self.assertTrue(all(s["why"] == "over --max 10" for s in data["skipped"]))

    def test_ten_live_runs_fill_the_default_cap(self):
        for n in range(40, 50):
            self.write_run(n, pid=self.sleeper(n))
        self.scenario(issues=[issue(12), issue(13)], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch")
        self.assertIn("10 live runs; --max 10 reached, nothing launched.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_real_dispatch_without_max_launches_ten_of_twelve(self):
        numbers = self.board(12)
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="ten123", **CREDENTIALS))
        for n in numbers[:10]:
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id ten123\)$")
        for n in numbers[10:]:
            self.assertNotRegex(p.stdout, rf"(?m)^#{n} bg on")
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:10]))
        self.assertEqual(len([r for r in self.run_records() if r["venue"] == "bg"]), 10)

    def test_a_real_dispatch_without_max_and_four_live_runs_launches_six(self):
        for n in range(40, 44):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:6]))

    def test_an_explicit_max_still_overrides_the_default(self):
        # 3 is the old default and 10 the new one: an explicit value equal to either must be honored as given.
        for most in (2, 3, 5, 10, 12):
            with self.subTest(max=most):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", "--max", str(most))
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_an_explicit_max_in_the_equals_form_or_abbreviated_is_honored(self):
        # `--max=3`, `--max=5` and the argparse abbreviation `--ma 3` are explicit too; none may fall back to 10.
        for spelling, most in ((("--max=3",), 3), (("--max=5",), 5), (("--max=10",), 10), (("--ma", "3"), 3),
                               (("--ma=5",), 5)):
            with self.subTest(args=" ".join(spelling)):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", *spelling)
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json", *spelling)
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_a_real_dispatch_with_max_equals_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max=3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))

    def test_a_real_dispatch_with_an_explicit_max_of_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))
        self.assertEqual(len(self.run_records()), 3)

    def test_a_real_dispatch_with_an_explicit_max_of_three_and_one_live_run_launches_two(self):
        self.write_run(40, pid=self.sleeper(40))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:2]))

    def test_a_real_dispatch_with_an_explicit_max_of_ten_and_three_live_runs_launches_seven(self):
        for n in range(40, 43):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "10", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:7]))




class DispatchEffort(WorkTest):
    """Ticket #176: `harness-work dispatch` launches each session at the effort the project configures
    (HARNESS_CODE_EFFORT in the main checkout's .claude/settings.json), as `--effort <level>`; the dispatcher's own
    CLAUDE_EFFORT neither decides the flag nor reaches the child. Without the setting nothing changes. WorkTest.env
    drops every CLAUDE* and HARNESS_* variable of the real environment, so these tests set their own."""

    BG_TODAY = ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"]

    def configure(self, **env):
        """The main checkout's settings.json with the given env keys (HARNESS_CODE_EFFORT=...)."""
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    def bg_with_effort(self, name, effort, prompt):
        return ["--bg", "-n", name, "--permission-mode", "auto", "--effort", effort, prompt]

    def warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:")]

    @staticmethod
    def effort_problem(raw):
        """The one warning for an invalid HARNESS_CODE_EFFORT: `harness-work: ` + load_routing's problems line."""
        return f"harness-work: HARNESS_CODE_EFFORT={raw!r} is not one of low, medium, high, xhigh, max; ignored"

    def dispatch_from(self, cwd, *args, env=None):
        p = self.work("dispatch", *args, env=env, cwd=cwd)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def headless_call(self, index=0):
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) > index), self.claude_calls())
        return self.claude_calls()[index]

    def settle(self):
        """Wait for the fake headless runs to end, so no process outlives the test."""
        for record in self.run_records():
            if record.get("pid"):
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_bg_launch_passes_the_configured_effort(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                        "--effort", "xhigh", "/harness:work 12"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["effort"]), ("bg", "abc123", "xhigh"))

    def test_the_flag_follows_the_setting_and_covers_every_launched_ticket(self):
        # Two tickets in one dispatch both get the level; a later dispatch with another level gives that one. A
        # hard-coded level or a flag that ignores the setting fails one of the three launches.
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        self.ok("dispatch", "--max", "2")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": self.bg_with_effort("proj #12 Add the board tool", "low",
                                                               "/harness:work 12"),
            "proj #13 Second": self.bg_with_effort("proj #13 Second", "low", "/harness:work 13")})

        self.configure(HARNESS_CODE_EFFORT="max")
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working",
                              "cwd": self.worktree_path("t/12-add-the-board-tool")}])
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(14, "Third")])
        self.ok("dispatch", env=self.env(FAKECLAUDE_AGENTS=agents))
        newest = self.claude_calls()[-1]["argv"]
        self.assertEqual(newest, self.bg_with_effort("proj #14 Third", "max", "/harness:work 14"))

    def test_every_valid_level_is_passed_as_given(self):
        # The level is the setting's value, lower-cased; one dispatch per level in a project of its own.
        for level, written in (("low", "low"), ("medium", "medium"), ("high", "High"), ("xhigh", "XHIGH"),
                               ("max", "max")):
            with self.subTest(level=written):
                self.setUp()                                     # a fresh project, home and logs per level
                self.configure(HARNESS_CODE_EFFORT=written)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", level,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], level)

    def test_headless_launch_passes_the_configured_effort(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}")
        self.ok("dispatch", "--venue", "headless", env=env)
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.assertNotIn(SECRET, json.dumps(self.claude_calls()))
        self.settle()

    def test_without_the_setting_argv_and_record_are_unchanged(self):
        # No settings file at all, a settings file without the key, and a blank value: all three are "not set".
        cases = (("no settings file", None), ("other keys only", {"HARNESS_CODE_TIER": "opus"}),
                 ("blank value", {"HARNESS_CODE_EFFORT": "  "}))
        for label, env in cases:
            with self.subTest(label):
                self.setUp()
                if env is not None:
                    self.configure(**env)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch")
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertEqual(sorted(record), sorted(["id", "ticket", "repo", "worktree", "branch", "venue", "log",
                                                         "started_at", "ended_at", "exit", "outcome", "bg_id"]))
        with self.subTest("headless"):
            self.setUp()
            self.scenario(issues=[issue()], ready=[ready_row(12)])
            self.ok("dispatch", "--venue", "headless")
            argv = self.headless_call()["argv"]
            self.assertEqual(len(argv), 8, argv)
            self.assertNotIn("--effort", argv)
            [record] = self.run_records()
            self.assertNotIn("effort", record)
            self.settle()

    def test_dry_run_and_json_name_the_effort(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])

        self.configure(HARNESS_CODE_EFFORT="xhigh")
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "Dry run: venue bg, effort xhigh, 1 live, 1 of 2 slots free.")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", lines)
        self.assertTrue(any(ln.startswith("skip #14: over --max 2") for ln in lines), p.stdout)

        p = self.ok("dispatch", "--dry-run", "--max", "3", "--venue", "headless")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue headless, effort xhigh, 1 live, 2 of 3 slots free.")

        self.configure(HARNESS_CODE_EFFORT="Max")                # the level as launched: lower-cased
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "max"))
        self.assertEqual([row["number"] for row in data["plan"]], [12])
        self.assertEqual(self.ok("dispatch", "--dry-run", "--max", "2").stdout.splitlines()[0],
                         "Dry run: venue bg, effort max, 1 live, 1 of 2 slots free.")

        self.configure()                                         # not set: the line is today's, effort is null
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 1 live, 1 of 2 slots free.")
        self.assertNotIn("effort", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        self.assertEqual(self.claude_calls(), [])

    def test_the_json_result_of_a_launch_carries_the_effort(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "high"))
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "high"))

        self.setUp()                                             # unset: the key is there, null, the record has none
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        [launched] = data["launched"]
        self.assertNotIn("effort", launched)

        self.setUp()                                             # nothing to launch: the key is still there
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario()
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["launched"], data["effort"]), ([], "high"))

    def test_the_dispatchers_claude_effort_is_not_inherited(self):
        # The dispatching Claude Code session exports CLAUDE_EFFORT (its own level) to its Bash tool.
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.assertNotIn("max", call["argv"])

        self.setUp()                                             # headless: the same
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", "--venue", "headless", env=self.env(CLAUDE_EFFORT="max"))
        call = self.headless_call()
        self.assertEqual(call["argv"][8:], ["--effort", "low"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.settle()

        self.setUp()                                             # no setting: the dispatcher's level decides nothing
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("--effort", call["argv"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        [record] = self.run_records()
        self.assertNotIn("effort", record)

    def test_invalid_effort_warns_and_launches_without_the_flag(self):
        for bad in ("turbo", "extra-high", "5"):
            with self.subTest(bad=bad):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT=bad)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
                self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                self.assertNotIn("--effort", call["argv"])
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)    # the ticket still launched

    def test_an_invalid_effort_is_one_warning_per_dispatch_not_per_launch(self):
        self.configure(HARNESS_CODE_EFFORT="turbo")
        self.scenario(issues=[issue(12), issue(13, title="Second")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", "--max", "2")
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
        self.assertIn("#12 bg on", p.stdout)
        self.assertIn("#13 bg on", p.stdout)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                            "/harness:work 12"],
            "proj #13 Second": ["--bg", "-n", "proj #13 Second", "--permission-mode", "auto", "/harness:work 13"]})
        self.assertTrue(all("effort" not in r for r in self.run_records()))

    def test_an_invalid_effort_is_warned_about_on_paths_that_launch_nothing(self):
        bad = "turbo"
        with self.subTest("dry run"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario(issues=[issue(12), issue(13, title="Second")],
                          ready=[ready_row(12), ready_row(13, "Second")])
            p = self.ok("dispatch", "--dry-run", "--max", "2")
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 0 live, 2 of 2 slots free.")
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario()
            p = self.ok("dispatch")
            self.assertIn("No ready tickets.", p.stdout)
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board, invalid value from the environment"):
            self.setUp()
            self.scenario()
            p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="extra-high"))
            self.assertEqual(self.warnings(p), [self.effort_problem("extra-high")], p.stderr)

    def test_with_json_the_invalid_effort_warning_is_on_stderr_only(self):
        for label, args, scenario in (
                ("launch", ("dispatch", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("dry run", ("dispatch", "--dry-run", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("empty board", ("dispatch", "--json"), {})):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="turbo")
                self.scenario(**scenario)
                p = self.ok(*args)
                self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
                self.assertNotIn("HARNESS_CODE_EFFORT", p.stdout)
                data = json.loads(p.stdout)                      # stdout stays pure JSON
                self.assertIn("effort", data)
                self.assertIsNone(data["effort"])
                if label == "launch":
                    [launched] = data["launched"]
                    self.assertNotIn("effort", launched)

    def test_a_valid_effort_is_not_warned_about(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        self.assertEqual(p.stderr.strip(), "")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "medium", "/harness:work 12"))

    def test_the_setting_is_read_from_the_main_checkout_whatever_the_cwd(self):
        # The setting lives only in the main checkout's settings.json. Dispatch run from a subdirectory of it, and
        # from another worktree of the repository (whose own settings.json says otherwise), uses the main one.
        for label in ("subdirectory", "other worktree"):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="xhigh")
                cwd = self.mk("proj", "docs", "deep")
                if label == "other worktree":
                    cwd = os.path.join(self.tmp, "side-wt")
                    self.git(self.repo, "worktree", "add", "-q", "-b", "side", cwd)
                    self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.json")))
                    write(os.path.join(cwd, ".claude", "settings.json"),
                          json.dumps({"env": {"HARNESS_CODE_EFFORT": "low"}}))
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.dispatch_from(cwd)
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh",
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_the_setting_is_found_from_a_subdirectory_for_headless_and_dry_run(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        sub = self.mk("proj", "a", "b")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.dispatch_from(sub, "--dry-run", "--max", "3")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, effort medium, 0 live, 3 of 3 slots free.")
        self.dispatch_from(sub, "--venue", "headless")
        self.assertEqual(self.headless_call()["argv"][8:], ["--effort", "medium"])
        self.settle()

    def test_the_environment_beats_the_file_when_not_blank(self):
        for label, in_env, in_file, want in (("env over file", "low", "xhigh", "low"),
                                            ("env level lower-cased", "MAX", "low", "max"),
                                            ("env padded", "  high ", "low", "high"),
                                            ("env only", "medium", None, "medium")):
            with self.subTest(label):
                self.setUp()
                if in_file is not None:
                    self.configure(HARNESS_CODE_EFFORT=in_file)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=in_env))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", want,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], want)

    def test_a_blank_environment_value_falls_back_to_the_file(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="high")
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=blank))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "high",
                                                                   "/harness:work 12"))

    def test_an_invalid_environment_value_is_warned_about_and_not_rescued_by_the_file(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="turbo"))
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

    def test_settings_local_json_is_never_read(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertIsNone(data["effort"])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

        self.setUp()                                             # nor does it override settings.json
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "low", "/harness:work 12"))

    def test_another_invalid_routing_key_is_its_own_warning_and_leaves_the_effort_alone(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p),
                         ["harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored"],
                         p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(self.run_records()[0]["effort"], "xhigh")

        self.setUp()                                             # three invalid keys: three warnings, routing order
        self.configure(HARNESS_CODE_EFFORT="turbo", HARNESS_REVIEW_TIER="gold", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [
            "harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored",
            self.effort_problem("turbo"),
            "harness-work: HARNESS_REVIEW_TIER='gold' is not one of sonnet, opus, fable; ignored"], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)

    def test_a_redispatched_ticket_gets_the_effort_too(self):
        # #12 is in progress and its last run ended in an error; #13 is fresh from the board.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Fresh")],
                      ready=[ready_row(13, "Fresh")])
        dry = self.ok("dispatch", "--dry-run", "--max", "3")
        self.assertIn("would start #12 Add the board tool (again after error) and launch bg", dry.stdout)
        self.assertEqual(dry.stdout.splitlines()[0], "Dry run: venue bg, effort xhigh, 0 live, 3 of 3 slots free.")

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo57\)$")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls["proj #12 Add the board tool"],
                         self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(calls["proj #13 Fresh"], self.bg_with_effort("proj #13 Fresh", "xhigh", "/harness:work 13"))
        again = next(r for r in self.run_records() if r["ticket"] == 12 and r.get("bg_id") == "redo57")
        self.assertEqual(again["effort"], "xhigh")

    def test_a_redispatched_ticket_launches_headless_with_the_effort(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        self.ok("dispatch", "--venue", "headless")
        argv = self.headless_call()["argv"]
        self.assertEqual((argv[:2], argv[8:]), (["-p", "/harness:work 12"], ["--effort", "high"]))
        self.settle()


class DispatchRouteEffort(WorkTest):
    """Ticket #239: with HARNESS_CODE_EFFORT unset, `harness-work dispatch` launches each ticket at its route's effort:
    a size:L or risk:high ticket gets `--effort xhigh` (bg and headless) and `effort: xhigh` in its run record; any
    other ticket launches as before, with no flag. HARNESS_CODE_EFFORT still wins when set; a HARNESS_CODE_TIER
    override replaces the labels' effort with its tier."""

    configure = DispatchEffort.configure
    bg_with_effort = DispatchEffort.bg_with_effort
    headless_call = DispatchEffort.headless_call
    settle = DispatchEffort.settle

    @staticmethod
    def labels_for(size, risk, kind="task", status="status:ready"):
        return (kind, status) + (("size:" + size,) if size else ()) + (("risk:" + risk,) if risk else ())

    @staticmethod
    def name(n, title):
        return f"proj #{n} {title}"

    def bg_today(self, n, title):
        return ["--bg", "-n", self.name(n, title), "--permission-mode", "auto", f"/harness:work {n}"]

    def test_each_ticket_launches_at_its_routes_effort(self):
        # One dispatch, four tickets: the route's effort per ticket (xhigh for size:L or risk:high, else no flag).
        tickets = [(12, "Small", "task", "S", "low"), (13, "Risky", "task", "M", "high"),
                   (14, "Large", "task", "L", "low"), (15, "Medium bug", "bug", "M", "low")]
        self.scenario(issues=[issue(n, labels=self.labels_for(s, r, k), title=t) for n, t, k, s, r in tickets],
                      ready=[ready_row(n, t, labels=self.labels_for(s, r, k)) for n, t, k, s, r in tickets])
        p = self.ok("dispatch", "--max", "4")
        self.assertNotIn("harness-work:", p.stderr)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        records = {r["ticket"]: r for r in self.run_records()}
        for n, title, kind, size, risk in tickets:
            effort = route_of(kind, size, risk)[1]
            with self.subTest(ticket=n, size=size, risk=risk):
                if effort:
                    self.assertEqual(calls[self.name(n, title)],
                                     self.bg_with_effort(self.name(n, title), effort, f"/harness:work {n}"))
                    self.assertEqual(records[n]["effort"], effort)
                else:
                    self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                    self.assertNotIn("effort", records[n])

    def test_a_size_l_ticket_launches_bg_with_effort_xhigh(self):
        large = self.labels_for("L", "low")
        self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
        data = self.ok_json("dispatch", "--json")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        [record] = self.run_records()
        self.assertEqual(record["effort"], "xhigh")
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "xhigh"))

    def test_a_risk_high_ticket_launches_headless_with_effort_xhigh(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        risky = self.labels_for("S", "high")
        self.scenario(issues=[issue(labels=risky)], ready=[ready_row(12, labels=risky)])
        self.ok("dispatch", "--venue", "headless", env=self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}"))
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:2], ["-p", "/harness:work 12"])
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.settle()

    def test_the_code_effort_setting_wins_over_the_route(self):
        # Unset: each ticket at its route's effort. Set (file or environment): every ticket at the setting.
        tickets = [(12, "Small", "S", "low"), (13, "Large", "L", "high")]
        for level, where in ((None, None), ("low", "settings.json"), ("max", "environment"),
                             ("medium", "settings.json")):
            with self.subTest(level=level, where=where):
                self.setUp()
                self.scenario(issues=[issue(n, labels=self.labels_for(s, r), title=t) for n, t, s, r in tickets],
                              ready=[ready_row(n, t, labels=self.labels_for(s, r)) for n, t, s, r in tickets])
                env = None
                if where == "settings.json":
                    self.configure(HARNESS_CODE_EFFORT=level)
                elif where == "environment":
                    env = self.env(HARNESS_CODE_EFFORT=level)
                self.ok("dispatch", "--max", "2", env=env)
                calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
                records = {r["ticket"]: r for r in self.run_records()}
                for n, title, size, risk in tickets:
                    want = level or route_of("task", size, risk)[1]
                    if want:
                        self.assertEqual(calls[self.name(n, title)],
                                         self.bg_with_effort(self.name(n, title), want, f"/harness:work {n}"))
                        self.assertEqual(records[n]["effort"], want)
                    else:
                        self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                        self.assertNotIn("effort", records[n])

    def test_a_tier_override_replaces_the_labels_effort(self):
        # HARNESS_CODE_TIER replaces the label rubric, its effort included: a size:L ticket launches with no flag
        # unless HARNESS_CODE_EFFORT is set too. Without the override the same ticket launches at xhigh.
        large = self.labels_for("L", "high")
        for tier in (None, "opus", "sonnet", "fable"):
            with self.subTest(tier=tier):
                self.setUp()
                if tier:
                    self.configure(HARNESS_CODE_TIER=tier)
                self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                if tier:
                    self.assertEqual(call["argv"], self.bg_today(12, TITLE))
                    self.assertNotIn("effort", self.run_records()[0])
                else:
                    self.assertEqual(call["argv"], self.bg_with_effort(self.name(12, TITLE), "xhigh",
                                                                       "/harness:work 12"))
                    self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_a_redispatched_size_l_ticket_gets_xhigh(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        large = self.labels_for("L", "low", status="status:in-progress")
        self.scenario(issues=[issue(12, labels=large)])
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="redo58"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo58\)$")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        again = next(r for r in self.run_records() if r.get("bg_id") == "redo58")
        self.assertEqual(again["effort"], "xhigh")


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


# =========================================================================== cloud (#58)
# `harness-work cloud N` starts the ticket, pushes its branch and runs `claude --cloud <prompt> --on-branch <branch>`
# with its stdout on a pseudo-terminal; the fake claude refuses without one, as the real one does. The sweep closes a
# cloud run only from a marker comment by the gh user (or a finished ticket, or 24 h of silence).
CLOUD_DONE = "<!-- harness-cloud: done -->"
CLOUD_BLOCKED = "<!-- harness-cloud: blocked -->"
ME = "rjeskuri"            # what the scenario's `gh api user` prints
IN_PROGRESS_LABELS = ("task", "status:in-progress", "size:S", "risk:low")


def exhausted(entry):
    """The exhausted_until of a budget entry when it is still ahead, else None."""
    if not isinstance(entry, dict) or not entry.get("exhausted_until"):
        return None
    when = dt.datetime.strptime(entry["exhausted_until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return when if when > dt.datetime.now(dt.timezone.utc) else None


class CloudTest(WorkTest):
    SLUG = "acme/widgets"

    def cloud_scenario(self, issues, slug=None, ready=(), failing=()):
        """issues: the `gh issue view N` answers (comments included). failing: calls (argv prefixes) that exit 1."""
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        calls = [dict(gone, match=list(prefix)) for prefix in failing]
        calls += [{"match": ["issue", "view", str(i["number"])], "stdout_json": i} for i in issues]
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": slug or self.SLUG}},
            {"match": ["issue", "list"], "stdout_json": list(ready)},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/acme/widgets/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": ME + "\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def gh_entries(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def comment_bodies(self, n):
        """The bodies of every `gh issue comment n` (--body or --body-file -)."""
        out = []
        for entry in self.gh_entries():
            argv = entry["argv"]
            if argv[:3] == ["issue", "comment", str(n)]:
                if "--body" in argv:
                    out.append(argv[argv.index("--body") + 1])
                elif "--body-file" in argv:
                    out.append(entry.get("stdin") or "")
        return out

    def added_labels(self, n):
        edits = [a for a in self.gh_calls() if a[:3] == ["issue", "edit", str(n)]]
        return [a[i + 1] for a in edits for i, tok in enumerate(a[:-1]) if tok == "--add-label"]

    def cloud_calls(self):
        return [c for c in self.claude_calls() if "--cloud" in c["argv"]]

    def commit_settings(self, env):
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "Settings")
        self.git(self.repo, "push", "-q", "origin", "main")

    def cloud_run(self, n, started_at, **fields):
        sid = f"session_0{n}SweptAbc"
        return self.write_run(n, venue="cloud", cloud_id=sid, url=f"https://claude.ai/code/{sid}?from=cli&m=0",
                              branch=f"t/{n}-x", started_at=started_at, **fields)


class CloudLaunch(CloudTest):
    def launch(self, n, title, sid, settings, slug, *extra):
        self.commit_settings(settings)
        self.cloud_scenario([issue(n, title=title)], slug=slug)
        env = self.env(FAKECLAUDE_CLOUD_ID=sid, FAKECLAUDE_TITLE=title, **CREDENTIALS)
        p = self.work("cloud", str(n), *extra, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def check_launch(self, n, branch, sid, slug, verify, absent=()):
        """The one cloud launch of ticket n: its argv, prompt, push, record, log and comments. Returns the record."""
        wt = self.worktree_path(branch)
        self.assertEqual(self.git(wt, "branch", "--show-current"), branch)
        [call] = self.cloud_calls()
        argv = call["argv"]
        self.assertTrue(call["tty"])
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        prompt = argv[argv.index("--cloud") + 1]
        self.assertFalse(prompt.startswith("-"), argv)
        self.assertEqual(argv[argv.index("--on-branch") + 1], branch)
        for name in CREDENTIALS:
            self.assertNotIn(name, call["env"])

        # the prompt is self-contained: repository, ticket, branch, gh api REST, both markers, the verify command
        for needle in (slug, f"#{n}", branch, "gh api", f"repos/{slug}/issues/{n}/comments", CLOUD_DONE,
                       CLOUD_BLOCKED, verify, "`harness-board ready` lists unblocked tickets"):
            self.assertIn(needle, prompt)
        for needle in absent:
            self.assertNotIn(needle, prompt)

        # pushed to origin before the launch, with an upstream
        head = self.git(wt, "rev-parse", "HEAD")
        self.assertIn(f"{head}\trefs/heads/{branch}", call["remote_heads"])
        self.assertEqual(self.git(wt, "rev-parse", "--abbrev-ref", "@{u}"), f"origin/{branch}")

        [record] = [r for r in self.run_records() if r["ticket"] == n and not r.get("ended_at")]
        url = record["url"]
        self.assertTrue(url.startswith(f"https://claude.ai/code/{sid}"), url)
        self.assertIsNone(re.search(r"[\x00-\x20\x7f]", url), repr(url))
        expected = {"ticket": n, "repo": os.path.realpath(self.repo), "worktree": wt, "branch": branch,
                    "venue": "cloud", "cloud_id": sid, "ended_at": None, "exit": None, "outcome": None}
        self.assertEqual({k: record.get(k) for k in expected}, expected)
        self.assertTrue({"id", "log", "started_at"} <= set(record), record)
        self.iso_close_to(record["started_at"], dt.timedelta(0))
        log = read_text(record["log"])
        self.assertIn("Created cloud session", log)
        self.assertIn(sid, log)
        self.assertNotIn("\r", log)
        self.assertNotIn("\x1b", log)

        # status in-progress with a start comment naming the venue, and the comment carrying the URL
        self.assertIn("status:in-progress", self.added_labels(n))
        bodies = self.comment_bodies(n)
        self.assertTrue([b for b in bodies if b.startswith(f"Started on {branch}") and "cloud" in b], bodies)
        self.assertTrue([b for b in bodies if f"Cloud session on {branch}: {url}" in b], bodies)
        return record

    def test_cloud_launch_end_to_end(self):
        sid = "session_01HwXq7Kp-Lm_9"
        p = self.launch(12, TITLE, sid, {"CLAUDE_VERIFY_CMD": "make verify-widgets",
                                         "HARNESS_TEST_CMD": "pytest -q {files}"}, "acme/widgets")
        record = self.check_launch(12, BRANCH, sid, "acme/widgets", "make verify-widgets", absent=("pytest -q",))
        url = record["url"]
        self.assertIn(f"#12 cloud on {BRANCH} ({url})", p.stdout)

        # runs lists it in the cloud; --json keeps cloud_id and url; a sweep with no marker leaves it open
        [row] = [r for r in self.ok_json("runs", "--json") if r["id"] == record["id"]]
        self.assertEqual((row["cloud_id"], row["url"], row["state"]), (sid, url, f"in the cloud ({url})"))
        line = next(ln for ln in self.ok("runs").stdout.splitlines() if record["id"] in ln)
        self.assertIn("in the cloud", line)
        [row] = [r for r in self.ok_json("runs", "--sweep", "--json") if r["id"] == record["id"]]
        self.assertIsNone(row["ended_at"])
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_cloud_launch_varies_with_the_ticket_session_repository_and_verify_command(self):
        # an ended cloud run of the ticket does not stop a new one, nor does an exhausted api budget
        old = self.cloud_run(31, iso_ago(hours=40), ended_at=iso_ago(hours=30), outcome="ok")
        self.write_budget("api", 5)
        sid = "session_9zz_Other-Run2"
        p = self.launch(31, "Cache the ready list", sid, {"HARNESS_TEST_CMD": "python3 -m pytest tests -q"},
                        "someone/thing", "--json")
        branch = "t/31-cache-the-ready-list"
        record = self.check_launch(31, branch, sid, "someone/thing", "python3 -m pytest tests -q",
                                   absent=("acme/widgets", BRANCH))
        self.assertEqual(json.loads(p.stdout), record)
        records = self.run_records()
        self.assertEqual(len(records), 2)
        self.assertIn(old, records)

    def test_the_session_id_comes_from_the_view_url_without_a_resume_line(self):
        self.commit_settings({"CLAUDE_VERIFY_CMD": "make check"})
        self.cloud_scenario([issue()])
        sid = "session_77NoResume_x"
        self.ok("cloud", "12", env=self.env(FAKECLAUDE_CLOUD="noresume", FAKECLAUDE_CLOUD_ID=sid))
        [record] = self.run_records()
        self.assertEqual(record["cloud_id"], sid)
        self.assertTrue(record["url"].startswith(f"https://claude.ai/code/{sid}"), record["url"])
        self.assertIsNone(record["ended_at"])

    def test_start_accepts_the_cloud_venue(self):
        self.scenario(issues=[issue()])
        data = self.ok_json("start", "12", "--venue", "cloud", "--json")
        self.assertEqual(data["branch"], BRANCH)
        comments = [c for c in self.gh_calls() if c[:3] == ["issue", "comment", "12"]]
        self.assertTrue([c for c in comments if "cloud" in c[c.index("--body") + 1]], comments)
        self.assertFalse(self.claude_calls())


class CloudRefusal(CloudTest):
    def assertNothingStarted(self, records_before):
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        calls = self.gh_calls()
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertFalse(self.claude_calls())
        self.assertEqual(self.run_records(), records_before)
        self.assertEqual(self.git(self.origin, "for-each-ref", "refs/heads/t"), "")

    def test_a_needs_local_ticket_is_refused_with_the_reason(self):
        for n in (12, 40):
            with self.subTest(ticket=n):
                self.cloud_scenario([issue(n, labels=("task", "status:ready", "size:S", "risk:low", "needs-local"))])
                p = self.work("cloud", str(n))
                self.assertOneLineError(p, "needs-local", f"/harness:work {n}")
                self.assertEqual(p.stdout, "")
                self.assertNothingStarted([])

    def test_an_exhausted_cloud_budget_is_refused(self):
        self.write_budget("cloud", 5, reason="credits used up")
        until = self.read_json(self.budgets_path())["cloud"]["exhausted_until"]
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12")
        self.assertOneLineError(p, "harness-work budget reset cloud", until)
        self.assertNothingStarted([])

    def test_an_unended_cloud_run_of_the_ticket_is_refused(self):
        record = self.cloud_run(12, iso_ago(hours=1))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS)])
        p = self.work("cloud", "12")
        self.assertOneLineError(p, record["id"])
        self.assertNothingStarted([record])

    def test_epic_closed_and_blocked_tickets_get_starts_refusal(self):
        cases = [(issue(labels=("epic", "status:ready"), title="The board"), "#12 is an epic"),
                 (issue(state="CLOSED"), "#12 is closed"),
                 (issue(labels=("task", "status:blocked")), "status:blocked")]
        for record, needle in cases:
            with self.subTest(needle=needle):
                self.cloud_scenario([record])
                self.assertOneLineError(self.work("cloud", "12"), needle)
                self.assertNothingStarted([])


class CloudLaunchFailure(CloudTest):
    def test_a_failed_launch_or_one_without_a_session_id_ends_the_record_in_error(self):
        cases = (("fail", "Error: cloud environment unavailable (HTTP 503)"),
                 ("attached", "No cloud session was created or opened: it would open in this terminal and need the "
                              "keyboard."))
        for mode, last in cases:
            with self.subTest(mode=mode):
                shutil.rmtree(self.runs_dir(), True)
                self.cloud_scenario([issue()])
                p = self.work("cloud", "12", env=self.env(FAKECLAUDE_CLOUD=mode))
                self.assertOneLineError(p, last)
                self.assertNotIn("\x1b", p.stderr)
                [record] = self.run_records()
                self.assertEqual((record["venue"], record["outcome"]), ("cloud", "error"))
                self.assertTrue(record["ended_at"])
                self.assertIn(last, record.get("error") or "")
                self.assertEqual(len(self.cloud_calls()), 1)
                os.unlink(self.claude_log)
        status = self.ok("budget", "status").stdout
        self.assertIn("cloud: available", status)
        self.assertIn("api: available", status)

    def test_billing_wording_marks_the_cloud_budget_exhausted(self):
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12", env=self.env(FAKECLAUDE_CLOUD="billing"))
        self.assertOneLineError(p, "credits are used up")
        [record] = self.run_records()
        self.assertEqual(record["outcome"], "error")
        budgets = self.read_json(self.budgets_path())
        self.iso_close_to(budgets["cloud"]["exhausted_until"], dt.timedelta(hours=24))
        self.assertIsNone(exhausted(budgets.get("api")))
        # the next launch is refused by the budget, not by the ended run
        self.assertOneLineError(self.work("cloud", "12"), "harness-work budget reset cloud")
        self.assertEqual(len(self.cloud_calls()), 1)

    def test_a_failed_push_launches_nothing(self):
        executable(os.path.join(self.origin, "hooks", "pre-receive"),
                   "#!/bin/sh\necho 'push refused by the test hook' >&2\nexit 1\n")
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12")
        self.assertOneLineError(p)
        self.assertIn("push", p.stderr.lower())
        self.assertTrue(os.path.isdir(self.worktree_path()))     # started first, then the push failed
        self.assertFalse(self.cloud_calls())
        self.assertEqual(self.git(self.origin, "for-each-ref", "refs/heads/t"), "")


class CloudSweep(CloudTest):
    def test_the_sweep_closes_a_cloud_run_only_on_a_marker_by_the_gh_user_after_its_start(self):
        two = iso_ago(hours=2)
        t = {k: iso_ago(minutes=k) for k in (10, 20, 30, 50, 60, 70, 90, 180)}
        cases = {   # ticket: (started_at, labels, state, comments, expected outcome, expected ended_at or "now")
            21: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[60], CLOUD_DONE + "\nDid it.\nRuling: x")],
                 "ok", t[60]),                                       # in progress, no PR: still ok, never early
            22: (two, IN_PROGRESS_LABELS, "OPEN", [comment("drive-by-user", t[60], CLOUD_DONE)], None, None),
            23: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[180], CLOUD_DONE)], None, None),
            24: (two, IN_PROGRESS_LABELS, "OPEN", [], None, None),
            25: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[60], "Progress so far.\n" + CLOUD_DONE)], None, None),
            27: (iso_ago(hours=25), IN_PROGRESS_LABELS, "OPEN", [], "unknown", "now"),
            28: (two, ("task", "status:in-review", "size:S", "risk:low"), "OPEN", [], "ok", "now"),
            29: (two, IN_PROGRESS_LABELS, "CLOSED", [], "ok", "now"),
            31: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[90], CLOUD_BLOCKED + "\nWhich base?"),
                                                   comment(ME, t[30], CLOUD_DONE + "\nAnswered and done.")],
                 "ok", t[30]),
            32: (iso_ago(hours=23), IN_PROGRESS_LABELS, "OPEN", [], None, None),
            33: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[70], CLOUD_DONE + "\nDone."),
                                                   comment(ME, t[20], "Started on t/33-x (mac, interactive)" + MARK)],
                 "ok", t[70]),
            34: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[50], CLOUD_DONE + "\nDone."),
                                                   comment("drive-by-user", t[10], CLOUD_BLOCKED + "\nStop?")],
                 "ok", t[50]),
        }
        records = {n: self.cloud_run(n, c[0]) for n, c in cases.items()}
        self.cloud_scenario([issue(n, labels=c[1], state=c[2], comments=c[3]) for n, c in cases.items()])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        saved = {r["ticket"]: r for r in self.run_records()}
        for n, (_, _, _, _, outcome, ended) in cases.items():
            with self.subTest(ticket=n):
                self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), (outcome, outcome))
                if ended is None:
                    self.assertIsNone(saved[n]["ended_at"])
                    self.assertEqual(rows[n]["state"], f"in the cloud ({records[n]['url']})")
                elif ended == "now":
                    self.iso_close_to(saved[n]["ended_at"], dt.timedelta(0))
                    self.assertEqual(rows[n]["state"], f"ended {outcome}")
                else:
                    self.assertEqual(saved[n]["ended_at"], ended)
                    self.assertEqual(rows[n]["state"], f"ended {outcome}")
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])   # no needs-input here
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_a_blocked_marker_ends_the_run_blocked_and_asks_the_owner(self):
        two = iso_ago(hours=2)
        asked = {26: (iso_ago(minutes=60), "Should the cache live in ~/.cache or under the repo?"),
                 30: (iso_ago(minutes=25), "The ticket names two verify commands; which one is right?")}
        comments = {26: [comment(ME, asked[26][0], CLOUD_BLOCKED + "\n" + asked[26][1] + "\n")],
                    30: [comment(ME, iso_ago(minutes=90), CLOUD_DONE + "\nFirst pass."),
                         comment(ME, asked[30][0], CLOUD_BLOCKED + "\n" + asked[30][1])]}
        for n in asked:
            self.cloud_run(n, two)
        self.cloud_scenario([issue(n, labels=IN_PROGRESS_LABELS, comments=comments[n]) for n in asked])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        saved = {r["ticket"]: r for r in self.run_records()}
        for n, (when, question) in asked.items():
            with self.subTest(ticket=n):
                self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), ("blocked", "blocked"))
                self.assertEqual(saved[n]["ended_at"], when)
                self.assertEqual(rows[n]["state"], "ended blocked")
                self.assertIn("status:needs-input", self.added_labels(n))
                bodies = self.comment_bodies(n)
                asking = [b for b in bodies if question in b]
                self.assertEqual(len(asking), 1, bodies)
                self.assertNotIn(CLOUD_BLOCKED, asking[0])
                for m, (_, other) in asked.items():
                    if m != n:
                        self.assertNotIn(other, "\n".join(bodies))
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_a_failing_gh_call_is_one_warning_and_leaves_the_run_open(self):
        for failing in (["issue", "view", "12"], ["api", "user"]):
            with self.subTest(failing=failing):
                shutil.rmtree(self.runs_dir(), True)
                record = self.cloud_run(12, iso_ago(hours=2))
                self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS,
                                           comments=[comment(ME, iso_ago(hours=1), CLOUD_DONE)])],
                                    failing=[failing])
                p = self.work("runs", "--sweep", "--json")
                self.assertEqual(p.returncode, 0, p.stderr)
                [row] = json.loads(p.stdout)
                self.assertIsNone(row["ended_at"])
                self.assertEqual(self.run_records(), [record])
                warnings = [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:")]
                self.assertEqual(len(warnings), 1, p.stderr)

    def test_dispatch_treats_an_unended_cloud_run_as_live(self):
        record = self.cloud_run(12, iso_ago(hours=1))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS), issue(13, title="Next")],
                            ready=[ready_row(12), ready_row(13, "Next")])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([r["number"] for r in data["plan"]], [13])
        self.assertIn({"number": 12, "why": f"live run {record['id']}"}, data["skipped"])
        self.assertEqual(data["live"], [record["id"]])
        self.assertIsNone(self.run_records()[0]["ended_at"])

    def test_dispatch_sweeps_a_done_cloud_run_and_picks_the_ticket_up_again(self):
        when = iso_ago(minutes=40)
        self.cloud_run(12, iso_ago(hours=3))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS, comments=[comment(ME, when, CLOUD_DONE + "\nDone.")])])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        [record] = self.run_records()
        self.assertEqual((record["outcome"], record["ended_at"]), ("ok", when))
        self.assertEqual([(r["number"], r.get("again")) for r in data["plan"]], [(12, "ok")])
        self.assertEqual(data["live"], [])


# =========================================================================== start: fast-forward (#58)
class StartFastForward(WorkTest):
    def started_and_pushed(self):
        self.scenario(issues=[issue()])
        wt = self.ok_json("start", "12", "--json")["worktree"]
        self.git(wt, "push", "-q", "-u", "origin", BRANCH)
        return wt

    def elsewhere(self):
        """A second clone on the branch: where the cloud session pushes from."""
        other = self.other_clone()
        self.git(other, "switch", "-q", BRANCH)
        return other

    def commit(self, clone, name, push=True):
        write(os.path.join(clone, name), name + "\n")
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", name)
        if push:
            self.git(clone, "push", "-q", "origin", BRANCH)
        return self.git(clone, "rev-parse", "HEAD")

    def branch_warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:") and BRANCH in ln]

    def test_a_clean_worktree_behind_origin_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        p = self.ok("start", "12")                                # up to date: nothing to say
        self.assertEqual(self.branch_warnings(p), [])
        other = self.elsewhere()
        for name in ("CLOUD1.md", "CLOUD2.md"):
            head = self.commit(other, name)
            p = self.ok("start", "12")
            self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
            self.assertTrue(os.path.isfile(os.path.join(wt, name)))
            self.assertEqual(self.branch_warnings(p), [])
            self.assertEqual(self.git(wt, "status", "--porcelain"), "")

    def test_a_reused_local_branch_without_a_worktree_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        self.git(self.repo, "worktree", "remove", wt)
        head = self.commit(self.elsewhere(), "CLOUD.md")
        data = self.ok_json("start", "12", "--json")
        self.assertEqual(data["worktree"], wt)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.repo, "rev-parse", BRANCH), head)

    def test_a_diverged_branch_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        local = self.commit(wt, "LOCAL.md", push=False)
        p = self.ok("start", "12")                                # only ahead: nothing to say, nothing changes
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.commit(self.elsewhere(), "CLOUD.md")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.assertFalse(os.path.exists(os.path.join(wt, "CLOUD.md")))

    def test_a_dirty_worktree_behind_origin_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        before = self.git(wt, "rev-parse", "HEAD")
        head = self.commit(self.elsewhere(), "CLOUD.md")
        write(os.path.join(wt, "README.md"), "# proj\nedited here\n")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), before)
        self.assertEqual(read_text(os.path.join(wt, "README.md")), "# proj\nedited here\n")
        self.git(wt, "checkout", "--", "README.md")              # clean again: the next start fast-forwards
        p = self.ok("start", "12")
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)


# =========================================================================== cloud: skill and docs (#58)


# =========================================================================== waves (#180)
WAVE_ID_RE = r"^w\d{14}(?:-\d+)?$"


class Waves(WorkTest):
    """#180: with HARNESS_WAVE_REVIEW set, dispatch records the tickets one call launched as a wave
    (harness_lib/waves.py) and stamps each run record with its id; `waves` shows where each wave stands from the
    ticket caches, `--refresh` asking gh once per waiting ticket; `wave drop` takes a stuck ticket out."""

    def waves_dir(self):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key, "waves")

    def wave_files(self):
        folder = self.waves_dir()
        if not os.path.isdir(folder):
            return []
        return [self.read_json(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if n.endswith(".json")]

    def write_wave(self, wave_id, tickets, review=None, created_at="2026-10-20T10:00:00Z", dropped=()):
        wave = {"id": wave_id, "repo": os.path.realpath(self.repo), "created_at": created_at,
                "tickets": list(tickets), "dropped": list(dropped), "tier": "fable", "effort": "xhigh",
                "review": review}
        write(os.path.join(self.waves_dir(), wave_id + ".json"), json.dumps(wave))
        return wave

    def write_cache(self, n, status):
        write(self.cache_path(n), json.dumps({"number": n, "title": f"Ticket {n}", "status": status}))

    def wave_env(self, tier="fable"):
        return self.env(HARNESS_WAVE_REVIEW=tier)

    def test_dispatch_groups_the_launched_tickets_into_one_wave(self):
        self.scenario(issues=[issue(), issue(13, title="Second")], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        [wave] = self.wave_files()
        self.assertRegex(wave["id"], WAVE_ID_RE)
        self.assertEqual((wave["tickets"], wave["dropped"], wave["tier"], wave["effort"], wave["review"]),
                         ([12, 13], [], "fable", "xhigh", None))
        self.assertEqual(wave["repo"], os.path.realpath(self.repo))
        self.assertIn(f"wave {wave['id']}: #12, #13 (reviewed together once all are in review)",
                      p.stdout.splitlines())
        records = self.run_records()
        self.assertEqual(sorted(r["ticket"] for r in records), [12, 13])
        for record in records:
            self.assertEqual(record["wave"], wave["id"])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "view"] and "state,labels" in c])

    def test_dispatch_json_names_the_wave(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env("opus"))
        [wave] = self.wave_files()
        self.assertEqual(data["wave"], wave["id"])
        self.assertEqual((wave["tier"], wave["effort"]), ("opus", "xhigh"))
        self.assertEqual([r["wave"] for r in data["launched"]], [wave["id"]])

    def test_without_the_setting_no_wave_is_written(self):
        for value in (None, "off"):
            with self.subTest(HARNESS_WAVE_REVIEW=value):
                self.setUp()
                self.scenario(issues=[issue(), issue(13, title="Second")],
                              ready=[ready_row(12), ready_row(13, "Second")])
                env = self.env() if value is None else self.env(HARNESS_WAVE_REVIEW=value)
                p = self.ok("dispatch", env=env)
                self.assertFalse(os.path.exists(self.waves_dir()))
                self.assertNotIn("wave", p.stdout)
                self.assertEqual(sorted(p.stdout.splitlines()),
                                 sorted([f"#12 bg on {BRANCH} (id abc123)",
                                         "#13 bg on t/13-second (id abc123)"]))
                for record in self.run_records():
                    self.assertNotIn("wave", record)
                self.setUp()
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                data = self.ok_json("dispatch", "--json", env=env)
                self.assertNotIn("wave", data)
                self.assertNotIn("wave", data["launched"][0])
                dry = self.ok_json("dispatch", "--dry-run", "--json", env=env)
                self.assertNotIn("wave", dry)
                self.assertNotIn("group", self.ok("dispatch", "--dry-run", env=env).stdout)

    def test_a_redispatched_ticket_keeps_its_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Second")], ready=[ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        waves = {w["id"]: w for w in self.wave_files()}
        self.assertEqual(len(waves), 2, waves)
        self.assertEqual(waves[old["id"]]["tickets"], [12])             # untouched
        [new] = [w for w in waves.values() if w["id"] != old["id"]]
        self.assertEqual(new["tickets"], [13])                           # 12 joins no new wave
        self.assertIn(f"wave {new['id']}: #13 (reviewed together once all are in review)", p.stdout.splitlines())
        launched = {r["ticket"]: r for r in self.run_records() if r.get("ended_at") is None}
        self.assertEqual(launched[12]["wave"], old["id"])
        self.assertEqual(launched[13]["wave"], new["id"])

    def test_a_dispatch_of_only_redispatched_tickets_makes_no_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        self.assertEqual([w["id"] for w in self.wave_files()], [old["id"]])
        self.assertIsNone(data["wave"])
        self.assertEqual([r["wave"] for r in data["launched"]], [old["id"]])

    def test_a_ticket_whose_wave_was_reviewed_joins_a_new_one(self):
        old = self.write_wave("w20261020100000", [12], review={"launched_at": "2026-10-21T10:00:00Z", "run": "r1",
                                                                "reviewed_at": "2026-10-21T11:00:00Z"})
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        [new] = [w for w in self.wave_files() if w["id"] != old["id"]]
        self.assertEqual((data["wave"], new["tickets"]), (new["id"], [12]))

    def test_dry_run_names_the_grouping_and_writes_nothing(self):
        self.write_wave("w20261020100000", [14])
        self.write_run(14, pid=self.dead_pid(), branch="t/14-x", started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(), issue(13, title="Second"), issue(14, labels=IN_PROGRESS, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        before = self.wave_files()
        p = self.ok("dispatch", "--dry-run", env=self.wave_env())
        self.assertIn("would group #12, #13 into a wave", p.stdout.splitlines())
        self.assertIn("would start #14 Third (again after early) and launch bg", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json", env=self.wave_env())
        self.assertEqual(data["wave"], {"id": None, "tickets": [12, 13]})
        self.assertEqual(self.wave_files(), before)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)

    def test_waves_lists_progress_from_the_caches_without_gh(self):
        self.write_wave("w20261020100000", [12, 13], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [14], created_at="2026-10-20T11:00:00Z")
        self.write_wave("w20261020120000", [15], created_at="2026-10-20T12:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        self.write_wave("w20261020130000", [16], created_at="2026-10-20T13:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-78",
                                "reviewed_at": "2026-10-21T11:30:00Z"})
        self.write_wave("w20261020140000", [17], created_at="2026-10-20T14:00:00Z")   # no ticket cache
        for n, status in ((12, "in-review"), (13, "in-progress"), (14, "in-review"), (15, "in-review"),
                          (16, "closed")):
            self.write_cache(n, status)
        text = self.ok("waves").stdout
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "TICKETS", "STATE", "DETAIL"])
        rows = {ln.split()[0]: ln for ln in lines[1:]}
        self.assertEqual(sorted(rows), ["w20261020100000", "w20261020110000", "w20261020120000",
                                        "w20261020140000"])          # 16 is closed: hidden without --all
        self.assertIn("#12, #13", rows["w20261020100000"])
        self.assertIn("working", rows["w20261020100000"])
        self.assertIn("1 of 2 in review; waiting for #13 (in-progress)", rows["w20261020100000"])
        self.assertIn("due", rows["w20261020110000"])
        self.assertIn("reviewing (run run-77)", rows["w20261020120000"])
        self.assertIn("waiting for #17 (", rows["w20261020140000"])
        every = {ln.split()[0]: ln for ln in self.ok("waves", "--all").stdout.splitlines()[1:]}
        self.assertIn("reviewed 2026-10-21T11:30:00Z", every["w20261020130000"])
        data = self.ok_json("waves", "--json")
        first = data[0]
        self.assertEqual((first["id"], first["state"], first["done"], first["waiting"]),
                         ("w20261020100000", "working", [12], [{"number": 13, "status": "in-progress"}]))
        self.assertEqual([w["id"] for w in data], sorted(rows))
        self.assertEqual(self.gh_calls(), [])

    def test_waves_with_none_says_so(self):
        p = self.ok("waves")
        self.assertIn("No open waves", p.stdout)
        self.assertEqual(self.ok_json("waves", "--json"), [])
        self.assertEqual(self.gh_calls(), [])

    def test_waves_refresh_asks_gh_once_per_waiting_ticket(self):
        self.write_wave("w20261020100000", [12, 13, 14], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [13, 15], created_at="2026-10-20T11:00:00Z")
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        self.write_cache(15, "in-progress")
        self.scenario(issues=[issue(13, labels=IN_REVIEW), issue(14, labels=IN_PROGRESS),
                              issue(15, labels=IN_PROGRESS, state="CLOSED")])
        data = {w["id"]: w for w in self.ok_json("waves", "--refresh", "--json")}
        views = [c for c in self.gh_calls() if c[:2] == ["issue", "view"]]
        self.assertEqual(sorted(c[2] for c in views), ["13", "14", "15"])
        for call in views:
            self.assertEqual(call[3:], ["--json", "state,labels"])
        self.assertEqual(data["w20261020100000"]["waiting"], [{"number": 14, "status": "in-progress"}])
        self.assertEqual(data["w20261020110000"]["state"], "due")
        self.assertEqual(len(self.gh_calls()), 3)
        self.assertEqual(self.read_json(self.cache_path(13))["status"], "in-progress")   # the caches are not written

    def test_wave_drop_makes_the_wave_due(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        p = self.ok("wave", "drop", "w20261020100000", "13")
        self.assertEqual(p.stdout.strip(), "dropped #13 from wave w20261020100000")
        [wave] = self.wave_files()
        self.assertEqual((wave["tickets"], wave["dropped"]), ([12], [13]))
        [row] = self.ok("waves").stdout.splitlines()[1:]
        self.assertIn("due", row)
        self.assertEqual(self.gh_calls(), [])

    def test_wave_drop_refuses_an_unknown_wave_or_ticket_and_a_launched_review(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_wave("w20261020110000", [14, 15],
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        before = self.wave_files()
        self.assertOneLineError(self.work("wave", "drop", "w20261020120000", "12"), "w20261020120000")
        self.assertOneLineError(self.work("wave", "drop", "nonsense", "12"), "nonsense")
        self.assertOneLineError(self.work("wave", "drop", "w20261020100000", "14"), "#14", "w20261020100000")
        self.assertOneLineError(self.work("wave", "drop", "w20261020110000", "14"), "w20261020110000", "launched")
        self.assertEqual(self.wave_files(), before)



if __name__ == "__main__":
    unittest.main()
