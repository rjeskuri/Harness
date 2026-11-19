"""Tests for harness-worklog's per-ticket hours and the month's invoice export (ticket #59).

    python3 -m unittest discover -s plugins/harness/tests -p "test_worklog_tickets.py" -v

`harness-worklog update` attributes each block's time to the ticket the session ran for: a session in a ticket
worktree (<repo>/.claude/worktrees/t-N-<slug>), on a ticket branch (gitBranch `t/N-...`), or one that ran
`/harness:work N`. Time in the main checkout with no ticket, or in a worktree that is not a ticket's, is
unattributed. A block that spans several tickets is split by message time. Each ticket worked on carries its
hours by day in one comment marked `<!-- harness-hours -->`, edited in place, one table row per day
(`| yyyy-mm-dd | H.HH |`), so a re-run never adds a comment nor counts a day twice. The repository the
tickets live in is the project's own origin.

`harness-worklog invoice --month yyyy-mm --out-dir DIR` writes DIR/invoice-yyyy-mm.csv and .md: one CSV row per
ticket and day (columns date, epic, ticket, title, hours, resolution), the unattributed time on rows of its own
(ticket not a number), and one row with a cell `Total`; the rows add up to the month's hours in
worklog/worklog.csv.

GitHub is worklog_support's stateful fake gh, so the tests read what each issue carries after a run. Expected
hours are worked out by hand from the fixture times (minutes to the nearest quarter hour).
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys
import unittest

sys.dont_write_bytecode = True

from worklog_support import HOURS_MARKER, LOGIN, BOARD_MARKER, WorklogCase  # noqa: E402

SLUG = "acme/billing-svc"
ORIGIN = "git@github.com:acme/billing-svc.git"

# A typed slash command as Claude Code stores it (message first).
WORK_44 = ("<command-message>harness:work</command-message>\n<command-name>/harness:work</command-name>\n"
           "<command-args>44</command-args>")


class TicketHours(WorklogCase):

    def setUp(self):
        super().setUp()
        self.repo = self.project("billing-svc", ORIGIN)
        self.init(self.repo, self.today - dt.timedelta(days=25))

    def add_tickets(self, *numbers):
        self.add_issue(SLUG, 2, "Unrelated ticket")                      # never worked on: never gets hours
        for n in numbers:
            self.add_issue(SLUG, n, f"Ticket {n}")

    def test_blocks_go_to_the_ticket_of_their_worktree_branch_or_work_command_and_main_time_to_none(self):
        self.add_tickets(7, 12, 31, 44)
        d1, d2 = self.today - dt.timedelta(days=3), self.today - dt.timedelta(days=2)
        main = self.folder(self.repo)
        wt7 = self.worktree(self.repo, "t-7-export")
        # a ticket worktree, with the cwd and branch Claude Code records
        self.worked(self.folder(wt7), "s-7a", d1, "09:00", "09:45", cwd=wt7, branch="t/7-export")       # 45 min
        # a ticket worktree known only by its transcript folder (removed since; no branch recorded)
        self.worked(self.folder(self.worktree(self.repo, "t-12-dupes")), "s-12", d1, "10:30", "11:00")  # 30 min
        # the main checkout on a ticket branch (a quick fix on t/31-...)
        self.worked(main, "s-31", d1, "12:00", "13:00", cwd=self.repo, branch="t/31-totals")           # 60 min
        # the main checkout on main, running /harness:work 44
        self.worked(main, "s-44", d1, "14:00", "14:15", cwd=self.repo, branch="main", text=WORK_44)    # 15 min
        # unattributed: the main checkout on main, and a worktree that is not a ticket's
        self.worked(main, "s-main", d1, "15:00", "16:15", cwd=self.repo, branch="main")                # 75 min
        scratch = self.worktree(self.repo, "scratch")
        self.worked(self.folder(scratch), "s-scratch", d1, "17:00", "17:30", cwd=scratch, branch="scratch")  # 30
        # ticket 7 again the next day: a second row on the same comment
        self.worked(self.folder(wt7), "s-7b", d2, "09:00", "09:30", cwd=wt7, branch="t/7-export")       # 30 min

        self.run_wl(self.repo, "update")

        self.assertEqual(self.ticket_hours(SLUG), {
            7: {d1.isoformat(): "0.75", d2.isoformat(): "0.50"},
            12: {d1.isoformat(): "0.50"},
            31: {d1.isoformat(): "1.00"},
            44: {d1.isoformat(): "0.25"},
        })
        # the day's billed hours are unchanged: 45 + 30 + 60 + 15 + 75 + 30 minutes = 4.25 h, the unattributed
        # 105 minutes included
        rows = {r["date"]: r for r in self.log_rows(self.repo) if r["source"] == "auto"}
        self.assertEqual((rows[d1.isoformat()]["hours"], rows[d2.isoformat()]["hours"]), ("4.25", "0.50"))

    def test_a_block_across_two_tickets_is_split_by_message_time(self):
        self.add_tickets(7, 12)
        day = self.today - dt.timedelta(days=2)
        wt7, wt12 = self.worktree(self.repo, "t-7-export"), self.worktree(self.repo, "t-12-dupes")
        at = lambda hhmm: self.at(day, hhmm)                                                      # noqa: E731
        # ticket 7's session from 10:00, ticket 12's from 10:46 to 11:15, then back in ticket 7's session until
        # 11:45: one block 10:00-11:45 (no pause over 30 minutes across the two sessions), 105 minutes
        self.transcript(self.folder(wt7), "s-7", [
            ("user", at("10:00"), "export the orders"), ("assistant", at("10:10"), None),
            ("assistant", at("10:20"), None), ("assistant", at("10:30"), None), ("assistant", at("10:45"), None),
            ("user", at("11:16"), "back to the export"), ("assistant", at("11:25"), None),
            ("assistant", at("11:35"), None), ("assistant", at("11:45"), None),
        ], cwd=wt7, branch="t/7-export")
        self.transcript(self.folder(wt12), "s-12", [
            ("user", at("10:46"), "flag the duplicates"), ("assistant", at("10:55"), None),
            ("assistant", at("11:05"), None), ("assistant", at("11:15"), None),
        ], cwd=wt12, branch="t/12-dupes")

        self.run_wl(self.repo, "update")

        row = next(r for r in self.log_rows(self.repo) if r["date"] == day.isoformat())
        self.assertEqual((row["blocks"], row["hours"]), ("10:00-11:45", "1.75"))
        # ticket 7: 10:00-10:45 and 11:16-11:45 (75 min, give or take the minute at each switch) -> 1.25;
        # ticket 12: 10:46-11:15 (30 min) -> 0.50. The whole block to one ticket gives 1.75 to it.
        self.assertEqual(self.ticket_hours(SLUG), {7: {day.isoformat(): "1.25"}, 12: {day.isoformat(): "0.50"}})

    def test_a_second_update_keeps_one_comment_per_ticket_and_changes_nothing(self):
        human = "Can we also export the refunds?"
        self.add_issue(SLUG, 7, "Export the orders", comments=[(LOGIN, human)])
        old, recent = self.today - dt.timedelta(days=20), self.today - dt.timedelta(days=3)
        wt7 = self.worktree(self.repo, "t-7-export")
        self.worked(self.folder(wt7), "s-old", old, "10:00", "10:30", cwd=wt7, branch="t/7-export")       # 0.50
        self.worked(self.folder(wt7), "s-recent", recent, "14:00", "14:45", cwd=wt7, branch="t/7-export")  # 0.75
        since = (self.today - dt.timedelta(days=21)).isoformat()
        expected = {old.isoformat(): "0.50", recent.isoformat(): "0.75"}

        self.run_wl(self.repo, "update", "--since", since)
        self.assertEqual(self.ticket_hours(SLUG), {7: expected})
        first = self.hours_comments(SLUG, 7)[0]

        # the same window again: still one hours comment, its body as it was
        self.run_wl(self.repo, "update", "--since", since)
        self.assertEqual([(c["id"], c["body"]) for c in self.hours_comments(SLUG, 7)], [(first["id"], first["body"])])

        # the board comments on the ticket after it, under the same account; the default window (the last 14 days)
        # no longer reaches the old day, which the comment keeps
        board = "**Status** in review\n\n" + BOARD_MARKER
        self.add_comment(SLUG, 7, board)
        self.run_wl(self.repo, "update")
        self.assertEqual([(c["id"], c["body"]) for c in self.hours_comments(SLUG, 7)], [(first["id"], first["body"])])

        # more work on the recent day: the same comment now says 1.25 h for it (45 + 30 minutes), not a second
        # row or comment, and the old day stays
        self.worked(self.folder(wt7), "s-more", recent, "16:00", "16:30", cwd=wt7, branch="t/7-export")
        self.run_wl(self.repo, "update")
        self.assertEqual(self.ticket_hours(SLUG), {7: {old.isoformat(): "0.50", recent.isoformat(): "1.25"}})
        bodies = [c["body"] for c in self.comments(SLUG, 7)]
        self.assertEqual(len(bodies), 3, bodies)                                   # the human's, the hours, the board's
        self.assertEqual((bodies[0], bodies[2]), (human, board))
        self.assertEqual(self.hours_comments(SLUG, 7)[0]["id"], first["id"])
        self.assertEqual([w["op"] for w in self.load()["writes"] if w["op"] == "create"], ["create"])

    def test_the_tickets_live_in_each_projects_own_repository(self):
        other_slug = "northwind/ops-portal"
        other = self.project("ops-portal", "https://github.com/northwind/ops-portal.git")
        self.init(other, self.today - dt.timedelta(days=25))
        self.add_issue(SLUG, 7, "Export the orders")
        self.add_issue(other_slug, 7, "Rotate the logs")                # the same number in the other repository
        day = self.today - dt.timedelta(days=2)
        for repo, minutes_end in ((self.repo, "10:45"), (other, "10:30")):
            wt = self.worktree(repo, "t-7-work")
            self.worked(self.folder(wt), "s-7", day, "10:00", minutes_end, cwd=wt, branch="t/7-work")

        self.run_wl(self.repo, "update")
        self.run_wl(other, "update")

        self.assertEqual(self.ticket_hours(SLUG), {7: {day.isoformat(): "0.75"}})
        self.assertEqual(self.ticket_hours(other_slug), {7: {day.isoformat(): "0.50"}})


class Invoice(WorklogCase):
    """September 2026 for one project: tickets 7 and 12 under epic 3, ticket 15 with no epic, unattributed time in
    the main checkout and a manual row, and October work that stays out."""

    MONTH = "2026-09"

    def setUp(self):
        super().setUp()
        self.repo = self.project("billing-svc", ORIGIN)
        self.init(self.repo, dt.date(2026, 8, 25))
        self.add_issue(SLUG, 3, "Glow sync")
        self.add_issue(SLUG, 7, "Export the orders", state="CLOSED", parent=3, comments=[
            (LOGIN, "**Resolution**\n\nOrders export nightly to the sheet.\n\n" + BOARD_MARKER)])
        self.add_issue(SLUG, 12, "Flag duplicates", parent=3)
        self.add_issue(SLUG, 15, "Fix the totals", state="CLOSED", comments=[
            (LOGIN, "**Resolution**\n\nTotals match the bank statement.\n\n" + BOARD_MARKER)])

    def test_a_months_ticket_rows_reconcile_with_the_worklog_in_csv_and_markdown(self):
        d1, d2, d3, oct2 = dt.date(2026, 9, 8), dt.date(2026, 9, 9), dt.date(2026, 9, 10), dt.date(2026, 10, 2)
        main = self.folder(self.repo)
        wts = {n: self.worktree(self.repo, name) for n, name in ((7, "t-7-export"), (12, "t-12-dupes"),
                                                                 (15, "t-15-totals"))}

        def on(n, sid, day, start, end):
            self.worked(self.folder(wts[n]), sid, day, start, end, cwd=wts[n], branch=f"t/{n}-x")

        on(7, "a", d1, "10:00", "10:45")                                                  # 45 min
        self.worked(main, "b", d1, "16:00", "16:30", cwd=self.repo, branch="main")        # 30 min, unattributed
        on(12, "c", d2, "09:00", "09:30")                                                 # 30 min
        on(7, "d", d2, "14:00", "15:00")                                                  # 60 min
        # one 40-minute block: ticket 15 then the main checkout (the day is 0.75 h; 0.25 + 0.25 when rounded apart)
        self.transcript(self.folder(wts[15]), "e", [
            ("user", self.at(d3, "13:00"), "fix the totals"), ("assistant", self.at(d3, "13:10"), None),
            ("assistant", self.at(d3, "13:20"), None)], cwd=wts[15], branch="t/15-x")
        self.transcript(main, "f", [
            ("user", self.at(d3, "13:21"), "now the readme"), ("assistant", self.at(d3, "13:30"), None),
            ("assistant", self.at(d3, "13:40"), None)], cwd=self.repo, branch="main")
        on(7, "g", oct2, "09:00", "09:30")                                                # October: not this invoice
        self.run_wl(self.repo, "update", "--since", "2026-09-01")
        self.run_wl(self.repo, "add", "--date", "2026-09-11", "--hours", "1.5", "--description", "Call")

        # the month's billed hours, from the log and from first principles: 75 + 90 + 40 minutes and 1.5 manual
        billed = sum(float(r["hours"]) for r in self.log_rows(self.repo) if r["date"].startswith(self.MONTH + "-"))
        self.assertAlmostEqual(billed, 5.00)

        out = self.mk("out")
        self.run_wl(self.repo, "invoice", "--month", self.MONTH, "--out-dir", out)
        csv_path = os.path.join(out, f"invoice-{self.MONTH}.csv")
        md_path = os.path.join(out, f"invoice-{self.MONTH}.md")
        self.assertTrue(os.path.isfile(csv_path), os.listdir(out))
        self.assertTrue(os.path.isfile(md_path), os.listdir(out))

        with open(csv_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(rows)
        self.assertLessEqual({"date", "epic", "ticket", "title", "hours", "resolution"},
                             {k.strip().lower() for k in rows[0]})
        rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in rows]
        totals = [r for r in rows if any(v.lower() == "total" for v in r.values())]
        self.assertEqual(len(totals), 1, rows)
        self.assertAlmostEqual(float(totals[0]["hours"]), billed)
        data = [r for r in rows if r not in totals]
        self.assertAlmostEqual(sum(float(r["hours"]) for r in data), billed)
        self.assertFalse([r for r in data if r["date"].startswith("2026-10")], "October work in September's invoice")

        tickets = [r for r in data if r["ticket"].lstrip("#").isdigit()]
        unattributed = [r for r in data if r not in tickets]
        by_ticket_day = {}
        for r in tickets:
            key = (int(r["ticket"].lstrip("#")), r["date"])
            by_ticket_day[key] = by_ticket_day.get(key, 0.0) + float(r["hours"])
        self.assertEqual({k: v for k, v in by_ticket_day.items() if k[0] != 15},
                         {(7, "2026-09-08"): 0.75, (7, "2026-09-09"): 1.00, (12, "2026-09-09"): 0.50})
        self.assertEqual([k for k in by_ticket_day if k[0] == 15], [(15, "2026-09-10")])
        # ticket 15 had 20 of the block's 40 minutes: some of the day, not all of it
        self.assertIn(by_ticket_day[(15, "2026-09-10")], (0.25, 0.50))
        unattributed_hours = sum(float(r["hours"]) for r in unattributed)
        self.assertAlmostEqual(unattributed_hours, billed - sum(by_ticket_day.values()))
        self.assertIn(round(unattributed_hours, 2), (2.25, 2.50))   # 30 min, ~20 min and the 1.50 manual hours

        for r in tickets:
            n = int(r["ticket"].lstrip("#"))
            with self.subTest(ticket=n, date=r["date"]):
                self.assertEqual(r["title"], {7: "Export the orders", 12: "Flag duplicates", 15: "Fix the totals"}[n])
                self.assertEqual(r["epic"].lstrip("#"), {7: "3", 12: "3", 15: ""}[n])
                want = {7: "Orders export nightly to the sheet.", 12: "", 15: "Totals match the bank statement."}[n]
                if want:
                    self.assertIn(want, r["resolution"])
                else:
                    self.assertEqual(r["resolution"], "")

        with open(md_path, encoding="utf-8") as fh:
            md = fh.read()
        for text in ("Glow sync", "Export the orders", "Flag duplicates", "Fix the totals",
                     "Orders export nightly to the sheet.", "5.00", f"{unattributed_hours:.2f}"):
            self.assertIn(text, md)
        self.assertNotIn(HOURS_MARKER, md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
