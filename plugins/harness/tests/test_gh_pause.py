"""Tests for the machine-wide gh pause (ticket #189, epic #185): a gh call GitHub refuses for a rate limit
records ~/.claude/harness/gh/pause.json, every harness process waits for that pause before its next gh call, and
the refused call is then run again with the same argv.

The contract these tests fix:
- harness_lib/ghlimit.py: refused(text) (the refusal pattern), pause_info() (the recorded pause, None for a
  missing, unreadable or corrupt file), record_refusal(reason, limits=None) (writes pause.json under pause.lock and
  returns the pause), GhPaused(Problem) with exit_code 75 and `until`, the per-process wait budget
  (HARNESS_GH_MAX_WAIT, default 90 s) and wait_for_clear(budget).
- Pause length: `gh api rate_limit` with graphql or core remaining 0 means until that reset + 5 s, kind primary;
  else kind secondary, HARNESS_GH_BACKOFF (default 60) s doubled per strike, at most HARNESS_GH_BACKOFF_MAX (default
  900). A refusal is a further strike when the previous pause ended less than HARNESS_GH_BACKOFF_MAX ago. A recorded
  pause is never shortened.
- harness_lib/gh.py: rate_limits(cwd=None) and gh_run, which waits, runs, records a refusal, waits and runs the
  same argv again; each attempt is one log line with waited_ms and refused.
- harness_lib/cli.py: Problem.exit_code (1), run_main exits with it, exit_problem(prog, err) prints the one line
  and exits with it; the mains of harness-board, harness-pr and harness-work use it.

Tools run as subprocesses with a temporary HOME, HARNESS_GH pointing at fakegh.py, FAKEGH_SCENARIO/FAKEGH_LOG and
small HARNESS_GH_* values. The in-process tests patch HOME. The fake executables and the git repositories are made
once for the module. No test sleeps much more than a second.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_gh_pause.py" -v
"""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tools must not leave __pycache__ in the checkout

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
BIN = os.path.join(PLUGIN, "bin")
LIB = os.path.join(PLUGIN, "lib")
FAKEGH = os.path.join(TESTS, "fakegh.py")
BOARD = os.path.join(BIN, "harness-board")
PR = os.path.join(BIN, "harness-pr")
WORK = os.path.join(BIN, "harness-work")
GIT = shutil.which("git")
UTC = dt.timezone.utc

GH = "#!{python}\nimport os, sys\nos.execv({python!r}, [{python!r}, {fakegh!r}] + sys.argv[1:])\n"
CLAUDE = '#!/bin/sh\n[ "$1 $2" = "agents --json" ] || exit 64\necho "[]"\n'

PRIMARY = "GraphQL: API rate limit already exceeded for user ID 1."
SECONDARY = "HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
TAIL = "; wait until then (harness-work budget wait) and run this again"

TASK_BODY = """## Request
Add the widget.

## Definition of done
- [ ] The widget frobs

## Tests that must pass
- `tests/test_widget.py::test_frob`: frobs
"""

W: dict = {}


# ---------------------------------------------------------------- fixtures
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_", "CLAUDE_"))}
    env.update(HOME=W["githome"], GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    return env


def git(repo, *args):
    p = subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                       env=git_env(), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {p.stderr}")
    return p.stdout.strip()


def setUpModule():
    if GIT is None:
        raise unittest.SkipTest("git not found")
    root = os.path.realpath(tempfile.mkdtemp(prefix="test-gh-pause-"))
    W["root"] = root
    try:
        W["githome"] = os.path.join(root, "githome")
        os.makedirs(W["githome"])
        W["fakebin"] = fakebin = os.path.join(root, "fakebin")
        W["gh"] = executable(os.path.join(fakebin, "gh"), GH.format(python=sys.executable, fakegh=FAKEGH))
        executable(os.path.join(fakebin, "claude"), CLAUDE)
        os.symlink(sys.executable, os.path.join(fakebin, "python3"))
        # macOS checks a new executable on its first run (about half a second): pay that here, not in a timed test
        subprocess.run([W["gh"], "warm-up"], capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
        main = W["main"] = os.path.join(root, "acme-widget")
        os.makedirs(main)
        git(main, "init", "-q", "-b", "main")
        write(os.path.join(main, "README.md"), "# acme-widget\n")
        write(os.path.join(main, ".git", "info", "exclude"), ".claude/worktrees/\n")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "First")
        git(main, "remote", "add", "origin", "https://github.com/acme/widget.git")
        git(main, "worktree", "add", "-q", "-b", "t/12-add-the-widget",
            os.path.join(main, ".claude", "worktrees", "t-12-add-the-widget"))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    if LIB not in sys.path:
        sys.path.insert(0, LIB)


def tearDownModule():
    shutil.rmtree(W.get("root", ""), ignore_errors=True)


def lib(name):
    """harness_lib.<name>, imported from this checkout's lib/ (the tools load the same one)."""
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    return importlib.import_module(f"harness_lib.{name}")


def load_tool(path, name):
    from importlib.machinery import SourceFileLoader
    import importlib.util
    loader = SourceFileLoader(name, path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def issue(number):
    return {"number": number, "title": f"Ticket {number}", "url": f"https://github.com/acme/widget/issues/{number}",
            "body": TASK_BODY, "labels": [{"name": n} for n in ("task", "status:ready", "size:S", "risk:low")],
            "state": "OPEN", "parent": None, "blockedBy": [], "comments": []}


def limits(graphql_remaining=2000, core_remaining=4947, reset=1761140000):
    return {"resources": {
        "core": {"limit": 5000, "used": 5000 - core_remaining, "remaining": core_remaining, "reset": reset},
        "graphql": {"limit": 5000, "used": 5000 - graphql_remaining, "remaining": graphql_remaining,
                    "reset": reset}}}


def iso(seconds):
    """Epoch seconds as 2026-10-22T14:05:00Z (whole seconds only)."""
    return dt.datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(seconds):
    """Epoch seconds as 2026-10-22T14:05:00.250Z."""
    when = dt.datetime.fromtimestamp(seconds, UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def epoch(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


REFUSED_VIEW = {"stderr": PRIMARY + "\n", "exit": 1}


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="case-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = tempfile.mkdtemp(prefix="home-", dir=self.tmp)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.fakegh_log = os.path.join(self.tmp, "fakegh.log")
        self.scenario([], default={"stdout": "[]"})

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.fakegh_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "FAKEGH_", "GHTEST_", "GIT_", "CLAUDE_", "GH_"))
               and k not in ("PWD", "OLDPWD")}
        path = [W["fakebin"]] + ([os.path.dirname(GIT)] if os.path.dirname(GIT) not in ("/usr/bin", "/bin") else [])
        env.update(HOME=self.home, PATH=os.pathsep.join(path + ["/usr/bin", "/bin"]), HARNESS_GH=W["gh"],
                   GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=W["root"],
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_POLL_SECONDS="0.01", PYTHONDONTWRITEBYTECODE="1",
                   HARNESS_GH_BACKOFF="0.2", HARNESS_GH_BACKOFF_MAX="5", HARNESS_GH_MAX_WAIT="5",
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.fakegh_log)
        env.update(extra)
        return env

    def run_tool(self, tool, *args, cwd=None, **extra):
        p = subprocess.run([sys.executable, tool] + list(args), cwd=cwd or W["main"], env=self.env(**extra),
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def timed(self, tool, *args, **extra):
        started = time.monotonic()
        p = self.run_tool(tool, *args, **extra)
        return p, time.monotonic() - started

    def gh_dir(self):
        return os.path.join(self.home, ".claude", "harness", "gh")

    def pause_path(self):
        return os.path.join(self.gh_dir(), "pause.json")

    def pause(self):
        with open(self.pause_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def write_pause(self, until, kind="secondary", reason=PRIMARY, strikes=1):
        write(self.pause_path(), json.dumps({"until": until, "kind": kind, "reason": reason, "strikes": strikes,
                                             "at": iso(time.time())}))

    def logged(self):
        folder = self.gh_dir()
        if not os.path.isdir(folder):
            return []
        out = []
        for name in sorted(os.listdir(folder)):
            if name.startswith("calls-") and name.endswith(".jsonl"):
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out.extend(json.loads(raw) for raw in fh if raw.strip())
        return out

    def fakegh_argv(self):
        if not os.path.exists(self.fakegh_log):
            return []
        with open(self.fakegh_log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def calls(self, *prefix):
        return [a for a in self.fakegh_argv() if a[:len(prefix)] == list(prefix)]

    def in_process(self, **extra):
        """Patch HOME (and the HARNESS_GH_* values) for the rest of the test."""
        env = self.env(**extra)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


# ==================================================================== a refused call is retried
@unittest.skipIf(GIT is None, "git not found")
class Retry(Case):
    def test_a_refused_board_read_is_retried_after_the_pause_and_succeeds(self):
        self.scenario([{"match": ["issue", "view", "12"], "responses": [REFUSED_VIEW, {"stdout_json": issue(12)}]},
                       {"match": ["api", "rate_limit"], "stdout_json": limits(graphql_remaining=2000)}])
        p, took = self.timed(BOARD, "show", "12", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["number"], 12)
        self.assertEqual(p.stderr, "")
        self.assertEqual(len(self.calls("issue", "view", "12")), 2, self.fakegh_argv())
        self.assertEqual(self.calls("api", "rate_limit"), [["api", "rate_limit"]])
        self.assertEqual(len(self.fakegh_argv()), 3)
        pause = self.pause()
        self.assertEqual(pause["kind"], "secondary")
        self.assertEqual(pause["strikes"], 1)
        self.assertEqual(pause["reason"], PRIMARY)
        self.assertGreaterEqual(took, 0.18)

    def test_harness_pr_and_harness_work_calls_retry_too(self):
        checks = {"statusCheckRollup": [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED",
                                         "conclusion": "SUCCESS", "detailsUrl": "", "workflowName": "CI"}]}
        with self.subTest("harness-pr checks"):
            self.scenario([{"match": ["pr", "view", "34"], "responses": [REFUSED_VIEW, {"stdout_json": checks}]},
                           {"match": ["api", "rate_limit"], "stdout_json": limits()}])
            p = self.run_tool(PR, "checks", "34", "--json")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(json.loads(p.stdout)["state"], "success")
            self.assertEqual(len(self.calls("pr", "view", "34")), 2, self.fakegh_argv())
            self.assertEqual(len(self.calls("api", "rate_limit")), 1)
        with self.subTest("harness-work worktrees"):
            os.unlink(self.pause_path())
            self.scenario([{"match": ["pr", "list"], "responses": [
                {"stderr": SECONDARY + "\n", "exit": 1},
                {"stdout_json": [{"number": 34, "state": "OPEN", "isDraft": False}]}]},
                {"match": ["api", "rate_limit"], "stdout_json": limits()}])
            p = self.run_tool(WORK, "worktrees", "--json")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertNotIn("warning", p.stderr)
            rows = json.loads(p.stdout)
            self.assertEqual([r["pr"] for r in rows], [{"number": 34, "state": "OPEN", "draft": False}])
            self.assertEqual(len(self.calls("pr", "list")), 2, self.fakegh_argv())
            self.assertEqual(self.pause()["reason"], SECONDARY)

    def test_a_refused_comment_is_posted_once_after_the_pause(self):
        url = "https://github.com/acme/widget/issues/12#issuecomment-1"
        self.scenario([{"match": ["issue", "comment", "12"], "responses": [
            {"stderr": SECONDARY + "\n", "exit": 1}, {"stdout": url + "\n"}]},
            {"match": ["api", "rate_limit"], "stdout_json": limits()}])
        p = self.run_tool(BOARD, "comment", "12", "--body", "Hello")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, url + "\n")
        comments = self.calls("issue", "comment", "12")
        self.assertEqual(len(comments), 2, self.fakegh_argv())
        self.assertEqual(comments[0], comments[1])    # the same argv, run again


# ==================================================================== the pause is machine-wide
@unittest.skipIf(GIT is None, "git not found")
class Shared(Case):
    def test_a_second_process_waits_for_the_pause_without_calling_gh(self):
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
        self.write_pause(iso_ms(time.time() + 0.5))
        p, took = self.timed(BOARD, "show", "12", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertGreaterEqual(took, 0.4)
        self.assertEqual([a[:3] for a in self.fakegh_argv()], [["issue", "view", "12"]])   # only its normal call
        lines = self.logged()
        self.assertEqual(len(lines), 1, lines)
        self.assertGreaterEqual(lines[0]["waited_ms"], 300)
        self.assertIs(lines[0]["refused"], False)

    def test_a_pause_beyond_the_wait_budget_exits_75_with_one_line(self):
        until = iso(time.time() + 300)
        self.write_pause(until, kind="secondary", reason=PRIMARY)
        p, took = self.timed(BOARD, "show", "12", "--json", HARNESS_GH_MAX_WAIT="0.2")
        self.assertEqual(p.returncode, 75, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertEqual(p.stderr, f"harness-board: GitHub rate limit: gh calls are paused until {until} "
                                   f"(secondary: {PRIMARY}){TAIL}\n")
        self.assertEqual(self.fakegh_argv(), [])
        self.assertLess(took, 5)


# ==================================================================== primary and secondary
@unittest.skipIf(GIT is None, "git not found")
class Kinds(Case):
    def test_exhausted_graphql_pauses_until_the_reset(self):
        for bucket in ("graphql", "core"):
            with self.subTest(bucket):
                if os.path.exists(self.pause_path()):
                    os.unlink(self.pause_path())
                reset = int(time.time()) + 1800
                shape = limits(graphql_remaining=0, reset=reset) if bucket == "graphql" else \
                    limits(core_remaining=0, reset=reset)
                self.scenario([{"match": ["issue", "view", "12"], "responses": [REFUSED_VIEW]},
                               {"match": ["api", "rate_limit"], "stdout_json": shape}])
                p = self.run_tool(BOARD, "show", "12", "--json", HARNESS_GH_MAX_WAIT="0")
                self.assertEqual(p.returncode, 75, p.stderr)
                self.assertEqual(p.stderr, f"harness-board: GitHub rate limit: gh calls are paused until "
                                           f"{iso(reset + 5)} (primary: {PRIMARY}){TAIL}\n")
                pause = self.pause()
                self.assertEqual((pause["until"], pause["kind"], pause["reason"]), (iso(reset + 5), "primary", PRIMARY))
                self.assertEqual(len(self.calls("issue", "view")), 1)    # no retry once the budget is outlasted
                self.assertEqual(len(self.calls("api", "rate_limit")), 1)

    def test_secondary_backoff_doubles_per_strike_caps_and_never_shortens(self):
        self.in_process(HARNESS_GH_BACKOFF="10", HARNESS_GH_BACKOFF_MAX="40")
        ghlimit = lib("ghlimit")

        def left(pause):
            return epoch(pause["until"]) - time.time()

        first = ghlimit.record_refusal(SECONDARY)
        self.assertEqual((first["kind"], first["strikes"], first["reason"]), ("secondary", 1, SECONDARY))
        self.assertAlmostEqual(left(self.pause()), 10, delta=1.5)
        self.assertEqual(ghlimit.pause_info()["until"], self.pause()["until"])
        # the previous pause ended just now: each refusal is a further strike, doubled, capped at 40
        for strikes, seconds in ((2, 20), (3, 40), (4, 40)):
            self.write_pause(iso(time.time() - 1), strikes=strikes - 1)
            ghlimit.record_refusal(SECONDARY)
            self.assertEqual(self.pause()["strikes"], strikes)
            self.assertAlmostEqual(left(self.pause()), seconds, delta=1.5)
        # the previous pause ended more than HARNESS_GH_BACKOFF_MAX ago: a first strike again
        self.write_pause(iso(time.time() - 60), strikes=3)
        ghlimit.record_refusal(SECONDARY)
        self.assertEqual(self.pause()["strikes"], 1)
        self.assertAlmostEqual(left(self.pause()), 10, delta=1.5)
        # never shortened: neither a secondary nor a primary refusal cuts a longer pause
        long_until = iso(time.time() + 1000)
        self.write_pause(long_until, kind="manual", reason="set by hand", strikes=0)
        ghlimit.record_refusal(SECONDARY)
        self.assertEqual((self.pause()["until"], self.pause()["kind"]), (long_until, "manual"))
        reset = int(time.time()) + 100
        ghlimit.record_refusal(PRIMARY, {"graphql": {"remaining": 0, "limit": 5000, "reset": reset},
                                         "core": {"remaining": 10, "limit": 5000, "reset": reset}})
        self.assertEqual((self.pause()["until"], self.pause()["kind"]), (long_until, "manual"))
        # a longer primary pause does replace a shorter one
        self.write_pause(iso(time.time() + 5))
        ghlimit.record_refusal(PRIMARY, {"graphql": {"remaining": 0, "limit": 5000, "reset": reset},
                                         "core": {"remaining": 10, "limit": 5000, "reset": reset}})
        self.assertEqual((self.pause()["until"], self.pause()["kind"]), (iso(reset + 5), "primary"))
        # the reason is the first line, at most 200 characters
        os.unlink(self.pause_path())
        ghlimit.record_refusal("x" * 300 + "\nsecond line")
        self.assertEqual(self.pause()["reason"], "x" * 200)

    def test_a_failed_probe_counts_as_secondary(self):
        probes = [("exit 1", {"stderr": "HTTP 502: Bad gateway\n", "exit": 1}),
                  ("not JSON", {"stdout": "<html>"}),
                  ("another shape", {"stdout_json": {"resources": {"graphql": {"remaining": "none"}}}})]
        for label, probe in probes:
            with self.subTest(label):
                if os.path.exists(self.pause_path()):
                    os.unlink(self.pause_path())
                self.scenario([{"match": ["issue", "view", "12"], "responses": [REFUSED_VIEW, {"stdout_json": issue(12)}]},
                               dict({"match": ["api", "rate_limit"]}, **probe)])
                p = self.run_tool(BOARD, "show", "12", "--json")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual((self.pause()["kind"], self.pause()["strikes"]), ("secondary", 1))
                self.assertEqual(len(self.calls("issue", "view")), 2)

    def test_rate_limits_reads_the_resources(self):
        self.in_process()
        self.scenario([{"match": ["api", "rate_limit"], "stdout_json": limits(graphql_remaining=0, reset=1761140000)}])
        self.assertEqual(lib("gh").rate_limits(W["main"]),
                         {"graphql": {"remaining": 0, "limit": 5000, "reset": 1761140000},
                          "core": {"remaining": 4947, "limit": 5000, "reset": 1761140000}})
        self.scenario([{"match": ["api", "rate_limit"], "stdout": "nope"}])
        self.assertIsNone(lib("gh").rate_limits(W["main"]))


# ==================================================================== what counts as a refusal
@unittest.skipIf(GIT is None, "git not found")
class Patterns(Case):
    def test_refusal_texts(self):
        ghlimit = lib("ghlimit")
        for text in ("GraphQL: API rate limit exceeded for user ID 1.", PRIMARY, SECONDARY, "HTTP 429: Too Many Requests",
                     "GraphQL: was submitted too quickly (createIssue)", "HTTP 403: API rate limit exceeded for user ID 1.",
                     "gh: API rate limit already exceeded for user ID 1.",
                     'non-200 OK status code: 429 Too Many Requests body: "{}"',
                     "You have triggered an abuse detection mechanism."):
            self.assertTrue(ghlimit.refused(text), text)
        for text in ("HTTP 404: Not Found", "HTTP 403: Resource not accessible by integration", "HTTP 502: Bad gateway",
                     "GraphQL: Could not resolve to an Issue with the number of 12.", "", None):
            self.assertFalse(ghlimit.refused(text), text)

    def test_a_non_rate_limit_failure_is_not_retried(self):
        self.scenario([{"match": ["issue", "view", "12"], "stderr": "HTTP 404: Not Found\n", "exit": 1}])
        p = self.run_tool(BOARD, "show", "12", "--json")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "harness-board: gh issue view 12 failed: HTTP 404: Not Found\n")
        self.assertEqual(len(self.fakegh_argv()), 1, self.fakegh_argv())
        self.assertFalse(os.path.exists(self.pause_path()))
        self.assertEqual([x["refused"] for x in self.logged()], [False])


# ==================================================================== the pause file
@unittest.skipIf(GIT is None, "git not found")
class Files(Case):
    def test_missing_or_corrupt_pause_file_means_no_pause(self):
        far = iso(time.time() + 300)
        corrupt = [("missing", None), ("not JSON", "{not json"), ("a list", "[1, 2]"),
                   ("until unreadable", json.dumps({"until": "soon", "kind": "secondary"})),
                   ("until missing", json.dumps({"kind": "secondary", "reason": "x"})),
                   ("until a number", json.dumps({"until": time.time() + 300})),
                   ("a folder", "folder"), ("not UTF-8", b"\xff\xfe" + far.encode())]
        for label, content in corrupt:
            with self.subTest(label):
                shutil.rmtree(self.gh_dir(), ignore_errors=True)
                if content == "folder":
                    os.makedirs(self.pause_path())
                elif isinstance(content, bytes):
                    os.makedirs(self.gh_dir())
                    with open(self.pause_path(), "wb") as fh:
                        fh.write(content)
                elif content is not None:
                    write(self.pause_path(), content)
                self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue(12)}])
                p = self.run_tool(BOARD, "show", "12", "--json", HARNESS_GH_MAX_WAIT="0")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(len(self.fakegh_argv()), 1)
                self.assertEqual([x["waited_ms"] for x in self.logged()], [0])

    def test_a_pause_that_cannot_be_written_still_waits_the_backoff(self):
        write(self.gh_dir(), "a file where the folder should be\n")
        self.scenario([{"match": ["issue", "view", "12"], "responses": [REFUSED_VIEW, {"stdout_json": issue(12)}]},
                       {"match": ["api", "rate_limit"], "stdout_json": limits()}])
        p, took = self.timed(BOARD, "show", "12", "--json", HARNESS_GH_BACKOFF="0.3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["number"], 12)
        self.assertGreaterEqual(took, 0.28)
        self.assertEqual(len(self.calls("issue", "view", "12")), 2)


# ==================================================================== the log
@unittest.skipIf(GIT is None, "git not found")
class Log(Case):
    def test_refused_and_waited_attempts_are_logged(self):
        self.scenario([{"match": ["issue", "view", "12"], "responses": [REFUSED_VIEW, {"stdout_json": issue(12)}]},
                       {"match": ["api", "rate_limit"], "stdout_json": limits()}])
        p = self.run_tool(BOARD, "show", "12", "--json", HARNESS_GH_BACKOFF="0.3")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.logged()
        self.assertEqual([(x["cmd"], x["exit"], x["refused"]) for x in lines],
                         [("issue view", 1, True), ("api rate_limit", 0, False), ("issue view", 0, False)])
        self.assertEqual([x["tool"] for x in lines], ["harness-board"] * 3)
        self.assertEqual(lines[0]["waited_ms"], 0)
        self.assertEqual(lines[1]["waited_ms"], 0)
        self.assertGreaterEqual(lines[2]["waited_ms"], 250)
        self.assertLess(lines[2]["waited_ms"], 3000)


# ==================================================================== exit 75
@unittest.skipIf(GIT is None, "git not found")
class Exit(Case):
    UNTIL = "2026-10-22T14:05:00Z"
    TEXT = f"GitHub rate limit: gh calls are paused until {UNTIL} (secondary: {PRIMARY}){TAIL}"

    def paused(self):
        err = lib("ghlimit").GhPaused(until=self.UNTIL, kind="secondary", reason=PRIMARY)
        self.assertIsInstance(err, lib("cli").Problem)
        self.assertEqual(err.exit_code, 75)
        self.assertEqual(err.until, self.UNTIL)
        self.assertEqual(str(err), self.TEXT)
        return err

    def exits(self, call):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as caught:
            call()
        code = caught.exception.code
        if isinstance(code, str):        # sys.exit(text): Python prints it and exits 1
            return 1, stderr.getvalue() + code + "\n"
        return code, stderr.getvalue()

    def test_gh_paused_exits_75_and_other_problems_exit_1(self):
        cli = lib("cli")
        paused, problem = self.paused(), cli.Problem("gh issue view 12 failed: HTTP 404: Not Found")
        self.assertEqual(problem.exit_code, 1)

        def raising(err):
            def main(*_args, **_kw):
                raise err
            return main

        # run_main and exit_problem
        self.assertEqual(self.exits(lambda: cli.run_main("harness-x", raising(paused))),
                         (75, f"harness-x: {self.TEXT}\n"))
        self.assertEqual(self.exits(lambda: cli.run_main("harness-x", raising(problem))),
                         (1, "harness-x: gh issue view 12 failed: HTTP 404: Not Found\n"))
        self.assertEqual(self.exits(lambda: cli.exit_problem("harness-x", paused)), (75, f"harness-x: {self.TEXT}\n"))
        self.assertEqual(self.exits(lambda: cli.exit_problem("harness-x", problem)),
                         (1, "harness-x: gh issue view 12 failed: HTTP 404: Not Found\n"))

        # the three tool mains
        self.in_process()
        board = load_tool(BOARD, "harness_board_pause")
        pr = load_tool(PR, "harness_pr_pause")
        work = load_tool(WORK, "harness_work_pause")
        for err, code in ((paused, 75), (problem, 1)):
            expected = str(err)
            with self.subTest("harness-board", code=code):
                with mock.patch.object(sys, "argv", ["harness-board", "show", "12"]), \
                        mock.patch.object(board, "open_repo", lambda *_: None), \
                        mock.patch.dict(board.COMMANDS, {"show": raising(err)}):
                    self.assertEqual(self.exits(board.main), (code, f"harness-board: {expected}\n"))
            with self.subTest("harness-pr", code=code):
                with mock.patch.object(sys, "argv", ["harness-pr", "checks", "34"]), \
                        mock.patch.dict(pr.COMMANDS, {"checks": raising(err)}):
                    self.assertEqual(self.exits(pr.main), (code, f"harness-pr: {expected}\n"))
            with self.subTest("harness-work", code=code):
                with mock.patch.object(sys, "argv", ["harness-work", "runs"]), \
                        mock.patch.object(work, "cmd_runs", raising(err)):
                    self.assertEqual(self.exits(work.main), (code, f"harness-work: {expected}\n"))


if __name__ == "__main__":
    unittest.main()
