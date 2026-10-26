---
name: plan
description: Turn a request into an epic and tasks on the project's GitHub board - interview the user, have the planner decompose the work into tickets with definitions of done and the tests that must pass, create them as status:proposed and show them. Use when the user runs /harness:plan or asks to plan, spec or break down a piece of work.
argument-hint: "[--no-interview] [what you want to build or fix]"
disable-model-invocation: true
---
# Plan

The request: $ARGUMENTS

Board today:
!`harness-board board 2>/dev/null | head -40`

You orchestrate; `harness:planner` (Opus, xhigh) decomposes; `harness-board` creates. This skill writes issues, never code.

1. **Read** enough of the code to ask informed questions: the modules this touches, how that area is tested, the verify and test commands (CLAUDE.md, `.claude/settings.json` env). If that means more than a handful of files, delegate the reading to an Explore agent and keep its conclusions.
2. **Interview** the user with AskUserQuestion (skip this step when the arguments carry `--no-interview`, for a small request), a few questions at a time. Skip what the code already answers. Spend the questions on what they may not have considered: data shapes and edge cases, failure behavior, what must not change, how they will know it works, what is out of scope, risk (data, money, live systems, credentials), and whether anything needs this Mac's files or credentials.
3. **Decompose**: hand the request, the answers and the code pointers to `harness:planner`. It returns JSON: the epic, the tasks with bodies in the ticket templates, type, size, risk, flags, blocked_by indexes, and open questions.
4. **Create** right after the planner returns, every issue as `status:proposed`, with no question first: write each body to a file in the scratchpad (the planner's open questions go under the epic's Decisions and open questions); `harness-board epic create --title ... --body-file ... --size ... --risk ... --proposed`; then each task with `harness-board task create --parent <epic#> --title ... --body-file ... --type ... --size ... --risk ... [--no-tests] [--needs-local] [--priority] [--blocked-by <real issue numbers>] --proposed`, in dependency order so the numbers exist. A task that depends on an open question is created with `--blocked-by` on the task that will answer it. `harness-board ready` and dispatch never list a proposed ticket, and `harness-work start` refuses one.
5. **Show** the created issues as a compact table (issue number, title, type, size, risk, tier, blocked by, number of tests), the epic URL and the open questions, then end the turn naming the user's next step: read the plan, change or close what is wrong on the board, then `harness-board promote <epic#>` makes the epic and its tasks ready.

If `harness-board` reports no remote or no `gh` login, stop and suggest `/harness:new-project`.
