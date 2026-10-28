"""Tests for harness-work: start (branch and worktree from origin/main, an existing remote branch reused, the
exclude line, the worktree setup, .worktreeinclude copies, refusals), attempt's retry, escalate and
needs-input sequence, dispatch (dry-run plan, bg on the subscription by default whatever the api budget says,
--venue headless billed through apiKeyHelper and refused while the api budget is exhausted, re-dispatch of a run
that ended without a PR), runs --sweep's outcome
classification and the budget it marks, the sweep stopping the bg session of a ticket that is in review, needs input or
closed (#123), budget status, exhaust and reset, worktrees and worktree remove.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME, a bare `origin` and a clone of it (`proj`, the main checkout) made with
real git. A `fakebin` folder holds `gh` (a wrapper around fakegh.py) and `claude` (a fake that logs its
argv, cwd and environment, answers `agents --json` from $FAKECLAUDE_AGENTS, prints `backgrounded · abc123`
for --bg, answers `stop <id>` with exit 0, or exit 1 for an id listed in $FAKECLAUDE_STOP_FAIL (comma-separated),
and for -p sleeps briefly, then prints a canned JSON result; for --cloud it refuses unless its stdout is a terminal,
logs the origin's heads and prints the create-and-exit lines with \\r\\n ends and ANSI escapes, or the failure
$FAKECLAUDE_CLOUD names), and terminal-notifier and osascript that do nothing. harness-board is the real one,
found next to harness-work. The fake executables are written once for the module: macOS checks a new
executable on its first run, which costs about half a second each time.
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


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
                                     "context"})
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("ready", False, []))
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
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("in-review", False, []))
        self.assertEqual(data["worktree"], self.worktree_path())
        self.assertTrue(os.path.isdir(data["worktree"]))
        calls = self.gh_calls()
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertFalse([c for c in calls if c[-2:] == ["--json", "comments"]])   # no answers read

    def needs_input(self, *replies):
        comments = [comment("rjeskuri", iso_ago(hours=3), QUESTION)] + list(replies)
        self.scenario(issues=[issue(labels=("task", "status:needs-input", "size:S", "risk:low"), comments=comments)])

    def test_start_on_needs_input_returns_the_owners_answer_and_moves_on(self):
        self.needs_input(comment("RJeskuri", iso_ago(hours=2), "Use main."),
                         comment("rjeskuri", iso_ago(hours=1), "Also add a flag."))
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"]), ("needs-input", False))
        self.assertEqual([a["body"] for a in data["answers"]], ["Use main.", "Also add a flag."])
        calls = self.gh_calls()
        read = calls.index(["issue", "view", "12", "--json", "comments"])
        edit = next(i for i, c in enumerate(calls) if c[:2] == ["issue", "edit"])
        self.assertLess(read, edit)
        self.assertIn("status:in-progress", calls[edit])
        self.assertIn("status:needs-input", calls[edit])                  # removed
        first_show = next(i for i, c in enumerate(calls) if c[:3] == ["issue", "view", "12"]
                          and c[-2:] not in (["--json", "labels"], ["--json", "comments"]))
        self.assertLess(read, first_show)                                  # answers before show refreshes the cache

        text = self.ok("start", "12").stdout                               # text mode shows the answer too
        self.assertIn("Use main.", text)

    def test_start_on_needs_input_without_an_answer_waits(self):
        self.needs_input()
        data = self.ok_json("start", "12", "--json")
        self.assertEqual((data["previous_status"], data["waiting"], data["answers"]), ("needs-input", True, []))
        calls = self.gh_calls()
        self.assertIn(["issue", "view", "12", "--json", "comments"], calls)
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

    def test_start_mirrors_in_progress_to_the_configured_project(self):
        """#64: with HARNESS_PROJECT set, the real harness-board's status change during start sets the project
        item's Status to "In progress", on the URL gh reports for the issue (not one built from the remote)."""
        url = "https://ghe.example.com/o/r/issues/12"
        self.scenario(issues=[dict(issue(), url=url)])
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, {"match": ["project"], "stdout": ""})
        write(self.scenario_path, json.dumps(data))
        p = self.ok("start", "12", "--json", env=self.env(HARNESS_PROJECT="acme-org/23"))
        self.assertEqual(json.loads(p.stdout)["previous_status"], "ready")
        projects = [c for c in self.gh_calls() if c[:1] == ["project"]]
        self.assertEqual(len(projects), 1, projects)
        argv = projects[0]
        self.assertEqual(project_argv(argv), ("item-edit", ["23"], {"owner": "acme-org", "url": url, "field": "Status",
                                                                     "value": "In progress"}))
        edit = next(c for c in self.gh_calls() if c[:2] == ["issue", "edit"])
        self.assertLess(self.gh_calls().index(edit), self.gh_calls().index(argv), "the label changes first")

    # ------------------------------------------------- #69: harness-board's Projects warning reaches the user
    def project_scenario(self, project_call, numbers=(12,)):
        """The issues, with `project_call` (the fake gh's answer to any `gh project ...`) ahead of the rest."""
        self.scenario(issues=[issue(n) for n in numbers])
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, dict(project_call, match=["project"]))
        write(self.scenario_path, json.dumps(data))

    def test_start_passes_the_board_warning_through_when_the_project_item_edit_fails(self):
        """A failing `gh project item-edit` during start: start still exits 0, and harness-board's one warning line,
        naming that board and gh's error, is on start's stderr exactly once (not on stdout). The board comes from
        the environment in one case and from ~/.claude/settings.json in the other; text and --json output alike."""
        cases = [(12, "acme-org/23", "HTTP 403: missing the project scope (token-a1)", "env", []),
                 (13, "other-owner/5", "item not found on the board (token-b2)", "settings", ["--json"])]
        for number, project, why, source, flags in cases:
            with self.subTest(number=number, source=source):
                self.project_scenario({"stderr": why + "\n", "exit": 1}, numbers=(12, 13))
                if source == "settings":
                    write(os.path.join(self.home, ".claude", "settings.json"),
                          json.dumps({"env": {"HARNESS_PROJECT": project}}))
                    env = self.env()
                else:
                    env = self.env(HARNESS_PROJECT=project)
                p = self.ok("start", str(number), *flags, env=env)
                owner, board_number = project.split("/")
                expected = (f"{WARNING}the Projects board {owner}/{board_number} was not updated: "
                            f"gh project item-edit {board_number} failed: {why}")
                self.assertEqual(p.stderr.splitlines(), [expected], p.stderr)
                self.assertNotIn(WARNING, p.stdout)
                if flags:
                    self.assertEqual(json.loads(p.stdout)["previous_status"], "ready")
                else:
                    self.assertTrue(p.stdout.startswith(self.worktree_path(f"t/{number}-add-the-board-tool")),
                                    p.stdout)
                self.assertIn(["issue", "edit", str(number), "--add-label", "status:in-progress",
                               "--remove-label", "status:ready"],
                              [c[:7] for c in self.gh_calls()], "the labels still changed")

    def test_start_keeps_other_board_stderr_hidden(self):
        """harness-board succeeds but writes other lines on stderr (a wrapper around the real one): only warning
        lines reach start's stderr, from every board call start makes (both `show`s as well as `status`), each
        once and in call order; with no warning anywhere, start's stderr stays empty as today. A ticket already in
        progress gets no status change, and its `show` warnings still come through."""
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        why = "HTTP 401: token expired (token-c3)"
        self.project_scenario({"stderr": why + "\n", "exit": 1}, numbers=(12, 13))
        log = os.path.join(self.tmp, "noisy.log")
        p = self.ok("start", "12", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log,
                                                NOISY_WARN="token-k1"))
        self.assertIn("status 12 in-progress", read_text(log), "the chattering board ran")
        self.assertEqual(p.stderr.splitlines(), [
            f"{WARNING}token-k1 from show 12 --json",
            f"{WARNING}the Projects board acme-org/31 was not updated: gh project item-edit 31 failed: {why}",
            f"{WARNING}token-k1 from show 12 --markdown"], p.stderr)
        for line in CHATTER:
            self.assertNotIn(line, p.stderr)
        self.project_scenario({"stdout": ""}, numbers=(12, 13))
        p = self.ok("start", "13", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log))
        self.assertIn("status 13 in-progress", read_text(log))
        self.assertEqual(p.stderr, "")
        self.assertTrue([c for c in self.gh_calls() if c[:2] == ["project", "item-edit"]], "the mirror ran")

        self.project_scenario({"stderr": "should never run\n", "exit": 1}, numbers=())
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, {"match": ["issue", "view", "14"], "stdout_json": issue(
            14, labels=("task", "status:in-progress", "size:S", "risk:low"))})
        write(self.scenario_path, json.dumps(data))
        p = self.ok("start", "14", env=self.env(PATH=noisy, HARNESS_PROJECT="acme-org/31", NOISY_LOG=log,
                                                NOISY_WARN="token-m3"))
        self.assertNotIn("status 14", read_text(log), "no status change from in-progress")
        self.assertEqual(p.stderr.splitlines(), [f"{WARNING}token-m3 from show 14 --json",
                                                 f"{WARNING}token-m3 from show 14 --markdown"], p.stderr)

    def test_start_stderr_is_unchanged_without_the_project_setting(self):
        """No HARNESS_PROJECT anywhere: no project call, and start's stderr is empty as before #69, with the plain
        board and with one that chatters on stderr."""
        noisy = os.pathsep.join([BINS["noisybin"], BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"])
        self.project_scenario({"stderr": "should never run\n", "exit": 1}, numbers=(12, 13))
        log = os.path.join(self.tmp, "noisy.log")
        for number, env in ((12, self.env()), (13, self.env(PATH=noisy, NOISY_LOG=log))):
            with self.subTest(number=number):
                p = self.ok("start", str(number), env=env)
                self.assertEqual(p.stderr, "")
        self.assertIn("status 13 in-progress", read_text(log), "the chattering board ran")
        self.assertEqual([c for c in self.gh_calls() if c[:1] == ["project"]], [])


# =========================================================================== attempt
class Attempt(WorkTest):
    def test_retry_escalate_needs_input_and_tier_progression(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["code", "fail"], "retry"),
            (["code", "fail"], "escalate opus"),
            (["code", "fail"], "needs-input"),
            (["code", "fail"], "needs-input"),
            (["tests", "fail"], "retry"),                    # each stage counts its own failures
            (["code", "pass"], "done"),
            (["code", "fail"], "retry"),                     # a pass ends the stage's run of failures
            (["review", "fail", "--tier", "opus"], "retry"),
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),     # #239: opus goes to opus xhigh
            (["ci", "fail", "--tier", "fable"], "retry"),
            (["ci", "fail", "--tier", "fable"], "needs-input"),    # #239: nothing above an explicit Fable
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # #239: effort recorded
        self.assertEqual((attempts[0]["stage"], attempts[0]["tier"], attempts[0]["result"]), ("code", "sonnet", "fail"))
        self.assertEqual(attempts[-1]["tier"], "fable")

        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"]), ("retry", 1, "sonnet"))

    def test_escalation_starts_from_the_overridden_tier(self):
        """#175: HARNESS_CODE_TIER in the main checkout's .claude/settings.json is the tier `start` reports and the
        cache holds, and `attempt` escalates from it, not from the labels' route (opus xhigh here). #239: the override
        replaces the labels' effort too, so opus escalates to opus xhigh and sonnet to opus."""
        hard = ("task", "status:ready", "size:L", "risk:high")
        self.scenario(issues=[issue(labels=hard), issue(number=13, labels=hard)])
        settings = os.path.join(self.repo, ".claude", "settings.json")
        for number, setting, escalated in ((12, "opus", "opus xhigh"), (13, "sonnet", "opus")):
            with self.subTest(setting=setting):
                write(settings, json.dumps({"env": {"HARNESS_CODE_TIER": setting}}))
                data = self.ok_json("start", str(number), "--json")
                self.assertEqual(data["tier"], setting)
                self.assertEqual(self.read_json(self.cache_path(number))["tier"], setting)
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(), "retry")
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(),
                                 f"escalate {escalated}")
                attempts = self.read_json(self.cache_path(number))["attempts"]
                self.assertEqual([(a["stage"], a["tier"], a["result"]) for a in attempts],
                                 [("code", setting, "fail")] * 2)

    def test_attempt_without_a_cache_says_to_start(self):
        self.assertOneLineError(self.work("attempt", "12", "code", "fail"), "harness-work start 12")

    def test_fix_stage_allows_two_rounds_then_needs_input(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["fix", "fail"], "retry"),
            (["fix", "fail"], "needs-input"),                # two rounds at most, never an escalation
            (["fix", "fail"], "needs-input"),
            (["fix", "pass"], "done"),
            (["fix", "fail"], "retry"),                      # a pass ends the run of fix failures
            (["fix", "fail", "--tier", "opus"], "needs-input"),
            (["fix", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "fable"], "retry"),
            (["fix", "fail", "--tier", "fable"], "needs-input"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
        self.assertEqual([a["stage"] for a in attempts], ["fix"] * len(sequence))
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[3]["tier"], attempts[3]["result"]), ("sonnet", "pass"))
        self.assertEqual((attempts[5]["tier"], attempts[5]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-1]["tier"], attempts[-1]["result"]), ("fable", "fail"))

        # --json on a fresh run: the tier never moves and no next tier is named
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        for failures, action in ((1, "retry"), (2, "needs-input"), (3, "needs-input")):
            with self.subTest(json_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--json")
                self.assertEqual((data["stage"], data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 ("fix", action, failures, "sonnet", None))
        data = self.ok_json("attempt", "12", "fix", "pass", "--json")
        self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("done", 0, None))
        for failures, action in ((1, "retry"), (2, "needs-input")):
            with self.subTest(json_opus_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--tier", "opus", "--json")
                self.assertEqual((data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 (action, failures, "opus", None))

    def test_breaker_stage_allows_two_rounds_then_needs_input(self):
        """#252: the breaker follows the fix rule: retry on a run's first failure, needs-input on the second and every
        later one, at any tier; never an escalation. A breaker pass ends the run; other stages neither advance nor
        reset it (the tests stage passes between breaker rounds)."""
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["breaker", "fail"], "retry"),
            (["breaker", "fail"], "needs-input"),            # two rounds at most; sonnet does not escalate to opus
            (["breaker", "fail"], "needs-input"),
            (["breaker", "pass"], "done"),
            (["breaker", "fail"], "retry"),                  # a pass ends the run of breaker failures
            (["tests", "pass"], "done"),                     # the tightened tests do not end the breaker's run
            (["breaker", "fail", "--tier", "opus"], "needs-input"),     # opus does not escalate to opus xhigh
            (["code", "fail"], "retry"),                     # nor does the breaker's run count for code
            (["breaker", "pass", "--tier", "opus"], "done"),
            (["breaker", "fail", "--tier", "opus"], "retry"),
            (["breaker", "fail", "--tier", "opus", "--effort", "high"], "needs-input"),
            (["breaker", "pass", "--tier", "fable"], "done"),
            (["breaker", "fail", "--tier", "fable"], "retry"),
            (["breaker", "fail", "--tier", "fable"], "needs-input"),
            (["breaker", "fail", "--tier", "sonnet"], "needs-input"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # the record is unchanged
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[6]["tier"], attempts[6]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-2]["tier"], attempts[-2]["result"]), ("fable", "fail"))

        # --json on a fresh run, at each tier: the tier never moves and no next tier (or effort) is named
        for tier in ("sonnet", "opus", "fable"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            for failures, action in ((1, "retry"), (2, "needs-input"), (3, "needs-input")):
                with self.subTest(json_tier=tier, json_failures=failures):
                    data = self.ok_json("attempt", "12", "breaker", "fail", "--tier", tier, "--json")
                    self.assertEqual((data["stage"], data["result"], data["action"], data["failures"], data["tier"],
                                      data["next_tier"], data["next_effort"]),
                                     ("breaker", "fail", action, failures, tier, None, None))
            data = self.ok_json("attempt", "12", "breaker", "pass", "--tier", tier, "--json")
            self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("done", 0, None))
            data = self.ok_json("attempt", "12", "breaker", "fail", "--tier", tier, "--json")
            self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("retry", 1, None))
        # without --tier the cached sonnet is the tier, and the second failure still names no next tier
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        self.assertEqual(self.ok_json("attempt", "12", "breaker", "fail", "--json")["next_tier"], None)
        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"], data["next_tier"]),
                         ("needs-input", 2, "sonnet", None))

    def test_ci_and_tests_answer_by_the_default_rule_and_count_apart(self):
        """`ci` and `tests` are answered by the default rule: retry, escalate, needs-input; a pass ends the run.
        Their failures count apart, and neither counts for code."""
        sequence = [
            (["fail"], "retry"),
            (["fail"], "escalate opus"),
            (["fail"], "needs-input"),
            (["fail"], "needs-input"),
            (["pass"], "done"),
            (["fail"], "retry"),
            (["fail", "--tier", "opus"], "escalate opus xhigh"),
            (["pass", "--tier", "opus"], "done"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "retry"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "needs-input"),
            (["pass", "--tier", "fable"], "done"),
            (["fail", "--tier", "fable"], "retry"),
            (["fail", "--tier", "fable"], "needs-input"),
        ]
        answers = {}
        for stage in ("ci", "tests"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            answers[stage] = []
            for argv, expected in sequence:
                with self.subTest(stage=stage, argv=argv):
                    out = self.ok("attempt", "12", stage, *argv).stdout.strip()
                    answers[stage].append(out)
                    self.assertEqual(out, expected)
            attempts = self.read_json(self.cache_path())["attempts"]
            self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(stage, argv[0]) for argv, _ in sequence])
            self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
        self.assertEqual(answers["tests"], answers["ci"])

        # --json: tests' escalation names the next tier as ci's does
        for stage in ("ci", "tests"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            first = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((first["stage"], first["action"], first["failures"], first["next_tier"]),
                             (stage, "retry", 1, None))
            second = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((second["stage"], second["action"], second["failures"], second["tier"],
                              second["next_tier"]), (stage, "escalate", 2, "sonnet", "opus"))

        # interleaved: each stage keeps its own run
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        interleaved = [
            (["tests", "fail"], "retry"),
            (["ci", "fail"], "retry"),                       # tests' failure is not ci's
            (["tests", "fail"], "escalate opus"),           # ci's failure did not reset verify
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["ci", "pass"], "done"),
            (["tests", "fail"], "needs-input"),             # a ci pass does not end tests' run
            (["ci", "fail"], "retry"),
            (["tests", "pass"], "done"),
            (["ci", "fail"], "escalate opus"),               # a tests pass does not end ci's run
            (["tests", "fail"], "retry"),
            (["ci", "fail"], "needs-input"),
            (["tests", "fail", "--tier", "opus"], "escalate opus xhigh"),
        ]
        for argv, expected in interleaved:
            with self.subTest(interleaved=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts],
                         [(argv[0], argv[1]) for argv, _ in interleaved])

    def test_fix_and_review_failures_count_apart(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["review", "fail", "--tier", "opus"], "retry"),
            (["fix", "fail"], "retry"),                      # review's failure is not fix's
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),   # fix's failure did not reset review
            (["fix", "fail"], "needs-input"),                # review's failures did not advance or reset fix
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["review", "fail", "--tier", "fable"], "needs-input"),
            (["fix", "pass"], "done"),
            (["review", "fail", "--tier", "fable"], "needs-input"),     # a fix pass does not end review's run
            (["fix", "fail", "--tier", "opus"], "retry"),
            (["review", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "opus"], "needs-input"),         # a review pass does not end fix's run
            (["review", "fail", "--tier", "opus"], "retry"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])

    def test_an_unknown_stage_is_refused(self):
        attempts = [{"stage": "code", "tier": "sonnet", "result": "fail", "at": "2026-10-01T00:00:00Z"}]
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": attempts}))
        for stage in ("deploy", "fixes", "Fix"):
            with self.subTest(stage=stage):
                p = self.work("attempt", "12", stage, "fail")
                self.assertOneLineError(p, stage, "fix")     # the refusal names the bad stage and the valid ones
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], attempts)


class AttemptEffort(WorkTest):
    """Ticket #239: `harness-work attempt` knows the effort a stage ran at (`--effort`, else the ticket's own route
    for that stage) and never escalates to Fable: sonnet goes to opus (`escalate opus`), opus to opus at xhigh
    (`escalate opus xhigh`), and a stage already at opus xhigh (or max), or on an explicit fable, goes to needs-input
    on its second failure. --json carries `effort` and `next_effort` beside `tier` and `next_tier`. WorkTest.env drops
    every HARNESS_* variable of the real environment."""

    SPREAD = [("task", "S", "low"), ("bug", "S", "low"), ("task", "M", "low"), ("chore", "M", "high"),
              ("task", "L", "low"), ("refactor", "L", "high"), ("task", "S", "high"), ("task", None, None),
              ("bug", "L", None)]

    def put_ticket(self, number, kind, size, risk, tier=None):
        """A ticket cache as `harness-board show` writes it: labels, type, size, risk and the effective tier."""
        write(self.cache_path(number), json.dumps({
            "number": number, "title": TITLE, "type": kind, "size": size, "risk": risk, "flags": [],
            "labels": ticket_labels(kind, size, risk), "tier": tier or route_of(kind, size, risk)[0],
            "attempts": []}))

    def attempt(self, number, *argv, env=None):
        return self.ok("attempt", str(number), *argv, env=env).stdout.strip()

    def attempt_json(self, number, *argv, env=None):
        return Keys(self.ok_json("attempt", str(number), *argv, "--json", env=env))

    def attempts(self, number):
        return [Keys(a) for a in self.read_json(self.cache_path(number))["attempts"]]

    def configure(self, **env):
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    # ----------------------------------------------------------- broad: the ticket's route decides the default
    def test_the_ticket_route_decides_the_tier_and_effort_of_each_failure(self):
        seen = set()
        for i, (kind, size, risk) in enumerate(self.SPREAD):
            tier, effort = route_of(kind, size, risk)
            seen.add(escalation_text(tier, effort))
            with self.subTest(kind=kind, size=size, risk=risk, output="text"):
                n = 100 + i
                self.put_ticket(n, kind, size, risk)
                answers = [self.attempt(n, "code", "fail") for _ in range(3)]
                self.assertEqual(answers, ["retry", escalation_text(tier, effort), "needs-input"])
                self.assertNotIn("fable", " ".join(answers))
                attempts = self.attempts(n)
                self.assertEqual([(a["stage"], a["tier"], a["effort"], a["result"]) for a in attempts],
                                 [("code", tier, effort, "fail")] * 3)
            with self.subTest(kind=kind, size=size, risk=risk, output="json"):
                n = 200 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual({k: first[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                        "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "retry", "failures": 1, "next_tier": None,
                                  "next_effort": None})
                second = self.attempt_json(n, "code", "fail")
                nxt = escalation_of(tier, effort)
                self.assertEqual({k: second[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                         "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "escalate" if nxt else "needs-input",
                                  "failures": 2, "next_tier": nxt[0] if nxt else None,
                                  "next_effort": nxt[1] if nxt else None})
                done = self.attempt_json(n, "code", "pass")
                self.assertEqual((done["action"], done["next_tier"], done["next_effort"]), ("done", None, None))
        # the spread reaches all three answers
        self.assertEqual(seen, {"escalate opus", "escalate opus xhigh", "needs-input"})

    def test_size_l_and_risk_high_run_at_opus_xhigh_and_go_to_needs_input(self):
        # DoD: a failure at opus xhigh gives needs-input; nothing prints fable. Every stage alike.
        for n, (kind, size, risk) in ((31, ("task", "L", "low")), (32, ("bug", "M", "high")),
                                      (33, ("chore", "S", "high"))):
            self.put_ticket(n, kind, size, risk)
            for stage in ("tests", "breaker", "code", "ci"):
                with self.subTest(ticket=n, stage=stage):
                    self.assertEqual(self.attempt(n, stage, "fail"), "retry")
                    data = self.attempt_json(n, stage, "fail")
                    self.assertEqual((data["tier"], data["effort"], data["action"], data["next_tier"],
                                      data["next_effort"]), ("opus", "xhigh", "needs-input", None, None))
                    self.assertEqual(self.attempt(n, stage, "fail"), "needs-input")

    def test_a_second_failure_at_opus_says_escalate_opus_xhigh(self):
        # DoD: an M/low ticket runs at opus with no effort; its second failure escalates to opus at xhigh
        for n, kind in ((41, "task"), (42, "bug"), (43, "refactor")):
            with self.subTest(kind=kind):
                self.put_ticket(n, kind, "M", "low")
                self.assertEqual(self.attempt(n, "code", "fail"), "retry")
                self.assertEqual(self.attempt(n, "code", "fail"), "escalate opus xhigh")
                # the escalated run, recorded at opus xhigh, fails again: needs-input
                self.assertEqual(self.attempt(n, "code", "fail", "--tier", "opus", "--effort", "xhigh"), "needs-input")

    # ----------------------------------------------------------- explicit --tier and --effort
    def test_every_explicit_tier_and_effort(self):
        n = 300
        for tier in ("sonnet", "opus", "fable"):
            for effort in (None,) + EFFORT_LEVELS:
                flags = ["--tier", tier] + (["--effort", effort] if effort else [])
                with self.subTest(tier=tier, effort=effort):
                    n += 1
                    self.put_ticket(n, "task", "M", "low")              # code route: opus, no effort
                    first = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((second["tier"], second["effort"]), (tier, effort))
                    nxt = escalation_of(tier, effort)
                    if tier == "sonnet":
                        self.assertEqual((second["action"], second["next_tier"]), ("escalate", "opus"))
                    else:
                        self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                         ("escalate" if nxt else "needs-input", nxt[0] if nxt else None,
                                          nxt[1] if nxt else None))
                    attempts = self.attempts(n)
                    self.assertEqual([(a["tier"], a["effort"]) for a in attempts], [(tier, effort)] * 2)
                    n += 1
                    self.put_ticket(n, "task", "M", "low")
                    self.assertEqual(self.attempt(n, "code", "fail", *flags), "retry")
                    text = self.attempt(n, "code", "fail", *flags)
                    self.assertNotIn("fable", text)
                    if tier == "sonnet":
                        self.assertTrue(text.startswith("escalate opus"), text)
                    else:
                        self.assertEqual(text, escalation_text(tier, effort))
                    if tier == "sonnet" and effort is None:
                        self.assertEqual(text, "escalate opus")

    def test_an_explicit_tier_that_matches_the_stage_route_takes_its_effort(self):
        # Without --effort, an attempt at the tier its stage routes to ran at that route's effort; at another tier it
        # ran at none. The review stage's route is the review's, not the coder's.
        cases = [
            # (labels, stage, flags, second answer)
            (("task", "L", "low"), "code", ["--tier", "opus"], "needs-input"),            # code route: opus xhigh
            (("task", "L", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),  # review route: opus
            (("task", "M", "high"), "review", ["--tier", "opus"], "needs-input"),        # review route: opus xhigh
            (("task", "M", "high"), "code", ["--tier", "opus"], "needs-input"),
            (("task", "M", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),
            (("task", "L", "low"), "code", ["--tier", "sonnet"], "escalate opus"),       # not the route's tier
            (("task", "L", "low"), "code", ["--tier", "opus", "--effort", "high"], "escalate opus xhigh"),
            (("task", "M", "high"), "review", ["--tier", "fable"], "needs-input"),
        ]
        for i, ((kind, size, risk), stage, flags, want) in enumerate(cases):
            with self.subTest(labels=(kind, size, risk), stage=stage, flags=flags):
                n = 400 + i
                self.put_ticket(n, kind, size, risk)
                self.assertEqual(self.attempt(n, stage, "fail", *flags), "retry")
                self.assertEqual(self.attempt(n, stage, "fail", *flags), want)

    def test_effort_takes_the_five_levels_and_refuses_anything_else(self):
        self.put_ticket(12, "task", "M", "low")
        for bad in ("extra", "XHIGHER", "5"):
            with self.subTest(bad=bad):
                p = self.work("attempt", "12", "code", "fail", "--effort", bad)
                self.assertOneLineError(p, bad)
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], [])
        for level in EFFORT_LEVELS:
            with self.subTest(level=level):
                data = self.attempt_json(12, "code", "pass", "--effort", level)
                self.assertEqual((data["effort"], data["action"]), (level, "done"))
        self.assertEqual([a["effort"] for a in self.attempts(12)], list(EFFORT_LEVELS))

    # ----------------------------------------------------------- settings and stale caches
    def test_a_pre_239_cache_that_says_fable_never_escalates_to_fable(self):
        # A cache written before #239 holds the old rubric's tier (fable for size:L or risk:high). The labels decide
        # now: opus at xhigh, and needs-input on the second failure.
        for n, (kind, size, risk) in ((51, ("task", "L", "high")), (52, ("task", "M", "high")),
                                      (53, ("bug", "L", "low"))):
            with self.subTest(kind=kind, size=size, risk=risk):
                self.put_ticket(n, kind, size, risk, tier="fable")
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), ("opus", "xhigh"))
                self.assertEqual(self.attempt(n, "code", "fail"), "needs-input")
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))

    def test_explicit_fable_from_the_settings_still_runs_fable_and_tops_out(self):
        for n, how in ((61, "settings.json"), (62, "environment")):
            with self.subTest(how=how):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="fable")
                else:
                    env = self.env(HARNESS_CODE_TIER="fable")
                self.put_ticket(n, "task", "M", "low", tier="fable")
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual(first["tier"], "fable")
                second = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((second["tier"], second["action"], second["next_tier"]),
                                 ("fable", "needs-input", None))

    def test_the_code_effort_setting_is_the_effort_a_stage_ran_at(self):
        # HARNESS_CODE_EFFORT wins over the labels' xhigh, as it does for `harness-board tier`
        cases = [("low", ("task", "L", "low"), "escalate opus xhigh"),
                 ("high", ("bug", "M", "high"), "escalate opus xhigh"),
                 ("max", ("task", "M", "low"), "needs-input"),
                 ("xhigh", ("task", "M", "low"), "needs-input"),
                 ("medium", ("task", "S", "low"), "escalate opus")]
        for i, (level, (kind, size, risk), want) in enumerate(cases):
            with self.subTest(level=level, labels=(kind, size, risk)):
                self.configure(HARNESS_CODE_EFFORT=level)
                n = 70 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (route_of(kind, size, risk)[0], level))
                self.assertEqual(self.attempt(n, "code", "fail"), want)

    def test_a_code_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_CODE_TIER replaces the labels' tier and their effort. A size:L ticket under
        # HARNESS_CODE_TIER=opus runs at opus with no effort, so its second failure escalates to opus xhigh.
        for n, how, (kind, size, risk) in ((91, "settings.json", ("task", "L", "low")),
                                           (92, "environment", ("bug", "L", "high")),
                                           (93, "settings.json", ("task", "M", "high"))):
            with self.subTest(how=how, labels=(kind, size, risk)):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="opus")
                else:
                    env = self.env(HARNESS_CODE_TIER="opus")
                self.put_ticket(n, kind, size, risk, tier="opus")       # the effective tier harness-board writes
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", None, "retry"))
                self.assertEqual(self.attempt(n, "code", "fail", env=env), "escalate opus xhigh")
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", None)] * 2)
                # HARNESS_CODE_EFFORT set beside it is the effort the stage ran at
                self.put_ticket(n + 10, kind, size, risk, tier="opus")
                env2 = self.env(HARNESS_CODE_TIER="opus", HARNESS_CODE_EFFORT="xhigh")
                self.assertEqual(self.attempt_json(n + 10, "code", "fail", env=env2)["effort"], "xhigh")
                self.assertEqual(self.attempt(n + 10, "code", "fail", env=env2), "needs-input")

    def test_a_review_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_REVIEW_TIER replaces the labels' review rule, their xhigh included. A risk:high review
        # recorded at `--tier opus` ran at opus xhigh by the labels (needs-input on the second failure), but at opus
        # with no effort under HARNESS_REVIEW_TIER=opus (escalate opus xhigh). HARNESS_REVIEW_TIER=fable runs at
        # fable xhigh and tops out.
        cases = [
            (None, "opus", "xhigh", "needs-input"),
            ({"HARNESS_REVIEW_TIER": "opus"}, "opus", None, "escalate opus xhigh"),
            ({"HARNESS_REVIEW_TIER": "fable"}, "fable", "xhigh", "needs-input"),
        ]
        for i, (settings, tier, effort, want) in enumerate(cases):
            for where in (("settings.json", "environment") if settings else (None,)):
                with self.subTest(settings=settings, where=where):
                    self.setUp()
                    env = None
                    if where == "settings.json":
                        self.configure(**settings)
                    elif where == "environment":
                        env = self.env(**settings)
                    n = 95 + i
                    self.put_ticket(n, "task", "M", "high")
                    first = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    nxt = escalation_of(tier, effort)
                    self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                     ("escalate" if nxt else "needs-input", nxt[0] if nxt else None,
                                      nxt[1] if nxt else None))
                    self.put_ticket(n + 10, "task", "M", "high")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), "retry")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), want)

    def test_a_cache_without_labels_that_says_fable_reads_as_opus_xhigh(self):
        # Agrees with `harness-board tier` on the same cache: a stored fable without labels is the pre-#239 rubric's
        # answer for size:L or risk:high, so the attempt runs at opus xhigh and the second failure is needs-input.
        for n, stage in ((85, "code"), (86, "tests"), (87, "ci")):
            with self.subTest(stage=stage):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": "fable", "attempts": []}))
                first = self.attempt_json(n, stage, "fail")
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", "xhigh", "retry"))
                second = self.attempt_json(n, stage, "fail")
                self.assertEqual((second["tier"], second["effort"], second["action"], second["next_tier"],
                                  second["next_effort"]), ("opus", "xhigh", "needs-input", None, None))
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", "xhigh")] * 2)
                self.assertNotIn("fable", json.dumps([dict(first), dict(second)]))
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))
                write(self.cache_path(n + 10), json.dumps({"number": n + 10, "title": TITLE, "tier": "fable",
                                                           "attempts": []}))
                self.assertEqual([self.attempt(n + 10, stage, "fail") for _ in range(2)], ["retry", "needs-input"])
        # the board agrees on the same cache
        board = os.path.join(os.path.dirname(TOOL), "harness-board")
        p = subprocess.run([sys.executable, board, "tier", "85", "--json"], cwd=self.repo, env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual((data["tier"], data["effort"]), ("opus", "xhigh"))
        # an explicit HARNESS_CODE_TIER=fable still gives fable on that cache
        write(self.cache_path(88), json.dumps({"number": 88, "title": TITLE, "tier": "fable", "attempts": []}))
        env = self.env(HARNESS_CODE_TIER="fable")
        self.assertEqual(self.attempt_json(88, "code", "fail", env=env)["tier"], "fable")
        self.assertEqual(self.attempt(88, "code", "fail", env=env), "needs-input")

    def test_a_cache_without_labels_keeps_its_tier(self):
        # Caches written by hand or by old tools carry no labels: the cached tier stands, with no effort.
        for n, tier, want in ((81, "sonnet", "escalate opus"), (82, "opus", "escalate opus xhigh")):
            with self.subTest(tier=tier):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": tier, "attempts": []}))
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (tier, None))
                self.assertEqual(self.attempt(n, "code", "fail"), want)


class Budget(WorkTest):
    def test_status_exhaust_and_reset(self):
        self.assertEqual(self.ok("budget").stdout.splitlines(), ["api: available", "cloud: available"])
        p = self.ok("budget", "exhaust", "cloud", "--reason", "ultrareview: out of credits", "--hours", "2")
        self.assertRegex(p.stdout.strip(), r"^cloud: exhausted until \S+Z \(ultrareview: out of credits\)$")
        saved = self.read_json(self.budgets_path())
        self.iso_close_to(saved["cloud"]["exhausted_until"], dt.timedelta(hours=2))
        self.assertEqual(saved["cloud"]["reason"], "ultrareview: out of credits")

        status = self.ok_json("budget", "status", "--json")
        self.assertTrue(status["cloud"]["exhausted"])
        self.assertFalse(status["api"]["exhausted"])

        self.assertEqual(self.ok("budget", "reset", "cloud").stdout.strip(), "cloud: available")
        self.assertIsNone(self.read_json(self.budgets_path())["cloud"]["exhausted_until"])

        self.write_budget("api", -1)                         # a time already past means available
        self.assertIn("api: available", self.ok("budget").stdout)
        p = self.work("budget", "exhaust", "api")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--reason", p.stderr)


# =========================================================================== worktrees
class Worktrees(WorkTest):
    def add_worktree(self, branch):
        path = self.worktree_path(branch)
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, path, "origin/main")
        return path

    def test_lists_ticket_worktrees_with_age_pr_state_and_stale_flag(self):
        wt = self.add_worktree("t/12-x")
        self.git(self.repo, "worktree", "add", "-q", "-b", "elsewhere", os.path.join(self.tmp, "elsewhere"), "main")

        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual((row["path"], row["branch"], row["pr"], row["dirty"]), (wt, "t/12-x", None, False))
        self.assertGreater(row["age_days"], 9.9)
        self.assertTrue(row["stale"])
        self.assertIn(["pr", "list", "--head", "t/12-x", "--state", "all", "--json", "number,state,isDraft",
                       "--limit", "1"], self.gh_calls())
        self.assertFalse(self.ok_json("worktrees", "--stale", "30", "--json")[0]["stale"])
        text = self.ok("worktrees").stdout
        self.assertIn("t/12-x", text)
        self.assertIn("stale", text)

        self.scenario(prs=[{"number": 7, "state": "OPEN", "isDraft": True}])
        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual(row["pr"], {"number": 7, "state": "OPEN", "draft": True})
        self.assertFalse(row["stale"])

        self.scenario(prs=[{"number": 7, "state": "MERGED", "isDraft": False}])
        self.assertTrue(self.ok_json("worktrees", "--json")[0]["stale"])

    def test_remove_refuses_a_dirty_worktree_without_discard(self):
        clean = self.add_worktree("t/12-x")
        dirty = self.add_worktree("t/13-y")
        write(os.path.join(clean, ".env"), "KEY=1\n")       # ignored files do not make a tree dirty
        write(os.path.join(dirty, "README.md"), "changed\n")

        p = self.ok("worktree", "remove", "t/12-x")
        self.assertIn(f"removed worktree {clean}", p.stdout)
        self.assertIn("deleted branch t/12-x", p.stdout)
        self.assertFalse(os.path.exists(clean))

        p = self.work("worktree", "remove", "t/13-y")
        self.assertOneLineError(p, "README.md", "--discard")
        self.assertTrue(os.path.isfile(os.path.join(dirty, "README.md")))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/13-y"), "t/13-y")

        p = self.ok("worktree", "remove", "t/13-y", "--discard")
        self.assertIn("deleted branch t/13-y", p.stdout)
        self.assertFalse(os.path.exists(dirty))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/*"), "")

    def test_remove_refuses_main(self):
        self.assertOneLineError(self.work("worktree", "remove", "main"), "refusing")


# =========================================================================== start: fast-forward (#58)
class StartFastForward(WorkTest):
    def started_and_pushed(self):
        self.scenario(issues=[issue()])
        wt = self.ok_json("start", "12", "--json")["worktree"]
        self.git(wt, "push", "-q", "-u", "origin", BRANCH)
        return wt

    def elsewhere(self):
        """A second clone on the branch: where the cloud session pushes from."""
        other = self.other_clone()
        self.git(other, "switch", "-q", BRANCH)
        return other

    def commit(self, clone, name, push=True):
        write(os.path.join(clone, name), name + "\n")
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", name)
        if push:
            self.git(clone, "push", "-q", "origin", BRANCH)
        return self.git(clone, "rev-parse", "HEAD")

    def branch_warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:") and BRANCH in ln]

    def test_a_clean_worktree_behind_origin_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        p = self.ok("start", "12")                                # up to date: nothing to say
        self.assertEqual(self.branch_warnings(p), [])
        other = self.elsewhere()
        for name in ("CLOUD1.md", "CLOUD2.md"):
            head = self.commit(other, name)
            p = self.ok("start", "12")
            self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
            self.assertTrue(os.path.isfile(os.path.join(wt, name)))
            self.assertEqual(self.branch_warnings(p), [])
            self.assertEqual(self.git(wt, "status", "--porcelain"), "")

    def test_a_reused_local_branch_without_a_worktree_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        self.git(self.repo, "worktree", "remove", wt)
        head = self.commit(self.elsewhere(), "CLOUD.md")
        data = self.ok_json("start", "12", "--json")
        self.assertEqual(data["worktree"], wt)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.repo, "rev-parse", BRANCH), head)

    def test_a_diverged_branch_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        local = self.commit(wt, "LOCAL.md", push=False)
        p = self.ok("start", "12")                                # only ahead: nothing to say, nothing changes
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.commit(self.elsewhere(), "CLOUD.md")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.assertFalse(os.path.exists(os.path.join(wt, "CLOUD.md")))

    def test_a_dirty_worktree_behind_origin_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        before = self.git(wt, "rev-parse", "HEAD")
        head = self.commit(self.elsewhere(), "CLOUD.md")
        write(os.path.join(wt, "README.md"), "# proj\nedited here\n")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), before)
        self.assertEqual(read_text(os.path.join(wt, "README.md")), "# proj\nedited here\n")
        self.git(wt, "checkout", "--", "README.md")              # clean again: the next start fast-forwards
        p = self.ok("start", "12")
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)


class Waves(WorkTest):
    """#180: with HARNESS_WAVE_REVIEW set, dispatch records the tickets one call launched as a wave
    (harness_lib/waves.py) and stamps each run record with its id; `waves` shows where each wave stands from the
    ticket caches, `--refresh` asking gh once per waiting ticket; `wave drop` takes a stuck ticket out."""

    def waves_dir(self):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key, "waves")

    def wave_files(self):
        folder = self.waves_dir()
        if not os.path.isdir(folder):
            return []
        return [self.read_json(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if n.endswith(".json")]

    def write_wave(self, wave_id, tickets, review=None, created_at="2026-10-20T10:00:00Z", dropped=()):
        wave = {"id": wave_id, "repo": os.path.realpath(self.repo), "created_at": created_at,
                "tickets": list(tickets), "dropped": list(dropped), "tier": "fable", "effort": "xhigh",
                "review": review}
        write(os.path.join(self.waves_dir(), wave_id + ".json"), json.dumps(wave))
        return wave

    def write_cache(self, n, status):
        write(self.cache_path(n), json.dumps({"number": n, "title": f"Ticket {n}", "status": status}))

    def wave_env(self, tier="fable"):
        return self.env(HARNESS_WAVE_REVIEW=tier)

    def test_dispatch_groups_the_launched_tickets_into_one_wave(self):
        self.scenario(issues=[issue(), issue(13, title="Second")], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        [wave] = self.wave_files()
        self.assertRegex(wave["id"], WAVE_ID_RE)
        self.assertEqual((wave["tickets"], wave["dropped"], wave["tier"], wave["effort"], wave["review"]),
                         ([12, 13], [], "fable", "xhigh", None))
        self.assertEqual(wave["repo"], os.path.realpath(self.repo))
        self.assertIn(f"wave {wave['id']}: #12, #13 (reviewed together once all are in review)",
                      p.stdout.splitlines())
        records = self.run_records()
        self.assertEqual(sorted(r["ticket"] for r in records), [12, 13])
        for record in records:
            self.assertEqual(record["wave"], wave["id"])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "view"] and "state,labels" in c])

    def test_dispatch_json_names_the_wave(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env("opus"))
        [wave] = self.wave_files()
        self.assertEqual(data["wave"], wave["id"])
        self.assertEqual((wave["tier"], wave["effort"]), ("opus", "xhigh"))
        self.assertEqual([r["wave"] for r in data["launched"]], [wave["id"]])

    def test_without_the_setting_no_wave_is_written(self):
        for value in (None, "off"):
            with self.subTest(HARNESS_WAVE_REVIEW=value):
                self.setUp()
                self.scenario(issues=[issue(), issue(13, title="Second")],
                              ready=[ready_row(12), ready_row(13, "Second")])
                env = self.env() if value is None else self.env(HARNESS_WAVE_REVIEW=value)
                p = self.ok("dispatch", env=env)
                self.assertFalse(os.path.exists(self.waves_dir()))
                self.assertNotIn("wave", p.stdout)
                self.assertEqual(sorted(p.stdout.splitlines()),
                                 sorted([f"#12 bg on {BRANCH} (id abc123)",
                                         "#13 bg on t/13-second (id abc123)"]))
                for record in self.run_records():
                    self.assertNotIn("wave", record)
                self.setUp()
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                data = self.ok_json("dispatch", "--json", env=env)
                self.assertNotIn("wave", data)
                self.assertNotIn("wave", data["launched"][0])
                dry = self.ok_json("dispatch", "--dry-run", "--json", env=env)
                self.assertNotIn("wave", dry)
                self.assertNotIn("group", self.ok("dispatch", "--dry-run", env=env).stdout)

    def test_a_redispatched_ticket_keeps_its_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Second")], ready=[ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        waves = {w["id"]: w for w in self.wave_files()}
        self.assertEqual(len(waves), 2, waves)
        self.assertEqual(waves[old["id"]]["tickets"], [12])             # untouched
        [new] = [w for w in waves.values() if w["id"] != old["id"]]
        self.assertEqual(new["tickets"], [13])                           # 12 joins no new wave
        self.assertIn(f"wave {new['id']}: #13 (reviewed together once all are in review)", p.stdout.splitlines())
        launched = {r["ticket"]: r for r in self.run_records() if r.get("ended_at") is None}
        self.assertEqual(launched[12]["wave"], old["id"])
        self.assertEqual(launched[13]["wave"], new["id"])

    def test_a_dispatch_of_only_redispatched_tickets_makes_no_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        self.assertEqual([w["id"] for w in self.wave_files()], [old["id"]])
        self.assertIsNone(data["wave"])
        self.assertEqual([r["wave"] for r in data["launched"]], [old["id"]])

    def test_a_ticket_whose_wave_was_reviewed_joins_a_new_one(self):
        old = self.write_wave("w20261020100000", [12], review={"launched_at": "2026-10-21T10:00:00Z", "run": "r1",
                                                                "reviewed_at": "2026-10-21T11:00:00Z"})
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        [new] = [w for w in self.wave_files() if w["id"] != old["id"]]
        self.assertEqual((data["wave"], new["tickets"]), (new["id"], [12]))

    def test_dry_run_names_the_grouping_and_writes_nothing(self):
        self.write_wave("w20261020100000", [14])
        self.write_run(14, pid=self.dead_pid(), branch="t/14-x", started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(), issue(13, title="Second"), issue(14, labels=IN_PROGRESS, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        before = self.wave_files()
        p = self.ok("dispatch", "--dry-run", env=self.wave_env())
        self.assertIn("would group #12, #13 into a wave", p.stdout.splitlines())
        self.assertIn("would start #14 Third (again after early) and launch bg", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json", env=self.wave_env())
        self.assertEqual(data["wave"], {"id": None, "tickets": [12, 13]})
        self.assertEqual(self.wave_files(), before)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)

    def test_waves_lists_progress_from_the_caches_without_gh(self):
        self.write_wave("w20261020100000", [12, 13], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [14], created_at="2026-10-20T11:00:00Z")
        self.write_wave("w20261020120000", [15], created_at="2026-10-20T12:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        self.write_wave("w20261020130000", [16], created_at="2026-10-20T13:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-78",
                                "reviewed_at": "2026-10-21T11:30:00Z"})
        self.write_wave("w20261020140000", [17], created_at="2026-10-20T14:00:00Z")   # no ticket cache
        for n, status in ((12, "in-review"), (13, "in-progress"), (14, "in-review"), (15, "in-review"),
                          (16, "closed")):
            self.write_cache(n, status)
        text = self.ok("waves").stdout
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "TICKETS", "STATE", "DETAIL"])
        rows = {ln.split()[0]: ln for ln in lines[1:]}
        self.assertEqual(sorted(rows), ["w20261020100000", "w20261020110000", "w20261020120000",
                                        "w20261020140000"])          # 16 is closed: hidden without --all
        self.assertIn("#12, #13", rows["w20261020100000"])
        self.assertIn("working", rows["w20261020100000"])
        self.assertIn("1 of 2 in review; waiting for #13 (in-progress)", rows["w20261020100000"])
        self.assertIn("due", rows["w20261020110000"])
        self.assertIn("reviewing (run run-77)", rows["w20261020120000"])
        self.assertIn("waiting for #17 (", rows["w20261020140000"])
        every = {ln.split()[0]: ln for ln in self.ok("waves", "--all").stdout.splitlines()[1:]}
        self.assertIn("reviewed 2026-10-21T11:30:00Z", every["w20261020130000"])
        data = self.ok_json("waves", "--json")
        first = data[0]
        self.assertEqual((first["id"], first["state"], first["done"], first["waiting"]),
                         ("w20261020100000", "working", [12], [{"number": 13, "status": "in-progress"}]))
        self.assertEqual([w["id"] for w in data], sorted(rows))
        self.assertEqual(self.gh_calls(), [])

    def test_waves_with_none_says_so(self):
        p = self.ok("waves")
        self.assertIn("No open waves", p.stdout)
        self.assertEqual(self.ok_json("waves", "--json"), [])
        self.assertEqual(self.gh_calls(), [])

    def test_waves_refresh_asks_gh_once_per_waiting_ticket(self):
        self.write_wave("w20261020100000", [12, 13, 14], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [13, 15], created_at="2026-10-20T11:00:00Z")
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        self.write_cache(15, "in-progress")
        self.scenario(issues=[issue(13, labels=IN_REVIEW), issue(14, labels=IN_PROGRESS),
                              issue(15, labels=IN_PROGRESS, state="CLOSED")])
        data = {w["id"]: w for w in self.ok_json("waves", "--refresh", "--json")}
        views = [c for c in self.gh_calls() if c[:2] == ["issue", "view"]]
        self.assertEqual(sorted(c[2] for c in views), ["13", "14", "15"])
        for call in views:
            self.assertEqual(call[3:], ["--json", "state,labels"])
        self.assertEqual(data["w20261020100000"]["waiting"], [{"number": 14, "status": "in-progress"}])
        self.assertEqual(data["w20261020110000"]["state"], "due")
        self.assertEqual(len(self.gh_calls()), 3)
        self.assertEqual(self.read_json(self.cache_path(13))["status"], "in-progress")   # the caches are not written

    def test_wave_drop_makes_the_wave_due(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        p = self.ok("wave", "drop", "w20261020100000", "13")
        self.assertEqual(p.stdout.strip(), "dropped #13 from wave w20261020100000")
        [wave] = self.wave_files()
        self.assertEqual((wave["tickets"], wave["dropped"]), ([12], [13]))
        [row] = self.ok("waves").stdout.splitlines()[1:]
        self.assertIn("due", row)
        self.assertEqual(self.gh_calls(), [])

    def test_wave_drop_refuses_an_unknown_wave_or_ticket_and_a_launched_review(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_wave("w20261020110000", [14, 15],
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        before = self.wave_files()
        self.assertOneLineError(self.work("wave", "drop", "w20261020120000", "12"), "w20261020120000")
        self.assertOneLineError(self.work("wave", "drop", "nonsense", "12"), "nonsense")
        self.assertOneLineError(self.work("wave", "drop", "w20261020100000", "14"), "#14", "w20261020100000")
        self.assertOneLineError(self.work("wave", "drop", "w20261020110000", "14"), "w20261020110000", "launched")
        self.assertEqual(self.wave_files(), before)


if __name__ == "__main__":
    unittest.main()
