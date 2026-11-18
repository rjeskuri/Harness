"""tests_first.py: an interpreter or shell run on a program file the worktree does not track (#321).

On a ticket branch the coder, the test-author and the test-breaker are refused a run of an
interpreter or shell (python, node, sh, bash, zsh among others) on a program file that is
not tracked at the worktree's HEAD (in /tmp, the scratchpad, ~, another repository, or new
in the worktree): what it writes cannot be seen. Tracked means in HEAD's tree, not the
index. A module python resolves to such a file is the same program, and a test runner
given a program file outside the worktree is refused the same way; the breaker's scratch
worktree follows the same rule. Tracked program files, a runner on the worktree's own
files (tracked or new, or with no target in the worktree), harness-red and installed
modules still run; the main session, other agents and every role off a ticket branch are
left alone (branch_guard.py on main too).

The cases, one row each, are what the loop's agents and the owner type. Ticket #377 (epic
#374) collapsed the tests into these two tables and dropped the rest: the ruby, perl, php,
bun and deno spellings, further spellings of the same decision (options, wrappers,
preloads, PYTHONPATH, standard-library runners, JS runner configs) and truncated commands.
The hook still judges those; this module no longer pins them.

Left to follow-up tickets: a program on stdin or a pipe (python3 - < f, bash < f,
cat f | python3, <<<), source f and . f, inline loaders (python3 -c 'import tools.gen',
exec(open(..)), node -e 'import(..)'), and the admitted hole that an in-tree untracked
program runs under a runner (python3 -m unittest tools/gen.py): the test-author's new files
must run. The fixtures are in guards_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_script_files.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403

ROLES = ("harness:coder", "harness:test-author", "harness:test-breaker")
BREAKER = "harness:test-breaker"
# One program per kind of file, each writing a test file and a source file, so whichever
# the role is barred from, the program would write it.
PROGRAMS = {
    "py": "open('tests/test_app.py', 'w').write('')\nopen('src/app.py', 'w').write('')\n",
    "mjs": (
        "import { writeFileSync } from 'node:fs';\n"
        "writeFileSync('tests/test_app.py', '');\n"
        "writeFileSync('src/app.py', '');\n"
    ),
    "sh": ": > tests/test_app.py\n: > src/app.py\n",
}


class ScriptFileTests(GuardTestCase):
    """The worktree is on t/12-fix-login with a current red record, so only the rule for
    program files can refuse a command here."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        # on main: ignore rules and the project's test command
        self.write(self.repo / ".gitignore", "build/\n.venv/\n")
        settings = {"env": {"HARNESS_TEST_CMD": "python3 -m unittest {files}"}}
        self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "Ignore rules and the test command")
        self.wt = self.worktree(self.repo)
        self.state = self.repo_state(self.repo)
        # tracked at the ticket branch's HEAD, not on main
        self.write(self.wt / "scripts/tool.py", PROGRAMS["py"])
        self.write(self.wt / "scripts/tool.sh", PROGRAMS["sh"])
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add the project's scripts")
        # untracked: outside any repository and in the worktree; one only staged
        self.outside = self.tmp / "tmp"
        for ext, program in PROGRAMS.items():
            self.write(self.outside / f"w.{ext}", program)
        self.write(self.wt / "tools/gen.py", PROGRAMS["py"])
        self.write(self.wt / "tools/staged.py", PROGRAMS["py"])
        self.git(self.wt, "add", "tools/staged.py")
        # the test-author's new test, not committed yet
        self.write(self.wt / "tests/test_new.py", "def test_new():\n    assert False\n")
        self.cache()
        self.red()

    # ---- fixtures

    def repo_state(self, repo: Path) -> Path:
        """~/.claude/harness/repos/<key> for the main checkout `repo`."""
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        return self.home / ".claude/harness/repos" / key

    def cache(self) -> None:
        ticket = {
            "number": 12,
            "title": "Fix login",
            "type": "task",
            "status": "in-progress",
            "flags": [],
            "labels": ["task", "status:in-progress"],
        }
        self.write(self.state / "tickets/12.json", json.dumps(ticket))

    def red(self) -> None:
        """A current red record for t/12-fix-login over tests/test_app.py."""
        rel = "tests/test_app.py"
        record = {
            "branch": BRANCH,
            "base_sha": "0" * 40,
            "test_files": [rel],
            "test_hash": content_hash(self.wt, [rel]),
            "cmd": "python3 -m unittest tests/test_app.py",
            "exit": 1,
            "kind": "assertion",
            "tail": "AssertionError",
            "at": "2026-11-15T10:00:00Z",
        }
        self.write(self.state / "red/t-12-fix-login.json", json.dumps(record))

    def scratch(self) -> Path:
        """The breaker's scratch worktree: detached at the ticket branch, in the repo's state."""
        path = self.state / "scratch" / "t-12-fix-login"
        path.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(path), BRANCH)
        return path

    def bash(self, command: str, agent=None, cwd: Path | None = None, hook: str = "tests_first.py"):
        """The deny reason `hook` gives a Bash `command` run in `cwd` (the worktree), or None.
        The hook's environment carries PWD as Claude Code's does: the session's directory,
        which is the part's only until the command cds."""
        payload = self.payload("Bash", {"command": command}, cwd or self.wt, agent)
        return self.decision(self.run_hook(hook, payload, env={"PWD": str(cwd or self.wt)}))

    def outside_file(self, template: str, ext: str) -> str:
        return template.format(f=self.outside / f"w.{ext}")

    def assertRefused(self, reason, part: str | None = None) -> None:
        """A Bash denial naming `part` (any part when None) that says to write with Edit or Write."""
        self.assertIsNotNone(reason, f"{part!r} was allowed")
        if part is None:
            self.assertTrue(reason.startswith('Refused "'), reason)
            why = reason.split('": ', 1)[-1]
        else:
            prefix = f'Refused "{as_shown(part)}": '
            self.assertTrue(reason.startswith(prefix), reason)
            why = reason[len(prefix):]
        self.assertIn("Edit", why, reason)
        self.assertIn("Write", why, reason)

    # ---- the two tables

    def test_a_program_file_the_worktree_does_not_track_is_refused(self):
        o = self.outside
        rows = (
            # (command, where it runs, the roles it is refused to)
            (f"python3 {o}/w.py", self.wt, ROLES),  # outside the worktree, for each interpreter kind
            (f"node {o}/w.mjs", self.wt, ROLES),
            (f"bash {o}/w.sh", self.wt, ROLES),
            (f"sh {o}/w.sh", self.wt, ROLES),
            (f".venv/bin/python {o}/w.py", self.wt, ROLES),  # the project's own interpreter
            ("python3 tools/gen.py", self.wt, ROLES),  # new in the worktree
            ("python3 tools/staged.py", self.wt, ROLES),  # staged, not committed: HEAD lacks it
            ("python3 -m tools.gen", self.wt, ROLES),  # a module resolved to an untracked file
            (f"python3 -m pytest -q {o}/w.py", self.wt, ROLES),  # a test runner given one
            (f"python3 {o}/w.py", self.scratch(), (BREAKER,)),  # the breaker's scratch worktree
            # judged where the part runs: outside any worktree, against the hook's
            (f"cd {o} && python3 w.py", self.wt, ROLES),
        )
        quoted = {f"cd {o} && python3 w.py": "python3 w.py"}  # the part the reason names
        for command, cwd, agents in rows:
            for agent in agents:
                with self.subTest(command=command, agent=agent, cwd=cwd.name):
                    self.assertRefused(self.bash(command, agent, cwd=cwd), quoted.get(command, command))

    def test_tracked_programs_runners_and_other_sessions_still_run(self):
        o, scratch = self.outside, self.scratch()
        tests_first = ("tests_first.py",)
        rows = (
            # (command, where it runs, the agents, the hooks that let it through)
            ("python3 scripts/tool.py", self.wt, ROLES, tests_first),  # tracked at HEAD
            ("sh scripts/tool.sh", self.wt, ROLES, tests_first),
            ("cd scripts && python3 tool.py", self.wt, ROLES, tests_first),  # found from the part's directory
            ("python3 scripts/tool.py", scratch, (BREAKER,), tests_first),
            # from the ticket worktree into the scratch: judged against the scratch it runs in
            (f"cd {scratch} && python3 scripts/tool.py", self.wt, (BREAKER,), tests_first),
            ("python3 -m unittest tests/test_new.py", self.wt, ROLES, tests_first),  # the new test
            ("python3 -m pytest -q", self.wt, ROLES, tests_first),  # no target: collects the worktree
            (f"harness-red run --worktree {self.wt}", self.wt, ROLES, tests_first),
            ("python3 -m pip --version", self.wt, ROLES, tests_first),  # an installed module
            (f"python3 {o}/w.py", self.wt, (None, "harness:reviewer"), tests_first),  # not a loop role
            # off a ticket branch: the main checkout on main, where branch_guard.py judges too
            (f"python3 {o}/w.py", self.repo, (None,) + ROLES, ("tests_first.py", "branch_guard.py")),
        )
        for command, cwd, agents, hooks in rows:
            for agent in agents:
                for hook in hooks:
                    with self.subTest(command=command, agent=agent, cwd=cwd.name, hook=hook):
                        self.assertIsNone(self.bash(command, agent, cwd=cwd, hook=hook))

    # ---- keeps its deadline

    def hook_with_slow_git(self, payload: dict, delay: float, hang=None):
        """(deny reason or None, seconds the hook took, git calls) for tests_first.py run as
        its own __main__ in this process, every git call taking `delay` seconds of a fake
        clock (SlowGit) and the calls `hang` picks never answering."""
        ours = ("harness_lib", "branch_guard")
        saved = {k: m for k, m in sys.modules.items() if k in ours or k.startswith("harness_lib.")}
        for k in saved:
            del sys.modules[k]
        slow = SlowGit(delay, hang)
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
                stack.enter_context(mock.patch.object(subprocess, "run", slow.run))
                stack.enter_context(mock.patch.object(time, "monotonic", slow.monotonic))
                stack.enter_context(mock.patch.object(time, "perf_counter", slow.perf_counter))
                stack.enter_context(mock.patch.object(time, "time", slow.time))
                stack.enter_context(mock.patch.object(sys, "stdin", stdin))
                stack.enter_context(mock.patch.object(sys, "dont_write_bytecode", True))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                start = slow.monotonic()
                try:
                    runpy.run_path(str(HOOKS / "tests_first.py"), run_name="__main__")
                except SystemExit as exc:
                    self.assertIn(exc.code, (0, None), err.getvalue())
                took = slow.monotonic() - start
        finally:
            for k in [k for k in sys.modules if k in ours or k.startswith("harness_lib.")]:
                del sys.modules[k]
            sys.modules.update(saved)
        r = types.SimpleNamespace(stdout=out.getvalue(), stderr=err.getvalue())
        return self.decision(r), took, slow.calls

    def test_a_program_file_is_judged_within_the_hooks_deadline(self):
        limit = hook_timeouts("tests_first.py")["Bash"] - STARTUP_ALLOWANCE
        untracked = (
            self.outside_file("python3 {f}", "py"),
            "python3 tools/gen.py",
            "python3 -m tools.gen",
        )
        tracked = "python3 scripts/tool.py"
        payloads = {
            c: self.payload("Bash", {"command": c}, self.wt, "harness:coder")
            for c in untracked + (tracked,)
        }
        # at full speed the in-process run decides as the hook's own process does
        for command, payload in payloads.items():
            with self.subTest(command=command, delay=0.0):
                reason, _, calls = self.hook_with_slow_git(payload, 0.0)
                self.assertEqual(reason, self.decision(self.run_hook("tests_first.py", payload)))
                if command == tracked:
                    self.assertIsNone(reason, calls)
                else:
                    self.assertRefused(reason, command)
        # slow git, and git calls that never answer: decided in time, and an untracked
        # program file is never let through because git was slow
        hangs = (
            ("none", None, (0.5, 1.0, 2.0)),
            ("the main checkout lookup", lambda argv, i: "--git-common-dir" in argv, (0.0, 1.0)),
            (
                "every call but the branch lookups",
                lambda argv, i: "--abbrev-ref=loose" not in argv,
                (0.0, 1.0),
            ),
        )
        for hang_name, hang, delays in hangs:
            for delay in delays:
                for command, payload in payloads.items():
                    with self.subTest(command=command, hang=hang_name, delay=delay):
                        reason, took, calls = self.hook_with_slow_git(payload, delay, hang)
                        self.assertLess(took, limit, calls)
                        self.assertNotIn("hung, no timeout", [s for _, s in calls], calls)
                        if command != tracked:
                            self.assertIsNotNone(reason, calls)


if __name__ == "__main__":
    unittest.main()
