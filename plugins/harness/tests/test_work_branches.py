"""harness-work branches [--json] [--prune] and the remote branch of worktree remove (#416).

`branches` lists the origin `t/*` branches that have no live worktree, each with its ticket (from the branch name), its
issue's state, its newest PR and a stale flag. A branch is stale only when its issue is closed, it has no open PR, no
live run record names it, and its remote tip is the head of one of its PRs, or it has no PR and its issue was closed
as completed. `--prune` deletes each stale branch with `gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`
and prints one line per branch, deleted or kept with the reason; `worktree remove <branch>` also deletes the remote
branch under the same rule, and its local removal never waits on gh.

The fixture: WorkTest's bare origin and main checkout, each branch of a table pushed to origin as two commits on main
(a parent and the tip, made in one `git fast-import`), and each PR's head both in the fake `gh pr list` answer
(headRefOid) and on origin as refs/pull/N/head, so the tool may read a PR's head either way. The fake gh answers
`issue view N` (state, stateReason), `pr list` per branch (the branch on its argv) and in bulk (headRefName on each
row), `repo view` (o/r) and a DELETE with GitHub's empty 204. It deletes nothing: origin keeps every ref, so a branch
the tool deleted shows only as a DELETE call in FAKEGH_LOG, and a ref gone from origin would mean a `git push` the
tool must never make. A live run is a cloud run record with no ended_at; an ended one has ended_at set.

Each test's first harness-work run is a process of its own; its further rows run in this process
(gitrepo_support.run_in_process). The fake executables are written once for the module (work_support.setUpModule).

Run: python3 -m unittest discover -s plugins/harness/tests -p test_work_branches.py -v
"""

from __future__ import annotations

import copy
import unittest

from gitrepo_support import run_in_process
from work_support import *  # noqa: F401,F403

# The endpoint of a branch DELETE: owner/repo as `gh repo view` names it (o/r), or gh's own placeholders.
DELETE_PATH = re.compile(r"/?repos/(?:o/r|\{owner\}/\{repo\})/git/refs/heads/(.+)")
KEEP_WORD = re.compile(r"\b(kept|keep|keeps|keeping)\b", re.I)
DELETE_WORD = re.compile(r"\bdelet(e|ed|es|ing)\b", re.I)
GH_TROUBLE = re.compile(r"\b(kept|keep|keeps|keeping|could not|cannot|can't|failed|fails)\b", re.I)
OPEN_WORD = re.compile(r"\bopen\b", re.I)
RUN_WORD = re.compile(r"\bruns?\b", re.I)
GH_DOWN = {"calls": [], "default": {"stderr": "HTTP 502: Bad gateway (https://api.github.com/graphql)\n", "exit": 1}}


class Row:
    """One origin branch: its issue's state and stateReason, its PRs newest first as (number, state, head) with head
    "tip" (the branch's remote tip) or "parent" (the commit before it), whether a run record names it ("live",
    "ended" or None), and `keep`: None when the branch is stale, else the reason it is kept ("issue", "pr", "run" or
    "tip")."""

    def __init__(self, branch, state, reason, prs=(), run=None, keep=None):
        self.branch, self.state, self.reason = branch, state, reason
        self.ticket = int(branch.split("/")[1].split("-")[0])
        self.prs, self.run, self.keep = list(prs), run, keep

    def __repr__(self):
        return self.branch


def unblocked(row):
    """The row with its one keep reason taken away, and so stale."""
    out = copy.deepcopy(row)
    if row.keep == "issue":
        out.state, out.reason = "CLOSED", "NOT_PLANNED"
    elif row.keep == "pr":
        out.prs = [(n, "CLOSED", head) for n, _, head in row.prs]
    elif row.keep == "run":
        out.run = "ended"
    elif row.keep == "tip" and row.prs:
        out.prs = [(row.prs[0][0], row.prs[0][1], "tip")] + row.prs[1:]
    elif row.keep == "tip":
        out.reason = "COMPLETED"
    out.keep = None
    return out


# Every keep reason once beside two stale branches; each kept row differs from a stale one in its one reason.
MIX = [
    # stale: its newest PR (#131, closed) holds the tip; closed as not planned; a run that ended names it
    Row("t/21-alpha", "CLOSED", "NOT_PLANNED", prs=[(131, "CLOSED", "tip"), (121, "CLOSED", "parent")], run="ended"),
    Row("t/22-bravo", "CLOSED", "COMPLETED"),                                         # stale: no PR, completed
    Row("t/23-charlie", "OPEN", "", prs=[(123, "CLOSED", "tip")], keep="issue"),
    Row("t/24-delta", "CLOSED", "NOT_PLANNED", prs=[(124, "OPEN", "tip")], keep="pr"),
    Row("t/25-echo", "CLOSED", "NOT_PLANNED", prs=[(125, "CLOSED", "tip")], run="live", keep="run"),
    Row("t/26-foxtrot", "CLOSED", "NOT_PLANNED", keep="tip"),                         # no PR, not completed
    # commits pushed after its PR merged: no PR ref holds the tip, and completed does not excuse a branch with a PR
    Row("t/27-golf", "CLOSED", "COMPLETED", prs=[(127, "MERGED", "parent")], keep="tip"),
]
LIVE_WORKTREE = Row("t/20-zulu", "CLOSED", "COMPLETED")    # stale by the rule, but its worktree is live
NOT_A_TICKET = "other/31-xray"

# worktree remove: the same rule; t/41-india first, the one run that is a process of its own
REMOVE = [
    Row("t/41-india", "CLOSED", "NOT_PLANNED", prs=[(141, "CLOSED", "tip")]),
    Row("t/42-juliet", "CLOSED", "COMPLETED"),
    Row("t/43-kilo", "OPEN", "", prs=[(143, "CLOSED", "tip")], keep="issue"),
    # the local branch goes with the worktree, so the remote holds the only copy of the commits after the merge
    Row("t/44-lima", "CLOSED", "COMPLETED", prs=[(144, "MERGED", "parent")], keep="tip"),
]
NEVER_PUSHED = Row("t/48-papa", "CLOSED", "COMPLETED")     # stale by the rule, but origin has no such branch
GH_FAILS = Row("t/49-quebec", "CLOSED", "COMPLETED")       # stale by the rule, but every gh call fails


def names(text, branch):
    """The lines of `text` that name `branch` (also as origin/<branch>), never a longer branch name."""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(branch) + r"(?![\w/.-])")
    return [line for line in text.splitlines() if pattern.search(line)]


def field(obj, *path):
    """obj[path[0]][path[1]]..., or a marker naming the missing key, so a missing field fails an assertion."""
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return f"<no {key}>"
        obj = obj[key]
    return obj


class BranchTest(WorkTest):
    def setUp(self):
        super().setUp()
        self.ran_a_process = False
        self.sha = {}

    # ----------------------------------------------------------- runs: the first a process, the rest in process
    def work(self, *args, env=None, cwd=None):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().work(*args, env=env, cwd=cwd)
        p = run_in_process(TOOL, args=args, cwd=cwd or self.repo, env=env or self.env())
        self.assertNotIn("Traceback", p.stderr)
        return p

    # ----------------------------------------------------------- fixture
    def git_input(self, repo, text, *args):
        p = subprocess.run([GIT, "-C", repo] + list(args), input=text, env=self.env(), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout

    def commits(self, rows, local=()):
        """Two commits on main per row's branch (main's tree), in the main checkout: at refs/heads/<branch> for a row
        in `local`, else at refs/tmp/<branch> (no local branch). self.sha[branch] = {parent, tip}."""
        main = self.git(self.repo, "rev-parse", "main")
        when = int(time.time())
        stream = []
        for row in rows:
            ref = ("refs/heads/" if row in local else "refs/tmp/") + row.branch
            for i, word in enumerate(("first", "second")):
                msg = f"{row.branch}: {word}"
                stream += [f"commit {ref}", f"committer Test <test@localhost> {when + i} +0000",
                           f"data {len(msg.encode('utf-8'))}", msg] + ([f"from {main}"] if i == 0 else []) + [""]
        self.git_input(self.repo, "\n".join(stream) + "\n", "fast-import", "--quiet")
        out = self.git(self.repo, "for-each-ref", "--format=%(refname) %(objectname) %(parent)",
                       "refs/tmp", "refs/heads/t")
        for line in out.splitlines():
            ref, tip, parent = line.split()
            branch = ref.split("/", 2)[2]
            if branch in {r.branch for r in rows}:
                self.sha[branch] = {"parent": parent, "tip": tip}

    def apply(self, rows, first=()):
        """The rows' PR heads on origin (refs/pull/N/head), their run records and the fake gh's answers about them;
        `first`: scenario entries ahead of the rows' own."""
        pulls = "".join(f"update refs/pull/{n}/head {self.sha[r.branch][head]}\n"
                        for r in rows for n, _, head in r.prs)
        if pulls:
            self.git_input(self.origin, pulls, "update-ref", "--stdin")
        for row in rows:
            rid = f"{self.key}-{row.ticket}-20261119080000"
            path = os.path.join(self.runs_dir(), rid + ".json")
            if row.run is None:
                if os.path.exists(path):
                    os.unlink(path)
                continue
            sid = f"session_0{row.ticket}Branch"
            self.write_run(row.ticket, id=rid, venue="cloud", branch=row.branch, cloud_id=sid,
                           url=f"https://claude.ai/code/{sid}?from=cli&m=0", worktree=self.worktree_path(row.branch),
                           started_at=iso_ago(minutes=30) if row.run == "live" else iso_ago(hours=5),
                           **({} if row.run == "live" else {"ended_at": iso_ago(hours=4), "exit": 0, "outcome": "pr"}))
        prs = {r.branch: [{"number": n, "state": s, "isDraft": False, "headRefName": r.branch,
                           "headRefOid": self.sha[r.branch][head], "createdAt": iso_ago(days=200 - n),
                           "url": f"https://github.com/o/r/pull/{n}"} for n, s, head in r.prs] for r in rows}
        issues = []
        for r in rows:
            answer = issue(r.ticket, labels=("task", "size:S", "risk:low"), state=r.state, title=f"Ticket {r.ticket}")
            answer["stateReason"] = r.reason
            issues.append(answer)
        calls = list(first) + [
            {"contains": ["api", "-X", "DELETE"], "stdout": ""},           # GitHub answers 204 No Content
            {"contains": ["api", "--method", "DELETE"], "stdout": ""},
        ]
        calls += [{"match": ["issue", "view", str(i["number"])], "stdout_json": i} for i in issues]
        calls += [{"contains": ["pr", "list", r.branch], "stdout_json": prs[r.branch]} for r in rows]
        calls += [
            {"match": ["pr", "list"], "stdout_json": sorted((p for r in rows for p in prs[r.branch]),
                                                           key=lambda p: -p["number"])},
            {"match": ["issue", "list"], "stdout_json": issues},
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        self.write_scenario({"calls": calls})

    def write_scenario(self, scenario):
        if scenario.get("calls"):
            self.put_scenario(scenario)          # #447: issues and pull requests through REST
            return
        write(self.scenario_path, json.dumps(scenario))     # GH_DOWN: every call, REST too, fails
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    # ----------------------------------------------------------- what happened
    def deletes(self, since=0):
        """The branch each `gh api` DELETE call after the first `since` gh calls names, in order (None for a DELETE
        whose endpoint is not repos/<owner>/<repo>/git/refs/heads/<branch>)."""
        out = []
        for argv in self.gh_calls()[since:]:
            if argv[:1] != ["api"] or not any(a in ("-X", "--method") and b.upper() == "DELETE"
                                              for a, b in zip(argv, argv[1:])):
                continue
            found = [m.group(1) for m in map(DELETE_PATH.fullmatch, argv) if m]
            out.append(found[0] if len(found) == 1 else None)
        return out

    def origin_heads(self):
        return set(self.git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads").split())

    def assertGivesReason(self, line, row):
        """A kept line names the row's one reason and not another's."""
        self.assertRegex(line, KEEP_WORD)
        if row.keep == "issue":
            self.assertRegex(line, OPEN_WORD)
            self.assertRegex(line, re.compile(rf"\b(issue|ticket)\b|#{row.ticket}\b", re.I))
        elif row.keep == "pr":
            self.assertRegex(line, OPEN_WORD)
            self.assertRegex(line, rf"\b{row.prs[0][0]}\b")
        elif row.keep == "run":
            self.assertRegex(line, RUN_WORD)
        else:
            self.assertRegex(line, re.compile(r"tip|head|commit|refs/pull|complet|planned", re.I))
        if row.keep in ("issue", "pr", "tip"):
            self.assertNotRegex(line, RUN_WORD)
        if row.keep in ("run", "tip"):
            self.assertNotRegex(line, OPEN_WORD)


# =========================================================================== branches and branches --prune
class Branches(BranchTest):
    def setUp(self):
        super().setUp()
        self.commits(MIX + [LIVE_WORKTREE], local=[LIVE_WORKTREE])
        self.git(self.repo, "worktree", "add", "-q", self.worktree_path(LIVE_WORKTREE.branch), LIVE_WORKTREE.branch)
        # one push: each tip as its branch (which also sets refs/remotes/origin/<branch>), the worktree's branch, and
        # a branch outside t/
        self.git(self.repo, "push", "-q", "origin", LIVE_WORKTREE.branch,
                 f"refs/tmp/{MIX[1].branch}:refs/heads/{NOT_A_TICKET}",
                 *[f"refs/tmp/{r.branch}:refs/heads/{r.branch}" for r in MIX])
        self.apply(MIX + [LIVE_WORKTREE])

    def test_lists_the_origin_ticket_branches_without_a_worktree_and_deletes_nothing(self):
        p = self.work("branches", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = {field(r, "branch"): r for r in json.loads(p.stdout)}
        self.assertEqual(set(rows), {r.branch for r in MIX})       # not t/20-zulu (a worktree), main or other/*
        for row in MIX:
            with self.subTest(branch=row.branch):
                got = rows[row.branch]
                self.assertEqual(field(got, "ticket"), row.ticket)
                self.assertEqual(str(field(got, "issue", "state")).upper(), row.state)
                if row.prs:
                    newest = row.prs[0]
                    self.assertEqual((field(got, "pr", "number"), str(field(got, "pr", "state")).upper()),
                                     newest[:2])
                else:
                    self.assertIsNone(field(got, "pr"))
                self.assertIs(field(got, "stale"), row.keep is None)
        self.assertEqual(self.deletes(), [])

    def test_prune_deletes_exactly_the_stale_branches_and_keeps_each_other_with_its_reason(self):
        before = self.origin_heads()
        p = self.work("branches", "--prune")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(self.deletes()), sorted(r.branch for r in MIX if r.keep is None))
        for row in MIX:                                   # one row per keep reason, beside the stale ones
            with self.subTest(branch=row.branch, keep=row.keep):
                lines = names(p.stdout, row.branch)
                self.assertEqual(len(lines), 1, f"one line names {row.branch}:\n{p.stdout}")
                if row.keep is None:
                    self.assertRegex(lines[0], DELETE_WORD)
                    self.assertNotRegex(lines[0], KEEP_WORD)
                else:
                    self.assertGivesReason(lines[0], row)
        self.assertEqual(self.origin_heads(), before)      # through the API only: no git push touched origin

        # In this process: each kept row with its one reason taken away is stale, and deleted with the others.
        self.apply([unblocked(r) for r in MIX] + [LIVE_WORKTREE])
        since = len(self.gh_calls())
        p = self.work("branches", "--prune")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(self.deletes(since)), sorted(r.branch for r in MIX))
        for row in MIX:
            with self.subTest(branch=row.branch, reason_gone=row.keep):
                lines = names(p.stdout, row.branch)
                self.assertEqual(len(lines), 1, p.stdout)
                self.assertRegex(lines[0], DELETE_WORD)
                self.assertNotRegex(lines[0], KEEP_WORD)


# =========================================================================== worktree remove takes the remote branch
class WorktreeRemove(BranchTest):
    def setUp(self):
        super().setUp()
        rows = REMOVE + [NEVER_PUSHED, GH_FAILS]
        self.commits(rows, local=rows)
        self.paths = {}
        for row in rows:
            self.paths[row.branch] = self.worktree_path(row.branch)
            self.git(self.repo, "worktree", "add", "-q", self.paths[row.branch], row.branch)
        self.git(self.repo, "push", "-q", "origin", *[r.branch for r in rows if r is not NEVER_PUSHED])
        self.apply(rows)

    def remove(self, row):
        """`worktree remove <branch>`: the worktree and the local branch are gone whatever happens to the remote."""
        since = len(self.gh_calls())
        p = self.work("worktree", "remove", row.branch)
        out = "\n".join([p.stdout, p.stderr])
        self.assertFalse(os.path.exists(self.paths[row.branch]), out)
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname:short)", row.branch), "", out)
        return p, self.deletes(since), out

    def test_remove_deletes_the_remote_branch_only_when_it_is_stale(self):
        pushed = {f"refs/heads/{r.branch}" for r in REMOVE + [GH_FAILS]}
        self.assertTrue(pushed <= self.origin_heads())
        for row in REMOVE:
            with self.subTest(branch=row.branch, keep=row.keep):
                p, deleted, out = self.remove(row)
                self.assertEqual(p.returncode, 0, out)
                kept = [line for line in names(out, row.branch) if KEEP_WORD.search(line)]
                if row.keep is None:
                    self.assertEqual(deleted, [row.branch], out)
                    self.assertEqual(kept, [], out)
                else:
                    self.assertEqual(deleted, [], out)
                    self.assertTrue(kept, f"no line says {row.branch} is kept:\n{out}")
                    self.assertGivesReason(kept[0], row)

        with self.subTest(branch=NEVER_PUSHED.branch, case="origin has no such branch"):
            p, deleted, out = self.remove(NEVER_PUSHED)
            self.assertEqual(p.returncode, 0, out)
            self.assertEqual(deleted, [], out)

        with self.subTest(branch=GH_FAILS.branch, case="every gh call fails"):
            self.write_scenario(GH_DOWN)
            p, deleted, out = self.remove(GH_FAILS)
            self.assertEqual(deleted, [], out)
            self.assertTrue([line for line in names(out, GH_FAILS.branch) if GH_TROUBLE.search(line)],
                            f"nothing says the remote {GH_FAILS.branch} was kept:\n{out}")

        self.assertTrue(pushed <= self.origin_heads())    # through the API only: no git push touched origin


if __name__ == "__main__":
    unittest.main()
