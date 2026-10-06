---
name: handoff
description: Write a handoff note that captures the current task's state so a fresh session can continue without rediscovering anything. Use when the user runs /harness:handoff, before clearing context mid-task, or when the context window is getting full.
disable-model-invocation: true
---
# Handoff

Current working tree:
!`git status --short`

Write `docs/agent/handoff.md`, replacing any previous version, so that a new session that reads only this file and the repo can pick up the work. Check `git log --oneline -10` for recent commits. Be concise and specific:

- **Goal**: what we are trying to achieve and why. Link the spec or plan file if there is one.
- **Done**: what is finished and verified, with the commands that verified it.
- **In progress**: exactly where things stand, including anything half-edited or uncommitted.
- **Next steps**: an ordered list, concrete enough to act on without asking. The session brief and the standup read this section, so keep it current.
- **Decisions**: choices made and the reason for each, including approaches tried and abandoned, and why they failed.
- **Gotchas**: anything surprising about the code, data or environment learned this session.
- **Key files**: paths with one line each.

Then tell the user to run `/clear` and start the next session with:
`Read docs/agent/handoff.md and continue from Next steps.`
