"""Tests for the harness bin tools: harness-projects, harness-standup and harness-worklog.

    python3 -m unittest discover -s plugins/harness/tests -v

The tools run as subprocesses with the interpreter running the tests, or are imported by path. Every
test has its own temporary HOME and repos and a PATH without the real `claude`, so nothing here reads
or writes the real ~/.claude, and nothing needs the network.
"""
from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

sys.dont_write_bytecode = True      # importing a bin script must not leave a __pycache__ in bin/

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
FAKEGH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakegh.py")
GIT = shutil.which("git")
UTC = dt.timezone.utc
# What gh says on a GitHub Actions runner without a token (its first line), before it looks at the repo.
GH_AUTH_ERROR = "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable. Example:"


def load_tool(name):
    """Import a bin script (no .py suffix) by path, as a fresh module."""
    module_name = name.replace("-", "_")
    loader = SourceFileLoader(module_name, os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def encode(path):
    """Claude Code's session folder name for a project path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def mkdirs(*parts):
    path = os.path.join(*parts)
    os.makedirs(path, exist_ok=True)
    return path


def write_file(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def system_path():
    """git's folder and the system folders: no `claude` unless a test puts one there."""
    return os.pathsep.join(([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"])


def tool_env(home, fakebin, ceiling, **extra):
    """The environment the tools run in: a fake HOME, `fakebin` first on PATH, git confined below `ceiling`."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "HARNESS_SESSION_PROJECT", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(HOME=home, PATH=os.pathsep.join([fakebin, system_path()]), GIT_CEILING_DIRECTORIES=ceiling,
               GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    env.update(extra)
    return env


def run_git(repo, env, *args):
    return subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
                          + list(args), env=env, capture_output=True, text=True, check=True)


class ToolTest(unittest.TestCase):
    """A temporary HOME and scratch folder; `fakebin` leads PATH and holds a fake `claude` when wanted."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.fakebin, self.ceiling = self.mk("home"), self.mk("fakebin"), self.tmp

    def mk(self, *parts):
        return mkdirs(self.tmp, *parts)

    def write(self, path, text, mtime=None):
        return write_file(path, text, mtime)

    def system_path(self):
        return system_path()

    def env(self, **extra):
        return tool_env(self.home, self.fakebin, self.ceiling, **extra)

    def run_tool(self, tool, *args, cwd=None, env=None):
        return subprocess.run([sys.executable, os.path.join(BIN, tool)] + list(args), cwd=cwd or self.tmp,
                              env=env or self.env(), capture_output=True, text=True, timeout=60)

    def git(self, repo, *args):
        return run_git(repo, self.env(), *args)

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)


# ======================================================================= harness-projects
class Projects(ToolTest):

    def registry(self):
        with open(os.path.join(self.home, ".claude", "harness", "projects.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_registry_round_trip(self):
        hk, glow = self.mk("Projects", "HKTrack"), self.mk("jotform-glow-sync")
        p = self.run_tool("harness-projects")                               # list is the default
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("No projects registered", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "harness")))   # listing writes nothing

        p = self.run_tool("harness-projects", "add", "HKTrack", cwd=os.path.dirname(hk))   # resolved to absolute
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"added HKTrack: {hk} (not billable)")
        p = self.run_tool("harness-projects", "add", glow, "--billable", "--client", "Acme Corp",
                          "--notes", "monthly invoice", "--json")
        self.assertEqual(json.loads(p.stdout), {"path": glow, "name": "jotform-glow-sync", "billable": True,
                                                "client": "Acme Corp", "notes": "monthly invoice"})
        self.assertEqual(self.registry(), {"version": 1, "projects": [
            {"path": hk, "name": "HKTrack", "billable": False, "client": "", "notes": ""},
            {"path": glow, "name": "jotform-glow-sync", "billable": True, "client": "Acme Corp",
             "notes": "monthly invoice"}]})

        lines = self.run_tool("harness-projects", "list").stdout.splitlines()
        self.assertEqual(lines[0].split(), ["NAME", "PATH", "BILLABLE", "CLIENT"])
        self.assertEqual(lines[1].split(), ["HKTrack", hk, "no"])
        self.assertEqual(lines[2].split(), ["jotform-glow-sync", glow, "yes", "Acme", "Corp"])
        self.assertEqual(json.loads(self.run_tool("harness-projects", "--json").stdout), self.registry())
        self.assertEqual(json.loads(self.run_tool("harness-projects", "list", "--json").stdout), self.registry())

        # refused: the same path again (written differently), a name already taken, a missing directory
        self.assertOneLineError(self.run_tool("harness-projects", "add", hk + "/"), "already registered")
        self.assertOneLineError(self.run_tool("harness-projects", "add", self.mk("other", "HKTrack")), "--name")
        self.assertOneLineError(self.run_tool("harness-projects", "add", os.path.join(self.tmp, "nope")),
                                "not a directory")

        # set, by name in any case or by path
        p = self.run_tool("harness-projects", "set", "hktrack", "--billable", "--client", "Self", "--name", "HK")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_tool("harness-projects", "set", glow, "--no-billable", "--notes", "", "--json")
        self.assertEqual(json.loads(p.stdout)["billable"], False)
        self.assertEqual(self.registry()["projects"], [
            {"path": hk, "name": "HK", "billable": True, "client": "Self", "notes": ""},
            {"path": glow, "name": "jotform-glow-sync", "billable": False, "client": "Acme Corp", "notes": ""}])
        self.assertOneLineError(self.run_tool("harness-projects", "set", "HK"), "nothing to change")
        self.assertOneLineError(self.run_tool("harness-projects", "set", "HK", "--billable", "--no-billable"))
        self.assertOneLineError(self.run_tool("harness-projects", "set", "HK", "--name", "JOTFORM-glow-sync"),
                                "taken")

        # remove, by path and by name
        p = self.run_tool("harness-projects", "remove", glow, "--json")
        self.assertEqual(json.loads(p.stdout)["name"], "jotform-glow-sync")
        self.assertOneLineError(self.run_tool("harness-projects", "remove", "jotform-glow-sync"),
                                "no registered project")
        self.assertEqual(self.run_tool("harness-projects", "remove", "HK").stdout.strip(), f"removed HK: {hk}")
        self.assertEqual(self.registry(), {"version": 1, "projects": []})

    def test_bad_invocations_are_one_line(self):
        self.assertOneLineError(self.run_tool("harness-projects", "bogus"), "invalid choice")
        self.assertOneLineError(self.run_tool("harness-projects", "add"), "required")
        self.assertOneLineError(self.run_tool("harness-projects", "remove", "ghost"), "no registered project")
        self.write(os.path.join(self.home, ".claude", "harness", "projects.json"), "{not json")
        self.assertOneLineError(self.run_tool("harness-projects", "list"), "not valid JSON")
        self.assertOneLineError(self.run_tool("harness-standup"), "not valid JSON")

    def test_recover_a_path_from_its_session_folder_name(self):
        tool = load_tool("harness-projects")
        root = self.mk("fs")
        glow = self.mk("fs", "Users", "ryaneskuri", "jotform-glow-sync")
        hk = self.mk("fs", "Users", "ryaneskuri", "Projects", "HKTrack")
        self.mk("fs", "Users", "ryaneskuri", "jotform")                 # a decoy with no glow/sync below it
        harness = self.mk("fs", "Users", "ryaneskuri", "portfolio_projects", "Harness")
        self.assertEqual(tool.encode("/Users/ryaneskuri/jotform-glow-sync"), "-Users-ryaneskuri-jotform-glow-sync")
        self.assertEqual(tool.recover("-Users-ryaneskuri-jotform-glow-sync", root=root), glow)
        self.assertEqual(tool.recover("-Users-ryaneskuri-Projects-HKTrack", root=root), hk)
        self.assertEqual(tool.recover("-Users-ryaneskuri-portfolio-projects-Harness", root=root), harness)
        self.assertIsNone(tool.recover("-Users-ryaneskuri-gone-project", root=root))
        # x/a/b and x/a-b both exist: the one that is a project wins
        self.mk("fs", "x", "a", "b")
        dashed = self.mk("fs", "x", "a-b")
        os.mkdir(os.path.join(dashed, ".git"))
        self.assertEqual(tool.recover("-x-a-b", root=root), dashed)

    def test_discover(self):
        now = time.time()
        glow = self.mk("work", "jotform-glow-sync")
        os.mkdir(os.path.join(glow, ".git"))
        hk = self.mk("work", "Projects", "HKTrack")
        self.write(os.path.join(hk, "CLAUDE.md"), "# HKTrack\n")
        harness = self.mk("work", "portfolio_projects", "Harness")
        os.mkdir(os.path.join(harness, ".git"))
        plain = self.mk("work", "plain-folder")                             # no .git, no CLAUDE.md
        known = self.mk("work", "registered")
        os.mkdir(os.path.join(known, ".git"))
        worktree = self.mk("work", "jotform-glow-sync", ".claude", "worktrees", "wf-1")
        self.write(os.path.join(worktree, ".git"), "gitdir: elsewhere\n")
        sessions = os.path.join(self.home, ".claude", "projects")
        for path, ago in ((glow, 3600), (hk, 7200), (harness, 60), (plain, 0), (known, 0), (worktree, 0)):
            self.write(os.path.join(sessions, encode(path), "s.jsonl"), "{}\n", mtime=now - ago)
        self.assertIn("-claude-worktrees-", encode(worktree))
        os.makedirs(os.path.join(sessions, encode(os.path.join(self.tmp, "work", "deleted-project"))))
        self.assertEqual(self.run_tool("harness-projects", "add", known).returncode, 0)

        p = self.run_tool("harness-projects", "discover", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        found = json.loads(p.stdout)
        self.assertEqual([c["path"] for c in found], [harness, glow, hk])     # newest session first
        self.assertEqual(found[0]["folder"], encode(harness))
        self.assertTrue(all(c["last_session"] for c in found))

        p = self.run_tool("harness-projects", "discover")
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["PATH", "LAST", "SESSION"])
        self.assertEqual([line.split()[0] for line in lines[1:4]], [harness, glow, hk])
        self.assertIn("(1m ago)", lines[1])
        self.assertIn("(1h ago)", lines[2])
        self.assertIn("(2h ago)", lines[3])
        self.assertTrue(lines[4].startswith("Register one with: harness-projects add"))
        for path in (plain, known, worktree):
            self.assertNotIn(path, p.stdout)


# ======================================================================= harness-standup
HANDOFF = """# Handoff: alpha

**Goal**: ship the parser.

## Next steps
1. Wire the export button
2. Write the parser tests

## Decisions
- Use csv, not pandas
"""

PROGRESS = """# Progress

## 2026-09-30 - first session
Did the first thing.
Next: an old next step

## 2026-10-02 - second session
Did more.
Next: the real next step
Next (after that): deploy it
"""

FEATURES = [{"id": "F1", "description": "Done already", "passes": True},
            {"id": "F2", "description": "Export to CSV", "passes": False},
            {"id": "F3", "description": "Import", "passes": False},
            {"id": "F4", "description": "Sync", "passes": False},
            {"id": "F5", "description": "Fifth", "passes": False}]


def fake_claude(folder, payload, exit_code=0, stderr=""):
    """A `claude` that answers `agents --json` with `payload` (and anything else with exit 64)."""
    path = write_file(os.path.join(folder, "claude"),
                      "#!/bin/sh\n"
                      '[ "$1 $2" = "agents --json" ] || { echo "unexpected: $*" >&2; exit 64; }\n'
                      + (f"echo {shlex.quote(stderr)} >&2\n" if stderr else "")
                      + f"cat <<'JSON'\n{json.dumps(payload) if payload is not None else ''}\nJSON\n"
                      + f"exit {exit_code}\n")
    os.chmod(path, 0o755)
    return path


class Standup(ToolTest):
    """Built once for the class: alpha, a billable git repo with a handoff, specs, a work log and sessions
    (its origin a local bare repo); beta, not a git repo, with PROGRESS.md and a feature list; gamma, a
    registered path that is gone. Tests that change the registry or ~/.local/bin use a HOME of their own.
    gh is always fakegh.py through HARNESS_GH, failing as on a runner without a token, so the real gh is
    never reached; the board itself is covered by test_standup_v2.py."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not GIT:
            raise unittest.SkipTest("git is not installed")
        cls.root = os.path.realpath(tempfile.mkdtemp(prefix="harness-standup-"))
        try:
            cls.build_world()
        except BaseException:
            shutil.rmtree(cls.root, True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)
        super().tearDownClass()

    @classmethod
    def build_world(cls):
        root, now, today = cls.root, time.time(), dt.date.today()
        month, last_month = today.strftime("%Y-%m"), (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        cls.world_home, cls.world_bin = mkdirs(root, "home"), mkdirs(root, "fakebin")
        env = tool_env(cls.world_home, cls.world_bin, root)
        cls.alpha = a = mkdirs(root, "alpha")
        write_file(os.path.join(a, "README.md"), "alpha\n")
        write_file(os.path.join(a, "docs", "agent", "handoff.md"), HANDOFF)
        write_file(os.path.join(a, "docs", "agent", "specs", "2026-10-01-old.md"), "old\n", mtime=now - 7200)
        write_file(os.path.join(a, "docs", "agent", "specs", "2026-10-02-new.md"), "new\n", mtime=now - 60)
        write_file(os.path.join(a, "worklog", "worklog.csv"),
                   "date,hours,blocks,description,source,zone\n"
                   f"{last_month}-28,3.00,10:00-13:00,Last month,auto,+09:00\n"
                   f"{month}-01,1.50,09:00-10:30,Built it,auto,+09:00\n"
                   f"{month}-01,0.75,,Call,manual,\n")
        remote = mkdirs(root, "remote.git")
        run_git(remote, env, "init", "--bare")
        run_git(a, env, "init")
        run_git(a, env, "add", "-A")
        run_git(a, env, "commit", "-m", "Initial commit")
        run_git(a, env, "remote", "add", "origin", remote)
        run_git(a, env, "push", "-u", "origin", "main")
        write_file(os.path.join(a, "parser.py"), "x = 1\n")
        run_git(a, env, "add", "parser.py")
        run_git(a, env, "commit", "-m", "Add the parser")                   # one ahead of origin/main
        write_file(os.path.join(a, "README.md"), "alpha, changed\n")       # uncommitted: modified
        write_file(os.path.join(a, "notes.txt"), "scratch\n")              # uncommitted: untracked
        write_file(os.path.join(cls.world_home, ".claude", "projects", encode(a), "s.jsonl"), "{}\n",
                   mtime=now - 2 * 3600 - 30)

        cls.beta = b = mkdirs(root, "beta")
        write_file(os.path.join(b, "docs", "PROGRESS.md"), PROGRESS)
        write_file(os.path.join(b, "docs", "plans", "feature_list.json"), json.dumps(FEATURES))
        write_file(os.path.join(b, "worklog", "worklog.csv"),     # not billable: never shown
                   f"date,hours,blocks,description,source,zone\n{month}-01,9.00,,x,manual,\n")
        cls.gamma = os.path.join(root, "gamma-was-deleted")
        cls.projects = [{"path": a, "name": "alpha", "billable": True, "client": "Acme", "notes": ""},
                        {"path": b, "name": "beta", "billable": False, "client": "", "notes": ""},
                        {"path": cls.gamma, "name": "gamma", "billable": False, "client": "", "notes": ""}]
        write_file(os.path.join(cls.world_home, ".claude", "harness", "projects.json"),
                   json.dumps({"version": 1, "projects": cls.projects}))
        cls.payload = [
            {"pid": 11, "cwd": a, "kind": "interactive", "startedAt": 1790577944042, "sessionId": "11111111-aaaa",
             "name": "alpha-1", "status": "idle"},
            {"pid": 12, "cwd": os.path.join(a, "src"), "kind": "background", "name": "alpha-2",
             "status": "waiting", "waitingFor": "permission prompt"},
            {"pid": 13, "cwd": "/somewhere/else", "kind": "interactive", "name": "other-3", "status": "busy"},
            {"pid": 14, "cwd": a + "-sibling", "sessionId": "abcdef12-3456", "status": "idle"}]   # no name
        fake_claude(cls.world_bin, cls.payload)
        cls.gh = write_file(os.path.join(cls.world_bin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
        os.chmod(cls.gh, 0o755)
        cls.gh_scenario = write_file(os.path.join(root, "gh-scenario.json"), json.dumps(
            {"default": {"stderr": GH_AUTH_ERROR + "\n  env:\n    GH_TOKEN: ${{ github.token }}\n", "exit": 4}}))

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="test-", dir=self.root))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.fakebin, self.ceiling = self.world_home, self.world_bin, self.root
        self.gh_log = os.path.join(self.tmp, "gh.log")

    def env(self, **extra):
        fake = dict(HARNESS_GH=self.gh, FAKEGH_SCENARIO=self.gh_scenario, FAKEGH_LOG=self.gh_log)
        fake.update(extra)
        return super().env(**fake)

    def gh_calls(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def own_home(self, projects=None):
        """A HOME of this test's own, with a registry of `projects` (none: no registry)."""
        home = self.mk("home")
        if projects is not None:
            write_file(os.path.join(home, ".claude", "harness", "projects.json"),
                       json.dumps({"version": 1, "projects": projects}))
        return home

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
        p = self.run_tool("harness-standup")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.sections(p.stdout)
        alpha, beta, gamma = f"## alpha `{self.alpha}`", f"## beta `{self.beta}`", f"## gamma `{self.gamma}`"
        self.assertEqual(list(s), [alpha, beta, gamma, "## Fleet"])
        self.assertEqual(s[alpha], [
            "- git: main, 2 uncommitted, 1 ahead / 0 behind origin/main, last commit just now: Add the parser",
            "- last session: 2h ago",
            "- next:",
            "  - 1. Wire the export button",
            "  - 2. Write the parser tests",
            "- latest spec: docs/agent/specs/2026-10-02-new.md",
            "- hours this month: 2.25",
            "- sessions: alpha-1 idle, alpha-2 waiting (permission prompt)"])
        self.assertEqual(s[beta], [
            "- git: not a git repo",
            "- last session: none recorded",
            "- next:",
            "  - Next: the real next step",
            "  - Next (after that): deploy it",
            "  - pending F2: Export to CSV",
            "  - pending F3: Import",
            "  - pending F4: Sync",
            "- sessions: none"])
        self.assertEqual(s[gamma], ["- path not found"])
        self.assertEqual(s["## Fleet"], [                                    # waiting first
            "- alpha-2 waiting (permission prompt) in src [background]",
            "- alpha-1 idle in alpha",
            "- other-3 busy in else",
            "- abcdef12 idle in alpha-sibling"])

    def test_json(self):
        p = self.run_tool("harness-standup", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual([r["name"] for r in data["projects"]], ["alpha", "beta", "gamma"])
        alpha, beta, gamma = data["projects"]
        self.assertEqual({k: alpha["git"][k] for k in ("repo", "branch", "uncommitted", "upstream", "ahead", "behind")},
                         {"repo": True, "branch": "main", "uncommitted": 2, "upstream": "origin/main",
                          "ahead": 1, "behind": 0})
        self.assertEqual(alpha["git"]["last_commit"]["subject"], "Add the parser")
        self.assertEqual(alpha["last_session"]["age"], "2h ago")
        self.assertEqual(alpha["next"], ["1. Wire the export button", "2. Write the parser tests"])
        self.assertEqual((alpha["latest_spec"], alpha["hours_this_month"]), ("docs/agent/specs/2026-10-02-new.md", 2.25))
        self.assertEqual([e["name"] for e in alpha["sessions"]], ["alpha-1", "alpha-2"])
        self.assertEqual((beta["git"], beta["hours_this_month"], beta["sessions"]), ({"repo": False}, None, []))
        self.assertEqual(gamma["error"], "path not found")
        self.assertEqual((data["fleet"], data["fleet_error"]), (self.payload, None))

    def test_one_project_by_name_or_path(self):
        p = self.run_tool("harness-standup", "BETA")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(list(self.sections(p.stdout)), [f"## beta `{self.beta}`", "## Fleet"])
        p = self.run_tool("harness-standup", self.alpha, "--json")
        self.assertEqual([r["name"] for r in json.loads(p.stdout)["projects"]], ["alpha"])
        self.assertOneLineError(self.run_tool("harness-standup", "ghost"), "no registered project")
        self.assertOneLineError(self.run_tool("harness-standup", "--bogus"), "unrecognized arguments")

    def test_a_fleet_that_cannot_be_read_is_said_once(self):
        bare = self.env(PATH=self.system_path())
        if shutil.which("claude", path=bare["PATH"]):
            self.skipTest("a real claude is on the system PATH")
        p = self.run_tool("harness-standup", env=bare)                     # no claude anywhere
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("could not read the fleet"), 1)
        self.assertNotIn("- sessions:", p.stdout)
        self.assertIn("- git: main, 2 uncommitted", p.stdout)            # the projects still render
        broken = self.mk("broken-bin")
        fake_claude(broken, None, exit_code=1, stderr="boom")
        p = self.run_tool("harness-standup", env=self.env(PATH=os.pathsep.join([broken, bare["PATH"]])))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("- could not read the fleet: `claude agents --json` exited 1: boom", p.stdout)
        home = self.own_home()
        fake_claude(os.path.join(home, ".local", "bin"), self.payload[:1])        # ~/.local/bin/claude
        p = self.run_tool("harness-standup", "--json", env=dict(bare, HOME=home))
        self.assertEqual(json.loads(p.stdout)["fleet"], self.payload[:1])

    def test_an_unreadable_project_never_stops_the_others(self):
        broken = self.mk("delta")
        self.write(os.path.join(broken, "worklog", "worklog.csv"), f"date,hours\n{dt.date.today():%Y-%m}-01,lots\n")
        home = self.own_home([{"path": broken, "name": "delta", "billable": True, "client": "", "notes": ""}]
                             + self.projects[1:])
        p = self.run_tool("harness-standup", env=self.env(HOME=home))
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.sections(p.stdout)
        self.assertEqual(len(s[f"## delta `{broken}`"]), 1)
        self.assertTrue(s[f"## delta `{broken}`"][0].startswith("- could not read it: ValueError"))
        self.assertIn("- git: not a git repo", s[f"## beta `{self.beta}`"])
        self.assertEqual(s[f"## gamma `{self.gamma}`"], ["- path not found"])

    def test_no_projects_registered(self):
        p = self.run_tool("harness-standup", env=self.env(HOME=self.own_home()))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("No projects registered"))
        self.assertIn("## Fleet", p.stdout)

    def test_no_board_without_a_github_origin(self):
        """Only an origin on github.com reads the board: an origin that is a local path or on another host never
        runs gh, though gh would fail; the same failing gh on a github.com origin is said in the board line."""
        elsewhere, hub = self.mk("elsewhere"), self.mk("hub")
        self.git(elsewhere, "init")
        self.git(elsewhere, "remote", "add", "origin", "https://gitlab.com/o/elsewhere.git")
        self.git(hub, "init")
        self.git(hub, "remote", "add", "origin", "https://github.com/o/hub.git")
        projects = [{"path": path, "name": name, "billable": False, "client": "", "notes": ""}
                    for name, path in (("alpha", self.alpha), ("elsewhere", elsewhere), ("hub", hub))]
        p = self.run_tool("harness-standup", env=self.env(HOME=self.own_home(projects)))
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.sections(p.stdout)
        for name, path in (("alpha", self.alpha), ("elsewhere", elsewhere)):
            self.assertEqual([line for line in s[f"## {name} `{path}`"] if "Board" in line], [])
        self.assertIn(f"- Board: could not read it (gh issue list failed: {GH_AUTH_ERROR})", s[f"## hub `{hub}`"])
        calls = self.gh_calls()
        self.assertTrue(calls)
        self.assertEqual([argv for argv in calls if argv[-2:] != ["-R", "o/hub"]], [])     # hub's calls only

    def test_next_step_sources(self):
        tool = load_tool("harness-standup")
        self.assertEqual(tool.handoff_next("**Goal**: x\n\n**Next steps**:\n1. One\n- **Two** things\n\n"
                                           "**Decisions**: y\n"), ["1. One", "**Two** things"])
        self.assertEqual(tool.handoff_next("- **Done**: a\n- **Next steps**: first\n  1. second\n- **Decisions**: b\n"),
                         ["first", "1. second"])
        self.assertEqual(tool.handoff_next("# Handoff\n\nNo section here\n"), ["# Handoff", "No section here"])
        self.assertEqual(tool.progress_next(PROGRESS), ["Next: the real next step", "Next (after that): deploy it"])
        self.assertEqual(tool.progress_next("No blocks\nNext: x\n"), [])


# ======================================================================= harness-worklog
class Worklog(ToolTest):

    def setUp(self):
        super().setUp()
        self.wl = load_tool("harness-worklog")
        self.repo = self.mk("client-repo")
        self.tz, name, _ = self.wl.system_zone()             # the Mac's zone, read as the tool reads it
        now = dt.datetime.now(UTC)
        self.zone_text = name or self.wl.offset_text(now.astimezone(self.tz))
        self.today = now.astimezone(self.tz).date()

    def run_wl(self, *args, **kw):
        return self.run_tool("harness-worklog", "--repo", self.repo, *args, **kw)

    def rows(self, name="worklog.csv", repo=None):
        with open(os.path.join(repo or self.repo, "worklog", name), encoding="utf-8", newline="") as fh:
            return list(csv.reader(fh)) if name == "timezones.csv" else list(csv.DictReader(fh))

    def transcript(self, path, entries):
        """A Claude Code transcript of (type, aware datetime, message content or None) entries."""
        lines = []
        for kind, when, content in entries:
            entry = {"type": kind, "timestamp": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            if content is not None:
                entry["message"] = {"content": content}
            lines.append(json.dumps(entry))
        return self.write(path, "\n".join(lines + [json.dumps({"type": "summary"}), "not json"]) + "\n")

    def session(self, name):
        return os.path.join(self.home, ".claude", "projects", encode(self.repo), name)

    def midnight(self, day):
        return dt.datetime.combine(day, dt.time(), self.wl.parse_zone(self.zone_text)).astimezone(UTC)

    def test_blocks_and_hours_from_a_synthetic_transcript(self):
        wl = self.wl
        plus8 = dt.timezone(dt.timedelta(hours=8))

        def at(hhmm):
            return dt.datetime.fromisoformat(f"2026-09-26T{hhmm}:00").replace(tzinfo=plus8)

        path = self.transcript(os.path.join(self.tmp, "t", "s.jsonl"), [
            ("user", at("09:00"), "hello"),
            ("assistant", at("09:20"), None),
            ("assistant", at("09:45"), None),
            ("assistant", at("11:00"), None),                                   # the agent alone: not billed
            ("user", at("11:20"), [{"type": "tool_result", "content": "ok"}]),   # a tool result is not the user
            ("user", at("14:00"), [{"type": "text", "text": "next"}]),
            ("assistant", at("14:10"), None)])
        evts = wl.events([path])
        self.assertEqual([human for _, human in evts], [True, False, False, False, False, True, False])
        self.assertEqual(evts[0][0], dt.datetime(2026, 9, 26, 1, 0, tzinfo=UTC))
        zones = [(dt.datetime(2026, 1, 1, tzinfo=UTC), wl.parse_zone("+08:00"), "+08:00", "")]
        days = wl.day_blocks(evts, zones)
        day = dt.date(2026, 9, 26)
        self.assertEqual(list(days), [day])
        self.assertEqual(wl.blocks_text(days[day]), "09:00-09:45, 14:00-14:10")
        self.assertEqual(wl.zones_text(days[day]), "+08:00")
        self.assertEqual(wl.day_hours(days[day]), 1.0)                         # 55 minutes -> 1.00
        self.assertEqual(wl.day_hours([(at("09:00"), at("09:07"))]), 0.25)      # a day with work: at least 0.25
        self.assertEqual(wl.day_hours([(at("09:00"), at("09:53"))]), 1.0)       # half up
        self.assertTrue(wl.is_human_message({"type": "user", "message": {"content": "hello"}}))
        self.assertFalse(hasattr(wl, "is_ryans_message"))
        self.assertEqual(wl.REPO, os.curdir)                                    # importing runs nothing

    def test_messages_name_harness_worklog(self):
        wl = self.wl
        zones = [(dt.datetime(2026, 9, 17, tzinfo=UTC), wl.parse_zone("+08:00"), "+08:00", "")]
        evts = [(dt.datetime(2026, 9, 28, 14, tzinfo=UTC), True), (dt.datetime(2026, 9, 30, 1, 50, tzinfo=UTC), True)]
        msg = wl.mac_zone_problem(zones, wl.parse_zone("Asia/Tokyo"), "Asia/Tokyo",
                                  dt.datetime(2026, 9, 30, 2, tzinfo=UTC), evts)
        self.assertIn("harness-worklog zone --since", msg)
        self.assertNotIn("tools/worklog.py", msg)
        with self.assertRaises(ValueError) as cm:
            wl.read_zones(os.path.join(self.tmp, "nowhere.csv"))
        self.assertIn("(start it with: harness-worklog init)", str(cm.exception))

    def test_init(self):
        p = self.run_wl("update")                                               # no timeline yet: points at init
        self.assertOneLineError(p, "worklog/timezones.csv is missing", f"harness-worklog --repo {self.repo} init")
        self.assertNotIn("RUNBOOK", p.stderr)
        p = self.run_wl("init")
        self.assertEqual(p.returncode, 0, p.stderr)
        first = (self.today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        timeline = [["since", "zone", "evidence"],
                    [self.midnight(first).isoformat(), self.zone_text, f"recorded {self.today} by harness-worklog init"]]
        self.assertEqual(self.rows("timezones.csv"), timeline)
        self.assertEqual(self.wl.read_zones(os.path.join(self.repo, "worklog", "timezones.csv"))[0][0],
                         self.midnight(first))
        self.assertOneLineError(self.run_wl("init", "--since", "2026-01-01"), "already exists")
        self.assertEqual(self.rows("timezones.csv"), timeline)                  # never overwritten
        other = self.mk("other-repo")
        self.assertEqual(self.run_tool("harness-worklog", "--repo", other, "init", "--since", "2026-09-15").returncode, 0)
        self.assertEqual(self.rows("timezones.csv", other)[1][:2],
                         [self.midnight(dt.date(2026, 9, 15)).isoformat(), self.zone_text])
        future = (self.today + dt.timedelta(days=2)).isoformat()
        self.assertOneLineError(self.run_tool("harness-worklog", "--repo", self.mk("third"), "init", "--since", future),
                                "after today")
        self.assertOneLineError(self.run_tool("harness-worklog", "--repo", os.path.join(self.tmp, "nope"), "init"),
                                "is not a directory")

    def test_a_zone_row_lands_on_a_line_of_its_own(self):
        path = os.path.join(self.repo, "worklog", "timezones.csv")
        timeline = "since,zone,evidence\n2026-09-01T00:00:00+00:00,+08:00,recorded 2026-09-01\n"
        row = "2026-09-20T03:00:00+00:00,+09:00,flight on 2026-09-20"
        for text in (timeline, timeline[:-1]):              # the last line with its newline, then without
            with self.subTest(trailing_newline=text.endswith("\n")):
                self.write(path, text)
                self.wl.append_zone(dt.datetime(2026, 9, 20, 3, tzinfo=UTC), "+09:00", "flight on 2026-09-20", path)
                with open(path, encoding="utf-8", newline="") as fh:
                    after = fh.read()
                lines = after.splitlines()
                self.assertEqual(len(lines), len(text.splitlines()) + 1, lines)     # one row more: no blank line
                self.assertEqual(lines, text.splitlines() + [row])                  # the new row is the last line
                self.assertTrue(after.endswith(row + "\n"))

    def test_update_and_describe(self):
        self.assertEqual(self.run_wl("init").returncode, 0)
        day = self.today - dt.timedelta(days=2)
        t0 = dt.datetime.combine(day, dt.time(10, 0), self.tz)
        self.transcript(self.session("a.jsonl"), [
            ("user", t0, "hello"),
            ("assistant", t0 + dt.timedelta(minutes=20), None),
            ("assistant", t0 + dt.timedelta(minutes=45), None),
            ("assistant", t0 + dt.timedelta(minutes=120), None)])               # the agent alone at 12:00
        p = self.run_wl("update")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"{day}   0.75 h   10:00-10:45", p.stdout)
        self.assertEqual(self.rows(), [{"date": day.isoformat(), "hours": "0.75", "blocks": "10:00-10:45",
                                        "description": "", "source": "auto", "zone": self.wl.offset_text(t0)}])
        # describe sets the description of the day's auto row, and update keeps it
        p = self.run_wl("describe", "--date", day.isoformat(), "--text", "  Built the export.  ")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.rows()[0]["description"], self.rows()[0]["hours"]), ("Built the export.", "0.75"))
        self.assertEqual(self.run_wl("update").returncode, 0)
        self.assertEqual(self.rows()[0]["description"], "Built the export.")
        # a day with blocks but no row yet: created with 0.25 h; the next update counts its hours
        day2 = self.today - dt.timedelta(days=3)
        t1 = dt.datetime.combine(day2, dt.time(11, 0), self.tz)
        self.transcript(self.session("b.jsonl"), [("user", t1, "go"),
                                                  ("assistant", t1 + dt.timedelta(minutes=25), None),
                                                  ("assistant", t1 + dt.timedelta(minutes=50), None),
                                                  ("assistant", t1 + dt.timedelta(minutes=60), None)])
        p = self.run_wl("describe", "--date", day2.isoformat(), "--text", "Fixed the totals")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("0.25 h", p.stdout)
        row = next(r for r in self.rows() if r["date"] == day2.isoformat())
        self.assertEqual((row["hours"], row["blocks"], row["description"], row["source"]),
                         ("0.25", "11:00-12:00", "Fixed the totals", "auto"))
        self.assertEqual(self.run_wl("update").returncode, 0)
        row = next(r for r in self.rows() if r["date"] == day2.isoformat())
        self.assertEqual((row["hours"], row["description"]), ("1.00", "Fixed the totals"))
        # a day without work is refused with a pointer to add, and a manual row is never touched
        day3 = (self.today - dt.timedelta(days=5)).isoformat()
        self.assertEqual(self.run_wl("add", "--date", day3, "--hours", "1.5", "--description", "Call").returncode, 0)
        before = self.rows()
        self.assertOneLineError(self.run_wl("describe", "--date", day3, "--text", "x"), "has no work",
                                f"add --date {day3}")
        self.assertOneLineError(self.run_wl("describe", "--date", day3, "--text", "   "), "--text is empty")
        self.assertEqual(self.rows(), before)

    def test_repo_resolution(self):
        if not GIT:
            self.skipTest("git is not installed")
        top = self.mk("resolved")
        self.git(top, "init")
        deep = self.mk("resolved", "src", "deep")
        from_env, flag, after, plain = self.mk("from-env"), self.mk("from-flag"), self.mk("after"), self.mk("plain")

        def started(repo):
            return os.path.isfile(os.path.join(repo, "worklog", "timezones.csv"))

        with_env = self.env(CLAUDE_PROJECT_DIR=from_env)
        self.assertEqual(self.run_tool("harness-worklog", "init", cwd=deep).returncode, 0)   # git's top level
        self.assertTrue(started(top) and not started(deep))
        self.assertEqual(self.run_tool("harness-worklog", "init", cwd=deep, env=with_env).returncode, 0)
        self.assertTrue(started(from_env))                                      # CLAUDE_PROJECT_DIR beats git
        self.assertEqual(self.run_tool("harness-worklog", "--repo", flag, "init", cwd=deep, env=with_env).returncode, 0)
        self.assertTrue(started(flag))                                          # --repo beats both
        self.assertEqual(self.run_tool("harness-worklog", "init", "--repo", after, cwd=deep, env=with_env).returncode, 0)
        self.assertTrue(started(after))                                         # also after the subcommand
        self.assertEqual(self.run_tool("harness-worklog", "init", cwd=plain).returncode, 0)
        self.assertTrue(started(plain))                                         # else the cwd

    def test_bad_invocations_and_the_unchanged_commands(self):
        self.assertOneLineError(self.run_wl(), "required")
        self.assertOneLineError(self.run_wl("bogus"), "invalid choice")
        self.assertOneLineError(self.run_wl("describe", "--date", "2026-13-01", "--text", "x"), "give a date like")
        self.assertOneLineError(self.run_wl("add", "--date", "2026-09-29", "--hours", "25", "--description", "x"),
                                "between 0 and 24")
        p = self.run_wl("add", "--date", "2026-09-29", "--hours", "1.5", "--description", "Call with the program team")
        self.assertEqual(p.stdout.strip(), "added 1.50 h on 2026-09-29 (manual)")
        p = self.run_wl("export", "--month", "2026-09", "--rate", "60")
        self.assertEqual(p.stdout.splitlines(), ["Date  Hours  Description  Amount",
                                                 "2026-09-29  1.50  Call with the program team  90.00",
                                                 "Total  1.50    90.00"])
        self.assertOneLineError(self.run_wl("export", "--month", "2026-9"), "give yyyy-mm")
        self.assertOneLineError(self.run_tool("harness-worklog", "--repo", os.path.join(self.tmp, "nope"), "add",
                                              "--date", "2026-09-29", "--hours", "1", "--description", "x"),
                                "is not a directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
