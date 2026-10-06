---
name: spec
description: Interview the user about a feature or change, then write a self-contained spec to docs/agent/specs/. Use when the user runs /spec or wants to plan a substantial change before any code is written.
argument-hint: "[what you want to build]"
disable-model-invocation: true
---
# Spec interview

Turn a rough idea into a spec that a fresh session can implement without coming back with questions.

The user's idea: $ARGUMENTS

1. Read enough of the codebase to ask informed questions: the modules this touches, patterns worth reusing, and how that area is tested today. If that means reading more than a handful of files, use a subagent and keep only its conclusions.
2. Interview the user with the AskUserQuestion tool, a few questions at a time. Skip anything the code already answers. Spend the questions on what they may not have considered: data shapes and edge cases, failure behavior, performance limits, what must not change, and how they'll know it works.
3. When no open questions remain, write `docs/agent/specs/<yyyy-mm-dd>-<slug>.md` with these sections:
   - **Goal**: one paragraph, including why this matters.
   - **Context**: relevant files, functions and existing patterns, with paths.
   - **Requirements**: numbered, each one testable.
   - **Interfaces**: signatures, inputs and outputs, schemas.
   - **Out of scope**: what this change leaves alone.
   - **Verification**: the exact commands and expected results that prove it works end to end.
   - **Risks and open questions**: empty, or each item explicitly accepted by the user.
4. Give the user the path and a five-line summary. Suggest implementing it in a fresh session, for example:
   `/goal every requirement in <spec path> is implemented and its verification commands pass, or stop after 30 turns`

This skill produces the spec only; leave the implementation for the next session.
