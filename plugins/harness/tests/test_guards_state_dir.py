"""The harness state directory's red/, red-files/ and tickets/ (#267): no session or agent writes them directly.

~/.claude/harness/repos/<key>/ lies outside every repository, so before #267 neither branch_guard
nor tests_first judged a write there, and a forged red record, side file or ticket cache (a
`no-tests` flag) got past the tests-first gate. Now an Edit, a Write and a Bash write (a
redirection, tee, cp, mv, sed -i) into red/, red-files/ or tickets/ of any repository's state is
denied, for the main session and every agent; the tools that own that state (harness-red,
harness-board, harness-work) still run and still write it, reading it stays allowed, and the rest
of the state directory (reviews/, the test-breaker's scratch worktree) is not newly guarded.

A call counts as denied when any PreToolUse hook hooks.json registers for its tool denies it,
as Claude Code decides, so the rule may live in whichever hook. Each method's first row runs
the hooks as real processes, its other rows in this process (gitrepo_support.run_in_process);
the fixture is built once for the class.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_state_dir.py" -v
"""

from __future__ import annotations

import stat
import unittest

from guards_support import *  # noqa: F401,F403

BIN = HOOKS.parent / "bin"
BOARD_TOOL = BIN / "harness-board"
FAKEGH = Path(__file__).resolve().parent / "fakegh.py"
OTHER_KEY = "0123456789ab"  # another repository's state under the same ~/.claude/harness/repos
SCRATCH_SLUG = "t-12-fix-login"
RED_BRANCH = "t/14-red-tool"  # the branch the real harness-red check records
AUTHOR_TEST = "from src.app import login\n\n\ndef test_login():\n    assert login() == 'ok'\n"
FORGED = json.dumps({"kind": "assertion", "flags": ["no-tests"], "type": "refactor"})


def pipeline(tool: str) -> list:
    """The hook scripts hooks.json runs before `tool`: every PreToolUse entry whose matcher
    matches the tool name in full, in order."""
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    scripts = []
    for entry in data["hooks"]["PreToolUse"]:
        if re.fullmatch(entry["matcher"], tool):
            for hook in entry["hooks"]:
                scripts.extend(HOOKS / os.path.basename(a) for a in hook.get("args", []) if a.endswith(".py"))
    return scripts


def denial(stdout: str) -> str | None:
    """The reason when a hook's output denies the call; None for no output, allow or ask."""
    if not stdout.strip():
        return None
    out = json.loads(stdout).get("hookSpecificOutput") or {}
    if out.get("permissionDecision") != "deny":
        return None
    return out.get("permissionDecisionReason") or "(denied, no reason)"


class StateDirGuardTests(GuardTestCase):
    """main on proj, the ticket worktree wt on t/12-fix-login with the test-author's commit, a
    current red record, side file and ticket cache for it, the test-breaker's scratch worktree
    under the state's scratch/, and a second worktree on t/14-red-tool for the real tools."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        fx = GuardTestCase("setUp")  # the suite's fixture helpers, once for the class
        fx.setUp()
        cls._fixture = fx
        cls.repo = fx.make_repo()
        cls.wt = fx.worktree(cls.repo)
        cls.wt14 = fx.worktree(cls.repo, RED_BRANCH, "wt14")
        for wt in (cls.wt, cls.wt14):  # the test-author's change, committed
            fx.write(wt / "tests/test_app.py", AUTHOR_TEST)
            fx.git(wt, "commit", "-q", "-am", "tests for login")
        key = hashlib.sha1(str(cls.repo).encode()).hexdigest()[:12]
        cls.state = fx.home / ".claude/harness/repos" / key
        cls.other = fx.home / ".claude/harness/repos" / OTHER_KEY
        test_hash = content_hash(cls.wt, ["tests/test_app.py"])
        record = {
            "branch": BRANCH, "base_sha": "0" * 40, "test_files": ["tests/test_app.py"],
            "run_files": ["tests/test_app.py"], "test_hash": test_hash,
            "cmd": "python3 -m unittest tests/test_app.py", "exit": 1, "kind": "assertion",
            "tail": "AssertionError", "at": "2026-11-19T10:00:00Z",
        }
        fx.write(cls.state / "red/t-12-fix-login.json", json.dumps(record))
        side = {"test_hash": test_hash, "files": {"tests/test_app.py": ""}}
        fx.write(cls.state / "red-files/t-12-fix-login.json", json.dumps(side))
        ticket = {"number": 12, "title": "Fix login", "type": "task", "status": "in-progress",
                  "flags": [], "labels": ["task", "status:in-progress"]}
        fx.write(cls.state / "tickets/12.json", json.dumps(ticket))
        fx.write(cls.state / "reviews/12/rounds.json", "[]")
        fx.write(cls.other / "tickets/5.json", json.dumps(dict(ticket, number=5)))
        (cls.other / "red").mkdir()
        cls.scratch = cls.state / "scratch" / SCRATCH_SLUG
        fx.git(cls.repo, "worktree", "add", "-q", "--detach", str(cls.scratch), BRANCH)
        cls.link = fx.tmp / "statelink"  # another spelling of the state directory
        cls.link.symlink_to(cls.state)
        cls.forged = fx.write(fx.tmp / "tmp/forged.json", FORGED)
        # gh for harness-board: the scripted fake, ticket #14 and the login
        fx.git(cls.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        gh =fx.write(fx.tmp / "bin/gh", f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        issue = {"number": 14, "title": "Red tool", "url": "https://github.com/o/r/issues/14", "body": "",
                 "labels": [{"name": "task"}, {"name": "status:in-progress"}], "state": "OPEN",
                 "parent": None, "blockedBy": [], "comments": []}
        scenario = {"calls": [{"match": ["issue", "view", "14"], "stdout_json": issue},
                              {"match": ["api", "user"], "stdout": "owner\n"}]}
        fx.write(fx.tmp / "scenario.json", json.dumps(scenario))
        cls.env = dict(
            fx.env,
            HARNESS_TEST_CMD=shlex.quote(sys.executable) + " -c " + shlex.quote(FAILING_RUN) + " {files}",
            HARNESS_GH=str(gh),
            FAKEGH_SCENARIO=str(fx.tmp / "scenario.json"),
            FAKEGH_LOG=str(fx.tmp / "gh.log"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fixture.doCleanups()
        super().tearDownClass()

    def setUp(self) -> None:  # the class's fixture, not one per test
        fx = self._fixture
        self.tmp, self.home, self.env = fx.tmp, fx.home, dict(self.env)

    # ---- the hooks

    def verdict(self, tool: str, tool_input: dict, cwd: Path, agent=None) -> str | None:
        """The deny reason of the first hook hooks.json runs before `tool` that denies the call,
        None when none does. The test's first call runs each hook as its own process."""
        stdin = json.dumps(self.payload(tool, tool_input, cwd, agent))
        first, self._ran_a_hook_process = not self._ran_a_hook_process, True
        for script in pipeline(tool):
            if first:
                r = subprocess.run(
                    [sys.executable, str(script)], input=stdin, capture_output=True, text=True,
                    env=self.env, cwd=str(self.tmp), timeout=60,
                )
            else:
                r = self.run_in_process(script, stdin)
            self.assertEqual((r.returncode, r.stderr), (0, ""), script.name)
            reason = denial(r.stdout)
            if reason is not None:
                return reason
        return None

    def edit_input(self, tool: str, path: Path) -> dict:
        if tool == "Edit":
            return {"file_path": str(path), "old_string": '"kind": "green"', "new_string": '"kind": "assertion"'}
        return {"file_path": str(path), "content": FORGED}

    # ---- Edit and Write

    def test_edit_and_write_into_red_red_files_and_tickets_are_denied(self):
        s, other = self.state, self.other
        rows = (  # (tool, path, agent_type or None for the main session, the session's cwd)
            ("Write", s / "red/t-12-fix-login.json", "harness:coder", self.wt),
            ("Edit", s / "red/t-12-fix-login.json", "harness:test-author", self.wt),
            ("Write", s / "red-files/t-12-fix-login.json", "harness:test-breaker", self.scratch),
            ("Edit", s / "red-files/t-12-fix-login.json", None, self.repo),
            ("Edit", s / "tickets/12.json", "harness:coder", self.wt),
            ("Write", s / "tickets/13.json", None, self.repo),  # a new cache
            ("Edit", s / "tickets/12.json", "harness:reviewer", self.wt),
            ("Write", s / "red/t-13-other.json", "harness:quick", self.wt),  # another branch's record
            ("Write", other / "tickets/5.json", "general-purpose", self.wt),  # another repository's state
            ("Write", other / "red/t-5-x.json", "harness:planner", self.repo),
            # the state directory through a symlink, and with ..
            ("Write", self.link / "reviews/../red/t-12-fix-login.json", "harness:coder", self.wt),
        )
        for tool, path, agent, cwd in rows:
            with self.subTest(tool=tool, path=str(path), agent=agent):
                self.assertIsNotNone(self.verdict(tool, self.edit_input(tool, path), cwd, agent))

    def test_edit_and_write_elsewhere_are_not_newly_denied(self):
        rows = (
            ("Write", self.state / "reviews/12/rounds.json", None, self.repo),  # the rest of the state
            ("Write", self.state / "cost/12.json", "harness:coder", self.wt),
            # the breaker's source edit in its scratch worktree under the state's scratch/
            ("Edit", self.scratch / "src/app.py", "harness:test-breaker", self.scratch),
            # the coder's source edit (the red record is current), in a directory named tickets
            ("Write", self.wt / "src/tickets/red.py", "harness:coder", self.wt),
            ("Write", self.tmp / "tmp/notes.json", "harness:test-author", self.wt),  # outside any repo
        )
        for tool, path, agent, cwd in rows:
            with self.subTest(tool=tool, path=str(path), agent=agent):
                self.assertIsNone(self.verdict(tool, self.edit_input(tool, path), cwd, agent))

    # ---- Bash

    def test_bash_writes_into_red_red_files_and_tickets_are_denied(self):
        s = self.state
        tilde = "~/" + str(s.relative_to(self.home))
        rows = (  # (command, agent_type or None, cwd)
            (f"echo '{FORGED}' > {tilde}/red/t-12-fix-login.json", "harness:coder", self.wt),
            (f"printf '%s' '{FORGED}' >> {s}/tickets/12.json", "harness:test-author", self.wt),
            (f"echo '{FORGED}' | tee {s}/tickets/12.json", None, self.repo),
            (f"cp {self.forged} {s}/red-files/t-12-fix-login.json", "harness:test-breaker", self.scratch),
            (f"cp forged.json {s}/red/", "harness:coder", self.tmp / "tmp"),  # into the directory
            (f"mv {self.forged} {s}/tickets/12.json", "harness:quick", self.wt),
            (f"sed -i '' 's/\"task\"/\"refactor\"/' {s}/tickets/12.json", "general-purpose", self.wt),
            (f"cd {s}/red && echo '{FORGED}' > t-12-fix-login.json", "harness:coder", self.wt),
            # from the breaker's scratch worktree, relative
            (f"echo '{FORGED}' > ../../red/t-12-fix-login.json", "harness:test-breaker", self.scratch),
            (f"echo '{FORGED}' > {self.other}/tickets/5.json", None, self.repo),  # another repository's
        )
        for command, agent, cwd in rows:
            with self.subTest(command=command, agent=agent):
                self.assertIsNotNone(self.verdict("Bash", {"command": command}, cwd, agent))

    def test_reads_and_the_harness_tools_are_not_denied(self):
        s = self.state
        tilde = "~/" + str(s.relative_to(self.home))
        rows = (
            (f"cat {s}/red/t-12-fix-login.json", "harness:coder", self.wt),
            (f"ls {tilde}/tickets", "harness:test-author", self.wt),
            (f"cat {s}/tickets/12.json > {self.tmp}/tmp/ticket.json", None, self.repo),  # read, write elsewhere
            (f"cp {s}/red/t-12-fix-login.json {self.tmp}/tmp/", "harness:test-breaker", self.scratch),
            (f"echo '[]' > {s}/reviews/12/rounds.json", None, self.repo),  # the rest of the state
            (f"harness-red check --worktree {self.wt}", None, self.repo),
            (f"{BIN}/harness-red run --worktree {self.wt}", "harness:test-author", self.wt),
            (f"cd {self.wt} && harness-red status --json", "harness:coder", self.repo),
            ("harness-red scratch run", "harness:test-breaker", self.scratch),
            ("harness-board show 12 --json", None, self.wt),
            ("harness-work start 12", None, self.repo),
        )
        for command, agent, cwd in rows:
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.verdict("Bash", {"command": command}, cwd, agent))

    # ---- the tools write their state

    def test_harness_red_check_and_harness_board_show_still_write_their_state(self):
        record_path = self.state / "red/t-14-red-tool.json"
        cache_path = self.state / "tickets/14.json"
        self.assertFalse(record_path.exists() or cache_path.exists())
        for command in (f"harness-red check --worktree {self.wt14}", "harness-board show 14"):
            self.assertIsNone(self.verdict("Bash", {"command": command}, self.repo), command)
        # the test's first runs were the hooks' own processes; the tools run in this process
        r = self.run_in_process(RED_TOOL, args=("check", "--worktree", str(self.wt14)), cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual((record["branch"], record["kind"]), (RED_BRANCH, "assertion"))
        self.assertEqual(record["test_hash"], content_hash(self.wt14, ["tests/test_app.py"]))
        side = json.loads((self.state / "red-files/t-14-red-tool.json").read_text(encoding="utf-8"))
        self.assertEqual(side["test_hash"], record["test_hash"])
        b = self.run_in_process(BOARD_TOOL, args=("show", "14"), cwd=self.repo)
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual((cache["number"], cache["title"], cache["type"]), (14, "Red tool", "task"))


if __name__ == "__main__":
    unittest.main()
