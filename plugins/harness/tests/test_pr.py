"""Tests for harness-pr, driven by the fake gh (fakegh.py), stub sibling tools and a fake claude.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME and real git repositories: a light one (a repository with an `origin` remote
that is never contacted) for the subcommands that only talk to gh, and for open and land a bare origin, a
main checkout pushed to it and a ticket worktree on t/12-add-the-widget. `fakebin` holds gh (fakegh.py
behind a wrapper), git (a wrapper that logs and runs the real git), claude, terminal-notifier and osascript
fakes, and python3; `stubbin` holds harness-board and harness-red stubs that log their calls and print
canned output. gh, git and the stubs also append one line per call to ORDER_LOG, so a test can check the
order across tools. The fake executables are written once for the module: macOS checks a new executable on
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
answer = outputs.get(argv[0] if argv else "", {})
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
        p = self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS))
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


def harness_review(rid, when, inline=(), body_only=(), model="fable"):
    """(the review, its inline comments) as `harness-pr review` posts them; a finding: (path, line, severity, body)."""
    findings = list(inline) + list(body_only)
    if not findings:
        body = f"Harness review: no findings. Checked: the diff against the ticket's definition of done\n\n{MARKER}"
    else:
        counts = {s: sum(1 for f in findings if f[2] == s) for s in ("high", "medium", "low")}
        parts = [f"Harness review ({model}): {len(findings)} finding{'s' if len(findings) != 1 else ''}, "
                 f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low."]
        if body_only:
            parts.append("\n".join(["Not on a changed line:"] + [f"- `{p}:{n}` [{s}] {b}" for p, n, s, b in body_only]))
        parts.append(MARKER)
        body = "\n\n".join(parts)
    review = {"id": rid, "user": {"login": "rjeskuri"}, "body": body, "state": "COMMENTED", "submitted_at": when,
              "commit_id": "c" * 40}
    comments = [{"id": rid * 100 + i, "pull_request_review_id": rid, "user": {"login": "rjeskuri"}, "path": p,
                 "line": n, "original_line": n, "side": "RIGHT", "body": f"[{s}] {b}\n\n{MARKER}", "created_at": when}
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
# name: (reviews, commits, the findings a refusal names or None for proceed, findings it must not name)
GATE_CASES = {
    "findings and no commit since": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES])], [pr_commit("a", at(9))],
        [FROBS, NOTES], []),
    "a commit after the findings": (
        [harness_review(101, at(10), inline=[FROBS], body_only=[NOTES])],
        [pr_commit("a", at(9)), pr_commit("b", at(11))],
        None, []),
    "an older review with findings, the latest clean": (
        [harness_review(101, at(10), inline=[OLD_NIT]), harness_review(102, at(12))], [pr_commit("a", at(9))],
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
        [harness_review(101, at(10), inline=[OLD_NIT]),
         harness_review(102, at(12), inline=[OFF_BY_ONE], body_only=[README])],
        [pr_commit("a", at(9)), pr_commit("b", at(11))], [OFF_BY_ONE, README], [OLD_NIT]),
    "every finding in the review body": (
        [harness_review(101, at(10), body_only=[LIMIT])], [pr_commit("a", at(9))], [LIMIT], []),
}


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
            self.assertTrue(severity in said or body[:20] in said, (severity, body, said))
        for path, line, _s, _b in not_named:
            self.assertNotIn(f"{path}:{line}", said)


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
                else:
                    self.assertRefused(p)
                    self.assertNamesFindings(p, named, not_named)

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

    def resolution_calls(self, fx, state="OPEN", issue_state="OPEN", comments=(), view_files=None, after=None):
        """The land conversation for the fixture's PR. `view_files`: the files `gh pr view --json` holds (all of
        the fixture's by default; GitHub stops at 100), while the files endpoint always lists them all; `after`:
        the answer to the PR read after the merge (`gh pr view P --json body,...`), in place of the PR's data."""
        pr, sha = str(fx["pr"]), fx["sha"]
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
                {"match": ["pr", "view", pr, "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}},
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


if __name__ == "__main__":
    unittest.main()
