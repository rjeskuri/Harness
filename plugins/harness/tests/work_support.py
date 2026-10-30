"""Shared by test_work, test_work_dispatch, test_work_runs, test_work_sessions: the fixtures, helpers and base classes of the
test_work suite, split by test class so no one module bounds the parallel suite. Not a test
module (no `test_` prefix): discovery and scripts/run_suite.py skip it. Each test module
imports everything here, `setUpModule` and `tearDownModule` included, so each still builds its
own fixtures once per process.
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



IN_PROGRESS = ("task", "status:in-progress", "size:S", "risk:low")


def sha12(path):
    """A path string's key, from first principles: the first 12 hex digits of its sha1."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


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


# =========================================================================== cloud: skill and docs (#58)


# =========================================================================== waves (#180)
WAVE_ID_RE = r"^w\d{14}(?:-\d+)?$"


# Everything above, underscored names included, for `from work_support import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
