"""Tests for the deploy_guard.py template (`revert --sha SHA [--smoke-log F]`), driven by the fake gh.

    python3 -m unittest discover -s plugins/harness/tests -v

The template runs straight from plugins/harness/templates with the checkout as its working directory: a temporary
bare `origin.git`, seeded through a `seed` clone, and a fresh clone of it (on main, full history) where the guard
runs. The seeded history is: Initial (app.txt, banner.txt, scripts/deploy.sh, scripts/smoke.sh), "Add the banner
(#30)", its revert (so origin/main already holds a revert of *another* commit), then the merge under test, "Add the
widget (#34)" style, which changes app.txt from version 1 to version 2. Some tests add a later commit.

scripts/deploy.sh and scripts/smoke.sh are fakes committed in the project: each appends `deploy <HEAD> <app.txt
first line>` or `smoke <HEAD>` to $GUARD_RUN_LOG and exits with $FAKE_DEPLOY_EXIT or $FAKE_SMOKE_EXIT (default 0);
smoke.sh execs `sleep $FAKE_SMOKE_SLEEP` when that is set.

gh is fakegh.py behind a wrapper named `gh` (on PATH, and HARNESS_GH unless a test drops it). The contract the
scenario encodes, beyond the ticket's outline:
- the PR of SHA comes from `gh api repos/O/R/commits/SHA/pulls`, answered with GitHub's raw JSON (a list of pull
  request objects); the fake ignores --jq, so the guard parses the full response;
- open incidents come from `gh issue list` (JSON with number, title, body, labels, state);
- the ticket's labels and comments come from `gh issue view N --json ...` (the REST comments endpoint answers too);
- the incident is created with `gh issue create` (--title, --label/-l, --assignee/-a, --body or --body-file -), its
  number taken from the issue URL gh prints; the ticket comment is `gh issue comment N` (--body or --body-file -);
  status labels change through `gh issue edit N --add-label/--remove-label`.
fakegh's scenarios are static, so idempotency is tested by running twice and swapping in a scenario that shows what
the first run created (the incident, the comment) before the second.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
TEMPLATE = os.path.join(PLUGIN, "templates", "deploy_guard.py")
BOARD = os.path.join(PLUGIN, "bin", "harness-board")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
MARKER = "<!-- harness -->"
NEEDS_INPUT = "**Needs your input**"
BOT = "github-actions[bot]"

APP_V1 = "version: 1\nname: shop\n"
APP_V2 = "version: 2\nname: shop\n"
APP_V3 = "version: 3\nname: shop\n"

DEPLOY_SH = """#!/bin/sh
cd "$(dirname "$0")/.."
printf 'deploy %s %s\\n' "$(git rev-parse HEAD)" "$(head -n 1 app.txt)" >> "$GUARD_RUN_LOG"
echo "deploying"
exit "${FAKE_DEPLOY_EXIT:-0}"
"""

SMOKE_SH = """#!/bin/sh
cd "$(dirname "$0")/.."
printf 'smoke %s\\n' "$(git rev-parse HEAD)" >> "$GUARD_RUN_LOG"
if [ -n "${FAKE_SMOKE_SLEEP:-}" ]; then
  exec sleep "$FAKE_SMOKE_SLEEP"
fi
echo "second smoke: checks ran"
exit "${FAKE_SMOKE_EXIT:-0}"
"""

SMOKE_LOG = "\n".join(f"smoke check {i:03d} FAILED: GET /health -> 500" for i in range(1, 101)) + "\n"


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


def find_python39():
    for cand in (shutil.which("python3.9"), "/usr/bin/python3", "/usr/local/bin/python3.9",
                 "/opt/homebrew/bin/python3.9"):
        if not cand or not os.path.exists(cand):
            continue
        try:
            p = subprocess.run([cand, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.stdout.strip() == "True":
            return cand
    return None


def setUpModule():
    """fakebin/gh: fakegh.py behind a wrapper, written once (macOS checks each new executable on its first run)."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-guard-bin-"))
    fakebin = os.path.join(root, "fakebin")
    executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, gh=os.path.join(fakebin, "gh"), py39=find_python39())


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


# ---------------------------------------------------------------- reading recorded gh calls
def flag_values(argv, *names):
    """Every value given to any of `names` (`--x V`, `-x V` or `--x=V`), commas split, in order."""
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        for name in names:
            if tok == name and i + 1 < len(argv):
                out.append(argv[i + 1])
                i += 1
                break
            if name.startswith("--") and tok.startswith(name + "="):
                out.append(tok[len(name) + 1:])
                break
        i += 1
    return out


def split(values):
    return [v.strip() for value in values for v in value.split(",") if v.strip()]


def targets(argv, number):
    """Does this `gh issue <verb> ...` argv name issue `number` (as a number or an issue URL)?"""
    for tok in argv[2:]:
        if tok == str(number) or tok == f"#{number}" or re.search(rf"/issues/{number}$", tok):
            return True
    return False


def fenced_blocks(text):
    """The contents of each ``` fenced block, as lists of lines."""
    blocks, current, fence = [], None, None
    for line in text.splitlines():
        m = re.match(r"^(`{3,})", line.strip())
        if current is None and m:
            current, fence = [], m.group(1)
        elif current is not None and line.strip() == fence:
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return blocks


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ------------------------------------------------------------- the world
    def make_world(self, name="w", *, slug="acme/shop", title="Add the widget", pr=34, ticket=12, incident=77,
                   later=None, deploy=True, reverted=False):
        """A bare origin with the seeded history, the merge under test, and a fresh clone to run the guard in.
        later: None, "conflict" (a later commit edits app.txt's changed line) or "other" (a later commit
        touches only other.txt). reverted: the revert of the merge is already on origin/main, as if a first
        run died right after its push."""
        base = os.path.join(self.tmp, name)
        w = types.SimpleNamespace(base=base, slug=slug, owner=slug.split("/")[0], title=title, pr=pr,
                                  ticket=ticket if pr else None, incident=incident,
                                  pr_url=f"https://github.com/{slug}/pull/{pr}" if pr else None)
        w.origin = os.path.join(base, "origin.git")
        w.seed = os.path.join(base, "seed")
        w.home = os.path.join(base, "home")
        w.run_log = os.path.join(base, "run.log")
        w.scenario_path = os.path.join(base, "scenario.json")
        w.gh_log = os.path.join(base, "gh.log")
        w.smoke_log = write(os.path.join(base, "smoke-first.log"), SMOKE_LOG)
        os.makedirs(w.home)
        self.setup_git(None, "init", "-q", "--bare", "-b", "main", w.origin)
        self.setup_git(None, "clone", "-q", w.origin, w.seed)
        write(os.path.join(w.seed, "README.md"), "# shop\n")
        write(os.path.join(w.seed, "app.txt"), APP_V1)
        write(os.path.join(w.seed, "banner.txt"), "old banner\n")
        if deploy:
            executable(os.path.join(w.seed, "scripts", "deploy.sh"), DEPLOY_SH)
        executable(os.path.join(w.seed, "scripts", "smoke.sh"), SMOKE_SH)
        self.commit(w.seed, "Initial")
        write(os.path.join(w.seed, "banner.txt"), "new banner\n")
        banner = self.commit(w.seed, "Add the banner (#30)")
        self.setup_git(w.seed, "revert", "--no-edit", banner)
        write(os.path.join(w.seed, "app.txt"), APP_V2)
        w.sha = self.commit(w.seed, f"{title} (#{pr})" if pr else title)
        if later == "conflict":
            write(os.path.join(w.seed, "app.txt"), APP_V3)
            self.commit(w.seed, "Tweak the widget (#35)")
        elif later == "other":
            write(os.path.join(w.seed, "other.txt"), "other\n")
            self.commit(w.seed, "Add other (#36)")
        if reverted:
            self.setup_git(w.seed, "revert", "--no-edit", w.sha)
            w.revert_sha = self.setup_git(w.seed, "rev-parse", "HEAD")
        self.setup_git(w.seed, "push", "-q", "origin", "main")
        w.work = self.clone(w, "work")
        self.w = w
        return w

    def clone(self, w, name):
        path = os.path.join(w.base, name)
        self.setup_git(None, "clone", "-q", w.origin, path)
        return path

    def setup_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_", "FAKEGH_", "GITHUB_"))}
        env.update(GIT_AUTHOR_NAME="Ryan", GIT_AUTHOR_EMAIL="ryan@localhost", GIT_COMMITTER_NAME="Ryan",
                   GIT_COMMITTER_EMAIL="ryan@localhost", GIT_CONFIG_NOSYSTEM="1", HOME=self.tmp,
                   GIT_CEILING_DIRECTORIES=self.tmp)
        return env

    def setup_git(self, repo, *args):
        cmd = [GIT] + (["-C", repo] if repo else []) + ["-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]
        p = subprocess.run(cmd + list(args), env=self.setup_env(), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def commit(self, repo, message):
        self.setup_git(repo, "add", "-A")
        self.setup_git(repo, "commit", "-q", "-m", message)
        return self.setup_git(repo, "rev-parse", "HEAD")

    def origin_git(self, *args, w=None):
        return self.setup_git(None, "--git-dir", (w or self.w).origin, *args)

    def origin_tip(self, w=None):
        return self.origin_git("rev-parse", "main", w=w)

    def origin_count(self, w=None):
        return int(self.origin_git("rev-list", "--count", "main", w=w))

    # ------------------------------------------------------------- the fake gh
    def pr_api(self, w):
        return {"number": w.pr, "title": w.title, "html_url": w.pr_url, "state": "closed",
                "url": f"https://api.github.com/repos/{w.slug}/pulls/{w.pr}", "merged_at": "2026-10-22T09:00:00Z",
                "merge_commit_sha": w.sha, "base": {"ref": "main"}, "user": {"login": w.owner}}

    def scenario(self, w=None, *, incidents=(), ticket_labels=("task", "status:in-review", "status:blocked",
                                                                "size:M", "risk:low"),
                 ticket_comments=(), ticket_state="CLOSED"):
        w = w or self.w
        owner, repo_name = w.slug.split("/")
        pulls = [self.pr_api(w)] if w.pr else []
        calls = [{"contains": [f"repos/{w.slug}/commits/{w.sha}/pulls"], "stdout_json": pulls},
                 {"contains": [f"/repos/{w.slug}/commits/{w.sha}/pulls"], "stdout_json": pulls}]
        if w.pr:
            closing = [{"number": w.ticket, "url": f"https://github.com/{w.slug}/issues/{w.ticket}",
                        "repository": {"name": repo_name, "owner": {"login": owner}}}] if w.ticket else []
            calls += [{"contains": ["pr", "view", str(w.pr)], "stdout_json": {
                "number": w.pr, "title": w.title, "url": w.pr_url, "state": "MERGED",
                "closingIssuesReferences": closing}}]
        if w.ticket:
            rest = [{"user": {"login": c["author"]["login"]}, "body": c["body"], "created_at": c["createdAt"]}
                    for c in ticket_comments]
            calls += [{"contains": [f"repos/{w.slug}/issues/{w.ticket}/comments"], "stdout_json": rest},
                      {"contains": [f"/repos/{w.slug}/issues/{w.ticket}/comments"], "stdout_json": rest},
                      {"contains": ["issue", "view", str(w.ticket)], "stdout_json": {
                          "number": w.ticket, "title": "The ticket", "state": ticket_state,
                          "url": f"https://github.com/{w.slug}/issues/{w.ticket}",
                          "labels": [{"name": n} for n in ticket_labels], "comments": list(ticket_comments)}}]
        calls += [{"contains": ["repo", "view"], "stdout_json": {"nameWithOwner": w.slug, "name": repo_name,
                                                                 "owner": {"login": owner}}},
                  {"contains": ["issue", "list"], "stdout_json": list(incidents)},
                  {"contains": ["issue", "create"], "stdout": f"https://github.com/{w.slug}/issues/{w.incident}\n"},
                  {"contains": ["issue", "comment"],
                   "stdout": f"https://github.com/{w.slug}/issues/{w.ticket}#issuecomment-4242\n"}]
        write(w.scenario_path, json.dumps({"calls": calls, "default": {"stdout": "", "exit": 0}}))
        for leftover in (w.scenario_path + ".state.json", w.gh_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def gh_calls(self, w=None):
        w = w or self.w
        if not os.path.exists(w.gh_log):
            return []
        with open(w.gh_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def creates(self, w=None):
        return [c for c in self.gh_calls(w) if c["argv"][:2] == ["issue", "create"]]

    def comments_on(self, number, w=None):
        return [c for c in self.gh_calls(w) if c["argv"][:2] == ["issue", "comment"] and targets(c["argv"], number)]

    def body(self, call):
        argv = call["argv"]
        bodies = flag_values(argv, "--body", "-b")
        if bodies:
            return bodies[-1]
        files = flag_values(argv, "--body-file", "-F")
        self.assertTrue(files, f"no --body or --body-file in {argv}")
        if files[-1] == "-":
            return call["stdin"]
        self.assertTrue(os.path.exists(files[-1]), f"--body-file {files[-1]} is gone; pass the body on stdin")
        with open(files[-1], encoding="utf-8") as fh:
            return fh.read()

    def title(self, call):
        titles = flag_values(call["argv"], "--title", "-t")
        self.assertTrue(titles, f"no --title in {call['argv']}")
        return titles[-1]

    def labels(self, call):
        return set(split(flag_values(call["argv"], "--label", "-l")))

    def assignees(self, call):
        return set(split(flag_values(call["argv"], "--assignee", "-a")))

    def final_labels(self, number, initial, w=None):
        """The ticket's labels after every recorded `gh issue edit N --add-label/--remove-label`."""
        labels = set(initial)
        for c in self.gh_calls(w):
            if c["argv"][:2] == ["issue", "edit"] and targets(c["argv"], number):
                labels -= set(split(flag_values(c["argv"], "--remove-label")))
                labels |= set(split(flag_values(c["argv"], "--add-label")))
        return labels

    def ran(self, w=None):
        w = w or self.w
        if not os.path.exists(w.run_log):
            return []
        with open(w.run_log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    # ------------------------------------------------------------- running the guard
    def guard_env(self, w, drop=(), **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "HARNESS_", "FAKEGH_", "GITHUB_", "GH_", "FAKE_", "DEPLOY_GUARD_",
                                    "XDG_"))}
        path = [BINS["fakebin"], os.path.dirname(GIT), "/usr/bin", "/bin"]
        env.update(HOME=w.home, PATH=os.pathsep.join(path), GIT_CONFIG_NOSYSTEM="1",
                   GIT_CEILING_DIRECTORIES=self.tmp, HARNESS_GH=BINS["gh"], GITHUB_REPOSITORY=w.slug,
                   GITHUB_SHA=w.sha, GH_TOKEN="fake-token", FAKEGH_SCENARIO=w.scenario_path, FAKEGH_LOG=w.gh_log,
                   GUARD_RUN_LOG=w.run_log)
        for name in drop:
            env.pop(name, None)
        env.update(extra)
        return env

    def guard(self, w=None, *, cwd=None, python=None, smoke_log=True, drop=(), timeout=120, **env):
        w = w or self.w
        argv = [python or sys.executable, TEMPLATE, "revert", "--sha", w.sha]
        if smoke_log:
            argv += ["--smoke-log", w.smoke_log]
        return subprocess.run(argv, cwd=cwd or w.work, env=self.guard_env(w, drop, **env), capture_output=True,
                              text=True, timeout=timeout)

    def assertExit(self, p, code):
        self.assertTrue(os.path.isfile(TEMPLATE), f"{TEMPLATE} does not exist")
        self.assertEqual(p.returncode, code, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        self.assertNotIn("Traceback", p.stderr)

    # ------------------------------------------------------------- shared checks
    def assertOneRevertPushed(self, w, before_tip, before_count):
        """origin/main gained exactly one commit: a revert of w.sha by github-actions[bot] on the old tip."""
        self.assertEqual(self.origin_count(w), before_count + 1, "origin/main gains exactly one commit")
        tip = self.origin_tip(w)
        self.assertEqual(self.origin_git("rev-parse", "main^", w=w), before_tip, "the revert sits on the old tip")
        message = self.origin_git("log", "-1", "--format=%B", "main", w=w)
        self.assertIn(f"This reverts commit {w.sha}", message)
        self.assertEqual(self.origin_git("log", "-1", "--format=%an", "main", w=w), BOT)
        self.assertEqual(self.origin_git("show", "main:app.txt", w=w) + "\n", APP_V1, "the merge's change is undone")
        return tip

    def body_lines(self, body):
        return [line.rstrip() for line in body.splitlines()]

    def assertIncidentBody(self, w, body, revert_sha=None):
        lines = self.body_lines(body)
        self.assertIn(f"Merge commit: {w.sha}", lines, body)
        if w.pr:
            self.assertIn(f"PR: #{w.pr} {w.pr_url}", lines, body)
            self.assertIn(f"Ticket: #{w.ticket}", lines, body)
        if revert_sha:
            self.assertIn(f"Revert commit: {revert_sha}", lines, body)

    def smoke_tail(self, body):
        """The one fenced block holding lines of the first smoke log."""
        blocks = [b for b in fenced_blocks(body) if any("smoke check" in line for line in b)]
        self.assertEqual(len(blocks), 1, body)
        return [line.rstrip() for line in blocks[0]]

    def expected_comment(self, w, revert_sha):
        return (f"{NEEDS_INPUT}\n\nReverted in {revert_sha[:7]} after the live smoke check failed: incident "
                f"#{w.incident}.\n\nReply in a comment; /harness:work {w.ticket} picks it up.\n\n{MARKER}")

    def assertFrozenIncident(self, w, revert_sha=None):
        creates = self.creates(w)
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls(w)])
        call = creates[0]
        self.assertEqual(self.title(call), f"Incident: {w.title}: revert failed, deploys frozen")
        self.assertTrue({"incident", "deploy-frozen"} <= self.labels(call), self.labels(call))
        self.assertEqual(self.assignees(call), {w.owner})
        self.assertIncidentBody(w, self.body(call), revert_sha)
        return call


class Revert(GuardTest):
    # ============================================================= broad behavior
    def test_healthy_revert_pushes_redeploys_opens_incident_reopens_ticket(self):
        w = self.make_world()
        other_sha = "f" * 40
        unrelated = {"number": 5, "title": "Incident: Something else reverted", "state": "OPEN",
                     "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/5",
                     "body": f"Merge commit: {other_sha}\nPR: #29 https://github.com/{w.slug}/pull/29\n"}
        old_question = {"author": {"login": "github-actions"}, "createdAt": "2026-10-01T09:00:00Z",
                        "body": f"{NEEDS_INPUT}\n\nReverted in 1234567 after the live smoke check failed: incident "
                                f"#5.\n\nReply in a comment; /harness:work {w.ticket} picks it up.\n\n{MARKER}"}
        initial = ("task", "status:in-progress", "status:done", "customer:acme", "size:M", "risk:low")
        self.scenario(incidents=[unrelated], ticket_labels=initial, ticket_comments=[old_question])
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 0)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        # deploy.sh then smoke.sh ran once each, on the pushed revert
        self.assertEqual(self.ran(), [f"deploy {revert_sha} version: 1", f"smoke {revert_sha}"])
        # the PR was looked up for this SHA in this repository
        self.assertTrue(any(f"repos/{w.slug}/commits/{w.sha}/pulls" in tok for c in self.gh_calls()
                            for tok in c["argv"]), [c["argv"] for c in self.gh_calls()])
        # one incident, despite an open incident for another merge
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        incident = creates[0]
        self.assertEqual(self.title(incident), f"Incident: {w.title} reverted")
        self.assertIn("incident", self.labels(incident))
        self.assertNotIn("deploy-frozen", self.labels(incident))
        self.assertEqual(self.assignees(incident), {w.owner})
        body = self.body(incident)
        self.assertIncidentBody(w, body, revert_sha)
        # the smoke tail, fenced: exactly the last 60 lines of the 100-line log, in order
        tail = self.smoke_tail(body)
        self.assertEqual(tail[0], "smoke check 041 FAILED: GET /health -> 500", body)
        self.assertEqual(tail, SMOKE_LOG.splitlines()[40:])
        # the ticket: reopened, needs-input instead of every other status, the question comment
        reopens = [c for c in self.gh_calls() if c["argv"][:2] == ["issue", "reopen"]]
        self.assertTrue(any(targets(c["argv"], w.ticket) for c in reopens), [c["argv"] for c in self.gh_calls()])
        labels = self.final_labels(w.ticket, initial)
        self.assertEqual({n for n in labels if n.startswith("status:")}, {"status:needs-input"}, labels)
        self.assertEqual(labels - {"status:needs-input"}, {"task", "customer:acme", "size:M", "risk:low"}, labels)
        comments = self.comments_on(w.ticket)
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        self.assertEqual(self.body(comments[0]).rstrip("\n"), self.expected_comment(w, revert_sha))

    def test_second_run_is_idempotent(self):
        w = self.make_world(slug="acme/shop", incident=81)
        self.scenario()
        p = self.guard()
        self.assertExit(p, 0)
        tip_after_first = self.origin_tip()
        count_after_first = self.origin_count()
        creates, comments = self.creates(), self.comments_on(w.ticket)
        self.assertEqual((len(creates), len(comments)), (1, 1), [c["argv"] for c in self.gh_calls()])
        incident_body, comment_body = self.body(creates[0]), self.body(comments[0])

        # GitHub now shows what the first run made; the second run starts from a fresh checkout and home
        existing = {"number": w.incident, "title": self.title(creates[0]), "state": "OPEN", "body": incident_body,
                    "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/{w.incident}"}
        posted = {"author": {"login": "github-actions"}, "createdAt": "2026-10-22T10:00:00Z", "body": comment_body}
        self.scenario(incidents=[existing], ticket_state="OPEN",
                      ticket_labels=("task", "status:needs-input", "size:M", "risk:low"), ticket_comments=[posted])
        w.work = self.clone(w, "work-again")
        w.home = os.path.join(w.base, "home-again")
        os.makedirs(w.home)

        p = self.guard()

        self.assertExit(p, 0)
        self.assertEqual(self.origin_tip(), tip_after_first, "no second revert")
        self.assertEqual(self.origin_count(), count_after_first)
        self.assertEqual(self.creates(), [], "no second incident")
        self.assertEqual(self.comments_on(w.ticket), [], "no second comment")

    def test_conflicting_revert_freezes_and_leaves_main(self):
        w = self.make_world(slug="globex/store", title="Rework the cart", pr=88, ticket=41, incident=19,
                            later="conflict")
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 1)
        self.assertEqual(self.origin_tip(), before_tip, "origin/main is unchanged")
        self.assertEqual(self.origin_count(), before_count)
        # the revert was aborted: no revert in progress, a clean tree, HEAD where it was
        self.assertFalse(os.path.exists(os.path.join(w.work, ".git", "REVERT_HEAD")), "git revert --abort")
        self.assertEqual(self.setup_git(w.work, "status", "--porcelain"), "")
        self.assertEqual(self.setup_git(w.work, "rev-parse", "HEAD"), before_tip)
        self.assertFrozenIncident(w)

    def test_failed_redeploy_or_second_smoke_freezes(self):
        cases = [("deploy fails", {"FAKE_DEPLOY_EXIT": "1"}, "initech/portal", "Swap the pricing", 7, 3, 64),
                 ("smoke fails", {"FAKE_SMOKE_EXIT": "1"}, "umbrella/api", "Cache the lookups", 150, 140, 200)]
        for name, env, slug, title, pr, ticket, incident in cases:
            with self.subTest(name):
                w = self.make_world(name.replace(" ", "-"), slug=slug, title=title, pr=pr, ticket=ticket,
                                    incident=incident)
                self.scenario(w)
                before_tip, before_count = self.origin_tip(w), self.origin_count(w)

                p = self.guard(w, **env)

                self.assertExit(p, 1)
                revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)   # the revert stays pushed
                self.assertTrue(any(line.startswith("deploy ") for line in self.ran(w)), self.ran(w))
                call = self.assertFrozenIncident(w, revert_sha)
                if name == "smoke fails":
                    self.assertIn(f"smoke {revert_sha}", self.ran(w))
                    body = self.body(call)
                    self.assertIn(f"Revert commit: {revert_sha}", self.body_lines(body))
                    self.assertIn("smoke check 100 FAILED: GET /health -> 500", self.smoke_tail(body))

    def test_sha_without_a_pr_names_no_ticket(self):
        # no GITHUB_REPOSITORY (gh repo view names it) and no HARNESS_GH (gh from PATH); the SHA is not the tip
        w = self.make_world(slug="zed-labs/api", title="Hotfix the importer", pr=None, incident=9, later="other")
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard(drop=("GITHUB_REPOSITORY", "HARNESS_GH"))

        self.assertExit(p, 0)
        self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(self.origin_git("show", "main:other.txt"), "other", "the later commit stays")
        self.assertTrue(any(f"repos/{w.slug}/commits/{w.sha}/pulls" in tok for c in self.gh_calls()
                            for tok in c["argv"]), [c["argv"] for c in self.gh_calls()])
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        # no PR title: the reverted commit's subject stands in
        self.assertEqual(self.title(creates[0]), "Incident: Hotfix the importer reverted")
        self.assertIn("incident", self.labels(creates[0]))
        self.assertEqual(self.assignees(creates[0]), {w.owner})
        body = self.body(creates[0])
        self.assertIn(f"Merge commit: {w.sha}", self.body_lines(body))
        self.assertFalse(any(re.match(r"^Ticket: #\d+", line) for line in self.body_lines(body)), body)
        for verb in ("reopen", "comment", "edit"):
            self.assertEqual([c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["issue", verb]], [],
                             f"no ticket to {verb}")

    def test_ticket_comment_is_a_harness_needs_input_question(self):
        w = self.make_world(slug="rjeskuri/ledger", title="Fix the export", pr=203, ticket=58, incident=311)
        self.scenario()
        p = self.guard()
        self.assertExit(p, 0)
        revert_sha = self.origin_tip()
        comments = self.comments_on(w.ticket)
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        body = self.body(comments[0]).rstrip("\n")
        self.assertEqual(body, self.expected_comment(w, revert_sha))
        self.assertTrue(body.lstrip().startswith(NEEDS_INPUT))
        self.assertEqual(body.splitlines()[-1], MARKER)

        # harness-board answers anchors on it: only the owner's reply after it counts, not the one after an older
        # question, and the guard's comment is not itself an answer
        board_repo = os.path.join(w.base, "board-repo")
        self.setup_git(None, "init", "-q", board_repo)
        self.setup_git(board_repo, "remote", "add", "origin", f"https://github.com/{w.slug}.git")
        thread = [
            {"author": {"login": "rjeskuri"}, "createdAt": "2026-10-20T09:00:00Z",
             "body": f"{NEEDS_INPUT}\n\nWhich format?\n\nReply in a comment; /harness:work 58 picks it up.\n\n{MARKER}"},
            {"author": {"login": "rjeskuri"}, "createdAt": "2026-10-20T10:00:00Z", "body": "CSV, please."},
            {"author": {"login": "rjeskuri"}, "createdAt": "2026-10-22T09:30:00Z", "body": body},
            {"author": {"login": "rjeskuri"}, "createdAt": "2026-10-22T11:00:00Z", "body": "Retry with a smaller batch."},
        ]
        write(w.scenario_path, json.dumps({"calls": [
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["issue", "view", "58", "--json", "comments"], "stdout_json": {"comments": thread}}]}))
        env = self.guard_env(w)
        q = subprocess.run([sys.executable, BOARD, "answers", "58"], cwd=board_repo, env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(q.returncode, 0, q.stdout + q.stderr)
        self.assertEqual([c["body"] for c in json.loads(q.stdout)], ["Retry with a smaller batch."])

    # ============================================================= specific cases
    def test_revert_already_pushed_still_opens_the_incident_and_asks(self):
        # a first run died after its push: the revert is on origin/main, but no incident and no comment exist
        w = self.make_world(slug="stark/forge", title="Retune the furnace", pr=71, ticket=26, incident=133,
                            reverted=True)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()
        self.assertEqual(before_tip, w.revert_sha)

        p = self.guard()

        self.assertExit(p, 0)
        self.assertEqual((self.origin_tip(), self.origin_count()), (before_tip, before_count), "no second revert")
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        self.assertEqual(self.title(creates[0]), f"Incident: {w.title} reverted")
        self.assertIncidentBody(w, self.body(creates[0]), w.revert_sha)
        comments = self.comments_on(w.ticket)
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        self.assertEqual(self.body(comments[0]).rstrip("\n"), self.expected_comment(w, w.revert_sha))

    def test_an_open_incident_for_the_sha_is_not_opened_again(self):
        # the revert is not on main yet, but an open incident already names this merge commit
        w = self.make_world(slug="wayne/cave", title="Rewire the lights", pr=96, ticket=47, incident=999)
        existing = {"number": 412, "title": f"Incident: {w.title} reverted", "state": "OPEN",
                    "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/412",
                    "body": f"Merge commit: {w.sha}\nPR: #{w.pr} {w.pr_url}\nTicket: #{w.ticket}\n"}
        self.scenario(incidents=[existing])
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 0)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(self.creates(), [], "no second incident")
        comments = self.comments_on(w.ticket)
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        w.incident = 412                                                  # the comment names the open incident
        self.assertEqual(self.body(comments[0]).rstrip("\n"), self.expected_comment(w, revert_sha))

    def test_smoke_past_a_short_timeout_freezes(self):
        # with the 2 s/60 s and 10 s/6 s cases, no constant timeout passes all three: it must be read
        w = self.make_world(slug="acme/shop", title="Queue the invoices", pr=64, ticket=33, incident=55)
        self.scenario()
        started = time.monotonic()

        p = self.guard(DEPLOY_GUARD_SMOKE_TIMEOUT="3", FAKE_SMOKE_SLEEP="5")

        self.assertLess(time.monotonic() - started, 30)
        self.assertExit(p, 1)
        self.assertFrozenIncident(w, self.origin_tip())

    def test_an_open_incident_is_matched_by_its_merge_commit_line_not_its_title(self):
        cases = [  # (name, existing incident title, the SHA its body names, creates expected)
            ("same sha, other title", "Deploy broke after the widget", "this", 0),
            ("same title, other sha", "Incident: Index the orders reverted", "other", 1)]
        for name, title, which, creates in cases:
            with self.subTest(name):
                w = self.make_world(name.replace(" ", "-").replace(",", ""), slug="soylent/green",
                                    title="Index the orders", pr=118, ticket=73, incident=640)
                named = w.sha if which == "this" else "e" * 40
                existing = {"number": 501, "title": title, "state": "OPEN", "labels": [{"name": "incident"}],
                            "url": f"https://github.com/{w.slug}/issues/501",
                            "body": f"Merge commit: {named}\nPR: #{w.pr} {w.pr_url}\n"}
                self.scenario(w, incidents=[existing])

                p = self.guard(w)

                self.assertExit(p, 0)
                self.assertEqual(len(self.creates(w)), creates, [c["argv"] for c in self.gh_calls(w)])
                comments = self.comments_on(w.ticket, w)
                self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls(w)])
                w.incident = 501 if which == "this" else 640
                self.assertEqual(self.body(comments[0]).rstrip("\n"), self.expected_comment(w, self.origin_tip(w)))

    def test_an_open_ticket_without_the_comment_is_still_asked(self):
        w = self.make_world(slug="tyrell/replicant", title="Extend the lifespan", pr=19, ticket=8, incident=27)
        initial = ("task", "status:in-progress", "size:S")
        other = {"author": {"login": "tyrell"}, "createdAt": "2026-10-21T09:00:00Z", "body": "Looks good to me."}
        self.scenario(ticket_state="OPEN", ticket_labels=initial, ticket_comments=[other])

        p = self.guard()

        self.assertExit(p, 0)
        comments = self.comments_on(w.ticket)
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        self.assertEqual(self.body(comments[0]).rstrip("\n"), self.expected_comment(w, self.origin_tip()))
        self.assertEqual(self.final_labels(w.ticket, initial), {"task", "status:needs-input", "size:S"})

    def test_a_closed_ticket_with_the_identical_comment_is_not_asked_again(self):
        # the revert and the incident exist and the comment was posted; the ticket was closed again since
        w = self.make_world(slug="cyberdyne/skynet", title="Teach the network", pr=29, ticket=17, incident=88,
                            reverted=True)
        existing = {"number": w.incident, "title": f"Incident: {w.title} reverted", "state": "OPEN",
                    "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/{w.incident}",
                    "body": f"Merge commit: {w.sha}\nPR: #{w.pr} {w.pr_url}\nTicket: #{w.ticket}\n"}
        posted = {"author": {"login": "github-actions"}, "createdAt": "2026-10-22T10:00:00Z",
                  "body": self.expected_comment(w, w.revert_sha)}
        self.scenario(incidents=[existing], ticket_state="CLOSED", ticket_comments=[posted],
                      ticket_labels=("task", "status:needs-input", "size:M"))
        before = (self.origin_tip(), self.origin_count())

        p = self.guard()

        self.assertExit(p, 0)
        self.assertEqual((self.origin_tip(), self.origin_count()), before)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.comments_on(w.ticket), [], "an identical comment is already there")

    def test_a_status_outside_the_harness_set_is_replaced_too(self):
        w = self.make_world(slug="aperture/lab", title="Calibrate the portal", pr=44, ticket=21, incident=36)
        initial = ("bug", "status:on-hold", "risk:high")
        self.scenario(ticket_labels=initial)

        p = self.guard()

        self.assertExit(p, 0)
        self.assertEqual(self.final_labels(w.ticket, initial), {"bug", "status:needs-input", "risk:high"})

    def test_a_pr_that_closes_no_issue_names_no_ticket(self):
        w = self.make_world(slug="oscorp/labs", title="Bump the serum", pr=57, ticket=None, incident=14)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 0)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        creates = self.creates()
        self.assertEqual([self.title(c) for c in creates], [f"Incident: {w.title} reverted"])
        lines = self.body_lines(self.body(creates[0]))
        self.assertIn(f"Merge commit: {w.sha}", lines)
        self.assertIn(f"PR: #{w.pr} {w.pr_url}", lines)
        self.assertIn(f"Revert commit: {revert_sha}", lines)
        self.assertFalse(any(line.startswith("Ticket:") for line in lines), lines)
        for verb in ("reopen", "comment", "edit"):
            self.assertEqual([c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["issue", verb]], [],
                             f"no ticket to {verb}")

    def test_smoke_within_the_timeout_passes(self):
        w = self.make_world(slug="acme/shop", title="Slow the poller", pr=62, ticket=31, incident=53)
        self.scenario()

        p = self.guard(DEPLOY_GUARD_SMOKE_TIMEOUT="10", FAKE_SMOKE_SLEEP="6")

        self.assertExit(p, 0)
        self.assertEqual([self.title(c) for c in self.creates()], [f"Incident: {w.title} reverted"])
        self.assertNotIn("deploy-frozen", self.labels(self.creates()[0]))

    def test_short_smoke_log_is_fenced_whole(self):
        w = self.make_world(slug="acme/shop", title="Shorten the log", pr=63, ticket=32, incident=54)
        short = [f"probe {c}: connection refused" for c in "abcde"]
        write(w.smoke_log, "\n".join(short) + "\n")
        self.scenario()

        p = self.guard()

        self.assertExit(p, 0)
        creates = self.creates()
        self.assertEqual(len(creates), 1)
        blocks = [[line.rstrip() for line in b] for b in fenced_blocks(self.body(creates[0]))
                  if any("probe" in line for line in b)]
        self.assertEqual(blocks, [short], self.body(creates[0]))

    def test_rejected_push_freezes(self):
        w = self.make_world(slug="hooli/web", title="Move the footer", pr=12, ticket=4, incident=23)
        executable(os.path.join(w.origin, "hooks", "pre-receive"), "#!/bin/sh\necho 'protected branch' >&2\nexit 1\n")
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 1)
        self.assertEqual((self.origin_tip(), self.origin_count()), (before_tip, before_count))
        self.assertFrozenIncident(w)

    def test_smoke_timeout_counts_as_a_failing_smoke(self):
        w = self.make_world(slug="acme/shop", title="Batch the emails", pr=61, ticket=30, incident=52)
        self.scenario()
        started = time.monotonic()

        p = self.guard(DEPLOY_GUARD_SMOKE_TIMEOUT="2", FAKE_SMOKE_SLEEP="60")

        self.assertLess(time.monotonic() - started, 40, "DEPLOY_GUARD_SMOKE_TIMEOUT bounds the second smoke")
        self.assertExit(p, 1)
        self.assertFrozenIncident(w, self.origin_tip())

    def test_without_deploy_sh_only_smoke_runs(self):
        w = self.make_world(slug="acme/docs", title="Trim the index", pr=5, ticket=2, incident=6, deploy=False)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 0)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(self.ran(), [f"smoke {revert_sha}"])
        self.assertEqual([self.title(c) for c in self.creates()], [f"Incident: {w.title} reverted"])

    def test_runs_under_python39_with_the_standard_library_only(self):
        self.assertTrue(os.path.isfile(TEMPLATE), f"{TEMPLATE} does not exist")
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("{{", text, "a standalone script, no template placeholders")
        tree = ast.parse(text, feature_version=(3, 9))
        stdlib = set(getattr(sys, "stdlib_module_names", ())) or {
            "argparse", "json", "os", "re", "shutil", "subprocess", "sys", "tempfile", "time", "typing", "pathlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for mod in mods:
                self.assertTrue(mod in stdlib or mod == "__future__", f"not standard library: {mod}")
        if not BINS.get("py39"):
            self.skipTest("no python3.9 interpreter here to run the template with")
        w = self.make_world(slug="acme/shop", title="Add the widget", pr=34, ticket=12, incident=77)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()
        p = self.guard(python=BINS["py39"])
        self.assertExit(p, 0)
        self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(len(self.creates()), 1)


if __name__ == "__main__":
    unittest.main()
