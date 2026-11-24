"""Ticket #447 (epic #446): the harness's own gh calls stay inside GitHub's GraphQL quota. harness-board and
harness-work read and edit issues through REST (`gh api`, the core bucket) and read an issue once per command.

The contract these tests read from the ticket:
- One ticket's run (dispatch, start, three stages, tier, the status changes, a comment, answers, the sweep, close and
  the epic check) logs no `gh issue ...` call from either tool, and at most 30% of the GraphQL calls the same run made
  before #447 (MAIN_GRAPHQL, counted on main at 8cbcb78 over this scenario: 70% fewer).
- One command that needs an issue twice (park, promote of a task or an epic) reads it once.

The fake gh is fakegh.py's REST layer (issues, pull requests) behind work_support's wrappers; the call log
is HOME's ~/.claude/harness/gh/calls-<date>.jsonl (harness_lib/ghlimit.py). Expected values are written out here.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_work_rest.py -v
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import unittest

from work_support import *  # noqa: F401,F403

BOARD = os.path.join(os.path.dirname(TOOL), "harness-board")
# The GraphQL calls the run below made on main before #447 (8cbcb78, this scenario, the call log's kind): every
# issue read, edit, comment, close and list, and every `pr list --head`. The run must make at most 30% of them.
MAIN_GRAPHQL = 19
EPIC = 3
URL = "https://github.com/o/r/issues/12"


def epic(subs):
    return dict(issue(EPIC, labels=("epic", "status:ready"), title="The board"), sub_issues=list(subs))


class RestCase(WorkTest):
    def setUp(self):
        super().setUp()
        self.board_ran = False

    def board(self, *args, env=None, check=True):
        """harness-board in the project: a real process the first time in a test, then in this process."""
        args = [str(a) for a in args]
        if self.board_ran:
            p = gitrepo_support.run_in_process(BOARD, args, cwd=self.repo, env=env or self.env())
        else:
            self.board_ran = True
            p = subprocess.run([sys.executable, BOARD] + args, cwd=self.repo, env=env or self.env(),
                               capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        if check:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def call_log(self):
        """Every line of the gh call log in HOME, oldest first."""
        out = []
        for path in sorted(glob.glob(os.path.join(self.home, ".claude", "harness", "gh", "calls-*.jsonl"))):
            with open(path, encoding="utf-8") as fh:
                out += [json.loads(line) for line in fh if line.strip()]
        return out

    def world(self, *issues, ready=(), prs=()):
        """The fake GitHub: these issues (and their sub-issues), the ready list, pull requests."""
        self.scenario(issues=list(issues), ready=list(ready), prs=list(prs))

    def since(self, start):
        """The gh calls after the first `start` log entries, REST writes as gh commands (fakegh.gh_view)."""
        return fakegh.gh_view(self.gh_entries()[start:], self.scenario_path)


# ======================================================================= one ticket's run
class OneRun(RestCase):
    def test_a_tickets_run_reads_and_edits_its_issue_through_rest(self):
        task = issue(labels=("task", "status:ready", "size:S", "risk:low"))
        task["parent"] = {"number": EPIC, "title": "The board"}
        subs = [{"number": 12, "title": TITLE, "state": "open", "labels": task["labels"]},
                {"number": 13, "title": "Done", "state": "closed", "labels": [{"name": "task"}]}]
        self.world(task, epic(subs), ready=[ready_row(12)])
        plan = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in plan["plan"]], [12])
        started = self.ok_json("start", "12", "--json")
        self.assertEqual((started["previous_status"], started["branch"]), ("ready", BRANCH))
        self.assertIn(["issue", "edit", "12", "--add-label", "status:in-progress", "--remove-label", "status:ready"],
                      self.gh_calls())

        # GitHub now says in progress, with the branch's ready pull request open
        task = dict(task, labels=[{"name": n} for n in IN_PROGRESS])
        pr = {"number": 34, "state": "OPEN", "isDraft": False, "headRefName": BRANCH,
              "url": "https://github.com/o/r/pull/34"}
        self.world(task, epic(subs), prs=[pr])
        before = len(self.call_log())
        for stage, result, nxt in (("tests", "pass", "code"), ("code", "fail", "code"), ("code", "pass", "review")):
            self.assertEqual(self.ok_json("stage", "12", stage, result, "--json")["next"], nxt)
        self.assertEqual(json.loads(self.board("tier", "12", "--json").stdout)["source"], "cache")
        self.assertEqual(json.loads(self.board("tier", "12", "--stage", "review", "--json").stdout)["source"],
                         "cache")
        self.assertEqual(self.call_log()[before:], [], "stage and tier read the ticket cache, not GitHub")

        record = self.write_run(12, pid=self.dead_pid(), branch=BRANCH, worktree=started["worktree"])
        write(record["log"], json.dumps(ROUND2_RESULT) + "\n")
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual(row["outcome"], "ok", row)        # a ready pull request: the run did not end early
        self.board("status", "12", "in-review", "--comment", "PR #34 ready for review: https://github.com/o/r/pull/34")
        self.assertEqual(self.read_json(self.cache_path())["status"], "in-review", "the status change wrote the cache")
        self.board("comment", "12", "--body", "Reviewed.")
        self.assertEqual(json.loads(self.board("answers", "12").stdout), [])

        self.world(dict(task, labels=[{"name": "task"}, {"name": "status:in-review"}]), epic(subs))
        self.board("close", "12")
        self.board("epic-check", EPIC)

        log = self.call_log()
        for tool in ("harness-work", "harness-board"):
            with self.subTest(tool=tool):
                lines = [x for x in log if x["tool"] == tool]
                self.assertTrue(lines, f"{tool} made no gh call: {log}")
                self.assertEqual([x["cmd"] for x in lines if x["cmd"].split(" ")[0] == "issue"], [],
                                 f"{tool} still runs gh issue commands")
                self.assertEqual([x for x in lines if x["cmd"] == "pr list"], [], f"{tool}: pr list --head is REST")
        graphql = [x["cmd"] for x in log if x["kind"] == "graphql"]
        self.assertLessEqual(len(graphql), int(MAIN_GRAPHQL * 0.3), graphql)


# ======================================================================= one read per command
class ReadOnce(RestCase):
    def test_a_command_that_needs_its_issue_twice_reads_it_once(self):
        rows = [  # (argv, the issues, {number: labels added} the command makes, the issues read once)
            (["park", "12", "--reason", "The base branch is gone", "--follow-up", "30"],
             [dict(issue(labels=IN_PROGRESS), parent={"number": EPIC, "title": "The board"})],
             {12: ["status:blocked"]}, (12,)),
            (["promote", "41"], [issue(41, labels=("bug", "status:proposed", "size:M"), title="Fix it")],
             {41: ["status:ready"]}, (41,)),
            (["promote", "7"],
             [dict(issue(7, labels=("epic", "status:proposed"), title="Plan"),
                   sub_issues=[{"number": 8, "title": "One", "state": "open",
                                "labels": [{"name": "task"}, {"name": "status:proposed"}]},
                               {"number": 9, "title": "Two", "state": "open",
                                "labels": [{"name": "task"}, {"name": "status:ready"}]}]),
              issue(8, labels=("task", "status:proposed"), title="One"),
              issue(9, labels=("task", "status:ready"), title="Two")],
             {8: ["status:ready"], 7: ["status:ready"]}, (7, 8)),
        ]
        for argv, issues, added, once in rows:
            with self.subTest(command=" ".join(argv[:2])):
                self.world(*issues)
                start = len(self.gh_entries())
                self.board(*argv)
                calls = self.since(start)
                edits = {int(a[2]): [a[i + 1] for i, t in enumerate(a) if t == "--add-label"]
                         for a in calls if a[:2] == ["issue", "edit"]}
                self.assertEqual(edits, added, calls)
                for n in once:
                    self.assertEqual(fakegh.issue_gets(self.gh_entries()[start:], n), 1,
                                     f"#{n} read more than once by one command: {calls}")


if __name__ == "__main__":
    unittest.main()
