---
name: land
description: Merge a reviewed pull request from the CLI - show the PR summary, the review findings and the checks, confirm once, squash-merge, remove the worktree and branch, close the ticket. With no number, list the PRs awaiting the user. Use when the user runs /harness:land or asks to merge a PR.
argument-hint: "[PR# [--discard] [--accept-findings]]"
disable-model-invocation: true
allowed-tools:
  - Bash(harness-pr:*)
  - Bash(harness-board:*)
  - Bash(gh pr view:*)
  - Bash(gh pr diff:*)
---
# Land

Arguments: $ARGUMENTS

Awaiting you:
!`harness-pr awaiting 2>/dev/null`

- **No number**: show the list above (number, title, ticket, age, checks, unresolved threads) and stop. Suggest `/harness:land <n>`.
- **A number**:
  1. Summarize in at most 15 lines: title and ticket, the definition-of-done checklist from the PR body (`gh pr view P --json body,title`), the review summary and unresolved threads (`harness-pr comments P --unresolved`), checks state (`harness-pr checks P`), files changed (`gh pr diff P --stat`), the risks line.
  2. Ask once with AskUserQuestion: merge, show the diff first, or stop. "Show the diff": print `gh pr diff P` in readable chunks and ask again.
  3. On merge: `harness-pr land P` (add `--discard` only when the user passed it, to drop a dirty worktree). It merges, removes the worktree and branch, fast-forwards main, closes the ticket and checks the epic. It refuses a PR whose latest harness review has findings and no commit after it, naming them: show them and stop. Add `--accept-findings` only when the user passed it.
  4. Report the merge commit, the ticket closed, the epic state, and what is awaiting next. On a billable project remind the user of `/harness:worklog`.

Never merge without the confirmation, never force, never bypass red checks. The user lands every PR, with `/harness:land` or by approving the land prompt; `harness-pr autoland` for eligible ones (risk:low, size S or M, review and checks green at head, no protected path) is the epic #132 plan.
