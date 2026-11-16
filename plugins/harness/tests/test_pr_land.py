"""harness-pr land: the merge and cleanup, a dirty main checkout, the Resolution comment, the review gate, refusals, and open --notes (which the Resolution repeats).

Split from test_pr.py; the fixtures are in pr_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_land.py" -v
"""

from __future__ import annotations

import unittest

import pr_support
from pr_support import *  # noqa: F401,F403

# ------------------------------------------------------------------ the land fixtures of the head checks (#337)
# pr_support.py counts as source to the tests-first hook, so the fixtures the head checks need live here and shadow
# pr_support's names in this module.
# What `land` reads of the PR before merging; headRefOid is the head the checks judge.
LAND_FIELDS = "isDraft,mergeable,headRefName,headRefOid,closingIssuesReferences,statusCheckRollup,title,url,state"
# The PR's head in the fake-only land fixtures: the commit pr_support's harness reviews are of, so by default the
# latest harness review is at the head.
HEAD_SHA = harness_review(1, at(1))[0]["commit_id"]


def land_view(**over):
    """pr_support's land view with the head's sha: HEAD_SHA unless `over` names another."""
    return pr_support.land_view(**dict({"headRefOid": HEAD_SHA}, **over))


def compare_calls(head, behind):
    """fakegh entries for `gh api repos/o/r/compare/main...<head>`: `behind` commits of main the head lacks. Only the
    head's sha is answered (asked by the branch name, the fake has no answer), through `--jq .behind_by` or as the
    whole comparison."""
    endpoint = f"repos/o/r/compare/main...{head}"
    return [{"contains": ["api", endpoint, "--jq"], "stdout": f"{behind}\n"},
            {"contains": ["api", endpoint], "stdout_json": {"behind_by": behind, "ahead_by": 1,
                                                            "status": "diverged" if behind else "ahead"}}]


def gate_calls(reviews=(), commits=(), pull=PULL, head=HEAD_SHA, behind=0):
    """pr_support's gate reads (the reviews, their inline comments, the PR's commits), after the compare of main with
    `head` and the authenticated account (the one the harness reviews are by, should the land judge reviews by it as
    `harness-pr gate` does)."""
    return compare_calls(head, behind) + [
        {"match": ["api", "user"], "contains": ["--jq"], "stdout": "rjeskuri\n"},
        {"match": ["api", "user"], "stdout_json": {"login": "rjeskuri", "type": "User"}},
    ] + pr_support.gate_calls(reviews, commits, pull)


def compare_failure(head=HEAD_SHA):
    """The compare of main with `head` failing; placed first in a scenario so it wins."""
    return gate_failure({"contains": ["api", f"repos/o/r/compare/main...{head}"]})


def review_of(sha, *inline, when=None):
    """The latest harness review (review, its inline comments), of commit `sha`, at(10) unless `when`, with these
    inline findings; none gives `Harness review: no findings.`"""
    review, comments = harness_review(101, when or at(10), inline=inline)
    return dict(review, commit_id=sha), comments


class BranchHistory:
    """Real history on the ticket branch, for the land checks that read git in the main checkout (a WorktreeTest
    mixin): the branch pushed to the bare origin; origin's main moved on from a second clone, as other PRs landing
    move it; main merged into the branch in the ticket worktree (`git merge origin/main` after a fetch, as a session
    updating its branch does), cleanly or not; and the PR's commits as GitHub's commits endpoint lists them."""

    def branch_head(self):
        return self.git(self.wt, "rev-parse", "HEAD")

    def push_branch(self):
        """Push the worktree's HEAD to origin's ticket branch (forced: a reset rewrites it); the head's sha."""
        self.git(self.wt, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{BRANCH}")
        self.git(self.wt, "fetch", "-q", "origin")
        return self.branch_head()

    def elsewhere(self):
        """The second clone of origin, on main and up to date."""
        clone = os.path.join(self.tmp, "elsewhere")
        if not os.path.isdir(clone):
            subprocess.run([GIT, "clone", "-q", self.origin, clone], env=self.env(), check=True, capture_output=True)
        self.git(clone, "switch", "-q", "main")
        self.git(clone, "pull", "-q", "--ff-only")
        return clone

    def advance_main(self, n=1):
        """`n` new commits on origin's main, the k-th move adding elsewhere/change_<k>.py (`CHANGE = <k>`), fetched
        into the shared repository (the worktree's and the main checkout's origin/main); origin/main's new sha."""
        self.moves = getattr(self, "moves", 0)
        for _ in range(n):
            self.moves += 1
            self.main_commit({f"elsewhere/change_{self.moves}.py": f"CHANGE = {self.moves}\n"},
                             f"Another PR lands ({self.moves})")
        return self.git(self.wt, "rev-parse", "origin/main")

    def main_commit(self, files, message):
        """One commit of `files` (path: text) on origin's main, fetched into the shared repository; its sha."""
        clone = self.elsewhere()
        for path, text in files.items():
            write(os.path.join(clone, path), text)
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", message)
        self.git(clone, "push", "-q", "origin", "main")
        self.git(self.wt, "fetch", "-q", "origin")
        return self.git(self.wt, "rev-parse", "origin/main")

    def merge_main(self):
        """Merge origin/main into the ticket branch, changing nothing else; pushed; the merge's sha."""
        self.git(self.wt, "fetch", "-q", "origin")
        self.git(self.wt, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        return self.push_branch()

    def evil_merge(self, path="widget.py", text="def frob():\n    return 99  # never reviewed\n"):
        """Merge origin/main with `path` written as `text` in the merge commit (a change of the branch's own folded
        into it); pushed; its sha."""
        self.git(self.wt, "fetch", "-q", "origin")
        self.git(self.wt, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
        write(os.path.join(self.wt, path), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "--no-edit")
        return self.push_branch()

    def conflict_merge(self, resolution):
        """Merge origin/main, which conflicts, resolved by hand as `resolution` (path: text); pushed; its sha."""
        self.git(self.wt, "fetch", "-q", "origin")
        merge = subprocess.run([GIT, "-C", self.wt, "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff",
                                "--no-edit", "origin/main"], env=self.env(), capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0, "the fixture should conflict with main")
        for path, text in resolution.items():
            write(os.path.join(self.wt, path), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "--no-edit")
        return self.push_branch()

    def rebase_onto_main(self):
        """The branch rebased onto origin/main: the same changes, new commits on main's tip; pushed; the head."""
        self.git(self.wt, "fetch", "-q", "origin")
        self.git(self.wt, "rebase", "-q", "origin/main")
        return self.push_branch()

    def merge_into_main(self, sha):
        """The branch as origin/main with `sha` merged into it: main the first parent, `sha` the second; pushed."""
        self.git(self.wt, "fetch", "-q", "origin")
        self.git(self.wt, "reset", "-q", "--hard", "origin/main")
        self.git(self.wt, "merge", "-q", "--no-ff", "--no-edit", sha)
        return self.push_branch()

    def revert_head(self):
        """`git revert` of the branch's head commit; pushed; the revert's sha."""
        self.git(self.wt, "revert", "--no-edit", "HEAD")
        return self.push_branch()

    def tree(self, sha):
        return self.git(self.wt, "rev-parse", f"{sha}^{{tree}}")

    def merged_tree(self, sha):
        """The tree of a clean merge of origin/main into `sha`, as git merge-tree makes it."""
        return self.git(self.wt, "merge-tree", "--write-tree", sha, "origin/main").splitlines()[0]

    def side_merge(self):
        """Merge a branch that is not main (a commit origin/main never had) into the ticket branch; pushed; its sha."""
        clone = self.elsewhere()
        self.git(clone, "switch", "-q", "-C", "side")
        write(os.path.join(clone, "side.py"), "SIDE = 1\n")
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "A side branch, never on main")
        self.git(clone, "push", "-q", "-f", "origin", "side")
        self.git(clone, "switch", "-q", "main")
        self.git(self.wt, "fetch", "-q", "origin")
        self.git(self.wt, "merge", "-q", "--no-ff", "--no-edit", "origin/side")
        return self.push_branch()

    def plain_commit(self, text="def frob():\n    return 3\n", path="widget.py"):
        """An ordinary (one-parent) commit on the ticket branch writing `path` as `text`; pushed; its sha."""
        write(os.path.join(self.wt, path), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Not reviewed")
        return self.push_branch()

    def reset_to(self, sha):
        self.git(self.wt, "reset", "-q", "--hard", sha)
        return self.push_branch()

    def pr_commits(self, reviewed):
        """The PR's commits as GitHub lists them (origin/main..head, oldest first, with their parents): the reviewed
        commit and its ancestors dated at(9), before the review at(10); every later one at(11), after it."""
        out = self.git(self.wt, "rev-list", "--reverse", "--parents", "origin/main..HEAD")
        before = set(self.git(self.wt, "rev-list", reviewed).split())
        commits = []
        for line in out.splitlines():
            sha, *parents = line.split()
            when = at(9) if sha in before else at(11)
            commits.append({"sha": sha, "parents": [{"sha": p} for p in parents],
                            "commit": {"message": f"Commit {sha[:7]}", "author": {"name": "Test", "date": when},
                                       "committer": {"name": "Test", "date": when}}})
        return commits


class Land(WorktreeTest):
    def land_calls(self, views=None, issue_state="OPEN", comments=(), reviews=(), commits=None, head=HEAD_SHA,
                   behind=0):
        """The land conversation: the review gate's reads first (no harness review by default, so the gate
        passes; `head` and `behind` the compare of main with the head), then the land; the last entries answer the
        PR's body and files, the ticket's other fields and a comment posted straight through gh, so the land may
        read and post the Resolution either way."""
        views = views or [land_view(headRefOid=head)]
        commits = [pr_commit("a", at(9))] if commits is None else commits
        issue = {"state": issue_state, "labels": [{"name": "task"}, {"name": "status:in-review"}],
                 "comments": list(comments)}
        return gate_calls(reviews, commits, head=head, behind=behind) + [
                {"match": ["pr", "view", "34", "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": SHA}}},
                {"match": ["pr", "view", "34", "--json", LAND_FIELDS],
                 "responses": [{"stdout_json": v} for v in views]},
                REPO_VIEW, {"match": ["pr", "merge", "34"], "stdout": ""},
                {"match": ["issue", "view", "12", "--json", "state,labels,comments"], "stdout_json": issue},
                {"match": ["issue", "edit", "12"], "stdout": ""},
                {"match": ["pr", "view", "34"], "stdout_json": pr_data(FIXTURE_A)},
                *files_calls(34, FIXTURE_A["files"]),
                {"match": ["issue", "view", "12"],
                 "stdout_json": dict(issue, number=12, title="Add the widget", body=TICKET_12_BODY,
                                     url="https://github.com/o/r/issues/12")},
                {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
                {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": []}]

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    def branch_exists(self):
        return subprocess.run([GIT, "-C", self.main, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
                              capture_output=True, env=self.env()).returncode == 0

    def test_land_merges_then_cleans_up_in_order(self):
        self.scenario(self.land_calls(views=[land_view(mergeable="UNKNOWN"), land_view()]))
        p = self.ok("land", "34", "--yes", cwd=self.wt)
        m = self.main
        steps = [self.position("gh pr merge 34 -R o/r --squash --delete-branch"),
                 self.position(f"git -C {m} worktree remove {self.wt}"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"git -C {m} fetch --prune"),
                 self.position(f"git -C {m} pull --ff-only"),
                 self.position("gh issue view 12 --json state,labels,comments"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position("gh issue edit 12 --remove-label status:in-review"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(sum(1 for a in self.gh_argv() if a == ["pr", "view", "34", "--json", LAND_FIELDS]), 2)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        lines = p.stdout.splitlines()
        self.assertEqual(len(lines), 4, p.stdout)
        self.assertEqual(lines[0], f'Merged PR #34 "Add the widget (#12)" into main as {SHA[:12]}.')
        self.assertIn(f"removed {self.wt}", lines[1])
        self.assertIn("fast-forwarded main", lines[2])
        self.assertEqual(lines[3], "Ticket: #12 closed, status labels removed, commented, "
                                   "epic check: #3: 1 of 2 tasks closed; left open.")

    def test_land_stops_after_the_merge_on_a_dirty_worktree(self):
        self.scenario(self.land_calls())
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        p = self.pr("land", "34", cwd=self.main)
        self.assertOneLineError(p, "is dirty", "cleanup stopped")
        self.assertIn("harness-pr land 34 --discard", p.stdout)
        self.assertIn(f"git -C {self.main} worktree remove --force {self.wt}", p.stdout)
        self.assertIn(f"git -C {self.main} branch -D {BRANCH}", p.stdout)
        self.position("gh pr merge 34")
        order = self.order()
        for gone in ("worktree remove", "branch -D", "fetch --prune", "pull --ff-only"):
            self.assertFalse(any(gone in line for line in order), gone)
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())

    def test_land_discard_forces_the_removal(self):
        self.scenario(self.land_calls(issue_state="CLOSED"))
        write(os.path.join(self.wt, "widget.py"), "changed\n")
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        p = self.ok("land", "34", "--discard", cwd=self.main)
        self.position(f"git -C {self.main} worktree remove --force {self.wt}")
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        board = self.stub_calls("harness-board")
        self.assertFalse(any(a[0] == "close" for a in board))        # GitHub closed it on the merge
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        self.assertIn("closed by GitHub", p.stdout)

    def test_land_without_a_closing_reference_warns_and_merges(self):
        self.scenario(self.land_calls(views=[land_view(closingIssuesReferences=[])]))
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn("closes no issue", p.stderr)
        self.position("gh pr merge 34")
        self.assertFalse(any(a[:2] == ["issue", "view"] for a in self.gh_argv()))
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertEqual(p.stdout.splitlines()[-1], "Ticket: none linked.")

    def test_land_resumes_after_a_dirty_stop_without_a_second_merge(self):
        self.scenario(self.land_calls())
        write(os.path.join(self.wt, "notes.txt"), "unsaved\n")
        self.assertOneLineError(self.pr("land", "34", cwd=self.main), "cleanup stopped")
        self.position("gh pr merge 34")
        # Now GitHub reports the PR merged; its checks and mergeability no longer matter.
        merged = land_view(state="MERGED", mergeable="UNKNOWN", statusCheckRollup=PENDING)
        self.scenario(self.land_calls(views=[merged]))
        p = self.ok("land", "34", "--discard", cwd=self.main)
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "already merged, finishing cleanup")
        self.assertEqual(lines[1], f'PR #34 "Add the widget (#12)" was already merged into main as {SHA[:12]}.')
        self.assertEqual(len(lines), 5, p.stdout)
        m = self.main
        steps = [self.position(f"git -C {m} worktree remove --force {self.wt}"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"git -C {m} fetch --prune"),
                 self.position(f"git -C {m} pull --ff-only"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position("gh issue edit 12 --remove-label status:in-review"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())

    def test_land_finishes_when_the_worktree_folder_was_removed_by_hand(self):
        self.scenario(self.land_calls())
        shutil.rmtree(self.wt)
        self.assertIn(f"worktree {self.wt}", self.git(self.main, "worktree", "list", "--porcelain"))   # still listed
        p = self.ok("land", "34", cwd=self.main)
        m = self.main
        steps = [self.position("gh pr merge 34"),
                 self.position(f"git -C {m} worktree prune"),
                 self.position(f"git -C {m} worktree list --porcelain"),
                 self.position(f"git -C {m} branch -D {BRANCH}"),
                 self.position(f"harness-board close 12 --repo {m}"),
                 self.position(f"harness-board comment 12 --body Landed in {SHA} --repo {m}"),
                 self.position(f"harness-board epic-check 3 --repo {m}")]
        self.assertEqual(steps, sorted(steps))
        self.assertFalse(any("worktree remove" in line for line in self.order()))
        self.assertFalse(self.branch_exists())
        self.assertIn(f"Worktree: no worktree for {BRANCH}; branch {BRANCH} deleted.", p.stdout)
        self.assertIn("Ticket: #12 closed, status labels removed, commented", p.stdout)

    def test_land_goes_on_to_the_ticket_when_a_locked_worktree_is_gone(self):
        self.scenario(self.land_calls())
        self.git(self.main, "worktree", "lock", self.wt)       # prune keeps a locked entry
        shutil.rmtree(self.wt)
        p = self.pr("land", "34", cwd=self.main)
        self.assertOneLineError(p, "PR #34 is merged, but", f"git branch -D {BRANCH} failed")
        self.assertIn(f"Worktree: {self.wt} was already gone; branch {BRANCH} kept.", p.stdout)
        self.assertFalse(any("worktree remove" in line for line in self.order()))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))
        self.assertIn("Ticket: #12 closed, status labels removed, commented", p.stdout)

    def test_land_asks_github_for_the_epic_without_a_cached_parent(self):
        os.unlink(self.state_path("tickets", "12.json"))
        parent = {"match": ["api", "repos/o/r/issues/12/parent"], "stdout_json": {"number": 3, "title": "Widgets"}}
        self.scenario([parent] + self.land_calls())
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn(["api", "repos/o/r/issues/12/parent"], self.gh_argv())
        self.assertIn(["epic-check", "3", "--repo", self.main], self.stub_calls("harness-board"))
        self.assertIn("epic check: #3: 1 of 2 tasks closed; left open.", p.stdout)

        # A cache without a parent asks too; a 404 means no epic, and the land still succeeds.
        write(self.state_path("tickets", "12.json"), json.dumps({"number": 12, "parent": None}))
        none = {"match": ["api", "repos/o/r/issues/12/parent"], "stderr": "gh: Not Found (HTTP 404)\n", "exit": 1}
        self.scenario([none] + self.land_calls(views=[land_view(state="MERGED")], issue_state="CLOSED"))
        p = self.ok("land", "34", cwd=self.main)
        self.assertIn(["api", "repos/o/r/issues/12/parent"], self.gh_argv())
        self.assertFalse(any(a[0] == "epic-check" for a in self.stub_calls("harness-board")))
        self.assertNotIn("epic check", p.stdout)

    def test_land_on_a_merged_pr_does_not_repeat_the_landed_comment(self):
        landed = {"author": {"login": "rjeskuri"}, "body": f"Landed in {SHA}", "createdAt": "2026-10-13T10:00:00Z"}
        self.scenario(self.land_calls(views=[land_view(state="MERGED")], issue_state="CLOSED", comments=[landed]))
        p = self.ok("land", "34", cwd=self.main)
        self.assertEqual(p.stdout.splitlines()[0], "already merged, finishing cleanup")
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))
        self.assertEqual(self.stub_calls("harness-board"), [["epic-check", "3", "--repo", self.main]])
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        self.assertIn("landed comment already there", p.stdout)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())


class LandDirtyMain(WorktreeTest):
    """The main checkout has uncommitted changes when `land` fast-forwards it. The squash merge is a real commit
    on origin/main (pushed from a second clone, as GitHub's merge would be), and gh reports its sha, so the land's
    fetch brings it and `git pull --ff-only` has something to apply. A dirty path whose bytes equal the landed
    commit's is restored and main fast-forwards; any other dirty path, or an untracked file in the way, refuses
    with the path named and leaves the checkout as it was."""

    land_calls = Land.land_calls

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        write(os.path.join(self.main, WORKLOG), WORKLOG_BEFORE)
        write(os.path.join(self.main, "data", "rates.tsv"), "client\trate\nacme\t120\n")
        write(os.path.join(self.main, "docs", "notes.md"), "# Notes\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "Track the worklog")
        self.git(self.main, "push", "-q", "origin", "main")

    def landed(self, files):
        """Push the squash merge of PR #34 to origin/main, changing `files` (path: content, None deletes it);
        return its sha."""
        clone = os.path.join(self.tmp, "github")
        if not os.path.isdir(clone):
            subprocess.run([GIT, "clone", "-q", self.origin, clone], env=self.env(), check=True, capture_output=True)
        self.git(clone, "pull", "-q", "--ff-only")
        for path, text in files.items():
            if text is None:
                os.unlink(os.path.join(clone, path))
            else:
                write(os.path.join(clone, path), text)
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "Add the widget (#34)")
        self.git(clone, "push", "-q", "origin", "main")
        return self.git(clone, "rev-parse", "HEAD")

    def land(self, sha):
        merge = {"match": ["pr", "view", "34", "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}}
        self.scenario([merge] + self.land_calls())
        return self.pr("land", "34", cwd=self.main)

    def head(self):
        return self.git(self.main, "rev-parse", "HEAD")

    def status(self):
        """{path: the two-letter porcelain code} for the main checkout, untracked files included."""
        out = subprocess.run([GIT, "-C", self.main, "status", "--porcelain=v1", "--untracked-files=all"],
                             env=self.env(), capture_output=True, text=True, check=True).stdout
        return {line[3:]: line[:2] for line in out.splitlines() if line.strip()}

    def read(self, path):
        with open(os.path.join(self.main, path), "rb") as fh:
            return fh.read().decode("utf-8")

    def assertRefused(self, p, sha, before, named, untouched):
        """A refusal: exit 1 with the path(s) named, main's HEAD where it was, nothing reported cleaned, and each
        file in `untouched` (path: content) still holding the local content. The refusal is one of the land's
        problems, reported at the end as today: the ticket is still closed, unlabelled and commented, and the
        summary still prints with main not pulled."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for path in named:
            self.assertIn(path, p.stderr)
        self.assertEqual(self.head(), before, "main moved although the land refused")
        self.assertNotIn("cleaned", p.stdout)
        self.assertNotIn(IDENTICAL, p.stdout)
        for path, text in untouched.items():
            self.assertEqual(self.read(path), text, path)
        board = self.stub_calls("harness-board")
        self.assertIn(["close", "12", "--repo", self.main], board)
        self.assertIn(["comment", "12", "--body", f"Landed in {sha}", "--repo", self.main], board)
        self.assertIn(["issue", "edit", "12", "--remove-label", "status:in-review"], self.gh_argv())
        lines = p.stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("Merged PR #34"), p.stdout)
        self.assertTrue(any(line.startswith("Main checkout:") and "main not pulled" in line for line in lines),
                        p.stdout)
        self.assertTrue(any(line.startswith("Ticket: #12 closed") for line in lines), p.stdout)

    def cleaned_text(self, p):
        """The stdout lines that report a restored path, joined; each must say `cleaned`."""
        cleaned = [line for line in p.stdout.splitlines() if IDENTICAL in line]
        self.assertTrue(cleaned, p.stdout)
        self.assertTrue(all("cleaned" in line for line in cleaned), cleaned)
        return "\n".join(cleaned)

    # ------------------------------------------------------------- broad
    def test_identical_dirty_paths_are_restored_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        before = self.head()
        self.assertNotEqual(before, sha)
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)          # the owner's edit, as the chore PR has it
        self.assertEqual(self.status(), {WORKLOG: " M"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {})
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        self.assertEqual(self.read("widget.py"), "def frob():\n    return 2\n")
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertIn("fast-forwarded main", p.stdout)

    def test_a_differing_dirty_path_refuses_and_restores_nothing(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = WORKLOG_LANDED + "2026-10-14,harness,0.25,not in the PR\n"     # one row more than the landed file
        write(os.path.join(self.main, WORKLOG), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: mine})
        self.assertEqual(self.status(), {WORKLOG: " M"})

    def test_an_untracked_file_in_the_way_refuses(self):
        sha = self.landed({"docs/widget.md": "# Widget\nIt frobs twice.\n", WORKLOG: WORKLOG_LANDED})
        before = self.head()
        write(os.path.join(self.main, "docs", "widget.md"), "# My draft\n")
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["docs/widget.md"],
                           {"docs/widget.md": "# My draft\n", WORKLOG: WORKLOG_BEFORE})
        self.assertEqual(self.status(), {"docs/widget.md": "??"})

    # ---------------------------------------------------------- specific
    def test_identical_paths_staged_or_not_are_each_named_and_restored(self):
        rates = "client\trate\nacme\t135\nglobex\t90\n"
        notes = "# Notes\n\nLanding restores identical files.\n"
        sha = self.landed({"data/rates.tsv": rates, "docs/notes.md": notes})
        write(os.path.join(self.main, "data", "rates.tsv"), rates)
        write(os.path.join(self.main, "docs", "notes.md"), notes)
        self.git(self.main, "add", "docs/notes.md")                          # one staged, one only modified
        self.assertEqual(self.status(), {"data/rates.tsv": " M", "docs/notes.md": "M "})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {})
        self.assertEqual((self.read("data/rates.tsv"), self.read("docs/notes.md")), (rates, notes))
        text = self.cleaned_text(p)
        for path in ("data/rates.tsv", "docs/notes.md"):
            self.assertIn(path, text)
        self.assertNotIn(WORKLOG, text)                                       # clean before the land: not named

    def test_one_differing_path_among_identical_ones_restores_nothing(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "README.md": "# proj\n\nThe widget frobs.\n"})
        before = self.head()
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "README.md"), "# proj\n\nMy own intro.\n")   # differs
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["README.md"],
                           {WORKLOG: WORKLOG_LANDED, "README.md": "# proj\n\nMy own intro.\n"})
        self.assertEqual(self.status(), {WORKLOG: " M", "README.md": " M"})  # the identical one not restored either
        problem = [line for line in p.stderr.splitlines() if "README.md" in line]
        self.assertTrue(problem, p.stderr)
        for line in problem:
            self.assertNotIn(WORKLOG, line)                                   # only the differing path is reported

    def refused_case(self, local, staged=None):
        """Land with the worklog dirty as `local` bytes (and `staged` in the index first, when given) against a
        landed commit that writes WORKLOG_LANDED; assert a refusal that keeps every byte, index and tree."""
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        path = os.path.join(self.main, WORKLOG)
        if staged is not None:
            with open(path, "wb") as fh:
                fh.write(staged.encode())
            self.git(self.main, "add", WORKLOG)
        with open(path, "wb") as fh:
            fh.write(local.encode())
        code = self.status()[WORKLOG]
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: local})
        self.assertEqual(self.status(), {WORKLOG: code})                      # index and tree as they were
        return code

    def test_reordered_rows_refuse(self):
        rows = WORKLOG_LANDED.splitlines(keepends=True)
        mine = "".join(rows[:-2] + [rows[-1], rows[-2]])
        self.assertEqual(sorted(mine.splitlines()), sorted(WORKLOG_LANDED.splitlines()))
        self.assertNotEqual(mine, WORKLOG_LANDED)
        self.refused_case(mine)

    def test_crlf_line_endings_refuse(self):
        self.refused_case(WORKLOG_LANDED.replace("\n", "\r\n"))

    def test_a_missing_final_newline_refuses(self):
        self.refused_case(WORKLOG_LANDED.rstrip("\n"))

    def test_a_staged_third_version_under_identical_tree_bytes_refuses(self):
        # The index holds a version that is neither HEAD's nor the landed one; the tree matches the landed bytes.
        # Restoring the path would drop the staged version, and plain git refuses this state, so the land must too.
        staged = WORKLOG_BEFORE + "2026-10-13,harness,4.0,staged only\n"
        self.assertNotIn(staged, (WORKLOG_BEFORE, WORKLOG_LANDED))
        self.assertEqual(self.refused_case(WORKLOG_LANDED, staged=staged), "MM")
        index = subprocess.run([GIT, "-C", self.main, "show", f":{WORKLOG}"], env=self.env(),
                               capture_output=True, check=True).stdout.decode("utf-8")
        self.assertEqual(index, staged, "the staged version was dropped from the index")

    def test_staged_landed_content_with_other_bytes_in_the_tree_keeps_the_edit_and_fast_forwards(self):
        # The index already holds the landed bytes, so git fast-forwards today and keeps the tree's edit; the land
        # must not get stricter than that, nor restore (and so drop) the unstaged row.
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        mine = WORKLOG_LANDED + "2026-10-14,harness,3.0,unstaged on top\n"
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)
        self.git(self.main, "add", WORKLOG)
        write(os.path.join(self.main, WORKLOG), mine)
        self.assertEqual(self.status(), {WORKLOG: "MM"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.read(WORKLOG), mine)
        self.assertEqual(self.status(), {WORKLOG: " M"})
        self.assertNotIn("cleaned", p.stdout)
        self.assertNotIn(IDENTICAL, p.stdout)

    def test_a_dirty_file_the_merge_deletes_refuses(self):
        sha = self.landed({"docs/notes.md": None, WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = "# Notes\n\nStill using these.\n"
        write(os.path.join(self.main, "docs", "notes.md"), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, ["docs/notes.md"], {"docs/notes.md": mine, WORKLOG: WORKLOG_BEFORE})
        self.assertEqual(self.status(), {"docs/notes.md": " M"})

    def test_a_same_length_dirty_path_with_other_bytes_refuses(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED})
        before = self.head()
        mine = WORKLOG_LANDED.replace("1.75", "1.50")
        self.assertEqual(len(mine.encode()), len(WORKLOG_LANDED.encode()))
        self.assertNotEqual(mine, WORKLOG_LANDED)
        write(os.path.join(self.main, WORKLOG), mine)
        p = self.land(sha)
        self.assertRefused(p, sha, before, [WORKLOG], {WORKLOG: mine})
        self.assertEqual(self.status(), {WORKLOG: " M"})

    def test_an_untracked_file_the_merge_does_not_touch_stays_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "scratch", "todo.txt"), "- ask about the rates\n")
        self.assertEqual(self.status(), {WORKLOG: " M", "scratch/todo.txt": "??"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {"scratch/todo.txt": "??"})
        self.assertEqual(self.read("scratch/todo.txt"), "- ask about the rates\n")
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        text = self.cleaned_text(p)
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertNotIn("scratch/todo.txt", text)

    def test_a_dirty_path_the_merge_does_not_touch_keeps_its_edit_and_main_fast_forwards(self):
        sha = self.landed({WORKLOG: WORKLOG_LANDED, "widget.py": "def frob():\n    return 2\n"})
        notes = "# Notes\n\nA local thought, not in the PR.\n"
        write(os.path.join(self.main, WORKLOG), WORKLOG_LANDED)              # identical
        write(os.path.join(self.main, "docs", "notes.md"), notes)            # dirty; the merge leaves it alone
        self.assertEqual(self.status(), {WORKLOG: " M", "docs/notes.md": " M"})
        p = self.land(sha)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.status(), {"docs/notes.md": " M"})
        self.assertEqual(self.read("docs/notes.md"), notes)
        self.assertEqual(self.read(WORKLOG), WORKLOG_LANDED)
        text = self.cleaned_text(p)
        self.assertIn(f"cleaned {WORKLOG} {IDENTICAL}", p.stdout)
        self.assertNotIn("docs/notes.md", text)


class LandResolution(Land):
    """`land` leaves one `**Resolution**` comment on the ticket, posted through `harness-board comment` before the
    `Landed in` comment. These run the real harness-board (no stubs), so every comment reaches the fake gh's log."""

    def resolution_calls(self, fx, state="OPEN", issue_state="OPEN", comments=(), view_files=None, after=None,
                         no_sha=False):
        """The land conversation for the fixture's PR. `view_files`: the files `gh pr view --json` holds (all of
        the fixture's by default; GitHub stops at 100), while the files endpoint always lists them all; `after`:
        the answer to the PR read after the merge (`gh pr view P --json body,...`), in place of the PR's data;
        `no_sha`: GitHub reports no merge commit (True) or the read fails ("fail"), so the land knows no sha and
        comments `Landed in PR #N`."""
        pr, sha = str(fx["pr"]), fx["sha"]
        if no_sha:
            fx, sha = dict(fx, sha=""), ""
        view = pr_data(dict(fx, files=fx["files"] if view_files is None else view_files))
        url = f"https://github.com/o/r/pull/{pr}"
        issue = {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12",
                 "body": TICKET_12_BODY, "state": issue_state,
                 "labels": [{"name": "task"}, {"name": "status:in-review"}], "comments": list(comments)}
        rest = [{"body": c["body"], "user": {"login": c["author"]["login"]}, "created_at": c["createdAt"]}
                for c in comments]
        # the review gate's reads: no harness review, so the gate lets the land through
        gate = gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}")
        return gate + [
                {"match": ["pr", "view", pr, "--json", "mergeCommit"],
                 **({"stderr": "GraphQL: timeout\n", "exit": 1} if no_sha == "fail"
                    else {"stdout_json": {"mergeCommit": {"oid": sha} if sha else None}})},
                {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
                 "stdout_json": land_view(url=url, state=state)},
                REPO_VIEW,
                {"match": ["pr", "merge", pr], "stdout": ""},
                {"match": ["pr", "view", pr], **(after or {"stdout_json": view})},
                *files_calls(pr, fx["files"]),
                {"match": ["issue", "view", "12"], "stdout_json": issue},
                {"match": ["issue", "edit", "12"], "stdout": ""},
                {"match": ["issue", "close", "12"], "stdout": ""},
                {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-9\n"},
                {"contains": ["repos/o/r/issues/12/comments", "POST"], "stdout_json": {"id": 9}},
                {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": rest},
                {"match": ["issue", "view", "3"],
                 "stdout_json": {"state": "OPEN", "subIssuesSummary": {"total": 2, "completed": 1}}}]

    def land(self, fx, **scenario):
        self.scenario(self.resolution_calls(fx, **scenario))
        return self.ok("land", str(fx["pr"]), "--yes", cwd=self.main, stubs=False)

    def posted(self, number=None):
        """(index in the gh log, issue number, body) of every comment posted: `gh issue comment N`, `gh issue close
        N --comment` and `gh api repos/o/r/issues/N/comments` with a body."""
        out = []
        for i, call in enumerate(self.gh_calls()):
            a, body, n = call["argv"], None, None
            if a[:2] in (["issue", "comment"], ["issue", "close"]) and len(a) > 2:
                n = a[2]
                for flag in ("--body", "-b", "--comment", "-c"):
                    if flag in a[:-1]:
                        body = a[a.index(flag) + 1]
                for flag in ("--body-file", "-F"):
                    if flag in a[:-1]:
                        path = a[a.index(flag) + 1]
                        if path == "-":
                            body = call["stdin"]
                        elif os.path.exists(path):
                            with open(path, encoding="utf-8") as fh:
                                body = fh.read()
            elif a[:1] == ["api"]:
                target = next((t for t in a if "/issues/" in t and "/comments" in t), None)
                if target:
                    n = target.split("/issues/")[1].split("/")[0]
                    if "--input" in a and call["stdin"].strip():
                        body = json.loads(call["stdin"]).get("body")
                    for j, tok in enumerate(a[:-1]):
                        if tok in ("-f", "-F", "--field", "--raw-field") and a[j + 1].startswith("body="):
                            body = a[j + 1][len("body="):]
            if body is not None and (number is None or n == str(number)):
                out.append((i, n, body))
        return out

    def resolutions(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith(RESOLUTION)]

    def landed(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith("Landed in")]

    def as_comments(self, bodies):
        return [{"author": {"login": "rjeskuri"}, "body": b, "createdAt": f"2026-10-13T1{k}:00:00Z"}
                for k, b in enumerate(bodies)]

    def first(self, lines, pred, what):
        for i, line in enumerate(lines):
            if pred(line):
                return i
        self.fail(f"{what} missing from the Resolution:\n" + "\n".join(lines))

    def assertFrame(self, body, fx):
        """The heading, the harness marker last, the PR link and the full squash sha; the body's lines."""
        self.assertTrue(body.lstrip().startswith(RESOLUTION), body)
        lines = body.rstrip().splitlines()
        self.assertEqual(lines[-1].strip(), HARNESS_MARK, body)
        self.assertIn(f"https://github.com/o/r/pull/{fx['pr']}", body)
        self.assertIn(fx["sha"], body, "the full squash sha missing from the Resolution")
        return lines

    def file_positions(self, lines, fx):
        """The index of each changed file's list line: exactly one of its own per file."""
        file_lines = {}
        for i, line in enumerate(lines):
            m = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if m:
                text = m.group(1).replace("`", "").strip()
                for path in fx["files"]:
                    if text == path or text.startswith(path + " "):
                        file_lines.setdefault(path, []).append(i)
        for path in fx["files"]:
            self.assertEqual(len(file_lines.get(path, [])), 1,
                             f"changed file {path} wants exactly one list line of its own:\n" + "\n".join(lines))
        return [i for found in file_lines.values() for i in found]

    def assertRedKind(self, lines, fx):
        """The red-run kind on exactly one line, and that line is about the red run."""
        kind = re.compile(r"\b" + fx["red_kind"] + r"\b")
        kind_lines = [line for line in lines if kind.search(line)]
        self.assertEqual(len(kind_lines), 1, f"want one red-run kind line, got {kind_lines}")
        self.assertRegex(kind_lines[0], r"(?i)\bred\b", "the kind belongs on a line about the red run")

    def assertVerifyResult(self, lines, fx):
        """The last line of the verify fence: once, outside any code fence; the earlier lines left out."""
        verify = [line for line in fx["verify"] if line.strip()]
        fenced, where = False, []
        for line in lines:
            if line.strip().startswith("```"):
                fenced = not fenced
            elif verify[-1] in line:
                where.append(fenced)
        self.assertEqual(where, [False],
                         f"the verify result line {verify[-1]!r} wants one unfenced line:\n" + "\n".join(lines))
        for text in verify[:-1]:
            self.assertNotIn(text, "\n".join(lines), f"the Resolution copies the earlier verify line {text!r}")

    def assertResolution(self, body, fx):
        """The Resolution carries the PR's summary, every changed file, the checklist as landed, the red-run
        kind and the verify result from the PR body, the PR link and the squash sha, and the harness marker."""
        lines = self.assertFrame(body, fx)

        # the summary, then the changed files (one list line each), then the checklist as landed
        at_summary = self.first(lines, lambda line: fx["summary"] in line, "the PR summary")
        at_files = self.file_positions(lines, fx)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in fx["dod"]]
        self.assertLess(at_summary, min(at_files), "the summary comes before the files")
        self.assertLess(max(at_files), min(at_dod), "the files come before the checklist")
        self.assertEqual(at_dod, sorted(at_dod), "the checklist keeps its order")

        self.assertRedKind(lines, fx)
        self.assertVerifyResult(lines, fx)

        # what the Resolution leaves out of the PR body: the red tail, the tests section, the review, the
        # risks and the closing line
        left_out = fx["red_tail"] + [
            fx["test_file"], "::test_main", "Test files run", fx["review"], "## Review", "## Risks", "None noted.",
            "Closes #12", "## Tests"]
        for text in left_out:
            self.assertNotIn(text, body, f"the Resolution copies {text!r} from the PR body")

    # ------------------------------------------------------------------ fresh lands
    def test_land_posts_one_resolution_after_the_merge(self):
        self.land(FIXTURE_A)
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        index, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_A)
        for other in FIXTURE_B["files"] + [FIXTURE_B["summary"], FIXTURE_B["sha"][:12]]:
            self.assertNotIn(other, body)
        calls = self.gh_calls()
        merge = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["pr", "merge"])
        self.assertGreater(index, merge, "the Resolution is posted after the merge")
        self.assertEqual(calls[index]["argv"][:3], ["issue", "comment", "12"],
                         "posted through harness-board comment")
        # the Landed comment is unchanged: one, `Landed in <sha>`, as harness-board posts it, after the Resolution
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in, so a resume never misses it")

    def test_land_resolution_follows_the_pr_it_lands(self):
        """Another PR, another sha, more files, an unchecked box, an import red run; GitHub closed the ticket."""
        self.land(FIXTURE_B, issue_state="CLOSED")
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        _, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_B)
        for other in FIXTURE_A["files"][2:] + [FIXTURE_A["summary"], FIXTURE_A["sha"][:12], "OK (skipped=1)"]:
            self.assertNotIn(other, body)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.landed(12)],
                         [f"Landed in {FIXTURE_B['sha']}"])

    # ------------------------------------------------------------------ resumed lands
    def land_then_resume(self, fx, keep):
        """Land once, then land again on the merged PR with the ticket showing the first run's comments that
        `keep` selects. Returns (Resolutions of the first run, Resolutions and Landed of the second)."""
        self.land(fx)
        first = self.resolutions(12)
        bodies = [b for _, _, b in self.posted(12) if keep(b)]
        self.land(fx, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        return first, self.resolutions(), self.landed()

    def test_resumed_land_adds_no_second_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: True)
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "a resumed land must not post a second Resolution")
        self.assertEqual(landed, [], "nor a second Landed comment")

    def test_resumed_land_with_only_the_resolution_posts_the_landed_comment(self):
        first, again, landed = self.land_then_resume(FIXTURE_B, keep=lambda b: b.lstrip().startswith(RESOLUTION))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "the Resolution already there is not repeated")
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                         [f"Landed in {FIXTURE_B['sha']}"])

    def test_resumed_land_after_the_landed_comment_posts_no_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: b.lstrip().startswith("Landed in"))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "once Landed in is on the ticket the land is recorded; no Resolution")
        self.assertEqual(landed, [])

    # ------------------------------------------------------------------ a reopened ticket's second landing (#24)
    REOPENED = "Reopened: the first pass broke the hooks."

    def first_pass(self, fx):
        """Land `fx` on a fresh ticket and return the bodies it posted: its Resolution and `Landed in`."""
        self.land(fx)
        bodies = [b for _, _, b in self.posted(12)]
        self.assertEqual(len(bodies), 2, f"the first pass posts a Resolution and Landed in: {bodies}")
        return bodies

    def old_resolution(self, pr_line, sha):
        """A Resolution in the land's format, with the given `PR:` line and squash sha (None: no such line)."""
        closing = ([pr_line] if pr_line else []) + ([f"Squash commit: {sha}"] if sha else [])
        return "\n".join([RESOLUTION, "", "A first pass that was reopened.", "", "Files changed (1):",
                          "- `old/first_pass.py`", "", *closing, "", HARNESS_MARK])

    def assertSecondLanding(self, fx, old_texts):
        """This land of `fx` posted exactly two comments on #12: a Resolution of `fx` (naming its PR, not an old
        one) and then `Landed in <fx's sha>`."""
        posted = self.posted(12)
        self.assertEqual(len(posted), 2, f"want a Resolution and Landed in for PR #{fx['pr']}, got {posted}")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
        index, _, body = found[0]
        self.assertResolution(body, fx)
        for text in old_texts:
            self.assertNotIn(text, body, "the new Resolution is about this PR only")
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_second_landing_of_a_reopened_ticket_comments_resolution_and_landed_in(self):
        """A ticket landed once (its Resolution and `Landed in <old sha>` on it), reopened, and landed again by
        another PR: this landing gets its own Resolution, naming the new PR, and `Landed in <new sha>`. Several
        pairs, so neither the old nor the new fixture's text can be hard-coded; GitHub has or has not closed it."""
        for old, new, issue_state in ((FIXTURE_A, FIXTURE_B, "CLOSED"), (FIXTURE_B, FIXTURE_A, "OPEN"),
                                      (FIXTURE_C, FIXTURE_A, "CLOSED"), (FIXTURE_A, FIXTURE_C, "OPEN")):
            with self.subTest(old=old["pr"], new=new["pr"]):
                bodies = self.first_pass(old) + [self.REOPENED]
                p = self.land(new, issue_state=issue_state, comments=self.as_comments(bodies))
                self.assertSecondLanding(new, [f"https://github.com/o/r/pull/{old['pr']}", old["sha"],
                                               old["summary"]])
                ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
                self.assertEqual(len(ticket), 1, p.stdout)
                self.assertIn("commented", ticket[0])
                self.assertNotIn("already there", ticket[0])

    def test_second_landing_ignores_every_shape_of_an_earlier_passs_comments(self):
        """Only this landing's own text counts as already landed: `Landed in <this sha>` (a whole token) or
        `Landed in PR #<this PR>`, and a Resolution whose `PR:` line is this PR's URL. An older pass's comments
        in any shape do not, including ones that share a prefix with this sha, PR number or URL. A Resolution
        with no `PR:` line names no PR, so it is not this landing's either. Landing FIXTURE_B (PR #57)."""
        new = FIXTURE_B
        sha = new["sha"]
        old_sha = FIXTURE_A["sha"]
        cases = [
            ("Landed in PR #34 and its Resolution",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", old_sha), "Landed in PR #34"]),
            ("a Resolution with no PR line",
             [self.old_resolution(None, old_sha), f"Landed in {old_sha}"]),
            ("PR #570 and pull/570, which start with this PR's number and URL",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570"]),
            ("a different sha sharing this sha's first 12 characters",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:12] + "0" * 28),
              f"Landed in {sha[:12]}{'0' * 28}"]),
            ("an abbreviated sha that is a prefix of this one",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:7]), f"Landed in {sha[:7]}"]),
            ("a longer hex token that starts with this sha",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha + "abc"), f"Landed in {sha}abc"]),
            ("Landed in PR #570 followed by more text",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570, see notes"]),
            # a Resolution belongs to the PR its `PR:` line names, whatever sha it quotes
            ("another PR's Resolution quoting this sha, and no Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), "Reverted by hand."]),
            ("another PR's Resolution quoting this sha, and its Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), f"Landed in {old_sha}"]),
        ]
        for name, old in cases:
            with self.subTest(name):
                bodies = [old[0], old[1] + "\n\n" + HARNESS_MARK, self.REOPENED]
                self.land(new, issue_state="CLOSED", comments=self.as_comments(bodies))
                self.assertSecondLanding(new, ["A first pass that was reopened.", "old/first_pass.py"])

    def test_resumed_land_posts_only_what_is_missing(self):
        """A reopened ticket's second land, resumed: the ticket carries the first pass's two comments and some of
        this landing's. With this PR's Resolution and no `Landed in <this sha>`, only `Landed in` is posted; with
        both (or `Landed in` alone, which records the land), nothing is; `Landed in PR #<this PR>` counts as this
        landing's Landed comment too. Run with the fixtures both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            self.land(new)                                   # this landing's own comments, as the land posts them
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertEqual((len(res), len(mine)), (1, 1))
            by_pr = f"Landed in PR #{new['pr']}\n\n{HARNESS_MARK}"
            cases = [("this Resolution only", res, [f"Landed in {new['sha']}"]),
                     ("this Resolution and Landed in", res + mine, []),
                     ("this Landed in only", mine, []),
                     ("this Resolution and Landed in PR #", res + [by_pr], [])]
            for name, kept, wanted in cases:
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                    self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "no second merge")
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_counts_this_prs_resolution_by_its_pr_line(self):
        """A Resolution is this landing's when its `PR:` line is this PR's URL, whatever sha it quotes or none:
        then only `Landed in <this sha>` is posted, never a second Resolution. A `Landed in` for this PR with
        trailing text after the whole number (`PR #57, see notes`) records the land; nothing is posted."""
        new, old = FIXTURE_B, FIXTURE_A
        url = f"PR: https://github.com/o/r/pull/{new['pr']}"
        first = self.first_pass(old) + [self.REOPENED]
        cases = [("this PR's Resolution without a Squash commit line",
                  [self.old_resolution(url, None)],
                  [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution quoting another sha",
                  [self.old_resolution(url, FIXTURE_C["sha"])], [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution and Landed in PR #57 with more text",
                  [self.old_resolution(url, new["sha"]), f"Landed in PR #{new['pr']}, see notes\n\n{HARNESS_MARK}"],
                  [])]
        for name, kept, wanted in cases:
            with self.subTest(name):
                self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_land_without_a_merge_sha_on_a_reopened_ticket(self):
        """GitHub reports no merge commit, so the land comments `Landed in PR #<pr>`. A second landing still gets
        its own Resolution (naming this PR) and `Landed in PR #<pr>` despite the first pass's comments and an old
        `Landed in PR #570`; a resume with this PR's Resolution posts only `Landed in PR #<pr>`; with both,
        nothing; never a second Resolution. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            by_pr = f"Landed in PR #{new['pr']}"
            first = self.first_pass(old) + [f"Landed in PR #{new['pr']}0\n\n{HARNESS_MARK}", self.REOPENED]
            self.land(new, no_sha=True)                      # this landing's own comments, as the land posts them
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertEqual(len(res), 1)
            self.assertNotIn("Squash commit", res[0], "no merge sha, no squash line")
            with self.subTest(old=old["pr"], new=new["pr"], case="second landing"):
                self.land(new, issue_state="CLOSED", comments=self.as_comments(first), no_sha=True)
                posted = self.posted(12)
                self.assertEqual(len(posted), 2, f"want a Resolution and {by_pr}, got {posted}")
                found = self.resolutions(12)
                self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
                index, _, body = found[0]
                self.assertIn(f"PR: https://github.com/o/r/pull/{new['pr']}", body)
                self.assertIn(new["summary"], body)
                for text in (f"https://github.com/o/r/pull/{old['pr']}", old["sha"], old["summary"]):
                    self.assertNotIn(text, body, "the new Resolution is about this PR only")
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [by_pr])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")
            for name, kept, wanted in (("this Resolution only", res, [by_pr]), ("both", res + mine, [])):
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                              no_sha=True)
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_without_a_sha_knows_its_own_landed_comment_by_the_resolutions_squash_sha(self):
        """The first run knew the sha and posted this PR's Resolution (`Squash commit: S`) and `Landed in S`; the
        resume cannot read the merge commit (null, or the read fails). `Landed in S` is still this landing's,
        found through the Squash commit of the Resolution naming this PR, so nothing is posted. An older pass's
        `Landed in <its sha>`, whose Resolution names another PR, is not: with only this PR's Resolution and
        that, only `Landed in PR #<pr>` is posted. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            self.land(new)                                   # the first run of this landing, sha known
            res = [b for _, _, b in self.resolutions(12)]
            mine = [b for _, _, b in self.landed(12)]
            self.assertIn(f"Squash commit: {new['sha']}", res[0])
            self.assertEqual([b.replace(HARNESS_MARK, "").strip() for b in mine], [f"Landed in {new['sha']}"])
            for how in (True, "fail"):
                for name, kept, wanted in (("both", res + mine, []),
                                           ("this Resolution only", res, [f"Landed in PR #{new['pr']}"])):
                    with self.subTest(old=old["pr"], new=new["pr"], merge_commit=how, case=name):
                        self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                                  no_sha=how)
                        self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)],
                                         wanted)

    # ------------------------------------------------------------------ the PR body's shape
    def one_resolution(self):
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        return found[0]

    def test_land_resolution_carries_every_line_of_a_longer_summary(self):
        """A Summary of two paragraphs and a bullet list (FIXTURE_C, edited by hand): every line of it is copied,
        in order, ahead of the files; the Risks section stays out."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        self.assertResolution(body, FIXTURE_C)
        lines = body.rstrip().splitlines()
        at = []
        for text in FIXTURE_C["summary_lines"]:
            wanted = text[2:] if text.startswith("- ") else text
            at.append(self.first(lines, lambda line, w=wanted: w in line, f"summary line {wanted!r}"))
        self.assertEqual(at, sorted(at), "the summary keeps its order")
        self.assertLess(max(at), min(self.file_positions(lines, FIXTURE_C)), "the whole summary precedes the files")
        for text in FIXTURE_C["risks"] + [FIXTURE_A["summary"], FIXTURE_B["summary"]]:
            self.assertNotIn(text, body)

    def test_land_resolution_reads_the_checklist_from_the_definition_of_done_only(self):
        """FIXTURE_C again: the `- [ ]` follow-up under Risks is not part of the checklist, while the wrapped
        item's continuation line is, and stays with its item."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        lines = self.assertFrame(body, FIXTURE_C)
        for text in FIXTURE_C["risks"]:
            self.assertNotIn(text, body, "a checkbox outside the Definition of done is not copied")
        self.assertNotIn("nightly", body)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in FIXTURE_C["dod"]]
        wrapped = [i for i, line in enumerate(lines) if FIXTURE_C["dod_wrapped"] in line]
        self.assertEqual(len(wrapped), 1, f"the wrapped item's continuation line wants one line:\n{body}")
        self.assertTrue(at_dod[1] <= wrapped[0] < at_dod[2],
                        f"the continuation line belongs with its item, between {at_dod[1]} and {at_dod[2]}:\n{body}")

    def test_land_resolution_finds_the_verify_result_whatever_the_fence_layout(self):
        """The verify fence need not follow its label directly: a blank line between, a ```text fence, or the
        `## Verify` heading of the PR template harness-scaffold installs as .github/pull_request_template.md. The
        last line of the fence is the result either way."""
        variants = [("a blank line before the fence", "Verify:\n\n```\n",
                     ["Ran 7 tests in 0.512s", "", "OK (expected failures=1)"]),
                    ("a text fence", "Verify:\n```text\n",
                     ["lint: clean", "Ran 52 tests in 1.907s", "", "OK (skipped=3)"]),
                    ("the PR template's heading", "## Verify\n```\n",
                     ["python3 -m unittest: 61 tests, all green", "VERIFY OK"])]
        for name, label, verify in variants:
            with self.subTest(name):
                fx = dict(FIXTURE_A, verify=verify)
                rendered = pr_body_text(fx)
                self.assertEqual(rendered.count("Verify:\n```\n"), 1)
                fx["body"] = rendered.replace("Verify:\n```\n", label)
                self.land(fx)
                _, _, body = self.one_resolution()
                self.assertResolution(body, fx)

    def test_land_resolution_without_a_red_run_line_has_no_red_line(self):
        """A body with no red-run paragraph at all (written by hand on a no-tests ticket): the land finishes,
        the Resolution says nothing about a red run and invents no kind."""
        fx = dict(FIXTURE_B, body=pr_body_text(FIXTURE_B, red=False))
        self.assertNotIn("Red run", fx["body"])
        self.land(fx)
        index, _, body = self.one_resolution()
        lines = self.assertFrame(body, fx)
        self.file_positions(lines, fx)
        self.assertVerifyResult(lines, fx)
        self.assertEqual([line for line in lines if re.search(r"(?i)\bred\b", line)], [],
                         f"no red run in the PR body, so no red line in the Resolution:\n{body}")
        self.assertIsNone(re.search(r"\bimport\b", body), f"a red-run kind invented:\n{body}")
        self.assertNotIn("ModuleNotFoundError", body)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0])

    def test_land_without_summary_or_verify_still_posts_the_resolution_and_lands(self):
        """A hand-written body missing its Summary and/or its Verify block: the land finishes, one Resolution
        still names every file, the checklist, the PR and the sha, nothing is invented, and `Landed in` follows."""
        cases = [("no Summary", FIXTURE_A, {"summary": False}),
                 ("no Verify", FIXTURE_B, {"verify": False}),
                 ("neither", FIXTURE_B, {"summary": False, "verify": False})]
        for name, base, drop in cases:
            with self.subTest(name):
                fx = dict(base, body=pr_body_text(base, **drop))
                self.land(fx)
                index, number, body = self.one_resolution()
                self.assertEqual(number, "12")
                lines = self.assertFrame(body, fx)
                self.file_positions(lines, fx)
                for item in fx["dod"]:
                    self.assertIn(item, body, f"checklist line {item!r} missing:\n{body}")
                self.assertRedKind(lines, fx)
                if drop.get("summary") is False:
                    self.assertNotIn(fx["summary"], body, "no Summary in the PR body, none to copy")
                else:
                    self.assertIn(fx["summary"], body)
                if drop.get("verify") is False:
                    for text in fx["verify"]:
                        if text.strip():
                            self.assertNotIn(text, body, "no Verify in the PR body, none to copy")
                else:
                    self.assertVerifyResult(lines, fx)
                for text in fx["red_tail"] + ["## Tests", "## Review", "## Risks", "Closes #12"]:
                    self.assertNotIn(text, body)
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                                 [f"Landed in {fx['sha']}"])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    # ------------------------------------------------------------------ every changed file
    def files_listed(self, body):
        """(N of the one `Files changed (N):` line, the paths on the list lines that follow it)."""
        lines = body.splitlines()
        heads = [i for i, line in enumerate(lines) if line.strip().startswith("Files changed")]
        self.assertEqual(len(heads), 1, f"want one `Files changed (N):` line:\n{body}")
        m = re.match(r"^Files changed \((\d+)\):$", lines[heads[0]].strip())
        self.assertIsNotNone(m, f"the files line reads `Files changed (N):`, got {lines[heads[0]]!r}")
        paths = []
        for line in lines[heads[0] + 1:]:
            item = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if not item:
                break
            paths.append(item.group(1).replace("`", "").strip().split(" ")[0])
        return int(m.group(1)), paths

    def test_land_resolution_lists_every_changed_file_past_the_first_hundred(self):
        """`gh pr view --json files` stops at 100 files. A PR touching 130 gets all 130 in its Resolution, from
        the paginated files endpoint, and the count says 130; the rest of the Resolution and `Landed in` as
        usual."""
        fx = FIXTURE_WIDE
        self.land(fx, view_files=fx["files"][:100])
        index, number, body = self.one_resolution()
        self.assertEqual(number, "12")
        count, listed = self.files_listed(body)
        for nth in (101, 130):
            path = fx["files"][nth - 1]
            self.assertTrue(path in listed, f"changed file no. {nth}, {path}, missing from the Resolution, which "
                                            f"lists {len(listed)} files under `Files changed ({count}):`")
        self.assertEqual(count, 130, "want `Files changed (130):`")
        self.assertEqual(sorted(listed), sorted(fx["files"]), "every changed file, each listed once")
        self.assertResolution(body, fx)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_land_resolution_lists_a_short_prs_files_once(self):
        """Three files, listed alike by the PR view and the files endpoint: `Files changed (3):` and each path
        once, whichever the land reads."""
        self.land(FIXTURE_A)
        _, _, body = self.one_resolution()
        count, listed = self.files_listed(body)
        self.assertEqual(sorted(listed), sorted(FIXTURE_A["files"]), f"each changed file once:\n{body}")
        self.assertEqual(count, 3, f"want `Files changed (3):`:\n{body}")
        self.assertResolution(body, FIXTURE_A)

    # ------------------------------------------------------------------ a failed read after the merge
    def test_land_posts_nothing_when_the_pr_read_after_the_merge_fails_and_the_resume_posts_both(self):
        """The merge goes through, then reading the PR for the Resolution (`gh pr view P --json body,...`) fails:
        no comment at all on the ticket, since `Landed in` alone would keep a resume from adding the Resolution,
        and land fails naming the step. A second land on the merged PR posts one Resolution, then `Landed in`."""
        self.scenario(self.resolution_calls(FIXTURE_A, after={"stderr": "GraphQL: something\n", "exit": 1}))
        p = self.pr("land", "34", "--yes", cwd=self.main, stubs=False)
        self.assertOneLineError(p, "PR #34 is merged", "#12", "gh pr view 34", "GraphQL: something")
        self.assertRegex(p.stderr, r"(?i)\bresolution\b", "the error names the step that failed: the Resolution")
        argv = self.gh_argv()
        self.assertTrue(any(a[:2] == ["pr", "merge"] for a in argv), "the merge went through")
        self.assertTrue(any(a[:3] == ["pr", "view", "34"] and "--json" in a[:-1]
                            and "body" in a[a.index("--json") + 1].split(",") for a in argv),
                        "the land read the PR's body after the merge, and that read failed")
        self.assertEqual([a for a in argv if a[:2] == ["issue", "comment"]], [], "no issue comment at all")
        self.assertEqual(self.posted(), [], "no comment of any kind on the ticket")
        ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
        self.assertEqual(len(ticket), 1, p.stdout)
        self.assertNotIn("commented", ticket[0])

        bodies = [b for _, _, b in self.posted(12)]        # what the ticket shows the resume: nothing
        self.land(FIXTURE_A, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "the resume does not merge again")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"the resume posts one Resolution, got {len(found)}: {self.posted()}")
        index, _, body = found[0]
        self.assertResolution(body, FIXTURE_A)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")


class LandRefusals(WorktreeTest):
    """A real origin (WorktreeTest), so a land that fetches before its checks can."""

    def test_land_refuses_before_merging(self):
        cases = [("draft", land_view(isDraft=True), "is a draft"),
                 ("conflicting", land_view(mergeable="CONFLICTING"), "conflicts with main"),
                 ("cancelled", land_view(statusCheckRollup=[check_run("build", conclusion="CANCELLED")]),
                  "checks are red on PR #34: build (cancelled)"),
                 ("failed", land_view(statusCheckRollup=[check_run("test", conclusion="FAILURE"), check_run("lint")]),
                  "checks are red on PR #34: test (failure)"),
                 ("pending", land_view(statusCheckRollup=PENDING), "still running"),
                 ("closed unmerged", land_view(state="CLOSED"), "closed without being merged")]
        for name, view, needle in cases:
            with self.subTest(name):
                self.scenario(gate_calls(commits=[pr_commit("a", at(9))])
                              + [{"match": ["pr", "view", "34"], "stdout_json": view}, REPO_VIEW,
                                 {"match": ["pr", "merge"], "stdout": ""}])
                self.assertOneLineError(self.pr("land", "34"), needle)
                self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))


class LandGate(GateAsserts, WorktreeTest):
    """`harness-pr land P` refuses a PR whose latest harness review, at the head (HEAD_SHA here), has high or medium
    findings, unless --accept-findings; it lands the PR when that review is clean or has only low findings, or there
    is no harness review. A review of another commit than the head: LandAtHead."""

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    def land_case(self, name, *flags):
        reviews, commits, named, not_named = GATE_CASES[name]
        self.scenario(Land.land_calls(self, reviews=reviews, commits=commits))
        return self.pr("land", "34", *flags, cwd=self.main), named, not_named

    def branch_exists(self):
        return Land.branch_exists(self)

    def assertLanded(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(["pr", "merge", "34", "-R", "o/r", "--squash", "--delete-branch"], self.gh_argv())
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))

    def test_land_refuses_unaddressed_findings_of_the_latest_harness_review(self):
        refused = [name for name, case in GATE_CASES.items() if case[2] is not None]
        self.assertGreaterEqual(len(refused), 5)
        for name in refused:
            with self.subTest(name):
                p, named, not_named = self.land_case(name)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any("worktree remove" in line or "branch -D" in line for line in self.order()))
                self.assertTrue(os.path.isdir(self.wt))
                self.assertTrue(self.branch_exists())
                self.assertNamesFindings(p, named, not_named)
                self.assertNotListedCount(name, p)

    def test_land_refusal_counts_the_high_and_medium_findings_listed_and_not_listed(self):
        """land's refusal carries the same "has <n> high or medium finding(s) and no commit after it" as ready's."""
        for name, when, n in GATE_REFUSAL_COUNTS:
            with self.subTest(name):
                p, _, _ = self.land_case(name)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertRegex(p.stderr, re.escape(f"PR #34: the latest harness review ({when}) has {n} high or "
                                                     "medium finding") + r"s? and no commit after it: ")
                self.assertIn("; fix them and push, or harness-pr land 34 --accept-findings", p.stderr)

    def test_land_proceeds_when_only_low_findings_are_open(self):
        """The same PR with a high finding refuses; with only low findings and no commit since, it lands."""
        self.assertTwins("findings and no commit since", "only low findings and no commit since")

    def test_land_proceeds_when_the_summary_counts_only_more_low_findings(self):
        self.assertTwins("the summary counts more high than are listed", "the summary counts more low than are listed")

    # Each proceed case first runs its refused twin (the same PR but for the one fact that decides), so the pair
    # shows that fact is what lets the land through.
    def assertTwins(self, refused, proceeds):
        p, _, _ = self.land_case(refused)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
        p, _, _ = self.land_case(proceeds)
        self.assertLanded(p)

    # #337 (R4): a commit after a review with findings no longer lets the land through by its date; the review must
    # cover the head (LandAtHead.test_a_review_of_an_earlier_commit_than_the_head_is_refused).

    def test_land_proceeds_when_the_latest_harness_review_is_clean(self):
        self.assertTwins("an older clean review, the latest with findings (listed newest first)",
                         "an older review with findings, the latest clean")

    def test_land_proceeds_without_a_harness_review(self):
        self.assertTwins("the owner reviewed after the harness findings", "no harness review, only the owner's")

    def test_accept_findings_overrides_the_refusal(self):
        p, _, _ = self.land_case("two reviews with findings, a commit between them")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)            # refused without the flag
        p, _, _ = self.land_case("two reviews with findings, a commit between them", "--accept-findings")
        self.assertLanded(p)

    def failing_land(self, failing, *flags):
        # The PR would land (the latest harness review at the head, with only low findings, so its inline comments
        # are read), so only the failed read can refuse it.
        reviews, commits, named, _ = GATE_CASES["only low findings and no commit since"]
        self.assertIsNone(named)
        self.scenario([failing] + Land.land_calls(self, reviews=reviews, commits=commits))
        return self.pr("land", "34", *flags, cwd=self.main)

    def test_land_fails_closed_when_a_gate_read_fails(self):
        # The compare of main with the head is a read of the gate now (#337). The PR's commits are not needed for a
        # review at the head; LandAtHead fails closed on them where they are.
        failures = dict({k: v for k, v in GATE_READ_FAILURES.items() if k != "commits"}, compare=compare_failure())
        for what, failing in failures.items():
            with self.subTest(what):
                p = self.failing_land(failing)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn(GATE_FAILURE_TEXT, p.stderr)
                self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any("worktree remove" in line or "branch -D" in line for line in self.order()))
                self.assertTrue(os.path.isdir(self.wt))
                self.assertTrue(self.branch_exists())

    def test_accept_findings_lands_through_a_failed_findings_read_with_a_warning(self):
        # #337 (R3): only a read the findings alone need, the review's inline comments. A read the head checks need
        # (the reviews, the PR's commits, the compare of main with the head) refuses whatever the flags (LandAtHead),
        # and gh repo view too: the merge itself needs the repository's name.
        failing = GATE_API_FAILURES["review comments"]
        self.assertNotEqual(self.failing_land(failing).returncode, 0)          # refused without the flag
        self.assertTrue(os.path.isdir(self.wt))
        p = self.failing_land(failing, "--accept-findings")
        self.assertLanded(p)
        self.assertIn(GATE_FAILURE_TEXT, p.stderr)                           # the warning names the failure


class LandAtHead(GateAsserts, BranchHistory, WorktreeTest):
    """#337: before merging, `land` checks that the PR's CI and its latest harness review cover the head being merged
    with today's main: the head not behind main (the compare of main with the head's sha answers behind_by 0), the
    review at the head (of the head itself, or of a commit the head adds only clean merges of main to: real commits
    here, judged by git in the main checkout), and green checks at the head. A refusal merges nothing and leaves the
    worktree, the branch and the ticket as they were; --accept-findings skips none of these checks."""

    def setUp(self):
        super().setUp()
        self.outputs(**{"harness-board": {"epic-check": {"stdout": EPIC_SAID}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        self.reviewed = self.push_branch()               # the commit the harness review is of, unless a case says

    def land(self, *flags, review="clean", behind=0, checks=None, mergeable="MERGEABLE", first=()):
        """`harness-pr land 34` at the branch's head as origin has it now. `review`: the latest harness review (a
        review_of pair), None for no harness review, "clean" for one without findings of self.reviewed; `behind` what
        the compare of main with the head answers; `checks` the head's statusCheckRollup (green by default); `first`
        fakegh entries ahead of the rest (a failing read, or an answer about another commit)."""
        head = self.branch_head()
        if review == "clean":
            review = review_of(self.reviewed)
        view = land_view(headRefOid=head, mergeable=mergeable, statusCheckRollup=GREEN if checks is None else checks)
        self.scenario(list(first) + Land.land_calls(self, views=[view], reviews=[review] if review else [],
                                                    commits=self.pr_commits(self.reviewed), head=head, behind=behind))
        return self.pr("land", "34", *flags, cwd=self.main)

    def assertRefused(self, p, *needles):
        """Refused before the merge, naming each needle: exit 1, no gh pr merge, no harness-board call, the worktree
        and the branch kept."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "merge"]], self.gh_argv())
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertFalse(any("worktree remove" in line or "branch -D" in line for line in self.order()))
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(Land.branch_exists(self))

    def assertLanded(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(["pr", "merge", "34", "-R", "o/r", "--squash", "--delete-branch"], self.gh_argv())
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))

    # ------------------------------------------------------------------ behind main
    def test_a_head_behind_main_is_refused_and_lands_once_main_is_merged_in_and_green(self):
        """Reviewed at the head and green, but main has two commits the head lacks: refused (`behind main by 2`),
        --accept-findings or not. Main merged in: refused while the checks at the new head run or fail, landed once
        they pass. The compare of main with the reviewed commit still answers 2, so asking about anything but the
        new head refuses."""
        self.advance_main(2)
        for flags in ((), ("--accept-findings",)):
            with self.subTest("behind main by 2", flags=flags):
                self.assertRefused(self.land(*flags, behind=2), "behind main by 2")
        merged = self.merge_main()
        self.assertNotEqual(merged, self.reviewed)
        stale = compare_calls(self.reviewed, 2)
        with self.subTest("main merged in, the checks at the new head still running"):
            self.assertRefused(self.land(checks=PENDING, first=stale), "still running")
        with self.subTest("main merged in, a check failed at the new head"):
            self.assertRefused(self.land(checks=[check_run("test", conclusion="FAILURE"), check_run("lint")],
                                         first=stale), "checks are red")
        with self.subTest("main merged in and green at the new head"):
            self.assertLanded(self.land(first=stale))

    def test_the_refusal_says_how_far_behind_main_the_head_is(self):
        """Main moves on by 1, then 2, then 4 more commits; each time the land is refused with the count the compare
        gives, with the review at the head and with no harness review at all (that proceeds only past the review
        rule)."""
        total = 0
        for n, review in ((1, "clean"), (2, None), (4, "clean")):
            total += n
            self.advance_main(n)
            with self.subTest(behind=total, review=review):
                self.assertRefused(self.land(review=review, behind=total), f"behind main by {total}")

    # ------------------------------------------------------------------ the review at the head
    def test_a_review_of_an_earlier_commit_than_the_head_is_refused(self):
        """The latest harness review is of the reviewed commit and the head adds an ordinary commit to it: refused,
        naming the review's and the head's short shas, whatever the review found and with --accept-findings too (R4:
        a commit dated after a review with findings no longer counts as addressing them). Re-reviewed at the head,
        the same PR lands."""
        head = self.plain_commit()
        for name, review in (("a clean review", review_of(self.reviewed)),
                             ("a review with a high finding", review_of(self.reviewed, FROBS)),
                             ("a review with only a low finding", review_of(self.reviewed, OLD_NIT))):
            for flags in ((), ("--accept-findings",)):
                with self.subTest(name, flags=flags):
                    self.assertRefused(self.land(*flags, review=review), self.reviewed[:7], head[:7])
        with self.subTest("re-reviewed at the head"):
            self.assertLanded(self.land(review=review_of(head, when=at(12))))

    def test_a_review_counts_through_clean_merges_of_main(self):
        """The head adds two merges of main to the reviewed commit (of an older and of a newer main), each changing
        nothing else: the review covers it and the PR lands. With an ordinary commit between the merges it does not:
        refused first."""
        self.advance_main(1)
        once = self.merge_main()
        self.plain_commit()
        self.advance_main(1)
        head = self.merge_main()
        with self.subTest("an ordinary commit among the merges"):
            self.assertRefused(self.land(), self.reviewed[:7], head[:7])
        self.reset_to(once)
        self.advance_main(1)
        twice = self.merge_main()
        self.assertEqual(len(self.git(self.wt, "rev-list", "--parents", "-n1", twice).split()), 3, "a merge commit")
        with self.subTest("two clean merges of main"):
            self.assertLanded(self.land())

    def test_findings_on_the_reviewed_commit_refuse_through_a_merge_of_main(self):
        """R4, findings are judged at the head: a high and a low finding on the reviewed commit, the head that commit
        plus a clean merge of main dated after the review. Refused, naming the high finding and not the low one;
        --accept-findings lands it."""
        self.advance_main(1)
        self.merge_main()
        review = review_of(self.reviewed, FROBS, OLD_NIT)
        p = self.land(review=review)
        self.assertRefused(p)
        self.assertNamesFindings(p, [FROBS], [OLD_NIT])
        self.assertLanded(self.land("--accept-findings", review=review))

    def test_a_merge_that_is_not_a_clean_merge_of_main_is_refused(self):
        """A merge of main with a change folded in, and a clean merge of a branch that is not main: the review of the
        commit before either does not cover it (--accept-findings or not). The same main merged with nothing else
        lands."""
        self.advance_main(1)
        evil = self.evil_merge()
        for flags in ((), ("--accept-findings",)):
            with self.subTest("a merge of main with a change folded in", flags=flags):
                self.assertRefused(self.land(*flags), self.reviewed[:7], evil[:7])
        self.reset_to(self.reviewed)
        side = self.side_merge()
        with self.subTest("a clean merge of a branch that is not main"):
            self.assertRefused(self.land(), self.reviewed[:7], side[:7])
        self.reset_to(self.reviewed)
        clean = self.merge_main()
        self.assertNotIn(clean, (evil, side))
        with self.subTest("the same main merged with nothing else"):
            self.assertLanded(self.land())

    # Each head below changes, after the reviewed commit, only files main changed too, so comparing the names of the
    # files changed with main's own would call it covered. Only the parents (every commit after the review a merge
    # whose second parent is on main, the reviewed commit on the head's first-parent line) and the trees (each such
    # merge's tree the one git makes of its two parents) tell that the review does not cover it.
    def test_a_merge_of_main_that_changes_a_file_main_changed_is_refused(self):
        """A merge of main with a different body folded into the very file main brings in, and a merge whose conflict
        with main (both added widget.py) was resolved by hand: neither is a clean merge of main."""
        self.advance_main(1)
        mine = f"elsewhere/change_{self.moves}.py"
        evil = self.evil_merge(mine, "CHANGE = 'hand-made'\n")
        self.assertNotEqual(self.tree(evil), self.merged_tree(self.reviewed))
        with self.subTest("a different body for a file of main's"):
            self.assertRefused(self.land(), self.reviewed[:7], evil[:7])
        self.reset_to(self.reviewed)
        self.main_commit({"widget.py": "def frob():\n    return 'main'\n"}, "Main adds a widget of its own")
        resolved = self.conflict_merge({"widget.py": "def frob():\n    return 'both'\n"})
        with self.subTest("a conflict with main resolved by hand"):
            self.assertRefused(self.land(), self.reviewed[:7], resolved[:7])

    def test_an_ordinary_commit_to_a_file_main_changed_is_refused_with_or_without_a_merge_after_it(self):
        """An ordinary commit after the review editing a file main changed: as the head, then under a clean merge of
        main; and one adding the very file main adds, byte for byte, under a clean merge of main (its tree is then
        the reviewed commit's plus main's)."""
        self.advance_main(1)
        self.merge_main()
        ordinary = self.plain_commit("CHANGE = 'branch'\n", f"elsewhere/change_{self.moves}.py")
        with self.subTest("the ordinary commit is the head"):
            self.assertRefused(self.land(), self.reviewed[:7], ordinary[:7])
        self.advance_main(1)
        head = self.merge_main()
        with self.subTest("a clean merge of main after the ordinary commit"):
            self.assertRefused(self.land(), self.reviewed[:7], head[:7])
        self.reset_to(self.reviewed)
        self.advance_main(1)
        self.plain_commit(f"CHANGE = {self.moves}\n", f"elsewhere/change_{self.moves}.py")
        head = self.merge_main()
        self.assertEqual(self.tree(head), self.merged_tree(self.reviewed))
        with self.subTest("main's own change committed by hand, then main merged"):
            self.assertRefused(self.land(), self.reviewed[:7], head[:7])

    def test_a_head_off_the_reviewed_commits_first_parent_line_is_refused(self):
        """The branch rebased onto the new main with the same content (the reviewed commit no longer in the head's
        history), and main merged the other way round (main the first parent, the reviewed commit the second): each
        head's tree is the reviewed commit's plus main's, and neither is covered."""
        self.advance_main(1)
        rebased = self.rebase_onto_main()
        self.assertEqual(self.tree(rebased), self.merged_tree(self.reviewed))
        with self.subTest("rebased onto main"):
            self.assertRefused(self.land(), self.reviewed[:7], rebased[:7])
        backwards = self.merge_into_main(self.reviewed)
        self.assertEqual(self.tree(backwards), self.merged_tree(self.reviewed))
        with self.subTest("the reviewed commit merged into main"):
            self.assertRefused(self.land(), self.reviewed[:7], backwards[:7])

    def test_an_ordinary_commit_and_its_revert_are_refused_with_or_without_a_merge_after_them(self):
        """An ordinary commit after the review and its revert: the head's tree is the reviewed commit's, then, under a
        clean merge of main, the reviewed commit's plus main's. Refused both times: no commit after the review but
        clean merges of main."""
        self.plain_commit()
        reverted = self.revert_head()
        self.assertEqual(self.tree(reverted), self.tree(self.reviewed))
        with self.subTest("the revert is the head"):
            self.assertRefused(self.land(), self.reviewed[:7], reverted[:7])
        self.advance_main(1)
        head = self.merge_main()
        self.assertEqual(self.tree(head), self.merged_tree(self.reviewed))
        with self.subTest("a clean merge of main after the revert"):
            self.assertRefused(self.land(), self.reviewed[:7], head[:7])

    # ------------------------------------------------------------------ failed reads (R3)
    def test_a_failed_read_the_head_checks_need_refuses_whatever_the_flags(self):
        """The reviews list and the compare of main with the head, at the reviewed head; the PR's commits once the
        head is the reviewed commit plus a merge of main. Each failing refuses, naming gh's error, --accept-findings
        or not; the same PR lands once every read answers."""
        for what, failing in (("reviews", GATE_API_FAILURES["reviews"]), ("compare", compare_failure(self.reviewed))):
            for flags in ((), ("--accept-findings",)):
                with self.subTest(what, flags=flags):
                    self.assertRefused(self.land(*flags, first=[failing]), GATE_FAILURE_TEXT)
        self.advance_main(1)
        self.merge_main()
        for flags in ((), ("--accept-findings",)):
            with self.subTest("commits", flags=flags):
                self.assertRefused(self.land(*flags, first=[GATE_API_FAILURES["commits"]]), GATE_FAILURE_TEXT)
        with self.subTest("every read answering"):
            self.assertLanded(self.land())

    # ------------------------------------------------------------------ what stays as today (R2)
    # The gate's _checks and _mergeable answer manual and wait for these; land keeps its own behaviour.
    def test_no_checks_registered_still_warns_and_lands(self):
        p = self.land(checks=[])
        self.assertLanded(p)
        self.assertIn(WARNING, p.stderr)

    def test_mergeability_github_has_not_worked_out_still_warns_and_lands(self):
        p = self.land(mergeable="UNKNOWN")
        self.assertLanded(p)
        self.assertIn(WARNING, p.stderr)

    def test_a_merged_pr_finishes_its_cleanup_whatever_its_head(self):
        """Already merged (an earlier land stopped at the cleanup, or GitHub merged it): no check of the head runs, so
        main having moved on, a review of an older commit with a high finding and pending checks change nothing."""
        head = self.plain_commit()
        self.advance_main(3)
        view = land_view(headRefOid=head, state="MERGED", statusCheckRollup=PENDING)
        self.scenario(Land.land_calls(self, views=[view], reviews=[review_of(self.reviewed, FROBS)],
                                      commits=self.pr_commits(self.reviewed), head=head, behind=3))
        p = self.ok("land", "34", cwd=self.main)
        self.assertEqual(p.stdout.splitlines()[0], "already merged, finishing cleanup")
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()))
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn(["close", "12", "--repo", self.main], self.stub_calls("harness-board"))


class OpenNotes(WorktreeTest):
    """`harness-pr open N --notes F`: the file's markdown becomes a `## Notes` section of the PR body between
    Summary and Definition of done, and the Resolution comment `land` posts repeats it."""

    def setUp(self):
        super().setUp()
        self.verify_log = write(os.path.join(self.tmp, "verify.log"), "verify line 1\nVERIFY OK\n")
        self.review = write(os.path.join(self.tmp, "findings.json"), json.dumps(FINDINGS))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("low"))},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"},
                                          "epic-check": {"stdout": "#3: 1 of 2 tasks closed; left open\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        write(self.state_path("tickets", "12.json"),
              json.dumps({"number": 12, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))

    # ------------------------------------------------------------------ open
    def open_body(self, notes=None, existing=False):
        """The PR body `harness-pr open` hands to gh: with --notes F when `notes` is the text of the file."""
        self.scenario([{"match": ["pr", "list"], "stdout_json": [{"number": 34, "url": PR_URL}] if existing else []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["pr", "edit", "34"], "stdout": PR_URL + "\n"}])
        args = ["open", "12", "--verify-log", self.verify_log, "--review", self.review]
        if notes is not None:
            args += ["--notes", write(os.path.join(self.tmp, "notes.md"), notes)]
        self.ok(*args, cwd=self.wt)
        calls = [c for c in self.gh_calls() if c["argv"][:2] in (["pr", "create"], ["pr", "edit"])]
        self.assertEqual(len(calls), 1, self.gh_argv())
        return calls[0]["stdin"]

    @staticmethod
    def headings(body):
        """[(line index, `## ` heading line)] outside code fences, as the section parser sees them."""
        found, fenced = [], False
        for i, line in enumerate(body.splitlines()):
            if re.match(r"^\s{0,3}(```|~~~)", line):
                fenced = not fenced
            elif not fenced and line.startswith("## "):
                found.append((i, line))
        return found

    def notes_section(self, body):
        """The text between the `## Notes` heading and the next `## ` heading outside a code fence (blank lines at
        the ends dropped)."""
        heads = self.headings(body)
        self.assertEqual([h for _, h in heads].count("## Notes"), 1, body)
        at = [i for i, h in heads if h == "## Notes"][0]
        end = next((i for i, _ in heads if i > at), len(body.splitlines()))
        return "\n".join(body.splitlines()[at + 1:end]).strip("\n")

    def test_notes_file_becomes_a_section_in_place(self):
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                lines = body.splitlines()
                self.assertEqual([h for _, h in self.headings(body)],
                                 ["## Summary", "## Notes", "## Definition of done", "## Tests", "## Review",
                                  "## Risks"], body)
                self.assertEqual(self.notes_section(body), text.strip("\n"))
                self.assertLess(body.index("## Summary"), body.index("## Notes"))
                self.assertLess(body.index("## Notes"), body.index("## Definition of done"))
                # directly under Summary (its text, a blank line, the heading) and directly above the checklist
                self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n\n## Notes\n", body)
                self.assertIn(text + "\n\n## Definition of done\n- [ ] The widget frobs twice", body)
                self.assertEqual(lines[0], "Closes #12")
                self.assertEqual(body.count(text), 1)
                self.assertNotIn("{{number}}", body)
                if "{{risks}}" in text:
                    self.assertIn("{{risks}} and {{summary}}", body)

    def test_notes_change_nothing_else_in_the_body(self):
        plain = self.open_body()
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                section = f"## Notes\n{text}\n\n"
                self.assertEqual(body.count(section), 1, body)
                self.assertEqual(body.replace(section, "", 1), plain)

    def test_notes_are_placed_by_the_template_not_by_the_first_matching_text(self):
        """A request whose first paragraph mentions `## Definition of done` (in a code span) puts that text in the
        Summary, ahead of the real heading: the Notes still go between the two real headings."""
        ticket = board_ticket("low")
        ticket["sections"]["request"] = ("Add the `## Definition of done` heading to the widget docs, after "
                                         "`## Summary`.\nIt must render.\n\nThe second paragraph is not the summary.")
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        summary = "Add the `## Definition of done` heading to the widget docs, after `## Summary`. It must render."
        plain = self.open_body()
        self.assertIn(f"## Summary\n{summary}\n\n## Definition of done\n", plain)
        for text in NOTES_TEXTS:
            with self.subTest(notes=text[:30]):
                body = self.open_body(text + "\n")
                self.assertEqual([h for _, h in self.headings(body)],
                                 ["## Summary", "## Notes", "## Definition of done", "## Tests", "## Review",
                                  "## Risks"], body)
                self.assertIn(f"## Summary\n{summary}\n\n## Notes\n{text}\n\n## Definition of done\n", body)
                self.assertEqual(self.notes_section(body), text)
                self.assertEqual(body.replace(f"## Notes\n{text}\n\n", "", 1), plain)

    def test_notes_reach_the_edit_of_an_existing_pr(self):
        body = self.open_body(NOTES_TEXTS[1], existing=True)
        self.assertEqual(self.gh_argv()[1], ["pr", "edit", "34", "--body-file", "-"])
        self.assertEqual(self.notes_section(body), NOTES_TEXTS[1])

    def test_no_notes_no_heading(self):
        body = self.open_body()
        self.assertNotIn("Notes", body)
        self.assertEqual([line for line in body.splitlines() if line.startswith("## ")],
                         ["## Summary", "## Definition of done", "## Tests", "## Review", "## Risks"])
        self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n\n## Definition of done\n", body)
        self.assertNotIn("{{", body)

    def test_a_missing_notes_file_stops_before_anything_is_pushed(self):
        self.scenario([])
        p = self.pr("open", "12", "--notes", os.path.join(self.tmp, "no-such-notes.md"), cwd=self.wt)
        self.assertOneLineError(p, "no-such-notes.md")
        self.assertNotIn("unrecognized", p.stderr)            # the flag exists; it is the file that is missing
        self.assertIn("notes", p.stderr.replace("no-such-notes.md", ""))
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()), self.order())

    def test_a_notes_file_with_a_second_level_heading_is_refused(self):
        """A `## ` line outside a code fence would end the Notes section early and land would drop the rest, so
        `open` refuses the file before anything is pushed. `###` headings and fenced `## ` lines are fine."""
        cases = {"Out of scope": "## Out of scope\n- `legacy/frob_old.py` left alone.\n",
                 "Follow-ups": "Second pass: fixed the loop.\n\n## Follow-ups\nTicket the cleanup.\n",
                 "Why it rode along": "Text first.\n```\n## fenced, fine\n```\n## Why it rode along   \nmore\n"}
        for heading, text in cases.items():
            with self.subTest(heading=heading):
                self.scenario([])
                path = write(os.path.join(self.tmp, "second-pass-notes.md"), text)
                p = self.pr("open", "12", "--verify-log", self.verify_log, "--notes", path, cwd=self.wt)
                self.assertOneLineError(p, "second-pass-notes.md", heading)
                self.assertNotIn("unrecognized", p.stderr)
                self.assertEqual(self.gh_calls(), [])
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()),
                                 self.order())
        # the same file without its heading line is accepted, so the refusal is about the heading
        body = self.open_body("Second pass: fixed the loop.\n\n### Follow-ups\nTicket the cleanup.\n")
        self.assertIn("### Follow-ups", body)

    def test_a_notes_file_with_a_bare_second_level_heading_is_refused(self):
        """`## ` with nothing after it is still a heading to the section parser (it ends the Notes section), so it
        is refused like any other; there is no heading text to name, so the error names the file."""
        cases = {"bare, one space": "Second pass.\n\n## \nLeft alone.\n",
                 "bare, trailing spaces": "## \t  \nOut of scope: the legacy parser.\n",
                 "bare, after a fence": "Text.\n```\n## in a fence\n```\n##   \nmore\n"}
        for what, text in cases.items():
            with self.subTest(what):
                self.scenario([])
                path = write(os.path.join(self.tmp, "bare-heading-notes.md"), text)
                p = self.pr("open", "12", "--verify-log", self.verify_log, "--notes", path, cwd=self.wt)
                self.assertOneLineError(p, "bare-heading-notes.md")
                self.assertNotIn("unrecognized", p.stderr)
                self.assertEqual(self.gh_calls(), [])
                self.assertEqual(self.stub_calls("harness-board"), [])
                self.assertFalse(any(line.startswith("git") and " push " in line for line in self.order()),
                                 self.order())

    # ------------------------------------------------------------------ land
    resolution_calls = LandResolution.resolution_calls
    posted = LandResolution.posted
    resolutions = LandResolution.resolutions

    def land_with_notes(self, fx, notes, where="before_dod"):
        """Land the fixture's PR whose body holds a Notes section: where `harness-pr open --notes` renders it
        (`before_dod`), or hand-moved to sit between Review and Risks (`after_review`) or at the very end (`last`)."""
        text = pr_body_text(fx)
        section = f"## Notes\n{notes}\n\n"
        if where == "before_dod":
            body = text.replace("## Definition of done", section + "## Definition of done", 1)
        elif where == "after_review":
            body = text.replace("## Risks", section + "## Risks", 1)
        else:
            body = text + "\n\n" + section.rstrip("\n") + "\n"
        self.assertEqual(body.count("## Notes\n"), 1)
        self.scenario(self.resolution_calls(dict(fx, body=body)))
        self.ok("land", str(fx["pr"]), "--yes", cwd=self.main, stubs=False)
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        return found[0][2]

    def test_resolution_repeats_the_notes(self):
        cases = ((FIXTURE_A, 1, "before_dod"), (FIXTURE_B, 2, "before_dod"), (FIXTURE_A, 0, "before_dod"),
                 (FIXTURE_A, 4, "before_dod"),        # a fenced `## Definition of done` inside the notes
                 (FIXTURE_B, 4, "after_review"), (FIXTURE_A, 3, "after_review"), (FIXTURE_B, 3, "last"),
                 (FIXTURE_A, 4, "last"), (FIXTURE_B, 1, "last"))
        for fx, n, where in cases:
            text = NOTES_TEXTS[n]
            with self.subTest(pr=fx["pr"], notes=n, where=where):
                resolution = self.land_with_notes(fx, text, where)
                self.assertIn(text, resolution)
                self.assertEqual(resolution.count(text), 1)
                lines = resolution.rstrip().splitlines()
                self.assertEqual(lines[0], RESOLUTION)
                self.assertEqual(lines[-1].strip(), HARNESS_MARK)
                # in a sensible place: after the summary, before the PR link and squash sha that close it
                at_notes = resolution.index(text)
                self.assertGreater(at_notes, resolution.index(fx["summary"]))
                self.assertLess(at_notes, resolution.index(f"PR: https://github.com/o/r/pull/{fx['pr']}"))
                self.assertLess(at_notes, resolution.index(f"Squash commit: {fx['sha']}"))
                # the rest of the Resolution is as before: the checklist, the files, no other PR body section
                for item in fx["dod"]:
                    self.assertIn(item, resolution)
                for path in fx["files"]:
                    self.assertIn(f"- `{path}`", resolution)
                for left_out in ("## Tests", "## Review", "## Risks", "Closes #12", fx["review"], "None noted."):
                    self.assertNotIn(left_out, resolution)
                # the Notes do not swallow the Definition of done that follows them in the PR body
                self.assertEqual(resolution.count("Definition of done:"), 1)

    def test_notes_do_not_replace_the_resolutions_proof_lines(self):
        """Notes sit ahead of `## Tests` in the PR body. A `Red run ...` line and a `Verify:` label with a fence
        inside the Notes are the Notes' own text: the Resolution's red-run and verify lines come from the Tests."""
        notes = ("Second pass.\n\nRed run of the added test: assertion, exit 1.\n\nVerify:\n```\n"
                 "notes-only check output\nNOTES VERIFY PASSED\n```\nThat is all.")
        for fx, red, verify in ((FIXTURE_A, "Red run before implementation (assertion, exit 1)", "OK (skipped=1)"),
                                (FIXTURE_B, "Red run before implementation (import, exit 2)",
                                 "VERIFY PASSED: 212 checks, 0 warnings")):
            with self.subTest(pr=fx["pr"]):
                resolution = self.land_with_notes(fx, notes)
                lines = resolution.rstrip().splitlines()
                self.assertEqual([x for x in lines if x.startswith("Red run before implementation")], [red])
                self.assertEqual([x for x in lines if x.startswith("Verify: ")], [f"Verify: {verify}"])
                self.assertNotIn("Verify: NOTES VERIFY PASSED", resolution)
                # the Notes still appear, whole, ahead of the closing block that holds the real lines
                self.assertEqual(resolution.count(notes), 1)
                closing = resolution.index(red)
                self.assertLess(resolution.index(notes), closing)
                self.assertLess(closing, resolution.index(f"Verify: {verify}"))
                pr_line = f"PR: https://github.com/o/r/pull/{fx['pr']}"
                self.assertLess(resolution.index(f"Verify: {verify}"), resolution.index(pr_line))

    def test_resolution_without_notes_has_no_notes(self):
        self.scenario(self.resolution_calls(FIXTURE_A))
        self.ok("land", "34", "--yes", cwd=self.main, stubs=False)
        found = self.resolutions()
        self.assertEqual(len(found), 1)
        self.assertNotIn("Notes", found[0][2])


# ------------------------------------------------------------------ the shared fixtures stay shared (#348)
class FixturesLiveInPrSupport(unittest.TestCase):
    """pr_support.py is a test file to the tests-first rules (#348), so the land modules take its fixtures as they are
    and bind none of its names themselves: a redefinition after `from pr_support import *` shadows the shared fixture
    in that one module, and the next change to pr_support misses it."""

    def test_the_land_modules_redefine_none_of_pr_supports_names(self):
        import ast

        def bound(name):
            """The names the top level of tests/`name` binds itself: assignments, functions, classes; not imports."""
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), name)
            names = set()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            return {n for n in names if not n.startswith("__")}

        shared = bound("pr_support.py")
        for name in ("LAND_FIELDS", "land_view", "gate_calls", "PrTest"):  # the parser sees pr_support's fixtures
            self.assertIn(name, shared)
        for module in ("test_pr_land.py", "test_pr_ultra.py"):
            with self.subTest(module=module):
                self.assertEqual(sorted(bound(module) & shared), [])


if __name__ == "__main__":
    unittest.main()
