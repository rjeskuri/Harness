"""harness-work dispatch: the plan, the venues, the effort each launch carries and the routed effort.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_dispatch.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


# =========================================================================== dispatch
class Dispatch(WorkTest):
    def test_dry_run_prints_the_plan_and_starts_nothing(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertIn("venue bg, 1 live, 1 of 2 slots free", p.stdout)
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)
        self.assertIn(f"skip #13: live run {self.key}-13-", p.stdout)
        self.assertIn("skip #14: over --max 2", p.stdout)
        self.assertNotIn("would start #13", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_headless_launch_bills_through_api_key_helper_and_keeps_the_key_off_argv(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent",
                       **CREDENTIALS)
        p = self.ok("dispatch", "--venue", "headless", env=env)
        self.assertIn(f"#12 headless on {BRANCH} (pid ", p.stdout)

        [record] = self.run_records()
        wt = self.worktree_path()
        self.assertRegex(record["id"], rf"^{self.key}-12-\d{{14}}$")
        self.assertEqual((record["ticket"], record["repo"], record["worktree"], record["branch"], record["venue"]),
                         (12, os.path.realpath(self.repo), wt, BRANCH, "headless"))
        self.assertIsInstance(record["pid"], int)
        self.assertNotIn("bg_id", record)
        self.assertEqual((record["ended_at"], record["exit"], record["outcome"]), (None, None, None))
        self.assertEqual(record["log"], os.path.join(self.runs_dir(), record["id"] + ".log"))
        self.assertTrue(record["started_at"].endswith("Z"))

        self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(record["log"])))
        [call] = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(len(argv), 8)
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        self.assertNotIn(SECRET, json.dumps(call))          # neither argv nor the environment carries the key
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", call["env"])
        for name, value in CREDENTIALS.items():                # an exported key would bill instead of the helper
            self.assertNotIn(name, call["env"])
            self.assertNotIn(value, json.dumps(call))
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", headless)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual(swept["outcome"], "ok")
        self.assertTrue(swept["ended_at"])
        self.assertIn("api: available", self.ok("budget").stdout)

    def test_headless_default_api_key_command(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", "--venue", "headless")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
        argv = self.claude_calls()[0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"apiKeyHelper": DEFAULT_KEY_CMD})
        self.assertTrue(wait_for(lambda: pid_gone(self.run_records()[0]["pid"])))

    def test_falls_back_to_bg_while_the_api_budget_is_exhausted(self):
        self.write_budget("api", 1, "billing: credit balance is too low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"])
        for name in CREDENTIALS:                               # the subscription login bills, not an exported key
            self.assertNotIn(name, call["env"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["ended_at"]), ("bg", "abc123", None))
        self.assertNotIn("pid", record)
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        # The session is still working: the next dispatch leaves the ticket alone.
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working", "cwd": self.worktree_path()}])
        again = self.ok("dispatch", "--dry-run", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertIn("skip #12: live run", again.stdout)
        self.assertIn("venue bg", again.stdout)

    def test_redispatches_an_in_progress_ticket_whose_run_ended_without_a_pr(self):
        self.write_run(12, pid=self.dead_pid(), ended_at=iso_ago(hours=1), outcome="billing")
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Add the board tool (again after billing) and launch bg", p.stdout)
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))],
                      prs=[{"number": 7}])
        p = self.ok("dispatch", "--dry-run")
        self.assertNotIn("would start", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)

    def test_skips_a_ticket_after_three_ended_runs(self):
        in_progress = issue(labels=("task", "status:in-progress", "size:S", "risk:low"))
        for hours in (3, 2):
            self.write_run(12, id=f"{self.key}-12-2026101309{hours:02d}00", pid=self.dead_pid(),
                           started_at=iso_ago(hours=hours, minutes=30), ended_at=iso_ago(hours=hours), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # two ended runs: a third is launched
        self.assertIn("would start #12 Add the board tool (again after error)", p.stdout)

        self.write_run(12, id=f"{self.key}-12-20261013100000", pid=self.dead_pid(),
                       started_at=iso_ago(hours=1, minutes=30), ended_at=iso_ago(hours=1), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # three: the fourth dispatch skips it
        self.assertNotIn("would start", p.stdout)
        self.assertIn("skip #12: 3 runs ended without a ready pull request in 7 days (last: error at ", p.stdout)
        self.assertIn("run it in this session with /harness:work 12, or wait for the window", p.stdout)
        self.assertNotIn("harness-board status 12 ready", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["plan"], [])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])

        p = self.ok("dispatch")                                # for real: nothing started, the skip said
        self.assertIn("skip #12: 3 runs ended", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 3)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    # ---- ticket #50: an exhausted api budget never launches headless, whatever --venue says
    def api_until(self):
        return self.read_json(self.budgets_path())["api"]["exhausted_until"]

    def assertRefusedHeadless(self, p, until):
        """Exit non-zero, the message names when the budget is exhausted until and how to launch headless again."""
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("harness-work budget reset api", out)
        self.assertIn(until, out)
        self.assertNotIn("would start", out)
        self.assertNotIn("launching headless anyway", out)

    def assertNothingStarted(self, records_before):
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])],
                         self.gh_calls())
        self.assertEqual(sorted(r["id"] for r in self.run_records()), sorted(records_before))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted(self):
        # #12 is ready on the board; #15 is in progress after a billing run (the #43 case): either would be launched.
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            with self.subTest(hours=hours):
                self.write_budget("api", hours, reason)
                until = self.api_until()
                self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)

                p = self.work("dispatch", "--venue", "headless", env=self.env(**CREDENTIALS))
                self.assertRefusedHeadless(p, until)
                self.assertNotIn("headless on", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--max", "5", "--dry-run")
                self.assertRefusedHeadless(p, until)                # the dry run says the same and plans nothing
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--dry-run", "--json")
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNothingStarted([ended["id"]])
                self.assertEqual(self.read_json(self.budgets_path())["api"]["exhausted_until"], until)

        # Without --venue the same board launches both on bg, as before: the refusal changed nothing.
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
            for name in CREDENTIALS:
                self.assertNotIn(name, call["env"])
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def unswept_failure(self, result):
        """The overnight #43 case from a clean slate: no budgets file; #15 in progress with a headless run whose
        process is gone, not yet swept, its log ending in `result`; #12 ready on the board. Returns the record."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.budgets_path(), self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)
        record = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(minutes=20))
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        self.scenario(issues=[issue(), issue(15, labels=IN_PROGRESS, title="Billing victim")],
                      ready=[ready_row(12)])
        return record

    def test_venue_headless_is_refused_when_dispatchs_own_sweep_exhausts_the_api_budget(self):
        # The budget is judged after dispatch's sweep: the sweep finds the billing (or auth) failure, marks the api
        # budget, and --venue headless is then refused, as it must have been at 23:55:51Z on 2026-10-13.
        low = dict(API_401, api_error_status=400, result="Credit balance is too low")
        cases = [("billing", low, ()), ("auth", API_401, ()), ("billing", low, ("--dry-run",)),
                 ("auth", API_401_HELPER, ("--dry-run",))]
        for outcome, result, extra in cases:
            with self.subTest(outcome=outcome, extra=extra):
                record = self.unswept_failure(result)
                p = self.work("dispatch", "--venue", "headless", "--max", "5", *extra, env=self.env(**CREDENTIALS))
                self.assertTrue(os.path.exists(self.budgets_path()), p.stdout + p.stderr)
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(outcome + ": "), api["reason"])
                self.assertRefusedHeadless(p, api["exhausted_until"])
                self.assertNotIn("headless on", p.stdout)
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([record["id"]])
                [swept] = self.run_records()
                self.assertEqual(swept["outcome"], outcome)
                self.assertTrue(swept["ended_at"])

        # The same board without --venue: the sweep exhausts the budget and both tickets go to bg, as before.
        record = self.unswept_failure(low)
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
        self.assertEqual({r["id"]: r["outcome"] for r in self.run_records() if r["id"] == record["id"]},
                         {record["id"]: "billing"})
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted_even_with_nothing_to_launch(self):
        self.write_budget("api", 5, "auth: invalid api key")
        until = self.api_until()
        self.scenario()                                                 # no ready ticket, no run on record
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                p = self.work("dispatch", "--venue", "headless", *extra)
                self.assertRefusedHeadless(p, until)
                self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg")                        # bg is still fine: nothing to do
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])

    def test_venue_headless_launches_headless_while_the_api_budget_is_available(self):
        # Four ways the api budget is not exhausted: no budgets file, its time has passed, only the cloud budget is
        # exhausted, and `harness-work budget reset api` after an exhaustion (the refusal's advice).
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        setups = [(12, "Add the board tool", no_file), (13, "Second ticket", lambda: self.write_budget("api", -2)),
                  (14, "Third ticket", lambda: self.write_budget("cloud", 5)), (16, "Fourth ticket", reset)]
        for n, title, setup in setups:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                if os.path.exists(self.claude_log):
                    os.unlink(self.claude_log)
                setup()
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", "--venue", "headless")
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn("budget reset", p.stdout + p.stderr)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                argv = self.claude_calls()[0]["argv"]
                self.assertEqual(argv[:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                            "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": DEFAULT_KEY_CMD})
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    # ---- ticket #57: without --venue, dispatch runs on the subscription (bg) whatever the api budget says
    def budget_states(self):
        """(label, setup) for every state of the api budget: no budgets file, an exhaustion whose time has passed,
        only the cloud budget exhausted, `budget reset api` after an exhaustion, and the api budget exhausted."""
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        return [("no budgets file", no_file), ("expired", lambda: self.write_budget("api", -2, "billing: old")),
                ("cloud only", lambda: self.write_budget("cloud", 5, "cloud: out")), ("reset", reset),
                ("api exhausted", lambda: self.write_budget("api", 3, "billing: credit balance is too low"))]

    def fresh_logs(self):
        for record in self.run_records():                  # a headless child still running would log into the next case
            if isinstance(record.get("pid"), int):
                wait_for(lambda: pid_gone(record["pid"]))
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)

    def test_default_venue_is_bg_whatever_the_api_budget_says(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        key_cmd = f"cat {keyfile}"
        tickets = [(12, "Add the board tool"), (13, "Second ticket"), (14, "Third ticket"), (16, "Fourth ticket"),
                   (17, "Fifth ticket")]
        for (label, setup), (n, title) in zip(self.budget_states(), tickets):
            with self.subTest(budget=label, ticket=n):
                self.fresh_logs()
                setup()
                budgets_before = read_text(self.budgets_path())
                status_before = self.ok("budget").stdout
                bg_id = f"bg{n}{label[:2]}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                env = self.env(HARNESS_API_KEY_CMD=key_cmd, FAKECLAUDE_BG_ID=bg_id, **CREDENTIALS)
                p = self.ok("dispatch", env=env)
                self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id {bg_id}\)$")
                self.assertNotIn("headless", p.stdout + p.stderr)
                self.assertNotIn("budget reset", p.stdout + p.stderr)

                [call] = self.claude_calls()                   # one session, on the subscription login
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {title}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                self.assertNotIn(key_cmd, json.dumps(call["argv"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name, value in CREDENTIALS.items():          # no exported key or token bills instead
                    self.assertNotIn(name, call["env"])
                    self.assertNotIn(value, json.dumps(call))
                [record] = self.run_records()
                self.assertTrue(record["branch"].startswith(f"t/{n}-"), record["branch"])
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"]),
                                 (n, "bg", bg_id, None))
                self.assertNotIn("pid", record)
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                body = comment[comment.index("--body") + 1]
                self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)

                # dispatch leaves the budgets as it found them: the default does not depend on them or change them
                self.assertEqual(read_text(self.budgets_path()), budgets_before)
                self.assertEqual(self.ok("budget").stdout, status_before)

    def test_dry_run_plan_names_bg_by_default_and_headless_only_on_request(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        board = dict(issues=[issue(), issue(13), issue(14, title="Third"), issue(15, title="Fourth")],
                     ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third"), ready_row(15, "Fourth")])
        for (label, setup), most in zip(self.budget_states()[:4], (2, 3, 4, 5)):    # the api budget is available
            with self.subTest(budget=label, max=most):
                setup()
                self.scenario(**board)
                free = most - 1
                planned = [n for n in (12, 14, 15)][:free]
                for venue, extra in (("bg", ()), ("bg", ("--venue", "bg")), ("headless", ("--venue", "headless"))):
                    p = self.ok("dispatch", "--dry-run", "--max", str(most), *extra)
                    self.assertIn(f"Dry run: venue {venue}, 1 live, {free} of {most} slots free.", p.stdout)
                    starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                    self.assertEqual(len(starts), len(planned), p.stdout)
                    for line in starts:
                        self.assertTrue(line.endswith(f"and launch {venue}"), line)
                    other = "headless" if venue == "bg" else "launch bg"
                    self.assertNotIn(other, p.stdout)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *extra)
                    self.assertEqual(data["venue"], venue)
                    self.assertEqual([row["number"] for row in data["plan"]], planned)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(len(self.run_records()), 1)
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_redispatched_ticket_launches_bg_by_default(self):
        # #12 ended early, #15 ended in an error, both still in progress with no PR; #16 is fresh from the board.
        # No budgets file: the api budget is available, and still every launch goes to the subscription.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="early")
        self.write_run(15, id=f"{self.key}-15-20261013080000", pid=self.dead_pid(), started_at=iso_ago(hours=5),
                       ended_at=iso_ago(hours=4), outcome="error")
        titles = {12: "Ended early", 15: "Errored", 16: "Fresh"}
        self.scenario(issues=[issue(12, labels=IN_PROGRESS, title=titles[12]),
                              issue(15, labels=IN_PROGRESS, title=titles[15]), issue(16, title=titles[16])],
                      ready=[ready_row(16, titles[16])])
        self.assertFalse(os.path.exists(self.budgets_path()))

        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Ended early (again after early) and launch bg", p.stdout.splitlines())
        self.assertIn("would start #15 Errored (again after error) and launch bg", p.stdout.splitlines())
        self.assertNotIn("headless", p.stdout)

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57", **CREDENTIALS))
        for n in (12, 15, 16):
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id redo57\)$")
        self.assertNotIn("headless", p.stdout)
        calls = {int(c["argv"][-1].split()[1]): c for c in self.claude_calls()}
        self.assertEqual(sorted(calls), [12, 15, 16])
        for n, call in calls.items():
            with self.subTest(ticket=n):
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {titles[n]}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual(sorted((r["ticket"], r["venue"], r.get("bg_id"), "pid" in r) for r in fresh),
                         [(12, "bg", "redo57", False), (15, "bg", "redo57", False), (16, "bg", "redo57", False)])

    # ---- ticket #57: `--api` is the CLI's own alias for `--venue headless`, the only other way onto the API key
    def test_api_launches_headless_on_the_api_key_while_the_budget_is_available(self):
        tickets = [(12, "Add the board tool", ("--api",)), (13, "Second ticket", ("--api", "--venue", "headless")),
                   (14, "Third ticket", ("--venue", "headless", "--api")), (16, "Fourth ticket", ("--api",))]
        for (label, setup), (n, title, flags) in zip(self.budget_states()[:4], tickets):
            with self.subTest(budget=label, ticket=n, flags=flags):
                self.fresh_logs()
                setup()
                keyfile = write(os.path.join(self.tmp, f"key{n}.txt"), SECRET + "\n")
                key_cmd = f"cat {keyfile}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", *flags, env=self.env(HARNESS_API_KEY_CMD=key_cmd, **CREDENTIALS))
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn(" bg on ", p.stdout)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertIsInstance(record["pid"], int)
                self.assertNotIn("bg_id", record)
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                [call] = self.claude_calls()
                self.assertEqual(call["argv"][:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                                    "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(call["argv"][7]), {"apiKeyHelper": key_cmd})
                self.assertEqual(len(call["argv"]), 8)
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                self.assertTrue(comment[comment.index("--body") + 1].splitlines()[0].endswith(", headless)"))
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_api_dry_run_plans_headless(self):
        self.scenario(issues=[issue(), issue(13), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        for most, flags in ((1, ("--api",)), (2, ("--api", "--venue", "headless")), (3, ("--api",))):
            with self.subTest(max=most, flags=flags):
                p = self.ok("dispatch", "--dry-run", "--max", str(most), *flags)
                self.assertIn(f"Dry run: venue headless, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for line in starts:
                    self.assertTrue(line.endswith("and launch headless"), line)
                self.assertNotIn("launch bg", p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *flags)
                self.assertEqual(data["venue"], "headless")
                self.assertEqual([row["number"] for row in data["plan"]], [12, 13, 14][:most])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_api_is_refused_while_the_api_budget_is_exhausted(self):
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            for flags in (("--api",), ("--api", "--venue", "headless")):
                for extra in ((), ("--dry-run",), ("--dry-run", "--json")):
                    with self.subTest(hours=hours, flags=flags, extra=extra):
                        self.write_budget("api", hours, reason)
                        until = self.api_until()
                        self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                        for path in (self.gh_log, self.claude_log):
                            if os.path.exists(path):
                                os.unlink(path)
                        p = self.work("dispatch", *flags, "--max", "5", *extra, env=self.env(**CREDENTIALS))
                        if "--json" in extra:
                            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                        else:
                            self.assertRefusedHeadless(p, until)
                            self.assertNotIn("launch headless", p.stdout)
                            self.assertNotIn("headless on", p.stdout)
                        self.assertNothingStarted([ended["id"]])
                        self.assertEqual(self.api_until(), until)
        # nothing to launch at all: still refused
        self.scenario()
        p = self.work("dispatch", "--api")
        self.assertRefusedHeadless(p, self.api_until())

    def test_api_with_venue_bg_is_an_error_and_starts_nothing(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])            # the api budget is available
        # --api alone is a known flag (so the error below is the conflict, not an unknown argument)
        p = self.ok("dispatch", "--api", "--dry-run")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch headless", p.stdout)
        for flags in (("--api", "--venue", "bg"), ("--venue", "bg", "--api")):
            for extra in ((), ("--dry-run",)):
                with self.subTest(flags=flags, extra=extra):
                    p = self.work("dispatch", *flags, *extra, env=self.env(**CREDENTIALS))
                    self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("--api", p.stderr)
                    self.assertNotIn("unrecognized", p.stderr)
                    self.assertNotIn("would start", p.stdout)
                    self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg", "--dry-run")              # the control: the same board plans bg
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)


class DispatchDefaultMax(WorkTest):
    """Ticket #130: `harness-work dispatch` without `--max` allows ten live runs at once (it was three)."""

    def board(self, count):
        numbers = list(range(12, 12 + count))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        return numbers

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def test_dry_run_without_max_plans_ten_and_skips_the_rest_over_max_ten(self):
        for count in (12, 15):
            with self.subTest(ready=count):
                numbers = self.board(count)
                p = self.ok("dispatch", "--dry-run")
                self.assertIn("Dry run: venue bg, 0 live, 10 of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), 10, p.stdout)
                for n in numbers[:10]:
                    self.assertIn(f"would start #{n} Ticket {n} (tier sonnet) and launch bg", p.stdout.splitlines())
                for n in numbers[10:]:
                    self.assertIn(f"skip #{n}: over --max 10", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:10])
                self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                                 [(n, "over --max 10") for n in numbers[10:]])
                self.assertEqual(self.claude_calls(), [])

    def test_dry_run_with_ten_or_fewer_ready_tickets_plans_them_all_with_nothing_skipped(self):
        for count in (4, 10):                       # four is past the old default of three
            with self.subTest(ready=count):
                numbers = self.board(count)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers)
                self.assertEqual(data["skipped"], [])
                self.assertNotIn("skip #", self.ok("dispatch", "--dry-run").stdout)

    def test_the_free_slots_are_ten_minus_the_live_runs(self):
        for live_count in (1, 4):
            with self.subTest(live=live_count):
                if os.path.isdir(self.runs_dir()):
                    shutil.rmtree(self.runs_dir())
                live_numbers = list(range(40, 40 + live_count))
                for n in live_numbers:
                    self.write_run(n, pid=self.sleeper(n))
                numbers = list(range(12, 24))
                self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers + live_numbers],
                              ready=[ready_row(n, f"Ticket {n}") for n in numbers])
                free = 10 - live_count
                p = self.ok("dispatch", "--dry-run")
                self.assertIn(f"Dry run: venue bg, {live_count} live, {free} of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), free, p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:free])
                self.assertEqual([s["number"] for s in data["skipped"]], numbers[free:])
                self.assertTrue(all(s["why"] == "over --max 10" for s in data["skipped"]))

    def test_ten_live_runs_fill_the_default_cap(self):
        for n in range(40, 50):
            self.write_run(n, pid=self.sleeper(n))
        self.scenario(issues=[issue(12), issue(13)], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch")
        self.assertIn("10 live runs; --max 10 reached, nothing launched.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_real_dispatch_without_max_launches_ten_of_twelve(self):
        numbers = self.board(12)
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="ten123", **CREDENTIALS))
        for n in numbers[:10]:
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id ten123\)$")
        for n in numbers[10:]:
            self.assertNotRegex(p.stdout, rf"(?m)^#{n} bg on")
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:10]))
        self.assertEqual(len([r for r in self.run_records() if r["venue"] == "bg"]), 10)

    def test_a_real_dispatch_without_max_and_four_live_runs_launches_six(self):
        for n in range(40, 44):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:6]))

    def test_an_explicit_max_still_overrides_the_default(self):
        # 3 is the old default and 10 the new one: an explicit value equal to either must be honored as given.
        for most in (2, 3, 5, 10, 12):
            with self.subTest(max=most):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", "--max", str(most))
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_an_explicit_max_in_the_equals_form_or_abbreviated_is_honored(self):
        # `--max=3`, `--max=5` and the argparse abbreviation `--ma 3` are explicit too; none may fall back to 10.
        for spelling, most in ((("--max=3",), 3), (("--max=5",), 5), (("--max=10",), 10), (("--ma", "3"), 3),
                               (("--ma=5",), 5)):
            with self.subTest(args=" ".join(spelling)):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", *spelling)
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json", *spelling)
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_a_real_dispatch_with_max_equals_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max=3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))

    def test_a_real_dispatch_with_an_explicit_max_of_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))
        self.assertEqual(len(self.run_records()), 3)

    def test_a_real_dispatch_with_an_explicit_max_of_three_and_one_live_run_launches_two(self):
        self.write_run(40, pid=self.sleeper(40))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:2]))

    def test_a_real_dispatch_with_an_explicit_max_of_ten_and_three_live_runs_launches_seven(self):
        for n in range(40, 43):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "10", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:7]))




class DispatchEffort(WorkTest):
    """Ticket #176: `harness-work dispatch` launches each session at the effort the project configures
    (HARNESS_CODE_EFFORT in the main checkout's .claude/settings.json), as `--effort <level>`; the dispatcher's own
    CLAUDE_EFFORT neither decides the flag nor reaches the child. Without the setting nothing changes. WorkTest.env
    drops every CLAUDE* and HARNESS_* variable of the real environment, so these tests set their own."""

    BG_TODAY = ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"]

    def configure(self, **env):
        """The main checkout's settings.json with the given env keys (HARNESS_CODE_EFFORT=...)."""
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    def bg_with_effort(self, name, effort, prompt):
        return ["--bg", "-n", name, "--permission-mode", "auto", "--effort", effort, prompt]

    def warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:")]

    @staticmethod
    def effort_problem(raw):
        """The one warning for an invalid HARNESS_CODE_EFFORT: `harness-work: ` + load_routing's problems line."""
        return f"harness-work: HARNESS_CODE_EFFORT={raw!r} is not one of low, medium, high, xhigh, max; ignored"

    def dispatch_from(self, cwd, *args, env=None):
        p = self.work("dispatch", *args, env=env, cwd=cwd)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def headless_call(self, index=0):
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) > index), self.claude_calls())
        return self.claude_calls()[index]

    def settle(self):
        """Wait for the fake headless runs to end, so no process outlives the test."""
        for record in self.run_records():
            if record.get("pid"):
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_bg_launch_passes_the_configured_effort(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                        "--effort", "xhigh", "/harness:work 12"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["effort"]), ("bg", "abc123", "xhigh"))

    def test_the_flag_follows_the_setting_and_covers_every_launched_ticket(self):
        # Two tickets in one dispatch both get the level; a later dispatch with another level gives that one. A
        # hard-coded level or a flag that ignores the setting fails one of the three launches.
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        self.ok("dispatch", "--max", "2")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": self.bg_with_effort("proj #12 Add the board tool", "low",
                                                               "/harness:work 12"),
            "proj #13 Second": self.bg_with_effort("proj #13 Second", "low", "/harness:work 13")})

        self.configure(HARNESS_CODE_EFFORT="max")
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working",
                              "cwd": self.worktree_path("t/12-add-the-board-tool")}])
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(14, "Third")])
        self.ok("dispatch", env=self.env(FAKECLAUDE_AGENTS=agents))
        newest = self.claude_calls()[-1]["argv"]
        self.assertEqual(newest, self.bg_with_effort("proj #14 Third", "max", "/harness:work 14"))

    def test_every_valid_level_is_passed_as_given(self):
        # The level is the setting's value, lower-cased; one dispatch per level in a project of its own.
        for level, written in (("low", "low"), ("medium", "medium"), ("high", "High"), ("xhigh", "XHIGH"),
                               ("max", "max")):
            with self.subTest(level=written):
                self.setUp()                                     # a fresh project, home and logs per level
                self.configure(HARNESS_CODE_EFFORT=written)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", level,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], level)

    def test_headless_launch_passes_the_configured_effort(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}")
        self.ok("dispatch", "--venue", "headless", env=env)
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.assertNotIn(SECRET, json.dumps(self.claude_calls()))
        self.settle()

    def test_without_the_setting_argv_and_record_are_unchanged(self):
        # No settings file at all, a settings file without the key, and a blank value: all three are "not set".
        cases = (("no settings file", None), ("other keys only", {"HARNESS_CODE_TIER": "opus"}),
                 ("blank value", {"HARNESS_CODE_EFFORT": "  "}))
        for label, env in cases:
            with self.subTest(label):
                self.setUp()
                if env is not None:
                    self.configure(**env)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch")
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertEqual(sorted(record), sorted(["id", "ticket", "repo", "worktree", "branch", "venue", "log",
                                                         "started_at", "ended_at", "exit", "outcome", "bg_id"]))
        with self.subTest("headless"):
            self.setUp()
            self.scenario(issues=[issue()], ready=[ready_row(12)])
            self.ok("dispatch", "--venue", "headless")
            argv = self.headless_call()["argv"]
            self.assertEqual(len(argv), 8, argv)
            self.assertNotIn("--effort", argv)
            [record] = self.run_records()
            self.assertNotIn("effort", record)
            self.settle()

    def test_dry_run_and_json_name_the_effort(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])

        self.configure(HARNESS_CODE_EFFORT="xhigh")
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "Dry run: venue bg, effort xhigh, 1 live, 1 of 2 slots free.")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", lines)
        self.assertTrue(any(ln.startswith("skip #14: over --max 2") for ln in lines), p.stdout)

        p = self.ok("dispatch", "--dry-run", "--max", "3", "--venue", "headless")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue headless, effort xhigh, 1 live, 2 of 3 slots free.")

        self.configure(HARNESS_CODE_EFFORT="Max")                # the level as launched: lower-cased
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "max"))
        self.assertEqual([row["number"] for row in data["plan"]], [12])
        self.assertEqual(self.ok("dispatch", "--dry-run", "--max", "2").stdout.splitlines()[0],
                         "Dry run: venue bg, effort max, 1 live, 1 of 2 slots free.")

        self.configure()                                         # not set: the line is today's, effort is null
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 1 live, 1 of 2 slots free.")
        self.assertNotIn("effort", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        self.assertEqual(self.claude_calls(), [])

    def test_the_json_result_of_a_launch_carries_the_effort(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "high"))
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "high"))

        self.setUp()                                             # unset: the key is there, null, the record has none
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        [launched] = data["launched"]
        self.assertNotIn("effort", launched)

        self.setUp()                                             # nothing to launch: the key is still there
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario()
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["launched"], data["effort"]), ([], "high"))

    def test_the_dispatchers_claude_effort_is_not_inherited(self):
        # The dispatching Claude Code session exports CLAUDE_EFFORT (its own level) to its Bash tool.
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.assertNotIn("max", call["argv"])

        self.setUp()                                             # headless: the same
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", "--venue", "headless", env=self.env(CLAUDE_EFFORT="max"))
        call = self.headless_call()
        self.assertEqual(call["argv"][8:], ["--effort", "low"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.settle()

        self.setUp()                                             # no setting: the dispatcher's level decides nothing
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("--effort", call["argv"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        [record] = self.run_records()
        self.assertNotIn("effort", record)

    def test_invalid_effort_warns_and_launches_without_the_flag(self):
        for bad in ("turbo", "extra-high", "5"):
            with self.subTest(bad=bad):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT=bad)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
                self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                self.assertNotIn("--effort", call["argv"])
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)    # the ticket still launched

    def test_an_invalid_effort_is_one_warning_per_dispatch_not_per_launch(self):
        self.configure(HARNESS_CODE_EFFORT="turbo")
        self.scenario(issues=[issue(12), issue(13, title="Second")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", "--max", "2")
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
        self.assertIn("#12 bg on", p.stdout)
        self.assertIn("#13 bg on", p.stdout)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                            "/harness:work 12"],
            "proj #13 Second": ["--bg", "-n", "proj #13 Second", "--permission-mode", "auto", "/harness:work 13"]})
        self.assertTrue(all("effort" not in r for r in self.run_records()))

    def test_an_invalid_effort_is_warned_about_on_paths_that_launch_nothing(self):
        bad = "turbo"
        with self.subTest("dry run"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario(issues=[issue(12), issue(13, title="Second")],
                          ready=[ready_row(12), ready_row(13, "Second")])
            p = self.ok("dispatch", "--dry-run", "--max", "2")
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 0 live, 2 of 2 slots free.")
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario()
            p = self.ok("dispatch")
            self.assertIn("No ready tickets.", p.stdout)
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board, invalid value from the environment"):
            self.setUp()
            self.scenario()
            p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="extra-high"))
            self.assertEqual(self.warnings(p), [self.effort_problem("extra-high")], p.stderr)

    def test_with_json_the_invalid_effort_warning_is_on_stderr_only(self):
        for label, args, scenario in (
                ("launch", ("dispatch", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("dry run", ("dispatch", "--dry-run", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("empty board", ("dispatch", "--json"), {})):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="turbo")
                self.scenario(**scenario)
                p = self.ok(*args)
                self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
                self.assertNotIn("HARNESS_CODE_EFFORT", p.stdout)
                data = json.loads(p.stdout)                      # stdout stays pure JSON
                self.assertIn("effort", data)
                self.assertIsNone(data["effort"])
                if label == "launch":
                    [launched] = data["launched"]
                    self.assertNotIn("effort", launched)

    def test_a_valid_effort_is_not_warned_about(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        self.assertEqual(p.stderr.strip(), "")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "medium", "/harness:work 12"))

    def test_the_setting_is_read_from_the_main_checkout_whatever_the_cwd(self):
        # The setting lives only in the main checkout's settings.json. Dispatch run from a subdirectory of it, and
        # from another worktree of the repository (whose own settings.json says otherwise), uses the main one.
        for label in ("subdirectory", "other worktree"):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="xhigh")
                cwd = self.mk("proj", "docs", "deep")
                if label == "other worktree":
                    cwd = os.path.join(self.tmp, "side-wt")
                    self.git(self.repo, "worktree", "add", "-q", "-b", "side", cwd)
                    self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.json")))
                    write(os.path.join(cwd, ".claude", "settings.json"),
                          json.dumps({"env": {"HARNESS_CODE_EFFORT": "low"}}))
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.dispatch_from(cwd)
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh",
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_the_setting_is_found_from_a_subdirectory_for_headless_and_dry_run(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        sub = self.mk("proj", "a", "b")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.dispatch_from(sub, "--dry-run", "--max", "3")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, effort medium, 0 live, 3 of 3 slots free.")
        self.dispatch_from(sub, "--venue", "headless")
        self.assertEqual(self.headless_call()["argv"][8:], ["--effort", "medium"])
        self.settle()

    def test_the_environment_beats_the_file_when_not_blank(self):
        for label, in_env, in_file, want in (("env over file", "low", "xhigh", "low"),
                                            ("env level lower-cased", "MAX", "low", "max"),
                                            ("env padded", "  high ", "low", "high"),
                                            ("env only", "medium", None, "medium")):
            with self.subTest(label):
                self.setUp()
                if in_file is not None:
                    self.configure(HARNESS_CODE_EFFORT=in_file)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=in_env))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", want,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], want)

    def test_a_blank_environment_value_falls_back_to_the_file(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="high")
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=blank))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "high",
                                                                   "/harness:work 12"))

    def test_an_invalid_environment_value_is_warned_about_and_not_rescued_by_the_file(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="turbo"))
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

    def test_settings_local_json_is_never_read(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertIsNone(data["effort"])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

        self.setUp()                                             # nor does it override settings.json
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "low", "/harness:work 12"))

    def test_another_invalid_routing_key_is_its_own_warning_and_leaves_the_effort_alone(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p),
                         ["harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored"],
                         p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(self.run_records()[0]["effort"], "xhigh")

        self.setUp()                                             # three invalid keys: three warnings, routing order
        self.configure(HARNESS_CODE_EFFORT="turbo", HARNESS_REVIEW_TIER="gold", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [
            "harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored",
            self.effort_problem("turbo"),
            "harness-work: HARNESS_REVIEW_TIER='gold' is not one of sonnet, opus, fable; ignored"], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)

    def test_a_redispatched_ticket_gets_the_effort_too(self):
        # #12 is in progress and its last run ended in an error; #13 is fresh from the board.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Fresh")],
                      ready=[ready_row(13, "Fresh")])
        dry = self.ok("dispatch", "--dry-run", "--max", "3")
        self.assertIn("would start #12 Add the board tool (again after error) and launch bg", dry.stdout)
        self.assertEqual(dry.stdout.splitlines()[0], "Dry run: venue bg, effort xhigh, 0 live, 3 of 3 slots free.")

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo57\)$")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls["proj #12 Add the board tool"],
                         self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(calls["proj #13 Fresh"], self.bg_with_effort("proj #13 Fresh", "xhigh", "/harness:work 13"))
        again = next(r for r in self.run_records() if r["ticket"] == 12 and r.get("bg_id") == "redo57")
        self.assertEqual(again["effort"], "xhigh")

    def test_a_redispatched_ticket_launches_headless_with_the_effort(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        self.ok("dispatch", "--venue", "headless")
        argv = self.headless_call()["argv"]
        self.assertEqual((argv[:2], argv[8:]), (["-p", "/harness:work 12"], ["--effort", "high"]))
        self.settle()


class DispatchRouteEffort(WorkTest):
    """Ticket #239: with HARNESS_CODE_EFFORT unset, `harness-work dispatch` launches each ticket at its route's effort:
    a size:L or risk:high ticket gets `--effort xhigh` (bg and headless) and `effort: xhigh` in its run record; any
    other ticket launches as before, with no flag. HARNESS_CODE_EFFORT still wins when set; a HARNESS_CODE_TIER
    override replaces the labels' effort with its tier."""

    configure = DispatchEffort.configure
    bg_with_effort = DispatchEffort.bg_with_effort
    headless_call = DispatchEffort.headless_call
    settle = DispatchEffort.settle

    @staticmethod
    def labels_for(size, risk, kind="task", status="status:ready"):
        return (kind, status) + (("size:" + size,) if size else ()) + (("risk:" + risk,) if risk else ())

    @staticmethod
    def name(n, title):
        return f"proj #{n} {title}"

    def bg_today(self, n, title):
        return ["--bg", "-n", self.name(n, title), "--permission-mode", "auto", f"/harness:work {n}"]

    def test_each_ticket_launches_at_its_routes_effort(self):
        # One dispatch, four tickets: the route's effort per ticket (xhigh for size:L or risk:high, else no flag).
        tickets = [(12, "Small", "task", "S", "low"), (13, "Risky", "task", "M", "high"),
                   (14, "Large", "task", "L", "low"), (15, "Medium bug", "bug", "M", "low")]
        self.scenario(issues=[issue(n, labels=self.labels_for(s, r, k), title=t) for n, t, k, s, r in tickets],
                      ready=[ready_row(n, t, labels=self.labels_for(s, r, k)) for n, t, k, s, r in tickets])
        p = self.ok("dispatch", "--max", "4")
        self.assertNotIn("harness-work:", p.stderr)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        records = {r["ticket"]: r for r in self.run_records()}
        for n, title, kind, size, risk in tickets:
            effort = route_of(kind, size, risk)[1]
            with self.subTest(ticket=n, size=size, risk=risk):
                if effort:
                    self.assertEqual(calls[self.name(n, title)],
                                     self.bg_with_effort(self.name(n, title), effort, f"/harness:work {n}"))
                    self.assertEqual(records[n]["effort"], effort)
                else:
                    self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                    self.assertNotIn("effort", records[n])

    def test_a_size_l_ticket_launches_bg_with_effort_xhigh(self):
        large = self.labels_for("L", "low")
        self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
        data = self.ok_json("dispatch", "--json")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        [record] = self.run_records()
        self.assertEqual(record["effort"], "xhigh")
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "xhigh"))

    def test_a_risk_high_ticket_launches_headless_with_effort_xhigh(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        risky = self.labels_for("S", "high")
        self.scenario(issues=[issue(labels=risky)], ready=[ready_row(12, labels=risky)])
        self.ok("dispatch", "--venue", "headless", env=self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}"))
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:2], ["-p", "/harness:work 12"])
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.settle()

    def test_the_code_effort_setting_wins_over_the_route(self):
        # Unset: each ticket at its route's effort. Set (file or environment): every ticket at the setting.
        tickets = [(12, "Small", "S", "low"), (13, "Large", "L", "high")]
        for level, where in ((None, None), ("low", "settings.json"), ("max", "environment"),
                             ("medium", "settings.json")):
            with self.subTest(level=level, where=where):
                self.setUp()
                self.scenario(issues=[issue(n, labels=self.labels_for(s, r), title=t) for n, t, s, r in tickets],
                              ready=[ready_row(n, t, labels=self.labels_for(s, r)) for n, t, s, r in tickets])
                env = None
                if where == "settings.json":
                    self.configure(HARNESS_CODE_EFFORT=level)
                elif where == "environment":
                    env = self.env(HARNESS_CODE_EFFORT=level)
                self.ok("dispatch", "--max", "2", env=env)
                calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
                records = {r["ticket"]: r for r in self.run_records()}
                for n, title, size, risk in tickets:
                    want = level or route_of("task", size, risk)[1]
                    if want:
                        self.assertEqual(calls[self.name(n, title)],
                                         self.bg_with_effort(self.name(n, title), want, f"/harness:work {n}"))
                        self.assertEqual(records[n]["effort"], want)
                    else:
                        self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                        self.assertNotIn("effort", records[n])

    def test_a_tier_override_replaces_the_labels_effort(self):
        # HARNESS_CODE_TIER replaces the label rubric, its effort included: a size:L ticket launches with no flag
        # unless HARNESS_CODE_EFFORT is set too. Without the override the same ticket launches at xhigh.
        large = self.labels_for("L", "high")
        for tier in (None, "opus", "sonnet", "fable"):
            with self.subTest(tier=tier):
                self.setUp()
                if tier:
                    self.configure(HARNESS_CODE_TIER=tier)
                self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                if tier:
                    self.assertEqual(call["argv"], self.bg_today(12, TITLE))
                    self.assertNotIn("effort", self.run_records()[0])
                else:
                    self.assertEqual(call["argv"], self.bg_with_effort(self.name(12, TITLE), "xhigh",
                                                                       "/harness:work 12"))
                    self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_a_redispatched_size_l_ticket_gets_xhigh(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        large = self.labels_for("L", "low", status="status:in-progress")
        self.scenario(issues=[issue(12, labels=large)])
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="redo58"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo58\)$")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        again = next(r for r in self.run_records() if r.get("bg_id") == "redo58")
        self.assertEqual(again["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
