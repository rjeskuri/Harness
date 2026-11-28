"""Tests for `harness-pr findings N --refresh` (ticket #518, epic #512): the owner's dismissals applied to the latest
review round again, on the same head, without Sol, without a new round and without a fix attempt.

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_refresh.py -v

--refresh needs a latest round whose head is the worktree's HEAD, a clean tree and a PR (the round's, else the
branch's open PR). It reads the owner's `dismiss` lines as findings does (#517), resolves them against the local
rounds, rebuilds the latest round's blocking, low and outside from its findings plus the ones it had dismissed (each
back in its `from` list), and saves the round in place: the same number, head, base, pr, review_id, reviewers and
fork. It rewrites round-k-blocking.json and notes.md, posts nothing, runs no harness-gpt and no harness-work, and
prints findings' output with `record` null and `refreshed` true. It is refused beside any other mode flag, and
refused with nothing written without a round, on a moved head, on a dirty tree, without a PR, or when the
dismissals cannot be read. `--latest` stays read-only.

Fixture: test_pr_findings_dismiss's DismissTest (pr_findings_support's PostTest: the repositories, fakegh with
ticket 12's and PR #34's comments in its REST world, the harness-board stub, the fake harness-gpt) plus a
harness-work stub in the module's fakebin that only logs its argv (harness-pr finds its sibling tools on PATH
first). The real harness-work runs, by its path, only where the test calls `harness-work stage`. The ticket cache is
written as harness-board show writes it (test_work_stage's fixture). Each test's first harness-pr run is a process
of its own; later runs run in this process (gitrepo_support.run_in_process).

The timeline (as test_pr_findings_dismiss): round 1 is set back to 09:00 and round 2 to 10:00 on 2026-11-20 once
recorded; the owner's comments are 11:00 and later that day, or `later()` than the real now.

Choices the ticket left open, fixed here (each a `Ruling:` for the PR):
- a second refresh still honors the dismissals the first one applied (the owner dismisses one finding, refreshes,
  later dismisses another and refreshes again): the refreshed round stays resolvable for comments made after the
  round was first recorded;
- with no dismissal left the round's `dismissed` is empty or absent, either way;
- the refusals are pinned by their exit code, by nothing written under ~/.claude and nothing posted, and by the
  same refresh succeeding once the cause is gone; only the no round refusal's words (`no review round recorded for
  #12`, the ticket's) are pinned; a round recorded without a PR refreshes once the branch's PR is open;
- round-k-review.json (what was posted) is not pinned: nothing new is posted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True

import test_pr_findings_dismiss as d  # noqa: E402  DismissTest, the comments and expectations
from pr_findings_support import BINS, OWNER, executable, norm, sections, write  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(os.path.dirname(HERE), "bin", "harness-work")
RID = 7300112
F1, F2, L26, O1, O2, S26 = d.F1, d.F2, d.L26, d.O1, d.O2, d.S26
WHY1 = d.WHY1
WHY_L = "naming is the owner's call"
WHY_O = "lib/x.py is not this ticket's"
WHY_2 = "the assertion lives in the fixture"
note, dismiss, gone, noted, bag, later = d.note, d.dismiss, d.gone, d.noted, d.bag, d.later

HWORK = r'''import json, os, sys
with open(os.environ["HWORK_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
'''


def setUpModule():
    d.setUpModule()
    executable(os.path.join(BINS["fakebin"], "harness-work"), f"#!{sys.executable}\n" + HWORK)


def tearDownModule():
    d.tearDownModule()


class RefreshTest(d.DismissTest):
    def env(self):
        env = super().env()
        env.update(HWORK_LOG=os.path.join(self.tmp, "hwork.log"))
        return env

    # ----------------------------------------------------------- the world
    def two_rounds(self, review):
        """Round 1 at H1 and round 2 at H2 (the worktree's HEAD), each recorded from `review` with PR #34 open
        (posting review RID), set back before the owner's comments; H1."""
        self.serve(review_id=RID)
        self.record(review)
        self.stamp("09:00")
        h1 = self.head()
        self.advance()
        self.record(review)
        self.stamp("10:00")
        return h1

    def put_cache(self):
        """Ticket 12's cache as harness-board show writes it; its path."""
        path = os.path.join(self.home, ".claude", "harness", "repos", self.key(), "tickets", "12.json")
        write(path, json.dumps({"number": 12, "title": "The ticket", "type": "task", "size": "M", "risk": "low",
                                "flags": [], "labels": ["task", "status:in-progress", "size:M", "risk:low"],
                                "tier": "opus", "attempts": []}))
        return path

    # ----------------------------------------------------------- runs
    def refresh(self, *extra, as_json=True):
        args = ["findings", "12", "--worktree", self.wt, "--refresh"] + (["--json"] if as_json else []) + list(extra)
        return self.pr(*args)

    def refreshed(self):
        p = self.refresh()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout), p

    def latest(self):
        p = self.pr("findings", "12", "--worktree", self.wt, "--latest", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def stage(self, *argv):
        """The real `harness-work stage 12 ...` from the main checkout."""
        return subprocess.run([sys.executable, WORK, "stage", "12"] + list(argv), cwd=self.main, env=self.env(),
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def blocking_file(self, k):
        with open(os.path.join(self.reviews_dir(), f"round-{k}-blocking.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def hwork_runs(self):
        return self.jsonl(os.path.join(self.tmp, "hwork.log"))

    def counts(self):
        """(harness-gpt runs, review POSTs, `gh pr review` calls, out-of-scope ticket creates, harness-work runs)."""
        return (len(self.gpt_runs()), len(self.posts()), len(self.comment_reviews()), len(self.creates()),
                len(self.hwork_runs()))

    # ----------------------------------------------------------- assertions
    def assertRefreshed(self, out, k, blocking=(), low=(), outside=(), dismissed=()):
        """The latest round, --json and round-k-blocking.json hold exactly these; the round is number k."""
        rnd = self.load_rounds()[-1]
        self.assertEqual(rnd.get("round"), k)
        self.assertEqual(out.get("round"), k)
        for name, want in (("blocking", blocking), ("low", low), ("outside", outside)):
            self.assertEqual(bag(rnd.get(name, [])), bag(map(norm, want)), f"round {name}")
            self.assertEqual(bag(out.get(name, [])), bag(map(norm, want)), f"--json {name}")
        self.assertEqual(bag(rnd.get("dismissed") or []), bag(dismissed), "round dismissed")
        self.assertEqual(bag(out.get("dismissed") or []), bag(dismissed), "--json dismissed")
        self.assertEqual(bag(self.blocking_file(k)), bag(map(norm, blocking)))
        self.assertIsNone(out.get("record", "missing"))
        self.assertIs(out.get("refreshed"), True)
        return rnd


# =========================================================================== applied in place
class Applied(RefreshTest):
    def test_refresh_applies_a_new_dismissal_in_place(self):
        review = [F1, F2, L26, O1, O2]
        h1 = self.two_rounds(review)
        h2 = self.head()
        before = self.load_rounds()
        self.assertEqual(len(before), 2)
        by = note(OWNER, "11:00", "Read the review.\n" + "\n".join([dismiss(F1, WHY1), dismiss(L26, WHY_L),
                                                                     dismiss(O2, WHY_O)]))
        self.serve(ticket=[by], review_id=RID)
        out, _p = self.refreshed()
        dismissed = [gone(F1, "blocking", WHY1, by), gone(L26, "low", WHY_L, by), gone(O2, "outside", WHY_O, by)]
        rnd = self.assertRefreshed(out, 2, blocking=[F2], outside=[O1], dismissed=dismissed)
        rounds = self.load_rounds()
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0], before[0])
        for name in ("head", "base", "pr", "review_id"):
            self.assertEqual(rnd.get(name), before[1].get(name), name)
        self.assertEqual((rnd["head"], rnd["base"], rnd["pr"], rnd["review_id"]), (h2, h1, 34, RID))
        self.assertEqual((out["head"], out["base"], out["pr"], out["review_id"]), (h2, h1, 34, RID))
        self.assertEqual(os.path.realpath(out["files"]["blocking"]),
                         os.path.realpath(os.path.join(self.reviews_dir(), "round-2-blocking.json")))
        self.assertEqual(self.stage_blocking(), [norm(F2)])
        self.assertEqual(sorted(sections(self.notes()).get("Dismissed", [])),
                         sorted([noted(F1, WHY1), noted(L26, WHY_L), noted(O2, WHY_O)]), self.notes())

        # Refreshed again with a second, later comment, as text: both comments hold, still round 2 of two.
        more = note(OWNER, later(), dismiss(F2, WHY_2), 34)
        self.serve(ticket=[by], pr=[more], review_id=RID)
        p = self.refresh(as_json=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines()[0], "#12 review round 2 refreshed: 0 blocking, 4 dismissed; "
                                                   "no stage to record")
        self.assertEqual(len(self.load_rounds()), 2)
        rnd = self.load_rounds()[-1]
        self.assertEqual(rnd["round"], 2)
        self.assertEqual(bag(rnd.get("blocking", [])), [])
        self.assertEqual(bag(rnd.get("outside", [])), bag([norm(O1)]))
        self.assertEqual(bag(rnd.get("dismissed") or []), bag(dismissed + [gone(F2, "blocking", WHY_2, more)]))
        self.assertEqual(self.blocking_file(2), [])
        self.assertIn(noted(F2, WHY_2), sections(self.notes()).get("Dismissed", []))

    def test_refresh_calls_no_sol_posts_nothing_and_records_no_attempt(self):
        # A paired round (Claude + Sol) with its review posted: the refresh keeps its reviewers and fork.
        self.sol([S26])
        self.serve(review_id=RID)
        self.check([F1, F2, O1])
        self.stamp()
        before = self.load_rounds()[-1]
        self.assertEqual(before.get("reviewers"), ["opus", "gpt-6.2-sol"])
        cache = self.put_cache()
        with open(cache, "rb") as fh:
            cached = fh.read()
        by = note(OWNER, "11:00", dismiss(F1, WHY1) + "\n" + dismiss(S26, "the caller checks the cap"), 34)
        self.serve(pr=[by], review_id=RID)
        counts, calls = self.counts(), len(self.gh_calls())
        out, _p = self.refreshed()
        rnd = self.assertRefreshed(out, 1, blocking=[F2], outside=[O1],
                                   dismissed=[gone(F1, "blocking", WHY1, by),
                                              gone(S26, "blocking", "the caller checks the cap", by)])
        for name in ("head", "base", "pr", "review_id", "reviewers", "fork"):
            self.assertEqual(rnd.get(name), before.get(name), name)
        self.assertEqual(len(self.load_rounds()), 1)
        self.assertEqual(self.counts(), counts, "harness-gpt, a POST, gh pr review, a ticket or harness-work ran")
        mutating = [c["argv"] for c in self.gh_calls()[calls:]
                    if c["argv"][:2] in (["pr", "review"], ["pr", "comment"], ["pr", "edit"], ["issue", "comment"])
                    or any(c["argv"][i] in ("-X", "--method") and c["argv"][i + 1].upper() != "GET"
                           for i in range(len(c["argv"]) - 1))]
        self.assertEqual(mutating, [])
        with open(cache, "rb") as fh:
            self.assertEqual(fh.read(), cached, "the ticket cache changed")


# =========================================================================== the stage reads the refreshed round
class Stage(RefreshTest):
    def test_stage_fix_pass_follows_a_refresh_that_cleared_every_blocker(self):
        self.two_rounds([F1, F2])
        self.put_cache()
        first = note(OWNER, "11:00", dismiss(F1, WHY1))
        self.serve(ticket=[first], review_id=RID)
        self.refreshed()
        p = self.stage("fix", "pass")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)       # F2 still blocks

        second = note(OWNER, later(), dismiss(F2, WHY_2), 34)
        self.serve(ticket=[first], pr=[second], review_id=RID)
        out, _p = self.refreshed()
        self.assertRefreshed(out, 2, dismissed=[gone(F1, "blocking", WHY1, first),
                                                gone(F2, "blocking", WHY_2, second)])
        p = self.stage("fix", "pass", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual((data["action"], data["next"]), ("done", "ci"))


# =========================================================================== withdrawn
class Withdrawn(RefreshTest):
    def test_refresh_restores_a_withdrawn_dismissal(self):
        self.two_rounds([F1, F2, L26, O2])
        c1 = note(OWNER, "11:00", dismiss(F1, WHY1))
        c2 = note(OWNER, "11:30", dismiss(L26, WHY_L) + "\n" + dismiss(O2, WHY_O), 34)
        steps = [  # name, ticket comments, PR comments, blocking, low, outside, dismissed
            ("both comments", [c1], [c2], [F2], [], [],
             [gone(F1, "blocking", WHY1, c1), gone(L26, "low", WHY_L, c2), gone(O2, "outside", WHY_O, c2)]),
            ("the ticket's comment deleted", [], [c2], [F1, F2], [], [],
             [gone(L26, "low", WHY_L, c2), gone(O2, "outside", WHY_O, c2)]),
            ("both deleted", [], [], [F1, F2], [L26], [O2], []),
        ]
        for name, ticket, pr, blocking, low, outside, dismissed in steps:
            with self.subTest(name):
                self.serve(ticket=ticket, pr=pr, review_id=RID)
                out, _p = self.refreshed()
                self.assertRefreshed(out, 2, blocking=blocking, low=low, outside=outside, dismissed=dismissed)
                self.assertEqual(len(self.load_rounds()), 2)
                self.assertEqual(bag(self.stage_blocking()), bag(map(norm, blocking)))
                want = [noted(f, f["why"]) for f in dismissed]
                self.assertEqual(sorted(sections(self.notes()).get("Dismissed", [])), sorted(want), self.notes())


# =========================================================================== refused
class Refused(RefreshTest):
    def assertRefusedCleanly(self, p, snapshot, counts, needle=None):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        if needle:
            self.assertIn(needle, p.stderr)
        self.assertEqual(self.snapshot(), snapshot, "something was written")
        self.assertEqual(self.counts(), counts, "something ran or was posted")

    def test_refresh_refuses_without_a_round_on_a_moved_head_or_a_dirty_tree(self):
        # Each refusal writes nothing; once its cause is gone the same refresh applies the dismissal.
        by = note(OWNER, "11:00", dismiss(F1, WHY1))
        app = os.path.join(self.wt, "src", "app.py")

        def served():
            self.serve(ticket=[by])

        def recorded():
            served()
            self.record([F1, F2])
            self.stamp()

        def moved_head():
            recorded()
            self.advance()

        def dirty_tree():
            recorded()
            with open(app, encoding="utf-8") as fh:
                text = fh.read()
            write(app, text + "V31 = 31\n")

        def no_pr():
            self.no_pr()
            self.record([F1, F2])
            self.stamp()

        def failed_read():
            recorded()
            self.serve(ticket=[by], calls=[d.failing("issues/12/comments")])

        rows = [  # name, the setup, the cure, the words the refusal must hold
            ("no round", served, recorded, "no review round recorded for #12"),
            ("a moved head", moved_head, recorded, None),          # a round recorded at the new head
            ("no PR", no_pr, served, None),                        # the branch's PR opened
            ("a failed dismissal read", failed_read, served, None),
            ("a dirty tree", dirty_tree, lambda: self.git(self.wt, "checkout", "--", "src/app.py"), None),
        ]
        for name, setup, cure, needle in rows:
            with self.subTest(name):
                self.fresh()
                setup()
                snapshot, counts = self.snapshot(), self.counts()
                self.assertRefusedCleanly(self.refresh(), snapshot, counts, needle)
                cure()
                out, _p = self.refreshed()
                self.assertEqual(bag(out.get("blocking", [])), bag([norm(F2)]))
                self.assertEqual(bag(out.get("dismissed") or []), bag([gone(F1, "blocking", WHY1, by)]))
        self.git(self.wt, "checkout", "--", "src/app.py")

    def test_refresh_refuses_beside_another_mode(self):
        self.serve(review_id=RID)
        self.record([F1, F2])
        self.stamp()
        by = note(OWNER, "11:00", dismiss(F1, WHY1))
        self.serve(ticket=[by], review_id=RID)
        rulings = write(os.path.join(self.tmp, "rulings-in", "r.md"), "Ruling: keep the scale factor.\n")
        rows = [
            ("--review", self.review_file([F1])),
            ("--gpt-check",),
            ("--head", self.head()),
            ("--rulings", rulings),
            ("--model", "sonnet"),
            ("--latest",),
        ]
        for extra in rows:
            with self.subTest(extra[0]):
                snapshot, counts = self.snapshot(), self.counts()
                self.assertRefusedCleanly(self.refresh(*extra), snapshot, counts)
        self.assertEqual(bag(self.load_rounds()[-1]["blocking"]), bag(map(norm, [F1, F2])))
        out, _p = self.refreshed()                    # alone, the same refresh applies the dismissal
        self.assertRefreshed(out, 1, blocking=[F2], dismissed=[gone(F1, "blocking", WHY1, by)])


# =========================================================================== --latest
class Latest(RefreshTest):
    def test_latest_stays_read_only(self):
        self.two_rounds([F1, F2])
        by = note(OWNER, "11:00", dismiss(F1, WHY1), 34)
        self.serve(pr=[by], review_id=RID)
        for step in ("before the refresh", "after it"):
            with self.subTest(step):
                if step == "after it":
                    self.refreshed()
                snapshot, calls = self.snapshot(), len(self.gh_calls())
                out = self.latest()
                self.assertEqual(self.snapshot(), snapshot, "--latest wrote something")
                self.assertEqual(len(self.gh_calls()), calls, "--latest called gh")
                self.assertEqual(out["round"], 2)
                if step == "before the refresh":       # the comment is not applied by --latest
                    self.assertEqual(bag(out["blocking"]), bag(map(norm, [F1, F2])))
                    self.assertEqual(out.get("dismissed") or [], [])
                else:                                  # it prints what the refresh saved
                    self.assertEqual(bag(out["blocking"]), bag([norm(F2)]))
                    self.assertEqual(bag(out.get("dismissed") or []), bag([gone(F1, "blocking", WHY1, by)]))


if __name__ == "__main__":
    unittest.main()
