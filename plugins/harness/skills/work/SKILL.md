---
name: work
description: Run a ticket from the board to a ready pull request - worktree, tests first with a red proof and an adversarial check, implementation, fresh-context review, PR with inline findings, CI - or, with no ticket number, dispatch a session per ready ticket. Use when the user runs /harness:work.
argument-hint: "[issue# | --max N | --venue headless|bg | --dry-run]"
disable-model-invocation: true
---
# Work

Arguments: $ARGUMENTS

Ready tickets:
!`harness-board ready 2>/dev/null | head -20`

## No ticket number: dispatch
Run `harness-work dispatch $ARGUMENTS`. With more than three ready tickets, run it with `--dry-run` first and show the plan. It starts a worktree per ready ticket and launches a headless run billed to the API key, or a background session on the subscription when the API budget is exhausted. Report what was launched, and that notifications arrive when a PR is ready or a ticket needs input. Do not work tickets in this session when dispatching.

## One ticket: the loop
You orchestrate; agents do the work; the tools keep the record. Work only in the worktree path that `harness-work start` prints, never in the main checkout. Pass `model: <tier>` on every Agent call: the tier from `harness-board tier N`, or one tier up on escalation (sonnet, opus, fable; and `effort: xhigh` for fable once the Agent tool offers effort). After every stage run `harness-work attempt N <stage> pass|fail` and do what it says: `retry` (same tier), `escalate <tier>`, or `needs-input` (go to the last paragraph).

1. **Start**: `harness-work start N --json`, then read the ticket context it prints (request, outline, definition of done, tests, context, the epic's goal, blockers, recent comments). A ticket in `status:needs-input`: `harness-board answers N` gives the owner's newest comment; that is the answer, continue with it. A ticket in `status:in-review` with a PR: go to step 10.
2. **Tests** (`harness:test-author`, stage `tests`): the context, the worktree, the test command (`HARNESS_TEST_CMD` from `.claude/settings.json`, or the detected default), and the ask for broad behavior tests first. Then `harness-red check --worktree <path>`. Red: continue. Green or no tests: the stage failed; send the author the tool's output.
3. **Break** (`harness:test-breaker`, stage `breaker`): `harness-red scratch create --worktree <path>`; give it the scratch path, the test files, the definition of done and the test command. `cheat passed`: back to the author with the breaker's suggestions (step 2 again, including `harness-red check`), at most two rounds, then continue and note it for the PR. `harness-red scratch remove` either way.
4. **Code** (`harness:coder`, stage `code`): the context, worktree, test files, red run, test command and `CLAUDE_VERIFY_CMD`. It commits when green. "A test looks wrong": back to the author with the coder's reasoning, one round, then red again. "Outgrown the size cap": `harness-board needs-input` with the split it proposes, and stop.
5. **Review** (`harness:reviewer`, stage `review`): worktree, ticket context, red and verify output. Save its JSON array to a file in the scratchpad. High or medium findings: the coder fixes them (same tier, at most two rounds) and the reviewer re-checks the fixes.
6. **PR**: `harness-pr open N --worktree <path> --verify-log <file> --review <findings.json> --json`, then `harness-pr review <P> --findings <findings.json>` so the findings and their fixes are visible on GitHub.
7. **CI** (stage `ci`): `harness-pr checks <P> --wait`. Failures: the coder fixes them, pushes with `git push` in the worktree, check again; at most two rounds.
8. **Ultrareview**, only when the ticket is `risk:high`: `harness-pr ultra <P> --post`. High findings go to the coder once.
9. **Ready**: `harness-pr ready <P>`. It sets `status:in-review` and notifies the user. Stop.
10. **Revise** (already in review): `harness-pr comments <P> --unresolved`; the owner's comments are the coder's task; push; `harness-pr checks <P> --wait`; answer each thread with what changed (`gh pr comment <P>` with a short list). Stop.

When a stage reaches `needs-input`, or an agent reports a question the ticket, the epic and the code cannot answer: `harness-board needs-input N --question "<the question or blocker>" --evidence-file <tail of the failing output>`, and stop. Never loosen a test, never edit tests yourself, never push to main, never merge: landing is the user's `/harness:land`.

## Report
Ticket and branch, PR URL and state, each stage with tier and attempts, review findings fixed and left, CI state, and anything the user must decide. Short.
