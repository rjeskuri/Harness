"""Shared by test_pr, test_pr_land, test_pr_ultra: the fixtures, helpers and base classes of the
test_pr suite, split by test class so no one module bounds the parallel suite. Not a test
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


NO_TESTS_RED = "none: the ticket is no-tests"
NO_RED = "No red run recorded on this branch."


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


# =========================================================================== checks
GREEN = [check_run("build"), check_run("lint", conclusion="SKIPPED"), status_context("deploy/preview", "SUCCESS")]
PENDING = [check_run("build", status="IN_PROGRESS", conclusion=""), check_run("lint"),
           status_context("ci/legacy", "PENDING")]


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


# =========================================================================== awaiting
def iso_ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


WORKLOG = "worklog/worklog.csv"
WORKLOG_BEFORE = "date,project,hours,note\n2026-10-12,harness,2.5,board review\n"
WORKLOG_LANDED = WORKLOG_BEFORE + "2026-10-13,harness,1.75,\"land, then restore\"\n2026-10-14,jotform,0.5,triage\n"
IDENTICAL = "(identical to the landed content)"


EPIC_SAID = "#3: 1 of 2 tasks closed; left open\n"


def cost_block(ticket, pr):
    """A Cost block as `harness-work cost N --pr P --markdown` might print it; different for every ticket and PR."""
    tokens = 100_000 * ticket + 37 * pr
    return "\n".join([f"**Cost** (#{ticket}, PR #{pr})", "",
                      "| Stage | Model | Tokens | Model time |", "|---|---|---|---|",
                      f"| coder | opus | {tokens:,} | {ticket}m {pr % 60:02d}s |",
                      f"| reviewer | fable | {tokens // 3:,} | {pr % 7}m 10s |", "",
                      f"Total: {tokens + tokens // 3:,} tokens; rounds {ticket % 4 + 1}"])


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


# Everything above, underscored names included, for `from pr_support import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
