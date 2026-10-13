---
name: plan
description: Turn a request into an epic and tasks on the project's GitHub board - interview the user, have the planner decompose the work into tickets with definitions of done and the tests that must pass, show the proposal, create the issues. Use when the user runs /harness:plan or asks to plan, spec or break down a piece of work.
argument-hint: "[what you want to build or fix]"
disable-model-invocation: true
---
# Plan

The request: $ARGUMENTS

Board today:
!`harness-board board 2>/dev/null | head -40`

You orchestrate; `harness:planner` (Opus, xhigh) decomposes; `harness-board` creates. This skill writes issues, never code.

1. **Read** enough of the code to ask informed questions: the modules this touches, how that area is tested, the verify and test commands (CLAUDE.md, `.claude/settings.json` env). If that means more than a handful of files, delegate the reading to an Explore agent and keep its conclusions.
2. **Interview** the user with AskUserQuestion, a few questions at a time. Skip what the code already answers. Spend the questions on what they may not have considered: data shapes and edge cases, failure behavior, what must not change, how they will know it works, what is out of scope, risk (data, money, live systems, credentials), and whether anything needs this Mac's files or credentials.
3. **Decompose**: hand the request, the answers and the code pointers to `harness:planner`. It returns JSON: the epic, the tasks with bodies in the ticket templates, type, size, risk, flags, blocked_by indexes, and open questions.
4. **Show** the proposal as a compact table (title, type, size, risk, tier, blocked by, number of tests) and the open questions. Ask the user to approve, trim or change it with AskUserQuestion. Resolve open questions with them; one nobody can answer goes under the epic's Decisions and open questions, and the task that depends on it is created with `--blocked-by` on the task that will answer it or marked `status:blocked`.
5. **Create** on approval: write each body to a file in the scratchpad; `harness-board epic create --title ... --body-file ... --size ... --risk ...`; then each task with `harness-board task create --parent <epic#> --title ... --body-file ... --type ... --size ... --risk ... [--no-tests] [--needs-local] [--priority] [--blocked-by <real issue numbers>]`, in dependency order so the numbers exist.
6. **Report** the epic URL and the task numbers, then the next step: `/harness:work` to dispatch every ready ticket, or `/harness:work <n>` to run one here.

If `harness-board` reports no remote or no `gh` login, stop and suggest `/harness:new-project`.
