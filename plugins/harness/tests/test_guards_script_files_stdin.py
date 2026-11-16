"""tests_first.py: a program file an interpreter reads on stdin, through a redirect, a pipe or
a here-string (#364).

#321 refuses python3 /tmp/w.py; the same program fed to an interpreter that reads its
program from stdin (python3 - < f, python3 < f, bash -s < f, cat f | python3,
python3 - <<< "$(cat f)") is the same run, so for the coder, the test-author and the
test-breaker on a ticket branch it is judged as #321 judges a program file: tracked at the
worktree's HEAD runs, untracked (new, ignored, a test file, only staged) or outside the
worktree (the temp directory, ~, another repository) is refused, and the refusal names the
command and says to write with Edit or Write. Data on stdin is not a program: an
interpreter given a program file, a module or -c reads its stdin as data, and a pipe whose
reader is not an interpreter (cat f | grep x, git diff | head) runs. Off a ticket branch,
for the main session and for other agents nothing changes.

The fixture (ProgramFormCase) is shared with test_guards_script_files_source.py and
test_guards_script_files_inline.py: the hook refuses the test-author an edit of
guards_support.py, which the default test patterns call source. Every program file is
inert (the hook judges the command and never runs one): what matters is where it stands,
whether HEAD tracks it, and its shebang.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_script_files*.py" -v
"""

from __future__ import annotations

import re
import unittest

from guards_support import *  # noqa: F401,F403

ROLES = ("harness:coder", "harness:test-author", "harness:test-breaker")
# Inert program files, each with the shebang it would be run by ("" is a file without an
# extension): the hook never runs them.
INERT = {
    "py": "#!/usr/bin/env python3\nprint('fixture')\n",
    "mjs": "#!/usr/bin/env node\nconsole.log('fixture');\n",
    "cjs": "#!/usr/bin/env node\nconsole.log('fixture');\n",
    "ts": "console.log('fixture');\n",
    "rb": "#!/usr/bin/env ruby\nputs 'fixture'\n",
    "pl": "#!/usr/bin/env perl\nprint \"fixture\\n\";\n",
    "php": "#!/usr/bin/env php\n<?php echo 'fixture';\n",
    "sh": "#!/bin/sh\n: fixture\n",
    "": "#!/bin/sh\n: fixture\n",
}
BIN = HOOKS.parent / "bin"


def named(stem: str, ext: str) -> str:
    """`stem` with the extension `ext`, or bare for ""."""
    return f"{stem}.{ext}" if ext else stem


class ProgramFormCase(GuardTestCase):
    """The worktree is on t/12-fix-login with a current red record, so only the rule for
    program files can refuse a command here. The same inert program stands, executable,
    in every place: tracked at the branch's HEAD (scripts/tool.*), and not: in the temp
    directory, in ~, tracked in another repository, new in the worktree (tools/gen.*),
    ignored (build/gen.*), a new test file (tests/helpers/gen.*) and staged but not
    committed (tools/staged.*)."""

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
        for ext in INERT:
            self.program(self.wt / named("scripts/tool", ext), ext)
        self.program(self.wt / "scripts/smoke.sh", "sh")
        self.write(self.wt / "scripts/run_tests.py", "import sys\nsys.exit(0)\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add the project's scripts")
        # not tracked at the worktree's HEAD
        self.outside = self.tmp / "tmp"
        self.elsewhere = self.make_repo("elsewhere", commit=False)
        stems = (
            self.outside / "w",
            self.home / "w",
            self.elsewhere / "w",
            self.wt / "tools/gen",
            self.wt / "build/gen",
            self.wt / "tests/helpers/gen",
            self.wt / "tools/staged",
        )
        for ext in INERT:
            for stem in stems:
                self.program(Path(named(str(stem), ext)), ext)
        self.git(self.elsewhere, "add", "-A")
        self.git(self.elsewhere, "commit", "-q", "-m", "Programs, tracked in another repository")
        self.git(self.wt, "add", *[named("tools/staged", ext) for ext in INERT])
        self.program(self.wt / "w.py", "py")  # a module at the worktree's top
        # the test-author's new tests and test setup, not committed yet
        self.write(self.wt / "tests/test_new.py", "def test_new():\n    assert False\n")
        self.write(self.wt / "tests/new.test.js", "require('node:assert').fail('red');\n")
        self.write(self.wt / "tests/new.test.ts", "throw new Error('red');\n")
        self.write(self.wt / "tests/setup.mjs", "process.env.TZ = 'UTC';\n")
        self.write(self.wt / "tests/fixtures/data.json", "{}\n")
        # data, not programs
        self.write(self.outside / "input.txt", "data\n")
        self.write(self.outside / "data.json", "{}\n")
        self.cache()
        self.red()

    # ---- fixtures

    def program(self, path: Path, ext: str, mode: int = 0o755) -> Path:
        """The inert program for `ext` at `path`, with permissions `mode`."""
        self.write(path, INERT[ext])
        os.chmod(path, mode)
        return path

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

    def refused_files(self, ext: str) -> list:
        """(where, the file as a command spells it) for each place the program `ext` is not
        tracked at the worktree's HEAD."""
        return [
            ("the temp directory", str(self.outside / named("w", ext))),
            ("~", "~/" + named("w", ext)),
            ("another repository", str(self.elsewhere / named("w", ext))),
            ("new in the worktree", named("tools/gen", ext)),
            ("new, spelled ./", "./" + named("tools/gen", ext)),
            ("new, spelled absolute", str(self.wt / named("tools/gen", ext))),
            ("ignored", named("build/gen", ext)),
            ("a new test file", named("tests/helpers/gen", ext)),
            ("staged, not committed", named("tools/staged", ext)),
        ]

    def tracked_files(self, ext: str) -> list:
        """The tracked program `ext`, spelled three ways."""
        rel = named("scripts/tool", ext)
        return [rel, "./" + rel, str(self.wt / rel)]

    def fill(self, template: str) -> str:
        """`template` with {o} the temp directory, {home} ~, {elsewhere} the other
        repository, {wt} the worktree, {red} and {bin} the plugin's harness-red and bin."""
        return template.format(
            o=self.outside, home=self.home, elsewhere=self.elsewhere, wt=self.wt,
            red=BIN / "harness-red", bin=BIN,
        )

    def bash(self, command: str, agent=None, cwd: Path | None = None, hook: str = "tests_first.py"):
        """The deny reason `hook` gives a Bash `command` run in `cwd` (the worktree), or None.
        The hook's environment carries PWD as Claude Code's does: the session's directory."""
        payload = self.payload("Bash", {"command": command}, cwd or self.wt, agent)
        return self.decision(self.run_hook(hook, payload, env={"PWD": str(cwd or self.wt)}))

    def assertRefused(self, reason, *parts: str) -> None:
        """A Bash denial naming one of `parts` (any part when none is given) that says to
        write with Edit or Write."""
        self.assertIsNotNone(reason, f"{parts[0] if parts else 'the command'!r} was allowed")
        if parts:
            prefixes = sorted((f'Refused "{as_shown(p)}": ' for p in parts), key=len, reverse=True)
            found = [p for p in prefixes if reason.startswith(p)]
            self.assertTrue(found, f"{reason!r} does not name any of {parts!r}")
            why = reason[len(found[0]):]
        else:
            self.assertTrue(reason.startswith('Refused "'), reason)
            why = reason.split('": ', 1)[-1]
        self.assertIn("Edit", why, reason)
        self.assertIn("Write", why, reason)

    def expect_refused(self, cases, roles=ROLES) -> None:
        """Each (command, the parts the reason may name) refused to each of `roles`."""
        for command, parts in cases:
            for agent in roles:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), *parts)

    def expect_allowed(self, commands, cwd: Path | None = None) -> None:
        """Each command allowed, the three roles taking turns."""
        for k, command in enumerate(commands):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent, cwd=cwd))

    def rotated(self, forms, parts_of) -> list:
        """Each form ({f} for the program file, its extension) on three of the places it is
        not tracked, one per role: across nine forms every place meets every role.
        (command, parts, role) triples; `parts_of(command)` gives the parts the reason may
        name."""
        cases = []
        for i, (template, ext) in enumerate(forms):
            files = self.refused_files(ext)
            for j, agent in enumerate(ROLES):
                _, f = files[(i + 4 * j) % len(files)]
                command = template.format(f=f)
                cases.append((command, parts_of(command), agent))
        return cases

    def expect_rotated(self, forms, parts_of) -> None:
        for command, parts, agent in self.rotated(forms, parts_of):
            with self.subTest(command=command, agent=agent):
                self.assertRefused(self.bash(command, agent), *parts)

    def expect_everywhere(self, forms, parts_of) -> None:
        """Each form refused on every place its program is not tracked, roles taking turns."""
        k = 0
        for template, ext in forms:
            for where, f in self.refused_files(ext):
                command = template.format(f=f)
                agent = ROLES[k % len(ROLES)]
                k += 1
                with self.subTest(command=command, agent=agent, where=where):
                    self.assertRefused(self.bash(command, agent), *parts_of(command))

    def expect_tracked_runs(self, forms) -> None:
        """Each form on the tracked program, spelled in turn as each tracked spelling."""
        for i, (template, ext) in enumerate(forms):
            spellings = self.tracked_files(ext)
            command = template.format(f=spellings[i % len(spellings)])
            agent = ROLES[i % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

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
                stack.enter_context(mock.patch.dict(os.environ, dict(self.env, PWD=payload["cwd"]), clear=True))
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

    def hook_contract(self, command: str, cwd: Path | None = None):
        """tests_first.py on `command` for the coder, held to the Bash timeout hooks.json
        gives it: it exits 0 within that timeout, prints a well-formed denial or nothing,
        and at most one line on stderr, never a traceback."""
        limit = hook_timeouts("tests_first.py")["Bash"]
        payload = self.payload("Bash", {"command": command}, cwd or self.wt, "harness:coder")
        started = time.monotonic()
        try:
            r = subprocess.run(
                [sys.executable, str(HOOKS / "tests_first.py")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=dict(self.env, PWD=str(cwd or self.wt)),
                cwd=str(self.tmp),
                timeout=limit,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"tests_first.py did not finish {command!r} within its {limit} s timeout")
        took = time.monotonic() - started
        self.assertLess(took, limit, command)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertLessEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.decision(r)  # empty, or a well-formed denial


def whole(command: str) -> tuple:
    """The reason names the command itself."""
    return (command,)


def piped(command: str) -> tuple:
    """`a | b`: the reason names the writer, the reader or the pipeline."""
    left, right = command.split(" | ", 1)
    return (left, right, command)


def here_string(command: str) -> tuple:
    """`interpreter <<< "$(cat f)"`, or a heredoc whose body is `$(cat f)`: the reason names
    the command, its first line (a heredoc's part) or the substitution ($(...) or `...`)."""
    m = re.search(r"\$\((.*?)\)|`(.*?)`", command)
    inner = m.group(1) if m.group(1) is not None else m.group(2)
    return (command, command.split("\n", 1)[0], inner)


# An interpreter or shell reading its program from stdin, redirected from the program file
# ({f}; the file's extension).
REDIRECTS = (
    ("python3 - < {f}", "py"),
    ("python3 < {f}", "py"),
    ("python3 <{f}", "py"),
    ("python3 0< {f}", "py"),
    ("python3 -u - < {f}", "py"),
    ("python3 - --flag value < {f}", "py"),  # arguments to the program read from stdin
    ("< {f} python3 -", "py"),
    ("env python3 - < {f}", "py"),
    ("python3 - < {f} > /dev/null 2>&1", "py"),
    ("bash < {f}", "sh"),
    ("bash -s < {f}", "sh"),
    ("bash -s -- a b < {f}", "sh"),
    ("sh -s < {f}", "sh"),
    ("zsh < {f}", "sh"),
    ("timeout 60 bash -e < {f}", "sh"),
    ("bash < {f}", ""),
    ("node < {f}", "mjs"),
    ("node - < {f}", "cjs"),
    ("ruby < {f}", "rb"),
    ("perl < {f}", "pl"),
    ("perl - < {f}", "pl"),
    ("php < {f}", "php"),
    # an option that takes a value before stdin: the value is not a program file
    ("python3 -X utf8 - < {f}", "py"),
    ("python3 -W ignore < {f}", "py"),
    ("python3 -X dev -W error < {f}", "py"),
    ("bash -o errexit < {f}", "sh"),
    ("bash -o pipefail -s < {f}", ""),
    ("bash --rcfile /dev/null < {f}", "sh"),
    ("ruby -I lib < {f}", "rb"),
    ("ruby -r json < {f}", "rb"),
    ("perl -I lib - < {f}", "pl"),
    ("perl -Mstrict < {f}", "pl"),
    ("node --conditions development < {f}", "mjs"),
    ("php -d display_errors=1 < {f}", "php"),
)
# The same program piped in: cat writes it, the interpreter reads its program from stdin.
PIPES = (
    ("cat {f} | python3", "py"),
    ("cat {f} | python3 -", "py"),
    ("cat -- {f} | python3 -u -", "py"),
    ("cat scripts/tool.py {f} | python3", "py"),  # a tracked file first does not cover the second
    ("cat {f} scripts/tool.py | python3 -", "py"),  # nor one after it
    ("cat {f} | env python3 -", "py"),
    ("cat {f} | bash", "sh"),
    ("cat {f} | bash -s --", "sh"),
    ("cat {f} | bash -s -- a", "sh"),
    ("cat {f} | sh", "sh"),
    ("cat {f} | zsh", ""),
    ("cat {f} | node", "mjs"),
    ("cat {f} | ruby", "rb"),
    ("cat {f} | perl", "pl"),
    ("cat {f} | php", "php"),
    # any writer that copies the file to the pipe, not only cat
    ("head -n 999 {f} | python3", "py"),
    ("tail -n +1 {f} | python3 -", "py"),
    ("head -c 99999 {f} | node", "mjs"),
    ("sed -n p {f} | bash", "sh"),
    ("sed 's/x/y/' {f} | bash -s", ""),
    ("cat < {f} | sh", "sh"),
    ("tr -d '\\r' < {f} | bash", "sh"),
    ("awk 1 {f} | ruby", "rb"),
    ("cut -c1- {f} | perl", "pl"),
    ("grep -v '^#!' {f} | php", "php"),
    # and a reader with an option that takes a value
    ("cat {f} | python3 -X utf8", "py"),
    ("cat {f} | python3 -W ignore -", "py"),
    ("cat {f} | bash -o errexit -s", "sh"),
    ("head -n 999 {f} | ruby -I lib", "rb"),
)
# A here-string of the program file's contents.
HERE_STRINGS = (
    ('python3 - <<< "$(cat {f})"', "py"),
    ('python3 <<< "$(cat {f})"', "py"),
    ('python3 - <<<"$(cat {f})"', "py"),
    ('python3 - <<< "$(< {f})"', "py"),
    ("python3 - <<< $(cat {f})", "py"),
    ('bash <<< "$(cat {f})"', "sh"),
    ('bash -s <<< "$(<{f})"', "sh"),
    ('sh -s -- a <<< "$(cat {f})"', ""),
    ('node <<< "$(cat {f})"', "mjs"),
    ('ruby <<< "$(cat {f})"', "rb"),
    # other writers, backticks, and options that take a value
    ('python3 <<< "$(head -c 99999 {f})"', "py"),
    ('bash <<< "`cat {f}`"', "sh"),
    ("bash -s <<< `cat {f}`", ""),
    ('python3 - <<< "$(sed -n p {f})"', "py"),
    ('perl <<< "$(tail -n +1 {f})"', "pl"),
    ('python3 -W ignore <<< "$(cat {f})"', "py"),
    ('ruby -I lib <<< "$(< {f})"', "rb"),
)
# A heredoc (delimiter unquoted, so the shell expands it) whose body is the program file's
# contents: the reason names the command, its first line or the substitution.
HEREDOCS = (
    ("python3 - <<EOF\n$(cat {f})\nEOF", "py"),
    ("python3 <<PY\nimport sys\n$(cat {f})\nPY", "py"),
    ("bash <<EOF\n$(cat {f})\nEOF", "sh"),
    ("bash -s <<END\n`cat {f}`\nEND", ""),
    ("node <<JS\n$(head -c 99999 {f})\nJS", "mjs"),
    ("ruby <<RB\n$(< {f})\nRB", "rb"),
    ("python3 -X utf8 - <<EOF\n$(cat {f})\nEOF", "py"),
    ("perl - <<PL\n$(sed -n p {f})\nPL", "pl"),
    ("php <<PHP\n$(cat {f})\nPHP", "php"),
)
# An inline program on stdin that writes, behind an option that takes a value: the existing
# rule for inline programs that write (#321 and before) must see the heredoc there too
# (command, the part the reason names).
HEREDOC_WRITES = (
    (heredoc("open('src/app.py', 'w').write('')", "python3 -W ignore"), "python3 -W ignore <<'PY'"),
    (heredoc("open('tests/test_app.py', 'w').write('')", "python3 -X utf8 -"), "python3 -X utf8 - <<'PY'"),
    (
        heredoc("import pathlib; pathlib.Path('src/app.py').write_text('')", "python3 -X dev -W error"),
        "python3 -X dev -W error <<'PY'",
    ),
    (
        heredoc("require('fs').writeFileSync('src/app.py', '')", "node --conditions development", "JS"),
        "node --conditions development <<'JS'",
    ),
)
# The same forms in a later part, or in a directory the command moves to (command, the parts
# the reason may name; {o} the temp directory).
LATER_PARTS = (
    ("cd tools && python3 - < gen.py", ("python3 - < gen.py",)),
    ("true && bash -s < tools/gen.sh", ("bash -s < tools/gen.sh",)),
    ("cd {o} && bash < w.sh", ("bash < w.sh",)),
    ("cd ~ && python3 - < w.py", ("python3 - < w.py",)),
    ("echo start; node < tools/gen.mjs", ("node < tools/gen.mjs",)),
    ("(cd tools; cat gen.py | python3)", ("cat gen.py", "python3", "cat gen.py | python3")),
    ("cd tests/helpers && cat gen.sh | bash -s", ("cat gen.sh", "bash -s", "cat gen.sh | bash -s")),
    ('cd {o} && python3 - <<< "$(cat w.py)"', ('python3 - <<< "$(cat w.py)"', "cat w.py")),
)
# A process substitution read on stdin first (< <(...)) does not hide the program file the
# next command runs ({f}; the file's extension), whatever separates them (#364 review).
AFTER_INPUT_SUBSTITUTION = (
    ("sort < <(ls) && bash -s < {f}", "sh"),
    ("while read f; do :; done < <(git ls-files); python3 - < {f}", "py"),
    ("mapfile -t a < <(ls)\ncat {f} | python3", "py"),
    ("exec 3< <(ls) || source {f}", "sh"),
    ("cat <<< <(ls); {f} a", ""),
    ("sort 0< <(git ls-files) && ruby < {f}", "rb"),
    ('sort < <(ls); node <<< "$(cat {f})"', "mjs"),
    ("while read f; do echo $f; done < <(ls)\nperl - < {f}", "pl"),
    ("sort < <(ls) && php < {f}", "php"),
)


def after_input_substitution(command: str) -> tuple:
    """The parts the reason may name: those of the command after the `< <(...)` prefix."""
    rest = re.split(r"\)(?:; |\n| && | \|\| )", command, maxsplit=1)[1]
    if " | " in rest:
        return piped(rest)
    return here_string(rest) if "$(" in rest else (rest,)


# The definition of done's commands, verbatim: the hook judges the command, so the files need
# not exist (parts the reason may name).
LITERAL = (
    ("python3 - < /tmp/w.py", ("python3 - < /tmp/w.py",)),
    ("python3 < /tmp/w.py", ("python3 < /tmp/w.py",)),
    ("bash < /tmp/w.sh", ("bash < /tmp/w.sh",)),
    ("bash -s < /tmp/w.sh", ("bash -s < /tmp/w.sh",)),
    ("cat /tmp/w.py | python3", ("cat /tmp/w.py", "python3", "cat /tmp/w.py | python3")),
    ('python3 - <<< "$(cat /tmp/w.py)"', ('python3 - <<< "$(cat /tmp/w.py)"', "cat /tmp/w.py")),
)
# Data on stdin, and pipes whose reader runs no program from stdin: they run whatever the
# file is ({o} the temp directory).
DATA = (
    "cat tools/gen.py | grep import",
    "cat {o}/w.py | wc -l",
    "cat tests/helpers/gen.sh | sort",
    "git diff | head -n 20",
    "git log --oneline | python3 scripts/tool.py",
    "cat tools/gen.py | python3 scripts/tool.py",
    "python3 scripts/tool.py < tools/gen.py",
    "python3 scripts/tool.py - < {o}/w.py",
    "cat tests/fixtures/data.json | python3 -m json.tool",
    "python3 -m json.tool < {o}/data.json",
    "python3 -c 'import sys; print(len(sys.stdin.read()))' < {o}/w.py",
    "bash scripts/tool.sh < {o}/input.txt",
    "bash -c 'wc -l' < tools/gen.sh",
    "node scripts/tool.mjs < {o}/w.mjs",
    "sort < tools/gen.py",
    "grep -c fixture < {o}/w.sh",
    "wc -l < build/gen.py",
    "python3 - <<< 'print(1)'",
    "head -n 5 tools/gen.py | grep import",
    "sed -n 1p {o}/w.sh | wc -c",
    "head -c 100 {o}/w.py | python3 scripts/tool.py",
    "python3 -W ignore scripts/tool.py < {o}/w.py",
    "python3 -X utf8 -m json.tool < tests/fixtures/data.json",
    "bash -o errexit scripts/tool.sh < tools/gen.sh",
    "ruby -I lib scripts/tool.rb < tools/gen.rb",
    "cat {o}/input.txt | bash -o errexit -c 'wc -l'",
    "python3 - <<EOF\nprint(1)\nEOF",
    # a quoted delimiter: the body is literal text, nothing is substituted
    "python3 - <<'PY'\nprint('$(cat tools/gen.py)')\nPY",
)


class StdinProgramTests(ProgramFormCase):
    # ---- refused on the ticket branch

    def test_a_redirected_program_file_is_refused_to_each_role(self):
        self.expect_rotated(REDIRECTS, whole)

    def test_a_piped_program_file_is_refused_to_each_role(self):
        self.expect_rotated(PIPES, piped)

    def test_a_here_string_of_a_program_file_is_refused_to_each_role(self):
        self.expect_rotated(HERE_STRINGS, here_string)

    def test_a_heredoc_of_a_program_file_is_refused_to_each_role(self):
        self.expect_rotated(HEREDOCS, here_string)

    def test_an_inline_program_that_writes_on_stdin_behind_an_option_value_is_refused(self):
        """python3 -W ignore <<'PY' reads its program from the heredoc as python3 <<'PY'
        does: the value of -W, -X or --conditions is not a program file."""
        self.expect_refused([(c, (p,)) for c, p in HEREDOC_WRITES])

    def test_each_place_a_program_file_is_not_tracked_is_refused(self):
        """Outside the worktree, in another repository, new, ignored, a new test file and
        only staged: none is a blob at the worktree's HEAD."""
        self.expect_everywhere(
            (
                ("python3 - < {f}", "py"),
                ("cat {f} | bash -s", "sh"),
                ('node <<< "$(cat {f})"', "mjs"),
                ("sed -n p {f} | bash", ""),
                ("python3 -X utf8 - < {f}", "py"),
                ("ruby <<RB\n`cat {f}`\nRB", "rb"),
            ),
            lambda c: piped(c) if " | " in c else (here_string(c) if "<<" in c else whole(c)),
        )

    def test_the_definition_of_done_commands_are_refused_to_each_role(self):
        self.expect_refused(LITERAL)

    def test_a_process_substitution_on_stdin_does_not_hide_the_next_command(self):
        """sort < <(ls) && bash -s < f runs f: the input redirect of the first command ends
        with it. The tracked file after the same prefix still runs."""
        self.expect_rotated(AFTER_INPUT_SUBSTITUTION, after_input_substitution)
        self.expect_tracked_runs(AFTER_INPUT_SUBSTITUTION)
        self.expect_allowed(["while read f; do echo $f; done < <(git ls-files)", "sort < <(ls) | head -n 3"])

    def test_a_later_part_and_another_directory_are_judged_where_they_run(self):
        self.expect_refused([(self.fill(c), tuple(self.fill(p) for p in parts)) for c, parts in LATER_PARTS])

    def test_the_breaker_scratch_worktree_follows_the_same_rule(self):
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        o = self.outside
        for command, parts in (
            (f"python3 - < {o}/w.py", (f"python3 - < {o}/w.py",)),
            (f"cat {o}/w.sh | bash", piped(f"cat {o}/w.sh | bash")),
            (f'bash <<< "$(cat {o}/w.sh)"', here_string(f'bash <<< "$(cat {o}/w.sh)"')),
        ):
            with self.subTest(command=command, where="scratch"):
                self.assertRefused(self.bash(command, breaker, cwd=scratch), *parts)
        for command in ("python3 - < scripts/tool.py", "cat scripts/tool.sh | bash"):
            with self.subTest(command=command, where="scratch"):
                self.assertIsNone(self.bash(command, breaker, cwd=scratch))

    # ---- still allowed

    def test_the_same_forms_on_a_tracked_program_file_still_run(self):
        self.expect_tracked_runs(REDIRECTS + PIPES + HERE_STRINGS + HEREDOCS)

    def test_data_on_stdin_and_pipes_into_no_interpreter_still_run(self):
        self.expect_allowed([self.fill(c) for c in DATA])

    def test_the_main_session_and_other_agents_are_left_alone(self):
        o = self.outside
        commands = [c for c, _ in LITERAL] + [
            f"python3 - < {o}/w.py",
            "bash -s < tools/gen.sh",
            f"cat {o}/w.rb | ruby",
            'python3 - <<< "$(cat tools/gen.py)"',
        ]
        for command in commands:
            for agent in (None, "harness:reviewer"):
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))

    def test_off_a_ticket_branch_nothing_changes(self):
        o = self.outside
        commands = [c for c, _ in LITERAL] + [
            f"python3 - < {o}/w.py",
            f"bash -s -- a < {o}/w.sh",
            f"cat {o}/w.py | python3",
            f"cat {o}/w.pl | perl",
            f'bash <<< "$(<{o}/w.sh)"',
            "python3 < ~/w.py",
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
        for k, command in enumerate(commands + ["python3 - < tools/gen.py", "cat tools/gen.sh | bash"]):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent, where="feature/login"):
                self.assertIsNone(self.bash(command, agent))


if __name__ == "__main__":
    unittest.main()
