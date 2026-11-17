"""harness-pr land runs the project's canary (#338): after the main checkout fast-forwards to the landed merge, its
`scripts/smoke.sh`, when there and executable, runs there under HARNESS_SMOKE_TIMEOUT seconds. Exit 0 lands as today;
2 or the time limit lands with a `harness-pr: warning:` line and a ticket comment; 1 or any other exit reverts the
merge on main as `harness-pr revert` does (the revert pushed, the incident bug, the ticket reopened as needs-input, a
notification) and the land exits 1 naming the revert. No executable scripts/smoke.sh lands as today.

The fixtures are pr_support's (WorktreeTest: a bare origin, the main checkout `proj`, the ticket worktree), plus a
project whose committed scripts/smoke.sh reads the landed content: canary/status is its exit, canary/sleep how long
it sleeps first, canary/linger how long a background child it leaves (its pid recorded) lives, canary/note what it
prints (on stdout and on stderr) and logs with its working directory. Main holds the healthy values before the land;
PR #34's squash merge, a real commit pushed to origin from a second clone as GitHub's merge would be, sets the case's
values, so only a canary run after the fast-forward sees them. Every slow canary sleeps the same SLOW seconds, so
only the limit in force (SHORT or LONG, from the environment, else the main checkout's settings, else the default)
decides. The hanging canary sleeps HANG seconds, longer than the BOUND a land is given to return, so only a limit
that stops it while it runs lets the land return in time.

Run: PYTHONPATH=plugins/harness/tests python3 -m unittest plugins/harness/tests/test_pr_land_canary.py -v
"""

from __future__ import annotations

import re
import signal
import time
import unittest

from pr_support import *  # noqa: F401,F403

# The head-check fixtures (#337: LAND_FIELDS, HEAD_SHA, land_view, land_gate_calls) are pr_support's (#348).
SMOKE = "scripts/smoke.sh"
BUG = 40
SLOW = 2            # seconds every slow canary takes before it exits
SHORT = "1"         # a limit it runs past: degraded
LONG = "8"          # a limit it ends within: its exit decides
HANG = 30           # seconds the hanging canary sleeps before it would exit 1
BOUND = 10          # seconds a land with a hanging canary is given to return

# The fixture project's canary, as the scaffold's contract has it (0 healthy, 1 big failure, 2 degraded); what it
# does is read from the checkout it runs in. {log} is the absolute path of the run log, {pids} where the pid of its
# lingering background child goes (that child holds none of the canary's output streams).
SMOKE_SCRIPT = """#!/bin/sh
printf '%s|%s\\n' "$(pwd -P)" "$(cat canary/note)" >> "{log}"
echo "canary out: $(cat canary/note)"
echo "canary err: $(cat canary/note)" >&2
if [ -f canary/linger ]; then
    sleep "$(cat canary/linger)" </dev/null >/dev/null 2>&1 &
    echo $! > "{pids}"
fi
if [ -f canary/sleep ]; then sleep "$(cat canary/sleep)"; fi
exit "$(cat canary/status)"
"""


class LandCanary(WorktreeTest):
    def setUp(self):
        super().setUp()
        self.smoke_log = os.path.join(self.tmp, "smoke-runs.log")
        self.smoke_pids = os.path.join(self.tmp, "smoke-child.pid")
        self.bug_stdin = os.path.join(self.tmp, "bug-stdin.jsonl")
        self.outputs(**{"harness-board": {
            "epic-check": {"stdout": EPIC_SAID},
            "task create": {"stdout": json.dumps({"number": BUG, "url": f"https://github.com/o/r/issues/{BUG}"}),
                            "read_stdin": self.bug_stdin},
            "needs-input": {"stdout": "https://github.com/o/r/issues/12#issuecomment-9\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    # ----------------------------------------------------------- the fixture project
    def project(self, smoke="executable", settings=None):
        """Main as the project has it before the land, committed and pushed: scripts/smoke.sh (`executable`, a
        `plain` file, or None for none), the canary's healthy inputs and, when given, .claude/settings.json `env`."""
        write(os.path.join(self.main, "canary", "status"), "0\n")
        write(os.path.join(self.main, "canary", "note"), "before the land\n")
        if smoke:
            script = SMOKE_SCRIPT.replace("{log}", self.smoke_log).replace("{pids}", self.smoke_pids)
            path = write(os.path.join(self.main, SMOKE), script)
            if smoke == "executable":
                os.chmod(path, 0o755)
            else:
                os.chmod(path, 0o644)
        if settings is not None:
            write(os.path.join(self.main, ".claude", "settings.json"), json.dumps({"env": settings}))
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "The project's canary")
        self.git(self.main, "push", "-q", "origin", "main")
        mode = self.git(self.main, "ls-files", "-s", SMOKE).split(" ")[0] if smoke else ""
        self.assertEqual(mode, {"executable": "100755", "plain": "100644", None: ""}[smoke])

    def landed(self, status, note, sleep=None, linger=None):
        """Push PR #34's squash merge to origin/main from a second clone: the widget, and the canary's inputs for
        this case (its exit, what it prints, how long it sleeps first, how long its background child lives); its
        sha."""
        clone = os.path.join(self.tmp, "github")
        if not os.path.isdir(clone):
            subprocess.run([GIT, "clone", "-q", self.origin, clone], env=self.env(), check=True, capture_output=True)
        self.git(clone, "pull", "-q", "--ff-only")
        files = {"widget.py": "def frob():\n    return 2\n", "canary/status": f"{status}\n",
                 "canary/note": f"{note}\n"}
        if sleep is not None:
            files["canary/sleep"] = f"{sleep}\n"
        if linger is not None:
            files["canary/linger"] = f"{linger}\n"
        for path, text in files.items():
            write(os.path.join(clone, path), text)
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "Add the widget (#34)")
        self.git(clone, "push", "-q", "origin", "main")
        sha = self.git(clone, "rev-parse", "HEAD")
        self.assertNotEqual(self.git(self.main, "rev-parse", "HEAD"), sha)
        return sha

    def calls(self, sha):
        """The land of PR #34 merged as `sha`, then what a revert of it reads and writes: the PR that merged it, no
        open incident, the bug's label, and the ticket (closed by the land) reopened."""
        before = {"state": "OPEN", "labels": [{"name": "task"}, {"name": "status:in-review"}], "comments": []}
        after = {"number": 12, "state": "CLOSED", "labels": [{"name": "task"}], "comments": [],
                 "title": "Add the widget", "body": TICKET_12_BODY, "url": "https://github.com/o/r/issues/12"}
        merged = dict(pr_data(FIXTURE_A), mergeCommit={"oid": sha}, state="MERGED")
        # the head checks: the head, HEAD_SHA, level with main and with no harness review, so the land proceeds
        return land_gate_calls(commits=[pr_commit("a", at(9))]) + [
            {"match": ["pr", "view", "34", "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}},
            {"match": ["pr", "view", "34", "--json", LAND_FIELDS], "stdout_json": land_view()},
            REPO_VIEW, {"match": ["pr", "merge", "34"], "stdout": ""},
            {"match": ["issue", "view", "12", "--json", "state,labels,comments"], "stdout_json": before},
            {"match": ["issue", "edit", "12"], "stdout": ""},
            {"match": ["api", f"repos/o/r/commits/{sha}/pulls"],
             "stdout_json": [{"number": 34, "title": merged["title"], "html_url": merged["url"],
                              "merge_commit_sha": sha, "merged_at": "2026-11-15T10:00:00Z"}]},
            {"match": ["pr", "view", "34"], "stdout_json": merged},
            *files_calls(34, FIXTURE_A["files"]),
            {"match": ["issue", "list"], "stdout_json": []},
            {"match": ["issue", "edit", str(BUG)], "stdout": ""},
            {"match": ["issue", "reopen", "12"], "stdout": ""},
            {"match": ["issue", "view", "12"], "stdout_json": after},
            {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": []}]

    def land(self, sha, cwd=None, **env):
        self.scenario(self.calls(sha))
        return self.pr("land", "34", cwd=cwd or self.main, **env)

    def bounded_land(self, sha, **env):
        """self.land's run from the main checkout, given BOUND seconds to return: still running then, it is killed
        with its process group and the test fails, so a canary that never ends cannot hang the suite. Its output goes
        to files, not pipes, so the land's own exit is timed, not that of whatever still holds its output."""
        self.scenario(self.calls(sha))
        argv = [sys.executable, TOOL, "land", "34"]
        out_path, err_path = os.path.join(self.tmp, "land.out"), os.path.join(self.tmp, "land.err")
        start = time.monotonic()
        with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(argv, cwd=self.main, env=self.env(**env), stdout=out, stderr=err,
                                    start_new_session=True)
        try:
            code = proc.wait(timeout=BOUND)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            code = None
        elapsed = time.monotonic() - start
        with open(out_path, encoding="utf-8") as fh:
            stdout = fh.read()
        with open(err_path, encoding="utf-8") as fh:
            stderr = fh.read()
        if code is None:
            if os.path.exists(self.smoke_pids):
                with open(self.smoke_pids, encoding="utf-8") as fh:
                    pid = fh.read().strip()
                if pid.isdigit():
                    self.addCleanup(self.kill_quietly, int(pid))
            self.fail(f"the land was still running {elapsed:.1f} s after it started, with a {SHORT} s limit on a "
                      f"canary that sleeps {HANG} s: the limit did not stop it\n{stdout}{stderr}")
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    # ----------------------------------------------------------- what happened
    def smoke_runs(self):
        """(working directory, the canary/note it read) for each run of the fixture's scripts/smoke.sh."""
        if not os.path.exists(self.smoke_log):
            return []
        with open(self.smoke_log, encoding="utf-8") as fh:
            return [tuple(line.split("|", 1)) for line in fh.read().splitlines() if line]

    def origin_head(self):
        return self.git(self.origin, "rev-parse", "main")

    def reverts_on_origin(self):
        return self.git(self.origin, "log", "--format=%H", "--fixed-strings", "--grep=This reverts commit",
                        "main").split()

    def board(self):
        return self.stub_calls("harness-board")

    def ticket_comments(self):
        return [a[a.index("--body") + 1] for a in self.board() if a[:2] == ["comment", "12"] and "--body" in a]

    def canary_comments(self):
        """The ticket comments other than the land's own two (the Resolution and `Landed in`)."""
        return [b for b in self.ticket_comments() if not b.lstrip().startswith((RESOLUTION, "Landed in"))]

    def smoke_warnings(self, p):
        return [line for line in p.stderr.splitlines() if line.startswith(WARNING) and SMOKE in line]

    def without_paths(self, text):
        return text.replace(self.tmp, "<tmp>")

    def asked(self):
        return [a for a in self.board() if a[:2] == ["needs-input", "12"]]

    def revert_notes(self):
        return [n for n in self.notifications() if "revert" in " ".join(n).lower()]

    # ----------------------------------------------------------- assertions
    def assertNothingReverted(self, p, sha):
        """origin/main still at the merge with no revert made anywhere; the ticket closed, unlabelled and commented
        as today; no bug, no needs-input, no reopen, no revert notification."""
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.origin_head(), sha, "origin/main moved after the merge")
        self.assertEqual(self.reverts_on_origin(), [])
        self.assertFalse([line for line in self.order() if line.startswith("git") and " revert " in f" {line} "],
                         self.order())
        board = self.board()
        self.assertIn(["close", "12", "--repo", self.main], board)
        self.assertIn(["comment", "12", "--body", f"Landed in {sha}", "--repo", self.main], board)
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        self.assertFalse([a for a in board if a[:2] == ["task", "create"] or a[0] == "needs-input"], board)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["issue", "reopen"]], self.gh_argv())
        self.assertEqual(self.revert_notes(), [])

    def assertLandedAsToday(self, p, sha):
        """Exit 0, the main checkout fast-forwarded to the merge, and nothing reverted."""
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), sha)
        self.assertNothingReverted(p, sha)

    def assertDegraded(self, p, sha, note, timed_out):
        """Landed as today, plus exactly one warning line naming scripts/smoke.sh and its exit 2 (or the time
        limit), and one ticket comment naming scripts/smoke.sh."""
        self.assertLandedAsToday(p, sha)
        self.assertEqual(self.smoke_runs(), [(self.main, note)])
        said = self.smoke_warnings(p)
        self.assertEqual(len(said), 1, p.stderr)
        if timed_out:     # the time limit, in words ("timed out", "time limit") or as its seconds ("past 1 s")
            self.assertRegex(self.without_paths(said[0]),
                             re.compile(rf"tim(e|ed)|\b{SHORT}(\.0+)?\s?s(ec\w*)?\b", re.IGNORECASE))
        else:
            self.assertRegex(self.without_paths(said[0]), r"\b2\b")
        comments = self.canary_comments()
        self.assertEqual(len(comments), 1, self.ticket_comments())
        self.assertIn(SMOKE, comments[0])

    def assertNoCanaryRan(self, p, sha):
        """A land whose main checkout was not fast-forwarded: the canary never ran, nothing was reverted."""
        self.assertEqual(self.smoke_runs(), [])
        self.assertNothingReverted(p, sha)
        self.assertEqual(self.canary_comments(), [])
        self.assertEqual(self.smoke_warnings(p), [])

    def assertChildGone(self):
        """The canary's lingering background child (it would live 30 s) is dead soon after the land returns: the
        time limit stopped the canary's whole process group, not only the script."""
        with open(self.smoke_pids, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        self.addCleanup(self.kill_quietly, pid)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:          # the pid went to another user's process: ours is gone
                return
            time.sleep(0.05)
        self.fail(f"the canary's background child {pid} still runs after the timed-out land")

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def assertReverted(self, p, sha, note, code):
        """Exit 1 naming the revert; origin/main's head a revert of the merge on top of it, the main checkout at it;
        one incident bug (`Merge commit: <sha>`, the canary's output as evidence) labelled incident; the ticket
        asked once, naming the revert, scripts/smoke.sh and the exit `code`. The revert's sha."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.smoke_runs(), [(self.main, note)])
        reverts = self.reverts_on_origin()
        self.assertEqual(len(reverts), 1, self.git(self.origin, "log", "--format=%h %s", "main"))
        revert = reverts[0]
        self.assertEqual(self.origin_head(), revert)
        self.assertEqual(self.git(self.origin, "rev-parse", f"{revert}^"), sha)
        self.assertIn(f"This reverts commit {sha}", self.git(self.origin, "log", "-1", "--format=%B", revert))
        self.assertEqual(self.git(self.origin, "show", "main:canary/status"), "0")      # the landed change undone
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), revert)
        self.assertIn(revert[:7], p.stderr)
        creates = [a for a in self.board() if a[:2] == ["task", "create"]]
        self.assertEqual(len(creates), 1, self.board())
        self.assertEqual(creates[0][creates[0].index("--type") + 1], "bug", creates[0])
        bodies = self.jsonl(self.bug_stdin)
        self.assertEqual(len(bodies), 1, bodies)
        body = bodies[0]
        self.assertIn(f"Merge commit: {sha}", body.splitlines())
        self.assertIn(revert, body)
        self.assertIn(SMOKE, body)
        self.assertIn(f"canary out: {note}", body)
        self.assertIn(f"canary err: {note}", body)
        self.assertIn(["issue", "edit", str(BUG), "--add-label", "incident"], self.gh_argv())
        asks = self.asked()
        self.assertEqual(len(asks), 1, self.board())
        question = asks[0][asks[0].index("--question") + 1]
        self.assertIn(revert[:7], question)
        self.assertIn(SMOKE, question)
        self.assertRegex(question.replace(revert[:7], ""), rf"\b{code}\b")
        self.assertTrue(any("reverted PR #34" in " ".join(n) for n in self.notifications()), self.notifications())
        return revert

    # ------------------------------------------------------------- broad
    def test_a_healthy_canary_lands_as_today(self):
        note = "healthy landing 5e1"
        self.project()
        sha = self.landed(0, note)
        p = self.land(sha, cwd=self.wt)          # from the ticket worktree, which the land removes: main runs it
        self.assertLandedAsToday(p, sha)
        self.assertEqual(self.smoke_runs(), [(self.main, note)])
        self.assertEqual(self.canary_comments(), [])
        self.assertEqual(self.smoke_warnings(p), [])

    def test_a_failing_canary_reverts_the_merge_files_the_bug_and_reopens_the_ticket(self):
        note = "broken landing 3b8"
        self.project()
        sha = self.landed(1, note)
        p = self.land(sha)
        self.assertReverted(p, sha, note, 1)
        self.assertIn(["issue", "reopen", "12"], self.gh_argv())
        # the land's ticket steps first, then the canary's verdict: the ticket ends reopened and asked
        steps = [self.position(f"git -C {self.main} pull --ff-only"),
                 self.position("harness-board close 12"),
                 self.position(f"harness-board comment 12 --body Landed in {sha}"),
                 self.position("gh issue reopen 12"),
                 self.position("harness-board needs-input 12")]
        self.assertEqual(steps, sorted(steps), "\n".join(self.order()))

    def test_a_degraded_canary_lands_with_a_warning_and_a_ticket_comment(self):
        note = "degraded landing 7c4"
        self.project()
        sha = self.landed(2, note)
        p = self.land(sha)
        self.assertDegraded(p, sha, note, timed_out=False)

    def test_without_a_smoke_script_the_land_is_as_today(self):
        self.project(smoke=None)
        sha = self.landed(1, "no canary here 2f0")
        p = self.land(sha)
        self.assertLandedAsToday(p, sha)
        self.assertEqual(self.smoke_runs(), [])
        self.assertEqual(self.canary_comments(), [])
        self.assertEqual(self.smoke_warnings(p), [])

    def test_a_land_that_does_not_fast_forward_main_runs_no_canary(self):
        # the main checkout's own edit of a landed file blocks the pull (as today: exit 1, main not pulled), so the
        # canary never sees the landed content, which would fail it
        self.project()
        sha = self.landed(1, "unpulled landing 1d9")
        before = self.git(self.main, "rev-parse", "HEAD")
        write(os.path.join(self.main, "canary", "note"), "my own note, not the landed one\n")
        p = self.land(sha)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("canary/note", p.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), before)
        self.assertNoCanaryRan(p, sha)

    # ---------------------------------------------------------- specific
    def test_a_main_checkout_on_another_branch_runs_no_canary(self):
        # as today: exit 0, main not pulled; the checkout's scripts/smoke.sh is not run there either
        self.project()
        self.git(self.main, "switch", "-q", "-c", "notes")
        before = self.git(self.main, "rev-parse", "HEAD")
        sha = self.landed(1, "landing while on notes 0c5")
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "--abbrev-ref", "HEAD"), "notes")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), before)
        self.assertNoCanaryRan(p, sha)

    def test_any_other_failing_exit_is_a_big_failure(self):
        # SLOW seconds under the default limit (none set): a failure, not a timeout
        note = "exit three landing 6a1"
        self.project()
        sha = self.landed(3, note, sleep=SLOW)
        p = self.land(sha)
        self.assertReverted(p, sha, note, 3)

    def test_a_canary_past_its_time_limit_lands_as_degraded(self):
        # it would exit 1 after SLOW seconds: the limit in the environment stops it first, its child too
        note = "hanging landing 9d2"
        self.project()
        sha = self.landed(1, note, sleep=SLOW, linger=30)
        p = self.land(sha, HARNESS_SMOKE_TIMEOUT=SHORT)
        self.assertDegraded(p, sha, note, timed_out=True)
        self.assertChildGone()

    def test_the_time_limit_comes_from_the_settings_when_the_environment_sets_none(self):
        note = "hanging landing, limit in the settings 4e7"
        self.project(settings={"HARNESS_SMOKE_TIMEOUT": SHORT})
        sha = self.landed(1, note, sleep=SLOW)
        p = self.land(sha)
        self.assertDegraded(p, sha, note, timed_out=True)

    def test_a_long_limit_in_the_settings_lets_a_slow_failing_canary_revert(self):
        note = "slow failing landing, long limit in the settings 5d3"
        self.project(settings={"HARNESS_SMOKE_TIMEOUT": LONG})
        sha = self.landed(1, note, sleep=SLOW)
        p = self.land(sha)
        self.assertReverted(p, sha, note, 1)

    def test_a_long_limit_in_the_environment_wins_over_a_short_one_in_the_settings(self):
        note = "slow failing landing, long limit in the environment 2a6"
        self.project(settings={"HARNESS_SMOKE_TIMEOUT": SHORT})
        sha = self.landed(1, note, sleep=SLOW)
        p = self.land(sha, HARNESS_SMOKE_TIMEOUT=LONG)
        self.assertReverted(p, sha, note, 1)

    def test_a_hanging_canary_is_stopped_at_its_limit_and_the_land_lands_as_degraded(self):
        # it would sleep HANG seconds, then exit 1: the 1 s limit stops it while it runs, its child too, and the
        # land returns within BOUND seconds, without a revert
        note = "hung landing b71"
        self.project()
        sha = self.landed(1, note, sleep=HANG, linger=HANG)
        p = self.bounded_land(sha, HARNESS_SMOKE_TIMEOUT=SHORT)
        self.assertDegraded(p, sha, note, timed_out=True)
        self.assertChildGone()

    def test_the_main_checkouts_settings_set_the_limit_when_the_land_runs_from_the_ticket_worktree(self):
        # the ticket worktree, the land's working directory, carries its own committed settings with the long
        # limit; the main checkout's short one is in force, so the slow failing canary times out, no revert
        note = "slow landing run from the worktree, short limit in main 6c2"
        self.project(settings={"HARNESS_SMOKE_TIMEOUT": SHORT})
        write(os.path.join(self.wt, ".claude", "settings.json"), json.dumps({"env": {"HARNESS_SMOKE_TIMEOUT": LONG}}))
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "The worktree's own limit")
        self.assertEqual(self.git(self.wt, "status", "--porcelain"), "")
        sha = self.landed(1, note, sleep=SLOW)
        p = self.land(sha, cwd=self.wt)
        self.assertDegraded(p, sha, note, timed_out=True)

    def test_a_short_limit_in_the_environment_wins_over_a_long_one_in_the_settings(self):
        note = "slow failing landing, short limit in the environment 8f1"
        self.project(settings={"HARNESS_SMOKE_TIMEOUT": LONG})
        sha = self.landed(1, note, sleep=SLOW)
        p = self.land(sha, HARNESS_SMOKE_TIMEOUT=SHORT)
        self.assertDegraded(p, sha, note, timed_out=True)

    def test_a_smoke_script_that_is_not_executable_is_not_run(self):
        self.project(smoke="plain")
        sha = self.landed(1, "plain file landing 8b3")
        p = self.land(sha)
        self.assertLandedAsToday(p, sha)
        self.assertEqual(self.smoke_runs(), [])
        self.assertEqual(self.canary_comments(), [])


if __name__ == "__main__":
    unittest.main()
