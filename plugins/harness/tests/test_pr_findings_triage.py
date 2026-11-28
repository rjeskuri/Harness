"""Tests for Sol's findings as unrated candidates Opus triages (ticket #534, epic #512).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_triage.py -v

The contract pinned here. `harness-pr findings N --head H [--review F] --gpt-check --json` runs `harness-gpt check`
as before, keeps Sol's findings (candidates, unrated) and its calls on the prior blockers in the receipt, and stops:
exit 0, no review posted, no round recorded, no ticket filed, the JSON

    {"ticket": N, "head": H, "result": "triage", "candidates_file": "<absolute path>", ...}   (no `round`, `record`)

naming a JSON file

    {"ticket": N, "head": H,
     "candidates": [{"id": 1, "path", "line", "body"}, ...],      Sol's findings in Sol's order, ids 1..n, no severity
     "prior": [{"id": 1, "path", "line", "severity", "body",     each prior blocker sent to Sol, ids 1..m,
                "sol": "fixed" | "unresolved"}, ...]}            with Sol's call on it

A --gpt-check repeated at the same head and brief does not run harness-gpt again and names the same candidates.
`harness-pr findings N --head H --gpt-triage V --json` reads Opus's verdicts V

    {"candidates": [{"id": i, "verdict": "accept", "severity": "high" | "medium" | "low"}
                    | {"id": i, "verdict": "set-aside", "reason": "<one line>"}, ...],
     "prior": [{"id": j, "verdict": "fixed" | "unresolved", "reason": "<one line>"}, ...]}

with exactly one verdict per candidate id and per prior id of the candidates file (in any order; a candidate maps to
its verdict by `id`), `reason` required on a set-aside and on a prior verdict that differs from Sol's call. It then
does what --gpt-check did before #534: an accepted candidate is the finding {path, line, severity: Opus's, body}
beside Claude's own, split by the ticket's files like any finding; a prior blocker Opus calls unresolved stays a
blocker and one it calls fixed does not; one review is posted (the receipt's marker reconciles an interrupted post),
the round recorded, out-of-scope tickets filed, and its --json is the findings JSON as before. A set-aside candidate
and each prior verdict that overrides Sol's call are in notes.md, one line each holding the finding's body and Opus's
reason, under a heading of their own (none of Low findings, Out of scope, Dismissed, Rulings), every round's, as the
notes are rebuilt from all rounds; they block nothing, are posted nowhere and are never ticketed.
A verdicts file that misses a verdict, an accept without a severity, a set-aside or an override without a reason, or
a triage at a head no --gpt-check ran for exits 1 with nothing posted and no round recorded.

Fixture: pr_findings_support's PostTest (the repositories, fakegh, the harness-board stub), plus a fake harness-gpt
in the module's fakebin that logs its argv and the brief it read and prints {"head", "model": "gpt-6.2-sol",
"findings": <conf findings, unrated>, "prior_fixed": <per prior blocker: its body in conf `fixed` when that is a list,
else conf `fixed`>}. Each test's first harness-pr run is a process of its own; later runs run the tool in this
process (gitrepo_support.run_in_process), git, gh, harness-board and harness-gpt still their own processes. Expected
values are derived here: the split from the touched files (pr_findings_support.split), the posted findings from the
verdicts, the notes lines from the reasons given.

Left open (not pinned): the wording and name of the notes heading; whether a confirmed prior call or Claude's own
findings appear in the candidates file; where the candidates file lives; the posted review's model label.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unittest

sys.dont_write_bytecode = True

import pr_findings_support as support  # noqa: E402
from pr_findings_support import (BINS, BRANCH_CHANGES, MARKER, OWNER, TOOL, PostTest, app_text,  # noqa: E402
                                 create_options, entry, executable, norm, opus_verdicts, sections, split,
                                 tearDownModule, unrated, write)  # noqa: F401  (tearDownModule: unittest's)
from gitrepo_support import run_in_process  # noqa: E402

STANDARD = {"Low findings", "Out of scope", "Dismissed", "Rulings"}
SEVERITIES = ("high", "medium", "low")

F1 = {"path": "src/app.py", "line": 3, "severity": "high", "body": "V3 is scaled twice."}
F2 = {"path": "tests/test_app.py", "line": 2, "severity": "medium", "body": "The test asserts nothing."}
O1 = {"path": "README.md", "line": 1, "severity": "high", "body": "The README names the wrong flag."}
# Sol's candidates: no severity.
S1 = {"path": "src/app.py", "line": 26, "body": "The cap at V26 is never checked."}
S2 = {"path": "tests/test_app.py", "line": 3, "body": "The fixture never closes its file."}
S3 = {"path": "src/app.py", "line": 12, "body": "V12 overflows on a 2 GB input."}
S4 = {"path": "lib/x.py", "line": 2, "body": "Z is read before it is set."}

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
fixed = conf.get("fixed", True)
calls = [p.get("body") in fixed for p in prior] if isinstance(fixed, list) else [bool(fixed)] * len(prior)
found = [{k: f[k] for k in ("path", "line", "body") if k in f} for f in conf.get("findings", [])]
print(json.dumps({"head": argv[argv.index("--head") + 1], "model": "gpt-6.2-sol", "findings": found,
                  "prior_fixed": calls}))
'''

COMMENT = re.compile(r"\[(high|medium|low)\] (.*)\n\n" + re.escape(MARKER) + r"\Z", re.S)
LISTED = re.compile(r"- `([^`]+?)(?::(\d+))?` \[(high|medium|low)\] (.*)\Z")


def setUpModule():
    support.setUpModule()
    executable(os.path.join(BINS["fakebin"], "harness-gpt"), f"#!{sys.executable}\n" + GPT)


def bag(items):
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def rated(f, severity):
    """Sol's candidate f as Opus accepted it."""
    return dict(f, severity=severity)


def places(findings):
    return sorted((f["path"], f["line"], f["severity"], f["body"]) for f in findings)


def published(sent):
    """(path, line, severity, body) of every finding a review POST carries: its inline comments and the findings
    listed in its body."""
    out = []
    for c in sent.get("comments") or []:
        m = COMMENT.match(c["body"])
        assert m, c["body"]
        out.append((c["path"], c["line"], m.group(1), m.group(2)))
    for line in sent["body"].splitlines():
        m = LISTED.match(line)
        if m:
            out.append((m.group(1), int(m.group(2)) if m.group(2) else None, m.group(3), m.group(4)))
    return sorted(out)


class TriageTest(PostTest):
    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    def setUp(self):
        super().setUp()
        self.k = 0

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
    def serve(self, calls=(), review_id=None, reviews=()):
        """PR #34 open at the worktree's HEAD."""
        self.open_pr(34, calls=calls, review_id=review_id, reviews=reviews)

    def advance(self):
        """A fix commit on the branch changing src/app.py line 10 + k, the PR moved to it; the new head."""
        self.k += 1
        head = self.commit("src/app.py", app_text(BRANCH_CHANGES + (10 + self.k,)))
        self.serve()
        return head

    def fresh(self):
        shutil.rmtree(self.reviews_dir(), True)

    def sol(self, findings=(), fixed=True):
        """Sol raises `findings` (unrated); `fixed` is its call on every prior blocker, or the bodies it calls fixed."""
        write(os.path.join(self.tmp, "gpt.json"), json.dumps({"findings": list(findings), "fixed": fixed}))

    # ----------------------------------------------------------- runs
    def check(self, review=None):
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json", "--head", self.head()]
        if review is not None:
            args += ["--review", self.review_file(review)]
        return self.pr(*args)

    def checked(self, review=None):
        """A --gpt-check that stops at triage: its JSON."""
        p = self.check(review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out.get("result"), "triage", out)
        return out

    def paired(self, review=None, accept=None, prior=None, reason=support.SET_ASIDE):
        """The check, then Opus's triage: (the triage call's JSON, its process)."""
        cands = self.candidates(self.checked(review))
        return self.triaged(opus_verdicts(cands, accept=accept, prior=prior, reason=reason))

    def gpt_runs(self):
        return self.jsonl(self.gpt_log)

    def stage_blocking(self):
        """What `harness-work stage` reads: reviews.blocking of the latest round."""
        import importlib
        return importlib.import_module("harness_lib.reviews").blocking(self.load_rounds()[-1])

    def state(self):
        return len(self.posts()), len(self.comment_reviews()), len(self.creates()), self.load_rounds()

    # ----------------------------------------------------------- the notes
    def assertNoted(self, f, reason):
        """notes.md has a line naming f's body and `reason`, under a heading of its own."""
        notes = self.notes()
        where = [h for h, lines in sections(notes).items() for line in lines if f["body"] in line and reason in line]
        self.assertTrue(where, f"{f['body']!r} with {reason!r} not in the notes:\n{notes}")
        self.assertFalse(set(where) & STANDARD, notes)

    def assertNotFiled(self, f):
        """f is neither a low finding nor an out-of-scope one in the notes, and no ticket names it."""
        parts = sections(self.notes())
        for heading in ("Low findings", "Out of scope"):
            self.assertFalse([line for line in parts.get(heading, []) if f["body"] in line], self.notes())
        titles = [create_options(c["argv"]).get("title") for c in self.creates() if not c.get("failed")]
        self.assertFalse([t for t in titles if t and t.startswith(f"{f['path']}: ") and f["body"][:20] in t], titles)


# =========================================================================== the check stops at triage
class Check(TriageTest):
    def test_gpt_check_stops_at_triage_with_sols_candidates(self):
        rows = [  # name, Sol's findings, Sol's fixed bodies on a fix re-check after a round blocking on F1 and F2
            ("three candidates on a first review", [S4, S1, S2], None),
            ("no candidates on a first review", [], None),
            ("a fix re-check", [S3], [F2["body"]]),
        ]
        for name, raised, fixed in rows:
            with self.subTest(name):
                self.fresh()
                self.serve()
                review = [F1, O1]                       # O1: an outside high a recorded round would ticket
                if fixed is not None:
                    self.sol([])
                    self.paired([F1, F2])
                    self.advance()
                    review = None
                self.sol(raised, fixed=True if fixed is None else fixed)
                before, runs = self.state(), len(self.gpt_runs())
                out = self.checked(review)
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                self.assertEqual(out["head"], self.head())
                self.assertNotIn("round", out)
                self.assertNotIn("record", out)
                self.assertEqual(self.state(), before, "the check posted, recorded or ticketed something")
                cands = self.candidates(out)
                self.assertEqual(cands["head"], self.head())
                got = cands["candidates"]
                self.assertEqual([c.get("id") for c in got], list(range(1, len(raised) + 1)))
                self.assertEqual([{k: c.get(k) for k in ("path", "line", "body")} for c in got], unrated(raised))
                self.assertFalse([c for c in got if "severity" in c], got)
                prior = [] if fixed is None else [dict(norm(F1), sol="unresolved"), dict(norm(F2), sol="fixed")]
                self.assertEqual(sorted(p.get("id") for p in cands["prior"]), list(range(1, len(prior) + 1)))
                self.assertEqual(bag({k: p.get(k) for k in ("path", "line", "severity", "body", "sol")}
                                     for p in cands["prior"]), bag(prior))


# =========================================================================== the triage publishes
class Publish(TriageTest):
    def test_triage_publishes_the_accepted_candidates_at_opus_severity(self):
        rows = [  # name, Sol's findings in its order, Opus's verdict by body (a severity, else the set-aside reason),
            #       the verdicts listed in reverse id order
            ("a medium and a low accepted, two set aside", [S1, S2, S3, S4],
             {S1["body"]: "medium", S2["body"]: "low", S3["body"]: "No caller passes more than 1 MB.",
              S4["body"]: "lib/x.py sets Z at import, before any read."}, False),
            ("a high, a low and an outside medium, the verdicts in reverse", [S4, S3, S2, S1],
             {S1["body"]: "The caller checks the cap before V26.", S2["body"]: "high", S3["body"]: "low",
              S4["body"]: "medium"}, True),
            ("every candidate set aside", [S3, S1],
             {S3["body"]: "Speculative: the input is bounded upstream.", S1["body"]: "V26 has no cap to check."},
             False),
        ]
        for name, raised, verdicts, reverse in rows:
            with self.subTest(name):
                self.fresh()
                self.sol(raised)
                self.serve(review_id=7300)
                posts, creates = len(self.posts()), len(self.creates())
                cands = self.candidates(self.checked([F1]))
                ids = {c["body"]: c["id"] for c in cands["candidates"]}
                given = [{"id": ids[body], "verdict": "accept", "severity": v} if v in SEVERITIES else
                         {"id": ids[body], "verdict": "set-aside", "reason": v} for body, v in verdicts.items()]
                given.sort(key=lambda v: v["id"], reverse=reverse)
                out, _p = self.triaged({"candidates": given, "prior": []})

                accepted = [rated(f, verdicts[f["body"]]) for f in raised if verdicts[f["body"]] in SEVERITIES]
                set_aside = [(f, verdicts[f["body"]]) for f in raised if verdicts[f["body"]] not in SEVERITIES]
                blocking, low, outside = split([F1] + accepted)
                rnd = self.load_rounds()[-1]
                for key, want in (("blocking", blocking), ("low", low), ("outside", outside)):
                    self.assertEqual(bag(rnd.get(key, [])), bag(want), f"round {key}")
                    self.assertEqual(bag(out.get(key, [])), bag(want), f"--json {key}")
                self.assertEqual(bag(self.stage_blocking()), bag(blocking))
                self.assertEqual((out["round"], out["record"]), (1, {"stage": "review", "result": "pass"}))

                # One review: Claude's finding and the accepted ones at Opus's severity, nothing set aside.
                self.assertEqual(len(self.posts()), posts + 1)
                self.assertEqual(self.comment_reviews(), [])
                sent = json.loads(self.posts()[-1]["stdin"])
                self.assertEqual(published(sent), places(blocking + low + [f for f in outside
                                                                           if f["severity"] == "low"]))
                for f, _why in set_aside:
                    self.assertNotIn(f["body"], self.posts()[-1]["stdin"])

                # The notes: an accepted low as any low; each set-aside one with its reason, under its own heading.
                lows = sections(self.notes()).get("Low findings", [])
                for f in low:
                    self.assertIn(entry(f), lows)
                for f, why in set_aside:
                    self.assertNoted(f, why)
                    self.assertNotFiled(f)

                # Tickets: only an accepted outside high or medium.
                ticketed = [f for f in outside if f["severity"] in ("high", "medium")]
                self.assertEqual(len(self.creates()) - creates, len(ticketed), [c["argv"] for c in self.creates()])
                for f in ticketed:
                    self.assertIn(self.number_for(f), out["tickets"])


# =========================================================================== the prior blockers
class Prior(TriageTest):
    def test_opus_confirms_or_overrides_sols_prior_blocker_calls(self):
        r0 = "Speculative: nothing reaches V12 with 2 GB."
        r1 = "The fix commit rewrote V3 and its scale is applied once."
        r2 = "The test still asserts nothing after the fix."
        rows = [  # name, Opus's verdict by prior blocker body (verdict, reason), the blocking after, the overrides
            ("Opus overrides both calls", {F1["body"]: ("fixed", r1), F2["body"]: ("unresolved", r2)}, [F2],
             [(F1, r1), (F2, r2)]),
            ("Opus confirms both calls", {}, [F1], []),
            ("Opus calls Sol's unresolved blocker fixed", {F1["body"]: ("fixed", r1)}, [], [(F1, r1)]),
        ]
        for name, verdicts, blocking, overridden in rows:
            with self.subTest(name):
                self.fresh()
                self.serve()
                self.sol([S3])
                self.paired([F1, F2], reason=r0)            # round 1 blocks on F1 and F2; S3 set aside
                self.advance()
                self.sol([], fixed=[F2["body"]])           # Sol: F1 unresolved, F2 fixed
                cands = self.candidates(self.checked())
                self.assertEqual(sorted((p["body"], p["sol"]) for p in cands["prior"]),
                                 sorted([(F1["body"], "unresolved"), (F2["body"], "fixed")]))
                out, _p = self.triaged(opus_verdicts(cands, prior=verdicts))
                self.assertEqual(out["round"], 2)
                self.assertEqual(bag(out["blocking"]), bag(map(norm, blocking)))
                self.assertEqual(bag(self.load_rounds()[-1]["blocking"]), bag(map(norm, blocking)))
                self.assertEqual(bag(self.stage_blocking()), bag(map(norm, blocking)))
                self.assertEqual(out["record"], {"stage": "fix", "result": "fail" if blocking else "pass"})
                for f, why in overridden:
                    self.assertNoted(f, why)
                self.assertNoted(S3, r0)                    # round 1's set-aside stays in the notes


# =========================================================================== an interrupted post
class Recovery(TriageTest):
    def test_a_triage_repeated_after_an_interrupted_post_publishes_once(self):
        failed = {"stderr": "gh: HTTP 502: Bad Gateway\n", "exit": 1}
        rows = [  # name, whether GitHub received the post whose reply failed, the id GitHub gave the review
            ("the post failed", False, 9101),
            ("the post reached GitHub, its reply was lost", True, 9202),
        ]
        for name, landed, rid in rows:
            with self.subTest(name):
                self.fresh()
                self.sol([S1, S3])
                post = {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"],
                        "responses": [failed] if landed else [failed, {"stdout_json": {"id": rid}}]}
                self.serve(calls=[post])
                head, runs, posts = self.head(), len(self.gpt_runs()), len(self.posts())
                cands = self.candidates(self.checked([F1]))
                verdicts = opus_verdicts(cands, accept={S1["body"]: "medium"})

                p = self.triage(verdicts)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(len(self.posts()), posts + 1)
                self.assertEqual(self.load_rounds(), [])
                if landed:                                  # GitHub holds the review the failed call sent
                    sent = json.loads(self.posts()[-1]["stdin"])
                    review = {"id": rid, "user": {"login": OWNER}, "body": sent["body"], "commit_id": head,
                              "submitted_at": "2026-11-28T10:00:00Z", "state": "COMMENTED"}
                    self.serve(calls=[post], reviews=[review])

                # The check again at this head: Sol is not asked again and the candidates are the same.
                again = self.candidates(self.checked([F1]))
                self.assertEqual(again["candidates"], cands["candidates"])
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                self.assertEqual(len(self.posts()), posts + 1)

                out, _p = self.triaged(verdicts)
                self.assertEqual(len(self.posts()), posts + (1 if landed else 2))
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                self.assertEqual([(r["round"], r["head"], r["review_id"]) for r in self.load_rounds()],
                                 [(1, head, rid)])
                self.assertEqual(bag(out["blocking"]), bag([norm(F1), rated(S1, "medium")]))
                self.assertNoted(S3, support.SET_ASIDE)


# =========================================================================== malformed verdicts
class Refused(TriageTest):
    def test_incomplete_or_unreasoned_verdicts_are_refused(self):
        self.serve()
        self.sol([])
        self.paired([F1])                                   # round 1 blocks on F1
        self.advance()                                      # the fix changes src/app.py line 11
        on_fix = {"path": "src/app.py", "line": 11, "body": "V11 is written twice."}
        self.sol([on_fix, S3], fixed=False)
        cands = self.candidates(self.checked())
        ids = {c["body"]: c["id"] for c in cands["candidates"]}
        a, b, j = ids[on_fix["body"]], ids[S3["body"]], cands["prior"][0]["id"]
        accept = {"id": a, "verdict": "accept", "severity": "medium"}
        aside = {"id": b, "verdict": "set-aside", "reason": "The input is bounded."}
        confirm = {"id": j, "verdict": "unresolved"}
        rows = [
            ("a candidate without a verdict", {"candidates": [accept], "prior": [confirm]}),
            ("an accept without a severity", {"candidates": [{"id": a, "verdict": "accept"}, aside],
                                              "prior": [confirm]}),
            ("a set-aside without a reason", {"candidates": [accept, {"id": b, "verdict": "set-aside"}],
                                              "prior": [confirm]}),
            ("a prior blocker without a verdict", {"candidates": [accept, aside], "prior": []}),
            ("an override of Sol's call without a reason", {"candidates": [accept, aside],
                                                            "prior": [{"id": j, "verdict": "fixed"}]}),
        ]
        for name, verdicts in rows:
            with self.subTest(name):
                before = self.state()
                p = self.triage(verdicts)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertTrue(p.stderr.strip())
                self.assertEqual(self.state(), before)

        out, _p = self.triaged({"candidates": [aside, accept], "prior": [confirm]})
        self.assertEqual(out["round"], 2)
        self.assertEqual(bag(out["blocking"]), bag([norm(F1), rated(on_fix, "medium")]))

        # A head no --gpt-check ran for.
        self.advance()
        before = self.state()
        p = self.triage({"candidates": [], "prior": [{"id": 1, "verdict": "unresolved"}]})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
