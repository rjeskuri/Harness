---
name: standup
description: Morning brief across every registered project - git state, next steps from handoff and progress notes, running sessions from the fleet, hours this month on billable work - followed by a proposed plan for the day. Use when the user runs /harness:standup or asks where things stand across projects.
argument-hint: "[--json | project name]"
disable-model-invocation: true
allowed-tools:
  - Bash(harness-standup:*)
  - Bash(harness-projects:*)
  - Bash(claude agents:*)
---
# Standup

1. Run `harness-standup $ARGUMENTS` (it is on PATH while this plugin is enabled; otherwise `python3 "${CLAUDE_PLUGIN_ROOT}/bin/harness-standup"`). It prints one section per registered project and a fleet section listing the Claude sessions that are running, idle or waiting on a permission.
2. If it reports no registered projects, run `harness-projects discover`, show the user the repositories it found, and register the ones they confirm with `harness-projects add <path> [--billable --client <name>]`. Then run the standup again.
3. Summarize for the user, at most three lines per project: state, blocker or open question, next step. Name any session that is waiting on them first.
4. Propose an ordered plan for today with the one project to open first, and ask whether that order is right. Do not start work on another project from this session; each project gets its own session.
