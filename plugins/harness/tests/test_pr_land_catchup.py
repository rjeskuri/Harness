"""harness-pr land --catch-up (#386): a PR behind main gets origin/main merged into its ticket worktree and pushed,
its checks waited for at that merge, then today's land; main moving during the wait takes another round, up to
--rounds. A conflict, red checks at the merge, no clean worktree at the head, or no round left refuses with exit 1;
checks still pending when --timeout is used up exit 8. Without --catch-up a behind PR is refused as before.

The fixtures are pr_support's (the scripted fake gh, the harness-board and harness-work stand-ins) and a real
project per test or table row: a bare origin, the main checkout `proj`, the ticket worktree on BRANCH pushed with
an upstream (as `harness-pr open` pushes it), and a second clone that moves origin's main as other PRs landing do.
In front of fakegh sits a "live" gh that answers from the bare origin what GitHub derives from pushed history:
`gh pr view` (headRefOid is origin's ticket branch, shown `lag` views late after it moves, as GitHub is; the
statusCheckRollup scripted per head: by sha, else per pushed head in order, one entry per view, the last repeating),
the compare of main with a commit (behind_by counted by git) and the PR's commits. Main moving during the wait is a
commit prepared under refs/moves/<k> that the live gh makes origin's main when it first shows the k-th pushed head.
Every other gh call goes to fakegh. The harness review is of the pre-merge head, so each land after a catch-up merge
passes land_gate's review-at-head check through the clean merges.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_pr_land_catchup.py -v
"""

from __future__ import annotations

import re
import unittest

import pr_support
from gitrepo_support import run_in_process
from pr_support import *  # noqa: F401,F403

# The live gh: argv[1] the git binary, argv[2] fakegh, the rest gh's own arguments. LIVEGH_SCENARIO names its JSON:
# {"origin", "branch", "number", "view": the PR's other fields, "checks": {sha: rollup}, "new_heads": [[rollup per
# view, ...] per pushed head], "moves": {"<k>": ref}, "lag": views}; its state is kept beside it in .state.json.
LIVE_GH = r'''import json, os, re, subprocess, sys

GIT, FAKEGH, ARGV = sys.argv[1], sys.argv[2], sys.argv[3:]
SCENARIO = os.environ["LIVEGH_SCENARIO"]
with open(SCENARIO, encoding="utf-8") as fh:
    SC = json.load(fh)
STATE = SCENARIO + ".state.json"
PULLS = "repos/o/r/pulls/" + SC["number"] + "/commits"


def git(*args):
    done = subprocess.run([GIT, "--git-dir", SC["origin"]] + list(args), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout.strip()


def sha_of(rev):
    code, out = git("rev-parse", "--verify", "--quiet", rev + "^{commit}")
    return out if code == 0 and out else None


def log():
    with open(os.environ["FAKEGH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": ARGV, "stdin": ""}) + "\n")


def answer(data):
    if "--jq" in ARGV:
        expr = ARGV[ARGV.index("--jq") + 1]
        if expr != ".":
            if not re.fullmatch(r"(\.[A-Za-z_]\w*)+", expr):
                sys.stderr.write("livegh: unsupported --jq " + expr + "\n")
                sys.exit(1)
            for key in expr[1:].split("."):
                data = data.get(key) if isinstance(data, dict) else None
        sys.stdout.write((data if isinstance(data, str) else json.dumps(data)) + "\n")
    else:
        sys.stdout.write(json.dumps(data))
    sys.exit(0)


def view():
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            state = json.load(fh)
    actual = sha_of("refs/heads/" + SC["branch"])
    shown = state.get("shown") or actual
    if actual != shown:                       # the branch moved: GitHub shows the old head for `lag` more views
        if state.get("lag_for") != actual:
            state["lag_for"], state["lag_left"] = actual, SC.get("lag", 0)
        if state["lag_left"] > 0:
            state["lag_left"] -= 1
        else:
            shown = actual
    state["shown"] = shown
    if shown in SC["checks"]:
        rollup = SC["checks"][shown]
    else:
        new = state.setdefault("new", [])
        if shown not in new:
            new.append(shown)
            ref = SC.get("moves", {}).get(str(len(new)))
            if ref:                           # main moves while the checks at this pushed head run
                git("update-ref", "refs/heads/main", ref)
        heads = SC.get("new_heads") or [[[]]]
        seq = heads[min(new.index(shown), len(heads) - 1)]
        seen = state.setdefault("seen", {})
        n = seen.get(shown, 0)
        seen[shown] = n + 1
        rollup = seq[min(n, len(seq) - 1)]
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    answer(dict(SC["view"], headRefOid=shown, statusCheckRollup=rollup))


def compare(token):
    base, _, head = token.split("/compare/", 1)[1].partition("...")
    head_sha, base_sha = sha_of(head), sha_of("refs/heads/" + base)
    if not head_sha or not base_sha:
        sys.stderr.write("gh: Not Found (HTTP 404)\n")
        sys.exit(1)
    behind = int(git("rev-list", "--count", head_sha + ".." + base_sha)[1])
    ahead = int(git("rev-list", "--count", base_sha + ".." + head_sha)[1])
    status = "diverged" if behind and ahead else "behind" if behind else "ahead" if ahead else "identical"
    answer({"behind_by": behind, "ahead_by": ahead, "status": status, "total_commits": ahead})


def commits():
    out = git("rev-list", "--reverse", "--parents", "refs/heads/main..refs/heads/" + SC["branch"])[1]
    items = []
    for line in out.splitlines():
        sha, *parents = line.split()
        when = "2026-10-12T09:00:00Z"
        items.append({"sha": sha, "parents": [{"sha": p} for p in parents],
                      "commit": {"message": "Commit " + sha[:7], "author": {"name": "Test", "date": when},
                                 "committer": {"name": "Test", "date": when}}})
    answer([items] if "--slurp" in ARGV else items)


if ARGV[:2] == ["pr", "view"] and "--json" in ARGV and SC["number"] in ARGV:
    log()
    view()
if ARGV[:1] == ["api"]:
    for tok in ARGV:
        if tok.startswith("repos/o/r/compare/"):
            log()
            compare(tok)
        if tok.split("?")[0] == PULLS:
            log()
            commits()
os.execv(sys.executable, [sys.executable, "-I", "-S", FAKEGH] + ARGV)
'''

LIVE = {}


def setUpModule():
    pr_support.setUpModule()
    script = write(os.path.join(BINS["root"], "livegh.py"), LIVE_GH)
    livebin = os.path.join(BINS["root"], "livebin")
    gh = executable(os.path.join(livebin, "gh"),
                    f'#!{SH}\nprintf \'%s\\n\' "gh $*" >> "$ORDER_LOG"\n'
                    f'exec "{sys.executable}" -I -S "{script}" "{GIT}" "{FAKEGH}" "$@"\n')
    LIVE.update(bin=livebin, gh=gh)


def tearDownModule():
    pr_support.tearDownModule()


# The checks at a pushed merge that fail: two of them, among one that passes.
RED_AT_MERGE = [check_run("unit-tests", conclusion="FAILURE"), check_run("lint"),
                status_context("e2e/smoke", "FAILURE")]
MERGE_CALL = ["pr", "merge", "34", "-R", "o/r", "--squash", "--delete-branch"]


class CatchUpTest(PrTest):
    """A real project per test or table row (new_repos), the live gh in front of fakegh, and the land conversation.
    Each test's first `harness-pr` run is a process of its own; its later runs (the rows of a table, the second run
    of a resumed land) run the tool in this process through gitrepo_support.run_in_process, with the same argv, cwd
    and environment."""

    ran_a_process = False

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": EPIC_SAID}}})

    # ----------------------------------------------------------- the project
    def make_repos(self):
        self.new_repos()

    def new_repos(self):
        """A fresh project under tmp/r<k>: the bare origin; `proj` (README.md, `.claude/worktrees/` excluded, the
        commit "First") pushed to it; the ticket worktree on BRANCH from main with widget.py and
        tests/test_widget.py committed as "Add the widget" and pushed with an upstream (self.head0); the clone
        `elsewhere` that moves origin's main; and the ticket's cached parent, epic #3."""
        self.built = getattr(self, "built", 0) + 1
        root = self.mk(f"r{self.built}")
        self.origin, self.main = self.mk(f"r{self.built}", "origin.git"), self.mk(f"r{self.built}", "proj")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, ".git", "info", "exclude"), ".claude/worktrees/\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "t-12-add-the-widget")
        self.git(self.main, "worktree", "add", "-q", "--no-track", "-b", BRANCH, self.wt, "main")
        write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 2\n")
        write(os.path.join(self.wt, "tests", "test_widget.py"), "def test_frob():\n    assert True\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add the widget")
        self.git(self.wt, "push", "-q", "-u", "origin", BRANCH)
        self.head0 = self.git(self.wt, "rev-parse", "HEAD")
        self.elsewhere = os.path.join(root, "elsewhere")
        subprocess.run([GIT, "clone", "-q", self.origin, self.elsewhere], env=self.env(), check=True,
                       capture_output=True)
        self.moved = 0
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    def main_commits(self, commits, ref="refs/heads/main"):
        """A commit in `elsewhere` for each (files, message), `files` path: text, pushed together to origin's
        `ref`; the last one's sha. Nothing is fetched into the project: catching up is the tool's job."""
        for files, message in commits:
            for path, text in files.items():
                write(os.path.join(self.elsewhere, path), text)
            self.git(self.elsewhere, "add", "-A")
            self.git(self.elsewhere, "commit", "-q", "-m", message)
        self.git(self.elsewhere, "push", "-q", "origin", f"HEAD:{ref}")
        return self.git(self.elsewhere, "rev-parse", "HEAD")

    def advance_main(self, n):
        """`n` commits on origin's main, each adding a file of its own outside the PR's; main's new sha."""
        commits = []
        for _ in range(n):
            self.moved += 1
            commits.append(({f"elsewhere/change_{self.moved}.py": f"CHANGE = {self.moved}\n"},
                            f"Another PR lands ({self.moved})"))
        return self.main_commits(commits)

    def prepare_moves(self, n):
        """`n` later commits of main, the k-th under refs/moves/<k> (each on top of the one before), for the live gh
        to make origin's main while the k-th pushed head's checks run; ({"<k>": ref}, [their shas])."""
        moves, shas = {}, []
        for k in range(1, n + 1):
            ref = f"refs/moves/{k}"
            shas.append(self.main_commits([({f"elsewhere/later_{k}.py": f"LATER = {k}\n"},
                                            f"Main moves during the wait ({k})")], ref=ref))
            moves[str(k)] = ref
        return moves, shas

    # ----------------------------------------------------------- reading origin
    def origin_rev(self, ref):
        return self.git(self.origin, "rev-parse", ref)

    def pr_head(self):
        return self.origin_rev(f"refs/heads/{BRANCH}")

    def origin_refs(self):
        return self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")

    def local_branch(self):
        return self.git(self.main, "rev-parse", f"refs/heads/{BRANCH}")

    def no_merge_in_progress(self):
        return subprocess.run([GIT, "-C", self.wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"], env=self.env(),
                              capture_output=True).returncode != 0

    # ----------------------------------------------------------- the fakes
    def env(self, stubs=True, **extra):
        env = super().env(stubs=stubs, **extra)
        env.update(PATH=LIVE["bin"] + os.pathsep + env["PATH"], HARNESS_GH=LIVE["gh"],
                   LIVEGH_SCENARIO=os.path.join(self.tmp, "live.json"))
        return env

    def live(self, checks=None, new_heads=(), moves=None, lag=1):
        """The live gh's scenario, its state reset: `checks` {sha: rollup} (the pre-merge head green by default),
        `new_heads` the rollups of each pushed head view by view, `moves` {"<k>": ref}, `lag` views."""
        view = dict(pr_data(FIXTURE_A))
        view.update(land_view())
        for key in ("headRefOid", "statusCheckRollup"):
            view.pop(key)
        path = os.path.join(self.tmp, "live.json")
        write(path, json.dumps({"origin": self.origin, "branch": BRANCH, "number": "34", "view": view,
                                "checks": {self.head0: GREEN} if checks is None else checks,
                                "new_heads": list(new_heads), "moves": moves or {}, "lag": lag}))
        if os.path.exists(path + ".state.json"):
            os.unlink(path + ".state.json")

    def conversation(self):
        """fakegh's part of the land, its logs cleared: a clean harness review of the pre-merge head (self.head0),
        the account, the repository, the merge, the ticket and the Resolution's reads."""
        review, comments = harness_review(101, at(10))
        issue = {"state": "OPEN", "labels": [{"name": "task"}, {"name": "status:in-review"}], "comments": []}
        self.scenario(gate_calls([(dict(review, commit_id=self.head0), comments)]) + [
            {"match": ["api", "user"], "contains": ["--jq"], "stdout": "rjeskuri\n"},
            {"match": ["api", "user"], "stdout_json": {"login": "rjeskuri", "type": "User"}},
            REPO_VIEW, {"match": ["pr", "merge", "34"], "stdout": ""},
            {"match": ["issue", "view", "12", "--json", "state,labels,comments"], "stdout_json": issue},
            {"match": ["issue", "edit", "12"], "stdout": ""},
            *files_calls(34, FIXTURE_A["files"]),
            {"match": ["issue", "view", "12"],
             "stdout_json": dict(issue, number=12, title="Add the widget", body=TICKET_12_BODY,
                                 url="https://github.com/o/r/issues/12")},
            {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": []}])

    def pr(self, *args, cwd=None, stubs=True, **env):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().pr(*args, cwd=cwd, stubs=stubs, **env)
        return run_in_process(TOOL, args=args, cwd=cwd or self.main, env=self.env(stubs=stubs, **env))

    # ----------------------------------------------------------- asserts
    def merged(self):
        return [a for a in self.gh_argv() if a[:2] == ["pr", "merge"]]

    def assertLanded(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.merged(), [MERGE_CALL])
        self.assertFalse(os.path.exists(self.wt), "the worktree should be removed")

    def assertNotLanded(self, p, code):
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.merged(), [])
        self.assertTrue(os.path.isdir(self.wt), "the worktree should be kept")

    def assertCatchUpMerges(self, mains, base=None):
        """Origin's ticket branch is `base` (self.head0) plus one merge commit per entry of `mains`, oldest first:
        each merge's parents the commit before it and that main, its tree what `git merge-tree --write-tree` makes
        of the two. The merges' shas, oldest first."""
        base = base or self.head0
        chain = self.git(self.origin, "rev-list", "--first-parent", "--reverse", f"{base}..refs/heads/{BRANCH}")
        chain = chain.split()
        self.assertEqual(len(chain), len(mains), f"merge commits pushed on {BRANCH}: {chain}")
        before = base
        for sha, tip in zip(chain, mains):
            parents = self.git(self.origin, "rev-list", "--parents", "-n1", sha).split()[1:]
            self.assertEqual(parents, [before, tip], f"the parents of {sha[:7]}")
            merged = self.git(self.origin, "merge-tree", "--write-tree", before, tip).splitlines()[0]
            self.assertEqual(self.origin_rev(f"{sha}^{{tree}}"), merged, f"the tree of {sha[:7]}")
            before = sha
        return chain

    # =========================================================== the tests
    def test_behind_clean_merge_waits_and_lands(self):
        """Behind main by 1, then (a fresh project) by 3 commits on other files: one merge commit of the head and
        origin/main pushed, the checks waited for at it (GitHub shows the old, green head one view late; the merge's
        own checks run once), then today's land: the squash merge, the worktree removed, exit 0."""
        for behind in (1, 3):
            with self.subTest(behind=behind):
                if behind != 1:
                    self.new_repos()
                tip = self.advance_main(behind)
                self.live(new_heads=[[PENDING, GREEN]])
                self.conversation()
                p = self.pr("land", "34", "--catch-up")
                self.assertLanded(p)
                [merge] = self.assertCatchUpMerges([tip])
                self.assertIn(merge[:7], p.stdout)

    def test_main_moves_during_wait_takes_another_round(self):
        """Behind by 1; main moves again while the checks at the pushed merge run. The default rounds: a second merge
        (of the moved main) is pushed and the PR lands. --rounds 1, main moving once, and --rounds 2, main moving
        twice: every round's merge pushed, then exit 1 naming the rounds, and no gh pr merge."""
        cases = [("the default rounds, main moves once", (), 1, 2, None),
                 ("--rounds 1, main moves once", ("--rounds", "1"), 1, 1, 1),
                 ("--rounds 2, main moves twice", ("--rounds", "2"), 2, 2, 2)]
        for i, (name, flags, moves, merges, rounds) in enumerate(cases):
            with self.subTest(name):
                if i:
                    self.new_repos()
                tip = self.advance_main(1)
                refs, later = self.prepare_moves(moves)
                self.live(new_heads=[[PENDING, GREEN]], moves=refs)
                self.conversation()
                p = self.pr("land", "34", "--catch-up", *flags)
                self.assertCatchUpMerges(([tip] + later)[:merges])
                if rounds is None:
                    self.assertLanded(p)
                    continue
                self.assertNotLanded(p, 1)
                self.assertRegex(p.stderr, rf"(?i)\b{rounds}\b[^\n]*round|round[^\n]*\b{rounds}\b")

    def test_conflict_refuses_without_merging(self):
        """Main changes widget.py and tests/test_widget.py as the branch does, and adds a file of its own: exit 1
        naming the two conflicted files (not the clean one); the worktree's HEAD and status, the local branch and
        every ref on origin as they were, no merge in progress, nothing landed."""
        self.main_commits([({"widget.py": "def frob():\n    return 7\n",
                             "tests/test_widget.py": "def test_frob():\n    assert 7\n",
                             "elsewhere/clean.py": "CLEAN = 1\n"}, "Main changes the widget too")])
        self.live(new_heads=[[GREEN]])
        self.conversation()

        def snapshot():
            return (self.git(self.wt, "rev-parse", "HEAD"), self.git(self.wt, "status", "--porcelain"),
                    self.local_branch(), self.origin_refs())

        before = snapshot()
        p = self.pr("land", "34", "--catch-up")
        self.assertNotLanded(p, 1)
        self.assertRegex(p.stderr, r"(?<![\w/])widget\.py")
        self.assertIn("tests/test_widget.py", p.stderr)
        self.assertNotIn("elsewhere/clean.py", p.stderr)
        self.assertEqual(snapshot(), before)
        self.assertTrue(self.no_merge_in_progress())

    def test_red_checks_after_merge_refuses(self):
        """Behind by 1; the checks at the pushed merge run, then two fail: exit 1 naming both failed checks and not
        the passing one; the merge stays pushed (and in the worktree), nothing landed."""
        tip = self.advance_main(1)
        self.live(new_heads=[[PENDING, RED_AT_MERGE]])
        self.conversation()
        p = self.pr("land", "34", "--catch-up")
        self.assertNotLanded(p, 1)
        for name in ("unit-tests", "e2e/smoke"):
            self.assertIn(name, p.stderr)
        self.assertNotIn("lint", p.stderr)
        [merge] = self.assertCatchUpMerges([tip])
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), merge)

    def test_pending_at_timeout_exits_8_then_resumes(self):
        """Behind by 1, the checks at the pushed merge never finish: with no poll interval and --timeout 1 the land
        exits 8, the merge pushed, saying to run the same command again. Run again once those checks are green: no
        new merge commit, no ref on origin changed, and the PR lands."""
        tip = self.advance_main(1)
        self.live(new_heads=[[PENDING]])
        self.conversation()
        args = ("land", "34", "--catch-up", "--timeout", "1")
        p = self.pr(*args, HARNESS_POLL_SECONDS="0")
        self.assertNotLanded(p, 8)
        [merge] = self.assertCatchUpMerges([tip])
        said = p.stdout + p.stderr
        self.assertIn("land 34", said)
        self.assertIn("--catch-up", said)
        refs = self.origin_refs()
        self.live(checks={merge: GREEN})
        self.conversation()
        p = self.pr(*args, HARNESS_POLL_SECONDS="0")
        self.assertLanded(p)
        self.assertEqual(self.origin_refs(), refs)
        self.assertEqual(self.pr_head(), merge)

    def test_refuses_without_a_clean_worktree_at_head(self):
        """Behind by 1. A modified tracked file in the worktree, a worktree whose HEAD is a commit the PR head lacks,
        and no worktree for the branch: each exit 1 naming the worktree or the branch, with no merge and no push
        (every ref on origin, the local branch and the worktree's HEAD as they were). The same PR with its worktree
        back, clean at the head, catches up and lands."""
        tip = self.advance_main(1)
        refs = self.origin_refs()
        widget = os.path.join(self.wt, "widget.py")

        def dirty():
            write(widget, "def frob():\n    return 5  # not committed\n")

        def stale():
            write(os.path.join(self.wt, "notes.md"), "not pushed yet\n")
            self.git(self.wt, "add", "-A")
            self.git(self.wt, "commit", "-q", "-m", "Not pushed yet")

        rows = [("a dirty worktree", dirty, lambda: self.git(self.wt, "checkout", "--", "widget.py")),
                ("a worktree whose HEAD is not the PR head", stale,
                 lambda: self.git(self.wt, "reset", "-q", "--hard", self.head0)),
                ("no worktree for the branch", lambda: self.git(self.main, "worktree", "remove", self.wt),
                 lambda: self.git(self.main, "worktree", "add", "-q", self.wt, BRANCH))]
        for name, make, undo in rows:
            make()
            try:
                with self.subTest(name):
                    local = self.local_branch()
                    head, kept = None, None
                    if os.path.isdir(self.wt):
                        head = self.git(self.wt, "rev-parse", "HEAD")
                        with open(widget, encoding="utf-8") as fh:
                            kept = fh.read()
                    self.live(new_heads=[[GREEN]])
                    self.conversation()
                    p = self.pr("land", "34", "--catch-up")
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertNotIn("Traceback", p.stderr)
                    self.assertTrue(self.wt in p.stderr or BRANCH in p.stderr,
                                    f"the refusal names neither the worktree nor the branch: {p.stderr}")
                    self.assertEqual(self.merged(), [])
                    self.assertEqual(self.origin_refs(), refs)
                    self.assertEqual(self.local_branch(), local)
                    if head is not None:
                        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), head)
                        self.assertTrue(self.no_merge_in_progress())
                        with open(widget, encoding="utf-8") as fh:
                            self.assertEqual(fh.read(), kept)
            finally:
                undo()
        with self.subTest("the worktree back, clean at the head"):
            self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), self.head0)
            self.live(new_heads=[[GREEN]])
            self.conversation()
            p = self.pr("land", "34", "--catch-up")
            self.assertLanded(p)
            self.assertCatchUpMerges([tip])

    def test_without_catch_up_behind_is_refused_as_before(self):
        """Plain `harness-pr land 34` on a head 2 commits behind main: today's refusal (behind main by 2 commits at the
        head, merge origin/main into the branch), exit 1; no fetch-and-merge, no push, nothing landed."""
        self.advance_main(2)
        self.live(new_heads=[[GREEN]])
        self.conversation()
        refs = self.origin_refs()
        p = self.pr("land", "34")
        self.assertNotLanded(p, 1)
        self.assertIn(f"PR #34 is behind main by 2 commits at {self.head0[:7]}; merge origin/main into {BRANCH}",
                      p.stderr)
        self.assertEqual(self.origin_refs(), refs)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), self.head0)
        self.assertEqual(self.local_branch(), self.head0)


if __name__ == "__main__":
    unittest.main()
