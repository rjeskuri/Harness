"""Tests for #72, ops tickets in harness-work: `start` on an ops ticket makes no branch and no worktree and hands the
run a before-values folder outside every repository; dispatch never launches an ops ticket headless (it needs this
Mac's connectors) and launches it bg in the main checkout by default.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_ops.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403

OPS_LABELS = ("ops", "status:ready", "size:S", "risk:low", "needs-local")
OPS_BODY = """## Request
Fill in the blank region on the imported contacts.

## Change
- Contact.Region__c on 14 records: blank -> "North"

## Source of truth
SELECT Id FROM Contact WHERE Region__c = null

## Apply
The Salesforce connector.

## Verify
Run the query again: 0 rows.

## Rollback
Set Region__c back to blank from before.json.

## Context
- none
"""


def ops_issue(number, title="Fill in the blank regions"):
    return dict(issue(number, labels=OPS_LABELS, title=title), body=OPS_BODY)


class OpsTest(WorkTest):
    def before_dir(self, n):
        return os.path.join(self.home, ".claude", "harness", "ops", self.key, str(n))

    def assertNoBranchOrWorktree(self):
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.git(self.repo, "branch", "--list", "t/*"), "")
        heads = self.git(self.repo, "ls-remote", "--heads", "origin")
        self.assertEqual([line.split()[-1] for line in heads.splitlines()], ["refs/heads/main"])

    def ops_lines(self, text, n):
        """The output lines that name #n and say it is an ops ticket."""
        return [ln for ln in text.splitlines() if f"#{n}" in ln and "ops" in ln.lower()]


# =========================================================================== start
class OpsStart(OpsTest):
    def test_start_of_an_ops_ticket_makes_no_worktree_and_creates_the_before_dir(self):
        for n in (12, 31):
            with self.subTest(ticket=n):
                self.scenario(issues=[ops_issue(n)])
                data = self.ok_json("start", str(n), "--json")
                self.assertEqual(data["number"], n)
                self.assertIs(data.get("ops"), True, sorted(data))
                self.assertIsNone(data["branch"])
                self.assertIsNone(data["worktree"])
                self.assertTrue(data["context"].startswith(f"# #{n} "), data["context"][:80])
                before = data["before_dir"]
                self.assertEqual(os.path.realpath(before), os.path.realpath(self.before_dir(n)))
                self.assertTrue(os.path.isdir(before), before)
                self.assertFalse(os.path.realpath(before).startswith(os.path.realpath(self.repo) + os.sep))
                self.assertNoBranchOrWorktree()
                edits = [c for c in self.gh_calls() if c[:3] == ["issue", "edit", str(n)]]
                self.assertTrue(any("status:in-progress" in c for c in edits), edits)


# =========================================================================== dispatch
class OpsDispatch(OpsTest):
    BOARD = dict(issues=[ops_issue(12), issue(13, title="Second ticket")],
                 ready=[ready_row(12, "Fill in the blank regions", labels=OPS_LABELS), ready_row(13, "Second ticket")])

    def test_headless_dry_run_leaves_the_ops_ticket_out_and_says_why_bg_plans_it(self):
        self.scenario(**self.BOARD)
        for extra in (("--venue", "headless"), ("--api",)):
            with self.subTest(venue=extra):
                data = self.ok_json("dispatch", "--dry-run", "--json", *extra)
                self.assertEqual(data["venue"], "headless")
                self.assertEqual([row["number"] for row in data["plan"]], [13])
                [skip] = [s for s in data["skipped"] if s["number"] == 12]
                self.assertIn("ops", skip["why"].lower())
                p = self.ok("dispatch", "--dry-run", *extra)
                self.assertTrue(self.ops_lines(p.stdout, 12), p.stdout)
                self.assertFalse([ln for ln in p.stdout.splitlines() if ln.startswith("would start #12")], p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")                 # the default venue plans it
        self.assertEqual(data["venue"], "bg")
        self.assertEqual([row["number"] for row in data["plan"]], [12, 13])
        self.assertFalse([s for s in data["skipped"] if s["number"] == 12])
        self.assertEqual(self.claude_calls(), [])

    def test_headless_dispatch_launches_the_task_and_never_the_ops_ticket(self):
        self.scenario(**self.BOARD)
        p = self.work("dispatch", "--venue", "headless")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) >= 1))
        for record in self.run_records():
            if isinstance(record.get("pid"), int):
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        launched = [call["argv"] for call in self.claude_calls()]
        self.assertEqual(len(launched), 1, launched)
        self.assertIn("/harness:work 13", launched[0])
        self.assertEqual([r["ticket"] for r in self.run_records()], [13])
        self.assertTrue(self.ops_lines(p.stdout + p.stderr, 12), p.stdout + p.stderr)
        self.assertFalse([c for c in self.gh_calls() if c[:3] in (["issue", "edit", "12"], ["issue", "comment", "12"])])

    def test_default_dispatch_launches_the_ops_ticket_bg_in_the_main_checkout(self):
        self.scenario(issues=[ops_issue(12)], ready=[ready_row(12, "Fill in the blank regions", labels=OPS_LABELS)])
        self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="bg12ops"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"][0], "--bg")
        self.assertEqual(call["argv"][-1], "/harness:work 12")
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo))
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["venue"], record["bg_id"]), (12, "bg", "bg12ops"))
        self.assertNoBranchOrWorktree()


if __name__ == "__main__":
    unittest.main()
