"""Shared by test_guards, test_guards_exemptions, test_guards_tests_first, test_guards_main_merge: the fixtures, helpers and base classes of the
test_guards suite, split by test class so no one module bounds the parallel suite. Not a test
module (no `test_` prefix): discovery and scripts/run_suite.py skip it. Each test module
imports everything here, `setUpModule` and `tearDownModule` included, so each still builds its
own fixtures once per process.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SHOWN_LIMIT = 160  # a refused part is displayed cut to 160 characters


def as_shown(part: str) -> str:
    """The refused part as a denial displays it: up to SHOWN_LIMIT characters,
    a longer part cut to SHOWN_LIMIT - 1 characters plus an ellipsis."""
    if len(part) <= SHOWN_LIMIT:
        return part
    return part[: SHOWN_LIMIT - 1] + "…"


SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
FORCE_ADVICE = (
    "Force pushes are refused on every branch; push new commits instead, or ask the user "
    "to rewrite history by hand."
)
AUTHOR_ON_SOURCE = (
    "You write tests only on this ticket. Report the source change the implementation "
    "needs; the coder makes it."
)
CODER_ON_TEST = (
    "Tests are fixed for this ticket. If a test is wrong, stop and report it; the "
    "orchestrator sends it back to the test-author."
)
PUSH_ADVICE = (
    "main and master change only through a pull request: push the ticket branch and "
    "open one (harness-pr open <issue#>)."
)
CODER_ON_CONFIG = (
    "{rel} decides which tests run and how, so it is fixed for this ticket like the "
    "tests. If it has to change, stop and ask the orchestrator."
)
BREAKER_ON_TEST = (
    "Tests are fixed for this ticket: find the cheapest source change that passes them, "
    "and if there is none, report that."
)
# git forms that rewrite the working tree wholesale: they touch every test file.
BULK_GIT_FORMS = (
    "git checkout -- .",
    "git checkout .",
    "git checkout HEAD -- .",
    "git checkout -- src",
    "git checkout HEAD src",
    "git restore .",
    "git restore -- .",
    "git restore --source=HEAD --worktree .",
    "git restore --staged --worktree .",
    "git restore src",
    "git reset --hard",
    "git reset --hard HEAD",
    "git reset --merge",
    "git reset --merge ORIG_HEAD",
    "git stash",
    "git stash -u",
    "git stash push",
    "git stash push -m wip",
    "git stash pop",
    "git stash apply",
    "git stash apply stash@{0}",
    "git stash drop",
    "git clean -fd",
    "git clean -fdx",
)
# Writers whose paths cannot be seen: patch reading the files from the diff, find -exec.
# (command, the part the reason quotes)
HIDDEN_WRITER_FORMS = (
    ("patch -p1 < fix.diff", "patch -p1 < fix.diff"),
    ("patch -R -p1 < fix.diff", "patch -R -p1 < fix.diff"),
    ("patch -p0 -i fix.diff", "patch -p0 -i fix.diff"),
    ("patch -d tests -p1 < fix.diff", "patch -d tests -p1 < fix.diff"),
    ("git diff HEAD | patch -p1", "patch -p1"),
    (
        "find . -name '*.py' -exec sed -i '' 's/pass/assert 0/' {} +",
        "find . -name '*.py' -exec sed -i '' 's/pass/assert 0/' {} +",
    ),
    ("find tests -type f -exec rm {} \\;", "find tests -type f -exec rm {} \\;"),
    (
        "find . -name '*.py' -execdir perl -pi -e 's/a/b/' {} +",
        "find . -name '*.py' -execdir perl -pi -e 's/a/b/' {} +",
    ),
    ("find . -type f -exec touch {} +", "find . -type f -exec touch {} +"),
    (
        "find . -name 'test_*.py' -exec cp /dev/null {} \\;",
        "find . -name 'test_*.py' -exec cp /dev/null {} \\;",
    ),
    ("find . -exec mv {} /tmp \\;", "find . -exec mv {} /tmp \\;"),
    ("find . -exec sh -c 'echo > {}' \\;", "find . -exec sh -c 'echo > {}' \\;"),
    (
        "find . -name '*.py' -exec sed --in-place 's/a/b/' {} +",
        "find . -name '*.py' -exec sed --in-place 's/a/b/' {} +",
    ),
    ("find . -exec tee {} \\;", "find . -exec tee {} \\;"),
    # -i / --input names the diff, not a file to change
    ("patch -p1 -i fix.py", "patch -p1 -i fix.py"),
    ("patch -p1 --input=fix.py", "patch -p1 --input=fix.py"),
)
# checkout or restore of any directory is bulk, whatever the directory holds; the
# fixture (commit_directories) makes lib/ (source only), pkg/ (source only) and docs/
# (no tests at all).
DIRECTORY_FORMS = (
    "git checkout -- lib",
    "git checkout HEAD -- lib/",
    "git restore pkg/",
    "git restore --source=HEAD pkg",
    "git checkout -- docs",
    "git restore docs",
)
INLINE = "Write files with Edit or Write so the tests-first rules can see the path."
NO_RED = (
    "Tests first on t/12: write the tests, then run harness-red check. "
    "(No red run recorded.)"
)
NO_CACHE = " There is no ticket cache either; harness-work start 12 writes it."
# #275: a stale record can also be re-proved against main's code with harness-red reprove.
CHANGED = (
    "Tests changed since the red run; run harness-red check again, or harness-red "
    "reprove once the branch has source changes."
)
BRANCH = "t/12-fix-login"
# Test configuration (tests_first's TEST_CONFIG) that the default HARNESS_TEST_PATTERNS do
# not also call tests: on a no-tests ticket the coder may edit these (#281). conftest.py is
# in both lists, so it stays a test.
CONFIG_NOT_TESTS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "jest.config.js",
    "vitest.config.ts",
    "package.json",
    "web/package.json",
    "web/jest.config.cjs",
)
# Ticket caches that flag no-tests, as source_refusal reads them: in flags, in labels only,
# or beside another flag on a ticket of another type.
NO_TESTS_CACHES = (
    {"flags": ["no-tests"], "type": "chore"},
    {"flags": [], "labels": ["chore", "no-tests", "status:in-progress"], "type": "chore"},
    {"flags": ["needs-local", "no-tests"], "labels": ["task", "needs-local"], "type": "task"},
)


def refusal(part: str, why: str) -> str:
    """A Bash denial as tests_first.py words it."""
    return f'Refused "{part}": {why}'


_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-guards-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def load_hook(name: str) -> types.ModuleType:
    """Import a hook script by path without running its main() or writing bytecode."""
    path = HOOKS / f"{name}.py"
    module = types.ModuleType(f"harness_hook_{name.replace('-', '_')}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-guards-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        # Built from scratch, so nothing leaks in from a Claude Code session running the
        # tests (CLAUDE_PROJECT_DIR, CLAUDE_VERIFY_CMD, HARNESS_*).
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )

    # ---- repos

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return out.stdout.strip()

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_repo(
        self,
        name: str = "proj",
        commit: bool = True,
        branch: str = "main",
        object_format: str | None = None,
    ) -> Path:
        """A repo on `branch` with README.md, src/app.py and tests/test_app.py committed
        (or nothing, with commit=False); `object_format` "sha256" makes a SHA-256 repo."""
        repo = self.tmp / name
        fmt = [f"--object-format={object_format}"] if object_format else []
        subprocess.run(
            ["git", "init", "-q", "-b", branch, *fmt, str(repo)],
            check=True,
            capture_output=True,
            env=self.env,
        )
        if commit:
            self.write(repo / "README.md", "hello\n")
            self.write(repo / "src/app.py", "def login():\n    return None\n")
            self.write(repo / "tests/test_app.py", "def test_login():\n    pass\n")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def worktree(self, repo: Path, branch: str = BRANCH, name: str = "wt") -> Path:
        path = self.tmp / name
        self.git(repo, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    # ---- running a hook

    def run_hook(self, name: str, payload, env: dict | None = None):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        r = subprocess.run(
            [sys.executable, str(HOOKS / name)],
            input=stdin,
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            cwd=str(self.tmp),
            timeout=60,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r

    def payload(self, tool: str, tool_input: dict, cwd: Path, agent=None) -> dict:
        data = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_01",
        }
        if agent is not None:  # present only inside a subagent
            data.update(agent_id="agent-1", agent_type=agent)
        return data

    def decision(self, r) -> str | None:
        """The deny reason, or None when the hook allowed the call (printed nothing)."""
        if not r.stdout.strip():
            return None
        out = json.loads(r.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"])
        d = out["hookSpecificOutput"]
        self.assertEqual(
            [d["hookEventName"], d["permissionDecision"]], ["PreToolUse", "deny"]
        )
        return d["permissionDecisionReason"]


# Ignore rules committed in BranchGuardBashExemptions' repo.
EXEMPT_GITIGNORE = ".env.local\n.venv\n*.log\ndata/\ncache/\n"
OUTSIDE_JSON = "/private/tmp/x/out.json"


def heredoc(program: str, interpreter: str = "python3 -", tag: str = "PY") -> str:
    """`interpreter <<'TAG'` reading `program` from a heredoc."""
    return f"{interpreter} <<'{tag}'\n{program}\n{tag}"



TEMP_ROOT = os.path.realpath(tempfile.gettempdir())


def swapped_below(path: Path, base: str = TEMP_ROOT) -> Path:
    """`path` with the case of every component below `base` swapped (the fixture's own folders too)."""
    rel = os.path.relpath(str(path), base)
    assert not rel.startswith(os.pardir), (path, base)
    return Path(base, *[part.swapcase() for part in rel.split(os.sep)])


def listed(path: Path) -> bool:
    """True when every component of the absolute `path` is spelled as its parent lists it."""
    parent = os.sep
    for part in str(path).strip(os.sep).split(os.sep):
        try:
            if part not in os.listdir(parent):
                return False
        except OSError:
            return False
        parent = os.path.join(parent, part)
    return True


# ---------------------------------------------------------------- a merge of main (#231)

RED_TOOL = HOOKS.parent / "bin" / "harness-red"
# The test command harness-red runs: it fails as an assertion would, whatever the files.
FAILING_RUN = "import sys; print('AssertionError: login is not built yet'); sys.exit(1)"
# Ten small tests, three lines apart, so edits to the first, the middle and the last
# are separate hunks that git merges cleanly.
BASE_TESTS = "\n".join(
    f"def test_case_{i}():\n    assert {i} + {i} == {2 * i}\n" for i in range(10)
)
MERGE_TEST_FILES = ("tests/test_app.py", "tests/test_util.py")
# The branch's own test change, in the middle of each file.
MAIN_LINE_5 = "    assert 5 + 5 == 10\n"
BRANCH_LINE_5 = "    assert login() == 'ok'\n"


MAIN_LINE_5_REWRITTEN = "    assert 5 * 2 == 10\n"  # main's conflicting rewrite
RED_KINDS = ("assertion", "import", "error")


def replaced(text: str, old: str, new: str) -> str:
    """`text` with the one occurrence of `old` replaced; fails loudly when it is absent."""
    assert text.count(old) == 1, (old, text)
    return text.replace(old, new)


def swapped(text: str, a: str, b: str) -> str:
    """`text` with the one occurrence each of `a` and `b` swapped: same lines, new order."""
    marker = "\0swap\0"
    return replaced(replaced(replaced(text, a, marker), b, a), marker, b)


def case_line(i: int) -> str:
    """The assert line of test_case_i as main first wrote it."""
    return f"    assert {i} + {i} == {2 * i}\n"


def moved_to_end(text: str, block: str) -> str:
    """`text` with `block` (a test and its blank separator) moved after the last test:
    the same lines, in another order."""
    return replaced(text, block + "\n", "") + "\n" + block


def line_moved_into(text: str, line: str, anchor: str) -> str:
    """`text` with `line` taken out of its test and put after `anchor`, in another one."""
    return replaced(replaced(text, line, ""), anchor, anchor + line)


# Repeated context: ten identical top-level tests with PEP 8's two blank lines between
# them, so every gap between tests has the same surroundings, however many lines of
# context a diff shows; only the position tells the gaps apart. The branch's own change
# is a whole new test inserted into gap 5.
SAME_TEST = "def test_case():\n    assert ok()"
LOGIN_TEST = "def test_login():\n    assert login() == 'ok'"
MAIN_LOGIN_TEST = "def test_login_main():\n    assert login() is not None"


def pep8(tests) -> str:
    return "\n\n\n".join(tests) + "\n"


def pep8_tests(text: str) -> list:
    assert text.endswith("\n") and not text.endswith("\n\n"), text
    return text[:-1].split("\n\n\n")


def put_at(text: str, gap: int, test: str) -> str:
    """`text` with `test` inserted as the gap-th top-level item (0: first)."""
    tests = pep8_tests(text)
    tests.insert(gap, test)
    return pep8(tests)


def moved_to_gap(text: str, test: str, gap: int) -> str:
    """`text` with its one `test` moved, unchanged, to another gap."""
    tests = pep8_tests(text)
    assert tests.count(test) == 1, text
    tests.remove(test)
    tests.insert(gap, test)
    return pep8(tests)


def with_test(text: str, index: int, test: str) -> str:
    """`text` with its index-th top-level item replaced by `test`."""
    tests = pep8_tests(text)
    tests[index] = test
    return pep8(tests)


# A unique first item (the imports) and last test bound the repeated run: main's clean
# edits go there, where git's alignment is unambiguous (an edit inside the identical
# run can be aligned to another copy, and the merge then moves the branch's test).
# Items: 0 the imports, 1-10 the repeated tests, 11 the last test; the branch's test
# goes in as item 6, after the fifth repeated test.
REPEATED_TESTS = pep8(["import pytest", *[SAME_TEST] * 10, "def test_last():\n    assert last()"])
LOGIN_GAP = 6


# Blank-line runs (#231 review): main trims the blank lines after the imports, as black
# does, and appends a test; the branch adds a line after the first blank line of
# test_login's body. git merges it cleanly (ort, and merge-file's Myers alike), but
# taking main's change back out of the merged file aligns the runs of blank lines
# differently and lands the branch's line in the wrong place.
TRIM_TESTS = (
    "import pytest\n\n\n\n\ndef test_login():\n\n\n    assert True\n\n\n"
    "def test_other():\n    assert True\n"
)
TRIM_ANCHOR = "def test_login():\n\n"


def trim_author(text: str) -> str:
    return replaced(text, TRIM_ANCHOR, TRIM_ANCHOR + BRANCH_LINE_5)


def trim_main(text: str) -> str:
    return (
        replaced(text, "import pytest\n\n\n\n\n", "import pytest\n\n\n")
        + "\n\ndef test_more():\n    assert True\n"
    )


# ort vs Myers (#231 re-check): main adds an import header and strips the trailing blank
# lines; the branch adds a line to test_login. `git merge` (ort, histogram) merges it
# cleanly; `git merge-file` (Myers) on the same three inputs conflicts.
DIVERGE_TESTS = "def test_login():\n    pass\n\n\n"
DIVERGE_MERGED = "import pytest\n\n\ndef test_login():\n" + BRANCH_LINE_5 + "    pass\n"


def diverge_author(text: str) -> str:
    return replaced(text, "def test_login():\n", "def test_login():\n" + BRANCH_LINE_5)


def diverge_main(text: str) -> str:
    assert text == DIVERGE_TESTS, text
    return "import pytest\n\n\ndef test_login():\n    pass\n"


def myers_merge(tmp: Path, ours: str, base: str, theirs: str, env: dict) -> tuple:
    """(exit status, output) of `git merge-file -p` (Myers) on the three texts."""
    paths = []
    for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
        path = tmp / f"myers-{name}"
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    r = subprocess.run(
        ["git", "merge-file", "-p", "-q", *paths], capture_output=True, text=True, env=env
    )
    return r.returncode, r.stdout


def content_hash(root: Path, files) -> str:
    """The red record's test_hash, from first principles: sha256 over each file's path,
    a newline and its bytes, in sorted path order."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\n" + (root / name).read_bytes())
    return h.hexdigest()


# The hook's deadline (#231 ultrareview): hooks.json gives tests_first.py a timeout; a hook
# Claude Code kills at it prints nothing, and the write goes through. Interpreter start and
# imports come out of the same timeout, so the hook's own work must end this much sooner.
STARTUP_ALLOWANCE = 0.5
_REAL_RUN = subprocess.run
_REAL_MONOTONIC = time.monotonic
_REAL_PERF_COUNTER = time.perf_counter
_REAL_TIME = time.time


def hook_timeouts(script: str) -> dict:
    """matcher -> the timeout (seconds) hooks.json gives `script` under PreToolUse."""
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    found = {}
    for entry in data["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            if any(str(a).endswith("/" + script) for a in hook.get("args", [])):
                found[entry["matcher"]] = hook["timeout"]
    return found


class SlowGit:
    """Every git that subprocess.run starts in this process takes `delay` seconds of a fake
    clock (time.monotonic, time.perf_counter and time.time run ahead by it), or, given a
    shorter timeout, finishes JUST_IN_TIME before it: the slowest git that never times out.
    A call with no time left (a timeout under JUST_IN_TIME) is cut at its timeout and raises
    TimeoutExpired, as subprocess.run would. A call that finishes runs the real git, under a
    real timeout of REAL_TIMEOUT instead of the fake one. The fake clock stands still between
    git calls, and the real git's own wall time does not count either: only the fake git time
    moves it, so a run decides the same however loaded the machine is (#285: real time on the
    clock cut the JUST_IN_TIME margin under load).
    `hang(argv, index)`, given, picks the calls (index counts this run's git calls from 0)
    that never answer: each takes its whole timeout and raises TimeoutExpired. No real
    sleeping."""

    JUST_IN_TIME = 0.05
    REAL_TIMEOUT = 60

    def __init__(self, delay: float, hang=None) -> None:
        self.delay = delay
        self.hang = hang
        self.ahead = 0.0
        self.calls: list = []
        self._monotonic = _REAL_MONOTONIC()
        self._perf_counter = _REAL_PERF_COUNTER()
        self._time = _REAL_TIME()

    def monotonic(self) -> float:
        return self._monotonic + self.ahead

    def perf_counter(self) -> float:
        return self._perf_counter + self.ahead

    def time(self) -> float:
        return self._time + self.ahead

    def run(self, args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if not (argv and os.path.basename(str(argv[0])) == "git"):
            return _REAL_RUN(args, *rest, **kwargs)
        timeout = kwargs.get("timeout")
        if self.hang is not None and self.hang(argv, len(self.calls)):
            # a git that never answers: subprocess.run waits out its timeout, then raises
            if timeout is None:  # it would hang the hook itself, past hooks.json's timeout
                self.calls.append((argv, "hung, no timeout"))
                raise subprocess.TimeoutExpired(argv, 0)
            self.ahead += max(float(timeout), 0.0)
            self.calls.append((argv, "timed out"))
            raise subprocess.TimeoutExpired(argv, timeout)
        takes = self.delay
        if timeout is not None:
            takes = min(takes, float(timeout) - self.JUST_IN_TIME)
        if takes < 0:
            self.ahead += max(float(timeout), 0.0)
            self.calls.append((argv, "timed out"))
            raise subprocess.TimeoutExpired(argv, timeout)
        self.ahead += takes
        self.calls.append((argv, "ran"))
        if timeout is not None:
            # The fake clock has decided this call finishes in time; the real git gets a real
            # timeout of its own, or under load it would be cut at the fake one (#285).
            kwargs["timeout"] = self.REAL_TIMEOUT
        return _REAL_RUN(args, *rest, **kwargs)


# Everything above, underscored names included, for `from guards_support import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
