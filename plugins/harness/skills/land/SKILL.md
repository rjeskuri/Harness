---
name: land
description: Merge a reviewed pull request the work loop left at ready (a risk:high ticket's, or one it could not land) from the CLI - show the PR summary, the review findings and the checks, confirm once, squash-merge, remove the worktree and branch, close the ticket. With no number, list the PRs awaiting the user. Use when the user runs /harness:land or asks to merge a PR.
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

For the pull requests `/harness:work` leaves at ready: a `risk:high` ticket's, one with no risk label, and one whose merge of main or `harness-pr land` the loop could not finish and reported. A `risk:low` ticket's PR lands at the end of its own `/harness:work` run.

Awaiting you:
!`harness-pr awaiting 2>/dev/null`

- **No number**: show the list above (number, title, ticket, age, checks, unresolved threads) and stop. Suggest `/harness:land <n>`.
- **A number**:
  1. Summarize in at most 15 lines: title and ticket, the definition-of-done checklist from the PR body (`gh pr view P --json body,title`), the review summary and unresolved threads (`harness-pr comments P --unresolved`), checks state (`harness-pr checks P`), files changed (`gh pr diff P --stat`), the risks line.
  2. Ask once with AskUserQuestion: merge, show the diff first, or stop. "Show the diff": print `gh pr diff P` in readable chunks and ask again.
  3. On merge: `harness-pr land P` (add `--discard` only when the user passed it, to drop a dirty worktree). It merges, removes the worktree and branch, fast-forwards main, closes the ticket and checks the epic. It refuses a PR whose head is behind main, whose latest harness review does not cover the head (only clean merges of main may follow it), or whose latest harness review has high or medium findings at the head, naming them: show the refusal and stop. Add `--accept-findings` only when the user passed it.
  4. Report the merge commit, the ticket closed, the epic state, and what is awaiting next. On a billable project remind the user of `/harness:worklog`.

Never merge without the confirmation and never bypass red checks; `--discard` and `--accept-findings` only when the user passed them. `harness-pr autoland` for eligible ones is the epic #132 plan.
