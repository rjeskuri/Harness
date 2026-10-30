"""harness-work runs and dispatch's redispatch: the sweep, early endings, the redispatch cap and runs by repo field.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_runs.py" -v
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


class RedispatchCap(WorkTest):
    """Ticket #29: the redispatch-cap skip names the way forward (finish it in this session), carries its numbers
    in --json, and `runs` marks the capped ticket's newest ended record. The time in the skip line is the last
    run's `ended_at` exactly as the record holds it (ISO-8601 UTC, `...Z`), which is how harness-work prints every
    other time (budget reasons, run records). The cap itself (3 ended runs in 7 days) is unchanged."""

    def ended(self, ticket, outcomes, hours_ago):
        """One ended headless run per outcome, oldest first; hours_ago[i] is when run i ended. Returns the records."""
        records = []
        for i, (outcome, hours) in enumerate(zip(outcomes, hours_ago)):
            records.append(self.write_run(
                ticket, id=f"{self.key}-{ticket}-2026100{i + 1}090000", pid=self.dead_pid(),
                started_at=iso_ago(hours=hours + 1), ended_at=iso_ago(hours=hours), outcome=outcome))
        return records

    def skip_why(self, ticket, runs, last):
        return (f"{runs} runs ended without a ready pull request in 7 days (last: {last['outcome']} at "
                f"{last['ended_at']}); run it in this session with /harness:work {ticket}, or wait for the window")

    def capped_pair(self):
        """#12: three ended runs, the last ok; #15: four, the last early. Both in progress without a PR. (No billing
        or auth run here: those do not count toward the cap, ticket #50, so the counts 3 and 4 are unambiguous.)"""
        first = self.ended(12, ["error", "error", "ok"], [30, 20, 10])
        second = self.ended(15, ["error", "ok", "error", "early"], [60, 50, 40, 5])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        return first[-1], second[-1]

    def test_the_skip_names_the_in_session_path_and_the_window(self):
        last12, last15 = self.capped_pair()
        why12, why15 = self.skip_why(12, 3, last12), self.skip_why(15, 4, last15)

        p = self.ok("dispatch", "--dry-run")                   # the dry run prints the new line, per ticket
        self.assertIn("skip #12: " + why12, p.stdout.splitlines())
        self.assertIn("skip #15: " + why15, p.stdout.splitlines())
        self.assertNotIn("would start", p.stdout)
        self.assertNotIn("harness-board status", p.stdout)
        self.assertNotIn("by hand", p.stdout)

        data = self.ok_json("dispatch", "--dry-run", "--json")   # --json carries the numbers and the advice
        self.assertEqual(data["plan"], [])
        by_number = {s["number"]: s for s in data["skipped"]}
        self.assertEqual(sorted(by_number), [12, 15])
        self.assertEqual(by_number[12], {"number": 12, "why": why12, "runs": 3, "window_days": 7,
                                         "advice": "/harness:work 12"})
        self.assertEqual(by_number[15], {"number": 15, "why": why15, "runs": 4, "window_days": 7,
                                         "advice": "/harness:work 15"})

        p = self.ok("dispatch")                                # for real: nothing started, the same lines said
        self.assertIn("skip #12: " + why12, p.stdout.splitlines())
        self.assertIn("skip #15: " + why15, p.stdout.splitlines())
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 7)
        real = self.ok_json("dispatch", "--json")
        self.assertEqual({s["number"]: (s["why"], s["runs"], s["window_days"], s["advice"]) for s in real["skipped"]},
                         {12: (why12, 3, 7, "/harness:work 12"), 15: (why15, 4, 7, "/harness:work 15")})
        self.assertEqual(real["launched"], [])
        self.assertEqual(self.claude_calls(), [])

    def test_the_skip_text_follows_the_last_run_not_the_first(self):
        records = self.ended(12, ["early", "error", "ok"], [40, 30, 20])      # billing/auth would not count (#50)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + self.skip_why(12, 3, records[-1]), p.stdout.splitlines())
        for earlier in records[:-1]:
            self.assertNotIn(earlier["ended_at"], p.stdout)

    def test_a_ticket_under_the_cap_is_redispatched_with_no_skip(self):
        self.ended(13, ["error", "ok"], [30, 10])                       # two ended runs
        self.ended(16, ["error", "error", "ok"], [24 * 8, 20, 10])      # three, one outside the 7 days
        self.scenario(issues=[issue(13, labels=IN_PROGRESS, title="Two"),
                              issue(16, labels=IN_PROGRESS, title="Straddling")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Two (again after ok) and launch bg", p.stdout)
        self.assertIn("would start #16 Straddling (again after ok) and launch bg", p.stdout)
        self.assertNotIn("skip #", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["skipped"], [])
        self.assertEqual([row["number"] for row in data["plan"]], [13, 16])

    def test_other_skips_carry_no_cap_fields(self):
        self.ended(12, ["error", "error", "ok"], [30, 20, 10])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13), issue(14)],
                      ready=[ready_row(13, "Second"), ready_row(14, "Third")])
        data = self.ok_json("dispatch", "--dry-run", "--max", "1", "--json")
        by_number = {s["number"]: s for s in data["skipped"]}
        self.assertEqual(by_number[14]["why"], "over --max 1")
        self.assertEqual(sorted(by_number[12]), ["advice", "number", "runs", "why", "window_days"])
        self.assertNotIn("advice", by_number[14])
        self.assertNotIn("runs", by_number[14])

    def row_for(self, text, record):
        [row] = [line for line in text.splitlines() if record["id"] in line]
        return row

    def test_runs_marks_the_capped_ticket(self):
        capped = self.ended(12, ["error", "error", "ok"], [30, 20, 10])      # newest ended run is the third
        also = self.ended(15, ["error", "ok", "error", "billing"], [60, 50, 40, 5])
        two = self.ended(13, ["error", "ok"], [30, 10])                      # under the cap
        straddle = self.ended(16, ["error", "error", "ok"], [24 * 8, 20, 10])  # only two inside the 7 days
        stale = self.ended(17, ["error", "error", "ok"], [24 * 10, 24 * 9, 24 * 8])  # the window has passed
        self.scenario(issues=[issue(n, labels=IN_PROGRESS) for n in (12, 13, 15, 16, 17)])

        text = self.ok("runs", "--all").stdout
        self.assertIn("STATE", text.splitlines()[0])
        self.assertIn("capped", self.row_for(text, capped[-1]))
        self.assertIn("capped", self.row_for(text, also[-1]))
        for record in capped[:-1] + also[:-1] + two + straddle + stale:   # only the newest of a capped ticket
            self.assertNotIn("capped", self.row_for(text, record))
        self.assertEqual(text.count("capped"), 2)
        # the mark is added to the state, it does not replace the outcome
        self.assertIn("ok", self.row_for(text, capped[-1]))
        self.assertIn("billing", self.row_for(text, also[-1]))

        recent = self.ok("runs").stdout                                  # the default 24 h view marks it too
        self.assertIn("capped", self.row_for(recent, capped[-1]))
        self.assertEqual(recent.count("capped"), 2)

    def test_runs_marks_nothing_when_no_ticket_is_capped(self):
        records = self.ended(12, ["error", "ok"], [30, 10]) + self.ended(13, ["ok"], [5])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS)])
        text = self.ok("runs", "--all").stdout
        for record in records:
            self.assertIn(record["id"], text)
        self.assertNotIn("capped", text)

    # ---- what "the cap holds" means: exactly the condition under which dispatch emits the skip
    def run_at(self, ticket, outcome, started, ended, repo=None):
        """One ended headless run of `ticket`, started/ended that many hours ago, in `repo` (default: this one)."""
        self.seq = getattr(self, "seq", 0) + 1
        repo = repo or os.path.realpath(self.repo)
        prefix = self.key if repo == os.path.realpath(self.repo) else sha12(repo)
        return self.write_run(ticket, id=f"{prefix}-{ticket}-20261001{self.seq:06d}", repo=repo,
                              pid=self.dead_pid(), started_at=iso_ago(hours=started), ended_at=iso_ago(hours=ended),
                              outcome=outcome)

    def three(self, ticket, repo=None):
        """Three ended runs inside the window, oldest first."""
        return [self.run_at(ticket, o, s, e, repo) for o, s, e in (("error", 31, 30), ("error", 21, 20),
                                                                    ("ok", 11, 10))]

    def with_prs(self, *branches):
        """Add to the scenario, ahead of its generic `pr list` answer: a pull request for each of these branches."""
        data = self.read_json(self.scenario_path)
        data["calls"] = [{"match": ["pr", "list", "--head", b], "stdout_json": [{"number": 99}]}
                         for b in branches] + data["calls"]
        write(self.scenario_path, json.dumps(data))

    def live_run_of(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return self.write_run(ticket, id=f"{self.key}-{ticket}-20261001999999", pid=live.pid)

    def sibling(self, name="proj2"):
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.origin, path)
        return os.path.realpath(path)

    def marked(self, *extra):
        """{ticket: [ids of its records marked capped]} from `runs --all` (plus `extra` flags), the text table and
        --json both, which must say the same thing."""
        records = {r["id"]: r for r in self.run_records()}
        in_text = {}
        for line in self.ok("runs", "--all", *extra).stdout.splitlines()[1:]:
            ids = [rid for rid in records if rid in line]
            self.assertEqual(len(ids), 1, line)
            if "capped" in line:
                in_text.setdefault(records[ids[0]]["ticket"], []).append(ids[0])
        in_json = {}
        for row in self.ok_json("runs", "--all", *extra, "--json"):
            if row.get("capped"):
                self.assertIs(row["capped"], True)
                in_json.setdefault(row["ticket"], []).append(row["id"])
        self.assertEqual(in_text, in_json)
        return in_text

    def skipped_by_cap(self):
        """(sorted tickets dispatch skips for the cap, the whole dry-run --json)."""
        data = self.ok_json("dispatch", "--dry-run", "--json")
        return sorted(s["number"] for s in data["skipped"] if "advice" in s), data

    def test_a_ticket_with_a_pull_request_is_not_capped_and_not_skipped(self):
        self.three(12)                                       # a pull request exists for t/12-x
        control = self.three(15)                             # none for t/15-x
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.with_prs("t/12-x")
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])
        self.assertNotIn(12, [row["number"] for row in data["plan"]])

    def test_a_ticket_with_a_live_run_is_not_capped_and_not_skipped(self):
        self.three(12)
        live = self.live_run_of(12)
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual(data["live"], [live["id"]])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])

    def test_a_ticket_not_in_progress_or_not_open_is_not_capped_and_not_skipped(self):
        for n in (12, 13, 14):
            self.three(n)
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=("task", "status:ready", "size:S", "risk:low")),
                              issue(13, labels=IN_PROGRESS, state="CLOSED"),
                              issue(14, labels=("task", "status:needs-input", "size:S", "risk:low")),
                              issue(15, labels=IN_PROGRESS, title="Control")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([s["number"] for s in data["skipped"]], [15])

    def test_runs_of_the_same_ticket_number_in_another_repo_do_not_cap_this_repos_ticket(self):
        other = self.sibling()
        self.run_at(12, "error", 31, 30), self.run_at(12, "ok", 21, 20)    # two of ours ...
        self.run_at(12, "error", 11, 10, repo=other)                       # ... and a foreign one, the newest
        self.run_at(13, "error", 51, 50, repo=other)                       # a foreign one, the oldest ...
        self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 21, 20)    # ... and two of ours
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([row["number"] for row in data["plan"]], [12, 13])   # two of ours: redispatched

    def test_the_mark_goes_to_the_latest_started_run_as_dispatch_picks_it(self):
        # #12's runs overlap: the second started before the third but was swept later, so it ENDED newest while the
        # third STARTED newest. Dispatch reads the record that started last (its outcome is "last: ..."); so does `runs`.
        self.run_at(12, "error", 50, 40)
        self.run_at(12, "ok", 45, 2)                                      # newest ended_at
        newest_started = self.run_at(12, "early", 30, 20)                 # newest started_at
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        self.assertEqual(self.marked(), {12: [newest_started["id"]]})
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: " + self.skip_why(12, 3, newest_started), p.stdout.splitlines())

    def test_runs_json_adds_a_capped_flag_and_leaves_the_state_alone(self):
        records = self.three(12)
        under = [self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 11, 10)]
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS, title="Two")])
        rows = {r["id"]: r for r in self.ok_json("runs", "--all", "--json")}
        self.assertIs(rows[records[-1]["id"]].get("capped"), True)
        self.assertEqual(rows[records[-1]["id"]]["state"], "ended ok")        # the state is not mangled
        for record in records[:-1] + under:
            self.assertFalse(rows[record["id"]].get("capped"), record["id"])
            self.assertNotIn("capped", rows[record["id"]]["state"])
        text = self.ok("runs", "--all").stdout                                  # the table says it in STATE
        self.assertIn("ended ok capped", self.row_for(text, records[-1]))

    def test_runs_and_dispatch_agree_on_which_tickets_are_capped(self):
        other = self.sibling()
        cap12 = self.three(12)
        cap16 = [self.run_at(16, o, s, e) for o, s, e in (("error", 100, 90), ("billing", 80, 70),
                                                          ("early", 60, 50), ("auth", 30, 25), ("error", 11, 5))]
        self.three(13)                                                          # has a pull request
        self.three(14)                                                          # and a live run
        self.live_run_of(14)
        self.run_at(17, "error", 31, 30), self.run_at(17, "ok", 21, 20)         # two of ours ...
        self.run_at(17, "error", 11, 10, repo=other)                            # ... and one foreign
        self.run_at(18, "error", 24 * 8 + 1, 24 * 8)                            # one outside the window, two inside
        self.run_at(18, "error", 21, 20), self.run_at(18, "ok", 11, 10)
        for n in (19, 20, 21):
            self.three(n)                                                       # ready, closed, needs-input
        self.run_at(22, "error", 31, 30), self.run_at(22, "ok", 11, 10)         # under the cap
        self.scenario(issues=[
            issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS), issue(14, labels=IN_PROGRESS),
            issue(16, labels=IN_PROGRESS), issue(17, labels=IN_PROGRESS), issue(18, labels=IN_PROGRESS),
            issue(19, labels=("task", "status:ready", "size:S", "risk:low")),
            issue(20, labels=IN_PROGRESS, state="CLOSED"),
            issue(21, labels=("task", "status:needs-input", "size:S", "risk:low")),
            issue(22, labels=IN_PROGRESS)])
        self.with_prs("t/13-x")
        marked = self.marked()
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 16])
        self.assertEqual(sorted(marked), capped)                  # the same tickets, in `runs` and in `dispatch`
        self.assertEqual(marked, {12: [cap12[-1]["id"]], 16: [cap16[-1]["id"]]})   # one mark each: its newest run

    def test_a_stale_latest_run_means_no_skip_and_no_mark_whatever_else_ended_in_the_window(self):
        # #12's latest-started run ended before the 7-day cutoff; three older-started runs of it (overlapping it, swept
        # late) ended inside the window. Dispatch judges the latest record and moves on: no skip, so no mark either.
        for outcome, started, ended in (("error", 24 * 10 + 3, 30), ("error", 24 * 10 + 2, 20), ("ok", 24 * 10 + 1, 10)):
            self.run_at(12, outcome, started, ended)
        self.run_at(12, "error", 24 * 9, 24 * 8)                          # started last, ended outside the window
        control = self.three(15)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(15, labels=IN_PROGRESS, title="Second")])
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertNotIn(12, [s["number"] for s in data["skipped"]])

    def test_a_dead_unended_run_is_judged_by_its_process_after_the_sweep_as_dispatch_does(self):
        # `runs` without --sweep leaves a dead process's record un-ended, so the two can only agree once swept:
        # `runs --sweep` ends it (as dispatch does first) and it counts. #12: two ended runs and a dead un-ended one
        # make three; #13: two ended and a LIVE un-ended one stay under the cap; #15: three ended is the control.
        self.run_at(12, "error", 31, 30), self.run_at(12, "error", 21, 20)
        dead = self.write_run(12, id=f"{self.key}-12-20261001555555", pid=self.dead_pid(), started_at=iso_ago(hours=1))
        write(dead["log"], "claude exited before printing a result\n")
        self.run_at(13, "error", 31, 30), self.run_at(13, "error", 21, 20)
        live = self.live_run_of(13)
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        marked = self.marked("--sweep")
        self.assertEqual(marked, {12: [dead["id"]], 15: [control[-1]["id"]]})   # the swept run is #12's newest
        swept = {r["id"]: r for r in self.run_records()}
        self.assertTrue(swept[dead["id"]]["ended_at"])
        self.assertIsNone(swept[live["id"]]["ended_at"])
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 15])
        self.assertEqual(sorted(marked), capped)
        self.assertEqual(data["live"], [live["id"]])

    def test_a_ticket_whose_gh_check_fails_is_neither_skipped_nor_marked(self):
        # dispatch warns and moves on when it cannot tell whether #12 (pr list) or #13 (issue view) still lacks a pull
        # request; `runs` must not claim they are capped, and must not crash either. #15 is the control.
        self.three(12), self.three(13)
        control = self.three(15)
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"T{n}") for n in (12, 13, 15)])
        data = self.read_json(self.scenario_path)
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        data["calls"] = [dict(gone, contains=["pr", "list", "--head", "t/12-x"]),
                         dict(gone, match=["issue", "view", "13"])] + data["calls"]
        write(self.scenario_path, json.dumps(data))
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [15])
        self.assertEqual([row["number"] for row in data["plan"]], [])
        p = self.ok("dispatch", "--dry-run")
        for n in (12, 13):
            self.assertIn(f"#{n}: cannot check whether its ended run needs another", p.stderr)
            self.assertNotIn(f"skip #{n}:", p.stdout)

    def asked_about(self, ticket):
        """The gh calls that look the ticket up: its `issue view` and its branch's `pr list`."""
        return [c for c in self.gh_calls()
                if c[:3] == ["issue", "view", str(ticket)] or (c[:2] == ["pr", "list"] and f"t/{ticket}-x" in c)]

    def test_runs_asks_gh_only_about_tickets_at_the_cap(self):
        # #13 (two ended runs) and #16 (three, one outside the window: two inside) are under the cap, so `runs`
        # has nothing to mark for them and must not spend gh calls on them; #12 is the capped control.
        self.run_at(13, "error", 31, 30), self.run_at(13, "ok", 11, 10)
        self.run_at(16, "error", 24 * 8 + 1, 24 * 8), self.run_at(16, "error", 21, 20), self.run_at(16, "ok", 11, 10)
        control = self.three(12)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, labels=IN_PROGRESS, title="Two"),
                              issue(16, labels=IN_PROGRESS, title="Straddling")])
        self.assertEqual(self.marked(), {12: [control[-1]["id"]]})
        self.assertTrue(self.asked_about(12))                       # the capped ticket is still checked
        self.assertEqual(self.asked_about(13), [])
        self.assertEqual(self.asked_about(16), [])

        os.unlink(self.gh_log)                                      # dispatch still asks about them: candidates
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [13, 16])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])
        self.assertTrue(self.asked_about(13))
        self.assertTrue(self.asked_about(16))

    def test_runs_outside_a_repository_lists_the_records_marks_nothing_and_asks_gh_nothing(self):
        records = self.three(12)
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        plain = self.mk("not-a-repo")
        self.assertNotEqual(subprocess.run([GIT, "-C", plain, "rev-parse", "--git-dir"], env=self.env(),
                                           capture_output=True).returncode, 0)
        p = self.work("runs", "--all", cwd=plain)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for record in records:
            self.assertIn(record["id"], p.stdout)
        self.assertNotIn("capped", p.stdout)
        rows = json.loads(self.work("runs", "--all", "--json", cwd=plain).stdout)
        self.assertEqual(sorted(r["id"] for r in rows), sorted(r["id"] for r in records))
        self.assertFalse([r for r in rows if r.get("capped")])
        self.assertEqual(self.gh_calls(), [])
        # the same records, from inside the repository, are marked: the difference is the cwd alone
        self.assertEqual(self.marked(), {12: [records[-1]["id"]]})

    # ---- ticket #50: billing and auth runs are the budget's fault, not the ticket's: they do not count toward the cap
    def cap_lines(self, text):
        """{ticket: its skip line} for the cap skips in a dispatch's text output."""
        return {int(m.group(1)): line for line in text.splitlines()
                for m in [re.match(r"skip #(\d+): \d+ runs ended without a ready pull request", line)] if m}

    def assertCapSkip(self, line, n, last):
        self.assertTrue(line.startswith(f"skip #{n}: "), line)
        self.assertTrue(line.endswith(f"in 7 days (last: {last['outcome']} at {last['ended_at']}); run it in this "
                                      f"session with /harness:work {n}, or wait for the window"), line)

    def assertRelaunchedOnBg(self, failing):
        """`failing` holds, per ticket, outcomes that are all billing or auth. With the api budget exhausted each
        ticket is redispatched on bg with no cap skip and no `capped` mark; #15 (error, early, ok) is the capped
        control in the same dispatch."""
        self.write_budget("api", 12, "billing: credit balance is too low")
        titles = {n: f"Budget victim {n}" for n in failing}
        last = {}
        for n, outcomes in failing.items():
            hours = [10 * (len(outcomes) - i) + n % 7 for i in range(len(outcomes))]
            last[n] = self.ended(n, outcomes, hours)[-1]
        control = self.ended(15, ["error", "early", "ok"], [30, 20, 10])
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=titles[n]) for n in failing]
                      + [issue(15, labels=IN_PROGRESS, title="Control")])

        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})      # `runs` marks only the control

        p = self.ok("dispatch", "--dry-run", "--max", "9")
        self.assertIn("Dry run: venue bg,", p.stdout)
        for n in failing:
            self.assertIn(f"would start #{n} {titles[n]} (again after {last[n]['outcome']}) and launch bg",
                          p.stdout.splitlines())
            self.assertNotIn(f"skip #{n}:", p.stdout)
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        self.assertCapSkip(self.cap_lines(p.stdout)[15], 15, control[-1])
        data = self.ok_json("dispatch", "--dry-run", "--max", "9", "--json")
        self.assertEqual(data["venue"], "bg")
        self.assertEqual([row["number"] for row in data["plan"]], sorted(failing))
        self.assertEqual([s["number"] for s in data["skipped"]], [15])
        self.assertEqual(self.claude_calls(), [])

        p = self.ok("dispatch", "--max", "9", env=self.env(**CREDENTIALS))       # for real
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        for n in failing:
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id abc123\)$")
            self.assertNotIn(f"skip #{n}:", p.stdout)
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), sorted(f"/harness:work {n}" for n in failing))
        for call in calls:
            n = int(call["argv"][-1].split()[-1])
            self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {titles[n]}", "--permission-mode", "auto",
                                            f"/harness:work {n}"])
            for name in CREDENTIALS:
                self.assertNotIn(name, call["env"])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual(sorted((r["ticket"], r["venue"], r.get("bg_id")) for r in fresh),
                         [(n, "bg", "abc123") for n in sorted(failing)])

    def test_billing_runs_do_not_cap_a_ticket_and_it_is_redispatched_on_bg(self):
        self.assertRelaunchedOnBg({12: ["billing", "billing", "billing"],
                                   13: ["billing", "billing", "billing", "billing", "billing"]})

    def test_auth_runs_do_not_cap_a_ticket_and_it_is_redispatched_on_bg(self):
        self.assertRelaunchedOnBg({12: ["auth", "auth", "auth"],
                                   14: ["auth", "billing", "billing", "auth"],
                                   16: ["billing", "auth", "auth", "billing", "auth", "billing"]})

    def test_only_runs_other_than_billing_and_auth_count_toward_the_cap(self):
        # Only billing and auth are left out of the count: every other outcome counts, the bg ones (`stopped`,
        # `unknown`) and any state kept as is (`timeout`) included, not just ok, error and early.
        under = {12: ["early", "billing", "early", "billing"],          # fewer than three count: redispatched
                 13: ["billing", "billing", "early", "early"],
                 14: ["auth", "early", "billing", "error", "auth"],
                 17: ["error", "auth", "billing", "auth", "billing", "ok"],
                 19: ["early", "early"],
                 22: ["stopped", "auth", "billing"],
                 23: ["timeout", "billing", "unknown", "auth"]}
        capped = {16: ["early", "early", "early", "billing"],           # three or more count: capped
                  18: ["billing", "early", "error", "early"],
                  20: ["early", "auth", "error", "ok"],
                  21: ["auth", "error", "billing", "early", "billing", "ok", "auth"],
                  24: ["stopped", "unknown", "early", "billing"],
                  25: ["timeout", "billing", "timeout", "stopped", "auth", "early"],
                  26: ["unknown", "unknown", "auth", "unknown"]}
        counted = {16: 3, 18: 3, 20: 3, 21: 3, 24: 3, 25: 4, 26: 3}     # the runs that count, as the skip says
        last = {}
        for n, outcomes in sorted({**under, **capped}.items()):
            hours = [8 * (len(outcomes) - i) + n % 5 for i in range(len(outcomes))]
            last[n] = self.ended(n, outcomes, hours)[-1]
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"Mixed {n}") for n in sorted(last)])

        self.assertEqual(self.marked(), {n: [last[n]["id"]] for n in capped})

        p = self.ok("dispatch", "--dry-run", "--max", "9")
        self.assertIn("Dry run: venue bg,", p.stdout)
        for n in under:
            self.assertIn(f"would start #{n} Mixed {n} (again after {last[n]['outcome']}) and launch bg",
                          p.stdout.splitlines())
        lines = self.cap_lines(p.stdout)
        self.assertEqual(sorted(lines), sorted(capped))
        for n in capped:
            self.assertCapSkip(lines[n], n, last[n])
            self.assertEqual(lines[n], f"skip #{n}: " + self.skip_why(n, counted[n], last[n]))
            self.assertNotIn(f"would start #{n} ", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--max", "9", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], sorted(under))
        self.assertEqual(sorted(s["number"] for s in data["skipped"] if "advice" in s), sorted(capped))
        self.assertEqual({s["number"]: s for s in data["skipped"]},
                         {n: {"number": n, "why": self.skip_why(n, counted[n], last[n]), "runs": counted[n],
                              "window_days": 7, "advice": f"/harness:work {n}"} for n in capped})
        self.assertEqual(self.claude_calls(), [])

    def test_an_ended_run_without_an_outcome_counts_toward_the_cap(self):
        # Only billing and auth are left out: an ended record whose outcome is null still counts.
        none3 = self.ended(12, [None, None, None], [30, 20, 10])
        mixed = self.ended(13, [None, "early", "stopped", "billing"], [35, 25, 15, 5])
        self.ended(14, [None, "billing", None, "auth"], [36, 26, 16, 6])            # two count: redispatched
        self.scenario(issues=[issue(n, labels=IN_PROGRESS, title=f"Null {n}") for n in (12, 13, 14)])
        self.assertEqual(self.marked(), {12: [none3[-1]["id"]], 13: [mixed[-1]["id"]]})
        capped, data = self.skipped_by_cap()
        self.assertEqual(capped, [12, 13])
        self.assertEqual([row["number"] for row in data["plan"]], [14])
        self.assertEqual({s["number"]: s["runs"] for s in data["skipped"]}, {12: 3, 13: 3})

    def test_billing_runs_do_not_cap_a_ticket_after_the_budget_is_reset_and_it_relaunches_headless(self):
        self.write_budget("api", 12, "billing: credit balance is too low")
        last = self.ended(12, ["billing", "billing", "billing"], [30, 20, 10])[-1]
        control = self.ended(15, ["unknown", "error", "stopped"], [31, 21, 11])
        self.scenario(issues=[issue(12, labels=IN_PROGRESS, title="Credits added"),
                              issue(15, labels=IN_PROGRESS, title="Control")])
        self.ok("budget", "reset", "api")                                   # credits added
        self.assertEqual(self.marked(), {15: [control[-1]["id"]]})
        p = self.ok("dispatch", "--venue", "headless", "--dry-run")
        self.assertIn(f"would start #12 Credits added (again after {last['outcome']}) and launch headless",
                      p.stdout.splitlines())
        self.assertEqual(sorted(self.cap_lines(p.stdout)), [15])
        p = self.ok("dispatch", "--venue", "headless")
        self.assertRegex(p.stdout, r"(?m)^#12 headless on t/12-\S+ \(pid \d+, log ")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"][:7], ["-p", "/harness:work 12", "--permission-mode", "auto",
                                            "--output-format", "json", "--settings"])
        [fresh] = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual((fresh["ticket"], fresh["venue"]), (12, "headless"))
        self.assertTrue(wait_for(lambda: pid_gone(fresh["pid"])))


class DispatchRunsByRepoField(WorkTest):
    """Which run records dispatch counts as this repository's (ticket #7): a record whose `repo` field is
    another spelling of the main tree is ours even when its id carries another key (written before the
    key was canonical); a record of another repository is not ours, even when its path shares a prefix
    with ours or differs from it only in case. Every record here has an id that does NOT start with this
    repository's key, so only the repo field can decide."""

    def spelled_repo(self):
        """The main checkout in another case spelling; the test is skipped where that is another path."""
        spelled = os.path.join(self.tmp, "PROJ")
        if not (os.path.exists(spelled) and os.path.samefile(spelled, self.repo)):
            self.skipTest("two spellings of one path need a case-insensitive file system")
        self.assertNotEqual(sha12(spelled), self.key)
        return spelled

    def sibling(self, name="proj2"):
        """Another repository next to ours whose path starts with ours."""
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.origin, path)
        self.assertNotEqual(sha12(path), self.key)
        return path

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def live_run(self, ticket, repo):
        rid = f"{sha12(repo)}-{ticket}-20261013110000"
        return self.write_run(ticket, id=rid, repo=repo, pid=self.sleeper(ticket),
                              worktree=os.path.join(repo, ".claude", "worktrees", f"t-{ticket}-x"))

    def ended_runs(self, ticket, repo, count=3):
        for i in range(count):
            hours = count - i
            self.write_run(ticket, id=f"{sha12(repo)}-{ticket}-202610130{i}0000", repo=repo, pid=self.dead_pid(),
                           started_at=iso_ago(hours=hours, minutes=30), ended_at=iso_ago(hours=hours),
                           outcome="error")

    def test_a_live_run_recorded_in_another_spelling_is_this_repos_and_takes_a_slot(self):
        spelled = self.spelled_repo()
        other = self.sibling()
        ours = self.live_run(13, spelled)
        theirs = self.live_run(14, other)
        self.scenario(issues=[issue(), issue(13), issue(14), issue(15)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third"),
                             ready_row(15, "Fourth")])
        p = self.ok("dispatch", "--dry-run", "--max", "3")
        self.assertIn("venue bg, 1 live, 2 of 3 slots free", p.stdout)
        self.assertIn(f"skip #13: live run {ours['id']}", p.stdout)
        self.assertNotIn("would start #13", p.stdout)
        # the sibling's live run on #14 is not ours: #14 is planned here, and the sibling's run is not named
        self.assertIn("would start #12 Add the board tool (tier sonnet)", p.stdout)
        self.assertIn("would start #14 Third (tier sonnet)", p.stdout)
        self.assertNotIn(theirs["id"], p.stdout)
        self.assertIn("skip #15: over --max 3", p.stdout)

        data = self.ok_json("dispatch", "--dry-run", "--max", "3", "--json")
        self.assertEqual(data["live"], [ours["id"]])
        self.assertEqual([row["number"] for row in data["plan"]], [12, 14])
        self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                         [(13, f"live run {ours['id']}"), (15, "over --max 3")])

    def test_the_cap_counts_only_this_repos_live_runs_in_any_spelling(self):
        spelled = self.spelled_repo()
        first, second = self.live_run(13, spelled), self.live_run(14, self.repo + os.sep)
        for name in ("proj2", "proj-old"):                     # two live runs elsewhere: not ours
            self.live_run(20 if name == "proj2" else 21, self.sibling(name))
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        p = self.ok("dispatch", "--max", "2")                  # for real: both slots are taken by our runs
        self.assertIn("2 live runs; --max 2 reached, nothing launched.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        data = self.ok_json("dispatch", "--dry-run", "--max", "3", "--json")
        self.assertEqual(sorted(data["live"]), sorted([first["id"], second["id"]]))
        self.assertEqual([row["number"] for row in data["plan"]], [12])

    def test_ended_runs_recorded_in_another_spelling_count_toward_the_redispatch_cap(self):
        spelled = self.spelled_repo()
        other = self.sibling()
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.ended_runs(12, spelled)                            # ours: three ended, the fourth is skipped
        self.ended_runs(14, other)                              # the sibling's: never ours to redispatch or cap
        self.scenario(issues=[issue(labels=in_progress), issue(14, labels=in_progress, title="Theirs")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("skip #12: 3 runs ended without a ready pull request in 7 days (last: error at ", p.stdout)
        self.assertIn("run it in this session with /harness:work 12, or wait for the window", p.stdout)
        self.assertNotIn("would start #12", p.stdout)
        self.assertNotIn("#14", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)

    def test_two_ended_runs_in_another_spelling_redispatch_the_ticket(self):
        spelled = self.spelled_repo()
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.ended_runs(12, spelled, count=2)
        self.scenario(issues=[issue(labels=in_progress)])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Add the board tool (again after error) and launch bg", p.stdout)

    def test_a_run_of_a_directory_differing_only_in_case_is_not_ours_on_a_case_sensitive_file_system(self):
        upper = os.path.join(self.tmp, "PROJ")
        if os.path.exists(upper):
            self.skipTest("PROJ is another spelling of proj on this (case-insensitive) file system")
        self.git(self.tmp, "clone", "-q", self.origin, upper)
        theirs = self.live_run(13, upper)
        self.ended_runs(12, upper)
        in_progress = ("task", "status:in-progress", "size:S", "risk:low")
        self.scenario(issues=[issue(labels=in_progress), issue(13)], ready=[ready_row(13, "Second")])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["live"], [])
        self.assertEqual([row["number"] for row in data["plan"]], [13])
        self.assertEqual(data["skipped"], [])
        self.assertNotIn(theirs["id"], json.dumps(data))


# =========================================================================== runs
class Runs(WorkTest):
    def test_sweep_classifies_billing_and_marks_the_api_budget(self):
        record = self.write_run(12, pid=self.dead_pid())
        low = dict(API_401, api_error_status=400, result="Credit balance is too low")   # told by the result text
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(low) + "\n")
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual((swept["id"], swept["outcome"]), (record["id"], "billing"))
        self.assertTrue(swept["ended_at"])
        self.assertEqual(self.run_records()[0]["outcome"], "billing")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertIn("credit balance", api["reason"].lower())
        self.assertTrue(api["reason"].startswith("billing: "))
        self.assertIn("api: exhausted until", self.ok("budget", "status").stdout)

        # The next dispatch falls back to a background session.
        self.scenario(issues=[issue()], ready=[ready_row(13, "Next")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Next (tier sonnet) and launch bg", p.stdout)

    def test_sweep_classifies_ok_auth_and_error(self):
        logs = {
            12: json.dumps({"type": "result", "is_error": False, "result": "Fixed the billing page; see 401 and 402."}),
            13: "Invalid API key · Please run /login",                  # no JSON result: the API-error lines
            14: "Traceback: something else broke",
            15: "warning: a hook printed 402 Payment Required",           # not an API-error line
        }
        for n, text in logs.items():
            record = self.write_run(n, pid=self.dead_pid())
            write(record["log"], text + "\n")
        swept = {r["ticket"]: r["outcome"] for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual(swept, {12: "ok", 13: "auth", 14: "error", 15: "error"})
        api = self.read_json(self.budgets_path())["api"]
        self.assertTrue(api["reason"].startswith("auth: Invalid API key"), api["reason"])

    def sweep_one(self, n, result):
        record = self.write_run(n, pid=self.dead_pid())
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        return next(r for r in self.ok_json("runs", "--sweep", "--json") if r["ticket"] == n)

    def test_sweep_reads_the_api_error_fields_of_the_result(self):
        # Other API errors and the model's own words never exhaust a budget.
        overloaded = dict(API_401, api_error_status=529, result="Overloaded")
        own_words = {k: v for k, v in API_401.items() if k not in ("terminal_reason", "api_error_status")}
        own_words["result"] = "Fix the billing page: 401 handling"
        for n, result in ((12, overloaded), (13, own_words)):
            with self.subTest(result=result["result"]):
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "error")
                self.assertIsNone(swept["exit"])
                self.assertFalse(os.path.exists(self.budgets_path()))

        # The real invalid-key result: auth, and the api budget is marked.
        self.assertEqual(self.sweep_one(14, API_401)["outcome"], "auth")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertEqual(api["reason"], "auth: API error 401: Invalid API key \u00b7 Fix external API key")

        # The same result with 402: billing.
        self.ok("budget", "reset", "api")
        self.assertEqual(self.sweep_one(15, dict(API_401, api_error_status=402))["outcome"], "billing")
        self.assertTrue(self.read_json(self.budgets_path())["api"]["reason"].startswith("billing: API error 402"))

    def test_sweep_classifies_an_unscoped_api_key_as_auth_and_falls_back_to_bg(self):
        # The real 400 of 2026-10-13: a 400 is no auth status, the result text names the key.
        swept = self.sweep_one(12, API_400_WORKSPACE)
        self.assertEqual(swept["outcome"], "auth")
        self.assertEqual(self.run_records()[0]["outcome"], "auth")
        api = self.read_json(self.budgets_path())["api"]
        self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
        self.assertTrue(api["reason"].startswith("auth: API error 400: API Error: 400 This API key is not scoped"),
                        api["reason"])
        self.assertIn("anthropic-workspace-id", api["reason"])
        self.assertIn("api: exhausted until", self.ok("budget", "status").stdout)

        # The next dispatch falls back to a background session instead of retrying the key headless.
        self.scenario(issues=[issue()], ready=[ready_row(13, "Next")])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #13 Next (tier sonnet) and launch bg", p.stdout)

    def test_sweep_classifies_key_and_helper_failures_as_auth_whatever_the_4xx(self):
        # (result, the status the reason must name, a fragment of the result the reason must carry)
        cases = [
            (API_400_WORKSPACE, "400", "not scoped to a workspace"),
            (API_401_HELPER, "401", "apiKeyHelper script is failing"),
            # The helper text under a status that is not an auth status: the text decides.
            (dict(API_401_HELPER, api_error_status=400,
                  result="API Error: 400 Your apiKeyHelper script is failing: timed out after 10s"),
             "400", "timed out after 10s"),
            (dict(API_401_HELPER, api_error_status=400,
                  result="Your apiKeyHelper script is failing: security: item not found"),
             "400", "security: item not found"),
            # Any other 4xx naming the API key.
            (dict(API_401, api_error_status=404,
                  result="API Error: 404 This API key does not have access to model claude-opus-5-5"),
             "404", "does not have access to model"),
            (dict(API_401, api_error_status=400,
                  result="API Error: 400 Your API key belongs to an archived workspace"),
             "400", "archived workspace"),
            # The workspace header named without the words "API key".
            (dict(API_401, api_error_status=400,
                  result="API Error: 400 Missing anthropic-workspace-id header for an organization credential"),
             "400", "organization credential"),
        ]
        for n, (result, status, fragment) in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                self.ok("budget", "reset", "api")
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "auth")
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(f"auth: API error {status}: "), api["reason"])
                self.assertIn(fragment, api["reason"])

        # terminal_reason api_error without a status: the text still decides, and the reason names no status.
        self.ok("budget", "reset", "api")
        no_status = {k: v for k, v in API_400_WORKSPACE.items() if k != "api_error_status"}
        self.assertEqual(self.sweep_one(40, no_status)["outcome"], "auth")
        reason = self.read_json(self.budgets_path())["api"]["reason"]
        self.assertTrue(reason.startswith("auth: API error: API Error: 400 This API key"), reason)

    def test_sweep_keeps_rate_limits_overloads_and_other_errors_as_error(self):
        # Guard against over-matching: transient statuses and the model's own words never exhaust a budget,
        # even when their text names an API key, the workspace header or the helper.
        own_words = {k: v for k, v in API_400_WORKSPACE.items() if k not in ("terminal_reason", "api_error_status")}
        cases = [
            dict(API_401, api_error_status=529, result="Overloaded"),
            dict(API_401, api_error_status=529, result="API Error: 529 Overloaded"),
            dict(API_401, api_error_status=429, result=(
                "API Error: 429 Number of request tokens has exceeded your per-minute rate limit for this API key")),
            dict(API_401, api_error_status=500,
                 result="API Error: 500 Internal server error while validating the API key"),
            dict(API_401, api_error_status=503,
                 result="API Error: 503 Service unavailable; API key checks are degraded"),
            # An explicit 429 or 5xx stays transient even when the text names the helper or the workspace header.
            dict(API_401, api_error_status=503, result=API_401_HELPER["result"].replace("401", "503", 1)),
            dict(API_401, api_error_status=429, result=API_400_WORKSPACE["result"].replace("400", "429", 1)),
            # Other 4xx: only the text tells auth from error, and these name neither key, helper nor header.
            dict(API_401, api_error_status=400,
                 result="API Error: 400 prompt is too long: 212000 tokens > 200000 maximum"),
            dict(API_401, api_error_status=400,
                 result="API Error: 400 messages.0.content: Input should be a valid list"),
            dict(API_401, api_error_status=404, result="API Error: 404 model: claude-foo not found"),
            dict(API_401, api_error_status=413, result="API Error: 413 Request too large"),
            dict(API_401, api_error_status=400,
                 result="API Error: 400 invalid_request_error: tool_use ids must be unique"),
            dict(API_401, api_error_status=408, result="API Error: 408 Request timeout"),
            dict(API_401, api_error_status=409, result="API Error: 409 Conflict"),
            # terminal_reason api_error without a status, and nothing about a key in the text.
            {k: v for k, v in dict(API_401, result="API Error: Connection error.").items()
             if k != "api_error_status"},
            {k: v for k, v in dict(API_401, result="API Error: Request timed out.").items()
             if k != "api_error_status"},
            own_words,
            dict(own_words, result="Your apiKeyHelper script is failing, so I added a retry to it."),
        ]
        for n, result in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], "error")
                self.assertFalse(os.path.exists(self.budgets_path()))

        # A run that succeeded is ok whatever its text says.
        ok = {"type": "result", "is_error": False, "result": API_400_WORKSPACE["result"]}
        self.assertEqual(self.sweep_one(50, ok)["outcome"], "ok")
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_sweep_reads_key_and_workspace_in_their_credential_sense_only(self):
        # The words "key" and "workspace" on their own decide nothing. A 4xx about an unexpected key in the request
        # schema, a missing workspace file or a keychain read is `error`; the API key named as the credential, the
        # apiKeyHelper or the anthropic-workspace-id header is `auth`. Interleaved and worded unlike the real probes, so
        # neither a substring match on either word nor a list of the probes' phrases passes.
        no_status = {k: v for k, v in API_401.items() if k != "api_error_status"}   # terminal_reason api_error only
        cases = [
            # (result, outcome, the start of the api budget's reason when auth, a fragment of the text it must carry)
            (dict(API_401, api_error_status=400, result="API Error: 400 tools.0.input_schema: unexpected key 'foo'"),
             "error", None, None),
            (dict(API_401, api_error_status=400, result="API Error: 400 The provided API key has been disabled"),
             "auth", "auth: API error 400: ", "has been disabled"),
            (dict(API_401, api_error_status=400, result="API Error: 400 metadata: key user_id must be a string"),
             "error", None, None),
            (dict(API_401, api_error_status=404, result="API Error: 404 workspace file not found: /tmp/x"),
             "error", None, None),
            (dict(API_401, api_error_status=404, result="API Error: 404 No API key found for this organization"),
             "auth", "auth: API error 404: ", "No API key found"),
            (dict(API_401, api_error_status=422,
                  result="API Error: 422 The anthropic-workspace-id header names a deleted workspace"),
             "auth", "auth: API error 422: ", "names a deleted workspace"),
            (dict(API_401, api_error_status=400, result="API Error: 400 cache_control: unexpected key 'ttl'"),
             "error", None, None),
            # terminal_reason api_error without a status: the same words, the same split.
            (dict(no_status, result="API Error: Connection error while reading keychain"), "error", None, None),
            (dict(no_status, result="API Error: 400 Unexpected key 'workspace' in request body"), "error", None, None),
            (dict(no_status, result="apiKeyHelper script is failing: produced no output · Fix the apiKeyHelper "
                                    "setting"),
             "auth", "auth: API error: apiKeyHelper script is failing", "produced no output"),
        ]
        for n, (result, outcome, prefix, fragment) in enumerate(cases, start=12):
            with self.subTest(result=result["result"]):
                self.ok("budget", "reset", "api")
                swept = self.sweep_one(n, result)
                self.assertEqual(swept["outcome"], outcome)
                self.assertEqual(next(r for r in self.run_records() if r["ticket"] == n)["outcome"], outcome)
                status = self.ok("budget", "status").stdout
                if outcome == "error":
                    self.assertIn("api: available", status)
                    continue
                self.assertIn("api: exhausted until", status)
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(prefix), api["reason"])
                self.assertIn(fragment, api["reason"])

    def test_sweep_closes_a_bg_run_on_any_state_that_is_not_live(self):
        for n, bg_id in ((12, "s-blocked"), (13, "s-idle"), (14, "s-done"), (15, "s-archived"), (16, "s-gone"),
                         (17, "s-nostate")):
            self.write_run(n, venue="bg", bg_id=bg_id)
        agents = json.dumps([{"id": "s-blocked", "kind": "background", "state": "blocked"},
                             {"id": "s-idle", "kind": "background", "state": "idle"},
                             {"id": "s-done", "kind": "background", "state": "done", "exitCode": 0},
                             {"id": "s-archived", "kind": "background", "state": "archived"},
                             {"id": "s-nostate", "kind": "background"}])
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                     env=self.env(FAKECLAUDE_AGENTS=agents))}
        self.assertEqual({n: rows[n]["outcome"] for n in rows},
                         {12: None, 13: None, 14: "ok", 15: "archived", 16: "unknown", 17: None})
        self.assertEqual(rows[14]["exit"], 0)
        self.assertIsNone(rows[15]["exit"])
        self.assertEqual((rows[12]["state"], rows[13]["state"], rows[17]["state"]), ("blocked", "idle", "live"))

    def test_sweep_leaves_a_live_run_alone(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 12"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(12, pid=live.pid)
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertIsNone(row["ended_at"])
        self.assertEqual(row["state"], f"alive (pid {live.pid})")

    def test_sweep_treats_a_recycled_pid_as_ended(self):
        later = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(later.wait)
        self.addCleanup(later.kill)
        self.write_run(12, pid=later.pid, started_at=iso_ago(hours=1))   # the pid now belongs to a younger process
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertTrue(row["ended_at"])
        self.assertEqual(row["outcome"], "error")

    def test_runs_lists_recent_runs_and_all_with_all(self):
        self.write_run(12, ended_at=iso_ago(days=3), outcome="ok", pid=1)
        self.write_run(13, ended_at=iso_ago(hours=1), outcome="error", pid=1)
        self.assertEqual([r["ticket"] for r in self.ok_json("runs", "--json")], [13])
        self.assertEqual(sorted(r["ticket"] for r in self.ok_json("runs", "--all", "--json")), [12, 13])
        text = self.ok("runs").stdout
        self.assertIn("RUN", text)
        self.assertIn("proj #13", text)
        self.assertIn("ended error", text)


class EndedEarly(WorkTest):
    def write_scenario(self, tickets, branches=None, states=None, failing=None):
        """tickets: {n: (labels, open_prs, all_prs)}. `pr list --head <branch>` (t/<n>-x unless branches[n] says
        otherwise) gives open_prs; with --state all it gives all_prs (merged and closed included), as real gh does.
        Any other pr list gives []. states[n] is the issue's state when it is not OPEN. failing[n] names the gh call
        that fails for ticket n ("issue": `issue view n`; "pr": every `pr list --head <its branch>`): exit 1 with a
        message on stderr and nothing on stdout, as gh does when the network or the token is gone."""
        branches, states, failing = branches or {}, states or {}, failing or {}
        gone = {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}
        calls = []
        for n, (labels, open_prs, all_prs) in tickets.items():
            if failing.get(n) == "issue":
                calls.append(dict(gone, match=["issue", "view", str(n)]))
            else:
                calls.append({"match": ["issue", "view", str(n)],
                              "stdout_json": issue(n, labels=labels, state=states.get(n, "OPEN"))})
        for n, (labels, open_prs, all_prs) in tickets.items():
            branch = branches.get(n, f"t/{n}-x")
            if failing.get(n) == "pr":
                calls.append(dict(gone, contains=["pr", "list", "--head", branch]))
                continue
            calls.append({"contains": ["pr", "list", "--head", branch, "--state", "all"], "stdout_json": all_prs})
            calls.append({"contains": ["pr", "list", "--head", branch], "stdout_json": open_prs})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": []},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def ended_run(self, n, result, warning=True, **fields):
        """A headless run of ticket n whose process is gone, with `result` (a dict, or raw text) as its log, after the
        connectors warning unless warning=False."""
        record = self.write_run(n, pid=self.dead_pid(), **fields)
        text = json.dumps(result) if isinstance(result, dict) else result
        write(record["log"], (CONNECTORS_WARNING + "\n" if warning else "") + text + "\n")
        return record

    def fresh(self):
        """Forget every run record and gh call, for the next pass of a test."""
        shutil.rmtree(self.runs_dir(), True)
        if os.path.exists(self.gh_log):
            os.unlink(self.gh_log)

    def run_lines(self):
        """{ticket: its line} of `harness-work runs` (no sweep)."""
        return {int(m.group(1)): line for line in self.ok("runs").stdout.splitlines()
                for m in [re.search(r"proj #(\d+)\b", line)] if m}

    def sweep_pair(self, **scenario):
        """Two ended runs with the same log, both tickets in progress with no PR; the scenario keywords make #8
        differ. Returns {ticket: row} of the sweep after checking that both runs were ended."""
        for n in (7, 8):
            self.ended_run(n, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, [], [])}, **scenario)
        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual(sorted(rows), [7, 8])
        for n in (7, 8):
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertIsNone(rows[n]["exit"])
        return rows

    def assertEarlyAndOk(self, rows):
        """#7 ended early, #8 ended ok: in the sweep's rows, the saved records and the runs listing."""
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {7: "early", 8: "ok"})
        self.assertEqual((rows[7]["state"], rows[8]["state"]), ("ended early (no PR)", "ended ok"))
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {7: "early", 8: "ok"})
        lines = self.run_lines()
        self.assertIn("ended early (no PR)", lines[7])
        self.assertIn("ended ok", lines[8])
        self.assertNotIn("early", lines[8])
        self.assertIn("api: available", self.ok("budget", "status").stdout)

    def test_a_closed_issue_still_labelled_in_progress_is_ok_not_early(self):
        # Same labels, same log, no PR either way: only the issue's state differs. A closed ticket is never worked
        # again (the redispatch skips issues that are not OPEN), so its run did not end early.
        self.assertEarlyAndOk(self.sweep_pair(states={8: "CLOSED"}))

    def test_a_failing_issue_view_leaves_the_run_ok(self):
        # gh cannot say what the board holds for #8: the sweep still ends its run, as ok (not early: the facts that
        # make early are missing), exits 0, and classifies #7 all the same. A warning on stderr is fine.
        self.assertEarlyAndOk(self.sweep_pair(failing={8: "issue"}))

    def test_a_failing_pr_list_leaves_the_run_ok(self):
        # The board says in-progress, but gh cannot list the branch's PRs: the same, ok, exit 0, #7 still early.
        self.assertEarlyAndOk(self.sweep_pair(failing={8: "pr"}))

    def test_ok_run_on_an_in_progress_ticket_without_a_pr_is_ended_early(self):
        record = self.ended_run(7, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], [])})

        text = self.ok("runs", "--sweep").stdout
        self.assertIn("proj #7", text)
        self.assertIn("ended early (no PR)", text)
        self.assertNotIn("ended ok", text)

        [saved] = self.run_records()
        self.assertEqual((saved["id"], saved["outcome"], saved["exit"]), (record["id"], "early", None))
        self.assertTrue(saved["ended_at"])

        [row] = self.ok_json("runs", "--json")             # no sweep: the saved outcome alone gives the state
        self.assertEqual((row["outcome"], row["state"]), ("early", "ended early (no PR)"))
        self.assertIn("ended early (no PR)", self.ok("runs").stdout)
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

        # The redispatch says why it runs the ticket again.
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #7 Add the board tool (again after early)", p.stdout)

    def test_sweep_json_reports_early_on_the_sweep_itself(self):
        self.ended_run(7, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], [])})
        [row] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual((row["ticket"], row["outcome"], row["state"]), (7, "early", "ended early (no PR)"))
        self.assertTrue(row["ended_at"])

    def test_sweep_labels_early_only_for_ok_runs_on_in_progress_tickets_without_any_pr(self):
        own_words = {k: v for k, v in API_401.items() if k not in ("terminal_reason", "api_error_status")}
        own_words["result"] = "I could not finish: the tests do not import."
        pr_open = [{"number": 40, "state": "OPEN", "isDraft": True}]
        pr_merged = [{"number": 41, "state": "MERGED", "isDraft": False}]
        # n: (log result, labels, open PRs, PRs of any state, outcome)
        cases = {
            12: (EARLY_RESULT, IN_PROGRESS, [], [], "early"),
            13: ({"type": "result", "is_error": False, "result": "PR #31 is ready for review."},
                 IN_PROGRESS, [], [], "early"),                           # the text claims a PR; there is none
            14: (EARLY_RESULT, IN_PROGRESS, pr_open, pr_open, "early"),   # only a draft PR: still early (#42)
            15: (EARLY_RESULT, IN_PROGRESS, [], pr_merged, "ok"),          # a merged PR counts (--state all)
            16: (EARLY_RESULT, ("task", "status:in-review", "size:S", "risk:low"), [], [], "ok"),
            17: (EARLY_RESULT, ("task", "status:needs-input", "size:S", "risk:low"), [], [], "ok"),
            18: (EARLY_RESULT, ("task", "status:ready", "size:S", "risk:low"), [], [], "ok"),
            19: ("Traceback: something else broke", IN_PROGRESS, [], [], "error"),
            20: (dict(API_401, api_error_status=402), IN_PROGRESS, [], [], "billing"),
            21: (API_401, IN_PROGRESS, [], [], "auth"),
            22: (own_words, IN_PROGRESS, [], [], "error"),
            23: ({"type": "result", "is_error": False, "result": "Done."}, IN_PROGRESS, [], [], "early"),
            24: (EARLY_RESULT, IN_PROGRESS, [], [{"number": 42, "state": "CLOSED", "isDraft": False}], "ok"),
            25: (EARLY_RESULT, IN_PROGRESS, [], [], "ok"),                # the issue itself is CLOSED (below)
        }
        for n, (result, *_rest) in cases.items():
            self.ended_run(n, result)
        self.write_scenario({n: (labels, open_prs, all_prs) for n, (_r, labels, open_prs, all_prs, _o)
                             in cases.items()}, states={25: "CLOSED"})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        expected = {n: c[-1] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        saved = {r["ticket"]: r["outcome"] for r in self.run_records()}
        self.assertEqual(saved, expected)
        states = {n: ("ended early (draft PR #40)" if n == 14 else "ended early (no PR)") if outcome == "early"
                  else f"ended {outcome}" for n, outcome in expected.items()}
        for n, state in states.items():
            with self.subTest(ticket=n):
                self.assertEqual(rows[n]["state"], state)

        lines = self.run_lines()
        for n, outcome in expected.items():
            with self.subTest(ticket=n, text=True):
                self.assertEqual("ended early" in lines[n], outcome == "early", lines[n])
                if outcome == "early":
                    self.assertIn(states[n], lines[n])

    def test_only_the_board_and_the_pr_decide_early_never_the_ticket_number_or_the_log(self):
        # Every ticket goes through every label, issue and PR state across the passes (a rotation), each run's log is
        # one of three byte-identical texts whatever its state (with and without the connectors warning), and each
        # branch has a slug the ticket number does not give. So no list of ticket numbers, no reading of the log and
        # no branch built from the number can tell early from ok: only `issue view` (labels and state) and
        # `pr list --head <branch> --state all`.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        states = [   # (labels, open PRs, PRs of any state, issue state, outcome)
            (IN_PROGRESS, [], [], "OPEN", "early"),
            (IN_PROGRESS, [{"number": 60, "state": "OPEN", "isDraft": True}],     # only a draft: early (#42)
             [{"number": 60, "state": "OPEN", "isDraft": True}], "OPEN", "early"),
            (IN_PROGRESS, [{"number": 61, "state": "OPEN", "isDraft": False}],
             [{"number": 61, "state": "OPEN", "isDraft": False}], "OPEN", "ok"),
            (IN_PROGRESS, [], [{"number": 62, "state": "MERGED", "isDraft": False}], "OPEN", "ok"),
            (IN_PROGRESS, [], [{"number": 63, "state": "CLOSED", "isDraft": False}], "OPEN", "ok"),  # closed unmerged
            (IN_PROGRESS, [], [], "CLOSED", "ok"),                        # the issue is closed, label or not
            (in_review, [], [], "OPEN", "ok"),
            (("task", "status:needs-input", "size:S", "risk:low"), [], [], "OPEN", "ok"),
            (("task", "status:ready", "size:S", "risk:low"), [], [], "OPEN", "ok"),
        ]
        logs = [(EARLY_RESULT, True), (EARLY_RESULT, False),
                ({"type": "result", "is_error": False, "result": "PR #7 ready"}, False)]
        tickets = [7, 12, 13, 23, 58, 104, 311, 9, 77]
        branches = {n: f"t/{n}-sweep-{chr(97 + i)}{n * 7}" for i, n in enumerate(tickets)}
        for k in range(len(states)):
            self.fresh()
            plan = {n: states[(j + k) % len(states)] for j, n in enumerate(tickets)}
            for j, n in enumerate(tickets):
                result, warning = logs[(j + 2 * k) % len(logs)]
                self.ended_run(n, result, warning=warning, branch=branches[n],
                               worktree=os.path.join(self.repo, ".claude", "worktrees", branches[n].replace("/", "-")))
            self.write_scenario({n: s[:3] for n, s in plan.items()}, branches=branches,
                                states={n: s[3] for n, s in plan.items() if s[3] != "OPEN"})

            rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
            expected = {n: s[4] for n, s in plan.items()}
            with self.subTest(rotation=k):
                self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
                self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, expected)
                for n, outcome in expected.items():
                    early = "ended early (draft PR #60)" if plan[n][1] else "ended early (no PR)"
                    self.assertEqual(rows[n]["state"], early if outcome == "early" else "ended ok")
                # Early needs both facts: the ticket's labels and every PR of its branch, merged and closed included.
                calls = self.gh_calls()
                for n in (n for n, o in expected.items() if o == "early"):
                    self.assertTrue(any(a[:3] == ["issue", "view", str(n)] for a in calls), (n, calls))
                    self.assertTrue(any(a[:2] == ["pr", "list"] and "--head" in a
                                        and a[a.index("--head") + 1] == branches[n]
                                        and "--state" in a and a[a.index("--state") + 1] == "all"
                                        for a in calls), (n, calls))

    def test_a_run_without_a_repo_stays_ok_and_asks_gh_nothing(self):
        # A record with no repo (the key absent, empty or null) cannot say which repository's issue it ran: the sweep
        # must not read issue N of whatever repository it runs in (here proj, where #7..#9 are in progress with no
        # PR, so a guess would label them early). The control #10 has its repo and ends early as before. The ids
        # carry a key no repository here has, so nothing can tie the records to proj.
        for n, repo in ((7, "absent"), (8, ""), (9, None)):
            record = self.ended_run(n, EARLY_RESULT, id=f"0123456789ab-{n}-20261013120000")
            if repo == "absent":
                record.pop("repo")
            else:
                record["repo"] = repo
            write(os.path.join(self.runs_dir(), record["id"] + ".json"), json.dumps(record))
        self.ended_run(10, EARLY_RESULT)
        self.write_scenario({n: (IN_PROGRESS, [], []) for n in (7, 8, 9, 10)})

        p = self.work("runs", "--sweep", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {7: "ok", 8: "ok", 9: "ok", 10: "early"})
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {7: "ok", 8: "ok", 9: "ok", 10: "early"})
        for n in (7, 8, 9):
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertEqual(rows[n]["state"], "ended ok")
        calls = self.gh_calls()
        for n in (7, 8, 9):
            with self.subTest(ticket=n):
                self.assertFalse([a for a in calls if a[:3] == ["issue", "view", str(n)]], calls)
                self.assertFalse([a for a in calls if a[:2] == ["pr", "list"] and f"t/{n}-x" in a], calls)
        self.assertTrue([a for a in calls if a[:3] == ["issue", "view", "10"]], calls)

    def test_dispatch_asks_gh_about_a_freshly_ended_run_once(self):
        # Three headless runs ended ok since the last sweep: #7 in progress with no PR (early, dispatched again), #8 in
        # progress with an open ready PR, #9 in review. One dispatch closes them (the sweep) and picks what to run again
        # (the redispatch); both need `issue view` and `pr list --head` for the same ticket and branch, and the
        # answer is the same, so each is asked at most once per ticket in one invocation.
        pr_open = [{"number": 40, "state": "OPEN", "isDraft": False}]
        for n in (7, 8, 9):
            self.ended_run(n, EARLY_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, pr_open, pr_open),
                             9: (("task", "status:in-review", "size:S", "risk:low"), [], [])})

        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #7 Add the board tool (again after early)", p.stdout)
        self.assertNotIn("would start #8", p.stdout)
        self.assertNotIn("would start #9", p.stdout)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {7: "early", 8: "ok", 9: "ok"})
        calls = self.gh_calls()
        for n in (7, 8, 9):
            with self.subTest(ticket=n):
                views = [a for a in calls if a[:3] == ["issue", "view", str(n)]]
                heads = [a for a in calls if a[:2] == ["pr", "list"] and "--head" in a
                         and a[a.index("--head") + 1] == f"t/{n}-x"]
                self.assertLessEqual(len(views), 1, calls)
                self.assertLessEqual(len(heads), 1, calls)
                if n == 7:                                  # early needs both facts: asked exactly once each
                    self.assertEqual((len(views), len(heads)), (1, 1), calls)

    # ---- #42: a draft pull request is not a ready one
    def test_a_draft_pull_request_does_not_make_an_early_end_ok(self):
        # The run of #31 on 2026-10-13 ended `success` mid-review with its fix uncommitted and PR #41 still a draft.
        # Several tickets, each with its own draft PR number (and two different logs), so neither the PR number
        # nor the ticket can be a constant; #8 has no PR at all (the old early), #12 a ready PR (ok).
        cases = {   # n: (log, open PRs = PRs of any state, outcome, state)
            7: (ROUND2_RESULT, draft(41), "early", "ended early (draft PR #41)"),
            13: (EARLY_RESULT, draft(58), "early", "ended early (draft PR #58)"),
            31: (ROUND2_RESULT, draft(9), "early", "ended early (draft PR #9)"),
            8: (ROUND2_RESULT, [], "early", "ended early (no PR)"),
            12: (ROUND2_RESULT, ready_pr(77), "ok", "ended ok"),
        }
        for n, (result, *_rest) in cases.items():
            self.ended_run(n, result)
        self.write_scenario({n: (IN_PROGRESS, prs, prs) for n, (_r, prs, _o, _s) in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({n: rows[n]["state"] for n in rows}, {n: c[3] for n, c in cases.items()})
        for n in cases:
            self.assertTrue(rows[n]["ended_at"], rows[n])
            self.assertIsNone(rows[n]["exit"])
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {n: c[2] for n, c in cases.items()})

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")},
                         {n: c[3] for n, c in cases.items()})
        lines = self.run_lines()
        for n, (_r, prs, outcome, state) in cases.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                for other in (c[1][0]["number"] for m, c in cases.items() if m != n and c[1]):
                    self.assertNotIn(f"PR #{other})", lines[n])
        self.assertNotIn("draft", lines[8] + lines[12])
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

    def test_a_ready_pull_request_or_a_waiting_ticket_is_a_normal_end(self):
        # The same log for every run; only the board and the branch's PRs differ. A ready PR, a ticket the run moved
        # to in-review or needs-input (draft or not), a merged PR and a closed issue are normal ends: `ok`, never
        # dispatched again. #26 (in progress, draft only) is the control that does end early.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        needs_input = ("task", "status:needs-input", "size:S", "risk:low")
        merged = [{"number": 74, "state": "MERGED", "isDraft": False}]
        cases = {   # n: (labels, open PRs, PRs of any state, outcome)
            20: (IN_PROGRESS, ready_pr(70), ready_pr(70), "ok"),
            21: (in_review, draft(71), draft(71), "ok"),
            22: (needs_input, draft(72), draft(72), "ok"),
            23: (in_review, ready_pr(73), ready_pr(73), "ok"),
            24: (IN_PROGRESS, [], merged, "ok"),
            25: (IN_PROGRESS, draft(75), draft(75), "ok"),             # the issue is CLOSED (below)
            27: (needs_input, [], [], "ok"),
            26: (IN_PROGRESS, draft(76), draft(76), "early"),
        }
        for n in cases:
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({n: c[:3] for n, c in cases.items()}, states={25: "CLOSED"})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        expected = {n: c[3] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, expected)
        lines = self.run_lines()
        for n, outcome in expected.items():
            with self.subTest(ticket=n):
                if outcome == "ok":
                    self.assertEqual(rows[n]["state"], "ended ok")
                    self.assertNotIn("early", lines[n])
        self.assertEqual(rows[26]["state"], "ended early (draft PR #76)")

        p = self.ok("dispatch", "--dry-run")
        started = [line for line in p.stdout.splitlines() if line.startswith("would start #")]
        self.assertEqual(len(started), 1, p.stdout)
        self.assertTrue(started[0].startswith("would start #26 "), p.stdout)
        self.assertIn("#76", started[0])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [26])
        self.assertEqual(data["skipped"], [])

    def test_dispatch_asks_gh_about_a_freshly_ended_draft_run_once(self):
        # The draft case of the gh-once guarantee: #10 and #11 in progress with only a draft PR each (#43, #44),
        # #7 with no PR, #8 with a ready PR. One dispatch sweeps all four (early, early, early, ok) and redispatches
        # the three early ones; `issue view` and `pr list --head` are asked exactly once for each early ticket, the
        # draft's PR number included in that one answer.
        for n in (7, 8, 10, 11):
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({7: (IN_PROGRESS, [], []), 8: (IN_PROGRESS, ready_pr(40), ready_pr(40)),
                             10: (IN_PROGRESS, draft(43), draft(43)), 11: (IN_PROGRESS, draft(44), draft(44))})

        p = self.ok("dispatch", "--dry-run")
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {7: "early", 8: "ok", 10: "early", 11: "early"})
        starts = {n: [ln for ln in p.stdout.splitlines() if ln.startswith(f"would start #{n} ")] for n in (7, 8, 10, 11)}
        self.assertEqual({n: len(found) for n, found in starts.items()}, {7: 1, 8: 0, 10: 1, 11: 1}, p.stdout)
        self.assertIn("#43", starts[10][0])
        self.assertNotIn("#44", starts[10][0])
        self.assertIn("#44", starts[11][0])
        self.assertNotIn("#43", starts[11][0])
        calls = self.gh_calls()
        for n in (7, 8, 10, 11):
            with self.subTest(ticket=n):
                views = [a for a in calls if a[:3] == ["issue", "view", str(n)]]
                heads = [a for a in calls if a[:2] == ["pr", "list"] and "--head" in a
                         and a[a.index("--head") + 1] == f"t/{n}-x"]
                self.assertLessEqual(len(views), 1, calls)
                self.assertLessEqual(len(heads), 1, calls)
                if n != 8:                                  # early needs both facts: asked exactly once each
                    self.assertEqual((len(views), len(heads)), (1, 1), calls)

    def test_every_pull_request_of_the_branch_counts_and_the_open_draft_is_named(self):
        # A branch can have several PRs (gh lists them newest first). Early means none of them, in any state, is
        # ready: an old closed or merged non-draft PR beside an open draft is a normal end, wherever it sits in the
        # list (a row without isDraft is not a draft). Only drafts, closed ones included, is early, and the state
        # names the open draft, the newest. gh is asked with --state all; the rows are what it would return.
        def pr(number, state, is_draft=None):
            row = {"number": number, "state": state}
            if is_draft is not None:
                row["isDraft"] = is_draft
            return row
        cases = {   # n: (PRs of any state, newest first; outcome, state)
            30: ([pr(81, "OPEN", True), pr(80, "CLOSED", False)], "ok", "ended ok"),
            31: ([pr(83, "OPEN", True), pr(82, "MERGED", False)], "ok", "ended ok"),
            32: ([pr(85, "OPEN", True), pr(84, "CLOSED", True)], "early", "ended early (draft PR #85)"),
            33: ([pr(87, "CLOSED", False), pr(86, "OPEN", True)], "ok", "ended ok"),
            34: ([pr(89, "OPEN", True), pr(88, "MERGED")], "ok", "ended ok"),
            35: ([pr(93, "OPEN", True), pr(92, "CLOSED", True), pr(91, "CLOSED", True)], "early",
                 "ended early (draft PR #93)"),
        }
        for n in cases:
            self.ended_run(n, ROUND2_RESULT)
        self.write_scenario({n: (IN_PROGRESS, [r for r in prs if r["state"] == "OPEN"], prs)
                             for n, (prs, _o, _s) in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json")}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: c[1] for n, c in cases.items()})
        self.assertEqual({n: rows[n]["state"] for n in rows}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()}, {n: c[1] for n, c in cases.items()})

        p = self.ok("dispatch", "--dry-run")
        started = {int(m.group(1)): line for line in p.stdout.splitlines()
                   for m in [re.match(r"would start #(\d+) ", line)] if m}
        self.assertEqual(sorted(started), [32, 35], p.stdout)
        self.assertIn("#85", started[32])
        self.assertNotIn("#84", started[32])
        self.assertIn("#93", started[35])
        self.assertNotIn("#92", started[35])
        self.assertNotIn("#91", started[35])

    # ---- #57: bg is the default venue, so a bg run that ends `done` is judged like a headless `ok`
    def bg_runs(self, cases):
        """cases: {n: (bg id, `claude agents` state)}. Writes an unended bg run record for each ticket (repo proj,
        branch t/<n>-x, no pid, no log) and returns the FAKECLAUDE_AGENTS listing that reports each state. The
        listing gives no exitCode, as `claude agents --json` documents none."""
        for n, (bg_id, _state) in cases.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        return json.dumps([{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo,
                            "name": f"proj #{n} {TITLE}"} for n, (bg_id, state) in cases.items()])

    def test_a_done_bg_run_on_an_in_progress_ticket_without_a_ready_pr_ended_early(self):
        # The same facts that make a headless `ok` early make a bg `done` early: the issue OPEN, status:in-progress,
        # and no ready PR of its branch in any state (none, or only drafts). The early run keeps the newest open
        # draft as draft_pr. Anything else that ends `done` stays `ok`; `failed` and `stopped` are never upgraded,
        # whatever the board says. gh failing for a done run leaves it `ok` (the facts are missing), as headless.
        in_review = ("task", "status:in-review", "size:S", "risk:low")
        needs_input = ("task", "status:needs-input", "size:S", "risk:low")
        merged = [{"number": 95, "state": "MERGED", "isDraft": False}]
        cases = {   # n: (bg id, agents state, labels, open PRs, PRs of any state, outcome, state shown)
            40: ("s-9f1", "done", IN_PROGRESS, [], [], "early", "ended early (no PR)"),
            41: ("s-0a7", "done", IN_PROGRESS, draft(91), draft(91), "early", "ended early (draft PR #91)"),
            42: ("s-c3e", "done", IN_PROGRESS, draft(57), draft(57), "early", "ended early (draft PR #57)"),
            43: ("s-b12", "done", IN_PROGRESS, ready_pr(92), ready_pr(92), "ok", "ended ok"),
            44: ("s-77d", "done", in_review, [], [], "ok", "ended ok"),
            45: ("s-e05", "done", in_review, draft(93), draft(93), "ok", "ended ok"),
            46: ("s-5aa", "done", IN_PROGRESS, [], [], "ok", "ended ok"),          # the issue is CLOSED (below)
            47: ("s-2d4", "done", IN_PROGRESS, [], merged, "ok", "ended ok"),
            51: ("s-6c0", "done", needs_input, [], [], "ok", "ended ok"),
            52: ("s-31f", "done", IN_PROGRESS, [], [], "ok", "ended ok"),          # gh issue view fails (below)
            48: ("s-d8b", "failed", IN_PROGRESS, [], [], "error", "ended error"),
            49: ("s-4e9", "failed", IN_PROGRESS, draft(94), draft(94), "error", "ended error"),
            50: ("s-a60", "stopped", IN_PROGRESS, [], [], "stopped", "ended stopped"),
        }
        agents = self.bg_runs({n: c[:2] for n, c in cases.items()})
        self.write_scenario({n: c[2:5] for n, c in cases.items()}, states={46: "CLOSED"}, failing={52: "issue"})

        p = self.work("runs", "--sweep", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {r["ticket"]: r for r in json.loads(p.stdout)}
        expected = {n: c[5] for n, c in cases.items()}
        shown = {n: c[6] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({n: rows[n]["state"] for n in rows}, shown)
        for n in cases:
            self.assertTrue(rows[n]["ended_at"], rows[n])

        saved = {r["ticket"]: r for r in self.run_records()}
        self.assertEqual({n: r["outcome"] for n, r in saved.items()}, expected)
        self.assertEqual({n: saved[n].get("draft_pr") for n in (40, 41, 42)}, {40: None, 41: 91, 42: 57})
        for n in (43, 44, 45, 46, 47, 48, 49, 50, 51, 52):         # only an early run names a draft
            self.assertIsNone(saved[n].get("draft_pr"), saved[n])

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")}, shown)
        lines = self.run_lines()
        for n, state in shown.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                self.assertEqual("ended early" in lines[n], expected[n] == "early", lines[n])
        self.assertIn("api: available", self.ok("budget", "status").stdout)   # not a budget failure

    def test_only_the_board_and_the_pr_decide_a_bg_early_never_the_ticket_or_the_session_id(self):
        # Each ticket keeps its bg id across the passes while its agents state, labels and PRs rotate, so neither the
        # ticket number nor the session id can tell early from ok; a done run's outcome follows the board and the
        # branch's PRs, a failed run stays error.
        states = [   # (agents state, labels, open PRs, PRs of any state, outcome, state shown)
            ("done", IN_PROGRESS, [], [], "early", "ended early (no PR)"),
            ("done", IN_PROGRESS, draft(66), draft(66), "early", "ended early (draft PR #66)"),
            ("done", IN_PROGRESS, ready_pr(67), ready_pr(67), "ok", "ended ok"),
            ("failed", IN_PROGRESS, [], [], "error", "ended error"),
            ("done", ("task", "status:in-review", "size:S", "risk:low"), draft(68), draft(68), "ok", "ended ok"),
        ]
        tickets = {9: "s-x1", 23: "s-k4", 58: "s-q7", 104: "s-m2", 311: "s-z9"}
        for k in range(len(states)):
            self.fresh()
            plan = {n: states[(j + k) % len(states)] for j, n in enumerate(tickets)}
            agents = self.bg_runs({n: (bg_id, plan[n][0]) for n, bg_id in tickets.items()})
            self.write_scenario({n: s[1:4] for n, s in plan.items()})
            rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                         env=self.env(FAKECLAUDE_AGENTS=agents))}
            with self.subTest(rotation=k):
                self.assertEqual({n: rows[n]["outcome"] for n in rows}, {n: s[4] for n, s in plan.items()})
                self.assertEqual({n: rows[n]["state"] for n in rows}, {n: s[5] for n, s in plan.items()})
                self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                                 {n: s[4] for n, s in plan.items()})

    def test_dispatch_runs_a_bg_early_ticket_again_after_early(self):
        # One dispatch sweeps four bg runs and plans the redispatch: #40 (done, no PR) and #41 (done, only draft #91)
        # go again after early, the draft named; #43 (done, ready PR) is not run again; #48 (failed) goes again after
        # error, not early.
        cases = {40: ("s-9f1", "done", []), 41: ("s-0a7", "done", draft(91)),
                 43: ("s-b12", "done", ready_pr(92)), 48: ("s-d8b", "failed", [])}
        agents = self.bg_runs({n: c[:2] for n, c in cases.items()})
        self.write_scenario({n: (IN_PROGRESS, c[2], c[2]) for n, c in cases.items()})

        p = self.ok("dispatch", "--dry-run", env=self.env(FAKECLAUDE_AGENTS=agents))
        starts = {int(m.group(1)): line for line in p.stdout.splitlines()
                  for m in [re.match(r"would start #(\d+) ", line)] if m}
        self.assertEqual(sorted(starts), [40, 41, 48], p.stdout)
        self.assertIn("would start #40 Add the board tool (again after early)", starts[40])
        self.assertIn("(again after early", starts[41])
        self.assertIn("#91", starts[41])
        self.assertIn("(again after error)", starts[48])
        self.assertNotIn("early", starts[48])
        self.assertEqual({r["ticket"]: r["outcome"] for r in self.run_records()},
                         {40: "early", 41: "early", 43: "ok", 48: "error"})
        self.assertEqual(next(r for r in self.run_records() if r["ticket"] == 41).get("draft_pr"), 91)

        data = self.ok_json("dispatch", "--dry-run", "--json", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertEqual({row["number"]: row.get("again") for row in data["plan"]},
                         {40: "early", 41: "early", 48: "error"})

    def test_an_early_bg_run_shows_early_even_when_the_listing_gives_an_exit_code(self):
        # Should `claude agents` give an exitCode, the record keeps it, but an early end is what `runs` shows: the 0
        # of a done session says less than that its ticket is still in progress without a ready PR. Any other ended
        # run with an exit code still shows `exited N` (a done one with a ready PR, a failed one with or without
        # a draft), each with its own code.
        cases = {   # n: (bg id, agents state, exitCode, open PRs = PRs of any state, outcome, state shown)
            60: ("s-e01", "done", 0, [], "early", "ended early (no PR)"),
            61: ("s-e02", "done", 0, draft(88), "early", "ended early (draft PR #88)"),
            62: ("s-e03", "done", 0, draft(47), "early", "ended early (draft PR #47)"),
            63: ("s-e04", "done", 0, ready_pr(89), "ok", "exited 0"),
            64: ("s-e05", "failed", 3, [], "error", "exited 3"),
            65: ("s-e06", "failed", 1, draft(90), "error", "exited 1"),
        }
        self.bg_runs({n: c[:2] for n, c in cases.items()})
        agents = json.dumps([{"id": c[0], "kind": "background", "state": c[1], "exitCode": c[2], "cwd": self.repo,
                              "name": f"proj #{n} {TITLE}"} for n, c in cases.items()])
        self.write_scenario({n: (IN_PROGRESS, c[3], c[3]) for n, c in cases.items()})

        rows = {r["ticket"]: r for r in self.ok_json("runs", "--sweep", "--json",
                                                     env=self.env(FAKECLAUDE_AGENTS=agents))}
        expected = {n: c[4] for n, c in cases.items()}
        shown = {n: c[5] for n, c in cases.items()}
        self.assertEqual({n: rows[n]["outcome"] for n in rows}, expected)
        self.assertEqual({n: rows[n]["state"] for n in rows}, shown)
        saved = {r["ticket"]: r for r in self.run_records()}
        self.assertEqual({n: saved[n]["exit"] for n in saved}, {n: c[2] for n, c in cases.items()})
        self.assertEqual({n: saved[n].get("draft_pr") for n in (60, 61, 62)}, {60: None, 61: 88, 62: 47})

        # Without a sweep, from the saved records alone: the same states, in --json and in the table.
        self.assertEqual({r["ticket"]: r["state"] for r in self.ok_json("runs", "--json")}, shown)
        lines = self.run_lines()
        for n, state in shown.items():
            with self.subTest(ticket=n):
                self.assertIn(state, lines[n])
                self.assertEqual("exited" in lines[n], expected[n] != "early", lines[n])


if __name__ == "__main__":
    unittest.main()
