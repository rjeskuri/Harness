"""tests_first.py: a program file sourced (source f, . f) or run directly by its path, a
shebang script (/tmp/w.py, ./w.sh, ~/w.sh) (#364).

For the coder, the test-author and the test-breaker on a ticket branch, the file a shell
sources and the script a command runs by its path are judged as #321 judges a program file
an interpreter runs: tracked at the worktree's HEAD runs; untracked (new, ignored, a new
test file, only staged) or outside the worktree (the temp directory, ~, another repository)
is refused, from any directory and in any part of the command, and the refusal names the
part and says to write with Edit or Write. A file without a shebang that the shell would
run as a script counts, and so does one made executable earlier in the same command. What
the loop runs still runs: sh scripts/smoke.sh, source .venv/bin/activate (a virtual
environment's own script), the venv's python, binaries by their path (/usr/bin/git,
/bin/ls) and the plugin's own tools by theirs. Off a ticket branch, for the main session
and for other agents nothing changes. The hook still fails open on its own errors and
keeps its deadline: a FIFO, an unreadable file, /dev/zero or a cut-off command never hangs
it or makes it crash.

The fixture is ProgramFormCase, in test_guards_script_files_stdin.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_script_files*.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403
from test_guards_script_files_stdin import ROLES, ProgramFormCase, whole

# A shell sourcing the program file ({f}; the file's extension, "" for none).
SOURCES = (
    ("source {f}", "sh"),
    ("source {f} a b", "sh"),
    (". {f}", "sh"),
    (". {f} --flag", ""),
    ("source {f}", ""),
    ("builtin source {f}", "sh"),
    ("builtin . {f}", "sh"),
    ("command . {f}", ""),
    ("source {f} > /dev/null", "sh"),
    (". {f} 2>&1", "sh"),
)
# A script run by its path, whatever its extension or wrapper; the part is the command.
DIRECT = (
    ("{f}", "py"),
    ("{f}", "sh"),
    ("{f}", ""),
    ("{f} a b", "rb"),
    ("{f} --flag=1 > /dev/null", "mjs"),
    ("timeout 60 {f}", "pl"),
    ("FOO=1 {f}", "php"),
    ("env FOO=1 {f}", "py"),
    ("nohup {f}", "sh"),
    ("exec {f}", ""),
    ("command {f} -v", "cjs"),
)
# In a later part, or from a directory the command moves to (command, the part the reason
# names; {o} is the temp directory).
LATER_PARTS = (
    ("cd tools && source gen.sh", "source gen.sh"),
    ("cd tools && . ./gen.sh", ". ./gen.sh"),
    ("cd {o} && . ./w.sh", ". ./w.sh"),
    ("true && source ~/w.sh", "source ~/w.sh"),
    ("cd tests && source helpers/gen.sh", "source helpers/gen.sh"),
    ("source .venv/bin/activate && source tools/gen.sh", "source tools/gen.sh"),
    ("source scripts/tool.sh; . build/gen.sh", ". build/gen.sh"),
    ("cd tools && ./gen.sh", "./gen.sh"),
    ("cd tools && ./gen", "./gen"),
    ("(cd tools; ./gen.py a)", "./gen.py a"),
    ("cd {o} && ./w.sh", "./w.sh"),
    ("cd ~ && ./w.rb", "./w.rb"),
    ("cd scripts && ../tools/gen.sh", "../tools/gen.sh"),
    ("cd tests && helpers/gen.py", "helpers/gen.py"),
    ("true && {o}/w.py", "{o}/w.py"),
    ("echo start; ~/w.sh", "~/w.sh"),
    ("python3 scripts/tool.py && ./tools/gen.sh", "./tools/gen.sh"),
    ("./scripts/tool.sh | ./tools/gen.py", "./tools/gen.py"),
    # executable only once an earlier part has made it so
    ("chmod +x {o}/plain.sh && {o}/plain.sh", "{o}/plain.sh"),
    ("chmod +x build/plain.sh && ./build/plain.sh", "./build/plain.sh"),
    # no shebang: the shell runs a text file it can execute as a script
    ("./tools/noshebang", "./tools/noshebang"),
    ("{o}/noshebang", "{o}/noshebang"),
)
# A sourced file whose path cannot be known (command, the part).
UNKNOWN = (
    ("source $F", "source $F"),
    ('. "$SCRIPT"', '. "$SCRIPT"'),
    ("F=/tmp/w.sh; source $F", "source $F"),
    ('source "$(mktemp)"', 'source "$(mktemp)"'),
)
# The definition of done's commands ({o} is the temp directory: a shebang script is judged
# by its contents, so it exists there; a sourced file need not exist).
LITERAL = (
    "source /tmp/w.sh",
    ". /tmp/w.sh",
    "source ~/w.sh",
    "{o}/w.py",
    "./w.sh",
    "~/w.sh",
)
# Tracked twins of the commands above, spelled as the loop would.
TRACKED = (
    "cd scripts && ./tool.sh",
    "cd scripts && . ./tool.sh",
    "cd tests && ../scripts/tool.py",
    "cd {o} && {wt}/scripts/tool",
    "source scripts/tool.sh && ./scripts/tool.py",
    "timeout 60 ./scripts/tool.sh",
    "FOO=1 ./scripts/tool.rb",
)
# What the loop runs: the project's scripts, the virtual environment, binaries by their path,
# the plugin's own tools by theirs ({red} and {bin}).
LOOP = (
    "sh scripts/smoke.sh",
    "bash scripts/smoke.sh",
    "./scripts/smoke.sh",
    "source .venv/bin/activate",
    ". .venv/bin/activate",
    "source .venv/bin/activate && python3 -m pytest tests/test_new.py",
    "cd tests && source ../.venv/bin/activate",
    "source {wt}/.venv/bin/activate",
    ".venv/bin/python -m pytest tests/test_new.py",
    ".venv/bin/python scripts/tool.py",
    "/usr/bin/git status",
    "/bin/ls tests",
    "/usr/bin/env bash scripts/tool.sh",
    "/usr/bin/python3 scripts/tool.py",
    "{red} run --worktree {wt}",
    "{bin}/harness-board show 12 --markdown",
    "git status && ls tools",
)


class SourceAndShebangTests(ProgramFormCase):
    def setUp(self) -> None:
        super().setUp()
        # a virtual environment, ignored, as python3 -m venv leaves it: its python a link
        # to the interpreter, its activate script for the shell
        venv = self.wt / ".venv"
        self.write(venv / "pyvenv.cfg", f"home = {Path(sys.executable).parent}\nversion = 3.12.0\n")
        self.write(venv / "bin/activate", 'VIRTUAL_ENV="$(pwd)/.venv"\nexport VIRTUAL_ENV\n')
        os.symlink(os.path.realpath(sys.executable), venv / "bin/python")
        # untracked: at the worktree's top, without a shebang, not executable yet
        self.program(self.wt / "w.sh", "sh")
        for path in (self.wt / "tools/noshebang", self.outside / "noshebang"):
            self.write(path, ": fixture\n")
            os.chmod(path, 0o755)
        for path in (self.outside / "plain.sh", self.wt / "build/plain.sh"):
            self.program(path, "sh", mode=0o644)

    # ---- refused on the ticket branch

    def test_a_sourced_program_file_is_refused_to_each_role(self):
        self.expect_rotated(SOURCES, whole)

    def test_a_script_run_by_its_path_is_refused_to_each_role(self):
        self.expect_rotated(DIRECT, whole)

    def test_each_place_a_program_file_is_not_tracked_is_refused(self):
        """Outside the worktree, in another repository, new, ignored, a new test file and
        only staged: none is a blob at the worktree's HEAD."""
        self.expect_everywhere((("source {f}", "sh"), ("{f}", "py"), ("{f} a", "")), whole)

    def test_the_definition_of_done_commands_are_refused_to_each_role(self):
        self.expect_refused([(self.fill(c), (self.fill(c),)) for c in LITERAL])

    def test_a_later_part_and_another_directory_are_judged_where_they_run(self):
        self.expect_refused([(self.fill(c), (self.fill(p),)) for c, p in LATER_PARTS])

    def test_a_sourced_file_whose_path_is_not_known_is_refused(self):
        self.expect_refused([(c, (p,)) for c, p in UNKNOWN])

    def test_the_breaker_scratch_worktree_follows_the_same_rule(self):
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        o = self.outside
        for command in (f"source {o}/w.sh", f"{o}/w.py", "~/w.sh a"):
            with self.subTest(command=command, where="scratch"):
                self.assertRefused(self.bash(command, breaker, cwd=scratch), command)
        for command in ("source scripts/tool.sh", "./scripts/tool.py", "sh scripts/smoke.sh"):
            with self.subTest(command=command, where="scratch"):
                self.assertIsNone(self.bash(command, breaker, cwd=scratch))

    # ---- still allowed

    def test_the_same_forms_on_a_tracked_program_file_still_run(self):
        self.expect_tracked_runs(SOURCES + DIRECT)
        self.expect_allowed([self.fill(c) for c in TRACKED])

    def test_what_the_loop_runs_still_runs(self):
        self.expect_allowed([self.fill(c) for c in LOOP])

    def test_the_main_session_and_other_agents_are_left_alone(self):
        commands = [self.fill(c) for c in LITERAL] + [
            "source tools/gen.sh",
            "./tools/gen.py",
            self.fill(". {elsewhere}/w.sh"),
            "cd tools && ./gen",
        ]
        for command in commands:
            for agent in (None, "harness:reviewer"):
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))

    def test_off_a_ticket_branch_nothing_changes(self):
        o = self.outside
        commands = ["source /tmp/w.sh", ". /tmp/w.sh"] + [
            f"source {o}/w.sh",
            f". {o}/w a",
            f"{o}/w.py",
            f"{o}/w.rb a b",
            "~/w.sh",
            f"cd {o} && ./w.sh",
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
        for k, command in enumerate(commands + ["source tools/gen.sh", "./tools/gen.py", "./w.sh"]):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent, where="feature/login"):
                self.assertIsNone(self.bash(command, agent))

    # ---- fails open, keeps its deadline

    def test_files_that_cannot_be_read_keep_the_hook_contract(self):
        """A FIFO no one writes, a file no one may read, an endless device, a file with no
        line end and a directory, sourced, run by their path or fed on stdin: the hook
        exits 0 in time with a well-formed answer, never hanging on the read or crashing."""
        o = self.outside
        for fifo in (self.wt / "tools/fifo", o / "fifo"):
            os.mkfifo(fifo)
        for path, ext in ((self.wt / "tools/locked.sh", "sh"), (self.wt / "tools/locked.py", "py"), (o / "locked.py", "py")):
            self.program(path, ext, mode=0o000)
        long_line = o / "long"
        long_line.write_bytes(b"#!" + b"a" * (8 << 20))
        os.chmod(long_line, 0o755)
        for command in (
            "./tools/fifo",
            f"{o}/fifo a",
            "source tools/fifo",
            f". {o}/fifo",
            "python3 - < tools/fifo",
            f"cat {o}/fifo | python3",
            'bash <<< "$(cat tools/fifo)"',
            "python3 -c 'import tools.fifo'",
            "./tools/locked.sh",
            f"{o}/locked.py",
            "source tools/locked.sh",
            "python3 -c 'import tools.locked'",
            f"python3 -c \"exec(open('{o}/locked.py').read())\"",
            f"{o}/long",
            f"source {o}/long",
            "/dev/zero",
            "/dev/urandom",
            "source /dev/zero",
            ". /dev/urandom",
            "python3 - < /dev/zero",
            "cat /dev/zero | bash",
            "./tools",
            f"{o}",
            "/",
            "./",
        ):
            with self.subTest(command=command):
                self.hook_contract(command)

    def test_cut_off_commands_keep_the_hook_contract(self):
        for command in (
            "source",
            ".",
            "source ''",
            ". ''",
            "source -",
            "source --",
            "builtin source",
            "python3 - <",
            "python3 <",
            "bash -s <",
            "< tools/gen.py",
            "python3 - <<<",
            'python3 - <<< "$(<)"',
            'python3 - <<< "$(cat)"',
            "python3 - <<< $(",
            "| python3",
            "cat |",
            "cat tools/gen.py |",
            "cat | python3",
            "cat tools/gen.py | |",
            "./tools/gen.sh |",
            "source tools/gen.sh |",
            "~/",
            "~",
            "../",
            "tools/",
            "$(cat tools/gen.sh)",
            "python3 -c 'import'",
            "python3 -c 'from . import x'",
            "python3 -c 'import ...'",
            "python3 -c 'from tools import'",
            """python3 -c "exec(open(\"""",
            "python3 -c 'exec(open())'",
            "python3 -c 'exec(open(\"\").read())'",
            "python3 -c 'import runpy; runpy.run_path()'",
            "node -e 'require('",
            "node -e 'import()'",
            "node -e 'require(\"\")'",
            "node --input-type=module -e 'import'",
            "ruby -e 'require'",
            "ruby -e 'require_relative \"\"'",
            "perl -e 'do'",
            "perl -e 'require \"\"'",
            "php -r 'include'",
            "php -r 'require_once \"\";'",
        ):
            with self.subTest(command=command):
                self.hook_contract(command)


if __name__ == "__main__":
    unittest.main()
