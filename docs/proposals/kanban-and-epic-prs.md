# Proposal: a Projects kanban and epic-level pull requests

Ticket #54, epic #52. Written 2026-10-14 against gh 2.99.0 and the code on main at a6df5e2. Every platform fact was read on 2026-10-14 from the URL cited; docs.github.com pages carry no last-updated date. Items marked **UNVERIFIED** could not be confirmed from a primary source. Nothing was created or changed on GitHub.

## Recommendation in short

1. **Board: add a GitHub Projects board as a mirrored view and keep labels as the source of truth.** Use one user-owned project across all repositories. Status is the column field. The lanes are grouped by Parent issue (the epic), with Priority and Repository as alternate views. `harness-board` writes Status on every status change, one `gh project item-edit` call each, and never reads the board, except in a `project sync` command that repairs drift. No reader of labels changes: `ready`, `board`, session_brief, standup, digest and dispatch all stay as they are.
2. **Epic pull requests: opt-in per epic (an `epic-branch` flag), not the new default.** Task branches cut from and merge into `e/<epic>-<slug>`. The per-task loop (tests first, Fable review, CI, Resolution) is unchanged. Ryan reviews and lands one pull request from the epic branch to main, merged with a merge commit so each task's squash sha stays on main. A named checkpoint lands a snapshot branch `cp/E-K`, which closes nothing.
3. **Decisions Ryan has to make first** (see [Decisions for Ryan](#decisions-for-ryan)):
   - Whether agents may merge task pull requests into an epic branch. Without that, epic pull requests add review load rather than remove it.
   - Granting the `project` token scope.

## 1. Platform facts

### Fields and lanes

| Fact | Source |
|---|---|
| Up to 50 fields per project, built-in ones included. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects |
| Custom field types are single select, multi select, iteration, number, date and text. Single select allows up to 50 options and a default option for new items. | https://docs.github.com/en/graphql/reference/projects; https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-single-select-fields |
| Board columns can only be "any single select or iteration field". Dragging a card to another column sets that value. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-board-layout |
| Horizontal groups (swim lanes) can use a custom field value. "You cannot group by title, labels, reviewers, or linked pull requests." Dragging a card between groups sets that value. | same |
| The built-in "Parent issue" and "Sub-issue progress" fields are hidden by default and can be enabled. "The "Parent issue" field can be used to group items", with the filter `parent-issue:"O/R#N"`. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-parent-issue-and-sub-issue-progress-fields |
| Iteration fields can be grouped and filtered (`@current`, `@next`). | https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-iteration-fields |
| Issue dependencies are not a project field. A board shows a "Blocked" icon, and the filters `is:blocked`, `is:blocking` and `blocked-by:` exist. Each relationship type allows up to 50 links. | https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies; https://github.blog/changelog/2025-08-21-dependencies-on-issues/ |
| A parent holds up to 100 sub-issues, nested up to 8 levels deep. Projects can "build views, filter, and group by parent issue". | https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues |

Not confirmed:
- **UNVERIFIED:** the exact default Status options. The docs use `Todo`, `In progress` and `Done` without listing the full set.
- **UNVERIFIED:** the limit on saved views.
- **UNVERIFIED:** whether adding a parent to a project also adds its sub-issues.

### Automation

| Fact | Source |
|---|---|
| Two workflows are on by default: closed issues or pull requests become **Done**, and merged pull requests become **Done**. Others set Todo when an item is added, close an issue when its status changes, auto-archive and auto-add. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations |
| Auto-add is "limited per plan": Free 1, Pro 5, Team 5, Enterprise 20. Existing matching items are not back-filled. Its filters are `is`, `label`, `reason`, `assignee` and `no`, with no parent qualifier. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically |
| A project holds up to 50,000 items, archived items included. Auto-archive filters are `is`, `reason` and `updated` only. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/archiving-items-automatically |
| Actions' `GITHUB_TOKEN` "cannot access projects". A user project needs a classic PAT with `project` (and `repo`). Fine-grained PATs cannot reach user-owned projects. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions; https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens |

Not confirmed:
- **UNVERIFIED:** the full list of built-in workflow names.
- **UNVERIFIED:** whether the auto-add limit follows the project owner's plan.
- **UNVERIFIED:** how many projects a personal account may own.
- No plan restriction on Projects features for private repositories was found beyond the auto-add count.

### CLI and API

| Fact | Source |
|---|---|
| `gh project` needs the `project` scope; to add it, run `gh auth refresh -s project`. Queries need `read:project`, mutations need `project`. | `gh project --help` (gh 2.99.0); https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects |
| `gh project item-edit <number> --owner <o> --url <issue url> --field Status --value "In Progress"` sets a single select by field name and option name, so no ID lookup is needed. "For non-draft issues, only a single field value can be updated per invocation." | `gh project item-edit --help` (gh 2.99.0) |
| `gh project item-list` takes `--limit` (default 30), `--query "<Projects filter>"`, `--format json` and `--jq`. `field-list` and `list` also default to 30. `item-add --url` adds an issue. `link` links a repository. | `gh project <sub> --help`; https://cli.github.com/manual/gh_project_item-list |
| GraphQL reads `ProjectV2.items { fieldValues { ... on ProjectV2ItemFieldSingleSelectValue { name } } }`, with page size 1 to 100. `updateProjectV2ItemFieldValue` moves an item and `addProjectV2ItemById` adds one; one call cannot do both. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects; https://docs.github.com/en/graphql/reference/projects; https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api |
| **This Mac's gh login** has scopes `gist`, `read:org`, `repo` and `workflow`, with **no `project` or `read:project`**. Every `gh project` call fails until Ryan refreshes it. | `gh auth status` (2026-10-14, token not printed) |

### Pull requests on a non-default base

| Fact | Source |
|---|---|
| Closing keywords "are interpreted only when the pull request targets the repository's *default* branch. If the pull request targets *any other branch* … merging the PR has no effect on the issues." | https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue |
| When a merged pull request's head branch is deleted, GitHub retargets every open pull request based on it to that pull request's base. | https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request; https://github.blog/changelog/2020-05-19-pull-request-retargeting |
| "Automatically delete head branches" deletes head branches after merge. The page does not exempt a branch that is the base of open pull requests (**UNVERIFIED** either way). | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches |
| `PUT /repos/O/R/pulls/N/update-branch` "Updates the pull request branch with the latest upstream changes by merging HEAD from the base branch". It returns 202 (accepted; the merge is queued and happens asynchronously) and 422 for "Validation failed, or the endpoint has been spammed", a head-sha mismatch included. **UNVERIFIED:** the response to a merge conflict. | https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request-branch |
| Protected branches and rulesets are unavailable on private repositories under GitHub Free, as design.md already records. | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches; https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets |
| rjeskuri/Harness has merge commits, squash and rebase allowed, and `deleteBranchOnMerge` true. | `gh repo view --json ...` (2026-10-14) |

## 2. The board

### Fields and views

One user project, "Harness board", owned by `@me`, holding issues from every registered repository. The alternative, one project per repository, multiplies setup and gains nothing. A single project also gives the cross-project view `/harness:standup` builds by hand today.

Fields:

| Field | Kind | Values |
|---|---|---|
| Status | built-in single select, used as the columns | Ready, In progress, In review, Blocked, Needs input, Done. These map 1:1 to the `status:*` labels; closed issues are Done. |
| Priority | single select | High, Normal. Mirrors `priority:high`. |
| Parent issue | built-in, enabled | the epic |
| Sub-issue progress | built-in, enabled | shown on epic cards |
| Repository | built-in | |

Fields left out:
- Size, risk and type stay labels. Labels cannot be lanes, and nobody lanes by size.
- No iteration field: there is no sprint cadence, and an iteration nobody maintains is noise.

Views:
- **Epics**: board, columns by Status, grouped by Parent issue, sliced by Repository. This is the swim-lane view Ryan asked for.
- **Priority**: board grouped by Priority.
- **Blocked**: table, filtered by `is:blocked`. Dependencies cannot be a lane, so they get a filter.
- **Done**: auto-archive Done items after 14 days, so lanes stay short. This is the `updated` filter.

### How `harness-board` reads and moves items

Labels stay the source of truth. Every reader keeps working unchanged:
- `ready`, `board` and `show`
- session_brief's board line
- standup and digest
- `harness-work dispatch` and `harness-pr awaiting`
- the tests, whose fakegh scenarios also stay as they are

The board is written, never read on the hot path:
- **Config**: `HARNESS_PROJECT=<owner>/<number>`, under `env` in `~/.claude/settings.json` (one project machine-wide, so not per repository). When it is unset, nothing below runs.
- **Create**: `epic create` and `task create` add the item with `gh project item-add <n> --owner <o> --url <issue url>`, then set Status Ready and, with `--priority`, Priority High. The harness adds items itself rather than using auto-add, because Free allows a single auto-add workflow and it does not back-fill.
- **Status**: `status N <s>` swaps labels as today, then calls `gh project item-edit <n> --owner <o> --url <issue url> --field Status --value "<option>"`. This is one call, by name, with no ID cache.
- **Close**: the built-in "closed → Done" workflow already covers `close`, `epic-check` and land.
- **Failures**: a failed project call is a warning on stderr, never a failed status change. This is principle 8: fail open.
- **Sync**: `harness-board project sync [--repo PATH | --all-projects]` reads `gh project item-list <n> --owner <o> --format json --limit 1000` plus the open issues. It adds missing items, rewrites Status and Priority where the board disagrees with the labels, and prints each change. It is the repair path after a scope lapse or a manual drag.
- **Dragging a card is overwritten by the next sync.** Two-way sync, where the board decides status, would make every label reader eventually consistent with a GraphQL view. That is the expensive design (C below), and its only gain is drag-to-change.

Cost to Ryan:
- Once: `gh auth refresh -s project` (interactive, adds the scope to the keyring login).
- Once: create the project and its fields and views in the web UI. The view layout (grouping, slicing) is set in the UI, and the CLI exposes no view editing; **UNVERIFIED** whether the API can create views.
- `harness-board project check` verifies the fields and options exist.

Options considered:
- **A. Today**: labels only. Free to run, but it has no swim lanes and the board exists only as `harness-board board` text.
- **B. Mirrored view (recommended)**: one call per status change, about two tickets of work, and a real kanban with epic lanes. Drift is possible and repaired by `sync`.
- **C. Projects as the source of truth**: replace the `status:*` labels with the Status field. This rewrites every reader to paginated GraphQL with item IDs, raises gh calls per board read from 1 to at least 2, and adds a hard dependency on the `project` scope, including for headless runs. It is a large, risky change that buys only drag-to-change status. Rejected.

## 3. Epic-level pull requests

### Lifecycle

1. **Opt in.** The planner, or Ryan, labels the epic `epic-branch`, a new flag. Epics without it run exactly as today.
2. **`harness-work epic start E`:**
   - Creates `e/E-<slug>` from `origin/main` and pushes it.
   - Opens a draft pull request from `e/E-<slug>` to `main`, titled `<epic title> (#E)`, with a body of `Closes #E` and the task list.
   - Comments on the epic.
   - Run by `/harness:plan` on approval, or lazily by the first `harness-work start` of a task.
3. **Task start.** `harness-work start N` for a task whose parent carries `epic-branch` cuts `t/N-<slug>` from `origin/e/E-<slug>`, which `git ls-remote` resolves by the `e/E-` prefix. Worktrees stay per task, as today.
4. **Red run.** `harness-red check` defaults its base to the epic branch, via the merge-base with `origin/e/E-*`. Today it is `origin/main`.
5. **Open.** `harness-pr open N` uses `--base e/E-<slug>`. The body keeps `Closes #N`, but GitHub ignores it on a non-default base. `ready` and `land` therefore take the ticket from the head branch `t/N-` when `closingIssuesReferences` is empty. `TICKET_BRANCH` already exists in harness-pr.
6. **Per-task review and CI are unchanged.** The reviewer, the findings gate, `checks --wait` and ultrareview for risk:high all run on the task pull request.
   - CI already runs: both `.github/workflows/ci.yml` and `templates/ci.yml` trigger on `pull_request:` with no branch filter.
7. **Task land into the epic branch.** `harness-pr land P` on a pull request whose base is `e/*`:
   - Squash-merges into the epic branch and removes the worktree and branch, as today.
   - Closes the ticket itself (`land_ticket` already does when GitHub has not).
   - Posts the Resolution with one added line, `Base: e/E-<slug>`, and the usual `Landed in <sha>`.
   - Calls `epic-check`.
   - Who runs this land is the main decision (D1).
8. **Epic complete.** With an `epic-branch` epic, `epic-check` no longer closes the epic. When all tasks are closed, it marks the epic pull request ready, sets the epic `status:in-review`, and notifies Ryan.
9. **Epic land.** `/harness:land <epic PR>` runs `harness-pr land` with three differences:
   - **Merge commit**: `gh pr merge --merge`, not `--squash`, for any head on `e/*` or `cp/*`. Each task's squash commit then reaches main with its sha intact, so every task's `Landed in <sha>` stays true on main. Squashing the epic would orphan those shas.
   - **Refusal**: it refuses while any open pull request has the epic branch as its base. Deleting the branch would retarget them to main (fact above), so an unfinished task would silently become a main-bound pull request.
   - **Epic Resolution**: it posts a Resolution on the epic that lists each task's Resolution title, squash sha and pull request. The epic closes through `Closes #E`, because that pull request targets main.
10. **Checkpoint.** `harness-pr checkpoint E [--name K]`:
    - Pushes the snapshot branch `cp/E-K` at the epic branch's tip and opens a pull request from it to main.
    - The snapshot lives outside the `e/` namespace on purpose. `start` and `harness-red` find the epic branch by the `e/E-` prefix, and a name like `e/E-<slug>-cpK` would also match that prefix.
    - Ryan lands the snapshot with a merge commit; auto-delete removes only the snapshot.
    - The checkpoint PR body closes nothing: no `Closes #E`, and no epic Resolution. It names the epic and the tasks it carries. The epic closes only through its own PR.
    - The epic branch keeps going. The next sync merges main back in, a trivial merge.
    - Merging the epic branch itself at a checkpoint would get it deleted by `deleteBranchOnMerge`, which is on in every scaffolded repository.
11. **Sync.** `harness-work epic sync E` brings main into the epic branch:
    - Calls `PUT repos/O/R/pulls/<epic PR>/update-branch` with `expected_head_sha` set to the tip it read. This is a server-side merge with no local checkout of `e/*`.
    - Runs before `epic start` hands out a task base, and before the epic pull request is marked ready.
    - **A 202 only queues the merge.** Sync then polls until `origin/e/E-<slug>` moves past the sha it sent, or until the PR's `mergeable` settles. It cuts no task base before one of those happens.
    - **A 422 is ambiguous.** It can mean a head-sha mismatch, the "endpoint has been spammed" rate limit, or a validation failure. Sync re-reads the tip and retries a bounded number of times.
    - **Only a confirmed conflict files the chore.** A conflict is confirmed when `mergeable` is `CONFLICTING` and the tip has not moved. Sync then files a `chore` ticket "Merge main into e/E-<slug>". That ticket is worked in a worktree on a `t/` branch based on the epic branch, and lands like any task.
    - **Anything else exits non-zero** and names the response, so the next sync tries again.
    - **UNVERIFIED:** the exact update-branch response on a conflict.

### What changes in each tool

| Piece | Change |
|---|---|
| `harness-board` | Adds the `epic-branch` flag to the label table. `epic-check` marks the epic pull request ready instead of closing, for flagged epics. `show --json` exposes `epic_branch`. |
| `harness-work` | New `epic start E` and `epic sync E`. `start N` bases on the epic branch when the parent is flagged. The worktree layout is unchanged. |
| `harness-red` | Default `--base`: the epic branch for a task of a flagged epic, else `origin/main`. |
| `harness-pr` | Changes to: <ul><li>`open`: `--base` from the ticket's epic.</li><li>`ready` and `land`: ticket from the head branch as a fallback.</li><li>`land`: merge method by base (squash into `e/*` or for a task to main; merge commit for an `e/*` or `cp/*` head into main) and the open-children refusal.</li></ul> Adds: <ul><li>the epic Resolution</li><li>`checkpoint`</li></ul> |
| `hooks/branch_guard.py` | `PROTECTED` becomes a predicate: `main`, `master`, `e/*` or `cp/*`. <ul><li>**Linked worktrees.** Today a linked worktree is exempt: `protected_repo` returns None when the git dir differs from the common dir ("its branch is never main"), and `holder` exempts `.claude/worktrees/`. That premise fails for `e/*`. The guard instead denies creating such a checkout: `git worktree add` naming an `e/*` or `cp/*` branch, and `git switch`/`git checkout` of one, in any repository. No flow needs one, because conflict chores run on `t/` branches.</li><li>**Checkouts that already exist.** As a second line, `protected_repo` judges a linked worktree whose own branch is `e/*` or `cp/*` by the main rules, instead of returning None. `holder` stops exempting a `.claude/worktrees/` path whose worktree is on such a branch. A bare `git push` from there is then denied as rule (b) denies it on main.</li><li>**Pushes.** Every push whose destination names `e/*` or `cp/*` is denied, on any branch, creation included. The harness tools create, push and merge these branches through their own subprocesses, which the hook never sees.</li></ul> Force pushes are already denied everywhere. |
| `hooks/tests_first.py`, `session_brief.py` | No change. Tasks are still on `t/` branches. |
| Skills | <ul><li>`plan`: asks per epic whether to use an epic branch (default no).</li><li>`work`: `start` hides the base. Under D1, the loop gains a step after `harness-pr ready`: `harness-pr land P` for a task PR whose base is `e/*`.</li><li>`land`: lists epic and checkpoint pull requests separately and says "merge commit".</li></ul> |
| `docs/design.md`, `user/CLAUDE.md` | <ul><li>The board section, the land section, and the interview note "no Projects board".</li><li>Under D1, three texts change together with the `work` skill, in ticket B4:<ul><li>design.md principle 3: "landing goes through a pull request the user merges"</li><li>the work-loop listing</li><li>user/CLAUDE.md "Never on main": "Only I merge" becomes "Only I merge to main; the harness merges task PRs into `e/*` after the gates".</li></ul></li></ul> |

## 4. Compared with today

| | Today: one PR per task to main | Epic branch: task PRs into `e/*`, one PR to main |
|---|---|---|
| Agent cost | One Fable review and one CI run per task | <ul><li>Same per task.</li><li>Plus epic PR runs. Every push to `e/*` fires `pull_request` (synchronize) on the open epic PR. That is one extra CI run per task landing (each squash into `e/*`) and one per sync that moves the tip: about 2N runs plus one per sync, in place of N.</li><li>Plus, optionally, one Fable review of the whole epic diff. For a 5-task epic that is up to about 1,500 lines, roughly 5x one task review's tokens.</li></ul> |
| Ryan's review load | N land decisions, each about 300 lines, with a ticket beside it | <ul><li>D1 yes (agents land tasks): 1 decision over the cumulative diff.</li><li>D1 no: N+1 decisions, which is more than today.</li></ul> The per-task PRs, reviews and Resolutions stay available to read. |
| Deploys and main churn | Every task reaches main and runs `deploy.yml` (push on main) | One deploy per epic or checkpoint. This is the real gain for live projects such as jotform-glow-sync. |
| Conflict risk | Low: tasks branch from fresh main. Parallel sibling tasks conflict with each other. | <ul><li>Siblings conflict exactly as today, against `e/*` instead of main.</li><li>New: the epic branch drifts from main for the life of the epic. Mitigated by `epic sync`, cheap for a solo repository with one or two live epics.</li><li>New: the retarget hazard, handled by the land refusal.</li></ul> |
| One bad task | Already on main, possibly deployed. Fix with a revert PR to main. | <ul><li>On `e/*` only. Fix with a revert or fix task into `e/*`; main is untouched.</li><li>A bad task found at the epic review is reverted on `e/*` before the epic lands.</li><li>Cost: head-of-line blocking. The good siblings wait for the bad one, unless a checkpoint lands them first.</li></ul> |
| Abandoning the work | Each landed task stays; a revert per task | Close the epic PR; main never saw it. Good tasks can be salvaged by cherry-picking their squash shas. |
| Tool complexity | Today's | About 300 to 500 more lines across harness-pr, harness-work, harness-board and branch_guard, plus their tests. |

**Verdict.** The epic flow pays off only when D1 is yes, or when batching deploys matters. For the Harness repository itself (no deploy, small tasks), per-task PRs remain the better fit. That is why the flow should be opt-in per epic, not the new default.

## Decisions for Ryan

- **D1. Task PRs into an epic branch: who merges them?**
  - Recommended: the harness, through `harness-pr land`, only when the review gate and CI are green, only into `e/*`, and never `--accept-findings`.
  - Alternative: Ryan keeps merging every PR, and epic PRs only batch deploys.
- **D2. The `project` scope.** Run `gh auth refresh -s project` once. Without it, the board part is off and `HARNESS_PROJECT` stays unset.
- **D3. Board drags.** The next sync overwrites a drag (recommended). The alternative is two-way sync, which is option C and is rejected above.
- **D4. Epic diff review.** Either add a Fable review of the whole epic PR before it is marked ready, or rely on the per-task reviews. Recommended: add it for risk:high epics only.

## 5. Tickets to implement it

These are not created on GitHub. Two epics, so the board part can ship without the epic-branch part.

### Epic A: Projects board as a mirrored view

| # | Title | Type | Size | Risk | Blocked by |
|---|---|---|---|---|---|
| A1 | harness-board mirrors status and priority to a configured Projects board | task | M | low | D2 |
| A2 | harness-board project sync and project check repair and verify the board | task | M | low | A1 |
| A3 | design.md and guide: the Projects board as a mirrored view | chore, no-tests | S | low | A1 |

- **A1.**
  - Read `HARNESS_PROJECT` (`owner/number`) from the environment, then `~/.claude/settings.json` `env`; when unset, do nothing.
  - `epic create` and `task create`: after the issue exists, call `gh project item-add <n> --owner <o> --url <url>`, then `item-edit --field Status --value Ready`. With `--priority`, also set `--field Priority --value High`.
  - `status`: after the label swap, call `item-edit --field Status --value <mapped option>`.
  - Every project call warns on failure and never changes the exit code.
  - Tests: extend the fakegh scenarios to cover the calls in order, a project failure that still exits 0, and no project calls when the setting is unset.
- **A2.**
  - `project check`: `gh project field-list` must show Status with the six options and Priority with High and Normal. Prints what is missing.
  - `project sync [--all-projects]`: reads `item-list --format json --limit 1000` and each repository's open issues. Adds missing items and fixes Status and Priority from the labels.
  - Output and dry-run: prints one line per change; `--dry-run` changes nothing.
  - Tests: drift in each direction, a missing item, and the `--dry-run` output.
- **A3.**
  - Replace "no Projects board" in the interview decisions and the board section with the mirrored-view design.
  - Document the one-time UI setup: fields, the Epics, Priority and Blocked views, and auto-archive.
  - Document the scope refresh, and that labels stay authoritative.

### Epic B: Epic-level pull requests (opt-in)

| # | Title | Type | Size | Risk | Blocked by |
|---|---|---|---|---|---|
| B1 | branch_guard protects e/* epic branches like main | task | M | high | — |
| B2 | harness-work epic start and epic sync create and refresh an epic branch and its draft PR | task | M | low | B1 |
| B3 | Tasks of an epic-branch epic branch from, test against and open PRs into the epic branch | task | M | low | B2 |
| B4 | harness-pr land merges task PRs into the epic branch and readies the epic PR when all tasks land | task | M | high | B3, D1 |
| B5 | harness-pr lands an epic PR with a merge commit and checkpoints through snapshot branches | task | M | high | B4 |
| B6 | Skills, user CLAUDE.md and design.md describe the epic-branch flow | chore, no-tests | S | low | B5 |

- **B1.**
  - `PROTECTED` becomes a match on `main`, `master`, `e/` plus anything, or `cp/` plus anything.
  - Deny checking out a protected branch, in any repository:
    - `git worktree add` naming an `e/*` or `cp/*` branch
    - `git switch` or `git checkout` of one
  - Judge a linked worktree on `e/*` or `cp/*` by the main rules:
    - `protected_repo` returns that worktree's top-level and branch, instead of None, when its own branch is protected and not main or master.
    - `holder` no longer exempts a `.claude/worktrees/` path whose worktree is on such a branch.
  - Deny every push whose destination names `e/*` or `cp/*`, creation included. The harness tools push through their own subprocesses.
  - `harness-work worktree remove` refuses `e/*` and `cp/*`.
  - Tests:
    - The main cases mirrored for an `e/3-x` checkout: edit, commit, `sed -i`, redirect and bare push denied.
    - The same in a linked worktree on `e/3-x`, created with plain `git` in the test so the guard is not in the way. This covers a bare `git push` and a write to a path under `.claude/worktrees/e-3-x/`.
    - `git worktree add <path> e/3-x` and `git switch e/3-x` denied.
    - `git push origin HEAD:e/3-x` and `git push -u origin e/3-x` denied from a `t/` branch.
    - `t/` worktrees and their pushes unaffected.
- **B2.**
  - Add the `epic-branch` flag to `labels ensure`, and expose `epic_branch` in `show --json`.
  - `epic start E`:
    - Refuses an epic without the flag.
    - Creates `e/E-<slug>` from `origin/main` and pushes it.
    - Opens a draft PR to main with `Closes #E`.
    - Comments on the epic.
    - Is idempotent: reuses an existing branch and PR.
  - `epic sync E`:
    - Calls `update-branch` on the epic PR with `expected_head_sha`.
    - After a 202, polls until the tip moves or `mergeable` settles.
    - Retries a 422 after re-reading the tip.
    - Files the conflict chore only on a confirmed conflict: `CONFLICTING` with an unmoved tip.
    - Exits non-zero otherwise.
  - Tests: run against a temporary origin and fakegh.
- **B3.**
  - `harness-work start N`:
    - Bases on `origin/e/<parent>-*` when the parent is flagged.
    - Runs `epic start` first when the branch is missing.
  - `harness-red check`: defaults its base to that branch.
  - `harness-pr open`: `--base` the epic branch.
  - `harness-pr ready`: falls back to the ticket from the head branch when `closingIssuesReferences` is empty.
  - Tests: the base used by each tool, and the ready fallback.
- **B4.**
  - `land` on a PR whose base is `e/*`:
    - Squash-merges.
    - Closes the ticket explicitly.
    - Posts the Resolution with a `Base:` line.
    - Calls `epic-check`.
  - `epic-check` on a flagged epic with all tasks closed:
    - Marks the epic PR ready, sets `status:in-review` and notifies, instead of closing.
  - If D1 is yes, three texts change in this same ticket, so the merge rule never says two things at once:
    - The `work` skill lands the task PR itself after `ready`, only for an `e/*` base.
    - `user/CLAUDE.md` "Never on main": "Only I merge" becomes "Only I merge to main".
    - design.md principle 3 and the work-loop listing.
  - Risk is high because it changes who merges.
- **B5.**
  - `land` on a PR whose head is `e/*` or `cp/*`: uses `--merge`.
  - `land` on a PR whose head is `e/*` also:
    - Refuses while any open PR has that head as its base, naming them.
    - Posts the epic Resolution, aggregating each task's newest Resolution (title, squash sha, PR).
  - `checkpoint E --name K`:
    - Pushes `cp/E-K` at the tip.
    - Opens a PR to main whose body names the epic and the tasks it carries, and closes nothing (no `Closes #E`).
    - Its land posts no epic Resolution and leaves the epic open.
  - Tests:
    - The merge method chosen by base and head.
    - The retarget refusal.
    - The Resolution text.
    - A checkpoint body without `Closes`.
    - `start` and `harness-red` resolve `e/E-` and not `cp/E-K`.
- **B6.**
  - The `plan` skill asks about epic branches per epic.
  - The `land` skill lists epic PRs and checkpoints.
  - The D1 texts (the `work` skill, user/CLAUDE.md, design.md principle 3) ship with B4, not here.
  - `docs/design.md` covers the board, the tools and the interview-decision update.

## Unverified

- The default Status options of a new project.
- The full list of built-in workflow names.
- Whether adding a parent adds its sub-issues.
- The saved-view limit.
- The project count limit for a personal account.
- Whether the auto-add limit follows the owner's plan.
- Whether views can be created through the API.
- Whether "Automatically delete head branches" spares a branch that is the base of open PRs. The design avoids relying on it either way.
- The `update-branch` response on a merge conflict.
- Ryan's account plan: `gh api user --jq .plan.name` printed nothing with the current scopes. The Free assumption comes from design.md, which recorded 403s on rulesets.
- Every `gh project` behavior is quoted from local `--help` and the docs. None was exercised, because the token lacks the `project` scope.
