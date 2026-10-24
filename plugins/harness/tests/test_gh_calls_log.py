"""Tests for the gh call log (ticket #187): every gh call a harness tool or hook makes is one JSON line in
~/.claude/harness/gh/calls-<UTC date>.jsonl.

The contract these tests fix:
- harness_lib/ghlimit.py: gh_dir() (harness_home("gh"), read at call time), call_kind(args), call_name(args),
  ticket_of(cwd), repo_of(cwd), log_call(args, *, cwd, exit, ms, waited_ms=0, refused=False), read_calls(since).
- A line is one JSON object with exactly the fields at, tool, cmd, kind, repo, ticket, exit, ms, waited_ms and
  refused: at is text.now_iso() (UTC, to the second), tool the basename of sys.argv[0], cmd call_name(args),
  kind call_kind(args), repo repo_of(cwd), ticket ticket_of(cwd), exit gh's exit code (null for a call that timed
  out or was stopped), ms an int, waited_ms 0 and refused false in this task.
- harness_lib.gh.gh_run logs each call when it returns or times out; GhBatch logs each call it reads and each call
  close() had to stop (exit null); session_brief.py's run_side_by_side logs its calls (tool session_brief.py).
- repo_of: the folder name of the main checkout (the part of cwd before /.claude/worktrees/), else cwd's basename.
  ticket_of: N from /.claude/worktrees/t-N- in PWD first, then in cwd; else null.
- Day files more than 14 days old (by the date in their name) go when a new day's file is created.
- A log that cannot be written is skipped silently: stdout, stderr, exit code and gh argv stay the same.

Tools run as subprocesses with a temporary HOME, HARNESS_GH pointing at a wrapper around fakegh.py (it sleeps
first when its argv holds a token named in GHTEST_SLOW), FAKEGH_SCENARIO/FAKEGH_LOG, and no PWD unless a test
sets one (the runner's own PWD may be a ticket worktree). The in-process tests patch HOME, PWD and sys.argv.
Expected values are written out here, never computed by the code under test. The fake executables and the git
repositories are made once for the module. No test sleeps; the deadline tests wait well under 2 s.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_gh_calls_log.py" -v
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pwd
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tools must not leave __pycache__ in the checkout

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
BIN = os.path.join(PLUGIN, "bin")
LIB = os.path.join(PLUGIN, "lib")
FAKEGH = os.path.join(TESTS, "fakegh.py")
BOARD = os.path.join(BIN, "harness-board")
PR = os.path.join(BIN, "harness-pr")
WORK = os.path.join(BIN, "harness-work")
STANDUP = os.path.join(BIN, "harness-standup")
DIGEST = os.path.join(BIN, "harness-digest")
SESSION_BRIEF = os.path.join(PLUGIN, "hooks", "session_brief.py")
GIT = shutil.which("git")
UTC = dt.timezone.utc

FIELDS = {"at", "tool", "cmd", "kind", "repo", "ticket", "exit", "ms", "waited_ms", "refused"}
AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
DAY_FILE = re.compile(r"^calls-(\d{4}-\d\d-\d\d)\.jsonl$")
MAIN_NAME, BETA_NAME = "acme-widget", "beta-tool"     # the main checkouts' folder names: the expected `repo`

# The fake gh: sleeps first when its argv holds one of the comma-separated GHTEST_SLOW tokens (`token` sleeps
# GHTEST_SLEEP seconds, `token=S` S seconds), then becomes fakegh.py (which logs to FAKEGH_LOG and answers from
# FAKEGH_SCENARIO).
WRAPPER = '''#!{python}
import os, sys, time
for item in os.environ.get("GHTEST_SLOW", "").split(","):
    token, _, seconds = item.partition("=")
    if token and token in sys.argv[1:]:
        time.sleep(float(seconds or os.environ.get("GHTEST_SLEEP", "30")))
        break
os.execv({python!r}, [{python!r}, {fakegh!r}] + sys.argv[1:])
'''
CLAUDE = '#!/bin/sh\n[ "$1 $2" = "agents --json" ] || exit 64\necho "[]"\n'

TASK_BODY = """## Request
Add the widget.

## Definition of done
- [ ] The widget frobs

## Tests that must pass
- `tests/test_widget.py::test_frob`: frobs
"""
EPIC_BODY = "## Goal\nShip the widget family.\n\n## Tasks\n- #12\n"

W: dict = {}


# ---------------------------------------------------------------- fixtures
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_", "CLAUDE_"))}
    env.update(HOME=W["githome"], GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    return env


def git(repo, *args):
    p = subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                       env=git_env(), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {p.stderr}")
    return p.stdout.strip()


def new_repo(parent, name, origin):
    path = os.path.join(parent, name)
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    write(os.path.join(path, "README.md"), f"# {name}\n")
    write(os.path.join(path, ".git", "info", "exclude"), ".claude/worktrees/\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "First")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def add_worktree(main, branch, folder=None):
    path = os.path.join(main, ".claude", "worktrees", folder or branch.replace("/", "-"))
    git(main, "worktree", "add", "-q", "-b", branch, path)
    return path


def setUpModule():
    if GIT is None:
        raise unittest.SkipTest("git not found")
    root = os.path.realpath(tempfile.mkdtemp(prefix="test-gh-calls-log-"))
    W["root"] = root
    try:
        W["githome"] = os.path.join(root, "githome")
        os.makedirs(W["githome"])
        W["fakebin"] = fakebin = os.path.join(root, "fakebin")
        W["gh"] = executable(os.path.join(fakebin, "gh"), WRAPPER.format(python=sys.executable, fakegh=FAKEGH))
        executable(os.path.join(fakebin, "claude"), CLAUDE)
        os.symlink(sys.executable, os.path.join(fakebin, "python3"))
        # macOS checks a new executable on its first run (about half a second): pay that here, not in a deadline test
        subprocess.run([W["gh"], "warm-up"], capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
        W["main"] = new_repo(root, MAIN_NAME, "https://github.com/acme/widget.git")
        W["wt12"] = add_worktree(W["main"], "t/12-add-the-widget")
        W["wt40"] = add_worktree(W["main"], "t/40-other-work")
        W["beta"] = new_repo(root, BETA_NAME, "git@github.com:acme/beta-tool.git")
        # a ticket worktree folder on a branch that is not a ticket branch: the session brief shows the board there
        W["beta77"] = add_worktree(W["beta"], "spike", "t-77-spike")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    if LIB not in sys.path:
        sys.path.insert(0, LIB)


def tearDownModule():
    shutil.rmtree(W.get("root", ""), ignore_errors=True)


def lib(name):
    """harness_lib.<name>, imported from this checkout's lib/ (the tools load the same one)."""
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    return importlib.import_module(f"harness_lib.{name}")


def load_tool(path, name):
    from importlib.machinery import SourceFileLoader
    import importlib.util
    loader = SourceFileLoader(name, path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def today():
    return dt.datetime.now(UTC).date()


def day(k):
    """The UTC date k days before today, as YYYY-MM-DD."""
    return (today() - dt.timedelta(days=k)).isoformat()


def issue(number, labels=("task", "status:ready", "size:S", "risk:low"), parent=None, body=TASK_BODY):
    return {"number": number, "title": f"Ticket {number}", "url": f"https://github.com/acme/widget/issues/{number}",
            "body": body, "labels": [{"name": n} for n in labels], "state": "OPEN", "parent": parent,
            "blockedBy": [], "comments": []}


def check_run(name, conclusion="SUCCESS", url=""):
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion,
            "detailsUrl": url, "workflowName": "CI"}


def real_home():
    """The account's real home folder, whatever HOME says."""
    return pwd.getpwuid(os.getuid()).pw_dir


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="case-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.new_home()
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.fakegh_log = os.path.join(self.tmp, "fakegh.log")
        self.scenario([], default={"stdout": "[]"})

    # ---------------------------------------------------------- helpers
    def new_home(self):
        return tempfile.mkdtemp(prefix="home-", dir=self.tmp)

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.fakegh_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def env(self, home=None, **extra):
        """A clean environment: the temporary HOME, the fake gh, no PWD (unless given), nothing of the caller's
        harness, git, claude or gh settings."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "FAKEGH_", "GHTEST_", "GIT_", "CLAUDE_", "GH_"))
               and k not in ("PWD", "OLDPWD")}
        path = [W["fakebin"]] + ([os.path.dirname(GIT)] if os.path.dirname(GIT) not in ("/usr/bin", "/bin") else [])
        env.update(HOME=home or self.home, PATH=os.pathsep.join(path + ["/usr/bin", "/bin"]), HARNESS_GH=W["gh"],
                   GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=W["root"],
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_POLL_SECONDS="0.01", PYTHONDONTWRITEBYTECODE="1",
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.fakegh_log)
        env.update(extra)
        return env

    def run_tool(self, tool, *args, cwd, home=None, **extra):
        p = subprocess.run([sys.executable, tool] + list(args), cwd=cwd, env=self.env(home, **extra),
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def in_process(self, tool_path, home=None, pwd_dir=None, **extra):
        """Patch the environment (temporary HOME, the fake gh, PWD only when given) and sys.argv[0] for the rest of
        the test, as a tool at `tool_path` would run."""
        env = self.env(home, **extra)
        if pwd_dir is not None:
            env["PWD"] = pwd_dir
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(sys, "argv", [tool_path])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.assert_real_home_has_none_of_ours)

    def gh_dir(self, home=None):
        return os.path.join(home or self.home, ".claude", "harness", "gh")

    def logged(self, home=None):
        """Every line of every day file under `home`, oldest file first. Each line is a JSON object whose `at`
        date is its file's date."""
        folder = self.gh_dir(home)
        if not os.path.isdir(folder):
            return []
        out = []
        for name in sorted(os.listdir(folder)):
            m = DAY_FILE.match(name)
            if not m:
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    line = json.loads(raw)
                    self.assertIsInstance(line, dict, raw)
                    self.assertEqual(str(line.get("at", ""))[:10], m.group(1), f"{name}: {raw}")
                    out.append(line)
        return out

    def fakegh_argv(self):
        if not os.path.exists(self.fakegh_log):
            return []
        with open(self.fakegh_log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def assertLine(self, line, started=None, ended=None, **expected):
        """One log line: exactly the ten fields, `at` a UTC second (inside [started, ended] when given), ms an int,
        waited_ms 0 and refused false unless expected says otherwise, and the `expected` values."""
        self.assertEqual(set(line), FIELDS, line)
        self.assertRegex(line["at"], AT)
        if started is not None:
            at = dt.datetime.strptime(line["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            self.assertGreaterEqual(at, started.replace(microsecond=0) - dt.timedelta(seconds=1), line)
            self.assertLessEqual(at, ended + dt.timedelta(seconds=1), line)
        self.assertIsInstance(line["ms"], int, line)
        self.assertNotIsInstance(line["ms"], bool, line)
        self.assertGreaterEqual(line["ms"], 0, line)
        self.assertEqual(line["waited_ms"], expected.pop("waited_ms", 0), line)
        self.assertIs(line["refused"], expected.pop("refused", False), line)
        for key, value in expected.items():
            if value is None or isinstance(value, bool):
                self.assertIs(line[key], value, f"{key}: {line}")
            else:
                self.assertEqual(line[key], value, f"{key}: {line}")

    def assert_real_home_has_none_of_ours(self):
        """No line naming this module's checkouts reached the real ~/.claude/harness/gh."""
        folder = os.path.join(real_home(), ".claude", "harness", "gh")
        ours = {MAIN_NAME, BETA_NAME, "shop-front"}
        for name in (f"calls-{day(1)}.jsonl", f"calls-{day(0)}.jsonl"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as fh:
                    leaked = [raw for raw in fh if any(f'"repo": "{o}"' in raw or f'"repo":"{o}"' in raw for o in ours)]
            except OSError:
                continue
            self.assertEqual(leaked, [], f"a test wrote into the real {folder}/{name}")

    def window(self):
        return dt.datetime.now(UTC)


# ==================================================================== the tools log every gh call
@unittest.skipIf(GIT is None, "git not found")
class ToolCallsAreLogged(Case):
    def test_each_gh_call_of_a_board_command_is_one_line(self):
        """`harness-board show N --json`: one line per gh call (as many as fakegh saw), in call order, each with
        tool harness-board, its own cmd and kind, exit 0, the repo and no ticket from the main checkout."""
        parent = {"number": 3, "title": "Widgets"}     # no body: show fetches the parent's goal (a second call)
        cases = [
            ("a task", "12", [{"match": ["issue", "view", "12"], "stdout_json": issue(12)}],
             [("issue view", "graphql")]),
            ("a task with a parent", "12",
             [{"match": ["issue", "view", "12"], "stdout_json": issue(12, parent=parent)},
              {"match": ["issue", "view", "3"], "stdout_json": {"number": 3, "title": "Widgets", "body": EPIC_BODY}}],
             [("issue view", "graphql"), ("issue view", "graphql")]),
            ("an epic", "3",
             [{"match": ["issue", "view", "3"],
               "stdout_json": issue(3, labels=("epic", "status:ready"), body=EPIC_BODY)},
              {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "acme/widget"}},
              {"match": ["api"], "stdout_json": [{"number": 12, "title": "Ticket 12", "state": "OPEN", "labels": []}]}],
             [("issue view", "graphql"), ("repo view", "graphql"),
              ("api repos/:owner/:repo/issues/:n/sub_issues", "rest")]),
        ]
        for label, number, calls, expected in cases:
            with self.subTest(label):
                home = self.new_home()
                self.scenario(calls)
                started = self.window()
                p = self.run_tool(BOARD, "show", number, "--json", cwd=W["main"], home=home)
                ended = self.window()
                self.assertEqual(p.returncode, 0, p.stderr)
                lines = self.logged(home)
                self.assertEqual(len(lines), len(self.fakegh_argv()), lines)
                self.assertEqual([(x["cmd"], x["kind"]) for x in lines], expected)
                for line in lines:
                    self.assertLine(line, started, ended, tool="harness-board", exit=0, repo=MAIN_NAME, ticket=None)
                # a second run appends: twice the lines, the first ones unchanged
                self.run_tool(BOARD, "show", number, "--json", cwd=W["main"], home=home)
                again = self.logged(home)
                self.assertEqual(len(again), 2 * len(expected))
                self.assertEqual(again[:len(lines)], lines)

    def test_pr_and_work_calls_carry_their_tool(self):
        """harness-pr checks 34 (a pr view and a run view for the failed check) and harness-work worktrees (a pr
        list per ticket worktree) each log their own name as the tool."""
        failing = [check_run("test", "FAILURE", "https://github.com/acme/widget/actions/runs/987/job/654"),
                   check_run("lint")]
        self.scenario([{"match": ["pr", "view", "34"], "stdout_json": {"statusCheckRollup": failing}},
                       {"match": ["run", "view", "987"], "stdout": "boom\n"},
                       {"match": ["pr", "list"], "stdout_json": []}])
        started = self.window()
        p = self.run_tool(PR, "checks", "34", cwd=W["main"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)    # checks failed: the tool's own exit, untouched
        p = self.run_tool(WORK, "worktrees", cwd=W["main"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ended = self.window()
        argv = self.fakegh_argv()
        lines = self.logged()
        self.assertEqual(len(lines), len(argv), lines)
        self.assertEqual([(x["tool"], x["cmd"], x["kind"]) for x in lines],
                         [("harness-pr", "pr view", "graphql"), ("harness-pr", "run view", "rest"),
                          ("harness-work", "pr list", "graphql"), ("harness-work", "pr list", "graphql")])
        for line in lines:
            self.assertLine(line, started, ended, exit=0, repo=MAIN_NAME, ticket=None)

    def test_board_calls_run_by_harness_pr_are_logged_as_harness_board(self):
        """harness-pr open runs the real harness-board (show, then comment): those calls carry tool harness-board,
        harness-pr's own calls harness-pr. With PWD in the ticket worktree every line has the ticket, also the
        board's calls, which run in the main checkout."""
        origin = os.path.join(self.tmp, "origin.git")
        os.makedirs(origin)
        git(origin, "init", "-q", "--bare", "-b", "main")
        main = new_repo(self.tmp, "shop-front", None)
        git(main, "remote", "add", "origin", origin)
        git(main, "push", "-q", "-u", "origin", "main")
        wt = add_worktree(main, "t/12-add-the-widget")
        write(os.path.join(wt, "widget.py"), "def frob():\n    return 2\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "Add the widget")
        url = "https://github.com/acme/shop-front/pull/34"
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)},
                       {"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": url + "\n"},
                       {"match": ["issue", "comment", "12"],
                        "stdout": "https://github.com/acme/shop-front/issues/12#c1\n"}])
        started = self.window()
        p = self.run_tool(PR, "open", "12", "--json", cwd=wt, PWD=wt)
        ended = self.window()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout), {"number": 34, "url": url})
        argv = self.fakegh_argv()
        lines = self.logged()
        self.assertEqual(len(lines), len(argv), lines)
        self.assertEqual(sorted(x["cmd"] for x in lines), sorted(" ".join(a[:2]) for a in argv))
        for line in lines:
            tool = "harness-board" if line["cmd"].startswith("issue ") else "harness-pr"
            self.assertLine(line, started, ended, tool=tool, kind="graphql", exit=0, repo="shop-front", ticket=12)
        self.assertEqual(sorted(x["cmd"] for x in lines if x["tool"] == "harness-board"),
                         ["issue comment", "issue view"])
        self.assertEqual(sorted(x["cmd"] for x in lines if x["tool"] == "harness-pr"), ["pr create", "pr list"])

    def test_standup_and_digest_batch_calls_are_logged(self):
        """harness-standup and harness-digest: one line per batch call for each registered project; a batch that
        runs out of time logs the calls it stopped with exit null."""
        registry = {"version": 1, "projects": [
            {"path": p, "name": n, "billable": False, "client": "", "notes": ""}
            for p, n in ((W["main"], MAIN_NAME), (W["beta"], BETA_NAME))]}
        write(os.path.join(self.home, ".claude", "harness", "projects.json"), json.dumps(registry))
        started = self.window()
        p = self.run_tool(STANDUP, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        standup_argv = self.fakegh_argv()
        p = self.run_tool(DIGEST, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ended = self.window()
        argv = self.fakegh_argv()                      # both tools' calls (the log is kept between the runs)
        self.assertEqual(len(standup_argv), 8, standup_argv)
        self.assertEqual(len(argv), 16, argv)
        lines = self.logged()
        self.assertEqual(len(lines), 16, lines)
        for tool, part in (("harness-standup", argv[:8]), ("harness-digest", argv[8:])):
            mine = [x for x in lines if x["tool"] == tool]
            with self.subTest(tool=tool):
                self.assertEqual(sorted(x["cmd"] for x in mine), sorted(" ".join(a[:2]) for a in part))
                self.assertEqual(sorted(x["repo"] for x in mine), sorted([MAIN_NAME] * 4 + [BETA_NAME] * 4))
                for line in mine:
                    self.assertLine(line, started, ended, kind="graphql", exit=0, ticket=None)

        # the deadline: harness-digest's batch for one project, its last two calls still running when the time is up
        home = self.new_home()
        self.in_process("/opt/harness/bin/harness-digest", home=home,
                        GHTEST_SLOW="status:in-progress,headRefName", GHTEST_SLEEP="30")
        digest = load_tool(DIGEST, "harness_digest_gh_calls_log")
        digest.GH_BUDGET = 1.0
        out = digest.project_digest({"name": "w", "path": W["wt40"]}, W["gh"], time.time())
        self.assertIn("timed out", out["error"] or "", out)
        lines = self.logged(home)
        self.assertEqual(len(lines), 4, lines)
        for line in lines:
            self.assertLine(line, tool="harness-digest", kind="graphql", repo=MAIN_NAME, ticket=40)
        self.assertEqual(sorted((x["cmd"], x["exit"] is None) for x in lines),
                         [("issue list", False), ("issue list", True), ("pr list", False), ("pr list", True)])
        self.assertEqual(sorted(x["exit"] for x in lines if x["exit"] is not None), [0, 0])
        for line in lines:
            if line["exit"] is None:
                self.assertGreaterEqual(line["ms"], 500, line)   # it ran until the 1 s deadline stopped it

    def test_session_brief_board_calls_are_logged(self):
        """The session brief's two board calls (made off a ticket branch) are two lines with tool
        session_brief.py: from the main checkout (no ticket, or PWD's), and from a worktree folder t-77-... of
        another project (ticket from the cwd)."""
        self.scenario([{"match": ["issue", "list"], "stdout_json": [{"labels": [{"name": "status:ready"}]}]},
                       {"match": ["pr", "list"], "stdout_json": []}])
        for cwd, pwd_value, repo, ticket in ((W["main"], None, MAIN_NAME, None), (W["main"], W["wt40"], MAIN_NAME, 40),
                                             (W["beta77"], None, BETA_NAME, 77)):
            with self.subTest(cwd=os.path.basename(cwd), ticket=ticket):
                home = self.new_home()
                payload = {"session_id": "sess-1", "transcript_path": "/dev/null", "cwd": cwd,
                           "permission_mode": "default", "hook_event_name": "SessionStart", "source": "startup"}
                extra = {"PWD": pwd_value} if pwd_value is not None else {}
                started = self.window()
                p = subprocess.run([sys.executable, SESSION_BRIEF], input=json.dumps(payload), capture_output=True,
                                   text=True, env=self.env(home, **extra), cwd=self.tmp, timeout=60)
                ended = self.window()
                self.assertEqual((p.returncode, p.stderr), (0, ""))
                self.assertIn("Board: 1 ready", p.stdout)
                lines = self.logged(home)
                self.assertEqual(len(lines), 2, lines)
                self.assertEqual(sorted(x["cmd"] for x in lines), ["issue list", "pr list"])
                for line in lines:
                    self.assertLine(line, started, ended, tool="session_brief.py", kind="graphql", exit=0,
                                    repo=repo, ticket=ticket)


# ==================================================================== the fields of a line
@unittest.skipIf(GIT is None, "git not found")
class LineFields(Case):
    def test_failed_call_logs_its_exit_and_a_timeout_logs_null(self):
        # a failed call through a tool: gh's own exit code, whatever it is; the tool fails as before
        for code in (1, 4, 42):
            with self.subTest(code=code):
                home = self.new_home()
                self.scenario([{"match": ["issue", "view", "12"], "stderr": "HTTP 502: Bad Gateway\n", "exit": code}])
                p = self.run_tool(BOARD, "show", "12", "--json", cwd=W["main"], home=home)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("HTTP 502", p.stderr)
                lines = self.logged(home)
                self.assertEqual(len(lines), 1, lines)
                self.assertLine(lines[0], tool="harness-board", cmd="issue view", kind="graphql", exit=code)
        # harness-pr: the failed run log fetch is exit 3, the view before it exit 0
        home = self.new_home()
        failing = [check_run("test", "FAILURE", "https://github.com/acme/widget/actions/runs/555/job/1")]
        self.scenario([{"match": ["pr", "view", "34"], "stdout_json": {"statusCheckRollup": failing}},
                       {"match": ["run", "view", "555"], "stderr": "no logs\n", "exit": 3}])
        p = self.run_tool(PR, "checks", "34", cwd=W["main"], home=home)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([(x["cmd"], x["exit"]) for x in self.logged(home)], [("pr view", 0), ("run view", 3)])

        # in process: gh_run returning a non-zero exit logs it; one that times out logs exit null and the time spent
        gh = lib("gh")
        cli = lib("cli")
        home = self.new_home()
        self.in_process("/opt/harness/bin/harness-pr", home=home, GHTEST_SLOW="SLOWCALL", GHTEST_SLEEP="30")
        self.scenario([{"match": ["label", "list"], "stdout": "x\n", "exit": 5}], default={"stdout": "{}"})
        done = gh.gh_run(["label", "list"], W["main"])
        self.assertEqual(done.returncode, 5)
        with self.assertRaises(cli.Problem):
            gh.gh_run(["api", "repos/acme/widget/pulls/7/files?per_page=100", "SLOWCALL"], W["main"], timeout=0.5)
        lines = self.logged(home)
        self.assertEqual(len(lines), 2, lines)
        self.assertLine(lines[0], tool="harness-pr", cmd="label list", kind="rest", exit=5, repo=MAIN_NAME, ticket=None)
        self.assertLine(lines[1], tool="harness-pr", cmd="api repos/:owner/:repo/pulls/:n/files", kind="rest",
                        exit=None, repo=MAIN_NAME, ticket=None)
        self.assertGreaterEqual(lines[1]["ms"], 400, lines[1])

        # GhBatch: the call results() reads with a non-zero exit logs that exit
        self.scenario([{"match": ["workflow", "list"], "stderr": "denied\n", "exit": 2}], default={"stdout": "[]"})
        batch = gh.GhBatch(W["gh"], [["pr", "list"], ["issue", "list"], ["workflow", "list"]], W["wt12"],
                           time.monotonic() + 30, budget_seconds=6)
        try:
            with self.assertRaises(cli.Problem):
                batch.results()
        finally:
            batch.close()
        batch_lines = self.logged(home)[2:]
        self.assertEqual(sorted((x["cmd"], x["kind"], x["exit"]) for x in batch_lines),
                         [("issue list", "graphql", 0), ("pr list", "graphql", 0), ("workflow list", "rest", 2)])
        for line in batch_lines:
            self.assertLine(line, tool="harness-pr", repo=MAIN_NAME, ticket=12)

        # GhBatch: every call is one line even when results() stops early. The first call fails after 1 s; by then
        # the second has finished but is never read (close() logs it with its own exit), and the third is still
        # running (close() stops it: exit null).
        self.scenario([{"match": ["issue", "list", "FIRSTSLOW"], "stderr": "HTTP 500\n", "exit": 4},
                       {"match": ["label", "list"], "stderr": "nope\n", "exit": 6}], default={"stdout": "[]"})
        with mock.patch.dict(os.environ, {"GHTEST_SLOW": "FIRSTSLOW=1.0,STUCK=30"}):
            batch = gh.GhBatch(W["gh"], [["issue", "list", "FIRSTSLOW"], ["label", "list"], ["pr", "list", "STUCK"]],
                               W["wt40"], time.monotonic() + 30, budget_seconds=6)
            try:
                with self.assertRaises(cli.Problem) as caught:
                    batch.results()
            finally:
                batch.close()
        self.assertIn("HTTP 500", str(caught.exception))
        early_lines = self.logged(home)[5:]
        self.assertEqual(sorted((x["cmd"], x["kind"], x["exit"] if x["exit"] is not None else -1) for x in early_lines),
                         [("issue list", "graphql", 4), ("label list", "rest", 6), ("pr list", "graphql", -1)])
        for line in early_lines:
            self.assertLine(line, tool="harness-pr", repo=MAIN_NAME, ticket=40)

    def test_ticket_from_the_worktree_in_PWD_else_cwd(self):
        ghlimit = lib("ghlimit")
        cases = [
            # (PWD, cwd, ticket)
            (None, W["wt12"], 12),
            (None, os.path.join(W["wt12"], "plugins", "harness"), 12),
            (None, W["main"], None),
            (W["wt40"], W["wt12"], 40),                          # PWD first
            (os.path.join(W["wt40"], "src"), W["main"], 40),
            ("/Users/someone/src/app", W["wt12"], 12),           # PWD outside a worktree: the cwd
            ("", W["wt12"], 12),
            ("/x/proj/.claude/worktrees/t-907-long-slug-here", "/elsewhere", 907),
            (None, "/x/proj/.claude/worktrees/t-0031-padded", 31),
            (None, "/x/proj/.claude/worktrees/t-5", None),       # no dash after the number
            (None, "/x/proj/worktrees/t-5-a", None),             # not under .claude/worktrees
            (None, "/x/proj/.claude/worktrees/feature-t-9-x", None),
            ("/a/b", "/c/d", None),
        ]
        for pwd_value, cwd, expected in cases:
            with self.subTest(PWD=pwd_value, cwd=cwd):
                env = {k: v for k, v in os.environ.items() if k != "PWD"}
                if pwd_value is not None:
                    env["PWD"] = pwd_value
                with mock.patch.dict(os.environ, env, clear=True):
                    got = ghlimit.ticket_of(cwd)
                self.assertEqual(got, expected)
                if expected is not None:
                    self.assertIs(type(got), int)
        # cwd None: the process's working directory
        here = os.getcwd()
        env = {k: v for k, v in os.environ.items() if k != "PWD"}
        try:
            os.chdir(W["wt40"])
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(ghlimit.ticket_of(None), 40)
        finally:
            os.chdir(here)

        # through harness-board: the cwd's worktree, or the PWD's when it has one
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
        runs = [(W["wt12"], None, 12), (W["wt12"], W["wt40"], 40), (W["main"], W["wt40"], 40),
                (W["main"], None, None), (W["wt40"], self.tmp, 40)]
        for cwd, pwd_value, expected in runs:
            with self.subTest(tool_cwd=os.path.basename(cwd), PWD=pwd_value):
                home = self.new_home()
                extra = {"PWD": pwd_value} if pwd_value is not None else {}
                p = self.run_tool(BOARD, "show", "12", "--json", cwd=cwd, home=home, **extra)
                self.assertEqual(p.returncode, 0, p.stderr)
                lines = self.logged(home)
                self.assertEqual(len(lines), 1, lines)
                self.assertLine(lines[0], tool="harness-board", ticket=expected, repo=MAIN_NAME)

    def test_repo_is_the_main_checkout_folder_from_a_worktree_or_the_main_checkout(self):
        ghlimit = lib("ghlimit")
        cases = [
            ("/a/b/Harness/.claude/worktrees/t-187-log-every-call", "Harness"),
            ("/a/b/Harness/.claude/worktrees/t-187-log-every-call/plugins/harness/tests", "Harness"),
            ("/a/b/Harness", "Harness"),
            ("/Users/someone/src/jotform-sync", "jotform-sync"),
            ("/Users/someone/src/proj/sub/dir", "dir"),
            (W["wt40"], MAIN_NAME),
            (W["main"], MAIN_NAME),
            (W["beta"], BETA_NAME),
        ]
        for cwd, expected in cases:
            with self.subTest(cwd=cwd):
                self.assertEqual(ghlimit.repo_of(cwd), expected)
        here = os.getcwd()
        try:
            for cwd, expected in ((W["beta"], BETA_NAME), (W["wt12"], MAIN_NAME)):
                os.chdir(cwd)
                self.assertEqual(ghlimit.repo_of(None), expected)
        finally:
            os.chdir(here)

        # through the tools: from the main checkout and from a worktree of it, and another project
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
        for cwd, expected in ((W["main"], MAIN_NAME), (W["wt12"], MAIN_NAME), (W["wt40"], MAIN_NAME)):
            with self.subTest(tool_cwd=cwd):
                home = self.new_home()
                p = self.run_tool(BOARD, "show", "12", "--json", cwd=cwd, home=home)
                self.assertEqual(p.returncode, 0, p.stderr)
                lines = self.logged(home)
                self.assertEqual(len(lines), 1, lines)
                self.assertLine(lines[0], repo=expected)
        home = self.new_home()
        p = self.run_tool(BOARD, "show", "12", "--json", cwd=W["beta"], home=home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([x["repo"] for x in self.logged(home)], [BETA_NAME])


# ==================================================================== cmd and kind
class Names(unittest.TestCase):
    def test_call_kind_and_call_name_table(self):
        ghlimit = lib("ghlimit")
        table = [
            # the ticket's table
            (["issue", "view", "12", "--json", "labels"], "issue view", "graphql"),
            (["api", "repos/o/r/issues/12/sub_issues?per_page=100"],
             "api repos/:owner/:repo/issues/:n/sub_issues", "rest"),
            (["api", "-X", "POST", "repos/o/r/pulls/34/reviews", "--input", "-"],
             "api POST repos/:owner/:repo/pulls/:n/reviews", "rest"),
            (["api", "--paginate", "repos/o/r/pulls/34/files?per_page=100", "--slurp"],
             "api repos/:owner/:repo/pulls/:n/files", "rest"),
            (["api", "graphql", "-f", "query=..."], "api graphql", "graphql"),
            (["api", "user", "--jq", ".login"], "api user", "rest"),
            (["api", "rate_limit"], "api rate_limit", "rest"),
            (["api", "repos/o/r/commits/0123abc4567/check-runs"],
             "api repos/:owner/:repo/commits/:sha/check-runs", "rest"),
            (["project", "item-edit", "3", "--owner", "o", "--url", "u"], "project item-edit", "graphql"),
            (["run", "view", "987", "--log-failed"], "run view", "rest"),
            # more of the same rules, so no row is a special case
            (["pr", "view", "34", "--json", "statusCheckRollup"], "pr view", "graphql"),
            (["pr", "list", "--head", "t/12-x", "--state", "all"], "pr list", "graphql"),
            (["issue", "comment", "7", "--body", "a/b/c"], "issue comment", "graphql"),
            (["repo", "view", "--json", "nameWithOwner"], "repo view", "graphql"),
            (["auth", "status"], "auth status", "graphql"),
            (["search", "prs", "--author", "@me"], "search prs", "graphql"),
            (["label", "create", "status:ready", "--color", "0e8a16"], "label create", "rest"),
            (["release", "list"], "release list", "rest"),
            (["workflow", "run", "ci.yml"], "workflow run", "rest"),
            (["run", "list", "--branch", "main"], "run list", "rest"),
            (["api", "--method", "PATCH", "repos/acme/widget/issues/7"],
             "api PATCH repos/:owner/:repo/issues/:n", "rest"),
            (["api", "-X", "GET", "repos/acme/widget/pulls/1"], "api repos/:owner/:repo/pulls/:n", "rest"),
            (["api", "-X", "DELETE", "repos/o/r/issues/comments/99"],
             "api DELETE repos/:owner/:repo/issues/comments/:n", "rest"),
            (["api", "repos/Big-Org/some.repo/git/refs/heads/main"],
             "api repos/:owner/:repo/git/refs/heads/main", "rest"),
            (["api", "repos/o/r/commits/" + "a1b2c3d4e5" * 4 + "/status"], "api repos/:owner/:repo/commits/:sha/status",
             "rest"),
            (["api", "repos/o/r/commits/abc12/status"], "api repos/:owner/:repo/commits/abc12/status", "rest"),
            (["api", "repos/o/r/commits/" + "f" * 41 + "/status"],
             "api repos/:owner/:repo/commits/" + "f" * 41 + "/status", "rest"),
            (["api", "repos/o/r/pulls/34/requested_reviewers"],
             "api repos/:owner/:repo/pulls/:n/requested_reviewers", "rest"),
            (["api", "--paginate", "graphql", "-f", "query=x"], "api graphql", "graphql"),
            (["api", "-X", "POST", "graphql", "-f", "query=x"], "api POST graphql", "graphql"),
            (["api", "users/someone/repos", "--jq", ".[].name"], "api users/someone/repos", "rest"),
            (["api", "notifications?all=true"], "api notifications", "rest"),
        ]
        for args, name, kind in table:
            with self.subTest(args=args):
                self.assertEqual(ghlimit.call_name(list(args)), name)
                self.assertEqual(ghlimit.call_kind(list(args)), kind)

        # rows made up now from random parts, so no lookup of known argv can pass; the seed is in each subTest
        seed = random.randrange(1 << 30)
        rng = random.Random(seed)
        for args, name, kind in random_rows(rng, 200):
            with self.subTest(seed=seed, args=args):
                self.assertEqual(ghlimit.call_name(list(args)), name)
                self.assertEqual(ghlimit.call_kind(list(args)), kind)


def random_name(rng):
    """An owner or repository name: starts with a letter, holds letters, digits, '-' and '.'."""
    first = rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return first + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789-.") for _ in range(rng.randint(0, 12)))


def random_hex(rng, length):
    """A hex string of `length` with at least one letter (an all-digit segment is a number, not a sha)."""
    text = [rng.choice("0123456789abcdef") for _ in range(length)]
    text[rng.randrange(length)] = rng.choice("abcdef")
    return "".join(text)


def random_rows(rng, count):
    """(args, call_name, call_kind) rows: api calls on random repositories, numbers, shas, methods and flags, and
    other commands with random numbers. The expected name is put together from the parts, not computed."""
    rows = []
    for _ in range(count):
        if rng.random() < 0.2:
            n = str(rng.randint(1, 999999))
            group, verb = rng.choice([("issue", "view"), ("issue", "edit"), ("pr", "view"), ("pr", "merge"),
                                      ("pr", "checks"), ("run", "view"), ("run", "rerun"), ("label", "delete"),
                                      ("release", "view"), ("workflow", "view"), ("project", "item-list")])
            kind = "rest" if group in ("run", "label", "release", "workflow") else "graphql"
            extra = rng.choice([[], ["--json", "a/b"], ["-R", "o/r"]])
            rows.append(([group, verb, n] + extra, f"{group} {verb}", kind))
            continue
        owner, repo, n = random_name(rng), random_name(rng), str(rng.randint(1, 999999))
        sha_len = rng.choice((7, 39, 40, 41, 6))
        sha = random_hex(rng, sha_len)
        sha_shown = ":sha" if 7 <= sha_len <= 40 else sha
        path, shown = rng.choice([
            (f"repos/{owner}/{repo}/issues/{n}", "repos/:owner/:repo/issues/:n"),
            (f"repos/{owner}/{repo}/pulls/{n}/reviews", "repos/:owner/:repo/pulls/:n/reviews"),
            (f"repos/{owner}/{repo}/pulls/{n}/files", "repos/:owner/:repo/pulls/:n/files"),
            (f"repos/{owner}/{repo}/issues/{n}/sub_issues", "repos/:owner/:repo/issues/:n/sub_issues"),
            (f"repos/{owner}/{repo}/issues/comments/{n}", "repos/:owner/:repo/issues/comments/:n"),
            (f"repos/{owner}/{repo}/commits/{sha}/check-runs", f"repos/:owner/:repo/commits/{sha_shown}/check-runs"),
            (f"repos/{owner}/{repo}/commits/{sha}/status", f"repos/:owner/:repo/commits/{sha_shown}/status"),
            (f"repos/{owner}/{repo}/actions/runs/{n}/jobs", "repos/:owner/:repo/actions/runs/:n/jobs"),
        ])
        if rng.random() < 0.5:
            path += f"?per_page={rng.randint(1, 100)}" + rng.choice(["", f"&page={rng.randint(1, 9)}"])
        method = rng.choice(["POST", "PATCH", "DELETE", "GET", None])
        args = ["api"]
        if rng.random() < 0.3:
            args.append("--paginate")
        if method:
            args += [rng.choice(["-X", "--method"]), method]
        args.append(path)
        args += rng.choice([[], ["--slurp"], ["--jq", ".[].name"], ["--input", "-"], ["-f", "body=x"]])
        name = "api " + (f"{method} " if method and method != "GET" else "") + shown
        rows.append((args, name, "rest"))
    return rows


# ==================================================================== the day files
@unittest.skipIf(GIT is None, "git not found")
class Files(Case):
    def test_day_files_older_than_14_days_go_when_a_new_day_starts(self):
        ghlimit = lib("ghlimit")
        # gh_dir() follows HOME at the time of the call
        for home in (self.new_home(), self.new_home()):
            with mock.patch.dict(os.environ, {"HOME": home}):
                self.assertEqual(os.path.realpath(ghlimit.gh_dir()),
                                 os.path.realpath(os.path.join(home, ".claude", "harness", "gh")))

        def seed(home, ages):
            folder = self.gh_dir(home)
            for k in ages:
                write(os.path.join(folder, f"calls-{day(k)}.jsonl"), json.dumps({"at": f"{day(k)}T12:00:00Z"}) + "\n")
            write(os.path.join(folder, "pause.json"), "{}\n")
            write(os.path.join(folder, "notes.txt"), "keep me\n")

        def remaining(home):
            return sorted(os.listdir(self.gh_dir(home)))

        # through a tool: the first call of the day creates today's file and drops the files over 14 days old
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
        home = self.new_home()
        seed(home, (1, 2, 7, 13, 14, 15, 16, 30, 400))
        p = self.run_tool(BOARD, "show", "12", "--json", cwd=W["main"], home=home)
        self.assertEqual(p.returncode, 0, p.stderr)
        kept = sorted([f"calls-{day(k)}.jsonl" for k in (0, 1, 2, 7, 13, 14)] + ["notes.txt", "pause.json"])
        self.assertEqual(remaining(home), kept)
        with open(os.path.join(self.gh_dir(home), f"calls-{day(0)}.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(len([x for x in fh if x.strip()]), 1)
        with open(os.path.join(self.gh_dir(home), f"calls-{day(7)}.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), json.dumps({"at": f"{day(7)}T12:00:00Z"}) + "\n")     # kept as it was

        # in process: log_call on another set of ages; it writes what it is given
        home = self.new_home()
        seed(home, (3, 14, 15, 60))
        self.in_process("/opt/harness/bin/harness-work", home=home)
        ghlimit.log_call(["issue", "view", "1"], cwd=W["wt40"], exit=0, ms=5)
        ghlimit.log_call(["api", "repos/o/r/pulls/2"], cwd=W["beta"], exit=None, ms=1234, waited_ms=250, refused=True)
        self.assertEqual(remaining(home), sorted([f"calls-{day(k)}.jsonl" for k in (0, 3, 14)] +
                                                 ["notes.txt", "pause.json"]))
        lines = [x for x in self.logged(home) if x.get("tool") == "harness-work"]
        self.assertEqual(len(lines), 2, lines)
        self.assertLine(lines[0], cmd="issue view", kind="graphql", exit=0, ms=5, repo=MAIN_NAME, ticket=40)
        self.assertLine(lines[1], cmd="api repos/:owner/:repo/pulls/:n", kind="rest", exit=None, ms=1234,
                        waited_ms=250, refused=True, repo=BETA_NAME, ticket=None)

        # only a new day prunes: with today's file already there, a call appends and an old file stays
        home = self.new_home()
        old_line = json.dumps({"at": f"{day(20)}T08:00:00Z"}) + "\n"
        today_line = json.dumps({"at": f"{day(0)}T00:00:00Z"}) + "\n"
        write(os.path.join(self.gh_dir(home), f"calls-{day(20)}.jsonl"), old_line)
        write(os.path.join(self.gh_dir(home), f"calls-{day(0)}.jsonl"), today_line)
        with mock.patch.dict(os.environ, {"HOME": home}):
            ghlimit.log_call(["pr", "view", "8"], cwd=W["main"], exit=0, ms=7)
            ghlimit.log_call(["run", "view", "9"], cwd=W["main"], exit=1, ms=8)
        self.assertEqual(remaining(home), [f"calls-{day(20)}.jsonl", f"calls-{day(0)}.jsonl"])
        with open(os.path.join(self.gh_dir(home), f"calls-{day(20)}.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), old_line)
        with open(os.path.join(self.gh_dir(home), f"calls-{day(0)}.jsonl"), encoding="utf-8") as fh:
            rows = [x for x in fh if x.strip()]
        self.assertEqual(rows[0], today_line)
        self.assertEqual([json.loads(x)["cmd"] for x in rows[1:]], ["pr view", "run view"])

        # read_calls(since): every good line with at >= since, from the day files that can hold them
        home = self.new_home()
        folder = self.gh_dir(home)

        def full(at, cmd):
            return json.dumps({"at": at, "tool": "harness-board", "cmd": cmd, "kind": "graphql", "repo": "r",
                               "ticket": None, "exit": 0, "ms": 1, "waited_ms": 0, "refused": False})
        write(os.path.join(folder, f"calls-{day(3)}.jsonl"), full(f"{day(3)}T10:00:00Z", "a") + "\n")
        write(os.path.join(folder, f"calls-{day(2)}.jsonl"), "\n".join([
            full(f"{day(2)}T11:59:59Z", "b"), full(f"{day(2)}T12:00:00Z", "c"), "not json",
            full(f"{day(2)}T23:00:00Z", "d")]) + "\n")
        write(os.path.join(folder, f"calls-{day(1)}.jsonl"), "\n".join([
            full(f"{day(1)}T01:00:00Z", "e"), "[1, 2]", full(f"{day(1)}T03:00:00Z", "f")]) + "\n")
        write(os.path.join(folder, f"calls-{day(0)}.jsonl"), "\n".join([
            full(f"{day(0)}T00:00:05Z", "g"), "", '{"at": broken']) + "\n")

        def utc(date, hms):
            return dt.datetime.strptime(f"{date} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        minus4 = dt.timezone(dt.timedelta(hours=-4))
        cases = [
            (utc(day(2), "12:00:00"), ["c", "d", "e", "f", "g"]),
            (utc(day(1), "02:00:00").astimezone(minus4), ["f", "g"]),      # 22:00 the day before, at -04:00
            (utc(day(3), "00:00:00"), ["a", "b", "c", "d", "e", "f", "g"]),
            (utc(day(0), "00:00:05"), ["g"]),
            (utc(day(0), "00:00:05") + dt.timedelta(days=1), []),
        ]
        with mock.patch.dict(os.environ, {"HOME": home}):
            for since, expected in cases:
                with self.subTest(since=since.isoformat()):
                    got = list(ghlimit.read_calls(since))
                    self.assertTrue(all(isinstance(x, dict) for x in got), got)
                    self.assertEqual(sorted(x["cmd"] for x in got), expected)
        self.assertEqual(sorted(os.listdir(folder)), [f"calls-{day(k)}.jsonl" for k in (3, 2, 1, 0)])  # read only

    def test_unwritable_log_never_fails_a_command(self):
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])

        def shown(p):
            data = json.loads(p.stdout)
            data.pop("fetched_at", None)
            return data

        base = self.run_tool(BOARD, "show", "12", "--json", cwd=W["main"], home=self.new_home())
        self.assertEqual(base.returncode, 0, base.stderr)
        base_argv = self.fakegh_argv()
        self.assertEqual(len(base_argv), 1)

        def gh_is_a_file(home):
            write(self.gh_dir(home), "not a folder\n")

        def gh_is_read_only(home):
            os.makedirs(self.gh_dir(home))
            os.chmod(self.gh_dir(home), 0o555)
            self.addCleanup(os.chmod, self.gh_dir(home), 0o755)

        def day_file_is_a_folder(home):
            os.makedirs(os.path.join(self.gh_dir(home), f"calls-{day(0)}.jsonl"))

        for breakage in (gh_is_a_file, gh_is_read_only, day_file_is_a_folder):
            with self.subTest(breakage.__name__):
                home = self.new_home()
                breakage(home)
                self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
                p = self.run_tool(BOARD, "show", "12", "--json", cwd=W["main"], home=home)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(shown(p), shown(base))
                self.assertEqual(p.stderr, base.stderr)
                self.assertEqual(self.fakegh_argv(), base_argv)

        # harness-pr's failing checks: the same exit code and output with or without a log
        failing = [check_run("test", "FAILURE", "https://github.com/acme/widget/actions/runs/987/job/654")]
        pr_scenario = [{"match": ["pr", "view", "34"], "stdout_json": {"statusCheckRollup": failing}},
                       {"match": ["run", "view", "987"], "stdout": "boom\n"}]
        self.scenario(pr_scenario)
        good = self.run_tool(PR, "checks", "34", cwd=W["main"], home=self.new_home())
        good_argv = self.fakegh_argv()
        home = self.new_home()
        gh_is_a_file(home)
        self.scenario(pr_scenario)
        bad = self.run_tool(PR, "checks", "34", cwd=W["main"], home=home)
        self.assertEqual((bad.returncode, bad.stdout, bad.stderr), (good.returncode, good.stdout, good.stderr))
        self.assertEqual(good.returncode, 1)
        self.assertEqual(self.fakegh_argv(), good_argv)

        # in process: log_call, gh_run and GhBatch under a gh "folder" that is a file
        gh, ghlimit = lib("gh"), lib("ghlimit")
        home = self.new_home()
        gh_is_a_file(home)
        self.in_process("/opt/harness/bin/harness-board", home=home)
        self.scenario([{"match": ["pr", "list"], "stdout_json": [{"number": 7}]}], default={"stdout": "[]"})
        self.assertIsNone(ghlimit.log_call(["issue", "view", "1"], cwd=W["main"], exit=0, ms=3))
        done = gh.gh_run(["pr", "list"], W["main"])
        self.assertEqual((done.returncode, done.stdout), (0, '[{"number": 7}]'))
        batch = gh.GhBatch(W["gh"], [["pr", "list"], ["issue", "list"]], W["main"], time.monotonic() + 30,
                           budget_seconds=6)
        try:
            self.assertEqual(batch.results(), [[{"number": 7}], []])
        finally:
            batch.close()
        with open(self.gh_dir(home), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not a folder\n")


if __name__ == "__main__":
    unittest.main()
