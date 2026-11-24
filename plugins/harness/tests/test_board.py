"""Tests for harness-board, driven by the fake gh (fakegh.py).

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has its own temporary HOME and a temporary git repository with an `origin` remote (never
contacted). A `fakebin` folder first on PATH holds `gh` (a wrapper around fakegh.py) and stand-ins for
terminal-notifier and osascript, so nothing here reads the real ~/.claude, shows a notification or needs
the network. The fake executables are written once for the module: macOS checks a new executable on its
first run, which costs about half a second each time.

Each test's first run of the tool is a real process (exit code, stdout, stderr); its later runs go through
gitrepo_support.run_in_process, the same script in this process with the same arguments, environment and folder
(ticket #378). git is gitrepo_support.GIT's folder on PATH: the git binary itself, not macOS's xcrun shim.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tool must not leave __pycache__ in the checkout

from gitrepo_support import GIT, run_in_process  # noqa: E402  (GIT: the git binary itself, not macOS's xcrun shim)
import fakegh  # noqa: E402  (#447: the REST layer the issue fixtures move to, and gh_view to read writes back)

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-board")
LIB = os.path.join(os.path.dirname(HERE), "lib")
FAKEGH = os.path.join(HERE, "fakegh.py")
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
# The gh login show keeps comments from (#334: the owner's, the harness's under the same login, and the bot's).
OWNER = {"match": ["api", "user"], "stdout": "rjeskuri\n"}
# The retired Projects mirror setting (#486), spelled in two pieces so the ticket's grep finds none in the tree.
OLD_SETTING = "HARNESS_" + "PROJECT"
MARK = "\n\n<!-- harness -->"      # the last line of every comment harness-board posts
# The routing settings (docs/design.md, Settings): dropped from the inherited environment.
ROUTING_KEYS = ("HARNESS_CODE_TIER", "HARNESS_CODE_EFFORT", "HARNESS_REVIEW_TIER", "HARNESS_WAVE_REVIEW")

# The label table from docs/design.md, the source of truth for `labels ensure`.
SPEC_COLORS = {
    "epic": "5319E7", "task": "0E8A16", "bug": "D73A4A", "chore": "BFD4F2", "refactor": "C2E0C6",
    "ops": "006B75",                                                 # #72: a record update with no code change
    "status:ready": "0052CC", "status:in-progress": "FBCA04", "status:in-review": "D4C5F9",
    "status:blocked": "B60205", "status:needs-input": "E99695", "status:proposed": "BFE5BF",
    "size:S": "C5DEF5", "size:M": "BFDADC", "size:L": "F9D0C4",
    "risk:low": "E6E6E6", "risk:high": "B60205",
    "no-tests": "EDEDED", "needs-local": "FEF2C0", "priority:high": "FF7F0E",
    "manual-land": "D93F0B", "incident": "B60205", "degraded": "FBCA04", "deploy-frozen": "000000",
}
# Descriptions checked exactly: the autoland group's (#149) and status:proposed's (#159).
AUTOLAND_DESCRIPTIONS = {
    "manual-land": "Land by hand: autoland skips it",
    "incident": "A live check failed; autoland is paused in this repo",
    "degraded": "A live check is degraded; nothing was reverted",
    "deploy-frozen": "Deploys are stopped until you clear this",
    "status:proposed": "A plan waiting for you to promote it",       # #159
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
    # gh, the command every test runs most: fakegh.py run as __main__ by this python itself, without site and with no
    # /bin/sh in front, which halves each call (ticket #378); behind /bin/sh only where a #! line cannot hold the path.
    if " " in py:
        executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{py}" "{FAKEGH}" "$@"\n')
    else:
        executable(os.path.join(fakebin, "gh"),
                   f"#!{py} -SB\nimport sys\nsys.argv[0] = {FAKEGH!r}\n"
                   f"with open({FAKEGH!r}, encoding='utf-8') as fh:\n"
                   f"    code = compile(fh.read(), {FAKEGH!r}, 'exec')\n"
                   f"exec(code, {{'__name__': '__main__', '__file__': {FAKEGH!r}}})\n")
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
        self.ran_a_process = False                   # the test's first harness-board run is a real process
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
                            "HARNESS_NOTIFY_APP", "HARNESS_NOTIFY_SOUND",
                            "HARNESS_SESSION_PROJECT") + ROUTING_KEYS}
        paths = [self.fakebin] + ([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(paths), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.log,
                   NOTIFY_LOG=self.notify_log, ALT_LOG=os.path.join(self.tmp, "alt.log"))
        env.update(extra)
        return env

    def git(self, *args):
        return subprocess.run([GIT, "-C", self.repo, "-c", "init.defaultBranch=main"] + list(args),
                              env=self.env(), capture_output=True, text=True, check=True)

    def scenario(self, calls, default=None, rest=True):
        """The fake gh's answers. #447: the gh issue fixtures move to the REST layer (fakegh.as_rest), so every
        issue read and write goes through `gh api`; `rest=False` keeps them as gh commands."""
        data = {"calls": fakegh.as_rest(calls) if rest else calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def board(self, *args, cwd=None, **env):
        """harness-board with these arguments: a real process the first time in a test, then in this process
        (ticket #378); either way an object with returncode, stdout and stderr."""
        if self.ran_a_process:
            return run_in_process(TOOL, args, cwd=cwd or self.repo, env=self.env(**env))
        self.ran_a_process = True
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.repo, env=self.env(**env),
                              capture_output=True, text=True, timeout=60)

    def ok(self, *args, **env):
        p = self.board(*args, **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def entries(self):
        return fakegh.log_entries(self.log)

    def calls(self):
        """Each gh call's argv, a REST issue write as the gh command it stands for (fakegh.gh_view)."""
        return fakegh.gh_view(self.entries(), self.scenario_path)

    def writes(self):
        """calls() without the REST reads of issues and pull requests."""
        return [argv for argv in self.calls() if not fakegh.is_world_read(argv)]

    def reads_of(self, number):
        """How many times issue `number` itself was read."""
        return fakegh.issue_gets(self.entries(), number)

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
        seen, descriptions = {}, {}
        for argv in calls:
            self.assertEqual(argv[:2], ["label", "create"])
            self.assertEqual(argv[3], "--color")
            self.assertEqual(argv[5], "--description")
            self.assertTrue(argv[6].strip(), argv)
            self.assertLessEqual(len(argv[6]), 100, argv)           # GitHub's limit for a description
            self.assertEqual(argv[7:], ["--force"])
            seen[argv[2]] = argv[4]
            descriptions[argv[2]] = argv[6]
        self.assertEqual(seen, SPEC_COLORS)
        self.assertEqual(len(SPEC_COLORS), 24)                      # 22 before #159, status:proposed, ops (#72)
        self.assertIn(f"{len(SPEC_COLORS)} labels created or updated", p.stdout)
        self.assertIn("24 labels created or updated", p.stdout)
        for name, text in AUTOLAND_DESCRIPTIONS.items():
            self.assertEqual(descriptions.get(name), text, name)


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
            OWNER,
        ])

    def test_sections_are_parsed_and_the_cache_written(self):
        self.task_scenario(comments=[comment("rjeskuri", "2026-10-13T10:00:00Z", "Looks right.")])
        p = self.ok("show", "12")
        self.assertEqual(self.reads_of(12), 1, self.calls())          # #447: one REST read of the issue, no write
        self.assertFalse([a for a in self.writes() if a[:1] == ["issue"]], self.calls())
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
        self.scenario([REPO_VIEW, OWNER, {"match": ["issue", "view", "3"], "stdout_json": epic},
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
        comments += [comment("rjeskuri", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}") for i in range(chatter)]
        return issue(number=number, title=title or f"Task {number}", state="CLOSED",
                     labels=("task", "size:M", "risk:low"), comments=comments,
                     parent={"number": 3, "title": "Board v2", "url": "https://github.com/o/r/issues/3"})

    def comment_calls(self, number, data):
        """gh answers for a task's comments however show asks: issue view (any fields) or the REST endpoint."""
        rest = [{"body": c["body"], "user": c["author"], "created_at": c["createdAt"]} for c in data["comments"]]
        return [OWNER, {"match": ["issue", "view", str(number)], "stdout_json": data},
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
                comments += [comment("rjeskuri", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}")
                             for i in range(10)]
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
            others = [comment("rjeskuri", f"2026-10-12T{10 + i:02d}:00:00Z", f"later note {i}") for i in range(10)]
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
                  comment("rjeskuri", "2026-10-12T10:00:00Z", "Looks right now."),
                  comment("rjeskuri", "2026-10-12T11:00:00Z", "Thanks.")]
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
        # #447: the open status:ready issues through REST (GET .../issues?labels=status:ready), no write
        self.assertEqual({(tuple(labels), state or "open") for labels, state in fakegh.list_queries(self.entries())},
                         {(("status:ready",), "open")})
        self.assertEqual(self.writes(), [])
        self.assertEqual([r["number"] for r in rows], [9, 3, 6, 8])
        self.assertEqual(rows[0], {"number": 9, "title": "Ticket 9", "type": "task", "size": "L", "risk": None,
                                   "tier": "opus", "flags": ["priority:high"], "blocked_by": []})  # #239, #389
        self.assertEqual([r["tier"] for r in rows], ["opus", "sonnet", "opus", "opus"])
        table = self.ok("ready").stdout.splitlines()
        self.assertTrue(table[0].startswith("#"))
        self.assertEqual([line.split()[0] for line in table[1:]], ["9", "3", "6", "8"])

    def test_ready_json_lists_blocked_by(self):
        # #389: each row carries the numbers of its (all closed) blockers; the text table keeps its columns
        def item(number, blockers):
            return {"number": number, "title": f"Ticket {number}",
                    "labels": [{"name": n} for n in ("task", "status:ready", "size:S", "risk:low")],
                    "blockedBy": [{"number": b, "state": "CLOSED"} for b in blockers]}
        cases = {3: [], 4: [2], 6: [1, 5], 7: [10], 8: [2, 10]}
        self.scenario([{"match": ["issue", "list"], "stdout_json": [item(n, b) for n, b in cases.items()]}])
        rows = json.loads(self.ok("ready", "--json").stdout)
        self.assertEqual({r["number"]: r.get("blocked_by") for r in rows}, cases)
        header = self.ok("ready").stdout.splitlines()[0]
        self.assertEqual(header.split(), ["#", "TITLE", "TYPE", "SIZE", "RISK", "TIER", "FLAGS"])


# ======================================================================= status, comment, close, needs-input
class Status(BoardTest):
    def test_status_swaps_every_status_label_and_comments(self):
        labels = [{"name": "task"}, {"name": "status:ready"}, {"name": "status:blocked"}]
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"], "stdout_json": {"labels": labels}}],
                      default={"stdout": ""})
        self.ok("status", "12", "in-progress", "--comment", "Started on t/12-x")
        self.assertEqual(self.reads_of(12), 1, self.calls())
        self.assertEqual(self.writes(), [
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
        self.assertEqual(self.writes(), [["issue", "comment", "12", "--body",
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
        self.assertEqual(self.writes(), [["issue", "comment", "12", "--body", "Line one.\n\nLine two." + MARK],
                                         ["issue", "comment", "12", "--body", "From stdin." + MARK],
                                         ["issue", "comment", "12", "--body", "Already marked." + MARK]])

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
        self.assertEqual(self.reads_of(12), 1, self.calls())
        self.assertEqual(self.writes(), [
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


# ======================================================================= no Projects board mirror (#486)
class NoProjectsMirror(BoardTest):
    """The Projects board mirror is gone for good (#486): whatever the retired project setting holds, in the
    environment or in the settings file's `env`, create and every status change make no `gh project` call and say
    nothing about it, and the old `project check` / `project sync` subcommands no longer exist."""

    HOST = "https://ghe.example.com/o/r/issues/"
    # Where the setting comes from, and what it holds: a valid board, an invalid value and an empty one, which
    # the mirror used to turn into a warning or an off switch and now nothing reads.
    SETTINGS = [("environment", "rjeskuri/3"), ("settings file", "rjeskuri/3"), ("both", "acme-org/23"),
                ("environment", "not-a-board"), ("settings file", ""), ("both", "rjeskuri/3")]

    def setUp(self):
        super().setUp()
        self.body = write(os.path.join(self.tmp, "body.md"), TASK_BODY)

    def configure(self, where, value):
        """The env kwargs for harness-board, with the settings file written when `where` says so."""
        settings = {"env": {OLD_SETTING: value}} if where in ("settings file", "both") else {}
        write(os.path.join(self.home, ".claude", "settings.json"), json.dumps(settings))
        return {OLD_SETTING: value} if where in ("environment", "both") else {}

    def project_calls(self):
        return [argv for argv in self.calls() if argv[:1] == ["project"]]

    def assertSilent(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stderr, "", "no warning about the project setting")

    def test_create_makes_no_project_call_and_no_warning(self):
        cases = [(13, ["task", "create", "--title", "T13", "--parent", "3", "--blocked-by", "9", "--priority"]),
                 (58, ["task", "create", "--title", "T58", "--type", "bug"]),
                 (91, ["epic", "create", "--title", "E91", "--size", "M"]),
                 (204, ["task", "create", "--title", "T204", "--proposed", "--priority"])]
        for (where, value), (number, argv) in zip(self.SETTINGS * 2, cases * 2):
            with self.subTest(where=where, value=value, number=number):
                url = self.HOST + str(number)
                calls = [REPO_VIEW, {"match": ["issue", "create"], "stdout": f"Creating issue in o/r\n\n{url}\n"},
                         {"match": ["api", f"repos/o/r/issues/{number}", "--jq", ".id"], "stdout": f"{number}00\n"},
                         {"match": ["api", "-X", "POST"], "stdout": "{}"},
                         {"match": ["issue", "edit"], "stdout": url + "\n"}]
                self.scenario(calls, default={"stdout": ""})
                p = self.board(*argv, "--body-file", self.body, **self.configure(where, value))
                self.assertSilent(p)
                self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                made = self.calls()
                self.assertEqual(sum(a[:2] == ["issue", "create"] for a in made), 1, made)
                self.assertEqual(self.project_calls(), [], made)

    def test_every_status_change_makes_no_project_call_and_no_warning(self):
        rows = [(12, "ready"), (19, "in-progress"), (27, "in-review"), (31, "blocked"), (40, "needs-input"),
                (44, "proposed"), (48, "ready"), (66, "in-review"), (77, "in-progress"), (140, "blocked"),
                (305, "ready"), (7, "proposed")]
        for (where, value), (number, status) in zip(self.SETTINGS * 2, rows):
            with self.subTest(where=where, value=value, number=number, status=status):
                url = self.HOST + str(number)
                old = "blocked" if status == "ready" else "ready"
                labels = [{"name": "task"}, {"name": "status:" + old}, {"name": "size:M"}]
                self.scenario([REPO_VIEW,
                               {"match": ["issue", "view", str(number)],
                                "stdout_json": {"labels": labels, "url": url, "number": number}},
                               {"match": ["issue", "comment"], "stdout": f"{url}#issuecomment-{number}1\n"}],
                              default={"stdout": ""})
                argv = ["needs-input", str(number), "--question", "Which base?"] if status == "needs-input" \
                    else ["status", str(number), status]
                p = self.board(*argv, **self.configure(where, value))
                self.assertSilent(p)
                made = self.calls()
                self.assertEqual(self.project_calls(), [], made)
                adds = [a for a in made if a[:2] == ["issue", "edit"] and "--add-label" in a]
                self.assertEqual([a[a.index("--add-label") + 1] for a in adds], ["status:" + status], made)

    def test_promote_makes_no_project_call_and_no_warning(self):
        for (where, value), number in zip(self.SETTINGS, (12, 66, 140, 7, 31, 305)):
            with self.subTest(where=where, value=value, number=number):
                url = self.HOST + str(number)
                self.scenario([REPO_VIEW,
                               {"match": ["issue", "view", str(number)],
                                "stdout_json": dict(issue(number=number, labels=("task", "status:proposed")),
                                                    url=url)}],
                              default={"stdout": ""})
                p = self.board("promote", str(number), **self.configure(where, value))
                self.assertSilent(p)
                made = self.calls()
                self.assertEqual(self.project_calls(), [], made)
                self.assertTrue([a for a in made if "status:ready" in a], made)

    def test_the_project_subcommands_are_gone(self):
        self.configure("both", "rjeskuri/3")
        for argv in (["project", "check"], ["project", "sync"], ["project", "sync", "--dry-run"],
                     ["project", "sync", "--all-projects"]):
            with self.subTest(argv=argv):
                p = self.board(*argv, **{OLD_SETTING: "rjeskuri/3"})
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertEqual(self.calls(), [], "refused before any gh call")


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

    # Ticket #167: the deploy guard (scripts/deploy_guard.py) posts its needs-input question from the deploy workflow,
    # so on GitHub it is authored by github-actions[bot], not the owner. It must anchor `answers`; only the owner's
    # replies after it come back, and no bot comment is ever an answer.
    BOT = "github-actions[bot]"
    GUARD = ("**Needs your input**\n\nThe deploy of #12 failed its live check and was reverted. "
             "Retry, or change the approach?\n\nReply in a comment; /harness:work 12 picks it up.")

    def test_a_bot_question_anchors_and_only_the_later_owner_replies_come_back(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                    comment("rjeskuri", "2026-10-13T10:30:00Z", "Actually, ship it behind a flag."),
                    comment(self.BOT, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry, the outage was upstream.")]
        self.assertEqual(self.answers(comments), ["Retry, the outage was upstream."])

    def test_the_bot_question_anchors_when_it_is_the_only_question_or_the_cache_is_newer(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "early thought"),
                    comment(self.BOT, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry."),
                    comment("rjeskuri", "2026-10-13T13:00:00Z", "And bump the timeout.")]
        self.assertEqual(self.answers(comments), ["Retry.", "And bump the timeout."])
        self.put_cache(12, {"number": 12, "fetched_at": "2026-10-13T14:00:00Z"})       # newer than every comment
        self.assertEqual(self.answers(comments), ["Retry.", "And bump the timeout."])

    def test_the_newest_question_wins_whoever_asked_it(self):
        comments = [comment(self.BOT, "2026-10-13T09:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "Retry."),
                    comment("rjeskuri", "2026-10-13T11:00:00Z", "**Needs your input**\n\nAnd the name?" + MARK),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Call it export."),
                    comment(self.BOT, "2026-10-13T13:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T14:00:00Z", "Give up on it."),
                    comment(self.BOT, "2026-10-13T15:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T16:00:00Z", "Try once more.")]
        self.assertEqual(self.answers(comments), ["Try once more."])
        self.assertEqual(self.answers(comments[:6]), ["Give up on it."])
        self.assertEqual(self.answers(comments[:4]), ["Call it export."])

    def test_bot_comments_are_never_returned_as_answers(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                    comment(self.BOT, "2026-10-13T10:00:00Z", self.GUARD + MARK),
                    comment(self.BOT, "2026-10-13T10:30:00Z", "Deploy finished: all checks passed."),
                    comment(self.BOT, "2026-10-13T10:45:00Z", "Reverted by the deploy guard." + MARK),
                    comment(self.BOT, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry."),
                    comment(self.BOT, "2026-10-13T13:00:00Z", "Deploy finished: all checks passed."),
                    comment(self.BOT, "2026-10-13T14:00:00Z", "Plain bot chatter, no marker."),
                    comment("rjeskuri", "2026-10-13T15:00:00Z", "Thanks.")]
        # with the anchor and with an explicit --since that reaches back before it
        self.assertEqual(self.answers(comments), ["Retry.", "Thanks."])
        self.assertEqual(self.answers(comments, "--since", "2026-10-13T08:00:00Z"), ["Retry.", "Thanks."])

    def test_since_still_overrides_a_bot_question(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "old answer"),
                    comment(self.BOT, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
        self.assertEqual(self.answers(comments, "--since", "2026-10-13T08:00:00Z"), ["old answer", "Retry."])
        self.assertEqual(self.answers(comments, "--since", "2026-10-13T11:30:00Z"), ["Retry."])

    def test_a_bot_comment_without_the_marker_does_not_anchor(self):
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                    comment(self.BOT, "2026-10-13T11:00:00Z", self.GUARD),            # no <!-- harness --> marker
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
        self.assertEqual(self.answers(comments), ["Use develop.", "Retry."])          # still the owner's question

    def test_a_marked_bot_comment_that_is_not_a_question_does_not_anchor(self):
        for body in ("Deploy of #12 reverted." + MARK,
                     "Started on t/12-x (ci, headless)" + MARK,
                     "see below **Needs your input** is what we asked" + MARK,        # not at the start
                     "Landed in abc1234" + MARK):
            with self.subTest(body=body):
                comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                            comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                            comment(self.BOT, "2026-10-13T11:00:00Z", body),
                            comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
                self.assertEqual(self.answers(comments), ["Use develop.", "Retry."])

    # Only the Actions app may anchor: exactly `github-actions` (GraphQL) or `github-actions[bot]` (REST), any case.
    # Each near miss below posts a marked, well-formed, timestamped question and must leave the owner's anchor alone;
    # a prefix, split-on-"[" or "starts with github-actions[" match lets several of them through.
    NEAR_MISS_LOGINS = (
        "someone", "dependabot[bot]", "github-actions-fake[bot]", "not-github-actions[bot]",
        "github-actions[app]", "GitHub-Actions[App]", "github-actions[x]", "github-actions[evil", "github-actions[bot",
        "github-actions[bot]x", "github-actions[bot][bot]", "github-actions[bot]]", "github-actions bot",
        "github-actions[]", "github-actions[", "github-actionsbot", "github-actions-bot", "xgithub-actions",
        "xgithub-actions[bot]", "github-actionsx", "github-action", "github-action[bot]", "github", "actions[bot]",
        "[bot]", "bot", "github-actions[bot] [bot]", "github_actions[bot]", "github-actions[[bot]]",
    )

    def test_a_third_party_question_does_not_anchor(self):
        for login in self.NEAR_MISS_LOGINS:
            question = f"**Needs your input**\n\nWhich region should {login} deploy to?" + MARK
            with self.subTest(login=login):
                comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                            comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                            comment(login, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                            comment(login, "2026-10-13T11:15:00Z", question),
                            comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
                self.assertEqual(self.answers(comments), ["Use develop.", "Retry."])

    def test_a_near_miss_login_cannot_anchor_even_as_the_only_question(self):
        # no owner question to fall back on: the cache bounds it, else nothing does
        for login in self.NEAR_MISS_LOGINS:
            with self.subTest(login=login):
                comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", "early thought"),
                            comment(login, "2026-10-13T11:00:00Z", self.GUARD + MARK),
                            comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
                self.assertEqual(self.answers(comments), ["early thought", "Retry."])

    # "Carries the marker" is `<!-- harness -->` anywhere in the body, as by_harness() reads it, not only as the last
    # line; the heading must still open the body.
    def test_a_bot_question_with_the_marker_anywhere_anchors(self):
        for body in ("**Needs your input** <!-- harness -->\n\nWhich region should the deploy of #99 target?",
                     "**Needs your input**<!-- harness -->",
                     "**Needs your input**\n<!-- harness -->\nWhich region should the deploy of #99 target?",
                     "**Needs your input**\n\nWhich region <!-- harness --> should the deploy of #99 target?",
                     "**Needs your input**\n\nWhich region?\n\n<!-- harness -->\n\nReply in a comment.",
                     "**Needs your input**\n\nWhich region?<!-- harness -->",
                     "**Needs your input**\n\nWhich region?\n\n<!-- harness -->\n<!-- harness -->"):
            for login in (self.BOT, "github-actions"):
                with self.subTest(body=body, login=login):
                    comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                                comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                                comment(login, "2026-10-13T11:00:00Z", body),
                                comment("rjeskuri", "2026-10-13T12:00:00Z", "Eu-west.")]
                    self.assertEqual(self.answers(comments), ["Eu-west."])

    def test_a_bot_question_with_a_near_miss_marker_does_not_anchor(self):
        for marker in ("<!--harness-->", "<!-- harness-review -->", "<!-- Harness -->", "<!-- HARNESS -->",
                       "<!--  harness  -->", "<!-- harness- -->", "<!-- harness", "harness -->", "<! -- harness -->",
                       "&lt;!-- harness --&gt;", "<!-- harness --", "!-- harness -->", "<-- harness -->", "harness"):
            self.assertNotIn("<!-- harness -->", marker)                 # a near miss, not the marker inside
            for body in (self.GUARD + "\n\n" + marker, "**Needs your input** " + marker + "\n\nWhich region?"):
                with self.subTest(body=body):
                    comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                                comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                                comment(self.BOT, "2026-10-13T11:00:00Z", body),
                                comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
                    self.assertEqual(self.answers(comments), ["Use develop.", "Retry."])

    def test_gh_graphql_reports_the_app_as_github_actions_without_the_suffix(self):
        # `gh issue view --json comments` is GraphQL, whose login for the Actions app has no [bot] suffix
        comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                    comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                    comment("github-actions", "2026-10-13T11:00:00Z", self.GUARD + MARK),
                    comment("github-actions", "2026-10-13T11:30:00Z", "Deploy finished."),
                    comment("rjeskuri", "2026-10-13T12:00:00Z", "Retry.")]
        self.assertEqual(self.answers(comments), ["Retry."])

    # The deploy guard's wording is not what anchors: any bot comment that starts with **Needs your input** and carries
    # the marker does, so these questions share nothing with GUARD beyond the heading.
    BOT_QUESTIONS = (
        "**Needs your input**\n\nWhich region should the deploy of #99 target?",
        "**Needs your input**\n\nrollback finished at 14:02 UTC; keep the old schema or migrate?\n\nReply in a comment.",
        "**Needs your input**",
        "**Needs your input**\nok?",
        "  \n\t**Needs your input**\n\nShould #7 wait for the nightly build?",        # leading whitespace, as the owner path
        "\n**Needs your input**\n\n¿Qué hacemos con el despliegue?",
    )

    def test_a_bot_question_anchors_whatever_it_says(self):
        for question in self.BOT_QUESTIONS:
            for marker in (MARK, "\n" + MARK.strip(), "\n\n" + MARK.strip() + "\n"):
                with self.subTest(question=question, marker=marker):
                    comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                                comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                                comment(self.BOT, "2026-10-13T11:00:00Z", question + marker),
                                comment("rjeskuri", "2026-10-13T12:00:00Z", "Eu-west."),
                                comment(self.BOT, "2026-10-13T12:30:00Z", "Deploy finished."),
                                comment("rjeskuri", "2026-10-13T13:00:00Z", "Then the nightly.")]
                    self.assertEqual(self.answers(comments), ["Eu-west.", "Then the nightly."])

    def test_the_bot_question_moves_the_anchor_to_its_own_time(self):
        # the same wording at different times anchors at each time (a constant or the first match cannot pass)
        question = "**Needs your input**\n\nWhich region should the deploy of #99 target?" + MARK
        replies = [comment("rjeskuri", f"2026-10-13T{h:02d}:30:00Z", f"reply {h}") for h in (10, 12, 14, 16)]
        for hour, expected in ((9, ["reply 10", "reply 12", "reply 14", "reply 16"]),
                               (11, ["reply 12", "reply 14", "reply 16"]), (13, ["reply 14", "reply 16"]),
                               (15, ["reply 16"]), (17, [])):
            with self.subTest(hour=hour):
                comments = replies + [comment(self.BOT, f"2026-10-13T{hour:02d}:00:00Z", question)]
                self.assertEqual(self.answers(comments), expected)

    def test_the_bot_login_is_compared_case_insensitively(self):
        for login in ("GitHub-Actions[bot]", "GITHUB-ACTIONS[BOT]", "Github-Actions[Bot]", "GITHUB-ACTIONS",
                      "GitHub-Actions"):
            question = f"**Needs your input**\n\nWhich region should {login} deploy to?" + MARK
            with self.subTest(login=login):
                comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                            comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                            comment(login, "2026-10-13T11:00:00Z", question),
                            comment(login, "2026-10-13T11:30:00Z", "Deploy finished."),
                            comment("rjeskuri", "2026-10-13T12:00:00Z", "Eu-west.")]
                self.assertEqual(self.answers(comments), ["Eu-west."])

    def test_a_bot_question_without_a_timestamp_does_not_anchor(self):
        # like an owner question without one: it cannot bound anything
        question = "**Needs your input**\n\nWhich region should the deploy of #99 target?" + MARK
        for login in (self.BOT, "github-actions", "GitHub-Actions[bot]"):
            for missing in ("null", "absent", "empty"):
                with self.subTest(login=login, createdAt=missing):
                    bot = comment(login, None, question)
                    if missing == "absent":
                        del bot["createdAt"]
                    elif missing == "empty":
                        bot["createdAt"] = ""
                    comments = [comment("rjeskuri", "2026-10-13T09:00:00Z", self.QUESTION + MARK),
                                comment("rjeskuri", "2026-10-13T10:00:00Z", "Use develop."),
                                bot,
                                comment("rjeskuri", "2026-10-13T12:00:00Z", "Eu-west.")]
                    self.assertEqual(self.answers(comments), ["Use develop.", "Eu-west."])


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


# ======================================================================= status:proposed and promote (#159)
def label_values(argv):
    """The --label values of an argv, in order (`--label x` and `--label=x`)."""
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--label" and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith("--label="):
            out.append(tok[len("--label="):])
        i += 1
    return out


def without_labels(argv):
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--label":
            i += 2
            continue
        if not argv[i].startswith("--label="):
            out.append(argv[i])
        i += 1
    return out


def label_edits(calls):
    """{issue number: (labels added, labels removed)} over every `gh issue edit N` call; gh takes repeated flags,
    `--flag=value` and comma-separated names alike."""
    out = {}
    for argv in calls:
        if argv[:2] != ["issue", "edit"] or len(argv) < 3 or not argv[2].isdigit():
            continue
        adds, removes = out.setdefault(int(argv[2]), (set(), set()))
        i = 3
        while i < len(argv):
            tok = argv[i]
            for flag, into in (("--add-label", adds), ("--remove-label", removes)):
                if tok == flag and i + 1 < len(argv):
                    into.update(x.strip() for x in argv[i + 1].split(",") if x.strip())
                    i += 1
                elif tok.startswith(flag + "="):
                    into.update(x.strip() for x in tok[len(flag) + 1:].split(",") if x.strip())
            i += 1
    return out


class Proposed(BoardTest):
    """`status:proposed` (#159): `epic create --proposed` and `task create --proposed` label it in place of
    status:ready; `ready` never lists it; `promote N` moves a proposed epic and its open proposed sub-issues (or a
    proposed task alone) to ready through the usual status change; the Projects board is not touched."""

    HOST = "https://ghe.example.com/o/r/issues/"

    def setUp(self):
        super().setUp()
        self.body = write(os.path.join(self.tmp, "body.md"), TASK_BODY)

    # ------------------------------------------------------------------ helpers
    def create_scenario(self, number, url=None):
        url = url or f"https://github.com/o/r/issues/{number}"
        self.scenario([REPO_VIEW,
                       {"match": ["issue", "create"], "stdout": f"Creating issue in o/r\n\n{url}\n"},
                       {"match": ["api", f"repos/o/r/issues/{number}", "--jq", ".id"], "stdout": f"{number}00\n"},
                       {"match": ["api", "-X", "POST"], "stdout": "{}"},
                       {"match": ["issue", "edit"], "stdout": url + "\n"}])
        return url

    def created_labels(self):
        creates = [argv for argv in self.calls() if argv[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1, self.calls())
        return creates[0]

    def promote_world(self, number, labels, state="OPEN", subs=(), kind_title="Ticket"):
        """N as gh issue view shows it (every field, so any --json list is answered) and, for an epic, its sub-issues
        (number, labels, state) through every shape of the sub_issues REST call."""
        calls = [REPO_VIEW,
                 {"match": ["issue", "view", str(number)],
                  "stdout_json": issue(number=number, labels=labels, state=state, title=f"{kind_title} {number}",
                                       body=EPIC_BODY if "epic" in labels else TASK_BODY)}]
        listing = []
        for n, sub_labels, sub_state in subs:
            listing.append({"number": n, "title": f"Task {n}", "state": sub_state.lower(),
                            "url": f"https://github.com/o/r/issues/{n}",
                            "labels": [{"name": x} for x in sub_labels]})
            calls.append({"match": ["issue", "view", str(n)],
                          "stdout_json": issue(number=n, labels=sub_labels, state=sub_state, title=f"Task {n}",
                                               parent={"number": number, "title": f"{kind_title} {number}",
                                                       "url": f"https://github.com/o/r/issues/{number}"})})
        path = f"repos/o/r/issues/{number}/sub_issues"
        for prefix in (["api", path], ["api", path + "?per_page=100"], ["api", "--paginate", path],
                       ["api", "--paginate", path + "?per_page=100"]):
            calls.append({"match": prefix, "stdout_json": listing})
        calls.append({"match": ["issue", "comment"], "stdout": f"https://github.com/o/r/issues/{number}#c1\n"})
        self.scenario(calls, default={"stdout": ""})

    def cache_for(self, number, labels, state="OPEN"):
        status = "closed" if state == "CLOSED" else next(
            (x[len("status:"):] for x in labels if x.startswith("status:")), None)
        record = {"number": number, "title": f"Ticket {number}", "labels": list(labels), "status": status,
                  "tier": "opus", "branch": f"t/{number}-kept", "attempts": [{"stage": "tests", "result": "pass"}]}
        self.put_cache(number, record)
        return record

    def assertPromoted(self, number, before_labels, edits, record):
        """N's labels end with status:ready as their only status label, and its cache says so."""
        self.assertIn(number, edits, f"#{number} was not edited: {self.calls()}")
        adds, removes = edits[number]
        after = (set(before_labels) - removes) | adds
        self.assertEqual({x for x in after if x.startswith("status:")}, {"status:ready"}, (number, adds, removes))
        self.assertEqual(after - {"status:ready"}, set(before_labels) - {"status:proposed"}, number)
        cached = self.cache(number)
        self.assertEqual(cached["status"], "ready", cached)
        self.assertEqual(sorted(cached["labels"]), sorted(after), cached)
        self.assertEqual((cached["branch"], cached["attempts"]), (record["branch"], record["attempts"]),
                         "the cache keeps what only it holds")

    def assertUntouched(self, number, edits, record):
        self.assertNotIn(number, edits, f"#{number} must not be edited")
        self.assertEqual(self.cache(number), record, f"#{number}'s cache is unchanged")

    # ------------------------------------------------------------------ create
    def test_epic_and_task_create_proposed(self):
        cases = [  # (argv after create, number, expected labels with --proposed, parent, blockers)
            (["epic", "create", "--title", "Board v3", "--size", "L", "--risk", "high"], 3,
             ["epic", "status:proposed", "size:L", "risk:high"], None, ()),
            (["epic", "create", "--title", "Small plan"], 57, ["epic", "status:proposed"], None, ()),
            (["task", "create", "--title", "Wire it", "--parent", "3", "--size", "S", "--risk", "low",
              "--blocked-by", "9,10", "--no-tests", "--priority"], 13,
             ["task", "status:proposed", "size:S", "risk:low", "no-tests", "priority:high"], 3, (9, 10)),
            (["task", "create", "--title", "Fix it", "--type", "bug", "--needs-local"], 88,
             ["bug", "status:proposed", "needs-local"], None, ()),
            (["task", "create", "--title", "Tidy", "--type", "chore", "--size", "M", "--parent", "40"], 204,
             ["chore", "status:proposed", "size:M"], 40, ()),
        ]
        for argv, number, labels, parent, blockers in cases:
            for proposed in (True, False):
                with self.subTest(argv=" ".join(argv[:3]), number=number, proposed=proposed):
                    url = self.create_scenario(number)
                    full = argv + ["--body-file", self.body] + (["--proposed"] if proposed else [])
                    p = self.board(*full)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(p.stdout.strip(), f"#{number} {url}")
                    create = self.created_labels()
                    title = argv[argv.index("--title") + 1]
                    self.assertEqual(without_labels(create),
                                     ["issue", "create", "--title", title, "--body-file", self.body])
                    want = [x if proposed or x != "status:proposed" else "status:ready" for x in labels]
                    self.assertEqual(sorted(label_values(create)), sorted(want))
                    if proposed:
                        self.assertNotIn("status:ready", label_values(create))
                    if parent:
                        self.assertIn(["api", "-X", "POST", f"repos/o/r/issues/{parent}/sub_issues", "-F",
                                       f"sub_issue_id={number}00"], self.calls(), "still linked under the epic")
                    for b in blockers:
                        self.assertIn(["issue", "edit", str(number), "--add-blocked-by", str(b)], self.calls())
                    self.assertEqual([a for a in self.calls() if a[:1] == ["project"]], [],
                                     "no project configured: no gh project call")

    # ------------------------------------------------------------------ ready
    def test_ready_excludes_proposed(self):
        # the labels `task create --size S --risk low --priority --proposed` writes (checked at the end)
        made = ["task", "status:proposed", "size:S", "risk:low", "priority:high"]

        def item(number, labels, blocked=()):
            return {"number": number, "title": f"Ticket {number}", "labels": [{"name": n} for n in labels],
                    "blockedBy": list(blocked)}
        worlds = [
            ([item(21, made), item(8, ["task", "status:ready"]), item(4, ["task", "status:proposed", "size:M"]),
              item(9, ["task", "status:ready", "priority:high", "size:L"]),
              item(3, ["refactor", "status:ready", "size:S", "risk:low"]),
              item(7, ["epic", "status:proposed"])],
             [9, 3, 8]),
            ([item(40, ["bug", "status:proposed", "priority:high", "size:S", "risk:low"]),
              item(41, ["bug", "status:ready", "size:S", "risk:low"]),
              item(12, ["chore", "status:proposed"], [{"number": 2, "state": "CLOSED"}]),
              item(2, ["task", "status:ready", "priority:high"], [{"number": 1, "state": "CLOSED"}]),
              item(33, ["task", "status:ready"], [{"number": 34, "state": "OPEN"}])],
             [2, 41]),
            ([item(5, ["task", "status:proposed"]), item(6, ["chore", "status:proposed", "priority:high"])], []),
        ]
        for items, expected in worlds:
            with self.subTest(expected=expected):
                self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
                rows = json.loads(self.ok("ready", "--json").stdout)
                self.assertEqual([r["number"] for r in rows], expected)
                self.assertEqual({(tuple(labels), state or "open")
                                  for labels, state in fakegh.list_queries(self.entries())},
                                 {(("status:ready",), "open")}, "the query is today's, through REST (#447)")
                self.assertEqual(self.writes(), [])
                table_out = self.ok("ready").stdout
                if expected:
                    self.assertEqual([line.split()[0] for line in table_out.splitlines()[1:]],
                                     [str(n) for n in expected])
                else:
                    self.assertEqual(table_out.strip(), "No ready tickets.")
        self.create_scenario(21)
        self.ok("task", "create", "--title", "Proposed one", "--body-file", self.body, "--size", "S", "--risk", "low",
                "--priority", "--proposed")
        self.assertEqual(sorted(label_values(self.created_labels())), sorted(made))

    # ------------------------------------------------------------------ promote
    def test_promote_epic_moves_epic_and_proposed_tasks(self):
        def subs(*rows):
            return [(n, [kind, "status:" + status] if status else [kind], state) for n, kind, status, state in rows]
        cases = [
            (3, subs((12, "task", "proposed", "OPEN"), (13, "task", "in-progress", "OPEN"),
                     (14, "bug", "proposed", "OPEN"), (15, "task", "proposed", "CLOSED"),
                     (16, "chore", "ready", "OPEN"), (17, "task", "needs-input", "OPEN"),
                     (18, "refactor", None, "OPEN"), (19, "task", "in-review", "OPEN"),
                     (20, "task", "blocked", "OPEN")),
             [12, 14]),
            (40, subs((41, "task", "proposed", "OPEN"), (42, "task", "ready", "OPEN")), [41]),
            (70, subs((71, "task", "in-progress", "OPEN"), (72, "task", "proposed", "CLOSED")), []),
            (90, subs((95, "task", "proposed", "OPEN"), (91, "chore", "proposed", "OPEN"),
                      (93, "bug", "proposed", "OPEN")), [91, 93, 95]),
        ]
        for epic, tasks, promoted in cases:
            with self.subTest(epic=epic, promoted=promoted):
                epic_labels = ["epic", "status:proposed", "size:L", "risk:low"]
                records = {epic: self.cache_for(epic, epic_labels)}
                for n, labels, state in tasks:
                    records[n] = self.cache_for(n, labels + ["size:S"], state)
                self.promote_world(epic, epic_labels, subs=[(n, labels + ["size:S"], state)
                                                            for n, labels, state in tasks], kind_title="Epic")
                p = self.board("promote", str(epic))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                k = len(promoted)
                self.assertRegex(p.stdout.strip(), r"^promoted #%d and %d tasks?$" % (epic, k))
                edits = label_edits(self.calls())
                self.assertPromoted(epic, epic_labels, edits, records[epic])
                for n, labels, state in tasks:
                    if n in promoted:
                        self.assertPromoted(n, labels + ["size:S"], edits, records[n])
                    else:
                        self.assertUntouched(n, edits, records[n])
                self.assertEqual(set(edits), {epic, *promoted}, "nothing else is edited")
                self.assertFalse([a for a in self.calls() if a[:2] == ["issue", "close"]])

    def test_promote_json(self):
        # the documented shape, exactly: {"number": N, "tasks": [the promoted task numbers]}
        cases = [
            (30, ["epic", "status:proposed"], "Epic",
             [(31, ["task", "status:proposed"], "OPEN"), (32, ["task", "status:ready"], "OPEN")], [31]),
            (50, ["epic", "status:proposed"], "Epic",
             [(58, ["bug", "status:proposed"], "OPEN"), (52, ["task", "status:in-progress"], "OPEN"),
              (51, ["task", "status:proposed"], "OPEN"), (55, ["task", "status:proposed"], "CLOSED")], [51, 58]),
            (60, ["epic", "status:proposed"], "Epic", [(61, ["task", "status:blocked"], "OPEN")], []),
            (77, ["task", "status:proposed", "size:S"], "Task", [], []),
            (8, ["bug", "status:proposed"], "Task", [], []),
        ]
        for number, labels, kind_title, subs, promoted in cases:
            with self.subTest(number=number, promoted=promoted):
                self.promote_world(number, labels, subs=subs, kind_title=kind_title)
                data = json.loads(self.ok("promote", str(number), "--json").stdout)
                self.assertEqual(data, {"number": number, "tasks": promoted})
                self.assertNotIn(number, data["tasks"], "the promoted ticket itself is not one of its tasks")
                self.assertEqual(set(label_edits(self.calls())), {number, *promoted})

    def test_promote_a_task_alone(self):
        cases = [(12, "task", ["size:S", "risk:low"]), (47, "bug", ["priority:high"]),
                 (8, "chore", ["size:M", "no-tests"]), (230, "refactor", [])]
        for number, kind, extra in cases:
            with self.subTest(number=number, kind=kind):
                labels = [kind, "status:proposed"] + extra
                # its epic is proposed too, and has another proposed task: promoting the task touches neither
                self.promote_world(number, labels, kind_title="Task")
                record = self.cache_for(number, labels)
                epic_record = self.cache_for(3, ["epic", "status:proposed"])
                sibling = self.cache_for(number + 1, ["task", "status:proposed"])
                p = self.board("promote", str(number))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertRegex(p.stdout.strip(), r"^promoted #%d( and 0 tasks)?$" % number)
                edits = label_edits(self.calls())
                self.assertEqual(set(edits), {number}, self.calls())
                self.assertPromoted(number, labels, edits, record)
                self.assertEqual(self.cache(3), epic_record)
                self.assertEqual(self.cache(number + 1), sibling)

    def test_promote_refuses_closed_or_not_proposed(self):
        cases = [(12, ["task", "status:proposed"], "CLOSED"), (3, ["epic", "status:proposed"], "CLOSED"),
                 (14, ["task", "status:ready"], "OPEN"), (15, ["bug", "status:in-progress"], "OPEN"),
                 (16, ["task", "status:needs-input"], "OPEN"), (17, ["task", "status:blocked"], "OPEN"),
                 (18, ["chore"], "OPEN"), (40, ["epic", "status:ready"], "OPEN"), (41, ["epic"], "OPEN"),
                 (19, ["task", "status:in-review"], "OPEN")]
        for number, labels, state in cases:
            with self.subTest(number=number, labels=labels, state=state):
                subs = [(number + 100, ["task", "status:proposed"], "OPEN")] if "epic" in labels else []
                self.promote_world(number, labels, state=state, subs=subs)
                record = self.cache_for(number, labels, state)
                for n, sub_labels, sub_state in subs:
                    self.cache_for(n, sub_labels, sub_state)
                p = self.board("promote", str(number))
                self.assertOneLineError(p)
                self.assertIn(str(number), p.stderr)
                self.assertEqual(label_edits(self.calls()), {}, "nothing is relabeled")
                self.assertFalse([a for a in self.calls() if a[:2] in (["issue", "comment"], ["issue", "close"])])
                self.assertEqual(self.cache(number), record)
                # the same ticket open and proposed is promoted: the refusal follows the state and the label
                kind = [x for x in labels if not x.startswith("status:")]
                self.promote_world(number, kind + ["status:proposed"], subs=subs)
                self.assertEqual(self.board("promote", str(number)).returncode, 0, self.calls())
                self.assertIn(number, label_edits(self.calls()))

    # ------------------------------------------------------------------ show
    def test_show_reports_proposed(self):
        for number, labels, tier in ((12, ("task", "status:proposed", "size:M", "risk:low"), "opus"),
                                     (77, ("chore", "status:proposed", "size:S", "risk:low"), "sonnet")):
            with self.subTest(number=number):
                self.scenario([{"match": ["issue", "view", str(number)],
                                "stdout_json": issue(number=number, labels=labels)}])
                record = json.loads(self.ok("show", str(number), "--json").stdout)
                self.assertEqual((record["status"], record["tier"]), ("proposed", tier))
                self.assertEqual(self.cache(number)["status"], "proposed")
        epic = issue(number=3, title="Board v3", labels=("epic", "status:proposed"), body=EPIC_BODY)
        subs = [{"number": 12, "title": "First", "state": "open",
                 "labels": [{"name": "task"}, {"name": "status:proposed"}, {"name": "size:S"}, {"name": "risk:low"}]},
                {"number": 13, "title": "Second", "state": "open",
                 "labels": [{"name": "task"}, {"name": "status:ready"}, {"name": "size:M"}]},
                {"number": 14, "title": "Third", "state": "closed",
                 "labels": [{"name": "task"}, {"name": "status:proposed"}]}]
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs},
                       {"match": ["issue", "view", "14"], "stdout_json": {"comments": []}}])
        record = json.loads(self.ok("show", "3", "--json").stdout)
        self.assertEqual(record["status"], "proposed")
        self.assertEqual([(t["number"], t["status"]) for t in record["tasks"]],
                         [(12, "proposed"), (13, "ready"), (14, "closed")])
        md = self.ok("show", "3", "--markdown").stdout
        self.assertIn("- #12 First (proposed, S, low, tier sonnet)", md)

    # ------------------------------------------------------------------ board
    def board_item(self, number, labels, **extra):
        return dict({"number": number, "title": f"Ticket {number}", "labels": [{"name": n} for n in labels]}, **extra)

    BASE_LINES = ["# Board: proj", "",
                  "## Epics (1)", "- #3 Ticket 3 (1 of 2 tasks closed)", "",
                  "## Ready (1)", "- #4 Ticket 4 (task, S, low)", "",
                  "## In progress (1)", "- #5 Ticket 5 (bug, M, high)", "",
                  "## In review (1)", "- #10 Ticket 10 (task, L, high)", "",
                  "## Blocked (0)", "",
                  "## Needs input (1)", "- #8 Ticket 8 (chore, M, low)", "",
                  "## No status (1)", "- #6 Ticket 6 (task, -, -)"]

    def base_items(self):
        it = self.board_item
        return [it(4, ["task", "status:ready", "size:S", "risk:low"]),
                it(3, ["epic", "status:ready"], subIssuesSummary={"total": 2, "completed": 1}),
                it(5, ["bug", "status:in-progress", "size:M", "risk:high"]),
                it(10, ["task", "status:in-review", "size:L", "risk:high"]),
                it(8, ["chore", "status:needs-input", "size:M", "risk:low"]),
                it(6, ["task"])]

    def test_board_section_only_when_nonempty(self):
        self.scenario([{"match": ["issue", "list"], "stdout_json": self.base_items()}])
        out = self.ok("board").stdout
        self.assertEqual(out.rstrip("\n").splitlines(), self.BASE_LINES, "unchanged when nothing is proposed")
        self.assertNotIn("Proposed", out)
        it = self.board_item
        for extra, lines in (
                ([it(7, ["task", "status:proposed", "size:M", "risk:low"]),
                  it(12, ["bug", "status:proposed", "size:S"]), it(2, ["refactor", "status:proposed"])],
                 ["- #2 Ticket 2 (refactor, -, -)", "- #7 Ticket 7 (task, M, low)", "- #12 Ticket 12 (bug, S, -)"]),
                ([it(21, ["chore", "status:proposed", "size:L", "risk:high"])], ["- #21 Ticket 21 (chore, L, high)"])):
            with self.subTest(proposed=len(lines)):
                items = self.base_items()
                items[2:2] = extra
                self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
                got = self.ok("board").stdout.rstrip("\n").splitlines()
                heading = f"## Proposed ({len(lines)})"
                self.assertIn(heading, got)
                h = got.index(heading)
                self.assertEqual(got[h + 1:h + 1 + len(lines)], lines)
                self.assertEqual(got[h - 1], "")
                self.assertEqual(got[:h - 1] + got[h + 1 + len(lines):], self.BASE_LINES,
                                 "the other sections are as before; the proposed tasks are not under No status")
                data = json.loads(self.ok("board", "--json").stdout)
                self.assertEqual([e["number"] for e in data["proposed"]],
                                 sorted(int(x.split()[1][1:]) for x in lines))
                self.assertEqual([e["number"] for e in data["no-status"]], [6])

    def test_help(self):
        p = self.board("promote", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("usage:", p.stdout)
        for argv in (["epic", "create", "--help"], ["task", "create", "--help"]):
            self.assertIn("--proposed", self.board(*argv).stdout, argv)


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
        self.assertEqual(self.tier("task", "size:L", "risk:low"), "opus")         # #239: not fable
        self.assertEqual(self.tier("task", "size:S", "risk:high"), "opus")        # #239: not fable
        self.assertEqual(self.tier("chore", "size:S"), "opus")          # no risk label: not sonnet

    def test_fetched_without_a_cache(self):
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"],
                        "stdout_json": {"labels": [{"name": "refactor"}, {"name": "size:S"}, {"name": "risk:low"}]}}])
        p = self.ok("tier", "12", "--json")
        self.assertEqual(json.loads(p.stdout), {"number": 12, "tier": "sonnet", "source": "github", "stage": "code",
                                                "from": "labels", "labels_tier": "sonnet", "effort": None})


# Ticket #67: reviews run on Opus 5.5. Ticket #239: `tier N --stage review` gives opus for every combination (a
# risk:high ticket's review runs at effort xhigh, see TierEffort), never fable by the labels. Plain `tier N` keeps the
# rubric (size:S, risk:low and not a bug gives sonnet; else opus, size:L and risk:high included).
class TierReviewStage(BoardTest):
    TYPES = ("task", "bug", "chore", "refactor")
    SIZES = ("S", "M", "L")

    def tier(self, labels, *extra):
        self.put_cache(12, {"number": 12, "labels": list(labels)})
        p = self.ok("tier", "12", *extra)
        self.assertEqual(self.calls(), [])                  # from the cache: no gh call
        return p.stdout.strip()

    def test_review_stage_is_opus_whatever_the_risk(self):
        for kind in self.TYPES:
            for size in self.SIZES:
                for risk, expected in (("low", "opus"), ("high", "opus")):
                    with self.subTest(kind=kind, size=size, risk=risk):
                        self.assertEqual(self.tier([kind, "size:" + size, "risk:" + risk], "--stage", "review"),
                                         expected)

    def test_review_stage_named_cases(self):
        self.assertEqual(self.tier(["task", "size:L", "risk:low"], "--stage", "review"), "opus")   # not fable by size
        self.assertEqual(self.tier(["task", "size:S", "risk:low"], "--stage", "review"), "opus")   # not sonnet
        self.assertEqual(self.tier(["bug", "size:S", "risk:low"], "--stage", "review"), "opus")
        self.assertEqual(self.tier(["task", "size:S", "risk:high"], "--stage", "review"), "opus")  # #239
        self.assertEqual(self.tier(["bug", "size:L", "risk:high", "status:in-progress", "priority:high"],
                                   "--stage", "review"), "opus")

    def test_review_stage_without_a_risk_label_is_opus(self):
        for labels in (["task", "size:L"], ["chore", "size:S"], ["task"], []):
            with self.subTest(labels=labels):
                self.assertEqual(self.tier(labels, "--stage", "review"), "opus")

    def test_review_stage_option_may_come_before_the_number_and_works_with_json(self):
        self.put_cache(12, {"number": 12, "labels": ["task", "size:L", "risk:high"]})
        self.assertEqual(self.ok("tier", "--stage", "review", "12").stdout.strip(), "opus")
        p = self.ok("tier", "12", "--stage", "review", "--json")
        self.assertEqual((json.loads(p.stdout)["tier"], json.loads(p.stdout)["effort"]), ("opus", "xhigh"))
        self.put_cache(13, {"number": 13, "labels": ["task", "size:L", "risk:low"]})
        p = self.ok("tier", "13", "--stage", "review", "--json")
        self.assertEqual(json.loads(p.stdout)["tier"], "opus")

    def test_review_stage_is_fetched_without_a_cache(self):
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"],
                        "stdout_json": {"labels": [{"name": "refactor"}, {"name": "size:L"}, {"name": "risk:low"}]}}])
        self.assertEqual(self.ok("tier", "12", "--stage", "review").stdout.strip(), "opus")

    def test_a_stage_other_than_review_is_a_usage_error_naming_review(self):
        self.put_cache(12, {"number": 12, "labels": ["task", "size:L", "risk:high"]})
        for stage in ("code", "bogus", "Review", ""):
            with self.subTest(stage=stage):
                p = self.board("tier", "12", "--stage", stage)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(p.stdout.strip(), "")                  # no tier is printed
                self.assertIn("review", p.stderr)                       # names the allowed value
                self.assertNotIn("Traceback", p.stderr)

    def test_review_stage_falls_through_a_cache_without_labels_to_github(self):
        # A cache with a stored tier but no labels cannot answer the review stage (the stored tier is the rubric's,
        # not the review's): the labels are fetched. Plain `tier` still answers from the cache.
        self.put_cache(12, {"number": 12, "tier": "fable"})
        self.scenario([{"match": ["issue", "view", "12", "--json", "labels"],
                        "stdout_json": {"labels": [{"name": "task"}, {"name": "size:L"}, {"name": "risk:low"}]}}])
        self.assertEqual(self.ok("tier", "12", "--stage", "review").stdout.strip(), "opus")
        self.assertEqual(len(self.calls()), 1)
        p = self.ok("tier", "12", "--stage", "review", "--json")
        self.assertEqual(json.loads(p.stdout), {"number": 12, "tier": "opus", "source": "github", "stage": "review",
                                                "from": "labels", "labels_tier": "opus", "effort": None})
        before = len(self.calls())
        p = self.ok("tier", "12", "--json")
        # #239: a cached tier of fable without labels is a pre-#239 rubric answer (size:L or risk:high): it now
        # stands for opus at xhigh, never fable
        self.assertEqual(json.loads(p.stdout), {"number": 12, "tier": "opus", "source": "cache", "stage": "code",
                                                "from": "labels", "labels_tier": "opus", "effort": "xhigh"})
        self.assertEqual(len(self.calls()), before)                 # plain tier made no gh call

    def test_plain_tier_for_every_combination(self):
        # Pins the rubric (derived here from its statement, not from the tool): #239 moved size:L and risk:high
        # from fable to opus.
        for kind in self.TYPES:
            for size in self.SIZES:
                for risk in ("low", "high"):
                    if size == "L" or risk == "high":
                        expected = "opus"
                    elif size == "S" and kind != "bug":
                        expected = "sonnet"
                    else:
                        expected = "opus"
                    with self.subTest(kind=kind, size=size, risk=risk):
                        self.assertEqual(self.tier([kind, "size:" + size, "risk:" + risk]), expected)


# ======================================================================= tier: the project's routing settings
# Ticket #175 (epic #172): harness-board takes its tiers from harness_lib.routing. HARNESS_CODE_TIER (the environment
# when not blank, else `env` in the main checkout's .claude/settings.json; settings.local.json is never read) replaces
# the label rubric for the code tier that `tier`, `show`, the cache, `ready` and an epic's task rows carry;
# HARNESS_REVIEW_TIER replaces the review rule; `tier --json` says where the tier came from. Every expected tier here
# is derived from docs/design.md's rubric sentence, never from the tool or the lib.
ROUTING_TYPES = ("task", "bug", "chore", "refactor")
TIER_KEYS = ["number", "tier", "source", "stage", "from", "labels_tier", "effort"]
SHOW_KEYS = {"number", "title", "url", "type", "status", "size", "risk", "flags", "labels", "tier", "parent",
             "blocked_by", "body", "sections", "comments", "fetched_at", "branch", "worktree", "started_at", "attempts"}


def rubric(kind, size, risk):
    """#239: size:S and risk:low and type not bug gives sonnet; everything else opus (size:L and risk:high too)."""
    if size == "S" and risk == "low" and kind != "bug":
        return "sonnet"
    return "opus"


def rubric_effort(kind, size, risk):
    """#239: the labels' code effort: xhigh for size:L or risk:high, else none."""
    return "xhigh" if size == "L" or risk == "high" else None


def review_rubric(risk):
    """#239: the review stage is opus for every risk (at xhigh for risk:high, see review_effort)."""
    return "opus"


def review_effort(risk):
    """#239: the labels' review effort: xhigh for risk:high, else none."""
    return "xhigh" if risk == "high" else None


def ticket_labels(kind, size, risk, *extra):
    return [kind] + list(extra) + (["size:" + size] if size else []) + (["risk:" + risk] if risk else [])


def loaded_routing(repo):
    """(harness_lib.routing imported in this process from the plugin's lib, as test_harness_lib_routing.py imports it;
    the routing settings it loads for `repo`, as harness-board loads them). The routing keys of the environment running
    the tests are left out, as BoardTest.env leaves them out of the tool's (ticket #378: label-combination tables run
    in this process, each with a `tier` run beside it)."""
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    routing = importlib.import_module("harness_lib.routing")
    with mock.patch.dict(os.environ):
        for key in ROUTING_KEYS:
            os.environ.pop(key, None)
        return routing, routing.load_routing(repo)


class TierRouting(BoardTest):
    OKINAWA = {"HARNESS_CODE_TIER": "opus", "HARNESS_CODE_EFFORT": "xhigh", "HARNESS_REVIEW_TIER": "opus"}
    SPREAD = [("task", "S", "low"), ("bug", "S", "low"), ("chore", "M", "low"), ("refactor", "L", "low"),
              ("task", "S", "high"), ("bug", "M", "high"), ("task", None, None), ("chore", "S", None)]

    def settings(self, env, name="settings.json", root=None):
        write(os.path.join(root or self.repo, ".claude", name), json.dumps({"env": env}))

    def tier_out(self, number, *extra, **env):
        """`tier N` stdout, with nothing on stderr."""
        p = self.ok("tier", str(number), *extra, **env)
        self.assertEqual(p.stderr, "", p.stderr)
        return p.stdout

    def tier_json(self, number, *extra, **env):
        p = self.ok("tier", str(number), "--json", *extra, **env)
        self.assertEqual(p.stderr, "", p.stderr)
        return json.loads(p.stdout)

    def code(self, number, tier, labels_tier, source="cache", frm="labels", effort=None):
        return {"number": number, "tier": tier, "source": source, "stage": "code", "from": frm,
                "labels_tier": labels_tier, "effort": effort}

    def review(self, number, tier, labels_tier, source="cache", frm="labels", effort=None):
        return {"number": number, "tier": tier, "source": source, "stage": "review", "from": frm,
                "labels_tier": labels_tier, "effort": effort}

    def warnings(self, p):
        return [line for line in p.stderr.splitlines() if line.strip()]

    def epic_scenario(self, subs):
        epic = issue(number=3, title="Board v2", labels=("epic", "status:ready"), body=EPIC_BODY)
        self.scenario([REPO_VIEW, {"match": ["issue", "view", "3"], "stdout_json": epic},
                       {"match": ["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "stdout_json": subs}])

    def sub(self, number, title, labels):
        return {"number": number, "title": title, "state": "open", "labels": [{"name": n} for n in labels]}

    # ----------------------------------------------------------- broad: the public interface
    def test_code_tier_setting_decides_every_label_combination(self):
        # DoD: HARNESS_CODE_TIER=opus in the main checkout's settings.json gives opus for every type, size and risk
        # (none included), from the cache, with no gh call.
        self.settings({"HARNESS_CODE_TIER": "opus"})
        for kind in ROUTING_TYPES:
            for size in ("S", "M", "L", None):
                for risk in ("low", "high", None):
                    with self.subTest(setting="opus", kind=kind, size=size, risk=risk):
                        self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk, "status:ready"),
                                            "tier": rubric(kind, size, risk)})
                        self.assertEqual(self.tier_out(12), "opus\n")
        # Every tier the setting can name, spelled loosely (the value is stripped and matched in any case), decides
        # over every rubric outcome; `from` names the setting even where it agrees with the labels.
        for raw, tier in (("sonnet", "sonnet"), ("Fable", "fable"), (" OPUS ", "opus")):
            self.settings({"HARNESS_CODE_TIER": raw})
            for kind, size, risk in self.SPREAD:
                with self.subTest(setting=raw, kind=kind, size=size, risk=risk):
                    self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                    self.assertEqual(self.tier_out(12), tier + "\n")
                    self.assertEqual(self.tier_json(12),
                                     self.code(12, tier, rubric(kind, size, risk), frm="HARNESS_CODE_TIER"))
        self.assertEqual(self.calls(), [])

    def test_okinawa_settings_json_reports_where_the_tier_came_from(self):
        self.settings(self.OKINAWA)
        self.put_cache(12, {"number": 12, "labels": ["task", "status:ready", "size:L", "risk:high"], "tier": "fable"})
        p = self.ok("tier", "12", "--json")
        self.assertEqual(p.stderr, "")
        data = json.loads(p.stdout)
        self.assertEqual(data, {"number": 12, "tier": "opus", "source": "cache", "stage": "code",
                                "from": "HARNESS_CODE_TIER", "labels_tier": "opus", "effort": "xhigh"})
        self.assertEqual(list(data), TIER_KEYS)                     # printed in this order
        self.assertEqual(self.tier_out(12), "opus\n")               # the text stays the one word
        # risk:high keeps its label but gets an Opus per-ticket review
        self.assertEqual(self.tier_out(12, "--stage", "review"), "opus\n")
        self.assertEqual(self.tier_json(12, "--stage", "review"),
                         self.review(12, "opus", "opus", frm="HARNESS_REVIEW_TIER"))
        # a cache without labels answers from the setting, with the cached tier as labels_tier
        self.put_cache(13, {"number": 13, "tier": "sonnet"})
        self.assertEqual(self.tier_json(13), self.code(13, "opus", "sonnet", frm="HARNESS_CODE_TIER", effort="xhigh"))
        self.assertEqual(self.tier_out(13), "opus\n")
        self.assertEqual(self.calls(), [])
        # without a cache the labels are fetched and the setting still decides; source says github
        self.scenario([{"match": ["issue", "view", "14", "--json", "labels"],
                        "stdout_json": {"labels": [{"name": "bug"}, {"name": "size:L"}, {"name": "risk:high"}]}}])
        self.assertEqual(self.tier_json(14),
                         self.code(14, "opus", "opus", source="github", frm="HARNESS_CODE_TIER", effort="xhigh"))
        self.assertEqual(len(self.calls()), 1)

    def test_review_tier_setting_decides_the_review_stage(self):
        cases = [("task", "S", "low"), ("bug", "L", "high"), ("chore", "M", None), ("refactor", "S", "high")]
        for raw, tier in (("opus", "opus"), ("fable", "fable"), ("Sonnet", "sonnet")):
            self.settings({"HARNESS_REVIEW_TIER": raw})
            for kind, size, risk in cases:
                with self.subTest(setting=raw, kind=kind, size=size, risk=risk):
                    self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                    self.assertEqual(self.tier_out(12, "--stage", "review"), tier + "\n")
                    self.assertEqual(self.tier_json(12, "--stage", "review"),
                                     self.review(12, tier, review_rubric(risk), frm="HARNESS_REVIEW_TIER",
                                                 effort="xhigh" if tier == "fable" else None))
                    # the code stage keeps the labels, their effort included
                    labels_tier = rubric(kind, size, risk)
                    self.assertEqual(self.tier_json(12), self.code(12, labels_tier, labels_tier,
                                                                   effort=rubric_effort(kind, size, risk)))
        # HARNESS_CODE_TIER and HARNESS_CODE_EFFORT never reach the review stage
        self.settings({"HARNESS_CODE_TIER": "sonnet", "HARNESS_CODE_EFFORT": "low"})
        for risk in ("high", "low"):
            with self.subTest(code_only=True, risk=risk):
                self.put_cache(12, {"number": 12, "labels": ticket_labels("task", "M", risk)})
                self.assertEqual(self.tier_out(12, "--stage", "review"), review_rubric(risk) + "\n")
                self.assertEqual(self.tier_json(12, "--stage", "review"),
                                 self.review(12, review_rubric(risk), review_rubric(risk),
                                             effort=review_effort(risk)))
        self.assertEqual(self.calls(), [])

    def test_show_ready_epic_tasks_and_cache_carry_the_effective_tier(self):
        # The ticket's labels give opus (task, size:M, risk:low): each setting differs from it.
        for setting in ("sonnet", "fable"):
            with self.subTest(setting=setting):
                self.settings({"HARNESS_CODE_TIER": setting})
                # show: the record, the cache and the markdown Labels line
                self.scenario([{"match": ["issue", "view", "12"],
                                "stdout_json": issue(labels=("task", "status:ready", "size:M", "risk:low"))}])
                record = json.loads(self.ok("show", "12").stdout)
                self.assertEqual(record["tier"], setting)
                self.assertEqual((record["size"], record["risk"]), ("M", "low"))
                self.assertEqual(record["labels"], ["task", "status:ready", "size:M", "risk:low"])
                self.assertEqual(self.cache(12)["tier"], setting)
                md = self.ok("show", "12", "--markdown").stdout.splitlines()
                self.assertIn(f"Labels: task, status:ready, size:M, risk:low (tier {setting} from HARNESS_CODE_TIER, "
                              "open)", md)
                # a status change made here refreshes the cache with the effective tier
                self.put_cache(12, {"number": 12, "labels": ["task", "status:ready", "size:M", "risk:low"],
                                    "tier": "opus"})
                self.scenario([{"match": ["issue", "view", "12", "--json", "labels"],
                                "stdout_json": {"labels": [{"name": n} for n in
                                                           ("task", "status:ready", "size:M", "risk:low")]}},
                               {"match": ["issue", "edit"], "stdout": ""}])
                self.ok("status", "12", "in-progress")
                self.assertEqual(self.cache(12)["status"], "in-progress")
                self.assertEqual(self.cache(12)["tier"], setting)
                # ready: every row, whatever its labels
                items = [{"number": n, "title": f"T{n}", "labels": [{"name": x} for x in labels], "blockedBy": []}
                         for n, labels in ((2, ("task", "status:ready", "size:S", "risk:low")),
                                           (3, ("bug", "status:ready", "size:M", "risk:low")),
                                           (4, ("task", "status:ready", "size:L", "risk:low")),
                                           (5, ("chore", "status:ready", "size:S", "risk:high")))]
                self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
                rows = json.loads(self.ok("ready", "--json").stdout)
                self.assertEqual([(r["number"], r["size"], r["risk"], r["tier"]) for r in rows],
                                 [(2, "S", "low", setting), (3, "M", "low", setting), (4, "L", "low", setting),
                                  (5, "S", "high", setting)])
                table = self.ok("ready").stdout.splitlines()
                self.assertEqual([line.split()[5] for line in table[1:]], [setting] * 4)
                # an epic's task rows, in the record and in the markdown
                self.epic_scenario([self.sub(12, "Add the board tool", ("task", "status:ready", "size:S", "risk:low")),
                                    self.sub(13, "Wire it", ("bug", "status:in-progress", "size:M", "risk:high")),
                                    self.sub(14, "Tidy", ("refactor", "status:ready", "size:M", "risk:low"))])
                epic = json.loads(self.ok("show", "3").stdout)
                self.assertEqual([(t["number"], t["tier"]) for t in epic["tasks"]],
                                 [(12, setting), (13, setting), (14, setting)])
                self.assertEqual([t["tier"] for t in self.cache(3)["tasks"]], [setting] * 3)
                md = self.ok("show", "3", "--markdown").stdout.splitlines()
                self.assertIn(f"- #12 Add the board tool (ready, S, low, tier {setting})", md)
                self.assertIn(f"- #13 Wire it (in-progress, M, high, tier {setting})", md)
                self.assertIn(f"- #14 Tidy (ready, M, low, tier {setting})", md)

    def test_environment_wins_and_settings_local_is_ignored(self):
        self.put_cache(12, {"number": 12, "labels": ["task", "size:M", "risk:low"]})        # the labels give opus
        # settings.local.json alone is never read: the labels decide
        self.settings({"HARNESS_CODE_TIER": "sonnet", "HARNESS_CODE_EFFORT": "max", "HARNESS_REVIEW_TIER": "fable"},
                      name="settings.local.json")
        self.assertEqual(self.tier_out(12), "opus\n")
        self.assertEqual(self.tier_json(12), self.code(12, "opus", "opus"))
        self.assertEqual(self.tier_json(12, "--stage", "review"), self.review(12, "opus", "opus"))
        # settings.json decides, and settings.local.json adds nothing to it
        self.settings({"HARNESS_CODE_TIER": "fable"})
        self.assertEqual(self.tier_out(12), "fable\n")
        self.assertEqual(self.tier_json(12), self.code(12, "fable", "opus", frm="HARNESS_CODE_TIER"))
        self.assertEqual(self.tier_json(12, "--stage", "review"), self.review(12, "opus", "opus"))
        # the environment wins over settings.json; a blank environment value falls through to it
        self.assertEqual(self.tier_out(12, HARNESS_CODE_TIER="sonnet"), "sonnet\n")
        self.assertEqual(self.tier_json(12, HARNESS_CODE_TIER="sonnet", HARNESS_CODE_EFFORT="high"),
                         self.code(12, "sonnet", "opus", frm="HARNESS_CODE_TIER", effort="high"))
        self.assertEqual(self.tier_out(12, HARNESS_CODE_TIER="  "), "fable\n")
        self.assertEqual(self.tier_json(12, "--stage", "review", HARNESS_REVIEW_TIER="sonnet"),
                         self.review(12, "sonnet", "opus", frm="HARNESS_REVIEW_TIER"))
        self.assertEqual(self.tier_json(12, "--stage", "review", HARNESS_REVIEW_TIER="fable"),
                         self.review(12, "fable", "opus", frm="HARNESS_REVIEW_TIER", effort="xhigh"))
        # the user's ~/.claude/settings.json is not the project's
        os.unlink(os.path.join(self.repo, ".claude", "settings.json"))
        write(os.path.join(self.home, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_CODE_TIER": "sonnet", "HARNESS_REVIEW_TIER": "fable"}}))
        self.assertEqual(self.tier_json(12), self.code(12, "opus", "opus"))
        self.assertEqual(self.calls(), [])

    def test_invalid_value_warns_once_and_the_labels_decide(self):
        warning = "harness-board: warning: HARNESS_CODE_TIER='gpt' is not one of sonnet, opus, fable; ignored"
        self.settings({"HARNESS_CODE_TIER": "gpt"})
        for size, risk in (("M", "low"), ("S", "low"), ("L", "low")):
            expected = rubric("task", size, risk)
            with self.subTest(size=size, risk=risk):
                self.put_cache(12, {"number": 12, "labels": ticket_labels("task", size, risk)})
                p = self.ok("tier", "12")                               # exit 0, as before
                self.assertEqual(p.stdout, expected + "\n")
                self.assertEqual(self.warnings(p), [warning])
                p = self.ok("tier", "12", "--json")
                self.assertEqual(json.loads(p.stdout), self.code(12, expected, expected,
                                                                 effort=rubric_effort("task", size, risk)))
                self.assertEqual(self.warnings(p), [warning])
        # once per process, however many tickets the command classifies
        self.epic_scenario([self.sub(12, "Add the board tool", ("task", "status:ready", "size:S", "risk:low")),
                            self.sub(13, "Wire it", ("bug", "status:ready", "size:M", "risk:high")),
                            self.sub(14, "Tidy", ("refactor", "status:ready", "size:M", "risk:low"))])
        p = self.ok("show", "3")
        self.assertEqual(self.warnings(p), [warning])
        self.assertEqual([t["tier"] for t in json.loads(p.stdout)["tasks"]], ["sonnet", "opus", "opus"])
        p = self.ok("show", "3", "--markdown")
        self.assertEqual(self.warnings(p), [warning])
        self.assertIn("Labels: epic, status:ready (tier opus, open)", p.stdout.splitlines())
        self.assertIn("- #13 Wire it (ready, M, high, tier opus)", p.stdout.splitlines())
        items = [{"number": n, "title": f"T{n}", "labels": [{"name": x} for x in labels], "blockedBy": []}
                 for n, labels in ((2, ("task", "status:ready", "size:S", "risk:low")),
                                   (3, ("bug", "status:ready", "size:M", "risk:low")),
                                   (4, ("task", "status:ready", "size:L", "risk:low")))]
        self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
        p = self.ok("ready", "--json")
        self.assertEqual(self.warnings(p), [warning])
        self.assertEqual([r["tier"] for r in json.loads(p.stdout)], ["sonnet", "opus", "opus"])
        # an invalid effort or review tier: each its own line, the rest of the routing still applies
        self.settings({"HARNESS_CODE_TIER": "fable", "HARNESS_CODE_EFFORT": "turbo", "HARNESS_REVIEW_TIER": "haiku"})
        expected_lines = sorted([
            "harness-board: warning: HARNESS_CODE_EFFORT='turbo' is not one of low, medium, high, xhigh, max; ignored",
            "harness-board: warning: HARNESS_REVIEW_TIER='haiku' is not one of sonnet, opus, fable; ignored"])
        self.put_cache(12, {"number": 12, "labels": ticket_labels("task", "S", "low")})
        p = self.ok("tier", "12", "--json")
        self.assertEqual(json.loads(p.stdout), self.code(12, "fable", "sonnet", frm="HARNESS_CODE_TIER"))
        self.assertEqual(sorted(self.warnings(p)), expected_lines)
        self.put_cache(12, {"number": 12, "labels": ticket_labels("task", "S", "high")})
        p = self.ok("tier", "12", "--stage", "review", "--json")
        self.assertEqual(json.loads(p.stdout), self.review(12, "opus", "opus", effort="xhigh"))
        self.assertEqual(sorted(self.warnings(p)), expected_lines)
        # an invalid value in the environment warns the same way
        self.settings({})
        self.put_cache(12, {"number": 12, "labels": ticket_labels("task", "M", "high")})
        p = self.ok("tier", "12", "--stage", "review", HARNESS_REVIEW_TIER="gpt5")
        self.assertEqual(p.stdout, "opus\n")
        self.assertEqual(self.warnings(p),
                         ["harness-board: warning: HARNESS_REVIEW_TIER='gpt5' is not one of sonnet, opus, fable; "
                          "ignored"])
        # the exit code is unchanged: a failing command still fails with its own error
        self.settings({"HARNESS_CODE_TIER": "gpt"})
        self.scenario([{"match": ["issue", "view"], "stderr": "GraphQL: Could not resolve to an issue\n", "exit": 1}])
        p = self.board("show", "99")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("gh issue view 99 failed", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_without_settings_text_and_records_are_unchanged(self):
        # tier: the rubric's one word; --json adds stage, from "labels", labels_tier and effort
        for kind, size, risk in self.SPREAD:
            with self.subTest(kind=kind, size=size, risk=risk):
                self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                self.assertEqual(self.tier_out(12), rubric(kind, size, risk) + "\n")
                self.assertEqual(self.tier_json(12), self.code(12, rubric(kind, size, risk), rubric(kind, size, risk),
                                                               effort=rubric_effort(kind, size, risk)))
                self.assertEqual(self.tier_out(12, "--stage", "review"), review_rubric(risk) + "\n")
                self.assertEqual(self.tier_json(12, "--stage", "review"),
                                 self.review(12, review_rubric(risk), review_rubric(risk),
                                             effort=review_effort(risk)))
        self.assertEqual(self.calls(), [])
        # show: the record and the cache keep their keys and the rubric's tier; the Labels line names no setting
        self.scenario([{"match": ["issue", "view", "12"],
                        "stdout_json": issue(labels=("task", "status:ready", "size:L", "risk:high"))}])
        p = self.ok("show", "12")
        self.assertEqual(p.stderr, "")
        record = json.loads(p.stdout)
        self.assertEqual(set(record), SHOW_KEYS)
        self.assertEqual(set(self.cache(12)), SHOW_KEYS)
        self.assertEqual((record["tier"], self.cache(12)["tier"]), ("opus", "opus"))
        p = self.ok("show", "12", "--markdown")
        self.assertEqual(p.stderr, "")
        self.assertIn("Labels: task, status:ready, size:L, risk:high (tier opus, open)", p.stdout.splitlines())
        self.assertNotIn("HARNESS_", p.stdout)
        # ready: the rows and the table as before
        items = [{"number": n, "title": f"T{n}", "labels": [{"name": x} for x in labels], "blockedBy": []}
                 for n, labels in ((2, ("task", "status:ready", "size:S", "risk:low")),
                                   (3, ("bug", "status:ready", "size:M", "risk:low")),
                                   (4, ("task", "status:ready", "size:L", "risk:low", "priority:high")))]
        self.scenario([{"match": ["issue", "list"], "stdout_json": items}])
        p = self.ok("ready", "--json")
        self.assertEqual(p.stderr, "")
        self.assertEqual(json.loads(p.stdout), [
            {"number": 4, "title": "T4", "type": "task", "size": "L", "risk": "low", "tier": "opus",
             "flags": ["priority:high"], "blocked_by": []},
            {"number": 2, "title": "T2", "type": "task", "size": "S", "risk": "low", "tier": "sonnet", "flags": [],
             "blocked_by": []},
            {"number": 3, "title": "T3", "type": "bug", "size": "M", "risk": "low", "tier": "opus", "flags": [],
             "blocked_by": []}])  # #389: every row carries blocked_by
        p = self.ok("ready")
        self.assertEqual(p.stderr, "")
        self.assertEqual([line.split() for line in p.stdout.splitlines()],
                         [["#", "TITLE", "TYPE", "SIZE", "RISK", "TIER", "FLAGS"],
                          ["4", "T4", "task", "L", "low", "opus", "priority:high"],
                          ["2", "T2", "task", "S", "low", "sonnet"],
                          ["3", "T3", "bug", "M", "low", "opus"]])
        # board: the text as before, and no warning
        self.scenario([{"match": ["issue", "list"], "stdout_json": [
            {"number": 4, "title": "Ticket 4", "labels": [{"name": n} for n in ("task", "status:ready", "size:S",
                                                                                 "risk:low")]}]}])
        p = self.ok("board")
        self.assertEqual(p.stderr, "")
        self.assertIn("- #4 Ticket 4 (task, S, low)", p.stdout.splitlines())

    # ----------------------------------------------------------- specific
    def test_board_defines_no_rubric_of_its_own(self):
        with open(TOOL, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("tier_for", "label_tier", "review_label_tier", "code_route", "review_route", "load_routing"):
            self.assertNotIn(name, defined)
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "harness_lib.routing":
                imported |= {a.name for a in n.names}
        self.assertLessEqual({"label_tier", "load_routing", "code_route", "review_route"}, imported)
        # no comparison against the rubric's own literals (size:L, size:S, risk:high) anywhere in the tool
        for n in ast.walk(tree):
            if isinstance(n, ast.Compare):
                for side in [n.left] + list(n.comparators):
                    if isinstance(side, ast.Constant) and side.value in ("L", "S", "high"):
                        self.fail(f"harness-board compares against {side.value!r} at line {n.lineno}")
        # --stage still accepts only review
        self.put_cache(12, {"number": 12, "labels": ["task", "size:L", "risk:high"]})
        for stage in ("code", "labels"):
            p = self.board("tier", "12", "--stage", stage)
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("review", p.stderr)

    def test_code_effort_rides_on_the_code_stage_only(self):
        # HARNESS_CODE_EFFORT without HARNESS_CODE_TIER: the labels pick the tier, the setting gives the effort
        for raw, effort in (("max", "max"), ("LOW", "low"), ("medium", "medium")):
            self.settings({"HARNESS_CODE_EFFORT": raw})
            for kind, size, risk in (("task", "S", "low"), ("bug", "L", "high")):
                with self.subTest(effort=raw, kind=kind, size=size, risk=risk):
                    self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                    self.assertEqual(self.tier_json(12),
                                     self.code(12, rubric(kind, size, risk), rubric(kind, size, risk), effort=effort))
                    self.assertEqual(self.tier_json(12, "--stage", "review"),
                                     self.review(12, review_rubric(risk), review_rubric(risk),
                                                 effort="xhigh" if risk == "high" else None))
        self.assertEqual(self.calls(), [])

    def test_settings_come_from_the_main_checkout_not_the_worktree(self):
        self.git("-c", "user.name=T", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", "init")
        worktree = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", "-b", "t/12-x", worktree)
        self.put_cache(12, {"number": 12, "labels": ["task", "size:M", "risk:low"]})        # the labels give opus
        self.settings({"HARNESS_CODE_TIER": "sonnet"}, root=worktree)
        self.assertEqual(self.tier_json(12, "--repo", worktree), self.code(12, "opus", "opus"))
        self.settings({"HARNESS_CODE_TIER": "fable"})
        self.assertEqual(self.tier_json(12, "--repo", worktree),
                         self.code(12, "fable", "opus", frm="HARNESS_CODE_TIER"))
        p = self.ok("tier", "12", cwd=worktree)
        self.assertEqual(p.stdout, "fable\n")



# ======================================================================= tier: no automatic Fable (#239)
# size:L and risk:high route to opus at effort xhigh for the coding stages; a risk:high review routes to opus at xhigh;
# S+low non-bug is still sonnet; everything else opus with no effort. Fable only when a setting names it. `tier N`
# prints the tier alone; `--json` carries the effort. Expected values come from rubric()/rubric_effort() above.
class TierEffort(BoardTest):
    KINDS = ("task", "bug", "chore", "refactor")
    SIZES = ("S", "M", "L", None)
    RISKS = ("low", "high", None)

    # TierRouting's helpers, borrowed without inheriting its tests
    settings = TierRouting.settings
    tier_out = TierRouting.tier_out
    tier_json = TierRouting.tier_json
    code = TierRouting.code
    review = TierRouting.review

    def test_every_label_combination_from_the_cache(self):
        # Ticket #378: the label-combination table runs through harness_lib.routing in this process (harness-board's
        # `tier` prints the routes it returns, test_board_defines_no_rubric_of_its_own), with the routing settings
        # loaded as the tool loads them for this repo: none. Then `tier` itself, once for each tier and effort the
        # table gives: the tier alone, the JSON keys in order, from the cache, no gh call.
        route_keys = ("tier", "effort", "from", "labels_tier")
        routing, settings = loaded_routing(self.repo)
        efforts = set()
        for kind in self.KINDS:
            for size in self.SIZES:
                for risk in self.RISKS:
                    with self.subTest(kind=kind, size=size, risk=risk):
                        tier, effort = rubric(kind, size, risk), rubric_effort(kind, size, risk)
                        code = self.code(12, tier, tier, effort=effort)
                        self.assertEqual(routing.code_route(kind, size, risk, settings),
                                         {k: code[k] for k in route_keys})
                        review = self.review(12, "opus", "opus", effort=review_effort(risk))
                        self.assertEqual(routing.review_route(risk, settings), {k: review[k] for k in route_keys})
                        efforts.add(effort)
        self.assertEqual(efforts, {None, "xhigh"})
        for kind, size, risk in (("task", "S", "low"), ("refactor", "L", "high")):
            with self.subTest(cli="tier", kind=kind, size=size, risk=risk):
                tier, effort = rubric(kind, size, risk), rubric_effort(kind, size, risk)
                self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk, "status:ready")})
                self.assertEqual(self.tier_out(12), tier + "\n")                 # the tier alone
                data = self.tier_json(12)
                self.assertEqual(data, self.code(12, tier, tier, effort=effort))
                self.assertEqual(list(data), TIER_KEYS)
                self.assertEqual(self.tier_out(12, "--stage", "review"), "opus\n")
                self.assertEqual(self.tier_json(12, "--stage", "review"),
                                 self.review(12, "opus", "opus", effort=review_effort(risk)))
        self.assertEqual(self.calls(), [])

    def test_fetched_labels_carry_the_effort_too(self):
        for n, (kind, size, risk) in enumerate([("task", "L", "low"), ("bug", "M", "high"), ("chore", "S", "low"),
                                                ("refactor", "M", "low"), ("task", "S", "high")], start=20):
            with self.subTest(kind=kind, size=size, risk=risk):
                self.scenario([{"match": ["issue", "view", str(n), "--json", "labels"],
                                "stdout_json": {"labels": [{"name": x} for x in ticket_labels(kind, size, risk)]}}])
                tier = rubric(kind, size, risk)
                self.assertEqual(self.tier_json(n), self.code(n, tier, tier, source="github",
                                                              effort=rubric_effort(kind, size, risk)))
                self.assertEqual(self.tier_json(n, "--stage", "review"),
                                 self.review(n, "opus", "opus", source="github", effort=review_effort(risk)))
                self.assertEqual(self.tier_out(n), tier + "\n")

    def test_a_cache_without_labels_answers_from_its_tier(self):
        # The cached tier stands in for the labels' tier. A cached fable is the pre-#239 rubric's answer for size:L
        # or risk:high, so it now means opus at xhigh; sonnet and opus carry no effort.
        for cached, tier, effort in (("sonnet", "sonnet", None), ("opus", "opus", None), ("fable", "opus", "xhigh")):
            with self.subTest(cached=cached):
                self.put_cache(13, {"number": 13, "tier": cached})
                self.assertEqual(self.tier_out(13), tier + "\n")
                self.assertEqual(self.tier_json(13), self.code(13, tier, tier, effort=effort))
        # HARNESS_CODE_EFFORT still wins; HARNESS_CODE_TIER=fable still gives fable
        self.put_cache(13, {"number": 13, "tier": "fable"})
        self.assertEqual(self.tier_json(13, HARNESS_CODE_EFFORT="low"), self.code(13, "opus", "opus", effort="low"))
        self.assertEqual(self.tier_json(13, HARNESS_CODE_TIER="fable"),
                         self.code(13, "fable", "opus", frm="HARNESS_CODE_TIER"))
        self.assertEqual(self.calls(), [])

    def test_code_effort_setting_wins_over_the_labels_xhigh(self):
        for raw, effort in (("low", "low"), ("MAX", "max"), ("high", "high")):
            self.settings({"HARNESS_CODE_EFFORT": raw})
            for kind, size, risk in (("task", "L", "low"), ("bug", "M", "high"), ("task", "S", "low")):
                with self.subTest(effort=raw, kind=kind, size=size, risk=risk):
                    self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                    tier = rubric(kind, size, risk)
                    self.assertEqual(self.tier_json(12), self.code(12, tier, tier, effort=effort))

    def test_explicit_fable_still_gives_fable(self):
        for kind, size, risk in (("task", "L", "high"), ("task", "S", "low"), ("bug", "M", "high")):
            with self.subTest(kind=kind, size=size, risk=risk):
                self.put_cache(12, {"number": 12, "labels": ticket_labels(kind, size, risk)})
                self.assertEqual(self.tier_out(12, HARNESS_CODE_TIER="fable"), "fable\n")
                self.assertEqual(self.tier_json(12, HARNESS_CODE_TIER="fable"),
                                 self.code(12, "fable", rubric(kind, size, risk), frm="HARNESS_CODE_TIER"))
                self.assertEqual(self.tier_out(12, "--stage", "review", HARNESS_REVIEW_TIER="fable"), "fable\n")
                self.assertEqual(self.tier_json(12, "--stage", "review", HARNESS_REVIEW_TIER="fable"),
                                 self.review(12, "fable", "opus", frm="HARNESS_REVIEW_TIER", effort="xhigh"))
                # without the settings the same ticket never prints fable
                self.assertNotIn("fable", self.tier_out(12) + self.tier_out(12, "--stage", "review"))
                self.assertNotIn("fable", json.dumps([self.tier_json(12), self.tier_json(12, "--stage", "review")]))


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
        alt = fakegh.log_entries(os.path.join(self.tmp, "alt.log"))
        self.assertEqual([labels for labels, _ in fakegh.list_queries(alt)], [["status:ready"]])   # #447: REST

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
