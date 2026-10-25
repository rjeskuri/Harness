"""Tests for `harness-red files` and `harness-red run` (ticket #253): the branch's own test files.

    python3 -m unittest plugins/harness/tests/test_red_own.py -v

`files` lists the changed runnable test files plus, for every other changed path, the runnable test modules
whose subject names it (the name rule) or that HARNESS_TEST_MAP adds. `run` passes exactly that list to
HARNESS_TEST_CMD and writes no red record.

Every case builds, with real git under a temporary folder, a bare "origin", a clone with `main`, and the branch
t/253-own in a linked worktree; HOME is temporary. The expected sets are written out by hand from the ticket's
Context table and the name rule, never computed by the tool. `python3` on PATH runs the interpreter running
these tests. This module keeps off test_red.py, which #231 edits.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
TOOL = os.path.join(BIN, "harness-red")
GIT = shutil.which("git")
BRANCH, SLUG, TICKET = "t/253-own", "t-253-own", 253
NOTHING_RUN = "no own test files for this branch's changes; nothing run"

T = "plugins/harness/tests/"
# The Context table's candidates, plus test_red_own.py (a distractor: its subject `red_own` names nothing here).
HARNESS_TESTS = [T + n for n in ("test_work.py", "test_pr.py", "test_red.py", "test_hooks.py", "test_guards.py",
                                 "test_harness_lib.py", "test_harness_lib_settings.py", "test_cost.py",
                                 "test_harness_lib_cost.py", "test_install.py", "test_red_own.py")]
HARNESS_OTHER = ["plugins/harness/bin/harness-work", "plugins/harness/bin/harness-pr", "plugins/harness/bin/harness-red",
                 "plugins/harness/lib/harness_lib/settings.py", "plugins/harness/lib/harness_lib/cost.py",
                 "plugins/harness/hooks/verify_on_stop.py", "plugins/harness/hooks/branch_guard.py",
                 "plugins/harness/skills/work/SKILL.md", "scripts/install.sh", "plugins/harness/agents/reviewer.md",
                 "docs/design.md",
                 T + "fakegh.py"]          # source by the default patterns (tests/** is top-level only): no candidate
HARNESS = HARNESS_TESTS + HARNESS_OTHER

APP = ["src/app/models.py", "src/app/views.py", "src/app/forms.py", "src/app/helpers.py", "lib/user_store.py",
       "tests/test_models.py", "tests/test_views.py", "tests/test_forms.py", "tests/test_store.py",
       "tests/user_store_test.py", "tests/conftest.py", "tests/fixtures/data.json", "tests/helpers.py",
       "tests/test_old.py", "docs/notes.md"]

EXAMPLE_MAP = ("plugins/harness/hooks/branch_guard.py=plugins/harness/tests/test_guards.py, "
               "plugins/harness/agents/*.md=plugins/harness/tests/test_work.py")

PASSING_TEST = ("import unittest\n\n\nclass ModelsTest(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertEqual(2 + 3, 5)\n")
FAILING_TEST = PASSING_TEST.replace("2 + 3, 5", "2 + 3, 6")

# The test command these tests install: it appends one JSON line per invocation (argv, cwd, bytecode env) to
# $RECORD_ARGV, prints $RUNNER_LINES numbered lines, sleeps $RUNNER_SLEEP seconds and exits $RUNNER_EXIT.
RECORDER = """\
import json, os, sys, time
with open(os.environ["RECORD_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                         "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE")}) + "\\n")
for i in range(1, int(os.environ.get("RUNNER_LINES", "0")) + 1):
    print("out %03d end" % i)
sys.stdout.flush()
time.sleep(float(os.environ.get("RUNNER_SLEEP", "0")))
sys.exit(int(os.environ.get("RUNNER_EXIT", "0")))
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Repos:
    """The fixture, mixed into each TestCase: fresh repositories, the tool as a subprocess, a recording command."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-red-own-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        fakebin = os.path.join(self.tmp, "fakebin")
        python3 = write(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(python3, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_")) and k not in ("CLAUDE_PROJECT_DIR", "PYTHONPATH")}
        env.update(HOME=self.home, PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        self.env = env
        self.cases = 0
        self.recorder = write(os.path.join(self.tmp, "recorder.py"), RECORDER)
        self.argv_log = os.path.join(self.tmp, "argv.jsonl")

    # ------------------------------------------------------------ repositories
    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", cwd, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                              env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def new_repo(self, files, main_extra=None, branch=BRANCH):
        """A fresh origin, clone and worktree on `branch`; main holds `files` (each with its own text) and
        `main_extra` (path -> text). Returns (main checkout, worktree)."""
        self.cases += 1
        root = os.path.join(self.tmp, f"case{self.cases}")
        origin, repo = os.path.join(root, "origin.git"), os.path.join(root, "repo")
        os.makedirs(root)
        self.git(root, "init", "--bare", "-q", origin)
        self.git(root, "clone", "-q", origin, repo)
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        for rel in files:
            write(os.path.join(repo, rel), f"# {rel}\n")
        for rel, text in (main_extra or {}).items():
            write(os.path.join(repo, rel), text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "main")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        with open(os.path.join(repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".claude/settings.local.json\n")
        wt = os.path.join(root, "wt")
        self.git(repo, "worktree", "add", "-q", wt, "-b", branch, "origin/main")
        return repo, wt

    def change(self, wt, changes, commit=False):
        """Apply path -> action ('edit', 'new', 'delete' (left unstaged), 'rm' (git rm)), then commit if asked."""
        for rel, action in changes.items():
            path = os.path.join(wt, rel)
            if action == "edit":
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("# changed on the branch\n")
            elif action == "new":
                write(path, f"# new {rel}\n")
            elif action == "delete":
                os.remove(path)
            elif action == "rm":
                self.git(wt, "rm", "-q", rel)
            else:
                raise AssertionError(action)
        if commit:
            self.git(wt, "add", "-A")
            self.git(wt, "commit", "-q", "-m", "branch changes")

    def state(self, repo):
        key = hashlib.sha1(os.path.realpath(repo).encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.home, ".claude", "harness", "repos", key)

    # ------------------------------------------------------------ the tool
    def red(self, wt, *args, cwd=None, **extra):
        return subprocess.run([sys.executable, TOOL, *args], cwd=cwd or wt, env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=120)

    def own(self, wt, *args, **extra):
        """`harness-red files` (text): the listed paths; fails unless it exits 0."""
        p = self.red(wt, "files", *args, **extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p.stdout.splitlines()

    def own_json(self, wt, *args, **extra):
        p = self.red(wt, "files", "--json", *args, **extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def recording(self, **extra):
        """Environment for a HARNESS_TEST_CMD that records its argv into self.argv_log."""
        return dict({"HARNESS_TEST_CMD": f"python3 {shlex.quote(self.recorder)} {{files}}",
                     "RECORD_ARGV": self.argv_log}, **extra)

    def invocations(self):
        if not os.path.exists(self.argv_log):
            return []
        with open(self.argv_log, encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        os.remove(self.argv_log)
        return calls

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)


# The name rule over the Context table and other layouts: (name, files on main, changes, commit, expected).
# Each row runs in a fresh repository; `expected` is written out by hand.
SPREAD = [
    # Harness's own layout (the Context table)
    ("harness-work", HARNESS, {"plugins/harness/bin/harness-work": "edit"}, False, [T + "test_work.py"]),
    ("harness-pr", HARNESS, {"plugins/harness/bin/harness-pr": "edit"}, True, [T + "test_pr.py"]),
    ("harness-red", HARNESS, {"plugins/harness/bin/harness-red": "edit"}, False, [T + "test_red.py"]),
    ("settings.py", HARNESS, {"plugins/harness/lib/harness_lib/settings.py": "edit"}, True,
     [T + "test_harness_lib.py", T + "test_harness_lib_settings.py"]),
    ("cost.py", HARNESS, {"plugins/harness/lib/harness_lib/cost.py": "edit"}, False,
     [T + "test_cost.py", T + "test_harness_lib.py", T + "test_harness_lib_cost.py"]),
    ("verify_on_stop.py", HARNESS, {"plugins/harness/hooks/verify_on_stop.py": "edit"}, True, [T + "test_hooks.py"]),
    ("branch_guard.py", HARNESS, {"plugins/harness/hooks/branch_guard.py": "edit"}, False, [T + "test_hooks.py"]),
    ("SKILL.md", HARNESS, {"plugins/harness/skills/work/SKILL.md": "edit"}, True, [T + "test_work.py"]),
    ("install.sh", HARNESS, {"scripts/install.sh": "edit"}, False, [T + "test_install.py"]),
    ("reviewer.md unmapped", HARNESS, {"plugins/harness/agents/reviewer.md": "edit"}, True, []),
    ("design.md unmapped", HARNESS, {"docs/design.md": "edit"}, False, []),
    ("unmapped together", HARNESS, {"docs/design.md": "edit", "plugins/harness/agents/reviewer.md": "edit"}, True, []),
    # a changed test file is listed itself and not name-mapped (its suffix `cost` would add test_cost.py)
    ("changed test plus source", HARNESS,
     {T + "test_harness_lib_cost.py": "edit", "plugins/harness/bin/harness-pr": "edit"}, False,
     [T + "test_harness_lib_cost.py", T + "test_pr.py"]),
    ("changed tests only", HARNESS, {T + "test_guards.py": "edit", T + "test_red_own.py": "edit"}, True,
     [T + "test_guards.py", T + "test_red_own.py"]),
    ("new untracked source", HARNESS, {"plugins/harness/bin/harness-cost": "new"}, False, [T + "test_cost.py"]),
    ("several sources, no duplicates", HARNESS,
     {"plugins/harness/bin/harness-work": "edit", "plugins/harness/skills/work/SKILL.md": "edit",
      "plugins/harness/hooks/branch_guard.py": "edit", "plugins/harness/hooks/verify_on_stop.py": "edit"}, False,
     [T + "test_hooks.py", T + "test_work.py"]),
    # other layouts (the Context examples)
    ("models.py", ["src/app/models.py", "tests/test_models.py", "tests/test_views.py"],
     {"src/app/models.py": "edit"}, False, ["tests/test_models.py"]),
    ("cart.ts", ["src/cart.ts", "src/cart.test.ts", "src/cartography.ts"], {"src/cart.ts": "edit"}, True,
     ["src/cart.test.ts"]),
    ("user_store.py", ["lib/user_store.py", "tests/test_store.py", "tests/user_store_test.py", "tests/test_user.py"],
     {"lib/user_store.py": "edit"}, False, ["tests/test_store.py", "tests/user_store_test.py"]),
    # the rule's edges: a whole `_` token suffix, not a prefix or a substring
    ("prefix is no suffix", ["src/app/models.py", "tests/test_models.py"], {"src/app/models_extra.py": "new"}, False,
     []),
    ("substring is no token", ["src/app/models.py", "tests/test_models.py"], {"src/supermodels.py": "new"}, True, []),
    # a directory's normalised name; case and punctuation normalised
    ("directory name", ["src/Payment-Gateway/client.py", "tests/test_payment_gateway.py", "tests/test_client.py",
                        "tests/test_gateway.py"],
     {"src/Payment-Gateway/client.py": "edit"}, False, ["tests/test_client.py", "tests/test_payment_gateway.py"]),
    # the other runnable basenames; a runnable-looking name outside the test patterns is source, no candidate
    ("spec.js", ["web/src/user-profile.js", "spec/user_profile.spec.js", "spec/profile.spec.js",
                 "web/test/user_profile.spec.js", "spec/user.spec.js"],
     {"web/src/user-profile.js": "edit"}, True, ["spec/profile.spec.js", "spec/user_profile.spec.js"]),
    ("test.mjs", ["src/util.mjs", "tests/util.test.mjs", "tests/util_extra.test.mjs"], {"src/util.mjs": "edit"}, False,
     ["tests/util.test.mjs"]),
    ("_test.ts and _test.js", ["pkg/parser.ts", "pkg/parser_test.ts", "pkg/lexer_test.js", "pkg/lexer.js"],
     {"pkg/parser.ts": "edit", "pkg/lexer.js": "edit"}, True, ["pkg/lexer_test.js", "pkg/parser_test.ts"]),
    ("spec.ts and test.js", ["src/router.ts", "spec/router.spec.ts", "src/menu.js", "src/menu.test.js"],
     {"src/router.ts": "edit"}, False, ["spec/router.spec.ts"]),
    # paths used nowhere else in this module: only the rule can answer them
    ("token suffixes, not prefixes", ["src/billing/invoice_line.py", "tests/test_invoice_line.py",
                                      "tests/test_line.py", "tests/test_invoice.py"],
     {"src/billing/invoice_line.py": "edit"}, True, ["tests/test_invoice_line.py", "tests/test_line.py"]),
    ("a normalised directory, another language", ["pkg/Order-Book/engine.go", "tests/test_order_book.py",
                                                  "tests/test_engine.py", "tests/test_book.py"],
     {"pkg/Order-Book/engine.go": "edit"}, False, ["tests/test_engine.py", "tests/test_order_book.py"]),
    ("case and punctuation on both sides", ["lib/Data-Sync/HTTP_Client.py", "tests/test_Data-Sync.py",
                                            "tests/HTTP-Client.test.js", "tests/test_client.py", "tests/test_sync.py",
                                            "tests/test_http.py"],
     {"lib/Data-Sync/HTTP_Client.py": "edit"}, True,
     ["tests/HTTP-Client.test.js", "tests/test_Data-Sync.py", "tests/test_client.py"]),
    # one source, other candidates, other answers (models.py above gives tests/test_models.py)
    ("models.py, longer suffixes", ["src/app/models.py", "tests/test_app_models.py", "tests/models_test.py",
                                    "tests/test_model.py"],
     {"src/app/models.py": "edit"}, False, ["tests/models_test.py", "tests/test_app_models.py"]),
    ("models.py, whole path and directory", ["src/app/models.py", "tests/test_src_app_models.py", "tests/test_app.py",
                                             "tests/test_models_app.py", "tests/test_src.py"],
     {"src/app/models.py": "edit"}, True, ["tests/test_app.py", "tests/test_src.py", "tests/test_src_app_models.py"]),
]


def random_rows():
    """Rows with names made up at run time, so no table of paths can answer them; expected by the token rule."""
    stem, other = "s" + uuid.uuid4().hex[:8], "m" + uuid.uuid4().hex[:8]
    upper = stem.upper()
    return [
        # src/<stem>_svc.py: suffixes svc and <stem>_svc; <stem> alone is a leading token, no suffix
        (f"random {stem}_svc", [f"src/{stem}_svc.py", f"tests/test_{stem}_svc.py", "tests/test_svc.py",
                                f"tests/test_{stem}.py"],
         {f"src/{stem}_svc.py": "edit"}, False, [f"tests/test_{stem}_svc.py", "tests/test_svc.py"]),
        # src/<STEM>-Core/<other>.py: the directory normalises to <stem>_core; core_<other> is a token suffix
        (f"random {upper}-Core/{other}", [f"src/{upper}-Core/{other}.py", f"tests/test_{stem}_core.py",
                                          f"tests/{other}_test.py", f"tests/test_core_{other}.py",
                                          f"tests/test_{stem}.py", "tests/test_core.py"],
         {f"src/{upper}-Core/{other}.py": "edit"}, True,
         [f"tests/test_{stem}_core.py", f"tests/{other}_test.py", f"tests/test_core_{other}.py"]),
        # a new untracked file named by the same stem in another folder
        (f"random new lib/{stem}.js", [f"tests/{stem}.test.js", f"tests/{stem}x.test.js", f"tests/x{stem}.test.js"],
         {f"lib/{stem}.js": "new"}, False, [f"tests/{stem}.test.js"]),
    ]


@unittest.skipUnless(GIT, "git is not installed")
class OwnFiles(Repos, unittest.TestCase):

    def test_changed_tests_and_name_mapped_modules_over_a_spread_of_layouts(self):
        for name, files, changes, commit, expected in SPREAD + random_rows() + random_rows():
            with self.subTest(name):
                _, wt = self.new_repo(files)
                self.change(wt, changes, commit=commit)
                p = self.red(wt, "files")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(p.stdout.splitlines(), sorted(expected), p.stdout + p.stderr)
                if not expected:
                    self.assertEqual(p.stdout, "")                  # nothing at all when there are none

    def test_json_lists_changed_tests_mapped_and_unmapped(self):
        _, wt = self.new_repo(HARNESS)
        self.change(wt, {"plugins/harness/bin/harness-work": "edit", "plugins/harness/lib/harness_lib/cost.py": "edit",
                         T + "test_pr.py": "edit"}, commit=True)
        self.change(wt, {"plugins/harness/bin/harness-pr": "edit", "plugins/harness/agents/reviewer.md": "edit",
                         "docs/design.md": "edit", "plugins/harness/bin/harness-new": "new"})
        got = self.own_json(wt)
        self.assertEqual(set(got), {"base", "files", "changed_tests", "mapped", "unmapped"})
        self.assertEqual(got["base"], "origin/main")
        files = sorted([T + "test_cost.py", T + "test_harness_lib.py", T + "test_harness_lib_cost.py",
                        T + "test_pr.py", T + "test_work.py"])
        self.assertEqual(got["files"], files)                      # sorted, test_pr.py once
        self.assertEqual(sorted(got["changed_tests"]), [T + "test_pr.py"])
        self.assertEqual({k: sorted(v) for k, v in got["mapped"].items()}, {
            "plugins/harness/bin/harness-work": [T + "test_work.py"],
            "plugins/harness/bin/harness-pr": [T + "test_pr.py"],
            "plugins/harness/lib/harness_lib/cost.py": sorted([T + "test_cost.py", T + "test_harness_lib.py",
                                                               T + "test_harness_lib_cost.py"]),
        })
        self.assertEqual(sorted(got["unmapped"]), ["docs/design.md", "plugins/harness/agents/reviewer.md",
                                                   "plugins/harness/bin/harness-new"])
        self.assertEqual(self.own(wt), files)                      # the text form lists the same files

        # --base: from HEAD only the uncommitted changes count (the committed harness-work, cost.py and
        # test_pr.py drop out); the base is echoed as given
        got = self.own_json(wt, "--base", "HEAD")
        self.assertEqual(got["base"], "HEAD")
        self.assertEqual(got["files"], [T + "test_pr.py"])
        self.assertEqual(got["changed_tests"], [])
        self.assertEqual({k: sorted(v) for k, v in got["mapped"].items()},
                         {"plugins/harness/bin/harness-pr": [T + "test_pr.py"]})
        self.assertEqual(sorted(got["unmapped"]), ["docs/design.md", "plugins/harness/agents/reviewer.md",
                                                   "plugins/harness/bin/harness-new"])
        self.assertEqual(self.own(wt, "--base", "main")[:1], [T + "test_cost.py"])
        self.assertOneLineError(self.red(wt, "files", "--base", "nope"), "unknown base 'nope'")

        # another layout, nothing but unmapped changes: empty lists, the paths under unmapped
        _, wt = self.new_repo(["src/app/models.py", "tests/test_models.py"])
        self.change(wt, {"README.md": "new", "src/app/loader.py": "new"})
        got = self.own_json(wt)
        self.assertEqual(dict(got, unmapped=sorted(got["unmapped"])),
                         {"base": "origin/main", "files": [], "changed_tests": [], "mapped": {},
                          "unmapped": ["README.md", "src/app/loader.py"]})
        self.assertEqual(self.red(wt, "files").stdout, "")

    def test_test_map_setting_adds_its_matches_and_skips_malformed_entries(self):
        changes = {"plugins/harness/hooks/branch_guard.py": "edit", "plugins/harness/agents/reviewer.md": "edit",
                   "docs/design.md": "edit"}
        _, wt = self.new_repo(HARNESS)
        self.change(wt, changes)
        self.assertEqual(self.own(wt), [T + "test_hooks.py"])

        # the ticket's example map, from the environment
        p = self.red(wt, "files", HARNESS_TEST_MAP=EXAMPLE_MAP)
        self.assertEqual((p.returncode, p.stderr), (0, ""), p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines(), [T + "test_guards.py", T + "test_hooks.py", T + "test_work.py"])
        got = self.own_json(wt, HARNESS_TEST_MAP=EXAMPLE_MAP)
        self.assertEqual({k: sorted(v) for k, v in got["mapped"].items()}, {
            "plugins/harness/hooks/branch_guard.py": [T + "test_guards.py", T + "test_hooks.py"],
            "plugins/harness/agents/reviewer.md": [T + "test_work.py"]})
        self.assertEqual(got["unmapped"], ["docs/design.md"])

        # a malformed entry: one warning line, skipped; the good entry still applies
        for value in ("not-a-pair, plugins/harness/agents/*.md=plugins/harness/tests/test_work.py",
                      "plugins/harness/agents/*.md=plugins/harness/tests/test_work.py,docs/design.md"):
            with self.subTest(value=value):
                for args in ((), ("--json",)):
                    p = self.red(wt, "files", *args, HARNESS_TEST_MAP=value)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertNotIn("Traceback", p.stderr)
                    warnings = [ln for ln in p.stderr.splitlines() if ln.strip()]
                    self.assertEqual(len(warnings), 1, p.stderr)
                    self.assertTrue(warnings[0].startswith("harness-red: warning:"), p.stderr)
                    listed = json.loads(p.stdout)["files"] if args else p.stdout.splitlines()
                    self.assertEqual(listed, [T + "test_hooks.py", T + "test_work.py"])

        # globs as glob_match reads them; a TEST_GLOB adds only candidates (runnable tests that exist)
        for value, expected in (
            ("docs/**=plugins/harness/tests/test_harness_lib*.py",
             [T + "test_harness_lib.py", T + "test_harness_lib_cost.py", T + "test_harness_lib_settings.py",
              T + "test_hooks.py"]),
            ("*.md=**/test_install.py", [T + "test_hooks.py", T + "test_install.py"]),
            ("plugins/harness/agents/*.md=plugins/harness/tests/fakegh.py", [T + "test_hooks.py"]),
            ("plugins/harness/agents/*.md=plugins/harness/tests/test_missing.py", [T + "test_hooks.py"]),
            ("plugins/harness/bin/*=plugins/harness/tests/test_install.py", [T + "test_hooks.py"]),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.own(wt, HARNESS_TEST_MAP=value), sorted(expected))

        # from the worktree's settings.json, settings.local.json over it, the environment over both; the main
        # checkout's settings.local.json is not read
        settings = json.dumps({"env": {"HARNESS_TEST_MAP": "plugins/harness/agents/*.md=" + T + "test_red.py"}})
        repo, wt = self.new_repo(HARNESS, {".claude/settings.json": settings})
        self.change(wt, changes)
        self.assertEqual(self.own(wt), [T + "test_hooks.py", T + "test_red.py"])
        write(os.path.join(repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_TEST_MAP": "docs/**=" + T + "test_pr.py"}}))
        self.assertEqual(self.own(wt), [T + "test_hooks.py", T + "test_red.py"])
        write(os.path.join(wt, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_TEST_MAP": "plugins/harness/agents/*.md=" + T + "test_install.py"}}))
        self.assertEqual(self.own(wt), [T + "test_hooks.py", T + "test_install.py"])
        self.assertEqual(self.own(wt, HARNESS_TEST_MAP="docs/**=" + T + "test_cost.py"),
                         [T + "test_cost.py", T + "test_hooks.py"])

    def test_deleted_and_non_runnable_test_files_are_not_listed(self):
        _, wt = self.new_repo(APP)
        self.change(wt, {"tests/test_old.py": "rm", "src/app/views.py": "rm"}, commit=True)   # deleted, committed
        self.change(wt, {
            "tests/test_forms.py": "delete",          # deleted, unstaged: still in the index
            "src/app/forms.py": "edit",               # names only the deleted test_forms.py
            "src/app/models.py": "delete",            # a deleted source still maps
            "tests/conftest.py": "edit", "tests/fixtures/data.json": "edit", "tests/helpers.py": "edit",
            "src/app/helpers.py": "edit",             # tests/helpers.py is not runnable, so no candidate
        })
        expected = ["tests/test_models.py", "tests/test_views.py"]
        self.assertEqual(self.own(wt), expected)
        got = self.own_json(wt)
        self.assertEqual(got["files"], expected)
        self.assertEqual(got["changed_tests"], [])
        self.assertEqual(got["mapped"].get("src/app/views.py"), ["tests/test_views.py"])
        self.assertEqual(got["mapped"].get("src/app/models.py"), ["tests/test_models.py"])
        never = {"tests/test_old.py", "tests/test_forms.py", "tests/conftest.py", "tests/fixtures/data.json",
                 "tests/helpers.py"}
        listed = set(got["files"]) | set(got["changed_tests"]) | {t for v in got["mapped"].values() for t in v}
        self.assertEqual(listed & never, set())

        # only non-runnable test changes and deleted tests: nothing at all
        _, wt = self.new_repo(APP)
        self.change(wt, {"tests/conftest.py": "edit", "tests/fixtures/data.json": "edit", "tests/test_old.py": "rm",
                         "tests/test_store.py": "delete"})
        self.assertEqual(self.red(wt, "files").stdout, "")
        self.assertEqual(self.own_json(wt)["files"], [])

    def test_no_tests_ticket_is_answered_like_any_branch(self):
        changes = {"src/app/models.py": "edit", "tests/test_views.py": "edit", "docs/notes.md": "edit"}
        expected = ["tests/test_models.py", "tests/test_views.py"]
        repo, wt = self.new_repo(APP)
        self.change(wt, changes)
        plain_text, plain_json = self.red(wt, "files").stdout, self.own_json(wt)
        self.assertEqual(plain_text.splitlines(), expected)

        write(os.path.join(self.state(repo), "tickets", f"{TICKET}.json"),
              json.dumps({"number": TICKET, "title": "A ticket", "flags": ["no-tests"],
                          "labels": ["task", "status:in-progress", "no-tests"]}))
        p = self.red(wt, "files")
        self.assertEqual((p.returncode, p.stdout), (0, plain_text), p.stdout + p.stderr)
        self.assertNotIn("no-tests", p.stdout + p.stderr)
        self.assertEqual(self.own_json(wt), plain_json)
        p = self.red(wt, "run", **self.recording())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([c["argv"] for c in self.invocations()], [expected])

        # a branch that is no ticket's at all, same changes: the same answer
        other = os.path.join(os.path.dirname(repo), "feature")
        self.git(repo, "worktree", "add", "-q", other, "-b", "feature/own", "origin/main")
        self.change(other, changes)
        self.assertEqual(self.red(other, "files").stdout, plain_text)
        self.assertEqual(self.red(other, "files", "--worktree", other, cwd=self.tmp).stdout, plain_text)

        # a detached HEAD is refused, as by the other subcommands
        detached = os.path.join(os.path.dirname(repo), "detached")
        self.git(repo, "worktree", "add", "-q", "--detach", detached, "main")
        for sub in ("files", "run"):
            with self.subTest(sub=sub):
                self.assertOneLineError(self.red(detached, sub, **self.recording()), "HEAD is detached")
        self.assertEqual(self.invocations(), [])


@unittest.skipUnless(GIT, "git is not installed")
class OwnRun(Repos, unittest.TestCase):

    def test_run_passes_exactly_the_own_files_to_the_test_command(self):
        for name, files, changes, expected in (
            ("app", APP, {"src/app/models.py": "edit", "tests/test_views.py": "edit", "docs/notes.md": "new"},
             ["tests/test_models.py", "tests/test_views.py"]),
            ("harness", HARNESS, {"plugins/harness/lib/harness_lib/cost.py": "edit"},
             [T + "test_cost.py", T + "test_harness_lib.py", T + "test_harness_lib_cost.py"]),
            ("store", APP, {"lib/user_store.py": "edit", "tests/conftest.py": "edit"},
             ["tests/test_store.py", "tests/user_store_test.py"]),
        ):
            with self.subTest(name):
                _, wt = self.new_repo(files)
                self.change(wt, changes)
                self.assertEqual(self.own(wt), expected)
                p = self.red(wt, "run", **self.recording())
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                calls = self.invocations()
                self.assertEqual(len(calls), 1, calls)                  # one run, with exactly the list
                self.assertEqual(calls[0]["argv"], expected)
                self.assertEqual(calls[0]["cwd"], wt)                   # in the worktree
                self.assertEqual(calls[0]["bytecode"], "1")
                self.assertIn(f"own tests: {len(expected)} files, exit 0", p.stdout.splitlines())

                p = self.red(wt, "run", "--worktree", wt, cwd=self.tmp, **self.recording())
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual([(c["argv"], c["cwd"]) for c in self.invocations()], [(expected, wt)])

        # --base narrows the list as it narrows `files`
        _, wt = self.new_repo(APP)
        self.change(wt, {"src/app/models.py": "edit"}, commit=True)
        self.change(wt, {"src/app/views.py": "edit"})
        p = self.red(wt, "run", "--base", "HEAD", **self.recording())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([c["argv"] for c in self.invocations()], [["tests/test_views.py"]])

        # the command comes from the worktree's settings when the environment has none
        _, wt = self.new_repo(APP, {".claude/settings.json": json.dumps({"env": {
            "HARNESS_TEST_CMD": f"python3 {shlex.quote(self.recorder)} {{files}}"}})})
        self.change(wt, {"src/app/forms.py": "edit"})
        p = self.red(wt, "run", RECORD_ARGV=self.argv_log)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([c["argv"] for c in self.invocations()], [["tests/test_forms.py"]])

    def test_run_exit_follows_the_command_and_prints_the_tail(self):
        _, wt = self.new_repo(APP)
        self.change(wt, {"src/app/models.py": "edit", "lib/user_store.py": "edit"})    # three files
        for code, want in ((0, 0), (1, 1), (3, 1)):
            with self.subTest(code=code):
                p = self.red(wt, "run", **self.recording(RUNNER_LINES="60", RUNNER_EXIT=str(code)))
                self.assertEqual(p.returncode, want, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn(f"own tests: 3 files, exit {code}", p.stdout.splitlines())
                self.assertIn("out 060 end", p.stdout)                  # the last 40 lines: 021 to 060
                self.assertIn("out 021 end", p.stdout)
                self.assertNotIn("out 020 end", p.stdout)
                self.assertNotIn("out 001 end", p.stdout)
                self.assertEqual(len(self.invocations()), 1)

        # a timeout: the command is stopped, the stopped line printed, exit 1
        started = time.monotonic()
        p = self.red(wt, "run", **self.recording(RUNNER_SLEEP="60", HARNESS_TEST_TIMEOUT="1"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("timed out after 1s", p.stdout + p.stderr)
        self.assertIn("stopped", p.stdout + p.stderr)

        # the detected default command (python3 -m unittest {files}) with real test modules
        _, wt = self.new_repo(["src/app/models.py", "src/app/views.py"],
                              {"tests/test_models.py": PASSING_TEST, "tests/test_views.py": PASSING_TEST})
        self.change(wt, {"src/app/models.py": "edit"})
        p = self.red(wt, "run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("own tests: 1 files, exit 0", p.stdout.splitlines())
        self.assertIn("OK", p.stdout)
        write(os.path.join(wt, "tests", "test_models.py"), FAILING_TEST)
        p = self.red(wt, "run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("own tests: 1 files, exit 1", p.stdout.splitlines())
        self.assertIn("AssertionError: 5 != 6", p.stdout)
        for _, dirs, _ in os.walk(wt):                                  # PYTHONDONTWRITEBYTECODE reached the tests
            self.assertNotIn("__pycache__", dirs)

    def test_run_with_no_own_files_runs_nothing_and_exits_zero(self):
        for name, changes in (
            ("no changes", {}),
            ("unmapped only", {"docs/notes.md": "edit", "src/app/loader.py": "new", "README.md": "new"}),
            ("non-runnable and deleted tests", {"tests/conftest.py": "edit", "tests/fixtures/data.json": "edit",
                                                "tests/test_old.py": "rm"}),
        ):
            with self.subTest(name):
                _, wt = self.new_repo(APP)
                self.change(wt, changes)
                self.assertEqual(self.red(wt, "files").stdout, "")
                p = self.red(wt, "run", **self.recording(RUNNER_EXIT="1"))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(NOTHING_RUN, p.stdout.splitlines())
                self.assertNotIn("own tests:", p.stdout)
                self.assertEqual(self.invocations(), [])               # the command never ran

    def test_run_writes_no_red_record(self):
        repo, wt = self.new_repo(APP)
        self.change(wt, {"src/app/models.py": "edit", "tests/test_views.py": "edit"})
        state = self.state(repo)
        for code in ("0", "1"):
            p = self.red(wt, "run", **self.recording(RUNNER_EXIT=code))
            self.assertEqual(p.returncode, int(code), p.stdout + p.stderr)
            self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(self.red(wt, "files").returncode, 0)
        for sub in ("red", "red-files"):                            # no record, no side file
            self.assertFalse(os.path.exists(os.path.join(state, sub)), sub)

        # an existing record, even a broken one, is neither read nor touched
        record = write(os.path.join(state, "red", SLUG + ".json"), "{not json")
        side = write(os.path.join(state, "red-files", SLUG + ".json"), "{also not json")
        past = time.time() - 3600
        for path in (record, side):
            os.utime(path, (past, past))
        for code in ("0", "1"):
            p = self.red(wt, "run", **self.recording(RUNNER_EXIT=code))
            self.assertEqual(p.returncode, int(code), p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual([c["argv"] for c in self.invocations()], [["tests/test_models.py", "tests/test_views.py"]])
        self.assertEqual((read(record), read(side)), ("{not json", "{also not json"))
        for path in (record, side):
            self.assertEqual(int(os.stat(path).st_mtime), int(past))
        self.assertEqual(sorted(os.listdir(os.path.join(state, "red"))), [SLUG + ".json"])
        self.assertEqual(sorted(os.listdir(os.path.join(state, "red-files"))), [SLUG + ".json"])


if __name__ == "__main__":
    unittest.main()
