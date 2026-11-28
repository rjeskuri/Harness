"""Tests for `harness-pr findings --gpt-check` scoping a fix re-check to what the fix changed (ticket #519, epic #512).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_scope.py -v

A fix re-check (review_base is the previous paired head, an ancestor of the head, the fork covered) records every
new high or medium finding, Sol's and Claude's (--review), as low unless its (path, line) is a head-side `+` line of
`git diff --unified=0 <review_base> <head>`: a context line, an untouched line of a touched file, a line that only
moved down, or no line is off the fix. A prior blocker Sol reports unresolved stays blocking wherever it sits, once,
repeats being matched by [path, body] (reviews.finding_key). First-round, rewritten-history and base-moved reviews
(review_base is the fork) demote nothing. The gpt receipt and the gpt-check round record `mode`; the receipt also
`base` and an integer `policy`: a receipt with no or an older policy is checked again, a current one keeps today's
interrupted-publication recovery (republish once, no harness-gpt run).

Fixture: pr_findings_support's PostTest (src/app.py's 30 lines `V<i> = <i>`, the branch rewriting lines 3 and 25 and
adding the 4-line tests/test_app.py; PR #34 open at the head), plus a fake harness-gpt in the module's fakebin that
logs its argv and brief and prints {"head", "model": "gpt-6.2-sol", "findings": <conf findings>, "prior_fixed"}, each
prior blocker fixed unless its body is in the conf's `unfixed`. GET pulls/34/reviews answers the reviews the test
names (none by default). Each test's first harness-pr run is a process of its own, later runs run in this process
(gitrepo_support.run_in_process). Expected values are written out by hand from the fix commits each test makes.

Choices the ticket left open, fixed here (each a `Ruling:` for the PR):
- a demoted finding is the normalised finding with severity `low`, its path, line and body as they were;
- of a finding and its repeats (same path and body) exactly one entry is recorded; which line it keeps is not pinned;
- the policy integer is not pinned: a fresh receipt's is an int of at least 1 and the same on every round.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

sys.dont_write_bytecode = True

import fakegh  # noqa: E402
import pr_findings_support as support  # noqa: E402
from pr_findings_support import (BINS, BRANCH_CHANGES, OWNER, TEST_LINES, TOOL, PostTest, app_text,  # noqa: E402
                                 executable, norm, tearDownModule, write)  # noqa: F401  (tearDownModule: unittest's)
from gitrepo_support import run_in_process  # noqa: E402

APP, TESTS, NEW = "src/app.py", "tests/test_app.py", "src/new.py"

GPT = r'''import json, os, sys
argv = sys.argv[1:]
text = sys.stdin.read()
with open(os.environ["GPT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": text}) + "\n")
try:
    with open(os.environ["GPT_CONF"], encoding="utf-8") as fh:
        conf = json.load(fh)
except OSError:
    conf = {}
prior = json.loads(text).get("prior_blockers") or []
unfixed = conf.get("unfixed", [])
print(json.dumps({"head": argv[argv.index("--head") + 1], "model": "gpt-6.2-sol", "findings": conf.get("findings", []),
                  "prior_fixed": [p.get("body") not in unfixed for p in prior]}))
'''


def setUpModule():
    support.setUpModule()
    executable(os.path.join(BINS["fakebin"], "harness-gpt"), f"#!{sys.executable}\n" + GPT)


# --------------------------------------------------------------- findings and expectations, derived here
def find(path, line, severity, body):
    """A finding as Sol prints it (and as a Claude review file holds it); line None: no line."""
    return {"path": path, "line": line, "severity": severity, "body": body}


def low(f):
    """Finding f recorded as low: only its severity changes."""
    return dict(norm(f), severity="low")


def key(f):
    return (str(f.get("path") or ""), str(f.get("body") or ""))


def bag(items):
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def app_lines(changes=(), drop=()):
    """src/app.py on the branch with `changes` rewritten too and the lines in `drop` cut."""
    lines = app_text(BRANCH_CHANGES + tuple(changes)).splitlines(True)
    return "".join(line for i, line in enumerate(lines, 1) if i not in drop)


def test_lines(drop=()):
    return "".join(f"# test line {i}\n" for i in range(1, TEST_LINES + 1) if i not in drop)


HEADER = "# header\n" + app_lines()          # one line inserted above line 1: every old line moves down one

P1 = find(APP, 3, "high", "V3 is scaled twice.")
P2 = find(TESTS, 2, "medium", "The test asserts nothing.")
P3 = find(APP, 20, "medium", "V20 is read before it is set.")


class ScopeTest(PostTest):
    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    def setUp(self):
        super().setUp()
        self.start = self.head()     # the branch's own commit, where every row starts

    @property
    def gpt_log(self):
        return os.path.join(self.tmp, "gpt.log")

    def env(self):
        env = super().env()
        env.update(GPT_LOG=self.gpt_log, GPT_CONF=os.path.join(self.tmp, "gpt.json"))
        return env

    def pr(self, *args, cwd=None):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().pr(*args, cwd=cwd)
        return run_in_process(TOOL, args=args, cwd=cwd or self.main, env=self.env())

    # ----------------------------------------------------------- the world
    def serve(self, reviews=(), fail_post=False):
        """PR #34 open at the worktree's HEAD; GET pulls/34/reviews answers `reviews`; with fail_post every review
        POST fails with a 502."""
        calls = []
        if fail_post:
            calls.append({"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"],
                          "stderr": "gh: HTTP 502: Bad Gateway\n", "exit": 1})
        calls.append(fakegh.rest_entry("pulls/34/reviews", "GET", exact=True, stdout_json=list(reviews)))
        self.open_pr(34, calls=calls)

    def sol(self, findings=(), unfixed=()):
        write(os.path.join(self.tmp, "gpt.json"),
              json.dumps({"findings": list(findings), "unfixed": [f["body"] for f in unfixed]}))

    def reset(self):
        """Back to the branch's own commit with no rounds, receipts or rulings: the next check is a first review."""
        self.git(self.wt, "reset", "-q", "--hard", self.start)
        shutil.rmtree(self.reviews_dir(), True)

    def fix(self, files, amend=False):
        """A commit on the branch writing `files` ({path: text}) (with amend: the last commit amended), the PR moved
        to it; the new head."""
        for name, text in files.items():
            write(os.path.join(self.wt, name), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", *(["--amend"] if amend else []), "-m", "Fix")
        self.serve()
        return self.head()

    def posted_review(self):
        """GitHub's harness review at HEAD, as the last review POST sent it."""
        body = json.loads(self.posts()[-1]["stdin"])["body"]
        return {"id": 7001, "user": {"login": OWNER}, "body": body, "commit_id": self.head(),
                "submitted_at": "2026-11-28T10:00:00Z", "state": "COMMENTED"}

    # ----------------------------------------------------------- runs
    def run_check(self, review=None):
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json", "--head", self.head()]
        if review is not None:
            args += ["--review", self.review_file(list(review))]
        return self.pr(*args)

    def check(self, review=None, sol=None, unfixed=()):
        """`harness-pr findings 12 --gpt-check --head HEAD --json` (--review F when given), Sol answering `sol`."""
        if sol is not None:
            self.sol(sol, unfixed)
        p = self.run_check(review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout)

    def first(self, review=(), sol=()):
        """A first paired review at HEAD."""
        self.serve()
        return self.check(review, sol)

    def gpt_runs(self):
        return self.jsonl(self.gpt_log)

    def receipt(self, head=None):
        path = os.path.join(self.reviews_dir(), f"gpt-34-{head or self.head()}.json")
        self.assertTrue(os.path.exists(path), f"no receipt at {path}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_receipt(self, receipt, head=None):
        write(os.path.join(self.reviews_dir(), f"gpt-34-{head or self.head()}.json"), json.dumps(receipt))

    def policy_of(self, receipt):
        self.assertIn("policy", receipt, "the receipt records no policy")
        policy = receipt["policy"]
        self.assertTrue(isinstance(policy, int) and not isinstance(policy, bool) and policy >= 1, policy)
        return policy

    def assertSplits(self, out, blocking=(), low_=()):
        """The latest round and --json hold exactly these blocking and low findings."""
        rnd = self.load_rounds()[-1]
        for name, want in (("blocking", [norm(f) for f in blocking]), ("low", list(low_))):
            self.assertEqual(bag(rnd.get(name, [])), bag(want), f"round {name}")
            self.assertEqual(bag(out.get(name, [])), bag(want), f"--json {name}")
        return rnd

    def recorded(self, out):
        """Every finding the round and --json recorded, blocking, low and outside."""
        rnd = self.load_rounds()[-1]
        found = [f for name in ("blocking", "low", "outside") for f in rnd.get(name, [])]
        self.assertEqual(bag(found), bag([f for name in ("blocking", "low", "outside") for f in out.get(name, [])]))
        return found

    def assertOnce(self, out, f, severity=None):
        """Exactly one recorded entry carries f's path and body, a blocking one, at `severity` (f's by default)."""
        hits = [g for g in self.recorded(out) if key(g) == key(f)]
        self.assertEqual(len(hits), 1, f"entries for {key(f)}: {hits}")
        self.assertIn(hits[0], out["blocking"])
        self.assertEqual(hits[0]["severity"], severity or f["severity"])


# =========================================================================== the fix lines
class FixLines(ScopeTest):
    def test_a_new_blocker_on_an_added_line_stays_blocking(self):
        rows = [  # name, the fix's files, Claude's findings, Sol's, those that stay blocking
            ("a rewritten line", {APP: app_lines((10,))},
             [], [find(APP, 10, "high", "V10 now divides by zero."),
                  find(APP, 11, "medium", "V11, a context line, reads stale state.")],
             [find(APP, 10, "high", "V10 now divides by zero.")]),
            ("an inserted line", {APP: HEADER},
             [find(APP, 1, "medium", "The header hides the docstring."),
              find(APP, 2, "high", "V1 only moved down a line.")], [],
             [find(APP, 1, "medium", "The header hides the docstring.")]),
            ("two hunks and a new file", {APP: app_lines((10, 20)), NEW: "A = 1\nB = 2\n"},
             [find(APP, 10, "medium", "V10 lost its guard."), find(NEW, None, "medium", "The module has no tests.")],
             [find(APP, 20, "high", "V20 overflows."), find(NEW, 2, "high", "B shadows the builtin."),
              find(APP, 21, "high", "V21 is untouched context.")],
             [find(APP, 10, "medium", "V10 lost its guard."), find(APP, 20, "high", "V20 overflows."),
              find(NEW, 2, "high", "B shadows the builtin.")]),
        ]
        for name, files, claude, sol, kept in rows:
            with self.subTest(name):
                self.reset()
                self.first()
                self.fix(files)
                out = self.check(claude, sol)
                rest = [low(f) for f in claude + sol if f not in kept]
                self.assertTrue(rest, "each row needs a finding off the fix lines")
                self.assertSplits(out, blocking=kept, low_=rest)
                self.assertEqual(out["record"], {"stage": "fix", "result": "fail"})

    def test_new_blockers_off_the_fix_lines_become_low(self):
        rows = [  # name, the fix's files, Claude's findings, Sol's, the one that stays blocking
            ("line 10 rewritten", {APP: app_lines((10,))},
             [find(TESTS, 2, "medium", "The test never runs the app."),
              find(APP, 13, "high", "V13, the last context line, is stale."),
              find(APP, 10, "low", "Name V10 after what it counts.")],
             [find(APP, 8, "high", "V8, a context line, is stale."),
              find(APP, 12, "medium", "V12 is read twice."),
              find(APP, 20, "high", "V20, untouched, overflows."),
              find(APP, 3, "medium", "V3, changed by the branch but not the fix, is scaled twice."),
              find(APP, None, "high", "The module never closes its file."),
              find(APP, 10, "high", "V10 now divides by zero.")],
             find(APP, 10, "high", "V10 now divides by zero.")),
            ("a header inserted above every line", {APP: HEADER},
             [find(TESTS, 1, "high", "The first test line is a stub."),
              find(APP, 1, "medium", "The header hides the docstring.")],
             [find(APP, 21, "high", "V20, moved to line 21, overflows."),
              find(APP, 4, "medium", "V3, moved to line 4, is scaled twice.")],
             find(APP, 1, "medium", "The header hides the docstring.")),
        ]
        for name, files, claude, sol, kept in rows:
            with self.subTest(name):
                self.reset()
                self.first()
                self.fix(files)
                out = self.check(claude, sol)
                self.assertSplits(out, blocking=[kept], low_=[low(f) for f in claude + sol if f != kept])
                self.assertEqual(self.load_rounds()[-1].get("mode"), "fix")

    def test_a_deletion_only_fix_demotes_every_new_blocker(self):
        claude = [find(TESTS, 2, "high", "The test never runs the app."), find(APP, 9, "medium", "V9 is stale.")]
        sol = [find(APP, 10, "high", "V12, now at line 10, divides by zero."),
               find(APP, 4, "medium", "V4 is never read."), find(APP, None, "high", "The module leaks a handle.")]
        rows = [  # name, the fix's files
            ("two lines cut from the app", {APP: app_lines(drop=(10, 11))}),
            ("a line cut from each file", {APP: app_lines(drop=(20,)), TESTS: test_lines(drop=(4,))}),
        ]
        for name, files in rows:
            with self.subTest(name):
                self.reset()
                self.first(review=[P1])
                self.fix(files)
                out = self.check(claude, sol, unfixed=[P1])
                self.assertSplits(out, blocking=[P1], low_=[low(f) for f in claude + sol])
                self.assertEqual(out["record"], {"stage": "fix", "result": "fail"})


# =========================================================================== prior blockers and repeats
class Prior(ScopeTest):
    def test_an_unresolved_prior_blocker_stays_blocking_anywhere(self):
        self.first(review=[P1, P2], sol=[P3])
        self.fix({APP: app_lines((10,))})
        new = find(APP, 15, "high", "V15 is off the fix.")
        out = self.check(None, [dict(P1, line=5), new], unfixed=[P1, P3])
        self.assertOnce(out, P1)
        self.assertOnce(out, P3)
        self.assertEqual(sorted(key(f) for f in out["blocking"]), sorted([key(P1), key(P3)]))
        self.assertEqual([f for f in self.recorded(out) if key(f) == key(P2)], [])
        self.assertEqual(out["low"], [low(new)])

        # The next fix round: Claude raises P3 again on another line, Sol still holds both unresolved.
        self.fix({APP: app_lines((10, 12))})
        out = self.check([dict(P3, line=21)], [], unfixed=[P1, P3])
        self.assertOnce(out, P1)
        self.assertOnce(out, P3)
        self.assertEqual(len(out["blocking"]), 2, out["blocking"])
        self.assertEqual(out["low"], [])

    def test_repeats_are_matched_by_path_and_body(self):
        x = find(APP, 3, "high", "V3 is multiplied by ten twice.")
        y = find(APP, 20, "high", "V20 overflows on large input.")
        rows = [  # name, a fix round, round 1's Claude findings, then (Claude's, Sol's, unresolved), the repeated one
            ("Sol repeats Claude's finding on another line", False, [x], [dict(x, line=4, severity="medium")], x),
            ("Sol reports one finding twice", False, [], [y, dict(y, line=22)], y),
            ("Claude repeats an unresolved prior blocker on another line", True, [P1], [dict(P1, line=4)], P1),
        ]
        for name, fixing, claude, again, f in rows:
            with self.subTest(name):
                self.reset()
                if not fixing:
                    out = self.first(review=claude, sol=again)
                else:
                    self.first(review=claude)
                    self.fix({APP: app_lines((10,))})
                    out = self.check(again, [], unfixed=[f])
                self.assertOnce(out, f)
                self.assertEqual(len(out["blocking"]), 1, out["blocking"])


# =========================================================================== full reviews
class Full(ScopeTest):
    def test_full_reviews_demote_nothing(self):
        claude = [find(APP, 12, "high", "V12, untouched, is read twice."), find(APP, 8, "medium", "V8 is stale.")]
        sol = [find(APP, 20, "medium", "V20, untouched, overflows."),
               find(APP, None, "high", "The module never closes its file.")]

        def first_review():
            self.serve()

        def rewritten():
            self.first()
            self.fix({APP: app_lines((10,))}, amend=True)

        def base_moved():
            self.first()
            self.git(self.wt, "merge", "-q", "--no-edit", "origin/main")
            self.fix({APP: app_lines((10,))})

        for name, setup in (("a first review", first_review), ("rewritten history", rewritten),
                            ("the base moved", base_moved)):
            with self.subTest(name):
                self.reset()
                setup()
                fork = self.git(self.wt, "merge-base", "HEAD", "origin/main")
                runs = len(self.gpt_runs())
                out = self.check(claude, sol)
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                argv = self.gpt_runs()[-1]["argv"]
                self.assertEqual(argv[argv.index("--base") + 1], fork)
                self.assertSplits(out, blocking=claude + sol)
                self.assertEqual(self.load_rounds()[-1].get("mode"), "full")


# =========================================================================== the receipt
class Receipt(ScopeTest):
    def test_the_receipt_records_mode_base_and_policy(self):
        h0 = self.head()
        self.first()
        r0 = self.receipt(h0)
        policy = self.policy_of(r0)
        self.assertEqual((r0.get("mode"), r0.get("base")), ("full", self.merge_base))
        self.assertEqual(self.load_rounds()[-1].get("mode"), "full")

        h1 = self.fix({APP: app_lines((10,))})
        self.check(None, [])
        r1 = self.receipt(h1)
        self.assertEqual((r1.get("mode"), r1.get("base"), self.policy_of(r1)), ("fix", h0, policy))
        self.assertEqual(self.load_rounds()[-1].get("mode"), "fix")

        h2 = self.fix({APP: app_lines((10, 14))})
        self.check([], [])
        r2 = self.receipt(h2)
        self.assertEqual((r2.get("mode"), r2.get("base"), self.policy_of(r2)), ("fix", h1, policy))

        h3 = self.fix({APP: app_lines((10, 14, 18))}, amend=True)
        self.check([], [])
        r3 = self.receipt(h3)
        self.assertEqual((r3.get("mode"), r3.get("base"), self.policy_of(r3)), ("full", self.merge_base, policy))
        self.assertEqual(self.load_rounds()[-1].get("mode"), "full")

    def test_a_receipt_from_an_older_policy_is_checked_again(self):
        s1 = find(APP, 20, "medium", "V20 overflows.")
        s2 = find(APP, 12, "high", "V12 is read twice.")
        for name, interrupted, stale in (("no policy, its publication interrupted", True, None),
                                         ("an older policy, its review posted", False, -1)):
            with self.subTest(name):
                self.reset()
                head = self.head()
                self.serve(fail_post=interrupted)
                self.sol([s1])
                p = self.run_check([P1])
                self.assertEqual(p.returncode != 0, interrupted, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                receipt = self.receipt(head)
                policy = self.policy_of(receipt)
                if stale is None:
                    receipt.pop("policy")
                else:
                    receipt["policy"] = policy + stale
                self.save_receipt(receipt, head)
                self.serve(reviews=[] if interrupted else [self.posted_review()])
                runs = len(self.gpt_runs())
                out = self.check([P1], [s2])
                self.assertEqual(len(self.gpt_runs()), runs + 1, "harness-gpt did not check the head again")
                argv = self.gpt_runs()[-1]["argv"]
                self.assertEqual(argv[argv.index("--head") + 1], head)
                self.assertEqual(out["head"], head)
                self.assertSplits(out, blocking=[P1, s2])
                self.assertEqual(self.policy_of(self.receipt(head)), policy)

    def test_an_interrupted_publication_republishes_without_sol(self):
        n10, n20 = find(APP, 10, "high", "V10 now divides by zero."), find(APP, 20, "high", "V20 overflows.")
        s1 = find(APP, 20, "medium", "V20 is read before it is set.")
        rows = [  # name, a fix round, Claude's findings, Sol's, blocking, low, mode
            ("a first review", False, [P1], [s1], [P1, s1], [], "full"),
            ("a fix re-check", True, None, [n10, n20], [n10], [low(n20)], "fix"),
        ]
        for name, fixing, claude, sol, blocking, low_, mode in rows:
            with self.subTest(name):
                self.reset()
                if fixing:
                    self.first()
                    self.fix({APP: app_lines((10,))})
                head = self.head()
                self.serve(fail_post=True)
                self.sol(sol)
                runs = len(self.gpt_runs())
                p = self.run_check(claude)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                receipt = self.receipt(head)
                self.policy_of(receipt)
                self.assertEqual(receipt.get("mode"), mode)

                self.serve(reviews=[])                           # GitHub holds no review of this head
                self.sol([find(APP, 12, "high", "Sol was asked again.")])
                posts = len(self.posts())
                out = self.check(claude)
                self.assertEqual(len(self.gpt_runs()), runs + 1, "harness-gpt ran again")
                self.assertEqual(len(self.posts()), posts + 1)
                self.assertEqual(out["head"], head)
                self.assertSplits(out, blocking=blocking, low_=low_)
                self.assertEqual(self.load_rounds()[-1].get("mode"), mode)

                self.serve(reviews=[self.posted_review()])       # now it holds the republished one
                out = self.check(claude)
                self.assertEqual(len(self.gpt_runs()), runs + 1, "harness-gpt ran again")
                self.assertEqual(len(self.posts()), posts + 1, "the review was published twice")
                self.assertSplits(out, blocking=blocking, low_=low_)


if __name__ == "__main__":
    unittest.main()
