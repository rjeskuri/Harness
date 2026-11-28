"""Tests for harness_lib/dismissals.py (ticket #515, epic #512): the owner's `dismiss <path>:<line> <why>` lines and
which review findings they stop from blocking.

- parse(comments, login, source): one request {"path", "line", "why", "at", "url", "source"} per standalone line of
  the owner's own comments (owner_comments.owner_comments: login casefolded, harness-marked comments dropped); the
  keyword `dismiss` (any case) first on its line after optional spaces, then `path:line` (optionally in backticks, a
  leading `./` dropped), then the reason ("" when none);
- resolve(requests, snapshots) -> (dismissed, unmatched): each request is matched in the newest snapshot older than
  its comment that holds a finding at its path and line; one finding_key dismisses it ({"key": [path, body], "path",
  "line", "why", "at", "url", "source"}, one per key, the earliest request's); otherwise it is unmatched
  ({"selector": "path:line", "reason", "at", "url", "source"}) as `no reason given`, `no finding at <path>:<line>
  before the comment` or `ambiguous: <n> findings at <path>:<line>`;
- apply(findings, dismissed) -> (kept, dropped): exactly the findings whose finding_key is dismissed are dropped,
  each with the dismissal's `why` and `url` added, whatever its line or severity;
- review_findings(review, comments): the findings harness-pr posted (review_body and post_review), inline and in the
  body, a multi-line body restored, a missing or unknown severity read as high; an inline comment sits where it was
  posted (original_line, else line), and only the trailer post_review appends is stripped from it, so a finding that
  quotes the marker comes back with its own body and key;
- read(cwd, tickets, pr) and github_snapshots(cwd, pr, reviews=None): the requests on the tickets and the PR ordered
  by time, and one {"at", "findings"} per harness review on the PR, oldest first: a review by the authenticated
  account (gh api user, casefolded) whose body ends with the generated marker (then, optionally, the GPT receipt),
  read from GitHub or given; every gh call goes through harness_lib.gh (so the gh call log counts it), and a failed
  read raises Problem.

The pure functions run in process on fixtures written here; the readers run against fakegh.py as HARNESS_GH (its REST
world for the comments, rest entries for the reviews), in a temporary HOME. Orders the ticket leaves open (the
findings of one review, the dismissed and unmatched lists) are compared as bags.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_dismissals.py" -v
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"
FAKEGH = TESTS / "fakegh.py"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import fakegh  # noqa: E402  (the scripted fake gh's scenario helpers)

DAY = "2026-11-27"
MARK = "<!-- harness -->"                 # harness-board's comments
REVIEW_MARK = "<!-- harness-review -->"   # harness-pr's reviews and their inline comments
RECEIPT = "<!-- harness-gpt-0b7e2c1a-5f7d-4c1e-9a43-2d1f0c9e8b77 -->"
OWNER = "rjeskuri"
BOT = "github-actions[bot]"


def lib(name):
    """harness_lib.<name> from the checkout's lib/; the import error while dismissals is missing is the red."""
    path = str(LIB)
    if path not in sys.path:
        sys.path.insert(0, path)
    importlib.invalidate_caches()
    return importlib.import_module(f"harness_lib.{name}")


def at(hhmm):
    return f"{DAY}T{hhmm}:00Z"


def url_of(number, hhmm):
    return f"https://github.com/o/r/issues/{number}#issuecomment-{number}{hhmm.replace(':', '')}"


def comment(login, hhmm, body, number=12):
    """A comment as harness_lib.issues.Issues.comments returns it; login None is a comment without an author."""
    return {"author": {"login": login} if login else None, "body": body, "createdAt": at(hhmm),
            "url": url_of(number, hhmm)}


def request(path, line, why, hhmm, source="ticket #12", number=12):
    """A dismissal request as parse gives it, for the comment at `hhmm` on issue `number`."""
    return {"path": path, "line": line, "why": why, "at": at(hhmm), "url": url_of(number, hhmm), "source": source}


def F(path, line, severity, body):
    return {"path": path, "line": line, "severity": severity, "body": body}


def snapshot(hhmm, *findings):
    return {"at": at(hhmm), "findings": list(findings)}


def dismissal(req, key):
    return {"key": list(key), "path": req["path"], "line": req["line"], "why": req["why"], "at": req["at"],
            "url": req["url"], "source": req["source"]}


def unmatched(req, reason):
    return {"selector": f"{req['path']}:{req['line']}", "reason": reason, "at": req["at"], "url": req["url"],
            "source": req["source"]}


def dropped(finding, req):
    return dict(finding, why=req["why"], url=req["url"])


def bag(items):
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def review(rid, hhmm, body, login=OWNER):
    """A GitHub review object as `gh api repos/{o}/{r}/pulls/<n>/reviews` lists it."""
    return {"id": rid, "user": {"login": login}, "body": body, "state": "COMMENTED", "submitted_at": at(hhmm),
            "commit_id": "a" * 40, "html_url": f"https://github.com/o/r/pull/7#pullrequestreview-{rid}"}


SAME = object()


def inline(cid, path, line, text, original_line=SAME):
    """An inline review comment as post_review posts it: `[sev] body`, a blank line, the review marker. GitHub's
    original_line is where it was posted (the line itself unless given), line where it sits on the PR's head now."""
    return {"id": cid, "path": path, "line": line, "original_line": line if original_line is SAME else original_line,
            "side": "RIGHT", "body": f"{text}\n\n{REVIEW_MARK}"}


# --------------------------------------------------------------- a review as harness-pr posts it, written out by hand
SUMMARY = "Harness review (claude-opus-5-5): 8 findings, 3 high, 3 medium, 2 low."
LISTED = "\n".join([
    "Not on a changed line:",
    "- `src/app.py:12` [high] V12 overflows.",
    "- `src/app.py:40` [medium] Fails on an empty list.",      # a multi-line body, indented two spaces
    "  Repro:",
    "      items = []",
    "  ",
    "  Guard the first element.",
    "- `docs/design.md` [low] The State entry lacks `dismissed`.",
    "- (no path) [urgent] The PR body names no ticket.",
])
BODY_FINDINGS = [
    F("src/app.py", 12, "high", "V12 overflows."),
    F("src/app.py", 40, "medium", "Fails on an empty list.\nRepro:\n    items = []\n\nGuard the first element."),
    F("docs/design.md", None, "low", "The State entry lacks `dismissed`."),
    F("", None, "high", "The PR body names no ticket."),
]
INLINE = [
    inline(1, "src/app.py", 7, "[high] Inline high."),
    inline(2, "src/app.py", None, "[medium] Outdated now.\nSecond line.", original_line=21),   # line null: outdated
    inline(3, "lib/db.py", 3, "No severity prefix.", original_line=2),   # posted at 2, a push moved it to 3
    inline(4, "lib/db.py", 9, "[low] Minor.", original_line=None),       # no original_line: at line
]
INLINE_FINDINGS = [
    F("src/app.py", 7, "high", "Inline high."),
    F("src/app.py", 21, "medium", "Outdated now.\nSecond line."),
    F("lib/db.py", 2, "high", "No severity prefix."),
    F("lib/db.py", 9, "low", "Minor."),
]
NO_FINDINGS = "Harness review: no findings. Checked: the diff against the ticket's definition of done"


class Base(unittest.TestCase):
    """dismissals loaded; resolve and apply give their pair as a tuple, whichever sequence the module returns."""

    def setUp(self):
        self.d = lib("dismissals")

    def resolve(self, requests, snapshots):
        return tuple(self.d.resolve(requests, snapshots))

    def apply(self, findings, dismissed):
        return tuple(self.d.apply(findings, dismissed))


class Requests(Base):
    def test_only_the_owners_standalone_lines_are_requests(self):
        comments = [
            comment("RJeskuri", "10:00", "Thanks.\ndismiss src/app.py:12 not reachable: the CLI validates first"),
            comment(OWNER, "10:05", "please dismiss src/app.py:12 later"),                  # inside a sentence
            comment("someone", "10:10", "dismiss src/app.py:12 I say so"),                   # another account
            comment(BOT, "10:15", "dismiss src/app.py:12 the bot\n\n" + MARK),               # the bot's
            comment(BOT, "10:16", "dismiss src/app.py:12 the bot again"),
            comment(OWNER, "10:20", "Posted by a session.\ndismiss src/app.py:12 an agent\n\n" + MARK),   # harness's
            comment(OWNER, "10:25", "Started on t/12-x (mac.local)\ndismiss src/app.py:12 an agent"),     # older one
            comment(None, "10:30", "dismiss src/app.py:12 a ghost"),
            comment(OWNER, "10:35", "> dismiss src/app.py:12 quoted\nNo."),
            comment(OWNER, "10:40", "Two more:\r\n  DISMISS `./lib/db.py:7` runs once a day\r\n"
                                    "Dismiss ./src/cli.py:3 covered by test_cli\r\n"),
            comment(OWNER, "10:45", "dismissed src/app.py:12 yesterday"),
            comment(OWNER, "10:50", "dismiss src/app.py:40"),                                # no reason
        ]
        want = [
            request("src/app.py", 12, "not reachable: the CLI validates first", "10:00"),
            request("lib/db.py", 7, "runs once a day", "10:40"),
            request("src/cli.py", 3, "covered by test_cli", "10:40"),
            request("src/app.py", 40, "", "10:50"),
        ]
        for login in ("rjeskuri", "RJESKURI", "RJeskuri"):
            with self.subTest(login=login):
                self.assertEqual(self.d.parse(comments, login, "ticket #12"), want)
        self.assertEqual(self.d.parse(comments, OWNER, "PR #7"), [dict(r, source="PR #7") for r in want])
        # another login reads that account's lines alone
        self.assertEqual(self.d.parse(comments, "someone", "ticket #12"),
                         [request("src/app.py", 12, "I say so", "10:10")])
        for name, cs, login in (("no comments", [], OWNER), ("no login", comments, ""),
                                ("only the owner's harness comments", comments[5:7], OWNER)):
            with self.subTest(name):
                self.assertEqual(self.d.parse(cs, login, "ticket #12"), [])

    def test_a_line_without_a_reason_is_unmatched(self):
        comments = [comment(OWNER, "10:00", "dismiss src/app.py:12"),
                    comment(OWNER, "10:05", "Looked again.\n  dismiss `src/app.py:12`   \nThat is all."),
                    comment(OWNER, "10:10", "dismiss lib/db.py:7")]
        requests = self.d.parse(comments, OWNER, "ticket #12")
        self.assertEqual(requests, [request("src/app.py", 12, "", "10:00"), request("src/app.py", 12, "", "10:05"),
                                    request("lib/db.py", 7, "", "10:10")])
        findings = [F("src/app.py", 12, "high", "V12 overflows."), F("lib/db.py", 7, "medium", "Slow query.")]
        snapshots = [snapshot("09:00", *findings)]
        dismissed, unmatched_ = self.resolve(requests, snapshots)
        self.assertEqual(dismissed, [])
        self.assertEqual(bag(unmatched_), bag(unmatched(r, "no reason given") for r in requests))
        self.assertEqual(self.apply(findings, dismissed), (findings, []))
        # the same selector with a reason dismisses
        given = request("src/app.py", 12, "the CLI validates first", "10:15")
        self.assertEqual(self.resolve([given], snapshots),
                         ([dismissal(given, ["src/app.py", "V12 overflows."])], []))


class Resolve(Base):
    def test_a_selector_dismisses_a_finding_from_before_the_comment(self):
        snapshots = [
            snapshot("08:00", F("src/app.py", 12, "high", "V12 overflows."),
                     F("src/app.py", 30, "medium", "Unchecked None."),
                     F("lib/db.py", 7, "low", "Slow query.")),
            snapshot("09:00", F("src/app.py", 14, "high", "V12 overflows."),        # moved two lines down
                     F("src/app.py", 12, "medium", "Leaks the handle."),
                     F("lib/db.py", 7, "low", "Slow query.")),
            snapshot("11:00", F("src/app.py", 12, "high", "Retries forever.")),
        ]
        cases = [
            ("the one snapshot before it", request("src/app.py", 12, "not reachable", "08:30"),
             ["src/app.py", "V12 overflows."]),
            ("the newest snapshot before it", request("src/app.py", 12, "closed on exit", "10:00"),
             ["src/app.py", "Leaks the handle."]),
            ("the newest one holding that place", request("src/app.py", 30, "checked by the caller", "10:10"),
             ["src/app.py", "Unchecked None."]),
            ("a low finding", request("lib/db.py", 7, "runs once a day", "10:20"), ["lib/db.py", "Slow query."]),
            ("after every snapshot", request("src/app.py", 12, "bounded by the CLI", "12:00"),
             ["src/app.py", "Retries forever."]),
            ("from a PR comment", request("src/app.py", 14, "same as before", "09:30", "PR #7", 7),
             ["src/app.py", "V12 overflows."]),
        ]
        for name, req, key in cases:
            with self.subTest(name):
                self.assertEqual(self.resolve([req], snapshots), ([dismissal(req, key)], []))
        dismissed, unmatched_ = self.resolve([req for _, req, _ in cases[:5]], snapshots)
        self.assertEqual(bag(dismissed), bag(dismissal(req, key) for _, req, key in cases[:5]))
        self.assertEqual(unmatched_, [])
        # what they dismissed stops blocking in the latest snapshot; the rest stays
        latest = [F("src/app.py", 12, "high", "Retries forever."), F("src/app.py", 50, "high", "New problem.")]
        self.assertEqual(self.apply(latest, dismissed), (latest[1:], [dropped(latest[0], cases[4][1])]))

    def test_an_exact_reraise_on_a_shifted_line_stays_dismissed(self):
        before = snapshot("09:00", F("src/app.py", 12, "high", "V12 overflows."),
                          F("src/app.py", 30, "medium", "Unchecked None."))
        first = request("src/app.py", 12, "not reachable: the CLI validates first", "10:00")
        key = ["src/app.py", "V12 overflows."]
        dismissed, _ = self.resolve([first], [before])
        self.assertEqual(dismissed, [dismissal(first, key)])
        others = [F("src/app.py", 12, "high", "Leaks the handle."), F("src/app.py", 30, "medium", "Unchecked None.")]
        for name, reraise in (("two lines down, medium", F("src/app.py", 14, "medium", "V12 overflows.")),
                              ("far down, low", F("src/app.py", 220, "low", "V12 overflows.")),
                              ("the same place", F("src/app.py", 12, "high", "V12 overflows.")),
                              ("no line", F("src/app.py", None, "high", "V12 overflows."))):
            with self.subTest(name):
                kept, gone = self.apply([others[0], reraise, others[1]], dismissed)
                self.assertEqual(kept, others)
                self.assertEqual(gone, [dropped(reraise, first)])
        # two dismissals, both re-raised elsewhere
        second = request("src/app.py", 30, "checked by the caller", "10:30")
        dismissed, unmatched_ = self.resolve([first, second], [before])
        self.assertEqual(unmatched_, [])
        current = [F("src/app.py", 16, "high", "V12 overflows."), F("src/app.py", 12, "high", "Leaks the handle."),
                   F("src/app.py", 33, "low", "Unchecked None.")]
        kept, gone = self.apply(current, dismissed)
        self.assertEqual(kept, [current[1]])
        self.assertEqual(bag(gone), bag([dropped(current[0], first), dropped(current[2], second)]))
        self.assertEqual(self.apply(current, []), (current, []))
        # a later selector on the shifted line names the same key: still one dismissal, the earliest request's
        after = snapshot("11:00", F("src/app.py", 14, "medium", "V12 overflows."))
        again = request("src/app.py", 14, "still not reachable", "11:30", "PR #7", 7)
        self.assertEqual(self.resolve([first, again], [before, after]), ([dismissal(first, key)], []))

    def test_a_comment_older_than_every_finding_matches_nothing(self):
        snapshots = [snapshot("09:00", F("src/app.py", 12, "high", "V12 overflows.")),
                     snapshot("10:00", F("src/app.py", 12, "high", "V12 overflows."),
                              F("lib/db.py", 7, "low", "Slow query."))]
        cases = [
            ("before every review", request("src/app.py", 12, "not reachable", "08:00"), "src/app.py:12"),
            ("before the review that raised it", request("lib/db.py", 7, "runs once a day", "09:30"), "lib/db.py:7"),
            ("a line no review named", request("src/app.py", 13, "not reachable", "11:00"), "src/app.py:13"),
            ("a file no review named", request("src/cli.py", 12, "not reachable", "11:00"), "src/cli.py:12"),
        ]
        for name, req, place in cases:
            with self.subTest(name):
                self.assertEqual(self.resolve([req], snapshots),
                                 ([], [unmatched(req, f"no finding at {place} before the comment")]))
        req = cases[0][1]
        self.assertEqual(self.resolve([req], []),
                         ([], [unmatched(req, "no finding at src/app.py:12 before the comment")]))
        dismissed, unmatched_ = self.resolve([c[1] for c in cases], snapshots)
        self.assertEqual(dismissed, [])
        self.assertEqual(len(unmatched_), len(cases))
        latest = snapshots[-1]["findings"]
        self.assertEqual(self.apply(latest, dismissed), (latest, []))
        # the same selectors once the findings were posted
        later = request("lib/db.py", 7, "runs once a day", "10:30")
        self.assertEqual(self.resolve([later], snapshots), ([dismissal(later, ["lib/db.py", "Slow query."])], []))

    def test_two_findings_at_one_place_are_ambiguous(self):
        snapshots = [
            snapshot("08:00", F("src/app.py", 12, "high", "V12 overflows.")),
            snapshot("09:00", F("src/app.py", 12, "high", "V12 overflows."),
                     F("src/app.py", 12, "medium", "Leaks the handle."),
                     F("src/app.py", 13, "low", "A typo.")),
            snapshot("11:00", F("lib/db.py", 7, "high", "Slow query."), F("lib/db.py", 7, "high", "No index."),
                     F("lib/db.py", 7, "low", "A long line.")),
        ]
        cases = [
            ("two at one place", request("src/app.py", 12, "not reachable", "10:00"),
             "ambiguous: 2 findings at src/app.py:12"),
            ("three at one place", request("lib/db.py", 7, "runs once a day", "12:00"),
             "ambiguous: 3 findings at lib/db.py:7"),
            ("the newest snapshot holding that place decides", request("src/app.py", 12, "not reachable", "11:30"),
             "ambiguous: 2 findings at src/app.py:12"),
        ]
        for name, req, reason in cases:
            with self.subTest(name):
                self.assertEqual(self.resolve([req], snapshots), ([], [unmatched(req, reason)]))
        dismissed, _ = self.resolve([c[1] for c in cases], snapshots)
        for snap in snapshots[1:]:
            self.assertEqual(self.apply(snap["findings"], dismissed), (snap["findings"], []))
        # before the second finding came, and on the line next to them, the selector is exact
        early = request("src/app.py", 12, "not reachable", "08:30")
        self.assertEqual(self.resolve([early], snapshots), ([dismissal(early, ["src/app.py", "V12 overflows."])], []))
        typo = request("src/app.py", 13, "a typo is fine", "10:00")
        self.assertEqual(self.resolve([typo], snapshots), ([dismissal(typo, ["src/app.py", "A typo."])], []))

    def test_a_reworded_reraise_is_kept(self):
        first = request("src/app.py", 12, "not reachable", "10:00")
        dismissed, _ = self.resolve([first], [snapshot("09:00", F("src/app.py", 12, "high", "V12 overflows."))])
        self.assertEqual(dismissed, [dismissal(first, ["src/app.py", "V12 overflows."])])
        exact = F("src/app.py", 15, "medium", "V12 overflows.")
        for name, other in (("reworded", F("src/app.py", 12, "high", "V12 overflows when n > 2**31.")),
                            ("its full stop dropped", F("src/app.py", 12, "high", "V12 overflows")),
                            ("in another case", F("src/app.py", 12, "high", "v12 overflows.")),
                            ("the same text on another file", F("src/cli.py", 12, "high", "V12 overflows."))):
            with self.subTest(name):
                self.assertEqual(self.apply([other, exact], dismissed), ([other], [dropped(exact, first)]))


class ReviewFindings(Base):
    def test_review_findings_restores_what_harness_pr_posted(self):
        gpt = "\n\n".join(["Harness review (gpt-6.2-sol): 2 findings, 0 high, 1 medium, 1 low.",
                           "Not on a changed line:\n- `lib/x.py:3` [medium] Two\n  lines.\n- `lib/x.py:9` [low] One.",
                           REVIEW_MARK, RECEIPT])
        cases = [
            ("body and inline", review(101, "09:00", "\n\n".join([SUMMARY, LISTED, REVIEW_MARK, RECEIPT])), INLINE,
             BODY_FINDINGS + INLINE_FINDINGS),
            ("the body alone, no receipt", review(102, "09:10", "\n\n".join([SUMMARY, LISTED, REVIEW_MARK])), [],
             BODY_FINDINGS),
            ("inline alone", review(103, "09:20", "\n\n".join([SUMMARY, REVIEW_MARK])), INLINE[:2],
             INLINE_FINDINGS[:2]),
            ("another reviewer's", review(104, "09:30", gpt), [],
             [F("lib/x.py", 3, "medium", "Two\nlines."), F("lib/x.py", 9, "low", "One.")]),
            ("no findings", review(105, "09:40", f"{NO_FINDINGS}\n\n{REVIEW_MARK}"), [], []),
            ("no findings, a receipt", review(106, "09:50", f"{NO_FINDINGS}\n\n{REVIEW_MARK}\n\n{RECEIPT}"), [], []),
        ]
        for name, rv, comments, want in cases:
            with self.subTest(name):
                got = self.d.review_findings(rv, comments)
                self.assertEqual(bag(got), bag(want))
        # restored exactly: a selector on a restored finding dismisses the original by its key
        rv = review(101, "09:00", "\n\n".join([SUMMARY, LISTED, REVIEW_MARK]))
        snap = {"at": rv["submitted_at"], "findings": self.d.review_findings(rv, INLINE)}
        req = request("src/app.py", 40, "an empty list never reaches it", "10:00")
        dismissed, _ = self.resolve([req], [snap])
        self.assertEqual(dismissed, [dismissal(req, ["src/app.py", BODY_FINDINGS[1]["body"]])])
        self.assertEqual(self.apply(BODY_FINDINGS, dismissed),
                         ([BODY_FINDINGS[0]] + BODY_FINDINGS[2:], [dropped(BODY_FINDINGS[1], req)]))

    def test_a_finding_that_quotes_the_marker_round_trips(self):
        """Only the trailer post_review appends to an inline comment (a blank line, then the marker) is the marker:
        a finding whose own text quotes it comes back with that text, so its finding_key is the original's."""
        quoted = [F("src/review.py", 8, "medium", f"review_findings drops every {REVIEW_MARK}, quoted ones too."),
                  F("src/review.py", 20, "high", f"A body may end with the marker:\n{REVIEW_MARK}")]
        outside = F("src/review.py", 30, "low", f"Not inline, and it quotes {REVIEW_MARK} as well.")
        findings = quoted + [outside]
        # built as post_review builds them: `[sev] body`, a blank line, the marker; the rest listed as review_body does
        comments = [{"id": 90 + i, "path": f["path"], "line": f["line"], "original_line": f["line"], "side": "RIGHT",
                     "body": f"[{f['severity']}] {f['body']}\n\n{REVIEW_MARK}"} for i, f in enumerate(quoted)]
        rv = review(109, "09:00", "\n\n".join([
            "Harness review (claude-opus-5-5): 3 findings, 1 high, 1 medium, 1 low.",
            f"Not on a changed line:\n- `src/review.py:30` [low] {outside['body']}", REVIEW_MARK]))
        restored = self.d.review_findings(rv, comments)
        self.assertEqual(bag(restored), bag(findings))
        # the owner's dismissal from the rebuilt snapshot drops the finding as the findings JSON raises it again
        for f in findings:
            with self.subTest(f["body"]):
                req = request(f["path"], f["line"], "quoting it is the point", "10:00")
                dismissed, _ = self.resolve([req], [{"at": rv["submitted_at"], "findings": restored}])
                self.assertEqual(dismissed, [dismissal(req, [f["path"], f["body"]])])
                self.assertEqual(self.apply(findings, dismissed),
                                 ([g for g in findings if g is not f], [dropped(f, req)]))


class GitHub(Base):
    """read and github_snapshots against fakegh's REST world, in a temporary HOME."""

    def setUp(self):
        super().setUp()
        self.Problem = lib("cli").Problem
        self.parse_iso = lib("text").parse_iso
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-dismissals-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GH_"))}
        env.update(HOME=self.home, HARNESS_GH=str(FAKEGH), FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------------------------------------------------- the world
    ISSUES = {
        "12": {"number": 12, "comments": [
            comment(OWNER, "10:00", "dismiss src/app.py:12 not reachable: the CLI validates first", 12),
            comment("someone", "10:05", "dismiss src/app.py:14 me too", 12),
            comment("RJeskuri", "12:00", "Thanks.\ndismiss `lib/db.py:7` runs once a day", 12)]},
        "13": {"number": 13, "comments": [comment(OWNER, "09:15", "dismiss ./src/app.py:30 covered by #13", 13)]},
        "7": {"number": 7, "pull_request": True, "comments": [
            comment(OWNER, "11:00", "dismiss src/app.py:14 the same overflow, shifted", 7),
            comment(OWNER, "11:30", "Fixed in abc1234.\ndismiss src/app.py:12 an agent\n\n" + MARK, 7)]},
        "56": {"number": 56, "comments": [comment(OWNER, "08:00", "dismiss src/app.py:12 not asked for", 56)]},
    }
    REQUESTS = [
        request("src/app.py", 30, "covered by #13", "09:15", "ticket #13", 13),
        request("src/app.py", 12, "not reachable: the CLI validates first", "10:00", "ticket #12", 12),
        request("src/app.py", 14, "the same overflow, shifted", "11:00", "PR #7", 7),
        request("lib/db.py", 7, "runs once a day", "12:00", "ticket #12", 12),
    ]
    R101 = review(101, "09:00", "\n\n".join([
        "Harness review (claude-opus-5-5): 3 findings, 1 high, 1 medium, 1 low.",
        "Not on a changed line:\n- `src/app.py:30` [medium] Unchecked None.\n- `lib/db.py:7` [low] Slow query.",
        REVIEW_MARK]))
    R102 = review(102, "10:30", "\n\n".join(["Harness review (gpt-6.2-sol): 2 findings, 2 high, 0 medium, 0 low.",
                                            REVIEW_MARK, RECEIPT]))
    R103 = review(103, "11:45", f"{NO_FINDINGS}\n\n{REVIEW_MARK}")
    R104 = review(104, "09:30", "Looks fine to me.", login="someone")       # no marker: no harness review
    # another account's review written like a harness one: it would make the owner's 10:00 `dismiss src/app.py:12`
    # waive "Leaks the handle." and leave "V12 overflows." blocking
    R105 = review(105, "09:40", "\n\n".join(["Harness review (claude-opus-5-5): 1 finding, 1 high, 0 medium, 0 low.",
                                            REVIEW_MARK]), login="someone")
    # the owner's own reviews citing the marker, not ending with it: no harness reviews
    R106 = review(106, "10:45", f"Read it by hand: harness-pr ends its reviews with {REVIEW_MARK}, so this one is "
                                "not the loop's.\n\nOne question inline.")
    R107 = review(107, "10:50", "\n\n".join(["Quoting the trailer the loop looks for:", REVIEW_MARK,
                                            "This review is mine, not the loop's."]))
    REVIEWS = [R103, R101, R106, R104, R105, R102, R107]
    COMMENTS = {101: [inline(11, "src/app.py", 14, "[high] V12 overflows.", original_line=12)],   # posted at 12,
                102: [inline(21, "src/app.py", 14, "[high] V12 overflows."),                     # pushed to 14 since
                      inline(22, "src/app.py", 12, "[high] Leaks the handle.")],
                103: [],
                104: [inline(41, "src/app.py", 12, "[high] Planted.")],
                105: [inline(51, "src/app.py", 12, "[high] Leaks the handle.")],
                106: [{"id": 61, "path": "src/app.py", "line": 14, "original_line": 14, "side": "RIGHT",
                       "body": "Is this overflow reachable? I could not hit it."}],
                107: [{"id": 71, "path": "src/app.py", "line": 14, "original_line": 14, "side": "RIGHT",
                       "body": "Same question: is the overflow real?"}]}
    SNAPSHOTS = [
        (at("09:00"), [F("src/app.py", 30, "medium", "Unchecked None."), F("lib/db.py", 7, "low", "Slow query."),
                       F("src/app.py", 12, "high", "V12 overflows.")]),
        (at("10:30"), [F("src/app.py", 14, "high", "V12 overflows."),
                       F("src/app.py", 12, "high", "Leaks the handle.")]),
        (at("11:45"), []),
    ]

    def world(self, overrides=(), login_answer=None):
        """The scenario: `overrides` (rest entries, first match wins) before the reviews and their comments, the
        issues above, and `gh api user` answering the owner's login."""
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.remove(state)
        calls = list(overrides)
        calls.append(fakegh.rest_entry("pulls/7/reviews", "GET", exact=True, stdout_json=self.REVIEWS))
        for rid, cs in self.COMMENTS.items():
            calls.append(fakegh.rest_entry(f"pulls/7/reviews/{rid}/comments", "GET", exact=True, stdout_json=cs))
        calls.append(dict({"match": ["api", "user"]}, **(login_answer or {"stdout": "RJeskuri\n"})))
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump({"calls": calls, "issues": self.ISSUES}, fh)

    def shaped(self, snapshots):
        return [(self.parse_iso(s["at"]), bag(s["findings"])) for s in snapshots]

    def expected(self, rows):
        return [(self.parse_iso(when), bag(findings)) for when, findings in rows]

    def gh_calls(self):
        """(fakegh invocations, gh calls harness_lib.gh logged in the temporary HOME)."""
        logged = 0
        for path in glob.glob(os.path.join(self.home, ".claude", "harness", "gh", "calls-*.jsonl")):
            with open(path, encoding="utf-8") as fh:
                logged += sum(1 for line in fh if line.strip())
        return len(fakegh.log_entries(self.gh_log)), logged

    # ---------------------------------------------------- tests
    def test_read_and_snapshots_come_from_github(self):
        self.world()
        requests = self.d.read(self.tmp, [12, 13], 7)
        self.assertEqual(requests, self.REQUESTS)
        snapshots = self.d.github_snapshots(self.tmp, 7)
        self.assertEqual(self.shaped(snapshots), self.expected(self.SNAPSHOTS))
        ran, logged = self.gh_calls()
        self.assertGreater(ran, 0)
        self.assertEqual(logged, ran, "every gh call goes through harness_lib.gh")

        # together: what the owner dismissed, from GitHub state alone
        dismissed, unmatched_ = self.resolve(requests, snapshots)
        r30, r12, r14, r7 = self.REQUESTS
        self.assertEqual(unmatched_, [])
        self.assertEqual(bag(dismissed), bag([dismissal(r30, ["src/app.py", "Unchecked None."]),
                                              dismissal(r12, ["src/app.py", "V12 overflows."]),
                                              dismissal(r7, ["lib/db.py", "Slow query."])]))
        latest = snapshots[1]["findings"]
        kept, gone = self.apply(latest, dismissed)
        self.assertEqual(bag(kept), bag([F("src/app.py", 12, "high", "Leaks the handle.")]))
        self.assertEqual(gone, [dropped(F("src/app.py", 14, "high", "V12 overflows."), r12)])

        # reviews given: those are read, not the PR's list (which fails here)
        self.world([fakegh.rest_entry("pulls/7/reviews", "GET", exact=True,
                                      stderr="HTTP 502: Bad Gateway\n", exit=1)])
        given = self.d.github_snapshots(self.tmp, 7, reviews=[self.R102, self.R104])
        self.assertEqual(self.shaped(given), self.expected(self.SNAPSHOTS[1:2]))
        # one ticket, its own comments only
        self.assertEqual(self.d.read(self.tmp, [13], 7), [self.REQUESTS[0], self.REQUESTS[2]])
        ran, logged = self.gh_calls()
        self.assertEqual(logged, ran, "every gh call goes through harness_lib.gh")

    def test_only_the_owners_generated_reviews_are_snapshots(self):
        """A stranger's review carrying the marker, and the owner's review citing it anywhere but as its trailer, are
        no snapshots: the owner's dismissal still resolves to the harness finding it names."""
        self.world()
        r30, r12, r14, r7 = self.REQUESTS
        latest = self.SNAPSHOTS[1][1]
        cases = [("a stranger's marked review", self.R105, r12),
                 ("the owner citing the marker in a sentence", self.R106, r14),
                 ("the owner quoting the marker mid-review", self.R107, r14)]
        for name, extra, req in cases:
            with self.subTest(name):
                snapshots = self.d.github_snapshots(self.tmp, 7, reviews=[extra, self.R102, self.R101])
                self.assertEqual([self.parse_iso(s["at"]) for s in snapshots],
                                 [self.parse_iso(at("09:00")), self.parse_iso(at("10:30"))])
                dismissed, unmatched_ = self.resolve([req], snapshots)
                self.assertEqual((dismissed, unmatched_), ([dismissal(req, ["src/app.py", "V12 overflows."])], []))
                self.assertEqual(self.apply(latest, dismissed),
                                 ([F("src/app.py", 12, "high", "Leaks the handle.")],
                                  [dropped(F("src/app.py", 14, "high", "V12 overflows."), req)]))

    def test_a_failed_read_raises(self):
        self.world()                    # the fixture itself reads cleanly
        self.d.read(self.tmp, [12, 13], 7)
        self.d.github_snapshots(self.tmp, 7)
        fail = {"stderr": "HTTP 502: Bad Gateway\n", "exit": 1}
        reads = {"read": lambda: self.d.read(self.tmp, [12, 13], 7),
                 "snapshots": lambda: self.d.github_snapshots(self.tmp, 7),
                 "given": lambda: self.d.github_snapshots(self.tmp, 7, reviews=[self.R101, self.R102])}
        cases = [
            ("gh api user fails", "read", [], {"stderr": "HTTP 401: Bad credentials\n", "exit": 1}),
            ("a ticket's comments fail", "read", [fakegh.rest_entry("issues/13/comments", "GET", **fail)], None),
            ("the PR's comments fail", "read", [fakegh.rest_entry("issues/7/comments", "GET", **fail)], None),
            ("a ticket gh cannot find", "missing", [], None),
            ("the reviews list fails", "snapshots", [fakegh.rest_entry("pulls/7/reviews", "GET", exact=True, **fail)],
             None),
            ("the reviews list is not JSON", "snapshots",
             [fakegh.rest_entry("pulls/7/reviews", "GET", exact=True, stdout="<html>unicorn</html>")], None),
            ("a review's comments fail", "snapshots",
             [fakegh.rest_entry("pulls/7/reviews/102/comments", "GET", exact=True, **fail)], None),
            ("a given review's comments fail", "given",
             [fakegh.rest_entry("pulls/7/reviews/101/comments", "GET", exact=True, **fail)], None),
            ("gh api user fails for the snapshots", "given", [], {"stderr": "HTTP 401: Bad credentials\n", "exit": 1}),
        ]
        reads["missing"] = lambda: self.d.read(self.tmp, [12, 99], 7)
        for name, which, overrides, login_answer in cases:
            with self.subTest(name):
                self.world(overrides, login_answer)
                with self.assertRaises(self.Problem):
                    reads[which]()


if __name__ == "__main__":
    unittest.main()
