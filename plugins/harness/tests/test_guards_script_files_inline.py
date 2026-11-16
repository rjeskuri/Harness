"""tests_first.py: an inline program that loads a program file or module, and an untracked
program under a test runner (#364).

python3 -c 'import tools.gen', exec(open(f).read()), runpy.run_path(f), node -e
'require(f)' or 'import(f)', ruby -e 'require f' or 'load f', perl -e 'do f' or
'require f', php -r 'include f;' (require, include_once, require_once) run the file they
load, so for the coder, the test-author and the test-breaker on a ticket branch it is judged
as #321 judges a program file: a module or file tracked at the worktree's HEAD runs; one
untracked (new, ignored, a new test file, only staged) or outside the worktree (the temp
directory, ~, another repository, found through PYTHONPATH or the directory the part runs
in) is refused, as is a path the hook cannot know, and the refusal names the part and says
to write with Edit or Write. The standard library, installed modules and inline programs
that load nothing still run.

Ruling (#364, outline 3): under a test runner (unittest, pytest, node --test, bun test,
deno test, vitest, jest) an in-tree untracked file that the project's test patterns
(HARNESS_TEST_PATTERNS, or the defaults) call a test, or that is test configuration, still
runs: the test-author's new tests, test setup and runner configs. An untracked file that is
neither (python3 -m unittest tools/gen.py, node --import ./tools/gen.mjs --test tests/) is
refused like any other untracked program, as python3 -m doctest tools/gen.py already is.

The hook still keeps its deadline on these forms: with slow or hanging git it decides in
time and never lets an untracked program through because git was slow.

The fixture is ProgramFormCase, in test_guards_script_files_stdin.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_script_files*.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403
from test_guards_script_files_stdin import ROLES, ProgramFormCase, piped

# Inline programs loading a program file or module that is not tracked at the worktree's
# HEAD: (command, the part the reason names or None for the whole command). {o} is the temp
# directory, {home} ~, {elsewhere} another repository, {wt} the worktree.
PYTHON = (
    ("python3 -c 'import tools.gen'", None),
    ("python3 -c 'import tests.helpers.gen'", None),
    ("python3 -c 'import build.gen'", None),
    ("python3 -c 'import tools.staged'", None),
    ("python3 -c 'import w'", None),
    ("python3 -c 'from tools import gen'", None),
    ("python3 -c 'from tools.gen import main; main()'", None),
    ("python3 -c 'import os, tools.gen'", None),
    ("python3 -c 'import sys; import tools.gen as g'", None),
    ("python3 -u -c 'import tools.gen'", None),
    ("python3 -W ignore -c 'import tools.gen'", None),
    ('python3 -c"import tools.gen"', None),
    ("env python3 -c 'import tools.gen'", None),
    ("PYTHONPATH={o} python3 -c 'import w'", None),
    ("PYTHONPATH=tools python3 -c 'import gen'", None),
    ("PYTHONPATH={elsewhere} python3 -c 'import w'", None),
    ("export PYTHONPATH={o}; python3 -c 'import w'", "python3 -c 'import w'"),
    ("cd tools && python3 -c 'import gen'", "python3 -c 'import gen'"),
    ("cd {o} && python3 -c 'import w'", "python3 -c 'import w'"),
    ("cd ~ && python3 -c 'from w import x'", "python3 -c 'from w import x'"),
    ("python3 -c \"exec(open('{o}/w.py').read())\"", None),
    ("python3 -c 'exec(open(\"tools/gen.py\").read())'", None),
    ("python3 -c \"exec(open('{home}/w.py').read())\"", None),
    ("python3 -c \"exec(open('{elsewhere}/w.py').read())\"", None),
    ("python3 -c \"exec(open('build/gen.py').read())\"", None),
    ("python3 -c \"exec(open('./tests/helpers/gen.py').read())\"", None),
    ("python3 -c \"exec(open('{wt}/tools/staged.py').read())\"", None),
    ("python3 -c \"import runpy; runpy.run_path('{o}/w.py')\"", None),
    ("python3 -c \"import runpy; runpy.run_path('tools/gen.py')\"", None),
    ("python3 -c \"import runpy; runpy.run_module('tools.gen')\"", None),
    # a path the hook cannot know
    ("python3 -c \"p = '{o}/w.py'; exec(open(p).read())\"", None),
)
NODE = (
    ("node -e 'import(\"{o}/w.mjs\")'", None),
    ("node -e 'require(\"./tools/gen.cjs\")'", None),
    ("node -e \"require('{o}/w.cjs')\"", None),
    ("node -e \"require('{home}/w.cjs')\"", None),
    ("node -e 'require(\"{elsewhere}/w.cjs\")'", None),
    ("node -e 'require(\"{wt}/tools/gen.cjs\")'", None),
    ("node -e 'require(\"./build/gen.cjs\")'", None),
    ("node -e 'require(\"./tools/staged.cjs\")'", None),
    ("node -p 'require(\"./tools/gen.cjs\")'", None),
    ("node --eval 'require(\"./tests/helpers/gen.cjs\")'", None),
    ("node --eval=\"import('./tools/gen.mjs')\"", None),
    ("node --print \"import('{o}/w.mjs')\"", None),
    ("node --input-type=module -e 'import \"./tools/gen.mjs\"'", None),
    ("node --input-type=module -e 'import {{ x }} from \"{o}/w.mjs\"'", None),
    ("cd tools && node -e 'require(\"./gen.cjs\")'", "node -e 'require(\"./gen.cjs\")'"),
)
RUBY = (
    ("ruby -e 'require \"{o}/w\"'", None),
    ("ruby -e 'load \"{o}/w.rb\"'", None),
    ("ruby -e \"require './tools/gen'\"", None),
    ("ruby -e 'require_relative \"tools/gen\"'", None),
    ("ruby -e 'load \"tools/gen.rb\"'", None),
    ("ruby -e 'require \"{home}/w.rb\"'", None),
    ("ruby -w -e 'require \"./tests/helpers/gen\"'", None),
    ("ruby -e 'require \"{elsewhere}/w\"'", None),
    ("ruby -e 'load \"./build/gen.rb\"'", None),
    ("cd tools && ruby -e 'load \"gen.rb\"'", "ruby -e 'load \"gen.rb\"'"),
)
PERL = (
    ("perl -e 'do \"{o}/w.pl\"'", None),
    ("perl -e 'require \"./tools/gen.pl\"'", None),
    ("perl -e 'do \"./tools/gen.pl\"; print 1'", None),
    ("perl -e 'do \"{home}/w.pl\"'", None),
    ("perl -w -e 'require \"./build/gen.pl\"'", None),
    ("perl -e 'do \"{elsewhere}/w.pl\"'", None),
    ("perl -e 'require \"./tools/staged.pl\"'", None),
    ("perl -e 'do \"./tests/helpers/gen.pl\"'", None),
)
PHP = (
    ("php -r 'include \"{o}/w.php\";'", None),
    ("php -r 'require \"tools/gen.php\";'", None),
    ("php -r \"require_once('tools/gen.php');\"", None),
    ("php -r 'include_once \"{home}/w.php\";'", None),
    ("php -r 'require \"./tests/helpers/gen.php\";'", None),
    ("php -r 'include(\"{elsewhere}/w.php\");'", None),
    ("php -r 'include \"build/gen.php\";'", None),
    ("php -r 'require \"{wt}/tools/staged.php\";'", None),
)
# A loader whose path the inline program takes from outside its own text, an argument or
# the environment: the hook cannot know what it loads ({f} the program file; its
# extension). Each runs on three places, one per role.
UNKNOWN = (
    ("python3 -c 'import sys; exec(open(sys.argv[1]).read())' {f}", "py"),
    ("F={f} python3 -c 'import os; exec(open(os.environ[\"F\"]).read())'", "py"),
    ("python3 -c 'import runpy, sys; runpy.run_path(sys.argv[1])' {f}", "py"),
    ("python3 -c 'import sys; exec(compile(open(sys.argv[1]).read(), \"w\", \"exec\"))' {f}", "py"),
    ("node -e 'require(process.argv[1])' {f}", "cjs"),
    ("F={f} node -e 'import(process.env.F)'", "mjs"),
    ("ruby -e 'load ARGV[0]' {f}", "rb"),
    ("perl -e 'do $ARGV[0]' {f}", "pl"),
    ("php -r 'include $argv[1];' {f}", "php"),
)
# Dynamic imports in python: importlib and __import__, by name.
DYNAMIC = (
    ("python3 -c 'import importlib; importlib.import_module(\"tools.gen\")'", None),
    ("python3 -c \"__import__('tools.gen')\"", None),
    ("python3 -c 'from importlib import import_module; import_module(\"tests.helpers.gen\")'", None),
    ("python3 -c 'import importlib; importlib.import_module(\"w\")'", None),
    ("python3 -c \"g = __import__('build.gen')\"", None),
    ("python3 -c 'import importlib; importlib.import_module(\"tools.staged\")'", None),
    ("PYTHONPATH={o} python3 -c \"__import__('w')\"", None),
    ("cd {o} && python3 -c \"import importlib; importlib.import_module('w')\"", "python3 -c \"import importlib; importlib.import_module('w')\""),
    ("cd ~ && python3 -c \"__import__('w')\"", "python3 -c \"__import__('w')\""),
    # a module name the hook cannot know
    ("python3 -c 'import importlib, sys; importlib.import_module(sys.argv[1])' tools.gen", None),
)
# The outline's commands, verbatim: the hook judges the command, so the files outside the
# worktree need not exist.
LITERAL = (
    "python3 -c 'import tools.gen'",
    "python3 -c 'exec(open(\"/tmp/w.py\").read())'",
    "node -e 'import(\"/tmp/w.mjs\")'",
    "node -e 'require(\"./tools/gen.cjs\")'",
    "ruby -e 'require \"/tmp/w\"'",
    "perl -e 'do \"/tmp/w.pl\"'",
    "php -r 'include \"/tmp/w.php\";'",
)
# The same loaders on tracked files, and inline programs that load nothing the worktree does
# not track: the standard library, installed modules, built-ins.
INLINE_ALLOWED = (
    "python3 -c 'import scripts.tool'",
    "python3 -c 'from scripts import tool'",
    "python3 -c 'from scripts.tool import x'",
    "python3 -c 'exec(open(\"scripts/tool.py\").read())'",
    "python3 -c \"import runpy; runpy.run_path('scripts/tool.py')\"",
    "python3 -c \"import runpy; runpy.run_module('scripts.tool')\"",
    "cd scripts && python3 -c 'import tool'",
    "PYTHONPATH=scripts python3 -c 'import tool'",
    "python3 -c 'import json, sys; print(json.dumps(sys.argv))'",
    "python3 -c 'from pathlib import Path; print(Path.cwd())'",
    "python3 -c 'import yaml'",
    "python3 -c 'print(1)'",
    "node -e 'require(\"./scripts/tool.cjs\")'",
    "node -e 'import(\"./scripts/tool.mjs\")'",
    "node -e 'require(\"{wt}/scripts/tool.cjs\")'",
    "node --input-type=module -e 'import \"./scripts/tool.mjs\"'",
    "node -p 'require(\"node:path\").sep'",
    "node -e 'console.log(require(\"os\").platform())'",
    "ruby -e 'require \"./scripts/tool\"'",
    "ruby -e 'require_relative \"scripts/tool\"'",
    "ruby -e 'load \"scripts/tool.rb\"'",
    "ruby -e 'require \"json\"; puts 1'",
    "perl -e 'do \"./scripts/tool.pl\"'",
    "perl -e 'require \"./scripts/tool.pl\"'",
    "perl -MPOSIX -e 'print 1'",
    "php -r 'include \"scripts/tool.php\";'",
    "php -r 'require_once \"./scripts/tool.php\";'",
    "php -r 'echo 1;'",
    # dynamic imports of tracked or installed modules
    "python3 -c 'import importlib; importlib.import_module(\"scripts.tool\")'",
    "python3 -c \"__import__('scripts.tool')\"",
    "python3 -c \"__import__('json')\"",
    "python3 -c 'import importlib; print(importlib.import_module(\"json\").dumps(1))'",
    # arguments and the environment read, nothing loaded from them
    "python3 -c 'import sys; print(sys.argv[1])' tools/gen.py",
    "F={o}/w.py python3 -c 'import os; print(os.environ[\"F\"])'",
    "node -e 'console.log(process.argv[1])' tools/gen.cjs",
    # data read, not run: a file outside the worktree, or a program file read as text
    "python3 -c 'print(open(\"/tmp/x.txt\").read())'",
    "python3 -c 'print(open(\"{o}/input.txt\").read())'",
    "python3 -c 'import json; print(json.load(open(\"tests/fixtures/data.json\")))'",
    "python3 -c 'import sys; print(open(sys.argv[1]).read())' {o}/w.py",
    "node -e 'console.log(require(\"fs\").readFileSync(\"/tmp/x.json\",\"utf8\"))'",
    "node -e 'console.log(require(\"fs\").readFileSync(\"{o}/data.json\", \"utf8\"))'",
    "ruby -e 'puts File.read(\"{o}/input.txt\")'",
    "perl -ne 'print' {o}/input.txt",
    "php -r 'echo file_get_contents(\"{o}/input.txt\");'",
)
# A test runner given an untracked file that is neither a test nor test configuration.
RUNNER_REFUSED = (
    "python3 -m unittest tools/gen.py",
    "python3 -m unittest tools.gen",
    "python3 -m unittest w",
    "python3 -m unittest tools/staged.py",
    "python3 -m pytest tools/gen.py",
    "python3 -m pytest -q build/gen.py",
    "pytest tools/gen.py",
    "uv run pytest tools/gen.py",
    "node --test tools/gen.mjs",
    "node --import ./tools/gen.mjs --test tests/",
    "node --import=./tools/gen.mjs --test tests/new.test.js",
    "node -r ./tools/gen.cjs --test",
    "bun test tools/gen.ts",
    "bun test --preload ./tools/gen.ts tests/",
    "deno test tools/gen.ts",
    "npx vitest run tools/gen.ts",
    "npx vitest run --config tools/gen.mjs tests/",
    "npx jest -c tools/gen.cjs tests/",
)
# The test-author's new tests, test setup and runner configs by the default test patterns
# (tests/**, **/test_*.py, **/*_test.py, **/*.test.js, **/*.test.ts) and the test
# configuration (vitest.config.*, jest.config.*), and tracked files: they run.
RUNNER_ALLOWED = (
    "python3 -m unittest tests/test_new.py",
    "python3 -m unittest tools/test_gen.py",
    "python3 -m unittest tools.test_gen",
    "python3 -m pytest tests/helpers/gen.py",
    "pytest tools/gen_test.py",
    "node --test tools/gen.test.js",
    "node --import ./tests/setup.mjs --test tests/",
    "node -r ./tests/helpers/gen.cjs --test",
    "bun test tools/gen.test.ts",
    "bun test --preload ./tests/setup.mjs tests/",
    "deno test tests/new.test.ts",
    "npx vitest run --config web/vitest.config.mjs tests/",
    "npx jest -c web/jest.config.js tests/",
    "python3 -m unittest scripts/tool.py",
    "node --test scripts/tool.mjs",
    # #321's row: a virtual environment's python by its path, with no environment behind it
    ".venv/bin/python -m pytest tests/test_new.py",
)


class InlineLoaderTests(ProgramFormCase):
    def setUp(self) -> None:
        super().setUp()
        # untracked: the test-author's tests by the default patterns, outside tests/, and
        # runner configs
        self.program(self.wt / "tools/test_gen.py", "py")
        self.program(self.wt / "tools/gen_test.py", "py")
        self.program(self.wt / "tools/gen.test.js", "cjs")
        self.program(self.wt / "tools/gen.test.ts", "ts")
        self.write(self.wt / "web/vitest.config.mjs", "export default {};\n")
        self.write(self.wt / "web/jest.config.js", "module.exports = {};\n")

    def cases(self, table) -> list:
        return [(self.fill(c), (self.fill(p) if p else self.fill(c),)) for c, p in table]

    # ---- refused on the ticket branch

    def test_an_inline_python_program_loading_an_untracked_module_or_file_is_refused(self):
        self.expect_refused(self.cases(PYTHON))

    def test_an_inline_node_program_loading_an_untracked_file_is_refused(self):
        self.expect_refused(self.cases(NODE))

    def test_an_inline_ruby_perl_or_php_program_loading_an_untracked_file_is_refused(self):
        self.expect_refused(self.cases(RUBY + PERL + PHP))

    def test_a_loader_whose_path_comes_from_outside_the_program_is_refused(self):
        """exec(open(sys.argv[1]).read()), require(process.argv[1]), load ARGV[0]: the path
        is not in the program's text, so the hook cannot know it, wherever the file is."""
        self.expect_rotated(UNKNOWN, lambda c: (c,))

    def test_a_dynamic_python_import_of_an_untracked_module_is_refused(self):
        self.expect_refused(self.cases(DYNAMIC))

    def test_the_outline_commands_are_refused_to_each_role(self):
        self.expect_refused([(c, (c,)) for c in LITERAL])

    def test_a_runner_given_an_untracked_file_that_is_no_test_is_refused(self):
        """The ruling: under a runner an untracked file runs only when it is a test or test
        configuration; python3 -m doctest tools/gen.py is refused today, and so is this."""
        self.expect_refused([(c, (c,)) for c in RUNNER_REFUSED])

    def test_what_is_a_test_comes_from_the_projects_test_patterns(self):
        """HARNESS_TEST_PATTERNS in the main checkout's settings decides what a runner may
        run untracked: its tests run, the defaults' do not."""
        settings = {
            "env": {
                "HARNESS_TEST_CMD": "python3 -m unittest {files}",
                "HARNESS_TEST_PATTERNS": "checks/**,**/*_check.py",
            }
        }
        self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.git(self.repo, "commit", "-q", "-am", "The project's own test patterns")
        self.program(self.wt / "checks/login.py", "py")
        self.program(self.wt / "tools/gen_check.py", "py")
        self.expect_allowed(
            [
                "python3 -m unittest checks/login.py",
                "python3 -m pytest tools/gen_check.py",
                "python3 -m unittest checks.login",
            ]
        )
        self.expect_refused(
            [
                (c, (c,))
                for c in (
                    "python3 -m unittest tests/test_new.py",
                    "python3 -m unittest tools/test_gen.py",
                    "node --test tests/new.test.js",
                )
            ]
        )

    def test_the_breaker_scratch_worktree_follows_the_same_rule(self):
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        o = self.outside
        for command in (
            f"python3 -c \"exec(open('{o}/w.py').read())\"",
            f"node -e 'require(\"{o}/w.cjs\")'",
            f"PYTHONPATH={o} python3 -c 'import w'",
        ):
            with self.subTest(command=command, where="scratch"):
                self.assertRefused(self.bash(command, breaker, cwd=scratch), command)
        for command in ("python3 -c 'import scripts.tool'", "node -e 'require(\"./scripts/tool.cjs\")'"):
            with self.subTest(command=command, where="scratch"):
                self.assertIsNone(self.bash(command, breaker, cwd=scratch))

    # ---- still allowed

    def test_inline_programs_loading_tracked_or_installed_code_still_run(self):
        self.expect_allowed([self.fill(c) for c in INLINE_ALLOWED])

    def test_a_runner_on_new_tests_and_test_configuration_still_runs(self):
        self.expect_allowed(RUNNER_ALLOWED)

    def test_the_main_session_and_other_agents_are_left_alone(self):
        commands = list(LITERAL) + [
            self.fill("python3 -c \"exec(open('{o}/w.py').read())\""),
            self.fill("ruby -e 'load \"{o}/w.rb\"'"),
            "python3 -m unittest tools/gen.py",
            "node --import ./tools/gen.mjs --test tests/",
        ]
        for command in commands:
            for agent in (None, "harness:reviewer"):
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))

    def test_off_a_ticket_branch_nothing_changes(self):
        o = self.outside
        commands = list(LITERAL[1:]) + [
            f"python3 -c \"exec(open('{o}/w.py').read())\"",
            f"PYTHONPATH={o} python3 -c 'import w'",
            f"node -e 'require(\"{o}/w.cjs\")'",
            f"perl -e 'do \"{o}/w.pl\"'",
            f"php -r 'include \"{o}/w.php\";'",
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
        extra = ["python3 -c 'import tools.gen'", "python3 -m unittest tools/gen.py", "node -e 'require(\"./tools/gen.cjs\")'"]
        for k, command in enumerate(commands + extra):
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent, where="feature/login"):
                self.assertIsNone(self.bash(command, agent))

    # ---- keeps its deadline

    def test_the_new_forms_are_judged_within_the_hooks_deadline(self):
        """Each new form on an untracked program and its tracked twin, with every git call
        slow, or never answering: decided in time, and an untracked program is never let
        through because git was slow."""
        limit = hook_timeouts("tests_first.py")["Bash"] - STARTUP_ALLOWANCE
        untracked = {
            "python3 - < tools/gen.py": ("python3 - < tools/gen.py",),
            "cat tools/gen.py | python3": piped("cat tools/gen.py | python3"),
            "sed -n p tools/gen.sh | bash": piped("sed -n p tools/gen.sh | bash"),
            "source tools/gen.sh": ("source tools/gen.sh",),
            "./tools/gen.sh": ("./tools/gen.sh",),
            "python3 -c 'import tools.gen'": ("python3 -c 'import tools.gen'",),
            "python3 -m unittest tools/gen.py": ("python3 -m unittest tools/gen.py",),
        }
        tracked = (
            "python3 - < scripts/tool.py",
            "source scripts/tool.sh",
            "./scripts/tool.sh",
            "python3 -c 'import scripts.tool'",
        )
        payloads = {
            c: self.payload("Bash", {"command": c}, self.wt, "harness:coder")
            for c in list(untracked) + list(tracked)
        }
        # at full speed the in-process run decides as the hook's own process does
        for command, payload in payloads.items():
            with self.subTest(command=command, delay=0.0):
                reason, _, calls = self.hook_with_slow_git(payload, 0.0)
                env = {"PWD": str(self.wt)}
                self.assertEqual(reason, self.decision(self.run_hook("tests_first.py", payload, env=env)))
                if command in tracked:
                    self.assertIsNone(reason, calls)
                else:
                    self.assertRefused(reason, *untracked[command])
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
                        if command not in tracked:
                            self.assertIsNotNone(reason, calls)


if __name__ == "__main__":
    unittest.main()
