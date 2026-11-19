"""harness-work dispatch --all-projects (#164): the hourly tick over the registered projects that opted in with
HARNESS_AUTODISPATCH=1, the cross-project cap, the subscription budget the sweep marks from a bg session's usage-limit
refusal and the hold it puts on bg dispatch, and the LaunchAgent that runs the tick.

The fixtures are in work_support.py (ProjectsTest: several projects, gh and claude routed by cwd).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_autodispatch.py" -v
"""

from __future__ import annotations

import plistlib
import unittest

import work_support
from work_support import *  # noqa: F401,F403


def setUpModule():
    work_support.setUpModule()
    routed_bins()


def tearDownModule():
    work_support.tearDownModule()


WOULD_START_RE = re.compile(r"would start #(\d+)\b")
BUDGET_KEYS = ("exhausted_until", "reason", "exhausted")
AVAILABLE = {"exhausted_until": None, "reason": "", "exhausted": False}


def lines_with(text, *needles):
    return [ln for ln in text.splitlines() if all(n in ln for n in needles)]


# =========================================================================== dispatch --all-projects
class DispatchAllProjects(ProjectsTest):
    """alpha-app ("1") and beta-app (1) opted in, gamma-app not, delta-app opted in; every tick runs from a folder in
    no repository, as launchd's job does."""

    def tick(self, *args, env=None):
        return self.work("dispatch", "--all-projects", *args, env=env or self.env(**CREDENTIALS), cwd=self.elsewhere)

    # ---- DoD 1: only opted-in projects without an open incident dispatch
    def test_only_opted_in_projects_without_an_open_incident_dispatch(self):
        self.board("alpha-app", ready=(21, 22))
        self.board("beta-app", ready=(31,))
        self.board("gamma-app", ready=(41,))
        self.board("delta-app", ready=(51,), incidents=(77,))
        # a dry run first, with HARNESS_AUTODISPATCH=1 exported: only the committed settings opt a project in
        p = self.tick("--dry-run", env=self.env(**dict(CREDENTIALS, **{AUTODISPATCH: "1"})))
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(sorted(int(n) for n in WOULD_START_RE.findall(p.stdout)), [21, 22, 31], out)
        self.assertEqual(self.launches(), [], out)

        p = self.tick()
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(sorted(self.launches()), [("alpha-app", 21), ("alpha-app", 22), ("beta-app", 31)], out)
        for name in ("gamma-app", "delta-app"):
            self.assertEqual(self.started_in(name), [], f"{name}: {out}")
        self.assertEqual(sorted((self.project_of(r["repo"]), r["ticket"]) for r in self.run_records()),
                         [("alpha-app", 21), ("alpha-app", 22), ("beta-app", 31)])
        # one line for each skipped project, naming it and why
        gamma = lines_with(out, "gamma-app")
        self.assertEqual(len(gamma), 1, out)
        self.assertIn("opted in", gamma[0].lower(), out)
        delta = lines_with(out, "delta-app")
        self.assertEqual(len(delta), 1, out)
        self.assertIn("incident", delta[0].lower(), out)
        self.assertIn("#77", delta[0], out)

    def test_opt_in_is_autodispatch_1_in_the_settings_committed_at_origin_main(self):
        # (case, how gamma-app's settings change, opted in)
        cases = (
            ("the string 1, committed", ("commit", {AUTODISPATCH: "1"}), True),
            ("the number 1, committed", ("commit", {AUTODISPATCH: 1}), True),
            ("the string 0, committed", ("commit", {AUTODISPATCH: "0"}), False),
            ("true, committed", ("commit", {AUTODISPATCH: "true"}), False),
            ("absent", ("commit", {"HARNESS_TEST_CMD": "pytest -q {files}"}), False),
            ("1 in the working tree only", ("worktree", {AUTODISPATCH: "1"}), False),
            ("1 in settings.local.json only", ("local", {AUTODISPATCH: "1"}), False),
        )
        for case, (how, env), opted in cases:
            with self.subTest(case=case):
                self.setUp()
                self.register("alpha-app", "gamma-app")
                self.board("alpha-app", ready=(21,))
                self.board("gamma-app", ready=(41,), incidents=(123,) if case == "the number 1, committed" else ())
                gamma = self.projects["gamma-app"]
                if how == "commit":
                    self.commit_settings("gamma-app", env)
                else:
                    name = "settings.json" if how == "worktree" else "settings.local.json"
                    write(os.path.join(gamma, ".claude", name), json.dumps({"env": env}))
                p = self.tick("--dry-run")
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                planned = sorted(int(n) for n in WOULD_START_RE.findall(p.stdout))
                gamma_lines = lines_with(out, "gamma-app")
                if case == "the number 1, committed":       # opted in, but an incident is open: skipped for it
                    self.assertEqual(planned, [21], out)
                    self.assertEqual(len(gamma_lines), 1, out)
                    self.assertIn("#123", gamma_lines[0], out)
                    self.assertIn("incident", gamma_lines[0].lower(), out)
                elif opted:
                    self.assertEqual(planned, [21, 41], out)
                else:
                    self.assertEqual(planned, [21], out)
                    self.assertEqual(len(gamma_lines), 1, out)
                    self.assertIn("opted in", gamma_lines[0].lower(), out)
                self.assertEqual(self.launches(), [], out)

    # ---- DoD 2: live runs across all projects never exceed --max (default 8) after a tick
    def test_live_runs_across_all_projects_never_exceed_max_after_a_tick(self):
        # (--max given, the cap, live runs {project: tickets}, ready tickets {project: tickets}). Live runs of the same
        # ticket number in different projects are different runs; a project that is not opted in still holds its
        # slots.
        rows = (
            ((), 8, {"alpha-app": (40,), "beta-app": (40,), "gamma-app": (40,)},
             {"alpha-app": (21, 22, 23, 24), "beta-app": (31, 32, 33)}),
            (("--max", "4"), 4, {"alpha-app": (40,)}, {"alpha-app": (21, 22), "beta-app": (31, 32, 33)}),
            (("--max", "3"), 3, {"alpha-app": (40,), "beta-app": (41,), "gamma-app": (42,)},
             {"alpha-app": (21,), "beta-app": (31,)}),
            ((), 8, {}, {"alpha-app": (21, 22), "beta-app": (31,)}),
        )
        for given, cap, live, ready in rows:
            with self.subTest(max=given, live=live):
                self.setUp()
                self.register("alpha-app", "beta-app", "gamma-app")
                states = {"abc123": "working"}       # every session this test launches (FAKE_CLAUDE's default id)
                for name in ("alpha-app", "beta-app", "gamma-app"):
                    self.board(name, ready=ready.get(name, ()), live=live.get(name, ()))
                    for n in live.get(name, ()):
                        bg_id = f"live-{name[:1]}{n}"
                        self.live_run(name, n, bg_id)
                        states[bg_id] = "working"
                env = self.env(FAKECLAUDE_AGENTS=self.agents(states), **CREDENTIALS)
                live_count = sum(len(v) for v in live.values())
                expected = max(0, min(cap - live_count, sum(len(v) for v in ready.values())))
                p = self.tick(*given, env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                launched = self.launches()
                self.assertEqual(len(launched), expected, out)
                self.assertEqual(len(set(launched)), len(launched), out)
                for name, n in launched:
                    self.assertIn(n, ready.get(name, ()), f"{name} #{n}: {out}")
                    self.assertIn(name, ("alpha-app", "beta-app"), out)
                fresh = [r for r in self.run_records() if r.get("bg_id") == "abc123"]
                self.assertEqual(len(fresh), expected, out)
                self.assertTrue(all(r["ended_at"] is None for r in fresh), fresh)
                # the next tick finds them live: it only tops up to the cap, so nothing more starts
                p = self.tick(*given, env=env)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(len(self.launches()), expected, p.stdout + p.stderr)

    # ---- DoD 3: --api or --venue headless with --all-projects is an argument error
    def test_api_or_headless_with_all_projects_is_an_argument_error(self):
        self.register("alpha-app")
        self.board("alpha-app", ready=(21,))
        for args in (("--all-projects", "--api"), ("--api", "--all-projects"),
                     ("--all-projects", "--venue", "headless"), ("--venue", "headless", "--all-projects"),
                     ("--all-projects", "--api", "--dry-run"), ("--all-projects", "--venue", "headless", "--dry-run")):
            with self.subTest(args=args):
                p = self.work("dispatch", *args, env=self.env(**CREDENTIALS), cwd=self.elsewhere)
                out = p.stdout + p.stderr
                self.assertNotEqual(p.returncode, 0, out)
                self.assertIn("error:", p.stderr)
                self.assertIn("--all-projects", p.stderr)
                self.assertNotIn("unrecognized", p.stderr)
                self.assertEqual(self.claude_calls(), [], out)
                self.assertEqual(self.started_in("alpha-app"), [], out)
                self.assertEqual(self.run_records(), [], out)
        p = self.tick("--dry-run")                   # the control: the same board plans bg
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(WOULD_START_RE.findall(p.stdout), ["21"], p.stdout + p.stderr)

    # ---- DoD 4: --dry-run launches nothing in any project
    def test_dry_run_launches_nothing_in_any_project(self):
        self.board("alpha-app", ready=(21, 22), live=(40,))
        self.board("beta-app", ready=(31,))
        self.board("gamma-app", ready=(41,))
        self.board("delta-app", ready=(51,), incidents=(77,))
        self.live_run("alpha-app", 40, "live-a40")
        env = self.env(FAKECLAUDE_AGENTS=self.agents({"live-a40": "working"}), **CREDENTIALS)
        before = self.runs_snapshot()
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                p = self.tick("--dry-run", *extra, env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                self.assertEqual([c["argv"] for c in self.claude_calls() if c["argv"][:1] != ["agents"]], [], out)
                for name in self.projects:
                    self.assertEqual(self.started_in(name), [], f"{name}: {out}")
                self.assertEqual(self.runs_snapshot(), before, out)
                if extra:
                    json.loads(p.stdout)
                else:                               # each opted-in project's plan
                    self.assertEqual(sorted(int(n) for n in WOULD_START_RE.findall(p.stdout)), [21, 22, 31], out)

    # ---- DoD 5: a failing project does not stop the others
    def test_a_failing_project_does_not_stop_the_others(self):
        # the failing project comes first in the registry, so a stop at the first failure would launch nothing
        for case in ("moved away", "gh fails"):
            with self.subTest(case=case):
                self.setUp()
                self.board("beta-app", ready=(31,))
                if case == "moved away":
                    bad = "vanished-app"
                    self.register(bad, "alpha-app", "beta-app")
                    self.board("alpha-app", ready=(21,))
                    expected = [("alpha-app", 21), ("beta-app", 31)]
                else:
                    bad = "alpha-app"
                    self.register("alpha-app", "beta-app")
                    self.board("alpha-app", ready=(21,), failing=True)
                    expected = [("beta-app", 31)]
                p = self.tick()
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 1, out)
                self.assertEqual(len(lines_with(p.stderr, bad)), 1, out)
                self.assertEqual(sorted(self.launches()), expected, out)
                if bad == "alpha-app":
                    self.assertEqual(self.started_in("alpha-app"), [], out)


# =========================================================================== the subscription budget
NOW_AT_LIMIT = "2026-10-24T10:20:00Z"       # after the 2026-10-24 refusal, before its reset at 11:40Z


class SubscriptionBudget(ProjectsTest):
    """A third budget, `subscription`, beside api and cloud: the run sweep marks it exhausted from a bg session's
    usage-limit refusal, until the reset the refusal names; while it is exhausted bg dispatch launches nothing."""

    def subscription(self, env):
        """`budget status --json`'s subscription entry: exhausted_until, reason and exhausted."""
        data = self.ok_json("budget", "status", "--json", env=env)
        self.assertIn("subscription", data)
        return {k: data["subscription"].get(k, f"<no {k}>") for k in BUDGET_KEYS}

    def assertAvailable(self, env, what="api"):
        entry = self.ok_json("budget", "status", "--json", env=env)[what]
        self.assertFalse(entry["exhausted"], entry)

    def limited_run(self, session, lines, job="file", ticket=40):
        """alpha-app's bg run of `ticket`, `claude agents` listing it blocked, and its session's transcript of
        `lines`. job: "file" (~/.claude/jobs/<bg_id>/state.json names the transcript), "none" (no job file) or
        "stale" (the job file names a path that no longer exists). Returns (the transcript's path, the listing)."""
        bg_id = session[:8]
        record = self.live_run("alpha-app", ticket, bg_id, started_at=transcript_start(lines))
        folder = re.sub(r"[^A-Za-z0-9]", "-", record["worktree"])
        path = os.path.join(self.home, ".claude", "projects", folder, session + ".jsonl")
        write(path, "".join(json.dumps(e) + "\n" for e in lines))
        if job != "none":
            link = path if job == "file" else os.path.join("/nonexistent-moved-away", folder, session + ".jsonl")
            write(os.path.join(self.home, ".claude", "jobs", bg_id, "state.json"),
                  json.dumps({"sessionId": session, "cwd": record["worktree"], "linkScanPath": link}))
        self.board("alpha-app", ready=(21,), live=(ticket,))
        return path, self.agents({bg_id: "blocked"})

    def fresh(self):
        for path in (self.runs_dir(), os.path.join(self.home, ".claude", "jobs"),
                     os.path.join(self.home, ".claude", "projects")):
            shutil.rmtree(path, True)
        if os.path.exists(self.budgets_path()):
            os.unlink(self.budgets_path())

    # ---- outline 3: budget status|exhaust|reset subscription
    def test_budget_status_exhaust_and_reset_know_the_subscription_budget(self):
        env = self.env(HARNESS_NOW="2026-11-19T08:00:00Z")
        self.assertEqual(self.subscription(env), AVAILABLE)
        for hours, until in (("3", "2026-11-19T11:00:00Z"), ("0.5", "2026-11-19T08:30:00Z")):
            with self.subTest(hours=hours):
                self.ok("budget", "exhaust", "subscription", "--reason", f"usage limit {hours}", "--hours", hours,
                        env=env)
                self.assertEqual(self.subscription(env), {"exhausted_until": until, "reason": f"usage limit {hours}",
                                                          "exhausted": True})
                self.assertAvailable(env, "api")
                self.assertAvailable(env, "cloud")
        self.ok("budget", "reset", "subscription", env=env)
        self.assertEqual(self.subscription(env), AVAILABLE)

    # ---- DoD 6: the sweep marks it from a usage-limit refusal, with and without a reset time
    def test_the_sweep_marks_the_subscription_budget_until_the_refusals_reset(self):
        no_reset = "You've hit your usage limit"
        rows = (
            # (case, session, refusal, where the transcript is found, now, the reset expected)
            ("rate_limit_info.resetsAt, found by the job file", "1a2b3c4d-1111-4aaa-8bbb-000000000001",
             usage_limit_refusal(), "file", NOW_AT_LIMIT, epoch_iso(1792842000)),
            ("another reset, no job file", "2b3c4d5e-2222-4aaa-8bbb-000000000002",
             usage_limit_refusal("2026-10-23T05:45:12.000Z", resets_at=1792736400,
                                 text="You've hit your session limit \u00b7 resets 3:20pm (Asia/Tokyo)"),
             "none", "2026-10-23T05:50:00Z", epoch_iso(1792736400)),
            ("quotaLimits.resetsAt only, a stale job file", "3c4d5e6f-3333-4aaa-8bbb-000000000003",
             usage_limit_refusal(resets_at=None, quota_resets_at=1792846800,
                                 text="You've hit your 5-hour limit \u00b7 resets 10pm (Asia/Tokyo)"),
             "stale", NOW_AT_LIMIT, epoch_iso(1792846800)),
            ("no reset time: its timestamp plus 5 h", "4d5e6f7a-4444-4aaa-8bbb-000000000004",
             usage_limit_refusal("2026-10-24T10:11:06.486Z", resets_at=None, quota_resets_at=None, text=no_reset),
             "file", NOW_AT_LIMIT, "2026-10-24T15:11:06Z"),
            ("no reset time, another timestamp", "5e6f7a8b-5555-4aaa-8bbb-000000000005",
             usage_limit_refusal("2026-10-23T05:47:40.100Z", resets_at=None, quota_resets_at=None, text=no_reset),
             "none", "2026-10-23T06:00:00Z", "2026-10-23T10:47:40Z"),
        )
        for case, session, refusal, job, now, until in rows:
            with self.subTest(case=case):
                self.fresh()
                _, agents = self.limited_run(session, limited_transcript(session, refusal), job=job)
                env = self.env(HARNESS_NOW=now, FAKECLAUDE_AGENTS=agents)
                p = self.work("runs", "--sweep", env=env)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                entry = self.subscription(env)
                self.assertTrue(entry["exhausted"], entry)
                got = dt.datetime.strptime(entry["exhausted_until"] or "", "%Y-%m-%dT%H:%M:%SZ")
                want = dt.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ")
                self.assertLessEqual(abs((got - want).total_seconds()), 1, entry)
                self.assertAvailable(env, "api")

    def test_the_sweep_leaves_it_alone_without_a_current_refusal(self):
        session = "6f7a8b9c-6666-4aaa-8bbb-000000000006"
        refusal = usage_limit_refusal()             # at 10:11:06Z, resets at 11:40Z
        went_on = (limited_transcript(session, refusal)
                   + [prompted("[Earlier usage-limit notes no longer apply. Continue working normally.]",
                               "2026-10-24T10:16:00.000Z", isMeta=True, usageLimitNote="release"),
                      said("Picking up where I stopped: the tests are red.", "2026-10-24T10:16:30.000Z")]
                   + after_turn(session, "2026-10-24T10:17:00.000Z"))
        quoted = [prompted("/harness:work 40", "2026-10-24T09:58:00.000Z"),
                  prompted("<task-notification>Agent terminated early due to an API error: " + SESSION_LIMIT_TEXT
                           + " (error type rate_limit, HTTP 429)</task-notification>", "2026-10-24T10:11:07.000Z"),
                  said("The test-author stopped early (" + SESSION_LIMIT_TEXT + "); I will wait.",
                       "2026-10-24T10:11:20.000Z")] + after_turn(session, "2026-10-24T10:11:21.000Z")
        rows = (
            ("the reset is already past", limited_transcript(session, refusal), "2026-10-24T12:00:00Z"),
            ("the session went on after the refusal, its reset still ahead", went_on, NOW_AT_LIMIT),
            ("only quoted on the main thread, refused in a subagent's file", quoted, NOW_AT_LIMIT),
        )
        for case, lines, now in rows:
            with self.subTest(case=case):
                self.fresh()
                path, agents = self.limited_run(session, lines)
                if lines is quoted:                 # the subagent's own transcript holds a real refusal
                    sub = os.path.join(os.path.dirname(path), session, "subagents", "agent-a1b2c3d4e5f6.jsonl")
                    write(sub, json.dumps(usage_limit_refusal(sidechain=True)) + "\n")
                env = self.env(HARNESS_NOW=now, FAKECLAUDE_AGENTS=agents)
                p = self.work("runs", "--sweep", env=env)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.subscription(env), AVAILABLE)

    def test_one_refusal_marks_the_budget_once(self):
        session = "7a8b9c0d-7777-4aaa-8bbb-000000000007"
        lines = limited_transcript(session, usage_limit_refusal())
        path, agents = self.limited_run(session, lines)
        env = self.env(HARNESS_NOW=NOW_AT_LIMIT, FAKECLAUDE_AGENTS=agents)
        self.ok("runs", "--sweep", env=env)
        self.assertEqual(self.subscription(env)["exhausted_until"], epoch_iso(1792842000))
        self.ok("budget", "reset", "subscription", env=env)
        self.ok("runs", "--sweep", env=env)             # the same refusal again: reset stays reset
        self.assertEqual(self.subscription(env), AVAILABLE)
        # the session resumed, then hit the limit again: a new refusal marks it again, until its own reset
        lines += ([prompted("[Earlier usage-limit notes no longer apply.]", "2026-10-24T10:25:00.000Z", isMeta=True,
                            usageLimitNote="release"),
                   said("Continuing.", "2026-10-24T10:25:10.000Z"),
                   usage_limit_refusal("2026-10-24T10:30:00.000Z", resets_at=1792846800,
                                       text="You've hit your 5-hour limit \u00b7 resets 10pm (Asia/Tokyo)")]
                  + after_turn(session, "2026-10-24T10:30:01.000Z"))
        write(path, "".join(json.dumps(e) + "\n" for e in lines))
        env = self.env(HARNESS_NOW="2026-10-24T10:35:00Z", FAKECLAUDE_AGENTS=agents)
        self.ok("runs", "--sweep", env=env)
        self.assertEqual(self.subscription(env)["exhausted_until"], epoch_iso(1792846800))

    # ---- DoD 6: bg dispatch holds while it is exhausted and names the reset; reset clears it
    def test_bg_dispatch_launches_nothing_while_the_subscription_budget_is_exhausted(self):
        self.register("alpha-app")
        self.board("alpha-app", ready=(21, 22))
        env = self.env(HARNESS_NOW="2026-11-19T08:00:00Z", **CREDENTIALS)
        held = ((("dispatch",), self.repo), (("dispatch", "--dry-run"), self.repo),
                (("dispatch", "--venue", "bg"), self.repo), (("dispatch", "--only", "21"), self.repo),
                (("dispatch", "--all-projects"), self.elsewhere),
                (("dispatch", "--all-projects", "--dry-run"), self.elsewhere))
        for hours, until in (("2", "2026-11-19T10:00:00Z"), ("7.5", "2026-11-19T15:30:00Z")):
            self.ok("budget", "exhaust", "subscription", "--reason", "usage limit", "--hours", hours, env=env)
            for args, cwd in held:
                with self.subTest(until=until, args=args):
                    p = self.work(*args, env=env, cwd=cwd)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 1, out)
                    self.assertTrue(lines_with(out, "subscription", until), out)
                    self.assertEqual(self.launches(), [], out)
                    self.assertEqual(self.started_in("alpha-app"), [], out)
                    self.assertEqual(self.run_records(), [], out)
        # headless runs on the API key: the subscription budget does not hold it
        p = self.work("dispatch", "--api", "--dry-run", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(WOULD_START_RE.findall(p.stdout)), ["21", "22"], p.stdout + p.stderr)
        self.assertIn("launch headless", p.stdout)
        # reset clears the hold
        self.ok("budget", "reset", "subscription", env=env)
        p = self.work("dispatch", env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(self.launches()), [("alpha-app", 21), ("alpha-app", 22)], p.stdout + p.stderr)

    def test_a_refusal_found_by_dispatchs_own_sweep_holds_that_dispatch(self):
        self.register("alpha-app")
        session = USAGE_LIMIT_SESSION
        _, agents = self.limited_run(session, limited_transcript(session, usage_limit_refusal()))
        env = self.env(HARNESS_NOW=NOW_AT_LIMIT, FAKECLAUDE_AGENTS=agents, **CREDENTIALS)
        until = epoch_iso(1792842000)
        for args, cwd in ((("dispatch", "--all-projects"), self.elsewhere), (("dispatch",), self.repo)):
            with self.subTest(args=args):
                p = self.work(*args, env=env, cwd=cwd)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 1, out)
                self.assertTrue(lines_with(out, "subscription", until), out)
                self.assertEqual(self.launches(), [], out)
                self.assertEqual(self.subscription(env)["exhausted_until"], until)
        self.ok("budget", "reset", "subscription", env=env)
        p = self.work("dispatch", env=env)                   # the same refusal does not mark it again
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.launches(), [("alpha-app", 21)], p.stdout + p.stderr)


# =========================================================================== the LaunchAgent
LABEL = "com.ryaneskuri.harness-dispatch"


class DispatchLaunchd(ProjectsTest):
    def plist(self):
        return os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")

    def launchctl_calls(self):
        if not os.path.exists(self.launch_log):
            return []
        with open(self.launch_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def install(self, **extra):
        return self.work("dispatch", "--install-launchd", env=self.env(**extra), cwd=self.elsewhere)

    # ---- DoD 7: the plist has the label, the hourly StartCalendarInterval, arguments, PATH and log
    def test_install_writes_the_hourly_plist_and_loads_it(self):
        uid, log = os.getuid(), os.path.join(self.home, ".claude", "harness", "dispatch", "launchd.log")
        job_path = f"/opt/homebrew/bin:/usr/local/bin:{self.home}/.local/bin:/usr/bin:/bin"
        for fail in ("", "bootout"):            # a failing bootout means no older copy was loaded
            with self.subTest(launchctl_fail=fail):
                if os.path.exists(self.launch_log):
                    os.unlink(self.launch_log)
                p = self.install(LAUNCHCTL_FAIL=fail)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                with open(self.plist(), "rb") as fh:
                    job = plistlib.load(fh)
                self.assertEqual(job.get("Label"), LABEL)
                args = job.get("ProgramArguments") or []
                self.assertEqual(args[1:], [os.path.realpath(TOOL), "dispatch", "--all-projects", "--max", "8"], job)
                self.assertTrue(os.path.basename(args[0]).startswith("python3") and os.access(args[0], os.X_OK),
                                job)
                self.assertEqual(job.get("StartCalendarInterval"), {"Minute": 0})
                self.assertNotIn("StartInterval", job)
                self.assertEqual(job.get("EnvironmentVariables"), {"PATH": job_path})
                self.assertEqual((job.get("StandardOutPath"), job.get("StandardErrorPath")), (log, log))
                self.assertIs(job.get("RunAtLoad"), False)
                self.assertTrue(os.path.isdir(os.path.dirname(log)))
                self.assertEqual(self.launchctl_calls(), [["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                                                          ["launchctl", "bootstrap", f"gui/{uid}", self.plist()]])
                self.assertEqual(self.claude_calls(), [])           # installing dispatches nothing
                self.assertEqual(self.gh_calls(), [])

    # ---- DoD 7: uninstall boots it out and removes it
    def test_uninstall_boots_it_out_and_removes_the_plist(self):
        uid = os.getuid()
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(self.plist()))
        os.unlink(self.launch_log)
        p = self.work("dispatch", "--uninstall-launchd", env=self.env(), cwd=self.elsewhere)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.launchctl_calls(), [["launchctl", "bootout", f"gui/{uid}/{LABEL}"]])
        self.assertFalse(os.path.exists(self.plist()))
        # again: nothing loaded and no file, still exit 0
        p = self.work("dispatch", "--uninstall-launchd", env=self.env(LAUNCHCTL_FAIL="bootout"), cwd=self.elsewhere)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.plist()))
        self.assertEqual(self.claude_calls(), [])


if __name__ == "__main__":
    unittest.main()
