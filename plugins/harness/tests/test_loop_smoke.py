"""Tests for #423: scripts/loop_smoke.py `check` and `reset`, the deterministic half of the loop smoke run.

    python3 -m unittest discover -s plugins/harness/tests -p test_loop_smoke.py

The sandbox is a temporary clone whose origin is a local bare repository named `harness-sandbox.git`, built once for
the module and copied per test; the bare repository's main is one commit ahead of the clone, so a `git pull --ff-only`
is seen to fast-forward, offline. `gh` is the scripted fake (fakegh.py) named by HARNESS_GH (and on PATH as `gh`), so
FAKEGH_LOG holds every gh call. The clone has a pre-push hook that records to $PUSH_LOG and refuses, and the bare
repository's refs are compared before and after, so a push shows either way. The script runs from a directory that is
not a git repository. Each test's first run is a process of its own; its further rows go through
gitrepo_support.run_in_process.

#427, `run`: the real harness-board creates the ticket (its `gh issue create` reaches fakegh), and `claude` is the
fake below, named by HARNESS_CLAUDE, which records its argv, cwd, PATH, standard input, the `--settings` it got and
its pid (and, told to hang, a child's pid) to $FAKECLAUDE_LOG. HOME is a temporary folder per row.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone

sys.dont_write_bytecode = True

from gitrepo_support import GIT, copy_repo, make_template, run_in_process  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(REPO, "scripts", "loop_smoke.py")
FAKEGH = os.path.join(HERE, "fakegh.py")
COMMENT = "Closed by loop smoke reset"
IDENTITY = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
PRE_PUSH = '#!/bin/sh\necho "pre-push $*" >> "$PUSH_LOG"\nexit 1\n'
BOARD = os.path.join(REPO, "plugins", "harness", "bin", "harness-board")
DEFAULT_BIN = os.path.join(REPO, "plugins", "harness", "bin")
RESULT_MARK = "fakeclaude-result-7c1e"
FAKE_CLAUDE = r'''
import json, os, select, subprocess, sys, time
argv = sys.argv[1:]
try:
    ready, _, _ = select.select([0], [], [], 0.3)
    stdin = ("eof" if os.read(0, 1) == b"" else "data") if ready else "open"
except (OSError, ValueError):
    stdin = "closed"
settings = None
if "--settings" in argv and argv.index("--settings") + 1 < len(argv):
    value = argv[argv.index("--settings") + 1]
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as fh:
            value = fh.read()
    settings = value
record = {"argv": argv, "cwd": os.getcwd(), "path": os.environ.get("PATH", ""), "stdin": stdin,
          "settings": settings, "pid": os.getpid()}
hang = os.environ.get("FAKECLAUDE_MODE") == "hang"
if hang:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record["child"] = child.pid
with open(os.environ["FAKECLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\n")
if hang:
    time.sleep(30)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "MARK"}), flush=True)
'''.replace("MARK", RESULT_MARK)

MOD = {}


def executable(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def base_env(home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "HARNESS_", "FAKEGH_", "CLAUDE_"))}
    paths = [MOD.get("fakebin", "")] + [os.path.dirname(GIT), "/usr/bin", "/bin"]
    env.update(IDENTITY, HOME=home, PATH=os.pathsep.join(p for p in paths if p), GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file")
    return env


def git(env, *args, cwd=None):
    p = subprocess.run([GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout.strip()


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-loop-smoke-tpl-"))
    MOD["root"] = root
    MOD["fakebin"] = os.path.join(root, "fakebin")
    executable(os.path.join(MOD["fakebin"], "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    home = os.path.join(root, "home")
    os.makedirs(home)
    env = base_env(home)
    seed = os.path.join(root, "seed")
    make_template(seed, env)
    tpl = os.path.join(root, "tpl")
    bare = os.path.join(tpl, "remote", "harness-sandbox.git")
    clone = os.path.join(tpl, "harness-sandbox")
    git(env, "clone", "-q", "--bare", seed, bare)
    git(env, "clone", "-q", bare, clone)
    git(env, "-C", clone, "config", "gc.auto", "0")
    git(env, "-C", clone, "config", "maintenance.auto", "false")
    executable(os.path.join(clone, ".git", "hooks", "pre-push"), PRE_PUSH)
    # origin's main moves one commit past the clone, so a pull has something to fast-forward
    with open(os.path.join(seed, "NEXT.md"), "w", encoding="utf-8") as fh:
        fh.write("next\n")
    git(env, "-C", seed, "add", "NEXT.md")
    git(env, "-C", seed, "commit", "-q", "-m", "Next")
    git(env, "-C", seed, "push", "-q", bare, "main")
    MOD["tpl"] = tpl
    # #427: the fake claude, and a plugin folder whose bin/ has a harness-board that runs the real one
    MOD["claude"] = executable(os.path.join(root, "fakeclaude", "claude"), f"#!{sys.executable}\n" + FAKE_CLAUDE)
    MOD["plugin"] = os.path.join(root, "plugin")
    executable(os.path.join(MOD["plugin"], "bin", "harness-board"),
               f'#!/bin/sh\nexec "{sys.executable}" "{BOARD}" "$@"\n')


def tearDownModule():
    shutil.rmtree(MOD.get("root", ""), True)


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-loop-smoke-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.log = os.path.join(self.tmp, "gh.log")
        self.push_log = os.path.join(self.tmp, "push.log")
        self.ran_a_process = False
        self.count = 0

    # ----------------------------------------------------------- fixtures
    def env(self):
        env = base_env(self.home)
        env.update(HARNESS_GH=os.path.join(MOD["fakebin"], "gh"), FAKEGH_SCENARIO=self.scenario_path,
                   FAKEGH_LOG=self.log, PUSH_LOG=self.push_log, GIT_CEILING_DIRECTORIES=self.tmp)
        return env

    def git(self, *args):
        return git(self.env(), *args)

    def sandbox(self, origin=None):
        """A fresh copy of the template: (clone, bare). origin None: the copied bare harness-sandbox.git."""
        self.count += 1
        base = os.path.join(self.tmp, f"s{self.count}")
        bare = os.path.join(base, "remote", "harness-sandbox.git")
        clone = os.path.join(base, "harness-sandbox")
        copy_repo(os.path.join(MOD["tpl"], "remote", "harness-sandbox.git"), bare)
        copy_repo(os.path.join(MOD["tpl"], "harness-sandbox"), clone)
        self.git("-C", clone, "remote", "set-url", "origin", origin or bare)
        return clone, bare

    def worktree(self, clone, path, branch, commit=False, dirty=False):
        self.git("-C", clone, "worktree", "add", "-q", "-b", branch, path)
        if commit:
            with open(os.path.join(path, "work.md"), "w", encoding="utf-8") as fh:
                fh.write(branch + "\n")
            self.git("-C", path, "add", "work.md")
            self.git("-C", path, "commit", "-q", "-m", "work on " + branch)
        if dirty:
            with open(os.path.join(path, "scratch.txt"), "w", encoding="utf-8") as fh:
                fh.write("untracked\n")

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        for leftover in (self.scenario_path + ".state.json", self.log, self.push_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def run_script(self, *args):
        if self.ran_a_process and os.path.exists(SCRIPT):
            return run_in_process(SCRIPT, args, cwd=self.cwd, env=self.env())
        self.ran_a_process = True
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.cwd, env=self.env(),
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def branches(self, clone):
        return set(self.git("-C", clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())

    def refs(self, bare):
        return self.git("-C", bare, "for-each-ref", "--format=%(refname) %(objectname)")

    def pushes(self):
        if not os.path.exists(self.push_log):
            return ""
        with open(self.push_log, encoding="utf-8") as fh:
            return fh.read()


def numbers(argv):
    return {int(tok) for tok in argv if tok.isdigit()}


# ---------------------------------------------------------------------------------------------- check

class CheckRow:
    """ticket, the issue's state, the PR list, how the worktree side is set up, and the expected (issue, pr, worktree)."""

    def __init__(self, name, ticket, state, prs, setup, expect):
        self.name, self.ticket, self.state, self.prs, self.setup, self.expect = name, ticket, state, prs, setup, expect


CHECK_ROWS = [
    # all hold; decoys: an open PR and a worktree of t/77-*, a closed earlier PR of t/7-*
    CheckRow("all pass", 7, "CLOSED",
             [{"number": 70, "state": "OPEN", "headRefName": "t/77-other"},
              {"number": 8, "state": "MERGED", "headRefName": "t/7-loop-smoke"},
              {"number": 6, "state": "CLOSED", "headRefName": "t/7-earlier"}],
             "decoy77", (True, True, True)),
    CheckRow("issue open", 12, "OPEN",
             [{"number": 13, "state": "MERGED", "headRefName": "t/12-loop-smoke"}],
             None, (False, True, True)),
    # t/7-* only closed; merged ones are of t/77-* and x/7-*
    CheckRow("pr not merged", 7, "CLOSED",
             [{"number": 8, "state": "CLOSED", "headRefName": "t/7-loop-smoke"},
              {"number": 78, "state": "MERGED", "headRefName": "t/77-other"},
              {"number": 79, "state": "MERGED", "headRefName": "x/7-loop-smoke"}],
             None, (True, False, True)),
    CheckRow("worktree in .claude/worktrees", 12, "CLOSED",
             [{"number": 13, "state": "MERGED", "headRefName": "t/12-loop-smoke"}],
             "registered", (True, True, False)),
    CheckRow("worktree elsewhere", 7, "CLOSED",
             [{"number": 8, "state": "MERGED", "headRefName": "t/7-loop-smoke"}],
             "elsewhere", (True, True, False)),
    CheckRow("leftover folder only", 7, "CLOSED",
             [{"number": 8, "state": "MERGED", "headRefName": "t/7-loop-smoke"}],
             "folder", (True, True, False)),
]


class Check(SandboxTest):
    def prepare(self, row):
        clone, _ = self.sandbox()
        n = row.ticket
        wt = os.path.join(clone, ".claude", "worktrees")
        if row.setup == "decoy77":
            self.worktree(clone, os.path.join(wt, "t-77-other"), "t/77-other")
        elif row.setup == "registered":
            self.worktree(clone, os.path.join(wt, f"t-{n}-loop-smoke"), f"t/{n}-loop-smoke")
        elif row.setup == "elsewhere":
            self.worktree(clone, os.path.join(self.tmp, f"elsewhere-{self.count}"), f"t/{n}-loop-smoke")
        elif row.setup == "folder":
            os.makedirs(os.path.join(wt, f"t-{n}-loop-smoke"))
        self.scenario([
            {"match": ["issue", "view", str(n)], "stdout_json": {"state": row.state, "number": n}},
            {"match": ["pr", "list"], "stdout_json": row.prs},
        ], default={"stdout": "", "exit": 0})
        return clone

    def test_check_passes_only_when_issue_closed_pr_merged_and_worktree_gone(self):
        for row in CHECK_ROWS:
            with self.subTest(row.name):
                clone = self.prepare(row)
                n = row.ticket
                all_ok = all(row.expect)
                p = self.run_script("check", "--sandbox", clone, "--ticket", str(n))
                lines = [line for line in p.stdout.splitlines() if line.strip()]
                self.assertEqual(p.returncode, 0 if all_ok else 1, p.stdout + p.stderr)
                self.assertEqual(len(lines), 3, p.stdout)
                for line, ok in zip(lines, row.expect):
                    self.assertTrue(line.startswith("ok " if ok else "FAIL "), (row.expect, lines))
                if row.expect[0]:
                    self.assertIn(f"#{n}", lines[0])
                if row.expect[1]:
                    merged = [pr["number"] for pr in row.prs
                              if pr["state"] == "MERGED" and pr["headRefName"].startswith(f"t/{n}-")]
                    self.assertIn(f"#{merged[0]}", lines[1])

                p = self.run_script("check", "--sandbox", clone, "--ticket", str(n), "--json")
                self.assertEqual(p.returncode, 0 if all_ok else 1, p.stdout + p.stderr)
                data = json.loads(p.stdout)
                self.assertEqual(data["ticket"], n)
                self.assertIs(data["ok"], all_ok)
                self.assertEqual([c["name"] for c in data["checks"]], ["issue", "pr", "worktree"])
                self.assertEqual(tuple(c["ok"] for c in data["checks"]), row.expect)
                for c in data["checks"]:
                    self.assertIsInstance(c["ok"], bool)
                    self.assertIsInstance(c["detail"], str)
                    self.assertTrue(c["detail"])


# ---------------------------------------------------------------------------------------------- reset

ISSUES = [
    {"number": 5, "title": "Loop smoke 2026-11-23"},
    {"number": 6, "title": "Loop smoke: second run"},
    {"number": 9, "title": "Fix the Loop smoke docs"},      # the search finds it; the title does not start so
]
OPEN_PRS = [
    {"number": 15, "state": "OPEN", "headRefName": "t/5-loop-smoke"},
    {"number": 16, "state": "OPEN", "headRefName": "t/6-loop-smoke"},
    {"number": 19, "state": "OPEN", "headRefName": "t/9-docs"},
    {"number": 55, "state": "OPEN", "headRefName": "t/55-other"},  # t/55-* is not t/5-*
]
READS = (("issue", "list"), ("issue", "view"), ("pr", "list"), ("pr", "view"), ("issue", "status"), ("pr", "status"))


class Reset(SandboxTest):
    def prepare(self, fail_pr=None):
        clone, bare = self.sandbox()
        wt = os.path.join(clone, ".claude", "worktrees")
        self.git("-C", clone, "switch", "-q", "-c", "side")
        # #5: a worktree with an unmerged commit and an untracked file; #6: a local branch only
        self.worktree(clone, os.path.join(wt, "t-5-loop-smoke"), "t/5-loop-smoke", commit=True, dirty=True)
        self.git("-C", clone, "branch", "t/6-loop-smoke", "main")
        self.worktree(clone, os.path.join(wt, "t-9-docs"), "t/9-docs")
        self.worktree(clone, os.path.join(wt, "t-55-other"), "t/55-other")
        calls = [
            {"match": ["issue", "list"], "stdout_json": ISSUES},
            {"match": ["pr", "list"], "stdout_json": OPEN_PRS},
        ]
        if fail_pr is not None:
            calls.insert(0, {"contains": ["pr", "close", str(fail_pr)], "stderr": "HTTP 502\n", "exit": 1})
        self.scenario(calls, default={"stdout": "", "exit": 0})
        return clone, bare, wt

    def mutating(self):
        return [c for c in self.calls() if tuple(c["argv"][:2]) not in READS]

    def closed(self, kind):
        return [c["argv"] for c in self.calls() if c["argv"][:2] == [kind, "close"]]

    def test_reset_closes_only_loop_smoke_issues_and_their_prs_and_worktrees(self):
        clone, bare, wt = self.prepare()
        remote_main = self.git("-C", bare, "rev-parse", "main")
        bare_refs = self.refs(bare)

        p = self.run_script("reset", "--sandbox", clone)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip(), "one line per action")

        # gh: only #5 and #6 and their PRs #15 and #16 are touched
        touched = set()
        for c in self.mutating():
            touched |= numbers(c["argv"])
        self.assertEqual(touched, {5, 6, 15, 16}, self.mutating())
        issue_closes = self.closed("issue")
        self.assertEqual(set().union(*map(numbers, issue_closes)) if issue_closes else set(), {5, 6})
        pr_closes = self.closed("pr")
        self.assertEqual(set().union(*map(numbers, pr_closes)) if pr_closes else set(), {15, 16})
        for argv in pr_closes:
            self.assertIn("--delete-branch", argv)
        for n in (5, 6):
            said = [c for c in self.calls() if c["argv"][:2] in (["issue", "close"], ["issue", "comment"])
                    and n in numbers(c["argv"]) and (COMMENT in " ".join(c["argv"]) or COMMENT in c["stdin"])]
            self.assertTrue(said, f"no `{COMMENT}` comment on #{n}")

        # git: #5's worktree and both branches gone; the decoys stay
        self.assertFalse(os.path.exists(os.path.join(wt, "t-5-loop-smoke")))
        self.assertTrue(os.path.isdir(os.path.join(wt, "t-9-docs")))
        self.assertTrue(os.path.isdir(os.path.join(wt, "t-55-other")))
        heads = self.branches(clone)
        self.assertNotIn("t/5-loop-smoke", heads)
        self.assertNotIn("t/6-loop-smoke", heads)
        self.assertTrue({"t/9-docs", "t/55-other", "side", "main"} <= heads, heads)
        porcelain = self.git("-C", clone, "worktree", "list", "--porcelain")
        self.assertNotIn("refs/heads/t/5-", porcelain)

        # on main, fast-forwarded to origin's main, and nothing pushed
        self.assertEqual(self.git("-C", clone, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git("-C", clone, "rev-parse", "HEAD"), remote_main)
        self.assertEqual(self.pushes(), "")
        self.assertEqual(self.refs(bare), bare_refs)

        # a failed action exits 1 and the rest still run
        clone, bare, wt = self.prepare(fail_pr=15)
        remote_main = self.git("-C", bare, "rev-parse", "main")
        p = self.run_script("reset", "--sandbox", clone)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn([16], [sorted(numbers(a)) for a in self.closed("pr")])
        self.assertIn(6, set().union(*map(numbers, self.closed("issue"))) if self.closed("issue") else set())
        self.assertNotIn("t/6-loop-smoke", self.branches(clone))
        self.assertEqual(self.git("-C", clone, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git("-C", clone, "rev-parse", "HEAD"), remote_main)
        self.assertEqual(self.pushes(), "")


# ---------------------------------------------------------------------------------------------- guard

class Guard(SandboxTest):
    ALL_PASS = [
        {"match": ["issue", "view", "7"], "stdout_json": {"state": "CLOSED"}},
        {"match": ["pr", "list"], "stdout_json": [{"number": 8, "state": "MERGED", "headRefName": "t/7-loop-smoke"}]},
        {"match": ["issue", "list"], "stdout_json": [{"number": 5, "title": "Loop smoke 2026-11-23"}]},
    ]

    def test_a_clone_whose_origin_is_not_the_sandbox_is_refused_untouched(self):
        harness_bare = os.path.join(self.tmp, "remote", "Harness.git")
        copy_repo(os.path.join(MOD["tpl"], "remote", "harness-sandbox.git"), harness_bare)
        refused = [
            harness_bare,
            "git@github.com:rjeskuri/Harness.git",
            "https://github.com/rjeskuri/harness-sandbox-old.git",
            None,  # a folder that is not a git clone
        ]
        for origin in refused:
            with self.subTest(origin=origin or "not a clone"):
                if origin is None:
                    clone = os.path.join(self.tmp, "plain")
                    os.makedirs(clone, exist_ok=True)
                else:
                    clone, _ = self.sandbox(origin)
                    self.git("-C", clone, "switch", "-q", "-c", "side")
                    wt = os.path.join(clone, ".claude", "worktrees", "t-5-loop-smoke")
                    self.worktree(clone, wt, "t/5-loop-smoke")
                    before = (self.git("-C", clone, "rev-parse", "HEAD"), self.branches(clone),
                              self.git("-C", clone, "worktree", "list", "--porcelain"))
                for args in (("check", "--sandbox", clone, "--ticket", "7"), ("reset", "--sandbox", clone)):
                    self.scenario(self.ALL_PASS, default={"stdout": "", "exit": 0})
                    p = self.run_script(*args)
                    self.assertEqual(p.returncode, 2, (args, p.stdout, p.stderr))
                    self.assertEqual(len((p.stdout + p.stderr).strip().splitlines()), 1, p.stdout + p.stderr)
                    self.assertEqual(self.calls(), [], args)
                    self.assertEqual(self.pushes(), "")
                    if origin is not None:
                        self.assertEqual(self.git("-C", clone, "symbolic-ref", "--short", "HEAD"), "side")
                        after = (self.git("-C", clone, "rev-parse", "HEAD"), self.branches(clone),
                                 self.git("-C", clone, "worktree", "list", "--porcelain"))
                        self.assertEqual(after, before)
                        self.assertTrue(os.path.isdir(wt))

        # the same clone with a harness-sandbox origin, in any URL form, gets past the guard and calls gh
        for origin in ("git@github.com:rjeskuri/harness-sandbox.git", "https://github.com/rjeskuri/harness-sandbox",
                       None):
            with self.subTest(accepted=origin or "local harness-sandbox.git"):
                clone, _ = self.sandbox(origin)
                self.scenario(self.ALL_PASS, default={"stdout": "", "exit": 0})
                p = self.run_script("check", "--sandbox", clone, "--ticket", "7")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertTrue(self.calls())
        clone, _ = self.sandbox()
        self.scenario([{"match": ["issue", "list"], "stdout_json": []}], default={"stdout": "", "exit": 0})
        p = self.run_script("reset", "--sandbox", clone)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls())


# ---------------------------------------------------------------------------------------------- run (#427)

def after(argv, flag):
    """The token after `flag` in argv, else None."""
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    p = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    state = p.stdout.strip()
    return bool(state) and not state.startswith("Z")


class Run(SandboxTest):
    def setUp(self):
        super().setUp()
        self.claude_log = os.path.join(self.tmp, "claude.log")
        self.claude = MOD["claude"]
        self.mode = ""
        self.homes = 0

    def env(self):
        env = super().env()
        env.update(HARNESS_CLAUDE=self.claude, FAKECLAUDE_LOG=self.claude_log, FAKECLAUDE_MODE=self.mode)
        return env

    def fresh_home(self):
        self.homes += 1
        self.home = os.path.join(self.tmp, f"home{self.homes}")
        os.makedirs(self.home)

    def run_scenario(self, n, issue_state="CLOSED"):
        """gh for one run: reset finds an earlier open Loop smoke issue #5, `issue create` makes #n, and check sees
        #n as issue_state with a merged PR from t/n-loop-smoke."""
        self.scenario([
            {"match": ["issue", "create"], "stdout": f"https://github.com/rjeskuri/harness-sandbox/issues/{n}\n"},
            {"match": ["issue", "list"], "stdout_json": [{"number": 5, "title": "Loop smoke 2026-11-01"}]},
            {"match": ["issue", "view", str(n)], "stdout_json": {"state": issue_state, "number": n}},
            {"match": ["pr", "list"], "stdout_json": [{"number": n + 100, "state": "MERGED",
                                                       "headRefName": f"t/{n}-loop-smoke"}]},
        ], default={"stdout": "", "exit": 0})
        if os.path.exists(self.claude_log):
            os.unlink(self.claude_log)

    def run_real(self, *args):
        """The script as its own process, its standard input a pipe held open for the whole run (so a launched
        session that inherits it, rather than getting a closed one, is seen to)."""
        self.ran_a_process = True
        out_path, err_path = os.path.join(self.tmp, "run.out"), os.path.join(self.tmp, "run.err")
        with open(out_path, "w") as out, open(err_path, "w") as err:
            p = subprocess.Popen([sys.executable, SCRIPT, *args], cwd=self.cwd, env=self.env(),
                                 stdin=subprocess.PIPE, stdout=out, stderr=err)
            try:
                code = p.wait(timeout=120)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                p.stdin.close()
        with open(out_path) as fh_out, open(err_path) as fh_err:
            return types.SimpleNamespace(returncode=code, stdout=fh_out.read(), stderr=fh_err.read())

    def launches(self):
        if not os.path.exists(self.claude_log):
            return []
        with open(self.claude_log, encoding="utf-8") as fh:
            found = [json.loads(line) for line in fh if line.strip()]
        for rec in found:
            for pid in (rec.get("pid"), rec.get("child")):
                if pid:
                    self.addCleanup(self.kill_quietly, pid)
        return found

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def creates(self):
        return [c["argv"] for c in self.calls() if c["argv"][:2] == ["issue", "create"]]

    def lines(self, p):
        return [line for line in (p.stdout + p.stderr).splitlines() if line.strip()]

    def assert_one_ticket_and_launch(self, p, n, clone):
        """One `Loop smoke <UTC stamp>` task created (S, low, ready) and one claude -p "/harness:work n" launched
        from the sandbox; the launch record."""
        creates = self.creates()
        self.assertEqual(len(creates), 1, (self.calls(), p.stdout, p.stderr))
        title = after(creates[0], "--title")
        self.assertTrue(title and title.startswith("Loop smoke "), title)
        self.assertIn(str(datetime.now(timezone.utc).year), title)
        labels = {creates[0][i + 1] for i, tok in enumerate(creates[0][:-1]) if tok == "--label"}
        self.assertTrue({"task", "status:ready", "size:S", "risk:low"} <= labels, creates[0])
        launches = self.launches()
        self.assertEqual(len(launches), 1, (launches, p.stdout, p.stderr))
        argv = launches[0]["argv"]
        self.assertIn("-p", argv)
        self.assertIn(f"/harness:work {n}", argv)
        self.assertEqual(after(argv, "--permission-mode"), "auto", argv)
        self.assertEqual(after(argv, "--output-format"), "json", argv)
        self.assertEqual(os.path.realpath(launches[0]["cwd"]), os.path.realpath(clone))
        return launches[0]

    def test_run_creates_a_ticket_launches_work_and_reports_the_check(self):
        # (ticket number, the issue's state at check, run's expected exit code)
        for n, state, code in ((41, "CLOSED", 0), (7, "OPEN", 1), (263, "CLOSED", 0)):
            with self.subTest(ticket=n, state=state):
                self.fresh_home()
                clone, bare = self.sandbox()
                remote_main = self.git("-C", bare, "rev-parse", "main")
                bare_refs = self.refs(bare)
                self.run_scenario(n, state)
                first = not self.ran_a_process
                if first:
                    p = self.run_real("run", "--sandbox", clone)
                else:
                    p = self.run_script("run", "--sandbox", clone)
                self.assertEqual(p.returncode, code, p.stdout + p.stderr)

                launch = self.assert_one_ticket_and_launch(p, n, clone)
                self.assertNotIn("--plugin-dir", launch["argv"])
                self.assertEqual(os.path.realpath(launch["path"].split(os.pathsep)[0]), os.path.realpath(DEFAULT_BIN))
                if first:
                    self.assertIn(launch["stdin"], ("eof", "closed"), "the session's standard input is not closed")

                # reset ran first: the earlier Loop smoke issue is closed, never the new one, and main fast-forwarded
                closed = set()
                for argv in (c["argv"] for c in self.calls() if c["argv"][:2] == ["issue", "close"]):
                    closed |= numbers(argv)
                self.assertEqual(closed, {5}, self.calls())
                self.assertEqual(self.git("-C", clone, "rev-parse", "HEAD"), remote_main)
                # nothing pushed, and the sandbox is not registered
                self.assertEqual(self.pushes(), "")
                self.assertEqual(self.refs(bare), bare_refs)
                registry = os.path.join(self.home, ".claude", "harness", "projects.json")
                if os.path.exists(registry):
                    with open(registry, encoding="utf-8") as fh:
                        self.assertNotIn("harness-sandbox", fh.read())

                # the session's output is in ~/.claude/harness/smoke/<stamp>.log, and run names that file
                smoke = os.path.join(self.home, ".claude", "harness", "smoke")
                logs = sorted(os.listdir(smoke)) if os.path.isdir(smoke) else []
                self.assertEqual(len(logs), 1, logs)
                self.assertTrue(logs[0].endswith(".log"), logs)
                log = os.path.join(smoke, logs[0])
                with open(log, encoding="utf-8") as fh:
                    self.assertIn(RESULT_MARK, fh.read())
                self.assertIn(logs[0], p.stdout)

                # check's own three lines for ticket n, in order, in run's output
                self.ran_a_process = True
                expected = self.run_script("check", "--sandbox", clone, "--ticket", str(n))
                want = [line for line in expected.stdout.splitlines() if line.strip()]
                self.assertEqual(len(want), 3, expected.stdout)
                self.assertEqual([line.startswith("ok ") for line in want], [state == "CLOSED", True, True], want)
                got = [line.strip() for line in p.stdout.splitlines()]
                starts = [i for i in range(len(got) - 2) if got[i:i + 3] == want]
                self.assertTrue(starts, (want, p.stdout))

    def test_plugin_dir_and_its_bin_reach_the_launched_session(self):
        plugin = MOD["plugin"]
        for n in (58, 9):
            with self.subTest(ticket=n):
                self.fresh_home()
                clone, bare = self.sandbox()
                bare_refs = self.refs(bare)
                self.run_scenario(n)
                p = self.run_script("run", "--sandbox", clone, "--plugin", plugin)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                launch = self.assert_one_ticket_and_launch(p, n, clone)
                argv = launch["argv"]
                self.assertEqual(os.path.realpath(after(argv, "--plugin-dir") or ""), os.path.realpath(plugin), argv)
                self.assertIsNotNone(launch["settings"], argv)
                settings = json.loads(launch["settings"])
                self.assertIs(settings["enabledPlugins"]["harness@ryan"], False, settings)
                first = launch["path"].split(os.pathsep)[0]
                self.assertEqual(os.path.realpath(first), os.path.realpath(os.path.join(plugin, "bin")))
                self.assertEqual(self.pushes(), "")
                self.assertEqual(self.refs(bare), bare_refs)

    def test_a_session_past_the_timeout_is_killed_and_fails(self):
        # a session that outlives --timeout 0.02 (1.2 s); check itself would pass, so the one FAIL is the timeout
        self.fresh_home()
        clone, _ = self.sandbox()
        self.run_scenario(31)
        self.mode = "hang"
        started = time.monotonic()
        p = self.run_real("run", "--sandbox", clone, "--timeout", "0.02")
        elapsed = time.monotonic() - started
        launches = self.launches()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertLess(elapsed, 25, "run waited for the session instead of its timeout")
        fails = [line for line in self.lines(p) if line.startswith("FAIL")]
        self.assertEqual(len(fails), 1, p.stdout + p.stderr)
        self.assertIn("time", fails[0].lower())
        self.assertEqual(len(launches), 1, launches)
        pids = [launches[0]["pid"], launches[0]["child"]]
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([pid for pid in pids if alive(pid)], [], "the session or its child outlived the timeout")

        # a launch that fails (no such claude) exits 1 with one FAIL line naming the launch
        self.mode = ""
        self.fresh_home()
        clone, _ = self.sandbox()
        self.run_scenario(32)
        self.claude = os.path.join(self.tmp, "no-such-claude")
        p = self.run_script("run", "--sandbox", clone)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        fails = [line for line in self.lines(p) if line.startswith("FAIL")]
        self.assertEqual(len(fails), 1, p.stdout + p.stderr)
        self.assertTrue("launch" in fails[0].lower() or "no-such-claude" in fails[0], fails[0])
        self.assertEqual(self.launches(), [])


if __name__ == "__main__":
    unittest.main()
