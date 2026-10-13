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
                            "HARNESS_NOTIFY_APP", "HARNESS_NOTIFY_SOUND")}
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
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs}])
        record = json.loads(self.ok("show", "3").stdout)
        self.assertEqual(record["type"], "epic")
        self.assertEqual(record["goal"], "Ship the board so agents read tickets before they work.")
        self.assertEqual([(t["number"], t["state"], t["status"], t["tier"]) for t in record["tasks"]],
                         [(12, "OPEN", "ready", "sonnet"), (13, "CLOSED", "closed", "opus")])
        md = self.ok("show", "3", "--markdown").stdout
        self.assertIn("## Tasks (1 of 2 closed)", md)
        self.assertIn("- #12 Add the board tool (ready, S, low, tier sonnet)", md)


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
            ["issue", "comment", "12", "--body", "Started on t/12-x"],
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
        self.assertEqual(self.calls(), [["issue", "comment", "12", "--body", "PR #4: https://github.com/o/r/pull/4"],
                                        ["issue", "close", "12", "--comment", "Done."]])

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
                    "Reply in a comment; /harness:work 12 picks it up.")
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
        self.assertEqual(body, "**Needs your input**\n\nShip it?\n\nReply in a comment; /harness:work 12 picks it up.")


# ======================================================================= answers, epic-check, board
class Answers(BoardTest):
    def test_only_the_users_comments_after_since_without_the_harness_question(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "old answer"),
                    comment("rjeskuri", "2026-10-13T11:00:00Z", "**Needs your input**\n\nWhich?"),
                    comment("someone", "2026-10-13T12:00:00Z", "drive-by"),
                    comment("RJeskuri", "2026-10-13T13:00:00Z", "Use main.")]
        self.scenario([{"match": ["api", "user"], "stdout": "rjeskuri\n"},
                       {"match": ["issue", "view", "12", "--json", "comments"], "stdout_json": {"comments": comments}}])
        out = json.loads(self.ok("answers", "12", "--since", "2026-10-13T10:00:00Z").stdout)
        self.assertEqual([c["body"] for c in out], ["Use main."])
        self.put_cache(12, {"number": 12, "fetched_at": "2026-10-13T08:00:00Z"})
        out = json.loads(self.ok("answers", "12").stdout)
        self.assertEqual([c["body"] for c in out], ["old answer", "Use main."])


class EpicCheck(BoardTest):
    def check(self, total, completed, state="OPEN"):
        self.scenario([{"match": ["issue", "view", "3", "--json", "state,subIssuesSummary"],
                        "stdout_json": {"state": state, "subIssuesSummary": {"total": total, "completed": completed,
                                                                             "percentCompleted": 0}}}],
                      default={"stdout": ""})
        self.ok("epic-check", "3")
        return [argv for argv in self.calls() if argv[:2] == ["issue", "close"]]

    def test_closes_only_when_every_task_is_closed(self):
        self.assertEqual(self.check(3, 3), [["issue", "close", "3", "--comment", "All tasks landed."]])
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
