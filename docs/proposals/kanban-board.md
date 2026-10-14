# Proposal: a GitHub Projects kanban board

Ticket #54, epic #52. Written 2026-10-14 against gh 2.99.0 and the code on main at a6df5e2.
- Every platform fact was read on 2026-10-14 from the URL cited; docs.github.com pages carry no last-updated date.
- Items marked **UNVERIFIED** could not be confirmed from a primary source.
- Nothing was created or changed on GitHub.

Until 2026-10-14 this file was `docs/proposals/kanban-and-epic-prs.md`. It was renamed when the epic-level pull request half was parked (see the last section).

## Decisions

Ryan decided on #54 on 2026-10-14:
- **Scope: build only the board** (Epic A below), opt-in through `HARNESS_PROJECT`. The board is a GitHub Projects view that mirrors the status labels, with swim lanes by epic.
- **Epic pull requests: parked.** See the last section.
- **D2, the `project` scope.** Ryan runs `gh auth refresh -s project` himself before the board goes live. Until then, `HARNESS_PROJECT` stays unset and the harness behaves exactly as today.
- **D3, board drags: the default stands.** Labels are the source of truth, and the next `project sync` overwrites a card dragged on the board.

## Recommendation

**Add one user-owned Projects board as a mirrored view; labels stay the source of truth.**
- **Layout.** The board spans all registered repositories. Status is the column field. The lanes are grouped by Parent issue (the epic), with Priority and Repository as alternate views.
- **Writes.** `harness-board` writes Status on every status change, one `gh project item-edit` call each.
- **Reads.** It reads the board only in `project sync`, which repairs drift.
- **Unchanged.** No reader of labels changes: `ready`, `board`, `show`, session_brief, standup, digest, dispatch and `harness-pr awaiting` all stay as they are.
- **Rollback.** Unsetting `HARNESS_PROJECT` turns the whole thing off.

Options considered:
- **Today: labels only.** Free to run, but it has no swim lanes, and the board exists only as `harness-board board` text.
- **Mirrored view (chosen).** One gh call per status change, three tickets, and a real kanban with epic lanes. Drift is possible and repaired by `sync`.
- **Projects as the source of truth: rejected.** It replaces the `status:*` labels with the Status field. Every reader would move to paginated GraphQL with item IDs, the gh calls per board read would go from 1 to at least 2, and the `project` scope would become a hard dependency, headless runs included. It is a large, risky change that buys only drag-to-change status.

## Platform facts

### Fields and lanes

| Fact | Source |
|---|---|
| Up to 50 fields per project, built-in ones included. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects |
| Custom field types are single select, multi select, iteration, number, date and text. Single select allows up to 50 options and a default option for new items. | https://docs.github.com/en/graphql/reference/projects; https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-single-select-fields |
| Board columns can only be "any single select or iteration field". Dragging a card to another column sets that value. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-board-layout |
| Horizontal groups (swim lanes) can use a custom field value. "You cannot group by title, labels, reviewers, or linked pull requests." | same |
| The built-in "Parent issue" and "Sub-issue progress" fields are hidden by default and can be enabled. "The "Parent issue" field can be used to group items." | https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-parent-issue-and-sub-issue-progress-fields |
| Issue dependencies are not a project field. "You can view blocked issues in your projects and in the repository Issues tab", and the filters `is:blocked` and `blocked-by:` find them. | https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies; https://github.blog/changelog/2025-08-21-dependencies-on-issues/ |
| A parent holds up to 100 sub-issues, nested up to 8 levels deep. Projects can "build views, filter, and group by parent issue". | https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues |

### Automation and limits

| Fact | Source |
|---|---|
| Two workflows are on by default: closed issues or pull requests become **Done**, and merged pull requests become **Done**. Auto-archive and auto-add also exist. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations |
| Auto-add is "limited per plan": Free 1, Pro 5. It does not back-fill existing items. Its filters have no parent qualifier. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically |
| A project holds up to 50,000 items, archived items included. Auto-archive filters on `is`, `reason` and `updated`. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/archiving-items-automatically |
| Actions' `GITHUB_TOKEN` "cannot access projects". Fine-grained PATs cannot reach user-owned projects; a classic PAT with `project` can. This only matters if the mirror ever moves into CI, which this proposal does not do. | https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions; https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens |

### CLI

| Fact | Source |
|---|---|
| `gh project` needs the `project` scope; to add it, run `gh auth refresh -s project`. Queries alone need only `read:project`. | `gh project --help` (gh 2.99.0); https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects |
| `gh project item-edit <number> --owner <o> --url <issue url> --field Status --value "In Progress"` sets a single select by field and option name, so no ID lookup is needed. It sets one field per call for issues. | `gh project item-edit --help` (gh 2.99.0) |
| `gh project item-list` takes `--limit` (default 30), `--query`, `--format json` and `--jq`. `field-list` also defaults to 30. `item-add --url` adds an issue. | `gh project <sub> --help`; https://cli.github.com/manual/gh_project_item-list |
| GraphQL can read items with field values (`ProjectV2ItemFieldSingleSelectValue`, at most 100 per page) and move them (`updateProjectV2ItemFieldValue`). The CLI covers everything this proposal needs. | https://docs.github.com/en/graphql/reference/projects; https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api |
| This Mac's gh login has the scopes `gist`, `read:org`, `repo` and `workflow`, with **no `project` scope**. | `gh auth status` (2026-10-14, token not printed) |

## The board

One user project owned by `@me`, holding issues from every registered repository. Per-repository projects would multiply the setup for no gain, and one project gives the cross-project view that `/harness:standup` assembles by hand today.

| Field | Kind | Values |
|---|---|---|
| Status | built-in single select, used as the columns | Ready, In progress, In review, Blocked, Needs input, Done. These map 1:1 to the `status:*` labels; closed issues are Done. |
| Priority | single select | High, Normal. Mirrors `priority:high`. |
| Parent issue | built-in, enabled | the epic |
| Sub-issue progress | built-in, enabled | shown on epic cards |
| Repository | built-in | |

Fields left out:
- **Size, risk and type** stay labels. Labels cannot be lanes, and nobody lanes by size.
- **Iteration:** none. There is no sprint cadence, and an iteration field nobody maintains is noise.

Views:
- **Epics** (board): columns by Status, grouped by Parent issue, sliced by Repository. These are the swim lanes Ryan asked for.
- **Priority** (board): grouped by Priority.
- **Blocked** (table): filtered by `is:blocked`. Dependencies cannot be a lane, so they get a filter.
- **Auto-archive:** Done items are archived after 14 days, so the lanes stay short.

## How harness-board writes and syncs

- **Config.** `HARNESS_PROJECT=<owner>/<number>`, read from the process environment, then from `env` in `~/.claude/settings.json`. It is one setting for the whole machine, not per repository. harness-board reads no settings file today, so A1 adds that reader. When the setting is unset, nothing below runs.
- **Create.** `epic create` and `task create` add the issue with `gh project item-add <n> --owner <o> --url <issue url>`, then set Status to Ready. `task create --priority` also sets Priority to High; `epic create` has no `--priority`, so epics get no Priority write.
  - The harness adds items itself rather than relying on auto-add, because Free allows a single auto-add workflow and it does not back-fill existing issues.
- **Status.** The shared `set_status` helper (behind `status` and `needs-input`, and `harness-work start` through them) swaps the labels as today. It then calls `gh project item-edit <n> --owner <o> --url <issue url> --field Status --value "<option>"`: one call, by name, with no ID cache.
- **Close.** The built-in "closed → Done" workflow covers `close`, `epic-check` and `harness-pr land`. `harness-pr` itself does not change.
- **Failure.** A failed project call is one warning on stderr per process and never fails the status change (principle 8: fail open).
- **Sync.** `harness-board project sync [--repo PATH | --all-projects] [--dry-run]` is the repair path, used once at go-live to back-fill, after a scope lapse, or after a drag on the board.
  - It reads `gh project item-list <n> --owner <o> --format json --limit 1000` plus each repository's open issues.
  - It adds missing items and rewrites Status and Priority where the board disagrees with the labels.
  - It prints one line per change, and warns when the item count reaches the limit.
- **Check.** `harness-board project check` verifies that Status has the six options and Priority has High and Normal, by name. Mirroring by name breaks when an option is renamed in the UI, and the check catches that.

## Tickets

These are not yet created on GitHub. They form one epic, "Projects board as a mirrored view".

| # | Title | Type | Size | Risk | Blocked by |
|---|---|---|---|---|---|
| A1 | harness-board mirrors status and priority to a configured Projects board | task | M | low | none to build. Going live needs D2 (Ryan's scope refresh). |
| A2 | harness-board project sync and project check repair and verify the board | task | M | low | A1 |
| A3 | design.md and guide: the Projects board as a mirrored view | chore, no-tests | S | low | A1 |

**A1. Mirror status and priority.**
- Add a reader for `HARNESS_PROJECT`: the environment first, then `env` in `~/.claude/settings.json`. Unset means no project calls at all.
- `epic create` and `task create`: once the issue exists, call `item-add --url`, then `item-edit --field Status --value Ready`. `task create --priority` also sets `--field Priority --value High`; epics get no Priority write (`epic create` has no `--priority`).
- `set_status`: after the label swap, call `item-edit --field Status --value <option>`. The status keys map to the options: ready to Ready, in-progress to In progress, in-review to In review, blocked to Blocked, needs-input to Needs input.
- A failed project call warns once and leaves the exit code unchanged.
- Tests (`tests/test_board.py`, fakegh):
  - With the setting, the gh calls come in order: create, then add, then edit Status, then Priority.
  - `status` and `needs-input` each make one edit, with the mapped option.
  - A failing project call still exits 0 and prints one warning.
  - Without the setting, no `gh project` call happens. Every existing scenario stays unchanged, which proves the readers are untouched.

**A2. Sync and check.**
- `project check`: `gh project field-list <n> --owner <o> --format json`. It prints the fields and options that are missing and exits 1 when any are.
- `project sync`:
  - Reads `item-list --format json --limit 1000` and the open issues of `--repo` (default: the current repo) or of every registered project with `--all-projects`.
  - Adds missing items and fixes Status and Priority from the labels.
  - Leaves closed issues to the built-in workflow.
  - Prints one line per change. `--dry-run` prints the same lines and changes nothing.
- Tests:
  - A missing item is added.
  - A Status drift and a Priority drift are each corrected.
  - An item already in agreement makes no call.
  - `--dry-run` makes no write calls.
  - `check` passes on complete fields, and reports a missing option.

**A3. Docs.**
- In `docs/design.md`, replace "no Projects board" (the interview decisions and The board) with the mirrored-view design, adding `HARNESS_PROJECT` to the sentence listing the machine-wide settings under `env` in `~/.claude/settings.json` (after the per-project table), and the new subcommands under harness-board.
- In `docs/guide.md`, cover:
  - the one-time setup: `gh auth refresh -s project`, then create the project, the Priority field, the Epics, Priority and Blocked views and auto-archive in the web UI
  - setting `HARNESS_PROJECT`
  - running `harness-board project check` and `project sync --all-projects` once to back-fill
- State that labels stay authoritative and that a drag is overwritten by the next sync (D3).

## Cost and risks

**Cost:**
- **Per create:** two or three more gh calls.
- **Per status change:** one more gh call. Estimate, not measured: about a second each, since every gh call is a process start plus an API round trip.
- **Readers:** no change.
- **Rate limits:** negligible at a solo developer's ticket volume.
- **Ryan's one-time setup:** the scope refresh plus about 15 minutes in the web UI. **UNVERIFIED:** whether views can be created through the API, so the setup is documented as manual.

**Risks:**
- **Drift.** A failed call, a drag, or an issue changed outside the harness leaves the board wrong until the next `project sync`. Since nothing reads the board, drift is cosmetic.
- **Scope lapse.** If the `project` scope is lost, every status change prints a warning until it is refreshed. Work continues.
- **Broader token.** `project` grants write access to all of Ryan's projects for the keyring login, which headless runs also use. The harness writes only to the configured project.
- **Option renames.** Renaming an option in the UI breaks mirroring by name. `project check` reports it.
- **Item limit.** 50,000 items per project, archived items included. Auto-archive keeps the active set small, and sync warns when it reaches its own read limit of 1,000.

## Unverified (relevant to the board)

- The default Status options of a new project. The six options are set by hand anyway.
- The full list of built-in workflow names.
- Whether adding a parent to a project also adds its sub-issues. The harness adds each issue itself.
- The limit on saved views, and on projects per personal account.
- Whether the auto-add limit follows the project owner's plan. It is not used.
- Whether views can be created through the API.
- Ryan's account plan. Free is assumed from design.md; nothing here depends on a paid plan.
- No `gh project` command was exercised, because the token lacks the `project` scope. Their behavior is quoted from `--help` and the docs. A1's tests use fakegh, so the first real run is at go-live.

## Parked: epic-level pull requests

**The idea.** Task branches would cut from and merge into an epic branch `e/<epic>-<slug>`, and Ryan would land one pull request to main per epic or per named checkpoint, with a merge commit so each task's squash sha reaches main. The gains:
- One review for Ryan instead of one per task, if agents may merge task PRs into the epic branch.
- Batched deploys for live projects.
- A bad task kept off main.

**Why it was parked.** Each of five Fable review rounds on this proposal found new lifecycle edge cases: the epic PR's existence and timing, ancestry lost to squashed sync merges, checkpoint branches reachable from main, sync races and leftovers, and review bases. It also needed a change to who merges. The cases still open at parking:
- the ordering of the B4 and B5 checks
- the review scope of a merge chore
- the open-chore lookup under the sync lock
- that the CI-cost baseline is 2N runs, not N
- the test-author and coder prompts for merge chores

The board does not depend on any of it. The full design (lifecycle, tool changes, guard rules, comparison, tickets B1 to B6 and the scratch-repo experiments) is in this file's git history at commit 422880b, under its old name `docs/proposals/kanban-and-epic-prs.md`.
