"""Tests for harness-board, driven by the fake gh (fakegh.py).

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has its own temporary HOME and a temporary git repository with an `origin` remote (never
contacted). A `fakebin` folder first on PATH holds `gh` (a wrapper around fakegh.py) and stand-ins for
terminal-notifier and osascript, so nothing here reads the real ~/.claude, shows a notification or needs
the network. The fake executables are written once for the module: macOS checks a new executable on its
first run, which costs about half a second each time.
"""
from __future__ import annotations

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

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-board")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
MARK = "\n\n<!-- harness -->"      # the last line of every comment harness-board posts

# The label table from docs/design.md, the source of truth for `labels ensure`.
SPEC_COLORS = {
    "epic": "5319E7", "task": "0E8A16", "bug": "D73A4A", "chore": "BFD4F2", "refactor": "C2E0C6",
    "status:ready": "0052CC", "status:in-progress": "FBCA04", "status:in-review": "D4C5F9",
    "status:blocked": "B60205", "status:needs-input": "E99695",
    "size:S": "C5DEF5", "size:M": "BFDADC", "size:L": "F9D0C4",
    "risk:low": "E6E6E6", "risk:high": "B60205",
    "no-tests": "EDEDED", "needs-local": "FEF2C0", "priority:high": "FF7F0E",
}

TASK_BODY = """## Request
Add the board tool, as Ryan asked.

## Outline
1. Write the script.
2. Write the tests.

## Definition of done
- [ ] `harness-board ready` lists unblocked tickets
- [x] Labels exist
Not a checkbox line.

## Tests that must pass
Broad behavior first, specific cases after.
- `tests/test_board.py::test_ready`: ordering and filtering
- `tests/test_board.py::test_show`: section parsing
The existing suite (`make verify`) stays green.

## Context
- `bin/harness-board`: the tool
- Must not change: the hooks
"""

EPIC_BODY = """## Goal
Ship the board so agents read tickets
before they work.

Second paragraph, not the goal.

## Why
Because.
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
    """fakebin: gh, terminal-notifier and osascript (both record to $NOTIFY_LOG); altbin: gh-alt, a gh that
    logs to $ALT_LOG; failbin: notifiers that record and fail."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-bin-"))
    py = sys.executable
    recorder = write(os.path.join(root, "recorder.py"),
                     "import json, os, sys\n"
                     "with open(os.environ['NOTIFY_LOG'], 'a') as fh:\n"
                     "    fh.write(json.dumps([os.path.basename(sys.argv[1])] + sys.argv[2:]) + '\\n')\n"
                     "sys.exit(int(os.environ.get('NOTIFY_EXIT', '0')))\n")
    fakebin, altbin, failbin = (os.path.join(root, d) for d in ("fakebin", "altbin", "failbin"))
    executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(altbin, "gh-alt"), f'#!/bin/sh\nFAKEGH_LOG="$ALT_LOG" exec "{py}" "{FAKEGH}" "$@"\n')
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), f'#!/bin/sh\nexec "{py}" "{recorder}" "$0" "$@"\n')
        executable(os.path.join(failbin, name), f'#!/bin/sh\nNOTIFY_EXIT=1 exec "{py}" "{recorder}" "$0" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, altbin=altbin, failbin=failbin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def comment(login, when, body):
    return {"author": {"login": login}, "createdAt": when, "body": body,
            "url": f"https://github.com/o/r/issues/12#issuecomment-{abs(hash(body)) % 100000}"}


def issue(number=12, labels=("task", "status:ready", "size:S", "risk:low"), body=TASK_BODY, parent=None,
          blocked_by=(), comments=(), state="OPEN", title="Add the board tool"):
    return {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}", "body": body,
            "labels": [{"name": n} for n in labels], "state": state, "parent": parent,
            "blockedBy": list(blocked_by), "comments": list(comments)}


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.fakebin = BINS["fakebin"]
        self.repo = self.mk("proj")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.log = os.path.join(self.tmp, "gh.log")
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")
        self.scenario([])

    # ----------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "HARNESS_GH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                            "HARNESS_NOTIFY_APP", "HARNESS_NOTIFY_SOUND", "HARNESS_PROJECT")}
        paths = [self.fakebin] + ([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(paths), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.log,
                   NOTIFY_LOG=self.notify_log, ALT_LOG=os.path.join(self.tmp, "alt.log"))
        env.update(extra)
        return env

    def git(self, *args):
        return subprocess.run([GIT, "-C", self.repo, "-c", "init.defaultBranch=main"] + list(args),
                              env=self.env(), capture_output=True, text=True, check=True)

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def board(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.repo, env=self.env(**env),
                              capture_output=True, text=True, timeout=60)

    def ok(self, *args, **env):
        p = self.board(*args, **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def notifications(self):
        if not os.path.exists(self.notify_log):
            return []
        with open(self.notify_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def key(self):
        return hashlib.sha1(os.path.realpath(self.repo).encode()).hexdigest()[:12]

    def cache_path(self, number):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key(), "tickets", f"{number}.json")

    def cache(self, number):
        with open(self.cache_path(number), encoding="utf-8") as fh:
            return json.load(fh)

    def put_cache(self, number, record):
        write(self.cache_path(number), json.dumps(record))

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)


# ======================================================================= labels ensure
class Labels(BoardTest):
    def test_one_forced_create_per_label_with_the_spec_colors(self):
        self.scenario([], default={"stdout": ""})
        p = self.ok("labels", "ensure")
        calls = self.calls()
        self.assertEqual(len(calls), len(SPEC_COLORS))
        seen = {}
        for argv in calls:
            self.assertEqual(argv[:2], ["label", "create"])
            self.assertEqual(argv[3], "--color")
            self.assertEqual(argv[5], "--description")
            self.assertTrue(argv[6].strip(), argv)
            self.assertLessEqual(len(argv[6]), 100, argv)           # GitHub's limit for a description
            self.assertEqual(argv[7:], ["--force"])
            seen[argv[2]] = argv[4]
        self.assertEqual(seen, SPEC_COLORS)
        self.assertIn(f"{len(SPEC_COLORS)} labels created or updated", p.stdout)

    def test_a_failed_create_is_one_line_with_ghs_error(self):
        self.scenario([{"match": ["label", "create"], "stderr": "HTTP 403: Resource not accessible\nmore\n",
                        "exit": 1}])
        p = self.board("labels", "ensure")
        self.assertOneLineError(p, "gh label create epic failed", "HTTP 403: Resource not accessible")
        self.assertEqual(len(self.calls()), 1)


# ======================================================================= epic create, task create
class Create(BoardTest):
    def setUp(self):
        super().setUp()
        self.body = write(os.path.join(self.tmp, "body.md"), TASK_BODY)

    def task_scenario(self, link_responses):
        self.scenario([
            REPO_VIEW,
            {"match": ["issue", "create"], "stdout": "https://github.com/o/r/issues/13\n"},
            {"match": ["api", "repos/o/r/issues/13", "--jq", ".id"], "stdout": "4242\n"},
            {"match": ["api", "-X", "POST", "repos/o/r/issues/3/sub_issues"], "responses": link_responses},
            {"match": ["issue", "edit", "13", "--add-blocked-by"], "stdout": "https://github.com/o/r/issues/13\n"},
        ])

    def create_argv(self, *labels):
        argv = ["issue", "create", "--title", "Add the board", "--body-file", self.body]
        for label in labels:
            argv += ["--label", label]
        return argv

    def test_epic_create_labels_and_output(self):
        self.scenario([{"match": ["issue", "create"], "stdout": "https://github.com/o/r/issues/3\n"}])
        p = self.ok("epic", "create", "--title", "Board v2", "--body-file", self.body, "--size", "L", "--risk", "high")
        self.assertEqual(p.stdout.strip(), "#3 https://github.com/o/r/issues/3")
        self.assertEqual(self.calls(), [["issue", "create", "--title", "Board v2", "--body-file", self.body,
                                         "--label", "epic", "--label", "status:ready", "--label", "size:L",
                                         "--label", "risk:high"]])

    def test_task_create_creates_then_links_then_blocks(self):
        self.task_scenario([{"stdout": "{}"}])
        p = self.ok("task", "create", "--title", "Add the board", "--body-file", self.body, "--parent", "3",
                    "--size", "S", "--risk", "low", "--blocked-by", "9,10", "--priority", "--no-tests")
        self.assertEqual(p.stdout.strip(), "#13 https://github.com/o/r/issues/13")
        self.assertEqual(self.calls(), [
            ["repo", "view", "--json", "nameWithOwner"],
            self.create_argv("task", "status:ready", "size:S", "risk:low", "no-tests", "priority:high"),
            ["api", "repos/o/r/issues/13", "--jq", ".id"],
            ["api", "-X", "POST", "repos/o/r/issues/3/sub_issues", "-F", "sub_issue_id=4242"],
            ["issue", "edit", "13", "--add-blocked-by", "9"],
            ["issue", "edit", "13", "--add-blocked-by", "10"],
        ])
        self.assertFalse(any("--parent" in argv for argv in self.calls()))

    def test_task_create_without_parent_makes_no_link(self):
        self.task_scenario([{"stdout": "{}"}])
        self.ok("task", "create", "--title", "Add the board", "--body-file", self.body, "--type", "bug",
                "--needs-local")
        self.assertEqual(self.calls(), [self.create_argv("bug", "status:ready", "needs-local")])

    def test_link_is_retried_once(self):
        self.task_scenario([{"stderr": "HTTP 502: Bad Gateway\n", "exit": 1}, {"stdout": "{}"}])
        p = self.ok("task", "create", "--title", "Add the board", "--body-file", self.body, "--parent", "3")
        self.assertEqual(p.stdout.strip(), "#13 https://github.com/o/r/issues/13")
        calls = self.calls()
        self.assertEqual(sum(argv[:2] == ["issue", "create"] for argv in calls), 1)
        posts = [argv for argv in calls if argv[:3] == ["api", "-X", "POST"]]
        self.assertEqual(posts, [["api", "-X", "POST", "repos/o/r/issues/3/sub_issues", "-F", "sub_issue_id=4242"]] * 2)

    def test_link_failing_twice_reports_the_issue_and_never_creates_another(self):
        self.task_scenario([{"stderr": "HTTP 422: Validation Failed\n", "exit": 1}])
        p = self.board("task", "create", "--title", "Add the board", "--body-file", self.body, "--parent", "3",
                       "--blocked-by", "9")
        self.assertOneLineError(p, "#13", "HTTP 422: Validation Failed", "#3")
        self.assertIn("#13 https://github.com/o/r/issues/13", p.stdout)
        calls = self.calls()
        self.assertEqual(sum(argv[:2] == ["issue", "create"] for argv in calls), 1)
        self.assertEqual(sum(argv[:3] == ["api", "-X", "POST"] for argv in calls), 2)
        self.assertFalse(any(argv[:2] == ["issue", "edit"] for argv in calls))

    def test_bad_blocked_by_fails_before_any_gh_call(self):
        p = self.board("task", "create", "--title", "T", "--body-file", self.body, "--blocked-by", "9,x")
        self.assertOneLineError(p, "--blocked-by")
        self.assertEqual(self.calls(), [])

    def test_missing_body_file_fails_before_any_gh_call(self):
        p = self.board("task", "create", "--title", "T", "--body-file", os.path.join(self.tmp, "nope.md"))
        self.assertOneLineError(p, "no such file")
        self.assertEqual(self.calls(), [])


# ======================================================================= show
class Show(BoardTest):
    def task_scenario(self, comments=()):
        data = issue(parent={"number": 3, "title": "Board v2", "url": "https://github.com/o/r/issues/3"},
                     labels=("task", "status:in-progress", "size:M", "risk:low", "no-tests"),
                     blocked_by=[{"number": 9, "state": "CLOSED"}, {"number": 10, "state": "OPEN"}],
                     comments=comments)
        self.scenario([
            {"match": ["issue", "view", "12"], "stdout_json": data},
            {"match": ["issue", "view", "3", "--json", "number,title,body"],
             "stdout_json": {"number": 3, "title": "Board v2", "body": EPIC_BODY}},
        ])

    def test_sections_are_parsed_and_the_cache_written(self):
        self.task_scenario(comments=[comment("rjeskuri", "2026-10-13T10:00:00Z", "Looks right.")])
        p = self.ok("show", "12")
        self.assertEqual(self.calls()[0], ["issue", "view", "12", "--json",
                                           "number,title,url,body,labels,state,parent,blockedBy,comments"])
        printed = json.loads(p.stdout)
        cached = self.cache(12)
        for record in (printed, cached):
            self.assertEqual(record["number"], 12)
            self.assertEqual(record["type"], "task")
            self.assertEqual(record["status"], "in-progress")
            self.assertEqual((record["size"], record["risk"], record["tier"]), ("M", "low", "opus"))
            self.assertEqual(record["flags"], ["no-tests"])
            sections = record["sections"]
            self.assertEqual(sections["request"], "Add the board tool, as Ryan asked.")
            self.assertEqual(sections["outline"], "1. Write the script.\n2. Write the tests.")
            self.assertEqual(sections["definition_of_done"],
                             ["- [ ] `harness-board ready` lists unblocked tickets", "- [x] Labels exist"])
            self.assertEqual(sections["tests"], ["`tests/test_board.py::test_ready`: ordering and filtering",
                                                 "`tests/test_board.py::test_show`: section parsing"])
            self.assertEqual(sections["context"], "- `bin/harness-board`: the tool\n- Must not change: the hooks")
            self.assertEqual(record["parent"], {"number": 3, "title": "Board v2",
                                                "goal": "Ship the board so agents read tickets before they work."})
            self.assertEqual(record["blocked_by"], [{"number": 9, "state": "CLOSED"}, {"number": 10, "state": "OPEN"}])
            self.assertEqual(record["comments"], [{"author": "rjeskuri", "created_at": "2026-10-13T10:00:00Z",
                                                   "body": "Looks right."}])
            self.assertRegex(record["fetched_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual((record["branch"], record["worktree"], record["started_at"], record["attempts"]),
                             (None, None, None, []))

    def test_headings_match_in_any_case(self):
        body = "## REQUEST\nDo it.\n\n## definition OF done\n- [ ] done\n```\n## Context\nin a fence\n```\n"
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(body=body)}])
        sections = json.loads(self.ok("show", "12").stdout)["sections"]
        self.assertEqual(sections["request"], "Do it.")
        self.assertEqual(sections["definition_of_done"], ["- [ ] done"])
        self.assertEqual(sections["context"], "")

    def test_cache_keeps_branch_worktree_started_and_attempts(self):
        attempts = [{"stage": "tests", "tier": "opus", "result": "fail", "at": "2026-10-13T09:00:00Z"}]
        self.put_cache(12, {"number": 12, "title": "Old title", "branch": "t/12-add-the-board-tool",
                            "worktree": "/abs/wt", "started_at": "2026-10-13T08:00:00Z", "attempts": attempts,
                            "fetched_at": "2026-10-12T00:00:00Z"})
        self.task_scenario()
        self.ok("show", "12")
        cached = self.cache(12)
        self.assertEqual(cached["title"], "Add the board tool")
        self.assertEqual(cached["branch"], "t/12-add-the-board-tool")
        self.assertEqual(cached["worktree"], "/abs/wt")
        self.assertEqual(cached["started_at"], "2026-10-13T08:00:00Z")
        self.assertEqual(cached["attempts"], attempts)
        self.assertNotEqual(cached["fetched_at"], "2026-10-12T00:00:00Z")

    def test_markdown_context(self):
        self.task_scenario(comments=[comment("rjeskuri", "2026-10-13T10:00:00Z", "Use the fake gh.")])
        md = self.ok("show", "12", "--markdown").stdout
        lines = md.splitlines()
        self.assertEqual(lines[0], "# #12 Add the board tool")
        self.assertIn("Labels: task, status:in-progress, size:M, risk:low, no-tests (tier opus, open)", md)
        self.assertIn("Epic #3: Board v2", md)
        self.assertIn("Epic goal: Ship the board so agents read tickets before they work.", md)
        self.assertNotIn("Second paragraph", md)
        self.assertIn("Blocked by: #9 (closed), #10 (open)", md)
        for heading in ("## Request", "## Outline", "## Definition of done", "## Tests that must pass", "## Context"):
            self.assertIn(heading, lines)
        self.assertIn("- [ ] `harness-board ready` lists unblocked tickets", lines)
        self.assertIn("**rjeskuri** (2026-10-13):", lines)
        self.assertIn("Use the fake gh.", lines)
        self.assertTrue(os.path.exists(self.cache_path(12)))

    def test_markdown_stays_within_120_lines_and_keeps_the_newest_comments(self):
        long_body = "\n".join(f"line {i}" for i in range(40))
        comments = [comment("rjeskuri", f"2026-10-{i + 1:02d}T10:00:00Z", f"comment {i}\n{long_body}")
                    for i in range(14)]
        self.task_scenario(comments=comments)
        lines = self.ok("show", "12", "--markdown").stdout.splitlines()
        self.assertLessEqual(len(lines), 120)
        self.assertIn("comment 13", lines)                 # the newest comment
        self.assertNotIn("comment 3", lines)               # older than the last ten
        self.assertNotIn("line 25", lines)                 # only the first 20 lines of each comment
        self.assertIn("## Definition of done", lines)

    def test_epic_lists_its_tasks_from_sub_issues(self):
        epic = issue(number=3, title="Board v2", labels=("epic", "status:ready"), body=EPIC_BODY)
        subs = [{"number": 12, "title": "Add the board tool", "state": "open",
                 "labels": [{"name": "task"}, {"name": "status:ready"}, {"name": "size:S"}, {"name": "risk:low"}]},
                {"number": 13, "title": "Wire it", "state": "closed", "labels": [{"name": "task"}]}]
        closed = issue(number=13, title="Wire it", labels=("task",), state="CLOSED",       # landed before Resolutions
                       comments=[comment("rjeskuri", "2026-10-11T09:00:00Z", "Landed in abc123\n\n<!-- harness -->")])
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs},
                       {"match": ["issue", "view", "13"], "stdout_json": closed},
                       {"contains": ["repos/o/r/issues/13/comments"], "stdout_json": [
                           {"body": "Landed in abc123\n\n<!-- harness -->", "user": {"login": "rjeskuri"},
                            "created_at": "2026-10-11T09:00:00Z"}]}])
        record = json.loads(self.ok("show", "3").stdout)
        self.assertEqual(record["type"], "epic")
        self.assertEqual(record["goal"], "Ship the board so agents read tickets before they work.")
        self.assertEqual([(t["number"], t["state"], t["status"], t["tier"]) for t in record["tasks"]],
                         [(12, "OPEN", "ready", "sonnet"), (13, "CLOSED", "closed", "opus")])
        md = self.ok("show", "3", "--markdown").stdout
        self.assertIn("## Tasks (1 of 2 closed)", md)
        self.assertIn("- #12 Add the board tool (ready, S, low, tier sonnet)", md)


# ======================================================================= show: Resolutions
def resolution(pr, sha, summary, files, dod, red_kind, verify):
    """A `**Resolution**` comment as `harness-pr land` posts it: summary, files, checklist, evidence, PR, sha."""
    return "\n".join(["**Resolution**", "", summary, "", f"Files changed ({len(files)}):",
                      *[f"- `{f}`" for f in files], "", "Definition of done:", *dod, "",
                      f"Red run before implementation: {red_kind}.", f"Verify: {verify}",
                      f"PR: https://github.com/o/r/pull/{pr}", f"Squash commit: {sha}", "", "<!-- harness -->"])


def content_lines(body):
    """The lines a printed Resolution must carry: everything but the heading, the marker and blank lines."""
    return [line for line in body.splitlines() if line.strip() and line.strip() not in ("**Resolution**",
                                                                                       "<!-- harness -->")]


RES_12 = resolution(34, "abc123def4567890abc123def4567890abc12345",
                    "The board tool lists unblocked tickets and caches each ticket it shows.",
                    ["plugins/harness/bin/harness-board", "plugins/harness/tests/test_board.py", "docs/design.md",
                     "plugins/harness/skills/work/SKILL.md", "plugins/harness/hooks/session_brief.py",
                     "plugins/harness/templates/task.md", "plugins/harness/templates/epic.md", "user/CLAUDE.md",
                     "scripts/verify.sh", "README.md", "plugins/harness/agents/planner.md"],
                    ["- [x] `harness-board ready` lists unblocked tickets", "- [x] Labels exist"],
                    "assertion", "Ran 88 tests in 3.104s OK")
RES_14 = resolution(41, "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f",
                    "Epic checks close the epic when its last task lands.",
                    ["plugins/harness/bin/harness-pr", "plugins/harness/tests/test_pr.py"],
                    ["- [x] The epic closes after its last task", "- [ ] The digest says so"],
                    "import", "VERIFY PASSED 140 checks")
RES_13_STALE = resolution(38, "0a0b0c0d0e0f1a1b1c1d1e1f2a2b2c2d2e2f3a3b",
                          "A first attempt at wiring, reverted when the ticket was reopened.",
                          ["plugins/harness/bin/harness-wire-old"], ["- [x] Wired once"], "assertion",
                          "STALE VERIFY LINE")


def wide_resolution(number, pr, sha, summary, nfiles, verify):
    """A Resolution for task #number that touched `nfiles` files of its own package: about nfiles + 14 lines
    when printed with a heading, so a handful of them outgrow the 120-line context."""
    files = [f"lib/part{number}/{name}.py" for name in
             ["__init__", "model", "schema", "loader", "writer", "errors", "cli", "config", "cache", "hooks",
              "render", "report", "stats", "sync", "queue", "index", "merge", "diff", "notify", "export"][:nfiles]]
    return resolution(pr, sha, summary, files, [f"- [x] Part {number} lands", f"- [x] Part {number} is documented"],
                      "assertion", verify)


EPIC_BODY_LONG = """## Goal
Ship the board so agents read tickets
before they work.

Second paragraph, not the goal.

## Why
Because.

## Scope
- The board tool and its tests.

## Definition of done
- [ ] Every task has landed and the digest says so
- [x] The labels exist on the repository

## Verification
- `harness-board board`: every column.
"""


class ShowResolution(BoardTest):
    def closed_task(self, number, res, chatter=0, title=None):
        """A closed task: harness comments, the Resolution, `Landed in`, then `chatter` short comments after."""
        comments = [comment("rjeskuri", "2026-10-10T09:00:00Z", "Started on t/x\n\n<!-- harness -->"),
                    comment("rjeskuri", "2026-10-10T12:00:00Z", "PR #34 opened\n\n<!-- harness -->"),
                    comment("rjeskuri", "2026-10-11T09:00:00Z", res),
                    comment("rjeskuri", "2026-10-11T09:01:00Z", "Landed in abc123\n\n<!-- harness -->")]
        comments += [comment("pal", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}") for i in range(chatter)]
        return issue(number=number, title=title or f"Task {number}", state="CLOSED",
                     labels=("task", "size:M", "risk:low"), comments=comments,
                     parent={"number": 3, "title": "Board v2", "url": "https://github.com/o/r/issues/3"})

    def comment_calls(self, number, data):
        """gh answers for a task's comments however show asks: issue view (any fields) or the REST endpoint."""
        rest = [{"body": c["body"], "user": c["author"], "created_at": c["createdAt"]} for c in data["comments"]]
        return [{"match": ["issue", "view", str(number)], "stdout_json": data},
                {"contains": [f"repos/o/r/issues/{number}/comments"], "stdout_json": rest},
                {"contains": [f"repos/o/r/issues/{number}/comments?per_page=100"], "stdout_json": rest}]

    def assertUnderResolutionHeading(self, md, res):
        """Every content line of `res` is printed after a `## Resolution` heading and before the next `## `
        heading (the Comments section does not count)."""
        lines = md.splitlines()
        self.assertIn("## Resolution", lines, md)
        start = lines.index("## Resolution")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        section = "\n".join(lines[start + 1:end])
        for line in content_lines(res):
            self.assertIn(line, section, f"Resolution line missing from the ## Resolution section: {line!r}\n{md}")

    def assertUnderTaskHeading(self, lines, res, number, tasks_at):
        """Every content line of `res` is printed below the task list, after a heading naming task #number and
        before the next heading."""
        own = re.compile(rf"#{number}\b")
        summary = content_lines(res)[0]
        at = next((i for i, text in enumerate(lines) if summary in text), None)
        self.assertIsNotNone(at, f"#{number}'s Resolution missing:\n" + "\n".join(lines))
        self.assertGreater(at, tasks_at, "the Resolutions go under the task list")
        head = max(i for i in range(at) if lines[i].startswith("#"))
        self.assertRegex(lines[head], own, f"the heading above #{number}'s Resolution names the task")
        self.assertGreater(head, tasks_at)
        end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("#")), len(lines))
        section = "\n".join(lines[head + 1:end])
        for line in content_lines(res):
            self.assertIn(line, section, f"#{number}'s Resolution line missing under its heading: {line!r}")

    def parent_call(self):
        return {"match": ["issue", "view", "3", "--json", "number,title,body"],
                "stdout_json": {"number": 3, "title": "Board v2", "body": EPIC_BODY}}

    def test_closed_task_prints_its_whole_resolution(self):
        """The Resolution is older than the ten newest comments and longer than the 20 lines a comment gets, so
        only a Resolution section shows it: every line of it, files and sha included."""
        data = self.closed_task(12, RES_12, chatter=10)
        self.scenario([self.parent_call()] + self.comment_calls(12, data))
        md = self.ok("show", "12", "--markdown").stdout
        self.assertLessEqual(len(md.splitlines()), 120)
        self.assertUnderResolutionHeading(md, RES_12)
        self.assertIn("## Definition of done", md.splitlines())         # the ticket's own sections stay

    def test_closed_task_resolution_follows_the_ticket(self):
        """Another closed task, another Resolution: what is printed is that ticket's comment."""
        data = self.closed_task(14, RES_14, chatter=10, title="Close epics")
        self.scenario([self.parent_call()] + self.comment_calls(14, data))
        md = self.ok("show", "14", "--markdown").stdout
        self.assertUnderResolutionHeading(md, RES_14)
        for line in ("caches each ticket it shows", "Ran 88 tests", "abc123def4567890abc123def4567890abc12345"):
            self.assertNotIn(line, md)

    def test_closed_task_prints_every_resolution(self):
        """Landed, reopened, landed again (#24): each landing's Resolution is printed whole, oldest first and
        newest last, outside the Comments section (all of them are older than the ten newest comments). Run in
        several orders and with three passes, so neither "the newest only", "the first only" nor "the last two"
        passes."""
        orders = [("stale then 12", [RES_13_STALE, RES_12]),
                  ("12 then stale", [RES_12, RES_13_STALE]),
                  ("14 then 12", [RES_14, RES_12]),
                  ("three passes", [RES_13_STALE, RES_14, RES_12])]
        for name, passes in orders:
            with self.subTest(name):
                comments = [comment("rjeskuri", "2026-10-06T09:00:00Z", "Started on t/x\n\n<!-- harness -->")]
                for k, res in enumerate(passes):
                    sha = content_lines(res)[-1].split()[-1]
                    comments += [comment("rjeskuri", f"2026-10-0{7 + k}T09:00:00Z", res),
                                 comment("rjeskuri", f"2026-10-0{7 + k}T09:01:00Z",
                                         f"Landed in {sha}\n\n<!-- harness -->")]
                    if k < len(passes) - 1:
                        comments.append(comment("rjeskuri", f"2026-10-0{7 + k}T12:00:00Z",
                                                f"Reopened after pass {k + 1}: it broke the hooks."))
                comments += [comment("pal", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}") for i in range(10)]
                data = issue(number=12, state="CLOSED", labels=("task", "size:M", "risk:low"), comments=comments,
                             parent={"number": 3, "title": "Board v2", "url": "https://github.com/o/r/issues/3"})
                self.scenario([self.parent_call()] + self.comment_calls(12, data))
                md = self.ok("show", "12", "--markdown").stdout
                lines = md.splitlines()
                self.assertLessEqual(len(lines), 120, md)
                heads = [i for i, line in enumerate(lines) if line.startswith("## Resolution")]
                self.assertTrue(heads, f"no ## Resolution heading:\n{md}")
                end = next((i for i, line in enumerate(lines) if line.startswith("## Comments")), len(lines))
                region = lines[heads[0]:end]
                # each pass's own lines (those no other pass shares), every one printed, in its pass's order
                spans = []
                for k, res in enumerate(passes):
                    others = {line for j, other in enumerate(passes) if j != k for line in content_lines(other)}
                    own = [line for line in content_lines(res) if line not in others]
                    at = []
                    for line in own:
                        found = [i for i, text in enumerate(region) if line in text]
                        self.assertTrue(found, f"pass {k + 1}'s Resolution line missing from the Resolution "
                                               f"section: {line!r}\n{md}")
                        at.append(found[0])
                    self.assertEqual(at, sorted(at), f"pass {k + 1}'s Resolution keeps its order:\n{md}")
                    spans.append((at[0], at[-1]))
                for (_, last), (first, _) in zip(spans, spans[1:]):
                    self.assertLess(last, first, f"the Resolutions are printed oldest first, newest last, "
                                                 f"one after the other:\n{md}")

    # ------------------------------------------------------------------ a reopened ticket's Resolutions and room
    def pass_resolution(self, k, nfiles):
        """Landing k's Resolution: 10 + nfiles + 2 content lines, every one of them its own."""
        sha = hashlib.sha1(f"pass-{k}".encode()).hexdigest()
        return resolution(80 + k, sha, f"Landing {k} of the reopened ticket.",
                          [f"lib/pass{k}/module_{i:02d}.py" for i in range(nfiles)],
                          [f"- [x] Pass {k} item one", f"- [x] Pass {k} item two"], "assertion",
                          f"Ran {100 + k} tests OK (pass {k})")

    def reopened_ticket(self, passes, body_lines, others=None, landed=True):
        """Closed #12 with a Request section of `body_lines` lines, each pass's Resolution (and its `Landed in`
        when `landed`), and `others` comments (ten later notes by default)."""
        body = "## Request\n" + "\n".join(f"Request line {i} of a long ticket body." for i in range(body_lines)) \
            + "\n\n## Definition of done\n- [ ] Lands twice\n"
        comments = []
        for k, res in enumerate(passes):
            sha = content_lines(res)[-1].split()[-1]
            comments.append(comment("rjeskuri", f"2026-10-0{1 + k}T09:00:00Z", res))
            if landed:
                comments.append(comment("rjeskuri", f"2026-10-0{1 + k}T09:01:00Z",
                                        f"Landed in {sha}\n\n<!-- harness -->"))
        if others is None:
            others = [comment("pal", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}") for i in range(10)]
        data = issue(number=12, state="CLOSED", labels=("task", "size:M", "risk:low"), body=body,
                     comments=comments + list(others),
                     parent={"number": 3, "title": "Board v2", "url": "https://github.com/o/r/issues/3"})
        self.scenario([self.parent_call()] + self.comment_calls(12, data))
        p = self.ok("show", "12", "--markdown")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stdout.splitlines()
        self.assertLessEqual(len(lines), 120, p.stdout)
        return lines

    def printed(self, lines, res):
        """'whole', 'none' or 'part' for how much of `res` the output carries, judged by its own lines (not the
        `Files changed (N):`, `Definition of done:` and red-run lines every Resolution may share)."""
        own = [line for line in content_lines(res)
               if not line.startswith(("Files changed", "Definition of done:", "Red run before implementation"))]
        found = [line for line in own if line in lines]
        return "whole" if len(found) == len(own) else "none" if not found else "part"

    def pointer(self, lines):
        found = [line for line in lines if "Not shown for lack of room" in line]
        self.assertEqual(len(found), 1, "want one pointer line:\n" + "\n".join(lines))
        line = found[0]
        self.assertIn("gh issue view 12 --comments", line, "the pointer gives the concrete command")
        self.assertNotIn("gh issue view N", line)
        self.assertEqual(len(re.findall(r"#12\b", line)), 1, f"the pointer names the ticket once: {line!r}")
        return line

    def test_closed_task_short_of_room_prints_the_newest_resolutions(self):
        """When the room left after a long body holds fewer Resolutions than the ticket has, the current landing
        wins: the oldest are dropped first and named on the pointer line, the ones printed are whole and in
        chronological order. Two passes with room for one; three passes with room for two."""
        two = [self.pass_resolution(k, 13) for k in (1, 2)]               # 25 content lines each
        lines = self.reopened_ticket(two, 65)
        md = "\n".join(lines)
        self.assertEqual(self.printed(lines, two[1]), "whole", f"the current landing's Resolution is printed:\n{md}")
        self.assertEqual(self.printed(lines, two[0]), "none", f"the superseded Resolution is left out:\n{md}")
        self.pointer(lines)

        three = [self.pass_resolution(k, 13) for k in (1, 2, 3)]
        lines = self.reopened_ticket(three, 40)
        md = "\n".join(lines)
        self.assertEqual([self.printed(lines, res) for res in three], ["none", "whole", "whole"], md)
        self.assertLess(lines.index(content_lines(three[1])[-1]), lines.index(content_lines(three[2])[0]),
                        f"printed oldest first, newest last:\n{md}")
        self.pointer(lines)

    def test_closed_task_with_no_room_for_any_resolution_points_once(self):
        """An 80-line body and two 30-file Resolutions: neither fits, so neither is printed, and one pointer line
        names #12 once with the concrete `gh issue view 12 --comments`, as for a single Resolution."""
        passes = [self.pass_resolution(k, 30) for k in (1, 2)]
        lines = self.reopened_ticket(passes, 80)
        self.assertEqual([self.printed(lines, res) for res in passes], ["none", "none"], "\n".join(lines))
        self.pointer(lines)

    def test_closed_task_keeps_every_resolution_out_of_the_comments(self):
        """Two Resolutions and three other comments, room for all: both Resolutions print in the Resolution
        section and the Comments section holds the three others only."""
        passes = [RES_13_STALE, RES_12]
        others = [comment("rjeskuri", "2026-10-09T09:00:00Z", "Reopened: it broke the hooks."),
                  comment("pal", "2026-10-12T10:00:00Z", "Looks right now."),
                  comment("pal", "2026-10-12T11:00:00Z", "Thanks.")]
        lines = self.reopened_ticket(passes, 3, others=others, landed=False)
        md = "\n".join(lines)
        self.assertEqual([self.printed(lines, res) for res in passes], ["whole", "whole"], md)
        heads = [i for i, line in enumerate(lines) if line.startswith("## Comments")]
        self.assertEqual(len(heads), 1, md)
        section = lines[heads[0]:]
        self.assertEqual(section[0], "## Comments (newest 3 of 3)", md)
        for text in ("Reopened: it broke the hooks.", "Looks right now.", "Thanks."):
            self.assertIn(text, section, md)
        for res in passes:
            for line in content_lines(res):
                self.assertNotIn(line, section, f"a Resolution line in the Comments section: {line!r}\n{md}")
        self.assertNotIn("**Resolution**", "\n".join(section))

    def test_epic_prints_each_closed_tasks_resolution_under_the_task_list(self):
        epic = issue(number=3, title="Board v2", labels=("epic", "status:ready"), body=EPIC_BODY)
        subs = [{"number": 12, "title": "Add the board tool", "state": "closed", "labels": [{"name": "task"}]},
                {"number": 13, "title": "Wire it", "state": "open",
                 "labels": [{"name": "task"}, {"name": "status:in-progress"}, {"name": "size:S"},
                            {"name": "risk:low"}]},
                {"number": 14, "title": "Close epics", "state": "closed", "labels": [{"name": "task"}]}]
        t12 = self.closed_task(12, RES_12, title="Add the board tool")
        t14 = self.closed_task(14, RES_14, title="Close epics")
        # 13 was landed once, then reopened: its old Resolution is not what landed, and it is open
        t13 = issue(number=13, title="Wire it", labels=("task", "status:in-progress", "size:S", "risk:low"),
                    comments=[comment("rjeskuri", "2026-10-09T09:00:00Z", RES_13_STALE),
                              comment("rjeskuri", "2026-10-09T10:00:00Z", "Reopened: it broke the hooks.")])
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs}]
                      + self.comment_calls(12, t12) + self.comment_calls(13, t13) + self.comment_calls(14, t14))
        md = self.ok("show", "3", "--markdown").stdout
        lines = md.splitlines()
        self.assertLessEqual(len(lines), 120)
        self.assertIn("## Tasks (2 of 3 closed)", lines)
        tasks_at = lines.index("## Tasks (2 of 3 closed)")
        for res, number in ((RES_12, 12), (RES_14, 14)):
            self.assertUnderTaskHeading(lines, res, number, tasks_at)
        self.assertEqual([line for line in lines if re.search(r"#13\b", line)],
                         ["- #13 Wire it (in-progress, S, low, tier sonnet)"], "the open task gets no Resolution")
        for line in ("A first attempt at wiring", "harness-wire-old", "STALE VERIFY LINE",
                     "0a0b0c0d0e0f1a1b1c1d1e1f2a2b2c2d2e2f3a3b"):
            self.assertNotIn(line, md, "an open task's Resolution is not printed")

    def epic_with(self, t13_calls):
        """Epic #3 with closed #12 (a Resolution) and closed #13, whose comments `t13_calls` answers."""
        epic = issue(number=3, title="Board v2", labels=("epic", "status:ready"), body=EPIC_BODY)
        subs = [{"number": 12, "title": "Add the board tool", "state": "closed", "labels": [{"name": "task"}]},
                {"number": 13, "title": "Wire it", "state": "closed", "labels": [{"name": "task"}]}]
        t12 = self.closed_task(12, RES_12, title="Add the board tool")
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs}]
                      + self.comment_calls(12, t12) + t13_calls)
        p = self.ok("show", "3", "--markdown")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stdout.splitlines()
        self.assertIn("## Tasks (2 of 2 closed)", lines)
        self.assertUnderTaskHeading(lines, RES_12, 12, lines.index("## Tasks (2 of 2 closed)"))
        self.assertEqual([line for line in lines if re.search(r"#13\b", line)], ["- #13 Wire it (closed)"],
                         "no Resolution text for #13")
        return p.stdout

    def test_epic_closed_task_without_a_resolution_prints_nothing_for_it(self):
        """#13 landed before Resolutions existed: only harness comments and `Landed in`."""
        t13 = issue(number=13, title="Wire it", labels=("task",), state="CLOSED",
                    comments=[comment("rjeskuri", "2026-10-09T09:00:00Z", "Started on t/13-wire\n\n<!-- harness -->"),
                              comment("rjeskuri", "2026-10-10T09:00:00Z", "Landed in 77aa88bb\n\n<!-- harness -->")])
        md = self.epic_with(self.comment_calls(13, t13))
        self.assertNotIn("77aa88bb", md)
        self.assertNotIn("t/13-wire", md)

    def test_epic_shows_what_it_can_when_a_tasks_comments_cannot_be_read(self):
        """Reading #13's comments fails: show still works and prints #12's Resolution."""
        failing = {"stderr": "HTTP 502: Bad Gateway\n", "exit": 1}
        self.epic_with([dict(failing, match=["issue", "view", "13"]),
                        dict(failing, contains=["repos/o/r/issues/13/comments"]),
                        dict(failing, contains=["repos/o/r/issues/13/comments?per_page=100"])])

    def test_epic_keeps_whole_resolutions_or_pointers_within_the_line_budget(self):
        """Five closed tasks whose Resolutions together outgrow the 120-line context: the output stays within
        it, the epic's own lines (goal, task list, definition of done) survive, and each Resolution is either
        printed whole under its task's heading or stands in as a short pointer line naming the task below the
        task list (as whole comments do today), never cut part-way."""
        tasks = {
            12: RES_12,
            14: RES_14,
            15: wide_resolution(15, 44, "7a7b7c7d7e7f8a8b8c8d8e8f9a9b9c9d9e9f0a0b",
                                "The loader streams records instead of reading the file whole.", 18,
                                "Ran 96 tests in 4.011s OK"),
            16: wide_resolution(16, 47, "b1b2b3b4b5b6b7b8b9c0c1c2c3c4c5c6c7c8c9d0",
                                "Reports render from the cache, so the board is offline-safe.", 16,
                                "VERIFY PASSED 151 checks"),
            17: wide_resolution(17, 52, "d1d2d3d4d5d6d7d8d9e0e1e2e3e4e5e6e7e8e9f0",
                                "Sync pushes label changes back to GitHub in one call.", 18,
                                "Ran 104 tests in 4.420s OK"),
        }
        epic = issue(number=3, title="Board v2", labels=("epic", "status:ready"), body=EPIC_BODY_LONG)
        subs = [{"number": n, "title": f"Task {n}", "state": "closed", "labels": [{"name": "task"}]} for n in tasks]
        calls = [REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                 {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs}]
        for n, res in tasks.items():
            calls += self.comment_calls(n, self.closed_task(n, res))
        self.scenario(calls)
        total = sum(len(res.splitlines()) for res in tasks.values())
        self.assertGreater(total, 120, "the fixture must not fit: the Resolutions alone pass the cap")

        p = self.ok("show", "3", "--markdown")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stdout.splitlines()
        self.assertLessEqual(len(lines), 120, p.stdout)
        self.assertIn("Goal: Ship the board so agents read tickets before they work.", lines)
        self.assertIn("## Definition of done", lines)
        self.assertIn("- [ ] Every task has landed and the digest says so", lines)
        self.assertIn("- [x] The labels exist on the repository", lines)
        self.assertIn("## Tasks (5 of 5 closed)", lines)
        tasks_at = lines.index("## Tasks (5 of 5 closed)")
        for n in tasks:
            self.assertIn(f"- #{n} Task {n} (closed)", lines)

        whole, pointed = [], []
        for n, res in tasks.items():
            others = [line for m, other in tasks.items() if m != n for line in content_lines(other)]
            own = [line for line in content_lines(res) if line not in others]
            found = [line for line in own if any(line in text for text in lines)]
            if found == own:
                self.assertUnderTaskHeading(lines, res, n, tasks_at)
                whole.append(n)
            else:
                self.assertEqual(found, [], f"#{n}'s Resolution is cut part-way:\n{p.stdout}")
                pointers = [text for i, text in enumerate(lines)
                            if i > tasks_at and re.search(rf"#{n}\b", text) and text != f"- #{n} Task {n} (closed)"]
                self.assertTrue(pointers, f"#{n}'s Resolution is left out without a line pointing at it:\n{p.stdout}")
                pointed.append(n)
        self.assertTrue(whole, f"the smallest Resolution fits, so at least one is printed whole:\n{p.stdout}")
        self.assertTrue(pointed, f"they cannot all fit in 120 lines, so at least one is a pointer:\n{p.stdout}")


# ======================================================================= ready
class Ready(BoardTest):
    def test_unblocked_non_epics_priority_first_then_lowest_number(self):
        def item(number, labels, blocked=()):
            return {"number": number, "title": f"Ticket {number}", "labels": [{"name": n} for n in labels],
                    "blockedBy": list(blocked)}
        items = [
            item(8, ["task", "status:ready"]),
            item(5, ["task", "status:ready", "size:S", "risk:low"], [{"number": 4, "state": "OPEN"}]),
            item(6, ["bug", "status:ready", "size:S", "risk:low"], [{"number": 4, "state": "CLOSED"}]),
            item(7, ["epic", "status:ready"]),
            item(9, ["task", "status:ready", "priority:high", "size:L"]),
            item(2, ["chore", "status:ready", "size:S", "risk:low", "no-tests"],
                 [{"number": 1, "state": "CLOSED"}, {"number": 4, "state": "OPEN"}]),
            item(3, ["refactor", "status:ready", "size:S", "risk:low"]),
        ]
        self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
        rows = json.loads(self.ok("ready", "--json").stdout)
        self.assertEqual(self.calls(), [["issue", "list", "--state", "open", "--label", "status:ready", "--json",
                                         "number,title,labels,blockedBy", "--limit", "200"]])
        self.assertEqual([r["number"] for r in rows], [9, 3, 6, 8])
        self.assertEqual(rows[0], {"number": 9, "title": "Ticket 9", "type": "task", "size": "L", "risk": None,
                                   "tier": "fable", "flags": ["priority:high"]})
        self.assertEqual([r["tier"] for r in rows], ["fable", "sonnet", "opus", "opus"])
        table = self.ok("ready").stdout.splitlines()
        self.assertTrue(table[0].startswith("#"))
        self.assertEqual([line.split()[0] for line in table[1:]], ["9", "3", "6", "8"])


# ======================================================================= status, comment, close, needs-input
class Status(BoardTest):
    def test_status_swaps_every_status_label_and_comments(self):
        labels = [{"name": "task"}, {"name": "status:ready"}, {"name": "status:blocked"}]
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"], "stdout_json": {"labels": labels}}],
                      default={"stdout": ""})
        self.ok("status", "12", "in-progress", "--comment", "Started on t/12-x")
        self.assertEqual(self.calls(), [
            ["issue", "view", "12", "--json", "labels"],
            ["issue", "edit", "12", "--add-label", "status:in-progress", "--remove-label", "status:ready",
             "--remove-label", "status:blocked"],
            ["issue", "comment", "12", "--body", "Started on t/12-x" + MARK],
        ])

    def test_status_updates_an_existing_cache(self):
        self.put_cache(12, {"number": 12, "status": "ready", "labels": ["task", "status:ready"], "tier": "opus",
                            "branch": "t/12-x"})
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": {"labels": [{"name": "task"},
                                                                                     {"name": "status:ready"}]}}],
                      default={"stdout": ""})
        self.ok("status", "12", "in-review")
        cached = self.cache(12)
        self.assertEqual(cached["status"], "in-review")
        self.assertEqual(cached["labels"], ["task", "status:in-review"])
        self.assertEqual(cached["branch"], "t/12-x")

    def test_bad_status_is_a_one_line_error(self):
        p = self.board("status", "12", "done")
        self.assertOneLineError(p, "invalid choice")
        self.assertEqual(self.calls(), [])

    def test_comment_and_close(self):
        self.scenario([], default={"stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"})
        p = self.ok("comment", "12", "--body", "PR #4: https://github.com/o/r/pull/4")
        self.assertEqual(p.stdout.strip(), "https://github.com/o/r/issues/12#issuecomment-1")
        self.ok("close", "12", "--comment", "Done.")
        self.assertEqual(self.calls(), [["issue", "comment", "12", "--body",
                                         "PR #4: https://github.com/o/r/pull/4" + MARK],
                                        ["issue", "close", "12", "--comment", "Done." + MARK]])

    def test_comment_bodies_from_a_file_or_stdin_are_marked_once(self):
        self.scenario([], default={"stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"})
        body = write(os.path.join(self.tmp, "comment.md"), "Line one.\n\nLine two.\n")
        self.ok("comment", "12", "--body-file", body)
        p = subprocess.run([sys.executable, TOOL, "comment", "12", "--body-file", "-"], cwd=self.repo,
                           env=self.env(), input="From stdin.\n", capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.ok("comment", "12", "--body", "Already marked." + MARK + "\n")
        with open(self.log, encoding="utf-8") as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([entry["argv"] for entry in logged[:2]], [["issue", "comment", "12", "--body-file", "-"]] * 2)
        self.assertEqual([entry["stdin"] for entry in logged[:2]],
                         ["Line one.\n\nLine two." + MARK, "From stdin." + MARK])
        self.assertEqual(logged[2]["argv"], ["issue", "comment", "12", "--body", "Already marked." + MARK])

    def test_needs_input_label_comment_and_notification(self):
        evidence = write(os.path.join(self.tmp, "evidence.txt"), "\n".join(f"E{i}" for i in range(1, 101)) + "\n")
        self.scenario([
            {"match": ["issue", "view", "12", "--json", "labels"],
             "stdout_json": {"labels": [{"name": "task"}, {"name": "status:in-progress"}]}},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-7\n"},
        ], default={"stdout": ""})
        p = self.ok("needs-input", "12", "--question", "Which branch is the base?", "--evidence-file", evidence)
        self.assertEqual(p.stdout.strip(), "https://github.com/o/r/issues/12#issuecomment-7")
        expected = ("**Needs your input**\n\n"
                    "Which branch is the base?\n\n"
                    "```\n[... 41 earlier lines omitted]\n" + "\n".join(f"E{i}" for i in range(42, 101)) + "\n```\n\n"
                    "Reply in a comment; /harness:work 12 picks it up." + MARK)
        self.assertEqual(self.calls(), [
            ["issue", "view", "12", "--json", "labels"],
            ["issue", "edit", "12", "--add-label", "status:needs-input", "--remove-label", "status:in-progress"],
            ["issue", "comment", "12", "--body", expected],
        ])
        fence = expected.split("```\n")[1]
        self.assertEqual(len(fence.splitlines()), 60)
        notes = self.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "terminal-notifier")
        self.assertIn("proj #12 needs input", notes[0])

    def test_needs_input_without_evidence_when_no_notifier_works(self):
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": {"labels": []}}],
                      default={"stdout": ""})
        failing_first = os.pathsep.join([BINS["failbin"], self.env()["PATH"]])   # both fail: still exit 0
        p = self.ok("needs-input", "12", "--question", "Ship it?", "--json", PATH=failing_first)
        self.assertFalse(json.loads(p.stdout)["notified"])
        self.assertEqual([n[0] for n in self.notifications()], ["terminal-notifier", "osascript"])
        body = self.calls()[-1][-1]
        self.assertEqual(body, "**Needs your input**\n\nShip it?\n\nReply in a comment; /harness:work 12 picks it up."
                         + MARK)


# ======================================================================= the Projects board mirror (#64)
# The Status options the labels map to, from the ticket (and docs/proposals/kanban-board.md), not the code.
PROJECT_OPTIONS = {"ready": "Ready", "in-progress": "In progress", "in-review": "In review", "blocked": "Blocked",
                   "needs-input": "Needs input"}
FAILED_SCOPE = "error: your authentication token is missing required scopes [project]\n"


def project_call(argv):
    """(subcommand, positionals, {option: value}) of a `gh project ...` argv; `--opt value` and `--opt=value` both
    count, and flag order does not matter. An option given twice is kept as a list, so it fails equality."""
    pos, opts, i = [], {}, 2
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            name, eq, value = tok[2:].partition("=")
            if not eq:
                i += 1
                value = argv[i] if i < len(argv) else None
            opts[name] = [opts[name], value] if name in opts else value
        else:
            pos.append(tok)
        i += 1
    return argv[1], pos, opts


def item_add(project, url):
    owner, number = project.rsplit("/", 1)
    return ("item-add", [number], {"owner": owner, "url": url})


def item_edit(project, url, field, value):
    owner, number = project.rsplit("/", 1)
    return ("item-edit", [number], {"owner": owner, "url": url, "field": field, "value": value})


class ProjectMirror(BoardTest):
    """HARNESS_PROJECT=owner/number mirrors create and every status change to a Projects board: item-add, then
    Status by option name, then Priority=High for `task create --priority`. The first failing project call stops
    the rest with one warning and exit 0; unset, empty or invalid means no `gh project` call at all.

    Issue URLs here are on ghe.example.com while the git remote and `gh repo view` say github.com/o/r, so only
    the URL gh itself reports for the issue passes as `--url`."""

    HOST = "https://ghe.example.com/o/r/issues/"
    PROJECT_OK = [{"match": ["project"], "stdout": ""}]

    def setUp(self):
        super().setUp()
        self.body = write(os.path.join(self.tmp, "body.md"), TASK_BODY)

    # ------------------------------------------------------------------ helpers
    def project_calls(self):
        return [project_call(argv) for argv in self.calls() if argv[:1] == ["project"]]

    def other_calls(self):
        return [argv for argv in self.calls() if argv[:1] != ["project"]]

    def index_of(self, predicate, what):
        calls = self.calls()
        found = [i for i, argv in enumerate(calls) if predicate(argv)]
        self.assertTrue(found, f"no {what} call in {calls}")
        return found[0]

    def settings(self, text):
        write(os.path.join(self.home, ".claude", "settings.json"), text)

    def create_scenario(self, number, parent=None, blockers=(), project=None):
        url = self.HOST + str(number)
        calls = [REPO_VIEW, {"match": ["issue", "create"], "stdout": f"Creating issue in o/r\n\n{url}\n"}]
        calls += project if project is not None else self.PROJECT_OK
        if parent:
            calls += [{"match": ["api", f"repos/o/r/issues/{number}", "--jq", ".id"], "stdout": f"{number}00\n"},
                      {"match": ["api", "-X", "POST", f"repos/o/r/issues/{parent}/sub_issues"], "stdout": "{}"}]
        for b in blockers:
            calls.append({"match": ["issue", "edit", str(number), "--add-blocked-by", str(b)], "stdout": url + "\n"})
        self.scenario(calls)
        return url

    def task_create(self, number, priority, parent=None, blockers=(), **env):
        argv = ["task", "create", "--title", f"Ticket {number}", "--body-file", self.body]
        argv += (["--parent", str(parent)] if parent else []) + (["--blocked-by", ",".join(map(str, blockers))]
                                                                 if blockers else [])
        argv += ["--priority"] if priority else []
        return self.board(*argv, **env)

    def task_create_calls(self, number, priority, parent=None, blockers=()):
        """The gh calls `task create` makes today, without a project."""
        labels = ["task", "status:ready"] + (["priority:high"] if priority else [])
        create = ["issue", "create", "--title", f"Ticket {number}", "--body-file", self.body] + \
            [x for label in labels for x in ("--label", label)]
        calls = [["repo", "view", "--json", "nameWithOwner"]] if parent else []
        calls.append(create)
        if parent:
            calls += [["api", f"repos/o/r/issues/{number}", "--jq", ".id"],
                      ["api", "-X", "POST", f"repos/o/r/issues/{parent}/sub_issues", "-F", f"sub_issue_id={number}00"]]
        calls += [["issue", "edit", str(number), "--add-blocked-by", str(b)] for b in blockers]
        return calls

    def status_scenario(self, number, old_status, project=None):
        url = self.HOST + str(number)
        labels = [{"name": "task"}, {"name": "status:" + old_status}, {"name": "size:M"}]
        self.scenario([REPO_VIEW,
                       {"match": ["issue", "view", str(number)], "stdout_json": {"labels": labels, "url": url,
                                                                                 "number": number}}]
                      + (project if project is not None else self.PROJECT_OK)
                      + [{"match": ["issue", "comment"], "stdout": f"{url}#issuecomment-{number}1\n"}],
                      default={"stdout": ""})
        return url

    def label_swap(self, number, new, old):
        return ["issue", "edit", str(number), "--add-label", "status:" + new, "--remove-label", "status:" + old]

    def assertOneWarning(self, p, *needles):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        lines = [line for line in p.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"want exactly one warning line:\n{p.stderr}")
        for needle in needles:
            self.assertIn(needle, lines[0])
        return lines[0]

    # ------------------------------------------------------------------ create
    def test_task_create_adds_the_issue_then_sets_status_ready_then_priority_high(self):
        cases = [("rjeskuri/7", 13, True, 3, (9,)),
                 ("acme-org/23", 58, True, None, ()),
                 ("someone/1", 204, False, 5, (40, 41)),
                 ("rjeskuri/12", 9, False, None, ())]
        for project, number, priority, parent, blockers in cases:
            with self.subTest(project=project, number=number, priority=priority):
                url = self.create_scenario(number, parent, blockers)
                p = self.task_create(number, priority, parent, blockers, HARNESS_PROJECT=project)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                self.assertEqual(self.other_calls(), self.task_create_calls(number, priority, parent, blockers),
                                 "the issue, link and blocked-by calls are unchanged")
                expected = [item_add(project, url), item_edit(project, url, "Status", "Ready")]
                if priority:
                    expected.append(item_edit(project, url, "Priority", "High"))
                self.assertEqual(self.project_calls(), expected)
                created = self.index_of(lambda a: a[:2] == ["issue", "create"], "issue create")
                first_project = self.index_of(lambda a: a[:1] == ["project"], "gh project")
                self.assertLess(created, first_project, "the issue exists before it is added to the project")

    def test_epic_create_adds_and_sets_status_ready_with_no_priority(self):
        for project, number in (("rjeskuri/7", 3), ("acme-org/23", 91), ("someone/1", 150)):
            with self.subTest(project=project, number=number):
                url = self.create_scenario(number)
                p = self.board("epic", "create", "--title", f"Epic {number}", "--body-file", self.body, "--size", "M",
                               HARNESS_PROJECT=project)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                self.assertEqual(self.other_calls(), [["issue", "create", "--title", f"Epic {number}", "--body-file",
                                                       self.body, "--label", "epic", "--label", "status:ready",
                                                       "--label", "size:M"]])
                self.assertEqual(self.project_calls(), [item_add(project, url),
                                                        item_edit(project, url, "Status", "Ready")])
                self.assertFalse(any("Priority" in argv for argv in self.calls()), "epics get no Priority write")

    # ------------------------------------------------------------------ status changes
    def test_status_makes_one_status_edit_with_the_mapped_option_for_every_status(self):
        cases = [("ready", 12, "blocked", "rjeskuri/7"),
                 ("in-progress", 31, "ready", "acme-org/23"),
                 ("in-review", 7, "in-progress", "someone/1"),
                 ("blocked", 140, "in-review", "rjeskuri/12"),
                 ("needs-input", 66, "in-progress", "acme-org/4")]
        self.assertEqual({c[0] for c in cases}, set(PROJECT_OPTIONS))
        for status, number, old, project in cases:
            for comment_text in (None, f"Moved #{number} on"):
                with self.subTest(status=status, number=number, comment=bool(comment_text)):
                    url = self.status_scenario(number, old)
                    extra = ["--comment", comment_text] if comment_text else []
                    p = self.board("status", str(number), status, *extra, HARNESS_PROJECT=project)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(p.stdout.strip(), f"#{number} status:{status}")
                    self.assertEqual(self.project_calls(), [item_edit(project, url, "Status", PROJECT_OPTIONS[status])],
                                     "one Status edit, by option name, on the URL gh reports for the issue")
                    swap = self.label_swap(number, status, old)
                    self.assertIn(swap, self.calls())
                    swapped = self.calls().index(swap)
                    edited = self.index_of(lambda a: a[:1] == ["project"], "gh project")
                    self.assertLess(swapped, edited, "the label is the source of truth: it changes first")
                    if comment_text:
                        self.assertIn(["issue", "comment", str(number), "--body", comment_text + MARK], self.calls())

    def test_needs_input_makes_one_status_edit_needs_input(self):
        for number, old, project in ((12, "in-progress", "rjeskuri/7"), (77, "ready", "acme-org/23"),
                                     (305, "blocked", "someone/1")):
            with self.subTest(number=number, project=project):
                url = self.status_scenario(number, old)
                p = self.board("needs-input", str(number), "--question", "Which base?", HARNESS_PROJECT=project)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.project_calls(), [item_edit(project, url, "Status", "Needs input")])
                swap = self.label_swap(number, "needs-input", old)
                self.assertIn(swap, self.calls())
                self.assertLess(self.calls().index(swap), self.index_of(lambda a: a[:1] == ["project"], "gh project"))
                comments = [a for a in self.calls() if a[:3] == ["issue", "comment", str(number)]]
                self.assertEqual(len(comments), 1, self.calls())
                self.assertTrue(comments[0][-1].startswith("**Needs your input**"), comments[0])

    # ------------------------------------------------------------------ failures fail open
    def test_a_failing_project_call_on_status_warns_once_and_exits_zero(self):
        failing = [{"match": ["project"], "stderr": FAILED_SCOPE, "exit": 1}]
        for status, number, old, project in (("in-progress", 12, "ready", "rjeskuri/7"),
                                             ("in-review", 48, "in-progress", "acme-org/23"),
                                             ("blocked", 9, "ready", "someone/1")):
            with self.subTest(status=status, number=number):
                url = self.status_scenario(number, old, project=failing)
                p = self.board("status", str(number), status, HARNESS_PROJECT=project)
                line = self.assertOneWarning(p)
                self.assertIn("project", line.lower())
                self.assertEqual(p.stdout.strip(), f"#{number} status:{status}")
                self.assertIn(self.label_swap(number, status, old), self.calls(), "the labels still change")
                self.assertEqual(self.project_calls(), [item_edit(project, url, "Status", PROJECT_OPTIONS[status])])

    def test_a_failing_item_add_stops_the_edits_with_one_warning(self):
        failing_add = [{"match": ["project", "item-add"], "stderr": FAILED_SCOPE, "exit": 1},
                       {"match": ["project"], "stdout": ""}]
        for number, priority, parent, project in ((13, True, 3, "acme-org/23"), (58, False, None, "rjeskuri/7")):
            with self.subTest(number=number, priority=priority):
                blockers = (9,) if parent else ()
                url = self.create_scenario(number, parent, blockers, project=failing_add)
                p = self.task_create(number, priority, parent, blockers, HARNESS_PROJECT=project)
                self.assertOneWarning(p)
                self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                self.assertEqual(self.other_calls(), self.task_create_calls(number, priority, parent, blockers),
                                 "the issue is created, linked and blocked as without a project")
                self.assertEqual(self.project_calls(), [item_add(project, url)], "no edit after a failed add")
        url = self.create_scenario(91, project=failing_add)
        p = self.board("epic", "create", "--title", "Epic 91", "--body-file", self.body, HARNESS_PROJECT="someone/1")
        self.assertOneWarning(p)
        self.assertEqual(p.stdout.strip(), f"#91 {url}")
        self.assertEqual(self.project_calls(), [item_add("someone/1", url)])

    def test_a_failing_status_edit_on_create_stops_the_priority_edit_with_one_warning(self):
        status_fails = [{"contains": ["project", "item-edit", "Status"], "stderr": "GraphQL: Could not resolve "
                         "option 'Ready' on field 'Status'\n", "exit": 1},
                        {"match": ["project"], "stdout": ""}]
        for number, project in ((13, "acme-org/23"), (204, "rjeskuri/7")):
            with self.subTest(number=number):
                url = self.create_scenario(number, project=status_fails)
                p = self.task_create(number, True, HARNESS_PROJECT=project)
                self.assertOneWarning(p)
                self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                self.assertEqual(self.project_calls(), [item_add(project, url),
                                                        item_edit(project, url, "Status", "Ready")],
                                 "no Priority edit after the Status edit failed")

    def test_a_failing_project_call_on_needs_input_still_asks_and_notifies(self):
        self.status_scenario(27, "in-progress", project=[{"match": ["project"], "stderr": FAILED_SCOPE, "exit": 1}])
        p = self.board("needs-input", "27", "--question", "Ship it?", HARNESS_PROJECT="rjeskuri/7")
        self.assertOneWarning(p)
        self.assertIn(self.label_swap(27, "needs-input", "in-progress"), self.calls())
        self.assertEqual(len([a for a in self.calls() if a[:3] == ["issue", "comment", "27"]]), 1)
        self.assertEqual(len(self.notifications()), 1)

    # ------------------------------------------------------------------ the setting
    def run_everything(self, check, **env):
        """epic create, task create --priority --parent, every status and needs-input, each followed by
        check(what, process) while its calls are still logged."""
        self.create_scenario(3)
        check("epic create", self.board("epic", "create", "--title", "E", "--body-file", self.body, **env))
        self.create_scenario(13, parent=3, blockers=(9,))
        check("task create", self.task_create(13, True, 3, (9,), **env))
        for status in PROJECT_OPTIONS:
            old = "ready" if status != "ready" else "blocked"
            self.status_scenario(12, old)
            p = self.board("status", "12", status, **env)
            check(f"status {status}", p)
            self.assertEqual(self.calls(), [["issue", "view", "12", "--json", "labels"],
                                            self.label_swap(12, status, old)],
                             f"status {status}: the calls are exactly today's")
        self.status_scenario(12, "in-progress")
        check("needs-input", self.board("needs-input", "12", "--question", "Q?", **env))

    def assertNoProjectCall(self, what, p, quiet=True):
        self.assertEqual(p.returncode, 0, (what, p.stdout + p.stderr))
        self.assertNotIn("Traceback", p.stderr)
        if quiet:
            self.assertEqual(p.stderr.strip(), "", what)
        self.assertTrue(self.calls(), what)
        self.assertFalse([a for a in self.calls() if a[:1] == ["project"]], what)

    def test_without_the_setting_no_project_call(self):
        for settings in (None, json.dumps({"env": {"OTHER_SETTING": "x"}, "model": "opus"}),
                         json.dumps({"permissions": {"allow": []}})):
            with self.subTest(settings=settings):
                if settings is not None:
                    self.settings(settings)
                self.run_everything(self.assertNoProjectCall)

    def test_an_empty_environment_value_turns_the_mirror_off_over_a_valid_settings_file(self):
        self.settings(json.dumps({"env": {"HARNESS_PROJECT": "acme-org/23"}}))
        self.run_everything(self.assertNoProjectCall, HARNESS_PROJECT="")

    def test_the_setting_comes_from_settings_env_when_the_environment_lacks_it(self):
        for project, number in (("acme-org/23", 12), ("rjeskuri/9", 40)):
            with self.subTest(project=project):
                self.settings(json.dumps({"env": {"HARNESS_PROJECT": project, "OTHER": "1"}, "model": "opus"}))
                url = self.status_scenario(number, "ready")
                self.ok("status", str(number), "in-review")
                self.assertEqual(self.project_calls(), [item_edit(project, url, "Status", "In review")])
                url = self.create_scenario(number + 1)
                self.ok("epic", "create", "--title", "E", "--body-file", self.body)
                self.assertEqual(self.project_calls(), [item_add(project, url),
                                                        item_edit(project, url, "Status", "Ready")])

    def test_the_environment_wins_over_settings(self):
        for env_project, file_project in (("rjeskuri/7", "acme-org/23"), ("acme-org/23", "rjeskuri/7")):
            with self.subTest(env=env_project, file=file_project):
                self.settings(json.dumps({"env": {"HARNESS_PROJECT": file_project}}))
                url = self.status_scenario(19, "in-progress")
                self.ok("status", "19", "blocked", HARNESS_PROJECT=env_project)
                self.assertEqual(self.project_calls(), [item_edit(env_project, url, "Status", "Blocked")])

    def test_a_malformed_or_misplaced_setting_means_unset(self):
        """Only env.HARNESS_PROJECT of a settings file that parses as a JSON object counts. Several fixtures carry
        a valid-looking `"HARNESS_PROJECT": "acme-org/23"` as text, so a text search finds it and fails."""
        texts = ['{"env": {"HARNESS_PROJECT": "acme-org/23",}}',              # trailing comma
                 '{"env": {"HARNESS_PROJECT": "acme-org/23"}',                # missing brace
                 '// settings\n{"env": {"HARNESS_PROJECT": "acme-org/23"}}',  # a comment: not JSON
                 '{"HARNESS_PROJECT": "acme-org/23"}',                        # top level
                 '{"permissions": {"HARNESS_PROJECT": "acme-org/23"}}',       # under another key
                 '{"extra": {"env": {"HARNESS_PROJECT": "acme-org/23"}}, "env": {}}',
                 '{"env": {"NOTE": "\\"HARNESS_PROJECT\\": \\"acme-org/23\\""}}',
                 '{"env": {"HARNESS_PROJECT_OLD": "acme-org/23"}}',
                 '[{"env": {"HARNESS_PROJECT": "acme-org/23"}}]',             # not an object
                 "{not json", "", '{"env": "HARNESS_PROJECT=acme-org/23"}', '{"env": null}',
                 '{"env": ["HARNESS_PROJECT", "acme-org/23"]}', '{"env": {"HARNESS_PROJECT": 23}}']
        for text in texts:
            with self.subTest(settings=text):
                self.settings(text)
                url = self.status_scenario(12, "ready")
                p = self.board("status", "12", "in-progress")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(p.stdout.strip(), "#12 status:in-progress")
                self.assertEqual(self.project_calls(), [])
                # the environment still works next to the file
                self.status_scenario(12, "ready")
                self.ok("status", "12", "in-progress", HARNESS_PROJECT="someone/5")
                self.assertEqual(self.project_calls(), [item_edit("someone/5", url, "Status", "In progress")])

    def test_an_invalid_value_warns_once_naming_it_and_makes_no_project_call(self):
        bad = ["acme-org", "acme-org/", "/7", "acme-org/x", "acme-org/0", "acme-org/-3", "acme-org/7x", "7"]
        for value in bad:
            for source in ("environment", "settings"):
                with self.subTest(value=value, source=source):
                    env = {"HARNESS_PROJECT": value} if source == "environment" else {}
                    if source == "settings":
                        self.settings(json.dumps({"env": {"HARNESS_PROJECT": value}}))
                    else:
                        self.settings(json.dumps({"env": {"HARNESS_PROJECT": "someone/5"}}))   # env wins, even bad
                    self.status_scenario(31, "ready")
                    p = self.board("status", "31", "in-review", **env)
                    self.assertOneWarning(p, value)
                    self.assertEqual(p.stdout.strip(), "#31 status:in-review")
                    self.assertIn(self.label_swap(31, "in-review", "ready"), self.calls())
                    self.assertEqual(self.project_calls(), [])
        for value in ("acme-org/x", "/7"):
            with self.subTest(create=value):
                url = self.create_scenario(58)
                p = self.task_create(58, True, HARNESS_PROJECT=value)
                self.assertOneWarning(p, value)
                self.assertEqual(p.stdout.strip(), f"#58 {url}")
                self.assertEqual(self.project_calls(), [])

    def test_a_padded_or_zero_led_value_is_invalid(self):
        """The value must be exactly OWNER/NUMBER: a leading zero, surrounding blanks or a trailing newline are
        not stripped into a valid one. The warning names the value (blanks aside)."""
        for value in ("acme-org/07", " acme-org/7", "acme-org/7 ", "acme-org/7\n", "rjeskuri/012"):
            for source in ("environment", "settings"):
                with self.subTest(value=value, source=source):
                    env = {"HARNESS_PROJECT": value} if source == "environment" else {}
                    self.settings(json.dumps({"env": {"HARNESS_PROJECT": value if source == "settings"
                                                      else "someone/5"}}))
                    self.status_scenario(44, "in-progress")
                    p = self.board("status", "44", "blocked", **env)
                    self.assertOneWarning(p, value.strip())
                    self.assertEqual(p.stdout.strip(), "#44 status:blocked")
                    self.assertIn(self.label_swap(44, "blocked", "in-progress"), self.calls())
                    self.assertEqual(self.project_calls(), [])

    def test_status_without_an_issue_url_warns_once_and_makes_no_project_call(self):
        for number, url in ((12, None), (47, ""), (93, "   ")):
            with self.subTest(number=number, url=url):
                labels = [{"name": "task"}, {"name": "status:ready"}]
                reply = {"labels": labels, "number": number}
                if url is not None:
                    reply["url"] = url
                self.scenario([REPO_VIEW, {"match": ["issue", "view", str(number)], "stdout_json": reply},
                               {"match": ["project"], "stdout": ""}], default={"stdout": ""})
                p = self.board("status", str(number), "in-progress", HARNESS_PROJECT="acme-org/23")
                self.assertOneWarning(p)
                self.assertEqual(p.stdout.strip(), f"#{number} status:in-progress")
                self.assertIn(self.label_swap(number, "in-progress", "ready"), self.calls())
                self.assertEqual(self.project_calls(), [])

    def test_commands_that_never_mirror_print_no_warning_for_an_invalid_value(self):
        epic_state = {"state": "OPEN", "subIssuesSummary": {"total": 2, "completed": 1, "percentCompleted": 50}}
        self.scenario([REPO_VIEW,
                       {"match": ["issue", "view", "3"], "stdout_json": epic_state},
                       {"match": ["issue", "view", "12"], "stdout_json": issue()},
                       {"match": ["issue", "list"], "stdout_json": []},
                       {"match": ["api", "user"], "stdout": "rjeskuri\n"},
                       {"match": ["project"], "stdout": ""}],
                      default={"stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"})
        commands = [("show", "12"), ("show", "12", "--markdown"), ("ready",), ("comment", "12", "--body", "Note."),
                    ("close", "12"), ("epic-check", "3"), ("board",), ("answers", "12")]
        for value, source in (("acme-org", "environment"), ("acme-org/x", "settings"), ("/7", "environment")):
            env = {"HARNESS_PROJECT": value} if source == "environment" else {}
            self.settings(json.dumps({"env": {"HARNESS_PROJECT": value}}) if source == "settings" else "{}")
            for argv in commands:
                with self.subTest(value=value, command=" ".join(argv)):
                    p = self.board(*argv, **env)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(p.stderr, "", "a command that never mirrors does not read the setting")
                    self.assertFalse([a for a in self.calls() if a[:1] == ["project"]])


# ======================================================================= answers, epic-check, board
class Answers(BoardTest):
    QUESTION = "**Needs your input**\n\nWhich base?\n\nReply in a comment; /harness:work 12 picks it up."
    STARTED = "Started on t/12-add-the-board-tool (mac.local, interactive)"

    def answers(self, comments, *args):
        self.scenario([{"match": ["api", "user"], "stdout": "rjeskuri\n"},
                       {"match": ["issue", "view", "12", "--json", "comments"], "stdout_json": {"comments": comments}}])
        return [c["body"] for c in json.loads(self.ok("answers", "12", *args).stdout)]

    def test_the_owners_reply_after_the_question_even_after_start_refreshed_the_cache(self):
        self.put_cache(12, {"number": 12, "fetched_at": "2026-10-13T15:00:00Z"})     # newer than every comment
        for mark in (MARK, ""):                                       # marked, and from before the marker
            comments = [comment("rjeskuri", "2026-10-13T11:00:00Z", self.QUESTION + mark),
                        comment("RJeskuri", "2026-10-13T12:00:00Z", "Use main."),
                        comment("rjeskuri", "2026-10-13T13:00:00Z", self.STARTED + mark)]
            self.assertEqual(self.answers(comments), ["Use main."], repr(mark))

    def test_the_newest_question_sets_the_default_since(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                    comment("rjeskuri", "2026-10-13T11:00:00Z", "**Needs your input**\n\nAnd the name?" + MARK),
                    comment("someone", "2026-10-13T12:00:00Z", "drive-by"),
                    comment("rjeskuri", "2026-10-13T13:00:00Z", "Call it export."),
                    comment("rjeskuri", "2026-10-13T14:00:00Z", "Also add a flag.")]
        self.assertEqual(self.answers(comments), ["Call it export.", "Also add a flag."])

    def test_without_a_question_the_cache_then_nothing_bounds_it(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "old note"),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "PR #4 ready for review: https://x" + MARK),
                    comment("rjeskuri", "2026-10-13T11:00:00Z", "Landed in abc1234"),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "new note")]
        self.assertEqual(self.answers(comments), ["old note", "new note"])              # no cache: every reply
        self.put_cache(12, {"number": 12, "fetched_at": "2026-10-13T10:30:00Z"})
        self.assertEqual(self.answers(comments), ["new note"])

    def test_since_overrides_and_harness_comments_stay_out(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "old answer"),
                    comment("rjeskuri", "2026-10-13T11:00:00Z", self.QUESTION + MARK),
                    comment("someone", "2026-10-13T12:00:00Z", "drive-by"),
                    comment("RJeskuri", "2026-10-13T13:00:00Z", "Use main."),
                    comment("rjeskuri", "2026-10-13T14:00:00Z", self.STARTED + MARK),
                    comment("rjeskuri", "2026-10-13T14:30:00Z", "Anything with the marker <!-- harness --> inside")]
        self.assertEqual(self.answers(comments, "--since", "2026-10-13T08:00:00Z"), ["old answer", "Use main."])
        self.assertEqual(self.answers(comments, "--since", "2026-10-13T12:30:00Z"), ["Use main."])
        p = self.board("answers", "12", "--since", "yesterday")
        self.assertOneLineError(p, "--since: not an ISO-8601 time")


class EpicCheck(BoardTest):
    def check(self, total, completed, state="OPEN"):
        self.scenario([{"match": ["issue", "view", "3", "--json", "state,subIssuesSummary"],
                        "stdout_json": {"state": state, "subIssuesSummary": {"total": total, "completed": completed,
                                                                             "percentCompleted": 0}}}],
                      default={"stdout": ""})
        self.ok("epic-check", "3")
        return [argv for argv in self.calls() if argv[:2] == ["issue", "close"]]

    def test_closes_only_when_every_task_is_closed(self):
        self.assertEqual(self.check(3, 3), [["issue", "close", "3", "--comment", "All tasks landed." + MARK]])
        self.assertEqual(self.check(3, 2), [])
        self.assertEqual(self.check(0, 0), [])
        self.assertEqual(self.check(2, 2, state="CLOSED"), [])


class Board(BoardTest):
    def test_markdown_by_status_with_counts(self):
        def item(number, labels, **extra):
            return dict({"number": number, "title": f"Ticket {number}", "labels": [{"name": n} for n in labels]},
                        **extra)
        items = [item(4, ["task", "status:ready", "size:S", "risk:low"]),
                 item(3, ["epic", "status:ready"], subIssuesSummary={"total": 2, "completed": 1}),
                 item(5, ["bug", "status:in-progress", "size:M", "risk:high"]),
                 item(6, ["task"])]
        self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
        md = self.ok("board").stdout.splitlines()
        self.assertIn("## Epics (1)", md)
        self.assertIn("- #3 Ticket 3 (1 of 2 tasks closed)", md)
        self.assertIn("## Ready (1)", md)
        self.assertIn("- #4 Ticket 4 (task, S, low)", md)
        self.assertIn("## In progress (1)", md)
        self.assertIn("- #5 Ticket 5 (bug, M, high)", md)
        self.assertIn("## Needs input (0)", md)
        self.assertIn("## No status (1)", md)
        data = json.loads(self.ok("board", "--json").stdout)
        self.assertEqual([e["number"] for e in data["ready"]], [4])
        self.assertEqual([e["number"] for e in data["no-status"]], [6])


# ======================================================================= tier
class Tier(BoardTest):
    def tier(self, *labels):
        self.put_cache(12, {"number": 12, "labels": list(labels)})
        p = self.ok("tier", "12")
        self.assertEqual(self.calls(), [])                  # from the cache: no gh call
        return p.stdout.strip()

    def test_rubric(self):
        self.assertEqual(self.tier("task", "size:S", "risk:low"), "sonnet")
        self.assertEqual(self.tier("bug", "size:S", "risk:low"), "opus")
        self.assertEqual(self.tier("task", "size:M", "risk:low"), "opus")
        self.assertEqual(self.tier("task", "size:L", "risk:low"), "fable")
        self.assertEqual(self.tier("task", "size:S", "risk:high"), "fable")
        self.assertEqual(self.tier("chore", "size:S"), "opus")          # no risk label: not sonnet

    def test_fetched_without_a_cache(self):
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"],
                        "stdout_json": {"labels": [{"name": "refactor"}, {"name": "size:S"}, {"name": "risk:low"}]}}])
        p = self.ok("tier", "12", "--json")
        self.assertEqual(json.loads(p.stdout), {"number": 12, "tier": "sonnet", "source": "github"})


# ======================================================================= errors, repo resolution, help
class Errors(BoardTest):
    def test_gh_missing(self):
        p = self.board("ready", HARNESS_GH=os.path.join(self.tmp, "no-such-gh"))
        self.assertOneLineError(p, "gh not found")

    def test_harness_gh_is_used(self):
        self.scenario([{"match": ["issue", "list"], "stdout_json": []}])
        p = self.ok("ready", HARNESS_GH=os.path.join(BINS["altbin"], "gh-alt"))
        self.assertEqual(p.stdout.strip(), "No ready tickets.")
        self.assertEqual(self.calls(), [])                  # not the gh on PATH
        with open(os.path.join(self.tmp, "alt.log"), encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["argv"][:2], ["issue", "list"])

    def test_failed_gh_call_names_the_call_and_ghs_first_line(self):
        self.scenario([{"match": ["issue", "view"], "stderr": "GraphQL: Could not resolve to an issue\nx\n",
                        "exit": 1}])
        p = self.board("show", "99")
        self.assertOneLineError(p, "gh issue view 99 failed", "GraphQL: Could not resolve to an issue")

    def test_no_remote(self):
        self.git("remote", "remove", "origin")
        p = self.board("ready")
        self.assertOneLineError(p, "no git remote")
        self.assertEqual(self.calls(), [])

    def test_not_a_repository(self):
        p = self.board("ready", "--repo", self.home)
        self.assertOneLineError(p, "not a git repository")

    def test_repo_option_and_project_dir(self):
        self.scenario([{"match": ["issue", "list"], "stdout_json": []}])
        self.ok("ready", "--repo", self.repo, cwd=self.home)
        self.ok("--repo", self.repo, "ready", cwd=self.home)
        self.ok("ready", cwd=self.home, CLAUDE_PROJECT_DIR=self.repo)

    def test_linked_worktree_shares_the_main_checkouts_cache(self):
        self.git("-c", "user.name=T", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", "init")
        worktree = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", "-b", "t/12-x", worktree)
        self.put_cache(12, {"number": 12, "labels": ["task", "size:S", "risk:low"]})
        p = self.ok("tier", "12", "--repo", worktree)
        self.assertEqual(p.stdout.strip(), "sonnet")

    def test_bad_invocations(self):
        for argv in (["show"], ["show", "abc"], ["nope"], ["labels"], ["comment", "12"],
                     ["task", "create", "--title", "T"]):
            p = self.board(*argv)
            self.assertOneLineError(p)

    def test_help_for_every_subcommand(self):
        for argv in (["--help"], ["labels", "ensure", "--help"], ["epic", "create", "--help"],
                     ["task", "create", "--help"], ["show", "--help"], ["ready", "--help"], ["status", "--help"],
                     ["comment", "--help"], ["needs-input", "--help"], ["answers", "--help"], ["close", "--help"],
                     ["epic-check", "--help"], ["board", "--help"], ["tier", "--help"]):
            p = self.board(*argv)
            self.assertEqual(p.returncode, 0, (argv, p.stderr))
            self.assertIn("usage:", p.stdout)


if __name__ == "__main__":
    unittest.main()
