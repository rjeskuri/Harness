"""Tests for harness-digest, driven by the fake gh (fakegh.py).

    python3 -m unittest discover -s plugins/harness/tests -v

The repositories are built once for the module: alpha has an `origin` on github.com (never contacted) and
ticket worktrees under .claude/worktrees/ (one stale, one old but with an open PR, one fresh) plus an old
worktree elsewhere; beta has no remote; gamma has an origin and nothing waiting. Every test has its own
temporary HOME with the registry, budgets.json and run records. A `fakebin` first on PATH holds gh (a
wrapper around fakegh.py) and recorders for terminal-notifier, osascript and launchctl, so nothing here
reads the real ~/.claude, shows a notification, loads a LaunchAgent or needs the network. The fake
executables are written once: macOS checks a new executable on its first run, which costs time.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

sys.dont_write_bytecode = True      # importing the bin script must not leave a __pycache__ in bin/

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-digest")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
UTC = dt.timezone.utc
DAY = 86400
LABEL = "com.ryaneskuri.harness-digest"

RECORDER = """import json, os, sys
name, args = os.path.basename(sys.argv[1]), sys.argv[2:]
log = os.environ.get("LAUNCHCTL_LOG" if name == "launchctl" else "NOTIFY_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps([name] + args) + "\\n")
if name == "launchctl" and args and args[0] in os.environ.get("LAUNCHCTL_FAIL", "").split(","):
    sys.stderr.write(args[0] + " failed: 5: Input/output error\\n")
    sys.exit(5)
"""


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def load_tool():
    loader = SourceFileLoader("harness_digest", TOOL)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("harness_digest", loader))
    loader.exec_module(module)
    return module


def z(seconds_ago):
    """An ISO-8601 UTC time `seconds_ago` before now, as GitHub prints it."""
    return dt.datetime.fromtimestamp(time.time() - seconds_ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(path):
    return hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()[:12]


def base_env(home):
    """A clean environment: the fake HOME, fakebin first on PATH, no harness or git variables from outside."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_", "LAUNCHCTL_", "NOTIFY_"))}
    env.update(HOME=home, PATH=os.pathsep.join([W["fakebin"], "/usr/bin", "/bin"]),
               HARNESS_GH=os.path.join(W["fakebin"], "gh"), GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    if GIT and os.path.dirname(GIT) not in ("/usr/bin", "/bin"):
        env["PATH"] = os.pathsep.join([W["fakebin"], os.path.dirname(GIT), "/usr/bin", "/bin"])
    return env


W = {}


def git(repo, *args, when=None):
    env = base_env(W["githome"])
    if when is not None:
        stamp = dt.datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
                          + list(args), env=env, capture_output=True, text=True, check=True)


def commit(repo, name, message, when=None):
    write(os.path.join(repo, name), message + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, when=when)


def worktree(repo, path, branch, when):
    git(repo, "worktree", "add", "-q", "-b", branch, path)
    commit(path, f"{branch.replace('/', '-')}.txt", f"Work on {branch}", when=when)


def setUpModule():
    if not GIT:
        raise unittest.SkipTest("git is not installed")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-digest-"))
    W.update(root=root, fakebin=os.path.join(root, "fakebin"), slowbin=os.path.join(root, "slowbin"),
             githome=os.path.join(root, "githome"))
    try:
        os.makedirs(W["githome"])
        py = sys.executable
        recorder = write(os.path.join(root, "recorder.py"), RECORDER)
        executable(os.path.join(W["fakebin"], "gh"), f'#!/bin/sh\nexec "{py}" "{FAKEGH}" "$@"\n')
        for name in ("terminal-notifier", "osascript", "launchctl"):
            executable(os.path.join(W["fakebin"], name), f'#!/bin/sh\nexec "{py}" "{recorder}" "$0" "$@"\n')
        executable(os.path.join(W["slowbin"], "gh"), "#!/bin/sh\nexec sleep 30\n")

        now = time.time()
        W["alpha"] = alpha = os.path.join(root, "alpha")
        os.makedirs(alpha)
        git(alpha, "init", "-q")
        write(os.path.join(alpha, ".git", "info", "exclude"), ".claude/worktrees/\n")
        commit(alpha, "README.md", "Initial commit")
        git(alpha, "remote", "add", "origin", "https://github.com/o/alpha.git")
        trees = os.path.join(alpha, ".claude", "worktrees")
        worktree(alpha, os.path.join(trees, "t-3-old"), "t/3-old", now - 10 * DAY)       # stale
        worktree(alpha, os.path.join(trees, "t-4-pr"), "t/4-pr", now - 10 * DAY)         # old, but has an open PR
        worktree(alpha, os.path.join(trees, "t-5-fresh"), "t/5-fresh", now - 3600)       # recent
        worktree(alpha, os.path.join(root, "alpha-side"), "side", now - 30 * DAY)        # not a ticket worktree

        W["beta"] = beta = os.path.join(root, "beta")                                    # no remote
        os.makedirs(beta)
        git(beta, "init", "-q")
        commit(beta, "README.md", "Initial commit")

        W["gamma"] = gamma = os.path.join(root, "gamma")                                 # origin, nothing waiting
        os.makedirs(gamma)
        git(gamma, "init", "-q")
        commit(gamma, "README.md", "Initial commit")
        git(gamma, "remote", "add", "origin", "git@github.com:o/gamma.git")
    except BaseException:
        shutil.rmtree(root, True)
        raise


def tearDownModule():
    shutil.rmtree(W.get("root", ""), True)


def alpha_scenario():
    url = "https://github.com/o/alpha"
    return {"calls": [
        {"contains": ["pr", "list", "@me", "o/alpha"], "stdout_json": [
            {"number": 7, "title": "Add the parser", "isDraft": False, "createdAt": z(2 * DAY + 60),
             "url": f"{url}/pull/7", "headRefName": "t/4-pr"},
            {"number": 8, "title": "Half done", "isDraft": True, "createdAt": z(DAY),
             "url": f"{url}/pull/8", "headRefName": "t/9-half"}]},
        {"contains": ["issue", "list", "status:needs-input", "o/alpha"], "stdout_json": [
            {"number": 12, "title": "Pick a name", "url": f"{url}/issues/12", "comments": [
                {"author": {"login": "ryan"}, "createdAt": z(3 * DAY),
                 "body": "**Needs your input**\n\nAn older question?"},
                {"author": {"login": "ryan"}, "createdAt": z(DAY),
                 "body": "**Needs your input**\n\nWhich name should the export use?\n\n```\nevidence\n```\n\n"
                         "Reply in a comment; /harness:work 12 picks it up."},
                {"author": {"login": "someone"}, "createdAt": z(3600), "body": "Still thinking."}]},
            {"number": 13, "title": "Asked elsewhere", "url": f"{url}/issues/13", "comments": []}]},
        {"contains": ["issue", "list", "status:in-progress", "o/alpha"], "stdout_json": [
            {"number": 5, "title": "Old work", "createdAt": z(5 * DAY + 60), "url": f"{url}/issues/5"},
            {"number": 4, "title": "Has a PR", "createdAt": z(6 * DAY), "url": f"{url}/issues/4"},
            {"number": 6, "title": "New work", "createdAt": z(DAY), "url": f"{url}/issues/6"}]},
        {"contains": ["pr", "list", "headRefName", "o/alpha"], "stdout_json": [
            {"headRefName": "t/4-pr"}, {"headRefName": "t/50-other"}, {"headRefName": "t/9-half"}]},
    ], "default": {"stdout": "[]"}}


ALPHA_LINES = [
    "## alpha",
    "",
    "### PRs awaiting review",
    "- #7 Add the parser (opened 2d ago) <https://github.com/o/alpha/pull/7>",
    "",
    "### Needs input",
    "- #12 Pick a name: Which name should the export use? <https://github.com/o/alpha/issues/12>",
    "- #13 Asked elsewhere <https://github.com/o/alpha/issues/13>",
    "",
    "### In progress over 3 days without a PR",
    "- #5 Old work (opened 5d ago) <https://github.com/o/alpha/issues/5>",
    "",
    "### Stale worktrees",
    "- t/3-old at .claude/worktrees/t-3-old (last commit 10d ago, no open PR)",
    "",
]


class Digest(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="test-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".claude", "harness")
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.ghlog, self.notifylog = os.path.join(self.tmp, "gh.log"), os.path.join(self.tmp, "notify.log")
        self.launchlog = os.path.join(self.tmp, "launchctl.log")

    # ------------------------------------------------------------ helpers
    def registry(self, *names):
        write(os.path.join(self.state, "projects.json"), json.dumps(
            {"version": 1, "projects": [{"path": W[n], "name": n, "billable": False, "client": "", "notes": ""}
                                        for n in names]}))

    def world(self):
        """alpha and beta registered, alpha's scenario, an exhausted budget and a set of run records."""
        self.registry("alpha", "beta")
        self.gh(alpha_scenario())
        self.until = z(-DAY)
        write(os.path.join(self.state, "budgets.json"), json.dumps({
            "api": {"exhausted_until": self.until, "reason": "credit balance too low"},
            "cloud": {"exhausted_until": None, "reason": ""},
            "old": {"exhausted_until": z(3600), "reason": "expired already"}}))
        k = key(W["alpha"])
        runs = {
            f"{k}-12-1.json": {"ticket": 12, "repo": W["alpha"], "venue": "headless", "outcome": "billing", "exit": 1,
                               "started_at": z(3 * 3600), "ended_at": z(2 * 3600), "log": "/logs/12.log"},
            f"{k}-9-1.json": {"ticket": 9, "venue": "bg", "outcome": "error", "exit": 2,          # no repo: by key
                              "started_at": z(6 * 3600), "ended_at": z(5 * 3600), "log": "/logs/9.log"},
            "0123456789ab-3-1.json": {"ticket": 3, "repo": "/somewhere/else/proj", "venue": "headless",
                                      "outcome": "auth", "exit": 1, "ended_at": z(30 * 60)},
            f"{k}-11-1.json": {"ticket": 11, "repo": W["alpha"], "venue": "headless", "outcome": "ok", "exit": 0,
                               "ended_at": z(3600)},
            f"{k}-8-1.json": {"ticket": 8, "repo": W["alpha"], "venue": "headless", "outcome": "error", "exit": 1,
                              "ended_at": z(3 * DAY)},
            f"{k}-14-1.json": {"ticket": 14, "repo": W["alpha"], "venue": "headless", "pid": os.getpid(),
                               "started_at": z(600), "ended_at": None, "exit": None, "outcome": None},
        }
        for name, record in runs.items():
            write(os.path.join(self.state, "runs", name), json.dumps(dict(record, id=name[:-5])))

    def gh(self, scenario):
        write(self.scenario, json.dumps(scenario))

    def env(self, **extra):
        env = base_env(self.home)
        env.update(FAKEGH_SCENARIO=self.scenario, FAKEGH_LOG=self.ghlog, NOTIFY_LOG=self.notifylog,
                   LAUNCHCTL_LOG=self.launchlog, HARNESS_LAUNCHCTL=os.path.join(W["fakebin"], "launchctl"))
        env.update(extra)
        return env

    def run_tool(self, *args, **extra):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=self.tmp, env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)

    # ------------------------------------------------------------ the digest
    def test_markdown_file_and_notification(self):
        self.world()
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        today = dt.date.today().isoformat()
        with open(os.path.join(self.state, "digest", f"{today}.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), p.stdout)                       # printed what it wrote
        self.assertEqual(p.stdout.splitlines(), [f"# Harness digest, {today}", ""] + ALPHA_LINES + [
            "## Budgets",
            f"- api exhausted until {self.until} (credit balance too low)",
            "",
            "## Runs",
            "- proj #3 headless: auth (exit 1), ended 30m ago",
            "- alpha #12 headless: billing (exit 1), ended 2h ago; log /logs/12.log",
            "- alpha #9 bg: error (exit 2), ended 5h ago; log /logs/9.log"])
        self.assertNotIn("beta", p.stdout)                               # no origin: not read, not shown
        self.assertEqual(self.lines(self.notifylog), [[
            "terminal-notifier", "-title", "Harness", "-message", "Harness digest: 1 PRs to review, 2 need input, 2 stale",
            "-group", "harness-digest", "-activate", "com.microsoft.VSCode", "-sound", "Glass"]])
        calls = [c["argv"] for c in self.lines(self.ghlog)]
        self.assertEqual(len(calls), 4)                                  # four lists, alpha only, all with -R
        self.assertTrue(all(c[-2:] == ["-R", "o/alpha"] for c in calls), calls)
        self.assertIn(["pr", "list", "--author", "@me", "--state", "open", "--json",
                       "number,title,isDraft,createdAt,url,headRefName", "--limit", "100", "-R", "o/alpha"], calls)

    def test_failed_runs_since_the_previous_digest(self):
        self.world()
        write(os.path.join(self.state, "digest", "2026-01-01.md"), "old\n", mtime=time.time() - 3 * 3600)
        write(os.path.join(self.state, "digest", "notes.md"), "not a digest\n", mtime=time.time() - 60)
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        runs = p.stdout.split("## Runs\n", 1)[1].splitlines()
        self.assertEqual(runs, ["- proj #3 headless: auth (exit 1), ended 30m ago",
                                "- alpha #12 headless: billing (exit 1), ended 2h ago; log /logs/12.log"])

    def test_a_same_day_rerun_keeps_the_window_back_to_the_previous_days_digest(self):
        # Yesterday's digest was written 6h ago; today's already exists (written a minute ago) and the tool is then
        # run twice more. Today's file is the one each run rewrites, so it never moves the window: every run lists
        # #3 (30m), #12 (2h) and #9 (5h), which ended after yesterday's digest but before today's first run. #19
        # (7h) ended before yesterday's digest, so that digest bounds the window: without it the default window
        # (a day) would list #19 too; #8 (3d) and #11 (ok) are never listed.
        self.world()
        k = key(W["alpha"])
        write(os.path.join(self.state, "runs", f"{k}-19-1.json"), json.dumps(
            {"id": f"{k}-19-1", "ticket": 19, "repo": W["alpha"], "venue": "headless", "outcome": "error", "exit": 1,
             "ended_at": z(7 * 3600)}))
        now = time.time()
        today = dt.date.today()
        folder = os.path.join(self.state, "digest")
        write(os.path.join(folder, f"{today - dt.timedelta(days=1)}.md"), "yesterday\n", mtime=now - 6 * 3600)
        write(os.path.join(folder, f"{today - dt.timedelta(days=4)}.md"), "older\n", mtime=now - 4 * DAY)
        write(os.path.join(folder, f"{today}.md"), "today, first run\n", mtime=now - 60)
        expected = ["proj #3", "alpha #12", "alpha #9"]
        for attempt in ("first rerun", "second rerun"):
            with self.subTest(attempt):
                p = self.run_tool()
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("## Runs\n", p.stdout)
                runs = p.stdout.split("## Runs\n", 1)[1].splitlines()
                tickets = [m.group(1) for line in runs for m in [re.match(r"- (\w+ #\d+) ", line)] if m]
                self.assertEqual(tickets, expected, runs)
        data = json.loads(self.run_tool("--json").stdout)
        self.assertEqual([(r["project"], r["ticket"]) for r in data["runs"]], [("proj", 3), ("alpha", 12), ("alpha", 9)])

        # Without yesterday's digest (only today's and the 4-day-old one), the window reaches back to that older
        # digest, so #19 (7h) and #8 (3d) join the list: yesterday's digest was what excluded them, and today's
        # still does not count.
        os.unlink(os.path.join(folder, f"{today - dt.timedelta(days=1)}.md"))
        data = json.loads(self.run_tool("--json").stdout)
        self.assertEqual([(r["project"], r["ticket"]) for r in data["runs"]],
                         [("proj", 3), ("alpha", 12), ("alpha", 9), ("alpha", 19), ("alpha", 8)])

    def test_runs_that_ended_early_are_listed_with_the_failed_ones(self):
        # `early` (harness-work runs --sweep: an ok run that left its ticket in progress with no PR) is listed with
        # billing, auth and error, newest first, within the same window. #15 is #11 (ok, not listed) but for its
        # outcome; #16 ended early before the window; #17 belongs to another repository, as #3 does.
        self.world()
        k = key(W["alpha"])
        early = {
            f"{k}-15-1.json": {"ticket": 15, "repo": W["alpha"], "venue": "headless", "outcome": "early", "exit": None,
                               "started_at": z(3 * 3600), "ended_at": z(90 * 60), "log": "/logs/15.log"},
            f"{k}-16-1.json": {"ticket": 16, "repo": W["alpha"], "venue": "headless", "outcome": "early", "exit": None,
                               "ended_at": z(3 * DAY), "log": "/logs/16.log"},
            "0123456789ab-17-1.json": {"ticket": 17, "repo": "/somewhere/else/proj", "venue": "headless",
                                       "outcome": "early", "exit": None, "ended_at": z(10 * 60)},
            f"{k}-18-1.json": {"ticket": 18, "repo": W["alpha"], "venue": "bg", "outcome": "early", "exit": None,
                               "ended_at": z(4 * 3600)},
        }
        for name, record in early.items():
            write(os.path.join(self.state, "runs", name), json.dumps(dict(record, id=name[:-5])))
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        runs = p.stdout.split("## Runs\n", 1)[1].splitlines()
        tickets = [m.group(1) for line in runs for m in [re.match(r"- (\w+ #\d+) ", line)] if m]
        self.assertEqual(tickets, ["proj #17", "proj #3", "alpha #15", "alpha #12", "alpha #18", "alpha #9"], runs)
        by = dict(zip(tickets, runs))
        for name in ("proj #17", "alpha #15", "alpha #18"):
            with self.subTest(run=name):
                self.assertIn("early", by[name])
                self.assertNotRegex(by[name], r"\b(?:billing|auth|error)\b")
        self.assertIn("/logs/15.log", by["alpha #15"])
        self.assertIn("1h ago", by["alpha #15"])
        self.assertIn(" bg", by["alpha #18"])
        self.assertEqual(by["alpha #12"], "- alpha #12 headless: billing (exit 1), ended 2h ago; log /logs/12.log")

        data = json.loads(self.run_tool("--json").stdout)
        self.assertEqual([(r["project"], r["ticket"], r["outcome"]) for r in data["runs"]],
                         [("proj", 17, "early"), ("proj", 3, "auth"), ("alpha", 15, "early"), ("alpha", 12, "billing"),
                          ("alpha", 18, "early"), ("alpha", 9, "error")])

    def test_json(self):
        self.world()
        p = self.run_tool("--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["counts"], {"prs": 1, "needs_input": 2, "stale": 2})
        self.assertTrue(data["notified"])
        self.assertTrue(os.path.isfile(data["file"]))
        self.assertEqual(data["date"], dt.date.today().isoformat())
        self.assertEqual(data["skipped"], [{"name": "beta", "path": W["beta"], "why": "no origin remote"}])
        (alpha,) = data["projects"]
        self.assertEqual((alpha["name"], alpha["repo"], alpha["error"]), ("alpha", "o/alpha", None))
        self.assertEqual([(x["number"], x["age"], x["branch"]) for x in alpha["prs"]], [(7, "2d ago", "t/4-pr")])
        self.assertEqual([(x["number"], x["question"]) for x in alpha["needs_input"]],
                         [(12, "Which name should the export use?"), (13, None)])
        self.assertEqual([x["number"] for x in alpha["in_progress"]], [5])
        (stale,) = alpha["stale_worktrees"]
        self.assertEqual((stale["branch"], stale["relative"], stale["last_commit"]["age"]),
                         ("t/3-old", os.path.join(".claude", "worktrees", "t-3-old"), "10d ago"))
        self.assertEqual(data["budgets"], [{"name": "api", "until": self.until, "reason": "credit balance too low"}])
        self.assertEqual([(r["project"], r["ticket"], r["outcome"], r["age"]) for r in data["runs"]],
                         [("proj", 3, "auth", "30m ago"), ("alpha", 12, "billing", "2h ago"),
                          ("alpha", 9, "error", "5h ago")])

    def test_nothing_waiting_is_silent(self):
        self.registry("beta", "gamma")
        self.gh({"calls": [], "default": {"stdout": "[]"}})
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, f"# Harness digest, {dt.date.today().isoformat()}\n\nNothing waiting on you.\n")
        self.assertEqual(self.lines(self.notifylog), [])                 # no notification at all
        calls = [c["argv"] for c in self.lines(self.ghlog)]
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c[-2:] == ["-R", "o/gamma"] for c in calls), calls)

    def test_no_projects_registered(self):
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("No projects registered", p.stdout)
        self.assertIn("Nothing waiting on you.", p.stdout)

    def test_a_project_that_cannot_be_read_gets_one_line(self):
        self.registry("alpha", "gamma")
        self.gh({"calls": [{"contains": ["o/alpha"], "stderr": "HTTP 502: Bad Gateway\nmore detail", "exit": 1}],
                 "default": {"stdout": "[]"}})
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines()[2:], ["## alpha", "- could not read it: gh pr list failed: HTTP 502: Bad Gateway"])
        self.assertEqual(self.lines(self.notifylog), [])
        p = self.run_tool(HARNESS_GH=os.path.join(self.tmp, "no-such-gh"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("## alpha\n- could not read it: gh not found", p.stdout)
        self.assertIn("## gamma\n- could not read it: gh not found", p.stdout)

    def test_gh_gets_six_seconds_a_project(self):
        tool = load_tool()
        tool.GH_BUDGET = 1
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"HOME": self.home}):       # #187: the gh log goes to the test's HOME
            out = tool.project_digest({"name": "gamma", "path": W["gamma"]}, os.path.join(W["slowbin"], "gh"),
                                      time.time())
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(out["error"], "gh pr list timed out after 1 s")

    def test_bad_invocations_are_one_line(self):
        self.assertOneLineError(self.run_tool("--bogus"), "unrecognized arguments")
        self.assertOneLineError(self.run_tool("--install-launchd", "--uninstall-launchd"), "not allowed with")
        write(os.path.join(self.state, "projects.json"), "{not json")
        self.assertOneLineError(self.run_tool(), "not valid JSON")

    # ------------------------------------------------------------ launchd
    def test_install_and_uninstall_launchd(self):
        plist = os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")
        uid, log = os.getuid(), os.path.join(self.state, "digest", "launchd.log")
        job_path = f"/opt/homebrew/bin:/usr/local/bin:{self.home}/.local/bin:/usr/bin:/bin"
        p = self.run_tool("--install-launchd")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("launchctl bootstrap", p.stdout)
        with open(plist, "rb") as fh:
            job = plistlib.load(fh)
        self.assertEqual(job, {
            "Label": LABEL,
            "ProgramArguments": [shutil.which("python3", path=job_path) or sys.executable, os.path.realpath(TOOL)],
            "StartCalendarInterval": {"Hour": 8, "Minute": 30},
            "EnvironmentVariables": {"PATH": job_path},
            "StandardOutPath": log, "StandardErrorPath": log, "RunAtLoad": False})
        self.assertTrue(os.path.isdir(os.path.dirname(log)))
        self.assertEqual(self.lines(self.launchlog), [["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                                                      ["launchctl", "bootstrap", f"gui/{uid}", plist]])

        os.remove(self.launchlog)                                       # bootstrap fails: load -w instead
        p = self.run_tool("--install-launchd", LAUNCHCTL_FAIL="bootout,bootstrap")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("load -w", p.stdout)
        self.assertEqual(self.lines(self.launchlog)[1:], [["launchctl", "bootstrap", f"gui/{uid}", plist],
                                                          ["launchctl", "load", "-w", plist]])
        self.assertOneLineError(self.run_tool("--install-launchd", LAUNCHCTL_FAIL="bootstrap,load"),
                                "could not load it")

        os.remove(self.launchlog)
        p = self.run_tool("--uninstall-launchd")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.lines(self.launchlog), [["launchctl", "bootout", f"gui/{uid}/{LABEL}"]])
        self.assertFalse(os.path.exists(plist))
        self.assertIn(f"removed {plist}", p.stdout)
        p = self.run_tool("--uninstall-launchd", LAUNCHCTL_FAIL="bootout")    # again: nothing there, still exit 0
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("was not loaded", p.stdout)
        self.assertIn(f"no {plist} to remove", p.stdout)


if __name__ == "__main__":
    unittest.main()
