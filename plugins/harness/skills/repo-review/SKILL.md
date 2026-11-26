---
name: repo-review
description: Get an independent GPT-6 Astra review of a repository for bugs, logic errors, missing behavior and possible refinements. Use when the owner or an agent asks for a repository-wide second opinion.
argument-hint: "[repository path] [review focus]"
allowed-tools:
  - Bash(harness-gpt repo-review *)
---
# Repository review

Use the current repository unless the caller names another. Run `harness-gpt repo-review --repo <path>` with the quoted path. Add `--brief <focus>` when the caller supplies a focus. An agent with shell access may invoke the same command directly.

Give the caller the reviewed commit, concrete findings in impact order, file and line evidence, and suggested fixes. Keep optional refinements distinct from bugs. Include excluded file counts and any areas Astra could not examine. This review covers committed source. Report any execution failure as an incomplete review.
