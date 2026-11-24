"""Tests for harness-work: start (branch and worktree from origin/main, an existing remote branch reused, the
exclude line, the worktree setup, .worktreeinclude copies, refusals), attempt's retry, escalate and
park sequence, dispatch (dry-run plan, bg on the subscription by default whatever the api budget says,
--venue headless billed through apiKeyHelper and refused while the api budget is exhausted, re-dispatch of a run
that ended without a PR), runs --sweep's outcome
classification and the budget it marks, the sweep stopping the bg session of a ticket that is in review, needs input,
blocked or closed (#123), budget status, exhaust and reset, worktrees and worktree remove. start's own class is in
test_work_start.py (#447: split off near the 180 s a module may take).

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME, a bare `origin` and a clone of it (`proj`, the main checkout) made with
real git. A `fakebin` folder holds `gh` (a wrapper around fakegh.py) and `claude` (a fake that logs its
argv, cwd and environment, answers `agents --json` from $FAKECLAUDE_AGENTS, prints `backgrounded · abc123`
for --bg, answers `stop <id>` with exit 0, or exit 1 for an id listed in $FAKECLAUDE_STOP_FAIL (comma-separated),
and for -p sleeps briefly, then prints a canned JSON result; for --cloud it refuses unless its stdout is a terminal,
logs the origin's heads and prints the create-and-exit lines with \\r\\n ends and ANSI escapes, or the failure
$FAKECLAUDE_CLOUD names), and terminal-notifier and osascript that do nothing. harness-board is the real one,
found next to harness-work. The fake executables are written once for the module: macOS checks a new
executable on its first run, which costs about half a second each time.
"""

from __future__ import annotations

import unittest

from work_support import *  # noqa: F401,F403


# =========================================================================== attempt
class Attempt(WorkTest):
    def test_retry_escalate_park_and_tier_progression(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["code", "fail"], "retry"),
            (["code", "fail"], "escalate opus"),
            (["code", "fail"], "park"),
            (["code", "fail"], "park"),
            (["tests", "fail"], "retry"),                    # each stage counts its own failures
            (["code", "pass"], "done"),
            (["code", "fail"], "retry"),                     # a pass ends the stage's run of failures
            (["review", "fail", "--tier", "opus"], "retry"),
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),     # #239: opus goes to opus xhigh
            (["ci", "fail", "--tier", "fable"], "retry"),
            (["ci", "fail", "--tier", "fable"], "park"),    # #239: nothing above an explicit Fable
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # #239: effort recorded
        self.assertEqual((attempts[0]["stage"], attempts[0]["tier"], attempts[0]["result"]), ("code", "sonnet", "fail"))
        self.assertEqual(attempts[-1]["tier"], "fable")

        data = self.ok_json("attempt", "12", "breaker", "fail", "--json")
        self.assertEqual((data["action"], data["failures"], data["tier"]), ("done", 1, "sonnet"))

    def test_escalation_starts_from_the_overridden_tier(self):
        """#175: HARNESS_CODE_TIER in the main checkout's .claude/settings.json is the tier `start` reports and the
        cache holds, and `attempt` escalates from it, not from the labels' route (opus xhigh here). #239: the override
        replaces the labels' effort too, so opus escalates to opus xhigh and sonnet to opus."""
        hard = ("task", "status:ready", "size:L", "risk:high")
        self.scenario(issues=[issue(labels=hard), issue(number=13, labels=hard)])
        settings = os.path.join(self.repo, ".claude", "settings.json")
        for number, setting, escalated in ((12, "opus", "opus xhigh"), (13, "sonnet", "opus")):
            with self.subTest(setting=setting):
                write(settings, json.dumps({"env": {"HARNESS_CODE_TIER": setting}}))
                data = self.ok_json("start", str(number), "--json")
                self.assertEqual(data["tier"], setting)
                self.assertEqual(self.read_json(self.cache_path(number))["tier"], setting)
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(), "retry")
                self.assertEqual(self.ok("attempt", str(number), "code", "fail").stdout.strip(),
                                 f"escalate {escalated}")
                attempts = self.read_json(self.cache_path(number))["attempts"]
                self.assertEqual([(a["stage"], a["tier"], a["result"]) for a in attempts],
                                 [("code", setting, "fail")] * 2)

    def test_attempt_without_a_cache_says_to_start(self):
        self.assertOneLineError(self.work("attempt", "12", "code", "fail"), "harness-work start 12")

    def test_fix_stage_allows_two_rounds_then_park(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["fix", "fail"], "retry"),
            (["fix", "fail"], "park"),                # two rounds at most, never an escalation
            (["fix", "fail"], "park"),
            (["fix", "pass"], "done"),
            (["fix", "fail"], "retry"),                      # a pass ends the run of fix failures
            (["fix", "fail", "--tier", "opus"], "park"),
            (["fix", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "fable"], "retry"),
            (["fix", "fail", "--tier", "fable"], "park"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
        self.assertEqual([a["stage"] for a in attempts], ["fix"] * len(sequence))
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[3]["tier"], attempts[3]["result"]), ("sonnet", "pass"))
        self.assertEqual((attempts[5]["tier"], attempts[5]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-1]["tier"], attempts[-1]["result"]), ("fable", "fail"))

        # --json on a fresh run: the tier never moves and no next tier is named
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        for failures, action in ((1, "retry"), (2, "park"), (3, "park")):
            with self.subTest(json_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--json")
                self.assertEqual((data["stage"], data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 ("fix", action, failures, "sonnet", None))
        data = self.ok_json("attempt", "12", "fix", "pass", "--json")
        self.assertEqual((data["action"], data["failures"], data["next_tier"]), ("done", 0, None))
        for failures, action in ((1, "retry"), (2, "park")):
            with self.subTest(json_opus_failures=failures):
                data = self.ok_json("attempt", "12", "fix", "fail", "--tier", "opus", "--json")
                self.assertEqual((data["action"], data["failures"], data["tier"], data["next_tier"]),
                                 (action, failures, "opus", None))

    def test_breaker_failure_answers_done(self):
        """#375: one breaker round; a failure goes to code with the gaps as requirements. A breaker fail answers
        done at any tier and however often it repeats: never retry, escalate or park. The breaker's
        failures do not count for code."""
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["breaker", "fail"], "done"),
            (["breaker", "fail"], "done"),
            (["breaker", "fail"], "done"),
            (["breaker", "pass"], "done"),
            (["breaker", "fail"], "done"),
            (["tests", "pass"], "done"),
            (["breaker", "fail", "--tier", "opus"], "done"),
            (["code", "fail"], "retry"),                     # the breaker's failures do not count for code
            (["breaker", "pass", "--tier", "opus"], "done"),
            (["breaker", "fail", "--tier", "opus"], "done"),
            (["breaker", "fail", "--tier", "opus", "--effort", "high"], "done"),
            (["breaker", "pass", "--tier", "fable"], "done"),
            (["breaker", "fail", "--tier", "fable"], "done"),
            (["breaker", "fail", "--tier", "fable"], "done"),
            (["breaker", "fail", "--tier", "sonnet"], "done"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual(len(attempts), len(sequence))
        self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})     # the record is unchanged
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])
        self.assertEqual((attempts[0]["tier"], attempts[0]["result"]), ("sonnet", "fail"))
        self.assertEqual((attempts[6]["tier"], attempts[6]["result"]), ("opus", "fail"))
        self.assertEqual((attempts[-2]["tier"], attempts[-2]["result"]), ("fable", "fail"))

        # --json on a fresh run, at each tier: the tier never moves and no next tier (or effort) is named
        for tier in ("sonnet", "opus", "fable"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            for failures in (1, 2, 3):
                with self.subTest(json_tier=tier, json_failures=failures):
                    data = self.ok_json("attempt", "12", "breaker", "fail", "--tier", tier, "--json")
                    self.assertEqual((data["stage"], data["result"], data["action"], data["failures"], data["tier"],
                                      data["next_tier"], data["next_effort"]),
                                     ("breaker", "fail", "done", failures, tier, None, None))

    def test_ci_and_tests_answer_by_the_default_rule_and_count_apart(self):
        """`ci` and `tests` are answered by the default rule: retry, escalate, park; a pass ends the run.
        Their failures count apart, and neither counts for code."""
        sequence = [
            (["fail"], "retry"),
            (["fail"], "escalate opus"),
            (["fail"], "park"),
            (["fail"], "park"),
            (["pass"], "done"),
            (["fail"], "retry"),
            (["fail", "--tier", "opus"], "escalate opus xhigh"),
            (["pass", "--tier", "opus"], "done"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "retry"),
            (["fail", "--tier", "opus", "--effort", "xhigh"], "park"),
            (["pass", "--tier", "fable"], "done"),
            (["fail", "--tier", "fable"], "retry"),
            (["fail", "--tier", "fable"], "park"),
        ]
        answers = {}
        for stage in ("ci", "tests"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            answers[stage] = []
            for argv, expected in sequence:
                with self.subTest(stage=stage, argv=argv):
                    out = self.ok("attempt", "12", stage, *argv).stdout.strip()
                    answers[stage].append(out)
                    self.assertEqual(out, expected)
            attempts = self.read_json(self.cache_path())["attempts"]
            self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(stage, argv[0]) for argv, _ in sequence])
            self.assertEqual(set(attempts[0]), {"stage", "tier", "effort", "result", "at"})
        self.assertEqual(answers["tests"], answers["ci"])

        # --json: tests' escalation names the next tier as ci's does
        for stage in ("ci", "tests"):
            write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
            first = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((first["stage"], first["action"], first["failures"], first["next_tier"]),
                             (stage, "retry", 1, None))
            second = self.ok_json("attempt", "12", stage, "fail", "--json")
            self.assertEqual((second["stage"], second["action"], second["failures"], second["tier"],
                              second["next_tier"]), (stage, "escalate", 2, "sonnet", "opus"))

        # interleaved: each stage keeps its own run
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        interleaved = [
            (["tests", "fail"], "retry"),
            (["ci", "fail"], "retry"),                       # tests' failure is not ci's
            (["tests", "fail"], "escalate opus"),           # ci's failure did not reset verify
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["ci", "pass"], "done"),
            (["tests", "fail"], "park"),             # a ci pass does not end tests' run
            (["ci", "fail"], "retry"),
            (["tests", "pass"], "done"),
            (["ci", "fail"], "escalate opus"),               # a tests pass does not end ci's run
            (["tests", "fail"], "retry"),
            (["ci", "fail"], "park"),
            (["tests", "fail", "--tier", "opus"], "escalate opus xhigh"),
        ]
        for argv, expected in interleaved:
            with self.subTest(interleaved=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts],
                         [(argv[0], argv[1]) for argv, _ in interleaved])

    def test_fix_and_review_failures_count_apart(self):
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": []}))
        sequence = [
            (["review", "fail", "--tier", "opus"], "retry"),
            (["fix", "fail"], "retry"),                      # review's failure is not fix's
            (["review", "fail", "--tier", "opus"], "escalate opus xhigh"),   # fix's failure did not reset review
            (["fix", "fail"], "park"),                # review's failures did not advance or reset fix
            (["code", "fail"], "retry"),                     # nor does either count for code
            (["review", "fail", "--tier", "fable"], "park"),
            (["fix", "pass"], "done"),
            (["review", "fail", "--tier", "fable"], "park"),     # a fix pass does not end review's run
            (["fix", "fail", "--tier", "opus"], "retry"),
            (["review", "pass", "--tier", "fable"], "done"),
            (["fix", "fail", "--tier", "opus"], "park"),         # a review pass does not end fix's run
            (["review", "fail", "--tier", "opus"], "retry"),
        ]
        for argv, expected in sequence:
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("attempt", "12", *argv).stdout.strip(), expected)
        attempts = self.read_json(self.cache_path())["attempts"]
        self.assertEqual([(a["stage"], a["result"]) for a in attempts], [(argv[0], argv[1]) for argv, _ in sequence])

    def test_an_unknown_stage_is_refused(self):
        attempts = [{"stage": "code", "tier": "sonnet", "result": "fail", "at": "2026-10-01T00:00:00Z"}]
        write(self.cache_path(), json.dumps({"number": 12, "title": TITLE, "tier": "sonnet", "attempts": attempts}))
        for stage in ("deploy", "fixes", "Fix"):
            with self.subTest(stage=stage):
                p = self.work("attempt", "12", stage, "fail")
                self.assertOneLineError(p, stage, "fix")     # the refusal names the bad stage and the valid ones
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], attempts)

    def test_attempt_final_failure_answers_park(self):
        """#387: a stage's final failure answers park (text `park`, --json action "park", no next tier or effort)
        where it answered needs-input: fix's second failure, any stage's third, and a second failure with nothing above
        it (opus xhigh, or an explicit fable). Every other answer is unchanged. The first row runs as a real process,
        the rest in process."""
        def expected(stage, tier, effort, failures):
            """The policy, from the ticket: retry first; fix never escalates; the third failure is final; the second
            escalates one step (sonnet to opus, opus to opus xhigh) or parks when nothing is above."""
            if failures == 1:
                return "retry", None, None
            if stage == "fix" or failures >= 3:
                return "park", None, None
            nxt = escalation_of(tier, effort)
            return ("escalate",) + nxt if nxt else ("park", None, None)

        def answer_of(action, next_tier, next_effort):
            return " ".join(x for x in (action, next_tier, next_effort) if x)

        first = [True]

        def attempt(n, *argv):
            args = ["attempt", str(n), *argv]
            if first[0]:
                first[0] = False
                p = self.work(*args)
            else:
                p = gitrepo_support.run_in_process(TOOL, args, cwd=self.repo, env=self.env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return p.stdout

        tiers = [("sonnet", None), ("opus", None), ("opus", "xhigh"), ("fable", None)]
        seen = set()
        n = 500
        for stage in ("tests", "code", "review", "fix", "ci"):
            for tier, effort in tiers:
                flags = ["--tier", tier] + (["--effort", effort] if effort else [])
                for output in ("text", "json"):
                    n += 1
                    # an S/low task: its code and review routes name no effort, so the flags alone decide
                    write(self.cache_path(n), json.dumps({
                        "number": n, "title": TITLE, "type": "task", "size": "S", "risk": "low", "flags": [],
                        "labels": ["task", "status:in-progress", "size:S", "risk:low"], "tier": "sonnet",
                        "attempts": []}))
                    for failures in (1, 2, 3):
                        want = expected(stage, tier, effort, failures)
                        with self.subTest(stage=stage, tier=tier, effort=effort, failures=failures, output=output):
                            if output == "text":
                                self.assertEqual(attempt(n, stage, "fail", *flags).strip(), answer_of(*want))
                            else:
                                data = json.loads(attempt(n, stage, "fail", *flags, "--json"))
                                self.assertEqual((data["action"], data["next_tier"], data["next_effort"],
                                                  data["failures"], data["tier"], data["effort"]),
                                                 want + (failures, tier, effort))
                        seen.add(want[0])
        self.assertEqual(seen, {"retry", "escalate", "park"})


class AttemptEffort(WorkTest):
    """Ticket #239: `harness-work attempt` knows the effort a stage ran at (`--effort`, else the ticket's own route
    for that stage) and never escalates to Fable: sonnet goes to opus (`escalate opus`), opus to opus at xhigh
    (`escalate opus xhigh`), and a stage already at opus xhigh (or max), or on an explicit fable, goes to park
    on its second failure. --json carries `effort` and `next_effort` beside `tier` and `next_tier`. WorkTest.env drops
    every HARNESS_* variable of the real environment."""

    SPREAD = [("task", "S", "low"), ("bug", "S", "low"), ("task", "M", "low"), ("chore", "M", "high"),
              ("task", "L", "low"), ("refactor", "L", "high"), ("task", "S", "high"), ("task", None, None),
              ("bug", "L", None)]

    def put_ticket(self, number, kind, size, risk, tier=None):
        """A ticket cache as `harness-board show` writes it: labels, type, size, risk and the effective tier."""
        write(self.cache_path(number), json.dumps({
            "number": number, "title": TITLE, "type": kind, "size": size, "risk": risk, "flags": [],
            "labels": ticket_labels(kind, size, risk), "tier": tier or route_of(kind, size, risk)[0],
            "attempts": []}))

    def attempt(self, number, *argv, env=None):
        return self.ok("attempt", str(number), *argv, env=env).stdout.strip()

    def attempt_json(self, number, *argv, env=None):
        return Keys(self.ok_json("attempt", str(number), *argv, "--json", env=env))

    def attempts(self, number):
        return [Keys(a) for a in self.read_json(self.cache_path(number))["attempts"]]

    def configure(self, **env):
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    # ----------------------------------------------------------- broad: the ticket's route decides the default
    def test_the_ticket_route_decides_the_tier_and_effort_of_each_failure(self):
        seen = set()
        for i, (kind, size, risk) in enumerate(self.SPREAD):
            tier, effort = route_of(kind, size, risk)
            seen.add(escalation_text(tier, effort))
            with self.subTest(kind=kind, size=size, risk=risk, output="text"):
                n = 100 + i
                self.put_ticket(n, kind, size, risk)
                answers = [self.attempt(n, "code", "fail") for _ in range(3)]
                self.assertEqual(answers, ["retry", escalation_text(tier, effort), "park"])
                self.assertNotIn("fable", " ".join(answers))
                attempts = self.attempts(n)
                self.assertEqual([(a["stage"], a["tier"], a["effort"], a["result"]) for a in attempts],
                                 [("code", tier, effort, "fail")] * 3)
            with self.subTest(kind=kind, size=size, risk=risk, output="json"):
                n = 200 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual({k: first[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                        "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "retry", "failures": 1, "next_tier": None,
                                  "next_effort": None})
                second = self.attempt_json(n, "code", "fail")
                nxt = escalation_of(tier, effort)
                self.assertEqual({k: second[k] for k in ("tier", "effort", "action", "failures", "next_tier",
                                                         "next_effort")},
                                 {"tier": tier, "effort": effort, "action": "escalate" if nxt else "park",
                                  "failures": 2, "next_tier": nxt[0] if nxt else None,
                                  "next_effort": nxt[1] if nxt else None})
                done = self.attempt_json(n, "code", "pass")
                self.assertEqual((done["action"], done["next_tier"], done["next_effort"]), ("done", None, None))
        # the spread reaches all three answers
        self.assertEqual(seen, {"escalate opus", "escalate opus xhigh", "park"})

    def test_size_l_and_risk_high_run_at_opus_xhigh_and_go_to_park(self):
        # DoD: a failure at opus xhigh gives park; nothing prints fable. Every retrying stage alike
        # (the breaker stage never retries: its failure answers done, pinned in test_work_stage.py).
        for n, (kind, size, risk) in ((31, ("task", "L", "low")), (32, ("bug", "M", "high")),
                                      (33, ("chore", "S", "high"))):
            self.put_ticket(n, kind, size, risk)
            for stage in ("tests", "code", "ci"):
                with self.subTest(ticket=n, stage=stage):
                    self.assertEqual(self.attempt(n, stage, "fail"), "retry")
                    data = self.attempt_json(n, stage, "fail")
                    self.assertEqual((data["tier"], data["effort"], data["action"], data["next_tier"],
                                      data["next_effort"]), ("opus", "xhigh", "park", None, None))
                    self.assertEqual(self.attempt(n, stage, "fail"), "park")

    def test_a_second_failure_at_opus_says_escalate_opus_xhigh(self):
        # DoD: an M/low ticket runs at opus with no effort; its second failure escalates to opus at xhigh
        for n, kind in ((41, "task"), (42, "bug"), (43, "refactor")):
            with self.subTest(kind=kind):
                self.put_ticket(n, kind, "M", "low")
                self.assertEqual(self.attempt(n, "code", "fail"), "retry")
                self.assertEqual(self.attempt(n, "code", "fail"), "escalate opus xhigh")
                # the escalated run, recorded at opus xhigh, fails again: park
                self.assertEqual(self.attempt(n, "code", "fail", "--tier", "opus", "--effort", "xhigh"), "park")

    # ----------------------------------------------------------- explicit --tier and --effort
    def test_every_explicit_tier_and_effort(self):
        n = 300
        for tier in ("sonnet", "opus", "fable"):
            for effort in (None,) + EFFORT_LEVELS:
                flags = ["--tier", tier] + (["--effort", effort] if effort else [])
                with self.subTest(tier=tier, effort=effort):
                    n += 1
                    self.put_ticket(n, "task", "M", "low")              # code route: opus, no effort
                    first = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "code", "fail", *flags)
                    self.assertEqual((second["tier"], second["effort"]), (tier, effort))
                    nxt = escalation_of(tier, effort)
                    if tier == "sonnet":
                        self.assertEqual((second["action"], second["next_tier"]), ("escalate", "opus"))
                    else:
                        self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                         ("escalate" if nxt else "park", nxt[0] if nxt else None,
                                          nxt[1] if nxt else None))
                    attempts = self.attempts(n)
                    self.assertEqual([(a["tier"], a["effort"]) for a in attempts], [(tier, effort)] * 2)
                    n += 1
                    self.put_ticket(n, "task", "M", "low")
                    self.assertEqual(self.attempt(n, "code", "fail", *flags), "retry")
                    text = self.attempt(n, "code", "fail", *flags)
                    self.assertNotIn("fable", text)
                    if tier == "sonnet":
                        self.assertTrue(text.startswith("escalate opus"), text)
                    else:
                        self.assertEqual(text, escalation_text(tier, effort))
                    if tier == "sonnet" and effort is None:
                        self.assertEqual(text, "escalate opus")

    def test_an_explicit_tier_that_matches_the_stage_route_takes_its_effort(self):
        # Without --effort, an attempt at the tier its stage routes to ran at that route's effort; at another tier it
        # ran at none. The review stage's route is the review's, not the coder's.
        cases = [
            # (labels, stage, flags, second answer)
            (("task", "L", "low"), "code", ["--tier", "opus"], "park"),            # code route: opus xhigh
            (("task", "L", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),  # review route: opus
            (("task", "M", "high"), "review", ["--tier", "opus"], "park"),        # review route: opus xhigh
            (("task", "M", "high"), "code", ["--tier", "opus"], "park"),
            (("task", "M", "low"), "review", ["--tier", "opus"], "escalate opus xhigh"),
            (("task", "L", "low"), "code", ["--tier", "sonnet"], "escalate opus"),       # not the route's tier
            (("task", "L", "low"), "code", ["--tier", "opus", "--effort", "high"], "escalate opus xhigh"),
            (("task", "M", "high"), "review", ["--tier", "fable"], "park"),
        ]
        for i, ((kind, size, risk), stage, flags, want) in enumerate(cases):
            with self.subTest(labels=(kind, size, risk), stage=stage, flags=flags):
                n = 400 + i
                self.put_ticket(n, kind, size, risk)
                self.assertEqual(self.attempt(n, stage, "fail", *flags), "retry")
                self.assertEqual(self.attempt(n, stage, "fail", *flags), want)

    def test_effort_takes_the_five_levels_and_refuses_anything_else(self):
        self.put_ticket(12, "task", "M", "low")
        for bad in ("extra", "XHIGHER", "5"):
            with self.subTest(bad=bad):
                p = self.work("attempt", "12", "code", "fail", "--effort", bad)
                self.assertOneLineError(p, bad)
                self.assertEqual(p.stdout, "")
        self.assertEqual(self.read_json(self.cache_path())["attempts"], [])
        for level in EFFORT_LEVELS:
            with self.subTest(level=level):
                data = self.attempt_json(12, "code", "pass", "--effort", level)
                self.assertEqual((data["effort"], data["action"]), (level, "done"))
        self.assertEqual([a["effort"] for a in self.attempts(12)], list(EFFORT_LEVELS))

    # ----------------------------------------------------------- settings and stale caches
    def test_a_pre_239_cache_that_says_fable_never_escalates_to_fable(self):
        # A cache written before #239 holds the old rubric's tier (fable for size:L or risk:high). The labels decide
        # now: opus at xhigh, and park on the second failure.
        for n, (kind, size, risk) in ((51, ("task", "L", "high")), (52, ("task", "M", "high")),
                                      (53, ("bug", "L", "low"))):
            with self.subTest(kind=kind, size=size, risk=risk):
                self.put_ticket(n, kind, size, risk, tier="fable")
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), ("opus", "xhigh"))
                self.assertEqual(self.attempt(n, "code", "fail"), "park")
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))

    def test_explicit_fable_from_the_settings_still_runs_fable_and_tops_out(self):
        for n, how in ((61, "settings.json"), (62, "environment")):
            with self.subTest(how=how):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="fable")
                else:
                    env = self.env(HARNESS_CODE_TIER="fable")
                self.put_ticket(n, "task", "M", "low", tier="fable")
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual(first["tier"], "fable")
                second = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((second["tier"], second["action"], second["next_tier"]),
                                 ("fable", "park", None))

    def test_the_code_effort_setting_is_the_effort_a_stage_ran_at(self):
        # HARNESS_CODE_EFFORT wins over the labels' xhigh, as it does for `harness-board tier`
        cases = [("low", ("task", "L", "low"), "escalate opus xhigh"),
                 ("high", ("bug", "M", "high"), "escalate opus xhigh"),
                 ("max", ("task", "M", "low"), "park"),
                 ("xhigh", ("task", "M", "low"), "park"),
                 ("medium", ("task", "S", "low"), "escalate opus")]
        for i, (level, (kind, size, risk), want) in enumerate(cases):
            with self.subTest(level=level, labels=(kind, size, risk)):
                self.configure(HARNESS_CODE_EFFORT=level)
                n = 70 + i
                self.put_ticket(n, kind, size, risk)
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (route_of(kind, size, risk)[0], level))
                self.assertEqual(self.attempt(n, "code", "fail"), want)

    def test_a_code_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_CODE_TIER replaces the labels' tier and their effort. A size:L ticket under
        # HARNESS_CODE_TIER=opus runs at opus with no effort, so its second failure escalates to opus xhigh.
        for n, how, (kind, size, risk) in ((91, "settings.json", ("task", "L", "low")),
                                           (92, "environment", ("bug", "L", "high")),
                                           (93, "settings.json", ("task", "M", "high"))):
            with self.subTest(how=how, labels=(kind, size, risk)):
                self.setUp()
                env = None
                if how == "settings.json":
                    self.configure(HARNESS_CODE_TIER="opus")
                else:
                    env = self.env(HARNESS_CODE_TIER="opus")
                self.put_ticket(n, kind, size, risk, tier="opus")       # the effective tier harness-board writes
                first = self.attempt_json(n, "code", "fail", env=env)
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", None, "retry"))
                self.assertEqual(self.attempt(n, "code", "fail", env=env), "escalate opus xhigh")
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", None)] * 2)
                # HARNESS_CODE_EFFORT set beside it is the effort the stage ran at
                self.put_ticket(n + 10, kind, size, risk, tier="opus")
                env2 = self.env(HARNESS_CODE_TIER="opus", HARNESS_CODE_EFFORT="xhigh")
                self.assertEqual(self.attempt_json(n + 10, "code", "fail", env=env2)["effort"], "xhigh")
                self.assertEqual(self.attempt(n + 10, "code", "fail", env=env2), "park")

    def test_a_review_tier_override_drops_the_labels_xhigh(self):
        # Ruling: HARNESS_REVIEW_TIER replaces the labels' review rule, their xhigh included. A risk:high review
        # recorded at `--tier opus` ran at opus xhigh by the labels (park on the second failure), but at opus
        # with no effort under HARNESS_REVIEW_TIER=opus (escalate opus xhigh). HARNESS_REVIEW_TIER=fable runs at
        # fable xhigh and tops out.
        cases = [
            (None, "opus", "xhigh", "park"),
            ({"HARNESS_REVIEW_TIER": "opus"}, "opus", None, "escalate opus xhigh"),
            ({"HARNESS_REVIEW_TIER": "fable"}, "fable", "xhigh", "park"),
        ]
        for i, (settings, tier, effort, want) in enumerate(cases):
            for where in (("settings.json", "environment") if settings else (None,)):
                with self.subTest(settings=settings, where=where):
                    self.setUp()
                    env = None
                    if where == "settings.json":
                        self.configure(**settings)
                    elif where == "environment":
                        env = self.env(**settings)
                    n = 95 + i
                    self.put_ticket(n, "task", "M", "high")
                    first = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    self.assertEqual((first["tier"], first["effort"], first["action"]), (tier, effort, "retry"))
                    second = self.attempt_json(n, "review", "fail", "--tier", tier, env=env)
                    nxt = escalation_of(tier, effort)
                    self.assertEqual((second["action"], second["next_tier"], second["next_effort"]),
                                     ("escalate" if nxt else "park", nxt[0] if nxt else None,
                                      nxt[1] if nxt else None))
                    self.put_ticket(n + 10, "task", "M", "high")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), "retry")
                    self.assertEqual(self.attempt(n + 10, "review", "fail", "--tier", tier, env=env), want)

    def test_a_cache_without_labels_that_says_fable_reads_as_opus_xhigh(self):
        # Agrees with `harness-board tier` on the same cache: a stored fable without labels is the pre-#239 rubric's
        # answer for size:L or risk:high, so the attempt runs at opus xhigh and the second failure is park.
        for n, stage in ((85, "code"), (86, "tests"), (87, "ci")):
            with self.subTest(stage=stage):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": "fable", "attempts": []}))
                first = self.attempt_json(n, stage, "fail")
                self.assertEqual((first["tier"], first["effort"], first["action"]), ("opus", "xhigh", "retry"))
                second = self.attempt_json(n, stage, "fail")
                self.assertEqual((second["tier"], second["effort"], second["action"], second["next_tier"],
                                  second["next_effort"]), ("opus", "xhigh", "park", None, None))
                self.assertEqual([(a["tier"], a["effort"]) for a in self.attempts(n)], [("opus", "xhigh")] * 2)
                self.assertNotIn("fable", json.dumps([dict(first), dict(second)]))
                self.assertNotIn("fable", json.dumps(self.read_json(self.cache_path(n))["attempts"]))
                write(self.cache_path(n + 10), json.dumps({"number": n + 10, "title": TITLE, "tier": "fable",
                                                           "attempts": []}))
                self.assertEqual([self.attempt(n + 10, stage, "fail") for _ in range(2)], ["retry", "park"])
        # the board agrees on the same cache
        board = os.path.join(os.path.dirname(TOOL), "harness-board")
        p = subprocess.run([sys.executable, board, "tier", "85", "--json"], cwd=self.repo, env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual((data["tier"], data["effort"]), ("opus", "xhigh"))
        # an explicit HARNESS_CODE_TIER=fable still gives fable on that cache
        write(self.cache_path(88), json.dumps({"number": 88, "title": TITLE, "tier": "fable", "attempts": []}))
        env = self.env(HARNESS_CODE_TIER="fable")
        self.assertEqual(self.attempt_json(88, "code", "fail", env=env)["tier"], "fable")
        self.assertEqual(self.attempt(88, "code", "fail", env=env), "park")

    def test_a_cache_without_labels_keeps_its_tier(self):
        # Caches written by hand or by old tools carry no labels: the cached tier stands, with no effort.
        for n, tier, want in ((81, "sonnet", "escalate opus"), (82, "opus", "escalate opus xhigh")):
            with self.subTest(tier=tier):
                write(self.cache_path(n), json.dumps({"number": n, "title": TITLE, "tier": tier, "attempts": []}))
                first = self.attempt_json(n, "code", "fail")
                self.assertEqual((first["tier"], first["effort"]), (tier, None))
                self.assertEqual(self.attempt(n, "code", "fail"), want)


class Budget(WorkTest):
    def test_status_exhaust_and_reset(self):
        self.assertEqual(self.ok("budget").stdout.splitlines(),
                         ["api: available", "cloud: available", "subscription: available"])
        p = self.ok("budget", "exhaust", "cloud", "--reason", "ultrareview: out of credits", "--hours", "2")
        self.assertRegex(p.stdout.strip(), r"^cloud: exhausted until \S+Z \(ultrareview: out of credits\)$")
        saved = self.read_json(self.budgets_path())
        self.iso_close_to(saved["cloud"]["exhausted_until"], dt.timedelta(hours=2))
        self.assertEqual(saved["cloud"]["reason"], "ultrareview: out of credits")

        status = self.ok_json("budget", "status", "--json")
        self.assertTrue(status["cloud"]["exhausted"])
        self.assertFalse(status["api"]["exhausted"])

        self.assertEqual(self.ok("budget", "reset", "cloud").stdout.strip(), "cloud: available")
        self.assertIsNone(self.read_json(self.budgets_path())["cloud"]["exhausted_until"])

        self.write_budget("api", -1)                         # a time already past means available
        self.assertIn("api: available", self.ok("budget").stdout)
        p = self.work("budget", "exhaust", "api")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--reason", p.stderr)


# =========================================================================== worktrees
class Worktrees(WorkTest):
    def add_worktree(self, branch):
        path = self.worktree_path(branch)
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, path, "origin/main")
        return path

    def test_lists_ticket_worktrees_with_age_pr_state_and_stale_flag(self):
        wt = self.add_worktree("t/12-x")
        self.git(self.repo, "worktree", "add", "-q", "-b", "elsewhere", os.path.join(self.tmp, "elsewhere"), "main")

        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual((row["path"], row["branch"], row["pr"], row["dirty"]), (wt, "t/12-x", None, False))
        self.assertGreater(row["age_days"], 9.9)
        self.assertTrue(row["stale"])
        # #447: the branch's newest pull request in any state, through REST (GET .../pulls?head=OWNER:t/12-x)
        self.assertIn(("t/12-x", "all"), fakegh.pull_queries(self.gh_entries()))
        self.assertFalse(self.ok_json("worktrees", "--stale", "30", "--json")[0]["stale"])
        text = self.ok("worktrees").stdout
        self.assertIn("t/12-x", text)
        self.assertIn("stale", text)

        self.scenario(prs=[{"number": 7, "state": "OPEN", "isDraft": True}])
        [row] = self.ok_json("worktrees", "--json")
        self.assertEqual(row["pr"], {"number": 7, "state": "OPEN", "draft": True})
        self.assertFalse(row["stale"])

        self.scenario(prs=[{"number": 7, "state": "MERGED", "isDraft": False}])
        self.assertTrue(self.ok_json("worktrees", "--json")[0]["stale"])

    def test_remove_refuses_a_dirty_worktree_without_discard(self):
        clean = self.add_worktree("t/12-x")
        dirty = self.add_worktree("t/13-y")
        write(os.path.join(clean, ".env"), "KEY=1\n")       # ignored files do not make a tree dirty
        write(os.path.join(dirty, "README.md"), "changed\n")

        p = self.ok("worktree", "remove", "t/12-x")
        self.assertIn(f"removed worktree {clean}", p.stdout)
        self.assertIn("deleted branch t/12-x", p.stdout)
        self.assertFalse(os.path.exists(clean))

        p = self.work("worktree", "remove", "t/13-y")
        self.assertOneLineError(p, "README.md", "--discard")
        self.assertTrue(os.path.isfile(os.path.join(dirty, "README.md")))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/13-y"), "t/13-y")

        p = self.ok("worktree", "remove", "t/13-y", "--discard")
        self.assertIn("deleted branch t/13-y", p.stdout)
        self.assertFalse(os.path.exists(dirty))
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", "t/*"), "")

    def test_remove_refuses_main(self):
        self.assertOneLineError(self.work("worktree", "remove", "main"), "refusing")


# =========================================================================== start: fast-forward (#58)
class StartFastForward(WorkTest):
    def started_and_pushed(self):
        self.scenario(issues=[issue()])
        wt = self.ok_json("start", "12", "--json")["worktree"]
        self.git(wt, "push", "-q", "-u", "origin", BRANCH)
        return wt

    def elsewhere(self):
        """A second clone on the branch: where the cloud session pushes from."""
        other = self.other_clone()
        self.git(other, "switch", "-q", BRANCH)
        return other

    def commit(self, clone, name, push=True):
        write(os.path.join(clone, name), name + "\n")
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", name)
        if push:
            self.git(clone, "push", "-q", "origin", BRANCH)
        return self.git(clone, "rev-parse", "HEAD")

    def branch_warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:") and BRANCH in ln]

    def test_a_clean_worktree_behind_origin_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        p = self.ok("start", "12")                                # up to date: nothing to say
        self.assertEqual(self.branch_warnings(p), [])
        other = self.elsewhere()
        for name in ("CLOUD1.md", "CLOUD2.md"):
            head = self.commit(other, name)
            p = self.ok("start", "12")
            self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
            self.assertTrue(os.path.isfile(os.path.join(wt, name)))
            self.assertEqual(self.branch_warnings(p), [])
            self.assertEqual(self.git(wt, "status", "--porcelain"), "")

    def test_a_reused_local_branch_without_a_worktree_is_fast_forwarded(self):
        wt = self.started_and_pushed()
        self.git(self.repo, "worktree", "remove", wt)
        head = self.commit(self.elsewhere(), "CLOUD.md")
        data = self.ok_json("start", "12", "--json")
        self.assertEqual(data["worktree"], wt)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.repo, "rev-parse", BRANCH), head)

    def test_a_diverged_branch_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        local = self.commit(wt, "LOCAL.md", push=False)
        p = self.ok("start", "12")                                # only ahead: nothing to say, nothing changes
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.commit(self.elsewhere(), "CLOUD.md")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), local)
        self.assertFalse(os.path.exists(os.path.join(wt, "CLOUD.md")))

    def test_a_dirty_worktree_behind_origin_gets_one_warning_and_is_left_alone(self):
        wt = self.started_and_pushed()
        before = self.git(wt, "rev-parse", "HEAD")
        head = self.commit(self.elsewhere(), "CLOUD.md")
        write(os.path.join(wt, "README.md"), "# proj\nedited here\n")
        p = self.ok("start", "12")
        self.assertEqual(len(self.branch_warnings(p)), 1, p.stderr)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), before)
        self.assertEqual(read_text(os.path.join(wt, "README.md")), "# proj\nedited here\n")
        self.git(wt, "checkout", "--", "README.md")              # clean again: the next start fast-forwards
        p = self.ok("start", "12")
        self.assertEqual(self.branch_warnings(p), [])
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), head)


class Waves(WorkTest):
    """#180: with HARNESS_WAVE_REVIEW set, dispatch records the tickets one call launched as a wave
    (harness_lib/waves.py) and stamps each run record with its id; `waves` shows where each wave stands from the
    ticket caches, `--refresh` asking gh once per waiting ticket; `wave drop` takes a stuck ticket out."""

    def waves_dir(self):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key, "waves")

    def wave_files(self):
        folder = self.waves_dir()
        if not os.path.isdir(folder):
            return []
        return [self.read_json(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if n.endswith(".json")]

    def write_wave(self, wave_id, tickets, review=None, created_at="2026-10-20T10:00:00Z", dropped=()):
        wave = {"id": wave_id, "repo": os.path.realpath(self.repo), "created_at": created_at,
                "tickets": list(tickets), "dropped": list(dropped), "tier": "fable", "effort": "xhigh",
                "review": review}
        write(os.path.join(self.waves_dir(), wave_id + ".json"), json.dumps(wave))
        return wave

    def write_cache(self, n, status):
        write(self.cache_path(n), json.dumps({"number": n, "title": f"Ticket {n}", "status": status}))

    def wave_env(self, tier="fable"):
        return self.env(HARNESS_WAVE_REVIEW=tier)

    def test_dispatch_groups_the_launched_tickets_into_one_wave(self):
        self.scenario(issues=[issue(), issue(13, title="Second")], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        [wave] = self.wave_files()
        self.assertRegex(wave["id"], WAVE_ID_RE)
        self.assertEqual((wave["tickets"], wave["dropped"], wave["tier"], wave["effort"], wave["review"]),
                         ([12, 13], [], "fable", "xhigh", None))
        self.assertEqual(wave["repo"], os.path.realpath(self.repo))
        self.assertIn(f"wave {wave['id']}: #12, #13 (reviewed together once all are in review)",
                      p.stdout.splitlines())
        records = self.run_records()
        self.assertEqual(sorted(r["ticket"] for r in records), [12, 13])
        for record in records:
            self.assertEqual(record["wave"], wave["id"])
        for n in (12, 13):    # no wave refresh read: harness-work's start read and harness-board's show and status
            self.assertLessEqual(self.issue_reads(n), 3, self.gh_calls())

    def test_dispatch_json_names_the_wave(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env("opus"))
        [wave] = self.wave_files()
        self.assertEqual(data["wave"], wave["id"])
        self.assertEqual((wave["tier"], wave["effort"]), ("opus", "xhigh"))
        self.assertEqual([r["wave"] for r in data["launched"]], [wave["id"]])

    def test_without_the_setting_no_wave_is_written(self):
        for value in (None, "off"):
            with self.subTest(HARNESS_WAVE_REVIEW=value):
                self.setUp()
                self.scenario(issues=[issue(), issue(13, title="Second")],
                              ready=[ready_row(12), ready_row(13, "Second")])
                env = self.env() if value is None else self.env(HARNESS_WAVE_REVIEW=value)
                p = self.ok("dispatch", env=env)
                self.assertFalse(os.path.exists(self.waves_dir()))
                self.assertNotIn("wave", p.stdout)
                self.assertEqual(sorted(p.stdout.splitlines()),
                                 sorted([f"#12 bg on {BRANCH} (id abc123)",
                                         "#13 bg on t/13-second (id abc123)"]))
                for record in self.run_records():
                    self.assertNotIn("wave", record)
                self.setUp()
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                data = self.ok_json("dispatch", "--json", env=env)
                self.assertNotIn("wave", data)
                self.assertNotIn("wave", data["launched"][0])
                dry = self.ok_json("dispatch", "--dry-run", "--json", env=env)
                self.assertNotIn("wave", dry)
                self.assertNotIn("group", self.ok("dispatch", "--dry-run", env=env).stdout)

    def test_a_redispatched_ticket_keeps_its_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Second")], ready=[ready_row(13, "Second")])
        p = self.ok("dispatch", env=self.wave_env())
        waves = {w["id"]: w for w in self.wave_files()}
        self.assertEqual(len(waves), 2, waves)
        self.assertEqual(waves[old["id"]]["tickets"], [12])             # untouched
        [new] = [w for w in waves.values() if w["id"] != old["id"]]
        self.assertEqual(new["tickets"], [13])                           # 12 joins no new wave
        self.assertIn(f"wave {new['id']}: #13 (reviewed together once all are in review)", p.stdout.splitlines())
        launched = {r["ticket"]: r for r in self.run_records() if r.get("ended_at") is None}
        self.assertEqual(launched[12]["wave"], old["id"])
        self.assertEqual(launched[13]["wave"], new["id"])

    def test_a_dispatch_of_only_redispatched_tickets_makes_no_wave(self):
        old = self.write_wave("w20261020100000", [12])
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        self.assertEqual([w["id"] for w in self.wave_files()], [old["id"]])
        self.assertIsNone(data["wave"])
        self.assertEqual([r["wave"] for r in data["launched"]], [old["id"]])

    def test_a_ticket_whose_wave_was_reviewed_joins_a_new_one(self):
        old = self.write_wave("w20261020100000", [12], review={"launched_at": "2026-10-21T10:00:00Z", "run": "r1",
                                                                "reviewed_at": "2026-10-21T11:00:00Z"})
        self.write_run(12, pid=self.dead_pid(), branch=BRANCH, started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        data = self.ok_json("dispatch", "--json", env=self.wave_env())
        [new] = [w for w in self.wave_files() if w["id"] != old["id"]]
        self.assertEqual((data["wave"], new["tickets"]), (new["id"], [12]))

    def test_dry_run_names_the_grouping_and_writes_nothing(self):
        self.write_wave("w20261020100000", [14])
        self.write_run(14, pid=self.dead_pid(), branch="t/14-x", started_at=iso_ago(hours=2),
                       ended_at=iso_ago(hours=1), outcome="early")
        self.scenario(issues=[issue(), issue(13, title="Second"), issue(14, labels=IN_PROGRESS, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        before = self.wave_files()
        p = self.ok("dispatch", "--dry-run", env=self.wave_env())
        self.assertIn("would group #12, #13 into a wave", p.stdout.splitlines())
        self.assertIn("would start #14 Third (again after early) and launch bg", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json", env=self.wave_env())
        self.assertEqual(data["wave"], {"id": None, "tickets": [12, 13]})
        self.assertEqual(self.wave_files(), before)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)

    def test_waves_lists_progress_from_the_caches_without_gh(self):
        self.write_wave("w20261020100000", [12, 13], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [14], created_at="2026-10-20T11:00:00Z")
        self.write_wave("w20261020120000", [15], created_at="2026-10-20T12:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        self.write_wave("w20261020130000", [16], created_at="2026-10-20T13:00:00Z",
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-78",
                                "reviewed_at": "2026-10-21T11:30:00Z"})
        self.write_wave("w20261020140000", [17], created_at="2026-10-20T14:00:00Z")   # no ticket cache
        for n, status in ((12, "in-review"), (13, "in-progress"), (14, "in-review"), (15, "in-review"),
                          (16, "closed")):
            self.write_cache(n, status)
        text = self.ok("waves").stdout
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "TICKETS", "STATE", "DETAIL"])
        rows = {ln.split()[0]: ln for ln in lines[1:]}
        self.assertEqual(sorted(rows), ["w20261020100000", "w20261020110000", "w20261020120000",
                                        "w20261020140000"])          # 16 is closed: hidden without --all
        self.assertIn("#12, #13", rows["w20261020100000"])
        self.assertIn("working", rows["w20261020100000"])
        self.assertIn("1 of 2 in review; waiting for #13 (in-progress)", rows["w20261020100000"])
        self.assertIn("due", rows["w20261020110000"])
        self.assertIn("reviewing (run run-77)", rows["w20261020120000"])
        self.assertIn("waiting for #17 (", rows["w20261020140000"])
        every = {ln.split()[0]: ln for ln in self.ok("waves", "--all").stdout.splitlines()[1:]}
        self.assertIn("reviewed 2026-10-21T11:30:00Z", every["w20261020130000"])
        data = self.ok_json("waves", "--json")
        first = data[0]
        self.assertEqual((first["id"], first["state"], first["done"], first["waiting"]),
                         ("w20261020100000", "working", [12], [{"number": 13, "status": "in-progress"}]))
        self.assertEqual([w["id"] for w in data], sorted(rows))
        self.assertEqual(self.gh_calls(), [])

    def test_waves_with_none_says_so(self):
        p = self.ok("waves")
        self.assertIn("No open waves", p.stdout)
        self.assertEqual(self.ok_json("waves", "--json"), [])
        self.assertEqual(self.gh_calls(), [])

    def test_waves_refresh_asks_gh_once_per_waiting_ticket(self):
        self.write_wave("w20261020100000", [12, 13, 14], created_at="2026-10-20T10:00:00Z")
        self.write_wave("w20261020110000", [13, 15], created_at="2026-10-20T11:00:00Z")
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        self.write_cache(15, "in-progress")
        self.scenario(issues=[issue(13, labels=IN_REVIEW), issue(14, labels=IN_PROGRESS),
                              issue(15, labels=IN_PROGRESS, state="CLOSED")])
        data = {w["id"]: w for w in self.ok_json("waves", "--refresh", "--json")}
        self.assertEqual({n: self.issue_reads(n) for n in (12, 13, 14, 15)}, {12: 0, 13: 1, 14: 1, 15: 1})   # #447
        self.assertEqual(data["w20261020100000"]["waiting"], [{"number": 14, "status": "in-progress"}])
        self.assertEqual(data["w20261020110000"]["state"], "due")
        self.assertEqual(len(self.gh_calls()), 3)
        self.assertEqual(self.read_json(self.cache_path(13))["status"], "in-progress")   # the caches are not written

    def test_wave_drop_makes_the_wave_due(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_cache(12, "in-review")
        self.write_cache(13, "in-progress")
        p = self.ok("wave", "drop", "w20261020100000", "13")
        self.assertEqual(p.stdout.strip(), "dropped #13 from wave w20261020100000")
        [wave] = self.wave_files()
        self.assertEqual((wave["tickets"], wave["dropped"]), ([12], [13]))
        [row] = self.ok("waves").stdout.splitlines()[1:]
        self.assertIn("due", row)
        self.assertEqual(self.gh_calls(), [])

    def test_wave_drop_refuses_an_unknown_wave_or_ticket_and_a_launched_review(self):
        self.write_wave("w20261020100000", [12, 13])
        self.write_wave("w20261020110000", [14, 15],
                        review={"launched_at": "2026-10-21T10:00:00Z", "run": "run-77", "reviewed_at": None})
        before = self.wave_files()
        self.assertOneLineError(self.work("wave", "drop", "w20261020120000", "12"), "w20261020120000")
        self.assertOneLineError(self.work("wave", "drop", "nonsense", "12"), "nonsense")
        self.assertOneLineError(self.work("wave", "drop", "w20261020100000", "14"), "#14", "w20261020100000")
        self.assertOneLineError(self.work("wave", "drop", "w20261020110000", "14"), "w20261020110000", "launched")
        self.assertEqual(self.wave_files(), before)


if __name__ == "__main__":
    unittest.main()
