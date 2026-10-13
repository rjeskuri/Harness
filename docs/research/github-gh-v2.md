# Research notes: GitHub and gh facts for v2

Gathered 2026-10-13 by an Opus research agent for gh 2.99.0 (2026-09-01), from `gh <cmd> --help`, the cli/cli source at tag v2.99.0, docs.github.com and read-only probes against public repos and the user's own private repos. Nothing was created or changed.

## Sub-issues
- `gh issue create --parent N` links a sub-issue; `gh issue edit --parent/--remove-parent/--add-sub-issue/--remove-sub-issue` exist. `edit --parent` sends `replaceParent: true` (silently re-parents). `create --parent` links only after the issue is created; if the link fails the issue exists, the command exits non-zero and prints no URL, so a naive retry duplicates.
- Listing: no `gh issue list` parent flag. `gh api repos/O/R/issues/N/sub_issues` returns full issues with labels (max 100 per page). `gh issue view --json subIssues,subIssuesSummary` has state but no labels. `--search 'parent-issue:O/R#N'` works through the Search API. GraphQL `subIssues(first:100){nodes{… labels(first:20){nodes{name}}}}` works.
- "You can add up to 100 sub-issues per parent issue and create up to eight levels of nested sub-issues." A parent does not auto-close when all sub-issues close (verified on two public repos).
- `gh issue view N --json id` returns the GraphQL node id; REST `.id` is the numeric database id used by `POST …/sub_issues` (`sub_issue_id`).

## Dependencies and types
- Issue dependencies "are available for users on GitHub Free, GitHub Pro, GitHub Team, and GitHub Enterprise Cloud plans." REST: `GET/POST /repos/O/R/issues/N/dependencies/blocked_by` (`issue_id` numeric), `DELETE …/blocked_by/{issue_id}`, `GET …/dependencies/blocking`. gh 2.99.0: `create --blocked-by/--blocking`, `edit --add-blocked-by/--remove-blocked-by/--add-blocking/--remove-blocking`, `--json blockedBy,blocking`.
- Issue types are organization-only ("Default issue types are included in every organization"); a user-owned repo returns `issueTypes: null`. Labels it is.

## Labels, lists, comments
- `gh label create <name> --color <hex> --description <text> --force`: "Update the label color and description if label already exists" (PATCH on 422). Without `--color` a random color overwrites the existing one; an empty `--description` is not applied.
- Repeated `--label a --label b` is AND. `--label` and `--search` go through the Search API (capped at 1000). `--search 'label:x -label:y'` works; `is:closed` in `--search` overrides the default state.
- `gh issue list --json` fields include `number,title,labels,state,body,assignees,url,comments,parent,subIssues,subIssuesSummary,blockedBy,blocking,id,stateReason`.
- `gh issue view N --json comments` pages through all comments (`author.login`, `body`, `createdAt`, `url`); `gh issue comment N --body-file -` posts; `--edit-last --create-if-none` is idempotent.

## Pull requests
- `gh pr create --draft --title --body-file --base main --head <branch>` ("Use `--head` to explicitly skip any forking or pushing behavior", so push first). Draft PRs are free on private repos since 2025-05-01. `gh pr ready [N]` (and `--undo`).
- `gh pr checks --watch` loops while any check is pending; `--fail-fast` exits on the first failure; exit 1 if any check failed, 8 if pending, else 0. CANCELLED checks are not counted as failures. With no checks registered it errors immediately (`no checks reported`), so poll until checks appear.
- `gh pr view --json statusCheckRollup,reviewDecision,mergeable,mergeStateStatus,isDraft,closingIssuesReferences`; `mergeable` is `MERGEABLE|CONFLICTING|UNKNOWN` (retry on UNKNOWN); `reviewDecision` is `""` on a Free private repo.
- "Pull request authors cannot approve their own pull requests." A `COMMENT` review with inline comments by the author works: `POST /repos/O/R/pulls/N/reviews` with `event: COMMENT`, `body` (required), `comments: [{path, line, side: RIGHT|LEFT, body}]`; `line` must be in the diff; `position` is deprecated. Invoke with `gh api repos/O/R/pulls/N/reviews --input review.json`.
- Merge (`pr/merge/merge.go`, worktree handling added 2026-08): order is merge, delete local branch, delete remote branch. Head branch checked out in the current linked worktree: local delete skipped with a hint, remote still deleted. In another linked worktree: `git worktree remove` (no `--force`), then `branch -D`; a dirty worktree skips the local delete. `-R O/R` turns off local deletion entirely. "When you merge a linked pull request into the default branch of a repository, its linked issue is automatically closed"; the keyword must be in the PR body (or a commit message) and the PR must target the default branch; check `closingIssuesReferences` before merging. Use `branch -D` after a squash merge.

## Repositories and protection
- `gh repo create <name> --private --source . --remote origin [--push]`: with `--push` it runs `git remote add` then `git push --set-upstream origin HEAD` (current branch only); with no commits and `--push` it fails before creating anything; an existing remote name fails after the GitHub repo is created.
- Protected branches: "available in public repositories with GitHub Free… Protected branches are also available in public and private repositories with GitHub Pro, GitHub Team, GitHub Enterprise Cloud". Rulesets say the same; push rulesets are Team-only for private repos. This account: `GET branches/main/protection`, `rulesets` and `rules/branches/main` all return 403 "Upgrade to GitHub Pro or make this repository public to enable this feature." Reading `plan.name` needs `read:user` (not granted; `gh api user --jq .plan` is null). No other server-side block exists on Free private repos.

## GitHub Actions
- Current: `actions/checkout@v7` (v7.0.1), `actions/setup-node@v7` (v7.1.0), `astral-sh/setup-uv@v10.3.0` (setup-uv stopped publishing major tags at v8: pin the full version or SHA; `enable-cache` defaults to auto). uv's guide: `uv sync --locked --all-extras --dev` then `uv run pytest`.
- `pull_request` default activity types are `opened`, `synchronize`, `reopened` (not `ready_for_review`). `concurrency: group: ${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress` as an expression is documented.

## What it meant for the design
- Read an epic with `gh api repos/O/R/issues/EPIC/sub_issues`; close the epic ourselves when `subIssuesSummary.completed == total`.
- Create tasks without `--parent`, then link with REST and retry only the link.
- Reviewer findings become a `COMMENT` review with inline comments; the merge gate is ours (checks green, no unresolved threads), since review decisions do not gate on this plan.
- Land with `gh pr merge N -R O/R --squash --delete-branch`, then `git worktree remove`, `git branch -D`, `git fetch --prune`, `git pull --ff-only` on main.
- Branch protection is unavailable; the local guard is the only block (GitHub Pro or public repos would add a server-side one).
