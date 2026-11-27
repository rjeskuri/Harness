"""Tests for harness_lib/owner_comments.py (ticket #513, epic #512): the owner-comment rules `harness-board answers`
uses, shared, plus a historical reader of every needs-input round.

- by_harness(body): the marker anywhere, or a body starting (after whitespace) with a harness prefix
  (`**Needs your input**`, `Started on `, `PR #`, `Landed in `);
- owner_comments(comments, login): the owner's own comments (login casefolded), harness ones dropped, in order;
- question_times(comments, login): when each question was asked, the owner's `**Needs your input**` comments and the
  deploy guard's (github-actions[bot] or github-actions) that also carry the marker;
- replies_since(comments, login, since): owner_comments created after `since` (None: no bound);
- answer_rounds(comments, login): one {"question", "asked_at", "answers": [{"at", "body"}]} per question, oldest
  first, holding the owner's replies between it and the next question; `question` is the question text alone;
- fetch(cwd, numbers): (login, {n: comments}) through `gh api user` and the REST comments route; Problem on a failed
  read or an empty login.

Comments are in harness_lib.issues.Issues.comments' shape: {"author": {"login"} | None, "body", "createdAt", "url"}.
fetch runs against fakegh.py's REST world as HARNESS_GH, in a temporary HOME.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_owner_comments.py" -v
"""

from __future__ import annotations

import datetime as dt
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

MARK = "\n\n<!-- harness -->"
REPLY = "Reply in a comment; /harness:work 12 picks it up."
BOT = "github-actions[bot]"


def lib(name):
    """harness_lib.<name> from the checkout's lib/; the import error while owner_comments is missing is the red."""
    path = str(LIB)
    if path not in sys.path:
        sys.path.insert(0, path)
    importlib.invalidate_caches()
    return importlib.import_module(f"harness_lib.{name}")


def comment(login, at, body):
    """A comment as Issues.comments returns it; login None is a comment without an author."""
    return {"author": {"login": login} if login else None, "body": body, "createdAt": f"2026-10-13T{at}:00Z",
            "url": f"https://github.com/o/r/issues/12#issuecomment-{at.replace(':', '')}"}


def utc(hhmm):
    h, m = hhmm.split(":")
    return dt.datetime(2026, 10, 13, int(h), int(m), tzinfo=dt.timezone.utc)


def question_body(question, evidence=None, marked=True):
    """A body in harness-board's needs-input format (needs_input_body), written out by hand."""
    parts = ["**Needs your input**", question]
    if evidence is not None:
        parts.append(evidence)
    parts.append(REPLY)
    return "\n\n".join(parts) + (MARK if marked else "")


def round_(question, at, answers):
    return {"question": question, "asked_at": f"2026-10-13T{at}:00Z",
            "answers": [{"at": f"2026-10-13T{a}:00Z", "body": b} for a, b in answers]}


GUARD_Q = "The deploy of #12 failed its live check and was reverted. Retry, or change the approach?"


class Rules(unittest.TestCase):
    def setUp(self):
        self.oc = lib("owner_comments")

    def test_rounds_group_the_owners_replies_by_question(self):
        evidence = "```\nFAILED test_x\nAssertionError: 1 != 2\n```"
        history = [
            comment("rjeskuri", "09:00", question_body("Which base?", evidence)),
            comment("RJeskuri", "10:00", "Use develop."),
            comment("rjeskuri", "10:30", "And keep main protected."),
            comment(BOT, "11:00", question_body(GUARD_Q)),
            comment("rjeskuri", "12:00", "Retry."),
            comment("someone", "12:30", "drive-by"),
            comment("rjeskuri", "13:00", question_body("And the name?", marked=False)),   # from before the marker
            comment("rjeskuri", "13:30", "Started on t/12-x (mac.local, interactive)" + MARK),
            comment("rjeskuri", "14:00", "Call it export."),
            comment("rjeskuri", "15:00", question_body("Which flag?")),                   # not answered yet
        ]
        expected = [round_("Which base?", "09:00", [("10:00", "Use develop."), ("10:30", "And keep main protected.")]),
                    round_(GUARD_Q, "11:00", [("12:00", "Retry.")]),
                    round_("And the name?", "13:00", [("14:00", "Call it export.")]),
                    round_("Which flag?", "15:00", [])]
        cases = [
            ("three owner questions and a deploy guard's", history, "rjeskuri", expected),
            ("the login in another case", history, "RJESKURI", expected),
            ("the first round alone", history[:3], "rjeskuri", expected[:1]),
            ("the guard as GraphQL names it", [comment("rjeskuri", "09:00", question_body("Ship it?")),
                                               comment("github-actions", "10:00", question_body(GUARD_Q)),
                                               comment("rjeskuri", "11:00", "Yes, retry.")], "rjeskuri",
             [round_("Ship it?", "09:00", []), round_(GUARD_Q, "10:00", [("11:00", "Yes, retry.")])]),
            ("an unmarked bot body is no question", [comment("rjeskuri", "09:00", question_body("Ship it?")),
                                                     comment(BOT, "10:00", question_body(GUARD_Q, marked=False)),
                                                     comment("rjeskuri", "11:00", "Yes.")], "rjeskuri",
             [round_("Ship it?", "09:00", [("11:00", "Yes.")])]),
            ("another account's question is no question", [
                comment("rjeskuri", "09:00", question_body("Ship it?")),
                comment("someone", "10:00", question_body("Mine?")),
                comment("rjeskuri", "11:00", "Yes.")], "rjeskuri",
             [round_("Ship it?", "09:00", [("11:00", "Yes.")])]),
            ("no comments", [], "rjeskuri", []),
        ]
        for name, comments, login, want in cases:
            with self.subTest(name):
                self.assertEqual(self.oc.answer_rounds(comments, login), want)

    def test_replies_since_keeps_the_answers_rules(self):
        comments = [
            comment("rjeskuri", "08:00", "old answer"),
            comment("rjeskuri", "09:00", question_body("Which base?")),
            comment("RJeskuri", "10:00", "Use develop."),
            comment("someone", "10:15", "drive-by"),
            comment(BOT, "10:20", "a bot note"),
            comment(None, "10:25", "ghost"),
            comment("rjeskuri", "10:30", "Started on t/12-x (mac.local)"),
            comment("rjeskuri", "10:40", "PR #4 ready for review: https://x"),
            comment("rjeskuri", "10:50", "  Landed in abc1234"),
            comment("rjeskuri", "11:00", "Anything with the marker <!-- harness --> inside"),
            comment(BOT, "11:30", question_body(GUARD_Q)),
            comment(BOT, "11:40", question_body(GUARD_Q, marked=False)),
            comment("someone", "11:45", question_body("Mine?")),
            comment("rJeskuri", "12:00", "Retry."),
            comment("rjeskuri", "13:00", question_body("And the name?", marked=False)),
            comment("rjeskuri", "14:00", "Call it export."),
        ]
        owners = ["old answer", "Use develop.", "Retry.", "Call it export."]
        for login in ("rjeskuri", "RJESKURI", "RJeskuri"):
            with self.subTest(login=login):
                self.assertEqual([c["body"] for c in self.oc.owner_comments(comments, login)], owners)
                self.assertEqual(self.oc.question_times(comments, login), [utc("09:00"), utc("11:30"), utc("13:00")])
                for since, want in ((None, owners), (utc("07:00"), owners),
                                    (utc("08:00"), owners[1:]),                 # strictly after the bound
                                    (utc("10:00"), owners[2:]), (utc("12:30"), owners[3:]), (utc("14:00"), [])):
                    got = self.oc.replies_since(comments, login, since)
                    self.assertEqual([c["body"] for c in got], want, f"since {since}")
                    self.assertEqual([c["createdAt"] for c in got],
                                     [c["createdAt"] for c in comments if c["body"] in want], f"since {since}")
        self.assertEqual(self.oc.owner_comments(comments, "someone-else"), [])
        self.assertEqual(self.oc.question_times(comments, "someone-else"), [utc("11:30")])   # the guard's only

    def test_bot_harness_and_other_accounts_are_never_answers(self):
        harness = ["**Needs your input**\n\nWhich base?", "Started on t/12-x (mac.local)", "PR #4 ready: https://x",
                   "Landed in abc1234", "\n  Started on t/12-y", "Use main." + MARK, "a <!-- harness --> b"]
        mine = ["Use main.", "I started on it.", "See PR #4.", "It landed in main.", "**Note** use develop", ""]
        for body in harness:
            self.assertTrue(self.oc.by_harness(body), repr(body))
        for body in mine:
            self.assertFalse(self.oc.by_harness(body), repr(body))
        self.assertFalse(self.oc.by_harness(None))

        comments = [comment("rjeskuri", "09:00", question_body("Which base?"))]
        clock = 10
        for login, body in ([("rjeskuri", b) for b in harness[1:]] +
                            [(BOT, "Use main."), ("github-actions", "Use main."), ("someone", "Use main."),
                             ("rjeskuri-bot", "Use main."), (None, "Use main.")]):
            comments.append(comment(login, f"{clock:02d}:00", body))
            clock += 1
        comments.append(comment("RJESKURI", f"{clock:02d}:00", "the one answer"))
        want = [round_("Which base?", "09:00", [(f"{clock:02d}:00", "the one answer")])]
        self.assertEqual(self.oc.answer_rounds(comments, "rjeskuri"), want)
        self.assertEqual([c["body"] for c in self.oc.replies_since(comments, "rjeskuri", None)], ["the one answer"])

    def test_replies_before_the_first_question_are_in_no_round(self):
        early = [comment("rjeskuri", "08:00", "a note before any question"),
                 comment("RJeskuri", "08:30", "another early note")]
        asked = early + [comment("rjeskuri", "09:00", question_body("Which base?")),
                         comment("rjeskuri", "10:00", "Use develop.")]
        self.assertEqual(self.oc.answer_rounds(asked, "rjeskuri"),
                         [round_("Which base?", "09:00", [("10:00", "Use develop.")])])
        guarded = early + [comment(BOT, "09:00", question_body(GUARD_Q)), comment("rjeskuri", "10:00", "Retry.")]
        self.assertEqual(self.oc.answer_rounds(guarded, "rjeskuri"), [round_(GUARD_Q, "09:00", [("10:00", "Retry.")])])
        self.assertEqual(self.oc.answer_rounds(early, "rjeskuri"), [])
        # answers, unbounded, still sees them: only the rounds leave them out
        self.assertEqual([c["body"] for c in self.oc.replies_since(early, "rjeskuri", None)],
                         ["a note before any question", "another early note"])

    def test_question_is_the_question_text_alone(self):
        long_fence = "````\n[... 12 earlier lines omitted]\n```python\nx = 1\n```\nTraceback: boom\n````"
        cases = [
            ("plain", question_body("Which base?"), "Which base?"),
            ("evidence", question_body("Which base?", "```\nFAILED test_x\nAssertionError\n```"), "Which base?"),
            ("a longer fence around backticks", question_body("Retry the deploy?", long_fence), "Retry the deploy?"),
            ("two paragraphs", question_body("Which base?\n\nmain is two weeks stale.", "```\nlog\n```"),
             "Which base?\n\nmain is two weeks stale."),
            ("from before the marker", question_body("And the name?", marked=False), "And the name?"),
            ("the deploy guard's", question_body(GUARD_Q, "```\ncurl: 502\n```"), GUARD_Q),
        ]
        for name, body, want in cases:
            with self.subTest(name):
                rounds = self.oc.answer_rounds([comment("rjeskuri", "09:00", body)], "rjeskuri")
                self.assertEqual([r["question"] for r in rounds], [want])


class Fetch(unittest.TestCase):
    def setUp(self):
        self.oc = lib("owner_comments")
        self.Problem = lib("cli").Problem
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-owner-comments-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GH_"))}
        env.update(HOME=self.home, HARNESS_GH=str(FAKEGH), FAKEGH_SCENARIO=self.scenario_path,
                   FAKEGH_LOG=os.path.join(self.tmp, "gh.log"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def world(self, login_answer, issues, calls=()):
        state = self.scenario_path + ".state.json"      # fakegh's consumed responses, from an earlier world
        if os.path.exists(state):
            os.remove(state)
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump({"calls": list(calls) + [dict({"match": ["api", "user"]}, **login_answer)],
                       "issues": issues}, fh)

    def test_fetch_reads_the_login_and_each_issues_comments(self):
        twelve = [comment("rjeskuri", "09:00", question_body("Which base?")),
                  comment("RJeskuri", "10:00", "Use develop.")]
        thirty_four = [comment("someone", "11:00", "drive-by"), comment("rjeskuri", "12:00", "Ship it.")]
        self.world({"stdout": "RJeskuri\n"},
                   {"12": {"number": 12, "comments": twelve}, "34": {"number": 34, "comments": thirty_four},
                    "56": {"number": 56, "comments": [comment("rjeskuri", "13:00", "not asked for")]}})
        login, by_issue = self.oc.fetch(self.tmp, [12, 34])
        self.assertEqual(login.casefold(), "rjeskuri")
        self.assertEqual(sorted(by_issue), [12, 34])
        for n, given in ((12, twelve), (34, thirty_four)):
            got = by_issue[n]
            self.assertEqual([((c.get("author") or {}).get("login"), c["body"], c["createdAt"]) for c in got],
                             [(c["author"]["login"], c["body"], c["createdAt"]) for c in given], n)
        # what fetch returns is what the rules read
        self.assertEqual(self.oc.answer_rounds(by_issue[12], login),
                         [round_("Which base?", "09:00", [("10:00", "Use develop.")])])

        self.world({"stdout": "rjeskuri\n"}, {"7": {"number": 7, "comments": []}})
        self.assertEqual(self.oc.fetch(self.tmp, [7]), ("rjeskuri", {7: []}))

    def test_a_failed_read_or_an_empty_login_raises(self):
        issues = {"12": {"number": 12, "comments": [comment("rjeskuri", "10:00", "Use develop.")]}}
        cases = [
            ("an empty login", {"stdout": "\n"}, issues, (), [12]),
            ("gh api user fails", {"stderr": "HTTP 401: Bad credentials\n", "exit": 1}, issues, (), [12]),
            ("an issue gh cannot find", {"stdout": "rjeskuri\n"}, issues, (), [12, 99]),
            ("a comments read that fails", {"stdout": "rjeskuri\n"}, issues,
             [{"rest": "issues/12/comments", "responses": [{"stderr": "HTTP 502: Bad Gateway\n", "exit": 1}]}], [12]),
        ]
        for name, login_answer, world, calls, numbers in cases:
            with self.subTest(name):
                self.world(login_answer, world, calls)
                with self.assertRaises(self.Problem):
                    self.oc.fetch(self.tmp, numbers)


if __name__ == "__main__":
    unittest.main()
