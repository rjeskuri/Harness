"""Tests for `harness-pr gate P [--json]` (ticket #141, epic #132): the auto-land gate's facts gathered from gh and
git, evaluated by harness_lib/autoland.py's `evaluate`, and printed.

    python3 -m unittest discover -s plugins/harness/tests -p test_autoland.py -v

Each test has a temporary HOME and real git repositories: a bare origin, the main checkout `proj` pushed to it
(where the gate runs) and a second clone `work` that pushes ticket branches and new commits on main, as GitHub
and other machines would. The opt-in is the `.claude/settings.json` committed on origin/main
(`{"env": {"HARNESS_AUTOLAND": "1"}}`). gh is fakegh.py behind a wrapper written once for the module (macOS
checks a new executable on its first run); every scenario answers the calls the ticket names, so an unexpected
call fails (and the gate then waits). Head shas, review commits and the PR's commit list are the real ones of the
pushed branches.

- The plain output is one line `<verdict>: <reason>`; --json holds pr, verdict, code, reason, rule, head and
  ticket; exit 0 for every verdict, 1 for a bad invocation or no repository.
- Verdict, code and rule are exact; reasons are checked for the values that tell one case from another (sha7s,
  numbers, names). A failing gh call's reason is exact: `<call> failed: <first stderr line>`, the call named as
  harness_lib/gh.py's describe() names it.
- nontest_files is checked through the gate (rule 5's size backstop: 8 source files land, 9 are too large,
  whatever the test files) and, for the literal `nontest_files 3`, in-process on
  harness_lib.autoland.gather_facts(main, slug, pr), the function the ticket's outline names. Test files are
  those the default HARNESS_TEST_PATTERNS match, in each shape they cover (TEST_SHAPED); paths that merely look
  like tests (TEST_LOOKING) count as source.
- Review-at-head cases all run on the one branch BRANCH, moving it between states that land and states that
  need a re-check, so neither the ref name nor anything but the PR's commits and their trees tells them apart.
- compare is answered only as `compare/main...<head sha>`; the GraphQL conversation's authors carry `login`
  (bots end in `[bot]`) and `__typename`.

`harness-pr autoland [P] [--dry-run] [--json]` (ticket #142) runs on the same fixture with a second eligible PR
(#57 on t/19-fix-the-frobber): the gate's answers plus what the land reads and writes (the merge, the merge
commit, the PR body, the ticket edit, a 404 for the ticket's parent). harness-board, harness-work and
terminal-notifier are stubs in fakebin that log their argv to STUB_LOG, so the Resolution and the notification
are read from there. The state file is read and written under the temporary HOME.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
TOOL = os.path.join(PLUGIN, "bin", "harness-pr")
LIB = os.path.join(PLUGIN, "lib")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
SLUG = "o/r"
LOGIN = "rjeskuri"
REVIEW_MARKER = "<!-- harness-review -->"
BOARD_MARKER = "<!-- harness -->"
PR = 34
TICKET = 12
BRANCH = "t/12-add-the-widget"
JSON_KEYS = {"pr", "verdict", "code", "reason", "rule", "head", "ticket"}
ON = {"HARNESS_AUTOLAND": "1"}

BINS = {}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# harness-board, harness-work and terminal-notifier for `harness-pr autoland`'s land: each logs its argv to STUB_LOG
# (when set) and prints nothing.
STUB = """import json, os, sys
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": os.path.basename(sys.argv[1]), "argv": sys.argv[2:]}) + "\\n")
"""


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-autoland-bin-"))
    fakebin = os.path.join(root, "fakebin")
    gh = executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    os.symlink(sys.executable, os.path.join(fakebin, "python3"))
    stub = write(os.path.join(root, "stub.py"), STUB)
    for name in ("harness-board", "harness-work", "terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$0" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, gh=gh)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


# ------------------------------------------------------------------ gh answers
def check_run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": f"https://github.com/o/r/actions/runs/9/job/{abs(hash(name)) % 1000}", "workflowName": "CI"}


def status_context(context, state):
    """A commit status (not a check run) as statusCheckRollup lists it."""
    return {"__typename": "StatusContext", "context": context, "state": state,
            "targetUrl": f"https://ci.example/{context}"}


def label_list(*names):
    return [{"id": f"L{i}", "name": n, "description": "", "color": "ededed"} for i, n in enumerate(names)]


def user(login):
    if login.endswith("[bot]"):
        return {"login": login, "__typename": "Bot"}
    return {"login": login, "__typename": "User"}


def gql_node(login, body, when="2026-10-24T11:00:00Z", **extra):
    node = {"author": user(login), "body": body, "createdAt": when,
            "url": f"https://github.com/o/r/pull/34#c{abs(hash(body)) % 100000}"}
    node.update(extra)
    return node


def connection(nodes, total=None):
    """A GraphQL connection as GitHub returns it when asked for totalCount and pageInfo { hasNextPage }: complete
    by default; `total` above the node count makes it a first page with more behind it."""
    nodes = list(nodes)
    total = len(nodes) if total is None else total
    return {"totalCount": total, "pageInfo": {"hasNextPage": total > len(nodes)}, "nodes": nodes}


def gql_thread(resolved, *comments, total=None):
    return {"isResolved": resolved, "comments": connection(comments, total)}


def graphql(threads=(), reviews=(), comments=(), totals=None):
    """The CONVERSATION answer; `totals` maps reviewThreads, reviews or comments to a totalCount above the nodes
    given (a truncated first page)."""
    totals = totals or {}
    return {"data": {"repository": {"pullRequest": {
        "reviewThreads": connection(threads, totals.get("reviewThreads")),
        "reviews": connection(reviews, totals.get("reviews")),
        "comments": connection(comments, totals.get("comments"))}}}}


def harness_review(commit_id, first_line, when="2026-10-24T10:00:00Z", rid=501, login=LOGIN):
    return {"id": rid, "user": {"login": login, "type": "User"}, "state": "COMMENTED", "commit_id": commit_id,
            "submitted_at": when, "body": f"{first_line}\n\nNot on a changed line:\n- `README.md` [low] Say it.\n\n"
                                          f"{REVIEW_MARKER}"}


LOW_ONLY = "Harness review (opus): 2 findings, 0 high, 0 medium, 2 low."
NO_FINDINGS = "Harness review: no findings. Checked: the diff against the ticket's definition of done"
ONE_LOW = "Harness review (sonnet): 1 finding, 0 high, 0 medium, 1 low."   # singular, as harness-pr review posts it

# The default HARNESS_TEST_PATTERNS (docs/design.md Settings; tests_first and harness-red hold the same list):
# tests/**, test/**, **/test_*.py, **/*_test.py, **/conftest.py, **/*_test.js, **/*.test.js, **/*.test.ts,
# **/*_test.ts, spec/**. conftest.py is left out here: it is also a manual path (rule 5 would stop first).
TEST_SHAPED = ["tests/test_a.py", "tests/helpers/fixture_data.py", "test/integration_check.py",
               "src/widget_test.py", "pkg/tests/test_x.py", "pkg/deep/er/test_y.py", "web/app.test.js",
               "web/app_test.js", "web/util.test.ts", "web/util_test.ts", "spec/models/user_spec.rb"]
# Paths that merely look like tests: outside every default pattern, so they count as non-test.
TEST_LOOKING = ["src/testing_utils.py", "src/latest.py", "pkg/tests_helper.py", "src/attest/check.py",
                "web/test.config.js", "src/my_tests.py", "lib/contest.py"]


def changed(name, additions=10, deletions=0, status="modified", previous=None):
    f = {"filename": name, "status": status, "additions": additions, "deletions": deletions,
         "changes": additions + deletions, "sha": "0" * 40}
    if previous:
        f["previous_filename"] = previous
    return f


# ------------------------------------------------------------------ the fixture
class GateFixture:
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-autoland-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, "src", "shared.py"), "VALUE = 1\n")
        write(os.path.join(self.main, ".claude", "settings.json"), json.dumps({"env": ON}, indent=2) + "\n")
        write(os.path.join(self.main, ".git", "info", "exclude"), ".claude/settings.local.json\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.work = os.path.join(self.tmp, "work")
        self.git(self.tmp, "clone", "-q", self.origin, self.work)
        self.head = self.ticket_branch(BRANCH, {"src/widget.py": "def frob():\n    return 2\n",
                                                "tests/test_widget.py": "def test_frob():\n    pass\n"})

    # -------------------------------------------------------------- git
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_"))}
        path = [BINS["fakebin"], os.path.dirname(GIT), "/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(path), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost", HARNESS_GH=BINS["gh"],
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log)
        env.update(extra)
        return env

    def git_run(self, repo, *args):
        return subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                              capture_output=True, text=True)

    def git(self, repo, *args):
        p = self.git_run(repo, *args)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def commit(self, files, message):
        """Write `files` in the work clone, commit them on its current branch; the new sha."""
        for path, text in files.items():
            write(os.path.join(self.work, path), text)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)
        return self.git(self.work, "rev-parse", "HEAD")

    def push(self, branch):
        self.git(self.work, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
        self.git(self.work, "fetch", "-q", "origin")

    def ticket_branch(self, name, files, start="origin/main"):
        """A branch `name` in the work clone from `start` with one commit of `files`, pushed; its sha."""
        self.git(self.work, "fetch", "-q", "origin")
        self.git(self.work, "switch", "-q", "-c", name, start)
        sha = self.commit(files, f"Work on {name}")
        self.push(name)
        return sha

    def advance_main(self, files, message="Main moves on"):
        """A new commit on origin's main (pushed from the work clone); the work clone returns to its branch."""
        back = self.git(self.work, "symbolic-ref", "--short", "HEAD")
        self.git(self.work, "switch", "-q", "main")
        self.git(self.work, "merge", "-q", "--ff-only", "origin/main")
        sha = self.commit(files, message)
        self.push("main")
        self.git(self.work, "switch", "-q", back)
        return sha

    def set_origin_settings(self, env):
        """Commit .claude/settings.json with `env` (no file for None) on origin's main."""
        back = self.git(self.work, "symbolic-ref", "--short", "HEAD")
        self.git(self.work, "switch", "-q", "main")
        self.git(self.work, "merge", "-q", "--ff-only", "origin/main")
        if env is None:
            self.git(self.work, "rm", "-q", ".claude/settings.json")
            self.git(self.work, "commit", "-q", "-m", "No settings")
        else:
            self.commit({".claude/settings.json": json.dumps({"env": env}, indent=2) + "\n"}, "Settings")
        self.push("main")
        self.git(self.work, "switch", "-q", back)

    def pr_commits(self, head):
        """The PR's commits as GitHub's commits endpoint lists them: origin/main..head, oldest first."""
        self.git(self.work, "fetch", "-q", "origin")
        out = self.git(self.work, "rev-list", "--reverse", "--parents", f"origin/main..{head}")
        commits = []
        for line in out.splitlines():
            sha, *parents = line.split()
            commits.append({"sha": sha, "parents": [{"sha": p} for p in parents],
                            "commit": {"message": "c", "author": {"date": "2026-10-24T09:00:00Z"},
                                       "committer": {"date": "2026-10-24T09:00:00Z"}}})
        return commits

    # -------------------------------------------------------------- gh answers
    def answers(self, pr=PR, ticket=TICKET, ref=BRANCH, head=None):
        """Every call answered for a PR that passes all ten rules."""
        head = head or self.head
        return {
            "pr": {"number": pr, "state": "OPEN", "isDraft": False, "headRefName": ref, "headRefOid": head,
                   "author": {"login": LOGIN, "is_bot": False}, "closingIssuesReferences": [
                       {"number": ticket, "url": f"https://github.com/o/r/issues/{ticket}"}],
                   "labels": label_list("enhancement"), "mergeable": "MERGEABLE",
                   "statusCheckRollup": [check_run("verify"), check_run("lint")]},
            "ticket_number": ticket,
            "ticket": {"state": "OPEN", "labels": label_list("task", "size:M", "risk:low", "status:in-review")},
            "incidents": [],
            "runs": [{"status": "completed", "conclusion": "success", "headSha": "f" * 40, "databaseId": 7}],
            "files": [changed("src/widget.py", 12, 2), changed("tests/test_widget.py", 20, 0)],
            "reviews": [harness_review(head, LOW_ONLY)],
            "commits": self.pr_commits(head),
            "conversation": graphql(
                reviews=[gql_node(LOGIN, f"{LOW_ONLY}\n\n{REVIEW_MARKER}")],
                comments=[gql_node(LOGIN, f"Pull request: https://github.com/o/r/pull/{pr}\n\n{BOARD_MARKER}")]),
            "behind": 0,
            "fail": {},
        }

    def calls(self, a):
        pr = a["pr"]
        n, head, ref = pr["number"], pr["headRefOid"], pr["headRefName"]
        base = f"repos/{SLUG}/pulls/{n}"
        login = a.get("login", LOGIN)                  # the authenticated account `gh api user` answers
        entries = [
            ("repo", {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": SLUG}}),
            ("user", {"match": ["api", "user"], "contains": ["--jq"], "stdout": login + "\n"}),
            ("user", {"match": ["api", "user"], "stdout_json": {"login": login, "type": "User"}}),
            ("pr", {"match": ["pr", "view", str(n)], "stdout_json": pr}),
            ("incidents", {"match": ["issue", "list"], "stdout_json": a["incidents"]}),
            ("deploy", {"match": ["run", "list"], "stdout_json": a["runs"]}),
            ("ticket", {"match": ["issue", "view", str(a["ticket_number"])], "stdout_json": a["ticket"]}),
            ("files", {"contains": [f"{base}/files?per_page=100"], "stdout_json": [a["files"]]}),
            ("reviews", {"contains": [f"{base}/reviews"], "stdout_json": a["reviews"]}),
            ("commits", {"contains": [f"{base}/commits"], "stdout_json": a["commits"]}),
            ("graphql", {"match": ["api", "graphql"], "stdout_json": a["conversation"]}),
        ]
        # Only the head sha form is answered: the ticket's `compare/main...<head>`, at the head the gate judges.
        # `compare/main...<headRefName>` gets no answer (fakegh: no match), so a gate asking by name waits.
        compare = f"repos/{SLUG}/compare/main...{head}"
        entries.append(("compare", {"contains": [compare, "--jq"], "stdout": f"{a['behind']}\n"}))
        entries.append(("compare", {"contains": [compare], "stdout_json": {
            "behind_by": a["behind"], "ahead_by": len(a["commits"]), "status": "ahead"}}))
        out = []
        for key, entry in entries:
            if key in a["fail"]:
                entry = {k: v for k, v in entry.items() if k in ("match", "contains")}
                entry.update(stderr=a["fail"][key], exit=1)
            out.append(entry)
        return out

    def scenario(self, a):
        write(self.scenario_path, json.dumps({"calls": self.calls(a)}))
        for leftover in (self.scenario_path + ".state.json", self.gh_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    # -------------------------------------------------------------- running the gate
    def run_tool(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.main, env=self.env(**env),
                              capture_output=True, text=True, timeout=120)

    def gate(self, a, pr=PR, **env):
        """The plain run: (process, its one line)."""
        self.scenario(a)
        p = self.run_tool("gate", str(pr), **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        return p, lines[0]

    def gate_json(self, a, pr=PR, **env):
        self.scenario(a)
        p = self.run_tool("gate", str(pr), "--json", **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        try:
            data = json.loads(p.stdout)
        except ValueError:
            self.fail(f"--json printed something other than JSON:\n{p.stdout}{p.stderr}")
        self.assertIsInstance(data, dict)
        self.assertLessEqual(JSON_KEYS, set(data), data)
        return data

    def assertVerdict(self, data, verdict, code, rule, *needles):
        self.assertEqual((data["verdict"], data["code"], data["rule"]), (verdict, code, rule), data)
        for needle in needles:
            self.assertIn(needle, data["reason"], data)

    def gh_argv(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]


class Gate(GateFixture, unittest.TestCase):
    # ============================================================== broad: the public interface
    def test_eligible_pr_prints_land(self):
        p, line = self.gate(self.answers())
        self.assertEqual(line, f"land: gate passed at {self.head[:7]}")

    def test_json_shape(self):
        other = self.ticket_branch("t/19-fix-the-frobber", {"src/frobber.py": "X = 19\n"})
        for pr, ticket, ref, head in ((PR, TICKET, BRANCH, self.head), (57, 19, "t/19-fix-the-frobber", other)):
            with self.subTest(pr=pr, ticket=ticket):
                data = self.gate_json(self.answers(pr=pr, ticket=ticket, ref=ref, head=head), pr=pr)
                self.assertEqual(data["pr"], pr)
                self.assertEqual(data["ticket"], ticket)
                self.assertEqual(data["head"], head)
                self.assertVerdict(data, "land", "land", 10)
                self.assertEqual(data["reason"], f"gate passed at {head[:7]}")
        with self.subTest("a verdict other than land keeps the shape"):
            a = self.answers()
            a["pr"]["isDraft"] = True
            data = self.gate_json(a)
            self.assertVerdict(data, "manual", "pr", 2, "draft")
            self.assertEqual((data["pr"], data["ticket"], data["head"]), (PR, TICKET, self.head))

    def test_bad_invocation_and_no_repository_exit_1(self):
        p = self.run_tool("gate", "--help")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("--json", p.stdout)
        nowhere = self.mk("not-a-repo")
        for name, args, cwd in (("no P", ["gate"], None), ("P not a number", ["gate", "abc"], None),
                                ("unknown option", ["gate", "34", "--bogus"], None),
                                ("no repository", ["gate", "34"], nowhere),
                                ("no repository, --json", ["gate", "34", "--json"], nowhere)):
            with self.subTest(name):
                self.scenario(self.answers())
                p = self.run_tool(*args, cwd=cwd)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)

    def test_not_opted_in_makes_no_pr_calls(self):
        settings = os.path.join(self.main, ".claude", "settings.json")
        local = os.path.join(self.main, ".claude", "settings.local.json")

        def committed_without():
            self.set_origin_settings({"HARNESS_AUTOLAND_SIZES": "S,M"})
            return {}

        def committed_zero():
            self.set_origin_settings({"HARNESS_AUTOLAND": "0"})
            return {}

        def no_settings_file():
            self.set_origin_settings(None)
            return {}

        def only_uncommitted_local_and_environment():
            self.set_origin_settings({})
            self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
            write(settings, json.dumps({"env": ON}))
            write(local, json.dumps({"env": ON}))
            return {"HARNESS_AUTOLAND": "1"}

        def an_unpushed_commit_on_main():
            self.git(self.main, "checkout", "-q", "--", ".")
            os.unlink(local)                                   # origin/main still says {} from the case before
            self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
            write(settings, json.dumps({"env": ON}))
            self.git(self.main, "add", "-A")
            self.git(self.main, "commit", "-q", "-m", "Local opt-in, not pushed")
            return {}

        for case in (committed_without, committed_zero, no_settings_file, only_uncommitted_local_and_environment,
                     an_unpushed_commit_on_main):
            with self.subTest(case.__name__):
                env = case()
                p, line = self.gate(self.answers(), **env)
                self.assertEqual(line, "manual: autoland off")
                self.assertEqual([a for a in self.gh_argv() if a[:1] == ["pr"]], [])
                data = self.gate_json(self.answers(), **env)
                self.assertVerdict(data, "manual", "off", 1)
                self.assertEqual(data["pr"], PR)
                self.assertEqual([a for a in self.gh_argv() if a[:1] == ["pr"]], [])

    def test_the_opt_in_is_read_from_origin_after_a_fetch(self):
        self.set_origin_settings({})
        self.git(self.main, "fetch", "-q", "origin")          # the main checkout now knows origin/main is off
        self.set_origin_settings(ON)                           # turned on elsewhere; the gate must fetch to see it
        p, line = self.gate(self.answers())
        self.assertEqual(line, f"land: gate passed at {self.head[:7]}")

    # ============================================================== each fact
    def test_each_fact_flips_the_verdict(self):
        def pr(**kw):
            return lambda a: a["pr"].update(kw)

        def ticket_labels(*names):
            return lambda a: a["ticket"].update(labels=label_list(*names))

        def put(key, value):
            return lambda a: a.update({key: value})

        def first_review(line):
            return lambda a: a["reviews"].__setitem__(0, harness_review(self.head, line))

        def append(key, node):
            def change(a):
                conn = a["conversation"]["data"]["repository"]["pullRequest"][key]
                conn["nodes"].append(node)
                conn["totalCount"] = len(conn["nodes"])    # still the whole connection
            return change

        def add_comment(login, body):
            return append("comments", gql_node(login, body))

        def add_thread(*comments):
            return append("reviewThreads", gql_thread(False, *comments))

        cases = [
            ("draft", pr(isDraft=True), ("manual", "pr", 2, "draft")),
            ("author", pr(author={"login": "someone-else", "is_bot": False}), ("manual", "pr", 2, "someone-else")),
            ("closing issue", pr(closingIssuesReferences=[]), ("manual", "pr", 2, "#12")),
            ("closing two issues", pr(closingIssuesReferences=[{"number": 12}, {"number": 13}]),
             ("manual", "pr", 2, "#13")),
            ("incident", put("incidents", [{"number": 40, "title": "Smoke check failed"}]),
             ("wait", "incident", 3, "#40")),
            ("deploy in flight", put("runs", [{"status": "in_progress", "conclusion": "", "headSha": "e" * 40}]),
             ("wait", "deploy", 3, "in flight")),
            ("failed deploy", put("runs", [{"status": "completed", "conclusion": "failure", "headSha": "e" * 40}]),
             ("wait", "deploy", 3, "failed")),
            ("risk:high", ticket_labels("task", "size:M", "risk:high"), ("manual", "risk", 4, "risk:high")),
            ("size:L", ticket_labels("task", "size:L", "risk:low"), ("manual", "size", 4, "L")),
            ("ticket closed", put("ticket", {"state": "CLOSED", "labels": label_list("size:M", "risk:low")}),
             ("manual", "ticket", 4, "CLOSED")),
            ("manual-land", pr(labels=label_list("enhancement", "manual-land")), ("manual", "label", 4)),
            ("manual-land on the ticket", ticket_labels("task", "size:S", "risk:low", "manual-land"),
             ("manual", "label", 4)),
            ("protected file", put("files", [changed("src/widget.py"), changed(".github/workflows/ci.yml", 1, 1)]),
             ("manual", "path", 5, ".github/workflows/ci.yml")),
            ("renamed protected file",
             put("files", [changed("src/widget.py"),
                           changed("ci/old-verify.sh", 0, 0, "renamed", previous="scripts/verify.sh")]),
             ("manual", "path", 5, "scripts/verify.sh")),
            ("findings", first_review("Harness review (opus): 3 findings, 1 high, 0 medium, 2 low."),
             ("fix", "findings", 6, "1 high")),
            ("medium finding", first_review("Harness review (sonnet): 4 findings, 0 high, 2 medium, 2 low."),
             ("fix", "findings", 6, "2 medium")),
            ("unreadable harness review", first_review("Harness looked at it and it seems fine"),
             ("manual", "review", 6)),
            ("no harness review", put("reviews", [{"id": 9, "user": {"login": "teammate"}, "state": "APPROVED",
                                                   "commit_id": self.head, "submitted_at": "2026-10-24T10:00:00Z",
                                                   "body": "LGTM"}]),
             ("manual", "review", 6)),
            ("owner comment", add_comment(LOGIN, "Hold this one, I want to look at the parser first."),
             ("manual", "comment", 7, "1 owner")),
            ("unresolved thread", add_thread(gql_node("teammate", "Is this right?", path="src/widget.py", line=2)),
             ("manual", "comment", 7, "1 unresolved")),
            ("conflicting", pr(mergeable="CONFLICTING"), ("fix", "conflict", 8)),
            ("mergeable not computed yet", pr(mergeable="UNKNOWN"), ("wait", "mergeable", 8, "UNKNOWN")),
            ("behind", put("behind", 3), ("wait", "behind", 8, "3")),
            ("pending", pr(statusCheckRollup=[check_run("lint"), check_run("verify", "IN_PROGRESS", "")]),
             ("wait", "ci", 9, "verify")),
            ("failed", pr(statusCheckRollup=[check_run("verify"), check_run("lint", conclusion="FAILURE")]),
             ("fix", "ci", 9, "lint")),
            ("no checks", pr(statusCheckRollup=[]), ("manual", "checks", 9)),
        ]
        for name, change, (verdict, code, rule, *needles) in cases:
            with self.subTest(name):
                a = self.answers()
                change(a)
                data = self.gate_json(a)
                self.assertVerdict(data, verdict, code, rule, *needles)
        with self.subTest("older review"):
            second = self.commit({"src/widget.py": "A = 2\n"}, "A plain commit after the review")
            self.push(BRANCH)
            a = self.answers(head=second)
            a["reviews"] = [harness_review(self.head, LOW_ONLY)]
            data = self.gate_json(a)
            self.assertVerdict(data, "wait", "re-check", 6, self.head[:7], second[:7])
            self.assertEqual(data["reason"], f"review of {self.head[:7]}, head {second[:7]}")
            self.assertEqual(data["head"], second)

    def test_the_latest_harness_review_decides(self):
        high = "Harness review (opus): 1 findings, 1 high, 0 medium, 0 low."
        newest_first = [harness_review(self.head, NO_FINDINGS, "2026-10-24T11:00:00Z", 2),
                        harness_review(self.head, high, "2026-10-24T10:00:00Z", 1)]
        newest_last = [harness_review(self.head, NO_FINDINGS, "2026-10-24T10:00:00Z", 1),
                       harness_review(self.head, high, "2026-10-24T11:00:00Z", 2)]
        one_high = "Harness review (opus): 1 finding, 1 high, 0 medium, 0 low."
        one_medium = "Harness review (opus): 1 finding, 0 high, 1 medium, 0 low."
        for name, reviews, expected in (("newest, listed first, has no findings", newest_first, ("land", "land", 10)),
                                        ("newest, listed last, has a high", newest_last, ("fix", "findings", 6)),
                                        ("one low finding, singular", [harness_review(self.head, ONE_LOW)],
                                         ("land", "land", 10)),
                                        ("one high finding, singular", [harness_review(self.head, one_high)],
                                         ("fix", "findings", 6, "1 high")),
                                        ("one medium finding, singular", [harness_review(self.head, one_medium)],
                                         ("fix", "findings", 6, "1 medium"))):
            with self.subTest(name):
                a = self.answers()
                a["reviews"] = reviews
                self.assertVerdict(self.gate_json(a), *expected)

    def test_only_the_authenticated_accounts_reviews_are_harness_reviews(self):
        """A review carries the harness marker in its body, which anyone can type: only reviews whose user.login is
        the `gh api user` login count. A newer marker-bearing review by another account never decides rule 6 (and,
        rule 6 coming before rule 7, the verdict is the real review's)."""
        high = "Harness review (opus): 3 findings, 1 high, 0 medium, 2 low."
        spoof_body = f"Harness review: no findings.\n\n{REVIEW_MARKER}"

        def spoof(login, when="2026-10-24T11:00:00Z", rid=777, kind="User"):
            return {"id": rid, "user": {"login": login, "type": kind}, "state": "COMMENTED", "commit_id": self.head,
                    "submitted_at": when, "body": spoof_body}

        def with_login(a, login):
            a["login"] = login
            a["pr"]["author"] = {"login": login, "is_bot": False}
            return a

        alt = "ryan-alt"
        cases = [
            ("a newer foreign no-findings review after the owner's high, listed last", LOGIN,
             [harness_review(self.head, high), spoof("mallory")], ("fix", "findings", 6, "1 high")),
            ("the same, the foreign review listed first", LOGIN,
             [spoof("mallory"), harness_review(self.head, high)], ("fix", "findings", 6, "1 high")),
            ("a newer bot review with the marker", LOGIN,
             [harness_review(self.head, high), spoof("github-actions[bot]", kind="Bot")],
             ("fix", "findings", 6, "1 high")),
            ("only a foreign marker-bearing review", LOGIN, [spoof("mallory")], ("manual", "review", 6)),
            ("another authenticated login: its own high, then a newer one by rjeskuri", alt,
             [harness_review(self.head, high, login=alt), spoof(LOGIN)], ("fix", "findings", 6, "1 high")),
            ("another authenticated login: only rjeskuri's review", alt,
             [harness_review(self.head, LOW_ONLY)], ("manual", "review", 6)),
            ("another authenticated login: its own review lands", alt,
             [harness_review(self.head, LOW_ONLY, login=alt)], ("land", "land", 10)),
        ]
        for name, login, reviews, expected in cases:
            with self.subTest(name):
                a = with_login(self.answers(), login)
                a["reviews"] = copy.deepcopy(reviews)
                # The conversation lists the same reviews, as GitHub's GraphQL would.
                a["conversation"]["data"]["repository"]["pullRequest"]["reviews"] = connection(
                    [gql_node(r["user"]["login"], r["body"], r["submitted_at"], state="COMMENTED") for r in reviews])
                self.assertVerdict(self.gate_json(a), *expected)

    # ============================================================== at head
    # Every scenario here runs on the one ticket branch BRANCH, so nothing in a ref name tells the cases apart: the
    # same branch goes through land states and re-check states, and what changes between them is the PR's
    # commits (and their trees) after the reviewed commit.
    def merge_main(self):
        """Merge origin/main into BRANCH in the work clone as GitHub's update-branch does; push; the sha."""
        self.git(self.work, "fetch", "-q", "origin")
        self.git(self.work, "merge", "-q", "--no-edit", "--no-ff", "origin/main")
        self.push(BRANCH)
        return self.git(self.work, "rev-parse", "HEAD")

    def judge(self, reviewed, head):
        """--json for BRANCH at `head` with the one harness review at `reviewed`, everything else passing."""
        a = self.answers(head=head)
        a["reviews"] = [harness_review(reviewed, LOW_ONLY)]
        data = self.gate_json(a)
        self.assertEqual(data["head"], head, data)
        return data

    def assertLands(self, data, head):
        self.assertVerdict(data, "land", "land", 10)
        self.assertEqual(data["reason"], f"gate passed at {head[:7]}")

    def assertRecheck(self, data, reviewed, head):
        self.assertVerdict(data, "wait", "re-check", 6)
        self.assertEqual(data["reason"], f"review of {reviewed[:7]}, head {head[:7]}")

    def test_update_branch_merge_keeps_the_review_at_head(self):
        reviewed = self.head
        with self.subTest("the reviewed commit is the head"):
            self.assertLands(self.judge(reviewed, reviewed), reviewed)
        self.advance_main({"src/elsewhere1.py": "E = 1\n"})
        once = self.merge_main()
        self.assertNotEqual(once, reviewed)
        with self.subTest("one update-branch merge"):
            self.assertLands(self.judge(reviewed, once), once)
        self.advance_main({"src/elsewhere2.py": "E = 2\n"})
        twice = self.merge_main()
        with self.subTest("two update-branch merges, the first of an older main"):
            self.assertLands(self.judge(reviewed, twice), twice)
        plain = self.commit({"src/widget.py": "def frob():\n    return 3\n"}, "Not reviewed")
        self.push(BRANCH)
        with self.subTest("then a plain commit"):
            self.assertRecheck(self.judge(reviewed, plain), reviewed, plain)
        self.advance_main({"src/elsewhere3.py": "E = 3\n"})
        after = self.merge_main()
        with self.subTest("then an update-branch merge on top of the plain commit"):
            self.assertRecheck(self.judge(reviewed, after), reviewed, after)
        with self.subTest("a review of the plain commit covers the merge after it"):
            self.assertLands(self.judge(plain, after), after)

    def test_conflict_resolution_merge_needs_a_recheck(self):
        reviewed = self.commit({"src/shared.py": "VALUE = 'branch'\n"}, "Change the shared value")
        self.push(BRANCH)
        self.advance_main({"src/shared.py": "VALUE = 'main'\n"})
        self.git(self.work, "fetch", "-q", "origin")
        merge = self.git_run(self.work, "merge", "--no-edit", "origin/main")
        self.assertNotEqual(merge.returncode, 0, "the fixture should conflict")
        write(os.path.join(self.work, "src", "shared.py"), "VALUE = 'resolved'\n")
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "--no-edit")
        self.push(BRANCH)
        resolved = self.git(self.work, "rev-parse", "HEAD")
        self.assertEqual(len(self.git(self.work, "rev-list", "--parents", "-n1", resolved).split()), 3)
        with self.subTest("a merge whose conflict was resolved by hand"):
            self.assertRecheck(self.judge(reviewed, resolved), reviewed, resolved)
        with self.subTest("re-reviewed at the resolved merge"):
            self.assertLands(self.judge(resolved, resolved), resolved)
        self.advance_main({"src/other_side.py": "OTHER = 1\n"})
        self.git(self.work, "fetch", "-q", "origin")
        self.git(self.work, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
        write(os.path.join(self.work, "src", "widget.py"), "def frob():\n    return 99  # never reviewed\n")
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "--no-edit")
        self.push(BRANCH)
        evil = self.git(self.work, "rev-parse", "HEAD")
        self.assertEqual(len(self.git(self.work, "rev-list", "--parents", "-n1", evil).split()), 3)
        with self.subTest("a clean merge of main with an extra change folded in"):
            self.assertRecheck(self.judge(resolved, evil), resolved, evil)
        self.git(self.work, "reset", "-q", "--hard", resolved)
        clean = self.merge_main()                          # the same main, merged without the extra change
        self.assertNotEqual(clean, evil)
        with self.subTest("the same merge of main without the extra change"):
            self.assertLands(self.judge(resolved, clean), clean)

    def test_other_commits_after_the_review_need_a_recheck(self):
        reviewed = self.head
        with self.subTest("a clean merge of a branch that is not main"):
            self.git(self.work, "switch", "-q", "-c", "side", "origin/main")
            self.commit({"src/side_b.py": "B = 1\n"}, "A side branch, never on main")
            self.git(self.work, "switch", "-q", BRANCH)
            self.git(self.work, "merge", "-q", "--no-edit", "--no-ff", "side")
            self.push(BRANCH)
            head = self.git(self.work, "rev-parse", "HEAD")
            self.assertRecheck(self.judge(reviewed, head), reviewed, head)
        with self.subTest("main merged the other way round: main first, the reviewed commit second"):
            self.git(self.work, "reset", "-q", "--hard", reviewed)
            self.advance_main({"src/elsewhere4.py": "E = 4\n"})
            self.git(self.work, "switch", "-q", "-c", "swapped", "origin/main")
            self.git(self.work, "merge", "-q", "--no-edit", "--no-ff", reviewed)
            head = self.git(self.work, "rev-parse", "HEAD")
            self.git(self.work, "switch", "-q", BRANCH)
            self.git(self.work, "reset", "-q", "--hard", head)
            self.push(BRANCH)
            parents = self.git(self.work, "rev-list", "--parents", "-n1", head).split()[1:]
            self.assertEqual(parents[1], reviewed)
            self.assertRecheck(self.judge(reviewed, head), reviewed, head)
        with self.subTest("a review of a commit that is not in the pull request"):
            self.git(self.work, "reset", "-q", "--hard", reviewed)
            self.push(BRANCH)
            stranger = self.git(self.work, "rev-parse", "origin/main")
            self.assertRecheck(self.judge(stranger, reviewed), stranger, reviewed)

    # ============================================================== comments
    def conversation_of(self, threads=(), reviews=(), comments=()):
        return graphql(threads=threads, reviews=reviews, comments=comments)

    def test_harness_and_bot_comments_are_not_owner_comments(self):
        harness_reviews = [gql_node(LOGIN, f"{LOW_ONLY}\n\n{REVIEW_MARKER}", state="COMMENTED")]
        harness_comments = [gql_node(LOGIN, f"Pull request: https://github.com/o/r/pull/34\n\n{BOARD_MARKER}"),
                            gql_node(LOGIN, f"PR #34 ready for review: https://github.com/o/r/pull/34\n\n"
                                            f"{BOARD_MARKER}")]
        noise = dict(
            threads=[gql_thread(True, gql_node("teammate", "nit: rename this", path="src/widget.py", line=1)),
                     gql_thread(False, gql_node(LOGIN, f"[low] Assert the value.\n\n{REVIEW_MARKER}",
                                                path="tests/test_widget.py", line=2)),
                     gql_thread(False, gql_node("coderabbitai[bot]", "Consider a docstring.",
                                                path="src/widget.py", line=1))],
            reviews=harness_reviews + [
                gql_node("copilot-pull-request-reviewer[bot]", "Copilot reviewed 2 out of 2 changed files.",
                         state="COMMENTED"),
                gql_node(LOGIN, "", state="APPROVED"),
                gql_node("teammate", "", state="APPROVED")],
            comments=harness_comments + [gql_node("github-actions[bot]", "Preview deployed to https://preview.example/34"),
                                         gql_node("dependabot[bot]", "Rebasing.")])
        teammate_comment = gql_node("teammate", "Did you try the old parser?")
        owner_review = gql_node(LOGIN, "Hold on, I want to look at this.", state="COMMENTED")
        todo_comment = gql_node(LOGIN, "Ship it after the release.\n<!-- todo: remind me -->")
        harness_first_thread = gql_thread(
            False, gql_node(LOGIN, f"[low] Because.\n\n{REVIEW_MARKER}", path="src/widget.py", line=2),
            gql_node("teammate", "I disagree, see the old parser.", path="src/widget.py", line=2))
        human_first_thread = gql_thread(
            False, gql_node("teammate", "Why 2?", path="src/widget.py", line=2),
            gql_node(LOGIN, f"[low] Because.\n\n{REVIEW_MARKER}", path="src/widget.py", line=2))
        bot_then_harness_thread = gql_thread(
            False, gql_node("coderabbitai[bot]", "Nit.", path="src/widget.py", line=3),
            gql_node(LOGIN, f"[low] Agreed.\n\n{REVIEW_MARKER}", path="src/widget.py", line=3))
        cases = [
            ("harness, bot, empty-bodied and resolved only", {}, None),
            ("a teammate's PR comment", {"comments": [teammate_comment]}, (0, 1)),
            ("the owner's review with a body", {"reviews": [owner_review]}, (0, 1)),
            ("a comment holding an unrelated HTML comment", {"comments": [todo_comment]}, (0, 1)),
            ("a thread a harness comment opened, a person answered", {"threads": [harness_first_thread]}, (1, 0)),
            ("a thread a person opened, the harness answered", {"threads": [human_first_thread]}, (1, 0)),
            ("a thread of a bot and the harness only", {"threads": [bot_then_harness_thread]}, None),
            ("all of them", {"comments": [teammate_comment, todo_comment], "reviews": [owner_review],
                             "threads": [harness_first_thread, human_first_thread, bot_then_harness_thread]},
             (2, 3)),
        ]
        for name, extra, counts in cases:
            with self.subTest(name):
                parts = copy.deepcopy(noise)
                for key, nodes in extra.items():
                    parts[key] = parts[key] + copy.deepcopy(nodes)
                a = self.answers()
                a["conversation"] = self.conversation_of(**parts)
                data = self.gate_json(a)
                if counts is None:
                    self.assertVerdict(data, "land", "land", 10)
                else:
                    self.assertVerdict(data, "manual", "comment", 7)
                    self.assertEqual(data["reason"],
                                     f"{counts[0]} unresolved threads, {counts[1]} owner comments")

    def graphql_query(self):
        """The query text of the gate's `gh api graphql` call (an argv `query=` field or stdin), or ''."""
        with open(self.gh_log, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        for entry in entries:
            if entry["argv"][:2] == ["api", "graphql"]:
                fields = [t[len("query="):] for t in entry["argv"] if t.startswith("query=")]
                return "\n".join(fields) + (entry.get("stdin") or "")
        return ""

    def test_a_conversation_longer_than_one_page_does_not_land(self):
        """The conversation is read one page per connection (review threads, each thread's comments, reviews, PR
        comments). When any connection has more than that page holds (pageInfo.hasNextPage, totalCount above the
        nodes returned), the unread rest may hold an owner comment: rule 7 does not pass, and the reason says the
        conversation is too long to read. The visible nodes are all harness, bot or resolved, so only the
        truncation can stop it."""
        harness_review_node = gql_node(LOGIN, f"{LOW_ONLY}\n\n{REVIEW_MARKER}", state="COMMENTED")
        board = gql_node(LOGIN, f"Pull request: https://github.com/o/r/pull/34\n\n{BOARD_MARKER}")
        resolved = gql_thread(True, gql_node("teammate", "nit: rename this", path="src/widget.py", line=1))

        def harness_thread(total=None):
            return gql_thread(False, gql_node(LOGIN, f"[low] Assert the value.\n\n{REVIEW_MARKER}",
                                              path="tests/test_widget.py", line=2), total=total)

        def conversation(threads_total=None, thread_comments_total=None, reviews_total=None, comments_total=None):
            totals = {k: v for k, v in (("reviewThreads", threads_total), ("reviews", reviews_total),
                                        ("comments", comments_total)) if v is not None}
            return graphql(threads=[resolved, harness_thread(thread_comments_total)],
                           reviews=[harness_review_node], comments=[board], totals=totals)

        with self.subTest("every connection complete lands"):
            a = self.answers()
            a["conversation"] = conversation()
            self.assertVerdict(self.gate_json(a), "land", "land", 10)
        for name, totals in (("more review threads than the first page", {"threads_total": 101}),
                             ("more comments in a thread than its first page", {"thread_comments_total": 51}),
                             ("more reviews than the first page", {"reviews_total": 51}),
                             ("more PR comments than the first page", {"comments_total": 101})):
            with self.subTest(name):
                a = self.answers()
                a["conversation"] = conversation(**totals)
                data = self.gate_json(a)
                self.assertNotEqual(data["verdict"], "land", data)
                self.assertEqual((data["code"], data["rule"]), ("comment", 7), data)
                self.assertIn("too long", data["reason"].lower(), data)
        with self.subTest("the query asks GitHub for each connection's page info"):
            # GitHub returns only the fields asked for: without them a truncated page looks complete.
            query = self.graphql_query()
            self.assertGreaterEqual(max(query.count("totalCount"), query.count("hasNextPage")), 4, query)

    # ============================================================== checks
    def test_checks_follow_harness_pr_check_entries_and_overall(self):
        """statusCheckRollup read as harness-pr checks reads it (check_entries, overall): NEUTRAL and SKIPPED pass,
        CANCELLED and the other failed conclusions fail, a status context by its state, a failure wins over
        pending."""
        ok = check_run("verify")
        cases = [
            ("neutral and skipped pass", [ok, check_run("lint", conclusion="NEUTRAL"),
                                          check_run("docs", conclusion="SKIPPED")], ("land", "land", 10), None),
            ("only skipped passes", [check_run("docs", conclusion="SKIPPED")], ("land", "land", 10), None),
            ("a successful status context", [ok, status_context("ci/circleci", "SUCCESS")],
             ("land", "land", 10), None),
            ("a pending status context", [ok, status_context("ci/circleci", "PENDING")],
             ("wait", "ci", 9), "pending: ci/circleci"),
            ("an expected status context", [ok, status_context("required/gate", "EXPECTED")],
             ("wait", "ci", 9), "pending: required/gate"),
            ("a status context in error", [ok, status_context("codecov/patch", "ERROR")],
             ("fix", "ci", 9), "failed: codecov/patch"),
            ("a failed status context", [ok, status_context("ci/circleci", "FAILURE")],
             ("fix", "ci", 9), "failed: ci/circleci"),
            ("a cancelled check run", [check_run("lint"), check_run("verify", conclusion="CANCELLED")],
             ("fix", "ci", 9), "failed: verify"),
            ("a timed-out check run", [ok, check_run("e2e", conclusion="TIMED_OUT")],
             ("fix", "ci", 9), "failed: e2e"),
            ("a queued check run", [ok, check_run("e2e", "QUEUED", "")], ("wait", "ci", 9), "pending: e2e"),
            ("completed with a conclusion that is neither", [ok, check_run("e2e", conclusion="STALE")],
             ("wait", "ci", 9), "pending: e2e"),
            ("a failure wins over pending", [check_run("e2e", "IN_PROGRESS", ""), status_context("lint", "FAILURE"),
                                             ok], ("fix", "ci", 9), "failed: lint"),
            ("two pending, both named", [check_run("e2e", "IN_PROGRESS", ""), status_context("deploy/preview",
                                                                                            "PENDING")],
             ("wait", "ci", 9), "pending: e2e, deploy/preview"),
        ]
        for name, rollup, expected, reason in cases:
            with self.subTest(name):
                a = self.answers()
                a["pr"]["statusCheckRollup"] = rollup
                data = self.gate_json(a)
                self.assertVerdict(data, *expected)
                if reason is not None:
                    self.assertEqual(data["reason"], reason)

    # ============================================================== failing calls
    def test_a_failing_gh_call_waits_and_names_it(self):
        cases = [
            ("pr", "gh pr view 34",
             "GraphQL: Could not resolve to a PullRequest with the number of 34. (repository.pullRequest)"),
            ("incidents", "gh issue list", "HTTP 502: Bad Gateway (https://api.github.com/graphql)"),
            ("ticket", "gh issue view 12", "GraphQL: Could not resolve to an issue or pull request (12)"),
            ("deploy", "gh run list", "HTTP 500: Internal Server Error (https://api.github.com/repos/o/r/actions)"),
            ("files", "gh api repos/o/r/pulls/34/files?per_page=100", "HTTP 404: Not Found (files)"),
            ("reviews", "gh api repos/o/r/pulls/34/reviews", "HTTP 503: Service Unavailable (reviews)"),
            ("commits", "gh api repos/o/r/pulls/34/commits", "HTTP 401: Bad credentials (commits)"),
            ("graphql", "gh api graphql", "error connecting to api.github.com"),
            ("compare", f"gh api repos/o/r/compare/main...{self.head}", "HTTP 404: Not Found (compare)"),
        ]
        for key, call, first in cases:
            with self.subTest(key):
                a = self.answers()
                a["fail"] = {key: f"\n{first}\ncheck your internet connection or https://githubstatus.com\n"}
                p, line = self.gate(a)
                self.assertEqual(line, f"wait: {call} failed: {first}")
                data = self.gate_json(a)
                # rule is not pinned: docs/design.md gives no rule number to a failed gather.
                self.assertEqual((data["verdict"], data["code"]), ("wait", "gh"), data)
                self.assertEqual(data["reason"], f"{call} failed: {first}")
        with self.subTest("git fetch"):
            self.git(self.main, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
            data = self.gate_json(self.answers())
            self.assertEqual((data["verdict"], data["code"]), ("wait", "gh"), data)
            self.assertIn("git fetch", data["reason"])
            self.assertIn("failed: fatal:", data["reason"])

    def test_no_deploy_workflow_passes_rule_three(self):
        """`gh run list --workflow deploy.yml` (a file name selector) in a repository without that workflow: gh 2.99
        exits 1 with the API's 404 for the workflow (what the gate sees), not the `could not find any workflows
        named` of a name selector."""
        a = self.answers()
        a["fail"] = {"deploy": f"HTTP 404: workflow deploy.yml not found on the default branch "
                               f"(https://api.github.com/repos/{SLUG}/actions/workflows/deploy.yml)\n"}
        with self.subTest("the 404 for deploy.yml: no deploy workflow"):
            p, line = self.gate(a)
            self.assertEqual(line, f"land: gate passed at {self.head[:7]}")
            self.assertVerdict(self.gate_json(a), "land", "land", 10)
        others = [
            "HTTP 500: Internal Server Error (https://api.github.com/repos/o/r/actions/workflows/deploy.yml)",
            "HTTP 404: Not Found (https://api.github.com/repos/o/r/actions/runs?branch=main&per_page=1)",
            "HTTP 404: workflow smoke.yml not found on the default branch "
            "(https://api.github.com/repos/o/r/actions/workflows/smoke.yml)",
            "HTTP 401: Bad credentials (https://api.github.com/repos/o/r/actions/workflows/deploy.yml)",
        ]
        for first in others:
            with self.subTest("any other run list failure still waits", stderr=first):
                a["fail"] = {"deploy": f"{first}\n"}
                data = self.gate_json(a)
                self.assertEqual((data["verdict"], data["code"]), ("wait", "gh"), data)
                self.assertEqual(data["reason"], f"gh run list failed: {first}")

    # ============================================================== non-test files
    def test_nontest_files_count_source_files_only(self):
        """Test files are the paths the default HARNESS_TEST_PATTERNS match, in every shape they cover; a path that
        only looks like a test counts as source."""
        tests = [changed(name, 200, 50) for name in TEST_SHAPED]
        with self.subTest("3 source files and every test shape land"):
            a = self.answers()
            a["files"] = [changed(f"src/mod{i}.py", 8, 2) for i in range(3)] + tests
            self.assertVerdict(self.gate_json(a), "land", "land", 10)
        with self.subTest("8 source files, 7 of them test-looking, land"):
            a = self.answers()
            a["files"] = [changed("src/mod0.py", 3, 1)] + [changed(n, 3, 1) for n in TEST_LOOKING] + tests
            self.assertVerdict(self.gate_json(a), "land", "land", 10)
        with self.subTest("9 source files, 7 of them test-looking, are too large"):
            a = self.answers()
            a["files"] = ([changed(f"src/mod{i}.py", 3, 1) for i in range(2)]
                          + [changed(n, 3, 1) for n in TEST_LOOKING] + tests[:2])
            self.assertVerdict(self.gate_json(a), "manual", "too large", 5, "9 non-test files", "36 non-test lines")
        with self.subTest("401 source lines are too large, test lines never count"):
            a = self.answers()
            a["files"] = [changed("src/big.py", 250, 0), changed("src/testing_utils.py", 100, 51)] + tests
            self.assertVerdict(self.gate_json(a), "manual", "too large", 5, "2 non-test files", "401 non-test lines")
        with self.subTest("400 source lines land"):
            a = self.answers()
            a["files"] = [changed("src/big.py", 250, 0), changed("src/testing_utils.py", 100, 50)] + tests
            self.assertVerdict(self.gate_json(a), "land", "land", 10)

    def gather(self, a):
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        autoland = importlib.import_module("harness_lib.autoland")
        self.assertTrue(hasattr(autoland, "gather_facts"), "harness_lib.autoland has no gather_facts")
        self.scenario(a)
        env = {k: v for k, v in self.env().items() if k.startswith(("HARNESS_GH", "FAKEGH_", "GIT_"))}
        with mock.patch.dict(os.environ, env):
            return autoland.gather_facts(self.main, SLUG, PR)

    def test_gather_facts_reports_nontest_files_and_lines(self):
        """In-process: nothing the gate prints carries the facts, and the ticket names the count. Expected values
        are summed here from the fixture tables, file by file."""
        cases = [
            ("3 source files and 5 test files",
             [("src/a.py", 4, 1), ("src/b.py", 10, 0), ("lib/c.py", 0, 7)]
             + [(f"tests/test_x{i}.py", 30, 3) for i in range(5)], 3, 22),
            ("test-looking source and every test shape",
             [(n, i + 1, 2 * i) for i, n in enumerate(TEST_LOOKING)]
             + [(n, 40, 9) for n in TEST_SHAPED], len(TEST_LOOKING),
             sum(i + 1 + 2 * i for i in range(len(TEST_LOOKING)))),
            ("only test files", [(n, 5, 5) for n in TEST_SHAPED], 0, 0),
            ("one source file among the tests", [("src/only.py", 1, 0)] + [(n, 7, 7) for n in TEST_SHAPED], 1, 1),
        ]
        for name, rows, files, lines in cases:
            with self.subTest(name):
                a = self.answers()
                a["files"] = [changed(path, add, rm) for path, add, rm in rows]
                facts = self.gather(a)
                self.assertEqual(facts["nontest_files"], files, facts)
                self.assertEqual(facts["nontest_lines"], lines, facts)
                self.assertEqual(facts["head_sha"], self.head)
                self.assertEqual(sorted(facts["files"]), sorted(path for path, _, _ in rows))


# ------------------------------------------------------------------ autoland
SECOND = 57
SECOND_TICKET = 19
SECOND_BRANCH = "t/19-fix-the-frobber"
TITLES = {PR: "Add the widget (#12)", SECOND: "Fix the frobber (#19)"}
MERGE_SHAS = {PR: "abc123def4567890abc123def4567890abc12345", SECOND: "0123456789abcdef0123456789abcdef01234567"}
WRITES = (["pr", "merge"], ["pr", "edit"], ["pr", "ready"], ["pr", "comment"], ["issue", "edit"],
          ["issue", "comment"], ["issue", "close"])


def pr_body(ticket):
    return (f"## Summary\n\nDoes the work of #{ticket}.\n\nCloses #{ticket}\n\n## Definition of done\n\n"
            f"- [x] It works\n\n## Tests\n\nRed run before implementation (assertion, exit 1):\n\n```\nFAILED\n```\n\n"
            f"Verify:\n\n```\nRan 9 tests in 0.1s\n\nOK\n```\n")


class Autoland(GateFixture, unittest.TestCase):
    def setUp(self):
        self.stub_log = os.devnull                     # until the fixture's tmp exists
        super().setUp()
        self.stub_log = os.path.join(self.tmp, "stub.log")
        self.second = self.ticket_branch(SECOND_BRANCH, {"src/frobber.py": "X = 19\n"})
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        paths = importlib.import_module("harness_lib.paths")
        self.state_dir = os.path.join(self.home, ".claude", "harness", "repos", paths.repo_key(self.main))
        self.state_file = os.path.join(self.state_dir, "autoland.json")

    def env(self, **extra):
        return super().env(STUB_LOG=self.stub_log, **extra)

    # -------------------------------------------------------------- the scenario
    def pr_answers(self, pr=PR, state="OPEN"):
        """The gate's answers for #34 (ticket 12) or #57 (ticket 19), every rule passing, with a title and url."""
        if pr == PR:
            a = self.answers()
        else:
            a = self.answers(pr=SECOND, ticket=SECOND_TICKET, ref=SECOND_BRANCH, head=self.second)
        a["pr"].update(state=state, title=TITLES[pr], url=f"https://github.com/o/r/pull/{pr}")
        return a

    def land_entries(self, a):
        """What the land reads and writes beyond the gate's answers."""
        n, t = a["pr"]["number"], a["ticket_number"]
        return [
            {"match": ["pr", "view", str(n), "--json", "mergeCommit"],
             "stdout_json": {"mergeCommit": {"oid": MERGE_SHAS[n]}}},
            {"match": ["pr", "view", str(n), "--json", "body,files,url"],
             "stdout_json": {"body": pr_body(t), "files": [], "url": a["pr"]["url"]}},
            {"match": ["pr", "merge", str(n)], "stdout": ""},
            {"contains": [f"repos/{SLUG}/pulls/{n}/reviews/501/comments"], "stdout_json": []},
            {"match": ["issue", "edit", str(t)], "stdout": ""},
            {"match": ["api", f"repos/{SLUG}/issues/{t}/parent"], "stderr": "gh: Not Found (HTTP 404)\n", "exit": 1},
        ]

    def autoland_scenario(self, prs, listed=None):
        """`prs`: the answers of each PR; `listed`: what gh pr list prints (default: each of them, highest first)."""
        if listed is None:
            listed = [{"number": a["pr"]["number"], "headRefName": a["pr"]["headRefName"], "isDraft": False,
                       "title": a["pr"]["title"]} for a in reversed(prs)]
        calls = [entry for a in prs for entry in self.land_entries(a)]
        calls.append({"match": ["pr", "list"], "stdout_json": listed})
        for a in prs:
            calls += self.calls(a)
        write(self.scenario_path, json.dumps({"calls": calls}))
        for leftover in (self.scenario_path + ".state.json", self.gh_log, self.stub_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def autoland(self, *args, code=0):
        p = self.run_tool("autoland", *args)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    # -------------------------------------------------------------- reading what happened
    def merges(self):
        return [a for a in self.gh_argv() if a[:2] == ["pr", "merge"]]

    def writes(self):
        return [a for a in self.gh_argv() if a[:2] in WRITES]

    def stubs(self, tool):
        if not os.path.exists(self.stub_log):
            return []
        with open(self.stub_log, encoding="utf-8") as fh:
            return [e["argv"] for e in (json.loads(line) for line in fh if line.strip()) if e["tool"] == tool]

    def state(self):
        with open(self.state_file, encoding="utf-8") as fh:
            return json.load(fh)

    def landed_line(self, sha):
        return f"land: gate passed at {sha[:7]}"

    def dirty_worktree(self):
        """A worktree of BRANCH next to the main checkout with an untracked file: (its path, the file)."""
        wt = os.path.join(self.tmp, "wt-12")
        self.git(self.main, "fetch", "-q", "origin")
        self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, wt, f"origin/{BRANCH}")
        return wt, write(os.path.join(wt, "notes.txt"), "unsaved\n")

    # -------------------------------------------------------------- the tests
    def test_lands_the_first_eligible_pr_and_only_one_per_pass(self):
        prs = [self.pr_answers(PR), self.pr_answers(SECOND)]
        listed = [{"number": SECOND, "headRefName": SECOND_BRANCH, "isDraft": False, "title": TITLES[SECOND]},
                  {"number": 60, "headRefName": "t/60-a-draft", "isDraft": True, "title": "A draft"},
                  {"number": 61, "headRefName": "feature/not-a-ticket", "isDraft": False, "title": "Not a ticket"},
                  {"number": PR, "headRefName": BRANCH, "isDraft": False, "title": TITLES[PR]}]
        self.autoland_scenario(prs, listed)
        p = self.autoland()
        self.assertEqual(p.stdout.splitlines(), [f"#{PR} {self.landed_line(self.head)}",
                                                 f"#{SECOND} {self.landed_line(self.second)}",
                                                 f"#{PR} landed"], p.stdout + p.stderr)
        self.assertEqual(self.merges(), [["pr", "merge", str(PR), "-R", SLUG, "--squash", "--delete-branch"]])
        listing = [a for a in self.gh_argv() if a[:2] == ["pr", "list"]]
        self.assertEqual(len(listing), 1, listing)
        self.assertEqual(listing[0][:7], ["pr", "list", "--author", "@me", "--state", "open", "--json"])
        self.assertLessEqual({"number", "headRefName", "isDraft"}, set(listing[0][7].split(",")))
        viewed = {a[2] for a in self.gh_argv() if a[:2] == ["pr", "view"]}
        self.assertEqual(viewed, {str(PR), str(SECOND)})
        with self.subTest("--json: a list, one entry per PR"):
            os.unlink(self.state_file)
            self.autoland_scenario(prs, listed)
            data = json.loads(self.autoland("--json").stdout)
            self.assertIsInstance(data, list)
            self.assertEqual([(d["pr"], d["verdict"]) for d in data], [(PR, "land"), (SECOND, "land")])
            self.assertEqual(data[0]["action"], "landed")
            self.assertIsNone(data[1]["action"])
            self.assertEqual(len(self.merges()), 1)
        with self.subTest("P names the one PR: no list"):
            self.autoland_scenario(prs)
            p = self.autoland(str(SECOND))
            self.assertEqual(p.stdout.splitlines(), [f"#{SECOND} {self.landed_line(self.second)}",
                                                     f"#{SECOND} landed"])
            self.assertEqual([a for a in self.gh_argv() if a[:2] == ["pr", "list"]], [])
            self.assertEqual(self.merges(), [["pr", "merge", str(SECOND), "-R", SLUG, "--squash", "--delete-branch"]])

    def test_records_each_verdict_in_the_state_file(self):
        earlier = [{"pr": i, "ticket": i, "title": f"Old {i}", "sha": "e" * 40, "at": "2026-10-01T00:00:00Z"}
                   for i in range(1, 51)]
        write(self.state_file, json.dumps({"future": {"kept": True}, "landed": earlier, "cleanup_pending": None,
                                           "prs": {"99": {"verdict": "land"}}}))
        risky = self.pr_answers(SECOND)
        risky["ticket"]["labels"] = label_list("task", "size:M", "risk:high")
        self.autoland_scenario([self.pr_answers(PR), risky])
        p = self.autoland()
        self.assertIn(f"#{SECOND} manual: risk risk:high, not risk:low", p.stdout.splitlines())
        state = self.state()
        self.assertEqual(state["future"], {"kept": True})
        self.assertIsNone(state["cleanup_pending"])
        self.assertTrue(state["updated_at"].endswith("Z"), state)
        first = state["prs"][str(PR)]
        self.assertEqual({k: first[k] for k in ("head", "verdict", "code", "reason", "title", "ticket")},
                         {"head": self.head, "verdict": "land", "code": "land",
                          "reason": f"gate passed at {self.head[:7]}", "title": TITLES[PR], "ticket": TICKET})
        second = state["prs"][str(SECOND)]
        self.assertEqual({k: second[k] for k in ("head", "verdict", "code", "reason", "title", "ticket")},
                         {"head": self.second, "verdict": "manual", "code": "risk",
                          "reason": "risk risk:high, not risk:low", "title": TITLES[SECOND], "ticket": SECOND_TICKET})
        for record in (first, second):
            self.assertTrue(record["at"].endswith("Z"), record)
        self.assertEqual(len(state["landed"]), 50)
        self.assertEqual(state["landed"][0]["pr"], 2)                      # the oldest went
        newest = state["landed"][-1]
        self.assertEqual({k: newest[k] for k in ("pr", "ticket", "title", "sha")},
                         {"pr": PR, "ticket": TICKET, "title": TITLES[PR], "sha": MERGE_SHAS[PR]})
        self.assertTrue(newest["at"].endswith("Z"))

    def test_not_opted_in_does_nothing(self):
        self.set_origin_settings({"HARNESS_AUTOLAND": "0"})
        for args in ((), (str(PR),), ("--dry-run",)):
            with self.subTest(args=args):
                self.autoland_scenario([self.pr_answers(PR), self.pr_answers(SECOND)])
                p = self.autoland(*args)
                self.assertEqual(p.stdout.splitlines(), ["autoland off in proj"])
                self.assertEqual([a for a in self.gh_argv() if a[:1] == ["pr"]], [])
                self.assertFalse(os.path.exists(self.state_file))

    def test_dry_run_merges_nothing_and_writes_no_state(self):
        before = json.dumps({"future": 1, "prs": {}, "landed": [], "cleanup_pending": None}, indent=2) + "\n"
        write(self.state_file, before)
        self.autoland_scenario([self.pr_answers(PR), self.pr_answers(SECOND)])
        p = self.autoland("--dry-run")
        self.assertEqual(p.stdout.splitlines(), [f"#{PR} {self.landed_line(self.head)}",
                                                 f"#{SECOND} {self.landed_line(self.second)}",
                                                 f"#{PR} would land"])
        with self.subTest("--json"):
            self.autoland_scenario([self.pr_answers(PR), self.pr_answers(SECOND)])
            data = json.loads(self.autoland("--dry-run", "--json").stdout)
            self.assertEqual([(d["pr"], d["action"]) for d in data], [(PR, "would land"), (SECOND, None)])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.stubs("harness-board"), [])
        self.assertEqual(self.stubs("terminal-notifier"), [])
        with open(self.state_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_held_lock_skips_the_pass(self):
        import fcntl
        self.autoland_scenario([self.pr_answers(PR)])
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "autoland.lock"), "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = self.autoland()
            self.assertEqual(p.stdout.splitlines(), ["autoland: another pass is running in proj"])
            self.assertEqual(self.gh_argv(), [])
            self.assertFalse(os.path.exists(self.state_file))
        p = self.autoland()                                       # released: the next pass runs
        self.assertIn(f"#{PR} landed", p.stdout.splitlines())

    def test_resolution_says_landed_by_autoland(self):
        self.autoland_scenario([self.pr_answers(PR)])
        self.autoland()
        comments = [a for a in self.stubs("harness-board") if a[:2] == ["comment", str(TICKET)]]
        self.assertEqual(len(comments), 2, comments)
        resolution = comments[0][comments[0].index("--body") + 1].splitlines()
        self.assertEqual(resolution[0], "**Resolution**")
        at = resolution.index("Verify: OK")
        self.assertEqual(resolution[at + 1], f"Landed by: autoland (gate passed at {self.head[:7]})")
        self.assertEqual(resolution[at + 2], f"PR: https://github.com/o/r/pull/{PR}")
        self.assertEqual(resolution[at + 3], f"Squash commit: {MERGE_SHAS[PR]}")
        self.assertEqual(comments[1][comments[1].index("--body") + 1], f"Landed in {MERGE_SHAS[PR]}")

    def test_notifies_on_land(self):
        self.autoland_scenario([self.pr_answers(PR), self.pr_answers(SECOND)])
        self.autoland()
        calls = self.stubs("terminal-notifier")
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][calls[0].index("-message") + 1], f"proj PR #{PR} auto-landed: {TITLES[PR]}")
        with self.subTest("nothing landed, no notification"):
            prs = [self.pr_answers(PR), self.pr_answers(SECOND)]
            for a in prs:
                a["pr"]["isDraft"] = True
            self.autoland_scenario(prs)
            self.autoland()
            self.assertEqual(self.stubs("terminal-notifier"), [])

    def test_open_incident_lands_nothing(self):
        prs = [self.pr_answers(PR), self.pr_answers(SECOND)]
        for a in prs:
            a["incidents"] = [{"number": 40, "title": "Smoke check failed"}]
        self.autoland_scenario(prs)
        p = self.autoland()
        self.assertEqual(p.stdout.splitlines(), [f"#{PR} wait: incident #40 open", f"#{SECOND} wait: incident #40 open"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.stubs("terminal-notifier"), [])
        state = self.state()
        self.assertEqual([state["prs"][str(n)]["code"] for n in (PR, SECOND)], ["incident", "incident"])
        self.assertEqual(state.get("landed") or [], [])

    def test_cleanup_pending_is_finished_before_any_new_land(self):
        wt, dirty = self.dirty_worktree()
        self.autoland_scenario([self.pr_answers(PR), self.pr_answers(SECOND)])
        p = self.autoland(code=1)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[:3], [f"#{PR} {self.landed_line(self.head)}",
                                     f"#{SECOND} {self.landed_line(self.second)}", f"#{PR} landed"])
        self.assertEqual(len(lines), 4, p.stdout)
        self.assertTrue(lines[3].startswith(f"#{PR} cleanup pending: "), lines[3])
        self.assertEqual(self.state()["cleanup_pending"], PR)
        self.assertEqual([r["pr"] for r in self.state()["landed"]], [PR])

        with self.subTest("still dirty: reported, nothing new gated or landed"):
            merged = self.pr_answers(PR, state="MERGED")
            self.autoland_scenario([merged, self.pr_answers(SECOND)],
                                   listed=[{"number": SECOND, "headRefName": SECOND_BRANCH, "isDraft": False,
                                            "title": TITLES[SECOND]}])
            p = self.autoland(code=1)
            lines = p.stdout.splitlines()
            self.assertEqual(len(lines), 1, p.stdout)
            self.assertTrue(lines[0].startswith(f"#{PR} cleanup pending: "), lines[0])
            self.assertEqual(self.merges(), [])
            self.assertEqual([a for a in self.gh_argv() if a[:2] == ["pr", "list"]], [])
            self.assertEqual(self.state()["cleanup_pending"], PR)
            self.assertTrue(os.path.exists(dirty))

        os.unlink(dirty)
        merged = self.pr_answers(PR, state="MERGED")
        self.autoland_scenario([merged, self.pr_answers(SECOND)],
                               listed=[{"number": SECOND, "headRefName": SECOND_BRANCH, "isDraft": False,
                                        "title": TITLES[SECOND]}])
        p = self.autoland()
        self.assertEqual(p.stdout.splitlines(), [f"#{PR} cleanup finished",
                                                 f"#{SECOND} {self.landed_line(self.second)}", f"#{SECOND} landed"])
        self.assertEqual(self.merges(), [["pr", "merge", str(SECOND), "-R", SLUG, "--squash", "--delete-branch"]])
        self.assertFalse(os.path.exists(wt))
        state = self.state()
        self.assertIsNone(state["cleanup_pending"])
        self.assertEqual([r["pr"] for r in state["landed"]], [PR, SECOND])
        comments = [a for a in self.stubs("harness-board") if a[:2] == ["comment", str(TICKET)]]
        self.assertIn(f"Landed by: autoland (gate passed at {self.head[:7]})",
                      comments[0][comments[0].index("--body") + 1].splitlines())

    def test_dirty_worktree_after_merge_is_never_discarded(self):
        wt, dirty = self.dirty_worktree()
        self.autoland_scenario([self.pr_answers(PR)])
        p = self.autoland(code=1)
        self.assertIn(f"#{PR} landed", p.stdout.splitlines())
        self.assertEqual(len(self.merges()), 1)
        self.assertTrue(os.path.exists(dirty))
        self.assertIn(f"worktree {wt}", self.git(self.main, "worktree", "list", "--porcelain"))
        self.assertEqual(self.git(self.main, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"),
                         self.head)
        self.assertEqual(self.state()["cleanup_pending"], PR)
        self.assertEqual(self.stubs("harness-board"), [])        # the ticket is left until the cleanup finishes

    def test_never_accepts_findings(self):
        """The gate reads the summary's counts, land's review gate the findings listed: a review listing a high
        finding under a summary that counts none passes the gate, and land still refuses it (no --accept-findings)."""
        a = self.pr_answers(PR)
        a["reviews"] = [dict(harness_review(self.head, ONE_LOW),
                             body=f"{ONE_LOW}\n\nNot on a changed line:\n- `src/widget.py` [high] Frobs once.\n\n"
                                  f"{REVIEW_MARKER}")]
        self.autoland_scenario([a])
        p = self.autoland(code=1)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], f"#{PR} {self.landed_line(self.head)}")
        self.assertTrue(lines[1].startswith(f"#{PR} land failed: "), p.stdout)
        self.assertIn("high or medium finding", lines[1])
        self.assertEqual(self.merges(), [])
        self.assertIsNone(self.state()["cleanup_pending"])


if __name__ == "__main__":
    unittest.main()
