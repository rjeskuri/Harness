"""harness-pr ready and land honor the owner's dismissals (ticket #516, epic #512).

A `dismiss <path>:<line> <why>` line in the owner's own comment on a ticket the PR closes (ready: --ticket N, else
the PR's closingIssuesReferences) or on the PR itself selects the finding at that place in the newest harness review
posted before the comment (harness_lib.dismissals, #515); that finding no longer refuses `harness-pr ready P` or
`harness-pr land P`, wherever the latest harness review raises it again. Everything is read from GitHub: there is no
~/.claude/harness/repos/<key>/reviews/ folder in these tests. What is left refuses exactly as today (the findings
named, `K more high or medium not listed` counted from the review as posted, before any dismissal), a selector that
matches nothing exactly keeps its finding blocking with one warning naming it, and a failed read of the login, a
ticket's or the PR's comments keeps every finding blocking with a warning naming the failure, and one of the PR's
review comments (the gate's read too) refuses naming the failure. A latest review that lists no high or medium
finding reads no dismissal at all.

The fake gh answers the gate's reads as test_pr.py's ReadyGate and test_pr_land.py's LandGate do (pr_support's
gate_calls and land_calls), the owner's comments from its REST world (issue entries, the PR's with pull_request)
and the reviews and their inline comments in the REST form harness_lib reads them too (repos/{owner}/{repo}/...).
As on GitHub (#533), GET pulls/{n}/reviews/{id}/comments gives an inline comment no line (line and original_line
null, only its diff position) and GET pulls/{n}/comments lists every review's inline comments with their lines and
pull_request_review_id.
Each test's first harness-pr run is a process, its further rows run in process (RowsInProcess).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_gate_dismiss.py" -v
"""

from __future__ import annotations

import collections
import glob
import os
import re
import shutil
import unittest

import fakegh
from pr_support import *  # noqa: F401,F403

OWNER = "rjeskuri"            # the account `gh api user` names; harness_review's reviews are by it too
PAL = "pal"                   # another account
BOT = "github-actions[bot]"
FAILED = GATE_FAILURE_TEXT

# Findings (path, line, severity, body), as pr_support.harness_review posts them.
FROBS = ("widget.py", 3, "high", "Frobs twice on the empty input.")
STALE = ("docs/count.md", 7, "medium", "Stale count of the widgets.")
RETRY = ("lib/cap.py", 17, "medium", "Cap is never checked on retry.")
HANDLE = ("widget.py", 3, "medium", "Leaks the handle it opens.")        # a second finding at FROBS's place
SHIFTED = ("widget.py", 5, "high", FROBS[3])                              # FROBS raised again two lines down
LOW = ("docs/notes.md", 4, "low", "Mention the flag in the notes.")
MOVED = ("widget.py", 12, "high", "Frobs twice when the cache is cold.")  # inline, posted at 12 (see posted)
OLD = ("docs/old.md", None, "medium", "Still names the old flag.")      # listed with no line (posted)
GONE = ("lib/gone.py", None, "high", "The moved helper lost its lock.")
OLD_AT = ("docs/old.md", 4, "medium", "The example uses the old flag.")  # a lined finding at OLD's path
LOGIN = {"match": ["api", "user"], "stdout": OWNER + "\n"}


def note(login, hour, body, number, minute=0):
    """A comment on issue or PR `number` at `hour`:`minute` on pr_support.at's day, as the REST world serves it."""
    kind = "pull" if number == 34 else "issues"
    return {"author": {"login": login}, "body": body, "createdAt": f"2026-10-12T{hour:02d}:{minute:02d}:00Z",
            "url": f"https://github.com/o/r/{kind}/{number}#issuecomment-{number}{hour:02d}{minute:02d}"}


def dismiss(finding, why):
    return f"dismiss {finding[0]}:{finding[1]} {why}".rstrip()


def selector(finding):
    return f"{finding[0]}:{finding[1]}"


def owner_dismissal(finding, number=34, hour=11, why="out of scope, #500 owns it"):
    return note(OWNER, hour, dismiss(finding, why), number)


def posted(rid, when, inline=(), body_only=(), now=None, **kw):
    """pr_support.harness_review, then: a body finding whose line is None listed with no line (``- `path` [sev]
    body``, as harness-pr findings lists one carried across a rename), and each inline comment whose (path, line as
    posted) `now` maps sitting at that line on the head instead (a push moved it; its original_line stays)."""
    review, comments = harness_review(rid, when, inline=inline, body_only=body_only, **kw)
    review = dict(review, body=review["body"].replace(":None` [", "` ["))
    now = now or {}
    return review, [dict(c, line=now.get((c["path"], c["original_line"]), c["line"])) for c in comments]


def snapshot_calls(reviews, pr=34):
    """The PR's reviews and their inline comments as harness_lib reads them (repos/{owner}/{repo}/pulls/...), as
    GitHub answers (#533): each review's comments endpoint without their lines (pr_support.review_endpoint), the PR's
    comments list, every review's, with them; gate_calls answers harness-pr's own repos/o/r/... reads of the same."""
    calls = [fakegh.rest_entry(f"pulls/{pr}/reviews/{r['id']}/comments", "GET", exact=True,
                               stdout_json=review_endpoint(c)) for r, c in reviews]
    calls.append(fakegh.rest_entry(f"pulls/{pr}/comments", "GET", exact=True,
                                   stdout_json=[x for _, c in reviews for x in c]))
    calls.append(fakegh.rest_entry(f"pulls/{pr}/reviews", "GET", exact=True, stdout_json=[r for r, _ in reviews]))
    return calls


def comment_world(notes):
    """Issue entries holding the comments: {number: [comments]}, 34 being the PR."""
    return [fakegh.issue_entry(n, comments=list(cs), **({"pull_request": True} if n == 34 else {}))
            for n, cs in notes.items()]


def read_failure(route):
    """A GET of `route` (after repos/{o}/{r}/) failing, through the REST world."""
    return fakegh.rest_entry(route, "GET", exact=True, stderr=FAILED + "\n", exit=1)


class GateDismissals(LandBase):
    """Both gates over the same reviews and comments: ready from the main checkout with a light scenario, land with
    LandBase's land conversation (a fresh copy of the repositories before each land after the first)."""

    def setUp(self):
        super().setUp()
        self.lands = 0

    # ------------------------------------------------------------------ running the gates
    def ready(self, reviews, notes=None, closing=(12,), first=(), flags=(), commits=None):
        """`harness-pr ready 34`: the PR closes `closing`; `notes` {number: comments} (every closing ticket and the
        PR have none unless given); `first` entries win over the rest (a failing read)."""
        given, notes = notes or {}, {n: [] for n in list(closing) + [34]}
        notes.update(given)
        view = {"match": ["pr", "view", "34", "--json"],
                "stdout_json": {"url": PR_URL, "title": "Add the widget (#12)", "labels": [],
                                "closingIssuesReferences": [{"number": n, "url": f"https://github.com/o/r/issues/{n}"}
                                                            for n in closing]}}
        self.scenario(list(first) + comment_world(notes)
                      + [{"match": ["pr", "ready", "34"], "stdout": ""}, view, REPO_VIEW, LOGIN]
                      + gate_calls(reviews, [pr_commit("a", at(7))] if commits is None else commits)
                      + snapshot_calls(reviews))
        if os.path.exists(self.notify_log):
            os.unlink(self.notify_log)
        return self.pr("ready", "34", *flags, cwd=self.main)

    def land(self, reviews, notes=None, first=(), flags=()):
        """`harness-pr land 34` at the head the reviews are of, ticket #12's comments `notes[12]`, the PR's
        `notes[34]`."""
        if self.lands:
            for name in ("origin.git", "proj"):
                shutil.rmtree(os.path.join(self.tmp, name))
            self.make_repos()
        self.lands += 1
        notes = dict(notes or {})
        ticket = notes.pop(12, [])
        notes.setdefault(34, [])
        self.scenario(list(first) + comment_world(notes) + snapshot_calls(reviews)
                      + self.land_calls(reviews=reviews, commits=[pr_commit("a", at(7))], comments=ticket))
        return self.pr("land", "34", *flags, cwd=self.main)

    # ------------------------------------------------------------------ what came of it
    def warnings(self, p):
        return [line for line in p.stderr.splitlines() if line.startswith("harness-pr: warning:")]

    def refusal(self, p):
        """What the refusal lists: the text after `after it: ` on the line naming the latest harness review."""
        for line in p.stderr.splitlines():
            if "the latest harness review (" in line and "after it: " in line and "warning:" not in line:
                return line.split("after it: ", 1)[1]
        self.fail(f"no refusal naming the latest harness review in:\n{p.stdout}{p.stderr}")

    def assertBlocking(self, p, kept, gone=(), unlisted=0):
        """The refusal names each finding `kept` (one listed with no line by its path) and none `gone`, and counts
        the kept ones plus `unlisted`."""
        listed = self.refusal(p)
        for path, line, _sev, body in kept:
            self.assertIn(path if line is None else f"{path}:{line}", listed, p.stderr)
            self.assertIn(body, listed, p.stderr)
        for finding in gone:
            self.assertNotIn(finding[3], listed, p.stderr)
        n = len(kept) + unlisted
        self.assertIn(f"has {n} high or medium finding{'' if n == 1 else 's'} and", p.stderr)
        if unlisted:
            self.assertIn(f"{unlisted} more high or medium not listed (see the review)", listed)
        else:
            self.assertNotIn("not listed", listed)

    def assertReady(self, p, tickets=(12,)):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(["pr", "ready", "34"], self.gh_argv())
        self.assertEqual([a[:3] for a in self.stub_calls("harness-board")],
                         [["status", str(n), "in-review"] for n in tickets])
        self.assertEqual(len(self.notifications()), 1)

    def assertNotReady(self, p):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "ready"]], self.gh_argv())
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertEqual(self.notifications(), [])

    def assertLanded(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(["pr", "merge", "34", "-R", "o/r", "--squash", "--delete-branch"], self.gh_argv())
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))

    def assertNotLanded(self, p):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())

    def assertWarnsOf(self, p, *needles):
        """One warning line names all of `needles`."""
        hits = [w for w in self.warnings(p) if all(n in w for n in needles)]
        self.assertEqual(len(hits), 1, (needles, p.stderr))

    def dismissal_reads(self, before_merge=False):
        """The gh calls only a dismissal read makes: `gh api user`, a GET of an issue's or the PR's comments, and a
        second read of the PR's reviews, of one review's comments or of the PR's review comments; with
        `before_merge`, only those before gh pr merge (land reads its ticket's comments after it)."""
        argv = self.gh_argv()
        if before_merge:
            merges = [i for i, a in enumerate(argv) if a[:2] == ["pr", "merge"]]
            argv = argv[:merges[0]] if merges else argv
        out, reviews = [], collections.Counter()
        for a in argv:
            req = fakegh.parse_api(a)
            if a[:2] == ["api", "user"]:
                out.append(a)
            elif req and req["method"] == "GET" and re.fullmatch(r"issues/\d+/comments", req["route"]):
                out.append(a)
            elif req and req["method"] == "GET" and (req["route"].startswith("pulls/34/reviews")
                                                     or req["route"] == "pulls/34/comments"):
                reviews[req["route"]] += 1
        return out + [f"{route} read {n} times" for route, n in reviews.items() if n > 1]

    # ------------------------------------------------------------------ tests
    def test_ready_passes_once_every_listed_blocker_is_dismissed(self):
        """Every high and medium finding the latest harness review lists, dismissed by the owner on the PR or the
        ticket after it: ready marks the PR ready, sets the ticket in review and notifies; the same PR without the
        comments refuses. A low finding still gets ready's warning. A latest review that lists no high or medium
        finding makes no dismissal read."""
        rows = [  # (name, the latest review's (inline, body_only), the comments)
            ("the example: two comments on the PR", ([FROBS, LOW], [STALE]),
             {34: [owner_dismissal(FROBS), note(OWNER, 12, dismiss(STALE, "docs only, no reader"), 34)]}),
            ("one comment, two lines, on the ticket", ([FROBS], [STALE]),
             {12: [note(OWNER, 11, "Looked at both.\n" + dismiss(FROBS, "not reachable: the CLI validates first")
                        + "\n" + dismiss(STALE, "regenerated nightly"), 12)]}),
            ("three blockers, ticket and PR", ([RETRY, FROBS], [STALE]),
             {12: [owner_dismissal(RETRY, 12, 11, "retries are capped upstream")],
              34: [owner_dismissal(FROBS, 34, 11), owner_dismissal(STALE, 34, 13, "docs only")]}),
        ]
        for name, (inline, body_only), notes in rows:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only)]
            blockers = [f for f in inline + body_only if f[2] != "low"]
            with self.subTest(name, comments=False):
                p = self.ready(reviews)
                self.assertNotReady(p)
                self.assertBlocking(p, blockers)
            with self.subTest(name):
                p = self.ready(reviews, notes)
                self.assertReady(p)
                lows = [f for f in inline + body_only if f[2] == "low"]
                low_lines = [w for w in self.warnings(p) if "low finding" in w]
                self.assertEqual(len(low_lines), 1 if lows else 0, p.stderr)
                for path, line, _sev, body in lows:
                    self.assertIn(f"{path}:{line}", low_lines[0])
                    self.assertIn(body, low_lines[0])
        # no high or medium finding listed: nothing about dismissals is read, whether ready passes or refuses
        for name, review, passes in (
                ("only a low finding listed", harness_review(101, at(10), inline=[LOW]), True),
                ("the summary counts one high, none listed", harness_review(101, at(10), summary=(1, 1, 0, 0)), False)):
            with self.subTest(name):
                p = self.ready([review], {34: [owner_dismissal(FROBS)]})
                if passes:
                    self.assertReady(p)
                else:
                    self.assertNotReady(p)
                self.assertEqual(self.dismissal_reads(), [])

    def test_land_lands_once_every_listed_blocker_is_dismissed(self):
        """The same at the head: every listed high and medium finding dismissed, land merges and cleans up without
        --accept-findings; the comments are what lets it through. A latest review listing only a low finding reads
        no dismissal before the merge."""
        rows = [  # (name, the latest review's (inline, body_only), the comments)
            ("the example: two comments on the PR", ([FROBS, LOW], [STALE]),
             {34: [owner_dismissal(FROBS), owner_dismissal(STALE, 34, 12, "docs only, no reader")]}),
            ("three blockers, one comment", ([RETRY], [STALE, FROBS]),
             {34: [note(OWNER, 11, "\n".join([dismiss(RETRY, "capped upstream"), dismiss(STALE, "regenerated nightly"),
                                              dismiss(FROBS, "covered by #500")]), 34)]}),
        ]
        for name, (inline, body_only), notes in rows:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only)]
            with self.subTest(name, comments=False):
                p = self.land(reviews)
                self.assertNotLanded(p)
                self.assertBlocking(p, [f for f in inline + body_only if f[2] != "low"])
            with self.subTest(name):
                self.assertLanded(self.land(reviews, notes))
        with self.subTest("only a low finding listed"):
            p = self.land([harness_review(101, at(10), inline=[LOW])], {34: [owner_dismissal(FROBS)]})
            self.assertLanded(p)
            self.assertEqual(self.dismissal_reads(before_merge=True), [])

    def test_ready_and_land_refuse_naming_the_blockers_left(self):
        """Some blockers dismissed: both gates refuse, naming and counting only those left."""
        rows = [  # (name, inline, body_only, comments, left, dismissed)
            ("the example, one dismissed", [FROBS], [STALE], {34: [owner_dismissal(FROBS)]}, [STALE], [FROBS]),
            ("three listed, one dismissed on the ticket", [FROBS, RETRY], [STALE],
             {12: [owner_dismissal(STALE, 12, 11, "docs only")]}, [FROBS, RETRY], [STALE]),
            ("three listed, two dismissed", [FROBS, RETRY], [STALE],
             {12: [owner_dismissal(FROBS, 12)], 34: [owner_dismissal(STALE, 34, 12, "docs only")]}, [RETRY],
             [FROBS, STALE]),
        ]
        for name, inline, body_only, notes, left, dismissed in rows:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only)]
            with self.subTest(name, gate="ready"):
                p = self.ready(reviews, notes)
                self.assertNotReady(p)
                self.assertBlocking(p, left, dismissed)
                self.assertIn("harness-pr ready 34", p.stderr)
        for name, inline, body_only, notes, left, dismissed in rows[:2]:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only)]
            with self.subTest(name, gate="land"):
                p = self.land(reviews, notes)
                self.assertNotLanded(p)
                self.assertBlocking(p, left, dismissed)
                self.assertIn("--accept-findings", p.stderr)

    def test_ticket_and_pr_comments_both_dismiss(self):
        """A dismissal counts from any ticket the gate reads (ready: --ticket N, else every ticket the PR closes;
        land: the tickets it closes) and from the PR itself."""
        reviews = [harness_review(101, at(10), inline=[FROBS], body_only=[STALE])]
        rows = [  # (name, closing, ready's flags, comments, the tickets set in review)
            ("both on the ticket", (12,), (), {12: [owner_dismissal(FROBS, 12), owner_dismissal(STALE, 12, 12)]},
             (12,)),
            ("both on the PR", (12,), (), {34: [owner_dismissal(FROBS, 34), owner_dismissal(STALE, 34, 12)]}, (12,)),
            ("one on each", (12,), (), {12: [owner_dismissal(STALE, 12)], 34: [owner_dismissal(FROBS, 34)]}, (12,)),
            ("on the second ticket the PR closes", (12, 13), (),
             {13: [owner_dismissal(FROBS, 13)], 34: [owner_dismissal(STALE, 34)]}, (12, 13)),
            ("on the ticket --ticket names", (12,), ("--ticket", "7"),
             {7: [owner_dismissal(FROBS, 7), owner_dismissal(STALE, 7, 12)]}, (7,)),
        ]
        for name, closing, flags, notes, tickets in rows:
            with self.subTest(name, gate="ready"):
                p = self.ready(reviews, notes, closing=closing, flags=flags)
                self.assertReady(p, tickets)
        with self.subTest("one on the ticket, one on the PR", gate="land"):
            p = self.land(reviews, {12: [owner_dismissal(FROBS, 12)], 34: [owner_dismissal(STALE, 34, 12)]})
            self.assertLanded(p)

    def test_no_local_review_cache_is_needed(self):
        """No reviews folder in the harness state: the dismissal resolves against an earlier harness review read from
        GitHub (it raised FROBS at widget.py:3; the latest raises it again at line 5), and both gates go through."""
        self.assertEqual(glob.glob(os.path.join(self.home, ".claude", "harness", "**", "reviews"), recursive=True), [])
        self.assertFalse(os.path.exists(self.state_path("reviews")))
        reviews = [harness_review(100, at(8), inline=[FROBS], body_only=[STALE]),
                   harness_review(101, at(10), inline=[SHIFTED], body_only=[STALE])]
        notes = {12: [owner_dismissal(FROBS, 12, 9, "not reachable: the CLI validates first")],
                 34: [owner_dismissal(STALE, 34, 11, "docs only")]}
        p = self.ready(reviews, notes)
        self.assertReady(p)
        p = self.land(reviews, notes)
        self.assertLanded(p)

    def test_foreign_bot_and_harness_dismiss_lines_are_ignored(self):
        """A dismiss line by another account, by the bot, or in a comment the harness posted (its marker) dismisses
        nothing; the owner's own line does."""
        reviews = [harness_review(101, at(10), inline=[FROBS])]
        rows = [
            ("another account, on the ticket", {12: [note(PAL, 11, dismiss(FROBS, "I say so"), 12)]}),
            ("the bot, on the PR", {34: [note(BOT, 11, dismiss(FROBS, "the bot says so"), 34)]}),
            ("the harness's own comment, on the ticket",
             {12: [note(OWNER, 11, "Posted by a session.\n" + dismiss(FROBS, "an agent") + "\n\n" + HARNESS_MARK,
                        12)]}),
        ]
        for name, notes in rows:
            with self.subTest(name):
                p = self.ready(reviews, notes)
                self.assertNotReady(p)
                self.assertBlocking(p, [FROBS])
        with self.subTest("the owner's own"):
            self.assertReady(self.ready(reviews, {12: [owner_dismissal(FROBS, 12)]}))
        with self.subTest("all three", gate="land"):
            notes = {12: rows[0][1][12] + rows[2][1][12], 34: rows[1][1][34]}
            p = self.land(reviews, notes)
            self.assertNotLanded(p)
            self.assertBlocking(p, [FROBS])

    def test_an_ambiguous_or_reasonless_selector_keeps_the_finding_blocking(self):
        """A selector with no reason, naming no finding, or naming two findings at one place keeps what it meant
        blocking and gets one warning naming the selector; the request beside it that is exact still dismisses."""
        rows = [  # (name, inline, body_only, comments, left, dismissed, the bad selector, its reason)
            ("no reason", [FROBS], [STALE],
             {34: [owner_dismissal(FROBS), note(OWNER, 12, dismiss(STALE, ""), 34)]}, [STALE], [FROBS],
             selector(STALE), "no reason given"),
            ("no finding at that line", [FROBS], [STALE],
             {12: [owner_dismissal(FROBS, 12), note(OWNER, 12, "dismiss docs/count.md:8 docs only", 12)]}, [STALE],
             [FROBS], "docs/count.md:8", "no finding at docs/count.md:8 before the comment"),
            ("two findings at one place", [FROBS], [HANDLE, STALE],
             {34: [owner_dismissal(FROBS), owner_dismissal(STALE, 34, 12, "docs only")]}, [FROBS, HANDLE], [STALE],
             selector(FROBS), "ambiguous: 2 findings at widget.py:3"),
        ]
        for name, inline, body_only, notes, left, dismissed, bad, reason in rows:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only)]
            with self.subTest(name, gate="ready"):
                p = self.ready(reviews, notes)
                self.assertNotReady(p)
                self.assertBlocking(p, left, dismissed)
                self.assertWarnsOf(p, bad, reason)
        name, inline, body_only, notes, left, dismissed, bad, reason = rows[1]
        with self.subTest(name, gate="land"):
            p = self.land([harness_review(101, at(10), inline=inline, body_only=body_only)], notes)
            self.assertNotLanded(p)
            self.assertBlocking(p, left, dismissed)
            self.assertWarnsOf(p, bad, reason)

    def test_a_comment_older_than_the_finding_dismisses_nothing(self):
        """A comment posted before every harness review that lists a finding at its place dismisses nothing (a
        warning names the selector); after the review it does."""
        latest = harness_review(101, at(10), inline=[FROBS], body_only=[STALE])
        earlier = harness_review(100, at(8), body_only=[STALE])          # STALE was listed before 9:00, FROBS not
        both_at_9 = {34: [note(OWNER, 9, dismiss(FROBS, "not reachable") + "\n" + dismiss(STALE, "docs only"), 34)]}
        rows = [  # (name, reviews, comments, left, dismissed)
            ("before the only review", [latest], {34: [owner_dismissal(FROBS, 34, 9), owner_dismissal(STALE)]},
             [FROBS], [STALE]),
            ("after a review that did not list it", [earlier, latest], both_at_9, [FROBS], [STALE]),
        ]
        for name, reviews, notes, left, dismissed in rows:
            with self.subTest(name, gate="ready"):
                p = self.ready(reviews, notes)
                self.assertNotReady(p)
                self.assertBlocking(p, left, dismissed)
                self.assertWarnsOf(p, selector(FROBS))
        with self.subTest("after the review", gate="ready"):
            self.assertReady(self.ready([latest], {34: [owner_dismissal(FROBS, 34, 11),
                                                        owner_dismissal(STALE, 34, 12)]}))
        name, reviews, notes, left, dismissed = rows[1]
        with self.subTest(name, gate="land"):
            p = self.land(reviews, notes)
            self.assertNotLanded(p)
            self.assertBlocking(p, left, dismissed)
            self.assertWarnsOf(p, selector(FROBS))

    def test_dismissing_a_listed_blocker_adds_no_unlisted_one(self):
        """The summary's K more high or medium findings than the review lists are counted from the review as posted:
        dismissing a listed blocker leaves K as it was, and those K still refuse."""
        rows = [  # (name, inline, body_only, summary (total, high, medium, low), dismissed, left, K)
            ("one unlisted, the one listed blocker dismissed", [FROBS], [LOW], (3, 2, 0, 1), [FROBS], [], 1),
            ("two unlisted, one of two listed dismissed", [FROBS], [STALE], (4, 2, 2, 0), [FROBS], [STALE], 2),
            ("two unlisted, both listed dismissed", [FROBS], [STALE], (4, 2, 2, 0), [FROBS, STALE], [], 2),
        ]
        for name, inline, body_only, summary, dismissed, left, k in rows:
            reviews = [harness_review(101, at(10), inline=inline, body_only=body_only, summary=summary)]
            notes = {34: [owner_dismissal(f, 34, 11 + i, "out of scope") for i, f in enumerate(dismissed)]}
            with self.subTest(name, gate="ready"):
                p = self.ready(reviews, notes)
                self.assertNotReady(p)
                self.assertBlocking(p, left, dismissed, unlisted=k)
        name, inline, body_only, summary, dismissed, left, k = rows[1]
        with self.subTest(name, gate="land"):
            p = self.land([harness_review(101, at(10), inline=inline, body_only=body_only, summary=summary)],
                          {34: [owner_dismissal(f, 34, 11, "out of scope") for f in dismissed]})
            self.assertNotLanded(p)
            self.assertBlocking(p, left, dismissed, unlisted=k)

    def test_a_failed_dismissal_read_keeps_the_findings_blocking(self):
        """Every listed blocker dismissed, but one of the reads the dismissals need fails: the gate refuses naming
        every finding, and a warning names the failure. The PR's review comments (GET pulls/34/comments, where an
        inline finding's line comes from, #533) are the gate's read too: their failure refuses, naming it."""
        reviews = [harness_review(100, at(8), inline=[FROBS]),
                   harness_review(101, at(10), inline=[FROBS], body_only=[STALE])]
        notes = {12: [owner_dismissal(FROBS, 12)], 34: [owner_dismissal(STALE, 34, 12, "docs only")]}
        self.assertReady(self.ready(reviews, notes))           # the world reads cleanly
        failures = {
            "the login": [gate_failure({"match": ["api", "user"]})],
            "the ticket's comments": [read_failure("issues/12/comments")],
            "the PR's comments": [read_failure("issues/34/comments")],
        }
        for what, first in failures.items():
            with self.subTest(what, gate="ready"):
                p = self.ready(reviews, notes, first=first)
                self.assertNotReady(p)
                self.assertBlocking(p, [FROBS, STALE])
                self.assertTrue([w for w in self.warnings(p) if FAILED in w], p.stderr)
        with self.subTest("the PR's review comments", gate="ready"):
            p = self.ready(reviews, notes, first=[read_failure("pulls/34/comments")])
            self.assertNotReady(p)
            self.assertIn(FAILED, p.stderr)
        for what in ("the login", "the PR's comments"):
            with self.subTest(what, gate="land"):
                p = self.land(reviews, notes, first=failures[what])
                self.assertNotLanded(p)
                self.assertBlocking(p, [FROBS, STALE])
                self.assertTrue([w for w in self.warnings(p) if FAILED in w], p.stderr)

    def test_a_moved_inline_finding_is_named_where_a_selector_matches_it(self):
        """A push (a clean catch-up merge, say) moved two inline findings: each comment sits lower on the head
        (`line`) than where it was posted (`original_line`). Both refusals name them where they were posted, the
        place a `dismiss` selector matches, not where they sit now; the owner's `dismiss` of those places (and of the
        body finding) lets both gates through."""
        reviews = [posted(101, at(10), inline=[MOVED, RETRY], body_only=[STALE],
                          now={(MOVED[0], MOVED[1]): 14, (RETRY[0], RETRY[1]): 21})]
        notes = {12: [owner_dismissal(STALE, 12, 11, "docs only")],
                 34: [owner_dismissal(MOVED), owner_dismissal(RETRY, 34, 12, "capped upstream")]}
        for gate, run, refused, passed in (("ready", self.ready, self.assertNotReady, self.assertReady),
                                           ("land", self.land, self.assertNotLanded, self.assertLanded)):
            with self.subTest(gate=gate, comments=False):
                p = run(reviews)
                refused(p)
                self.assertBlocking(p, [MOVED, RETRY, STALE])
                for now in ("widget.py:14", "lib/cap.py:21"):
                    self.assertNotIn(now, self.refusal(p))
            with self.subTest(gate=gate):
                passed(run(reviews, notes))

    def test_an_inline_finding_github_lists_without_a_line_is_dismissed_at_its_line(self):
        """#533, the PR #528 case: GitHub's reviews/{id}/comments gives the latest harness review's inline findings
        no line, and pulls/34/comments lists them with their lines beside the comments of the PR's other reviews (an
        earlier harness review's and another account's, at FROBS's place). Both refusals name the latest review's
        inline findings at path:line and none of the other reviews' comments; the owner's `dismiss <path>:<line>` of
        each finding lets both gates through, with no warning that a selector dismisses nothing."""
        asked = "Is the empty input reachable at all?"
        pal = ({"id": 201, "user": {"login": PAL}, "body": "One question inline.", "state": "COMMENTED",
                "submitted_at": at(9), "commit_id": "c" * 40},
               [{"id": 20100, "pull_request_review_id": 201, "user": {"login": PAL}, "path": FROBS[0],
                 "line": FROBS[1], "original_line": FROBS[1], "side": "RIGHT", "body": asked, "created_at": at(9)}])
        reviews = [harness_review(100, at(8), inline=[HANDLE]), pal,
                   harness_review(101, at(10), inline=[FROBS, RETRY], body_only=[STALE])]
        notes = {12: [owner_dismissal(STALE, 12, 11, "docs only")],
                 34: [owner_dismissal(FROBS, 34, 11, "not reachable: the CLI validates first"),
                      owner_dismissal(RETRY, 34, 12, "capped upstream")]}
        for gate, run, refused, passed in (("ready", self.ready, self.assertNotReady, self.assertReady),
                                           ("land", self.land, self.assertNotLanded, self.assertLanded)):
            with self.subTest(gate=gate, comments=False):
                p = run(reviews)
                refused(p)
                self.assertBlocking(p, [FROBS, RETRY, STALE], [HANDLE, (FROBS[0], FROBS[1], "", asked)])
            with self.subTest(gate=gate):
                p = run(reviews, notes)
                passed(p)
                self.assertEqual([w for w in self.warnings(p) if "dismisses nothing" in w], [])

    def test_a_path_only_selector_dismisses_the_finding_listed_without_a_line(self):
        """A finding the review lists with no line (``- `path` [sev] body``) is dismissed by the owner's standalone
        `dismiss <path> <why>` (in backticks or not, a leading ./ dropped): it selects the line-less finding at that
        path, never one with a line there, which stays blocking with one warning `no finding at <path> before the
        comment`; with no reason it dismisses nothing (`no reason given`)."""
        two = note(OWNER, 11, "Carried over from the rename.\ndismiss docs/old.md renamed in #510\n"
                   "dismiss `./lib/gone.py` the lock moved with it", 12)
        rows = [  # (name, inline, body_only, comments, left, dismissed, the one warning's needles)
            ("two listed without a line, plain and in backticks", [FROBS], [OLD, GONE],
             {12: [two], 34: [owner_dismissal(FROBS)]}, [], [FROBS, OLD, GONE], ()),
            ("a line-less finding beside a lined one at its path", [OLD_AT], [OLD, STALE],
             {34: [note(OWNER, 11, "dismiss docs/old.md renamed in #510", 34), owner_dismissal(STALE, 34, 12)]},
             [OLD_AT], [OLD, STALE], ()),
            ("a path-only selector where the finding has a line", [FROBS], [STALE],
             {34: [owner_dismissal(FROBS), note(OWNER, 12, "dismiss docs/count.md docs only", 34)]}, [STALE],
             [FROBS], ("no finding at docs/count.md before the comment",)),
            ("no reason", [FROBS], [OLD],
             {34: [owner_dismissal(FROBS), note(OWNER, 12, "dismiss docs/old.md", 34)]}, [OLD], [FROBS],
             ("docs/old.md", "no reason given")),
        ]
        name, inline, body_only, notes = rows[0][:4]
        first = [posted(101, at(10), inline=inline, body_only=body_only)]
        with self.subTest(name, gate="ready", comments=False):
            p = self.ready(first)
            self.assertNotReady(p)
            self.assertBlocking(p, [FROBS, OLD, GONE])
        for name, inline, body_only, notes, left, dismissed, warned in rows:
            with self.subTest(name, gate="ready"):
                p = self.ready([posted(101, at(10), inline=inline, body_only=body_only)], notes)
                if left:
                    self.assertNotReady(p)
                    self.assertBlocking(p, left, dismissed)
                else:
                    self.assertReady(p)
                if warned:
                    self.assertWarnsOf(p, *warned)
        name, notes = rows[0][0], rows[0][3]
        with self.subTest(name, gate="land", comments=False):
            p = self.land(first)
            self.assertNotLanded(p)
            self.assertBlocking(p, [FROBS, OLD, GONE])
        with self.subTest(name, gate="land"):
            self.assertLanded(self.land(first, notes))

    def test_accept_findings_lands_reading_no_dismissal(self):
        """`land --accept-findings` accepts every finding, so dismissals cannot change whether it lands and none is
        read: with the owner's dismissal of one of its two blockers on the PR, it lands, makes no dismissal read
        before the merge, and its one findings warning names both blockers, the dismissed one too."""
        reviews = [harness_review(101, at(10), inline=[FROBS], body_only=[STALE])]
        p = self.land(reviews, {34: [owner_dismissal(FROBS)]}, flags=("--accept-findings",))
        self.assertLanded(p)
        self.assertEqual(self.dismissal_reads(before_merge=True), [])
        self.assertWarnsOf(p, selector(FROBS), FROBS[3], selector(STALE), STALE[3])


if __name__ == "__main__":
    unittest.main()
