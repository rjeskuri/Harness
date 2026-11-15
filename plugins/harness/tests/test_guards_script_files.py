"""tests_first.py: an interpreter or shell run on a program file the worktree does not track (#321).

On a ticket branch the coder, the test-author and the test-breaker are refused a run of
python, node, deno, bun, ruby, perl, php, sh, bash or zsh on a program file that is not
tracked at the worktree's HEAD (in /tmp, the scratchpad, ~, another repository, or new in
the worktree): what it writes cannot be seen. Tracked program files, the test runners,
npm and harness-red still run, and off a ticket branch nothing changes. The fixtures are
in guards_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_script_files.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403

ROLES = ("harness:coder", "harness:test-author", "harness:test-breaker")
# One program per kind of file, each writing a test file and a source file, so whichever
# the role is barred from, the program would write it.
NODE_PROGRAM = (
    "import { writeFileSync } from 'node:fs';\n"
    "writeFileSync('tests/test_app.py', '');\n"
    "writeFileSync('src/app.py', '');\n"
)
PROGRAMS = {
    "py": "open('tests/test_app.py', 'w').write('')\nopen('src/app.py', 'w').write('')\n",
    "mjs": NODE_PROGRAM,
    "ts": NODE_PROGRAM,
    "rb": "File.write('tests/test_app.py', '')\nFile.write('src/app.py', '')\n",
    "pl": "for my $f ('tests/test_app.py', 'src/app.py') { open(my $h, '>', $f) or die; close $h; }\n",
    "php": "<?php\nfile_put_contents('tests/test_app.py', '');\nfile_put_contents('src/app.py', '');\n",
    "sh": ": > tests/test_app.py\n: > src/app.py\n",
}
# Each interpreter and shell the ticket names, run on a program file: (command, with {f}
# for the file, and the file's extension).
INTERPRETERS = (
    ("python {f}", "py"),
    ("python3 {f}", "py"),
    ("node {f}", "mjs"),
    ("deno run {f}", "ts"),
    ("bun {f}", "ts"),
    ("ruby {f}", "rb"),
    ("perl {f}", "pl"),
    ("php {f}", "php"),
    ("sh {f}", "sh"),
    ("bash {f}", "sh"),
    ("zsh {f}", "sh"),
)
# The same runs behind options, wrappers and other spellings of the interpreter.
SPELLINGS = (
    ("python3 -I {f}", "py"),
    ("python3 -u -B {f}", "py"),
    ("python3 -W ignore {f}", "py"),
    ("python3 -X utf8 {f}", "py"),
    ("python3.12 {f}", "py"),
    ("/usr/bin/python3 {f}", "py"),
    (".venv/bin/python {f}", "py"),
    ("env python3 {f}", "py"),
    ("env PYTHONDONTWRITEBYTECODE=1 python3 {f}", "py"),
    ("timeout 60 python3 {f}", "py"),
    ("/usr/bin/env node {f}", "mjs"),
    ("node --no-warnings {f}", "mjs"),
    ("deno {f}", "ts"),
    ("deno run -A {f}", "ts"),
    ("bun run {f}", "ts"),
    ("ruby -w {f}", "rb"),
    ("perl -w {f}", "pl"),
    ("php -f {f}", "php"),
    ("bash -e {f}", "sh"),
    ("/bin/zsh {f}", "sh"),
)
# The definition of done's commands, verbatim: the hook judges the command, so the files
# need not exist.
LITERAL = ("python3 /tmp/w.py", "node /tmp/w.mjs", "bash /tmp/w.sh", "env python3 /tmp/w.py")
# What the loop runs, on the test-author's new, uncommitted tests among others ({wt} is
# the worktree, {red} the plugin's harness-red by its path).
RUNNERS = (
    "python3 -m unittest tests/test_new.py",
    "python3 -m unittest discover -s tests -p 'test_*.py'",
    "python3 -m pytest -q tests/test_new.py",
    ".venv/bin/python -m pytest tests/test_new.py",
    "uv run pytest -q tests/test_new.py",
    "node --test",
    "node --test tests/new.test.js",
    "node --test tests/",
    "npm test",
    "npm test -- tests/new.test.js",
    "npm run test:unit",
    "npm run build",
    "npx vitest run tests/new.test.ts",
    "deno test tests/new.test.ts",
    "deno test -A",
    "bun test",
    "bun test tests/new.test.ts",
    "harness-red run --worktree {wt}",
    "harness-red check",
    "{red} run --worktree {wt}",
    "python3 -m json.tool tests/fixtures/data.json",
    "python3 --version",
    "node --version",
    "bash --version",
    "python3 -c 'print(1)'",
    "sh -c 'echo hi'",
    "bash -lc 'npm test'",
)
# Tracked program files run in other spellings ({wt} is the worktree); the last is the
# project's test command (HARNESS_TEST_CMD in the main checkout's tracked settings).
TRACKED_SPELLINGS = (
    "python3 ./scripts/tool.py",
    "python3 {wt}/scripts/tool.py",
    "cd scripts && python3 tool.py",
    "env python3 scripts/tool.py",
    "python3 -I scripts/tool.py",
    "python3 -m scripts.tool",
    "python3 scripts/run_tests.py tests/test_new.py",
)


class ScriptFileTests(GuardTestCase):
    """The worktree is on t/12-fix-login with a current red record, so only the rule for
    program files can refuse a command here."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        # on main: ignore rules and the project's test command
        self.write(self.repo / ".gitignore", "build/\n.venv/\n")
        settings = {"env": {"HARNESS_TEST_CMD": "python3 scripts/run_tests.py {files}"}}
        self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "Ignore rules and the test command")
        self.wt = self.worktree(self.repo)
        self.state = self.repo_state(self.repo)
        # tracked at the ticket branch's HEAD, not on main
        for ext, program in PROGRAMS.items():
            self.write(self.wt / f"scripts/tool.{ext}", program)
        self.write(self.wt / "scripts/run_tests.py", "import sys\nsys.exit(0)\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add the project's scripts")
        # untracked: outside any repository (the temp directory, ~) and in the worktree
        self.outside = self.tmp / "tmp"
        for ext, program in PROGRAMS.items():
            self.write(self.outside / f"w.{ext}", program)
            self.write(self.home / f"w.{ext}", program)
            self.write(self.wt / f"tools/gen.{ext}", program)
            self.write(self.wt / f"tests/helpers/gen.{ext}", program)
        self.write(self.wt / "build/gen.py", PROGRAMS["py"])  # ignored as well
        self.write(self.wt / "w.py", PROGRAMS["py"])
        self.write(self.wt / "genpkg/__init__.py", "")
        self.write(self.wt / "genpkg/__main__.py", PROGRAMS["py"])
        # the test-author's new tests, not committed yet
        self.write(self.wt / "tests/test_new.py", "def test_new():\n    assert False\n")
        self.write(self.wt / "tests/new.test.js", "require('node:assert').fail('red');\n")
        self.write(self.wt / "tests/new.test.ts", "throw new Error('red');\n")
        self.write(self.wt / "tests/fixtures/data.json", "{}\n")
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

    def bash(self, command: str, agent=None, cwd: Path | None = None, hook: str = "tests_first.py"):
        """The deny reason `hook` gives a Bash `command` run in `cwd` (the worktree), or None."""
        payload = self.payload("Bash", {"command": command}, cwd or self.wt, agent)
        return self.decision(self.run_hook(hook, payload))

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

    # ---- refused on the ticket branch

    def test_an_untracked_program_file_outside_the_worktree_is_refused_to_each_role(self):
        commands = [self.outside_file(t, ext) for t, ext in INTERPRETERS]
        commands += ["python3 ~/w.py", "node ~/w.mjs", "bash ~/w.sh"]
        # tracked, but in another repository: not the worktree's own file
        other = self.make_repo("elsewhere", commit=False)
        for ext in ("py", "sh"):
            self.write(other / f"w.{ext}", PROGRAMS[ext])
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "A program")
        commands += [f"python3 {other}/w.py", f"bash {other}/w.sh"]
        for command in commands:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_the_definition_of_done_commands_are_refused_to_each_role(self):
        for command in LITERAL:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_options_wrappers_and_interpreter_paths_do_not_hide_the_program_file(self):
        for k, (template, ext) in enumerate(SPELLINGS):
            command = self.outside_file(template, ext)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertRefused(self.bash(command, agent), command)

    def test_an_untracked_program_file_in_the_worktree_is_refused_to_each_role(self):
        cases = [(t.format(f=f"tools/gen.{ext}"), None) for t, ext in INTERPRETERS]
        cases += [
            # test files the test-author may write, run as programs
            ("python3 tests/helpers/gen.py", None),
            ("node tests/helpers/gen.mjs", None),
            ("bash tests/helpers/gen.sh", None),
            ("python3 build/gen.py", None),  # ignored by git as well as untracked
            ("python3 ./tools/gen.py", None),
            (f"python3 {self.wt}/tools/gen.py", None),
            ("cd tools && python3 gen.py", "python3 gen.py"),
            # -m with a module the worktree does not track
            ("python3 -m tools.gen", None),
            ("python3 -m genpkg", None),
            ("python3 -m w", None),
        ]
        for command, part in cases:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part or command)

    def test_the_same_command_follows_whether_head_tracks_the_file(self):
        """A new program file is refused until it is committed, then runs; a committed one
        taken out of HEAD (still on disk) is refused again."""
        new = ("python3 tools/gen.py", "node tools/gen.mjs", "bash tools/gen.sh", "python3 -m tools.gen")
        dropped = ("python3 scripts/tool.py", "bash scripts/tool.sh", "python3 -m scripts.tool")
        for command in new:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent, tracked=False):
                    self.assertRefused(self.bash(command, agent), command)
        self.git(self.wt, "add", "tools/gen.py", "tools/gen.mjs", "tools/gen.sh")
        self.git(self.wt, "commit", "-q", "-m", "Commit the generators")
        self.git(self.wt, "rm", "-q", "--cached", "scripts/tool.py", "scripts/tool.sh")
        self.git(self.wt, "commit", "-q", "-m", "Untrack two scripts")
        self.assertTrue((self.wt / "scripts/tool.py").is_file())
        for command in new:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent, tracked=True):
                    self.assertIsNone(self.bash(command, agent))
        for command in dropped:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent, tracked=False):
                    self.assertRefused(self.bash(command, agent), command)

    def test_a_program_file_whose_path_is_not_known_is_refused(self):
        for command, part in (
            ("python3 $F", "python3 $F"),
            ('bash "$SCRIPT"', 'bash "$SCRIPT"'),
            ("F=/tmp/w.py; node $F", "node $F"),
        ):
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part)

    def test_each_part_of_a_command_is_judged_where_it_runs(self):
        py, sh = self.outside / "w.py", self.outside / "w.sh"
        mjs, rb, pl = self.outside / "w.mjs", self.outside / "w.rb", self.outside / "w.pl"
        for k, (cwd, command, part) in enumerate(
            (
                (self.wt, f"cd tests && python3 {py}", f"python3 {py}"),
                (self.wt, f"true && bash {sh}", f"bash {sh}"),
                (self.wt, f"(cd src && node {mjs})", f"node {mjs}"),
                (self.wt, f"echo start; ruby {rb}", f"ruby {rb}"),
                (self.wt, f"sh -c 'python3 {py}'", None),
                (self.repo, f"cd {self.wt} && perl {pl}", f"perl {pl}"),
            )
        ):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertRefused(self.bash(command, agent, cwd=cwd), part)

    def test_an_inline_program_that_writes_stays_refused(self):
        for command in (
            """python3 -c "open('tests/test_app.py', 'w').write('')\"""",
            """node -e "require('fs').writeFileSync('src/app.py', '')\"""",
            heredoc("open('src/app.py', 'w').write('')"),
        ):
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command.split("\n", 1)[0])

    def test_the_breaker_scratch_worktree_follows_the_same_rule(self):
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        refused = (
            self.outside_file("python3 {f}", "py"),
            self.outside_file("bash {f}", "sh"),
            LITERAL[0],
        )
        for command in refused:
            with self.subTest(command=command, where="scratch"):
                self.assertRefused(self.bash(command, breaker, cwd=scratch), command)
        for command in ("python3 scripts/tool.py", "bash scripts/tool.sh", "node --test"):
            with self.subTest(command=command, where="scratch"):
                self.assertIsNone(self.bash(command, breaker, cwd=scratch))

    # ---- still allowed

    def test_a_tracked_program_file_still_runs(self):
        for template, ext in INTERPRETERS:
            command = template.format(f=f"scripts/tool.{ext}")
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))
        for k, template in enumerate(TRACKED_SPELLINGS):
            command = template.format(wt=self.wt)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

    def test_test_runners_npm_and_harness_red_still_run(self):
        red = HOOKS.parent / "bin" / "harness-red"
        for k, template in enumerate(RUNNERS):
            command = template.format(wt=self.wt, red=red)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

    def test_the_main_session_and_other_agents_are_left_alone(self):
        commands = list(LITERAL) + [
            self.outside_file("python3 {f}", "py"),
            self.outside_file("ruby {f}", "rb"),
            self.outside_file("zsh {f}", "sh"),
            "python3 tools/gen.py",
        ]
        for command in commands:
            for agent in (None, "harness:reviewer"):
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))

    def test_off_a_ticket_branch_nothing_changes(self):
        commands = list(LITERAL) + [
            self.outside_file("python3 {f}", "py"),
            self.outside_file("deno run {f}", "ts"),
            self.outside_file("ruby {f}", "rb"),
            self.outside_file("zsh {f}", "sh"),
        ]
        detached = self.tmp / "detached"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(detached), BRANCH)
        for where, cwd in (("main checkout on main", self.repo), ("detached worktree", detached)):
            for k, command in enumerate(commands):
                agent = ROLES[k % len(ROLES)]
                with self.subTest(command=command, agent=agent, where=where):
                    self.assertIsNone(self.bash(command, agent, cwd=cwd))
        # branch_guard.py on main lets them run as it does today
        agents = (None,) + ROLES
        for k, command in enumerate(commands):
            agent = agents[k % len(agents)]
            with self.subTest(command=command, agent=agent, hook="branch_guard.py"):
                self.assertIsNone(self.bash(command, agent, cwd=self.repo, hook="branch_guard.py"))
        self.git(self.wt, "switch", "-q", "-c", "feature/login")
        for k, command in enumerate(commands + ["python3 tools/gen.py"]):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent, where="feature/login"):
                self.assertIsNone(self.bash(command, agent))

    # ---- fails open, keeps its deadline

    def test_odd_interpreter_commands_keep_the_hook_contract(self):
        """However a command is cut short, the hook exits 0, prints a well-formed denial or
        nothing, and at most one line on stderr: never a traceback."""
        for command in (
            "python3 '/tmp/w.py",
            "python3 -m",
            "python3 -I",
            "python3 -W",
            "python3 ''",
            "node --",
            "node -r",
            "bash -",
            "env",
            "deno run",
            "bun run",
            "php -f",
        ):
            with self.subTest(command=command):
                payload = self.payload("Bash", {"command": command}, self.wt, "harness:coder")
                r = subprocess.run(
                    [sys.executable, str(HOOKS / "tests_first.py")],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    env=self.env,
                    cwd=str(self.tmp),
                    timeout=60,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertLessEqual(len(r.stderr.splitlines()), 1, r.stderr)
                self.decision(r)  # empty, or a well-formed denial

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
