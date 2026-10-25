"""Tests for the sequential gh wrappers in harness_lib/gh.py (ticket #94, T16 of
docs/proposals/refactor.md, section 10): describe, gh_run, gh and gh_json, which harness-board and
harness-pr adopt, and the two-line adapter harness-work keeps over them.

The signatures these tests fix (everything after `cwd` is passed by keyword here):
- describe(args) -> str: the union of board's and pr's describe. For `api`, "gh api " plus the
  first argument after `api` that is "graphql" or holds "/" and does not start with "-" ("" when
  none); otherwise "gh " plus up to three words before the first flag-shaped argument.
- gh_run(args, cwd, stdin=None, timeout=60, *, errors="replace", env=None, describe=describe,
  texts=None) -> subprocess.CompletedProcess with str stdout/stderr (decoded utf-8 with
  `errors`), never raising on a non-zero exit. Runs gh_exe() (HARNESS_GH, else `gh`) with
  exactly `args`, in `cwd` (None: the current directory), stdin from `stdin` or /dev/null when
  None, environment os.environ plus `env` (None: {"GH_PROMPT_DISABLED": "1"}). Problem on a gh
  that is missing (`not_found`), cannot start (`cannot_run`) or times out (`timed_out`).
- gh(args, cwd, stdin=None, timeout=60, *, pick=first_line, errors="replace", env=None,
  describe=describe, texts=None, run=None) -> str: gh_run's stdout; on a non-zero exit Problem
  (`failed`, why = pick(stderr), called once, or "exit N"). With `run`, the call is
  run(args, cwd, stdin, timeout) (those four, positionally) in place of gh_run.
- gh_json(args, cwd, *, pick=first_line, empty_ok=False, via=None, **kw) -> the decoded JSON, where
  kw (stdin, timeout, errors, env, describe, texts) also reaches gh; empty stdout is None when
  empty_ok, otherwise, like any non-JSON output, Problem (`not_json`, excerpt = pick(stdout)[:80],
  pick called once). With `via`, stdout is via(args, cwd) (just those two) in place of gh's.
  `run` and `via` let a tool's adapter resolve its own gh_run / gh at call time (the existing
  test_harness_lib_cli.py ToolsRunTheLib tests patch them on the tool), and `pick` is the tool's
  own first_line binding (those tests spy on it).
- texts: a mapping laid over the defaults (keys given replace those keys only) of str.format
  templates, formatted with exe (gh_exe()), call (describe(args)), args (the list), timeout,
  why (strerror for cannot_run; pick(stderr) or "exit N" for failed) and excerpt
  (first_line(stdout)[:80]). The defaults are board's and pr's texts today:
    not_found  "gh not found ({exe}); install it with `brew install gh`, then `gh auth login`"
    cannot_run "cannot run gh ({exe}): {why}"
    timed_out  "{call} timed out after {timeout} s"
    failed     "{call} failed: {why}"
    not_json   "{call} printed something other than JSON: {excerpt}"

Adoption: harness-board and harness-pr define no describe or gh_run and bind the lib's (board
describe; pr describe and gh_run). Their gh and gh_json stay, as the existing suite calls them (board
repo-first: gh(repo, args, stdin=None), gh_json(repo, args); pr gh(args, cwd, stdin=None, timeout=..),
gh_json(args, cwd)), but only as adapters of at most two statements, with no try and no nested def,
that call the lib's gh or gh_json and run nothing themselves. Neither tool launches anything but git,
a sibling harness tool or claude, or names HARNESS_GH, GH_PROMPT_DISABLED or the install hint, and
every gh call board's and pr's code makes goes through the lib's wrappers (the poison tests). harness-work keeps
gh_json(repo, args) as an adapter of at most two statements that calls the lib's gh_run, gh or
gh_json and runs nothing itself, and every text it gives stays as today (the guards below pass
before the change): `<exe> not found`, `cannot run <exe>: <strerror>`, `<exe> <args[0]> <args[1]>
timed out after <GH_TIMEOUT> s` (GH_TIMEOUT read at call time), `gh <args[0]> <args[1]> failed:
<LAST stderr line or exit N>`, `gh <args[0]> <args[1]> printed something other than JSON` (no
excerpt); empty stdout is None; cwd is repo.main.

Expected values are written out here from today's copies in the tools, never computed by the
code under test.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_gh_wrappers.py" -v
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import importlib
import io
import importlib.util
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tools must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
LIB = PLUGIN / "lib"
GH_PY = LIB / "harness_lib" / "gh.py"
FAKEGH = TESTS / "fakegh.py"
BOARD = BIN / "harness-board"
PR = BIN / "harness-pr"
WORK = BIN / "harness-work"

WRAPPERS = ("describe", "gh_run", "gh", "gh_json")
GH_ENV_EXPECTED = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
INSTALL_HINT = "; install it with `brew install gh`, then `gh auth login`"

# The fake gh: records argv, cwd, what its stdin is and a few environment variables; sleeps when
# its argv holds GHTEST_SLOW; prints raw bytes when GHTEST_RAW names a file; else becomes fakegh.py.
WRAPPER = '''#!{python}
import json, os, stat, sys, time
WATCH = ("GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER", "NO_COLOR", "GHTEST_INHERITED", "GHTEST_MARK")
def stdin_kind():
    try:
        st = os.fstat(0)
    except OSError:
        return "closed"
    if stat.S_ISFIFO(st.st_mode):
        return "pipe"
    try:
        dn = os.stat(os.devnull)
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == dn.st_rdev:
            return "devnull"
    except OSError:
        pass
    return "other"
log = os.environ.get("GHTEST_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin_kind(),
                             "env": {{k: os.environ.get(k) for k in WATCH}}}}) + "\\n")
slow = os.environ.get("GHTEST_SLOW")
if slow and slow in sys.argv[1:]:
    time.sleep(float(os.environ.get("GHTEST_SLEEP", "0")))
raw = os.environ.get("GHTEST_RAW")
if raw:
    with open(raw, encoding="utf-8") as fh:
        spec = json.load(fh)
    sys.stdout.buffer.write(bytes.fromhex(spec.get("stdout_hex", "")))
    sys.stderr.buffer.write(bytes.fromhex(spec.get("stderr_hex", "")))
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(spec.get("exit", 0))
os.execv({python!r}, [{python!r}, {fakegh!r}] + sys.argv[1:])
'''

POISON = '''#!{python}
import os, sys
mark = os.environ.get("GHTEST_POISON_MARK")
if mark:
    with open(mark, "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.stderr.write("poison gh: ran without harness_lib.gh\\n")
sys.exit(1)
'''

W: dict = {}


def setUpModule():
    W["tmp"] = tempfile.mkdtemp(prefix="test-harness-lib-gh-wrappers-")
    fakebin = os.path.join(W["tmp"], "fakebin")
    os.makedirs(fakebin)
    W["gh"] = executable(os.path.join(fakebin, "gh"),
                         WRAPPER.format(python=sys.executable, fakegh=str(FAKEGH)))
    W["nogh"] = os.path.join(W["tmp"], "empty-path")      # a PATH directory with no gh in it
    os.makedirs(W["nogh"])
    # HARNESS_GH in the routing tests: a gh any copy of the old wrappers would run (they read HARNESS_GH
    # themselves), while the lib finds the real fake through its gh_exe(), patched there.
    W["poison"] = executable(os.path.join(fakebin, "gh-poison"), POISON.format(python=sys.executable))
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    # #187: gh_run logs each gh call under ~/.claude/harness/gh; the calls made in this process (and the tools
    # started with clean_env(), which copies it) go to a temporary HOME, never the real one.
    W["home"] = os.path.join(W["tmp"], "home")
    os.makedirs(W["home"])
    W["home_patch"] = mock.patch.dict(os.environ, {"HOME": W["home"]})
    W["home_patch"].start()


def tearDownModule():
    if "home_patch" in W:
        W.pop("home_patch").stop()
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
    """os.environ without the GH variables, HARNESS_GH or the fakes' variables, plus `extra`."""
    env = {k: v for k, v in os.environ.items()
           if k not in GH_ENV_EXPECTED and not k.startswith(("HARNESS_", "FAKEGH_", "GHTEST_"))}
    env.update(extra)
    return env


def real(path):
    return os.path.realpath(path)


class Case(unittest.TestCase):
    """A test with the fake gh as HARNESS_GH; scenario() scripts fakegh, raw() prints bytes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="case-", dir=W["tmp"])
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.ghlog = os.path.join(self.tmp, "gh.log")
        self.wraplog = os.path.join(self.tmp, "wrapper.log")
        self.gh = lib("gh")
        self.Problem = lib("cli").Problem
        self.first_line = lib("text").first_line
        self.last_line = lib("text").last_line

    def scenario(self, data=None, **extra):
        with open(self.scenario_path, "w", encoding="utf-8") as fh:
            json.dump(data if data is not None else {"default": {"stdout": ""}}, fh)
        for leftover in (self.scenario_path + ".state.json", self.ghlog, self.wraplog):
            if os.path.exists(leftover):
                os.remove(leftover)
        env = clean_env(**dict({"HARNESS_GH": W["gh"], "FAKEGH_SCENARIO": self.scenario_path,
                                "FAKEGH_LOG": self.ghlog, "GHTEST_LOG": self.wraplog}, **extra))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw(self, stdout=b"", stderr=b"", exit=0, **extra):
        path = os.path.join(self.tmp, "raw.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stdout_hex": stdout.hex(), "stderr_hex": stderr.hex(), "exit": exit}, fh)
        self.scenario(GHTEST_RAW=path, **extra)

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def problem(self, fn, *args, **kwargs):
        with self.assertRaises(self.Problem) as caught:
            fn(*args, **kwargs)
        return str(caught.exception)


# ---------------------------------------------------------------- describe
class DescribeTests(unittest.TestCase):
    def setUp(self):
        self.describe = lib("gh").describe

    def check(self, table):
        for args, expected in table:
            with self.subTest(args=args):
                self.assertEqual(self.describe(list(args)), expected)

    def test_every_api_call_board_and_pr_make_is_described_as_today(self):
        """The call shapes at board:431, 637, 641, 796 and pr:665, 782, 896, 1173, 1266, over two repositories."""
        for slug, n in (("acme/widget", 3), ("other-owner/tool.js", 41)):
            self.check([
                (["api", f"repos/{slug}/issues/{n}/sub_issues?per_page=100"],
                 f"gh api repos/{slug}/issues/{n}/sub_issues?per_page=100"),
                (["api", f"repos/{slug}/issues/{n}", "--jq", ".id"], f"gh api repos/{slug}/issues/{n}"),
                (["api", "-X", "POST", f"repos/{slug}/issues/{n}/sub_issues", "-F", f"sub_issue_id={n + 1000}"],
                 f"gh api repos/{slug}/issues/{n}/sub_issues"),
                (["api", "user", "--jq", ".login"], "gh api "),
                (["api", "-X", "POST", f"repos/{slug}/pulls/{n}/reviews", "--input", "-"],
                 f"gh api repos/{slug}/pulls/{n}/reviews"),
                (["api", "--paginate", f"repos/{slug}/pulls/{n}/comments?per_page=100"],
                 f"gh api repos/{slug}/pulls/{n}/comments?per_page=100"),
                (["api", "graphql", "-f", "query=query($owner: String!) { x }", "-f", f"owner={slug.split('/')[0]}",
                  "-f", f"repo={slug.split('/')[1]}", "-F", f"number={n}"], "gh api graphql"),
                (["api", f"repos/{slug}/issues/{n}/parent"], f"gh api repos/{slug}/issues/{n}/parent"),
                (["api", f"repos/{slug}/pulls/{n}/reviews", "--paginate", "--slurp"],
                 f"gh api repos/{slug}/pulls/{n}/reviews"),
            ])

    def test_the_union_names_graphql_and_skips_flag_shaped_paths(self):
        """Neither tool's copy alone gives all of these: board's misses graphql, pr's takes a flag with a slash."""
        self.check([
            (["api", "graphql"], "gh api graphql"),
            (["api", "-H", "X-Test: 1", "graphql", "-f", "q=a/b"], "gh api graphql"),
            (["api", "--header=Accept: application/vnd.github+json", "repos/acme/widget/pulls"],
             "gh api repos/acme/widget/pulls"),
            (["api", "-Faccept=a/b", "repos/o/r/issues/9"], "gh api repos/o/r/issues/9"),
            (["api", "--input=/tmp/x.json", "user"], "gh api "),
            (["api", "-X", "GET", "rate_limit"], "gh api "),
            (["api"], "gh api "),
        ])

    def test_other_calls_are_up_to_three_words_before_the_first_flag(self):
        self.check([
            (["issue", "view", "12", "--json", "labels"], "gh issue view 12"),
            (["issue", "view", "7", "--json", "number,title,body"], "gh issue view 7"),
            (["label", "create", "epic", "--color", "5319E7", "--description", "x", "--force"],
             "gh label create epic"),
            (["issue", "create", "--title", "T", "--body-file", "/tmp/b"], "gh issue create"),
            (["issue", "comment", "31", "--body-file", "-"], "gh issue comment 31"),
            (["issue", "edit", "5", "--add-blocked-by", "4"], "gh issue edit 5"),
            (["issue", "list", "--state", "open", "--label", "status:ready"], "gh issue list"),
            (["repo", "view", "--json", "nameWithOwner"], "gh repo view"),
            (["project", "field-list", "31", "--owner", "acme-org", "--format", "json"], "gh project field-list 31"),
            (["pr", "list", "--head", "t/94-x", "--state", "open"], "gh pr list"),
            (["pr", "merge", "88", "-R", "acme/widget", "--squash", "--delete-branch"], "gh pr merge 88"),
            (["run", "view", "123456", "--log-failed"], "gh run view 123456"),
            (["pr", "ready", "9"], "gh pr ready 9"),
            (["a", "b", "c", "d", "e"], "gh a b c"),
            (["--version"], "gh "),
            ([], "gh "),
        ])


# ---------------------------------------------------------------- gh_run
class GhRunTests(Case):
    def test_returns_the_completed_process_and_never_raises_on_a_failed_exit(self):
        for args, out, err, code in ((["pr", "view", "7"], "seven\n", "", 0),
                                     (["issue", "view", "12"], "partial\n", "HTTP 404: Not Found\nmore\n", 4),
                                     (["api", "user"], "", "boom\n", 1)):
            with self.subTest(args=args):
                self.scenario({"calls": [{"match": args, "stdout": out, "stderr": err, "exit": code}]})
                done = self.gh.gh_run(args, self.tmp)
                self.assertIsInstance(done, subprocess.CompletedProcess)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (code, out, err))
                self.assertEqual([c["argv"] for c in self.lines(self.ghlog)], [args])

    def test_runs_harness_gh_with_exactly_the_args(self):
        for args in (["issue", "view", "12", "--json", "labels"], ["api", "-X", "POST", "repos/o/r/issues/1", "-f", "a=b c"]):
            with self.subTest(args=args):
                self.scenario()
                self.gh.gh_run(args, self.tmp)
                self.assertEqual([c["argv"] for c in self.lines(self.wraplog)], [args])

    def test_runs_in_cwd_or_the_current_directory_when_none(self):
        other = tempfile.mkdtemp(prefix="other-", dir=self.tmp)
        for cwd, expected in ((self.tmp, self.tmp), (other, other), (None, os.getcwd())):
            with self.subTest(cwd=cwd):
                self.scenario()
                self.gh.gh_run(["pr", "list"], cwd)
                self.assertEqual([real(c["cwd"]) for c in self.lines(self.wraplog)], [real(expected)])

    def test_stdin_is_the_text_given_or_dev_null(self):
        for text in ("payload one\n", json.dumps({"body": "x" * 50, "n": [1, 2]})):
            with self.subTest(stdin=text[:12]):
                self.scenario()
                self.gh.gh_run(["issue", "comment", "3", "--body-file", "-"], self.tmp, stdin=text)
                self.assertEqual([c["stdin"] for c in self.lines(self.wraplog)], ["pipe"])
                self.assertEqual([c["stdin"] for c in self.lines(self.ghlog)], [text])
        self.scenario()
        self.gh.gh_run(["pr", "list"], self.tmp)
        self.assertEqual([c["stdin"] for c in self.lines(self.wraplog)], ["devnull"])

    def test_default_environment_is_the_callers_with_prompts_disabled(self):
        for mark in ("from-the-caller", "another"):
            with self.subTest(mark=mark):
                self.scenario(GHTEST_INHERITED=mark)
                self.gh.gh_run(["pr", "list"], self.tmp)
                env = self.lines(self.wraplog)[0]["env"]
                self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
                self.assertEqual(env["GHTEST_INHERITED"], mark)

    def test_env_is_laid_over_the_callers_environment(self):
        for extra in ({"GHTEST_MARK": "m1"}, {"GHTEST_MARK": "m2", "NO_COLOR": "1"}, dict(GH_ENV_EXPECTED)):
            with self.subTest(env=extra):
                self.scenario(GHTEST_INHERITED="kept")
                self.gh.gh_run(["pr", "list"], self.tmp, env=extra)
                env = self.lines(self.wraplog)[0]["env"]
                self.assertEqual(env["GHTEST_INHERITED"], "kept")
                for key, value in extra.items():
                    self.assertEqual(env[key], value, key)
        self.scenario()
        self.gh.gh_run(["pr", "list"], self.tmp, env=self.gh.GH_ENV)
        env = self.lines(self.wraplog)[0]["env"]
        self.assertEqual({k: env[k] for k in GH_ENV_EXPECTED}, GH_ENV_EXPECTED)

    def test_a_missing_gh_is_the_gh_not_found_text(self):
        for name in ("gh-missing", "nowhere/gh"):
            missing = os.path.join(self.tmp, name)
            with self.subTest(exe=missing):
                self.scenario(HARNESS_GH=missing)
                self.assertEqual(self.problem(self.gh.gh_run, ["pr", "list"], self.tmp),
                                 f"gh not found ({missing}){INSTALL_HINT}")
        self.scenario(PATH=W["nogh"])
        os.environ.pop("HARNESS_GH")
        self.assertEqual(self.problem(self.gh.gh_run, ["issue", "view", "3"], self.tmp), f"gh not found (gh){INSTALL_HINT}")

    def test_a_gh_that_cannot_start_names_the_os_error(self):
        blocked = os.path.join(self.tmp, "gh-not-executable")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(blocked, 0o644)
        self.scenario(HARNESS_GH=blocked)
        self.assertEqual(self.problem(self.gh.gh_run, ["pr", "list"], self.tmp),
                         f"cannot run gh ({blocked}): Permission denied")

    def test_a_timeout_names_the_call_and_the_timeout_it_was_given(self):
        for args, timeout, expected in ((["pr", "view", "7", "--json", "x"], 0.4, "gh pr view 7 timed out after 0.4 s"),
                                        (["api", "graphql", "-f", "q=1"], 0.7, "gh api graphql timed out after 0.7 s")):
            with self.subTest(args=args):
                self.scenario(GHTEST_SLOW=args[1], GHTEST_SLEEP="5")
                self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, timeout=timeout), expected)

    def test_a_timeout_uses_the_describe_given(self):
        self.scenario(GHTEST_SLOW="view", GHTEST_SLEEP="5")
        msg = self.problem(self.gh.gh_run, ["issue", "view", "12"], self.tmp, timeout=0.4,
                           describe=lambda a: "gh " + " ".join(a[:2]))
        self.assertEqual(msg, "gh issue view timed out after 0.4 s")

    def test_the_default_timeout_is_sixty_seconds(self):
        for fn in (self.gh.gh_run, self.gh.gh):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(inspect.signature(fn).parameters["timeout"].default, 60)

    def test_output_is_decoded_as_utf8_with_the_errors_given(self):
        data = "café ✓\n".encode() + b"bad \xe9\xff end\n"
        for errors, expected in (("replace", "café ✓\nbad �� end\n"),
                                 ("surrogateescape", "café ✓\nbad \udce9\udcff end\n")):
            with self.subTest(errors=errors):
                self.raw(stdout=data, stderr=b"warn \xff\n")
                done = self.gh.gh_run(["pr", "diff", "3"], self.tmp, errors=errors)
                self.assertEqual(done.stdout, expected)
                self.assertEqual(done.stderr, "warn �\n" if errors == "replace" else "warn \udcff\n")
        self.raw(stdout=data)
        self.assertEqual(self.gh.gh_run(["pr", "diff", "3"], self.tmp).stdout, "café ✓\nbad �� end\n")


# ---------------------------------------------------------------- gh
class GhTests(Case):
    def test_returns_each_calls_stdout(self):
        rows = [(["issue", "view", "12"], "twelve\n"), (["pr", "list"], "[1, 2]\n"),
                (["api", "user", "--jq", ".login"], "ryanje\n"), (["label", "create", "x"], "")]
        self.scenario({"calls": [{"match": a, "stdout": out} for a, out in rows]})
        for args, out in rows:
            with self.subTest(args=args):
                self.assertEqual(self.gh.gh(args, self.tmp), out)

    def test_a_failed_call_names_itself_with_ghs_first_stderr_line(self):
        table = [
            (["label", "create", "epic", "--color", "5319E7"], "HTTP 403: Resource not accessible\nmore\n",
             "gh label create epic failed: HTTP 403: Resource not accessible"),
            (["issue", "view", "12", "--json", "labels"], "\n  GraphQL: Could not resolve to an Issue  \nsecond\n",
             "gh issue view 12 failed: GraphQL: Could not resolve to an Issue"),
            (["api", "graphql", "-f", "q=1"], "gh: Bad credentials (HTTP 401)\n",
             "gh api graphql failed: gh: Bad credentials (HTTP 401)"),
            (["api", "-X", "POST", "repos/acme/widget/issues/5/sub_issues"], "HTTP 422: Validation Failed\nx\ny\n",
             "gh api repos/acme/widget/issues/5/sub_issues failed: HTTP 422: Validation Failed"),
        ]
        for args, err, expected in table:
            with self.subTest(args=args):
                self.scenario({"calls": [{"match": args[:2], "stderr": err, "exit": 1}]})
                self.assertEqual(self.problem(self.gh.gh, args, self.tmp), expected)

    def test_a_failed_call_without_stderr_gives_its_exit_status(self):
        for err, code in (("", 3), ("  \n\n", 7), ("", 1)):
            with self.subTest(code=code, err=err):
                self.scenario({"default": {"stderr": err, "exit": code}})
                self.assertEqual(self.problem(self.gh.gh, ["pr", "ready", "9"], self.tmp), f"gh pr ready 9 failed: exit {code}")

    def test_the_picker_chooses_the_line(self):
        err = "first problem\nmiddle\nlast problem\n\n"
        self.scenario({"default": {"stderr": err, "exit": 2}})
        args = ["issue", "view", "4"]
        self.assertEqual(self.problem(self.gh.gh, args, self.tmp, pick=self.last_line),
                         "gh issue view 4 failed: last problem")
        self.assertEqual(self.problem(self.gh.gh, args, self.tmp, pick=lambda text: f"<{len(text)} chars>"),
                         f"gh issue view 4 failed: <{len(err)} chars>")
        self.assertEqual(self.problem(self.gh.gh, args, self.tmp, pick=lambda text: ""),
                         "gh issue view 4 failed: exit 2")

    def test_the_describe_given_names_the_failed_call(self):
        self.scenario({"default": {"stderr": "nope\n", "exit": 1}})
        msg = self.problem(self.gh.gh, ["issue", "view", "12", "--json", "x"], self.tmp,
                           describe=lambda a: "gh " + " ".join(a[:2]))
        self.assertEqual(msg, "gh issue view failed: nope")

    def test_stdin_cwd_env_timeout_and_errors_reach_the_call(self):
        other = tempfile.mkdtemp(prefix="other-", dir=self.tmp)
        self.raw(stdout=b"ok \xff\n")
        out = self.gh.gh(["issue", "comment", "3", "--body-file", "-"], other, stdin="hello\n",
                         env={"GHTEST_MARK": "g1"}, timeout=30, errors="surrogateescape")
        self.assertEqual(out, "ok \udcff\n")
        call = self.lines(self.wraplog)[0]
        self.assertEqual((real(call["cwd"]), call["stdin"], call["env"]["GHTEST_MARK"]), (real(other), "pipe", "g1"))
        self.scenario(GHTEST_SLOW="view", GHTEST_SLEEP="5")
        self.assertEqual(self.problem(self.gh.gh, ["run", "view", "1"], self.tmp, timeout=0.5),
                         "gh run view 1 timed out after 0.5 s")
        self.scenario()
        self.gh.gh(["pr", "list"], self.tmp)
        call = self.lines(self.wraplog)[0]
        self.assertEqual((call["stdin"], call["env"]["GH_PROMPT_DISABLED"]), ("devnull", "1"))

    def test_a_missing_gh_is_the_gh_not_found_text(self):
        missing = os.path.join(self.tmp, "no-gh-here")
        self.scenario(HARNESS_GH=missing)
        self.assertEqual(self.problem(self.gh.gh, ["pr", "list"], self.tmp), f"gh not found ({missing}){INSTALL_HINT}")


# ---------------------------------------------------------------- gh_json
class GhJsonTests(Case):
    def test_decodes_what_gh_printed(self):
        for value in ({"nameWithOwner": "acme/widget"}, [{"number": 1}, {"number": 2, "x": [None]}], 7, "s", None, []):
            with self.subTest(value=value):
                self.scenario({"default": {"stdout_json": value}})
                self.assertEqual(self.gh.gh_json(["repo", "view", "--json", "nameWithOwner"], self.tmp), value)

    def test_output_that_is_not_json_is_named_with_its_first_line_cut_at_80(self):
        long = "<html>" + "x" * 120
        for args, out, expected in (
                (["issue", "view", "12", "--json", "labels"], f"\n  {long}  \nmore\n",
                 f"gh issue view 12 printed something other than JSON: {long[:80]}"),
                (["api", "graphql", "-f", "q=1"], "not json\n", "gh api graphql printed something other than JSON: not json"),
                (["api", "repos/o/r/issues/3/sub_issues?per_page=100"], "{broken\n",
                 "gh api repos/o/r/issues/3/sub_issues?per_page=100 printed something other than JSON: {broken")):
            with self.subTest(args=args):
                self.scenario({"default": {"stdout": out}})
                self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp), expected)

    def test_empty_output_is_none_only_when_empty_ok(self):
        self.scenario({"default": {"stdout": ""}})
        msg = self.problem(self.gh.gh_json, ["pr", "view", "5"], self.tmp)
        self.assertTrue(msg.startswith("gh pr view 5 printed something other than JSON"), msg)
        self.assertIsNone(self.gh.gh_json(["pr", "view", "5"], self.tmp, empty_ok=True))
        self.scenario({"default": {"stdout_json": {"n": 5}}})
        self.assertEqual(self.gh.gh_json(["pr", "view", "5"], self.tmp, empty_ok=True), {"n": 5})

    def test_a_failed_call_uses_the_picker(self):
        self.scenario({"default": {"stderr": "top line\nbottom line\n", "exit": 1}})
        args = ["issue", "view", "8", "--json", "labels"]
        self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp), "gh issue view 8 failed: top line")
        self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp, pick=self.last_line),
                         "gh issue view 8 failed: bottom line")

    def test_keywords_reach_gh(self):
        other = tempfile.mkdtemp(prefix="other-", dir=self.tmp)
        self.scenario({"default": {"stdout_json": {"ok": True}}})
        self.assertEqual(self.gh.gh_json(["api", "repos/o/r/issues/1"], other, env={"GHTEST_MARK": "j2"}, timeout=30),
                         {"ok": True})
        call = self.lines(self.wraplog)[0]
        self.assertEqual((real(call["cwd"]), call["env"]["GHTEST_MARK"]), (real(other), "j2"))
        self.scenario({"default": {"stdout": "nope"}})
        self.assertEqual(self.problem(self.gh.gh_json, ["issue", "view", "3"], self.tmp,
                                      describe=lambda a: "gh " + " ".join(a[:2])),
                         "gh issue view printed something other than JSON: nope")
        self.scenario(GHTEST_SLOW="view", GHTEST_SLEEP="5")
        self.assertEqual(self.problem(self.gh.gh_json, ["pr", "view", "2"], self.tmp, timeout=0.4),
                         "gh pr view 2 timed out after 0.4 s")


# ---------------------------------------------------------------- texts: callers supply their own messages
class TextsTests(Case):
    CALLS = (["issue", "view", "12", "--json", "labels"], ["pr", "list", "--head", "t/9-x"])

    def blocked_gh(self):
        path = os.path.join(self.tmp, "gh-not-executable")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o644)
        return path

    def test_not_found_cannot_run_and_timed_out_take_the_callers_templates(self):
        texts = {"not_found": "{exe} missing for {call} [{args[0]}]",
                 "cannot_run": "{exe} blocked: {why} ({args[1]})",
                 "timed_out": "{exe} {args[0]} {args[1]} slow after {timeout} s / {call}"}
        for args in self.CALLS:
            call = {"issue": "gh issue view 12", "pr": "gh pr list"}[args[0]]
            with self.subTest(args=args, kind="not_found"):
                missing = os.path.join(self.tmp, f"no-gh-{args[0]}")
                self.scenario(HARNESS_GH=missing)
                for fn in (self.gh.gh_run, self.gh.gh, self.gh.gh_json):
                    self.assertEqual(self.problem(fn, args, self.tmp, texts=texts),
                                     f"{missing} missing for {call} [{args[0]}]")
            with self.subTest(args=args, kind="cannot_run"):
                blocked = self.blocked_gh()
                self.scenario(HARNESS_GH=blocked)
                self.assertEqual(self.problem(self.gh.gh, args, self.tmp, texts=texts),
                                 f"{blocked} blocked: Permission denied ({args[1]})")
            with self.subTest(args=args, kind="timed_out"):
                self.scenario(GHTEST_SLOW=args[1], GHTEST_SLEEP="5")
                self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp, timeout=0.4, texts=texts),
                                 f"{W['gh']} {args[0]} {args[1]} slow after 0.4 s / {call}")

    def test_failed_and_not_json_take_the_callers_templates(self):
        texts = {"failed": "{call} broke ({args[1]}): {why}", "not_json": "{exe}: {call} junk <{excerpt}>"}
        for args, call in zip(self.CALLS, ("gh issue view 12", "gh pr list")):
            with self.subTest(args=args):
                self.scenario({"default": {"stderr": "first\nlast one\n", "exit": 1}})
                self.assertEqual(self.problem(self.gh.gh, args, self.tmp, texts=texts),
                                 f"{call} broke ({args[1]}): first")
                self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp, pick=self.last_line, texts=texts),
                                 f"{call} broke ({args[1]}): last one")
                self.scenario({"default": {"stderr": "", "exit": 6}})
                self.assertEqual(self.problem(self.gh.gh, args, self.tmp, texts=texts), f"{call} broke ({args[1]}): exit 6")
                self.scenario({"default": {"stdout": f"\n<html> {args[0]} " + "y" * 100 + "\n"}})
                excerpt = (f"<html> {args[0]} " + "y" * 100)[:80]
                self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp, texts=texts),
                                 f"{W['gh']}: {call} junk <{excerpt}>")

    def test_a_template_without_fields_drops_them(self):
        """The way an adapter keeps a text with no excerpt or no install hint."""
        args = ["pr", "view", "5"]
        self.scenario({"default": {"stdout": "<html>\n"}})
        self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp,
                                      texts={"not_json": "gh {args[0]} {args[1]} printed something other than JSON"}),
                         "gh pr view printed something other than JSON")
        missing = os.path.join(self.tmp, "gone")
        self.scenario(HARNESS_GH=missing)
        self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, texts={"not_found": "{exe} not found"}),
                         f"{missing} not found")

    def test_texts_given_replace_only_their_keys(self):
        args = ["issue", "view", "3"]
        only = {"not_found": "NF {exe}"}
        self.scenario({"default": {"stderr": "HTTP 500\n", "exit": 1}})
        self.assertEqual(self.problem(self.gh.gh, args, self.tmp, texts=only), "gh issue view 3 failed: HTTP 500")
        self.scenario({"default": {"stdout": "nope\n"}})
        self.assertEqual(self.problem(self.gh.gh_json, args, self.tmp, texts={"failed": "F {call}"}),
                         "gh issue view 3 printed something other than JSON: nope")
        self.scenario(GHTEST_SLOW="view", GHTEST_SLEEP="5")
        self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, timeout=0.4, texts=only),
                         "gh issue view 3 timed out after 0.4 s")
        blocked = self.blocked_gh()
        self.scenario(HARNESS_GH=blocked)
        self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, texts=only),
                         f"cannot run gh ({blocked}): Permission denied")
        missing = os.path.join(self.tmp, "absent-gh")
        self.scenario(HARNESS_GH=missing)
        self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, texts={"failed": "F"}),
                         f"gh not found ({missing}){INSTALL_HINT}")

    def test_describe_feeds_call_in_every_template(self):
        words = lambda a: "gh " + " ".join(a[:2])   # noqa: E731
        args = ["issue", "view", "9", "--json", "x"]
        self.scenario({"default": {"stderr": "bad\n", "exit": 1}})
        self.assertEqual(self.problem(self.gh.gh, args, self.tmp, describe=words, texts={"failed": "<{call}> {why}"}),
                         "<gh issue view> bad")
        self.scenario(GHTEST_SLOW="view", GHTEST_SLEEP="5")
        self.assertEqual(self.problem(self.gh.gh_run, args, self.tmp, timeout=0.4, describe=words),
                         "gh issue view timed out after 0.4 s")


# ---------------------------------------------------------------- run, via and pick: a tool's own collaborators
class CollaboratorTests(Case):
    """What lets board's and pr's adapters resolve gh_run, gh and first_line through their own namespace."""

    def test_gh_calls_the_run_given_with_args_cwd_stdin_and_timeout_only(self):
        for args, cwd, stdin, timeout, out in ((["pr", "view", "9", "--json", "url"], "/p/a", None, 60, "u9\n"),
                                               (["issue", "comment", "3", "--body-file", "-"], None, "body\n", 120, "")):
            with self.subTest(args=args):
                self.scenario()
                seen = []

                def run(a, c, s=None, t=None):          # no **kwargs: only the four may reach it
                    seen.append((list(a), c, s, t))
                    return subprocess.CompletedProcess(list(a), 0, out, "")
                self.assertEqual(self.gh.gh(args, cwd, stdin=stdin, timeout=timeout, run=run), out)
                self.assertEqual(seen, [(args, cwd, stdin, timeout)])
                self.assertEqual(self.lines(self.wraplog), [], "gh ran although a run was given")

    def test_a_failure_from_the_run_given_is_named_with_the_picked_line(self):
        err = "\n  GraphQL: Could not resolve to a PullRequest\nsecond line\n"
        for code in (1, 4):
            with self.subTest(code=code):
                run = lambda a, c, s=None, t=60: subprocess.CompletedProcess(list(a), code, "", err)  # noqa: E731
                self.assertEqual(self.problem(self.gh.gh, ["pr", "view", "9", "--json", "url"], "/p", run=run),
                                 "gh pr view 9 failed: GraphQL: Could not resolve to a PullRequest")
                self.assertEqual(self.problem(self.gh.gh, ["pr", "view", "9"], "/p", run=run, pick=self.last_line),
                                 "gh pr view 9 failed: second line")
                quiet = lambda a, c, s=None, t=60: subprocess.CompletedProcess(list(a), code, "", "")  # noqa: E731
                self.assertEqual(self.problem(self.gh.gh, ["api", "graphql"], "/p", run=quiet),
                                 f"gh api graphql failed: exit {code}")

    def test_gh_json_reads_the_output_of_the_via_given(self):
        for args, cwd, value in ((["pr", "list", "--head", "t/1-x"], "/p/a", [{"number": 1}]),
                                 (["issue", "view", "7", "--json", "labels"], None, {"labels": []})):
            with self.subTest(args=args):
                self.scenario()
                seen = []

                def via(a, c):                          # just the two
                    seen.append((list(a), c))
                    return json.dumps(value) + "\n"
                self.assertEqual(self.gh.gh_json(args, cwd, via=via), value)
                self.assertEqual(seen, [(args, cwd)])
                self.assertEqual(self.lines(self.wraplog), [], "gh ran although a via was given")

    def test_output_from_via_that_is_not_json_is_named_as_from_gh(self):
        out = "\n\n <html>oops\nmore\n"
        via = lambda a, c: out                          # noqa: E731
        self.assertEqual(self.problem(self.gh.gh_json, ["pr", "list", "--head", "t/1-x"], "/p", via=via),
                         "gh pr list printed something other than JSON: <html>oops")
        self.assertEqual(self.problem(self.gh.gh_json, ["issue", "view", "4"], "/p", via=via, pick=self.last_line),
                         "gh issue view 4 printed something other than JSON: more")
        self.assertEqual(self.problem(self.gh.gh_json, ["issue", "view", "4"], "/p", via=via,
                                      describe=lambda a: "gh " + " ".join(a[:2]),
                                      texts={"not_json": "{call} printed something other than JSON"}),
                         "gh issue view printed something other than JSON")
        self.assertIsNone(self.gh.gh_json(["pr", "view", "1"], "/p", via=lambda a, c: "", empty_ok=True))

    def test_pick_cuts_the_excerpt_and_runs_once_per_message(self):
        """The tools pass their own first_line as pick; it is called exactly once, on the text it names."""
        long = "<html>" + "z" * 100
        for out, expected in ((f"\n{long}\nsecond\n", long[:80]), ("one\ntwo\n", "one")):
            with self.subTest(out=out[:10]):
                self.scenario({"default": {"stdout": out}})
                picked = []

                def pick(text):
                    picked.append(text)
                    return self.first_line(text)
                self.assertEqual(self.problem(self.gh.gh_json, ["pr", "view", "2"], self.tmp, pick=pick),
                                 f"gh pr view 2 printed something other than JSON: {expected}")
                self.assertEqual(picked, [out])
        self.scenario({"default": {"stdout": "first\nlast\n"}})
        self.assertEqual(self.problem(self.gh.gh_json, ["pr", "view", "2"], self.tmp, pick=self.last_line),
                         "gh pr view 2 printed something other than JSON: last")
        err = "  HTTP 502: bad gateway\nretry later\n"
        self.scenario({"default": {"stderr": err, "exit": 1}})
        for fn in (self.gh.gh, self.gh.gh_json):
            with self.subTest(fn=fn.__name__):
                picked = []

                def pick(text):
                    picked.append(text)
                    return self.first_line(text)
                self.assertEqual(self.problem(fn, ["issue", "view", "7"], self.tmp, pick=pick),
                                 "gh issue view 7 failed: HTTP 502: bad gateway")
                self.assertEqual(picked, [err])


# ---------------------------------------------------------------- board and pr adopt the lib
def definitions(path: Path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [(node.name, node.lineno) for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]


class BoardAndPrAdoptTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_wrappers_are_defined_in_gh_py(self):
        self.assertEqual(sorted(name for name, _ in definitions(GH_PY, WRAPPERS)), sorted(WRAPPERS))

    def test_board_and_pr_keep_no_copy(self):
        """No describe or gh_run of their own; gh and gh_json at most once each (the adapters checked below)."""
        for path in (BOARD, PR):
            with self.subTest(tool=path.name):
                self.assertEqual(definitions(path, ("describe", "gh_run")), [])
                for name in ("gh", "gh_json"):
                    self.assertLessEqual(len(definitions(path, (name,))), 1, f"{name} defined more than once")

    def test_board_and_pr_gh_and_gh_json_are_short_adapters_over_the_lib(self):
        """Each kept gh/gh_json: at most two statements (docstring aside), no try, no nested def or class, no
        process launch, and a call to the lib's gh or gh_json (a name bound to it, or harness_lib.gh.<name>)."""
        gh = lib("gh")
        targets = {id(gh.gh), id(gh.gh_json)} if hasattr(gh, "gh") and hasattr(gh, "gh_json") else set()
        for path, name in ((BOARD, "harness_board_adapters_t"), (PR, "harness_pr_adapters_t")):
            tool = load(path, name)
            src = Source(path.read_text(encoding="utf-8"), str(path))
            for fn in [n for n in src.tree.body if isinstance(n, ast.FunctionDef) and n.name in ("gh", "gh_json")]:
                with self.subTest(tool=path.name, adapter=fn.name):
                    body = fn.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        body = body[1:]
                    self.assertLessEqual(len(body), 2, f"{fn.name} has {len(body)} statements")
                    calls_lib = False
                    for node in ast.walk(fn):
                        self.assertNotIsInstance(node, ast.Try, f"line {getattr(node, 'lineno', '?')}")
                        if node is not fn:
                            self.assertNotIsInstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                                                     f"line {getattr(node, 'lineno', '?')}: nested definition")
                        if isinstance(node, ast.Call):
                            self.assertFalse(src.launcher(node), f"line {node.lineno}: {fn.name} launches a process")
                            f = node.func
                            target = None
                            if isinstance(f, ast.Name):
                                target = vars(tool).get(f.id)
                            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                                    and vars(tool).get(f.value.id) is gh:
                                target = getattr(gh, f.attr, None)
                            calls_lib = calls_lib or (target is not None and id(target) in targets)
                    self.assertTrue(calls_lib, f"{path.name} {fn.name} does not call harness_lib.gh's gh or gh_json")

    def test_board_and_pr_bind_the_libs_wrappers_in_their_own_namespace(self):
        """The module's own dict holds the lib's objects (not a module __getattr__), and loading the tool adds
        nothing to builtins (where a kept copy could hide from the module's dict)."""
        gh = lib("gh")
        for path, name, names in ((BOARD, "harness_board_wrappers_t", ("describe",)),
                                  (PR, "harness_pr_wrappers_t", ("describe", "gh_run"))):
            before = dict(vars(builtins))
            tool = load(path, name)
            with self.subTest(tool=path.name, check="builtins"):
                added = {k for k in vars(builtins) if k not in before or vars(builtins)[k] is not before[k]}
                self.assertEqual(added, set(), f"{path.name} changes builtins")
            with self.subTest(tool=path.name, check="__getattr__"):
                self.assertNotIn("__getattr__", vars(tool))
            for attr in names:
                with self.subTest(tool=path.name, name=attr):
                    self.assertIs(vars(tool).get(attr), getattr(gh, attr, "harness_lib.gh has no " + attr))


# ---------------------------------------------------------------- board and pr run no gh of their own (static)
LAUNCH_ATTRS = {"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}
OS_LAUNCH = {"system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
             "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
             "posix_spawn", "posix_spawnp"}
# What board and pr may launch themselves today: git, a sibling harness tool, and claude (HARNESS_CLAUDE).
ALLOWED_HEADS = {"git", "sibling", "env:HARNESS_CLAUDE"}
MARKERS = ("HARNESS_GH", "GH_PROMPT_DISABLED", "brew install gh")


class Source:
    """A parsed tool: the processes it launches (with what each argv starts with) and its strings, folded."""

    def __init__(self, text, name="<source>"):
        self.tree = ast.parse(text, filename=name)
        self.parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.mods = {"subprocess": {"subprocess"}, "os": {"os"}}
        self.funcs = {}
        for _ in range(3):                      # aliases of aliases
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name in self.mods:
                            self.mods[a.name].add(a.asname or a.name)
                elif isinstance(node, ast.ImportFrom) and node.module in self.mods:
                    for a in node.names:
                        self.funcs[a.asname or a.name] = (node.module, a.name)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                    for names in self.mods.values():
                        if node.value.id in names:
                            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
                    if node.value.id in self.funcs:
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                self.funcs[t.id] = self.funcs[node.value.id]

    def launcher(self, call):
        f = call.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id in self.mods["subprocess"] and f.attr in LAUNCH_ATTRS:
                return True
            if f.value.id in self.mods["os"] and f.attr in OS_LAUNCH:
                return True
        if isinstance(f, ast.Name) and f.id in self.funcs:
            mod, attr = self.funcs[f.id]
            return attr in (LAUNCH_ATTRS if mod == "subprocess" else OS_LAUNCH)
        return False

    def scope(self, node):
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return self.tree

    def assigned(self, name, at):
        """The values assigned to `name` in the scope of `at`, else at module level."""
        for scope in (self.scope(at), self.tree):
            values = [n.value for n in ast.walk(scope) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value
                      is not None and any(isinstance(t, ast.Name) and t.id == name
                                          for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
            if values:
                return values
        return []

    def heads(self, expr, depth=0):
        """What an argv expression starts with: a set of "git"-like literals, "sibling", "env:<VAR>" or None."""
        if depth > 8:
            return {None}
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self.heads(expr.left, depth + 1)
        if isinstance(expr, (ast.List, ast.Tuple)):
            return self.elem(expr.elts[0], depth + 1) if expr.elts else {None}
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in ("list", "tuple") \
                and expr.args:
            return self.heads(expr.args[0], depth + 1)
        if isinstance(expr, ast.Name):
            values = self.assigned(expr.id, expr)
            return set().union(*(self.heads(v, depth + 1) for v in values)) if values else {None}
        return self.elem(expr, depth + 1)

    def elem(self, e, depth):
        if depth > 8:
            return {None}
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return {e.value}
        if isinstance(e, ast.Call):
            f = e.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if fname == "sibling":
                arg = e.args[0] if e.args else None
                if isinstance(arg, ast.Constant) and not str(arg.value).startswith("harness-"):
                    return {None}
                return {"sibling"}
            if fname in ("get", "getenv") and e.args and isinstance(e.args[0], ast.Constant):
                return {f"env:{e.args[0].value}"}
            if fname == "str" and e.args:
                return self.elem(e.args[0], depth + 1)
        if isinstance(e, ast.Subscript) and isinstance(getattr(e, "slice", None), ast.Constant):
            return {f"env:{e.slice.value}"}
        if isinstance(e, ast.BoolOp):
            return self.elem(e.values[0], depth + 1)
        if isinstance(e, ast.Name):
            values = self.assigned(e.id, e)
            return set().union(*(self.elem(v, depth + 1) for v in values)) if values else {None}
        return {None}

    def bad_launches(self):
        """(line, heads) for every process launch whose argv does not start with an allowed head."""
        out = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and self.launcher(node):
                argv = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ("args", "argv")),
                                                            None)
                heads = self.heads(argv) if argv is not None else {None}
                if not heads or not heads <= ALLOWED_HEADS:
                    out.append((node.lineno, sorted(map(str, heads))))
        return out

    def bad_tool_names(self):
        """sibling()/tool() calls naming something that is not a harness tool."""
        out = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("sibling", "tool") \
                    and node.args and isinstance(node.args[0], ast.Constant) \
                    and not str(node.args[0].value).startswith("harness-"):
                out.append((node.lineno, node.args[0].value))
        return out

    def fold(self, e):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return e.value
        if isinstance(e, ast.JoinedStr):
            return "".join(self.fold(v) for v in e.values)
        if isinstance(e, ast.FormattedValue):
            return self.fold(e.value)
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            return self.fold(e.left) + self.fold(e.right)
        if isinstance(e, ast.Name):
            values = [v for v in self.assigned(e.id, e) if isinstance(v, (ast.Constant, ast.JoinedStr, ast.BinOp))]
            return self.fold(values[0]) if len(values) == 1 else "\0"
        return "\0"

    def marked_strings(self):
        """(line, marker) for each marker in a string (f-strings and + chains folded, docstrings left out) or a
        keyword argument name."""
        docs = set()
        for node in ast.walk(self.tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
        out = []
        for node in ast.walk(self.tree):
            text = None
            if isinstance(node, (ast.JoinedStr, ast.BinOp)) or (isinstance(node, ast.Constant) and id(node) not in docs):
                text = self.fold(node) if not isinstance(node, ast.BinOp) or isinstance(node.op, ast.Add) else None
            elif isinstance(node, ast.keyword) and node.arg:
                text = node.arg
            for marker in MARKERS:
                if text and marker in text:
                    out.append((getattr(node, "lineno", getattr(getattr(node, "value", None), "lineno", 0)), marker))
        return sorted(set(out))


class StaticNoOwnGhTests(unittest.TestCase):
    """Board and pr run gh only through harness_lib.gh: whatever a copy of the old wrappers is renamed to,
    it launches a process whose argv starts with HARNESS_GH, and names HARNESS_GH, GH_PROMPT_DISABLED or the
    `brew install gh` hint. Their own launches today (git, sibling tools, claude) stay allowed."""

    def source(self, path):
        return Source(path.read_text(encoding="utf-8"), str(path))

    def test_board_and_pr_launch_only_git_sibling_tools_and_claude(self):
        for path in (BOARD, PR):
            with self.subTest(tool=path.name):
                self.assertEqual(self.source(path).bad_launches(), [],
                                 f"{path.name} launches processes other than git, sibling tools and claude")

    def test_board_and_pr_keep_no_gh_setting_or_install_hint(self):
        for path in (BOARD, PR):
            with self.subTest(tool=path.name):
                self.assertEqual(self.source(path).marked_strings(), [])

    def test_board_and_pr_name_only_harness_tools_as_siblings(self):
        for path in (BOARD, PR):
            with self.subTest(tool=path.name):
                self.assertEqual(self.source(path).bad_tool_names(), [])

    def test_the_checker_allows_todays_other_launches_and_flags_gh_in_any_shape(self):
        """The checker itself (green before the change): the shapes board and pr keep pass, gh launches fail."""
        allowed = [
            'import subprocess\ndef git(p, *a):\n    return subprocess.run(["git", "-C", p] + list(a))\n',
            'import subprocess\ndef tool(name, args):\n    return subprocess.run([sibling(name)] + list(args))\n',
            'import os, subprocess\ndef ultra(n):\n    exe = os.environ.get("HARNESS_CLAUDE", "claude")\n'
            '    cmd = [exe, "ultrareview", str(n)]\n    cmd += ["--post"]\n    return subprocess.run(cmd)\n',
        ]
        flagged = [
            'import os, subprocess\ndef old(args):\n    exe = os.environ.get("HARNESS_GH", "gh")\n'
            '    return subprocess.run([exe] + list(args))\n',
            'import subprocess as sp\ndef x():\n    sp.Popen(["gh", "api", "user"])\n',
            'from subprocess import run as r\ndef x():\n    r(["gh", "issue", "view"])\n',
            'import subprocess\ndef x(cmd):\n    subprocess.run(cmd)\n',
            'import os\ndef x(path):\n    os.execv(path, [path])\n',
            'import shutil, subprocess\ndef x():\n    subprocess.check_output([shutil.which("gh"), "pr", "list"])\n',
            'import subprocess\ndef x():\n    subprocess.run([sibling("gh"), "api"])\n',
        ]
        for text in allowed:
            with self.subTest(allowed=text.splitlines()[1]):
                self.assertEqual(Source(text).bad_launches(), [])
        for text in flagged:
            with self.subTest(flagged=text.splitlines()[-1].strip()):
                self.assertNotEqual(Source(text).bad_launches(), [])
        marked = ['x = "GH_PROMPT_" + "DISABLED"\n', "y = f\"brew {'install'} gh\"\n",
                  'import os\nenv = dict(os.environ, GH_PROMPT_DISABLED="1")\n',
                  'A = "HARNESS"\nB = A + "_GH"\n', 'def f(e):\n    return f"gh not found ({e}); install it with `brew install gh`"\n']
        for text in marked:
            with self.subTest(marked=text.strip()):
                self.assertNotEqual(Source(text).marked_strings(), [])
        self.assertEqual(Source('"""gh is HARNESS_GH when set."""\ndef f():\n    """HARNESS_GH too."""\n').marked_strings(), [])
        self.assertEqual(Source('t = tool("harness-board", ["show"], ".")\n').bad_tool_names(), [])
        self.assertNotEqual(Source('t = tool("gh", ["api"], ".")\n').bad_tool_names(), [])


# ---------------------------------------------------------------- board and pr route every gh call through the lib
def git_repo(root: str, origin: str = "git@github.com:acme/widget.git") -> str:
    repo = os.path.join(root, "repo")
    env = clean_env(GIT_CONFIG_NOSYSTEM="1", HOME=root)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", repo], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", origin], env=env, check=True, capture_output=True)
    return repo


class Routed(Case):
    """The tool loaded while harness_lib.gh's gh_run, gh and gh_json are recorders that hand on to the real
    ones, and its gh_exe() gives the fake. HARNESS_GH names the poison gh: a tool that runs gh any way but
    through the lib's wrappers runs the poison, which leaves a mark.

    The lib side this needs: gh_run finds gh through the module's gh_exe() at call time, and gh and gh_json
    reach gh_run (or gh) through the module's globals at call time."""

    SLUGS = ("acme/widget", "other-owner/tool.js")
    NEEDS_GIT = True

    def setUp(self):
        super().setUp()
        self.mark = os.path.join(self.tmp, "poison.mark")
        self.calls = []
        self.repo_path = git_repo(self.tmp) if self.NEEDS_GIT else None

    def routed_scenario(self, data, **extra):
        self.scenario(data, HARNESS_GH=W["poison"], GHTEST_POISON_MARK=self.mark, HOME=self.tmp, **extra)

    def route(self, path, name):
        gh = self.gh
        reals = {n: getattr(gh, n) for n in ("gh_run", "gh", "gh_json")}

        def recorder(n):
            def rec(*a, **k):
                args = a[0] if a else k.get("args")
                cwd = a[1] if len(a) > 1 else k.get("cwd")
                self.calls.append({"fn": n, "args": list(args), "cwd": cwd, "kwargs": dict(k)})
                return reals[n](*a, **k)
            return rec
        patcher = mock.patch.multiple(gh, gh_exe=lambda: W["gh"], **{n: recorder(n) for n in reals})
        patcher.start()
        self.addCleanup(patcher.stop)
        return load(path, name)

    def seen(self, *fns):
        return [c["args"] for c in self.calls if c["fn"] in fns]

    def assertNoPoison(self):
        ran = open(self.mark, encoding="utf-8").read() if os.path.exists(self.mark) else ""
        self.assertEqual(ran, "", "gh ran without harness_lib.gh for: " + ran.strip())


@unittest.skipIf(shutil.which("git") is None, "git not found")
class BoardRoutesThroughTheLibTests(Routed):
    def epic(self, title):
        return {"number": 12, "title": title, "url": "https://github.com/x/y/issues/12", "labels": [{"name": "epic"}],
                "state": "OPEN", "body": "## Goal\nShip it.\n", "comments": [], "parent": None, "blockedBy": []}

    def test_fetch_record_runs_issue_view_repo_view_and_api_through_the_lib(self):
        for slug in self.SLUGS:
            with self.subTest(slug=slug):
                self.calls.clear()
                title = f"Epic for {slug}"
                sub = f"repos/{slug}/issues/12/sub_issues?per_page=100"
                self.routed_scenario({"calls": [
                    {"match": ["issue", "view", "12"], "stdout_json": self.epic(title)},
                    {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": slug}},
                    {"match": ["api", sub], "stdout_json": [{"number": 13, "title": "t13", "labels": [], "state": "OPEN"},
                                                            {"number": 14, "title": "t14", "labels": [], "state": "OPEN"}]}]})
                board = self.route(BOARD, f"harness_board_routed_{len(slug)}")
                record = board.fetch_record(board.Repo(self.repo_path), 12)
                self.assertEqual((record["title"], [t["number"] for t in record["tasks"]]), (title, [13, 14]))
                top = self.seen("gh", "gh_json")
                for prefix in (["issue", "view", "12"], ["repo", "view"], ["api", sub]):
                    self.assertTrue(any(a[:len(prefix)] == prefix for a in top), f"{prefix} not through the lib: {top}")
                self.assertTrue(self.seen("gh_run"), "the lib's gh/gh_json reach gh_run")
                self.assertNoPoison()

    def test_board_failures_name_the_call_with_ghs_first_stderr_line(self):
        for slug, token in zip(self.SLUGS, ("t-a1", "t-b2")):
            with self.subTest(slug=slug):
                self.routed_scenario({"calls": [
                    {"match": ["issue", "view", "12"], "stderr": f"GraphQL: Could not resolve ({token})\nsecond\n", "exit": 1},
                    {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": slug}},
                    {"match": ["api", f"repos/{slug}/issues/5"], "stderr": f"\nHTTP 403: Forbidden ({token})\nmore\n",
                     "exit": 1}]})
                board = self.route(BOARD, f"harness_board_routed_fail_{token}")
                repo = board.Repo(self.repo_path)
                with self.assertRaises(self.Problem) as caught:
                    board.fetch_record(repo, 12)
                self.assertEqual(str(caught.exception), f"gh issue view 12 failed: GraphQL: Could not resolve ({token})")
                with mock.patch.object(board, "LINK_RETRY_DELAY", 0):
                    error, _ = board.link_sub_issue(repo, 4, 5)
                self.assertEqual(str(error), f"gh api repos/{slug}/issues/5 failed: HTTP 403: Forbidden ({token})")
                self.assertIn(["api", f"repos/{slug}/issues/5", "--jq", ".id"], self.seen("gh", "gh_json"))
                self.assertNoPoison()


    def test_board_commands_run_gh_only_through_the_lib(self):
        """Whole commands through board's main(): every gh call they make goes through the lib (none reaches the
        poison), with the calls and output they make today."""
        url = "https://github.com/acme/widget/issues/12#issuecomment-77"
        cases = [
            (["ready", "--json"],
             [{"match": ["issue", "list"], "stdout_json": [{"number": 21, "title": "T21", "blockedBy": [],
                                                            "labels": [{"name": "status:ready"}, {"name": "task"}]}]}],
             ["issue", "list", "--state", "open", "--label", "status:ready"], '"number": 21'),
            (["status", "12", "in-progress", "--comment", "moving"],
             [{"match": ["issue", "view", "12"], "stdout_json": {"labels": [{"name": "status:ready"}, {"name": "task"}]}},
              {"match": ["issue", "edit", "12"], "stdout": ""},
              {"match": ["issue", "comment", "12"], "stdout": url + "\n"}],
             ["issue", "edit", "12", "--add-label", "status:in-progress", "--remove-label", "status:ready"],
             "#12 status:in-progress"),
            (["comment", "12", "--body", "hello there"],
             [{"match": ["issue", "comment", "12"], "stdout": url + "\n"}], ["issue", "comment", "12", "--body"], url),
            (["close", "12", "--comment", "done"],
             [{"match": ["issue", "close", "12"], "stdout": ""}], ["issue", "close", "12"], "closed #12"),
        ]
        self.routed_scenario({"default": {"stdout": ""}})
        board = self.route(BOARD, "harness_board_routed_main")
        for argv, calls, made, printed in cases:
            with self.subTest(cmd=argv[0]):
                self.routed_scenario({"calls": calls})
                out = io.StringIO()
                with mock.patch.object(sys, "argv", ["harness-board"] + argv + ["--repo", self.repo_path]), \
                        contextlib.redirect_stdout(out):
                    try:
                        board.main()
                    except SystemExit as stop:
                        self.assertIn(stop.code, (None, 0), f"harness-board {' '.join(argv)}: {stop.code}")
                self.assertIn(printed, out.getvalue())
                argvs = [c["argv"] for c in self.lines(self.ghlog)]
                self.assertTrue(any(a[:len(made)] == made for a in argvs), f"{made} not in {argvs}")
                self.assertTrue(any(a[:len(made)] == made for a in self.seen("gh", "gh_json")),
                                f"{made} not through the lib's gh/gh_json")
                self.assertNoPoison()


@unittest.skipIf(shutil.which("git") is None, "git not found")
class PrRoutesThroughTheLibTests(Routed):
    def test_pr_view_api_list_and_ticket_parent_run_through_the_lib(self):
        for slug, n in zip(self.SLUGS, (3, 8)):
            with self.subTest(slug=slug):
                self.calls.clear()
                comments = f"repos/{slug}/pulls/{n}/comments"
                self.routed_scenario({"calls": [
                    {"match": ["pr", "view", str(n)], "stdout_json": {"number": n, "title": f"PR {slug}"}},
                    {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": slug}},
                    {"match": ["api", "--paginate", comments], "stdout": f"[{n}, 2]\n[{n + 1}]\n"},
                    {"match": ["api", f"repos/{slug}/issues/{n}/parent"], "stdout_json": {"number": n + 40}}]})
                pr = self.route(PR, f"harness_pr_routed_{n}")
                repo = pr.Repo(self.repo_path)
                self.assertEqual(pr.pr_view(repo, n, "number,title"), {"number": n, "title": f"PR {slug}"})
                self.assertEqual(pr.api_list(repo, comments), [n, 2, n + 1])
                self.assertEqual(pr.ticket_parent(repo, n), n + 40)
                self.assertIn(["pr", "view", str(n), "--json", "number,title"], self.seen("gh", "gh_json"))
                self.assertIn(["api", "--paginate", comments], self.seen("gh", "gh_json"))
                self.assertIn(["api", f"repos/{slug}/issues/{n}/parent"], self.seen("gh_run"))
                self.assertNoPoison()

    def test_pr_failures_name_the_call_with_ghs_first_stderr_line(self):
        for slug, token in zip(self.SLUGS, ("p-c3", "p-d4")):
            with self.subTest(slug=slug):
                comments = f"repos/{slug}/pulls/3/comments"
                self.routed_scenario({"calls": [
                    {"match": ["pr", "view", "3"], "stderr": f"no pull requests ({token})\nsecond\n", "exit": 1},
                    {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": slug}},
                    {"match": ["api", "--paginate", comments], "stderr": f"HTTP 502: Bad Gateway ({token})\nx\n", "exit": 1},
                    {"match": ["api", f"repos/{slug}/issues/7/parent"], "stderr": f"HTTP 500: boom ({token})\nlater\n",
                     "exit": 1}]})
                pr = self.route(PR, f"harness_pr_routed_fail_{token}")
                repo = pr.Repo(self.repo_path)
                for call, expected in (
                        (lambda: pr.pr_view(repo, 3, "number"), f"gh pr view 3 failed: no pull requests ({token})"),
                        (lambda: pr.api_list(repo, comments), f"gh api {comments} failed: HTTP 502: Bad Gateway ({token})"),
                        (lambda: pr.ticket_parent(repo, 7),
                         f"gh api repos/{slug}/issues/7/parent failed: HTTP 500: boom ({token})")):
                    with self.assertRaises(self.Problem) as caught:
                        call()
                    self.assertEqual(str(caught.exception), expected)
                self.assertNoPoison()


# ---------------------------------------------------------------- harness-work keeps its adapter
class WorkRoutesThroughTheLibTests(Routed):
    """work.gh_json(repo, args) with a real (truthy) repo runs gh only through the lib, in repo.main."""
    NEEDS_GIT = False

    def test_work_gh_json_runs_through_the_lib_in_repo_main(self):
        for n, value in ((4, {"labels": [{"name": "type:bug"}]}), (9, [{"number": 9, "state": "OPEN"}])):
            with self.subTest(n=n):
                self.calls.clear()
                main = tempfile.mkdtemp(prefix=f"main-{n}-", dir=self.tmp)
                repo = SimpleNamespace(main=main, root=self.tmp, key="k", name="repo")
                self.routed_scenario({"calls": [
                    {"match": ["issue", "view", str(n)], "stdout_json": value},
                    {"match": ["pr", "list"], "stderr": f"noise {n}\nlast words {n}\n", "exit": 1}]})
                work = self.route(WORK, f"harness_work_routed_{n}")
                self.assertEqual(work.gh_json(repo, ["issue", "view", str(n), "--json", "labels"]), value)
                with self.assertRaises(self.Problem) as caught:
                    work.gh_json(repo, ["pr", "list", "--head", "b", "--json", "number"])
                self.assertEqual(str(caught.exception), f"gh pr list failed: last words {n}")
                ran = [c for c in self.calls if c["args"][:3] == ["issue", "view", str(n)]]
                self.assertTrue(ran, "work's gh_json did not call the lib")
                self.assertEqual({real(c["cwd"]) for c in ran}, {real(main)})
                self.assertTrue(any(c["fn"] == "gh_run" for c in ran), "the lib's gh_run ran the call")
                self.assertNoPoison()


class WorkAdapterTests(Case):
    def setUp(self):
        super().setUp()
        self.work = load(WORK, "harness_work_wrappers_t")
        self.main = tempfile.mkdtemp(prefix="main-", dir=self.tmp)
        self.repo = SimpleNamespace(main=self.main, root=self.tmp)

    def test_gh_json_is_a_two_line_adapter_that_runs_nothing_itself(self):
        tree = ast.parse(WORK.read_text(encoding="utf-8"), filename=str(WORK))
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "gh_json"]
        self.assertEqual(len(found), 1, "harness-work keeps gh_json(repo, args)")
        body = found[0].body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        self.assertLessEqual(len(body), 2, f"gh_json has {len(body)} statements")
        gh = lib("gh")
        wrappers = {id(getattr(gh, name)) for name in ("gh_run", "gh", "gh_json") if hasattr(gh, name)}
        uses_lib = False
        for node in ast.walk(found[0]):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    target = getattr(self.work, func.id, None)
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    owner = getattr(self.work, func.value.id, None)
                    target = getattr(owner, func.attr, None) if owner is gh else None
                else:
                    target = None
                uses_lib = uses_lib or (target is not None and id(target) in wrappers)
        self.assertTrue(uses_lib, "gh_json calls harness_lib.gh's gh_run, gh or gh_json")
        for node in ast.walk(found[0]):
            self.assertNotIsInstance(node, ast.Try, f"line {getattr(node, 'lineno', '?')}: gh_json handles errors itself")
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                self.assertNotIn(called, ("run", "Popen", "check_output"), f"line {node.lineno}: gh_json runs {called}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn("GH_PROMPT_DISABLED", node.value)
            if isinstance(node, ast.keyword):
                self.assertNotEqual(node.arg, "GH_PROMPT_DISABLED")

    # Guards (green before the change, by design): test_work's fakegh stderr is one line, so only these
    # see whether work still reports the LAST line and its `gh <two words>` form after it adopts the lib.
    def test_work_reports_the_last_stderr_line_and_the_first_two_args(self):
        for args, err, expected in (
                (["issue", "view", "12", "--json", "labels"], "warning: noise\nHTTP 404: Not Found\n",
                 "gh issue view failed: HTTP 404: Not Found"),
                (["pr", "list", "--head", "t/9-x", "--state", "all"], "first\nsecond\n\nGraphQL: denied\n",
                 "gh pr list failed: GraphQL: denied")):
            with self.subTest(args=args):
                self.scenario({"default": {"stderr": err, "exit": 1}})
                self.assertEqual(self.problem(self.work.gh_json, self.repo, args), expected)
        self.scenario({"default": {"stderr": "", "exit": 5}})
        self.assertEqual(self.problem(self.work.gh_json, self.repo, ["issue", "view", "3"]), "gh issue view failed: exit 5")

    def test_work_decodes_in_repo_main_and_takes_empty_output_as_none(self):
        for value in ({"labels": [{"name": "type:bug"}]}, [{"number": 4}], None):
            with self.subTest(value=value):
                self.scenario({"default": {"stdout_json": value}})
                self.assertEqual(self.work.gh_json(self.repo, ["issue", "view", "4", "--json", "labels"]), value)
                call = self.lines(self.wraplog)[0]
                self.assertEqual((real(call["cwd"]), call["env"]["GH_PROMPT_DISABLED"]), (real(self.main), "1"))
        self.scenario({"default": {"stdout": ""}})
        self.assertIsNone(self.work.gh_json(self.repo, ["pr", "list", "--head", "b"]))
        for args, out in ((["pr", "list", "--head", "b"], "<html>\n"), (["issue", "view", "7", "--json", "x"], "{bad")):
            with self.subTest(args=args):
                self.scenario({"default": {"stdout": out}})
                self.assertEqual(self.problem(self.work.gh_json, self.repo, args),
                                 f"gh {args[0]} {args[1]} printed something other than JSON")

    def test_work_decodes_with_surrogateescape(self):
        for raw, expected in ((b'{"t": "caf\xe9"}', {"t": "caf\udce9"}), (b'["\xff", "ok"]', ["\udcff", "ok"])):
            with self.subTest(raw=raw):
                self.raw(stdout=raw)
                self.assertEqual(self.work.gh_json(self.repo, ["issue", "view", "4"]), expected)

    def test_work_keeps_its_not_found_cannot_run_and_timeout_texts(self):
        for name in ("gh-missing", "nowhere/gh"):
            missing = os.path.join(self.tmp, name)
            with self.subTest(exe=missing):
                self.scenario(HARNESS_GH=missing)
                self.assertEqual(self.problem(self.work.gh_json, self.repo, ["issue", "view", "3"]), f"{missing} not found")
        blocked = os.path.join(self.tmp, "gh-not-executable")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(blocked, 0o644)
        self.scenario(HARNESS_GH=blocked)
        self.assertEqual(self.problem(self.work.gh_json, self.repo, ["pr", "list"]),
                         f"cannot run {blocked}: Permission denied")
        for args, timeout in ((["issue", "view", "12", "--json", "labels"], 0.4), (["pr", "list", "--head", "b"], 0.6)):
            with self.subTest(args=args):
                self.scenario(GHTEST_SLOW=args[1], GHTEST_SLEEP="5")
                with mock.patch.object(self.work, "GH_TIMEOUT", timeout):
                    self.assertEqual(self.problem(self.work.gh_json, self.repo, args),
                                     f"{W['gh']} {args[0]} {args[1]} timed out after {timeout} s")


if __name__ == "__main__":
    unittest.main()
