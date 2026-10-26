"""Tests for `harness-pr revert` (ticket #153, epic #133): revert a landed PR on main through the main checkout,
file a risk:high incident bug under the ticket's epic, reopen the ticket as needs-input and notify.

    python3 -m unittest discover -s plugins/harness/tests -p "test_revert.py" -v

Each test has a temporary HOME, a bare origin and a main checkout `proj` pushed to it, whose last commit stands
for PR #34's squash merge (`Add the widget (#34)`, closing ticket #12 under epic #3). `fakebin` holds gh
(fakegh.py behind a wrapper), git (a wrapper that logs and runs the real git; with RACE_ONCE set, the first
`push origin main` first runs that script once, so a test can make origin move under the push) and
terminal-notifier and osascript recorders; `stubbin` holds a harness-board stub that logs its calls, prints
canned output keyed by its first two arguments, else its first, and with `read_stdin` records its stdin.
The fake executables are written once for the module.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-pr")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
PR_TITLE = "Add the widget (#12)"
PR_URL = "https://github.com/o/r/pull/34"
REASON = "the canary failed: harness-pr --help exited 1"
EVIDENCE = "ok compile\nFAIL harness-pr --help: exit 1\nTraceback (most recent call last)\n"
NEEDS_INPUT = "**Needs your input**"

STUB = """import json, os, sys
name, argv = os.path.basename(sys.argv[1]), sys.argv[2:]
with open(os.environ["ORDER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join([name] + argv) + "\\n")
record = {"tool": name, "argv": argv}
if "--evidence-file" in argv:
    with open(argv[argv.index("--evidence-file") + 1], encoding="utf-8") as fh:
        record["evidence"] = fh.read()
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")
outputs = {}
path = os.environ.get("STUB_OUTPUTS")
if path and os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        outputs = json.load(fh).get(name, {})
answer = outputs.get(" ".join(argv[:2]), outputs.get(argv[0] if argv else "", {}))
if answer.get("read_stdin"):
    with open(answer["read_stdin"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.stdin.read()) + "\\n")
sys.stdout.write(answer.get("stdout", ""))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("exit", 0))
"""

RECORDER = """import json, os, sys
with open(os.environ["NOTIFY_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[1])] + sys.argv[2:]) + "\\n")
"""

GIT_WRAPPER = """#!/bin/sh
printf '%s\\n' "git $*" >> "$ORDER_LOG"
if [ -n "$RACE_ONCE" ] && [ ! -e "$RACE_ONCE.done" ]; then
    case " $* " in *" push origin main "*) : > "$RACE_ONCE.done"; sh "$RACE_ONCE" ;; esac
fi
exec "{git}" "$@"
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


BINS = {}


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-revert-bin-"))
    py = sys.executable
    fakebin, stubbin = os.path.join(root, "fakebin"), os.path.join(root, "stubbin")
    stub, recorder = write(os.path.join(root, "stub.py"), STUB), write(os.path.join(root, "recorder.py"), RECORDER)
    executable(os.path.join(fakebin, "gh"),
               f'#!/bin/sh\nprintf \'%s\\n\' "gh $*" >> "$ORDER_LOG"\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "git"), GIT_WRAPPER.replace("{git}", GIT))
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), f'#!/bin/sh\nexec "{py}" "{recorder}" "$0" "$@"\n')
    os.symlink(py, os.path.join(fakebin, "python3"))
    executable(os.path.join(stubbin, "harness-board"), f'#!/bin/sh\nexec "{py}" "{stub}" "$0" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, stubbin=stubbin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


class RevertTest(unittest.TestCase):
    """A bare origin and the main checkout `proj` pushed to it; its last commit is PR #34's squash merge."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-revert-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.order_log = os.path.join(self.tmp, "order.log")
        self.stub_log = os.path.join(self.tmp, "stub.log")
        self.stub_outputs = os.path.join(self.tmp, "stub-outputs.json")
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.bug_stdin = os.path.join(self.tmp, "bug-stdin.jsonl")
        self.evidence = write(os.path.join(self.tmp, "canary.log"), EVIDENCE)
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        write(os.path.join(self.main, "widget.py"), "def frob():\n    return 2\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", PR_TITLE)
        self.git(self.main, "push", "-q", "origin", "main")
        self.sha = self.git(self.main, "rev-parse", "HEAD")
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        self.outputs()
        self.scenario(self.calls())

    # ----------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["stubbin"], BINS["fakebin"], "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test",
                   GIT_AUTHOR_EMAIL="test@localhost", GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
                   HARNESS_GH=os.path.join(BINS["fakebin"], "gh"), FAKEGH_SCENARIO=self.scenario_path,
                   FAKEGH_LOG=self.gh_log, ORDER_LOG=self.order_log, STUB_LOG=self.stub_log,
                   STUB_OUTPUTS=self.stub_outputs, NOTIFY_LOG=self.notify_log)
        env.update(extra)
        return env

    def git(self, repo, *args):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def state_path(self, *parts):
        key = hashlib.sha1(os.path.realpath(self.main).encode()).hexdigest()[:12]
        return os.path.join(self.home, ".claude", "harness", "repos", key, *parts)

    def calls(self, pr_state="MERGED", pulls=None, incidents=None, comments=None, issue_state="CLOSED"):
        """The gh conversation of a revert of PR #34. `incidents` and `comments` are lists of responses, one per
        run (the last repeating): the open incident issues and the ticket's comments."""
        pulls = [{"number": 34, "title": PR_TITLE, "html_url": PR_URL, "merge_commit_sha": self.sha,
                  "merged_at": "2026-10-26T10:00:00Z"}] if pulls is None else pulls
        incidents = [[]] if incidents is None else incidents
        comments = [[]] if comments is None else comments
        return [
            REPO_VIEW,
            {"match": ["pr", "view", "34"],
             "stdout_json": {"number": 34, "state": pr_state, "title": PR_TITLE, "url": PR_URL,
                             "headRefName": "t/12-add-the-widget",
                             "mergeCommit": {"oid": self.sha} if pr_state == "MERGED" else None,
                             "closingIssuesReferences": [{"number": 12}]}},
            {"match": ["api", f"repos/o/r/commits/{self.sha}/pulls"], "stdout_json": pulls},
            {"match": ["issue", "list"], "responses": [{"stdout_json": i} for i in incidents]},
            {"match": ["issue", "edit", "40"], "stdout": ""},
            {"match": ["issue", "view", "12"],
             "responses": [{"stdout_json": {"number": 12, "state": issue_state, "comments": c}} for c in comments]},
            {"match": ["issue", "reopen", "12"], "stdout": ""},
        ]

    def scenario(self, calls):
        write(self.scenario_path, json.dumps({"calls": calls}))
        for leftover in (self.scenario_path + ".state.json", self.gh_log, self.order_log, self.stub_log,
                         self.notify_log, self.bug_stdin):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def outputs(self):
        write(self.stub_outputs, json.dumps({"harness-board": {
            "task create": {"stdout": json.dumps({"number": 40, "url": "https://github.com/o/r/issues/40"}),
                            "read_stdin": self.bug_stdin},
            "needs-input": {"stdout": "https://github.com/o/r/issues/12#issuecomment-9\n"}}}))

    def pr(self, *args, **env):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=self.main, env=self.env(**env),
                              capture_output=True, text=True, timeout=120)

    def ok(self, *args, **env):
        p = self.pr(*args, **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def refused(self, *needles, args=("revert", "34", "--reason", REASON)):
        p = self.pr(*args)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)
        return p

    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def order(self):
        if not os.path.exists(self.order_log):
            return []
        with open(self.order_log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def gh_argv(self):
        return [c["argv"] for c in self.jsonl(self.gh_log)]

    def board_calls(self):
        return [c for c in self.jsonl(self.stub_log) if c["tool"] == "harness-board"]

    def board_argv(self, first):
        return [c["argv"] for c in self.board_calls() if c["argv"][:len(first)] == first]

    def origin_log(self):
        return self.git(self.origin, "log", "--format=%H %s", "main").splitlines()

    def reverts_on_origin(self):
        return [line for line in self.origin_log() if line.split(" ", 1)[1].startswith("Revert ")]

    def assertNothingFiled(self):
        self.assertEqual(self.board_calls(), [])
        self.assertFalse([a for a in self.gh_argv() if a[:2] in (["issue", "edit"], ["issue", "reopen"])])


class Revert(RevertTest):
    def test_reverts_files_bug_reopens_ticket_and_notifies(self):
        p = self.ok("revert", "34", "--reason", REASON, "--evidence-file", self.evidence, "--json")
        # one revert commit on origin/main, on top of the merge, and the main checkout at it
        reverts = self.reverts_on_origin()
        self.assertEqual(len(reverts), 1, self.origin_log())
        revert = reverts[0].split(" ", 1)[0]
        self.assertEqual(self.origin_log()[0].split(" ", 1)[0], revert)
        self.assertIn(f"This reverts commit {self.sha}", self.git(self.origin, "log", "-1", "--format=%B", revert))
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), revert)
        self.assertFalse(os.path.exists(os.path.join(self.main, "widget.py")))
        # one incident bug under the ticket's epic, its body in the task template
        creates = self.board_argv(["task", "create"])
        self.assertEqual(len(creates), 1, self.board_calls())
        create = creates[0]
        for flag, value in (("--type", "bug"), ("--risk", "high"), ("--size", "S"), ("--parent", "3"),
                            ("--title", f"Reverted: {PR_TITLE}")):
            self.assertEqual(create[create.index(flag) + 1], value, create)
        self.assertEqual(create[create.index("--body-file") + 1], "-")
        body = self.jsonl(self.bug_stdin)[0]
        for heading in ("## Request", "## Outline", "## Definition of done", "## Tests that must pass", "## Context"):
            self.assertIn(heading, body)
        self.assertLess(body.index("## Request"), body.index("## Context"))
        self.assertIn(REASON, body)
        self.assertIn(f"Merge commit: {self.sha}", body.splitlines())
        self.assertIn(revert, body)
        self.assertIn("#34", body)
        self.assertIn("FAIL harness-pr --help: exit 1", body)
        self.assertIn(["issue", "edit", "40", "--add-label", "incident"], self.gh_argv())
        # the ticket reopened, then asked
        self.assertIn(["issue", "reopen", "12"], self.gh_argv())
        asks = self.board_argv(["needs-input", "12"])
        self.assertEqual(len(asks), 1, self.board_calls())
        ask = asks[0]
        self.assertEqual(ask[ask.index("--question") + 1],
                         f"Reverted in {revert[:7]}: {REASON}. Bug #40 tracks the fix.")
        self.assertIn("--evidence-file", ask)
        self.assertEqual(next(c for c in self.board_calls() if c["argv"][0] == "needs-input")["evidence"], EVIDENCE)
        order = self.order()
        reopen = next(i for i, line in enumerate(order) if line.startswith("gh issue reopen 12"))
        needs = next(i for i, line in enumerate(order) if line.startswith("harness-board needs-input 12"))
        self.assertLess(reopen, needs)
        # notified, and the JSON summary
        notes = self.jsonl(self.notify_log)
        self.assertTrue(any(f"proj reverted PR #34: {REASON}" in " ".join(n) for n in notes), notes)
        out = json.loads(p.stdout)
        self.assertEqual(out, {"pr": 34, "sha": self.sha, "revert": revert, "ticket": 12, "bug": 40})

    def test_second_run_is_idempotent(self):
        self.ok("revert", "34", "--reason", REASON)
        revert = self.reverts_on_origin()[0].split(" ", 1)[0]
        question = f"Reverted in {revert[:7]}: {REASON}. Bug #40 tracks the fix."
        incident = {"number": 40, "title": f"Reverted: {PR_TITLE}",
                    "body": f"## Context\nMerge commit: {self.sha}\n", "labels": [{"name": "incident"}]}
        asked = {"author": {"login": "rjeskuri"}, "createdAt": "2026-10-26T11:00:00Z",
                 "body": f"{NEEDS_INPUT}\n\n{question}\n\nReply in a comment; /harness:work 12 picks it up."}
        self.scenario(self.calls(incidents=[[incident]], comments=[[asked]], issue_state="OPEN"))
        before = self.origin_log()
        p = self.ok("revert", "34", "--reason", REASON, "--json")
        self.assertEqual(self.origin_log(), before)
        self.assertFalse(any(line.startswith("git") and " revert " in f" {line} "
                             for line in self.order()))
        self.assertNothingFiled()
        self.assertEqual(json.loads(p.stdout), {"pr": 34, "sha": self.sha, "revert": revert, "ticket": 12, "bug": 40})

    def test_refuses_an_unmerged_pr(self):
        for state in ("OPEN", "CLOSED"):
            with self.subTest(state=state):
                self.scenario(self.calls(pr_state=state))
                before = self.origin_log()
                self.refused("PR #34", state.lower())
                self.assertEqual(self.origin_log(), before)
                self.assertNothingFiled()

    def test_refuses_main_checkout_off_main_or_with_tracked_changes(self):
        before = self.origin_log()
        self.git(self.main, "switch", "-q", "-c", "elsewhere")
        self.refused("main checkout", "elsewhere")
        self.git(self.main, "switch", "-q", "main")
        write(os.path.join(self.main, "README.md"), "# proj, edited\n")
        self.refused("main checkout", "README.md")
        self.assertEqual(self.origin_log(), before)
        self.assertNothingFiled()
        self.git(self.main, "checkout", "--", "README.md")
        write(os.path.join(self.main, "scratch.txt"), "untracked does not count\n")
        self.ok("revert", "34", "--reason", REASON)
        self.assertEqual(len(self.reverts_on_origin()), 1)

    def test_conflict_aborts_and_leaves_main(self):
        write(os.path.join(self.main, "widget.py"), "def frob():\n    return 3\n")
        self.git(self.main, "commit", "-q", "-am", "Frob three times")
        self.git(self.main, "push", "-q", "origin", "main")
        head = self.git(self.main, "rev-parse", "HEAD")
        before = self.origin_log()
        self.refused("conflict", "widget.py")
        self.assertEqual(self.origin_log(), before)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.main, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.main, ".git", "REVERT_HEAD")))
        self.assertNothingFiled()

    def test_sha_target_without_a_pr(self):
        self.scenario(self.calls(pulls=[]))
        p = self.ok("revert", self.sha, "--reason", REASON, "--json")
        self.assertEqual(len(self.reverts_on_origin()), 1)
        out = json.loads(p.stdout)
        self.assertEqual((out["pr"], out["ticket"], out["sha"]), (None, None, self.sha))
        self.assertEqual(self.board_argv(["needs-input"]), [])
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["issue", "reopen"]])
        create = self.board_argv(["task", "create"])[0]
        self.assertNotIn("--parent", create)
        self.assertEqual(create[create.index("--title") + 1], f"Reverted: {PR_TITLE}")
        # the sha form also finds the PR and its ticket
        self.git(self.main, "reset", "-q", "--hard", self.sha)
        self.git(self.main, "push", "-q", "--force", "origin", "main")
        self.scenario(self.calls())
        p = self.ok("revert", self.sha[:12], "--reason", REASON, "--json")
        out = json.loads(p.stdout)
        self.assertEqual((out["pr"], out["ticket"], out["sha"]), (34, 12, self.sha))
        self.assertEqual(len(self.board_argv(["needs-input", "12"])), 1)

    def test_rejected_push_rebases_once(self):
        other = os.path.join(self.tmp, "other")
        race = write(os.path.join(self.tmp, "race.sh"), "\n".join([
            f'"{GIT}" clone -q "{self.origin}" "{other}"',
            f'printf "notes\\n" > "{other}/NOTES.md"',
            f'"{GIT}" -C "{other}" add NOTES.md',
            f'"{GIT}" -C "{other}" -c commit.gpgsign=false commit -q -m "Someone else landed"',
            f'"{GIT}" -C "{other}" push -q origin main', ""]))
        self.ok("revert", "34", "--reason", REASON, RACE_ONCE=race)
        subjects = [line.split(" ", 1)[1] for line in self.origin_log()]
        self.assertTrue(subjects[0].startswith("Revert "), subjects)
        self.assertIn("Someone else landed", subjects)
        self.assertEqual(len(self.reverts_on_origin()), 1)
        order = self.order()
        self.assertEqual(sum(1 for line in order if line.startswith("git") and "pull --rebase" in line), 1, order)
        self.assertEqual(sum(1 for line in order if line.startswith("git") and "push origin main" in line), 2, order)

    def test_a_second_rejected_push_says_the_revert_is_local(self):
        hook = write(os.path.join(self.origin, "hooks", "pre-receive"), "#!/bin/sh\necho no >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        self.refused("local", "unpushed")
        self.assertEqual(self.reverts_on_origin(), [])
        order = self.order()
        self.assertEqual(sum(1 for line in order if line.startswith("git") and "pull --rebase" in line), 1, order)
        self.assertNothingFiled()


class LocalCommitsOnMain(RevertTest):
    """Local main ahead of origin/main: a push of main would publish every local-only commit along with the
    revert, so the revert refuses unless the only local commit is this run's own pending revert of the same sha."""

    def commit(self, name, text, message):
        write(os.path.join(self.main, name), text)
        self.git(self.main, "add", name)
        self.git(self.main, "commit", "-q", "-m", message)
        return self.git(self.main, "rev-parse", "HEAD")

    def pending_revert(self, sha):
        """`git revert` of `sha` in the main checkout, left unpushed (an earlier run whose pushes both failed)."""
        self.git(self.main, "revert", "--no-edit", sha)
        head = self.git(self.main, "rev-parse", "HEAD")
        self.assertIn(f"This reverts commit {sha}", self.git(self.main, "log", "-1", "--format=%B", head))
        return head

    def assertRefusedLocal(self, local):
        """Refused in one line naming a local-only commit; origin/main, local main and the board untouched."""
        origin_before, head = self.origin_log(), self.git(self.main, "rev-parse", "HEAD")
        p = self.refused()
        self.assertTrue(any(c[:7] in p.stderr for c in local), (local, p.stderr))
        self.assertEqual(self.origin_log(), origin_before)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.main, "rev-list", "--count", "origin/main..HEAD"), str(len(local)))
        self.assertFalse([line for line in self.order() if line.startswith("git") and " push " in f" {line} "],
                         self.order())
        self.assertFalse([line for line in self.order() if line.startswith("git") and " revert " in f" {line} "],
                         self.order())
        self.assertNothingFiled()
        self.assertEqual(self.jsonl(self.notify_log), [])
        return p

    def test_refuses_when_local_main_has_commits_not_on_origin(self):
        cases = {
            "one unrelated commit": lambda: [self.commit("NOTES.md", "local notes\n", "Local notes")],
            "two unrelated commits": lambda: [self.commit("NOTES.md", "local notes\n", "Local notes"),
                                              self.commit("TODO.md", "later\n", "Local todo")],
            "own pending revert on top of an unrelated commit":
                lambda: [self.commit("NOTES.md", "local notes\n", "Local notes"), self.pending_revert(self.sha)],
            "unrelated commit on top of the own pending revert":
                lambda: [self.pending_revert(self.sha), self.commit("NOTES.md", "local notes\n", "Local notes")],
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                # each case starts with main and origin/main both at the merge, whatever an earlier case pushed
                self.git(self.main, "reset", "-q", "--hard", self.sha)
                self.git(self.main, "push", "-q", "--force", "origin", "main")
                self.scenario(self.calls())
                local = make()
                self.assertRefusedLocal(local)

    def test_refuses_with_a_pending_revert_of_a_different_pr(self):
        # PR A (ticket #13) lands after #34; an earlier revert of A left its commit local when both pushes failed
        other = self.commit("gadget.py", "def gadget():\n    return 1\n", "Add the gadget (#13)")
        self.git(self.main, "push", "-q", "origin", "main")
        pending = self.pending_revert(other)
        self.scenario(self.calls())
        p = self.assertRefusedLocal([pending])
        self.assertIn(pending[:7], p.stderr)
        self.assertEqual(self.reverts_on_origin(), [])
        self.assertTrue(os.path.exists(os.path.join(self.main, "widget.py")))

    def test_only_its_own_pending_revert_is_reused_and_pushed_alone(self):
        # an earlier `revert 34` whose pushes both failed left the revert local; something was committed over it
        own = self.pending_revert(self.sha)
        unrelated = self.commit("NOTES.md", "local notes\n", "Local notes")
        self.assertRefusedLocal([own, unrelated])
        # with only the own pending revert left local, the run reuses it: origin/main gains exactly that commit
        self.git(self.main, "reset", "-q", "--hard", own)
        self.scenario(self.calls())
        before = self.origin_log()
        p = self.ok("revert", "34", "--reason", REASON, "--json")
        after = self.origin_log()
        self.assertEqual(after[1:], before, after)
        self.assertEqual(after[0].split(" ", 1)[0], own)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), own)
        self.assertFalse([line for line in self.order() if line.startswith("git") and " revert " in f" {line} "],
                         self.order())
        self.assertEqual(len(self.board_argv(["task", "create"])), 1, self.board_calls())
        self.assertEqual(json.loads(p.stdout), {"pr": 34, "sha": self.sha, "revert": own, "ticket": 12, "bug": 40})


if __name__ == "__main__":
    unittest.main()
