---
name: build
description: Implement a spec or task with the routing rule - the main session plans and reviews, the coder agent (Opus) implements, the reviewer agent (Fable) checks - and report evidence per requirement. Use when the user runs /harness:build, or asks to implement a spec written by /harness:spec.
argument-hint: "[spec path or task description]"
disable-model-invocation: true
---
# Build

Input: $ARGUMENTS (a spec path, usually under `docs/agent/specs/`, or a task description).

You are the orchestrator. You plan, delegate, verify and review; you do not write the code yourself except for one-line fixes.

1. **Plan.** Read the spec and the code it touches. Split the work into tasks a coder can finish in one go, each with: the files, what changes, the conventions that apply, and how to verify it. Say which tasks are independent.
2. **Implement.** Delegate each task to the `harness:coder` subagent with the task text, the relevant conventions and the project's verify command. Use `harness:quick` only for trivial mechanical steps. For several independent tasks, run the coders in parallel, or launch a Workflow with Opus workers when there are more than three; say what you launched and why.
3. **Verify.** Run the project's verify command yourself (from CLAUDE.md, or `CLAUDE_VERIFY_CMD` in the project settings). Send failures back to the coder with the output.
4. **Review.** Have `harness:reviewer` review the full diff against the spec in a fresh context. Fix correctness findings through the coder; list the rest for the user instead of acting on them.
5. **Report** one line per requirement: done or not done, with the command that proves it. Name anything left out and why. Suggest `/harness:ship` when everything is green.
