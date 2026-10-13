"""Tests for harness-standup's v2 sections: the board lines per project, Budgets and Runs.

    python3 -m unittest discover -s plugins/harness/tests -v

The repositories are built once for the module: alpha has an `origin` on github.com (never contacted) and
ticket worktrees under .claude/worktrees/ (one stale, one old but with an open PR, one fresh); beta has an
origin whose gh calls fail (offline); gamma has no remote. Every test has its own temporary HOME with the
registry, budgets.json and run records. `fakebin` first on PATH holds gh (a wrapper around fakegh.py) and
a `claude` that answers `agents --json` with an empty fleet, written once for the module (macOS checks a
new executable on its first run, which costs time). The v1 behaviour is covered by test_tools.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-standup")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
UTC = dt.timezone.utc
DAY = 86400
W = {}


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


def z(seconds_ago):
    """An ISO-8601 UTC time `seconds_ago` before now, as GitHub prints it."""
    return dt.datetime.fromtimestamp(time.time() - seconds_ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(path):
    return hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()[:12]


def base_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_"))}
    folders = [W["fakebin"]] + ([os.path.dirname(GIT)] if GIT and os.path.dirname(GIT) not in ("/usr/bin", "/bin")
                                else []) + ["/usr/bin", "/bin"]
    env.update(HOME=home, PATH=os.pathsep.join(folders), HARNESS_GH=os.path.join(W["fakebin"], "gh"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=W["root"],
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    return env


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


def new_repo(name, origin=None):
    path = os.path.join(W["root"], name)
    os.makedirs(path)
    git(path, "init", "-q")
    write(os.path.join(path, ".git", "info", "exclude"), ".claude/worktrees/\n")
    commit(path, "README.md", "Initial commit")
    if origin:
        git(path, "remote", "add", "origin", origin)
    W[name] = path
    return path


def setUpModule():
    if not GIT:
        raise unittest.SkipTest("git is not installed")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-standup2-"))
    W.update(root=root, fakebin=os.path.join(root, "fakebin"), githome=os.path.join(root, "githome"))
    try:
        os.makedirs(W["githome"])
        executable(os.path.join(W["fakebin"], "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
        executable(os.path.join(W["fakebin"], "claude"),
                   '#!/bin/sh\n[ "$1 $2" = "agents --json" ] || exit 64\necho "[]"\n')
        now = time.time()
        alpha = new_repo("alpha", "https://github.com/o/alpha.git")
        for name, branch, when in (("t-3-old", "t/3-old", now - 10 * DAY), ("t-4-pr", "t/4-pr", now - 10 * DAY),
                                   ("t-5-fresh", "t/5-fresh", now - 3600)):
            tree = os.path.join(alpha, ".claude", "worktrees", name)
            git(alpha, "worktree", "add", "-q", "-b", branch, tree)
            commit(tree, f"{name}.txt", f"Work on {branch}", when=when)
        new_repo("beta", "git@github.com:o/beta.git")
        new_repo("gamma")
        proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])          # a pid that is gone by the time it is used
        proc.wait()
        W["dead_pid"] = proc.pid
    except BaseException:
        shutil.rmtree(root, True)
        raise


def tearDownModule():
    shutil.rmtree(W.get("root", ""), True)


def scenario():
    url = "https://github.com/o/alpha"
    labels = [["task", "status:ready"], ["bug", "status:ready"], ["epic", "status:ready"], ["status:in-progress"],
              ["status:in-review"], ["status:needs-input"], ["chore", "status:needs-input"], []]
    return {"calls": [
        {"contains": ["o/beta"], "stderr": "error connecting to api.github.com\ncheck your internet connection",
         "exit": 1},
        {"contains": ["issue", "list", "labels", "o/alpha"],
         "stdout_json": [{"labels": [{"name": n} for n in names]} for names in labels]},
        {"contains": ["pr", "list", "@me", "o/alpha"], "stdout_json": [
            {"number": 7, "title": "Add the parser", "isDraft": False, "createdAt": z(2 * DAY + 60),
             "url": f"{url}/pull/7", "headRefName": "t/4-pr"},
            {"number": 8, "title": "Half done", "isDraft": True, "createdAt": z(DAY),
             "url": f"{url}/pull/8", "headRefName": "t/9-half"}]},
        {"contains": ["issue", "list", "status:needs-input", "o/alpha"], "stdout_json": [
            {"number": 12, "title": "Pick a name", "url": f"{url}/issues/12", "comments": [
                {"author": {"login": "ryan"}, "createdAt": z(3 * DAY), "body": "**Needs your input**\n\nOlder?"},
                {"author": {"login": "ryan"}, "createdAt": z(DAY),
                 "body": "**Needs your input**\n\nWhich name should the export use?\n\nReply in a comment."}]},
            {"number": 13, "title": "Asked elsewhere", "url": f"{url}/issues/13", "comments": []}]},
        {"contains": ["pr", "list", "headRefName", "o/alpha"], "stdout_json": [{"headRefName": "t/4-pr"}]},
    ]}


ALPHA_V2 = [
    "- Board: 2 ready, 1 in progress, 1 in review, 2 needs input",
    "- PRs awaiting you:",
    "  - #7 Add the parser (opened 2d ago)",
    "- Needs input:",
    "  - #12 Pick a name: Which name should the export use?",
    "  - #13 Asked elsewhere",
    "- Stale worktrees:",
    "  - t/3-old at .claude/worktrees/t-3-old, last commit 10d ago",
]


class StandupV2(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="test-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".claude", "harness")
        self.scenario, self.ghlog = os.path.join(self.tmp, "scenario.json"), os.path.join(self.tmp, "gh.log")
        write(self.scenario, json.dumps(scenario()))
        write(os.path.join(self.state, "projects.json"), json.dumps({"version": 1, "projects": [
            {"path": W[n], "name": n, "billable": False, "client": "", "notes": ""} for n in ("alpha", "beta", "gamma")]}))
        self.until = z(-DAY)
        write(os.path.join(self.state, "budgets.json"), json.dumps({
            "api": {"exhausted_until": self.until, "reason": "credit balance too low"},
            "cloud": {"exhausted_until": z(60), "reason": "expired a minute ago"}}))
        k = key(W["alpha"])
        log16 = write(os.path.join(self.tmp, "logs", "16.log"), "partial output\n", mtime=time.time() - 20 * 60)
        runs = {
            "14": {"venue": "headless", "pid": os.getpid(), "started_at": z(2 * 3600), "ended_at": None,
                   "outcome": None, "exit": None},                                         # alive
            "15": {"venue": "bg", "bg_id": "bg-15", "started_at": z(10 * 60), "ended_at": None, "outcome": None},
            "16": {"venue": "headless", "pid": W["dead_pid"], "started_at": z(3600), "ended_at": None,
                   "outcome": None, "log": log16},                                         # gone, not swept
            "11": {"venue": "headless", "started_at": z(2 * 3600), "ended_at": z(3600), "outcome": "ok", "exit": 0,
                   "log": "/logs/11.log"},
            "9": {"venue": "bg", "started_at": z(4 * 3600), "ended_at": z(3 * 3600), "outcome": "error", "exit": 2,
                  "log": "/logs/9.log"},
            "8": {"venue": "headless", "started_at": z(31 * 3600), "ended_at": z(30 * 3600), "outcome": "billing",
                  "exit": 1},                                                              # over a day ago
        }
        for ticket, record in runs.items():
            write(os.path.join(self.state, "runs", f"{k}-{ticket}-20261013070000.json"),
                  json.dumps(dict(record, id=f"{k}-{ticket}", ticket=int(ticket), repo=W["alpha"],
                                  worktree=None, branch=f"t/{ticket}-x")))

    def run_tool(self, *args, **extra):
        env = base_env(self.home)
        env.update(FAKEGH_SCENARIO=self.scenario, FAKEGH_LOG=self.ghlog)
        env.update(extra)
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=self.tmp, env=env, capture_output=True,
                              text=True, timeout=60)

    def sections(self, text):
        """{'## heading': [its non-blank lines]} in order."""
        out, current = {}, None
        for line in text.splitlines():
            if line.startswith("## "):
                current = line
                out[current] = []
            elif line.strip() and current:
                out[current].append(line)
        return out

    def test_markdown(self):
        started = time.monotonic()
        p = self.run_tool()
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.sections(p.stdout)
        alpha, beta, gamma = (f"## {n} `{W[n]}`" for n in ("alpha", "beta", "gamma"))
        self.assertEqual(list(s), [alpha, beta, gamma, "## Fleet", "## Budgets", "## Runs"])
        self.assertTrue(s[alpha][0].startswith("- git: main, 0 uncommitted, last commit "), s[alpha])
        self.assertEqual(s[alpha][1:], ["- last session: none recorded", "- next: none recorded"] + ALPHA_V2
                         + ["- sessions: none"])
        self.assertTrue(s[beta][0].startswith("- git: main, 0 uncommitted, last commit "), s[beta])   # offline: v1 lines
        self.assertEqual(s[beta][1:], ["- last session: none recorded", "- next: none recorded",
                                       "- Board: could not read it (gh issue list failed: error connecting to "
                                       "api.github.com)", "- sessions: none"])
        self.assertNotIn("Board", "\n".join(s[gamma]))                 # no origin: no board lines
        self.assertEqual(s["## Fleet"], ["- no sessions running"])
        self.assertEqual(s["## Budgets"], [f"- api exhausted until {self.until} (credit balance too low)"])
        self.assertEqual(s["## Runs"], [
            "- alpha #15 bg: running, started 10m ago",
            "- alpha #14 headless: running, started 2h ago",
            f"- alpha #16 headless: exited, ended 20m ago, not swept yet; log {self.tmp}/logs/16.log",
            "- alpha #11 headless: ok (exit 0), ended 1h ago",
            "- alpha #9 bg: error (exit 2), ended 3h ago; log /logs/9.log"])
        with open(self.ghlog, encoding="utf-8") as fh:
            calls = [json.loads(line)["argv"] for line in fh]
        repos = [c[-2:] for c in calls]
        self.assertEqual(repos.count(["-R", "o/alpha"]), 4)
        # beta's first failure stops its other calls, some before the fake gh logs them
        self.assertGreaterEqual(repos.count(["-R", "o/beta"]), 1)
        self.assertEqual(len(repos), 4 + repos.count(["-R", "o/beta"]))
        self.assertIn(["issue", "list", "--state", "open", "--json", "labels", "--limit", "200", "-R", "o/alpha"], calls)

    def test_json(self):
        p = self.run_tool("--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        alpha, beta, gamma = data["projects"]
        self.assertEqual(alpha["board"], {"ready": 2, "in_progress": 1, "in_review": 1, "needs_input": 2})
        self.assertIsNone(alpha["board_error"])
        self.assertEqual([(x["number"], x["age"]) for x in alpha["prs_awaiting"]], [(7, "2d ago")])
        self.assertEqual([(x["number"], x["question"]) for x in alpha["needs_input"]],
                         [(12, "Which name should the export use?"), (13, None)])
        self.assertEqual([w["branch"] for w in alpha["stale_worktrees"]], ["t/3-old"])
        self.assertEqual({k: beta[k] for k in ("board", "prs_awaiting", "needs_input", "stale_worktrees")},
                         dict.fromkeys(("board", "prs_awaiting", "needs_input", "stale_worktrees")))
        self.assertTrue(beta["board_error"].startswith("gh issue list failed: error connecting"), beta["board_error"])
        self.assertEqual(beta["git"]["branch"], "main")                 # the v1 fields are all there
        self.assertEqual({k: gamma[k] for k in ("board", "board_error", "stale_worktrees")},
                         {"board": None, "board_error": None, "stale_worktrees": None})
        self.assertEqual(data["budgets"], [{"name": "api", "until": self.until, "reason": "credit balance too low"}])
        self.assertEqual([(r["ticket"], r["state"]) for r in data["runs"]],
                         [(15, "running"), (14, "running"), (16, "exited"), (11, "ok"), (9, "error")])

    def test_without_gh_the_v1_lines_only(self):
        p = self.run_tool(HARNESS_GH=os.path.join(self.tmp, "no-such-gh"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Board:", p.stdout)
        self.assertIn("- git: main, 0 uncommitted", p.stdout)
        self.assertIn("## Budgets", p.stdout)                           # local state still read

    def test_one_project_keeps_its_board(self):
        p = self.run_tool("alpha")
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.sections(p.stdout)
        self.assertEqual(list(s), [f"## alpha `{W['alpha']}`", "## Fleet", "## Budgets", "## Runs"])
        self.assertIn(ALPHA_V2[0], s[f"## alpha `{W['alpha']}`"])


if __name__ == "__main__":
    unittest.main()
