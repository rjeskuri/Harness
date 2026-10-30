"""harness-work's background and cloud sessions: stopping finished sessions, redispatch from a draft, cloud launch, refusal, failure and sweep.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_sessions.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


class StopFinishedSessions(WorkTest):
    def board_scenario(self, tickets, ready=(), failing=()):
        """tickets: {n: (labels, issue state, PRs of its branch t/<n>-x)}; `ready` the rows of `harness-board ready`.
        For a ticket in `failing`, `gh issue view n` exits 1 with a message on stderr, as gh does when the network or
        the token is gone."""
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

    # ---- broad: the ticket's status decides, across agents states, statuses and PR states
    def test_sweep_stops_the_session_of_every_finished_ticket_and_of_no_in_progress_one(self):
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, stopped, record closed, outcome)
            70: ("s-a11", "blocked", IN_REVIEW, "OPEN", ready_pr(120), True, True, None),
            71: ("s-b22", "blocked", NEEDS_INPUT, "OPEN", [], True, True, None),
            72: ("s-c33", "blocked", IN_REVIEW, "CLOSED", MERGED, True, True, None),
            73: ("s-d44", "done", IN_REVIEW, "OPEN", ready_pr(121), True, True, "ok"),
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
            (BLOCKED_STATUS, "OPEN", draft(153), False),
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

    def test_only_in_review_needs_input_or_closed_end_a_session_and_an_unreadable_ticket_stays_live(self):
        # The finished set is exactly status:in-review, status:needs-input or a CLOSED issue (closed wins over any
        # leftover label, status:in-progress included). An OPEN ticket with status:ready, status:blocked or no status
        # label is not finished: its live session is never stopped and holds a slot. Nor is a ticket gh cannot read.
        cases = {   # n: (bg id, agents state, labels, issue state, PRs, finished)
            120: ("s-rd0", "blocked", READY, "OPEN", [], False),
            121: ("s-bk1", "blocked", BLOCKED_STATUS, "OPEN", draft(190), False),
            122: ("s-ns2", "blocked", NO_STATUS, "OPEN", [], False),
            123: ("s-ci3", "blocked", IN_PROGRESS, "CLOSED", MERGED, True),
            124: ("s-gf4", "blocked", IN_REVIEW, "OPEN", ready_pr(191), False),     # gh issue view fails (below)
            125: ("s-ci5", "done", IN_PROGRESS, "CLOSED", MERGED, True),
            126: ("s-rv6", "blocked", IN_REVIEW, "OPEN", ready_pr(192), True),
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
        # runs --sweep alone: one `claude stop <id>`, reported, and the record closed.
        agents = self.bg_sessions({90: ("s-rev1", "blocked")})
        self.board_scenario({90: (IN_REVIEW, "OPEN", ready_pr(160))})
        p = self.work("runs", "--sweep", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.stops(), {"s-rev1": 1})
        self.assertIn("stopped session s-rev1", p.stdout + p.stderr)
        self.assertTrue(self.records()[90]["ended_at"])

        # dispatch: the session does not hold a slot, so --max N plans N launches, for each finished status. Whichever
        # command stops the session, it is stopped once in all.
        ready = [ready_row(n, f"Ready {n}") for n in (91, 92, 93, 94)]
        for labels, state in ((IN_REVIEW, "OPEN"), (NEEDS_INPUT, "OPEN"), (IN_REVIEW, "CLOSED")):
            for most in (1, 2, 3):
                with self.subTest(labels=labels, state=state, max=most):
                    self.fresh()
                    agents = self.bg_sessions({90: ("s-rev1", "blocked")})
                    self.board_scenario({90: (labels, state, ready_pr(160))}, ready=ready)
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
        self.board_scenario({90: (IN_REVIEW, "OPEN", ready_pr(160))}, ready=ready)
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
            28: (two, ("task", "status:in-review", "size:S", "risk:low"), "OPEN", [], "ok", "now"),
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


if __name__ == "__main__":
    unittest.main()
