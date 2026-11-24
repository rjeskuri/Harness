"""harness-work dispatch: a dry run stops no session, --only, a resumed ticket's prompt and --after.

Split from test_work_dispatch.py (#447: that module alone came near the 180 s a module may take); the fixtures are in
work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_dispatch_only.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


# =========================================================================== #335: a dry run stops no session
WOULD_STOP_RE = re.compile(r"would stop session ([A-Za-z0-9_-]+) \(#(\d+) [^)\n]+\)")


class DispatchDryRunStopsNothing(RowsInProcess, CopiedProject, WorkTest):
    """#335: `dispatch --dry-run` runs dispatch's sweep without its effects: a finished ticket's bg session is not
    stopped and its record not written, a `would stop session <id> (#N <state>)` line says what a real dispatch would
    do, and the plan counts the slot as freed. A real dispatch and `runs --sweep` still stop and close."""

    # Each fixture: finished {n: (bg id, agents state, labels, issue state, PRs)}, live {n: (bg id, agents state,
    # PRs)} of status:in-progress tickets, the ready tickets in board order, and --max.
    FIXTURES = (
        # in review without an open ready PR: a ready one is before the land, not finished (#422)
        {"finished": {301: ("s-fin1", "blocked", IN_REVIEW, "OPEN", draft(401)),
                      302: ("s-fin2", "idle", NEEDS_INPUT, "OPEN", []),
                      303: ("s-fin3", "done", IN_REVIEW, "CLOSED", MERGED)},
         "live": {304: ("s-run4", "running", [])},
         "ready": [310, 311, 312, 313, 314, 315], "max": 3},
        {"finished": {57: ("s-q57", "blocked", IN_PROGRESS, "CLOSED", MERGED)},
         "live": {58: ("s-b58", "blocked", []), 59: ("s-i59", "idle", draft(459))},
         "ready": [60, 61, 62, 63, 64], "max": 4},
        {"finished": {88: ("s-x88", "blocked", NEEDS_INPUT, "OPEN", draft(488)),
                      89: ("s-y89", "blocked", IN_REVIEW, "OPEN",
                           [{"number": 489, "state": "CLOSED", "isDraft": False}])},
         "live": {},
         "ready": [90, 91, 92], "max": 2},
    )

    def setup_fixture(self, fx):
        """Unended bg records for every session, the `claude agents` listing and the gh scenario. Returns env."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.claude_log, self.gh_log):
            if os.path.exists(path):
                os.unlink(path)
        sessions = {n: (c[0], c[1]) for n, c in fx["finished"].items()}
        sessions.update({n: (c[0], c[1]) for n, c in fx["live"].items()})
        for n, (bg_id, _state) in sessions.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        listing = [{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo, "name": f"proj #{n} {TITLE}"}
                   for n, (bg_id, state) in sessions.items()]
        tickets = {n: (c[2], c[3], c[4]) for n, c in fx["finished"].items()}
        tickets.update({n: (IN_PROGRESS, "OPEN", c[2]) for n, c in fx["live"].items()})
        calls = [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels=labels, state=state)}
                 for n, (labels, state, _prs) in tickets.items()]
        calls += [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, title=f"Ready {n}")}
                  for n in fx["ready"]]
        for n, (_labels, _state, prs) in tickets.items():
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x", "--state", "all"], "stdout_json": prs})
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x"],
                          "stdout_json": [pr for pr in prs if pr["state"] == "OPEN"]})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": [ready_row(n, f"Ready {n}") for n in fx["ready"]]},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        self.put_scenario({"calls": calls})
        return self.env(FAKECLAUDE_AGENTS=json.dumps(listing), **CREDENTIALS)

    def runs_snapshot(self):
        """{file name: bytes} of everything in the runs directory."""
        folder = self.runs_dir()
        out = {}
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), "rb") as fh:
                out[name] = fh.read()
        return out

    def stops(self):
        """{session id: times `claude stop` was called with it}, from the fake claude's argv log."""
        counts = {}
        for call in self.claude_calls():
            if call["argv"][:1] == ["stop"]:
                for arg in call["argv"][1:]:
                    if not arg.startswith("-"):
                        counts[arg] = counts.get(arg, 0) + 1
        return counts

    def bg_launches(self):
        return sorted(c["argv"][-1] for c in self.claude_calls() if "--bg" in c["argv"])

    def record_id(self, n):
        return f"{self.key}-{n}-20261013120000"

    @staticmethod
    def expected_plan(fx):
        """What today's dry run plans: the finished sessions hold no slot, the live ones do."""
        free = fx["max"] - len(fx["live"])
        return fx["ready"][:free], fx["ready"][free:], free

    # ---- DoD 1: no `claude stop`, the runs directory byte-identical, a `would stop` line per finished session
    def test_dry_run_stops_no_session_writes_no_record_and_says_what_it_would_stop(self):
        for i, fx in enumerate(self.FIXTURES):
            for as_json in (False, True):
                with self.subTest(fixture=i, json=as_json):
                    env = self.setup_fixture(fx)
                    before = self.runs_snapshot()
                    self.assertEqual(len(before), len(fx["finished"]) + len(fx["live"]))
                    args = ["dispatch", "--dry-run", "--max", str(fx["max"])] + (["--json"] if as_json else [])
                    p = self.work(*args, env=env)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 0, out)
                    self.assertEqual(self.stops(), {}, out)
                    self.assertEqual([c["argv"] for c in self.claude_calls() if c["argv"][:1] != ["agents"]], [],
                                     out)
                    self.assertEqual(self.runs_snapshot(), before, out)
                    self.assertEqual(re.findall(STOPPED_RE, out), [], out)
                    expected = sorted((c[0], str(n)) for n, c in fx["finished"].items())
                    self.assertEqual(sorted(WOULD_STOP_RE.findall(out)), expected, out)    # none for a live one
                    if as_json:
                        json.loads(p.stdout)        # the would-stop lines leave stdout whole

    def test_a_second_dry_run_sees_the_same_sessions_and_still_stops_none(self):
        fx = self.FIXTURES[0]
        env = self.setup_fixture(fx)
        before = self.runs_snapshot()
        for _ in range(2):
            p = self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)
            self.assertEqual(sorted(WOULD_STOP_RE.findall(p.stdout + p.stderr)),
                             sorted((c[0], str(n)) for n, c in fx["finished"].items()), p.stdout + p.stderr)
        self.assertEqual(self.stops(), {})
        self.assertEqual(self.runs_snapshot(), before)
        self.assertEqual(self.bg_launches(), [])

    # ---- DoD 2: the plan and slot count of today's dry run, the finished sessions' slots freed
    def test_dry_run_plans_as_if_the_finished_sessions_had_been_stopped(self):
        for i, fx in enumerate(self.FIXTURES):
            plan, over, free = self.expected_plan(fx)
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(fx["max"]), "--json", env=env)
                self.assertEqual(sorted(data["live"]), sorted(self.record_id(n) for n in fx["live"]), data)
                self.assertEqual([row["number"] for row in data["plan"]], plan, data)
                self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                                 [(n, f"over --max {fx['max']}") for n in over], data)

                p = self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)
                self.assertIn(f"Dry run: venue bg, {len(fx['live'])} live, {free} of {fx['max']} slots free.",
                              p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual([int(re.match(r"would start #(\d+)", ln).group(1)) for ln in starts], plan,
                                 p.stdout)
                self.assertEqual(self.stops(), {})

    # ---- DoD 3: the same fixture without --dry-run stops and closes as today, and launches into the freed slots
    def test_a_real_dispatch_stops_the_finished_sessions_and_closes_their_records(self):
        for i, fx in enumerate(self.FIXTURES):
            plan, _over, _free = self.expected_plan(fx)
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)    # changes nothing
                self.assertEqual(self.stops(), {})
                p = self.work("dispatch", "--max", str(fx["max"]), env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                finished_ids = sorted(c[0] for c in fx["finished"].values())
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished_ids}, out)
                self.assertEqual(sorted(re.findall(STOPPED_RE, out)), finished_ids, out)
                self.assertEqual(WOULD_STOP_RE.findall(out), [], out)
                saved = {r["id"]: r for r in self.run_records()}
                for n in fx["finished"]:
                    record = saved[self.record_id(n)]
                    self.assertTrue(record["ended_at"], record)
                    self.assertEqual(record["outcome"], "ok", record)
                for n in fx["live"]:
                    self.assertIsNone(saved[self.record_id(n)]["ended_at"], saved[self.record_id(n)])
                self.assertEqual(self.bg_launches(), sorted(f"/harness:work {n}" for n in plan), out)

    # ---- outline 2: `runs --sweep` is unchanged
    def test_runs_sweep_still_stops_the_finished_sessions_and_closes_their_records(self):
        for i, fx in enumerate(self.FIXTURES):
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                p = self.work("runs", "--sweep", env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                finished_ids = sorted(c[0] for c in fx["finished"].values())
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished_ids}, out)
                self.assertEqual(sorted(re.findall(STOPPED_RE, out)), finished_ids, out)
                self.assertEqual(WOULD_STOP_RE.findall(out), [], out)
                saved = {r["id"]: r for r in self.run_records()}
                for n in fx["finished"]:
                    self.assertTrue(saved[self.record_id(n)]["ended_at"])
                for n in fx["live"]:
                    self.assertIsNone(saved[self.record_id(n)]["ended_at"])


# =========================================================================== dispatch --only N[,N...] (#336)
class DispatchOnly(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --only` plans only the named tickets, through the normal path: run record, start, launch."""

    TITLES = {5: "Five", 7: "Seven", 9: "Nine", 11: "Eleven"}

    def board(self, ready=(5, 7, 9), extra_issues=()):
        titles = self.TITLES
        self.scenario(issues=[issue(n, title=titles[n]) for n in ready] + list(extra_issues),
                      ready=[ready_row(n, titles[n]) for n in ready])

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def launched_tickets(self):
        return sorted(int(c["argv"][-1].split()[1]) for c in self.claude_calls())

    def started_tickets(self):
        """Tickets whose start comment or status edit reached the board."""
        return {int(argv[2]) for argv in self.gh_calls() if argv[:2] in (["issue", "edit"], ["issue", "comment"])}

    def assertSaysWhy(self, p, n):
        text = p.stdout + p.stderr
        self.assertNotIn("unrecognized arguments", text)    # an unknown argument is not a reason
        self.assertRegex(text, rf"#{n}\b", text)

    def test_only_one_of_three_ready_tickets_launches_that_one_with_its_run_record(self):
        self.board()
        p = self.ok("dispatch", "--only", "7", env=self.env(FAKECLAUDE_BG_ID="only7", **CREDENTIALS))
        self.assertRegex(p.stdout, r"(?m)^#7 bg on t/7-\S+ \(id only7\)$")
        self.assertNotRegex(p.stdout, r"#5\b|#9\b")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #7 Seven", "--permission-mode", "auto",
                                        "/harness:work 7"])
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"]),
                         (7, "bg", "only7", None))
        self.assertTrue(record["id"].startswith(f"{self.key}-7-"), record["id"])
        self.assertTrue(record["branch"].startswith("t/7-"), record["branch"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(record["worktree"]))
        self.assertEqual(self.started_tickets(), {7})     # the start comment and status edit: #7 only
        self.assertTrue(os.path.isdir(record["worktree"]))

    def test_a_different_named_ticket_is_the_one_launched(self):
        self.board()
        self.ok("dispatch", "--only", "9", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [9])
        self.assertEqual([r["ticket"] for r in self.run_records()], [9])
        self.assertEqual(self.started_tickets(), {9})

    def test_only_with_a_comma_list_launches_exactly_those_tickets(self):
        self.board(ready=(5, 7, 9, 11))
        p = self.ok("dispatch", "--only", "11,5", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [5, 11])
        self.assertEqual(sorted(r["ticket"] for r in self.run_records()), [5, 11])
        self.assertEqual(self.started_tickets(), {5, 11})
        for n in (5, 11):
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on ")
        self.assertNotRegex(p.stdout, r"#7\b|#9\b")

    def test_only_a_ticket_that_is_not_ready_launches_nothing_and_says_why(self):
        proposed = issue(8, labels=("task", "status:proposed", "size:S", "risk:low"), title="Proposed")
        waiting = issue(10, labels=NEEDS_INPUT, title="Waiting")
        working = issue(13, labels=IN_PROGRESS, title="Working")        # in progress, no run record at all
        for n in (8, 10, 13, 99):                                        # 99 is not on the board
            with self.subTest(only=n):
                self.board(extra_issues=[proposed, waiting, working])
                p = self.work("dispatch", "--only", str(n), env=self.env(**CREDENTIALS))
                self.assertSaysWhy(p, n)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(self.run_records(), [])
                self.assertEqual(self.started_tickets(), set())
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_only_a_ticket_with_a_live_run_launches_nothing_and_says_why(self):
        self.write_run(13, pid=self.sleeper(13))
        self.board(extra_issues=[issue(13, labels=IN_PROGRESS, title="Working")])
        before = self.run_records()
        p = self.work("dispatch", "--only", "13", env=self.env(**CREDENTIALS))
        self.assertSaysWhy(p, 13)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), before)
        self.assertEqual(self.started_tickets(), set())

    def test_a_ready_ticket_that_is_live_is_not_launched_again_but_the_other_named_one_is(self):
        self.write_run(5, pid=self.sleeper(5))
        self.board()
        p = self.work("dispatch", "--only", "5,7", env=self.env(**CREDENTIALS))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertSaysWhy(p, 5)
        self.assertEqual(self.launched_tickets(), [7])
        self.assertEqual(self.started_tickets(), {7})

    def test_a_not_ready_name_does_not_stop_the_ready_one_beside_it(self):
        self.board(extra_issues=[issue(8, labels=("task", "status:proposed", "size:S", "risk:low"))])
        p = self.work("dispatch", "--only", "8,9", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [9])
        self.assertSaysWhy(p, 8)
        self.assertEqual([r["ticket"] for r in self.run_records()], [9])
        self.assertEqual(self.started_tickets(), {9})

    def test_only_redispatches_an_in_progress_ticket_whose_run_ended_without_a_pr(self):
        for n in (13, 14):
            self.write_run(n, pid=self.dead_pid(), started_at=iso_ago(hours=3), ended_at=iso_ago(hours=2),
                           outcome="error")
        self.board(extra_issues=[issue(13, labels=IN_PROGRESS, title="Working"),
                                 issue(14, labels=IN_PROGRESS, title="Other")])
        p = self.ok("dispatch", "--only", "13", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, r"(?m)^#13 bg on ")
        self.assertEqual(self.launched_tickets(), [13])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual([r["ticket"] for r in fresh], [13])     # #14 ended too, but was not named
        self.assertNotIn(14, self.started_tickets())             # nothing on the board touches #14
        self.assertTrue(os.path.isdir(fresh[0]["worktree"]))
        worktrees = os.path.join(self.repo, ".claude", "worktrees")
        self.assertEqual([d for d in os.listdir(worktrees) if d.startswith("t-14-")], [])

    def test_only_with_dry_run_plans_the_named_tickets_and_starts_nothing(self):
        self.board(ready=(5, 7, 9, 11))
        p = self.ok("dispatch", "--dry-run", "--only", "7,11")
        starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
        self.assertEqual(sorted(ln.split()[2] for ln in starts), ["#11", "#7"], p.stdout)
        for n in (5, 9):
            self.assertNotRegex(p.stdout, rf"#{n}\b")
        data = self.ok_json("dispatch", "--dry-run", "--only", "7,11", "--json")
        self.assertEqual(sorted(row["number"] for row in data["plan"]), [7, 11])
        self.assertTrue(data["dry_run"])
        data = self.ok_json("dispatch", "--dry-run", "--only", "9", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [9])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), [])
        self.assertEqual(self.started_tickets(), set())
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_only_with_dry_run_says_why_a_named_ticket_is_not_planned(self):
        self.write_run(13, pid=self.sleeper(13))
        self.board(extra_issues=[issue(8, labels=("task", "status:proposed", "size:S", "risk:low")),
                                 issue(13, labels=IN_PROGRESS, title="Working")])
        for n in (8, 13):
            with self.subTest(only=n):
                p = self.work("dispatch", "--dry-run", "--only", f"{n},5")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn(f"would start #{n}", p.stdout)
                self.assertIn("would start #5", p.stdout)
                self.assertSaysWhy(p, n)
                data = json.loads(self.work("dispatch", "--dry-run", "--only", f"{n},5", "--json").stdout)
                self.assertEqual([row["number"] for row in data["plan"]], [5])
                self.assertIn(n, [s["number"] for s in data["skipped"]])
        self.assertEqual(self.claude_calls(), [])


# =========================================================================== dispatch resumes an answered ticket (#367)
class DispatchResume(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --only N` plans a status:needs-input ticket the owner has answered (harness-board answers N), as
    `cloud` does, and confirms each launch: the session's own `harness-work start` leaves a "Started on" comment, and
    a launch that shows none within HARNESS_START_WAIT seconds (default 60) ends its run record in error. The fake
    claude (work_support) posts that comment when it is launched, unless FAKECLAUDE_NO_START is set."""

    SKIP = "not ready, and not in progress with an ended run to redispatch"
    STARTED_EARLIER = "Started on t/{n}-first-run (host, bg)" + MARK

    def needs_input(self, n, *replies, title="Resumed"):
        comments = [comment(ME, iso_ago(hours=5), self.STARTED_EARLIER.format(n=n)),
                    comment(ME, iso_ago(hours=3), QUESTION)] + list(replies)
        self.scenario(issues=[issue(n, labels=NEEDS_INPUT, title=title, comments=comments)])

    def edits(self, n):
        return [a for a in self.gh_calls() if a[:3] == ["issue", "edit", str(n)]]

    def test_only_launches_an_answered_needs_input_ticket_with_a_run_record(self):
        cases = ((12, [comment(ME, iso_ago(hours=1), "Use main.")]),
                 (40, [comment("drive-by-user", iso_ago(hours=2), "Me too"),
                       comment(ME.upper(), iso_ago(minutes=5), "The --fast one."),
                       comment(ME, iso_ago(minutes=4), "Note to self." + MARK)]))
        for n, replies in cases:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)
                self.needs_input(n, *replies, title=f"Resumed {n}")
                bg_id = f"resume{n}"
                p = self.ok("dispatch", "--only", str(n), env=self.env(FAKECLAUDE_BG_ID=bg_id, **CREDENTIALS))
                self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id {bg_id}\)$")
                self.assertNotIn(self.SKIP, p.stdout + p.stderr)
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} Resumed {n}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"],
                                  record["outcome"]), (n, "bg", bg_id, None, None))
                self.assertTrue(record["id"].startswith(f"{self.key}-{n}-"), record["id"])
                self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(record["worktree"]))
                moved = [a for a in self.edits(n) if "status:in-progress" in a and "status:needs-input" in a]
                self.assertTrue(moved, self.edits(n))
                self.assertTrue([a for a in self.gh_calls() if a[:3] == ["issue", "comment", str(n)]
                                 and "Started on" in " ".join(a)])

    def test_only_still_skips_a_needs_input_ticket_without_an_answer_since_its_question(self):
        question = comment(ME, iso_ago(hours=3), QUESTION)
        shapes = {"no reply at all": [question],
                  "only the harness's own note": [question, comment(ME, iso_ago(hours=1), "Reminder." + MARK)],
                  "a reply before the question": [comment(ME, iso_ago(hours=4), "Use main."), question],
                  "a reply by someone else": [question, comment("drive-by-user", iso_ago(hours=1), "Use main.")]}
        for shape, comments in shapes.items():
            with self.subTest(shape):
                self.scenario(issues=[issue(12, labels=NEEDS_INPUT, comments=comments)])
                p = self.ok("dispatch", "--only", "12", env=self.env(**CREDENTIALS))
                self.assertIn(f"skip #12: {self.SKIP}", p.stdout + p.stderr)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(self.run_records(), [])
                self.assertFalse([a for a in self.gh_calls() if a[:2] in (["issue", "edit"], ["issue", "comment"])])
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_launch_whose_session_never_starts_ends_its_record_in_error_and_the_next_dispatch_retries(self):
        self.needs_input(12, comment(ME, iso_ago(hours=1), "Use main."))   # an older "Started on" is not this launch's
        began = time.monotonic()
        p = self.work("dispatch", "--only", "12", env=self.env(FAKECLAUDE_NO_START="1", HARNESS_START_WAIT="2",
                                                               FAKECLAUDE_BG_ID="never12", **CREDENTIALS))
        took = time.monotonic() - began
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        launch, stop = self.claude_calls()                               # the launch, then the stop of that session
        self.assertEqual(launch["argv"][0], "--bg")
        self.assertEqual(stop["argv"], ["stop", "never12"])
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["outcome"]), (12, "error"))
        self.assertEqual(record["bg_id"], "never12")
        self.assertTrue(record["ended_at"])
        self.assertTrue(record.get("error") and "\n" not in record["error"], record)
        self.assertIn("#12", p.stderr)
        self.assertIn(record["error"], p.stderr)                         # it says so
        self.assertGreaterEqual(took, 1.5)                                # it waited for the session before giving up
        self.assertLess(took, 30)
        # the ended record is what the next dispatch retries: a healthy session now starts and the run goes on
        self.ok("dispatch", "--only", "12", env=self.env(HARNESS_START_WAIT="10", FAKECLAUDE_BG_ID="again12",
                                                         **CREDENTIALS))
        records = self.run_records()
        self.assertEqual(len(records), 2)
        newest = max(records, key=lambda r: r["started_at"] + r["id"])
        self.assertEqual((newest["bg_id"], newest["ended_at"], newest["outcome"]), ("again12", None, None))
        calls = self.claude_calls()                                       # launch, stop, launch again: no second stop
        self.assertEqual([c["argv"][0] for c in calls], ["--bg", "stop", "--bg"])

    def test_every_launch_that_never_starts_is_stopped_after_one_shared_wait(self):
        issues = [issue(n, labels=NEEDS_INPUT, title=f"Resumed {n}", comments=[
            comment(ME, iso_ago(hours=5), self.STARTED_EARLIER.format(n=n)), comment(ME, iso_ago(hours=3), QUESTION),
            comment(ME, iso_ago(hours=1), "Use main.")]) for n in (12, 13)]
        self.scenario(issues=issues)
        wait = 8                                                          # big enough that the fixed overhead is small beside it
        began = time.monotonic()
        p = self.work("dispatch", "--only", "12,13", env=self.env(FAKECLAUDE_NO_START="1", FAKECLAUDE_BG_ID="never",
                                                                  HARNESS_START_WAIT=str(wait), **CREDENTIALS))
        took = time.monotonic() - began
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreaterEqual(took, wait - 0.5)
        self.assertLess(took, 2 * wait - 1)                               # waits in turn take at least 2 * wait plus the overhead
        records = self.run_records()
        self.assertEqual(sorted((r["ticket"], r["outcome"]) for r in records), [(12, "error"), (13, "error")])
        self.assertTrue(all(r["ended_at"] for r in records), records)
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls if c["argv"][0] == "--bg"),
                         ["/harness:work 12", "/harness:work 13"])
        self.assertEqual([c["argv"] for c in calls if c["argv"][0] == "stop"], [["stop", "never"]] * 2)
        for n in (12, 13):
            self.assertIn(f"#{n}", p.stderr)

    def test_a_headless_run_that_exits_without_starting_is_left_for_the_sweep_to_classify(self):
        self.scenario(issues=[issue(12)], ready=[ready_row(12)])
        env = self.env(FAKECLAUDE_NO_START="1", FAKECLAUDE_MODE="billing", HARNESS_START_WAIT="20", **CREDENTIALS)
        began = time.monotonic()
        p = self.work("dispatch", "--venue", "headless", env=env)
        self.assertLess(time.monotonic() - began, 15)                     # it saw the exit, it did not wait it out
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertRegex(p.stderr, r"#12 not launched: the session exited before it posted")
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["venue"], record["ended_at"], record["outcome"]),
                         (12, "headless", None, None))                    # not ended by dispatch
        self.assertEqual([c["argv"][0] for c in self.claude_calls()], ["-p"])   # nothing to stop
        self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertTrue(swept["ended_at"])
        self.assertNotEqual(swept["outcome"], "ok")
        self.assertTrue(self.read_json(self.budgets_path())["api"]["exhausted_until"])   # billing marked the budget
        self.assertIn("api: exhausted", self.ok("budget").stdout)

    def test_a_session_that_starts_is_never_failed_by_the_wait(self):
        self.scenario(issues=[issue(7, title="Seven")], ready=[ready_row(7, "Seven")])
        began = time.monotonic()
        self.ok("dispatch", "--only", "7", env=self.env(HARNESS_START_WAIT="30", **CREDENTIALS))
        self.assertLess(time.monotonic() - began, 20)                     # a seen "Started on" ends the wait at once
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["ended_at"], record["outcome"]), (7, None, None))


# =========================================================================== dispatch --after N (#389)
def blocked_row(number, title, blockers=(), labels=READY):
    """A ready row as `gh issue list` gives it, its blockers all closed (harness-board ready drops any other)."""
    return {"number": number, "title": title, "labels": [{"name": n} for n in labels],
            "blockedBy": [{"number": b, "state": "CLOSED"} for b in blockers]}


class DispatchAfter(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --after N` launches the ready tickets whose blockers include N, or else the first ready ticket
    without a live run, through --only's path (slots, run records, start comments, --dry-run)."""

    # ticket: (title, its closed blockers)
    BOARD = {5: ("Five", ()), 11: ("Eleven", (10,)), 12: ("Twelve", (8, 10)), 13: ("Thirteen", (9,))}

    def board(self, numbers=(5, 11, 12, 13), high=()):
        rows, issues = [], []
        for n in numbers:
            title, blockers = self.BOARD[n]
            labels = READY + (("priority:high",) if n in high else ())
            rows.append(blocked_row(n, title, blockers, labels))
            issues.append(issue(n, labels=labels, title=title,
                                blocked_by=[{"number": b, "state": "CLOSED"} for b in blockers]))
        self.scenario(issues=issues, ready=rows)

    sleeper = DispatchOnly.sleeper
    launched_tickets = DispatchOnly.launched_tickets
    started_tickets = DispatchOnly.started_tickets

    def assertNothingStarted(self, records_before=()):
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.started_tickets(), set())
        self.assertEqual(sorted(r["id"] for r in self.run_records()), sorted(records_before))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_after_launches_unblocked_tickets(self):
        # #12 waits on #8 and #10, #13 on #9, #5 on nothing: only the tickets that #N blocked are launched
        for after, unblocked, line in ((10, [11, 12], "after #10: #11, #12 unblocked"),
                                       (9, [13], "after #9: #13 unblocked")):
            with self.subTest(after=after):
                self.setUp()
                self.board()
                p = self.ok("dispatch", "--after", str(after), env=self.env(**CREDENTIALS))
                self.assertIn(line, p.stdout.splitlines())
                for n in unblocked:
                    self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ ")
                self.assertEqual(self.launched_tickets(), unblocked)
                self.assertEqual(sorted(r["ticket"] for r in self.run_records()), unblocked)
                self.assertEqual(self.started_tickets(), set(unblocked))
            with self.subTest(after=after, json=True):
                self.setUp()
                self.board()
                data = self.ok_json("dispatch", "--after", str(after), "--json", env=self.env(**CREDENTIALS))
                self.assertEqual(data["after"], {"ticket": after, "unblocked": unblocked, "next": None})
                self.assertEqual(sorted(r["ticket"] for r in data["launched"]), unblocked)
                self.assertEqual(self.launched_tickets(), unblocked)

    def test_after_falls_back_to_next_ready(self):
        # nothing on the board waited on #20: exactly one launch, the first ready ticket in ready's order not live
        for live, high, expected in (((), (), 5), ((5,), (), 11), ((), (13,), 13)):   # priority:high leads ready
            with self.subTest(live=live, high=high):
                self.setUp()
                before = [self.write_run(n, pid=self.sleeper(n))["id"] for n in live]
                self.board((5, 11, 13), high)
                p = self.ok("dispatch", "--after", "20", env=self.env(**CREDENTIALS))
                self.assertIn(f"after #20: none unblocked; next ready #{expected}", p.stdout.splitlines())
                self.assertEqual(self.launched_tickets(), [expected])
                self.assertEqual(self.started_tickets(), {expected})
                self.assertEqual([r["ticket"] for r in self.run_records() if r["id"] not in before], [expected])
            with self.subTest(live=live, high=high, json=True):
                self.setUp()
                for n in live:
                    self.write_run(n, pid=self.sleeper(n))
                self.board((5, 11, 13), high)
                data = self.ok_json("dispatch", "--after", "20", "--json", env=self.env(**CREDENTIALS))
                self.assertEqual(data["after"], {"ticket": 20, "unblocked": [], "next": expected})
                self.assertEqual([r["ticket"] for r in data["launched"]], [expected])
        with self.subTest(ready="none"):            # no ready ticket: nothing launched, exit 0
            self.setUp()
            self.board(())
            p = self.ok("dispatch", "--after", "20", env=self.env(**CREDENTIALS))
            self.assertIn("after #20: nothing ready", p.stdout.splitlines())
            self.assertNothingStarted()
            data = self.ok_json("dispatch", "--after", "20", "--json", env=self.env(**CREDENTIALS))
            self.assertEqual(data["after"], {"ticket": 20, "unblocked": [], "next": None})
            self.assertEqual(data["launched"], [])
            self.assertNothingStarted()

    def test_after_respects_slots_and_dry_run(self):
        # --max filled by live runs on other tickets: nothing launched, and it says so, unblocked or not
        for after in (10, 20):
            with self.subTest(after=after, live=2, max=2):
                self.setUp()
                before = [self.write_run(n, pid=self.sleeper(n))["id"] for n in (40, 41)]
                self.board()
                p = self.ok("dispatch", "--after", str(after), "--max", "2", env=self.env(**CREDENTIALS))
                self.assertIn("--max 2 reached", p.stdout)
                self.assertNothingStarted(before)
        with self.subTest(after=10, live=1, max=2):         # one free slot: the first unblocked ticket only
            self.setUp()
            self.write_run(40, pid=self.sleeper(40))
            self.board()
            self.ok("dispatch", "--after", "10", "--max", "2", env=self.env(**CREDENTIALS))
            self.assertEqual(self.launched_tickets(), [11])
        # --dry-run plans what a real run would launch and launches nothing
        for after, plan, unblocked, nxt in ((10, [11, 12], [11, 12], None), (20, [5], [], 5)):
            with self.subTest(after=after, dry_run=True):
                self.setUp()
                self.board()
                p = self.ok("dispatch", "--after", str(after), "--dry-run")
                starts = sorted(int(ln.split()[2][1:]) for ln in p.stdout.splitlines()
                                if ln.startswith("would start #"))
                self.assertEqual(starts, plan, p.stdout)
                data = self.ok_json("dispatch", "--after", str(after), "--dry-run", "--json")
                self.assertTrue(data["dry_run"])
                self.assertEqual(sorted(row["number"] for row in data["plan"]), plan)
                self.assertEqual(data["after"], {"ticket": after, "unblocked": unblocked, "next": nxt})
                self.assertNothingStarted()

    def test_after_with_only_is_an_error(self):
        self.board()
        p = self.ok("dispatch", "--after", "10", "--dry-run")           # the control: --after alone is accepted
        self.assertIn("would start #11", p.stdout)
        for flags in (("--after", "10", "--only", "11"), ("--only", "11", "--after", "10")):
            for extra in ((), ("--dry-run",)):
                with self.subTest(flags=flags, extra=extra):
                    p = self.work("dispatch", *flags, *extra, env=self.env(**CREDENTIALS))
                    self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertNotIn("unrecognized", p.stderr)
                    self.assertIn("--after", p.stderr)
                    self.assertIn("--only", p.stderr)
                    self.assertNotIn("would start", p.stdout)
                    self.assertNothingStarted()


# =========================================================================== #491: the landing session's own sweep
LANDED = ("task", "size:S", "risk:low")          # a ticket the land closed: its status labels removed


def session_uuid(short):
    """A full session id whose short id (a bg run record's bg_id) is `short`."""
    return f"{short}-4e1f-4a2b-9c3d-5e6f7a8b9c0d"


class DispatchAfterFromTheLandingSession(RowsInProcess, CopiedProject, WorkTest):
    """#491: `harness-pr land N --loop` runs `dispatch --after N` inside ticket N's own bg session, whose run record
    names that session. Dispatch's sweep must not `claude stop` the session it runs in (that killed the land and its
    dispatch before anything launched): it leaves that record open for a later sweep and starts the ready ticket N
    unblocked. The session is known from CLAUDE_CODE_SESSION_ID (the full id) or CLAUDE_JOB_DIR
    (~/.claude/jobs/<short id>), either one alone. Another finished ticket's session is still stopped and closed."""

    # (landed N, its session's short id, ready M blocked only by N, another finished ticket K, K's session)
    CASES = ((12, "95171f6f", 13, 30, "a0b1c2d3"),
             (475, "c103ebd9", 477, 451, "5e6f7a8b"),
             (447, "0d9e8f7a", 468, 452, "b4c5d6e7"))
    # which of the two variables name the current session
    IDENTITIES = ("both", "session id", "job dir")

    stops = DispatchDryRunStopsNothing.stops
    started_tickets = DispatchOnly.started_tickets

    def bg_launched(self):
        """The tickets a `claude --bg` was run for, from the fake claude's argv log."""
        out = []
        for call in self.claude_calls():
            if "--bg" in call["argv"]:
                found = re.search(r"/harness:work (\d+)", " ".join(call["argv"]))
                out.append(int(found.group(1)) if found else None)
        return sorted(out)

    def record_of(self, n):
        return next(r for r in self.run_records() if r["ticket"] == n)

    def landed_board(self, n, short, m, k, k_short):
        """N closed by its land, its bg session `running` (the land); K closed earlier, its session `blocked`; M
        ready, blocked only by N. Returns the environment of no session."""
        self.write_run(n, venue="bg", bg_id=short, log=None, branch=f"t/{n}-x",
                       id=f"{self.key}-{n}-20261124100000")
        self.write_run(k, venue="bg", bg_id=k_short, log=None, branch=f"t/{k}-x",
                       id=f"{self.key}-{k}-20261124090000")
        listing = [{"id": short, "kind": "background", "state": "running", "cwd": self.repo,
                    "name": f"proj #{n} {TITLE}"},
                   {"id": k_short, "kind": "background", "state": "blocked", "cwd": self.repo,
                    "name": f"proj #{k} {TITLE}"}]
        calls = [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels=LANDED, state="CLOSED")},
                 {"match": ["issue", "view", str(k)], "stdout_json": issue(k, labels=LANDED, state="CLOSED")},
                 {"match": ["issue", "view", str(m)],
                  "stdout_json": issue(m, title=f"Ready {m}", blocked_by=[{"number": n, "state": "CLOSED"}])}]
        for t in (n, k):
            calls.append({"contains": ["pr", "list", "--head", f"t/{t}-x", "--state", "all"],
                          "stdout_json": [{"number": t + 1000, "state": "MERGED", "isDraft": False}]})
            calls.append({"contains": ["pr", "list", "--head", f"t/{t}-x"], "stdout_json": []})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": [blocked_row(m, f"Ready {m}", (n,))]},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/1#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        self.put_scenario({"calls": calls})
        return self.env(FAKECLAUDE_AGENTS=json.dumps(listing), **CREDENTIALS)

    def inside(self, env, short, identity="both"):
        """`env` as a bg session with short id `short` has it: its full id, its job folder, or both."""
        job_dir = os.path.join(self.home, ".claude", "jobs", short)
        os.makedirs(job_dir, exist_ok=True)
        extra = {}
        if identity in ("both", "session id"):
            extra["CLAUDE_CODE_SESSION_ID"] = session_uuid(short)
        if identity in ("both", "job dir"):
            extra["CLAUDE_JOB_DIR"] = job_dir
        return dict(env, **extra)

    # ---- DoD 1: a ready ticket whose only blocker is N is started by N's land (its dispatch --after N)
    def test_dispatch_after_from_the_landed_tickets_session_starts_what_it_unblocked(self):
        for n, short, m, k, k_short in self.CASES:
            for identity in self.IDENTITIES:
                with self.subTest(landed=n, ready=m, identity=identity):
                    self.setUp()
                    env = self.inside(self.landed_board(n, short, m, k, k_short), short, identity)
                    p = self.work("dispatch", "--after", str(n), "--repo", self.repo, env=env)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 0, out)
                    self.assertNotIn(short, self.stops(), out)              # its own session is never stopped
                    self.assertIsNone(self.record_of(n)["ended_at"], out)   # left for a later sweep
                    self.assertEqual(self.stops(), {k_short: 1}, out)       # the control: K's session still is
                    self.assertTrue(self.record_of(k)["ended_at"], out)
                    self.assertIn(f"after #{n}: #{m} unblocked", p.stdout.splitlines(), out)
                    self.assertEqual(self.bg_launched(), [m], out)
                    self.assertEqual(self.started_tickets(), {m}, out)
                    self.assertIsNone(self.record_of(m)["ended_at"], out)

    def test_a_sweep_inside_the_session_leaves_it_for_a_later_sweep_outside(self):
        for n, short, m, k, k_short in self.CASES:
            with self.subTest(landed=n):
                self.setUp()
                env = self.landed_board(n, short, m, k, k_short)
                p = self.work("runs", "--sweep", env=self.inside(env, short))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.stops(), {k_short: 1}, p.stdout + p.stderr)
                self.assertIsNone(self.record_of(n)["ended_at"])
                # a later sweep from another session stops and closes it as before
                p = self.work("runs", "--sweep", env=self.inside(env, "ffee0011"))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.stops(), {k_short: 1, short: 1}, p.stdout + p.stderr)
                self.assertTrue(self.record_of(n)["ended_at"])

    def test_another_sessions_dispatch_still_stops_the_landed_tickets_session(self):
        # the control: the environment names a session that is not N's (or none): N's session is stopped as today
        for n, short, m, k, k_short in self.CASES:
            for other in ("ffee0011", k_short, None):
                with self.subTest(landed=n, session=other):
                    self.setUp()
                    env = self.landed_board(n, short, m, k, k_short)
                    if other is not None:
                        env = self.inside(env, other)
                    p = self.work("dispatch", "--after", str(n), "--repo", self.repo, env=env)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 0, out)
                    self.assertEqual(self.stops().get(short), 1, out)
                    self.assertTrue(self.record_of(n)["ended_at"], out)
                    self.assertEqual(self.bg_launched(), [m], out)


if __name__ == "__main__":
    unittest.main()
