"""Tests for harness_lib/gh.py (ticket #90, refactor 7 of docs/proposals/refactor.md): the gh
batch runner harness-standup and harness-digest share, and the gh lookup and environment
session_brief.py shares.

- gh.py: gh_exe() (HARNESS_GH, else `gh`), gh_binary() (HARNESS_GH as a path when it holds a
  separator, else looked up on PATH; None when not found), GH_ENV (GH_PROMPT_DISABLED,
  GH_NO_UPDATE_NOTIFIER, NO_COLOR, each "1"), describe_list_call(args), repo_slug(origin_url)
  and GhBatch(gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call), with
  Problem from cli.py. Expected values are written out here, matching what the copies in the
  tools return today.
- GhBatch runs its calls side by side through fakegh, in `cwd`, with GH_ENV over the caller's
  environment; results() gives one list of objects per call, in call order; the first call
  that fails names itself with the tools' messages today; a timeout says
  `<describe> timed out after <budget_seconds> s` with the budget it was given.
- Exists once: GhBatch, repo_slug, describe_list_call and gh_binary are defined in gh.py and
  nowhere else in the plugin. harness-standup and harness-digest bind the lib's objects, pass
  their own GH_BUDGET as budget_seconds (a recorder put in the tool's GhBatch binding sees it),
  and surface the lib's Problem as their one error line. session_brief.py binds gh_binary and
  GH_ENV from the lib, calls them through those bindings, and keeps no gh_command or GH
  environment of its own (outside the fallback branch of its harness_lib import, as for hookio).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_gh.py" -v
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tools must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
HOOKS = PLUGIN / "hooks"
LIB = PLUGIN / "lib"
GH_PY = LIB / "harness_lib" / "gh.py"
FAKEGH = TESTS / "fakegh.py"
STANDUP = BIN / "harness-standup"
DIGEST = BIN / "harness-digest"
SESSION_BRIEF = HOOKS / "session_brief.py"
GIT = shutil.which("git")

GH_ENV_EXPECTED = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
MOVED = ("GhBatch", "repo_slug", "describe_list_call", "gh_binary")

# The fake gh every GhBatch test runs: it records argv, cwd, pid, start time and a few
# environment variables, sleeps when its argv holds GHTEST_SLOW, then becomes fakegh.py.
WRAPPER = '''#!{python}
import json, os, sys, time
WATCH = ("GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER", "NO_COLOR", "GHTEST_INHERITED")
log = os.environ.get("GHTEST_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
                             "start": time.time(),
                             "env": {{k: os.environ.get(k) for k in WATCH}}}}) + "\\n")
slow = os.environ.get("GHTEST_SLOW")
if slow and slow in sys.argv[1:]:
    time.sleep(float(os.environ.get("GHTEST_SLEEP", "0")))
os.execv({python!r}, [{python!r}, {fakegh!r}] + sys.argv[1:])
'''

W: dict = {}


def setUpModule():
    W["tmp"] = tempfile.mkdtemp(prefix="test-harness-lib-gh-")
    fakebin = os.path.join(W["tmp"], "fakebin")
    os.makedirs(fakebin)
    W["gh"] = executable(os.path.join(fakebin, "gh"),
                         WRAPPER.format(python=sys.executable, fakegh=str(FAKEGH)))
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))


def tearDownModule():
    shutil.rmtree(W["tmp"], ignore_errors=True)


def executable(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def lib(name):
    """harness_lib.<name>, imported from this checkout's lib/ (the tools load the same one)."""
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    return importlib.import_module(f"harness_lib.{name}")


def load(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def clean_env(**extra):
    """os.environ without the GH_ENV variables, HARNESS_GH or the fakes' variables, plus `extra`."""
    env = {k: v for k, v in os.environ.items()
           if k not in GH_ENV_EXPECTED and not k.startswith(("HARNESS_", "FAKEGH_", "GHTEST_"))}
    env.update(extra)
    return env


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="case-", dir=W["tmp"])
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.ghlog = os.path.join(self.tmp, "gh.log")
        self.wraplog = os.path.join(self.tmp, "wrapper.log")

    def scenario(self, data, **extra):
        """Run the rest of the test with fakegh scripted by `data` and the wrappers' logs on."""
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        env = clean_env(FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.ghlog,
                        GHTEST_LOG=self.wraplog, **extra)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def batch(self, calls, budget_seconds=6, wait=8.0, **kwargs):
        gh = lib("gh")
        b = gh.GhBatch(W["gh"], calls, self.tmp, time.monotonic() + wait, budget_seconds=budget_seconds, **kwargs)
        self.addCleanup(b.close)
        return b

    def problem(self, calls, **kwargs):
        """The one-line Problem the batch's results() raises."""
        cli = lib("cli")
        b = self.batch(calls, **kwargs)
        with self.assertRaises(cli.Problem) as caught:
            b.results()
        return str(caught.exception)


# ---------------------------------------------------------------- GhBatch
class GhBatchTests(Case):
    PR = ["pr", "list", "--state", "open", "--json", "number,title", "-R", "acme/widget"]
    ASK = ["issue", "list", "--state", "open", "--label", "status:needs-input", "--json", "number", "-R", "acme/widget"]
    OPEN = ["issue", "list", "--state", "open", "--json", "labels", "--limit", "200"]

    def test_results_are_one_list_of_objects_per_call_in_call_order(self):
        self.scenario({"calls": [
            {"match": ["pr", "list"], "stdout_json": [{"number": 7, "title": "seven"}, {"number": 8, "title": "eight"}]},
            {"contains": ["--label", "status:needs-input"], "stdout_json": [{"number": 31}, 3, "x", None, [1]]},
            {"match": ["issue", "list"], "stdout": "[]\n"},
        ]}, GHTEST_SLOW="pr", GHTEST_SLEEP="0.6")   # the first call finishes last
        out = self.batch([self.PR, self.ASK, self.OPEN]).results()
        self.assertEqual(out, [[{"number": 7, "title": "seven"}, {"number": 8, "title": "eight"}], [{"number": 31}], []])
        # each call ran as `gh <args>` with exactly the args given
        self.assertEqual(sorted(c["argv"] for c in self.lines(self.ghlog)), sorted([self.PR, self.ASK, self.OPEN]))

    def test_different_calls_give_different_results(self):
        """Not a canned answer: the same batch shape over another scenario returns that scenario's data."""
        for rows in ([], [{"n": 1}], [{"n": 2}, {"n": 3, "deep": {"x": [1, 2]}}]):
            with self.subTest(rows=rows):
                self.scenario({"calls": [{"match": ["pr", "list"], "stdout_json": rows},
                                         {"match": ["issue", "list"], "stdout_json": rows[::-1]}]})
                out = self.batch([["pr", "list"], ["issue", "list"]]).results()
                self.assertEqual(out, [rows, rows[::-1]])

    def test_calls_run_side_by_side(self):
        self.scenario({"default": {"stdout": "[]"}}, GHTEST_SLOW="list", GHTEST_SLEEP="1.2")
        started = time.monotonic()
        out = self.batch([["pr", "list"], ["issue", "list"], ["issue", "list", "--label", "epic"]]).results()
        elapsed = time.monotonic() - started
        self.assertEqual(out, [[], [], []])
        self.assertLess(elapsed, 2.4, "three 1.2 s calls one after another take 3.6 s")
        starts = [c["start"] for c in self.lines(self.wraplog)]
        self.assertEqual(len(starts), 3)
        self.assertLess(max(starts) - min(starts), 0.8, starts)

    def test_calls_run_in_cwd_with_gh_env_over_the_callers_environment(self):
        self.scenario({"default": {"stdout": "[]"}}, GHTEST_INHERITED="from-the-caller")
        self.batch([["pr", "list"], ["api", "user"]]).results()
        seen = self.lines(self.wraplog)
        self.assertEqual(len(seen), 2)
        for call in seen:
            self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.tmp))
            self.assertEqual(call["env"], dict(GH_ENV_EXPECTED, GHTEST_INHERITED="from-the-caller"))

    def test_a_timeout_names_the_call_and_the_budget_it_was_given(self):
        """The message carries budget_seconds as passed (not a constant of the lib, not the
        time actually waited): each budget gives its own message."""
        cases = [(1, ["pr", "list", "--json", "number"], "gh pr list timed out after 1 s"),
                 (7, ["issue", "list", "--label", "status:in-progress", "--json", "number"],
                  "gh issue list --label status:in-progress timed out after 7 s"),
                 (13, ["api", "graphql", "-f", "query=x"], "gh api graphql timed out after 13 s")]
        for budget, args, expected in cases:
            with self.subTest(budget=budget):
                self.scenario({"default": {"stdout": "[]"}}, GHTEST_SLOW=args[0], GHTEST_SLEEP="20")
                started = time.monotonic()
                self.assertEqual(self.problem([args], budget_seconds=budget, wait=0.5), expected)
                self.assertLess(time.monotonic() - started, 3)

    def test_the_first_slow_call_in_order_is_the_one_named(self):
        self.scenario({"default": {"stdout": "[]"}}, GHTEST_SLOW="issue", GHTEST_SLEEP="20")
        msg = self.problem([["pr", "list"], ["issue", "list", "--label", "epic"], ["issue", "view"]],
                           budget_seconds=4, wait=0.5)
        self.assertEqual(msg, "gh issue list --label epic timed out after 4 s")

    def test_a_failed_call_gives_the_first_nonblank_line_of_its_stderr(self):
        self.scenario({"calls": [{"contains": ["status:needs-input"], "stderr": "\n   \n  HTTP 404: Not Found  \nmore detail\n",
                                  "exit": 1}], "default": {"stdout": "[]"}})
        self.assertEqual(self.problem([self.ASK]), "gh issue list --label status:needs-input failed: HTTP 404: Not Found")

    def test_a_failed_call_without_stderr_gives_its_exit_status(self):
        for code in (1, 3, 42):
            with self.subTest(code=code):
                self.scenario({"calls": [{"match": ["pr"], "stdout": "[]", "exit": code}]})
                self.assertEqual(self.problem([["pr", "list", "--json", "x"]]), f"gh pr list failed: exit {code}")

    def test_output_that_is_not_json_is_named_with_its_first_line_cut_at_80(self):
        long = "y" * 120
        cases = [("not json at all\nsecond line", "not json at all"),
                 ("\n\n  <html>oops</html>  \n", "<html>oops</html>"),
                 (long + "\nnext", "y" * 80),
                 ("", "")]
        for stdout, shown in cases:
            with self.subTest(stdout=stdout[:20]):
                self.scenario({"calls": [{"match": ["pr"], "stdout": stdout}]})
                self.assertEqual(self.problem([["pr", "list"]]), f"gh pr list printed something other than JSON: {shown}")

    def test_json_that_is_not_a_list_is_named(self):
        for value in ({"a": 1}, 5, "text", None):
            with self.subTest(value=value):
                self.scenario({"calls": [{"match": ["api"], "stdout_json": value}]})
                self.assertEqual(self.problem([["api", "user"]]), "gh api user did not print a JSON list")

    def test_the_first_failing_call_in_call_order_is_named(self):
        self.scenario({"calls": [
            {"match": ["pr", "list"], "stdout": "[]"},
            {"match": ["issue", "list"], "stderr": "B broke", "exit": 1},
            {"match": ["issue", "view"], "stderr": "C broke", "exit": 1},
        ]}, GHTEST_SLOW="list", GHTEST_SLEEP="0.4")   # the third fails first in time
        self.assertEqual(self.problem([["pr", "list"], ["issue", "list"], ["issue", "view"]]),
                         "gh issue list failed: B broke")

    def test_describe_can_be_replaced(self):
        self.scenario({"calls": [{"match": ["pr"], "stderr": "nope", "exit": 2}]})
        msg = self.problem([["pr", "list", "--json", "x"]], describe=lambda args: "custom<" + "|".join(args) + ">")
        self.assertEqual(msg, "custom<pr|list|--json|x> failed: nope")
        self.scenario({"default": {"stdout": "[]"}}, GHTEST_SLOW="pr", GHTEST_SLEEP="20")
        msg = self.problem([["pr", "view"]], budget_seconds=2, wait=0.5, describe=lambda args: args[-1].upper())
        self.assertEqual(msg, "VIEW timed out after 2 s")

    def test_a_gh_that_cannot_start_is_one_problem(self):
        gh, cli = lib("gh"), lib("cli")
        self.scenario({"default": {"stdout": "[]"}})
        missing = os.path.join(self.tmp, "no-such-gh")
        with self.assertRaises(cli.Problem) as caught:
            gh.GhBatch(missing, [["pr", "list"]], self.tmp, time.monotonic() + 5, budget_seconds=6)
        self.assertEqual(str(caught.exception), f"cannot run gh ({missing}): No such file or directory")

    def test_close_stops_calls_still_running(self):
        self.scenario({"default": {"stdout": "[]"}}, GHTEST_SLOW="pr", GHTEST_SLEEP="30")
        b = self.batch([["pr", "list"], ["pr", "view"]])
        deadline = time.monotonic() + 5
        while len(self.lines(self.wraplog)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = [c["pid"] for c in self.lines(self.wraplog)]
        self.assertEqual(len(pids), 2)
        started = time.monotonic()
        b.close()
        self.assertLess(time.monotonic() - started, 3)
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"gh call {pid} still running after close()"):
                os.kill(pid, 0)

    def test_problem_is_the_one_from_cli(self):
        self.assertIs(lib("gh").Problem, lib("cli").Problem)


# ---------------------------------------------------------------- the small helpers
class HelperTests(unittest.TestCase):
    def test_gh_env(self):
        self.assertEqual(dict(lib("gh").GH_ENV), GH_ENV_EXPECTED)

    def test_describe_list_call(self):
        cases = [
            (["pr", "list", "--json", "number", "-R", "o/r"], "gh pr list"),
            (["issue", "list", "--label", "status:needs-input", "--json", "a"], "gh issue list --label status:needs-input"),
            (["issue", "list", "--state", "open", "--label", "epic", "--label", "bug"], "gh issue list --label epic"),
            (["issue", "list", "--json", "a", "--label"], "gh issue list"),   # --label last: nothing follows it
            (["api", "graphql", "-f", "query=x"], "gh api graphql"),
            (["search", "prs", "--label", "x y"], "gh search prs --label x y"),
            (["status"], "gh status"),
        ]
        gh = lib("gh")
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(gh.describe_list_call(args), expected)

    def test_repo_slug(self):
        cases = [
            # github.com: OWNER/REPO
            ("git@github.com:acme/widget.git", "acme/widget"),
            ("git@github.com:acme/widget", "acme/widget"),
            ("github.com:Acme/Widget.git", "Acme/Widget"),
            ("https://github.com/acme/widget.git", "acme/widget"),
            ("https://github.com/acme/widget", "acme/widget"),
            ("https://github.com/acme/widget/", "acme/widget"),
            ("  https://www.github.com/Owner/Repo.git\n", "Owner/Repo"),
            ("https://GitHub.COM/o/r", "o/r"),
            ("https://user:tok@github.com/o2/r2.git", "o2/r2"),
            ("ssh://git@github.com/acme/tool.git", "acme/tool"),
            ("ssh://git@github.com:22/acme/tool.git", "acme/tool"),
            ("git://github.com/a/b", "a/b"),
            # another host: HOST/OWNER/REPO
            ("git@gitlab.com:grp/proj.git", "gitlab.com/grp/proj"),
            ("https://ghe.example.com/team/tool.git", "ghe.example.com/team/tool"),
            ("https://ghe.example.com:8443/team/tool", "ghe.example.com/team/tool"),
            ("ssh://git@ghe.corp:2222/t/x.git", "ghe.corp/t/x"),
            # not a repo gh can name
            ("/Users/someone/src/repo", None),
            ("../relative/repo", None),
            ("file:///Users/someone/repo.git", None),
            ("file://host/o/r", None),
            ("https://github.com/acme/group/widget.git", None),
            ("https://github.com/acme", None),
            ("git@github.com:acme/.git", None),
            ("", None),
        ]
        gh = lib("gh")
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(gh.repo_slug(url), expected)

    def test_gh_exe(self):
        gh = lib("gh")
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            self.assertEqual(gh.gh_exe(), "gh")
        for value in ("/opt/bin/gh-two", "mygh", "/nowhere/gh"):   # taken as is, found or not
            with self.subTest(value=value), mock.patch.dict(os.environ, clean_env(HARNESS_GH=value), clear=True):
                self.assertEqual(gh.gh_exe(), value)

    def test_gh_binary(self):
        gh = lib("gh")
        root = tempfile.mkdtemp(prefix="ghbin-", dir=W["tmp"])
        onpath, empty = os.path.join(root, "onpath"), os.path.join(root, "empty")
        os.makedirs(onpath)
        os.makedirs(empty)
        path_gh = executable(os.path.join(onpath, "gh"), "#!/bin/sh\nexit 0\n")
        other = executable(os.path.join(onpath, "othergh"), "#!/bin/sh\nexit 0\n")
        direct = executable(os.path.join(root, "direct-gh"), "#!/bin/sh\nexit 0\n")
        plain = os.path.join(root, "not-executable")
        with open(plain, "w") as fh:
            fh.write("#!/bin/sh\n")
        cases = [
            ({"PATH": onpath}, path_gh),                                   # gh on PATH
            ({"PATH": empty}, None),                                       # no gh on PATH
            ({"PATH": onpath, "HARNESS_GH": "othergh"}, other),            # a bare name is looked up on PATH
            ({"PATH": empty, "HARNESS_GH": "othergh"}, None),
            ({"PATH": empty, "HARNESS_GH": direct}, direct),               # a path is taken as is
            ({"PATH": onpath, "HARNESS_GH": plain}, None),                 # ... when it is executable
            ({"PATH": onpath, "HARNESS_GH": os.path.join(root, "gone")}, None),
            ({"PATH": onpath, "HARNESS_GH": root}, None),                  # a directory is not gh
        ]
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, clean_env(**env), clear=True):
                self.assertEqual(gh.gh_binary(), expected)


# ---------------------------------------------------------------- exists once
def python_sources():
    """Every Python source in the plugin: lib/ and hooks/ .py files and the python tools in bin/."""
    out = sorted(LIB.rglob("*.py")) + sorted(HOOKS.glob("*.py"))
    for path in sorted(BIN.iterdir()):
        if path.is_file():
            with open(path, "rb") as fh:
                if b"python" in fh.readline():
                    out.append(path)
    return out


def fallback_nodes(tree) -> set:
    """ids of the nodes in an except branch of a top-level try that imports harness_lib (the
    one place session_brief.py may keep a fallback copy)."""
    skip = set()
    for node in tree.body:
        if isinstance(node, ast.Try) and any(
                isinstance(n, ast.ImportFrom) and (n.module or "").startswith("harness_lib") for n in node.body):
            for handler in node.handlers:
                skip.update(id(n) for n in ast.walk(handler))
    return skip


def definitions(path: Path, names, *, allow_fallback=False):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = fallback_nodes(tree) if allow_fallback else set()
    return [(node.name, node.lineno) for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in names and id(node) not in skip]


def strings_and_keywords(node) -> set:
    found = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            found.add(n.value)
        elif isinstance(n, ast.keyword) and n.arg:
            found.add(n.arg)
    return found


class ExistsOnceTests(unittest.TestCase):
    def test_each_moved_name_is_defined_once_in_gh_py(self):
        self.assertTrue(GH_PY.is_file(), f"{GH_PY} does not exist")
        for name in MOVED + ("gh_exe",):
            with self.subTest(name=name):
                where = []
                for path in python_sources():
                    allow = path == SESSION_BRIEF and name == "gh_binary"
                    where += [f"{path.relative_to(PLUGIN)}:{line}"
                              for _, line in definitions(path, (name,), allow_fallback=allow)]
                self.assertEqual(len(where), 1, f"{name} defined at {where}")
                self.assertTrue(where[0].startswith("lib/harness_lib/gh.py:"), where)

    def test_standup_and_digest_keep_no_copy(self):
        """No def of the moved helpers or of today's `describe`, no class with GhBatch's
        results/close shape, no GH environment and no HARNESS_GH-then-which lookup of their own."""
        for path in (STANDUP, DIGEST):
            with self.subTest(tool=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                self.assertEqual(definitions(path, MOVED + ("describe", "gh_exe", "gh_command")), [])
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                        self.assertFalse({"results", "close"} <= methods,
                                         f"class {node.name} (line {node.lineno}) is a GhBatch copy")
                    if isinstance(node, ast.FunctionDef):
                        seen = strings_and_keywords(node)
                        self.assertFalse(seen & set(GH_ENV_EXPECTED),
                                         f"{node.name} (line {node.lineno}) builds its own GH environment")
                        calls_which = any(isinstance(n, ast.Attribute) and n.attr == "which" for n in ast.walk(node))
                        self.assertFalse(calls_which and "HARNESS_GH" in seen,
                                         f"{node.name} (line {node.lineno}) is a gh_binary copy")
                        self.assertNotIn("www.github.com", seen, f"{node.name} (line {node.lineno}) is a repo_slug copy")

    def test_session_brief_keeps_no_gh_command_or_gh_environment(self):
        tree = ast.parse(SESSION_BRIEF.read_text(encoding="utf-8"), filename=str(SESSION_BRIEF))
        skip = fallback_nodes(tree)
        self.assertEqual(definitions(SESSION_BRIEF, ("gh_command", "gh_binary", "gh_exe"), allow_fallback=True), [])
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.keyword) and node.arg in GH_ENV_EXPECTED:
                self.fail(f"line {node.value.lineno}: session_brief sets {node.arg} itself")
            if isinstance(node, ast.Constant) and node.value in GH_ENV_EXPECTED:
                self.fail(f"line {node.lineno}: session_brief names {node.value} itself")
        imported = {alias.name for node in tree.body if isinstance(node, ast.Try)
                    for stmt in node.body if isinstance(stmt, ast.ImportFrom) and stmt.module == "harness_lib.gh"
                    for alias in stmt.names}
        self.assertTrue({"gh_binary", "GH_ENV"} <= imported,
                        "session_brief imports gh_binary and GH_ENV from harness_lib.gh in its lib try")


# ---------------------------------------------------------------- the tools use the lib
def git_repo(root: str, origin: str) -> str:
    repo = os.path.join(root, "repo")
    env = clean_env(GIT_CONFIG_NOSYSTEM="1", HOME=root)
    subprocess.run([GIT, "-c", "init.defaultBranch=main", "init", "-q", repo], env=env, check=True, capture_output=True)
    subprocess.run([GIT, "-C", repo, "remote", "add", "origin", origin], env=env, check=True, capture_output=True)
    return repo


class Recorder:
    """Stands in for GhBatch in a tool: records how it was built; results() gives `rows` or raises `error`."""

    def __init__(self, rows, error=None):
        self.made, self.rows, self.error = [], rows, error

    def __call__(self, gh, calls, cwd, deadline, *args, **kwargs):
        self.made.append({"gh": gh, "calls": calls, "cwd": cwd, "args": args, "kwargs": kwargs})
        rec = self

        class Batch:
            def results(self):
                if rec.error is not None:
                    raise rec.error
                return [list(r) for r in rec.rows]

            def close(self):
                pass

        return Batch()


@unittest.skipIf(GIT is None, "git not found")
class ToolsUseTheLibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tool-", dir=W["tmp"])
        self.repo = git_repo(self.tmp, "git@github.com:acme/widget.git")
        patcher = mock.patch.dict(os.environ, clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_standup_and_digest_bind_the_libs_objects(self):
        gh = lib("gh")
        for path, name in ((STANDUP, "harness_standup_gh_t"), (DIGEST, "harness_digest_gh_t")):
            tool = load(path, name)
            for attr in MOVED:
                with self.subTest(tool=path.name, name=attr):
                    self.assertIs(getattr(tool, attr, None), getattr(gh, attr))

    def slug_recorder(self, value):
        seen = []

        def repo_slug(url):
            seen.append(url)
            return value
        return seen, repo_slug

    def test_standup_passes_its_own_gh_budget_and_slug(self):
        tool = load(STANDUP, "harness_standup_gh_budget")
        for budget, slug in ((9, "rec/one"), (2, "rec/two")):
            with self.subTest(budget=budget):
                rec = Recorder([[], [], [], []])
                urls, tool.repo_slug = self.slug_recorder(slug)
                tool.GhBatch, tool.GH_BUDGET = rec, budget
                tool.gh_binary = lambda: "/recorded/gh"
                out = tool.board_report({"name": "repo", "path": self.repo}, time.time())
                self.assertIsNone(out["board_error"], out)
                self.assertEqual(len(rec.made), 1)
                made = rec.made[0]
                self.assertEqual(made["gh"], "/recorded/gh")
                self.assertEqual(made["kwargs"].get("budget_seconds"), budget, made)
                self.assertEqual([c[-2:] for c in made["calls"]], [["-R", slug]] * 4)
                self.assertEqual([u.strip() for u in urls], ["git@github.com:acme/widget.git"])

    def test_digest_passes_its_own_gh_budget_and_slug(self):
        tool = load(DIGEST, "harness_digest_gh_budget")
        for budget, slug in ((9, "rec/one"), (3, "rec/two")):
            with self.subTest(budget=budget):
                rec = Recorder([[], [], [], []])
                urls, tool.repo_slug = self.slug_recorder(slug)
                tool.GhBatch, tool.GH_BUDGET = rec, budget
                out = tool.project_digest({"name": "repo", "path": self.repo}, "/recorded/gh", time.time())
                self.assertIsNone(out["error"], out)
                self.assertEqual(out["repo"], slug)
                self.assertEqual(len(rec.made), 1)
                self.assertEqual(rec.made[0]["gh"], "/recorded/gh")
                self.assertEqual(rec.made[0]["kwargs"].get("budget_seconds"), budget, rec.made[0])
                self.assertEqual(urls, ["git@github.com:acme/widget.git"])

    def test_the_libs_problem_is_the_tools_one_error_line(self):
        cli = lib("cli")
        for message in ("gh pr list failed: HTTP 502", "gh issue list timed out after 4 s"):
            with self.subTest(message=message):
                standup = load(STANDUP, "harness_standup_gh_problem")
                standup.GhBatch = Recorder([], error=cli.Problem(message))
                standup.gh_binary = lambda: "/recorded/gh"
                out = standup.board_report({"name": "repo", "path": self.repo}, time.time())
                self.assertEqual(out["board_error"], message)
                digest = load(DIGEST, "harness_digest_gh_problem")
                digest.GhBatch = Recorder([], error=cli.Problem(message))
                out = digest.project_digest({"name": "repo", "path": self.repo}, "/recorded/gh", time.time())
                self.assertEqual(out["error"], message)


class SessionBriefUsesTheLibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brief-", dir=W["tmp"])
        patcher = mock.patch.dict(os.environ, clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sb = load(SESSION_BRIEF, "session_brief_gh_t")

    def test_binds_the_libs_gh_binary_and_gh_env(self):
        gh = lib("gh")
        self.assertIs(getattr(self.sb, "gh_binary", None), gh.gh_binary)
        self.assertIs(getattr(self.sb, "GH_ENV", None), gh.GH_ENV)

    def test_board_part_finds_gh_through_gh_binary(self):
        calls = []

        def recorder():
            calls.append(1)
            return None
        self.sb.gh_binary = recorder
        self.assertEqual(self.sb.board_part(Path(self.tmp)), [])
        self.assertEqual(calls, [1])

    def test_run_side_by_side_sets_gh_env(self):
        show = [sys.executable, "-c",
                "import os; print(' '.join(str(os.environ.get(k)) for k in "
                "('GH_PROMPT_DISABLED', 'GH_NO_UPDATE_NOTIFIER', 'NO_COLOR', 'GHTEST_MARK')))"]
        self.assertEqual(self.sb.run_side_by_side([show], Path(self.tmp), 10), ["1 1 1 None\n"])
        for mark in ("m1", "other"):
            with self.subTest(mark=mark):
                self.sb.GH_ENV = {"GHTEST_MARK": mark}
                self.assertEqual(self.sb.run_side_by_side([show], Path(self.tmp), 10), [f"None None None {mark}\n"])


if __name__ == "__main__":
    unittest.main()
