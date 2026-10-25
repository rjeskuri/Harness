"""Tests for the deploy_guard.py template (`run --sha SHA [--force]`, `degraded --sha SHA --smoke-log F`, `revert --sha
SHA [--smoke-log F]`), driven by the fake gh, and for the deploy.yml v2 template.

    python3 -m unittest discover -s plugins/harness/tests -v

The template runs straight from plugins/harness/templates with the checkout as its working directory: a temporary
bare `origin.git`, seeded through a `seed` clone, and a fresh clone of it (on main, full history) where the guard
runs. The seeded history is: Initial (app.txt, banner.txt, scripts/deploy.sh, scripts/smoke.sh), "Add the banner
(#30)", its revert (so origin/main already holds a revert of *another* commit), then the merge under test, "Add the
widget (#34)" style, which changes app.txt from version 1 to version 2. Some tests add a later commit.

scripts/deploy.sh and scripts/smoke.sh are fakes committed in the project: each appends `deploy <HEAD> <app.txt
first line>` or `smoke <HEAD>` to $GUARD_RUN_LOG and exits with $FAKE_DEPLOY_EXIT or $FAKE_SMOKE_EXIT (default 0);
deploy.sh execs `sleep $FAKE_DEPLOY_SLEEP` after its line when that is set;
smoke.sh execs `sleep $FAKE_SMOKE_SLEEP` when that is set. On the merge under test (app.txt at version 2) smoke.sh is
the first, live check of `run`: it prints the file $FAKE_SMOKE_OUTPUT and the line $FAKE_SMOKE_STDERR (on stderr) when
set, execs `sleep $FAKE_SMOKE_SLEEP_V2` when set, and exits $FAKE_SMOKE_EXIT_V2, else $FAKE_SMOKE_EXIT, else 0; on the
reverted code (version 1) it behaves as before, so a run whose first smoke fails can end in a healthy revert.

gh is fakegh.py behind a wrapper named `gh` (on PATH, and HARNESS_GH unless a test drops it). The contract the
scenario encodes, beyond the ticket's outline:
- the PR of SHA comes from `gh api repos/O/R/commits/SHA/pulls`, answered with GitHub's raw JSON (a list of pull
  request objects); the fake ignores --jq, so the guard parses the full response;
- open incidents come from `gh issue list` (JSON with number, title, body, labels, state);
- the ticket's labels and comments come from `gh issue view N --json ...` (the REST comments endpoint answers too);
- the incident is created with `gh issue create` (--title, --label/-l, --assignee/-a, --body or --body-file -), its
  number taken from the issue URL gh prints; the ticket comment is `gh issue comment N` (--body or --body-file -);
  status labels change through `gh issue edit N --add-label/--remove-label`.
- open issues by label come from `gh issue list` with the label as its own argv token (`--label deploy-frozen`, `-l
  degraded`, as the guard already does for `incident`); the fake answers each label with its own list and a list
  without a known label with every open issue (labels included in the JSON);
- the degraded issue is `gh issue create` too, a comment on it `gh issue comment N`.
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
if [ -n "${FAKE_DEPLOY_SLEEP:-}" ]; then
  exec sleep "$FAKE_DEPLOY_SLEEP"
fi
exit "${FAKE_DEPLOY_EXIT:-0}"
"""

SMOKE_SH = """#!/bin/sh
cd "$(dirname "$0")/.."
printf 'smoke %s\\n' "$(git rev-parse HEAD)" >> "$GUARD_RUN_LOG"
if [ "$(head -n 1 app.txt)" = "version: 2" ]; then
  if [ -n "${FAKE_SMOKE_OUTPUT:-}" ]; then
    cat "$FAKE_SMOKE_OUTPUT"
  fi
  if [ -n "${FAKE_SMOKE_STDERR:-}" ]; then
    echo "$FAKE_SMOKE_STDERR" >&2
  fi
  if [ -n "${FAKE_SMOKE_SLEEP_V2:-}" ]; then
    exec sleep "$FAKE_SMOKE_SLEEP_V2"
  fi
  exit "${FAKE_SMOKE_EXIT_V2:-${FAKE_SMOKE_EXIT:-0}}"
fi
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
    # pybin/python3: records the deploy.yml step's python3 calls (argv, GH_TOKEN) to $FAKE_PY_LOG, exits $FAKE_PY_EXIT
    pybin = os.path.join(root, "pybin")
    recorder = write(os.path.join(root, "pyrecorder.py"), (
        "import json, os, sys\n"
        "with open(os.environ['FAKE_PY_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'gh_token': os.environ.get('GH_TOKEN')}) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_PY_EXIT') or '0'))\n"))
    executable(os.path.join(pybin, "python3"), f'#!/bin/sh\nexec "{sys.executable}" "{recorder}" "$@"\n')
    BINS.update(root=root, fakebin=fakebin, gh=os.path.join(fakebin, "gh"), py39=find_python39(), pybin=pybin)


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
                   later=None, deploy=True, smoke=True, reverted=False):
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
        if smoke:
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
                 ticket_comments=(), ticket_state="CLOSED", label_create_fails=False, frozen=(), degraded=()):
        w = w or self.w
        owner, repo_name = w.slug.split("/")
        pulls = [self.pr_api(w)] if w.pr else []
        calls = [{"contains": ["label", "create"], "stderr": "label with name already exists; use `--force`\n",
                  "exit": 1}] if label_create_fails else []
        calls += [{"contains": [f"repos/{w.slug}/commits/{w.sha}/pulls"], "stdout_json": pulls},
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
                  {"contains": ["issue", "list", "deploy-frozen"], "stdout_json": list(frozen)},
                  {"contains": ["issue", "list", "degraded"], "stdout_json": list(degraded)},
                  {"contains": ["issue", "list", "incident"], "stdout_json": list(incidents)},
                  {"contains": ["issue", "list"], "stdout_json": list(
                      {i["number"]: i for i in (*incidents, *frozen, *degraded)}.values())},
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

    def label_creates(self, w=None):
        """(index in the gh log, label name, argv) of each `gh label create NAME ...`."""
        valued = {"--color", "-c", "--description", "-d", "--repo", "-R"}
        out = []
        for i, call in enumerate(self.gh_calls(w)):
            argv = call["argv"]
            if argv[:2] != ["label", "create"]:
                continue
            j, name = 2, None
            while j < len(argv):
                if argv[j] in valued:
                    j += 2
                    continue
                if not argv[j].startswith("-"):
                    name = argv[j]
                    break
                j += 1
            out.append((i, name, argv))
        return out

    def index_of(self, verb, w=None):
        found = [i for i, c in enumerate(self.gh_calls(w)) if c["argv"][:2] == ["issue", verb]]
        self.assertTrue(found, f"no gh issue {verb} in {[c['argv'] for c in self.gh_calls(w)]}")
        return found[0]


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
        os.unlink(w.run_log)

        p = self.guard()
        self.assertEqual(self.ran(), [f"deploy {tip_after_first} version: 1", f"smoke {tip_after_first}"],
                         "the second run redeploys the reverted code")

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

    def test_redeploy_past_the_deploy_timeout_freezes(self):
        # `revert` runs deploy.sh only once, after the revert, so FAKE_DEPLOY_SLEEP hangs just the redeploy
        w = self.make_world(slug="stark/forge", title="Retune the furnace", pr=73, ticket=36, incident=118)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()
        started = time.monotonic()
        try:
            p = self.guard(timeout=25, DEPLOY_GUARD_DEPLOY_TIMEOUT="2", FAKE_DEPLOY_SLEEP="40")
        except subprocess.TimeoutExpired:
            self.fail("DEPLOY_GUARD_DEPLOY_TIMEOUT=2 did not bound the 40 s redeploy after the revert")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 20, "DEPLOY_GUARD_DEPLOY_TIMEOUT bounds the redeploy")
        self.assertExit(p, 1)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)   # the revert stays pushed
        self.assertEqual(self.ran(), [f"deploy {revert_sha} version: 1"], "no smoke after a timed-out redeploy")
        call = self.assertFrozenIncident(w, revert_sha)
        frozen = [line for line in self.body_lines(self.body(call)) if line.startswith("Deploys are frozen:")]
        self.assertEqual(len(frozen), 1, self.body(call))
        self.assertIn("scripts/deploy.sh", frozen[0])

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
    def test_rerun_from_a_checkout_at_the_bad_merge_deploys_the_revert(self):
        # GitHub's "re-run job": the checkout is GITHUB_SHA (the bad merge, detached) and main already holds the
        # revert; the guard must not redeploy the merge's code
        w = self.make_world(slug="initrode/tps", title="Reformat the reports", pr=77, ticket=39, incident=402,
                            reverted=True)
        self.setup_git(w.work, "checkout", "-q", "--detach", w.sha)
        existing = {"number": w.incident, "title": f"Incident: {w.title} reverted", "state": "OPEN",
                    "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/{w.incident}",
                    "body": f"Merge commit: {w.sha}\nPR: #{w.pr} {w.pr_url}\nTicket: #{w.ticket}\n"}
        self.scenario(incidents=[existing])
        before = (self.origin_tip(), self.origin_count())
        self.assertEqual(before[0], w.revert_sha)

        p = self.guard()

        self.assertExit(p, 0)
        self.assertEqual((self.origin_tip(), self.origin_count()), before, "no new commit on origin/main")
        self.assertEqual(self.ran(), [f"deploy {w.revert_sha} version: 1", f"smoke {w.revert_sha}"])

    def test_labels_are_made_sure_of_before_they_are_used(self):
        cases = [  # (name, world options, guard env, labels needed before the call, the call)
            ("healthy", {}, {}, {"incident"}, "create"),
            ("frozen", {"later": "conflict"}, {}, {"incident", "deploy-frozen"}, "create"),
            ("frozen, incident open", {}, {"FAKE_SMOKE_EXIT": "1"}, {"deploy-frozen"}, "edit")]
        for name, opts, env, needed, verb in cases:
            with self.subTest(name):
                w = self.make_world(re.sub(r"\W+", "-", name), slug="vandelay/imports", title="Import the latex",
                                    pr=91, ticket=45, incident=318, **opts)
                incidents = []
                if verb == "edit":
                    incidents = [{"number": 250, "title": f"Incident: {w.title} reverted", "state": "OPEN",
                                  "labels": [{"name": "incident"}], "url": f"https://github.com/{w.slug}/issues/250",
                                  "body": f"Merge commit: {w.sha}\nPR: #{w.pr} {w.pr_url}\nTicket: #{w.ticket}\n"}]
                self.scenario(w, incidents=incidents)

                p = self.guard(w, **env)

                self.assertExit(p, 0 if name == "healthy" else 1)
                if verb == "edit":
                    edits = [i for i, c in enumerate(self.gh_calls(w)) if c["argv"][:2] == ["issue", "edit"]
                             and targets(c["argv"], 250) and "deploy-frozen" in split(flag_values(c["argv"],
                                                                                                    "--add-label"))]
                    self.assertTrue(edits, [c["argv"] for c in self.gh_calls(w)])
                    at = edits[0]
                else:
                    at = self.index_of("create", w)
                made = {label for i, label, _ in self.label_creates(w) if i < at}
                self.assertTrue(needed <= made, f"labels created before gh issue {verb}: {made}")
                for _, label, argv in self.label_creates(w):
                    self.assertFalse({"--force", "-f"} & set(argv), f"a customised {label} label stays as it is")

    def test_a_failing_label_create_does_not_stop_the_incident(self):
        w = self.make_world(slug="pendant/publishing", title="Print the catalog", pr=26, ticket=11, incident=97)
        self.scenario(label_create_fails=True)
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.guard()

        self.assertExit(p, 0)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertTrue(any(label == "incident" for _, label, _ in self.label_creates()), "the label was tried")
        creates = self.creates()
        self.assertEqual([self.title(c) for c in creates], [f"Incident: {w.title} reverted"])
        self.assertIn("incident", self.labels(creates[0]))
        comments = self.comments_on(w.ticket)
        self.assertEqual([self.body(c).rstrip("\n") for c in comments], [self.expected_comment(w, revert_sha)])

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


class Run(GuardTest):
    """`run --sha SHA [--force]`: frozen, stop; scripts/deploy.sh; scripts/smoke.sh; 0 done, 2 degraded, else revert."""

    def run_guard(self, w=None, *extra, timeout=120, **env):
        w = w or self.w
        argv = [sys.executable, TEMPLATE, "run", "--sha", w.sha, *extra]
        return subprocess.run(argv, cwd=w.work, env=self.guard_env(w, **env), capture_output=True, text=True,
                              timeout=timeout)

    def degraded_cmd(self, w, smoke_log):
        argv = [sys.executable, TEMPLATE, "degraded", "--sha", w.sha, "--smoke-log", smoke_log]
        return subprocess.run(argv, cwd=w.work, env=self.guard_env(w), capture_output=True, text=True, timeout=120)

    def smoke_output(self, w, lines):
        """A file smoke.sh prints on the merge under test (FAKE_SMOKE_OUTPUT)."""
        return write(os.path.join(w.base, "smoke-output.txt"), "\n".join(lines) + "\n")

    def issue(self, w, number, title, *labels, body=""):
        return {"number": number, "title": title, "state": "OPEN", "labels": [{"name": n} for n in labels],
                "url": f"https://github.com/{w.slug}/issues/{number}", "body": body}

    def snapshot(self, w=None):
        w = w or self.w
        return self.origin_tip(w), self.origin_count(w), self.setup_git(w.work, "rev-parse", "HEAD")

    def output(self, p):
        return p.stdout + p.stderr

    def writes(self, w=None):
        """Every recorded gh call that changes GitHub."""
        out = []
        for c in self.gh_calls(w):
            a = c["argv"]
            if a[:1] == ["issue"] and a[1:2] and a[1] in ("create", "comment", "edit", "reopen", "close", "delete"):
                out.append(a)
            elif a[:1] == ["pr"] and a[1:2] and a[1] in ("create", "comment", "edit", "merge", "close"):
                out.append(a)
            elif a[:1] == ["api"]:
                methods = [m.upper() for m in flag_values(a, "-X", "--method")]
                fields = flag_values(a, "-f", "-F", "--field", "--raw-field", "--input")
                if any(m != "GET" for m in methods) or (fields and not methods):
                    out.append(a)
        return out

    def comments(self, w=None):
        return [c for c in self.gh_calls(w) if c["argv"][:2] == ["issue", "comment"]]

    def fenced_with(self, body, needle):
        """The one fenced block holding a line with `needle`, its lines right-stripped."""
        blocks = [[line.rstrip() for line in b] for b in fenced_blocks(body) if any(needle in line for line in b)]
        self.assertEqual(len(blocks), 1, body)
        return blocks[0]

    def assertDegradedIssue(self, w, call):
        # `Degraded: <repo>`: the ticket does not say slug or name, so either is accepted
        self.assertIn(self.title(call), {f"Degraded: {w.slug}", f"Degraded: {w.slug.split('/')[1]}"})
        labels = self.labels(call)
        self.assertIn("degraded", labels)
        self.assertFalse({"incident", "deploy-frozen"} & labels, f"degraded is not an incident: {labels}")

    def deployed_and_smoked(self, w):
        return [f"deploy {w.sha} version: 2", f"smoke {w.sha}"]

    # ============================================================= broad behavior
    def test_healthy_deploy_and_smoke_exit_zero(self):
        cases = [("acme/shop", "Add the widget", 34, 12), ("globex/store", "Rework the cart", 88, 41)]
        for slug, title, pr, ticket in cases:
            with self.subTest(slug):
                w = self.make_world(slug.replace("/", "-"), slug=slug, title=title, pr=pr, ticket=ticket)
                other = self.issue(w, 5, "Incident: Something else reverted", "incident",
                                   body=f"Merge commit: {'f' * 40}\n")
                self.scenario(w, incidents=[other])
                before = self.snapshot(w)

                p = self.run_guard(w)

                self.assertExit(p, 0)
                self.assertEqual(self.ran(w), self.deployed_and_smoked(w), "deploy.sh then smoke.sh, once each")
                self.assertEqual(self.snapshot(w), before, "no git write")
                self.assertEqual(self.writes(w), [], "nothing filed on GitHub")

    def test_smoke_exit_one_reverts(self):
        w = self.make_world(slug="initech/portal", title="Swap the pricing", pr=7, ticket=3, incident=64)
        output = [f"live check {i:03d} FAILED: GET /pricing -> 502" for i in range(1, 76)]
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="1", FAKE_SMOKE_OUTPUT=self.smoke_output(w, output))

        self.assertExit(p, 0)                                   # a healthy revert exits 0
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(self.ran(), self.deployed_and_smoked(w) + [f"deploy {revert_sha} version: 1",
                                                                     f"smoke {revert_sha}"])
        creates = self.creates()
        self.assertEqual([self.title(c) for c in creates], [f"Incident: {w.title} reverted"],
                         [c["argv"] for c in self.gh_calls()])
        body = self.body(creates[0])
        self.assertIncidentBody(w, body, revert_sha)
        # the first smoke's output reached revert as its --smoke-log: the incident fences its last 60 lines
        self.assertEqual(self.fenced_with(body, "live check"), output[-60:])
        self.assertEqual([self.body(c).rstrip("\n") for c in self.comments_on(w.ticket)],
                         [self.expected_comment(w, revert_sha)])

    def test_other_exit_codes_and_timeout_revert(self):
        cases = [  # (name, guard env, slug, title, pr, ticket, incident)
            ("exit 3", {"FAKE_SMOKE_EXIT_V2": "3"}, "hooli/web", "Move the footer", 12, 4, 23),
            ("exit 127", {"FAKE_SMOKE_EXIT_V2": "127"}, "vandelay/imports", "Import the latex", 91, 45, 318),
            ("exit 255", {"FAKE_SMOKE_EXIT_V2": "255"}, "stark/forge", "Retune the furnace", 71, 26, 133),
            ("timeout", {"DEPLOY_GUARD_SMOKE_TIMEOUT": "2", "FAKE_SMOKE_SLEEP_V2": "60"}, "wayne/cave",
             "Rewire the lights", 96, 47, 412)]
        for name, env, slug, title, pr, ticket, incident in cases:
            with self.subTest(name):
                w = self.make_world(name.replace(" ", "-"), slug=slug, title=title, pr=pr, ticket=ticket,
                                    incident=incident)
                output = [f"{name}: probe {c} failed" for c in "xyz"]
                self.scenario(w)
                before_tip, before_count = self.origin_tip(w), self.origin_count(w)
                started = time.monotonic()

                p = self.run_guard(w, FAKE_SMOKE_OUTPUT=self.smoke_output(w, output), **env)

                self.assertLess(time.monotonic() - started, 40, "DEPLOY_GUARD_SMOKE_TIMEOUT bounds the first smoke")
                self.assertExit(p, 0)
                revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
                self.assertEqual(self.ran(w), self.deployed_and_smoked(w) + [f"deploy {revert_sha} version: 1",
                                                                              f"smoke {revert_sha}"])
                creates = self.creates(w)
                self.assertEqual([self.title(c) for c in creates], [f"Incident: {title} reverted"],
                                 [c["argv"] for c in self.gh_calls(w)])
                self.assertNotIn("degraded", self.labels(creates[0]))
                body = self.body(creates[0])
                self.assertIncidentBody(w, body, revert_sha)
                if name != "timeout":
                    self.assertTrue(set(output) <= set(self.fenced_with(body, "probe")), body)

    def test_smoke_exit_two_files_degraded_and_never_reverts(self):
        cases = [("cyberdyne/skynet", "Teach the network", 29, 17), ("tyrell/replicant", "Extend the lifespan", 19, 8)]
        for slug, title, pr, ticket in cases:
            with self.subTest(slug):
                w = self.make_world(slug.replace("/", "-"), slug=slug, title=title, pr=pr, ticket=ticket)
                output = [f"{slug} check {i:03d} DEGRADED: p95 {2000 + i} ms" for i in range(1, 76)]
                other = self.issue(w, 5, "Incident: Something else reverted", "incident",
                                   body=f"Merge commit: {'f' * 40}\n")
                self.scenario(w, incidents=[other])
                before = self.snapshot(w)

                p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2", FAKE_SMOKE_OUTPUT=self.smoke_output(w, output))

                self.assertExit(p, 0)
                self.assertEqual(self.snapshot(w), before, "no revert, no git write")
                self.assertEqual(self.ran(w), self.deployed_and_smoked(w), "no redeploy, no second smoke")
                creates = self.creates(w)
                self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls(w)])
                self.assertDegradedIssue(w, creates[0])
                body = self.body(creates[0])
                self.assertIn(w.sha, body)
                self.assertEqual(self.fenced_with(body, "DEGRADED"), output[-60:])
                for verb in ("comment", "edit", "reopen"):
                    self.assertEqual([c["argv"] for c in self.gh_calls(w) if c["argv"][:2] == ["issue", verb]], [],
                                     f"no gh issue {verb}: no incident, no ticket question")

    def test_degraded_appends_to_the_open_issue(self):
        w = self.make_world(slug="soylent/green", title="Index the orders", pr=118, ticket=73, incident=418)
        output = [f"check {c}: p95 3100 ms over the 2000 ms budget" for c in "abcde"]
        smoke_out = self.smoke_output(w, output)
        unrelated = self.issue(w, 501, "Incident: Something else reverted", "incident",
                               body=f"Merge commit: {'e' * 40}\n")
        self.scenario(incidents=[unrelated])
        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2", FAKE_SMOKE_OUTPUT=smoke_out)
        self.assertExit(p, 0)
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        self.assertDegradedIssue(w, creates[0])
        self.assertEqual(self.comments(), [])

        # GitHub now shows the degraded issue the first run made (#418); the next exit 2 comments on it
        existing = self.issue(w, 418, self.title(creates[0]), "degraded", body=self.body(creates[0]))
        self.scenario(incidents=[unrelated], degraded=[existing])
        os.unlink(w.run_log)
        before = self.snapshot()

        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2", FAKE_SMOKE_OUTPUT=smoke_out)

        self.assertExit(p, 0)
        self.assertEqual(self.snapshot(), before, "no git write")
        self.assertEqual(self.ran(), self.deployed_and_smoked(w))
        self.assertEqual(self.creates(), [], "the degraded issue is not opened twice")
        comments = self.comments()
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        self.assertTrue(targets(comments[0]["argv"], 418), comments[0]["argv"])
        body = self.body(comments[0])
        self.assertIn(w.sha, body)
        self.assertTrue(set(output) <= set(self.fenced_with(body, "p95")), body)

    def test_frozen_stops_unless_forced(self):
        cases = [  # (name, frozen issue number, world options, run arguments, deploys)
            ("frozen", 13, {}, (), False),
            ("frozen without a deploy script", 207, {"deploy": False}, (), False),
            ("frozen and forced", 13, {}, ("--force",), True)]
        for name, number, opts, args, deploys in cases:
            with self.subTest(name):
                w = self.make_world(re.sub(r"\W+", "-", name), slug="initrode/tps", title="Reformat the reports",
                                    pr=77, ticket=39, incident=402, **opts)
                frozen = self.issue(w, number, "Incident: Earlier merge: revert failed, deploys frozen", "incident",
                                    "deploy-frozen", body=f"Merge commit: {'d' * 40}\n")
                self.scenario(w, incidents=[frozen], frozen=[frozen])
                before = self.snapshot(w)

                # a failing smoke, so a guard that ignored the freeze would also revert
                p = self.run_guard(w, *args, **({} if deploys else {"FAKE_SMOKE_EXIT_V2": "1"}))

                self.assertExit(p, 0)
                if deploys:
                    # forced past the freeze, the run is a normal healthy one
                    self.assertEqual(self.ran(w), self.deployed_and_smoked(w))
                    self.assertEqual(self.snapshot(w), before)
                    self.assertEqual(self.writes(w), [])
                    continue

                self.assertIn(f"deploys are frozen by #{number}", self.output(p))
                self.assertNotIn("no deploy script", self.output(p), "the freeze is checked first")
                self.assertEqual(self.ran(w), [], "nothing deployed, nothing checked")
                self.assertEqual(self.snapshot(w), before)
                self.assertEqual(self.writes(w), [])

        # an open incident and an open degraded issue are not a freeze
        w = self.make_world("not-frozen", slug="initrode/tps", title="Reformat the reports", pr=77, ticket=39)
        self.scenario(w, incidents=[self.issue(w, 5, "Incident: Something reverted", "incident",
                                               body=f"Merge commit: {'c' * 40}\n")],
                      degraded=[self.issue(w, 6, "Degraded: initrode/tps", "degraded")])
        p = self.run_guard(w)
        self.assertExit(p, 0)
        self.assertNotIn("deploys are frozen", self.output(p))
        self.assertEqual(self.ran(w), self.deployed_and_smoked(w))

    def test_no_deploy_or_no_smoke_script_stops_cleanly(self):
        w = self.make_world("no-deploy", slug="acme/docs", title="Trim the index", pr=5, ticket=2, deploy=False)
        self.scenario(w)
        before = self.snapshot(w)
        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="1")
        self.assertExit(p, 0)
        self.assertIn("no deploy script", self.output(p))
        self.assertEqual(self.ran(w), [], "no deploy script: the smoke does not run either")
        self.assertEqual(self.snapshot(w), before)
        self.assertEqual(self.writes(w), [])

        w = self.make_world("no-smoke", slug="acme/api", title="Bump the client", pr=6, ticket=3, smoke=False)
        self.scenario(w)
        before = self.snapshot(w)
        p = self.run_guard(w)
        self.assertExit(p, 0)
        self.assertNotIn("no deploy script", self.output(p))
        self.assertEqual(self.ran(w), [f"deploy {w.sha} version: 2"])
        self.assertEqual(self.snapshot(w), before)
        self.assertEqual(self.writes(w), [])

    def test_failed_deploy_exits_one_without_revert(self):
        for code in ("1", "2", "3"):
            with self.subTest(deploy_exit=code):
                w = self.make_world(f"deploy-{code}", slug="oscorp/labs", title="Bump the serum", pr=57, ticket=14)
                self.scenario(w)
                before = self.snapshot(w)

                p = self.run_guard(w, FAKE_DEPLOY_EXIT=code, FAKE_SMOKE_EXIT_V2="1")

                self.assertExit(p, 1)
                self.assertEqual(self.ran(w), [f"deploy {w.sha} version: 2"], "no smoke after a failed deploy")
                self.assertEqual(self.snapshot(w), before, "no revert")
                self.assertEqual(self.writes(w), [], "no incident, no degraded issue")

    # ============================================================= specific cases
    def test_a_failed_revert_passes_its_exit_code_through(self):
        # the smoke fails on the merge and again on the revert: revert freezes deploys and exits 1, so run does
        w = self.make_world(slug="umbrella/api", title="Cache the lookups", pr=150, ticket=140, incident=200)
        self.scenario()
        before_tip, before_count = self.origin_tip(), self.origin_count()

        p = self.run_guard(w, FAKE_SMOKE_EXIT="1")

        self.assertExit(p, 1)
        revert_sha = self.assertOneRevertPushed(w, before_tip, before_count)
        self.assertEqual(self.ran(), self.deployed_and_smoked(w) + [f"deploy {revert_sha} version: 1",
                                                                     f"smoke {revert_sha}"])
        self.assertFrozenIncident(w, revert_sha)

    def test_smoke_timeout_is_read_from_the_environment(self):
        # 3 s against a 5 s smoke reverts, 10 s against a 6 s smoke passes: no constant timeout does both
        cases = [("3", "5", True), ("10", "6", False)]
        for limit, sleep, reverts in cases:
            with self.subTest(timeout=limit, sleep=sleep):
                w = self.make_world(f"t{limit}", slug="acme/shop", title="Queue the invoices", pr=64, ticket=33,
                                    incident=55)
                self.scenario(w)
                before = self.snapshot(w)

                p = self.run_guard(w, DEPLOY_GUARD_SMOKE_TIMEOUT=limit, FAKE_SMOKE_SLEEP_V2=sleep)

                self.assertExit(p, 0)
                if reverts:
                    self.assertOneRevertPushed(w, before[0], before[1])
                    self.assertEqual([self.title(c) for c in self.creates(w)],
                                     ["Incident: Queue the invoices reverted"])
                else:
                    self.assertEqual(self.snapshot(w), before)
                    self.assertEqual(self.ran(w), self.deployed_and_smoked(w))
                    self.assertEqual(self.writes(w), [])

    def test_degraded_command_files_then_appends_the_given_log(self):
        w = self.make_world(slug="pendant/publishing", title="Print the catalog", pr=26, ticket=11, incident=61)
        log = [f"catalog probe {i}: 1 of 3 regions slow" for i in range(1, 9)]
        smoke_log = write(os.path.join(w.base, "degraded.log"), "\n".join(log) + "\n")
        self.scenario()
        before = self.snapshot()

        p = self.degraded_cmd(w, smoke_log)

        self.assertExit(p, 0)
        self.assertEqual(self.ran(), [], "degraded deploys nothing and runs no smoke")
        self.assertEqual(self.snapshot(), before)
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        self.assertDegradedIssue(w, creates[0])
        body = self.body(creates[0])
        self.assertIn(w.sha, body)
        self.assertEqual(self.fenced_with(body, "catalog probe"), log, "a short log is fenced whole")
        self.assertEqual(self.comments(), [])

        self.scenario(degraded=[self.issue(w, 61, f"Degraded: {w.slug}", "degraded", body=body)])
        p = self.degraded_cmd(w, smoke_log)
        self.assertExit(p, 0)
        self.assertEqual(self.creates(), [])
        comments = self.comments()
        self.assertEqual(len(comments), 1, [c["argv"] for c in self.gh_calls()])
        self.assertTrue(targets(comments[0]["argv"], 61), comments[0]["argv"])
        self.assertIn(w.sha, self.body(comments[0]))
        self.assertEqual(self.fenced_with(self.body(comments[0]), "catalog probe"), log)

    def test_smoke_stderr_reaches_the_degraded_tail(self):
        w = self.make_world(slug="aperture/lab", title="Calibrate the portal", pr=44, ticket=21)
        output = ["portal check 1: ok", "portal check 2: slow"]
        self.scenario()

        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2", FAKE_SMOKE_OUTPUT=self.smoke_output(w, output),
                           FAKE_SMOKE_STDERR="portal check 3: warning: cache cold")

        self.assertExit(p, 0)
        creates = self.creates()
        self.assertEqual(len(creates), 1, [c["argv"] for c in self.gh_calls()])
        block = self.fenced_with(self.body(creates[0]), "portal check")
        self.assertTrue(set(output) | {"portal check 3: warning: cache cold"} <= set(block), block)

    def test_degraded_label_is_made_sure_of_before_the_issue(self):
        for fails in (False, True):
            with self.subTest(label_create_fails=fails):
                w = self.make_world(f"label-{fails}", slug="hooli/web", title="Move the footer", pr=12, ticket=4)
                self.scenario(w, label_create_fails=fails)

                p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2")

                self.assertExit(p, 0)
                at = self.index_of("create", w)
                made = [(i, argv) for i, label, argv in self.label_creates(w) if label == "degraded"]
                self.assertTrue(any(i < at for i, _ in made), "gh label create degraded before gh issue create")
                for _, argv in made:
                    self.assertFalse({"--force", "-f"} & set(argv), "a customised degraded label stays as it is")
                self.assertDegradedIssue(w, self.creates(w)[0])

    # ============================================================= GitHub or deploy.sh failing
    def answer_first(self, w, contains, response):
        """Put a call answering `contains` with `response` ahead of the scenario's own answers."""
        with open(w.scenario_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["calls"].insert(0, {"contains": list(contains), **response})
        write(w.scenario_path, json.dumps(data))

    def test_a_failed_freeze_lookup_deploys_nothing_unless_forced(self):
        answers = [("gh exits 1", {"stderr": "HTTP 502: Bad Gateway (https://api.github.com/graphql)\n", "exit": 1}),
                   ("gh exits 4", {"stderr": "gh: To use GitHub CLI in automation, set GH_TOKEN\n", "exit": 4}),
                   ("not JSON", {"stdout": "<html><body>rate limited</body></html>\n"}),
                   ("empty", {"stdout": ""})]
        for name, answer in answers:
            for forced in (False, True):
                with self.subTest(name, forced=forced):
                    w = self.make_world(re.sub(r"\W+", "-", f"{name}-{forced}"), slug="massive/dynamic",
                                        title="Shard the ledger", pr=83, ticket=52, incident=310)
                    self.scenario(w)
                    self.answer_first(w, ["issue", "list", "deploy-frozen"], answer)
                    before = self.snapshot(w)

                    if forced:
                        p = self.run_guard(w, "--force")
                        self.assertExit(p, 0)
                        self.assertEqual(self.ran(w), self.deployed_and_smoked(w), "--force deploys anyway")
                    else:
                        # a failing smoke, so a guard that deployed past the failed lookup would also revert
                        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="1")
                        self.assertExit(p, 1)
                        self.assertEqual(self.ran(w), [], "neither deploy.sh nor smoke.sh runs")
                    self.assertEqual(self.snapshot(w), before, "no git write")
                    self.assertEqual(self.writes(w), [], "nothing filed on GitHub")

    def test_deploy_timeout_is_read_from_the_environment(self):
        # 2 s against a 40 s deploy times out, 15 s against a 3 s deploy passes: no constant timeout does both
        cases = [("2", "40", True), ("15", "3", False)]
        for limit, sleep, times_out in cases:
            with self.subTest(timeout=limit, sleep=sleep):
                w = self.make_world(f"d{limit}", slug="wonka/factory", title="Retool the line", pr=48, ticket=27,
                                    incident=95)
                self.scenario(w)
                before = self.snapshot(w)
                # on a timeout the smoke would fail, so a guard that went on to smoke.sh would also revert
                env = {"FAKE_SMOKE_EXIT_V2": "1"} if times_out else {}
                started = time.monotonic()
                try:
                    p = self.run_guard(w, timeout=25, DEPLOY_GUARD_DEPLOY_TIMEOUT=limit, FAKE_DEPLOY_SLEEP=sleep,
                                       **env)
                except subprocess.TimeoutExpired:
                    self.fail(f"DEPLOY_GUARD_DEPLOY_TIMEOUT={limit} did not bound a {sleep} s scripts/deploy.sh")
                elapsed = time.monotonic() - started

                if times_out:
                    self.assertLess(elapsed, 20, "DEPLOY_GUARD_DEPLOY_TIMEOUT bounds deploy.sh")
                    self.assertExit(p, 1)
                    self.assertEqual(self.ran(w), [f"deploy {w.sha} version: 2"], "no smoke after a timed-out deploy")
                    self.assertEqual(self.snapshot(w), before, "no revert, no git write")
                    self.assertEqual(self.writes(w), [], "no incident, no degraded issue")
                else:
                    self.assertExit(p, 0)
                    self.assertEqual(self.ran(w), self.deployed_and_smoked(w))
                    self.assertEqual(self.snapshot(w), before)
                    self.assertEqual(self.writes(w), [])

    def test_unreachable_github_fails_degraded_and_never_reverts(self):
        refused = {"stderr": "error connecting to api.github.com\n", "exit": 1}
        cases = [  # (name, open degraded issue, failing call)
            ("list fails", False, ["issue", "list", "degraded"]),
            ("comment fails", True, ["issue", "comment"]),
            ("create fails", False, ["issue", "create"])]
        for name, open_issue, failing in cases:
            for via_run in (False, True):
                with self.subTest(name, via_run=via_run):
                    w = self.make_world(re.sub(r"\W+", "-", f"{name}-{via_run}"), slug="black-mesa/sector-c",
                                        title="Align the spectrometer", pr=61, ticket=19, incident=240)
                    log = [f"spectrometer probe {i}: p95 2600 ms" for i in range(1, 5)]
                    degraded = [self.issue(w, 88, f"Degraded: {w.slug}", "degraded")] if open_issue else []
                    self.scenario(w, degraded=degraded)
                    self.answer_first(w, failing, refused)
                    before = self.snapshot(w)

                    if via_run:
                        p = self.run_guard(w, FAKE_SMOKE_EXIT_V2="2", FAKE_SMOKE_OUTPUT=self.smoke_output(w, log))
                        self.assertEqual(self.ran(w), self.deployed_and_smoked(w), "no redeploy, no second smoke")
                    else:
                        smoke_log = write(os.path.join(w.base, "degraded.log"), "\n".join(log) + "\n")
                        p = self.degraded_cmd(w, smoke_log)
                        self.assertEqual(self.ran(w), [], "degraded deploys nothing and runs no smoke")

                    self.assertExit(p, 1)
                    self.assertEqual(self.snapshot(w), before, "no revert, no git write")
                    self.assertEqual([self.title(c) for c in self.creates(w) if self.title(c).startswith("Incident")],
                                     [], "no incident")
                    for verb in ("edit", "reopen"):
                        self.assertEqual([c["argv"] for c in self.gh_calls(w) if c["argv"][:2] == ["issue", verb]],
                                         [], f"no gh issue {verb}")


# ---------------------------------------------------------------- deploy.yml v2
DEPLOY_YML = os.path.join(PLUGIN, "templates", "deploy.yml")
TOKEN = "ghs_fake_workflow_token"


def yaml_scalar(text):
    text = text.strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        m = re.match(r"'((?:[^']|'')*)'" if quote == "'" else r'"((?:[^"\\]|\\.)*)"', text)
        if m:
            return m.group(1).replace("''", "'") if quote == "'" else m.group(1)
    text = re.split(r"\s+#", text, 1)[0].strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [yaml_scalar(part) for part in inner.split(",")] if inner else []
    return None if text in ("", "~", "null") else text


class MiniYaml:
    """Enough YAML for a workflow file: block mappings and lists, flow lists, quoted scalars and | > blocks."""
    KEY = re.compile(r"""^('(?:[^']|'')*'|"[^"]*"|[^'"\s\-\[{][^:]*?|-[^\s:][^:]*?):(?:\s+(.*))?$""")

    def __init__(self, text):
        self.lines = text.expandtabs().splitlines()
        self.i = 0

    @classmethod
    def load(cls, text):
        y = cls(text)
        value = y.node(0)
        if y.peek() is not None:
            raise ValueError(f"line {y.i + 1}: unexpected {y.lines[y.i]!r}")
        return value

    def peek(self):
        while self.i < len(self.lines) and (not self.lines[self.i].strip()
                                            or self.lines[self.i].lstrip().startswith("#")):
            self.i += 1
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        return len(line) - len(line.lstrip(" ")), line.strip()

    @staticmethod
    def is_item(content):
        return content == "-" or content.startswith("- ")

    def node(self, indent):
        p = self.peek()
        if p is None or p[0] < indent:
            return None
        return self.seq(p[0]) if self.is_item(p[1]) else self.mapping(p[0])

    def mapping(self, indent):
        out = {}
        while True:
            p = self.peek()
            if p is None or p[0] < indent:
                return out
            if p[0] > indent or self.is_item(p[1]):
                raise ValueError(f"line {self.i + 1}: unexpected {p[1]!r}")
            m = self.KEY.match(p[1])
            if not m:
                raise ValueError(f"line {self.i + 1}: not a key: {p[1]!r}")
            self.i += 1
            out[yaml_scalar(m.group(1))] = self.value(m.group(2) or "", indent)

    def value(self, rest, indent):
        rest = rest.strip()
        if re.fullmatch(r"[|>][-+]?\d*", rest):
            return self.block(indent, rest[0] == ">")
        if not rest or rest.startswith("#"):
            p = self.peek()
            if p and (p[0] > indent or (p[0] == indent and self.is_item(p[1]))):
                return self.node(p[0])
            return None
        return yaml_scalar(rest)

    def seq(self, indent):
        out = []
        while True:
            p = self.peek()
            if p is None or p[0] < indent or (p[0] == indent and not self.is_item(p[1])):
                return out
            if p[0] > indent:
                raise ValueError(f"line {self.i + 1}: unexpected {p[1]!r}")
            rest = p[1][1:]
            item = rest.lstrip()
            if not item:
                self.i += 1
                out.append(self.node(indent + 1))
            elif self.KEY.match(item):
                inner = indent + 1 + len(rest) - len(item)
                self.lines[self.i] = " " * inner + item
                out.append(self.mapping(inner))
            else:
                self.i += 1
                out.append(yaml_scalar(item))

    def block(self, indent, folded):
        lines = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            lines.append(line)
            self.i += 1
        while lines and not lines[-1].strip():
            lines.pop()
        cut = min((len(x) - len(x.lstrip(" ")) for x in lines if x.strip()), default=0)
        lines = [x[cut:] for x in lines]
        return (" ".join(lines) if folded else "\n".join(lines)) + "\n"


EXPR_TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|&&|\|\||!|\(|\))"
                        r"|(?P<ref>[A-Za-z_][\w.-]*))")


def gh_number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value.strip()) if value.strip() else 0.0
    except ValueError:
        return float("nan")


def gh_truthy(value):
    if isinstance(value, float) and value != value:
        return False
    return value not in (None, False, 0, 0.0, "")


def gh_equal(a, b):
    """GitHub's loose equality: same types compare (strings ignoring case), else both become numbers."""
    if type(a) is type(b) or (a is None and b is None):
        return a.lower() == b.lower() if isinstance(a, str) else a == b
    x, y = gh_number(a), gh_number(b)
    return x == y and x == x


def gh_render(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def gh_eval(expr, ctx):
    """A GitHub Actions expression: context references, literals, !, ==, !=, && and || with parentheses."""
    tokens, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = EXPR_TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"the test cannot evaluate ${{{{ {expr} }}}} at {expr[pos:]!r}")
        tokens.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
        while pos < len(expr) and expr[pos].isspace():
            pos += 1
    at = [0]

    def peek():
        return tokens[at[0]] if at[0] < len(tokens) else (None, None)

    def take():
        at[0] += 1
        return tokens[at[0] - 1]

    def primary():
        kind, text = take() if at[0] < len(tokens) else (None, None)
        if kind == "op" and text == "(":
            value = either()
            if take() != ("op", ")"):
                raise ValueError(f"unbalanced parentheses in {expr!r}")
            return value
        if kind == "op" and text == "!":
            return not gh_truthy(primary())
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "ref":
            low = text.lower()
            if low in ("true", "false"):
                return low == "true"
            if low == "null":
                return None
            if low not in ctx:
                raise ValueError(f"the test knows no context {text!r} (in ${{{{ {expr} }}}})")
            return ctx[low]
        raise ValueError(f"the test cannot evaluate ${{{{ {expr} }}}}")

    def compare():
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            op = take()[1]
            right = primary()
            left = gh_equal(left, right) == (op == "==")
        return left

    def both():
        left = compare()
        while peek() == ("op", "&&"):
            take()
            right = compare()
            left = right if gh_truthy(left) else left
        return left

    def either():
        left = both()
        while peek() == ("op", "||"):
            take()
            right = both()
            left = left if gh_truthy(left) else right
        return left

    value = either()
    if at[0] != len(tokens):
        raise ValueError(f"the test cannot evaluate ${{{{ {expr} }}}}")
    return value


def gh_substitute(text, ctx):
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: gh_render(gh_eval(m.group(1), ctx)), str(text), flags=re.S)


DEPLOY_STEP_SH = '#!/bin/sh\necho deployed >> "$DEPLOY_LOG"\necho "deploying"\nexit "${FAKE_DEPLOY_EXIT:-0}"\n'


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-deployyml-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def load(self):
        self.assertTrue(os.path.isfile(DEPLOY_YML), f"{DEPLOY_YML} does not exist")
        with open(DEPLOY_YML, encoding="utf-8") as fh:
            text = fh.read()
        doc = MiniYaml.load(text)
        self.assertIsInstance(doc, dict, text)
        jobs = doc.get("jobs") or {}
        self.assertEqual(len(jobs), 1, f"one job: {list(jobs)}")
        job = next(iter(jobs.values()))
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        return doc, job, steps

    @staticmethod
    def flat(value):
        return re.sub(r"\s+", "", str(value))

    def the_run_step(self, steps):
        runs = [s for s in steps if "run" in s]
        self.assertEqual(len(runs), 1, f"one run step: {steps}")
        return runs[0]

    def test_deploy_yml_v2_triggers_permissions_concurrency_and_step(self):
        doc, job, steps = self.load()
        on = doc.get("on")
        self.assertIsInstance(on, dict, f"on: {on!r}")
        self.assertEqual(set(on), {"push", "workflow_dispatch"})
        branches = (on.get("push") or {}).get("branches")
        self.assertEqual([branches] if isinstance(branches, str) else branches, ["main"])
        inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
        self.assertIn("force", inputs, f"workflow_dispatch inputs: {inputs!r}")
        force = inputs["force"] or {}
        self.assertEqual(str(force.get("type")).lower(), "boolean")
        if force.get("default") is not None:
            self.assertEqual(str(force["default"]).lower(), "false")

        concurrency = doc.get("concurrency") or job.get("concurrency") or {}
        self.assertIsInstance(concurrency, dict, f"concurrency: {concurrency!r}")
        self.assertEqual(concurrency.get("group"), "deploy-main")
        self.assertEqual(str(concurrency.get("cancel-in-progress")).lower(), "false")

        permissions = doc.get("permissions") or job.get("permissions") or {}
        self.assertIsInstance(permissions, dict, f"permissions: {permissions!r}")
        self.assertEqual(permissions.get("contents"), "write")
        self.assertEqual(permissions.get("issues"), "write")

        checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1, steps)
        self.assertEqual(str((checkouts[0].get("with") or {}).get("fetch-depth")), "0")
        step = self.the_run_step(steps)
        self.assertEqual([s for s in steps if s is not step and s is not checkouts[0]], [],
                         "checkout and the one run step only")
        self.assertLess(steps.index(checkouts[0]), steps.index(step))
        script = " ".join(step["run"].split())
        self.assertIn("python3 scripts/deploy_guard.py run --sha", script)
        self.assertTrue("GITHUB_SHA" in script or "github.sha" in self.flat(script), script)
        self.assertIn("--force", script)
        self.assertIn("scripts/deploy.sh", script, "without the guard, v1's deploy.sh logic")
        self.assertIn("no deploy script", script)
        env = {**(doc.get("env") or {}), **(job.get("env") or {}), **(step.get("env") or {})}
        self.assertEqual(self.flat(env.get("GH_TOKEN")), "${{github.token}}", env)

    def run_step(self, doc, job, step, *, event, force, sha, guard, deploy, py_exit=0, deploy_exit=0):
        """The step as the runner runs it: expressions filled in, bash -eo pipefail, in a fresh project."""
        project = os.path.join(self.tmp, f"project-{len(os.listdir(self.tmp))}")
        os.makedirs(os.path.join(project, "scripts"))
        if guard:
            write(os.path.join(project, "scripts", "deploy_guard.py"), "raise SystemExit('the fake python3 runs')\n")
        if deploy:
            write(os.path.join(project, "scripts", "deploy.sh"), DEPLOY_STEP_SH)     # not executable, as in v1
        dispatch = event == "workflow_dispatch"
        ctx = {"github.sha": sha, "github.token": TOKEN, "secrets.github_token": TOKEN, "github.event_name": event,
               "github.ref": "refs/heads/main", "github.ref_name": "main", "github.repository": "acme/shop",
               "github.workspace": project, "inputs.force": force if dispatch else None,
               "github.event.inputs.force": (gh_render(force) if dispatch else None)}
        condition = step.get("if")
        if condition is not None:
            raw = str(condition).strip()
            raw = raw[3:-2] if raw.startswith("${{") and raw.endswith("}}") else raw
            self.assertTrue(gh_truthy(gh_eval(raw, ctx)), f"the step is skipped on {event}: if: {condition}")
        py_log, deploy_log = os.path.join(project, "..", f"{os.path.basename(project)}-py.log"), \
            os.path.join(project, "..", f"{os.path.basename(project)}-deploy.log")
        env = {"PATH": os.pathsep.join([BINS["pybin"], "/usr/bin", "/bin"]), "HOME": self.tmp, "GITHUB_SHA": sha,
               "GITHUB_EVENT_NAME": event, "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "acme/shop",
               "GITHUB_WORKSPACE": project, "FAKE_PY_LOG": py_log, "FAKE_PY_EXIT": str(py_exit),
               "DEPLOY_LOG": deploy_log, "FAKE_DEPLOY_EXIT": str(deploy_exit)}
        for scope in (doc, job, step):
            for key, value in (scope.get("env") or {}).items():
                env[key] = gh_substitute(value, ctx)
        script = write(os.path.join(self.tmp, f"{os.path.basename(project)}-step.sh"),
                       gh_substitute(step["run"], ctx))
        shell = str(step.get("shell") or "bash")
        argv = (["/bin/sh", "-e", script] if shell == "sh" else
                ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", script])
        p = subprocess.run(argv, cwd=project, env=env, capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(py_log):
            with open(py_log, encoding="utf-8") as fh:
                calls = [json.loads(line) for line in fh if line.strip()]
        deploys = 0
        if os.path.exists(deploy_log):
            with open(deploy_log, encoding="utf-8") as fh:
                deploys = len(fh.read().splitlines())
        return p, calls, deploys

    def assertGuardCall(self, calls, sha, forced):
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]["argv"]
        self.assertEqual([os.path.normpath(argv[0])] + argv[1:2], ["scripts/deploy_guard.py", "run"], argv)
        rest = []
        for tok in argv[2:]:
            rest += tok.split("=", 1) if tok.startswith("--sha=") else [tok]
        self.assertEqual(rest.count("--force"), 1 if forced else 0, argv)
        self.assertEqual([t for t in rest if t != "--force"], ["--sha", sha], argv)
        self.assertEqual(calls[0]["gh_token"], TOKEN, "GH_TOKEN from github.token")

    def test_deploy_yml_v2_step_runs_the_guard_or_falls_back_to_v1(self):
        doc, job, steps = self.load()
        step = self.the_run_step(steps)
        sha_a, sha_b = "3f9c2e1" + "a" * 33, "b7d40c9" + "1" * 33
        cases = [  # (name, event, force input, sha, guard present, deploy.sh present, py exit, deploy exit)
            ("push", "push", None, sha_a, True, True, 0, 0),
            ("dispatch, force false", "workflow_dispatch", False, sha_b, True, True, 0, 0),
            ("dispatch, force true", "workflow_dispatch", True, sha_a, True, True, 0, 0),
            ("guard exits 1", "push", None, sha_b, True, True, 1, 0),
            ("no guard", "push", None, sha_a, False, True, 0, 0),
            ("no guard, forced", "workflow_dispatch", True, sha_b, False, True, 0, 0),
            ("no guard, no deploy.sh", "push", None, sha_a, False, False, 0, 0),
            ("no guard, deploy.sh exits 3", "push", None, sha_b, False, True, 0, 3)]
        for name, event, force, sha, guard, deploy, py_exit, deploy_exit in cases:
            with self.subTest(name):
                p, calls, deploys = self.run_step(doc, job, step, event=event, force=force, sha=sha, guard=guard,
                                                  deploy=deploy, py_exit=py_exit, deploy_exit=deploy_exit)
                out = f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
                if guard:
                    self.assertGuardCall(calls, sha, forced=force is True)
                    self.assertEqual(deploys, 0, "the guard runs deploy.sh itself")
                    if py_exit:
                        self.assertNotEqual(p.returncode, 0, "the guard's failure fails the step: " + out)
                    else:
                        self.assertEqual(p.returncode, 0, out)
                    continue
                self.assertEqual(calls, [], "no guard, no python3 scripts/deploy_guard.py")
                if not deploy:
                    self.assertEqual(p.returncode, 0, out)
                    self.assertIn("no deploy script", p.stdout + p.stderr)
                elif deploy_exit:
                    self.assertEqual(deploys, 1, out)
                    self.assertNotEqual(p.returncode, 0, "a failed deploy.sh fails the step: " + out)
                else:
                    self.assertEqual(deploys, 1, out)
                    self.assertEqual(p.returncode, 0, out)


if __name__ == "__main__":
    unittest.main()
