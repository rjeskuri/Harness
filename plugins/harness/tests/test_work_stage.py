"""Tests for `harness-work stage N <stage> pass|fail` (ticket #258, epic #249): it records the attempt exactly as
`harness-work attempt` does (same store, same answer, same options) and names the next step of the work loop.

- line 1 of the text output is attempt's answer, line 2 is `next: <step>`; --json is attempt's object plus "next";
- retry and escalate repeat the stage, except that a breaker failure goes back to `tests`; needs-input names
  needs-input;
- done: tests -> breaker (risk:high) or code; breaker -> code; code -> review; review and fix -> fix while the
  latest review round (harness_lib.reviews.latest_round) has blocking findings, else ci; ci -> `ultra, then ready`
  (risk:high) or ready;
- refused, exit 1, nothing recorded: review or fix with no review round; fix pass while the latest round has
  blocking findings and fix fail while it has none; tests or breaker on a no-tests ticket; breaker on a ticket that
  is not risk:high.

Fixtures: test_work.WorkTest (temporary HOME, a real git origin and clone, the fake gh); each ticket cache is
written here as `harness-board show` writes it (risk, flags, labels, tier); review rounds are written through
harness_lib.reviews.save_round with HOME pointed at the test's. Expected answers come from the ticket's table, and
where a line 1 is compared with `attempt`, `attempt` runs on a twin ticket with the same labels.

Choices the ticket leaves open, fixed here:
- `review fail` with no review round is refused as `review pass` is (the ticket says "review or fix").
- `review fail` with a round recorded is a plain retry/escalate of review: next `review`, whatever the round holds.
- The no-tests and breaker refusals are checked by their exit, the one stderr line naming `no-tests` or `risk:high`,
  and the store; the ticket gives no exact text for them.
- The fix-mismatch refusal names the latest round's blocking count as a number (`0` or `no` for none) and says
  `blocking`.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_stage.py" -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, HERE)

import test_work as tw  # noqa: E402  WorkTest, the fake gh and claude, the label route

NEXT = "next: "
NO_ROUND = "no review round recorded for #{n}; run harness-pr findings first"


def setUpModule():
    tw.setUpModule()


def tearDownModule():
    tw.tearDownModule()


def reviews_module():
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    import importlib
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.reviews")


def finding(k, severity="high"):
    return {"path": f"src/mod{k}.py", "line": 10 + k, "severity": severity, "body": f"Finding {k} ({severity})"}


def without_at(attempts):
    return [{k: v for k, v in a.items() if k != "at"} for a in attempts]


class Stage(tw.WorkTest):
    def setUp(self):
        super().setUp()
        self.next_number = 300
        self.heads = 0

    # ----------------------------------------------------------- fixtures
    def put_ticket(self, risk, size="M", kind="task", no_tests=False, number=None):
        """A fresh ticket cache as harness-board show writes it; returns its number."""
        if number is None:
            number = self.next_number
            self.next_number += 1
        flags = ["no-tests"] if no_tests else []
        labels = [kind, "status:in-progress", "size:" + size] + (["risk:" + risk] if risk else []) + flags
        tw.write(self.cache_path(number), json.dumps({
            "number": number, "title": tw.TITLE, "type": kind, "size": size, "risk": risk, "flags": flags,
            "labels": labels, "tier": tw.route_of(kind, size, risk)[0], "attempts": []}))
        return number

    def twin(self, number):
        """A second ticket with the same cache as `number` (attempts included), for `attempt` to answer."""
        cache = self.read_json(self.cache_path(number))
        other = self.next_number
        self.next_number += 1
        cache["number"] = other
        tw.write(self.cache_path(other), json.dumps(cache))
        return other

    def save_round(self, number, blocking, low=0, outside=0, head=None):
        """Record a review round for `number` with `blocking` blocking findings, through harness_lib.reviews."""
        if head is None:
            self.heads += 1
            head = f"{self.heads:040x}"
        rnd = {"at": "2026-10-26T10:00:00Z", "head": head, "base": "b" * 40,
               "blocking": [finding(k) for k in range(blocking)],
               "low": [finding(100 + k, "low") for k in range(low)],
               "outside": [finding(200 + k, "medium") for k in range(outside)], "pr": None, "review_id": None}
        reviews = reviews_module()
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return reviews.save_round(self.key, number, rnd)

    def raw_cache(self, number):
        with open(self.cache_path(number), encoding="utf-8") as fh:
            return fh.read()

    def attempts(self, number):
        return self.read_json(self.cache_path(number))["attempts"]

    # ----------------------------------------------------------- calls
    def stage(self, number, *argv):
        """(line 1, the step after `next: `) of a successful `stage` call printing exactly two lines."""
        p = self.ok("stage", str(number), *argv)
        lines = p.stdout.strip("\n").split("\n")
        self.assertEqual(len(lines), 2, p.stdout)
        self.assertTrue(lines[1].startswith(NEXT), p.stdout)
        return lines[0], lines[1][len(NEXT):]

    def stage_json(self, number, *argv):
        return json.loads(self.ok("stage", str(number), *argv, "--json").stdout)

    def attempt(self, number, *argv):
        return self.ok("attempt", str(number), *argv).stdout.strip()

    def assertRefused(self, number, argv, *needles):
        """`stage number *argv` exits 1 with one stderr line holding every needle, prints nothing on stdout and
        leaves the ticket cache byte for byte as it was."""
        before = self.raw_cache(number)
        p = self.work("stage", str(number), *argv)
        self.assertOneLineError(p, *needles)
        self.assertEqual(p.stdout, "")
        self.assertEqual(self.raw_cache(number), before)
        return p

    # ----------------------------------------------------------- broad: the table
    def test_next_step_for_each_stage_and_risk(self):
        for risk in ("low", "high"):
            high = risk == "high"

            with self.subTest(risk=risk, row="tests pass"):
                n = self.put_ticket(risk)
                self.assertEqual(self.stage(n, "tests", "pass"), ("done", "breaker" if high else "code"))

            with self.subTest(risk=risk, row="breaker pass"):
                n = self.put_ticket(risk)
                if high:
                    self.assertEqual(self.stage(n, "breaker", "pass"), ("done", "code"))
                else:
                    self.assertRefused(n, ["breaker", "pass"], "risk:high")

            with self.subTest(risk=risk, row="breaker fail, first then second"):
                n = self.put_ticket(risk)
                if high:
                    self.assertEqual(self.stage(n, "breaker", "fail"), ("retry", "tests"))
                    self.assertEqual(self.stage(n, "breaker", "fail"), ("needs-input", "needs-input"))
                else:
                    self.assertRefused(n, ["breaker", "fail"], "risk:high")
                    self.assertRefused(n, ["breaker", "fail"], "risk:high")
                    self.assertEqual(self.attempts(n), [])

            with self.subTest(risk=risk, row="code pass"):
                n = self.put_ticket(risk)
                self.assertEqual(self.stage(n, "code", "pass"), ("done", "review"))

            with self.subTest(risk=risk, row="review pass, 2 blocking"):
                n = self.put_ticket(risk)
                self.save_round(n, 2)
                self.assertEqual(self.stage(n, "review", "pass"), ("done", "fix"))

            with self.subTest(risk=risk, row="review pass, 0 blocking"):
                n = self.put_ticket(risk)
                self.save_round(n, 0)
                self.assertEqual(self.stage(n, "review", "pass"), ("done", "ci"))

            with self.subTest(risk=risk, row="fix fail, then the second fix fail"):
                n = self.put_ticket(risk)
                self.save_round(n, 2)
                self.assertEqual(self.stage(n, "fix", "fail"), ("retry", "fix"))
                self.assertEqual(self.stage(n, "fix", "fail"), ("needs-input", "needs-input"))

            with self.subTest(risk=risk, row="fix pass"):
                n = self.put_ticket(risk)
                self.save_round(n, 0)
                self.assertEqual(self.stage(n, "fix", "pass"), ("done", "ci"))

            with self.subTest(risk=risk, row="ci pass"):
                n = self.put_ticket(risk)
                self.assertEqual(self.stage(n, "ci", "pass"), ("done", "ultra, then ready" if high else "ready"))

        # the risk the step depends on is the ticket's own: a high and a low ticket side by side
        low, high = self.put_ticket("low", size="S"), self.put_ticket("high", size="L")
        for stage, low_next, high_next in (("tests", "code", "breaker"), ("ci", "ready", "ultra, then ready")):
            with self.subTest(side_by_side=stage):
                self.assertEqual(self.stage(low, stage, "pass"), ("done", low_next))
                self.assertEqual(self.stage(high, stage, "pass"), ("done", high_next))

    def test_failures_repeat_the_stage_and_a_breaker_failure_goes_back_to_tests(self):
        specs = [("task", "S", "low"), ("task", "M", "low"), ("bug", "S", "low"), ("task", "M", "high")]
        known = {   # line 1 of three failures in a row, from the escalation rule (#239, #252)
            (("task", "S", "low"), "code"): ["retry", "escalate opus", "needs-input"],
            (("task", "M", "low"), "code"): ["retry", "escalate opus xhigh", "needs-input"],
            (("task", "M", "high"), "code"): ["retry", "needs-input", "needs-input"],
            (("task", "S", "low"), "fix"): ["retry", "needs-input", "needs-input"],
            (("task", "M", "high"), "breaker"): ["retry", "needs-input", "needs-input"],
        }
        seen = set()
        for kind, size, risk in specs:
            stages = ["tests", "code", "review", "fix", "ci"] + (["breaker"] if risk == "high" else [])
            for stage in stages:
                with self.subTest(kind=kind, size=size, risk=risk, stage=stage):
                    n = self.put_ticket(risk, size=size, kind=kind)
                    if stage in ("review", "fix"):
                        self.save_round(n, 1)
                    other = self.twin(n)
                    answers = []
                    for _ in range(3):
                        answer, step = self.stage(n, stage, "fail")
                        self.assertEqual(answer, self.attempt(other, stage, "fail"))
                        if answer == "needs-input":
                            self.assertEqual(step, "needs-input")
                        else:
                            self.assertTrue(answer == "retry" or answer.startswith("escalate "), answer)
                            self.assertEqual(step, "tests" if stage == "breaker" else stage)
                        answers.append(answer)
                        seen.add(answer.split()[0])
                    if ((kind, size, risk), stage) in known:
                        self.assertEqual(answers, known[((kind, size, risk), stage)])
                    # a pass ends the run: the next failure is a retry of the same stage again
                    if stage in ("review", "fix"):
                        self.save_round(n, 0)
                    self.assertEqual(self.stage(n, stage, "pass")[0], "done")
                    if stage == "fix":
                        self.save_round(n, 1)
                    self.assertEqual(self.stage(n, stage, "fail"), ("retry", "tests" if stage == "breaker" else stage))
        self.assertEqual(seen, {"retry", "escalate", "needs-input"})

        # an explicit tier escalates as attempt does, and the stage still repeats
        n = self.put_ticket("high", size="L")
        self.assertEqual(self.stage(n, "code", "fail", "--tier", "sonnet"), ("retry", "code"))
        self.assertEqual(self.stage(n, "code", "fail", "--tier", "sonnet"), ("escalate opus", "code"))
        self.assertEqual(self.stage(n, "ci", "fail", "--tier", "opus", "--effort", "high"), ("retry", "ci"))
        self.assertEqual(self.stage(n, "ci", "fail", "--tier", "opus", "--effort", "high"),
                         ("escalate opus xhigh", "ci"))
        self.assertEqual(self.stage(n, "breaker", "fail", "--tier", "sonnet"), ("retry", "tests"))
        self.assertEqual(self.stage(n, "tests", "pass"), ("done", "breaker"))      # the tightened tests
        self.assertEqual(self.stage(n, "breaker", "fail", "--tier", "sonnet"), ("needs-input", "needs-input"))

    def test_records_the_same_attempt_as_attempt(self):
        # (argv, blocking count of a review round saved just before the call, or None)
        high = [
            (["tests", "fail"], None), (["tests", "pass"], None), (["breaker", "fail"], None),
            (["tests", "pass"], None), (["breaker", "pass", "--tier", "opus"], None), (["code", "fail"], None),
            (["code", "fail", "--tier", "opus", "--effort", "high"], None),
            (["code", "pass", "--tier", "opus", "--effort", "xhigh"], None), (["review", "pass"], 2),
            (["fix", "fail"], None), (["fix", "fail", "--tier", "sonnet"], None), (["fix", "pass"], 0),
            (["ci", "fail"], None), (["ci", "fail", "--effort", "max"], None), (["ci", "pass", "--tier", "fable"], None),
        ]
        low = [
            (["tests", "pass"], None), (["code", "fail"], None), (["code", "fail"], None),
            (["code", "pass", "--tier", "opus"], None), (["review", "fail"], 1), (["review", "pass"], None),
            (["fix", "fail", "--effort", "medium"], None), (["fix", "pass"], 0), (["ci", "pass"], None),
        ]
        for name, (risk, size, kind), sequence in (("high", ("high", "M", "task"), high),
                                                   ("low", ("low", "S", "task"), low),
                                                   ("bug", ("low", "M", "bug"), low)):
            n = self.put_ticket(risk, size=size, kind=kind)
            other = self.twin(n)
            for argv, blocking in sequence:
                with self.subTest(ticket=name, argv=argv):
                    if blocking is not None:
                        self.save_round(n, blocking)
                        self.save_round(other, blocking)
                    answer, _ = self.stage(n, *argv)
                    self.assertEqual(answer, self.attempt(other, *argv))
            mine, theirs = self.read_json(self.cache_path(n)), self.read_json(self.cache_path(other))
            with self.subTest(ticket=name, store="attempts"):
                self.assertEqual(len(mine["attempts"]), len(sequence))
                self.assertEqual(without_at(mine["attempts"]), without_at(theirs["attempts"]))
                self.assertEqual([set(a) for a in mine["attempts"]], [set(a) for a in theirs["attempts"]])
                self.assertEqual([(a["stage"], a["result"]) for a in mine["attempts"]],
                                 [(argv[0], argv[1]) for argv, _ in sequence])
            with self.subTest(ticket=name, store="the rest of the cache"):
                rest = {k: v for k, v in mine.items() if k not in ("attempts", "number")}
                self.assertEqual(rest, {k: v for k, v in theirs.items() if k not in ("attempts", "number")})

    # ----------------------------------------------------------- review rounds
    def test_review_and_fix_follow_the_latest_review_round(self):
        # the blocking count of the only round decides review's next step
        for blocking, expected in ((1, "fix"), (3, "fix"), (5, "fix"), (0, "ci")):
            with self.subTest(blocking=blocking):
                n = self.put_ticket("low")
                self.save_round(n, blocking)
                self.assertEqual(self.stage(n, "review", "pass"), ("done", expected))

        # low and outside findings are not blocking
        n = self.put_ticket("high")
        self.save_round(n, 0, low=3, outside=2)
        self.assertEqual(self.stage(n, "review", "pass"), ("done", "ci"))
        self.assertEqual(self.stage(n, "fix", "pass"), ("done", "ci"))

        # only the latest round counts, and a round on the same head replaces the latest
        n = self.put_ticket("low")
        self.save_round(n, 3)
        self.save_round(n, 0)
        self.assertEqual(self.stage(n, "review", "pass"), ("done", "ci"))
        latest = self.save_round(n, 2)
        self.assertEqual(self.stage(n, "review", "pass"), ("done", "fix"))
        self.save_round(n, 0, head=latest["head"])
        self.assertEqual(self.stage(n, "review", "pass"), ("done", "ci"))

        # a fix loop: two blocking, the fix fails; one still blocking, it fails again; none left, it passes
        n = self.put_ticket("high")
        self.save_round(n, 2)
        self.assertEqual(self.stage(n, "review", "pass"), ("done", "fix"))
        self.assertEqual(self.stage(n, "fix", "fail"), ("retry", "fix"))
        self.save_round(n, 1)
        self.assertEqual(self.stage(n, "fix", "fail"), ("needs-input", "needs-input"))
        self.save_round(n, 0)
        self.assertEqual(self.stage(n, "fix", "pass"), ("done", "ci"))
        self.assertEqual(self.stage(n, "ci", "pass"), ("done", "ultra, then ready"))

        # a review failure repeats the review whatever the round holds
        for blocking in (0, 2):
            with self.subTest(review_fail_blocking=blocking):
                n = self.put_ticket("low")
                self.save_round(n, blocking)
                self.assertEqual(self.stage(n, "review", "fail"), ("retry", "review"))

        # rounds are the ticket's own: another ticket's blocking round does not send this one to fix
        a, b = self.put_ticket("low"), self.put_ticket("low")
        self.save_round(a, 4)
        self.save_round(b, 0)
        self.assertEqual(self.stage(b, "review", "pass"), ("done", "ci"))
        self.assertEqual(self.stage(a, "review", "pass"), ("done", "fix"))

    def test_fix_result_must_match_the_latest_round(self):
        # fix pass while the latest round still has blocking findings: refused, naming the count
        for blocking in (2, 3, 5):
            with self.subTest(fix_pass_blocking=blocking):
                n = self.put_ticket("low")
                self.save_round(n, blocking)
                p = self.assertRefused(n, ["fix", "pass"], "blocking")
                self.assertRegex(p.stderr, rf"(?<![\d#]){blocking}(?!\d)")
                self.assertEqual(self.attempts(n), [])
                # the refusal did not count: the first recorded fix failure is a retry
                self.assertEqual(self.stage(n, "fix", "fail"), ("retry", "fix"))

        # fix fail while the latest round has none: refused, naming the (zero) count
        for risk in ("low", "high"):
            with self.subTest(fix_fail_none=risk):
                n = self.put_ticket(risk)
                self.save_round(n, 0, low=2)
                p = self.assertRefused(n, ["fix", "fail"], "blocking")
                self.assertTrue(re.search(r"(?<![\d#])0(?!\d)", p.stderr) or re.search(r"\bno\b", p.stderr),
                                p.stderr)
                self.assertEqual(self.stage(n, "fix", "pass"), ("done", "ci"))

        # the latest round decides, not an earlier one
        n = self.put_ticket("high")
        self.save_round(n, 3)
        self.save_round(n, 0)
        self.assertRefused(n, ["fix", "fail"], "blocking")
        self.assertEqual(self.stage(n, "fix", "pass"), ("done", "ci"))
        self.save_round(n, 2)
        p = self.assertRefused(n, ["fix", "pass"], "blocking")
        self.assertRegex(p.stderr, r"(?<![\d#])2(?!\d)")
        self.assertEqual(self.stage(n, "fix", "fail"), ("retry", "fix"))
        self.assertEqual([(a["stage"], a["result"]) for a in self.attempts(n)], [("fix", "pass"), ("fix", "fail")])

    def test_no_review_round_is_refused(self):
        for risk in ("low", "high"):
            n = self.put_ticket(risk)
            # earlier attempts stay as they are
            self.stage(n, "tests", "pass")
            self.stage(n, "code", "fail")
            self.stage(n, "code", "pass")
            recorded = self.attempts(n)
            self.assertEqual(len(recorded), 3)
            # another ticket's round is not this one's
            self.save_round(self.put_ticket(risk), 2)
            for argv in (["review", "pass"], ["review", "fail"], ["fix", "pass"], ["fix", "fail"],
                         ["fix", "fail", "--tier", "opus"], ["review", "pass", "--tier", "fable"]):
                with self.subTest(risk=risk, argv=argv):
                    self.assertRefused(n, argv, NO_ROUND.format(n=n))
            self.assertEqual(self.attempts(n), recorded)
            # once a round is recorded the same call goes through
            self.save_round(n, 0)
            self.assertEqual(self.stage(n, "review", "pass"), ("done", "ci"))

    def test_breaker_on_a_low_risk_ticket_and_tests_on_a_no_tests_ticket_are_refused(self):
        # breaker on a ticket that is not risk:high: low, or no risk at all, at any size
        for risk, size in (("low", "S"), ("low", "M"), ("low", "L"), (None, "M"), (None, "S")):
            n = self.put_ticket(risk, size=size)
            self.stage(n, "tests", "pass")
            for argv in (["breaker", "pass"], ["breaker", "fail"], ["breaker", "fail", "--tier", "opus"]):
                with self.subTest(risk=risk, size=size, argv=argv):
                    self.assertRefused(n, argv, "risk:high")
            self.assertEqual([a["stage"] for a in self.attempts(n)], ["tests"])

        # tests and breaker on a no-tests ticket, low or high risk
        for risk in ("low", "high"):
            n = self.put_ticket(risk, no_tests=True)
            for argv in (["tests", "pass"], ["tests", "fail"], ["breaker", "pass"], ["breaker", "fail"]):
                with self.subTest(no_tests=risk, argv=argv):
                    if argv[0] == "tests" or risk == "high":
                        self.assertRefused(n, argv, "no-tests")
                    else:       # both refusals apply; either may be the one named
                        p = self.assertRefused(n, argv)
                        self.assertTrue("no-tests" in p.stderr or "risk:high" in p.stderr, p.stderr)
            self.assertEqual(self.attempts(n), [])
            # the rest of the loop still runs on a no-tests ticket
            self.assertEqual(self.stage(n, "code", "pass"), ("done", "review"))
            after_ci = "ultra, then ready" if risk == "high" else "ready"
            self.assertEqual(self.stage(n, "ci", "pass"), ("done", after_ci))

        # the same ticket without the flag takes tests (and breaker on risk:high)
        n = self.put_ticket("high")
        self.assertEqual(self.stage(n, "tests", "pass"), ("done", "breaker"))
        self.assertEqual(self.stage(n, "breaker", "pass"), ("done", "code"))

    # ----------------------------------------------------------- --json
    def test_json_adds_next_to_the_attempt_object(self):
        # (argv, blocking count of a round saved just before, the expected next)
        high = [
            (["tests", "pass"], None, "breaker"),
            (["breaker", "fail"], None, "tests"),
            (["breaker", "fail"], None, "needs-input"),
            (["code", "fail", "--tier", "sonnet"], None, "code"),
            (["code", "fail", "--tier", "sonnet"], None, "code"),       # escalate opus
            (["code", "pass"], None, "review"),
            (["review", "pass"], 2, "fix"),
            (["fix", "fail"], None, "fix"),
            (["fix", "pass"], 0, "ci"),
            (["ci", "fail"], None, "ci"),
            (["ci", "pass"], None, "ultra, then ready"),
        ]
        low = [
            (["tests", "pass"], None, "code"),
            (["code", "fail"], None, "code"),
            (["code", "fail"], None, "code"),
            (["code", "fail"], None, "needs-input"),
            (["review", "pass"], 0, "ci"),
            (["ci", "pass"], None, "ready"),
        ]
        actions = set()
        for name, (risk, size), sequence in (("high", ("high", "M"), high), ("low", ("low", "S"), low)):
            n = self.put_ticket(risk, size=size)
            other = self.twin(n)
            for argv, blocking, expected in sequence:
                with self.subTest(ticket=name, argv=argv):
                    if blocking is not None:
                        self.save_round(n, blocking)
                        self.save_round(other, blocking)
                    data = self.stage_json(n, *argv)
                    theirs = json.loads(self.ok("attempt", str(other), *argv, "--json").stdout)
                    self.assertEqual(set(data), set(theirs) | {"next"})
                    self.assertEqual(data["next"], expected)
                    self.assertEqual(data["number"], n)
                    self.assertEqual({k: v for k, v in data.items() if k not in ("next", "number")},
                                     {k: v for k, v in theirs.items() if k != "number"})
                    actions.add(data["action"])
        self.assertEqual(actions, {"done", "retry", "escalate", "needs-input"})


if __name__ == "__main__":
    unittest.main()
