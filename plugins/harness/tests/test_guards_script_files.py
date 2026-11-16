"""tests_first.py: an interpreter or shell run on a program file the worktree does not track (#321).

On a ticket branch the coder, the test-author and the test-breaker are refused a run of
python, node, deno, bun, ruby, perl, php, sh, bash or zsh on a program file that is not
tracked at the worktree's HEAD (in /tmp, the scratchpad, ~, another repository, or new in
the worktree): what it writes cannot be seen. Tracked means in HEAD's tree, not the
index. A test runner given a program file outside the worktree is refused the same way.
The program file is judged wherever it stands and whatever option precedes it: behind an
option whose value names a tracked path or directory (ruby -I lib, -C dir; perl -I lib;
php -c dir, -d k=v; node -r file, --env-file; deno --config, -c, --import-map), as a
preload run next to a tracked program or next to nothing (node -r, --require, --import,
--loader; ruby -r; bun --preload, -r; php -d auto_prepend_file=), as a module python
resolves from the part's directory or its PYTHONPATH (on the part or exported in an
earlier part; separate or attached: -m w, -mw, -Imw), through a standard-library module
that runs a file or a module (pdb, cProfile, profile, trace, doctest, runpy), behind a
wrapper (uv run, poetry run, npx, bunx) or a bare runner (pytest, vitest), as a tracked
directory whose entry file is untracked (tracked means a blob at HEAD, not a tree), and
from any directory the part runs in (cd /tmp && python3 f). A PYTHONPATH entry the hook
cannot expand ($PWD, $(pwd)) with a module that is not installed is not known, so it is
refused like python3 $F. A test runner's test directory, working directory or config
outside the worktree is refused; a load path or a runner's output path is not a program:
ruby -I /tmp/gems on a tracked program, pytest --junitxml /tmp/r.xml, PYTHONPATH=x
python3 -m pytest run. A runner on the worktree's own files, tracked or new, as a path or
a dotted module (python3 -m unittest tests.test_new), still runs, as do tracked program
files, npm and harness-red; off a ticket branch nothing changes.

Left to follow-up tickets: a program on stdin or a pipe (python3 - < f, bash < f,
cat f | python3, <<<), source f and . f, inline loaders (python3 -c 'import tools.gen',
exec(open(..)), node -e 'import(..)', ruby -e 'require ..', php -r 'include ..'), and the
admitted hole that an in-tree untracked program runs under a runner (python3 -m unittest
tools/gen.py, node --import ./tools/gen.mjs --test tests/): the test-author's new files
must run. The fixtures are in guards_support.py.

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
    "cjs": "const fs = require('node:fs');\nfs.writeFileSync('tests/test_app.py', '');\nfs.writeFileSync('src/app.py', '');\n",
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
    ("python3 -- {f}", "py"),
    ("python3 -W ignore -- {f}", "py"),
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
    ("node --conditions development {f}", "mjs"),
    ("node -C development {f}", "mjs"),
    # an option's value naming a tracked file does not make the run tracked
    ("python3 -W scripts/tool.py {f}", "py"),
    ("python3 -X scripts/tool.py {f}", "py"),
    ("node -C scripts/tool.mjs {f}", "mjs"),
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
    "python3 -m pytest tests/test_app.py",  # tracked, in the worktree
    "node --test scripts/tool.mjs",
    # a runner's test target, config or working directory in the worktree
    "python3 -m pytest tests/",
    "python3 -m pytest -c pytest.ini tests/",
    "python3 -m pytest --rootdir . tests/",
    "python3 -m pytest --rootdir {wt} -c pytest.ini tests/test_new.py",
    "python3 -m unittest tests.test_new",  # the new test as a dotted module
    "python3 -m unittest -v tests.test_new tests.test_app",
    "python3 -m pytest --pyargs tests.test_new",
    "node --test ./tests/",
    "node --test {wt}/tests/",
    "deno test tests/",
    "bun test --cwd tests",
    "bun test --cwd . tests/new.test.ts",
    # wrappers and bare runners on the worktree's files
    "uv run python scripts/tool.py",
    "uv run scripts/tool.py",
    "uv run python -m pytest tests/test_new.py",
    "uv run python -m unittest tests/test_new.py",
    "uv run pytest tests/",
    "poetry run python scripts/tool.py",
    "poetry run pytest tests/",
    "npx tsx scripts/tool.ts",
    "bunx tsx scripts/tool.ts",
    "pytest tests/test_new.py",
    "pytest -q tests/",
    "vitest run tests/new.test.ts",
    "harness-red run --worktree {wt}",
    "harness-red check",
    "{red} run --worktree {wt}",
    "python3 --version",
    "node --version",
    "bash --version",
    "python3 -c 'print(1)'",
    "sh -c 'echo hi'",
    "bash -lc 'npm test'",
)
# -m with a module that is not the worktree's own file: the standard library, installed.
MODULES = (
    "python3 -m pip --version",
    "python3 -m venv --help",
    "python3 -m timeit pass",
    "python3 -m site",
    "python3 -m json.tool tests/fixtures/data.json",
    # the standard library's runners of a file, given a tracked one
    "python3 -m pdb scripts/tool.py",
    "python3 -m cProfile -o /tmp/prof scripts/tool.py",
    "python3 -m cProfile -s cumtime scripts/tool.py",
    "python3 -m trace --trace scripts/tool.py",
    "python3 -m doctest scripts/tool.py",
    "python3 -m runpy scripts.tool",
)
# Tracked program files run in other spellings ({wt} is the worktree, {o} the outside
# directory): behind options, some taking a value; from another directory; as modules,
# resolved from the directory the part runs in; as a directory with a tracked entry file;
# the last is the project's test command (HARNESS_TEST_CMD in the main checkout's tracked
# settings).
TRACKED_SPELLINGS = (
    "python3 ./scripts/tool.py",
    "python3 {wt}/scripts/tool.py",
    "cd scripts && python3 tool.py",
    "cd {o} && python3 {wt}/scripts/tool.py",
    "cd /tmp && bash {wt}/scripts/tool.sh",
    "env python3 scripts/tool.py",
    "python3 -mscripts.tool",
    "python3 -Imscripts.tool",
    "python3 scripts/pkg",
    "node scripts/app",
    "node ./scripts/app/",
    "python3 -I scripts/tool.py",
    "python3 -W ignore scripts/tool.py",
    "python3 -X utf8 scripts/tool.py",
    "python3 -X dev -W error scripts/tool.py",
    "python3 -X pycache_prefix=/tmp/pycache scripts/tool.py",
    "node --no-warnings scripts/tool.mjs",
    "node --conditions development scripts/tool.mjs",
    "node -C development scripts/tool.mjs",
    "deno run -A scripts/tool.ts",
    "php -f scripts/tool.php",
    "python3 -m scripts.tool",
    "python3 -m scripts.pkg",  # a package, run through its __main__.py
    "cd scripts && python3 -m tool",
    "cd scripts && python3 -m pkg",
    "python3 scripts/run_tests.py tests/test_new.py",
)
# An option taking a value that names a tracked path (a load path, a config, a preload),
# then the program file: the value is not the program, so the run is still judged by the
# file it runs ({f} is the untracked program).
VALUE_OPTION_SPELLINGS = (
    ("ruby -I scripts {f}", "rb"),
    ("ruby -I lib {f}", "rb"),
    ("ruby -Ilib -w {f}", "rb"),
    ("perl -I scripts {f}", "pl"),
    ("perl -I lib {f}", "pl"),
    ("perl -Ilib -w {f}", "pl"),
    ("perl -Mlib=scripts {f}", "pl"),
    ("perl -I scripts -Mlib=lib {f}", "pl"),
    ("php -c scripts {f}", "php"),
    ("php -d display_errors=1 {f}", "php"),
    ("php -c scripts -d display_errors=1 {f}", "php"),
    ("node -r scripts/setup.cjs {f}", "mjs"),
    ("node --require scripts/setup.cjs {f}", "mjs"),
    ("node --import scripts/setup.mjs {f}", "mjs"),
    ("node --import=./scripts/setup.mjs {f}", "mjs"),
    ("node --env-file .env {f}", "mjs"),
    ("node --env-file=.env {f}", "mjs"),
    ("deno run --config deno.json {f}", "ts"),
    ("deno run -c deno.json {f}", "ts"),
    ("deno run --config=deno.json -A {f}", "ts"),
    ("deno run --import-map deno.json {f}", "ts"),
    ("bun --preload scripts/setup.ts {f}", "ts"),
    ("python3 -W ignore -X utf8 {f}", "py"),
    # a value that is a directory: a working directory, a load path
    ("ruby -C scripts {f}", "rb"),
    ("ruby -C scripts -I lib {f}", "rb"),
)
# A preload is a program the interpreter runs before the one it is given: an untracked
# or out-of-tree preload next to a tracked program ({f} is the preload).
PRELOAD_SPELLINGS = (
    ("node -r {f} scripts/tool.mjs", "cjs"),
    ("node --require {f} scripts/tool.mjs", "cjs"),
    ("node --require={f} scripts/tool.mjs", "cjs"),
    ("node --import {f} scripts/tool.mjs", "mjs"),
    ("node --import={f} scripts/tool.mjs", "mjs"),
    ("node --loader {f} scripts/tool.mjs", "mjs"),
    ("node --experimental-loader={f} scripts/tool.mjs", "mjs"),
    ("ruby -r{f} scripts/tool.rb", "rb"),
    ("ruby -r {f} scripts/tool.rb", "rb"),
    ("bun --preload {f} scripts/tool.ts", "ts"),
    ("bun --preload={f} scripts/tool.ts", "ts"),
    ("bun -r {f} scripts/tool.ts", "ts"),
    ("php -d auto_prepend_file={f} scripts/tool.php", "php"),
    ("node --import {f} --test tests/", "mjs"),  # and next to a runner
    ("node -r {f} --test", "cjs"),
    # and with no program file after it: an inline program, or nothing
    ("node -r {f} -e 0", "cjs"),
    ("node --import {f} -p 1", "mjs"),
    ("ruby -r{f} -e 1", "rb"),
)
# The same options with tracked values and tracked programs, or a value that is a load
# path and not a program at all ({o} is the untracked directory outside the worktree,
# {wt} the worktree).
VALUE_OPTION_TRACKED = (
    "ruby -I lib scripts/tool.rb",
    "ruby -Ilib -w scripts/tool.rb",
    "ruby -I {o}/gems scripts/tool.rb",
    "ruby -r ./scripts/setup.rb scripts/tool.rb",
    "ruby -r ./scripts/setup.rb -e 1",
    "ruby -C scripts {wt}/scripts/tool.rb",
    "perl -I lib scripts/tool.pl",
    "perl -I ~/perl5/lib scripts/tool.pl",
    "perl -Mlib=scripts scripts/tool.pl",
    "php -c scripts scripts/tool.php",
    "php -d display_errors=1 scripts/tool.php",
    "php -d auto_prepend_file=scripts/setup.php scripts/tool.php",
    "node -r scripts/setup.cjs scripts/tool.mjs",
    "node -r scripts/setup.cjs -e 0",
    "node --import ./scripts/setup.mjs -p 1",
    "node --env-file .env scripts/tool.mjs",
    "node --env-file=.env scripts/tool.mjs",
    "deno run -c deno.json scripts/tool.ts",
    "deno run --import-map deno.json scripts/tool.ts",
    "node --require ./scripts/setup.cjs scripts/tool.mjs",
    "node --import ./scripts/setup.mjs scripts/tool.mjs",
    "node --import=./scripts/setup.mjs scripts/tool.mjs",
    "node --import ./scripts/setup.mjs --test tests/",
    "node --import ./tests/setup.mjs --test tests/",  # the test-author's new setup, run by a runner
    "deno run --config deno.json scripts/tool.ts",
    "bun --preload ./scripts/setup.ts scripts/tool.ts",
    "bun -r scripts/setup.ts scripts/tool.ts",
)
# -m with PYTHONPATH on the part: the module is resolved from the part's directory and
# each PYTHONPATH entry, so a module found in an untracked or out-of-tree directory is the
# untracked program it is. (command with {o} the outside directory and {home} ~, the part
# the reason quotes or None for the whole command)
PYTHONPATH_MODULES = (
    ("PYTHONPATH=tools python3 -m gen", None),
    ("env PYTHONPATH=tools python3 -m gen", None),
    ("PYTHONPATH=tests/helpers python3 -m gen", None),
    ("PYTHONPATH=scripts:tools python3 -m gen", None),
    ("PYTHONPATH=tools:scripts python3 -m gen", None),
    ("PYTHONPATH=./tools python3 -m gen", None),
    ("PYTHONPATH={o} python3 -m w", None),
    ("PYTHONPATH={home} python3 -m w", None),
    ("cd tests && PYTHONPATH=helpers python3 -m gen", "PYTHONPATH=helpers python3 -m gen"),
    ("cd scripts && PYTHONPATH=../tools python3 -m gen", "PYTHONPATH=../tools python3 -m gen"),
    # exported in an earlier part of the same command
    ("export PYTHONPATH=tools && python3 -m gen", "python3 -m gen"),
    ("export PYTHONPATH=tools; python3 -m gen", "python3 -m gen"),
    ("export PYTHONPATH=tests/helpers\npython3 -m gen", "python3 -m gen"),
    ("export PYTHONPATH={o}; python3 -m w", "python3 -m w"),
    # an entry the hook cannot expand, with a module that is not installed: not known
    ("PYTHONPATH=$PWD/tools python3 -m gen", None),
    ('PYTHONPATH="$PWD/tools" python3 -m gen', None),
    ("PYTHONPATH=$(pwd)/tools python3 -m gen", None),
    ("PYTHONPATH=$PWD/tools:$PYTHONPATH python3 -m gen", None),
)
# PYTHONPATH that leads to a tracked module, or to no module of that name at all (the
# runner is installed), and PYTHONPATH on a tracked program file: the load path does not
# decide, the program does; an entry the hook cannot expand does not matter to a program
# file or an installed module either.
PYTHONPATH_ALLOWED = (
    "PYTHONPATH=scripts python3 -m tool",
    "env PYTHONPATH=scripts python3 -m tool",
    "PYTHONPATH=scripts python3 -m pkg",
    "PYTHONPATH=tests python3 -m unittest tests/test_new.py",
    "PYTHONPATH=tools python3 -m pytest tests/test_new.py",
    "PYTHONPATH=tools python3 -m json.tool tests/fixtures/data.json",
    "PYTHONPATH=tools python3 scripts/tool.py",
    "PYTHONPATH={o} python3 scripts/tool.py",
    "export PYTHONPATH=tools && python3 -m unittest tests/test_new.py",
    "export PYTHONPATH=tools && python3 scripts/tool.py",
    "PYTHONPATH=$PWD/tools python3 -m pytest tests/test_new.py",
    "PYTHONPATH=$PWD/tools python3 -m json.tool tests/fixtures/data.json",
    "PYTHONPATH=$(pwd)/tools python3 scripts/tool.py",
)
# The standard library's modules that run a program file or a module: pdb, cProfile,
# profile, trace and doctest take a file, runpy a module; and a module given to a runner
# resolves like -m does, so one outside the worktree is a program outside the worktree
# ({o} is the outside directory; the part the reason quotes, or None for the whole).
STDLIB_RUNNERS = (
    ("python3 -m pdb {o}/w.py", None),
    ("python3 -m pdb tools/gen.py", None),
    ("python3 -m cProfile {o}/w.py", None),
    ("python3 -m cProfile -o /tmp/prof tools/gen.py", None),
    ("python3 -m profile {o}/w.py", None),
    ("python3 -m trace --trace {o}/w.py", None),
    ("python3 -m trace -t tools/gen.py", None),
    ("python3 -m doctest {o}/w.py", None),
    ("python3 -m doctest tools/gen.py", None),
    ("python3 -m runpy w", None),
    ("python3 -m runpy tools.gen", None),
    ("python3 -m runpy genpkg", None),
    ("PYTHONPATH={o} python3 -m unittest w", None),
    ("PYTHONPATH={o} python3 -m pytest --pyargs w", None),
    ("cd {o} && python3 -m unittest w", "python3 -m unittest w"),
)
# A test runner given a test directory, a config or a working directory outside the
# worktree: the runner imports what it finds there ({o} holds test_w.py, w.test.mjs and
# pytest.ini; {home} is ~).
RUNNER_OUTSIDE = (
    "node --test {o}",
    "node --test {o}/",
    "node --test {home}/",
    "python3 -m pytest {o}",
    "python3 -m pytest {o}/",
    "python3 -m pytest -c {o}/pytest.ini tests/",
    "python3 -m pytest --rootdir {o} -c {o}/pytest.ini tests/",
    "deno test {o}/",
    "bun test --cwd {o}",
    "pytest {o}",
)
# Wrappers that run what follows them, and bare runners: judged by the program file they
# are given ({o} is the outside directory).
WRAPPERS = (
    "uv run python {o}/w.py",
    "uv run {o}/w.py",
    "uv run python -m pytest {o}/w.py",
    "uv run pytest {o}/w.py",
    "uv run python tools/gen.py",
    "uv run tools/gen.py",
    "poetry run python {o}/w.py",
    "poetry run python tools/gen.py",
    "npx tsx {o}/w.ts",
    "npx ts-node {o}/w.ts",
    "npx tsx tools/gen.ts",
    "bunx tsx {o}/w.ts",
    "pytest {o}/w.py",
    "pytest -q {o}/w.py",
    "vitest run {o}/w.ts",
)
# A test runner's options whose values are out-of-tree non-program paths (a report, a
# temp directory, a cache): the loop writes those, so they stay allowed ({o} is the
# outside directory, where r.xml and r.txt exist).
RUNNER_VALUES = (
    "python3 -m pytest --junitxml {o}/r.xml tests/test_new.py",
    "python3 -m pytest --junitxml=/tmp/r.xml tests/test_new.py",
    "python3 -m pytest --basetemp {o}/pt tests/",
    "python3 -m pytest --basetemp=/tmp/pt -q tests/test_new.py",
    "python3 -m pytest -o cache_dir={o}/pc tests/test_new.py",
    "python3 -m pytest -p no:cacheprovider tests/test_new.py",
    "python3 -m unittest -k login tests/test_new.py",
    "uv run pytest --junitxml {o}/r.xml tests/test_new.py",
    "node --test --test-reporter spec --test-reporter-destination {o}/r.txt tests/",
    "node --test --test-reporter=junit --test-reporter-destination=/tmp/r.xml tests/new.test.js",
    "npx vitest run --reporter junit --outputFile {o}/r.xml tests/new.test.ts",
    "deno test --junit-path {o}/r.xml tests/new.test.ts",
    "bun test --timeout 5000 tests/new.test.ts",
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
        self.write(self.wt / "scripts/pkg/__init__.py", "")
        self.write(self.wt / "scripts/pkg/__main__.py", PROGRAMS["py"])
        # tracked values for the options that take one: preloads, load paths, configs
        for ext in ("mjs", "cjs", "rb", "ts", "php"):
            comment = {"rb": "# setup\n", "php": "<?php\n"}.get(ext, "// setup\n")
            self.write(self.wt / f"scripts/setup.{ext}", comment)
        self.write(self.wt / "scripts/app/index.js", "// app\n")  # a directory node runs
        self.write(self.wt / "lib/util.rb", "module Util; end\n")
        self.write(self.wt / "lib/Util.pm", "package Util;\n1;\n")
        self.write(self.wt / "deno.json", "{}\n")
        self.write(self.wt / "pytest.ini", "[pytest]\n")
        self.write(self.wt / ".env", "TZ=UTC\n")
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
        # untracked entry files in a tracked directory: `python3 scripts`, `node scripts`
        self.write(self.wt / "scripts/__main__.py", PROGRAMS["py"])
        self.write(self.wt / "scripts/index.js", PROGRAMS["cjs"])
        # outside the worktree: a test directory with its config, and not programs: a load
        # path, runner output, temp and cache dirs
        self.write(self.outside / "test_w.py", PROGRAMS["py"])
        self.write(self.outside / "w.test.mjs", PROGRAMS["mjs"])
        self.write(self.outside / "pytest.ini", "[pytest]\n")
        for d in ("gems", "pt", "pc"):
            (self.outside / d).mkdir()
        self.write(self.outside / "r.xml", "<testsuites/>\n")
        self.write(self.outside / "r.txt", "ok\n")
        # the test-author's new tests and test setup, not committed yet
        self.write(self.wt / "tests/test_new.py", "def test_new():\n    assert False\n")
        self.write(self.wt / "tests/new.test.js", "require('node:assert').fail('red');\n")
        self.write(self.wt / "tests/new.test.ts", "throw new Error('red');\n")
        self.write(self.wt / "tests/setup.mjs", "process.env.TZ = 'UTC';\n")
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
            # -m with a module the worktree does not track, separate or attached
            ("python3 -m tools.gen", None),
            ("python3 -m genpkg", None),
            ("python3 -m w", None),
            ("cd tools && python3 -m gen", "python3 -m gen"),
            ("python3 -mtools.gen", None),
            ("python3 -mw", None),
            ("python3 -Imtools.gen", None),
            # a tracked directory run as the program: its entry file is untracked
            ("python3 scripts", None),
            ("python3 ./scripts/", None),
            ("python3 -m scripts", None),
            ("node scripts", None),
            ("node ./scripts/", None),
        ]
        for command, part in cases:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part or command)

    def test_the_same_command_follows_whether_head_tracks_the_file(self):
        """Tracked means in HEAD's tree, not in the index. A new program file is refused
        while it is untracked and while it is only staged, and runs once it is committed; a
        committed one keeps running while its removal is only staged, and is refused once
        the removal is committed (the file still on disk)."""
        new = (
            "python3 tools/gen.py",
            "node tools/gen.mjs",
            "bash tools/gen.sh",
            "python3 -m tools.gen",
            "python3 -m genpkg",  # a package, run through its __main__.py
        )
        dropped = ("python3 scripts/tool.py", "bash scripts/tool.sh", "python3 -m scripts.tool")

        def expect(commands, allowed: bool, phase: str) -> None:
            for command in commands:
                for agent in ROLES:
                    with self.subTest(command=command, agent=agent, phase=phase):
                        reason = self.bash(command, agent)
                        if allowed:
                            self.assertIsNone(reason)
                        else:
                            self.assertRefused(reason, command)

        expect(new, False, "untracked")
        expect(dropped, True, "committed")
        self.git(
            self.wt, "add", "tools/gen.py", "tools/gen.mjs", "tools/gen.sh",
            "genpkg/__init__.py", "genpkg/__main__.py",
        )
        self.git(self.wt, "rm", "-q", "--cached", "scripts/tool.py", "scripts/tool.sh")
        expect(new, False, "staged, not committed")
        expect(dropped, True, "removal staged, not committed")
        self.git(self.wt, "commit", "-q", "-m", "Commit the generators, untrack two scripts")
        self.assertTrue((self.wt / "scripts/tool.py").is_file())
        expect(new, True, "committed")
        expect(dropped, False, "removal committed")

    def test_an_option_value_naming_a_tracked_path_does_not_hide_the_program_file(self):
        """ruby -I lib /tmp/w.rb runs /tmp/w.rb: the value of a load path, config or
        preload option is not the program, whatever interpreter it belongs to."""
        commands = [self.outside_file(t, ext) for t, ext in VALUE_OPTION_SPELLINGS]
        commands += [
            "ruby -I lib tools/gen.rb",
            "perl -I lib tests/helpers/gen.pl",
            "php -c scripts tools/gen.php",
            "node -r scripts/setup.cjs tools/gen.mjs",
            "node --import=./scripts/setup.mjs tests/helpers/gen.mjs",
            "deno run --config deno.json tools/gen.ts",
            "bun --preload scripts/setup.ts tools/gen.ts",
        ]
        for command in commands:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_an_untracked_preload_is_a_program_file(self):
        """A preload runs before the program; one the worktree does not track is refused
        however tracked the program next to it is, alone and beside a runner."""
        commands = [self.outside_file(t, ext) for t, ext in PRELOAD_SPELLINGS]
        commands += [
            "node -r tools/gen.cjs scripts/tool.mjs",
            "node --import ./tests/helpers/gen.mjs scripts/tool.mjs",
            "ruby -r ./tools/gen.rb scripts/tool.rb",
            "bun --preload tools/gen.ts scripts/tool.ts",
            "php -d auto_prepend_file=tools/gen.php scripts/tool.php",
            f"node --import {self.home}/w.mjs scripts/tool.mjs",
            "node -r tools/gen.cjs -e 0",
            "ruby -r ./tools/gen.rb -e 1",
        ]
        for command in commands:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_a_module_found_on_pythonpath_is_judged_like_the_file_it_is(self):
        for template, part in PYTHONPATH_MODULES:
            command = template.format(o=self.outside, home=self.home)
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part or command)

    def test_a_test_runner_given_a_program_file_outside_the_worktree_is_refused(self):
        """A runner imports and runs the files it is given, and what it finds in the test
        directory, working directory or config it is given: any of them outside the
        worktree is the same bypass. Runners on the worktree's own files, tracked or new,
        still run."""
        o = self.outside
        outside = [t.format(o=o, home=self.home) for t in RUNNER_OUTSIDE]
        for command in outside + [
            "node --test /tmp/w.mjs",
            f"node --test {o}/w.mjs",
            f"node --test tests/ {o}/w.mjs",
            "python3 -m pytest /tmp/t.py",
            f"python3 -m pytest -q {o}/w.py",
            "python3 -m unittest /tmp/w.py",
            f"python3 -m unittest tests/test_new.py {o}/w.py",
            ".venv/bin/python -m pytest ~/w.py",
            "deno test /tmp/w.ts",
            f"deno test -A {o}/w.ts",
            "bun test /tmp/w.ts",
            f"bun test {o}/w.ts",
        ]:
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_a_stdlib_module_that_runs_a_file_or_module_is_judged_by_it(self):
        """python3 -m pdb /tmp/w.py runs /tmp/w.py, -m runpy w runs w.py: the module
        after -m is the standard library's, the program is what it is given."""
        for template, part in STDLIB_RUNNERS:
            command = template.format(o=self.outside)
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part or command)

    def test_a_wrapper_or_bare_runner_is_judged_by_the_program_file_it_runs(self):
        """uv run python /tmp/w.py, npx tsx /tmp/w.ts, pytest /tmp/w.py: the wrapper runs
        what follows it, and a runner installed on the PATH is the same runner."""
        for template in WRAPPERS:
            command = template.format(o=self.outside)
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), command)

    def test_a_program_file_whose_path_is_not_known_is_refused(self):
        for command, part in (
            ("python3 $F", "python3 $F"),
            ('bash "$SCRIPT"', 'bash "$SCRIPT"'),
            ("F=/tmp/w.py; node $F", "node $F"),
            ('python3 "$HOME/w.py"', 'python3 "$HOME/w.py"'),
        ):
            for agent in ROLES:
                with self.subTest(command=command, agent=agent):
                    self.assertRefused(self.bash(command, agent), part)

    def test_each_part_of_a_command_is_judged_where_it_runs(self):
        """Including a part that runs outside the worktree: the program file is judged
        against the hook's worktree from any directory."""
        o = self.outside
        py, sh = o / "w.py", o / "w.sh"
        mjs, rb, pl = o / "w.mjs", o / "w.rb", o / "w.pl"
        for k, (cwd, command, part) in enumerate(
            (
                (self.wt, f"cd tests && python3 {py}", f"python3 {py}"),
                (self.wt, f"true && bash {sh}", f"bash {sh}"),
                (self.wt, f"(cd src && node {mjs})", f"node {mjs}"),
                (self.wt, f"echo start; ruby {rb}", f"ruby {rb}"),
                (self.wt, f"sh -c 'python3 {py}'", None),
                (self.repo, f"cd {self.wt} && perl {pl}", f"perl {pl}"),
                (self.wt, f"cd {o} && python3 w.py", "python3 w.py"),
                (self.wt, f"cd /tmp && python3 {py}", f"python3 {py}"),
                (self.wt, f"(cd {o}; bash w.sh)", "bash w.sh"),
                (self.wt, "cd ~ && node w.mjs", "node w.mjs"),
                (self.wt, f"cd {o} && python3 -m w", "python3 -m w"),
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
            command = template.format(wt=self.wt, o=self.outside)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

    def test_test_runners_npm_and_harness_red_still_run(self):
        red = HOOKS.parent / "bin" / "harness-red"
        for k, template in enumerate(RUNNERS + MODULES):
            command = template.format(wt=self.wt, red=red)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

    def test_option_values_preloads_and_pythonpath_on_tracked_programs_still_run(self):
        """The mirror of the refusals above: tracked values and preloads, a load path that
        is no program, and PYTHONPATH that leads to a tracked module, to an installed one
        or to nothing the program run depends on."""
        for k, template in enumerate(VALUE_OPTION_TRACKED + PYTHONPATH_ALLOWED):
            command = template.format(o=self.outside, wt=self.wt)
            agent = ROLES[k % len(ROLES)]
            with self.subTest(command=command, agent=agent):
                self.assertIsNone(self.bash(command, agent))

    def test_runner_options_with_out_of_tree_values_still_run(self):
        for k, template in enumerate(RUNNER_VALUES):
            command = template.format(o=self.outside)
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
            "node --import",
            "node --import=",
            "ruby -I",
            "ruby -r",
            "perl -I",
            "php -c",
            "bun --preload",
            "deno run --config",
            "PYTHONPATH= python3 -m gen",
            "PYTHONPATH=tools python3 -m",
            "PYTHONPATH=: python3 -m w",
            "export PYTHONPATH=",
            "export PYTHONPATH=tools &&",
            "python3 -m runpy",
            "python3 -m pdb",
            "python3 -m pytest -c",
            "python3 -mI",
            "bun test --cwd",
            "node --test --test-reporter-destination",
            "uv run",
            "poetry run",
            "npx",
            "pytest",
            "ruby -C",
            "php -d",
            "node --env-file",
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
