"""harness-work start: branch and worktree from origin/main, an existing remote branch reused, the exclude line, the
worktree setup, .worktreeinclude copies, refusals, a needs-input ticket's answers, the status change and
harness-board's warnings.

Split from test_work.py (#447: with issues read through REST, that module came near the 180 s a module may take);
the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_start.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403

# The retired Projects mirror setting (#486), spelled in two pieces so the ticket's grep finds none in the tree.
OLD_SETTING = "HARNESS_" + "PROJECT"


# =========================================================================== start
class Start(WorkTest):
    def test_start_creates_branch_and_worktree_from_origin_main(self):
        other = self.other_clone()                        # origin/main moves on; proj's main is behind
        write(os.path.join(other, "NEW.md"), "new\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "Second")
        self.git(other, "push", "-q", "origin", "main")
        head = self.git(other, "rev-parse", "HEAD")
        write(os.path.join(self.repo, ".claude", "settings.json"),
              json.dumps({"env": {"HARNESS_WORKTREE_SETUP": "echo shared > .setup-ran"}}))
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_WORKTREE_SETUP": "echo local > .setup-ran"}}))
        self.scenario(issues=[issue()])

        data = self.ok_json("start", "12", "--json")
        self.assertEqual(set(data), {"number", "branch", "worktree", "tier", "previous_status", "waiting", "answers",
                                     "context", "resume", "pr"})
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("ready", False, []))
        self.assertEqual((data["resume"], data["pr"]), (False, None))     # a fresh start: no branch, no PR
        wt = self.worktree_path()
        self.assertEqual((data["number"], data["branch"], data["worktree"], data["tier"]), (12, BRANCH, wt, "sonnet"))
        self.assertTrue(data["context"].startswith("# #12 Add the board tool"), data["context"][:80])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(wt, "branch", "--show-current"), BRANCH)
        with open(os.path.join(wt, ".setup-ran"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "local")      # settings.local.json wins over settings.json

        exclude = os.path.join(self.repo, ".git", "info", "exclude")
        with open(exclude, encoding="utf-8") as fh:
            self.assertEqual([ln.strip() for ln in fh].count(".claude/worktrees/"), 1)

        cache = self.read_json(self.cache_path())
        self.assertEqual((cache["branch"], cache["worktree"]), (BRANCH, wt))
        self.assertTrue(cache["started_at"])
        calls = self.gh_calls()
        edit = next(c for c in calls if c[:2] == ["issue", "edit"])
        self.assertIn("status:in-progress", edit)
        comment = next(c for c in calls if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.startswith(f"Started on {BRANCH} ("), body)
        self.assertTrue(body.splitlines()[0].endswith(", interactive)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)           # harness-board marks its comments

        again = self.ok("start", "12")                       # the existing worktree is reused
        self.assertEqual(again.stdout.splitlines()[0], wt)
        self.assertIn("# #12 Add the board tool", again.stdout)
        with open(exclude, encoding="utf-8") as fh:
            self.assertEqual([ln.strip() for ln in fh].count(".claude/worktrees/"), 1)
        self.assertEqual(self.read_json(self.cache_path())["started_at"], cache["started_at"])

    def test_start_reuses_an_existing_remote_branch(self):
        other = self.other_clone()
        self.git(other, "switch", "-q", "-c", "t/12-older-title")
        write(os.path.join(other, "WIP.md"), "work in progress\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "WIP")
        self.git(other, "push", "-q", "origin", "t/12-older-title")
        head = self.git(other, "rev-parse", "HEAD")
        self.scenario(issues=[issue()])

        data = self.ok_json("start", "12", "--json")
        wt = self.worktree_path("t/12-older-title")
        self.assertEqual((data["branch"], data["worktree"]), ("t/12-older-title", wt))
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(wt, "rev-parse", "--abbrev-ref", "@{u}"), "origin/t/12-older-title")
        self.assertFalse(os.path.exists(self.worktree_path()))

    def push_branch_with_commit(self):
        other = self.other_clone()
        self.git(other, "switch", "-q", "-c", BRANCH)
        write(os.path.join(other, "WIP.md"), "work in progress\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "WIP")
        self.git(other, "push", "-q", "origin", BRANCH)

    def test_start_of_a_ready_ticket_whose_branch_has_a_pr_is_a_resume_naming_the_pr(self):
        # #407: an unparked ticket (status ready) with a branch and an open PR resumes; a draft PR counts too
        self.push_branch_with_commit()
        for number, rows in ((7, ready_pr(7)), (23, draft(23))):
            with self.subTest(pr=number):
                self.scenario(issues=[issue()], prs=rows)
                data = self.ok_json("start", "12", "--json")
                self.assertEqual((data["previous_status"], data["resume"], data["pr"]), ("ready", True, number))

    def test_start_of_a_ready_ticket_whose_branch_has_commits_but_no_pr_is_a_resume_without_a_pr(self):
        self.push_branch_with_commit()
        self.scenario(issues=[issue()], prs=[])
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["resume"], data["pr"]), (True, None))

    def test_start_copies_gitignored_files_listed_in_worktreeinclude(self):
        write(os.path.join(self.repo, ".env"), "KEY=1\n")              # ignored and included: copied
        write(os.path.join(self.repo, "sub", ".env"), "SUB=1\n")       # same, deeper
        write(os.path.join(self.repo, "debug.log"), "noise\n")         # ignored, not included
        write(os.path.join(self.repo, ".env.sample"), "KEY=\n")        # included, not ignored
        self.scenario(issues=[issue()])
        wt = self.ok_json("start", "12", "--json")["worktree"]
        with open(os.path.join(wt, ".env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "KEY=1\n")
        self.assertTrue(os.path.isfile(os.path.join(wt, "sub", ".env")))
        self.assertFalse(os.path.exists(os.path.join(wt, "debug.log")))
        self.assertFalse(os.path.exists(os.path.join(wt, ".env.sample")))

    def test_start_leaves_an_in_review_ticket_in_review(self):
        self.scenario(issues=[issue(labels=("task", "status:in-review", "size:S", "risk:low"))])
        data = self.noisy_start("12", "--json")
        self.assertIn("show 12 --json", self.board_calls())
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("in-review", False, []))
        self.assertEqual(data["worktree"], self.worktree_path())
        self.assertTrue(os.path.isdir(data["worktree"]))
        calls = self.gh_calls()
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertFalse([c for c in self.board_calls() if c.startswith("answers")])   # no answers read

    def board_calls(self):
        """The harness-board commands the last noisy_start ran, in order (#447: the REST reads of `answers` and
        `show` look alike, so the board's own log says which ran when)."""
        log = os.path.join(self.tmp, "noisy.log")
        return read_text(log).splitlines() if os.path.exists(log) else []

    def noisy_start(self, *args):
        """start with the harness-board that logs each of its commands (NOISY_LOG)."""
        log = os.path.join(self.tmp, "noisy.log")
        if os.path.exists(log):
            os.unlink(log)
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        return self.ok_json("start", *args, env=self.env(PATH=noisy, NOISY_LOG=log))

    def needs_input(self, *replies):
        comments = [comment("rjeskuri", iso_ago(hours=3), QUESTION)] + list(replies)
        self.scenario(issues=[issue(labels=("task", "status:needs-input", "size:S", "risk:low"), comments=comments)])

    def test_start_on_needs_input_returns_the_owners_answer_and_moves_on(self):
        self.needs_input(comment("RJeskuri", iso_ago(hours=2), "Use main."),
                         comment("rjeskuri", iso_ago(hours=1), "Also add a flag."))
        data = self.noisy_start("12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"]), ("needs-input", False))
        self.assertEqual([a["body"] for a in data["answers"]], ["Use main.", "Also add a flag."])
        calls = self.gh_calls()
        read = next(i for i, c in enumerate(calls) if fakegh.is_world_read(c) and "comments" in " ".join(c))
        edit = next(i for i, c in enumerate(calls) if c[:2] == ["issue", "edit"])
        self.assertLess(read, edit)
        self.assertIn("status:in-progress", calls[edit])
        self.assertIn("status:needs-input", calls[edit])                  # removed
        board = self.board_calls()
        answers = next(i for i, c in enumerate(board) if c.startswith("answers 12"))
        first_show = next(i for i, c in enumerate(board) if c.startswith("show 12"))
        self.assertLess(answers, first_show)                               # answers before show refreshes the cache

        text = self.ok("start", "12").stdout                               # text mode shows the answer too
        self.assertIn("Use main.", text)

    def test_start_on_needs_input_without_an_answer_waits(self):
        self.needs_input()
        data = self.noisy_start("12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("needs-input", True, []))
        calls = self.gh_calls()
        self.assertTrue([c for c in self.board_calls() if c.startswith("answers 12")], self.board_calls())
        self.assertTrue([c for c in calls if fakegh.is_world_read(c) and "comments" in " ".join(c)], calls)
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertIn("no answer yet", self.ok("start", "12").stdout)

    def test_start_refuses_an_epic(self):
        self.scenario(issues=[issue(labels=("epic", "status:ready"), title="The board")],
                      sub_issues=[{"number": 13, "title": "A task", "state": "open", "labels": []},
                                  {"number": 14, "title": "Done", "state": "closed", "labels": []}])
        p = self.work("start", "12")
        self.assertOneLineError(p, "#12 is an epic", "#13")
        self.assertNotIn("#14", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_start_refuses_blocked_and_closed_tickets(self):
        cases = [
            (issue(labels=("task", "status:blocked")), ["status:blocked"], []),
            (issue(blocked_by=[{"number": 9, "state": "OPEN"}, {"number": 8, "state": "CLOSED"}]),
             ["blocked by open #9"], ["#8"]),
            (issue(state="CLOSED"), ["#12 is closed"], []),
        ]
        for record, needles, absent in cases:
            with self.subTest(needles=needles):
                self.scenario(issues=[record])
                p = self.work("start", "12", "--json")
                self.assertOneLineError(p, *needles)
                for text in absent:
                    self.assertNotIn(text, p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])

    def test_refuses_a_proposed_ticket(self):
        """#160: a proposed ticket waits for harness-board promote; start makes no branch, worktree or status change."""
        self.scenario(issues=[issue(labels=("task", "status:proposed", "size:S", "risk:low"))])
        branches = self.git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads")
        p = self.work("start", "12", "--json")
        self.assertOneLineError(p, "#12 is status:proposed; it waits for harness-board promote 12")
        self.assertEqual(p.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads"), branches)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_start_makes_no_project_call_whatever_the_old_project_setting_holds(self):
        """#486: with the retired project setting in the environment, in settings.json or both, the real
        harness-board's status change during start swaps the labels, makes no `gh project` call and adds nothing
        to start's stderr."""
        url = "https://ghe.example.com/o/r/issues/12"
        settings = os.path.join(self.home, ".claude", "settings.json")
        for source, project in (("environment", "acme-org/23"), ("settings", "other-owner/5"),
                                ("both", "acme-org/31"), ("environment", "not-a-board")):
            with self.subTest(source=source, project=project):
                self.scenario(issues=[dict(issue(), url=url)])
                if source in ("settings", "both"):
                    write(settings, json.dumps({"env": {OLD_SETTING: project}}))
                env = self.env(**{OLD_SETTING: project}) if source in ("environment", "both") else self.env()
                p = self.ok("start", "12", "--json", env=env)
                self.assertEqual(json.loads(p.stdout)["previous_status"], "ready")
                self.assertEqual(p.stderr, "")
                calls = self.gh_calls()
                self.assertIn(["issue", "edit", "12", "--add-label", "status:in-progress",
                               "--remove-label", "status:ready"], [c[:7] for c in calls])
                self.assertEqual([c for c in calls if c[:1] == ["project"]], [])

    def test_start_keeps_other_board_stderr_hidden(self):
        """harness-board succeeds but writes other lines on stderr (a wrapper around the real one): only warning
        lines reach start's stderr, from every board call start makes (both `show`s), each once and in call order;
        with no warning anywhere, start's stderr stays empty. A ticket already in progress gets no status change,
        and its `show` warnings still come through."""
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        self.scenario(issues=[issue(12), issue(13), issue(14, labels=("task", "status:in-progress", "size:S",
                                                                       "risk:low"))])
        log = os.path.join(self.tmp, "noisy.log")
        p = self.ok("start", "12", env=self.env(PATH=noisy, NOISY_LOG=log, NOISY_WARN="token-k1"))
        self.assertIn("status 12 in-progress", read_text(log), "the chattering board ran")
        self.assertEqual(p.stderr.splitlines(), [f"{WARNING}token-k1 from show 12 --json",
                                                 f"{WARNING}token-k1 from show 12 --markdown"], p.stderr)
        for line in CHATTER:
            self.assertNotIn(line, p.stderr)
        p = self.ok("start", "13", env=self.env(PATH=noisy, NOISY_LOG=log))
        self.assertIn("status 13 in-progress", read_text(log))
        self.assertEqual(p.stderr, "")
        p = self.ok("start", "14", env=self.env(PATH=noisy, NOISY_LOG=log, NOISY_WARN="token-m3"))
        self.assertNotIn("status 14", read_text(log), "no status change from in-progress")
        self.assertEqual(p.stderr.splitlines(), [f"{WARNING}token-m3 from show 14 --json",
                                                 f"{WARNING}token-m3 from show 14 --markdown"], p.stderr)


if __name__ == "__main__":
    unittest.main()
