---
name: cloud
description: Start a Claude cloud session on one ticket as a plain worker - start the ticket, push its branch, launch `claude --cloud` on it - and say how to follow it and bring it home. Review, the PR, CI and landing stay on this Mac. Use when the user runs /harness:cloud N or asks to run a ticket in the cloud.
argument-hint: "N"
disable-model-invocation: true
allowed-tools:
  - Bash(harness-work:*)
  - Bash(harness-board:*)
---
# Cloud

Arguments: $ARGUMENTS

A cloud session is a plain worker. It loads no harness plugin: no hooks, no `/harness:work`, no harness-board or harness-pr, no tests-first gate and no role guards run there, so nothing enforces what this repository's hooks enforce on the Mac. Its prompt asks for tests first, the verify command, a push to `t/N-*` only and a marker comment on the ticket; the safety net is what happens here afterwards: review, CI and the land gate.

Before the first launch for a repository, the owner connects the Claude GitHub integration for it on claude.ai. A session created before that runs from a seed bundle with no origin and can never push, and connecting later does not fix a session that is already running.

1. With no number, ask which ticket and stop.
2. Run `harness-work cloud N` (it is on PATH while this plugin is enabled; otherwise `python3 "${CLAUDE_PLUGIN_ROOT}/bin/harness-work" cloud N`). It refuses, with one line and nothing started, a `needs-local` ticket (run it here with `/harness:work N`), an exhausted `cloud` budget (`harness-work budget reset cloud` once cloud sessions work again), a ticket that already has an unended cloud run, and whatever `harness-work start` refuses. Otherwise it starts the ticket, pushes its branch, launches the session, writes the run record and comments the session URL on the ticket. Relay a refusal or a failure as it is; do not retry around it.
3. Show the user the session URL it printed and tell them to open it and check that the session was not "Cloned from seed bundle" before trusting it: such a session cannot push, and the run is lost; stop it and launch again once the integration is connected.
4. Following it: `harness-work runs --sweep` closes the run when the session comments `<!-- harness-cloud: done -->` (outcome ok) or `<!-- harness-cloud: blocked -->` (outcome blocked; the question goes to the ticket as needs-input), when the ticket is finished, or after 24 h of silence (unknown). `harness-work runs` shows it as `in the cloud (<url>)` until then.
5. Bringing it home: once the run has ended, `/harness:work N` in this repository picks up the pushed branch (start fast-forwards a clean worktree to it) and runs review, the PR and CI here. Say plainly that the cloud's own test run is unverified until then.
