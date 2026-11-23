"""harness-work's background and cloud sessions: stopping finished sessions, redispatch from a draft, cloud launch, refusal, failure and sweep.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_sessions.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


class BgSessions:
    """The helpers of the tests on bg sessions and the tickets that decide their end (StopFinishedSessions,
    InReviewBeforeLanding)."""

    def board_scenario(self, tickets, ready=(), failing=(), startable=False):
        """tickets: {n: (labels, issue state, PRs of its branch t/<n>-x)}; `ready` the rows of `harness-board ready`.
        For a ticket in `failing`, `gh issue view n` exits 1 with a message on stderr, as gh does when the network or
        the token is gone. `startable`: gh also answers what starting a ticket asks (issue edit and comment, gh api),
        for a dispatch that launches."""
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        calls = [dict(gone, match=["issue", "view", str(n)]) if n in failing else
                 {"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels=labels, state=state)}
                 for n, (labels, state, _prs) in tickets.items()]
        for n, (_labels, _state, prs) in tickets.items():
            open_prs = [pr for pr in prs if pr["state"] == "OPEN"]
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x", "--state", "all"], "stdout_json": prs})
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x"], "stdout_json": open_prs})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": list(ready)},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
        ]
        if startable:
            calls += [
                {"match": ["issue", "edit"], "stdout": ""},
                {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
                {"match": ["api"], "stdout_json": []},
            ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def bg_sessions(self, cases):
        """cases: {n: (bg id, `claude agents` state)}. Writes an unended bg run record per ticket and returns the
        FAKECLAUDE_AGENTS listing that reports each state."""
        for n, (bg_id, _state) in cases.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        return json.dumps([{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo,
                            "name": f"proj #{n} {TITLE}"} for n, (bg_id, state) in cases.items()])

    def headless_run(self, n, result):
        """An unended headless run record of ticket n whose process is gone, its log ending on `result`."""
        record = self.write_run(n, pid=self.dead_pid())
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        return record

    def run_lines(self, env):
        """{ticket: its line} of `harness-work runs --all` (no sweep)."""
        return {int(m.group(1)): line for line in self.ok("runs", "--all", env=env).stdout.splitlines()
                for m in [re.search(r"proj #(\d+)\b", line)] if m}

    def stops(self):
        """{session id: how many times `claude stop` was called with it}."""
        counts = {}
        for call in self.claude_calls():
            argv = call["argv"]
            if argv[:1] == ["stop"]:
                for arg in argv[1:]:
                    if not arg.startswith("-"):
                        counts[arg] = counts.get(arg, 0) + 1
        return counts

    def fresh(self):
        """Forget every run record, claude call and gh call, for the next pass of a test."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.claude_log, self.gh_log):
            if os.path.exists(path):
                os.unlink(path)

    def records(self):
        return {r["ticket"]: r for r in self.run_records()}

    def reported(self, p):
        """The session ids named on a `stopped session <id>` line of the command's output."""
        return sorted(re.findall(STOPPED_RE, p.stdout + p.stderr))

    def warnings(self, p):
        """The command's stderr lines other than a `stopped session <id>` report."""
        return [line for line in p.stderr.splitlines() if line.strip() and not re.search(STOPPED_RE, line)]


class StopFinishedSessions(BgSessions, WorkTest):
    # ---- broad: the ticket's status decides, across agents states, statuses and PR states
    def test_sweep_stops_the_session_of_every_finished_ticket_and_of_no_in_progress_one(self):
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, stopped, record closed, outcome)
            # #422: in review with an open ready PR is before the land: a blocked session (its turn over) is stopped
            # and ends early, a done one ends early without a stop
            70: ("s-a11", "blocked", IN_REVIEW, "OPEN", ready_pr(120), True, True, "early"),
            71: ("s-b22", "blocked", NEEDS_INPUT, "OPEN", [], True, True, None),
            72: ("s-c33", "blocked", IN_REVIEW, "CLOSED", MERGED, True, True, None),
            73: ("s-d44", "done", IN_REVIEW, "OPEN", ready_pr(121), False, True, "early"),
            74: ("s-e55", "done", NEEDS_INPUT, "OPEN", draft(122), True, True, "ok"),
            75: ("s-f66", "done", NO_STATUS, "CLOSED", MERGED, True, True, "ok"),
            76: ("s-g77", "idle", NEEDS_INPUT, "OPEN", [], True, True, None),
            77: ("s-h88", "blocked", IN_PROGRESS, "OPEN", [], False, False, None),
            78: ("s-i99", "blocked", IN_PROGRESS, "OPEN", ready_pr(123), False, False, None),
            79: ("s-j10", "idle", IN_PROGRESS, "OPEN", draft(124), False, False, None),
            80: ("s-k20", "running", IN_PROGRESS, "OPEN", [], False, False, None),
            81: ("s-l30", "done", IN_PROGRESS, "OPEN", ready_pr(125), False, True, "ok"),
            82: ("s-m40", "done", IN_PROGRESS, "OPEN", [], False, True, "early"),
        }
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        self.board_scenario({n: c[2:5] for n, c in cases.items()})

        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stopped = sorted(c[0] for c in cases.values() if c[5])
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in stopped}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), stopped, p.stdout + p.stderr)

        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, state=c[1], labels=c[2], issue=c[3]):
                self.assertEqual(bool(saved[n]["ended_at"]), c[6], saved[n])
                if not c[6]:
                    self.assertIsNone(saved[n]["outcome"], saved[n])
                if c[7] is not None:
                    self.assertEqual(saved[n]["outcome"], c[7], saved[n])

    def test_the_board_decides_the_stop_never_the_ticket_number_or_the_session_id(self):
        # Each ticket keeps its session id while its status rotates; every session reports `blocked` throughout. Only
        # the ticket's status and state can tell which sessions to stop and which records to close.
        statuses = [   # (labels, issue state, PRs, stop and close)
            (IN_REVIEW, "OPEN", ready_pr(150), True),
            (IN_PROGRESS, "OPEN", [], False),
            (NEEDS_INPUT, "OPEN", draft(151), True),
            (IN_PROGRESS, "OPEN", ready_pr(152), False),
            (IN_REVIEW, "CLOSED", MERGED, True),
            (READY, "OPEN", [], False),
            (IN_PROGRESS, "CLOSED", MERGED, True),          # closed wins over a leftover in-progress label
            (BLOCKED_STATUS, "OPEN", draft(153), True),     # #387: a parked ticket is finished
        ]
        tickets = {9: "s-x1", 23: "s-k4", 58: "s-q7", 104: "s-m2", 311: "s-z9", 412: "s-p3", 77: "s-w8"}
        for k in range(len(statuses)):
            self.fresh()
            plan = {n: statuses[(j + k) % len(statuses)] for j, n in enumerate(tickets)}
            agents = self.bg_sessions({n: (bg_id, "blocked") for n, bg_id in tickets.items()})
            self.board_scenario({n: s[:3] for n, s in plan.items()})
            p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
            with self.subTest(rotation=k):
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                expected = sorted(tickets[n] for n, s in plan.items() if s[3])
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in expected}, p.stdout + p.stderr)
                self.assertEqual(self.reported(p), expected, p.stdout + p.stderr)
                self.assertEqual({n: bool(r["ended_at"]) for n, r in self.records().items()},
                                 {n: s[3] for n, s in plan.items()})

    def test_only_in_review_needs_input_blocked_or_closed_end_a_session_and_an_unreadable_ticket_stays_live(self):
        # The finished set is exactly status:in-review (without an open ready PR, #422), status:needs-input,
        # status:blocked (#387) or a CLOSED issue (closed wins over any leftover label, status:in-progress included).
        # An OPEN ticket with status:ready or no status label is not finished: its live session is never stopped and
        # holds a slot. Nor is a ticket gh cannot read.
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, finished)
            120: ("s-rd0", "blocked", READY, "OPEN", [], False),
            121: ("s-bk1", "blocked", BLOCKED_STATUS, "OPEN", draft(190), True),
            122: ("s-ns2", "blocked", NO_STATUS, "OPEN", [], False),
            123: ("s-ci3", "blocked", IN_PROGRESS, "CLOSED", MERGED, True),
            124: ("s-gf4", "blocked", IN_REVIEW, "OPEN", ready_pr(191), False),     # gh issue view fails (below)
            125: ("s-ci5", "done", IN_PROGRESS, "CLOSED", MERGED, True),
            126: ("s-rv6", "blocked", IN_REVIEW, "OPEN", draft(192), True),
            127: ("s-rd7", "idle", READY, "OPEN", [], False),
        }
        failing = {124}
        finished = sorted(c[0] for c in cases.values() if c[5])
        ready = [ready_row(n, f"Ready {n}") for n in range(140, 150)]

        # runs --sweep
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        self.board_scenario({n: c[2:5] for n, c in cases.items()}, failing=failing)
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), finished, p.stdout + p.stderr)
        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, labels=c[2], issue=c[3], gh_fails=n in failing):
                self.assertEqual(bool(saved[n]["ended_at"]), c[5], saved[n])
                if not c[5]:
                    self.assertIsNone(saved[n]["outcome"], saved[n])
        self.assertEqual(saved[125]["outcome"], "ok")
        shown = {r["ticket"]: r["state"] for r in self.ok_json("runs", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))}
        self.assertEqual({n: shown[n] for n, c in cases.items() if not c[5]},
                         {n: c[1] for n, c in cases.items() if not c[5]})

        # dispatch: the unfinished sessions are the live runs and each holds a slot; the finished ones hold none.
        live_tickets = sorted(n for n, c in cases.items() if not c[5])
        for most in (len(live_tickets), len(live_tickets) + 1, len(live_tickets) + 3):
            with self.subTest(max=most):
                self.fresh()
                agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
                ids = self.records()
                self.board_scenario({n: c[2:5] for n, c in cases.items()}, ready=ready, failing=failing)
                env = self.env(FAKECLAUDE_AGENTS=agents)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                self.assertEqual(sorted(data["live"]), sorted(ids[n]["id"] for n in live_tickets), data)
                self.assertEqual(len(data["plan"]), most - len(live_tickets), data)
                self.assertTrue({row["number"] for row in data["plan"]} <= set(range(140, 150)), data)
                self.work("runs", "--sweep", env=env)
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished})

    def issue_views(self, n):
        """How many times gh was asked for `issue view n`."""
        return sum(1 for argv in self.gh_calls() if argv[:3] == ["issue", "view", str(n)])

    # ---- review of the fix: a stopped session, an unreadable done one, one issue read per command
    def test_a_stopped_session_is_never_stopped_again_and_its_ticket_is_not_read(self):
        # `claude agents` says #200's session is already `stopped`: the sweep closes its record as `stopped` without a
        # `claude stop`, a report or a read of its ticket, though the ticket is in review. Beside it, #201's blocked
        # session of an in-review ticket is stopped as usual (its ticket read once).
        agents = self.bg_sessions({200: ("s-st0", "stopped"), 201: ("s-bl1", "blocked")})
        self.board_scenario({200: (IN_REVIEW, "OPEN", ready_pr(200)), 201: (IN_REVIEW, "OPEN", ready_pr(201))})
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-bl1": 1})
        self.assertEqual(self.reported(p), ["s-bl1"])
        self.assertEqual(self.issue_views(200), 0, self.gh_calls())
        self.assertEqual(self.issue_views(201), 1, self.gh_calls())
        saved = self.records()
        self.assertTrue(saved[200]["ended_at"])
        self.assertEqual(saved[200]["outcome"], "stopped")
        self.assertTrue(saved[201]["ended_at"])

    def test_a_done_session_whose_ticket_cannot_be_read_closes_ok_with_one_warning(self):
        # gh issue view fails for #210: its `done` session is not stopped, its record closes `ok` (not `early`: the
        # early check needs the same unreadable issue and is skipped), and the command warns exactly once, naming it.
        for labels in (IN_PROGRESS, IN_REVIEW):
            with self.subTest(labels=labels):
                self.fresh()
                agents = self.bg_sessions({210: ("s-dn0", "done")})
                self.board_scenario({210: (labels, "OPEN", [])}, failing={210})
                p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.stops(), {})
                self.assertEqual(self.reported(p), [])
                warnings = self.warnings(p)
                self.assertEqual(len(warnings), 1, p.stderr)
                self.assertIn("#210", warnings[0])
                saved = self.records()[210]
                self.assertTrue(saved["ended_at"], saved)
                self.assertEqual(saved["outcome"], "ok", saved)
                self.assertIsNone(saved.get("draft_pr"), saved)

    def test_one_issue_read_per_ticket_per_command_for_the_finished_and_early_checks(self):
        # Done sessions of in-progress tickets go through both the finished check and the early check (#220 ends
        # early, #221 has a ready PR and ends ok); #222's blocked in-progress session through the finished check only.
        # Each command reads each issue once.
        cases = {220: ("s-on0", "done", []), 221: ("s-on1", "done", ready_pr(221)), 222: ("s-on2", "blocked", [])}
        for command in (("runs", "--sweep"), ("dispatch", "--dry-run"), ("dispatch", "--dry-run", "--json")):
            with self.subTest(command=command):
                self.fresh()
                agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
                self.board_scenario({n: (IN_PROGRESS, "OPEN", c[2]) for n, c in cases.items()},
                                    ready=[ready_row(230, "Ready 230")])
                p = self.work(*command, env=self.env(FAKECLAUDE_AGENTS=agents))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual({n: self.issue_views(n) for n in cases}, {n: 1 for n in cases}, self.gh_calls())
                self.assertEqual(self.stops(), {})
                outcomes = {n: r["outcome"] for n, r in self.records().items()}
                self.assertEqual(outcomes, {220: "early", 221: "ok", 222: None})

    # ---- the four cases the ticket names
    def test_a_blocked_session_of_an_in_review_ticket_is_stopped_once_and_frees_its_dispatch_slot(self):
        # The finished path: in review without an open ready PR (#422; with one, InReviewBeforeLanding).
        # runs --sweep alone: one `claude stop <id>`, reported, and the record closed.
        agents = self.bg_sessions({90: ("s-rev1", "blocked")})
        self.board_scenario({90: (IN_REVIEW, "OPEN", draft(160))})
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-rev1": 1})
        self.assertIn("stopped session s-rev1", p.stdout + p.stderr)
        self.assertTrue(self.records()[90]["ended_at"])

        # dispatch: the session does not hold a slot, so --max N plans N launches, for each finished status. Whichever
        # command stops the session, it is stopped once in all.
        ready = [ready_row(n, f"Ready {n}") for n in (91, 92, 93, 94)]
        for labels, state, prs in ((IN_REVIEW, "OPEN", draft(160)), (NEEDS_INPUT, "OPEN", ready_pr(160)),
                                   (IN_REVIEW, "CLOSED", ready_pr(160))):
            for most in (1, 2, 3):
                with self.subTest(labels=labels, state=state, max=most):
                    self.fresh()
                    agents = self.bg_sessions({90: ("s-rev1", "blocked")})
                    self.board_scenario({90: (labels, state, prs)}, ready=ready)
                    env = self.env(FAKECLAUDE_AGENTS=agents)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                    self.assertEqual(data["live"], [])
                    self.assertEqual(len(data["plan"]), most, data)
                    self.assertTrue({row["number"] for row in data["plan"]} <= {91, 92, 93, 94}, data)
                    p = self.work("runs", "--sweep", env=env)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(self.stops(), {"s-rev1": 1})
                    self.assertTrue(self.records()[90]["ended_at"])

        # The dry-run text says the same.
        self.fresh()
        agents = self.bg_sessions({90: ("s-rev1", "blocked")})
        self.board_scenario({90: (IN_REVIEW, "OPEN", [])}, ready=ready)
        p = self.ok("dispatch", "--dry-run", "--max", "2", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertIn("Dry run: venue bg, 0 live, 2 of 2 slots free.", p.stdout)
        self.assertEqual(len(re.findall(r"^would start #", p.stdout, re.M)), 2, p.stdout)

    def test_a_done_session_of_a_closed_ticket_is_stopped_by_the_first_sweep_only(self):
        agents = self.bg_sessions({95: ("s-cl5", "done")})
        # A record a sweep closed before (#103's case: outcome ok, its session still listed): never stopped again.
        self.write_run(96, id=f"{self.key}-96-20261012120000", venue="bg", bg_id="s-old6", log=None,
                       ended_at=iso_ago(hours=2), outcome="ok")
        listing = json.loads(agents) + [{"id": "s-old6", "kind": "background", "state": "done", "cwd": self.repo,
                                         "name": f"proj #96 {TITLE}"}]
        env = self.env(FAKECLAUDE_AGENTS=json.dumps(listing))
        self.board_scenario({95: (IN_REVIEW, "CLOSED", MERGED), 96: (IN_REVIEW, "OPEN", ready_pr(161))})

        first = self.work("runs", "--sweep", env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.stops(), {"s-cl5": 1})
        self.assertEqual(self.reported(first), ["s-cl5"])
        saved = self.records()
        self.assertTrue(saved[95]["ended_at"])
        self.assertEqual(saved[95]["outcome"], "ok")

        # The listing still shows the session (as when a stop has not taken effect yet): the closed record is skipped.
        second = self.work("runs", "--sweep", env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.stops(), {"s-cl5": 1})
        self.assertEqual(self.reported(second), [])
        self.ok("dispatch", "--dry-run", env=env)
        self.assertEqual(self.stops(), {"s-cl5": 1})

    def test_a_blocked_session_of_an_in_progress_ticket_stays_live_and_counts_toward_max(self):
        # Beside it, a blocked session of an in-review ticket (#99), which holds no slot: --max N plans N - 1 launches,
        # the in-progress session is the one live run, and only #99's session is ever stopped, whatever #100's PRs.
        ready = [ready_row(n, f"Ready {n}") for n in (101, 102, 103, 104)]
        for prs in ([], draft(170), ready_pr(171)):
            for most in (1, 2, 3, 4):
                with self.subTest(prs=prs, max=most):
                    self.fresh()
                    agents = self.bg_sessions({100: ("s-ip0", "blocked"), 99: ("s-rv9", "blocked")})
                    rid = self.records()[100]["id"]
                    self.board_scenario({100: (IN_PROGRESS, "OPEN", prs), 99: (IN_REVIEW, "OPEN", ready_pr(172))},
                                        ready=ready)
                    env = self.env(FAKECLAUDE_AGENTS=agents)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                    self.assertEqual(data["live"], [rid])
                    self.assertEqual(len(data["plan"]), most - 1, data)
                    p = self.work("runs", "--sweep", env=env)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(self.stops(), {"s-rv9": 1})
                    self.assertNotIn("s-ip0", self.reported(p))
                    saved = self.records()[100]
                    self.assertIsNone(saved["ended_at"], saved)
                    self.assertIsNone(saved["outcome"], saved)
                    self.assertTrue(self.records()[99]["ended_at"])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--json", env=env)}
        self.assertEqual(rows[100]["state"], "blocked")

    def test_a_failed_stop_is_one_warning_leaves_exit_0_and_still_closes_the_record(self):
        agents = self.bg_sessions({110: ("s-bad0", "blocked"), 111: ("s-ok11", "done")})
        self.board_scenario({110: (IN_REVIEW, "OPEN", ready_pr(180)), 111: (NEEDS_INPUT, "OPEN", [])})
        env = self.env(FAKECLAUDE_AGENTS=agents, FAKECLAUDE_STOP_FAIL="s-bad0")

        p = self.work("runs", "--sweep", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-bad0": 1, "s-ok11": 1})
        warnings = self.warnings(p)
        self.assertEqual(len(warnings), 1, p.stderr)
        self.assertIn("s-bad0", warnings[0])
        self.assertTrue(warnings[0].startswith("harness-work: "), warnings[0])
        self.assertEqual(self.reported(p), ["s-ok11"])
        saved = self.records()
        self.assertTrue(saved[110]["ended_at"], saved[110])
        self.assertTrue(saved[111]["ended_at"], saved[111])

        # The same with --json: the JSON on stdout is whole, the one warning on stderr.
        self.fresh()
        agents = self.bg_sessions({110: ("s-bad0", "blocked")})
        self.board_scenario({110: (IN_REVIEW, "OPEN", ready_pr(180))})
        env = self.env(FAKECLAUDE_AGENTS=agents, FAKECLAUDE_STOP_FAIL="s-bad0")
        p = self.work("runs", "--sweep", "--json", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        self.assertTrue(rows[110]["ended_at"], rows[110])
        warnings = self.warnings(p)
        self.assertEqual(len(warnings), 1, p.stderr)
        self.assertIn("s-bad0", warnings[0])
        self.assertEqual(self.stops(), {"s-bad0": 1})
        self.assertTrue(self.records()[110]["ended_at"])


# =========================================================================== #422: in review, before the land
class InReviewBeforeLanding(BgSessions, WorkTest):
    """#422: `harness-pr ready` sets status:in-review before /harness:work lands (#383), so a ticket that is OPEN and
    in review with an open, non-draft pull request of its run's branch is not finished: its run is on its way to the
    land. A live session (any live state but `blocked`) is neither stopped nor closed and holds its dispatch slot. A
    session that ended its turn (`done`; `blocked`, stopped once) and an exited headless `ok` run end `early` with
    `ready_pr`, and dispatch runs the ticket again, still in review, under the cap of 3 counted runs in 7 days. In
    review with no PR, only a draft, or a merged or closed one is finished as before.

    A test's first harness-work run is a process of its own; its further runs run in this process (#378)."""

    ran_a_process = False

    def work(self, *args, env=None, cwd=None):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().work(*args, env=env, cwd=cwd)
        p = gitrepo_support.run_in_process(TOOL, args=args, cwd=cwd or self.repo, env=env or self.env())
        self.assertNotIn("Traceback", p.stderr)
        return p

    def test_a_working_session_before_landing_is_not_stopped_and_holds_its_slot(self):
        cases = {   # n: (bg id, a live `claude agents` state other than blocked, the ready PR of its branch)
            90: ("s-wk90", "running", 160),
            91: ("s-wk91", "idle", 261),
            92: ("s-wk92", "working", 362),
            93: ("s-wk93", "waiting", 463),
        }
        ready = [ready_row(n, f"Ready {n}") for n in range(140, 146)]
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        ids = {n: r["id"] for n, r in self.records().items()}
        self.board_scenario({n: (IN_REVIEW, "OPEN", ready_pr(c[2])) for n, c in cases.items()}, ready=ready)
        env = self.env(FAKECLAUDE_AGENTS=agents)

        p = self.work("runs", "--sweep", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), [])
        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, state=c[1]):
                self.assertIsNone(saved[n]["ended_at"], saved[n])
                self.assertIsNone(saved[n]["outcome"], saved[n])
        shown = {r["ticket"]: r["state"] for r in self.ok_json("runs", "--json", env=env)}
        self.assertEqual(shown, {n: c[1] for n, c in cases.items()})

        # dispatch: each session is a live run holding its slot, so --max N plans N minus the live runs, from the
        # ready tickets only; a dry run would stop none of them, and a sweep after it still stops none.
        for extra in (1, 2):
            most = len(cases) + extra
            with self.subTest(max=most):
                p = self.ok("dispatch", "--dry-run", "--max", str(most), "--json", env=env)
                data = json.loads(p.stdout)
                self.assertEqual(sorted(data["live"]), sorted(ids.values()), data)
                self.assertEqual(len(data["plan"]), extra, data)
                self.assertTrue({row["number"] for row in data["plan"]} <= set(range(140, 146)), data)
                self.assertNotIn("would stop session", p.stdout + p.stderr)
        self.ok("runs", "--sweep", env=env)
        self.assertEqual(self.stops(), {})
        self.assertEqual({n: r["ended_at"] for n, r in self.records().items()}, {n: None for n in cases})

    def test_a_done_or_blocked_session_before_landing_ends_early_with_its_pr(self):
        # #90's bg session is `done`, #91's `blocked` (the supervisor's mark of a finished turn, #123), #92's headless
        # run exited `ok`; each ticket in review with its own ready PR. #93's `failed` session is the control: failed
        # keeps its outcome, never early.
        prs = {90: 160, 91: 271, 92: 382, 93: 493}
        agents = self.bg_sessions({90: ("s-dn90", "done"), 91: ("s-bl91", "blocked"), 93: ("s-fl93", "failed")})
        self.headless_run(92, {"type": "result", "subtype": "success", "is_error": False, "num_turns": 9,
                               "result": "PR #382 is ready; landing it next.", "stop_reason": "end_turn"})
        self.board_scenario({n: (IN_REVIEW, "OPEN", ready_pr(pr)) for n, pr in prs.items()})
        env = self.env(FAKECLAUDE_AGENTS=agents)
        early = {n: f"ended before landing (PR #{prs[n]})" for n in (90, 91, 92)}

        p = self.work("runs", "--sweep", "--json", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        saved = self.records()
        for n, state in early.items():
            with self.subTest(ticket=n):
                self.assertTrue(saved[n]["ended_at"], saved[n])
                self.assertEqual((saved[n]["outcome"], saved[n].get("ready_pr")), ("early", prs[n]), saved[n])
                self.assertEqual((rows[n]["outcome"], rows[n]["state"]), ("early", state), rows[n])
        self.assertTrue(saved[93]["ended_at"], saved[93])
        self.assertEqual((saved[93]["outcome"], rows[93]["state"]), ("error", "ended error"))
        # only the blocked session is stopped, once (the failed control's stop is not this ticket's business)
        self.assertEqual({k: v for k, v in self.stops().items() if k != "s-fl93"}, {"s-bl91": 1},
                         p.stdout + p.stderr)
        self.assertEqual([x for x in self.reported(p) if x != "s-fl93"], ["s-bl91"])

        # Without a sweep, from the saved records alone: the same state in --json and in the table.
        shown = {r["ticket"]: r["state"] for r in self.ok_json("runs", "--json", env=env)}
        self.assertEqual({n: shown[n] for n in early}, early)
        lines = self.run_lines(env)
        for n, state in early.items():
            self.assertIn(state, lines[n])
        self.ok("runs", "--sweep", env=env)                     # a second sweep stops nothing more
        self.assertEqual(self.stops().get("s-bl91"), 1)

    def test_dispatch_relaunches_it_in_review_until_the_cap(self):
        # #90's done session ends early in dispatch's own sweep; #93's run ended early (PR #263) at an earlier sweep.
        # #95 is a ready ticket beside them.
        prs = {90: 160, 93: 263}
        agents = self.bg_sessions({90: ("s-dn90", "done")})
        self.write_run(93, venue="bg", bg_id="s-old93", log=None, started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=1), outcome="early", ready_pr=263)
        self.board_scenario({n: (IN_REVIEW, "OPEN", ready_pr(pr)) for n, pr in prs.items()},
                            ready=[ready_row(95, "Ready 95")], startable=True)
        env = self.env(FAKECLAUDE_AGENTS=agents, FAKECLAUDE_BG_ID="s-new90")

        p = self.ok("dispatch", "--dry-run", "--max", "5", env=env)
        lines = p.stdout.splitlines()
        for n, pr in prs.items():
            self.assertIn(f"would start #{n} {TITLE} (again after early, PR #{pr} in review) and launch bg", lines)
        data = self.ok_json("dispatch", "--dry-run", "--max", "5", "--json", env=env)
        plan = {row["number"]: row for row in data["plan"]}
        self.assertEqual(sorted(plan), [90, 93, 95], data)
        for n, pr in prs.items():
            self.assertEqual((plan[n].get("again"), plan[n].get("ready_pr")), ("early", pr), plan[n])
        self.assertEqual(data["skipped"], [])
        for n, pr in prs.items():                               # --only goes through the same redispatch
            with self.subTest(only=n):
                data = self.ok_json("dispatch", "--dry-run", "--only", str(n), "--json", env=env)
                self.assertEqual([(r["number"], r.get("again"), r.get("ready_pr")) for r in data["plan"]],
                                 [(n, "early", pr)], data)
                self.assertEqual(data["skipped"], [])

        # For real: /harness:work 90 is launched and the ticket keeps status:in-review (no label edit).
        p = self.work("dispatch", "--only", "90", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [launch] = [c for c in self.claude_calls() if "--bg" in c["argv"]]
        self.assertRegex(launch["argv"][-1], r"^/harness:work 90\b")
        self.assertEqual([a for a in self.gh_calls() if a[:3] == ["issue", "edit", "90"]], [])
        [new] = [r for r in self.run_records() if r["ticket"] == 90 and not r.get("ended_at")]
        self.assertEqual(new.get("bg_id"), "s-new90", new)

        # The cap: three counted runs of #90 ended in the 7 days, so it is skipped with the cap's fields and its
        # newest record is marked capped; #93's three include a billing run, which does not count, so it goes again.
        self.fresh()
        histories = {90: ("early", "early", "early"), 93: ("early", "billing", "early")}
        last = {}
        for n, outcomes in histories.items():
            for i, outcome in enumerate(outcomes):
                hours = 30 - 10 * i
                extra = {"ready_pr": prs[n]} if outcome == "early" else {}
                last[n] = self.write_run(n, id=f"{self.key}-{n}-2026100{i + 1}090000", venue="bg",
                                         bg_id=f"s-h{n}{i}", log=None, started_at=iso_ago(hours=hours + 1),
                                         ended_at=iso_ago(hours=hours), outcome=outcome, **extra)
        self.board_scenario({n: (IN_REVIEW, "OPEN", ready_pr(pr)) for n, pr in prs.items()}, startable=True)
        env = self.env()
        data = self.ok_json("dispatch", "--dry-run", "--json", env=env)
        self.assertEqual([(r["number"], r.get("again"), r.get("ready_pr")) for r in data["plan"]],
                         [(93, "early", 263)], data)
        [skip] = data["skipped"]
        self.assertEqual(sorted(skip), ["advice", "number", "runs", "why", "window_days"])
        self.assertEqual({k: skip[k] for k in ("number", "runs", "window_days", "advice")},
                         {"number": 90, "runs": 3, "window_days": 7, "advice": "/harness:work 90"})
        data = self.ok_json("dispatch", "--dry-run", "--only", "90", "--json", env=env)
        self.assertEqual(data["plan"], [])
        self.assertEqual([(s["number"], s.get("runs"), s.get("advice")) for s in data["skipped"]],
                         [(90, 3, "/harness:work 90")])
        table = self.ok("runs", "--all", env=env).stdout.splitlines()
        capped = [line for line in table if "capped" in line]
        self.assertEqual(len(capped), 1, table)
        self.assertIn(last[90]["id"], capped[0])
        self.assertIn("ended before landing (PR #160)", capped[0])
        marked = [r["id"] for r in self.ok_json("runs", "--all", "--json", env=env) if r.get("capped")]
        self.assertEqual(marked, [last[90]["id"]])
        self.assertEqual([c for c in self.claude_calls() if "--bg" in c["argv"]], [])

    def test_in_review_without_an_open_ready_pr_stays_finished(self):
        cases = {   # n: (bg id, agents state, PRs of its branch t/<n>-x, finished)
            100: ("s-np0", "blocked", [], True),                                    # no PR (an ops ticket)
            101: ("s-dr1", "idle", draft(171), True),                               # only a draft
            102: ("s-mg2", "done", [{"number": 172, "state": "MERGED", "isDraft": False}], True),
            103: ("s-cl3", "running", [{"number": 173, "state": "CLOSED", "isDraft": False}], True),
            104: ("s-rp4", "idle", ready_pr(174), False),       # the control: an open ready PR, before the land
        }
        ready = [ready_row(n, f"Ready {n}") for n in range(110, 116)]
        agents = self.bg_sessions({n: c[:2] for n, c in cases.items()})
        self.board_scenario({n: (IN_REVIEW, "OPEN", c[2]) for n, c in cases.items()}, ready=ready)
        env = self.env(FAKECLAUDE_AGENTS=agents)

        p = self.work("runs", "--sweep", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        finished = sorted(c[0] for c in cases.values() if c[3])
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished}, p.stdout + p.stderr)
        self.assertEqual(self.reported(p), finished)
        saved = self.records()
        for n, c in cases.items():
            with self.subTest(ticket=n, state=c[1], prs=c[2]):
                if c[3]:
                    self.assertTrue(saved[n]["ended_at"], saved[n])
                    self.assertEqual(saved[n]["outcome"], "ok", saved[n])
                    self.assertIsNone(saved[n].get("ready_pr"), saved[n])
                else:
                    self.assertIsNone(saved[n]["ended_at"], saved[n])

        # Never dispatched again: the next dispatch plans only ready tickets, the control holding its slot.
        data = self.ok_json("dispatch", "--dry-run", "--max", "4", "--json", env=env)
        self.assertEqual(data["live"], [saved[104]["id"]], data)
        self.assertEqual([row["number"] for row in data["plan"]], [110, 111, 112], data)
        self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished})


# =========================================================================== #42: redispatch of a draft-PR run
class RedispatchDraft(WorkTest):
    """A run that ended with its ticket in progress and only a draft PR is dispatched again: the plan's `again`
    reason, the start comment and the prompt name the PR, and the prompt says the worktree may hold uncommitted
    work. The cap (3 ended runs in 7 days) holds for it, with the "ended without a ready pull request" line."""

    def setUp(self):
        super().setUp()
        # The ticket branch exists, pushed, with its worktree and the previous run's uncommitted fix in it.
        self.wt = self.worktree_path()
        self.git(self.repo, "worktree", "add", "-q", "-b", BRANCH, self.wt, "main")
        write(os.path.join(self.wt, "board.py"), "def ready():\n    return []\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Round 1")
        self.git(self.wt, "push", "-q", "-u", "origin", BRANCH)
        self.dirty = write(os.path.join(self.wt, "board.py"), "def ready():\n    return ['round 2']\n")

    def write_scenario(self, drafts, ready=()):
        """drafts: {ticket: (branch, its draft PR number, or None for no PR)}; every ticket OPEN and
        status:in-progress. ready: tickets on the board as status:ready (no run, no PR), listed by `harness-board
        ready`."""
        calls = [{"match": ["issue", "view", str(n)],
                  "stdout_json": issue(n, labels=IN_PROGRESS, title=TITLE if n == 12 else f"Ticket {n}")}
                 for n in drafts]
        calls += [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, title=f"Ticket {n}")} for n in ready]
        for n, (branch, pr) in drafts.items():
            calls.append({"contains": ["pr", "list", "--head", branch], "stdout_json": draft(pr) if pr else []})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": [ready_row(n, f"Ticket {n}") for n in ready]},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def ended_run(self, n, branch, worktree, result=None, **fields):
        record = self.write_run(n, pid=self.dead_pid(), branch=branch, worktree=worktree, **fields)
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result or ROUND2_RESULT) + "\n")
        return record

    def comment_bodies(self, n):
        """The text of every `gh issue comment n` call: its --body, else what came on stdin (--body-file -)."""
        bodies = []
        with open(self.gh_log, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                argv = entry["argv"]
                if argv[:3] == ["issue", "comment", str(n)]:
                    bodies.append(argv[argv.index("--body") + 1] if "--body" in argv else entry.get("stdin") or "")
        return bodies

    def test_dispatch_picks_the_ticket_up_again_and_names_the_pull_request(self):
        other = os.path.join(self.repo, ".claude", "worktrees", "t-15-x")
        self.ended_run(12, BRANCH, self.wt)                    # the #31 case: draft PR #41, fix uncommitted
        self.ended_run(15, "t/15-x", other, result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", 58)})

        # Dry run: both tickets come back, each `again` reason naming its own draft PR.
        p = self.ok("dispatch", "--dry-run")
        lines = p.stdout.splitlines()
        starts = {n: [ln for ln in lines if ln.startswith(f"would start #{n} ")] for n in (12, 15)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {12: 1, 15: 1}, p.stdout)
        [line12], [line15] = starts[12], starts[15]
        self.assertIn("again after early", line12)
        self.assertIn("#41", line12)
        self.assertNotIn("#58", line12)
        self.assertIn("again after early", line15)
        self.assertIn("#58", line15)
        self.assertNotIn("#41", line15)
        self.assertNotIn("No ready tickets.", p.stdout)
        self.assertNotIn("skip #", p.stdout)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {12: "early", 15: "early"})
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [12, 15])
        self.assertEqual(self.claude_calls(), [])

        # For real, one slot: #12 is launched in its existing worktree, the uncommitted work left alone.
        p = self.ok("dispatch", "--venue", "headless", "--max", "1")
        self.assertIn(f"#12 headless on {BRANCH} (pid ", p.stdout)
        launched = [r for r in self.run_records() if r.get("ended_at") is None]
        self.assertEqual([r["ticket"] for r in launched], [12])
        self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(launched[0]["log"])))
        self.assertTrue(wait_for(lambda: pid_gone(launched[0]["pid"])))
        [call] = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[0], "-p")
        prompt = argv[1]
        self.assertTrue(prompt.startswith("/harness:work 12"), prompt)
        self.assertIn("#41", prompt)
        self.assertNotIn("#58", prompt)
        self.assertIn("uncommitted", prompt.lower())
        self.assertIn("git status", prompt)
        self.assertIn("review", prompt.lower())                # it reads the PR's review comments first
        self.assertEqual(os.path.realpath(call["cwd"]), self.wt)
        self.assertEqual(read_text(self.dirty), "def ready():\n    return ['round 2']\n")

        bodies = self.comment_bodies(12)
        self.assertTrue([b for b in bodies if "#41" in b and "uncommitted" in b.lower()], bodies)
        self.assertFalse([b for b in bodies if "#58" in b], bodies)
        self.assertEqual(self.comment_bodies(15), [])

        # The cap is unchanged: three ended runs of #12 in the window, so the next dispatch skips it with the capped
        # line; #15 (one ended run) still comes back.
        shutil.rmtree(self.runs_dir(), True)
        os.unlink(self.gh_log)
        runs12 = [self.ended_run(12, BRANCH, self.wt, id=f"{self.key}-12-2026100{i + 1}090000",
                                 started_at=iso_ago(hours=hours + 1), ended_at=iso_ago(hours=hours), outcome=outcome)
                  for i, (outcome, hours) in enumerate((("error", 30), ("early", 20), ("early", 10)))]
        self.ended_run(15, "t/15-x", other, result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", 58)})
        last = runs12[-1]
        why = (f"3 runs ended without a ready pull request in 7 days (last: early at {last['ended_at']}); "
               f"run it in this session with /harness:work 12, or wait for the window")
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + why, p.stdout.splitlines())
        self.assertFalse([ln for ln in p.stdout.splitlines() if ln.startswith("would start #12")], p.stdout)
        self.assertTrue([ln for ln in p.stdout.splitlines() if ln.startswith("would start #15 ")], p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [15])
        self.assertEqual(data["skipped"], [{"number": 12, "why": why, "runs": 3, "window_days": 7,
                                            "advice": "/harness:work 12"}])
        table = self.ok("runs", "--all").stdout.splitlines()
        [row] = [ln for ln in table if last["id"] in ln]
        self.assertIn("capped", row)
        self.assertEqual(sum("capped" in ln for ln in table), 1)
        sessions = [c for c in self.claude_calls() if "-p" in c["argv"] or "--bg" in c["argv"]]
        self.assertEqual(len(sessions), 1)                     # nothing launched after the one run above

    def test_only_a_draft_pull_request_puts_the_pr_and_uncommitted_work_in_the_prompt(self):
        # One dispatch launches three tickets: #12 after an early run with draft PR #41 (the #31 case), #15 after an
        # early run with no PR, #16 fresh from the board (status:ready, never run). Only #12's prompt and start
        # comment name a PR and the uncommitted work; the other two get the plain prompt and no such comment, so the
        # PR text cannot be a constant of every launch.
        self.ended_run(12, BRANCH, self.wt)
        self.ended_run(15, "t/15-x", os.path.join(self.repo, ".claude", "worktrees", "t-15-x"), result=EARLY_RESULT)
        self.write_scenario({12: (BRANCH, 41), 15: ("t/15-x", None)}, ready=[16])

        p = self.ok("dispatch", "--dry-run")
        starts = {n: [ln for ln in p.stdout.splitlines() if ln.startswith(f"would start #{n} ")] for n in (12, 15, 16)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {12: 1, 15: 1, 16: 1}, p.stdout)
        self.assertIn("#41", starts[12][0])
        self.assertIn("(again after early)", starts[15][0])    # no PR: the reason as before #42
        self.assertNotIn("#41", starts[15][0] + starts[16][0])

        p = self.ok("dispatch", "--venue", "headless", "--max", "3")
        launched = [r for r in self.run_records() if r.get("ended_at") is None]
        self.assertEqual(sorted(r["ticket"] for r in launched), [12, 15, 16], p.stdout)
        for record in launched:
            self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(record["log"])))
            self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        prompts = {}
        for call in self.claude_calls():
            argv = call["argv"]
            self.assertEqual(argv[0], "-p", argv)
            m = re.match(r"/harness:work (\d+)\b", argv[1])
            self.assertTrue(m, argv[1])
            prompts[int(m.group(1))] = argv[1]
        self.assertEqual(sorted(prompts), [12, 15, 16])

        pr_words = re.compile(r"\bPR\b|pull request", re.IGNORECASE)
        self.assertIn("#41", prompts[12])
        self.assertIn("uncommitted", prompts[12].lower())
        self.assertIn("git status", prompts[12])
        self.assertEqual(prompts[16], "/harness:work 16")     # a plain dispatch: the prompt is unchanged
        for n in (15, 16):
            with self.subTest(ticket=n):
                prompt = prompts[n]
                self.assertNotIn("uncommitted", prompt.lower())
                self.assertNotIn("git status", prompt)
                self.assertNotIn("review", prompt.lower())
                self.assertNotIn("#41", prompt)
                self.assertNotRegex(prompt, pr_words)

        self.assertTrue([b for b in self.comment_bodies(12) if "#41" in b and "uncommitted" in b.lower()],
                        self.comment_bodies(12))
        self.assertTrue(self.comment_bodies(16))               # the plain "Started on ..." comment
        for n in (15, 16):
            with self.subTest(ticket=n, comment=True):
                for body in self.comment_bodies(n):
                    self.assertNotIn("uncommitted", body.lower())
                    self.assertNotIn("#41", body)
                    self.assertNotRegex(body, pr_words)


class CloudLaunch(CloudTest):
    def launch(self, n, title, sid, settings, slug, *extra):
        self.commit_settings(settings)
        self.cloud_scenario([issue(n, title=title)], slug=slug)
        env = self.env(FAKECLAUDE_CLOUD_ID=sid, FAKECLAUDE_TITLE=title, **CREDENTIALS)
        p = self.work("cloud", str(n), *extra, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def check_launch(self, n, branch, sid, slug, verify, absent=()):
        """The one cloud launch of ticket n: its argv, prompt, push, record, log and comments. Returns the record."""
        wt = self.worktree_path(branch)
        self.assertEqual(self.git(wt, "branch", "--show-current"), branch)
        [call] = self.cloud_calls()
        argv = call["argv"]
        self.assertTrue(call["tty"])
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        prompt = argv[argv.index("--cloud") + 1]
        self.assertFalse(prompt.startswith("-"), argv)
        self.assertEqual(argv[argv.index("--on-branch") + 1], branch)
        for name in CREDENTIALS:
            self.assertNotIn(name, call["env"])

        # the prompt is self-contained: repository, ticket, branch, gh api REST, both markers, the verify command
        for needle in (slug, f"#{n}", branch, "gh api", f"repos/{slug}/issues/{n}/comments", CLOUD_DONE,
                       CLOUD_BLOCKED, verify, "`harness-board ready` lists unblocked tickets"):
            self.assertIn(needle, prompt)
        for needle in absent:
            self.assertNotIn(needle, prompt)

        # pushed to origin before the launch, with an upstream
        head = self.git(wt, "rev-parse", "HEAD")
        self.assertIn(f"{head}\trefs/heads/{branch}", call["remote_heads"])
        self.assertEqual(self.git(wt, "rev-parse", "--abbrev-ref", "@{u}"), f"origin/{branch}")

        [record] = [r for r in self.run_records() if r["ticket"] == n and not r.get("ended_at")]
        url = record["url"]
        self.assertTrue(url.startswith(f"https://claude.ai/code/{sid}"), url)
        self.assertIsNone(re.search(r"[\x00-\x20\x7f]", url), repr(url))
        expected = {"ticket": n, "repo": os.path.realpath(self.repo), "worktree": wt, "branch": branch,
                    "venue": "cloud", "cloud_id": sid, "ended_at": None, "exit": None, "outcome": None}
        self.assertEqual({k: record.get(k) for k in expected}, expected)
        self.assertTrue({"id", "log", "started_at"} <= set(record), record)
        self.iso_close_to(record["started_at"], dt.timedelta(0))
        log = read_text(record["log"])
        self.assertIn("Created cloud session", log)
        self.assertIn(sid, log)
        self.assertNotIn("\r", log)
        self.assertNotIn("\x1b", log)

        # status in-progress with a start comment naming the venue, and the comment carrying the URL
        self.assertIn("status:in-progress", self.added_labels(n))
        bodies = self.comment_bodies(n)
        self.assertTrue([b for b in bodies if b.startswith(f"Started on {branch}") and "cloud" in b], bodies)
        self.assertTrue([b for b in bodies if f"Cloud session on {branch}: {url}" in b], bodies)
        return record

    def test_cloud_launch_end_to_end(self):
        sid = "session_01HwXq7Kp-Lm_9"
        p = self.launch(12, TITLE, sid, {"CLAUDE_VERIFY_CMD": "make verify-widgets",
                                         "HARNESS_TEST_CMD": "pytest -q {files}"}, "acme/widgets")
        record = self.check_launch(12, BRANCH, sid, "acme/widgets", "make verify-widgets", absent=("pytest -q",))
        url = record["url"]
        self.assertIn(f"#12 cloud on {BRANCH} ({url})", p.stdout)

        # runs lists it in the cloud; --json keeps cloud_id and url; a sweep with no marker leaves it open
        [row] = [r for r in self.ok_json("runs", "--json") if r["id"] == record["id"]]
        self.assertEqual((row["cloud_id"], row["url"], row["state"]), (sid, url, f"in the cloud ({url})"))
        line = next(ln for ln in self.ok("runs").stdout.splitlines() if record["id"] in ln)
        self.assertIn("in the cloud", line)
        [row] = [r for r in self.ok_json("runs", "--sweep", "--json") if r["id"] == record["id"]]
        self.assertIsNone(row["ended_at"])
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_cloud_launch_varies_with_the_ticket_session_repository_and_verify_command(self):
        # an ended cloud run of the ticket does not stop a new one, nor does an exhausted api budget
        old = self.cloud_run(31, iso_ago(hours=40), ended_at=iso_ago(hours=30), outcome="ok")
        self.write_budget("api", 5)
        sid = "session_9zz_Other-Run2"
        p = self.launch(31, "Cache the ready list", sid, {"HARNESS_TEST_CMD": "python3 -m pytest tests -q"},
                        "someone/thing", "--json")
        branch = "t/31-cache-the-ready-list"
        record = self.check_launch(31, branch, sid, "someone/thing", "python3 -m pytest tests -q",
                                   absent=("acme/widgets", BRANCH))
        self.assertEqual(json.loads(p.stdout), record)
        records = self.run_records()
        self.assertEqual(len(records), 2)
        self.assertIn(old, records)

    def test_the_session_id_comes_from_the_view_url_without_a_resume_line(self):
        self.commit_settings({"CLAUDE_VERIFY_CMD": "make check"})
        self.cloud_scenario([issue()])
        sid = "session_77NoResume_x"
        self.ok("cloud", "12", env=self.env(FAKECLAUDE_CLOUD="noresume", FAKECLAUDE_CLOUD_ID=sid))
        [record] = self.run_records()
        self.assertEqual(record["cloud_id"], sid)
        self.assertTrue(record["url"].startswith(f"https://claude.ai/code/{sid}"), record["url"])
        self.assertIsNone(record["ended_at"])

    def test_a_needs_input_ticket_with_an_owner_answer_after_its_question_launches_in_progress(self):
        """#295: an answer since the newest needs-input question (harness-board answers) lets the launch go on, and
        the ticket leaves needs-input for in-progress."""
        cases = ((12, "session_01Answered_a", [comment(ME, iso_ago(hours=5), "Earlier thought."),
                                               comment(ME, iso_ago(hours=3), QUESTION),
                                               comment(ME, iso_ago(hours=1), "Use main.")]),
                 (40, "session_02Answered-b", [comment(ME, iso_ago(hours=9), QUESTION),
                                               comment("drive-by-user", iso_ago(hours=8), "Me too"),
                                               comment(ME, iso_ago(hours=6), QUESTION.replace("Which base?",
                                                                                              "Which flag?")),
                                               comment(ME.upper(), iso_ago(minutes=5), "The --fast one.")]))
        for n, sid, comments in cases:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)
                self.cloud_scenario([issue(n, labels=NEEDS_INPUT, comments=comments)])
                p = self.work("cloud", str(n), env=self.env(FAKECLAUDE_CLOUD_ID=sid))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(len(self.cloud_calls()), 1)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["cloud_id"], record["ended_at"]), (n, sid, None))
                self.assertIn("status:in-progress", self.added_labels(n))
                edits = [a for a in self.gh_calls() if a[:3] == ["issue", "edit", str(n)]]
                self.assertTrue([a for a in edits if "status:in-progress" in a and "status:needs-input" in a], edits)

    def test_start_accepts_the_cloud_venue(self):
        self.scenario(issues=[issue()])
        data = self.ok_json("start", "12", "--venue", "cloud", "--json")
        self.assertEqual(data["branch"], BRANCH)
        comments = [c for c in self.gh_calls() if c[:3] == ["issue", "comment", "12"]]
        self.assertTrue([c for c in comments if "cloud" in c[c.index("--body") + 1]], comments)
        self.assertFalse(self.claude_calls())


class CloudRefusal(CloudTest):
    def assertNothingStarted(self, records_before):
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        calls = self.gh_calls()
        self.assertFalse([c for c in calls if c[:2] in (["issue", "edit"], ["issue", "comment"])], calls)
        self.assertFalse(self.claude_calls())
        self.assertEqual(self.run_records(), records_before)
        self.assertEqual(self.git(self.origin, "for-each-ref", "refs/heads/t"), "")

    def test_a_needs_local_ticket_is_refused_with_the_reason(self):
        for n in (12, 40):
            with self.subTest(ticket=n):
                self.cloud_scenario([issue(n, labels=("task", "status:ready", "size:S", "risk:low", "needs-local"))])
                p = self.work("cloud", str(n))
                self.assertOneLineError(p, "needs-local", f"/harness:work {n}")
                self.assertEqual(p.stdout, "")
                self.assertNothingStarted([])

    def test_a_needs_input_ticket_without_an_answer_since_its_question_is_refused(self):
        """#295: no owner answer after the newest needs-input question (none at all, only older ones, only the
        harness's own comments or someone else's) refuses the launch with one line: no worktree, label change,
        comment, push, cloud session or run record."""
        q = QUESTION
        cases = {
            12: [comment(ME, iso_ago(hours=3), q)],
            40: [comment(ME, iso_ago(hours=6), "Use main."), comment(ME, iso_ago(hours=3), q)],
            41: [comment(ME, iso_ago(hours=8), q), comment(ME, iso_ago(hours=7), "Use main."),
                 comment(ME, iso_ago(hours=2), q.replace("Which base?", "Which flag?"))],
            # jotform-glow-sync #146: the answer reached the ticket as a harness comment, so it is not an answer
            42: [comment(ME, iso_ago(hours=3), q),
                 comment(ME, iso_ago(minutes=20), "Ryan answered in chat: use main." + MARK)],
            43: [comment(ME, iso_ago(hours=3), q), comment("drive-by-user", iso_ago(hours=1), "Use main.")],
        }
        for n, comments in cases.items():
            with self.subTest(ticket=n):
                if os.path.exists(self.gh_log):
                    os.unlink(self.gh_log)
                self.cloud_scenario([issue(n, labels=NEEDS_INPUT, comments=comments)])
                p = self.work("cloud", str(n), env=self.env(FAKECLAUDE_CLOUD_ID=f"session_0{n}Refused"))
                self.assertOneLineError(p, "needs-input", f"#{n}")
                self.assertEqual(p.stdout, "")
                self.assertNothingStarted([])

    def test_an_exhausted_cloud_budget_is_refused(self):
        self.write_budget("cloud", 5, reason="credits used up")
        until = self.read_json(self.budgets_path())["cloud"]["exhausted_until"]
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12")
        self.assertOneLineError(p, "harness-work budget reset cloud", until)
        self.assertNothingStarted([])

    def test_an_unended_cloud_run_of_the_ticket_is_refused(self):
        record = self.cloud_run(12, iso_ago(hours=1))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS)])
        p = self.work("cloud", "12")
        self.assertOneLineError(p, record["id"])
        self.assertNothingStarted([record])

    def test_epic_closed_and_blocked_tickets_get_starts_refusal(self):
        cases = [(issue(labels=("epic", "status:ready"), title="The board"), "#12 is an epic"),
                 (issue(state="CLOSED"), "#12 is closed"),
                 (issue(labels=("task", "status:blocked")), "status:blocked")]
        for record, needle in cases:
            with self.subTest(needle=needle):
                self.cloud_scenario([record])
                self.assertOneLineError(self.work("cloud", "12"), needle)
                self.assertNothingStarted([])


class CloudLaunchFailure(CloudTest):
    def test_a_failed_launch_or_one_without_a_session_id_ends_the_record_in_error(self):
        cases = (("fail", "Error: cloud environment unavailable (HTTP 503)"),
                 ("attached", "No cloud session was created or opened: it would open in this terminal and need the "
                              "keyboard."))
        for mode, last in cases:
            with self.subTest(mode=mode):
                shutil.rmtree(self.runs_dir(), True)
                self.cloud_scenario([issue()])
                p = self.work("cloud", "12", env=self.env(FAKECLAUDE_CLOUD=mode))
                self.assertOneLineError(p, last)
                self.assertNotIn("\x1b", p.stderr)
                [record] = self.run_records()
                self.assertEqual((record["venue"], record["outcome"]), ("cloud", "error"))
                self.assertTrue(record["ended_at"])
                self.assertIn(last, record.get("error") or "")
                self.assertEqual(len(self.cloud_calls()), 1)
                os.unlink(self.claude_log)
        status = self.ok("budget", "status").stdout
        self.assertIn("cloud: available", status)
        self.assertIn("api: available", status)

    def test_billing_wording_marks_the_cloud_budget_exhausted(self):
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12", env=self.env(FAKECLAUDE_CLOUD="billing"))
        self.assertOneLineError(p, "credits are used up")
        [record] = self.run_records()
        self.assertEqual(record["outcome"], "error")
        budgets = self.read_json(self.budgets_path())
        self.iso_close_to(budgets["cloud"]["exhausted_until"], dt.timedelta(hours=24))
        self.assertIsNone(exhausted(budgets.get("api")))
        # the next launch is refused by the budget, not by the ended run
        self.assertOneLineError(self.work("cloud", "12"), "harness-work budget reset cloud")
        self.assertEqual(len(self.cloud_calls()), 1)

    def test_a_failed_push_launches_nothing(self):
        executable(os.path.join(self.origin, "hooks", "pre-receive"),
                   "#!/bin/sh\necho 'push refused by the test hook' >&2\nexit 1\n")
        self.cloud_scenario([issue()])
        p = self.work("cloud", "12")
        self.assertOneLineError(p)
        self.assertIn("push", p.stderr.lower())
        self.assertTrue(os.path.isdir(self.worktree_path()))     # started first, then the push failed
        self.assertFalse(self.cloud_calls())
        self.assertEqual(self.git(self.origin, "for-each-ref", "refs/heads/t"), "")


def parse_utc(text):
    """An ISO-8601 UTC time as the records and comments write it."""
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


class CloudSweep(CloudTest):
    def test_the_sweep_closes_a_cloud_run_only_on_a_marker_by_the_gh_user_after_its_start(self):
        two = iso_ago(hours=2)
        t = {k: iso_ago(minutes=k) for k in (10, 20, 30, 50, 60, 70, 90, 180)}
        cases = {   # ticket: (started_at, labels, state, comments, expected outcome, expected ended_at or "now")
            21: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[60], CLOUD_DONE + "\nDid it.\nRuling: x")],
                 "ok", t[60]),                                       # in progress, no PR: still ok, never early
            22: (two, IN_PROGRESS_LABELS, "OPEN", [comment("drive-by-user", t[60], CLOUD_DONE)], None, None),
            23: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[180], CLOUD_DONE)], None, None),
            24: (two, IN_PROGRESS_LABELS, "OPEN", [], None, None),
            25: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[60], "Progress so far.\n" + CLOUD_DONE)], None, None),
            27: (iso_ago(hours=25), IN_PROGRESS_LABELS, "OPEN", [], "unknown", "now"),
            # #295: a label other than in-progress is not an end; only a marker, a closed ticket or a merged PR is
            28: (two, ("task", "status:in-review", "size:S", "risk:low"), "OPEN", [], None, None),
            29: (two, IN_PROGRESS_LABELS, "CLOSED", [], "ok", "now"),
            31: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[90], CLOUD_BLOCKED + "\nWhich base?"),
                                                   comment(ME, t[30], CLOUD_DONE + "\nAnswered and done.")],
                 "ok", t[30]),
            32: (iso_ago(hours=23), IN_PROGRESS_LABELS, "OPEN", [], None, None),
            33: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[70], CLOUD_DONE + "\nDone."),
                                                   comment(ME, t[20], "Started on t/33-x (mac, interactive)" + MARK)],
                 "ok", t[70]),
            34: (two, IN_PROGRESS_LABELS, "OPEN", [comment(ME, t[50], CLOUD_DONE + "\nDone."),
                                                   comment("drive-by-user", t[10], CLOUD_BLOCKED + "\nStop?")],
                 "ok", t[50]),
        }
        records = {n: self.cloud_run(n, c[0]) for n, c in cases.items()}
        self.cloud_scenario([issue(n, labels=c[1], state=c[2], comments=c[3]) for n, c in cases.items()])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        saved = {r["ticket"]: r for r in self.run_records()}
        for n, (_, _, _, _, outcome, ended) in cases.items():
            with self.subTest(ticket=n):
                self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), (outcome, outcome))
                if ended is None:
                    self.assertIsNone(saved[n]["ended_at"])
                    self.assertEqual(rows[n]["state"], f"in the cloud ({records[n]['url']})")
                elif ended == "now":
                    self.iso_close_to(saved[n]["ended_at"], dt.timedelta(0))
                    self.assertEqual(rows[n]["state"], f"ended {outcome}")
                else:
                    self.assertEqual(saved[n]["ended_at"], ended)
                    self.assertEqual(rows[n]["state"], f"ended {outcome}")
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])   # no needs-input here
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def with_prs(self, prs):
        """Put {branch: `gh pr list --head <branch>` rows} ahead of the scenario's empty pr list."""
        with open(self.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"][:0] = [{"contains": ["pr", "list", "--head", branch], "stdout_json": rows}
                             for branch, rows in prs.items()]
        write(self.scenario_path, json.dumps(data))

    def test_a_status_label_alone_never_ends_a_live_cloud_run(self):
        """#295: the sweep ends a cloud run `ok` only on a done marker by the gh user after its start, a closed
        ticket or a merged pull request of its branch; a ticket labelled needs-input, ready, in-review, blocked or
        with no status, with none of those, keeps its run open (in the cloud), whatever else the ticket holds."""
        two = iso_ago(hours=2)
        harness_note = comment(ME, iso_ago(minutes=50), "Started on t/0-x (mac, cloud)" + MARK)
        merged = lambda n: [{"number": 100 + n, "state": "MERGED", "isDraft": False,   # noqa: E731
                             "mergedAt": iso_ago(minutes=15)}]
        cases = {   # ticket: (labels, state, comments, pr rows, expected outcome or None for open)
            # the jotform-glow-sync #146 shape: needs-input kept, harness comments only, ten minutes in
            51: (NEEDS_INPUT, "OPEN", [comment(ME, iso_ago(hours=3), QUESTION), harness_note], [], None),
            52: (READY, "OPEN", [harness_note], [], None),
            53: (IN_REVIEW, "OPEN", [], ready_pr(153), None),                # a ready PR, not merged
            54: (IN_REVIEW, "OPEN", [], draft(154), None),
            55: (BLOCKED_STATUS, "OPEN", [], [], None),
            56: (NO_STATUS, "OPEN", [comment("drive-by-user", iso_ago(minutes=30), CLOUD_DONE)], [], None),
            57: (NEEDS_INPUT, "OPEN", [comment(ME, iso_ago(hours=3), CLOUD_DONE)], [], None),   # before its start
            58: (IN_REVIEW, "OPEN", [{"author": {"login": ME}, "createdAt": iso_ago(minutes=40),
                                      "body": "Closing in on it.", "url": "u"}], [], None),
            # the positive ends, under labels that are not in-progress too
            61: (NEEDS_INPUT, "OPEN", [comment(ME, iso_ago(minutes=40), CLOUD_DONE + "\nDone.")], [], "ok"),
            62: (READY, "OPEN", [comment(ME, iso_ago(minutes=35), CLOUD_DONE + "\nDone.")], [], "ok"),
            63: (NEEDS_INPUT, "CLOSED", [], [], "ok"),
            64: (READY, "CLOSED", [], [], "ok"),
            65: (IN_PROGRESS_LABELS, "OPEN", [], merged(65), "ok"),
            66: (IN_REVIEW, "OPEN", [], merged(66), "ok"),
            67: (NEEDS_INPUT, "OPEN", [], merged(67), "ok"),
        }
        records = {n: self.cloud_run(n, two) for n in cases}
        self.cloud_scenario([issue(n, labels=c[0], state=c[1], comments=c[2]) for n, c in cases.items()])
        self.with_prs({records[n]["branch"]: c[3] for n, c in cases.items()})
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        saved = {r["ticket"]: r for r in self.run_records()}
        start = parse_utc(two)
        for n, (labels, state, comments, prs, outcome) in cases.items():
            with self.subTest(ticket=n, labels=labels, state=state):
                self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), (outcome, outcome))
                if outcome is None:
                    self.assertIsNone(saved[n]["ended_at"])
                    self.assertEqual(rows[n]["state"], f"in the cloud ({records[n]['url']})")
                else:
                    ended = parse_utc(saved[n]["ended_at"])
                    self.assertTrue(start <= ended <= parse_utc(iso_ago(seconds=-60)), saved[n]["ended_at"])
                    self.assertEqual(rows[n]["state"], "ended ok")
        for n in (61, 62):
            self.assertEqual(saved[n]["ended_at"], cases[n][2][0]["createdAt"])     # at the marker's time
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])

        # a second sweep, as ten minutes later on #146, still leaves the open ones open
        self.ok_json("runs", "--sweep", "--json")
        saved = {r["ticket"]: r for r in self.run_records()}
        for n, c in cases.items():
            with self.subTest(again=n):
                self.assertEqual(saved[n]["outcome"], c[4])
                self.assertEqual(saved[n]["ended_at"] is None, c[4] is None)

    def test_only_a_pr_merged_since_the_runs_start_ends_it(self):
        """#295: branch names are reused (a reverted ticket, answered, starts a new cloud run on the same t/<n>-x),
        so a pull request of the branch that merged before the run started is an earlier run's, not an end of this
        one; only one merged at or after its start ends it ok."""
        start = parse_utc(iso_ago(hours=2))
        at = lambda **d: (start + dt.timedelta(**d)).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
        row = lambda n, when: {"number": n, "state": "MERGED", "isDraft": False, "mergedAt": when}  # noqa: E731
        answered = [comment(ME, at(hours=-6), QUESTION), comment(ME, at(minutes=-5), "Use main.")]
        cases = {   # ticket: (labels, comments, pr rows, expected outcome or None for open)
            # the revert shape: the old PR merged a day ago, the ticket was reopened needs-input and answered
            71: (NEEDS_INPUT, answered, [row(171, at(hours=-24))], None),
            72: (IN_PROGRESS_LABELS, [], [row(172, at(hours=-3))], None),
            73: (READY, [], [row(173, at(minutes=-1))], None),                       # a minute before the start
            74: (IN_REVIEW, [], [row(174, at(hours=-30))] + ready_pr(274), None),    # old merged, new one open
            75: (IN_REVIEW, [], [row(175, at(hours=-30)), row(275, at(minutes=-10))], None),   # two old ones
            # merged since the start, alone or beside an old one, in either order
            81: (NEEDS_INPUT, answered, [row(181, at(hours=-24)), row(281, at(minutes=90))], "ok"),
            82: (IN_REVIEW, [], [row(282, at(minutes=1)), row(182, at(hours=-5))], "ok"),
            83: (READY, [], [row(183, at(minutes=100))], "ok"),
        }
        records = {n: self.cloud_run(n, start.strftime("%Y-%m-%dT%H:%M:%SZ")) for n in cases}
        self.cloud_scenario([issue(n, labels=c[0], comments=c[1]) for n, c in cases.items()])
        self.with_prs({records[n]["branch"]: c[2] for n, c in cases.items()})
        for sweep in (1, 2):
            rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
            saved = {r["ticket"]: r for r in self.run_records()}
            for n, (labels, _, prs, outcome) in cases.items():
                with self.subTest(sweep=sweep, ticket=n, merged=[p.get("mergedAt") for p in prs]):
                    self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), (outcome, outcome))
                    if outcome is None:
                        self.assertIsNone(saved[n]["ended_at"])
                        self.assertEqual(rows[n]["state"], f"in the cloud ({records[n]['url']})")
                    else:
                        ended = parse_utc(saved[n]["ended_at"])
                        self.assertTrue(start <= ended <= parse_utc(iso_ago(seconds=-60)), saved[n]["ended_at"])
                        self.assertEqual(rows[n]["state"], "ended ok")
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["issue", "edit"]])

    def test_dispatch_keeps_a_cloud_run_live_whatever_its_tickets_label(self):
        """#295: a cloud run whose ticket is not labelled in-progress is still a live run for dispatch, so the
        ticket is not picked up again beside it."""
        for n, labels in ((12, NEEDS_INPUT), (13, READY)):
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                record = self.cloud_run(n, iso_ago(minutes=10))
                self.cloud_scenario([issue(n, labels=labels, comments=[comment(ME, iso_ago(hours=1), QUESTION)]),
                                     issue(14, title="Next")],
                                    ready=[ready_row(n), ready_row(14, "Next")])
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([r["number"] for r in data["plan"]], [14])
                self.assertEqual(data["live"], [record["id"]])
                [saved] = self.run_records()
                self.assertIsNone(saved["ended_at"])

    def test_a_blocked_marker_ends_the_run_blocked_and_asks_the_owner(self):
        two = iso_ago(hours=2)
        asked = {26: (iso_ago(minutes=60), "Should the cache live in ~/.cache or under the repo?"),
                 30: (iso_ago(minutes=25), "The ticket names two verify commands; which one is right?")}
        comments = {26: [comment(ME, asked[26][0], CLOUD_BLOCKED + "\n" + asked[26][1] + "\n")],
                    30: [comment(ME, iso_ago(minutes=90), CLOUD_DONE + "\nFirst pass."),
                         comment(ME, asked[30][0], CLOUD_BLOCKED + "\n" + asked[30][1])]}
        for n in asked:
            self.cloud_run(n, two)
        self.cloud_scenario([issue(n, labels=IN_PROGRESS_LABELS, comments=comments[n]) for n in asked])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        saved = {r["ticket"]: r for r in self.run_records()}
        for n, (when, question) in asked.items():
            with self.subTest(ticket=n):
                self.assertEqual((rows[n]["outcome"], saved[n]["outcome"]), ("blocked", "blocked"))
                self.assertEqual(saved[n]["ended_at"], when)
                self.assertEqual(rows[n]["state"], "ended blocked")
                self.assertIn("status:needs-input", self.added_labels(n))
                bodies = self.comment_bodies(n)
                asking = [b for b in bodies if question in b]
                self.assertEqual(len(asking), 1, bodies)
                self.assertNotIn(CLOUD_BLOCKED, asking[0])
                for m, (_, other) in asked.items():
                    if m != n:
                        self.assertNotIn(other, "\n".join(bodies))
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_a_failing_gh_call_is_one_warning_and_leaves_the_run_open(self):
        for failing in (["issue", "view", "12"], ["api", "user"]):
            with self.subTest(failing=failing):
                shutil.rmtree(self.runs_dir(), True)
                record = self.cloud_run(12, iso_ago(hours=2))
                self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS,
                                           comments=[comment(ME, iso_ago(hours=1), CLOUD_DONE)])],
                                    failing=[failing])
                p = self.work("runs", "--sweep", "--json")
                self.assertEqual(p.returncode, 0, p.stderr)
                [row] = json.loads(p.stdout)
                self.assertIsNone(row["ended_at"])
                self.assertEqual(self.run_records(), [record])
                warnings = [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:")]
                self.assertEqual(len(warnings), 1, p.stderr)

    def test_dispatch_treats_an_unended_cloud_run_as_live(self):
        record = self.cloud_run(12, iso_ago(hours=1))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS), issue(13, title="Next")],
                            ready=[ready_row(12), ready_row(13, "Next")])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([r["number"] for r in data["plan"]], [13])
        self.assertIn({"number": 12, "why": f"live run {record['id']}"}, data["skipped"])
        self.assertEqual(data["live"], [record["id"]])
        self.assertIsNone(self.run_records()[0]["ended_at"])

    def test_dispatch_sweeps_a_done_cloud_run_and_picks_the_ticket_up_again(self):
        when = iso_ago(minutes=40)
        self.cloud_run(12, iso_ago(hours=3))
        self.cloud_scenario([issue(labels=IN_PROGRESS_LABELS, comments=[comment(ME, when, CLOUD_DONE + "\nDone.")])])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        [record] = self.run_records()
        self.assertEqual((record["outcome"], record["ended_at"]), ("ok", when))
        self.assertEqual([(r["number"], r.get("again")) for r in data["plan"]], [(12, "ok")])
        self.assertEqual(data["live"], [])


class CloudCrossProject(CloudTest):
    """#299: `harness-work cloud N --repo B` run from a session of project A, whose settings are exported into the
    session's environment, puts B's own verify (or test) command in the prompt, never A's. A launch from B's own
    session keeps the environment first. Repo A is a separate git repository, so B's main checkout is not its."""

    A_VERIFY = "make verify-alpha-only"
    A_TEST = "npm run alpha-tests -- {files}"

    def setUp(self):
        super().setUp()
        self.alpha = self.mk("alpha")
        self.git(self.alpha, "init", "-q", "-b", "main")
        write(os.path.join(self.alpha, "README.md"), "# alpha\n")
        write(os.path.join(self.alpha, ".claude", "settings.json"),
              json.dumps({"env": {"CLAUDE_VERIFY_CMD": self.A_VERIFY, "HARNESS_TEST_CMD": self.A_TEST}}))
        self.git(self.alpha, "add", "-A")
        self.git(self.alpha, "commit", "-q", "-m", "Alpha")

    def cloud_prompt(self, settings, cwd=None, **exported):
        """The prompt of `harness-work cloud 12 --repo <B>` with B's committed settings `settings`, run from `cwd`
        with `exported` in the environment."""
        self.commit_settings(settings)
        self.cloud_scenario([issue(12)])
        p = self.work("cloud", "12", "--repo", self.repo, env=self.env(**exported), cwd=cwd or self.repo)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [call] = self.cloud_calls()
        argv = call["argv"]
        return argv[argv.index("--cloud") + 1]

    def own_prompt(self, settings):
        """The reference: the prompt B's own session gets with nothing exported, from a fresh fixture."""
        self.setUp()
        return self.cloud_prompt(settings)

    def test_a_launch_for_another_repository_takes_its_command_not_the_sessions(self):
        both = {"CLAUDE_VERIFY_CMD": self.A_VERIFY, "HARNESS_TEST_CMD": self.A_TEST}
        cases = (
            ("B verify and test, A exports both, HARNESS_SESSION_PROJECT",
             {"CLAUDE_VERIFY_CMD": "make verify-widgets", "HARNESS_TEST_CMD": "pytest -q {files}"},
             "HARNESS_SESSION_PROJECT", both, "make verify-widgets"),
            ("B test only, A exports both, CLAUDE_PROJECT_DIR",
             {"HARNESS_TEST_CMD": "python3 -m pytest tests -q"}, "CLAUDE_PROJECT_DIR", both,
             "python3 -m pytest tests -q"),
            ("B test only, A exports its verify only, HARNESS_SESSION_PROJECT",
             {"HARNESS_TEST_CMD": "go test ./..."}, "HARNESS_SESSION_PROJECT",
             {"CLAUDE_VERIFY_CMD": self.A_VERIFY}, "go test ./..."),
            ("B verify only, A exports both, CLAUDE_PROJECT_DIR",
             {"CLAUDE_VERIFY_CMD": "tox -e lint,py39"}, "CLAUDE_PROJECT_DIR", both, "tox -e lint,py39"),
        )
        for label, settings, signal, exported, want in cases:
            with self.subTest(label):
                expected = self.own_prompt(settings)
                self.assertIn(f"`{want}`", expected)          # the reference itself names B's command
                self.setUp()
                prompt = self.cloud_prompt(settings, cwd=self.alpha, **{signal: self.alpha}, **exported)
                self.assertIn(f"`{want}`", prompt)
                self.assertNotIn(self.A_VERIFY, prompt)
                self.assertNotIn("alpha-tests", prompt)
                self.assertEqual(prompt, expected)

    def test_another_repository_without_a_command_gets_no_command_from_the_session(self):
        for signal, exported in (("HARNESS_SESSION_PROJECT", {"CLAUDE_VERIFY_CMD": self.A_VERIFY,
                                                              "HARNESS_TEST_CMD": self.A_TEST}),
                                 ("CLAUDE_PROJECT_DIR", {"HARNESS_TEST_CMD": self.A_TEST})):
            with self.subTest(signal):
                expected = self.own_prompt({})
                self.setUp()
                prompt = self.cloud_prompt({}, cwd=self.alpha, **{signal: self.alpha}, **exported)
                self.assertNotIn(self.A_VERIFY, prompt)
                self.assertNotIn("alpha-tests", prompt)
                self.assertEqual(prompt, expected)

    def test_a_launch_from_the_repositorys_own_session_keeps_the_environment_first(self):
        settings = {"CLAUDE_VERIFY_CMD": "make verify-widgets", "HARNESS_TEST_CMD": "pytest -q {files}"}
        for label, signal, exported_verify in (("no session project", None, "make exported-check"),
                                               ("HARNESS_SESSION_PROJECT is B", "HARNESS_SESSION_PROJECT",
                                                "just verify-all"),
                                               ("HARNESS_SESSION_PROJECT is inside B", "inside", "make ci-local"),
                                               ("CLAUDE_PROJECT_DIR is B", "CLAUDE_PROJECT_DIR", "nox -s verify")):
            with self.subTest(label):
                self.setUp()
                exported = {"CLAUDE_VERIFY_CMD": exported_verify}
                if signal == "inside":
                    exported["HARNESS_SESSION_PROJECT"] = self.mk("proj", "docs", "deep")
                elif signal:
                    exported[signal] = self.repo
                prompt = self.cloud_prompt(settings, **exported)
                self.assertIn(f"`{exported_verify}`", prompt)
                self.assertNotIn("make verify-widgets", prompt)


if __name__ == "__main__":
    unittest.main()
