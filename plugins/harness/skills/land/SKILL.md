---
name: land
description: Land a pull request the work loop parked, once the user chooses to - harness-pr land --catch-up with no confirmation question (catch up with main, wait for the checks, squash-merge, remove the worktree and branch, close the ticket), then report. With no number, list the PRs awaiting the user. Use when the user runs /harness:land or asks to merge a PR.
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

For the pull requests of tickets the work loop parked, which the user chooses to land. Every other PR lands at the end of its own `/harness:work` run, at any risk.

Awaiting you:
!`harness-pr awaiting 2>/dev/null`

- **No number**: show the list above (number, title, ticket, age, checks, unresolved threads) and stop. Suggest `/harness:land <n>`.
- **A number**: the user's choice is the approval; ask no confirmation question.
  1. Run `harness-pr land P --catch-up` as a plain command (add `--discard` only when the user passed it, to drop a dirty worktree, and `--accept-findings` only when the user passed it), with `timeout: 600000` on the Bash call. While the branch is behind main it merges origin/main into it when that merge is clean, pushes and waits for the checks; then it merges, removes the worktree and branch, fast-forwards main, closes the ticket, checks the epic and runs the canary. Exit 8 (checks still pending at its deadline): run the same command again, at most three times.
  2. A refusal (exit 1, nothing merged): show its reason and stop. A conflict with main, red checks, a head no harness review covers (`review the head again`) or open high or medium findings are the loop's to fix: say that `harness-board status N ready` unparks the ticket and `/harness:work N` resumes from its branch and PR (the high and medium findings to the coder, CI, a review of the new head, ready, land); `harness-board status N in-review` instead sends the owner's unresolved threads to the coder first.
  3. Report the merge commit, any canary or dirty-worktree lines, the ticket closed, the epic state, and what is awaiting next. On a billable project remind the user of `/harness:worklog`.

Never bypass red checks; `--discard` and `--accept-findings` only when the user passed them.
