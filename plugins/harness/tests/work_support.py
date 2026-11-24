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

import gitrepo_support  # noqa: E402
import fakegh  # noqa: E402  (#447: issue and pull request reads through gh api REST, and gh_view to read writes)
from gitrepo_support import copy_repo, run_in_process  # noqa: E402  (RowsInProcess, CopiedProject)

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-work")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = gitrepo_support.GIT  # the git binary itself, not macOS's xcrun shim (#378)
# The wrappers' shell (#378): dash when there is one, since macOS's /bin/sh re-executes bash.
SH = "/bin/dash" if os.path.exists("/bin/dash") else "/bin/sh"
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

FAKE_CLAUDE = """#!{python} -IS
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
def post_started():
    # A session that starts (#367): its `harness-work start` leaves a "Started on" comment on the ticket. Here the
    # comment is added to the scenario's `issue view N` answer, stamped two seconds ahead so a launch time kept in
    # whole seconds never ties with it. FAKECLAUDE_NO_START=1: a session that exits before it starts.
    import datetime, re
    path = os.environ.get("FAKEGH_SCENARIO")
    found = re.search(r"/harness:work (\\d+)", " ".join(argv))
    if os.environ.get("FAKECLAUDE_NO_START") or not path or not found or not os.path.exists(path):
        return
    number = found.group(1)
    branch = os.path.basename(os.getcwd()).replace("-", "/", 1)
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)
    with open(path, encoding="utf-8") as fh:
        scenario = json.load(fh)
    for call in scenario.get("calls", []):
        target = None
        if call.get("match") == ["issue", "view", number] and isinstance(call.get("stdout_json"), dict):
            target = call["stdout_json"]
        elif str(call.get("issue")) == number and isinstance(call.get("data"), dict):     # fakegh's REST layer
            target = call["data"]
        if target is not None:
            target.setdefault("comments", []).append({{
                "author": {{"login": "rjeskuri"}}, "createdAt": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": "https://github.com/o/r/issues/" + number + "#issuecomment-started",
                "body": "Started on " + branch + " (fakehost, session)\\n\\n<!-- harness -->"}})
    with open(path + ".claude", "w", encoding="utf-8") as fh:
        json.dump(scenario, fh)
    os.replace(path + ".claude", path)
if "--bg" in argv:
    post_started()
    print("backgrounded \\u00b7 " + os.environ.get("FAKECLAUDE_BG_ID", "abc123") + " \\u00b7 " + argv[argv.index("-n") + 1])
    sys.exit(0)
if "-p" in argv:
    post_started()
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
NOISY_BOARD = """#!{sh}
printf '%s\\n' "$*" >> "$NOISY_LOG"
printf '%s\\n' "{before}" >&2
"{{python}}" "{{board}}" "$@"
rc=$?
printf '%s\\n' "{after}" >&2
if [ "$1" = show ] && [ -n "$NOISY_WARN" ]; then
    printf 'harness-board: warning: %s from show %s %s\\n' "$NOISY_WARN" "$2" "$3" >&2
fi
exit $rc
""".format(sh=SH, before=CHATTER[0], after=CHATTER[1])
WARNING = "harness-board: warning: "


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
    """fakebin: gh and claude; toolbin: git (a wrapper) and python3 (a symlink) only, so PATH holds nothing else of
    ours."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-bin-"))
    fakebin, toolbin = os.path.join(root, "fakebin"), os.path.join(root, "toolbin")
    # The Python stand-ins start with -I -S (#378): no site, no user site, no PYTHON* variables.
    executable(os.path.join(fakebin, "gh"), f'#!{SH}\nexec "{sys.executable}" -I -S "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "claude"), FAKE_CLAUDE.format(python=sys.executable))
    for name in ("terminal-notifier", "osascript"):     # harness-board needs-input (a cloud run's blocked marker)
        executable(os.path.join(fakebin, name), f"#!{SH}\nexit 0\n")
    os.makedirs(toolbin)
    # A wrapper, never a link (#378): Apple's git finds its exec path and templates through the path it was started
    # by, so a link to GIT in toolbin would lose them.
    executable(os.path.join(toolbin, "git"), f'#!{SH}\nexec "{GIT}" "$@"\n')
    os.symlink(sys.executable, os.path.join(toolbin, "python3"))
    noisybin = os.path.join(root, "noisybin")       # only on PATH when a test puts it there
    executable(os.path.join(noisybin, "harness-board"), NOISY_BOARD.format(
        python=sys.executable, board=os.path.join(os.path.dirname(TOOL), "harness-board")))
    # One bytecode folder per module (#378): the tools this module runs and the rows run in this process compile
    # harness_lib once, even under PYTHONDONTWRITEBYTECODE=1, and never into the checkout.
    pycache = os.path.join(root, "pycache")
    os.makedirs(pycache)
    BINS.update(root=root, fakebin=fakebin, toolbin=toolbin, noisybin=noisybin, pycache=pycache,
                saved_prefix=sys.pycache_prefix)
    sys.pycache_prefix = pycache


def tearDownModule():
    if "saved_prefix" in BINS:
        sys.pycache_prefix = BINS.pop("saved_prefix")
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
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env["PYTHONPYCACHEPREFIX"] = BINS["pycache"]
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_GH=os.path.join(BINS["fakebin"], "gh"),
                   HARNESS_CLAUDE=os.path.join(BINS["fakebin"], "claude"),
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log, FAKECLAUDE_LOG=self.claude_log,
                   HARNESS_START_WAIT="10")     # #367: seconds a launch waits for its session's "Started on"
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
        calls += [fakegh.issue_entry(i["number"], sub_issues=list(sub_issues)) for i in issues if sub_issues]
        self.put_scenario({"calls": calls})

    def put_scenario(self, data):
        """The fake gh's answers, the gh issue and pr list fixtures moved to its REST layer (#447: harness-work and
        harness-board read and write issues, and harness-work lists a branch's pull requests, through gh api)."""
        data = dict(data, calls=fakegh.as_rest(data.get("calls") or [], pulls=True))
        write(self.scenario_path, json.dumps(data))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def gh_entries(self):
        return fakegh.log_entries(self.gh_log)

    def gh_calls(self):
        """Each gh call's argv, a REST issue write as the gh command it stands for (fakegh.gh_view)."""
        return fakegh.gh_view(self.gh_entries(), self.scenario_path)

    def issue_reads(self, number=12):
        """How many times issue `number` itself was read."""
        return fakegh.issue_gets(self.gh_entries(), number)

    def pr_reads(self, branch, state=None):
        """How many times the pull requests of `branch` were listed (with `state` when given)."""
        return sum(1 for b, s in fakegh.pull_queries(self.gh_entries()) if b == branch and state in (None, s))

    def add_calls(self, *calls):
        """Put `calls` ahead of the scenario's own, gh issue and pr list fixtures moved to the REST layer."""
        data = self.read_json(self.scenario_path)
        data["calls"] = fakegh.as_rest(list(calls), pulls=True) + data["calls"]
        write(self.scenario_path, json.dumps(data))

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
    xhigh; opus at xhigh or above, and an explicit fable, have nothing above them (None: park)."""
    if tier == "sonnet":
        return "opus", None
    if tier == "opus" and effort in (None, "low", "medium", "high"):
        return "opus", "xhigh"
    return None


def escalation_text(tier, effort):
    nxt = escalation_of(tier, effort)
    if nxt is None:
        return "park"
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
# ticket decides: a session whose ticket is status:in-review, status:needs-input, status:blocked (#387) or closed has
# ended, whatever its state; the sweep closes its record and runs `claude stop <id>`. A session whose ticket is
# status:in-progress is never stopped, and a live-state one stays live.
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
        self.put_scenario({"calls": calls})

    def comment_bodies(self, n):
        """The bodies of every comment on n (#447: a REST comment as gh_view reads it, --body or --body-file -)."""
        return [argv[4] for argv in self.gh_calls() if argv[:4] == ["issue", "comment", str(n), "--body"]]

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


# =========================================================================== #164: dispatch --all-projects
# gh calls name no repository (they run in the project's main checkout), so a test with several projects routes them
# by their working directory: FAKEGH_PROJECTS maps each project's main checkout (a real path) to its own scenario and
# gh log, and a gh or claude started in it, or under it (a ticket worktree), gets them as FAKEGH_SCENARIO and
# FAKEGH_LOG. A launched session's "Started on" comment (FAKE_CLAUDE's post_started) so lands in its own project's
# scenario. A call outside every project keeps the test's own FAKEGH_SCENARIO and FAKEGH_LOG.
ROUTED = """#!{python} -IS
import json, os, sys
table = json.loads(os.environ.get("FAKEGH_PROJECTS") or "{{}}")
here = os.path.realpath(os.getcwd())
best = ""
for root in table:
    if (here == root or here.startswith(root + os.sep)) and len(root) > len(best):
        best = root
if best:
    os.environ["FAKEGH_SCENARIO"] = table[best]["scenario"]
    os.environ["FAKEGH_LOG"] = table[best]["log"]
os.execv({python!r}, [{python!r}, "-I", "-S", {target!r}] + sys.argv[1:])
"""
# launchctl for --install-launchd and --uninstall-launchd (as test_digest's): each call one JSON line in LAUNCHCTL_LOG;
# a subcommand named in LAUNCHCTL_FAIL fails with exit 5.
LAUNCHCTL_RECORDER = """#!{python} -IS
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("LAUNCHCTL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(["launchctl"] + args) + "\\n")
if args and args[0] in os.environ.get("LAUNCHCTL_FAIL", "").split(","):
    sys.stderr.write(args[0] + " failed: 5: Input/output error\\n")
    sys.exit(5)
"""
AUTODISPATCH = "HARNESS_AUTODISPATCH"


def routed_bins():
    """routedbin, made once per module beside setUpModule's bins: gh and claude routed by cwd, and launchctl."""
    folder = os.path.join(BINS["root"], "routedbin")
    if not os.path.isdir(folder):
        executable(os.path.join(folder, "gh"), ROUTED.format(python=sys.executable, target=FAKEGH))
        executable(os.path.join(folder, "claude"),
                   ROUTED.format(python=sys.executable, target=os.path.join(BINS["fakebin"], "claude")))
        executable(os.path.join(folder, "launchctl"), LAUNCHCTL_RECORDER.format(python=sys.executable))
    return folder


class ProjectsTest(WorkTest):
    """Several registered projects (~/.claude/harness/projects.json): each its own main checkout and bare origin,
    with the .claude/settings.json of PROJECTS committed and pushed (None: none), built once per class and copied for
    each setUp. Each project has its own gh scenario (`board`) and gh log; `self.repo` is the first project. A test's
    first harness-work run is a process of its own, later ones run in this process (as test_work_dispatch's
    RowsInProcess), except a dispatch that may launch a headless session."""

    PROJECTS = {"alpha-app": {AUTODISPATCH: "1"},
                "beta-app": {AUTODISPATCH: 1},
                "gamma-app": {"CLAUDE_VERIFY_CMD": "make check"},
                "delta-app": {AUTODISPATCH: "1"}}
    ran_a_process = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-projects-"))
        cls.addClassCleanup(shutil.rmtree, root, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_", "FAKECLAUDE_"))}
        env.update(HOME=os.path.join(root, "home"), PATH=os.pathsep.join(["/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")

        def git(repo, *args, **extra):
            p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false", "-c", "maintenance.auto=false",
                                "-c", "gc.auto=0", *args], env=dict(env, **extra), capture_output=True, text=True)
            if p.returncode != 0:
                raise AssertionError(f"template: git {' '.join(args)}: {p.stderr}")

        for name, settings in cls.PROJECTS.items():
            origin, main = os.path.join(root, name + ".git"), os.path.join(root, name)
            for repo, flags in ((origin, ("--bare",)), (main, ())):
                os.makedirs(repo)
                git(repo, "init", "-q", *flags, "-b", "main")
                git(repo, "config", "gc.auto", "0")
                git(repo, "config", "maintenance.auto", "false")
            write(os.path.join(main, "README.md"), f"# {name}\n")
            write(os.path.join(main, ".gitignore"), ".env\n*.log\n")
            write(os.path.join(main, ".worktreeinclude"), "# copied into new worktrees\n.env\n.env.*\n")
            if settings is not None:
                write(os.path.join(main, ".claude", "settings.json"), json.dumps({"env": settings}))
            git(main, "add", "-A")
            git(main, "commit", "-q", "-m", "First", GIT_AUTHOR_DATE=TEN_DAYS_AGO, GIT_COMMITTER_DATE=TEN_DAYS_AGO)
            git(main, "remote", "add", "origin", origin)
            git(main, "push", "-q", "-u", "origin", "main")
        cls.template_root = root

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.claude_log = os.path.join(self.tmp, "claude.log")
        self.launch_log = os.path.join(self.tmp, "launchctl.log")
        self.elsewhere = self.mk("elsewhere")         # in no repository, as launchd's working directory
        root = type(self).template_root
        self.projects = {}
        for name in self.PROJECTS:
            gitrepo_support.copy_repo(os.path.join(root, name + ".git"), os.path.join(self.tmp, name + ".git"))
            main = os.path.join(self.tmp, name)
            gitrepo_support.copy_repo(os.path.join(root, name), main)
            config = os.path.join(main, ".git", "config")
            write(config, read_text(config).replace(os.path.join(root, name + ".git"),
                                                    os.path.join(self.tmp, name + ".git")))
            self.projects[name] = main
        first = next(iter(self.PROJECTS))
        self.repo, self.origin = self.projects[first], os.path.join(self.tmp, first + ".git")
        self.key = sha12(os.path.realpath(self.repo))
        self.scenario()
        for name in self.PROJECTS:
            self.board(name)
        self.register(*self.PROJECTS)

    # ----------------------------------------------------------- the projects
    def register(self, *names):
        """The registry: `names` in that order, each entry named after its folder; a name that is not one of the
        projects is a path where nothing is (a project that was moved away)."""
        paths = [self.projects.get(n) or os.path.join(self.tmp, n) for n in names]
        write(os.path.join(self.home, ".claude", "harness", "projects.json"), json.dumps(
            {"version": 1, "projects": [{"path": p, "name": os.path.basename(p)} for p in paths]}))

    def scenario_of(self, name):
        return os.path.join(self.tmp, name + ".scenario.json")

    def gh_log_of(self, name):
        return os.path.join(self.tmp, name + ".gh.log")

    @staticmethod
    def title(name, n):
        return f"{name} ticket {n}"

    def board(self, name, ready=(), live=(), incidents=(), failing=False):
        """Project `name`'s gh: `ready` tickets status:ready, `live` ones status:in-progress, `incidents` the open
        issues labelled incident; with `failing` every `gh issue list` fails."""
        calls = []
        if failing:
            calls.append({"match": ["issue", "list"], "exit": 1,
                          "stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n"})
        found = [{"number": n, "title": f"Outage {n}", "labels": [{"name": "incident"}]} for n in incidents]
        calls += [{"contains": ["issue", "list", "--label", "incident"], "stdout_json": found},
                  {"contains": ["issue", "list", "--label=incident"], "stdout_json": found}]
        calls += [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, title=self.title(name, n))}
                  for n in ready]
        calls += [{"match": ["issue", "view", str(n)],
                   "stdout_json": issue(n, labels=IN_PROGRESS, title=self.title(name, n))} for n in live]
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": f"o/{name}"}},
            {"match": ["issue", "list"], "stdout_json": [ready_row(n, self.title(name, n)) for n in ready]},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": f"https://github.com/o/{name}/issues/1#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        path = self.scenario_of(name)
        write(path, json.dumps({"calls": fakegh.as_rest(calls, pulls=True)}))    # #447: issues through REST
        if os.path.exists(path + ".state.json"):
            os.unlink(path + ".state.json")

    def commit_settings(self, name, env):
        """Project `name`'s .claude/settings.json with this env, committed and pushed to its origin."""
        main = self.projects[name]
        write(os.path.join(main, ".claude", "settings.json"), json.dumps({"env": env}))
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "Settings")
        self.git(main, "push", "-q", "origin", "main")

    def live_run(self, name, ticket, bg_id, started_at=None):
        """An unended bg run record of project `name`'s ticket (live while `claude agents` lists bg_id live)."""
        main = os.path.realpath(self.projects[name])
        rid = f"{sha12(main)}-{ticket}-20261019070000"
        record = {"id": rid, "ticket": ticket, "repo": main,
                  "worktree": os.path.join(main, ".claude", "worktrees", f"t-{ticket}-x"), "branch": f"t/{ticket}-x",
                  "venue": "bg", "bg_id": bg_id, "log": os.path.join(self.runs_dir(), rid + ".log"),
                  "started_at": started_at or iso_ago(hours=1), "ended_at": None, "exit": None, "outcome": None}
        write(os.path.join(self.runs_dir(), rid + ".json"), json.dumps(record))
        return record

    @staticmethod
    def agents(states):
        """`claude agents --json` listing {bg_id: state}."""
        return json.dumps([{"id": bg_id, "kind": "background", "state": state} for bg_id, state in states.items()])

    def env(self, **extra):
        bins = routed_bins()
        env = super().env()
        env.update(PATH=os.pathsep.join([bins, env["PATH"]]), HARNESS_GH=os.path.join(bins, "gh"),
                   HARNESS_CLAUDE=os.path.join(bins, "claude"), HARNESS_LAUNCHCTL=os.path.join(bins, "launchctl"),
                   LAUNCHCTL_LOG=self.launch_log,
                   FAKEGH_PROJECTS=json.dumps({os.path.realpath(main): {"scenario": self.scenario_of(name),
                                                                         "log": self.gh_log_of(name)}
                                               for name, main in self.projects.items()}))
        env.update(extra)
        return env

    def work(self, *args, env=None, cwd=None):
        headless = (tuple(args[:1]) == ("dispatch",) and "--dry-run" not in args
                    and any(a == "--api" or "headless" in a for a in args))
        if not self.ran_a_process or headless:
            self.ran_a_process = True
            return super().work(*args, env=env, cwd=cwd)
        p = gitrepo_support.run_in_process(TOOL, args=args, cwd=cwd or self.repo, env=env or self.env())
        self.assertNotIn("Traceback", p.stderr)
        return p

    # ----------------------------------------------------------- what happened
    def project_of(self, path):
        real = os.path.realpath(path)
        for name, main in self.projects.items():
            main = os.path.realpath(main)
            if real == main or real.startswith(main + os.sep):
                return name
        return None

    def launches(self):
        """(project, ticket) of every session launched (`claude --bg` or `claude -p`), in order."""
        out = []
        for call in self.claude_calls():
            if "--bg" in call["argv"] or "-p" in call["argv"]:
                found = re.search(r"/harness:work (\d+)", " ".join(call["argv"]))
                out.append((self.project_of(call["cwd"]), int(found.group(1)) if found else None))
        return out

    def project_gh_calls(self, name):
        """Project `name`'s gh calls, a REST issue write as the gh command it stands for (fakegh.gh_view)."""
        return fakegh.gh_view(fakegh.log_entries(self.gh_log_of(name)), self.scenario_of(name))

    def started_in(self, name):
        """What starting a ticket leaves in project `name`: a ticket worktree, or a gh issue edit or comment."""
        worktrees = os.path.join(self.projects[name], ".claude", "worktrees")
        found = [f"worktree {w}" for w in (os.listdir(worktrees) if os.path.isdir(worktrees) else [])]
        found += [" ".join(a[:3]) for a in self.project_gh_calls(name) if a[:2] in (["issue", "edit"],
                                                                                   ["issue", "comment"])]
        return found

    def runs_snapshot(self):
        """{file name: bytes} of the runs directory."""
        folder = self.runs_dir()
        out = {}
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), "rb") as fh:
                out[name] = fh.read()
        return out


# --------------------------------------------------------------- #164: a bg session's transcript at the usage limit
# The refusal Claude Code 2.1.310 wrote on 2026-10-24 when a bg session hit the plan's limit (verbatim, trimmed):
# one synthetic assistant line on the main thread; 1 to 8 non-assistant lines always followed it.
USAGE_LIMIT_SESSION = "106605e7-7ead-4edf-bd57-8a9e15b9a29d"
SESSION_LIMIT_TEXT = "You've hit your session limit \u00b7 resets 8:40pm (Asia/Tokyo)"


def usage_limit_refusal(timestamp="2026-10-24T10:11:06.486Z", resets_at=1792842000, quota_resets_at=None,
                        text=SESSION_LIMIT_TEXT, sidechain=False):
    """The refusal entry. `resets_at` goes to apiErrorParams.rate_limit_info.resetsAt, `quota_resets_at` (default:
    the same) to quotaLimits.resetsAt; None leaves that block out."""
    entry = {"type": "assistant", "isSidechain": sidechain, "timestamp": timestamp,
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
             "apiError": "usage_limit_reached"}
    quota = resets_at if quota_resets_at is None else quota_resets_at
    if resets_at is not None:
        entry["apiErrorParams"] = {"rate_limit_info": {
            "status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour", "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled", "isUsingOverage": False}}
    if quota is not None:
        entry["quotaLimits"] = {"status": "rejected", "resetsAt": quota, "rateLimitType": "five_hour"}
    entry.update({"error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429, "sessionKind": "bg",
                  "version": "2.1.310"})
    return entry


def said(text, when, sidechain=False):
    """An ordinary assistant reply."""
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": when,
            "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}}


def prompted(text, when, **fields):
    return dict({"type": "user", "isSidechain": False, "timestamp": when,
                 "message": {"role": "user", "content": text}}, **fields)


def after_turn(session, when):
    """The non-assistant lines that follow a turn's end in a real transcript."""
    return [{"type": "system", "subtype": "turn_duration", "durationMs": 48211, "isSidechain": False,
             "timestamp": when},
            {"type": "last-prompt", "lastPrompt": "/harness:work 40", "sessionId": session},
            {"type": "custom-title", "customTitle": "alpha-app #40 alpha-app ticket 40", "sessionId": session}]


def stamp(text, **delta):
    """A transcript timestamp (`...T10:11:06.486Z`) moved by `delta`, in the same form."""
    when = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ") + dt.timedelta(**delta)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def limited_transcript(session, refusal):
    """A bg session's main transcript that ends on `refusal`: the prompt, a reply and a subagent's line before it,
    then the lines that follow a turn; every time taken from the refusal's."""
    at = refusal["timestamp"]
    return ([prompted("/harness:work 40", stamp(at, minutes=-13)),
             said("Starting ticket #40: reading the ticket.", stamp(at, minutes=-12)),
             said("Tests written.", stamp(at, minutes=-6), sidechain=True),
             refusal] + after_turn(session, stamp(at, milliseconds=120)))


def transcript_start(lines):
    """A run's start a minute before its transcript's first line."""
    first = min(e["timestamp"] for e in lines if e.get("timestamp"))
    return (dt.datetime.strptime(first[:19], "%Y-%m-%dT%H:%M:%S") - dt.timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def epoch_iso(seconds):
    """A Unix time as the budgets' UTC stamp, from first principles."""
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===================================================================== the rows of a test run in this process (#378)
class RowsInProcess:
    """A test's first `harness-work` run is a process of its own, as WorkTest.work runs it: the exit code and the
    output of the process contract. Every later run, the rows of its table and the runs that only set up a later one,
    runs the tool in this process (gitrepo_support.run_in_process) with the same argv, cwd and environment, so a row
    pays for neither a Python start nor a compile of the tool; a call of setUp inside a test (a fresh project) does not
    make the next run a process again. What the tool runs (git, gh, harness-board, the fake claude) is still its own
    process either way, and a run that starts a headless session is always a process of its own: the `claude -p` it
    launches (start_new_session) outlives the run and must not be left a child of this process."""

    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    @staticmethod
    def starts_a_headless_session(args):
        """`dispatch --venue headless` or `dispatch --api`, not a dry run: it may launch `claude -p` in the background."""
        return (tuple(args[:1]) == ("dispatch",) and "--dry-run" not in args
                and any(a == "--api" or "headless" in a for a in args))

    def work(self, *args, env=None, cwd=None):
        if not self.ran_a_process or self.starts_a_headless_session(args):
            self.ran_a_process = True
            return super().work(*args, env=env, cwd=cwd)
        p = run_in_process(TOOL, args=args, cwd=cwd or self.repo, env=env or self.env())
        self.assertNotIn("Traceback", p.stderr)
        return p


# ===================================================================== the repositories, built once per class (#378)
class CopiedProject:
    """WorkTest.setUp (work_support) with its repositories built once per test class and copied for each setUp: the
    bare origin and the main checkout `proj` (README.md, a .gitignore of .env and *.log, a .worktreeinclude naming
    .env and .env.*, all committed as "First" ten days ago) pushed to it. A class whose every setUp makes more
    repositories adds them in `extend_template`, run once on the template; every folder in the template is copied.

    The template is built with git's background gc and maintenance off (on the command line and in each repository's
    config, so its copies carry it too): nothing changes under a copy while it is taken (#365). A copy's origin remote
    is rewritten to name the copied origin, as `git remote set-url` would. The rest is WorkTest.setUp's own: a fresh
    folder, home, logs and scenario, and the key of the copy's path, on every call (tests call setUp again for a
    fresh project)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-template-"))
        cls.addClassCleanup(shutil.rmtree, root, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_", "FAKECLAUDE_"))}
        env.update(HOME=os.path.join(root, "home"), PATH=os.pathsep.join(["/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")

        def git(repo, *args, **extra):
            p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false", "-c", "maintenance.auto=false",
                                "-c", "gc.auto=0", *args], env=dict(env, **extra), capture_output=True, text=True)
            if p.returncode != 0:
                raise AssertionError(f"template: git {' '.join(args)}: {p.stderr}")
            return p.stdout.strip()

        def init(repo, *flags):
            os.makedirs(repo)
            git(repo, "init", "-q", *flags, "-b", "main")
            git(repo, "config", "gc.auto", "0")
            git(repo, "config", "maintenance.auto", "false")

        origin, repo = os.path.join(root, "origin.git"), os.path.join(root, "proj")
        init(origin, "--bare")
        init(repo)
        write(os.path.join(repo, "README.md"), "# proj\n")
        write(os.path.join(repo, ".gitignore"), ".env\n*.log\n")
        write(os.path.join(repo, ".worktreeinclude"), "# copied into new worktrees\n.env\n.env.*\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "First", GIT_AUTHOR_DATE=TEN_DAYS_AGO, GIT_COMMITTER_DATE=TEN_DAYS_AGO)
        git(repo, "remote", "add", "origin", origin)
        git(repo, "push", "-q", "-u", "origin", "main")
        cls.extend_template(root, git, init)
        cls.template_root = root

    @classmethod
    def extend_template(cls, root, git, init):
        """More repositories every setUp of the class starts with, made in `root`; `git(repo, *args)` runs git on
        the template and `init(repo)` makes a repository there as the template's own are made."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.claude_log = os.path.join(self.tmp, "claude.log")
        root = type(self).template_root
        for name in sorted(os.listdir(root)):
            if name != "home":
                copy_repo(os.path.join(root, name), os.path.join(self.tmp, name))
        self.origin, self.repo = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "proj")
        config = os.path.join(self.repo, ".git", "config")
        with open(config, encoding="utf-8") as fh:
            text = fh.read()
        write(config, text.replace(os.path.join(root, "origin.git"), self.origin))
        self.key = hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12]
        self.scenario()


# Everything above, underscored names included, for `from work_support import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
