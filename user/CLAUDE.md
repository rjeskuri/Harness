# How I work

- I am a solo developer across several repositories, experienced in Python and SQL. Skip the basics; when you recommend something, give the reasoning and the tradeoffs.
- Keep replies short. For code changes I will review inline, show before/after snippets of the changed lines rather than whole files.
- I am on macOS with zsh, Homebrew and VS Code. macOS ships BSD `sed`, `grep` and `date`: no `grep -P`, `sed -i` needs `''`, and `date -d` does not exist. There is no bare `python`, only `python3` or the project's own interpreter.
- When you finish a task, show the evidence (the command you ran and its result) and say plainly what you could not verify.
- Ask before adding dependencies, pushing, or anything else that is hard to undo. Never put a secret on a command line; read it from the environment or a file.

## Model routing

- You, the main session, run on Fable: you own design decisions, orchestration and the final review.
- Delegate implementation to the `harness:coder` subagent (Opus): anything beyond a trivial edit, including new code, tests, refactors and bug fixes that need investigation. Give it the task, the files, the conventions and the verify command.
- Use `harness:quick` (Sonnet) only for small tasks you judge simple: a rename, a one-line fix, a lookup, a doc tweak, formatting.
- Before calling work done, have `harness:reviewer` (Fable, fresh context) review the diff against the spec or task, and fix correctness findings.
- Send fact-checking and "how does X behave" questions to `harness:researcher` (Opus), which answers from primary sources.
- Large or parallel work is your call: launch a Workflow when it pays off, with Fable workers for judgment-heavy review or design stages, Opus workers for coding, and Sonnet workers only for mechanical fan-out. Say what you launched and why.
- Effort defaults to xhigh for every tier; lower a subagent's effort only when its task is mechanical.

## Across projects

- Each project gets its own session; `/harness:standup` is the cross-project view, and `claude agents` in a terminal is the console for background sessions.
- Write `docs/agent/handoff.md` with `/harness:handoff` before clearing context mid-task; the session brief reads it on the next start.
- On a billable project, run `/harness:worklog` at the end of a working session.
