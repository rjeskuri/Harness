"""Tests for the gh shim (ticket #448): the gh calls the harness's tools do not make (an agent's own `gh` in
Bash, a poller) are logged too, one `tool: "agent"` line each in ~/.claude/harness/gh/calls-<UTC date>.jsonl.

The contract these tests fix:
- plugins/harness/shim/gh is an executable python3 script. It runs the first executable `gh` on PATH whose
  directory (realpath) is not its own (realpath) with its argv[1:] unchanged and stdin, stdout and stderr inherited
  (the same open files, never captured or rewritten), waits, and exits with that gh's exit code. With no other gh on
  PATH: one line on stderr, exit 127.
- After the call it logs one line through ghlimit.log_call(args, cwd=None, exit=<code>, ms=<ms>, tool="agent"):
  the usual ten fields, tool "agent", cmd and kind from the argv, repo and ticket from the cwd and PWD.
  log_call gains the keyword tool=None (None: the basename of sys.argv[0], as before).
- The harness's own gh runners (harness_lib.gh.gh_run, GhBatch, session_brief.run_side_by_side) put
  HARNESS_GH_LOGGED=1 in the environment of each gh child (never in their own process environment); the shim logs
  nothing when HARNESS_GH_LOGGED is set and not empty. A harness tool whose `gh` is the shim leaves only its own lines.
- A log that cannot be written (or a lib that cannot be loaded) never changes the call: the same stdout, stderr
  and exit, byte for byte.
- session_brief.py appends a line to CLAUDE_ENV_FILE that puts <plugin>/shim first on PATH unless it is already
  on PATH, so a session's Bash call that runs `gh` reaches the shim.

The stand-in for the real gh (realbin/gh) echoes its argv and stdin, and records each call (argv, the inodes of
its fds 0-2, HARNESS_GH_LOGGED) in SHIMTEST_RECORD; fakebin/gh records the same and becomes fakegh.py. python3 on
PATH (the shim's `env python3`) is a wrapper that stops a shim that runs itself after a few levels. Expected values
are written out here, never computed by the code under test. The fakes are written once for the module.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_gh_shim.py" -v
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
LIB = os.path.join(PLUGIN, "lib")
SHIM_DIR = os.path.join(PLUGIN, "shim")
SHIM = os.path.join(SHIM_DIR, "gh")
BOARD = os.path.join(PLUGIN, "bin", "harness-board")
SESSION_BRIEF = os.path.join(PLUGIN, "hooks", "session_brief.py")
FAKEGH = os.path.join(TESTS, "fakegh.py")
GIT = shutil.which("git")

FIELDS = {"at", "tool", "cmd", "kind", "repo", "ticket", "exit", "ms", "waited_ms", "refused"}
AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
DAY_FILE = re.compile(r"^calls-(\d{4}-\d\d-\d\d)\.jsonl$")

# The real gh's stand-in: records the call, prints `out:<argv as JSON>|<stdin>` and `err:<SHIMTEST_ERR>`, exits
# SHIMTEST_EXIT.
REALGH = '''#!{python}
import json, os, sys
data = sys.stdin.buffer.read()
record = os.environ.get("SHIMTEST_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "fds": [os.fstat(i).st_ino for i in (0, 1, 2)],
                             "logged": os.environ.get("HARNESS_GH_LOGGED")}}) + "\\n")
sys.stdout.buffer.write(b"out:" + json.dumps(sys.argv[1:]).encode("utf-8") + b"|" + data)
sys.stderr.buffer.write(b"err:" + os.environ.get("SHIMTEST_ERR", "").encode("utf-8"))
sys.exit(int(os.environ.get("SHIMTEST_EXIT", "0")))
'''
# The gh the harness tools reach in the end-to-end test: records the call, then becomes fakegh.py.
FAKEBIN_GH = '''#!{python}
import json, os, sys
record = os.environ.get("SHIMTEST_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "logged": os.environ.get("HARNESS_GH_LOGGED")}}) + "\\n")
os.execv({python!r}, [{python!r}, {fakegh!r}] + sys.argv[1:])
'''
# python3 for `#!/usr/bin/env python3`: the real interpreter, refused past five nested starts (a shim that finds
# itself instead of the real gh stops here instead of starting itself without end).
PYTHON3 = '''#!/bin/sh
depth=${{SHIMTEST_DEPTH:-0}}
if [ "$depth" -gt 5 ]; then echo "shim test: python3 nested too deep (the shim ran itself?)" >&2; exit 99; fi
SHIMTEST_DEPTH=$((depth + 1)) exec {python} "$@"
'''

M: dict = {}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def git_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_", "CLAUDE_"))}
    env.update(HOME=home, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
    return env


def git(repo, *args):
    p = subprocess.run([GIT, "-C", repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                       env=git_env(M["githome"]), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {p.stderr}")
    return p.stdout.strip()


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="test-gh-shim-"))
    M["root"] = root
    try:
        M["realbin"] = os.path.join(root, "realbin")
        M["realgh"] = executable(os.path.join(M["realbin"], "gh"), REALGH.format(python=sys.executable))
        M["fakebin"] = os.path.join(root, "fakebin")
        M["fakegh"] = executable(os.path.join(M["fakebin"], "gh"),
                                 FAKEBIN_GH.format(python=sys.executable, fakegh=FAKEGH))
        M["pybin"] = os.path.join(root, "pybin")
        executable(os.path.join(M["pybin"], "python3"), PYTHON3.format(python=sys.executable))
        M["githome"] = os.path.join(root, "githome")
        os.makedirs(M["githome"])
        # path-only checkouts for the shim's repo and ticket (no git needed: both come from the path)
        M["wt12"] = os.path.join(root, "acme-widget", ".claude", "worktrees", "t-12-add-the-widget")
        M["beta"] = os.path.join(root, "beta-tool")
        os.makedirs(M["wt12"])
        os.makedirs(M["beta"])
        if GIT is not None:   # a real repository with a github origin, for harness-board in the end-to-end test
            M["main"] = os.path.join(root, "shop-front")
            os.makedirs(M["main"])
            git(M["main"], "init", "-q", "-b", "main")
            write(os.path.join(M["main"], "README.md"), "# shop-front\n")
            git(M["main"], "add", "-A")
            git(M["main"], "commit", "-q", "-m", "First")
            git(M["main"], "remote", "add", "origin", "https://github.com/acme/shop-front.git")
        # macOS checks a new executable on its first run: pay that here
        for exe in (M["realgh"], M["fakegh"]):
            subprocess.run([exe, "warm-up"], capture_output=True, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin"}, timeout=60)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def tearDownModule():
    shutil.rmtree(M.get("root", ""), ignore_errors=True)


def lib(name):
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


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="case-", dir=M["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.new_home()
        self.record = os.path.join(self.tmp, "record.jsonl")

    def new_home(self):
        return tempfile.mkdtemp(prefix="home-", dir=self.tmp)

    def env(self, home=None, path=None, **extra):
        """A clean environment: a temporary HOME, PATH = python3 wrapper, the shim's folder, the real gh's stand-in,
        the system; nothing of the caller's harness, gh or claude settings."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "FAKEGH_", "SHIMTEST_", "GIT_", "CLAUDE_", "GH_"))
               and k not in ("PWD", "OLDPWD")}
        dirs = path if path is not None else [M["pybin"], SHIM_DIR, M["realbin"], "/usr/bin", "/bin"]
        env.update(HOME=home or self.home, PATH=os.pathsep.join(dirs), SHIMTEST_RECORD=self.record,
                   PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def shim(self, args, stdin=b"", cwd=None, exe=SHIM, **env):
        """Run the shim as a program (its shebang, its folder's real path) with `stdin` bytes."""
        self.assertTrue(os.path.isfile(exe) and os.access(exe, os.X_OK), f"no executable shim at {exe}")
        cwd = cwd or M["beta"]
        env.setdefault("PWD", cwd)
        return subprocess.run([exe] + list(args), input=stdin, capture_output=True, cwd=cwd,
                              env=self.env(**env), timeout=60)

    def records(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def logged(self, home=None):
        folder = os.path.join(home or self.home, ".claude", "harness", "gh")
        if not os.path.isdir(folder):
            return []
        out = []
        for name in sorted(os.listdir(folder)):
            if not DAY_FILE.match(name):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                out.extend(json.loads(raw) for raw in fh if raw.strip())
        return out

    def assertAgentLine(self, line, **expected):
        self.assertEqual(set(line), FIELDS, line)
        self.assertRegex(line["at"], AT)
        self.assertEqual(line["tool"], "agent", line)
        self.assertIsInstance(line["ms"], int, line)
        self.assertGreaterEqual(line["ms"], 0, line)
        self.assertEqual((line["waited_ms"], line["refused"]), (0, False), line)
        for key, value in expected.items():
            self.assertEqual(line[key], value, f"{key}: {line}")


# ==================================================================== the call passes through unchanged
class PassThrough(Case):
    def test_argv_stdin_output_and_exit_pass_through(self):
        """Whatever the argv, stdin and exit, the shim's stdout, stderr and exit are the real gh's, byte for byte,
        and the real gh ran once with the argv unchanged."""
        cases = [
            (["issue", "view", "12", "--json", "title,body"], b"", 0, ""),
            (["pr", "create", "--title", "A title with  spaces", "--body-file", "-"], b"line one\nline two\n", 1,
             "HTTP 422: Validation Failed\n"),
            (["api", "graphql", "-f", "query=query { viewer { login } }", ""], b"\xff\x00raw bytes\n", 3, "boom"),
            (["--version"], b"", 42, "unicode: \u00e9\u00e8 \u2713\n"),
            ([], b"", 0, ""),
        ]
        for args, stdin, code, err in cases:
            with self.subTest(args=args, exit=code):
                if os.path.exists(self.record):
                    os.unlink(self.record)
                p = self.shim(args, stdin=stdin, SHIMTEST_EXIT=str(code), SHIMTEST_ERR=err)
                self.assertEqual(p.stdout, b"out:" + json.dumps(args).encode("utf-8") + b"|" + stdin)
                self.assertEqual(p.stderr, b"err:" + err.encode("utf-8"))
                self.assertEqual(p.returncode, code)
                self.assertEqual([r["argv"] for r in self.records()], [args])

    def test_the_real_gh_inherits_the_shims_open_files(self):
        """stdin, stdout and stderr reach the real gh as the very files the shim was given (not pipes of its own)."""
        paths = [write(os.path.join(self.tmp, "in.txt"), "input text\n"),
                 os.path.join(self.tmp, "out.txt"), os.path.join(self.tmp, "err.txt")]
        self.assertTrue(os.access(SHIM, os.X_OK), f"no executable shim at {SHIM}")
        with open(paths[0], "rb") as fin, open(paths[1], "wb") as fout, open(paths[2], "wb") as ferr:
            code = subprocess.run([SHIM, "repo", "view"], stdin=fin, stdout=fout, stderr=ferr, cwd=M["beta"],
                                  env=self.env(PWD=M["beta"], SHIMTEST_EXIT="5"), timeout=60).returncode
        self.assertEqual(code, 5)
        with open(paths[1], "rb") as fh:
            self.assertEqual(fh.read(), b'out:["repo", "view"]|input text\n')
        (call,) = self.records()
        self.assertEqual(call["fds"], [os.stat(p).st_ino for p in paths])

    def test_finds_the_first_gh_on_path_that_is_not_the_shim(self):
        """The shim's own folder is skipped wherever it is on PATH (also twice, and through a symlink), the first
        other gh runs; with no other gh: one line on stderr and exit 127."""
        link = os.path.join(self.tmp, "shim-link")
        os.symlink(SHIM_DIR, link)
        second = os.path.join(self.tmp, "second")
        executable(os.path.join(second, "gh"), "#!/bin/sh\necho second-gh\nexit 7\n")
        for label, dirs, out, code in (
            ("shim first", [M["pybin"], SHIM_DIR, M["realbin"], second], b'out:["auth", "status"]|', 0),
            ("shim twice", [M["pybin"], SHIM_DIR, SHIM_DIR, M["realbin"]], b'out:["auth", "status"]|', 0),
            ("shim by a symlink", [M["pybin"], link, second, M["realbin"]], b"second-gh\n", 7),
            ("real gh first", [M["pybin"], second, SHIM_DIR, M["realbin"]], b"second-gh\n", 7),
        ):
            with self.subTest(label):
                p = self.shim(["auth", "status"], path=dirs + ["/usr/bin", "/bin"])
                self.assertEqual((p.stdout, p.returncode), (out, code), p.stderr)
        p = self.shim(["auth", "status"], path=[M["pybin"], SHIM_DIR, "/usr/bin", "/bin"])
        self.assertEqual(p.returncode, 127, p.stderr)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(len(p.stderr.decode("utf-8", "replace").strip().splitlines()), 1, p.stderr)


# ==================================================================== one log line per call
class Logged(Case):
    def test_each_call_is_one_agent_line(self):
        """One line per call: tool agent, cmd and kind from the argv, the call's exit, repo and ticket from the
        directory it ran in."""
        cases = [
            (["issue", "view", "12", "--json", "title"], 0, M["wt12"], "issue view", "graphql", "acme-widget", 12),
            (["api", "repos/acme/widget/issues/12/sub_issues"], 1, M["beta"],
             "api repos/:owner/:repo/issues/:n/sub_issues", "rest", "beta-tool", None),
            (["run", "list", "--limit", "5"], 3, M["wt12"], "run list", "rest", "acme-widget", 12),
            (["api", "-i", "graphql", "-f", "query=query { rateLimit { used } }"], 0, M["beta"], "api graphql",
             "graphql", "beta-tool", None),
        ]
        for args, code, cwd, cmd, kind, repo, ticket in cases:
            with self.subTest(args=args):
                home = self.new_home()
                p = self.shim(args, cwd=cwd, home=home, SHIMTEST_EXIT=str(code))
                self.assertEqual(p.returncode, code, p.stderr)
                lines = self.logged(home)
                self.assertEqual(len(lines), 1, lines)
                self.assertAgentLine(lines[0], cmd=cmd, kind=kind, exit=code, repo=repo, ticket=ticket)
        # a second call appends a second line
        home = self.new_home()
        self.shim(["pr", "list"], home=home)
        self.shim(["pr", "view", "34"], home=home, SHIMTEST_EXIT="1")
        self.assertEqual([(x["cmd"], x["exit"]) for x in self.logged(home)], [("pr list", 0), ("pr view", 1)])

    def test_no_line_when_a_harness_tool_logged_the_call(self):
        """HARNESS_GH_LOGGED set and not empty: no line; set but empty: a line, as when unset."""
        for value, count in (("1", 0), ("yes", 0), ("", 1)):
            with self.subTest(HARNESS_GH_LOGGED=value):
                home = self.new_home()
                p = self.shim(["issue", "list"], home=home, HARNESS_GH_LOGGED=value, SHIMTEST_EXIT="2")
                self.assertEqual((p.stdout, p.returncode), (b'out:["issue", "list"]|', 2))
                self.assertEqual(len(self.logged(home)), count, self.logged(home))

    def test_log_call_takes_the_tool_name(self):
        """log_call(..., tool="agent") writes tool agent; tool None or left out keeps the basename of sys.argv[0]."""
        ghlimit = lib("ghlimit")
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                mock.patch.object(sys, "argv", ["/opt/harness/bin/harness-pr"]):
            ghlimit.log_call(["pr", "view", "3"], cwd=M["beta"], exit=0, ms=4, tool="agent")
            ghlimit.log_call(["pr", "view", "3"], cwd=M["beta"], exit=0, ms=4, tool=None)
            ghlimit.log_call(["pr", "view", "3"], cwd=M["beta"], exit=0, ms=4)
        self.assertEqual([x["tool"] for x in self.logged()], ["agent", "harness-pr", "harness-pr"])

    def test_a_log_that_cannot_be_written_never_changes_the_call(self):
        """The gh folder a file, HOME read-only, or the lib missing (the shim copied away from the plugin): the same
        stdout, stderr and exit as the real gh gave, and the real gh ran once."""
        args, stdin = ["pr", "checks", "34"], b"some input\n"
        expected = (b'out:["pr", "checks", "34"]|some input\n', b"err:checks failed\n", 8)

        def gh_is_a_file(home):
            write(os.path.join(home, ".claude", "harness", "gh"), "not a folder\n")

        def home_is_read_only(home):
            os.chmod(home, 0o555)
            self.addCleanup(os.chmod, home, 0o755)

        copy_dir = os.path.join(self.tmp, "copied-shim")
        os.makedirs(copy_dir)
        copied = os.path.join(copy_dir, "gh")
        if os.path.exists(SHIM):
            shutil.copy2(SHIM, copied)
        for label, breakage, exe, dirs in (
            ("gh folder is a file", gh_is_a_file, SHIM, None),
            ("HOME read-only", home_is_read_only if os.geteuid() != 0 else gh_is_a_file, SHIM, None),
            ("no lib", None, copied, [M["pybin"], copy_dir, M["realbin"], "/usr/bin", "/bin"]),
        ):
            with self.subTest(label):
                if os.path.exists(self.record):
                    os.unlink(self.record)
                home = self.new_home()
                if breakage is not None:
                    breakage(home)
                p = self.shim(args, stdin=stdin, exe=exe, home=home, path=dirs, SHIMTEST_EXIT="8",
                              SHIMTEST_ERR="checks failed\n")
                self.assertEqual((p.stdout, p.stderr, p.returncode), expected)
                self.assertEqual([r["argv"] for r in self.records()], [args])


# ==================================================================== no double logging
class HarnessCalls(Case):
    def test_the_harness_runners_mark_their_gh_children(self):
        """gh_run, GhBatch and session_brief's run_side_by_side each start gh with HARNESS_GH_LOGGED=1, and leave
        their own process environment without it (so the agents and tools they start log their own gh calls)."""
        gh = lib("gh")
        brief = load_tool(SESSION_BRIEF, "session_brief_gh_shim")
        scenario = write(os.path.join(self.tmp, "scenario.json"),
                         json.dumps({"calls": [], "default": {"stdout": "[]"}}))
        env = self.env(HARNESS_GH=M["fakegh"], FAKEGH_SCENARIO=scenario)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["/opt/x/harness-board"]):
            self.assertEqual(gh.gh_run(["issue", "list"], self.tmp).stdout, "[]")
            batch = gh.GhBatch(M["fakegh"], [["pr", "list"], ["issue", "list"]], self.tmp, time.monotonic() + 30,
                               budget_seconds=30)
            try:
                self.assertEqual(batch.results(), [[], []])
            finally:
                batch.close()
            from pathlib import Path
            self.assertEqual(brief.run_side_by_side([[M["fakegh"], "issue", "list"], [M["fakegh"], "pr", "list"]],
                                                    Path(self.tmp), 30), ["[]", "[]"])
            self.assertNotIn("HARNESS_GH_LOGGED", os.environ)
        calls = self.records()
        self.assertEqual(len(calls), 5, calls)
        self.assertEqual([c["logged"] for c in calls], ["1"] * 5, calls)

    @unittest.skipIf(GIT is None, "git not found")
    def test_a_tool_whose_gh_is_the_shim_logs_each_call_once(self):
        """HARNESS_GH unset, the shim first on PATH: `harness-board show 12 --json` reaches fakegh through the
        shim and the log holds only the board's own lines; an agent's `gh` in a shell with the same PATH adds one
        agent line."""
        scenario = write(os.path.join(self.tmp, "scenario.json"), json.dumps({"calls": [
            {"match": ["issue", "view", "12"], "stdout_json": {
                "number": 12, "title": "Ticket 12", "url": "https://github.com/acme/shop-front/issues/12",
                "body": "## Request\nAdd it.\n\n## Definition of done\n- [ ] It works\n",
                "labels": [{"name": "task"}, {"name": "status:ready"}], "state": "OPEN", "parent": None,
                "blockedBy": [], "comments": []}},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"}]}))
        dirs = [M["pybin"], SHIM_DIR, M["fakebin"]]
        if os.path.dirname(GIT) not in ("/usr/bin", "/bin"):
            dirs.append(os.path.dirname(GIT))
        env = self.env(path=dirs + ["/usr/bin", "/bin"], FAKEGH_SCENARIO=scenario, GIT_CONFIG_NOSYSTEM="1",
                       HARNESS_POLL_SECONDS="0.01")
        self.assertNotIn("HARNESS_GH", env)
        p = subprocess.run([sys.executable, BOARD, "show", "12", "--json"], cwd=M["main"], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["number"], 12)
        calls = self.records()
        self.assertEqual([c["argv"][:2] for c in calls], [["issue", "view"], ["api", "user"]])
        self.assertEqual([c["logged"] for c in calls], ["1", "1"], calls)
        lines = self.logged()
        self.assertEqual(sorted((x["tool"], x["cmd"]) for x in lines),
                         [("harness-board", "api user"), ("harness-board", "issue view")])

        # an agent's own gh in a later Bash call, same PATH: one agent line more
        s = subprocess.run(["/bin/sh", "-c", 'gh issue view 12 --json title'], cwd=M["main"],
                           env=dict(env, PWD=M["main"]), capture_output=True, text=True, timeout=60)
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertEqual(json.loads(s.stdout)["number"], 12)
        agent = [x for x in self.logged() if x["tool"] == "agent"]
        self.assertEqual(len(agent), 1, self.logged())
        self.assertAgentLine(agent[0], cmd="issue view", kind="graphql", exit=0, repo="shop-front", ticket=None)
        self.assertEqual(len(self.logged()), 3)


# ==================================================================== the session puts the shim on PATH
class SessionWiring(Case):
    def test_session_start_puts_the_shim_first_on_path(self):
        """After the SessionStart hook, a shell that sources CLAUDE_ENV_FILE finds the shim as `gh`; a PATH that
        already holds the shim's folder is left as it was."""
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        env_file = write(os.path.join(self.tmp, "env.sh"), "export EARLIER_HOOK_VALUE='kept'\n")
        payload = {"session_id": "sess-1", "transcript_path": "/dev/null", "cwd": project,
                   "permission_mode": "default", "hook_event_name": "SessionStart", "source": "startup"}
        base = [M["fakebin"], "/usr/bin", "/bin"]
        hook_env = self.env(path=base, CLAUDE_PROJECT_DIR=project, CLAUDE_ENV_FILE=env_file,
                            GIT_CEILING_DIRECTORIES=self.tmp)
        r = subprocess.run([sys.executable, SESSION_BRIEF], input=json.dumps(payload), capture_output=True,
                           text=True, env=hook_env, cwd=self.tmp, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(env_file, encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("export EARLIER_HOOK_VALUE='kept'\n"))
        shells = [s for s in ("/bin/sh", "/bin/zsh", "/bin/bash") if os.path.exists(s)]
        real_shim_dir = os.path.realpath(SHIM_DIR)
        for shell in shells:
            with self.subTest(shell=shell):
                s = subprocess.run([shell, "-c", '. "$1" && command -v gh', "sh", env_file], capture_output=True,
                                   text=True, env=self.env(path=base), cwd=self.tmp, timeout=30)
                self.assertEqual(s.returncode, 0, s.stderr)
                self.assertEqual(os.path.realpath(s.stdout.strip()), os.path.realpath(SHIM))
                already = os.pathsep.join([real_shim_dir] + base)
                s = subprocess.run([shell, "-c", '. "$1" && printf %s "$PATH"', "sh", env_file], capture_output=True,
                                   text=True, env=self.env(path=[real_shim_dir] + base), cwd=self.tmp, timeout=30)
                self.assertEqual((s.returncode, s.stdout), (0, already), s.stderr)


if __name__ == "__main__":
    unittest.main()
