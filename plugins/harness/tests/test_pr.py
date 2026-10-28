"""Tests for harness-pr, driven by the fake gh (fakegh.py), stub sibling tools and a fake claude.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME and real git repositories: a light one (a repository with an `origin` remote
that is never contacted) for the subcommands that only talk to gh, and for open and land a bare origin, a
main checkout pushed to it and a ticket worktree on t/12-add-the-widget. `fakebin` holds gh (fakegh.py
behind a wrapper), git (a wrapper that logs and runs the real git), claude, terminal-notifier and osascript
fakes, a harness-work stub and python3; `stubbin` holds harness-board and harness-red stubs. The stubs log their
calls and print canned output, keyed by their first argument or their first two (`cost 12`); given a
`read_stdin` path they record what they read on stdin. gh, git and the stubs also append one line per call to
ORDER_LOG, so a test can check the order across tools. The fake executables are written once for the module: macOS checks a new executable on
its first run, which costs about half a second each time.
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
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-pr")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
MARKER = "<!-- harness-review -->"
BRANCH = "t/12-add-the-widget"
PR_URL = "https://github.com/o/r/pull/34"
SHA = "abc123def4567890abc123def4567890abc12345"
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
HIGH_RISK = "High risk: review the data and live-system paths before merging."
LAND_FIELDS = "isDraft,mergeable,headRefName,closingIssuesReferences,statusCheckRollup,title,url,state"

STUB = """import json, os, sys
name, argv = os.path.basename(sys.argv[1]), sys.argv[2:]
with open(os.environ["ORDER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join([name] + argv) + "\\n")
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": name, "argv": argv, "cwd": os.getcwd()}) + "\\n")
outputs = {}
path = os.environ.get("STUB_OUTPUTS")
if path and os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        outputs = json.load(fh).get(name, {})
answer = outputs.get(" ".join(argv[:2]), outputs.get(argv[0] if argv else "", {}))   # "cost 12", else "cost"
if answer.get("read_stdin"):     # a path: what the tool was given on stdin is appended there, one JSON line per call
    with open(answer["read_stdin"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.stdin.read()) + "\\n")
sys.stdout.write(answer.get("stdout", ""))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("exit", 0))
"""

CLAUDE = """import json, os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
for var, stream in (("CLAUDE_STDOUT", sys.stdout), ("CLAUDE_STDERR", sys.stderr)):
    path = os.environ.get(var)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            stream.write(fh.read())
sys.exit(int(os.environ.get("CLAUDE_EXIT", "0")))
"""

RECORDER = """import json, os, sys
with open(os.environ["NOTIFY_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[1])] + sys.argv[2:]) + "\\n")
"""

TICKET_REQUEST = "Add a widget that frobs.\nIt must frob twice.\n\nThe second paragraph is not the summary."
RED_TAIL = "\n".join(f"red line {i}" for i in range(1, 41))


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
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-bin-"))
    py = sys.executable
    fakebin, stubbin = os.path.join(root, "fakebin"), os.path.join(root, "stubbin")
    stub, claude, recorder = (write(os.path.join(root, n), t) for n, t in
                              (("stub.py", STUB), ("claude.py", CLAUDE), ("recorder.py", RECORDER)))
    executable(os.path.join(fakebin, "gh"),
               f'#!/bin/sh\nprintf \'%s\\n\' "gh $*" >> "$ORDER_LOG"\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "git"),
               f'#!/bin/sh\nprintf \'%s\\n\' "git $*" >> "$ORDER_LOG"\nexec "{GIT}" "$@"\n')
    executable(os.path.join(fakebin, "claude"), f'#!/bin/sh\nexec "{py}" "{claude}" "$@"\n')
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), f'#!/bin/sh\nexec "{py}" "{recorder}" "$0" "$@"\n')
    os.symlink(py, os.path.join(fakebin, "python3"))
    for name in ("harness-board", "harness-red"):
        executable(os.path.join(stubbin, name), f'#!/bin/sh\nexec "{py}" "{stub}" "$0" "$@"\n')
    # harness-work goes in fakebin, on PATH with or without the stubs: the land tests that run the real
    # harness-board must not run the real `harness-work cost` either.
    executable(os.path.join(fakebin, "harness-work"), f'#!/bin/sh\nexec "{py}" "{stub}" "$0" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, stubbin=stubbin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def check_run(name, status="COMPLETED", conclusion="SUCCESS", url=""):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "detailsUrl": url,
            "workflowName": "CI"}


def status_context(name, state):
    return {"__typename": "StatusContext", "context": name, "state": state, "targetUrl": "https://ci.example/1"}


def gql_comment(login, body, when, path="widget.py", line=None):
    return {"author": {"login": login}, "body": body, "path": path, "line": line, "createdAt": when,
            "url": f"https://github.com/o/r/pull/34#discussion_r{abs(hash(body)) % 100000}"}


def thread(resolved, *comments):
    return {"isResolved": resolved, "comments": {"nodes": list(comments)}}


def graphql(threads=(), reviews=(), comments=()):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(threads)},
                                                    "reviews": {"nodes": list(reviews)},
                                                    "comments": {"nodes": list(comments)}}}}}


class PrTest(unittest.TestCase):
    """The light fixture: a repository `proj` with an origin remote that is never contacted."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.order_log = os.path.join(self.tmp, "order.log")
        self.stub_log = os.path.join(self.tmp, "stub.log")
        self.stub_outputs = os.path.join(self.tmp, "stub-outputs.json")
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.make_repos()
        self.scenario([])

    def make_repos(self):
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        self.git(self.main, "remote", "add", "origin", "https://github.com/o/r.git")

    # ----------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, stubs=True, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_"))}
        path = ([BINS["stubbin"]] if stubs else []) + [BINS["fakebin"], "/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(path), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_GH=os.path.join(BINS["fakebin"], "gh"),
                   HARNESS_CLAUDE=os.path.join(BINS["fakebin"], "claude"), HARNESS_POLL_SECONDS="0.01",
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log, ORDER_LOG=self.order_log,
                   STUB_LOG=self.stub_log, STUB_OUTPUTS=self.stub_outputs, NOTIFY_LOG=self.notify_log)
        env.update(extra)
        return env

    def git(self, repo, *args):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.gh_log, self.order_log, self.stub_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def outputs(self, **tools):
        """Canned stub output: outputs(**{"harness-board": {"show": {"stdout": ...}}})."""
        write(self.stub_outputs, json.dumps(tools))

    def pr(self, *args, cwd=None, stubs=True, **env):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.main,
                              env=self.env(stubs=stubs, **env), capture_output=True, text=True, timeout=120)

    def ok(self, *args, **kw):
        p = self.pr(*args, **kw)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def fails(self, code, *args, **kw):
        p = self.pr(*args, **kw)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)

    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def gh_calls(self):
        return self.jsonl(self.gh_log)

    def gh_argv(self):
        return [c["argv"] for c in self.gh_calls()]

    def stub_calls(self, tool):
        return [c["argv"] for c in self.jsonl(self.stub_log) if c["tool"] == tool]

    def order(self):
        if not os.path.exists(self.order_log):
            return []
        with open(self.order_log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def position(self, prefix):
        lines = self.order()
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                return i
        self.fail(f"no call starting with {prefix!r} in:\n" + "\n".join(lines))

    def notifications(self):
        return self.jsonl(self.notify_log)

    def key(self):
        return hashlib.sha1(os.path.realpath(self.main).encode()).hexdigest()[:12]

    def state_path(self, *parts):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key(), *parts)


class WorktreeTest(PrTest):
    """A bare origin, the main checkout `proj` pushed to it, and the ticket worktree on t/12-add-the-widget."""

    def make_repos(self):
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, ".git", "info", "exclude"), ".claude/worktrees/\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "t-12-add-the-widget")
        self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, self.wt, "origin/main")
        write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 2\n")
        write(os.path.join(self.wt, "tests", "test_widget.py"), "def test_frob():\n    assert True\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add the widget")


# =========================================================================== open
def board_ticket(risk="high"):
    return {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12", "type": "task",
            "status": "in-progress", "size": "M", "risk": risk, "flags": [], "tier": "fable",
            "labels": ["task", "status:in-progress", "size:M", f"risk:{risk}"], "parent": None, "blocked_by": [],
            "body": "...", "comments": [],
            "sections": {"request": TICKET_REQUEST, "outline": "1. Write it.",
                         "definition_of_done": ["- [ ] The widget frobs twice", "- [x] The docs mention it"],
                         "tests": ["`tests/test_widget.py::test_frob`: frobs twice",
                                   "`tests/test_widget.py::test_empty`: empty input"],
                         "context": ""}}


RED_RECORD = {"branch": BRANCH, "base_sha": "0" * 40, "test_files": ["tests/test_widget.py", "tests/conftest.py"],
              "run_files": ["tests/test_widget.py"], "test_hash": "f" * 64, "cmd": "python3 -m pytest -q",
              "exit": 1, "kind": "assertion", "tail": RED_TAIL, "at": "2026-10-13T09:00:00-04:00", "current": True}

FINDINGS = [{"path": "widget.py", "line": 2, "severity": "high", "body": "Frobs only once on empty input."},
            {"path": "widget.py", "line": 1, "severity": "medium", "body": "Name is vague."},
            {"path": "tests/test_widget.py", "line": 2, "severity": "low", "body": "Assert the value."},
            {"path": "README.md", "line": 1, "severity": "low", "body": "Mention the widget."}]


class Open(WorktreeTest):
    def setUp(self):
        super().setUp()
        self.verify_log = write(os.path.join(self.tmp, "verify.log"),
                                "\n".join(f"verify line {i}" for i in range(1, 31)) + "\nVERIFY OK\n\n")
        self.review = write(os.path.join(self.tmp, "findings.json"), json.dumps(FINDINGS))

    def remote_head(self):
        p = subprocess.run([GIT, "--git-dir", self.origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
                           capture_output=True, text=True, env=self.env())
        return p.stdout.strip() if p.returncode == 0 else None

    def test_open_pushes_and_creates_a_draft_with_the_rendered_body(self):
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("high"))},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "12", "--verify-log", self.verify_log, "--review", self.review, cwd=self.wt)
        self.assertEqual(p.stdout, f"#34 {PR_URL}\n")
        self.assertEqual(self.remote_head(), self.git(self.wt, "rev-parse", "HEAD"))
        self.assertEqual(self.gh_argv(), [
            ["pr", "list", "--head", BRANCH, "--state", "open", "--json", "number,url"],
            ["pr", "create", "--draft", "--base", "main", "--head", BRANCH, "--title", "Add the widget (#12)",
             "--body-file", "-"]])
        self.assertLess(self.position(f"git -C {self.wt} push -u origin {BRANCH}"), self.position("gh pr create"))
        body = self.gh_calls()[1]["stdin"]
        lines = body.splitlines()
        self.assertEqual(lines[0], "Closes #12")
        self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n", body)
        self.assertNotIn("second paragraph", body)
        self.assertIn("## Definition of done\n- [ ] The widget frobs twice\n- [x] The docs mention it\n", body)
        self.assertIn("- `tests/test_widget.py::test_frob`: frobs twice\n"
                      "- `tests/test_widget.py::test_empty`: empty input", body)
        self.assertIn("Test files run: `tests/test_widget.py`", lines)
        self.assertIn("Red run before implementation (assertion, exit 1):", lines)
        self.assertIn("red line 26", lines)
        self.assertIn("red line 40", lines)
        self.assertNotIn("red line 25", lines)
        self.assertIn("verify line 12", lines)
        self.assertIn("VERIFY OK", lines)
        self.assertNotIn("verify line 11", lines)
        self.assertIn("4 findings: 1 high, 1 medium, 2 low.", lines)
        self.assertIn("- `widget.py:2`: Frobs only once on empty input.", lines)
        self.assertNotIn("Name is vague.", body)
        self.assertEqual(lines[-1], HIGH_RISK)
        self.assertNotIn("{{", body)
        self.assertEqual(self.stub_calls("harness-board"), [
            ["show", "12", "--json", "--repo", self.main],
            ["comment", "12", "--body", f"Pull request: {PR_URL}", "--repo", self.main]])
        self.assertEqual(self.stub_calls("harness-red"), [["status", "--json", "--worktree", self.wt]])

    def test_open_edits_the_existing_pr_and_prints_json(self):
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("low"))}},
                        "harness-red": {"status": {"stderr": "harness-red: No red run recorded\n", "exit": 1}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": [{"number": 34, "url": PR_URL}]},
                       {"match": ["pr", "edit", "34"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "#12", "--json", cwd=self.wt)
        self.assertEqual(json.loads(p.stdout), {"number": 34, "url": PR_URL})
        argv = self.gh_argv()
        self.assertEqual(argv[1], ["pr", "edit", "34", "--body-file", "-"])
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in argv))
        body = self.gh_calls()[1]["stdin"]
        self.assertTrue(body.startswith("Closes #12\n"))
        self.assertIn("Red run before implementation (none, exit -):", body)
        self.assertIn("No red run recorded on this branch.", body)
        self.assertIn("No verify log attached.", body)
        self.assertIn("No review attached.", body)
        self.assertEqual(body.splitlines()[-1], "None noted.")
        self.assertEqual(self.stub_calls("harness-board"), [["show", "12", "--json", "--repo", self.main]])

    def test_open_shows_the_warning_lines_of_every_board_call(self):
        """#69: warnings pass through from every harness-board call, not only `status`: open's `show` and `comment`
        each exit 0 with a warning among other stderr; open's stderr is exactly those warnings, once each, in call
        order, and its stdout is unchanged."""
        show_warning = "harness-board: warning: the Projects board acme/3 was not updated: show (token-n1)"
        comment_warning = "harness-board: warning: the Projects board acme/3 was not updated: comment (token-p2)"
        self.outputs(**{"harness-board": {
            "show": {"stdout": json.dumps(board_ticket("low")),
                     "stderr": f"harness-board: note: chatter (token-q3)\n{show_warning}\n"},
            "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n",
                        "stderr": f"{comment_warning}\nwarning: chatter without the prefix (token-r4)\n"}},
            "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "12", cwd=self.wt)
        self.assertEqual(p.stdout, f"#34 {PR_URL}\n")
        self.assertEqual(p.stderr.splitlines(), [show_warning, comment_warning], p.stderr)
        self.assertEqual([a[0] for a in self.stub_calls("harness-board")], ["show", "comment"])

    def test_open_refuses_a_dirty_tree(self):
        write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 3\n")
        write(os.path.join(self.wt, "notes.txt"), "scratch\n")
        p = self.pr("open", "12", cwd=self.wt)
        self.assertOneLineError(p, "uncommitted changes", "widget.py", "notes.txt")
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertIsNone(self.remote_head())

    def test_open_refuses_a_branch_that_is_not_the_tickets(self):
        self.git(self.wt, "switch", "-q", "-c", "feature/widget")
        self.assertOneLineError(self.pr("open", "12", cwd=self.wt), "not a ticket branch")
        self.git(self.wt, "switch", "-q", "-c", "t/13-other")
        self.assertOneLineError(self.pr("open", "12", "--worktree", self.wt), "ticket #13", "not #12")
        self.assertEqual(self.gh_calls(), [])

    def test_open_with_the_real_board_and_red_tools(self):
        """No stubs on PATH: harness-pr finds harness-board and harness-red next to itself."""
        write(self.state_path("red", "t-12-add-the-widget.json"), json.dumps(dict(RED_RECORD, current=None)))
        body = ("## Request\nAdd a widget that frobs.\n\n## Definition of done\n- [ ] The widget frobs twice\n\n"
                "## Tests that must pass\n- `tests/test_widget.py::test_frob`: frobs twice\n")
        issue = {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12", "body": body,
                 "labels": [{"name": "task"}, {"name": "risk:low"}], "state": "OPEN", "parent": None,
                 "blockedBy": [], "comments": []}
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue},
                       {"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#c1\n"}])
        p = self.ok("open", "12", "--json", cwd=self.wt, stubs=False)
        self.assertEqual(json.loads(p.stdout), {"number": 34, "url": PR_URL})
        calls = self.gh_calls()
        create = next(c for c in calls if c["argv"][:2] == ["pr", "create"])
        self.assertIn("- [ ] The widget frobs twice", create["stdin"])
        self.assertIn("- `tests/test_widget.py::test_frob`: frobs twice", create["stdin"])
        self.assertIn("Red run before implementation (assertion, exit 1):", create["stdin"])
        self.assertIn(["issue", "comment", "12", "--body", f"Pull request: {PR_URL}\n\n<!-- harness -->"],
                      [c["argv"] for c in calls])
        self.assertTrue(os.path.isfile(self.state_path("tickets", "12.json")))


NO_TESTS_RED = "none: the ticket is no-tests"
NO_RED = "No red run recorded on this branch."


class OpenNoTests(WorktreeTest):
    """A ticket flagged no-tests has no red run; the body says why instead of "No red run recorded"."""

    def open_body(self, flags, red_status):
        ticket = dict(board_ticket("low"), flags=list(flags), labels=["task", "status:in-progress", "risk:low"]
                      + list(flags))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": red_status}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        self.ok("open", "12", cwd=self.wt)
        return next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])["stdin"]

    def test_the_red_block_says_the_ticket_is_no_tests(self):
        # harness-red status on a no-tests ticket prints its message and exits 0 (not JSON), or has no record
        # and exits 1; either way the ticket's flag decides what the body says.
        for label, red_status in (("message", {"stdout": "ticket 12 is no-tests: no red run needed\n"}),
                                  ("refusal", {"stderr": "harness-red: No red run recorded\n", "exit": 1})):
            with self.subTest(red=label):
                body = self.open_body(["no-tests"], red_status)
                lines = body.splitlines()
                self.assertIn(NO_TESTS_RED, lines)
                self.assertNotIn(NO_RED, body)
                at = lines.index(NO_TESTS_RED)
                self.assertTrue(lines[at - 1] == "```" and lines[at + 1] == "```", "\n".join(lines[at - 2:at + 3]))
                self.assertTrue(lines[at - 2].startswith("Red run before implementation"), lines[at - 2])
                self.assertEqual(lines[0], "Closes #12")
                self.assertIn("## Definition of done\n- [ ] The widget frobs twice", body)
                self.assertIn("No verify log attached.", body)
                self.assertNotIn("{{", body)

    def open_body_labels(self, labels, flags, red_status):
        ticket = dict(board_ticket("low"), flags=list(flags), labels=["task", "status:in-progress", "risk:low"]
                      + list(labels))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": red_status}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        self.ok("open", "12", cwd=self.wt)
        return next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])["stdin"]

    def test_the_no_tests_json_object_from_harness_red_is_not_a_red_record(self):
        # `harness-red status --json` on a no-tests ticket prints {"no_tests": true, ...} and exits 0.
        base = {"no_tests": True, "ticket": 12, "message": "ticket 12 is no-tests: no red run needed"}
        for label, obj in (("plain", base),
                           ("with files", dict(base, run_files=["tests/leak.py"], test_files=["tests/leak2.py"]))):
            with self.subTest(obj=label):
                body = self.open_body(["no-tests"], {"stdout": json.dumps(obj)})
                self.assertIn(NO_TESTS_RED, body.splitlines())
                self.assertNotIn(NO_RED, body)
                self.assertNotIn("Test files run", body)
                self.assertNotIn("tests/leak", body)
                self.assertNotIn("unknown", body)
                self.assertNotIn("exit None", body)

    def test_the_body_trusts_flags_not_labels(self):
        # A `no-tests` label with empty flags: the body keeps the ordinary line.
        body = self.open_body_labels(["no-tests"], [], {"stderr": "harness-red: No red run recorded\n", "exit": 1})
        self.assertIn(NO_RED, body)
        self.assertNotIn(NO_TESTS_RED, body)

    def test_other_flags_keep_the_no_red_run_line(self):
        for flags in ([], ["needs-local"], ["priority:high", "needs-local"]):
            with self.subTest(flags=flags):
                body = self.open_body(flags, {"stderr": "harness-red: No red run recorded\n", "exit": 1})
                self.assertIn(NO_RED, body)
                self.assertNotIn("no-tests", body)

    def test_a_recorded_red_run_still_prints_its_tail_on_an_ordinary_ticket(self):
        body = self.open_body(["needs-local"], {"stdout": json.dumps(RED_RECORD)})
        self.assertIn("Red run before implementation (assertion, exit 1):", body.splitlines())
        self.assertIn("red line 40", body.splitlines())
        self.assertNotIn(NO_TESTS_RED, body)
        self.assertNotIn(NO_RED, body)

    def test_with_the_real_board_tool_the_no_tests_label_reaches_the_body(self):
        """No stubs: harness-board turns the `no-tests` label into the flag and harness-red has no record."""
        body = ("## Request\nFix the docs.\n\n## Definition of done\n- [ ] The guide is correct\n\n"
                "## Tests that must pass\n- None: docs only\n")
        issue = {"number": 12, "title": "Fix the docs", "url": "https://github.com/o/r/issues/12", "body": body,
                 "labels": [{"name": "task"}, {"name": "risk:low"}, {"name": "no-tests"}], "state": "OPEN",
                 "parent": None, "blockedBy": [], "comments": []}
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue},
                       {"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#c1\n"}])
        self.ok("open", "12", "--json", cwd=self.wt, stubs=False)
        create = next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])
        self.assertIn(NO_TESTS_RED, create["stdin"].splitlines())
        self.assertNotIn(NO_RED, create["stdin"])


# =========================================================================== review
DIFF = """diff --git a/widget.py b/widget.py
index 1111111..2222222 100644
--- a/widget.py
+++ b/widget.py
@@ -1,4 +1,5 @@
 import os
-old = 1
+new = 2
+more = 3
 def frob():
     return 1
@@ -20,2 +21,3 @@ def other():
 x = 1
+++ y = 2
 z = 3
\\ No newline at end of file
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1,2 @@
+# New
+text
"""

REVIEW_FINDINGS = [
    {"path": "widget.py", "line": 2, "severity": "high", "body": "Added line, wrong value."},
    {"path": "widget.py", "line": 4, "severity": "medium", "body": "Context line."},
    {"path": "widget.py", "line": 22, "severity": "low", "body": "A line whose text starts with ++ (not a header)."},
    {"path": "widget.py", "line": 10, "severity": "low", "body": "Between the hunks."},
    {"path": "old.py", "line": 1, "severity": "medium", "body": "The file is deleted."},
    {"path": "docs/new.md", "line": 2, "severity": "low", "body": "New file."},
    {"path": "README.md", "severity": "high", "body": "No line given."},
]


class Review(PrTest):
    def findings(self, data):
        return write(os.path.join(self.tmp, "findings.json"), json.dumps(data))

    def test_review_routes_on_diff_findings_inline_and_the_rest_to_the_body(self):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
        # #67: the default model is now opus; this test is about routing, so it names the model it expects in the body.
        p = self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS), "--model", "fable")
        self.assertIn("7 findings (4 inline, 3 in the body)", p.stdout)
        calls = self.gh_calls()
        self.assertEqual([c["argv"] for c in calls], [
            ["repo", "view", "--json", "nameWithOwner"], ["pr", "diff", "34"],
            ["api", "-X", "POST", "repos/o/r/pulls/34/reviews", "--input", "-"]])
        sent = json.loads(calls[2]["stdin"])
        self.assertEqual(sent["event"], "COMMENT")
        self.assertEqual(sent["comments"], [
            {"path": "widget.py", "line": 2, "side": "RIGHT", "body": f"[high] Added line, wrong value.\n\n{MARKER}"},
            {"path": "widget.py", "line": 4, "side": "RIGHT", "body": f"[medium] Context line.\n\n{MARKER}"},
            {"path": "widget.py", "line": 22, "side": "RIGHT",
             "body": f"[low] A line whose text starts with ++ (not a header).\n\n{MARKER}"},
            {"path": "docs/new.md", "line": 2, "side": "RIGHT", "body": f"[low] New file.\n\n{MARKER}"}])
        self.assertEqual(sent["body"], "Harness review (fable): 7 findings, 2 high, 2 medium, 3 low.\n\n"
                                       "Not on a changed line:\n"
                                       "- `widget.py:10` [low] Between the hunks.\n"
                                       "- `old.py:1` [medium] The file is deleted.\n"
                                       "- `README.md` [high] No line given.\n\n" + MARKER)

    def test_review_falls_back_to_the_body_when_github_refuses_inline_comments(self):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST"], "responses": [
                           {"stderr": "gh: Unprocessable Entity (HTTP 422)\n", "exit": 1}, {"stdout": "{}"}]}])
        p = self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS[:2]), "--model", "opus")
        self.assertIn("refused the inline comments", p.stderr)
        posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(json.loads(posts[0]["stdin"])["comments"]), 2)
        second = json.loads(posts[1]["stdin"])
        self.assertEqual(second["comments"], [])
        self.assertTrue(second["body"].startswith("Harness review (opus): 2 findings, 1 high, 1 medium, 0 low."))
        self.assertIn("- `widget.py:2` [high] Added line, wrong value.", second["body"])

    def posted_body(self, *extra):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
        self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS[:2]), *extra)
        posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
        self.assertEqual(len(posts), 1)
        return json.loads(posts[0]["stdin"])["body"]

    def test_review_without_model_names_opus_in_the_body(self):
        # Ticket #67: the default model is opus, so the body names the model that actually ran.
        body = self.posted_body()
        self.assertTrue(body.startswith("Harness review (opus): 2 findings, 1 high, 1 medium, 0 low."), body)
        self.assertNotIn("fable", body)

    def test_review_model_option_is_named_in_the_body(self):
        for model in ("fable", "opus", "sonnet"):
            with self.subTest(model=model):
                body = self.posted_body("--model", model)
                self.assertTrue(body.startswith(f"Harness review ({model}): 2 findings, "), body)

    def test_review_summary_parses_with_autoland_to_the_counts_it_declares(self):
        """#280: the summary line `harness-pr review` posts is one autoland.review_counts (the single parser of the
        per-severity counts) reads back as the high, medium and low it declared, for several mixes and models."""
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        from harness_lib import autoland
        mixes = [{"high": 1, "medium": 0, "low": 0}, {"high": 0, "medium": 3, "low": 1},
                 {"high": 2, "medium": 1, "low": 4}, {"high": 0, "medium": 0, "low": 2}]
        for model in ("fable", "opus", "sonnet"):
            for mix in mixes:
                with self.subTest(model=model, **mix):
                    sevs = [sev for sev in mix for _ in range(mix[sev])]
                    data = [{"path": "widget.py", "line": 2 + i, "severity": sev, "body": f"Finding {sev} {i}."}
                            for i, sev in enumerate(sevs)]
                    self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                                   {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
                    self.ok("review", "34", "--findings", self.findings(data), "--model", model)
                    posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
                    body = json.loads(posts[-1]["stdin"])["body"]
                    self.assertEqual(autoland.review_counts(body), mix, body)

    def test_review_with_no_findings_posts_a_plain_comment_review(self):
        self.scenario([{"match": ["pr", "review", "34"], "stdout": ""}])
        self.ok("review", "34", "--findings", self.findings([]), "--checked", "the red tests and the DoD")
        body = f"Harness review: no findings. Checked: the red tests and the DoD\n\n{MARKER}"
        self.assertEqual(self.gh_argv(), [["pr", "review", "34", "--comment", "-b", body]])


# =========================================================================== checks
GREEN = [check_run("build"), check_run("lint", conclusion="SKIPPED"), status_context("deploy/preview", "SUCCESS")]
PENDING = [check_run("build", status="IN_PROGRESS", conclusion=""), check_run("lint"),
           status_context("ci/legacy", "PENDING")]


class Checks(PrTest):
    def rollup(self, *responses):
        view = {"match": ["pr", "view", "34", "--json", "statusCheckRollup"]}
        if len(responses) == 1:
            view["stdout_json"] = {"statusCheckRollup": responses[0]}
        else:
            view["responses"] = [{"stdout_json": {"statusCheckRollup": r}} for r in responses]
        return view

    def polls(self):
        return sum(1 for a in self.gh_argv() if a[:2] == ["pr", "view"])

    def test_green_rollup_exits_0(self):
        self.scenario([self.rollup(GREEN)])
        p = self.ok("checks", "34")
        self.assertIn("checks green", p.stdout)
        self.assertEqual(self.gh_argv(), [["pr", "view", "34", "--json", "statusCheckRollup"]])

    def test_pending_without_wait_is_one_poll_and_exit_8(self):
        self.scenario([self.rollup(PENDING)])
        p = self.fails(8, "checks", "34", "--json")
        data = json.loads(p.stdout)
        self.assertEqual(data["state"], "pending")
        self.assertEqual(data["pending"], ["build", "ci/legacy"])
        self.assertEqual(self.polls(), 1)

    def test_wait_polls_until_green(self):
        self.scenario([self.rollup(PENDING, PENDING, GREEN)])
        self.ok("checks", "34", "--wait")
        self.assertEqual(self.polls(), 3)

    def test_wait_gives_up_pending_at_the_timeout(self):
        self.scenario([self.rollup(PENDING)])
        p = self.fails(8, "checks", "34", "--wait", "--timeout", "0.05")
        self.assertIn("checks pending after", p.stdout)
        self.assertGreaterEqual(self.polls(), 1)

    def test_failure_names_the_checks_and_prints_the_failed_run_log(self):
        failing = [check_run("test", conclusion="FAILURE", url="https://github.com/o/r/actions/runs/987/job/654"),
                   status_context("ci/legacy", "ERROR"), check_run("lint"),
                   check_run("slow", status="QUEUED", conclusion="")]
        log = "\n".join(f"log line {i}" for i in range(1, 71)) + "\n"
        self.scenario([self.rollup(failing), {"match": ["run", "view", "987", "--log-failed"], "stdout": log}])
        p = self.fails(1, "checks", "34", "--wait")
        self.assertEqual(self.polls(), 1)
        self.assertIn(["run", "view", "987", "--log-failed"], self.gh_argv())
        self.assertIn("checks failed: test (failure), ci/legacy (error)", p.stdout)
        lines = p.stdout.splitlines()
        self.assertIn("log line 11", lines)
        self.assertIn("log line 70", lines)
        self.assertNotIn("log line 10", lines)
        self.assertIn("checks failed on PR #34: test, ci/legacy", p.stderr)

    def test_cancelled_counts_as_failed(self):
        self.scenario([self.rollup([check_run("build", conclusion="CANCELLED"), check_run("lint")])])
        p = self.fails(1, "checks", "34", "--wait")
        self.assertIn("build (cancelled)", p.stdout)
        self.assertEqual(self.polls(), 1)
        self.assertFalse(any(a[:2] == ["run", "view"] for a in self.gh_argv()))

    def test_no_checks_after_six_polls_warns_and_exits_0(self):
        self.scenario([self.rollup([])])
        p = self.ok("checks", "34", "--wait")
        self.assertIn("no checks registered", p.stderr)
        self.assertEqual(self.polls(), 6)


# =========================================================================== the review gate
# What `harness-pr ready` and `harness-pr land` read to find the latest harness review and whether a commit came after
# it, all through the REST API (gh api, with any flags such as --paginate, but these exact endpoint paths):
#   repos/o/r/pulls/34/reviews                  [{id, user, body, state, submitted_at, commit_id}]
#   repos/o/r/pulls/34/reviews/<id>/comments    that review's inline comments [{path, line, body, ...}]
#   repos/o/r/pulls/34/commits                  [{sha, commit: {author: {date}, committer: {date}}}]
# A harness review is one whose body carries MARKER: `harness-pr review` posts "Harness review (<model>): N findings,
# h high, m medium, l low." with the findings not on a changed line listed in the body ("- `path:line` [severity]
# body") and the rest as inline comments ("[severity] body"), or, with no findings, "Harness review: no findings.
# Checked: ...".
PULL = "repos/o/r/pulls/34"


def at(hour):
    return f"2026-10-12T{hour:02d}:00:00Z"


def harness_review(rid, when, inline=(), body_only=(), model="fable", summary=None):
    """(the review, its inline comments) as `harness-pr review` posts them; a finding: (path, line, severity, body).
    An inline finding whose severity is None is posted with no `[severity]` prefix. `summary`, when given, is the
    (total, high, medium, low) the summary line declares instead of the counts of the listed findings; a string is
    the summary line verbatim (an older format, say), and "" leaves the summary line out."""
    findings = list(inline) + list(body_only)
    if not findings and summary is None:
        body = f"Harness review: no findings. Checked: the diff against the ticket's definition of done\n\n{MARKER}"
    else:
        counts = {s: sum(1 for f in findings if f[2] == s) for s in ("high", "medium", "low")}
        total = len(findings)
        if isinstance(summary, tuple):
            total, counts["high"], counts["medium"], counts["low"] = summary
        if isinstance(summary, str):
            parts = [summary] if summary else []
        else:
            parts = [f"Harness review ({model}): {total} finding{'s' if total != 1 else ''}, "
                     f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low."]
        if body_only:
            parts.append("\n".join(["Not on a changed line:"] + [f"- `{p}:{n}` [{s}] {b}" for p, n, s, b in body_only]))
        parts.append(MARKER)
        body = "\n\n".join(parts)
    review = {"id": rid, "user": {"login": "rjeskuri"}, "body": body, "state": "COMMENTED", "submitted_at": when,
              "commit_id": "c" * 40}
    comments = [{"id": rid * 100 + i, "pull_request_review_id": rid, "user": {"login": "rjeskuri"}, "path": p,
                 "line": n, "original_line": n, "side": "RIGHT",
                 "body": (f"{b}" if s is None else f"[{s}] {b}") + f"\n\n{MARKER}", "created_at": when}
                for i, (p, n, s, b) in enumerate(inline)]
    return review, comments


def owner_review(rid, when, body):
    return {"id": rid, "user": {"login": "pal"}, "body": body, "state": "COMMENTED", "submitted_at": when,
            "commit_id": "c" * 40}, []


def pr_commit(char, when):
    return {"sha": char * 40, "commit": {"message": f"Commit {char}", "author": {"name": "Test", "date": when},
                                         "committer": {"name": "Test", "date": when}}}


def gate_calls(reviews=(), commits=(), pull=PULL):
    """fakegh entries for the gate's reads: reviews is a list of (review, comments); `pull` the PR's endpoint."""
    calls = [{"contains": ["api", f"{pull}/reviews/{r['id']}/comments"], "stdout_json": c} for r, c in reviews]
    calls.append({"contains": ["api", f"{pull}/reviews"], "stdout_json": [r for r, _ in reviews]})
    calls.append({"contains": ["api", f"{pull}/commits"], "stdout_json": list(commits)})
    return calls


FROBS = ("widget.py", 2, "high", "Frobs only once on empty input.")
NOTES = ("docs/notes.md", 3, "low", "Mention the flag in the notes.")
OLD_NIT = ("widget.py", 9, "low", "Old nit about naming.")
OFF_BY_ONE = ("lib/frob.py", 41, "medium", "Off by one in the loop bound.")
README = ("README.md", 1, "high", "Document the new flag.")
TOKEN = ("src/app.py", 5, "high", "Leaks the token into the log.")
LIMIT = ("docs/x.md", 7, "medium", "Explain the limit.")
CAP = ("lib/cap.py", 17, "medium", "Cap is never checked on retry.")
TYPO = ("docs/guide.md", 22, "low", "Typo in the heading of the guide.")
UNMARKED = ("lib/gate.py", 12, None, "Returns before the lock is released.")     # no [severity] prefix at all
BLOCKER = ("lib/gate.py", 30, "blocker", "Swallows the gh error silently.")      # a severity the tool never posts
# name: (reviews, commits, the findings a refusal names or None for proceed, findings it must not name)
# Only high and medium findings refuse (#255); low ones never do, and a refusal does not name them.
GATE_CASES = {
    "findings and no commit since": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES])], [pr_commit("a", at(9))],
        [FROBS], [NOTES]),
    "only low findings and no commit since": (
        [harness_review(101, at(10), inline=[OLD_NIT], body_only=[NOTES])], [pr_commit("a", at(9))],
        None, []),
    "a medium finding in the body only": (
        [harness_review(101, at(10), inline=[OLD_NIT], body_only=[CAP, NOTES])], [pr_commit("a", at(9))],
        [CAP], [OLD_NIT, NOTES]),
    "an inline comment without a severity": (
        [harness_review(101, at(10), inline=[UNMARKED], body_only=[NOTES], summary=(2, 0, 0, 2))],
        [pr_commit("a", at(9))], [UNMARKED], [NOTES]),
    "an inline comment with an unknown severity": (
        [harness_review(101, at(10), inline=[OLD_NIT, BLOCKER], summary=(2, 0, 0, 2))],
        [pr_commit("a", at(9))], [BLOCKER], [OLD_NIT]),
    "the summary counts more high than are listed": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES], summary=(3, 2, 0, 1), model="opus")],
        [pr_commit("a", at(9))], [FROBS], [NOTES]),
    "only low findings listed, the summary counts a medium": (
        [harness_review(101, at(10), inline=[OLD_NIT], body_only=[TYPO], summary=(3, 0, 1, 2), model="sonnet")],
        [pr_commit("a", at(9))], [], [OLD_NIT, TYPO]),
    "the summary counts one high, none listed": (
        [harness_review(101, at(10), summary=(1, 1, 0, 0), model="opus")], [pr_commit("a", at(9))], [], []),
    "the summary counts more low than are listed": (
        [harness_review(101, at(10), inline=[OLD_NIT], summary=(3, 0, 0, 3))], [pr_commit("a", at(9))],
        None, []),
    "a commit after the findings": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES])],
        [pr_commit("a", at(9)), pr_commit("b", at(11))],
        None, []),
    "only low findings, a commit after them": (
        [harness_review(101, at(10), inline=[OLD_NIT], body_only=[TYPO])],
        [pr_commit("a", at(9)), pr_commit("b", at(11))],
        None, []),
    "an older review with findings, the latest clean": (
        [harness_review(101, at(10), inline=[TOKEN]), harness_review(102, at(12))], [pr_commit("a", at(9))],
        None, []),
    "an older clean review, the latest with findings (listed newest first)": (
        [harness_review(102, at(12), inline=[OFF_BY_ONE]), harness_review(101, at(10))],
        [pr_commit("a", at(9)), pr_commit("b", at(11))], [OFF_BY_ONE], []),
    "no harness review, only the owner's": (
        [owner_review(201, at(12), "Please rename the widget.")], [pr_commit("a", at(9))], None, []),
    "no review at all": ([], [pr_commit("a", at(9))], None, []),
    "the owner reviewed after the harness findings": (
        [harness_review(101, at(10), inline=[TOKEN]), owner_review(201, at(12), "LGTM")], [pr_commit("a", at(9))],
        [TOKEN], []),
    "two reviews with findings, a commit between them": (
        [harness_review(101, at(10), inline=[TOKEN]),
         harness_review(102, at(12), inline=[OFF_BY_ONE, NOTES], body_only=[README, TYPO])],
        [pr_commit("a", at(9)), pr_commit("b", at(11))], [OFF_BY_ONE, README], [TOKEN, NOTES, TYPO]),
    "every finding in the review body": (
        [harness_review(101, at(10), body_only=[LIMIT])], [pr_commit("a", at(9))], [LIMIT], []),
    # The listed findings' severities decide, whatever the summary line says or whether there is one at all.
    "an older summary without per-severity counts": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[CAP], summary="Harness review (opus): 2 findings.")],
        [pr_commit("a", at(9))], [FROBS, CAP], []),
    # An older summary's total beyond the findings listed counts as that many more high or medium (fail closed).
    "an older summary counting more findings than are listed": (
        [harness_review(101, at(10), body_only=[NOTES], summary="Harness review (opus): 3 findings.")],
        [pr_commit("a", at(9))], [], [NOTES]),
    "the summary counts no high or medium, a high and a medium listed": (
        [harness_review(101, at(10), inline=[OFF_BY_ONE], body_only=[README], summary=(2, 0, 0, 2))],
        [pr_commit("a", at(9))], [OFF_BY_ONE, README], []),
    "no summary line, a high listed": (
        [harness_review(101, at(10), inline=[TOKEN], body_only=[TYPO], summary="")],
        [pr_commit("a", at(9))], [TOKEN], [TYPO]),
    # #280: the per-severity counts are the ones autoland.review_counts reads, so a summary line it rejects (no final
    # period, text after the counts) has none: the total less the findings listed counts as high (fail closed).
    "a summary without the final period, only low findings listed": (
        [harness_review(101, at(10), body_only=[NOTES],
                        summary="Harness review (opus): 3 findings, 0 high, 0 medium, 3 low")],
        [pr_commit("a", at(9))], [], [NOTES]),
    "a summary with text after the counts, only low findings listed": (
        [harness_review(101, at(10), inline=[OLD_NIT], body_only=[TYPO],
                        summary="Harness review (sonnet): 4 findings, 0 high, 0 medium, 4 low. Checked the diff.")],
        [pr_commit("a", at(9))], [], [OLD_NIT, TYPO]),
    "a summary without the final period, a high listed": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES],
                        summary="Harness review (fable): 3 findings, 1 high, 0 medium, 2 low")],
        [pr_commit("a", at(9))], [FROBS], [NOTES]),
    "a one-finding summary without the final period, none listed": (
        [harness_review(101, at(10), summary="Harness review (opus): 1 finding, 0 high, 0 medium, 1 low")],
        [pr_commit("a", at(9))], [], []),
}
# The refused cases whose summary declares more high plus medium findings than the review lists: how many more.
GATE_NOT_LISTED = {
    "the summary counts more high than are listed": 1,
    "only low findings listed, the summary counts a medium": 1,
    "the summary counts one high, none listed": 1,
    "an older summary counting more findings than are listed": 2,
    "a summary without the final period, only low findings listed": 2,
    "a summary with text after the counts, only low findings listed": 2,
    "a summary without the final period, a high listed": 1,
    "a one-finding summary without the final period, none listed": 1,
}
# Refused cases: (name, the latest harness review's submitted_at, n) where n in "has <n> high or medium finding(s)"
# counts the high and medium findings listed plus the GATE_NOT_LISTED count.
GATE_REFUSAL_COUNTS = [
    ("findings and no commit since", at(10), 1),
    ("a medium finding in the body only", at(10), 1),
    ("an inline comment without a severity", at(10), 1),
    ("an older clean review, the latest with findings (listed newest first)", at(12), 1),
    ("two reviews with findings, a commit between them", at(12), 2),
    ("the summary counts one high, none listed", at(10), 1),
    ("only low findings listed, the summary counts a medium", at(10), 1),
    ("the summary counts more high than are listed", at(10), 2),
    ("an older summary without per-severity counts", at(10), 2),
    ("an older summary counting more findings than are listed", at(10), 2),
    ("a summary without the final period, only low findings listed", at(10), 2),
    ("a summary with text after the counts, only low findings listed", at(10), 2),
    ("a summary without the final period, a high listed", at(10), 2),
    ("a one-finding summary without the final period, none listed", at(10), 1),
]
# The proceed cases in which ready has nothing to warn about: a newer commit cleared the findings, the latest harness
# review is clean, or there is no harness review. Ready prints nothing on stderr for them.
GATE_QUIET = ("a commit after the findings", "only low findings, a commit after them",
              "an older review with findings, the latest clean", "no harness review, only the owner's",
              "no review at all")


GATE_FAILURE_TEXT = "HTTP 502: Bad Gateway (fake)"


def gate_failure(entry):
    return dict(entry, stderr=GATE_FAILURE_TEXT + "\n", exit=1)


# Each read the gate makes, failing; placed first in a scenario so it wins over the gate_calls answer.
GATE_API_FAILURES = {
    "reviews": gate_failure({"contains": ["api", f"{PULL}/reviews"]}),
    "review comments": gate_failure({"contains": ["api", f"{PULL}/reviews/101/comments"]}),
    "commits": gate_failure({"contains": ["api", f"{PULL}/commits"]}),
}
GATE_READ_FAILURES = dict(GATE_API_FAILURES, **{"repo view": gate_failure({"match": ["repo", "view"]})})


class GateAsserts:
    def assertNamesFindings(self, p, named, not_named):
        said = p.stdout + p.stderr
        for path, line, severity, body in named:
            self.assertIn(f"{path}:{line}", said)
            self.assertIn(body[:20], said, (severity, body, said))
        for path, line, _s, body in not_named:
            self.assertNotIn(f"{path}:{line}", said)
            self.assertNotIn(body[:20], said)

    def assertNotListedCount(self, name, p):
        """A refusal adds "K more high or medium not listed (see the review)" exactly when the summary declares K
        more high plus medium findings than the review lists."""
        if name in GATE_NOT_LISTED:
            self.assertIn(f"{GATE_NOT_LISTED[name]} more high or medium not listed (see the review)", p.stderr)
        else:
            self.assertNotIn("not listed", p.stdout + p.stderr)


# =========================================================================== ready
class Ready(PrTest):
    def view(self, closing):
        return {"match": ["pr", "view", "34", "--json", "url,title,closingIssuesReferences"],
                "stdout_json": {"url": PR_URL, "title": "Add the widget (#12)",
                                "closingIssuesReferences": [{"number": n, "url": f"https://github.com/o/r/issues/{n}"}
                                                            for n in closing]}}

    def scenario(self, calls, default=None):
        super().scenario(calls + [REPO_VIEW] + gate_calls(commits=[pr_commit("a", at(9))]), default)

    def test_ready_marks_the_pr_and_the_closing_ticket_and_notifies(self):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
        p = self.ok("ready", "34")
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        self.assertEqual([a for a in self.gh_argv() if a[0] not in ("api", "repo")],     # the gate's reads aside
                         [["pr", "ready", "34"], ["pr", "view", "34", "--json", "url,title,closingIssuesReferences"]])
        self.assertEqual(self.stub_calls("harness-board"), [
            ["status", "12", "in-review", "--comment", f"PR #34 ready for review: {PR_URL}", "--repo", self.main]])
        sent = self.notifications()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "terminal-notifier")
        self.assertIn("proj PR #34 ready for review: Add the widget (#12)", sent[0])

    def test_ready_takes_the_ticket_from_the_flag(self):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([])])
        self.ok("ready", "34", "--ticket", "7")
        self.assertEqual([a[:4] for a in self.stub_calls("harness-board")], [["status", "7", "in-review", "--comment"]])

    def test_ready_reports_a_failed_status_change(self):
        self.outputs(**{"harness-board": {"status": {"stderr": "harness-board: gh issue edit failed\n", "exit": 1}}})
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
        p = self.pr("ready", "34")
        self.assertOneLineError(p, "PR #34 is ready, but", "gh issue edit failed")
        self.assertEqual(len(self.notifications()), 1)

    def test_ready_with_the_real_board_mirrors_in_review_to_the_configured_project(self):
        """#64: no stubs, HARNESS_PROJECT set: the real harness-board sets the ticket's project item to "In review",
        on the URL gh reports for the issue (not one built from the remote)."""
        url = "https://ghe.example.com/o/r/issues/12"
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12]),
                       {"match": ["issue", "view", "12"], "stdout_json": {
                           "number": 12, "url": url, "labels": [{"name": "task"}, {"name": "status:in-progress"}]}},
                       {"match": ["issue", "edit", "12"], "stdout": url + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": url + "#issuecomment-1\n"},
                       {"match": ["project"], "stdout": ""}])
        p = self.ok("ready", "34", stubs=False, HARNESS_PROJECT="rjeskuri/7")
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        argvs = self.gh_argv()
        self.assertIn(["issue", "edit", "12", "--add-label", "status:in-review", "--remove-label",
                       "status:in-progress"], argvs)
        projects = [a for a in argvs if a[:1] == ["project"]]
        self.assertEqual(len(projects), 1, projects)
        self.assertEqual(project_argv(projects[0]), ("item-edit", ["7"], {"owner": "rjeskuri", "url": url,
                                                                           "field": "Status", "value": "In review"}))

    # ------------------------------------------------- #69: harness-board's Projects warning reaches the user
    def real_board_scenario(self, project_call):
        """ready 34 closing #12 with the real harness-board; `project_call` answers any `gh project ...`."""
        url = "https://github.com/o/r/issues/12"
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12]),
                       {"match": ["issue", "view", "12"], "stdout_json": {
                           "number": 12, "url": url, "labels": [{"name": "task"}, {"name": "status:in-progress"}]}},
                       {"match": ["issue", "edit", "12"], "stdout": url + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": url + "#issuecomment-1\n"},
                       dict(project_call, match=["project"])])

    def test_ready_passes_the_board_warning_through_when_the_project_item_edit_fails(self):
        """Real harness-board, a failing `gh project item-edit`: ready still exits 0 with its usual stdout, and
        harness-board's one warning line, naming that board and gh's error, is on ready's stderr exactly once. The
        board comes from the environment in one case and from ~/.claude/settings.json in the other."""
        cases = [("rjeskuri/7", "HTTP 403: missing the project scope (token-d4)", "env"),
                 ("some-org/42", "could not find the item (token-e5)", "settings")]
        for project, why, source in cases:
            with self.subTest(project=project, source=source):
                self.real_board_scenario({"stderr": why + "\n", "exit": 1})
                settings = os.path.join(self.home, ".claude", "settings.json")
                if source == "settings":
                    write(settings, json.dumps({"env": {"HARNESS_PROJECT": project}}))
                    p = self.ok("ready", "34", stubs=False)
                else:
                    p = self.ok("ready", "34", stubs=False, HARNESS_PROJECT=project)
                owner, number = project.split("/")
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertEqual(p.stderr.splitlines(), [f"harness-board: warning: the Projects board {owner}/{number} "
                                                         f"was not updated: gh project item-edit {number} failed: "
                                                         f"{why}"], p.stderr)
                self.assertIn(["issue", "edit", "12", "--add-label", "status:in-review", "--remove-label",
                               "status:in-progress"], self.gh_argv(), "the labels still changed")
                self.assertEqual(len(self.notifications()), 1 if source == "env" else 2)

    def test_ready_shows_only_the_warning_lines_of_a_successful_board_call(self):
        """A board that exits 0 with stderr: each `harness-board: warning:` line reaches ready's stderr once, in
        order; its other stderr lines stay hidden. A board with only other stderr leaves ready's stderr empty."""
        warnings = ["harness-board: warning: the Projects board acme/3 was not updated: gh project item-edit 3 "
                    "failed: stub says no (token-f6)",
                    "harness-board: warning: HARNESS_PROJECT='x' is not OWNER/NUMBER; the Projects board is not "
                    "updated (token-g7)"]
        chatter = ["harness-board: note: chatter (token-h8)", "warning: chatter without the prefix (token-i9)",
                   "gh: some progress line (token-j0)"]
        runs = [([chatter[0], warnings[0], chatter[1]], [warnings[0]]),
                ([warnings[1], chatter[2], warnings[0]], [warnings[1], warnings[0]]),
                (chatter, [])]
        for stderr, shown in runs:
            with self.subTest(stderr=stderr):
                self.outputs(**{"harness-board": {"status": {"stderr": "\n".join(stderr) + "\n", "exit": 0}}})
                self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
                p = self.ok("ready", "34")
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertEqual(p.stderr.splitlines(), shown, p.stderr)
                self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])

    def test_ready_stderr_is_unchanged_without_the_project_setting(self):
        """No HARNESS_PROJECT anywhere, real harness-board: no project call and ready's stderr is empty as before."""
        self.real_board_scenario({"stderr": "should never run\n", "exit": 1})
        p = self.ok("ready", "34", stubs=False)
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        self.assertEqual(p.stderr, "")
        self.assertEqual([a for a in self.gh_argv() if a[:1] == ["project"]], [])


class ReadyGate(GateAsserts, PrTest):
    """`harness-pr ready P` refuses a PR whose latest harness review has findings and no commit after it."""

    def run_case(self, reviews, commits, *flags):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, Ready.view(self, [12]), REPO_VIEW]
                      + gate_calls(reviews, commits))
        if os.path.exists(self.notify_log):
            os.unlink(self.notify_log)
        return self.pr("ready", "34", *flags)

    def assertRefused(self, p):
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "ready"]], self.gh_argv())   # not marked ready
        self.assertEqual(self.stub_calls("harness-board"), [])                                      # no status change
        self.assertEqual(self.notifications(), [])

    def test_ready_follows_the_latest_harness_review_and_the_commits_after_it(self):
        for name, (reviews, commits, named, not_named) in GATE_CASES.items():
            with self.subTest(name):
                p = self.run_case(reviews, commits)
                if named is None:
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn(["pr", "ready", "34"], self.gh_argv())
                    self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])
                    if name in GATE_QUIET:
                        self.assertEqual(p.stderr, "")
                else:
                    self.assertRefused(p)
                    self.assertNamesFindings(p, named, not_named)
                    self.assertNotListedCount(name, p)

    def test_ready_refusal_counts_the_high_and_medium_findings_and_says_what_to_do(self):
        """The refusal: "PR #P: the latest harness review (<submitted_at>) has <n> high or medium findings and no
        commit after it: <each>; fix them, push, then harness-pr ready P", n counting only high and medium, listed or
        counted by the summary beyond those listed."""
        for name, when, n in GATE_REFUSAL_COUNTS:
            with self.subTest(name):
                reviews, commits, _named, _ = GATE_CASES[name]
                p = self.run_case(reviews, commits)
                self.assertRefused(p)
                self.assertRegex(p.stderr, re.escape(f"PR #34: the latest harness review ({when}) has {n} high or "
                                                     "medium finding") + r"s? and no commit after it: ")
                self.assertIn("; fix them, push, then harness-pr ready 34", p.stderr)

    def test_ready_says_how_many_high_or_medium_findings_the_review_does_not_list(self):
        """K is the summary's high plus medium less the high and medium listed; extra low ones in the summary never
        add the line, and the low ones listed are not counted against it."""
        cases = [  # (inline, body_only, summary (total, high, medium, low), the model named, K or 0 for no line)
            ([FROBS], [NOTES], (3, 2, 0, 1), "fable", 1),
            ([OLD_NIT], [NOTES], (5, 1, 2, 2), "opus", 3),
            ([TOKEN], [NOTES, TYPO], (8, 3, 2, 3), "sonnet", 4),
            ([FROBS, OLD_NIT], [CAP], (4, 1, 2, 1), "opus", 1),
            ([FROBS], [NOTES], (6, 1, 0, 5), "opus", 0),
            ([], [], (1, 1, 0, 0), "sonnet", 1),
            ([], [], (1, 0, 1, 0), "fable", 1),
            ([], [], (2, 0, 2, 0), "opus", 2),
        ]
        for inline, body_only, summary, model, k in cases:
            with self.subTest(summary=summary, model=model):
                reviews = [harness_review(101, at(10), inline=inline, body_only=body_only, summary=summary,
                                          model=model)]
                p = self.run_case(reviews, [pr_commit("a", at(9))])
                self.assertRefused(p)
                listed = [f for f in inline + body_only if f[2] in ("high", "medium")]
                lows = [f for f in inline + body_only if f[2] == "low"]
                self.assertNamesFindings(p, listed, lows)
                if k:
                    self.assertIn(f"{k} more high or medium not listed (see the review)", p.stderr)
                else:
                    self.assertNotIn("not listed", p.stderr)

    def test_ready_warns_about_the_low_findings_it_lets_through(self):
        """Only low findings and no commit since: ready marks the PR, sets in-review and notifies as usual, and
        prints one line on stderr: "harness-pr: warning: PR #P: <n> low findings left for the owner: <each>"."""
        cases = [([OLD_NIT], [NOTES]), ([], [TYPO]), ([OLD_NIT, NOTES], [TYPO]), ([NOTES], [])]
        for inline, body_only in cases:
            lows = inline + body_only
            with self.subTest(lows=[f[0] for f in lows]):
                p = self.run_case([harness_review(101, at(10), inline=inline, body_only=body_only)],
                                  [pr_commit("a", at(9))])
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertIn(["pr", "ready", "34"], self.gh_argv())
                self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])
                self.assertEqual(len(self.notifications()), 1)
                lines = p.stderr.splitlines()
                self.assertEqual(len(lines), 1, p.stderr)
                self.assertRegex(lines[0], r"^harness-pr: warning: PR #34: " + str(len(lows))
                                 + r" low findings? left for the owner: ")
                for path, line, _s, body in lows:
                    self.assertIn(f"{path}:{line}", lines[0])
                    self.assertIn(body[:20], lines[0])

    def test_ready_has_no_override(self):
        reviews, commits, named, _ = GATE_CASES["findings and no commit since"]
        p = self.run_case(reviews, commits)
        self.assertRefused(p)
        self.assertNamesFindings(p, named, [])
        self.assertRefused(self.run_case(reviews, commits, "--accept-findings"))

    def test_ready_fails_closed_when_a_gate_read_fails(self):
        # The PR would pass the gate (a commit after the findings), so only the failed read can refuse it.
        reviews, commits, named, _ = GATE_CASES["a commit after the findings"]
        self.assertIsNone(named)
        for what, failing in GATE_READ_FAILURES.items():
            with self.subTest(what):
                self.scenario([failing, {"match": ["pr", "ready", "34"], "stdout": ""}, Ready.view(self, [12]),
                               REPO_VIEW] + gate_calls(reviews, commits))
                if os.path.exists(self.notify_log):
                    os.unlink(self.notify_log)
                p = self.pr("ready", "34")
                self.assertRefused(p)
                self.assertIn(GATE_FAILURE_TEXT, p.stderr)
                self.assertTrue(any(a[:1] == ["api"] or a[:2] == ["repo", "view"] for a in self.gh_argv()))


# =========================================================================== comments
CONVERSATION = graphql(
    threads=[thread(False, gql_comment("rjeskuri", "Rename this.", "2026-10-12T10:00:00Z", line=3)),
             thread(True, gql_comment("rjeskuri", "Old nit.", "2026-10-10T10:00:00Z", line=9)),
             thread(False, gql_comment("rjeskuri", f"[high] Frobs once.\n\n{MARKER}", "2026-10-11T10:00:00Z",
                                       line=2))],
    reviews=[{"author": {"login": "rjeskuri"}, "body": f"Harness review (fable): 1 finding\n\n{MARKER}",
              "state": "COMMENTED", "createdAt": "2026-10-11T10:00:00Z", "url": "https://github.com/o/r/pull/34#r1"},
             {"author": {"login": "pal"}, "body": "Looks fine overall.", "state": "COMMENTED",
              "createdAt": "2026-10-12T09:00:00Z", "url": "https://github.com/o/r/pull/34#r2"},
             {"author": {"login": "rjeskuri"}, "body": "", "state": "COMMENTED",
              "createdAt": "2026-10-12T10:00:00Z", "url": "https://github.com/o/r/pull/34#r3"}],
    comments=[{"author": {"login": "rjeskuri"}, "body": f"Harness review: no findings. Checked: x\n\n{MARKER}",
               "createdAt": "2026-10-11T08:00:00Z", "url": "https://github.com/o/r/pull/34#c1"},
              {"author": {"login": "rjeskuri"}, "body": "Please also update the docs.",
               "createdAt": "2026-10-09T08:00:00Z", "url": "https://github.com/o/r/pull/34#c2"}])


class Comments(PrTest):
    def setUp(self):
        super().setUp()
        self.scenario([REPO_VIEW, {"match": ["api", "graphql"], "stdout_json": CONVERSATION}])

    def test_comments_drop_the_harness_reviews(self):
        data = json.loads(self.ok("comments", "34", "--json").stdout)
        self.assertEqual([[c["body"] for c in t["comments"]] for t in data["threads"]],
                         [["Rename this."], ["Old nit."]])
        self.assertEqual([t["resolved"] for t in data["threads"]], [False, True])
        self.assertEqual(data["threads"][0]["path"], "widget.py")
        self.assertEqual(data["threads"][0]["line"], 3)
        self.assertEqual([r["body"] for r in data["reviews"]], ["Looks fine overall."])
        self.assertEqual([c["body"] for c in data["comments"]], ["Please also update the docs."])
        query = self.gh_argv()[1]
        self.assertEqual(query[:2], ["api", "graphql"])
        for piece in ("owner=o", "repo=r", "number=34"):
            self.assertIn(piece, query)
        self.assertEqual(query[query.index("number=34") - 1], "-F")
        text = next(a for a in query if a.startswith("query="))
        for field in ("reviewThreads(first: 100)", "isResolved", "reviews(first: 50)", "comments(first: 100)"):
            self.assertIn(field, text)

    def test_unresolved_keeps_unresolved_threads_only(self):
        data = json.loads(self.ok("comments", "34", "--unresolved", "--json").stdout)
        self.assertEqual([t["comments"][0]["body"] for t in data["threads"]], ["Rename this."])
        self.assertEqual(data["reviews"], [])
        self.assertEqual(data["comments"], [])

    def test_since_keeps_newer_items(self):
        data = json.loads(self.ok("comments", "34", "--since", "2026-10-11T00:00:00Z", "--json").stdout)
        self.assertEqual([t["comments"][0]["body"] for t in data["threads"]], ["Rename this."])
        self.assertEqual([r["body"] for r in data["reviews"]], ["Looks fine overall."])
        self.assertEqual(data["comments"], [])

    def test_markdown(self):
        out = self.ok("comments", "34").stdout
        self.assertIn("## Unresolved threads (1)", out)
        self.assertIn("`widget.py:3`", out)
        self.assertIn("**rjeskuri** (2026-10-12): Rename this.", out)
        self.assertIn("## Reviews (1)", out)
        self.assertNotIn(MARKER, out)
        self.assertNotIn("Frobs once", out)


# =========================================================================== awaiting
def iso_ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Awaiting(PrTest):
    def prs(self):
        return [{"number": 33, "title": "Draft work", "isDraft": True, "createdAt": iso_ago(hours=1),
                 "url": "https://github.com/o/r/pull/33", "headRefName": "t/14-draft", "closingIssuesReferences": [],
                 "statusCheckRollup": []},
                {"number": 34, "title": "Add the widget (#12)", "isDraft": False, "createdAt": iso_ago(days=2, hours=1),
                 "url": PR_URL, "headRefName": BRANCH, "closingIssuesReferences": [{"number": 12}],
                 "statusCheckRollup": GREEN},
                {"number": 35, "title": "Fix the thing", "isDraft": False, "createdAt": iso_ago(hours=5),
                 "url": "https://github.com/o/r/pull/35", "headRefName": "t/15-fix-the-thing",
                 "closingIssuesReferences": [], "statusCheckRollup": PENDING}]

    def calls(self):
        return [REPO_VIEW, {"match": ["pr", "list"], "stdout_json": self.prs()},
                {"contains": ["graphql", "number=34"], "stdout_json": CONVERSATION},
                {"contains": ["graphql", "number=35"], "stdout_json": graphql()}]

    def test_awaiting_lists_non_draft_prs_with_ticket_age_checks_and_threads(self):
        self.scenario(self.calls())
        rows = json.loads(self.ok("awaiting", "--json").stdout)
        self.assertEqual([(r["number"], r["ticket"], r["age"], r["checks"], r["unresolved"]) for r in rows],
                         [(34, 12, "2d", "green", 1), (35, 15, "5h", "pending", 0)])
        argv = self.gh_argv()
        self.assertIn(["pr", "list", "--author", "@me", "--state", "open", "--json",
                       "number,title,isDraft,createdAt,url,headRefName,closingIssuesReferences,statusCheckRollup"],
                      argv)
        self.assertFalse(any("number=33" in a for a in argv))
        out = self.ok("awaiting").stdout
        self.assertIn("UNRESOLVED", out.splitlines()[0])
        self.assertIn("#34", out)
        self.assertNotIn("#33", out)

    def test_awaiting_all_projects_walks_the_registry(self):
        self.scenario(self.calls())
        bare = self.mk("no-remote")
        self.git(bare, "init", "-q")
        write(os.path.join(self.home, ".claude", "harness", "projects.json"), json.dumps({"version": 1, "projects": [
            {"path": self.main, "name": "proj"}, {"path": bare, "name": "no-remote"},
            {"path": os.path.join(self.tmp, "gone"), "name": "gone"}]}))
        rows = json.loads(self.ok("awaiting", "--all-projects", "--json", cwd=self.tmp).stdout)
        self.assertEqual([(r["project"], r["number"]) for r in rows], [("proj", 34), ("proj", 35)])
        self.assertEqual(sum(1 for a in self.gh_argv() if a[:2] == ["pr", "list"]), 1)


# =========================================================================== ultra
class Ultra(PrTest):
    def claude(self, stdout="", stderr="", code=0):
        return {"CLAUDE_STDOUT": write(os.path.join(self.tmp, "claude.out"), stdout),
                "CLAUDE_STDERR": write(os.path.join(self.tmp, "claude.err"), stderr), "CLAUDE_EXIT": str(code)}

    def budgets_path(self):
        return os.path.join(self.home, ".claude", "harness", "budgets.json")

    def test_ultra_saves_the_json_and_counts_findings(self):
        out = json.dumps({"summary": "done", "findings": [{"severity": "high"}, {"severity": "medium"},
                                                          {"severity": "high"}, {"severity": "low"}]})
        p = self.ok("ultra", "34", "--post", **self.claude(stdout=out))
        self.assertEqual(self.stub_calls("claude"), [["ultrareview", "34", "--json", "--timeout", "30", "--post"]])
        with open(self.state_path("ultra", "34.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), out)
        self.assertIn("4 findings (2 high, 1 medium, 1 low)", p.stdout)
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_ultra_billing_error_marks_the_cloud_budget_exhausted(self):
        write(self.budgets_path(), json.dumps({"api": {"exhausted_until": None, "reason": ""}}))
        p = self.pr("ultra", "34", **self.claude(stderr="Starting\nError: Your credit balance is too low.\n", code=1))
        self.assertOneLineError(p, "credit balance is too low", "exhausted until")
        self.assertEqual(self.stub_calls("claude"), [["ultrareview", "34", "--json", "--timeout", "30"]])
        with open(self.budgets_path(), encoding="utf-8") as fh:
            budgets = json.load(fh)
        self.assertEqual(budgets["api"], {"exhausted_until": None, "reason": ""})
        self.assertEqual(budgets["cloud"]["reason"], "Error: Your credit balance is too low.")
        until = dt.datetime.fromisoformat(budgets["cloud"]["exhausted_until"].replace("Z", "+00:00"))
        expected = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        self.assertLess(abs((until - expected).total_seconds()), 300)

    def test_ultra_other_failure_leaves_the_budget_alone(self):
        p = self.pr("ultra", "34", **self.claude(stderr="network unreachable\n", code=1))
        self.assertOneLineError(p, "network unreachable")
        self.assertFalse(os.path.exists(self.budgets_path()))


ULTRA_OUT = json.dumps({"summary": "done", "findings": [{"severity": "high"}, {"severity": "low"}]})
WARNING = "harness-pr: warning:"
LIB = os.path.join(os.path.dirname(HERE), "lib")
# What each routing key accepts, in the words of the warning (routing.py's "%s=%r is not one of %s; ignored").
ACCEPTS = {
    "HARNESS_CODE_TIER": "sonnet, opus, fable",
    "HARNESS_CODE_EFFORT": "low, medium, high, xhigh, max",
    "HARNESS_REVIEW_TIER": "sonnet, opus, fable",
    "HARNESS_ULTRAREVIEW": "on, 1, true, yes, off, 0, false, no",
    "HARNESS_WAVE_REVIEW": "sonnet, opus, fable, off, 0, false, no",
}


def off_line(number):
    return f"ultrareview PR #{number}: off for this project (HARNESS_ULTRAREVIEW=off); nothing run"


def bad(key, shown):
    """The exact problem text for `key` given the (stripped) value `shown`."""
    return f"{key}={shown!r} is not one of {ACCEPTS[key]}; ignored"


class UltraSettingBase:
    """Helpers for the HARNESS_ULTRAREVIEW switch: settings files in the main checkout, the cloud budget and the
    saved ultra state, watched the way the Ultra tests watch them."""

    def claude(self, stdout=ULTRA_OUT, stderr="", code=0):
        return {"CLAUDE_STDOUT": write(os.path.join(self.tmp, "claude.out"), stdout),
                "CLAUDE_STDERR": write(os.path.join(self.tmp, "claude.err"), stderr), "CLAUDE_EXIT": str(code)}

    def budgets_path(self):
        return os.path.join(self.home, ".claude", "harness", "budgets.json")

    def settings(self, env, name="settings.json", root=None):
        write(os.path.join(root or self.main, ".claude", name), json.dumps({"env": env}))

    def go(self, number, file_env=None, raw=None, environ=None, flags=(), local=None):
        """A fresh run: call logs cleared, the main checkout's settings files replaced by `file_env` (or the
        exact text `raw`) and `local` (settings.local.json), then `harness-pr ultra <number>`."""
        self.scenario([])
        for name in ("settings.json", "settings.local.json"):
            path = os.path.join(self.main, ".claude", name)
            if os.path.exists(path):
                os.unlink(path)
        if raw is not None:
            write(os.path.join(self.main, ".claude", "settings.json"), raw)
        elif file_env is not None:
            self.settings(file_env)
        if local is not None:
            self.settings(local, name="settings.local.json")
        return self.pr("ultra", str(number), *flags, **(environ or {}), **self.claude())

    def warnings(self, p):
        return [line for line in p.stderr.splitlines() if line.startswith(WARNING)]

    def routing_problems(self, environ):
        """load_routing's problems for the main checkout as it is on disk now, under exactly `environ` as the
        HARNESS_* environment (the oracle for the warning text)."""
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        from harness_lib import routing
        saved = dict(os.environ)
        try:
            for name in [n for n in os.environ if n.startswith("HARNESS_")]:
                del os.environ[name]
            os.environ.update(environ)
            return routing.load_routing(self.main)["problems"]
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def assertWarnings(self, p, problems, environ=None):
        """The `harness-pr: warning:` lines are exactly `problems`, in order, one each; `problems` is hand-written
        and must agree with load_routing's."""
        expected = [WARNING + " " + text for text in problems]
        self.assertEqual([WARNING + " " + t for t in self.routing_problems(environ or {})], expected,
                         "the test's expected problems disagree with load_routing")
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.warnings(p), expected, p.stderr)

    def assertNothingRan(self, p, number):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stdout.strip(), off_line(number))
        self.assertEqual(self.stub_calls("claude"), [])
        self.assertEqual(self.gh_argv(), [])
        self.assertFalse(os.path.exists(self.state_path("ultra", f"{number}.json")))
        self.assertFalse(os.path.exists(self.budgets_path()))

    def assertRan(self, p, number, *flags):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stub_calls("claude"),
                         [["ultrareview", str(number), "--json", "--timeout", "30", *flags]])
        self.assertNotIn("off for this project", p.stdout)
        self.assertIn("2 findings (1 high, 1 low)", p.stdout)
        with open(self.state_path("ultra", f"{number}.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), ULTRA_OUT)


class UltraSetting(UltraSettingBase, PrTest):
    def test_off_runs_nothing_and_says_so(self):
        # Every spelling of off, in the settings file, for a spread of PR numbers; the PR number is in the line.
        for i, value in enumerate(["off", "OFF", "0", "false", "False", "no", " off "]):
            number = 7 + 27 * i
            with self.subTest(value=value):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                self.assertNothingRan(p, number)
                self.assertEqual(self.warnings(p), [])
        # A JSON number 0 in settings.json is off too (the settings reader turns it into "0").
        self.assertNothingRan(self.go(52, {"HARNESS_ULTRAREVIEW": 0}), 52)

    def test_off_with_post_runs_nothing_and_leaves_state_and_budget_alone(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"})
        old = write(self.state_path("ultra", "34.json"), '{"findings": []}')
        budgets = write(self.budgets_path(), json.dumps({"api": {"exhausted_until": None, "reason": ""}}))
        with open(budgets, "rb") as fh:
            before = fh.read()
        # A claude that would hit the billing error if it were called.
        p = self.pr("ultra", "34", "--post", **self.claude(stderr="Error: Your credit balance is too low.\n", code=1))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), off_line(34))
        self.assertEqual(self.stub_calls("claude"), [])
        self.assertEqual(self.gh_argv(), [])
        with open(old, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"findings": []}')
        with open(budgets, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_force_runs_it_anyway(self):
        for number, flags, setting in ((34, (), "off"), (91, ("--post",), "off"), (5, (), "0"),
                                       (12, ("--post",), "false")):
            with self.subTest(number=number, flags=flags, setting=setting):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": setting}, flags=("--force", *flags))
                self.assertRan(p, number, *flags)
                self.assertEqual(self.warnings(p), [])
        # --force with the environment saying off, too.
        p = self.go(63, environ={"HARNESS_ULTRAREVIEW": "off"}, flags=("--force",))
        self.assertRan(p, 63)

    def test_force_with_off_in_the_environment_and_the_settings_file_still_runs(self):
        p = self.go(48, {"HARNESS_ULTRAREVIEW": "off"}, environ={"HARNESS_ULTRAREVIEW": "false"},
                    flags=("--force",))
        self.assertRan(p, 48)
        self.assertEqual(self.warnings(p), [])
        p = self.go(49, {"HARNESS_ULTRAREVIEW": "no"}, environ={"HARNESS_ULTRAREVIEW": "0"},
                    flags=("--post", "--force"))
        self.assertRan(p, 49, "--post")
        self.assertEqual(self.warnings(p), [])

    def test_force_with_invalid_values_still_warns_and_runs(self):
        file_env = {"HARNESS_ULTRAREVIEW": "maybe", "HARNESS_CODE_TIER": "gigantic"}
        p = self.go(34, file_env, flags=("--force",))
        self.assertRan(p, 34)
        self.assertWarnings(p, [bad("HARNESS_CODE_TIER", "gigantic"), bad("HARNESS_ULTRAREVIEW", "maybe")])
        environ = {"HARNESS_ULTRAREVIEW": "sometimes", "HARNESS_WAVE_REVIEW": "huge"}
        p = self.go(35, {"HARNESS_ULTRAREVIEW": "off"}, environ=environ, flags=("--force", "--post"))
        self.assertRan(p, 35, "--post")
        self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", "sometimes"), bad("HARNESS_WAVE_REVIEW", "huge")], environ)

    def test_unset_and_on_run_as_today(self):
        cases = [("unset", None, None, 34, ()), ("settings on", {"HARNESS_ULTRAREVIEW": "on"}, None, 41, ("--post",)),
                 ("settings 1", {"HARNESS_ULTRAREVIEW": "1"}, None, 8, ()),
                 ("settings true", {"HARNESS_ULTRAREVIEW": "True"}, None, 19, ()),
                 ("settings yes", {"HARNESS_ULTRAREVIEW": "yes"}, None, 77, ("--post",)),
                 ("blank is unset", {"HARNESS_ULTRAREVIEW": "  "}, None, 23, ()),
                 ("other keys only", {"HARNESS_CODE_TIER": "opus"}, None, 6, ()),
                 ("environment on", None, "on", 15, ()),
                 ("environment on beats settings off", {"HARNESS_ULTRAREVIEW": "off"}, "on", 29, ("--post",))]
        for label, file_env, environment, number, flags in cases:
            with self.subTest(label):
                extra = {"HARNESS_ULTRAREVIEW": environment} if environment else {}
                p = self.go(number, file_env, environ=extra, flags=flags)
                self.assertRan(p, number, *flags)
                self.assertEqual(self.warnings(p), [])

    def test_invalid_value_warns_and_runs(self):
        for number, value in ((34, "maybe"), (58, "of"), (9, "2")):
            with self.subTest(value=value):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                self.assertRan(p, number)
                self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", value)])
        # From the environment, the same: one warning, and it runs.
        environ = {"HARNESS_ULTRAREVIEW": "sometimes"}
        p = self.go(71, environ=environ)
        self.assertRan(p, 71)
        self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", "sometimes")], environ)

    def test_every_problem_is_one_warning_line_with_load_routings_text(self):
        # (label, file env, environment, expected problems in load_routing's order, whether it is off)
        cases = [
            ("two invalid keys, off",
             {"HARNESS_ULTRAREVIEW": "off", "HARNESS_CODE_TIER": "gigantic", "HARNESS_CODE_EFFORT": "11"}, {},
             [bad("HARNESS_CODE_TIER", "gigantic"), bad("HARNESS_CODE_EFFORT", "11")], True),
            ("review tier, wave and switch invalid, switch ignored so it runs",
             {"HARNESS_REVIEW_TIER": "Huge", "HARNESS_WAVE_REVIEW": "sometimes", "HARNESS_ULTRAREVIEW": "perhaps"}, {},
             [bad("HARNESS_REVIEW_TIER", "Huge"), bad("HARNESS_ULTRAREVIEW", "perhaps"),
              bad("HARNESS_WAVE_REVIEW", "sometimes")], False),
            ("JSON numbers become their text",
             {"HARNESS_CODE_EFFORT": 11, "HARNESS_WAVE_REVIEW": 2.5, "HARNESS_ULTRAREVIEW": "off"}, {},
             [bad("HARNESS_CODE_EFFORT", "11"), bad("HARNESS_WAVE_REVIEW", "2.5")], True),
            ("environment values are stripped and reported as given",
             {"HARNESS_CODE_EFFORT": "extreme", "HARNESS_ULTRAREVIEW": "no"}, {"HARNESS_WAVE_REVIEW": "  xl  "},
             [bad("HARNESS_CODE_EFFORT", "extreme"), bad("HARNESS_WAVE_REVIEW", "xl")], True),
            ("environment keys",
             {"HARNESS_ULTRAREVIEW": "off"}, {"HARNESS_CODE_TIER": "Ultra", "HARNESS_REVIEW_TIER": "tiny"},
             [bad("HARNESS_CODE_TIER", "Ultra"), bad("HARNESS_REVIEW_TIER", "tiny")], True),
            ("all five invalid",
             {"HARNESS_CODE_TIER": "t1", "HARNESS_CODE_EFFORT": "e2", "HARNESS_REVIEW_TIER": "t3",
              "HARNESS_ULTRAREVIEW": "maybe", "HARNESS_WAVE_REVIEW": "w5"}, {},
             [bad("HARNESS_CODE_TIER", "t1"), bad("HARNESS_CODE_EFFORT", "e2"), bad("HARNESS_REVIEW_TIER", "t3"),
              bad("HARNESS_ULTRAREVIEW", "maybe"), bad("HARNESS_WAVE_REVIEW", "w5")], False),
            ("a single invalid wave value next to off",
             {"HARNESS_WAVE_REVIEW": "opuss", "HARNESS_ULTRAREVIEW": "0"}, {},
             [bad("HARNESS_WAVE_REVIEW", "opuss")], True),
        ]
        for i, (label, file_env, environ, problems, off) in enumerate(cases):
            number = 100 + 13 * i
            with self.subTest(label):
                p = self.go(number, file_env, environ=environ)
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, problems, environ)

    def test_other_invalid_keys_warn_while_ultrareview_is_unset_and_it_runs(self):
        cases = [({"HARNESS_CODE_TIER": "gigantic"}, [bad("HARNESS_CODE_TIER", "gigantic")]),
                 ({"HARNESS_REVIEW_TIER": "zzz", "HARNESS_ULTRAREVIEW": "on"}, [bad("HARNESS_REVIEW_TIER", "zzz")]),
                 ({"HARNESS_WAVE_REVIEW": "x", "HARNESS_CODE_EFFORT": "y"},
                  [bad("HARNESS_CODE_EFFORT", "y"), bad("HARNESS_WAVE_REVIEW", "x")])]
        for i, (file_env, problems) in enumerate(cases):
            with self.subTest(file_env=file_env):
                p = self.go(30 + i, file_env)
                self.assertRan(p, 30 + i)
                self.assertWarnings(p, problems)

    def test_valid_values_of_the_other_keys_are_quiet_whatever_their_case(self):
        file_env = {"HARNESS_CODE_TIER": "Opus", "HARNESS_CODE_EFFORT": " XHIGH ", "HARNESS_REVIEW_TIER": "FABLE",
                    "HARNESS_WAVE_REVIEW": "Off"}
        p = self.go(14, dict(file_env, HARNESS_ULTRAREVIEW="ON"))
        self.assertRan(p, 14)
        self.assertEqual(self.warnings(p), [])
        p = self.go(16, {"HARNESS_WAVE_REVIEW": "0", "HARNESS_CODE_EFFORT": "max", "HARNESS_CODE_TIER": "sonnet"})
        self.assertRan(p, 16)
        self.assertEqual(self.warnings(p), [])

    def test_valid_other_keys_do_not_hide_an_off(self):
        file_env = {"HARNESS_CODE_TIER": "Opus", "HARNESS_CODE_EFFORT": " XHIGH ", "HARNESS_REVIEW_TIER": "FABLE"}
        p = self.go(15, dict(file_env, HARNESS_WAVE_REVIEW="sonnet", HARNESS_ULTRAREVIEW="No"))
        self.assertNothingRan(p, 15)
        self.assertEqual(self.warnings(p), [])
        p = self.go(17, dict(file_env, HARNESS_WAVE_REVIEW="Off", HARNESS_ULTRAREVIEW=" FALSE"))
        self.assertNothingRan(p, 17)
        self.assertEqual(self.warnings(p), [])

    def test_json_booleans_and_other_non_text_values_are_not_values(self):
        # A JSON true/false/null/list/object is "unset", not on and not off: it runs and says nothing.
        for i, value in enumerate([True, False, None, ["off"], {"v": "off"}]):
            with self.subTest(value=value):
                p = self.go(60 + i, {"HARNESS_ULTRAREVIEW": value, "HARNESS_CODE_TIER": value})
                self.assertRan(p, 60 + i)
                self.assertEqual(self.warnings(p), [])

    def test_json_numbers_are_their_text(self):
        # A JSON number that is not a spelling of on or off is invalid, reported as its text.
        for i, (value, shown, off) in enumerate([(0.0, "0.0", False), (2, "2", False), (1, None, False),
                                                 (0, None, True)]):
            with self.subTest(number=value):
                number = 80 + i
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", shown)] if shown else [])

    def test_broken_or_oddly_shaped_settings_count_as_no_file(self):
        texts = ["{not json", "", "[]", '"off"', "null", '{"env": "off"}', '{"env": ["HARNESS_ULTRAREVIEW"]}',
                 '{"env": null}', '{"env": {"HARNESS_ULTRAREVIEW": "off"}', '{"other": {"HARNESS_ULTRAREVIEW": "off"}}']
        for i, text in enumerate(texts):
            with self.subTest(text=text):
                p = self.go(120 + i, raw=text)
                self.assertRan(p, 120 + i)
                self.assertEqual(self.warnings(p), [])

    def test_a_broken_settings_file_does_not_hide_the_environment(self):
        # Broken file, but the environment says off: off.
        p = self.go(140, raw="{not json", environ={"HARNESS_ULTRAREVIEW": "off"})
        self.assertNothingRan(p, 140)
        # A broken file with a bad environment value: only the environment's warning.
        environ = {"HARNESS_CODE_EFFORT": "wild"}
        p = self.go(141, raw="[1, 2", environ=environ)
        self.assertRan(p, 141)
        self.assertWarnings(p, [bad("HARNESS_CODE_EFFORT", "wild")], environ)

    def test_the_environment_wins_even_when_invalid_and_blank_falls_through(self):
        # (settings value, environment value, expected problems, off)
        cases = [
            ("off", "perhaps", [bad("HARNESS_ULTRAREVIEW", "perhaps")], False),
            ("maybe", "sometimes", [bad("HARNESS_ULTRAREVIEW", "sometimes")], False),
            ("maybe", "off", [], True),
            ("off", "   ", [], True),
            ("on", " OFF ", [], True),
            ("maybe", "on", [], False),
            ("maybe", "", [bad("HARNESS_ULTRAREVIEW", "maybe")], False),
            ("no", "yes", [], False),
        ]
        for i, (file_value, env_value, problems, off) in enumerate(cases):
            with self.subTest(file=file_value, environment=env_value):
                number = 150 + i
                environ = {"HARNESS_ULTRAREVIEW": env_value}
                p = self.go(number, {"HARNESS_ULTRAREVIEW": file_value}, environ=environ)
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, problems, environ)

    def test_environment_off_wins_and_settings_local_is_ignored(self):
        # The environment says off while settings.json says on: off.
        self.settings({"HARNESS_ULTRAREVIEW": "on"})
        self.assertNothingRan(self.pr("ultra", "34", HARNESS_ULTRAREVIEW="off", **self.claude()), 34)
        # The environment alone says off, with no settings files at all.
        os.unlink(os.path.join(self.main, ".claude", "settings.json"))
        self.assertNothingRan(self.pr("ultra", "35", HARNESS_ULTRAREVIEW="false", **self.claude()), 35)
        # settings.local.json saying off does not turn it off.
        p = self.go(36, local={"HARNESS_ULTRAREVIEW": "off"})
        self.assertRan(p, 36)
        self.assertEqual(self.warnings(p), [])
        # ...and settings.local.json saying on does not undo an off in settings.json.
        p = self.go(37, {"HARNESS_ULTRAREVIEW": "off"}, local={"HARNESS_ULTRAREVIEW": "on"})
        self.assertNothingRan(p, 37)

    def test_settings_local_never_adds_warnings(self):
        local = {"HARNESS_CODE_TIER": "gigantic", "HARNESS_ULTRAREVIEW": "maybe", "HARNESS_WAVE_REVIEW": "q"}
        p = self.go(44, {"HARNESS_REVIEW_TIER": "tiny"}, local=local)
        self.assertRan(p, 44)
        self.assertWarnings(p, [bad("HARNESS_REVIEW_TIER", "tiny")])


class UltraSettingFromWorktree(UltraSettingBase, WorktreeTest):
    """The switch is the main checkout's, also when harness-pr runs in the ticket worktree."""

    def test_main_checkout_settings_apply_in_the_worktree(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"})
        self.assertNothingRan(self.pr("ultra", "34", cwd=self.wt, **self.claude()), 34)
        self.scenario([])
        p = self.pr("ultra", "35", "--force", cwd=self.wt, **self.claude())
        self.assertRan(p, 35)

    def test_the_worktrees_own_settings_file_does_not_count(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"}, root=self.wt)
        self.assertRan(self.pr("ultra", "34", cwd=self.wt, **self.claude()), 34)

    def test_warnings_come_from_the_main_checkouts_settings_only(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off", "HARNESS_CODE_TIER": "gigantic"})
        self.settings({"HARNESS_REVIEW_TIER": "wrong", "HARNESS_WAVE_REVIEW": "nope"}, root=self.wt)
        p = self.pr("ultra", "34", cwd=self.wt, **self.claude())
        self.assertNothingRan(p, 34)
        self.assertWarnings(p, [bad("HARNESS_CODE_TIER", "gigantic")])


# =========================================================================== land
def land_view(**over):
    view = {"isDraft": False, "mergeable": "MERGEABLE", "headRefName": BRANCH,
            "closingIssuesReferences": [{"number": 12}], "statusCheckRollup": GREEN, "title": "Add the widget (#12)",
            "url": PR_URL, "state": "OPEN"}
    view.update(over)
    return view


HARNESS_MARK = "<!-- harness -->"     # the last line of every comment harness-board posts
RESOLUTION = "**Resolution**"


def pr_body_text(fx, summary=True, red=True, verify=True):
    """A PR body as `harness-pr open` renders it from templates/pr_body.md, for the fixture `fx`. A switch off
    leaves that part out, as a body edited by hand may."""
    parts = ["Closes #12", ""]
    if summary:
        parts += ["## Summary", fx["summary"], ""]
    parts += ["## Definition of done", *fx["dod"], "",
              "## Tests", f"- `{fx['test_file']}::test_main`: the main case", "",
              f"Test files run: `{fx['test_file']}`", ""]
    if red:
        parts += [f"Red run before implementation ({fx['red_kind']}, exit {fx['red_exit']}):",
                  "```", *fx["red_tail"], "```", ""]
    if verify:
        parts += ["Verify:", "```", *fx["verify"], "```", ""]
    parts += ["## Review", fx["review"], "", "## Risks", "None noted."]
    return "\n".join(parts)


def pr_data(fx):
    """Everything `gh pr view P --json ...` can say about the fixture's PR after the merge (a superset: the
    fake answers any field list with it). A fixture with a `body` of its own (hand-written) keeps it."""
    url = f"https://github.com/o/r/pull/{fx['pr']}"
    body = fx.get("body") or pr_body_text(fx)
    return {"number": fx["pr"], "title": "Add the widget (#12)", "url": url, "body": body,
            "files": [{"path": p, "additions": 3 + i, "deletions": i} for i, p in enumerate(fx["files"])],
            "mergeCommit": {"oid": fx["sha"]}, "headRefName": BRANCH, "baseRefName": "main",
            "closingIssuesReferences": [{"number": 12}]}


FILES_PAGE = 30          # GitHub's page size for GET repos/O/R/pulls/N/files when none is asked for


def files_calls(pr, paths):
    """`gh api repos/o/r/pulls/N/files`, which lists every changed file (`gh pr view --json files` stops at
    100): GitHub's pages of 30 objects with a `filename`. With --paginate gh prints one JSON array per page,
    back to back; with --paginate --slurp one array of the pages; without --paginate only the first page. The
    endpoint is one argv token, `repos/o/r/pulls/N/files` with or without `?per_page=100`."""
    objects = [{"sha": hashlib.sha1(path.encode()).hexdigest(), "filename": path, "status": "modified",
                "additions": 3, "deletions": 1, "changes": 4,
                "blob_url": f"https://github.com/o/r/blob/{SHA}/{path}", "patch": "@@ -1 +1 @@\n-old\n+new"}
               for path in paths]
    pages = [objects[i:i + FILES_PAGE] for i in range(0, len(objects), FILES_PAGE)] or [[]]
    back_to_back = "".join(json.dumps(page, indent=2) for page in pages)
    calls = []
    for endpoint in (f"repos/o/r/pulls/{pr}/files", f"repos/o/r/pulls/{pr}/files?per_page=100"):
        calls += [{"contains": [endpoint, "--paginate", "--slurp"], "stdout": json.dumps(pages)},
                  {"contains": [endpoint, "--paginate"], "stdout": back_to_back},
                  {"contains": [endpoint], "stdout": json.dumps(pages[0], indent=2)}]
    return calls


TICKET_12_BODY = ("## Request\nAdd a widget that frobs.\n\n## Definition of done\n- [ ] The widget frobs twice\n\n"
                  "## Tests that must pass\n- `tests/test_widget.py::test_frob`: frobs twice\n")

# Two landings that differ in every part the Resolution copies: PR, squash sha, summary, files, the checklist,
# the red-run kind and the verify result. Nothing in one appears in the other.
FIXTURE_A = {
    "pr": 34, "sha": SHA,
    "summary": "The widget frobs twice now, and empty input frobs once.",
    "files": ["widget.py", "tests/test_widget.py", "docs/widget.md"],
    "dod": ["- [x] The widget frobs twice", "- [x] The docs mention it"],
    "red_kind": "assertion", "red_exit": 1,
    "red_tail": ["E   frob() returned 1, wanted 2", "1 failed in 0.12s"],
    "verify": ["verify step: lint clean", "Ran 41 tests in 2.315s", "", "OK (skipped=1)"],
    "test_file": "tests/test_frob_spec.py",          # in the body's Tests section, not among the changed files
    "review": "2 findings: 0 high, 1 medium, 1 low.",
}
FIXTURE_B = {
    "pr": 57, "sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432",
    "summary": "Gizmo parsing moves into lib/gizmo with a strict tokenizer.",
    "files": ["lib/gizmo/__init__.py", "lib/gizmo/tokenizer.py", "bin/gizmo-cli", "tests/test_tokenizer.py",
              "CHANGELOG.md"],
    "dod": ["- [x] Tokens round-trip through the parser", "- [ ] The CLI prints a usage line on bad input",
            "- [x] Changelog entry"],
    "red_kind": "import", "red_exit": 2,
    "red_tail": ["ModuleNotFoundError: No module named 'gizmo'"],
    "verify": ["collected 212 items", "212 passed in 9.80s", "VERIFY PASSED: 212 checks, 0 warnings"],
    "test_file": "tests/test_gizmo_spec.py",
    "review": "Harness review (fable): no findings beyond a naming nit.",
}
# A harness-pr body edited by hand before the land, as the PR template invites: a Summary of two paragraphs and
# a bullet list, a checklist item wrapped onto a second line, and a follow-up checkbox under Risks that is not
# part of the definition of done.
FIXTURE_C = {
    "pr": 61, "sha": "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d",
    "summary": "Parsing moves into lib/gizmo/parser with a strict tokenizer; the CLI keeps its flags.",
    "summary_lines": ["Parsing moves into lib/gizmo/parser with a strict tokenizer; the CLI keeps its flags.",
                      "Bad input used to raise a traceback from deep inside the old parser.",
                      "- `gizmo-cli` now prints a usage line and exits 2 on bad input.",
                      "- Unterminated strings are rejected instead of read past the end."],
    "files": ["lib/gizmo/parser.py", "lib/gizmo/errors.py", "bin/gizmo-cli", "tests/test_parser.py"],
    "dod": ["- [x] Tokens round-trip through the parser", "- [x] The CLI prints a usage line on bad input,",
            "- [ ] Changelog entry"],
    "dod_wrapped": "and exits 2 instead of raising",          # the second item's continuation line
    "red_kind": "import", "red_exit": 2,
    "red_tail": ["ModuleNotFoundError: No module named 'gizmo.parser'"],
    "verify": ["collected 230 items", "230 passed in 10.42s", "VERIFY PASSED: 230 checks, 0 warnings"],
    "test_file": "tests/test_gizmo_spec.py",
    "review": "Harness review (fable): one low finding, fixed.",
    "risks": ["Legacy gizmo files parse differently; the nightly import job is the first to see it.",
              "- [ ] Watch the nightly import after the first run on main"],
}
FIXTURE_C["body"] = "\n".join([
    "Closes #12", "",
    "## Summary", *FIXTURE_C["summary_lines"][:2], "", *FIXTURE_C["summary_lines"][2:], "",
    "## Definition of done", FIXTURE_C["dod"][0], FIXTURE_C["dod"][1], "  " + FIXTURE_C["dod_wrapped"],
    FIXTURE_C["dod"][2], "",
    "## Tests", f"- `{FIXTURE_C['test_file']}::test_main`: the main case", "",
    f"Red run before implementation ({FIXTURE_C['red_kind']}, exit {FIXTURE_C['red_exit']}):",
    "```", *FIXTURE_C["red_tail"], "```", "",
    "Verify:", "```", *FIXTURE_C["verify"], "```", "",
    "## Review", FIXTURE_C["review"], "",
    "## Risks", *FIXTURE_C["risks"], ""])
# A PR wider than `gh pr view --json files` reaches: 130 changed files, of which the PR view holds the first 100.
FIXTURE_WIDE = dict(FIXTURE_A, pr=72, sha=hashlib.sha1(b"fixture-wide").hexdigest(),
                    summary="Every service module logs through the structured logger.",
                    files=[f"services/svc_{i:03d}/logging.py" for i in range(1, 131)])


class Land(WorktreeTest):
    def land_calls(self, views=None, issue_state="OPEN", comments=(), reviews=(), commits=None):
        """The land conversation: the review gate's reads first (no harness review by default, so the gate
        passes), then the land; the last entries answer the PR's body and files, the ticket's other fields and a
        comment posted straight through gh, so the land may read and post the Resolution either way."""
        views = views or [land_view()]
        commits = [pr_commit("a", at(9))] if commits is None else commits
        issue = {"state": issue_state, "labels": [{"name": "task"}, {"name": "status:in-review"}],
                 "comments": list(comments)}
        return gate_calls(reviews, commits) + [
                {"match": ["pr", "view", "34", "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": SHA}}},
                {"match": ["pr", "view", "34", "--json", LAND_FIELDS],
                 "responses": [{"stdout_json": v} for v in views]},
                REPO_VIEW, {"match": ["pr", "merge", "34"], "stdout": ""},
                {"match": ["issue", "view", "12", "--json", "state,labels,comments"], "stdout_json": issue},
                {"match": ["issue", "edit", "12"], "stdout": ""},
                {"match": ["pr", "view", "34"], "stdout_json": pr_data(FIXTURE_A)},
                *files_calls(34, FIXTURE_A["files"]),
                {"match": ["issue", "view", "12"],
                 "stdout_json": dict(issue, number=12, title="Add the widget", body=TICKET_12_BODY,
                                     url="https://github.com/o/r/issues/12")},
                {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
                {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": []}]

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    def branch_exists(self):
        return subprocess.run([GIT, "-C", self.main, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
                              capture_output=True, env=self.env()).returncode == 0

    def test_land_merges_then_cleans_up_in_order(self):
        self.scenario(self.land_calls(views=[land_view(mergeable="UNKNOWN"), land_view()]))
        p = self.ok("land", "34", "--yes", cwd=self.wt)
        m = self.main
        steps = [self.position("gh pr merge 34 -R o/r --squash --delete-branch"),
                 self.position(f"git -C {m} worktree remove {self.wt}"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"git -C {m} fetch --prune"),
                 self.position(f"git -C {m} pull --ff-only"),
                 self.position("gh issue view 12 --json state,labels,comments"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position("gh issue edit 12 --remove-label status:in-review"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(sum(1 for a in self.gh_argv() if a == ["pr", "view", "34", "--json", LAND_FIELDS]), 2)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 4, p.stdout)
        self.assertEqual(lines[0], f'Merged PR #34 "Add the widget (#12)" into main as {SHA[:12]}.')
        self.assertIn(f"removed {self.wt}", lines[1])
        self.assertIn("fast-forwarded main", lines[2])
        self.assertEqual(lines[3], "Ticket: #12 closed, status labels removed, commented, "
                                   "epic check: #3: 1 of 2 tasks closed; left open.")

    def test_land_stops_after_the_merge_on_a_dirty_worktree(self):
        self.scenario(self.land_calls())
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        p = self.pr("land", "34", cwd=self.main)
        self.assertOneLineError(p, "is dirty", "cleanup stopped")
        self.assertIn("harness-pr land 34 --discard", p.stdout)
        self.assertIn(f"git -C {self.main} worktree remove --force {self.wt}", p.stdout)
        self.assertIn(f"git -C {self.main} branch -D {BRANCH}", p.stdout)
        self.position("gh pr merge 34")
        order = self.order()
        for gone in ("worktree remove", "branch -D", "fetch --prune", "pull --ff-only"):
            self.assertFalse(any(gone in line for line in order), gone)
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())

    def test_land_discard_forces_the_removal(self):
        self.scenario(self.land_calls(issue_state="CLOSED"))
        write(os.path.join(self.wt, "widget.py"), "changed\n")
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        p = self.ok("land", "34", "--discard", cwd=self.main)
        self.position(f"git -C {self.main} worktree remove --force {self.wt}")
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        board = self.stub_calls("harness-board")
        self.assertFalse(any(a[0] == "close" for a in board))        # GitHub closed it on the merge
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        self.assertIn("closed by GitHub", p.stdout)

    def test_land_without_a_closing_reference_warns_and_merges(self):
        self.scenario(self.land_calls(views=[land_view(closingIssuesReferences=[])]))
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn("closes no issue", p.stderr)
        self.position("gh pr merge 34")
        self.assertFalse(any(a[:2] == ["issue", "view"] for a in self.gh_argv()))
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertEqual(p.stdout.splitlines()[-1], "Ticket: none linked.")

    def test_land_resumes_after_a_dirty_stop_without_a_second_merge(self):
        self.scenario(self.land_calls())
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        self.assertOneLineError(self.pr("land", "34", cwd=self.main), "cleanup stopped")
        self.position("gh pr merge 34")
        # Now GitHub reports the PR merged; its checks and mergeability no longer matter.
        merged = land_view(state="MERGED", mergeable="UNKNOWN", statusCheckRollup=PENDING)
        self.scenario(self.land_calls(views=[merged]))
        p = self.ok("land", "34", "--discard", cwd=self.main)
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "already merged, finishing cleanup")
        self.assertEqual(lines[1], f'PR #34 "Add the widget (#12)" was already merged into main as {SHA[:12]}.')
        self.assertEqual(len(lines), 5, p.stdout)
        m = self.main
        steps = [self.position(f"git -C {m} worktree remove --force {self.wt}"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"git -C {m} fetch --prune"),
                 self.position(f"git -C {m} pull --ff-only"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position("gh issue edit 12 --remove-label status:in-review"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())

    def test_land_finishes_when_the_worktree_folder_was_removed_by_hand(self):
        self.scenario(self.land_calls())
        shutil.rmtree(self.wt)
        self.assertIn(f"worktree {self.wt}", self.git(self.main, "worktree", "list", "--porcelain"))   # still listed
        p = self.ok("land", "34", cwd=self.main)
        m = self.main
        steps = [self.position("gh pr merge 34"),
                 self.position(f"git -C {m} worktree prune"),
                 self.position(f"git -C {m} worktree list --porcelain"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertFalse(any("worktree remove" in line for line in self.order()))
        self.assertFalse(self.branch_exists())
        self.assertIn(f"Worktree: no worktree for {BRANCH}; branch {BRANCH} deleted.", p.stdout)
        self.assertIn("Ticket: #12 closed, status labels removed, commented", p.stdout)

    def test_land_goes_on_to_the_ticket_when_a_locked_worktree_is_gone(self):
        self.scenario(self.land_calls())
        self.git(self.main, "worktree", "lock", self.wt)       # prune keeps a locked entry
        shutil.rmtree(self.wt)
        p = self.pr("land", "34", cwd=self.main)
        self.assertOneLineError(p, "PR #34 is merged, but", f"git branch -D {BRANCH} failed")
        self.assertIn(f"Worktree: {self.wt} was already gone; branch {BRANCH} kept.", p.stdout)
        self.assertFalse(any("worktree remove" in line for line in self.order()))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))
        self.assertIn("Ticket: #12 closed, status labels removed, commented", p.stdout)

    def test_land_asks_github_for_the_epic_without_a_cached_parent(self):
        os.unlink(self.state_path("tickets", "12.json"))
        parent = {"match": ["api", "repos/o/r/issues/12/parent"], "stdout_json": {"number": 3, "title": "Widgets"}}
        self.scenario([parent] + self.land_calls())
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn(["api", "repos/o/r/issues/12/parent"], self.gh_argv())
        self.assertIn(["epic-check", "3", "--repo", self.main], self.stub_calls("harness-board"))
        self.assertIn("epic check: #3: 1 of 2 tasks closed; left open.", p.stdout)

        # A cache without a parent asks too; a 404 means no epic, and the land still succeeds.
        write(self.state_path("tickets", "12.json"), json.dumps({"number": 12, "parent": None}))
        none = {"match": ["api", "repos/o/r/issues/12/parent"], "stderr": "gh: Not Found (HTTP 404)\n", "exit": 1}
        self.scenario([none] + self.land_calls(views=[land_view(state="MERGED")], issue_state="CLOSED"))
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn(["api", "repos/o/r/issues/12/parent"], self.gh_argv())
        self.assertFalse(any(a[0] == "epic-check" for a in self.stub_calls("harness-board")))
        self.assertNotIn("epic check", p.stdout)

    def test_land_on_a_merged_pr_does_not_repeat_the_landed_comment(self):
        landed = {"author": {"login": "rjeskuri"}, "body": f"Landed in {SHA}", "createdAt": "2026-10-13T10:00:00Z"}
        self.scenario(self.land_calls(views=[land_view(state="MERGED")], issue_state="CLOSED", comments=[landed]))
        p = self.ok("land", "34", cwd=self.main)
        self.assertEqual(p.stdout.splitlines()[0], "already merged, finishing cleanup")
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))
        self.assertEqual(self.stub_calls("harness-board"), [["epic-check", "3", "--repo", self.main]])
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        self.assertIn("landed comment already there", p.stdout)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())


WORKLOG = "worklog/worklog.csv"
WORKLOG_BEFORE = "date,project,hours,note\n2026-10-12,harness,2.5,board review\n"
WORKLOG_LANDED = WORKLOG_BEFORE + "2026-10-13,harness,1.75,\"land, then restore\"\n2026-10-14,jotform,0.5,triage\n"
IDENTICAL = "(identical to the landed content)"


class LandDirtyMain(WorktreeTest):
    """The main checkout has uncommitted changes when `land` fast-forwards it. The squash merge is a real commit
    on origin/main (pushed from a second clone, as GitHub's merge would be), and gh reports its sha, so the land's
    fetch brings it and `git pull --ff-only` has something to apply. A dirty path whose bytes equal the landed
    commit's is restored and main fast-forwards; any other dirty path, or an untracked file in the way, refuses
    with the path named and leaves the checkout as it was."""

    land_calls = Land.land_calls

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        write(os.path.join(self.main, WORKLOG), WORKLOG_BEFORE)
        write(os.path.join(self.main, "data", "rates.tsv"), "client\trate\nacme\t120\n")
        write(os.path.join(self.main, "docs", "notes.md"), "# Notes\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "Track the worklog")
        self.git(self.main, "push", "-q", "origin", "main")

    def landed(self, files):
        """Push the squash merge of PR #34 to origin/main, changing `files` (path: content, None deletes it);
        return its sha."""
        clone = os.path.join(self.tmp, "github")
        if not os.path.isdir(clone):
            subprocess.run([GIT, "clone", "-q", self.origin, clone], env=self.env(), check=True, capture_output=True)
        self.git(clone, "pull", "-q", "--ff-only")
        for path, text in files.items():
            if text is None:
                os.unlink(os.path.join(clone, path))
            else:
                write(os.path.join(clone, path), text)
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "Add the widget (#34)")
        self.git(clone, "push", "-q", "origin", "main")
        return self.git(clone, "rev-parse", "HEAD")

    def land(self, sha):
        merge = {"match": ["pr", "view", "34", "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}}
        self.scenario([merge] + self.land_calls())
        return self.pr("land", "34", cwd=self.main)

    def head(self):
        return self.git(self.main, "rev-parse", "HEAD")

    def status(self):
        """{path: the two-letter porcelain code} for the main checkout, untracked files included."""
        out = subprocess.run([GIT, "-C", self.main, "status", "--porcelain=v1", "--untracked-files=all"],
                             env=self.env(), capture_output=True, text=True, check=True).stdout
        return {line[3:]: line[:2] for line in out.splitlines() if line.strip()}

    def read(self, path):
        with open(os.path.join(self.main, path), "rb") as fh:
            return fh.read().decode("utf-8")

    def assertRefused(self, p, sha, before, named, untouched):
        """A refusal: exit 1 with the path(s) named, main's HEAD where it was, nothing reported cleaned, and each
        file in `untouched` (path: content) still holding the local content. The refusal is one of the land's
        problems, reported at the end as today: the ticket is still closed, unlabelled and commented, and the
        summary still prints with main not pulled."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for path in named:
            self.assertIn(path, p.stderr)
        self.assertEqual(self.head(), before, "main moved although the land refused")
        self.assertNotIn("cleaned", p.stdout)
        self.assertNotIn(IDENTICAL, p.stdout)
        for path, text in untouched.items():
            self.assertEqual(self.read(path), text, path)
        board = self.stub_calls("harness-board")
        self.assertIn(["close", "12", "--repo", self.main], board)
        self.assertIn(["comment", "12", "--body", f"Landed in {sha}", "--repo", self.main], board)
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        lines = p.stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("Merged PR #34"), p.stdout)
        self.assertTrue(any(line.startswith("Main checkout:") and "main not pulled" in line for line in lines),
                        p.stdout)
        self.assertTrue(any(line.startswith("Ticket: #12 closed") for line in lines), p.stdout)

    def cleaned_text(self, p):
        """The stdout lines that report a restored path, joined; each must say `cleaned`."""
        cleaned = [line for line in p.stdout.splitlines() if IDENTICAL in line]
        self.assertTrue(cleaned, p.stdout)
        self.assertTrue(all("cleaned" in line for line in cleaned), cleaned)
        return "\n".join(cleaned)

    # ------------------------------------------------------------- broad
    def test_identical_dirty_paths_are_restored_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        before = self.head()
        self.assertNotEqual(before, sha)
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)          # the owner's edit, as the chore PR has it
        self.assertEqual(self.status(), {WORKLOG: " M"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {})
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        self.assertEqual(self.read("widget.py"), "def frob():\n    return 2\n")
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertIn("fast-forwarded main", p.stdout)

    def test_a_differing_dirty_path_refuses_and_restores_nothing(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = WORKLOG_LANDED + "2026-10-14,harness,0.25,not in the PR\n"     # one row more than the landed file
        write(os.path.join(self.main, WORKLOG), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: mine})
        self.assertEqual(self.status(), {WORKLOG: " M"})

    def test_an_untracked_file_in_the_way_refuses(self):
        sha = self.landed({"docs/widget.md": "# Widget\nIt frobs twice.\n", WORKLOG: WORKLOG_LANDED})
        before = self.head()
        write(os.path.join(self.main, "docs", "widget.md"), "# My draft\n")
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["docs/widget.md"],
                           {"docs/widget.md": "# My draft\n", WORKLOG: WORKLOG_BEFORE})
        self.assertEqual(self.status(), {"docs/widget.md": "??"})

    # ---------------------------------------------------------- specific
    def test_identical_paths_staged_or_not_are_each_named_and_restored(self):
        rates = "client\trate\nacme\t135\nglobex\t90\n"
        notes = "# Notes\n\nLanding restores identical files.\n"
        sha = self.landed({"data/rates.tsv": rates, "docs/notes.md": notes})
        write(os.path.join(self.main, "data", "rates.tsv"), rates)
        write(os.path.join(self.main, "docs", "notes.md"), notes)
        self.git(self.main, "add", "docs/notes.md")                          # one staged, one only modified
        self.assertEqual(self.status(), {"data/rates.tsv": " M", "docs/notes.md": "M "})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {})
        self.assertEqual((self.read("data/rates.tsv"), self.read("docs/notes.md")), (rates, notes))
        text = self.cleaned_text(p)
        for path in ("data/rates.tsv", "docs/notes.md"):
            self.assertIn(path, text)
        self.assertNotIn(WORKLOG, text)                                       # clean before the land: not named

    def test_one_differing_path_among_identical_ones_restores_nothing(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "README.md": "# proj\n\nThe widget frobs.\n"})
        before = self.head()
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "README.md"), "# proj\n\nMy own intro.\n")   # differs
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["README.md"],
                           {WORKLOG: WORKLOG_LANDED, "README.md": "# proj\n\nMy own intro.\n"})
        self.assertEqual(self.status(), {WORKLOG: " M", "README.md": " M"})  # the identical one not restored either
        problem = [line for line in p.stderr.splitlines() if "README.md" in line]
        self.assertTrue(problem, p.stderr)
        for line in problem:
            self.assertNotIn(WORKLOG, line)                                   # only the differing path is reported

    def refused_case(self, local, staged=None):
        """Land with the worklog dirty as `local` bytes (and `staged` in the index first, when given) against a
        landed commit that writes WORKLOG_LANDED; assert a refusal that keeps every byte, index and tree."""
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        path = os.path.join(self.main, WORKLOG)
        if staged is not None:
            with open(path, "wb") as fh:
                fh.write(staged.encode())
            self.git(self.main, "add", WORKLOG)
        with open(path, "wb") as fh:
            fh.write(local.encode())
        code = self.status()[WORKLOG]
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: local})
        self.assertEqual(self.status(), {WORKLOG: code})                      # index and tree as they were
        return code

    def test_reordered_rows_refuse(self):
        rows = WORKLOG_LANDED.splitlines(keepends=True)
        mine = "".join(rows[:-2] + [rows[-1], rows[-2]])
        self.assertEqual(sorted(mine.splitlines()), sorted(WORKLOG_LANDED.splitlines()))
        self.assertNotEqual(mine, WORKLOG_LANDED)
        self.refused_case(mine)

    def test_crlf_line_endings_refuse(self):
        self.refused_case(WORKLOG_LANDED.replace("\n", "\r\n"))

    def test_a_missing_final_newline_refuses(self):
        self.refused_case(WORKLOG_LANDED.rstrip("\n"))

    def test_a_staged_third_version_under_identical_tree_bytes_refuses(self):
        # The index holds a version that is neither HEAD's nor the landed one; the tree matches the landed bytes.
        # Restoring the path would drop the staged version, and plain git refuses this state, so the land must too.
        staged = WORKLOG_BEFORE + "2026-10-13,harness,4.0,staged only\n"
        self.assertNotIn(staged, (WORKLOG_BEFORE, WORKLOG_LANDED))
        self.assertEqual(self.refused_case(WORKLOG_LANDED, staged=staged), "MM")
        index = subprocess.run([GIT, "-C", self.main, "show", f":{WORKLOG}"], env=self.env(),
                               capture_output=True, check=True).stdout.decode("utf-8")
        self.assertEqual(index, staged, "the staged version was dropped from the index")

    def test_staged_landed_content_with_other_bytes_in_the_tree_keeps_the_edit_and_fast_forwards(self):
        # The index already holds the landed bytes, so git fast-forwards today and keeps the tree's edit; the land
        # must not get stricter than that, nor restore (and so drop) the unstaged row.
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        mine = WORKLOG_LANDED + "2026-10-14,harness,3.0,unstaged on top\n"
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)
        self.git(self.main, "add", WORKLOG)
        write(os.path.join(self.main, WORKLOG), mine)
        self.assertEqual(self.status(), {WORKLOG: "MM"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.read(WORKLOG), mine)
        self.assertEqual(self.status(), {WORKLOG: " M"})
        self.assertNotIn("cleaned", p.stdout)
        self.assertNotIn(IDENTICAL, p.stdout)

    def test_a_dirty_file_the_merge_deletes_refuses(self):
        sha = self.landed({"docs/notes.md": None, WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = "# Notes\n\nStill using these.\n"
        write(os.path.join(self.main, "docs", "notes.md"), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["docs/notes.md"], {"docs/notes.md": mine, WORKLOG: WORKLOG_BEFORE})
        self.assertEqual(self.status(), {"docs/notes.md": " M"})

    def test_a_same_length_dirty_path_with_other_bytes_refuses(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = WORKLOG_LANDED.replace("1.75", "1.50")
        self.assertEqual(len(mine.encode()), len(WORKLOG_LANDED.encode()))
        self.assertNotEqual(mine, WORKLOG_LANDED)
        write(os.path.join(self.main, WORKLOG), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: mine})
        self.assertEqual(self.status(), {WORKLOG: " M"})

    def test_an_untracked_file_the_merge_does_not_touch_stays_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "scratch", "todo.txt"), "- ask about the rates\n")
        self.assertEqual(self.status(), {WORKLOG: " M", "scratch/todo.txt": "??"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {"scratch/todo.txt": "??"})
        self.assertEqual(self.read("scratch/todo.txt"), "- ask about the rates\n")
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        text = self.cleaned_text(p)
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertNotIn("scratch/todo.txt", text)

    def test_a_dirty_path_the_merge_does_not_touch_keeps_its_edit_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        notes = "# Notes\n\nA local thought, not in the PR.\n"
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "docs", "notes.md"), notes)            # dirty; the merge leaves it alone
        self.assertEqual(self.status(), {WORKLOG: " M", "docs/notes.md": " M"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {"docs/notes.md": " M"})
        self.assertEqual(self.read("docs/notes.md"), notes)
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        text = self.cleaned_text(p)
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertNotIn("docs/notes.md", text)


class LandResolution(Land):
    """`land` leaves one `**Resolution**` comment on the ticket, posted through `harness-board comment` before the
    `Landed in` comment. These run the real harness-board (no stubs), so every comment reaches the fake gh's log."""

    def resolution_calls(self, fx, state="OPEN", issue_state="OPEN", comments=(), view_files=None, after=None,
                         no_sha=False):
        """The land conversation for the fixture's PR. `view_files`: the files `gh pr view --json` holds (all of
        the fixture's by default; GitHub stops at 100), while the files endpoint always lists them all; `after`:
        the answer to the PR read after the merge (`gh pr view P --json body,...`), in place of the PR's data;
        `no_sha`: GitHub reports no merge commit (True) or the read fails ("fail"), so the land knows no sha and
        comments `Landed in PR #N`."""
        pr, sha = str(fx["pr"]), fx["sha"]
        if no_sha:
            fx, sha = dict(fx, sha=""), ""
        view = pr_data(dict(fx, files=fx["files"] if view_files is None else view_files))
        url = f"https://github.com/o/r/pull/{pr}"
        issue = {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12",
                 "body": TICKET_12_BODY, "state": issue_state,
                 "labels": [{"name": "task"}, {"name": "status:in-review"}], "comments": list(comments)}
        rest = [{"body": c["body"], "user": {"login": c["author"]["login"]}, "created_at": c["createdAt"]}
                for c in comments]
        # the review gate's reads: no harness review, so the gate lets the land through
        gate = gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}")
        return gate + [
                {"match": ["pr", "view", pr, "--json", "mergeCommit"],
                 **({"stderr": "GraphQL: timeout\n", "exit": 1} if no_sha == "fail"
                    else {"stdout_json": {"mergeCommit": {"oid": sha} if sha else None}})},
                {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
                 "stdout_json": land_view(url=url, state=state)},
                REPO_VIEW,
                {"match": ["pr", "merge", pr], "stdout": ""},
                {"match": ["pr", "view", pr], **(after or {"stdout_json": view})},
                *files_calls(pr, fx["files"]),
                {"match": ["issue", "view", "12"], "stdout_json": issue},
                {"match": ["issue", "edit", "12"], "stdout": ""},
                {"match": ["issue", "close", "12"], "stdout": ""},
                {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-9\n"},
                {"contains": ["repos/o/r/issues/12/comments", "POST"], "stdout_json": {"id": 9}},
                {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": rest},
                {"match": ["issue", "view", "3"],
                 "stdout_json": {"state": "OPEN", "subIssuesSummary": {"total": 2, "completed": 1}}}]

    def land(self, fx, **scenario):
        self.scenario(self.resolution_calls(fx, **scenario))
        return self.ok("land", str(fx["pr"]), "--yes", cwd=self.main, stubs=False)

    def posted(self, number=None):
        """(index in the gh log, issue number, body) of every comment posted: `gh issue comment N`, `gh issue close
        N --comment` and `gh api repos/o/r/issues/N/comments` with a body."""
        out = []
        for i, call in enumerate(self.gh_calls()):
            a, body, n = call["argv"], None, None
            if a[:2] in (["issue", "comment"], ["issue", "close"]) and len(a) > 2:
                n = a[2]
                for flag in ("--body", "-b", "--comment", "-c"):
                    if flag in a[:-1]:
                        body = a[a.index(flag) + 1]
                for flag in ("--body-file", "-F"):
                    if flag in a[:-1]:
                        path = a[a.index(flag) + 1]
                        if path == "-":
                            body = call["stdin"]
                        elif os.path.exists(path):
                            with open(path, encoding="utf-8") as fh:
                                body = fh.read()
            elif a[:1] == ["api"]:
                target = next((t for t in a if "/issues/" in t and "/comments" in t), None)
                if target:
                    n = target.split("/issues/")[1].split("/")[0]
                    if "--input" in a and call["stdin"].strip():
                        body = json.loads(call["stdin"]).get("body")
                    for j, tok in enumerate(a[:-1]):
                        if tok in ("-f", "-F", "--field", "--raw-field") and a[j + 1].startswith("body="):
                            body = a[j + 1][len("body="):]
            if body is not None and (number is None or n == str(number)):
                out.append((i, n, body))
        return out

    def resolutions(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith(RESOLUTION)]

    def landed(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith("Landed in")]

    def as_comments(self, bodies):
        return [{"author": {"login": "rjeskuri"}, "body": b, "createdAt": f"2026-10-13T1{k}:00:00Z"}
                for k, b in enumerate(bodies)]

    def first(self, lines, pred, what):
        for i, line in enumerate(lines):
            if pred(line):
                return i
        self.fail(f"{what} missing from the Resolution:\n" + "\n".join(lines))

    def assertFrame(self, body, fx):
        """The heading, the harness marker last, the PR link and the full squash sha; the body's lines."""
        self.assertTrue(body.lstrip().startswith(RESOLUTION), body)
        lines = body.rstrip().splitlines()
        self.assertEqual(lines[-1].strip(), HARNESS_MARK, body)
        self.assertIn(f"https://github.com/o/r/pull/{fx['pr']}", body)
        self.assertIn(fx["sha"], body, "the full squash sha missing from the Resolution")
        return lines

    def file_positions(self, lines, fx):
        """The index of each changed file's list line: exactly one of its own per file."""
        file_lines = {}
        for i, line in enumerate(lines):
            m = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if m:
                text = m.group(1).replace("`", "").strip()
                for path in fx["files"]:
                    if text == path or text.startswith(path + " "):
                        file_lines.setdefault(path, []).append(i)
        for path in fx["files"]:
            self.assertEqual(len(file_lines.get(path, [])), 1,
                             f"changed file {path} wants exactly one list line of its own:\n" + "\n".join(lines))
        return [i for found in file_lines.values() for i in found]

    def assertRedKind(self, lines, fx):
        """The red-run kind on exactly one line, and that line is about the red run."""
        kind = re.compile(r"\b" + fx["red_kind"] + r"\b")
        kind_lines = [line for line in lines if kind.search(line)]
        self.assertEqual(len(kind_lines), 1, f"want one red-run kind line, got {kind_lines}")
        self.assertRegex(kind_lines[0], r"(?i)\bred\b", "the kind belongs on a line about the red run")

    def assertVerifyResult(self, lines, fx):
        """The last line of the verify fence: once, outside any code fence; the earlier lines left out."""
        verify = [line for line in fx["verify"] if line.strip()]
        fenced, where = False, []
        for line in lines:
            if line.strip().startswith("```"):
                fenced = not fenced
            elif verify[-1] in line:
                where.append(fenced)
        self.assertEqual(where, [False],
                         f"the verify result line {verify[-1]!r} wants one unfenced line:\n" + "\n".join(lines))
        for text in verify[:-1]:
            self.assertNotIn(text, "\n".join(lines), f"the Resolution copies the earlier verify line {text!r}")

    def assertResolution(self, body, fx):
        """The Resolution carries the PR's summary, every changed file, the checklist as landed, the red-run
        kind and the verify result from the PR body, the PR link and the squash sha, and the harness marker."""
        lines = self.assertFrame(body, fx)

        # the summary, then the changed files (one list line each), then the checklist as landed
        at_summary = self.first(lines, lambda line: fx["summary"] in line, "the PR summary")
        at_files = self.file_positions(lines, fx)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in fx["dod"]]
        self.assertLess(at_summary, min(at_files), "the summary comes before the files")
        self.assertLess(max(at_files), min(at_dod), "the files come before the checklist")
        self.assertEqual(at_dod, sorted(at_dod), "the checklist keeps its order")

        self.assertRedKind(lines, fx)
        self.assertVerifyResult(lines, fx)

        # what the Resolution leaves out of the PR body: the red tail, the tests section, the review, the
        # risks and the closing line
        left_out = fx["red_tail"] + [
            fx["test_file"], "::test_main", "Test files run", fx["review"], "## Review", "## Risks", "None noted.",
            "Closes #12", "## Tests"]
        for text in left_out:
            self.assertNotIn(text, body, f"the Resolution copies {text!r} from the PR body")

    # ------------------------------------------------------------------ fresh lands
    def test_land_posts_one_resolution_after_the_merge(self):
        self.land(FIXTURE_A)
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        index, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_A)
        for other in FIXTURE_B["files"] + [FIXTURE_B["summary"], FIXTURE_B["sha"][:12]]:
            self.assertNotIn(other, body)
        calls = self.gh_calls()
        merge = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["pr", "merge"])
        self.assertGreater(index, merge, "the Resolution is posted after the merge")
        self.assertEqual(calls[index]["argv"][:3], ["issue", "comment", "12"],
                         "posted through harness-board comment")
        # the Landed comment is unchanged: one, `Landed in <sha>`, as harness-board posts it, after the Resolution
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in, so a resume never misses it")

    def test_land_resolution_follows_the_pr_it_lands(self):
        """Another PR, another sha, more files, an unchecked box, an import red run; GitHub closed the ticket."""
        self.land(FIXTURE_B, issue_state="CLOSED")
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        _, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_B)
        for other in FIXTURE_A["files"][2:] + [FIXTURE_A["summary"], FIXTURE_A["sha"][:12], "OK (skipped=1)"]:
            self.assertNotIn(other, body)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.landed(12)],
                         [f"Landed in {FIXTURE_B['sha']}"])

    # ------------------------------------------------------------------ resumed lands
    def land_then_resume(self, fx, keep):
        """Land once, then land again on the merged PR with the ticket showing the first run's comments that
        `keep` selects. Returns (Resolutions of the first run, Resolutions and Landed of the second)."""
        self.land(fx)
        first = self.resolutions(12)
        bodies = [b for _, _, b in self.posted(12) if keep(b)]
        self.land(fx, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        return first, self.resolutions(), self.landed()

    def test_resumed_land_adds_no_second_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: True)
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "a resumed land must not post a second Resolution")
        self.assertEqual(landed, [], "nor a second Landed comment")

    def test_resumed_land_with_only_the_resolution_posts_the_landed_comment(self):
        first, again, landed = self.land_then_resume(FIXTURE_B, keep=lambda b: b.lstrip().startswith(RESOLUTION))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "the Resolution already there is not repeated")
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                         [f"Landed in {FIXTURE_B['sha']}"])

    def test_resumed_land_after_the_landed_comment_posts_no_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: b.lstrip().startswith("Landed in"))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "once Landed in is on the ticket the land is recorded; no Resolution")
        self.assertEqual(landed, [])

    # ------------------------------------------------------------------ a reopened ticket's second landing (#24)
    REOPENED = "Reopened: the first pass broke the hooks."

    def first_pass(self, fx):
        """Land `fx` on a fresh ticket and return the bodies it posted: its Resolution and `Landed in`."""
        self.land(fx)
        bodies = [b for _, _, b in self.posted(12)]
        self.assertEqual(len(bodies), 2, f"the first pass posts a Resolution and Landed in: {bodies}")
        return bodies

    def old_resolution(self, pr_line, sha):
        """A Resolution in the land's format, with the given `PR:` line and squash sha (None: no such line)."""
        closing = ([pr_line] if pr_line else []) + ([f"Squash commit: {sha}"] if sha else [])
        return "\n".join([RESOLUTION, "", "A first pass that was reopened.", "", "Files changed (1):",
                          "- `old/first_pass.py`", "", *closing, "", HARNESS_MARK])

    def assertSecondLanding(self, fx, old_texts):
        """This land of `fx` posted exactly two comments on #12: a Resolution of `fx` (naming its PR, not an old
        one) and then `Landed in <fx's sha>`."""
        posted = self.posted(12)
        self.assertEqual(len(posted), 2, f"want a Resolution and Landed in for PR #{fx['pr']}, got {posted}")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
        index, _, body = found[0]
        self.assertResolution(body, fx)
        for text in old_texts:
            self.assertNotIn(text, body, "the new Resolution is about this PR only")
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_second_landing_of_a_reopened_ticket_comments_resolution_and_landed_in(self):
        """A ticket landed once (its Resolution and `Landed in <old sha>` on it), reopened, and landed again by
        another PR: this landing gets its own Resolution, naming the new PR, and `Landed in <new sha>`. Several
        pairs, so neither the old nor the new fixture's text can be hard-coded; GitHub has or has not closed it."""
        for old, new, issue_state in ((FIXTURE_A, FIXTURE_B, "CLOSED"), (FIXTURE_B, FIXTURE_A, "OPEN"),
                                      (FIXTURE_C, FIXTURE_A, "CLOSED"), (FIXTURE_A, FIXTURE_C, "OPEN")):
            with self.subTest(old=old["pr"], new=new["pr"]):
                bodies = self.first_pass(old) + [self.REOPENED]
                p = self.land(new, issue_state=issue_state, comments=self.as_comments(bodies))
                self.assertSecondLanding(new, [f"https://github.com/o/r/pull/{old['pr']}", old["sha"],
                                               old["summary"]])
                ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
                self.assertEqual(len(ticket), 1, p.stdout)
                self.assertIn("commented", ticket[0])
                self.assertNotIn("already there", ticket[0])

    def test_second_landing_ignores_every_shape_of_an_earlier_passs_comments(self):
        """Only this landing's own text counts as already landed: `Landed in <this sha>` (a whole token) or
        `Landed in PR #<this PR>`, and a Resolution whose `PR:` line is this PR's URL. An older pass's comments
        in any shape do not, including ones that share a prefix with this sha, PR number or URL. A Resolution
        with no `PR:` line names no PR, so it is not this landing's either. Landing FIXTURE_B (PR #57)."""
        new = FIXTURE_B
        sha = new["sha"]
        old_sha = FIXTURE_A["sha"]
        cases = [
            ("Landed in PR #34 and its Resolution",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", old_sha), "Landed in PR #34"]),
            ("a Resolution with no PR line",
             [self.old_resolution(None, old_sha), f"Landed in {old_sha}"]),
            ("PR #570 and pull/570, which start with this PR's number and URL",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570"]),
            ("a different sha sharing this sha's first 12 characters",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:12] + "0" * 28),
              f"Landed in {sha[:12]}{'0' * 28}"]),
            ("an abbreviated sha that is a prefix of this one",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:7]), f"Landed in {sha[:7]}"]),
            ("a longer hex token that starts with this sha",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha + "abc"), f"Landed in {sha}abc"]),
            ("Landed in PR #570 followed by more text",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570, see notes"]),
            # a Resolution belongs to the PR its `PR:` line names, whatever sha it quotes
            ("another PR's Resolution quoting this sha, and no Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), "Reverted by hand."]),
            ("another PR's Resolution quoting this sha, and its Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), f"Landed in {old_sha}"]),
        ]
        for name, old in cases:
            with self.subTest(name):
                bodies = [old[0], old[1] + "\n\n" + HARNESS_MARK, self.REOPENED]
                self.land(new, issue_state="CLOSED", comments=self.as_comments(bodies))
                self.assertSecondLanding(new, ["A first pass that was reopened.", "old/first_pass.py"])

    def test_resumed_land_posts_only_what_is_missing(self):
        """A reopened ticket's second land, resumed: the ticket carries the first pass's two comments and some of
        this landing's. With this PR's Resolution and no `Landed in <this sha>`, only `Landed in` is posted; with
        both (or `Landed in` alone, which records the land), nothing is; `Landed in PR #<this PR>` counts as this
        landing's Landed comment too. Run with the fixtures both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            self.land(new)                                   # this landing's own comments, as the land posts them
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertEqual((len(res), len(mine)), (1, 1))
            by_pr = f"Landed in PR #{new['pr']}\n\n{HARNESS_MARK}"
            cases = [("this Resolution only", res, [f"Landed in {new['sha']}"]),
                     ("this Resolution and Landed in", res + mine, []),
                     ("this Landed in only", mine, []),
                     ("this Resolution and Landed in PR #", res + [by_pr], [])]
            for name, kept, wanted in cases:
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                    self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "no second merge")
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_counts_this_prs_resolution_by_its_pr_line(self):
        """A Resolution is this landing's when its `PR:` line is this PR's URL, whatever sha it quotes or none:
        then only `Landed in <this sha>` is posted, never a second Resolution. A `Landed in` for this PR with
        trailing text after the whole number (`PR #57, see notes`) records the land; nothing is posted."""
        new, old = FIXTURE_B, FIXTURE_A
        url = f"PR: https://github.com/o/r/pull/{new['pr']}"
        first = self.first_pass(old) + [self.REOPENED]
        cases = [("this PR's Resolution without a Squash commit line",
                  [self.old_resolution(url, None)],
                  [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution quoting another sha",
                  [self.old_resolution(url, FIXTURE_C["sha"])], [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution and Landed in PR #57 with more text",
                  [self.old_resolution(url, new["sha"]), f"Landed in PR #{new['pr']}, see notes\n\n{HARNESS_MARK}"],
                  [])]
        for name, kept, wanted in cases:
            with self.subTest(name):
                self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_land_without_a_merge_sha_on_a_reopened_ticket(self):
        """GitHub reports no merge commit, so the land comments `Landed in PR #<pr>`. A second landing still gets
        its own Resolution (naming this PR) and `Landed in PR #<pr>` despite the first pass's comments and an old
        `Landed in PR #570`; a resume with this PR's Resolution posts only `Landed in PR #<pr>`; with both,
        nothing; never a second Resolution. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            by_pr = f"Landed in PR #{new['pr']}"
            first = self.first_pass(old) + [f"Landed in PR #{new['pr']}0\n\n{HARNESS_MARK}", self.REOPENED]
            self.land(new, no_sha=True)                      # this landing's own comments, as the land posts them
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertEqual(len(res), 1)
            self.assertNotIn("Squash commit", res[0], "no merge sha, no squash line")
            with self.subTest(old=old["pr"], new=new["pr"], case="second landing"):
                self.land(new, issue_state="CLOSED", comments=self.as_comments(first), no_sha=True)
                posted = self.posted(12)
                self.assertEqual(len(posted), 2, f"want a Resolution and {by_pr}, got {posted}")
                found = self.resolutions(12)
                self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
                index, _, body = found[0]
                self.assertIn(f"PR: https://github.com/o/r/pull/{new['pr']}", body)
                self.assertIn(new["summary"], body)
                for text in (f"https://github.com/o/r/pull/{old['pr']}", old["sha"], old["summary"]):
                    self.assertNotIn(text, body, "the new Resolution is about this PR only")
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [by_pr])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")
            for name, kept, wanted in (("this Resolution only", res, [by_pr]), ("both", res + mine, [])):
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                              no_sha=True)
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_without_a_sha_knows_its_own_landed_comment_by_the_resolutions_squash_sha(self):
        """The first run knew the sha and posted this PR's Resolution (`Squash commit: S`) and `Landed in S`; the
        resume cannot read the merge commit (null, or the read fails). `Landed in S` is still this landing's,
        found through the Squash commit of the Resolution naming this PR, so nothing is posted. An older pass's
        `Landed in <its sha>`, whose Resolution names another PR, is not: with only this PR's Resolution and
        that, only `Landed in PR #<pr>` is posted. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            self.land(new)                                   # the first run of this landing, sha known
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertIn(f"Squash commit: {new['sha']}", res[0])
            self.assertEqual([b.replace(HARNESS_MARK, "").strip() for b in mine], [f"Landed in {new['sha']}"])
            for how in (True, "fail"):
                for name, kept, wanted in (("both", res + mine, []),
                                           ("this Resolution only", res, [f"Landed in PR #{new['pr']}"])):
                    with self.subTest(old=old["pr"], new=new["pr"], merge_commit=how, case=name):
                        self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                                  no_sha=how)
                        self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)],
                                         wanted)

    # ------------------------------------------------------------------ the PR body's shape
    def one_resolution(self):
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        return found[0]

    def test_land_resolution_carries_every_line_of_a_longer_summary(self):
        """A Summary of two paragraphs and a bullet list (FIXTURE_C, edited by hand): every line of it is copied,
        in order, ahead of the files; the Risks section stays out."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        self.assertResolution(body, FIXTURE_C)
        lines = body.rstrip().splitlines()
        at = []
        for text in FIXTURE_C["summary_lines"]:
            wanted = text[2:] if text.startswith("- ") else text
            at.append(self.first(lines, lambda line, w=wanted: w in line, f"summary line {wanted!r}"))
        self.assertEqual(at, sorted(at), "the summary keeps its order")
        self.assertLess(max(at), min(self.file_positions(lines, FIXTURE_C)), "the whole summary precedes the files")
        for text in FIXTURE_C["risks"] + [FIXTURE_A["summary"], FIXTURE_B["summary"]]:
            self.assertNotIn(text, body)

    def test_land_resolution_reads_the_checklist_from_the_definition_of_done_only(self):
        """FIXTURE_C again: the `- [ ]` follow-up under Risks is not part of the checklist, while the wrapped
        item's continuation line is, and stays with its item."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        lines = self.assertFrame(body, FIXTURE_C)
        for text in FIXTURE_C["risks"]:
            self.assertNotIn(text, body, "a checkbox outside the Definition of done is not copied")
        self.assertNotIn("nightly", body)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in FIXTURE_C["dod"]]
        wrapped = [i for i, line in enumerate(lines) if FIXTURE_C["dod_wrapped"] in line]
        self.assertEqual(len(wrapped), 1, f"the wrapped item's continuation line wants one line:\n{body}")
        self.assertTrue(at_dod[1] <= wrapped[0] < at_dod[2],
                        f"the continuation line belongs with its item, between {at_dod[1]} and {at_dod[2]}:\n{body}")

    def test_land_resolution_finds_the_verify_result_whatever_the_fence_layout(self):
        """The verify fence need not follow its label directly: a blank line between, a ```text fence, or the
        `## Verify` heading of the PR template harness-scaffold installs as .github/pull_request_template.md. The
        last line of the fence is the result either way."""
        variants = [("a blank line before the fence", "Verify:\n\n```\n",
                     ["Ran 7 tests in 0.512s", "", "OK (expected failures=1)"]),
                    ("a text fence", "Verify:\n```text\n",
                     ["lint: clean", "Ran 52 tests in 1.907s", "", "OK (skipped=3)"]),
                    ("the PR template's heading", "## Verify\n```\n",
                     ["python3 -m unittest: 61 tests, all green", "VERIFY OK"])]
        for name, label, verify in variants:
            with self.subTest(name):
                fx = dict(FIXTURE_A, verify=verify)
                rendered = pr_body_text(fx)
                self.assertEqual(rendered.count("Verify:\n```\n"), 1)
                fx["body"] = rendered.replace("Verify:\n```\n", label)
                self.land(fx)
                _, _, body = self.one_resolution()
                self.assertResolution(body, fx)

    def test_land_resolution_without_a_red_run_line_has_no_red_line(self):
        """A body with no red-run paragraph at all (written by hand on a no-tests ticket): the land finishes,
        the Resolution says nothing about a red run and invents no kind."""
        fx = dict(FIXTURE_B, body=pr_body_text(FIXTURE_B, red=False))
        self.assertNotIn("Red run", fx["body"])
        self.land(fx)
        index, _, body = self.one_resolution()
        lines = self.assertFrame(body, fx)
        self.file_positions(lines, fx)
        self.assertVerifyResult(lines, fx)
        self.assertEqual([line for line in lines if re.search(r"(?i)\bred\b", line)], [],
                         f"no red run in the PR body, so no red line in the Resolution:\n{body}")
        self.assertIsNone(re.search(r"\bimport\b", body), f"a red-run kind invented:\n{body}")
        self.assertNotIn("ModuleNotFoundError", body)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0])

    def test_land_without_summary_or_verify_still_posts_the_resolution_and_lands(self):
        """A hand-written body missing its Summary and/or its Verify block: the land finishes, one Resolution
        still names every file, the checklist, the PR and the sha, nothing is invented, and `Landed in` follows."""
        cases = [("no Summary", FIXTURE_A, {"summary": False}),
                 ("no Verify", FIXTURE_B, {"verify": False}),
                 ("neither", FIXTURE_B, {"summary": False, "verify": False})]
        for name, base, drop in cases:
            with self.subTest(name):
                fx = dict(base, body=pr_body_text(base, **drop))
                self.land(fx)
                index, number, body = self.one_resolution()
                self.assertEqual(number, "12")
                lines = self.assertFrame(body, fx)
                self.file_positions(lines, fx)
                for item in fx["dod"]:
                    self.assertIn(item, body, f"checklist line {item!r} missing:\n{body}")
                self.assertRedKind(lines, fx)
                if drop.get("summary") is False:
                    self.assertNotIn(fx["summary"], body, "no Summary in the PR body, none to copy")
                else:
                    self.assertIn(fx["summary"], body)
                if drop.get("verify") is False:
                    for text in fx["verify"]:
                        if text.strip():
                            self.assertNotIn(text, body, "no Verify in the PR body, none to copy")
                else:
                    self.assertVerifyResult(lines, fx)
                for text in fx["red_tail"] + ["## Tests", "## Review", "## Risks", "Closes #12"]:
                    self.assertNotIn(text, body)
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                                 [f"Landed in {fx['sha']}"])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    # ------------------------------------------------------------------ every changed file
    def files_listed(self, body):
        """(N of the one `Files changed (N):` line, the paths on the list lines that follow it)."""
        lines = body.splitlines()
        heads = [i for i, line in enumerate(lines) if line.strip().startswith("Files changed")]
        self.assertEqual(len(heads), 1, f"want one `Files changed (N):` line:\n{body}")
        m = re.match(r"^Files changed \((\d+)\):$", lines[heads[0]].strip())
        self.assertIsNotNone(m, f"the files line reads `Files changed (N):`, got {lines[heads[0]]!r}")
        paths = []
        for line in lines[heads[0] + 1:]:
            item = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if not item:
                break
            paths.append(item.group(1).replace("`", "").strip().split(" ")[0])
        return int(m.group(1)), paths

    def test_land_resolution_lists_every_changed_file_past_the_first_hundred(self):
        """`gh pr view --json files` stops at 100 files. A PR touching 130 gets all 130 in its Resolution, from
        the paginated files endpoint, and the count says 130; the rest of the Resolution and `Landed in` as
        usual."""
        fx = FIXTURE_WIDE
        self.land(fx, view_files=fx["files"][:100])
        index, number, body = self.one_resolution()
        self.assertEqual(number, "12")
        count, listed = self.files_listed(body)
        for nth in (101, 130):
            path = fx["files"][nth - 1]
            self.assertTrue(path in listed, f"changed file no. {nth}, {path}, missing from the Resolution, which "
                                            f"lists {len(listed)} files under `Files changed ({count}):`")
        self.assertEqual(count, 130, "want `Files changed (130):`")
        self.assertEqual(sorted(listed), sorted(fx["files"]), "every changed file, each listed once")
        self.assertResolution(body, fx)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_land_resolution_lists_a_short_prs_files_once(self):
        """Three files, listed alike by the PR view and the files endpoint: `Files changed (3):` and each path
        once, whichever the land reads."""
        self.land(FIXTURE_A)
        _, _, body = self.one_resolution()
        count, listed = self.files_listed(body)
        self.assertEqual(sorted(listed), sorted(FIXTURE_A["files"]), f"each changed file once:\n{body}")
        self.assertEqual(count, 3, f"want `Files changed (3):`:\n{body}")
        self.assertResolution(body, FIXTURE_A)

    # ------------------------------------------------------------------ a failed read after the merge
    def test_land_posts_nothing_when_the_pr_read_after_the_merge_fails_and_the_resume_posts_both(self):
        """The merge goes through, then reading the PR for the Resolution (`gh pr view P --json body,...`) fails:
        no comment at all on the ticket, since `Landed in` alone would keep a resume from adding the Resolution,
        and land fails naming the step. A second land on the merged PR posts one Resolution, then `Landed in`."""
        self.scenario(self.resolution_calls(FIXTURE_A, after={"stderr": "GraphQL: something\n", "exit": 1}))
        p = self.pr("land", "34", "--yes", cwd=self.main, stubs=False)
        self.assertOneLineError(p, "PR #34 is merged", "#12", "gh pr view 34", "GraphQL: something")
        self.assertRegex(p.stderr, r"(?i)\bresolution\b", "the error names the step that failed: the Resolution")
        argv = self.gh_argv()
        self.assertTrue(any(a[:2] == ["pr", "merge"] for a in argv), "the merge went through")
        self.assertTrue(any(a[:3] == ["pr", "view", "34"] and "--json" in a[:-1]
                            and "body" in a[a.index("--json") + 1].split(",") for a in argv),
                        "the land read the PR's body after the merge, and that read failed")
        self.assertEqual([a for a in argv if a[:2] == ["issue", "comment"]], [], "no issue comment at all")
        self.assertEqual(self.posted(), [], "no comment of any kind on the ticket")
        ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
        self.assertEqual(len(ticket), 1, p.stdout)
        self.assertNotIn("commented", ticket[0])

        bodies = [b for _, _, b in self.posted(12)]        # what the ticket shows the resume: nothing
        self.land(FIXTURE_A, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "the resume does not merge again")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"the resume posts one Resolution, got {len(found)}: {self.posted()}")
        index, _, body = found[0]
        self.assertResolution(body, FIXTURE_A)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")


EPIC_SAID = "#3: 1 of 2 tasks closed; left open\n"


def cost_block(ticket, pr):
    """A Cost block as `harness-work cost N --pr P --markdown` might print it; different for every ticket and PR."""
    tokens = 100_000 * ticket + 37 * pr
    return "\n".join([f"**Cost** (#{ticket}, PR #{pr})", "",
                      "| Stage | Model | Tokens | Model time |", "|---|---|---|---|",
                      f"| coder | opus | {tokens:,} | {ticket}m {pr % 60:02d}s |",
                      f"| reviewer | fable | {tokens // 3:,} | {pr % 7}m 10s |", "",
                      f"Total: {tokens + tokens // 3:,} tokens; rounds {ticket % 4 + 1}"])


class LandCost(WorktreeTest):
    """`land` runs `harness-work cost N --pr P --markdown --repo <main>` for each ticket whose Resolution it posts and
    adds the stdout, stripped, to that ticket's Resolution after a blank line. The harness-work stub answers per
    ticket (`cost N`), and the harness-board stub logs each comment's body, so a test reads the Resolution posted
    for every ticket. Two PRs, each closing two tickets, so neither a PR number nor a ticket can be hard-coded."""

    CASES = ((FIXTURE_A, [12, 13]), (FIXTURE_B, [21, 12]))

    def setUp(self):
        super().setUp()
        for n in (12, 13, 21):
            write(self.state_path("tickets", f"{n}.json"),
                  json.dumps({"number": n, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        self.costs({})

    def costs(self, answers):
        """The harness-work stub's answer to `cost N` for each ticket N in `answers`; nothing, exit 0, for others."""
        self.outputs(**{"harness-board": {"epic-check": {"stdout": EPIC_SAID}},
                        "harness-work": {f"cost {n}": answer for n, answer in answers.items()}})

    def cost_calls(self, fx, tickets, state="OPEN", comments=None):
        """The land conversation for the fixture's PR closing `tickets`; `comments`: {ticket: [bodies]} on it."""
        pr, sha = str(fx["pr"]), fx["sha"]
        url = f"https://github.com/o/r/pull/{pr}"
        comments = comments or {}
        calls = gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}") + [
            {"match": ["pr", "view", pr, "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}},
            {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
             "stdout_json": land_view(url=url, state=state,
                                      closingIssuesReferences=[{"number": n} for n in tickets])},
            REPO_VIEW,
            {"match": ["pr", "merge", pr], "stdout": ""}]
        for n in tickets:
            issue = {"state": "CLOSED" if state == "MERGED" else "OPEN",
                     "labels": [{"name": "task"}, {"name": "status:in-review"}],
                     "comments": [{"author": {"login": "rjeskuri"}, "body": b + "\n\n" + HARNESS_MARK,
                                   "createdAt": f"2026-10-13T1{k}:00:00Z"} for k, b in enumerate(comments.get(n, []))]}
            calls += [{"match": ["issue", "view", str(n), "--json", "state,labels,comments"], "stdout_json": issue},
                      {"match": ["issue", "edit", str(n)], "stdout": ""}]
        return calls + [{"match": ["pr", "view", pr], "stdout_json": pr_data(fx)}, *files_calls(pr, fx["files"])]

    def fresh_worktree(self):
        """The ticket worktree and branch again, as before the first land, so every land cleans up alike."""
        if not os.path.isdir(self.wt):
            self.git(self.main, "worktree", "prune")
            self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, self.wt, "origin/main")
            write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 2\n")
            self.git(self.wt, "add", "-A")
            self.git(self.wt, "commit", "-q", "-m", "Add the widget")

    def land(self, fx, tickets, cwd=None, **scenario):
        self.fresh_worktree()
        self.scenario(self.cost_calls(fx, tickets, **scenario))
        p = self.pr("land", str(fx["pr"]), "--yes", cwd=cwd or self.main)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def comments_on(self, n):
        """The bodies of the comments the land posted on ticket `n` through harness-board, in order."""
        return [a[a.index("--body") + 1] for a in self.stub_calls("harness-board")
                if a[:2] == ["comment", str(n)] and "--body" in a]

    def resolution_of(self, n):
        found = [b for b in self.comments_on(n) if b.lstrip().startswith(RESOLUTION)]
        self.assertEqual(len(found), 1, f"want one Resolution on #{n}, got {self.comments_on(n)}")
        return found[0]

    def baseline(self, fx, tickets):
        """A land with no Cost output at all: (the process, the Resolution text, before any block)."""
        self.costs({})
        p = self.land(fx, tickets)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        texts = {self.resolution_of(n) for n in tickets}
        self.assertEqual(len(texts), 1, "the Resolution without a Cost block is the PR's, the same for each ticket")
        base = texts.pop()
        self.assertEqual(base.rstrip().splitlines()[-1], f"Squash commit: {fx['sha']}", base)
        return p, base

    def assertLandedFollows(self, n, sha):
        bodies = self.comments_on(n)
        at = [i for i, b in enumerate(bodies) if b.lstrip().startswith(RESOLUTION)]
        self.assertEqual(bodies[at[0] + 1:], [f"Landed in {sha}"], f"`Landed in` follows the Resolution on #{n}")

    def no_cost_warnings(self, stderr):
        return [line for line in stderr.splitlines() if "no cost block" in line]

    def isolated_land(self, fx, tickets, work_next_to_tool):
        """A land by a copy of harness-pr in a plugin of its own (its lib and templates linked to the real ones),
        with no harness-work on PATH: the fakes but harness-work, the stubs and the system. With
        `work_next_to_tool`, the harness-work stub sits next to the copy, where a sibling tool is looked up after
        PATH; without it, there is no harness-work anywhere the land could find one."""
        plugin = self.mk("plugin-copy")
        bindir = os.path.join(plugin, "bin")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
            shutil.copy2(TOOL, os.path.join(bindir, "harness-pr"))
            real = os.path.dirname(os.path.dirname(TOOL))
            for part in ("lib", "templates"):
                os.symlink(os.path.join(real, part), os.path.join(plugin, part))
        stub = os.path.join(bindir, "harness-work")
        if os.path.lexists(stub):
            os.remove(stub)
        if work_next_to_tool:
            os.symlink(os.path.join(BINS["fakebin"], "harness-work"), stub)
        nowork = self.mk("fakebin-without-harness-work")
        for name in os.listdir(BINS["fakebin"]):
            if name != "harness-work" and not os.path.lexists(os.path.join(nowork, name)):
                os.symlink(os.path.join(BINS["fakebin"], name), os.path.join(nowork, name))
        path = os.pathsep.join([BINS["stubbin"], nowork, "/usr/bin", "/bin"])
        self.fresh_worktree()
        self.scenario(self.cost_calls(fx, tickets))
        p = subprocess.run([sys.executable, os.path.join(bindir, "harness-pr"), "land", str(fx["pr"]), "--yes"],
                           cwd=self.main, env=self.env(PATH=path), capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    # ------------------------------------------------------------------ broad
    def test_land_adds_each_tickets_cost_block_to_its_resolution(self):
        for fx, tickets in self.CASES:
            with self.subTest(pr=fx["pr"], tickets=tickets):
                _, base = self.baseline(fx, tickets)
                blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
                # stdout with blank lines around it and trailing spaces: the block is the stdout stripped
                self.costs({n: {"stdout": "\n" + b + "  \n\n"} for n, b in blocks.items()})
                p = self.land(fx, tickets)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                for n in tickets:
                    body = self.resolution_of(n)
                    self.assertEqual(body, base + "\n\n" + blocks[n],
                                     f"#{n}: the Resolution, a blank line, then its own Cost block")
                    for other in tickets:
                        if other != n:
                            self.assertNotIn(blocks[other], body, f"#{n} carries #{other}'s block")
                    lines = body.splitlines()
                    self.assertIn(f"PR: https://github.com/o/r/pull/{fx['pr']}", lines, "the PR: line stays")
                    squash = lines.index(f"Squash commit: {fx['sha']}")
                    self.assertEqual(lines[squash + 1:squash + 3], ["", blocks[n].splitlines()[0]],
                                     "the block follows the Squash commit line after one blank line")
                    self.assertLandedFollows(n, fx["sha"])
                self.assertEqual(self.no_cost_warnings(p.stderr), [])

    def test_cost_runs_after_the_merge_with_the_pr_and_the_main_checkout(self):
        for (fx, tickets), cwd in zip(self.CASES, (self.wt, self.main)):     # from the worktree too: --repo is main
            with self.subTest(pr=fx["pr"], tickets=tickets, cwd=cwd):
                self.costs({n: {"stdout": cost_block(n, fx["pr"]) + "\n"} for n in tickets})
                p = self.land(fx, tickets, cwd=cwd)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                pr = str(fx["pr"])
                self.assertEqual(self.stub_calls("harness-work"),
                                 [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in tickets],
                                 "one cost run per ticket, with the PR and the main checkout")
                merge = self.position(f"gh pr merge {pr}")
                for n in tickets:
                    cost = self.position(f"harness-work cost {n} --pr {pr} --markdown --repo {self.main}")
                    posted = self.position(f"harness-board comment {n} --body {RESOLUTION}")
                    self.assertLess(merge, cost, f"the cost of #{n} runs after the merge")
                    self.assertLess(cost, posted, f"and before #{n}'s Resolution is posted")

        fx, tickets = FIXTURE_B, [21, 12]
        pr = str(fx["pr"])
        with self.subTest("harness-work next to harness-pr, not on PATH: the sibling tool is run from there"):
            blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
            self.costs({n: {"stdout": b + "\n"} for n, b in blocks.items()})
            p = self.isolated_land(fx, tickets, work_next_to_tool=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(self.no_cost_warnings(p.stderr), [], p.stderr)
            self.assertEqual(self.stub_calls("harness-work"),
                             [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in tickets])
            for n in tickets:
                self.assertTrue(self.resolution_of(n).endswith("\n\n" + blocks[n]), self.resolution_of(n))

        with self.subTest("the cost run gets no stdin: what the land was given is not passed on"):
            seen = os.path.join(self.tmp, "cost-stdin.jsonl")
            self.costs({n: {"stdout": cost_block(n, fx["pr"]) + "\n", "read_stdin": seen} for n in tickets})
            self.fresh_worktree()
            self.scenario(self.cost_calls(fx, tickets))
            p = subprocess.run([sys.executable, TOOL, "land", pr, "--yes"], cwd=self.main, env=self.env(),
                               input="typed at the land\n", capture_output=True, text=True, timeout=120)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(self.jsonl(seen), ["" for _ in tickets], "each cost run reads an empty stdin")

    # ------------------------------------------------------------------ specific
    def test_a_failing_cost_run_warns_and_lands_without_a_block(self):
        """#13's cost run fails; #12's works. One warning line for #13, its Resolution as without cost, `Landed
        in` after it, #12 with its block, and the land's stdout and exit as a land with no Cost output."""
        fx, tickets = FIXTURE_A, [12, 13]
        before, base = self.baseline(fx, tickets)
        good = cost_block(12, fx["pr"])
        cases = [("exit 1", {"stderr": "harness-work: no session transcripts for #13\n", "exit": 1},
                  "no session transcripts for #13"),
                 ("exit 2 with stdout", {"stdout": "**Cost** partial table\n",
                                         "stderr": "harness-work: cannot read the attempt store\n", "exit": 2},
                  "cannot read the attempt store"),
                 # a many-line stderr still makes one warning line, from its first line; the rest is not shown
                 ("exit 1 with a many-line stderr",
                  {"stderr": "harness-work: first trouble\ntrace detail one\n  trace detail two\n", "exit": 1},
                  "first trouble"),
                 # nothing said at all: the reason is the exit status
                 ("exit 3 saying nothing", {"exit": 3}, "exit 3"),
                 # only stdout said something: stdout is not a reason, so it falls back to the exit status,
                 # and none of the stdout reaches the warning, the stderr or the Resolution
                 ("exit 4 with only a partial table", {"stdout": "**Cost** partial table\n", "exit": 4}, "exit 4")]
        for name, answer, reason in cases:
            with self.subTest(name):
                self.costs({12: {"stdout": good + "\n"}, 13: answer})
                p = self.land(fx, tickets)
                self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
                self.assertEqual(p.stdout, before.stdout, "the land's report is as without the cost")
                warned = self.no_cost_warnings(p.stderr)
                self.assertEqual(len(warned), 1, p.stderr)
                prefix = "harness-pr: warning: no cost block for #13: "
                self.assertTrue(warned[0].startswith(prefix), warned[0])
                self.assertTrue(warned[0][len(prefix):].strip(), f"the warning gives a reason: {warned[0]!r}")
                self.assertIn(reason, warned[0], "the warning gives the reason")
                self.assertNotIn("trace detail", p.stderr, "one line: the rest of harness-work's stderr is not shown")
                self.assertNotIn("partial table", p.stderr, "harness-work's stdout is never the reason")
                self.assertEqual([line for line in p.stderr.splitlines() if line not in warned],
                                 before.stderr.splitlines(), "no other stderr than a land without cost")
                self.assertEqual(self.resolution_of(13), base, "#13's Resolution goes out without a block")
                self.assertNotIn("partial table", "\n".join(self.comments_on(13)))
                self.assertLandedFollows(13, fx["sha"])
                self.assertEqual(self.resolution_of(12), base + "\n\n" + good)
                self.assertLandedFollows(12, fx["sha"])

        with self.subTest("harness-work cannot be run"):
            broken = self.mk("brokenbin")
            executable(os.path.join(broken, "harness-work"), "#!/nonexistent/harness-interpreter\n")
            path = os.pathsep.join([broken, BINS["stubbin"], BINS["fakebin"], "/usr/bin", "/bin"])
            self.fresh_worktree()
            self.scenario(self.cost_calls(fx, tickets))
            p = self.pr("land", str(fx["pr"]), "--yes", cwd=self.main, PATH=path)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
            self.assertEqual(p.stdout, before.stdout)
            warned = self.no_cost_warnings(p.stderr)
            for n in tickets:
                mine = [line for line in warned if line.startswith(f"harness-pr: warning: no cost block for #{n}: ")]
                self.assertEqual(len(mine), 1, f"one warning for #{n}:\n{p.stderr}")
                self.assertTrue(mine[0].split(f"#{n}: ", 1)[1].strip(), "the warning gives a reason")
                self.assertEqual(self.resolution_of(n), base)
                self.assertLandedFollows(n, fx["sha"])
            self.assertEqual(len(warned), len(tickets), p.stderr)

        with self.subTest("no harness-work on PATH or next to harness-pr"):
            p = self.isolated_land(fx, tickets, work_next_to_tool=False)
            self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
            self.assertEqual(p.stdout, before.stdout)
            self.assertEqual(self.stub_calls("harness-work"), [], "no harness-work was run")
            warned = self.no_cost_warnings(p.stderr)
            for n in tickets:
                mine = [line for line in warned if line.startswith(f"harness-pr: warning: no cost block for #{n}: ")]
                self.assertEqual(len(mine), 1, f"one warning for #{n}:\n{p.stderr}")
                self.assertTrue(mine[0].split(f"#{n}: ", 1)[1].strip(), "the warning gives a reason")
                self.assertEqual(self.resolution_of(n), base)
                self.assertLandedFollows(n, fx["sha"])
            self.assertEqual(len(warned), len(tickets), p.stderr)
            self.assertEqual([line for line in p.stderr.splitlines() if line not in warned],
                             before.stderr.splitlines(), "no other stderr than a land without cost")

    def test_empty_cost_output_adds_nothing(self):
        for fx, tickets in self.CASES:
            for name, stdout in (("empty", ""), ("only blank lines", "\n  \n\n")):
                with self.subTest(pr=fx["pr"], output=name):
                    before, base = self.baseline(fx, tickets)
                    self.costs({n: {"stdout": stdout} for n in tickets})
                    p = self.land(fx, tickets)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual([a[:2] for a in self.stub_calls("harness-work")],
                                     [["cost", str(n)] for n in tickets], "the cost ran for each ticket")
                    for n in tickets:
                        self.assertEqual(self.resolution_of(n), base, f"#{n}: no block, not even a blank line")
                        self.assertLandedFollows(n, fx["sha"])
                    self.assertEqual(self.no_cost_warnings(p.stderr), [], "empty output is no warning")
                    self.assertEqual(p.stderr, before.stderr)
                    self.assertEqual(p.stdout, before.stdout)

    def test_a_resumed_land_with_a_resolution_runs_no_cost(self):
        """The first land posted each ticket's Resolution with its Cost block (and `Landed in`). A resumed land
        recognises that Resolution by its `PR:` line: no cost run and no second Resolution for a ticket that has
        it; a ticket without it still gets its Resolution, with its block, and only its cost runs."""
        for fx, tickets in self.CASES:
            pr, sha = str(fx["pr"]), fx["sha"]
            blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
            self.costs({n: {"stdout": b + "\n"} for n, b in blocks.items()})
            p = self.land(fx, tickets)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            first = {n: self.resolution_of(n) for n in tickets}
            for n in tickets:
                self.assertTrue(first[n].endswith("\n\n" + blocks[n]), f"the first land's Resolution of #{n}")
            done, rest = tickets
            cases = [("each ticket has its Resolution and Landed in",
                      {n: [first[n], f"Landed in {sha}"] for n in tickets}, [], {}),
                     ("each ticket has only its Resolution",
                      {n: [first[n]] for n in tickets}, [], {n: [f"Landed in {sha}"] for n in tickets}),
                     (f"#{done} has its Resolution, #{rest} nothing",
                      {done: [first[done]]}, [rest],
                      {done: [f"Landed in {sha}"], rest: [first[rest], f"Landed in {sha}"]})]
            for name, comments, ran, posted in cases:
                with self.subTest(pr=fx["pr"], case=name):
                    p = self.land(fx, tickets, state="MERGED", comments=comments)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "no second merge")
                    self.assertEqual(self.stub_calls("harness-work"),
                                     [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in ran],
                                     "the cost runs only for a ticket whose Resolution is posted")
                    for n in tickets:
                        self.assertEqual(self.comments_on(n), posted.get(n, []), f"what the resume posts on #{n}")


class LandRefusals(PrTest):
    def test_land_refuses_before_merging(self):
        cases = [("draft", land_view(isDraft=True), "is a draft"),
                 ("conflicting", land_view(mergeable="CONFLICTING"), "conflicts with main"),
                 ("cancelled", land_view(statusCheckRollup=[check_run("build", conclusion="CANCELLED")]),
                  "checks are red on PR #34: build (cancelled)"),
                 ("failed", land_view(statusCheckRollup=[check_run("test", conclusion="FAILURE"), check_run("lint")]),
                  "checks are red on PR #34: test (failure)"),
                 ("pending", land_view(statusCheckRollup=PENDING), "still running"),
                 ("closed unmerged", land_view(state="CLOSED"), "closed without being merged")]
        for name, view, needle in cases:
            with self.subTest(name):
                self.scenario(gate_calls(commits=[pr_commit("a", at(9))])
                              + [{"match": ["pr", "view", "34"], "stdout_json": view}, REPO_VIEW,
                                 {"match": ["pr", "merge"], "stdout": ""}])
                self.assertOneLineError(self.pr("land", "34"), needle)
                self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))


class LandGate(GateAsserts, WorktreeTest):
    """`harness-pr land P` refuses a PR whose latest harness review has findings and no commit after it, unless
    --accept-findings; it lands the PR when a commit is newer, the latest harness review is clean, or there is none."""

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    def land_case(self, name, *flags):
        reviews, commits, named, not_named = GATE_CASES[name]
        self.scenario(Land.land_calls(self, reviews=reviews, commits=commits))
        return self.pr("land", "34", *flags, cwd=self.main), named, not_named

    def branch_exists(self):
        return Land.branch_exists(self)

    def assertLanded(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(["pr", "merge", "34", "-R", "o/r", "--squash", "--delete-branch"], self.gh_argv())
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))

    def test_land_refuses_unaddressed_findings_of_the_latest_harness_review(self):
        refused = [name for name, case in GATE_CASES.items() if case[2] is not None]
        self.assertGreaterEqual(len(refused), 5)
        for name in refused:
            with self.subTest(name):
                p, named, not_named = self.land_case(name)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any("worktree remove" in line or "branch -D" in line for line in self.order()))
                self.assertTrue(os.path.isdir(self.wt))
                self.assertTrue(self.branch_exists())
                self.assertNamesFindings(p, named, not_named)
                self.assertNotListedCount(name, p)

    def test_land_refusal_counts_the_high_and_medium_findings_listed_and_not_listed(self):
        """land's refusal carries the same "has <n> high or medium finding(s) and no commit after it" as ready's."""
        for name, when, n in GATE_REFUSAL_COUNTS:
            with self.subTest(name):
                p, _, _ = self.land_case(name)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertRegex(p.stderr, re.escape(f"PR #34: the latest harness review ({when}) has {n} high or "
                                                     "medium finding") + r"s? and no commit after it: ")
                self.assertIn("; fix them and push, or harness-pr land 34 --accept-findings", p.stderr)

    def test_land_proceeds_when_only_low_findings_are_open(self):
        """The same PR with a high finding refuses; with only low findings and no commit since, it lands."""
        self.assertTwins("findings and no commit since", "only low findings and no commit since")

    def test_land_proceeds_when_the_summary_counts_only_more_low_findings(self):
        self.assertTwins("the summary counts more high than are listed", "the summary counts more low than are listed")

    # Each proceed case first runs its refused twin (the same PR but for the one fact that decides), so the pair
    # shows that fact is what lets the land through.
    def assertTwins(self, refused, proceeds):
        p, _, _ = self.land_case(refused)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
        p, _, _ = self.land_case(proceeds)
        self.assertLanded(p)

    def test_land_proceeds_when_a_commit_is_newer_than_the_findings(self):
        self.assertTwins("findings and no commit since", "a commit after the findings")

    def test_land_proceeds_when_the_latest_harness_review_is_clean(self):
        self.assertTwins("an older clean review, the latest with findings (listed newest first)",
                         "an older review with findings, the latest clean")

    def test_land_proceeds_without_a_harness_review(self):
        self.assertTwins("the owner reviewed after the harness findings", "no harness review, only the owner's")

    def test_accept_findings_overrides_the_refusal(self):
        p, _, _ = self.land_case("two reviews with findings, a commit between them")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)            # refused without the flag
        p, _, _ = self.land_case("two reviews with findings, a commit between them", "--accept-findings")
        self.assertLanded(p)

    def failing_land(self, failing, *flags):
        # The PR would land (a commit after the findings), so only the failed read can refuse it.
        reviews, commits, named, _ = GATE_CASES["a commit after the findings"]
        self.assertIsNone(named)
        self.scenario([failing] + Land.land_calls(self, reviews=reviews, commits=commits))
        return self.pr("land", "34", *flags, cwd=self.main)

    def test_land_fails_closed_when_a_gate_read_fails(self):
        for what, failing in GATE_READ_FAILURES.items():
            with self.subTest(what):
                p = self.failing_land(failing)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn(GATE_FAILURE_TEXT, p.stderr)
                self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any("worktree remove" in line or "branch -D" in line for line in self.order()))
                self.assertTrue(os.path.isdir(self.wt))
                self.assertTrue(self.branch_exists())

    def test_accept_findings_lands_through_a_failed_gate_read_with_a_warning(self):
        # gh repo view is left out: the merge itself needs the repository's name, so no flag can land without it.
        for i, (what, failing) in enumerate(GATE_API_FAILURES.items()):
            with self.subTest(what):
                if i:
                    self.setUp()                                  # a fresh repository and worktree for each case
                self.assertNotEqual(self.failing_land(failing).returncode, 0)          # refused without the flag
                self.assertTrue(os.path.isdir(self.wt))
                p = self.failing_land(failing, "--accept-findings")
                self.assertLanded(p)
                self.assertIn(GATE_FAILURE_TEXT, p.stderr)                           # the warning names the failure


# =========================================================================== notes
NOTES_TEXTS = [
    "Second pass: the first review found the frob loop off by one; fixed in widget.py.",
    ("Second pass, after the review:\n\n- `widget.py` frobs from a copy now (finding 1).\n"
     "- Left `legacy/frob_old.py` alone: out of scope for #12.\n\nFollow-up: ticket the legacy cleanup."),
    ("Out of scope but touched: `docs/gizmo.md` (a typo fix that rode along).\n```\nkept as is\n```\n"
     "The placeholders {{risks}} and {{summary}} in this text stay literal."),
    # sub-headings (`###`) belong to the Notes section; only a `## ` heading outside a code fence ends it
    "Second pass notes.\n\n### Files outside scope\n- `docs/gizmo.md`\n\n### Why\nIt rode along with the fix.",
    # a fenced block that holds `## ` lines, one of them the literal Definition of done heading, after a blank line
    ("Out of scope: the parser fixture.\n\n```\n\n## Definition of done\n## not a heading\n```\n"
     "Tail line after the fence."),
]


class OpenNotes(WorktreeTest):
    """`harness-pr open N --notes F`: the file's markdown becomes a `## Notes` section of the PR body between
    Summary and Definition of done, and the Resolution comment `land` posts repeats it."""

    def setUp(self):
        super().setUp()
        self.verify_log = write(os.path.join(self.tmp, "verify.log"), "verify line 1\nVERIFY OK\n")
        self.review = write(os.path.join(self.tmp, "findings.json"), json.dumps(FINDINGS))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("low"))},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"},
                                          "epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    # ------------------------------------------------------------------ open
    def open_body(self, notes=None, existing=False):
        """The PR body `harness-pr open` hands to gh: with --notes F when `notes` is the text of the file."""
        self.scenario([{"match": ["pr", "list"], "stdout_json": [{"number": 34, "url": PR_URL}] if existing else []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["pr", "edit", "34"], "stdout": PR_URL + "\n"}])
        args = ["open", "12", "--verify-log", self.verify_log, "--review", self.review]
        if notes is not None:
            args += ["--notes", write(os.path.join(self.tmp, "notes.md"), notes)]
        self.ok(*args, cwd=self.wt)
        calls = [c for c in self.gh_calls() if c["argv"][:2] in (["pr", "create"], ["pr", "edit"])]
        self.assertEqual(len(calls), 1, self.gh_argv())
        return calls[0]["stdin"]

    @staticmethod
    def headings(body):
        """[(line index, `## ` heading line)] outside code fences, as the section parser sees them."""
        found, fenced = [], False
        for i, line in enumerate(body.splitlines()):
            if re.match(r"^\s{0,3}(```|~~~)", line):
                fenced = not fenced
            elif not fenced and line.startswith("## "):
                found.append((i, line))
        return found

    def notes_section(self, body):
        """The text between the `## Notes` heading and the next `## ` heading outside a code fence (blank lines at
        the ends dropped)."""
        heads = self.headings(body)
        self.assertEqual([h for _, h in heads].count("## Notes"), 1, body)
        at = [i for i, h in heads if h == "## Notes"][0]
        end = next((i for i, _ in heads if i > at), len(body.splitlines()))
        return "\n".join(body.splitlines()[at + 1:end]).strip("\n")

    def test_notes_file_becomes_a_section_in_place(self):
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                lines = body.splitlines()
                self.assertEqual([h for _, h in self.headings(body)],
                                 ["## Summary", "## Notes", "## Definition of done", "## Tests", "## Review",
                                  "## Risks"], body)
                self.assertEqual(self.notes_section(body), text.strip("\n"))
                self.assertLess(body.index("## Summary"), body.index("## Notes"))
                self.assertLess(body.index("## Notes"), body.index("## Definition of done"))
                # directly under Summary (its text, a blank line, the heading) and directly above the checklist
                self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n\n## Notes\n", body)
                self.assertIn(text + "\n\n## Definition of done\n- [ ] The widget frobs twice", body)
                self.assertEqual(lines[0], "Closes #12")
                self.assertEqual(body.count(text), 1)
                self.assertNotIn("{{number}}", body)
                if "{{risks}}" in text:
                    self.assertIn("{{risks}} and {{summary}}", body)

    def test_notes_change_nothing_else_in_the_body(self):
        plain = self.open_body()
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                section = f"## Notes\n{text}\n\n"
                self.assertEqual(body.count(section), 1, body)
                self.assertEqual(body.replace(section, "", 1), plain)

    def test_notes_are_placed_by_the_template_not_by_the_first_matching_text(self):
        """A request whose first paragraph mentions `## Definition of done` (in a code span) puts that text in the
        Summary, ahead of the real heading: the Notes still go between the two real headings."""
        ticket = board_ticket("low")
        ticket["sections"]["request"] = ("Add the `## Definition of done` heading to the widget docs, after "
                                         "`## Summary`.\nIt must render.\n\nThe second paragraph is not the summary.")
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        summary = "Add the `## Definition of done` heading to the widget docs, after `## Summary`. It must render."
        plain = self.open_body()
        self.assertIn(f"## Summary\n{summary}\n\n## Definition of done\n", plain)
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                self.assertEqual([h for _, h in self.headings(body)],
                                 ["## Summary", "## Notes", "## Definition of done", "## Tests", "## Review",
                                  "## Risks"], body)
                self.assertIn(f"## Summary\n{summary}\n\n## Notes\n{text}\n\n## Definition of done\n", body)
                self.assertEqual(self.notes_section(body), text)
                self.assertEqual(body.replace(f"## Notes\n{text}\n\n", "", 1), plain)

    def test_notes_reach_the_edit_of_an_existing_pr(self):
        body = self.open_body(NOTES_TEXTS[1], existing=True)
        self.assertEqual(self.gh_argv()[1], ["pr", "edit", "34", "--body-file", "-"])
        self.assertEqual(self.notes_section(body), NOTES_TEXTS[1])

    def test_no_notes_no_heading(self):
        body = self.open_body()
        self.assertNotIn("Notes", body)
        self.assertEqual([line for line in body.splitlines() if line.startswith("## ")],
                         ["## Summary", "## Definition of done", "## Tests", "## Review", "## Risks"])
        self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n\n## Definition of done\n", body)
        self.assertNotIn("{{", body)

    def test_a_missing_notes_file_stops_before_anything_is_pushed(self):
        self.scenario([])
        p = self.pr("open", "12", "--notes", os.path.join(self.tmp, "no-such-notes.md"), cwd=self.wt)
        self.assertOneLineError(p, "no-such-notes.md")
        self.assertNotIn("unrecognized", p.stderr)            # the flag exists; it is the file that is missing
        self.assertIn("notes", p.stderr.replace("no-such-notes.md", ""))
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()), self.order())

    def test_a_notes_file_with_a_second_level_heading_is_refused(self):
        """A `## ` line outside a code fence would end the Notes section early and land would drop the rest, so
        `open` refuses the file before anything is pushed. `###` headings and fenced `## ` lines are fine."""
        cases = {"Out of scope": "## Out of scope\n- `legacy/frob_old.py` left alone.\n",
                 "Follow-ups": "Second pass: fixed the loop.\n\n## Follow-ups\nTicket the cleanup.\n",
                 "Why it rode along": "Text first.\n```\n## fenced, fine\n```\n## Why it rode along   \nmore\n"}
        for heading, text in cases.items():
            with self.subTest(heading=heading):
                self.scenario([])
                path = write(os.path.join(self.tmp, "second-pass-notes.md"), text)
                p = self.pr("open", "12", "--verify-log", self.verify_log, "--notes", path, cwd=self.wt)
                self.assertOneLineError(p, "second-pass-notes.md", heading)
                self.assertNotIn("unrecognized", p.stderr)
                self.assertEqual(self.gh_calls(), [])
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()),
                                 self.order())
        # the same file without its heading line is accepted, so the refusal is about the heading
        body = self.open_body("Second pass: fixed the loop.\n\n### Follow-ups\nTicket the cleanup.\n")
        self.assertIn("### Follow-ups", body)

    def test_a_notes_file_with_a_bare_second_level_heading_is_refused(self):
        """`## ` with nothing after it is still a heading to the section parser (it ends the Notes section), so it
        is refused like any other; there is no heading text to name, so the error names the file."""
        cases = {"bare, one space": "Second pass.\n\n## \nLeft alone.\n",
                 "bare, trailing spaces": "## \t  \nOut of scope: the legacy parser.\n",
                 "bare, after a fence": "Text.\n```\n## in a fence\n```\n##   \nmore\n"}
        for what, text in cases.items():
            with self.subTest(what):
                self.scenario([])
                path = write(os.path.join(self.tmp, "bare-heading-notes.md"), text)
                p = self.pr("open", "12", "--verify-log", self.verify_log, "--notes", path, cwd=self.wt)
                self.assertOneLineError(p, "bare-heading-notes.md")
                self.assertNotIn("unrecognized", p.stderr)
                self.assertEqual(self.gh_calls(), [])
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()),
                                 self.order())

    # ------------------------------------------------------------------ land
    resolution_calls = LandResolution.resolution_calls
    posted = LandResolution.posted
    resolutions = LandResolution.resolutions

    def land_with_notes(self, fx, notes, where="before_dod"):
        """Land the fixture's PR whose body holds a Notes section: where `harness-pr open --notes` renders it
        (`before_dod`), or hand-moved to sit between Review and Risks (`after_review`) or at the very end (`last`)."""
        text = pr_body_text(fx)
        section = f"## Notes\n{notes}\n\n"
        if where == "before_dod":
            body = text.replace("## Definition of done", section + "## Definition of done", 1)
        elif where == "after_review":
            body = text.replace("## Risks", section + "## Risks", 1)
        else:
            body = text + "\n\n" + section.rstrip("\n") + "\n"
        self.assertEqual(body.count("## Notes\n"), 1)
        self.scenario(self.resolution_calls(dict(fx, body=body)))
        self.ok("land", str(fx["pr"]), "--yes", cwd=self.main, stubs=False)
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        return found[0][2]

    def test_resolution_repeats_the_notes(self):
        cases = ((FIXTURE_A, 1, "before_dod"), (FIXTURE_B, 2, "before_dod"), (FIXTURE_A, 0, "before_dod"),
                 (FIXTURE_A, 4, "before_dod"),        # a fenced `## Definition of done` inside the notes
                 (FIXTURE_B, 4, "after_review"), (FIXTURE_A, 3, "after_review"), (FIXTURE_B, 3, "last"),
                 (FIXTURE_A, 4, "last"), (FIXTURE_B, 1, "last"))
        for fx, n, where in cases:
            text = NOTES_TEXTS[n]
            with self.subTest(pr=fx["pr"], notes=n, where=where):
                resolution = self.land_with_notes(fx, text, where)
                self.assertIn(text, resolution)
                self.assertEqual(resolution.count(text), 1)
                lines = resolution.rstrip().splitlines()
                self.assertEqual(lines[0], RESOLUTION)
                self.assertEqual(lines[-1].strip(), HARNESS_MARK)
                # in a sensible place: after the summary, before the PR link and squash sha that close it
                at_notes = resolution.index(text)
                self.assertGreater(at_notes, resolution.index(fx["summary"]))
                self.assertLess(at_notes, resolution.index(f"PR: https://github.com/o/r/pull/{fx['pr']}"))
                self.assertLess(at_notes, resolution.index(f"Squash commit: {fx['sha']}"))
                # the rest of the Resolution is as before: the checklist, the files, no other PR body section
                for item in fx["dod"]:
                    self.assertIn(item, resolution)
                for path in fx["files"]:
                    self.assertIn(f"- `{path}`", resolution)
                for left_out in ("## Tests", "## Review", "## Risks", "Closes #12", fx["review"], "None noted."):
                    self.assertNotIn(left_out, resolution)
                # the Notes do not swallow the Definition of done that follows them in the PR body
                self.assertEqual(resolution.count("Definition of done:"), 1)

    def test_notes_do_not_replace_the_resolutions_proof_lines(self):
        """Notes sit ahead of `## Tests` in the PR body. A `Red run ...` line and a `Verify:` label with a fence
        inside the Notes are the Notes' own text: the Resolution's red-run and verify lines come from the Tests."""
        notes = ("Second pass.\n\nRed run of the added test: assertion, exit 1.\n\nVerify:\n```\n"
                 "notes-only check output\nNOTES VERIFY PASSED\n```\nThat is all.")
        for fx, red, verify in ((FIXTURE_A, "Red run before implementation (assertion, exit 1)", "OK (skipped=1)"),
                                (FIXTURE_B, "Red run before implementation (import, exit 2)",
                                 "VERIFY PASSED: 212 checks, 0 warnings")):
            with self.subTest(pr=fx["pr"]):
                resolution = self.land_with_notes(fx, notes)
                lines = resolution.rstrip().splitlines()
                self.assertEqual([x for x in lines if x.startswith("Red run before implementation")], [red])
                self.assertEqual([x for x in lines if x.startswith("Verify: ")], [f"Verify: {verify}"])
                self.assertNotIn("Verify: NOTES VERIFY PASSED", resolution)
                # the Notes still appear, whole, ahead of the closing block that holds the real lines
                self.assertEqual(resolution.count(notes), 1)
                closing = resolution.index(red)
                self.assertLess(resolution.index(notes), closing)
                self.assertLess(closing, resolution.index(f"Verify: {verify}"))
                pr_line = f"PR: https://github.com/o/r/pull/{fx['pr']}"
                self.assertLess(resolution.index(f"Verify: {verify}"), resolution.index(pr_line))

    def test_resolution_without_notes_has_no_notes(self):
        self.scenario(self.resolution_calls(FIXTURE_A))
        self.ok("land", "34", "--yes", cwd=self.main, stubs=False)
        found = self.resolutions()
        self.assertEqual(len(found), 1)
        self.assertNotIn("Notes", found[0][2])


if __name__ == "__main__":
    unittest.main()
