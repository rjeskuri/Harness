<!--
Personal instructions for every project. Copy to ~/.claude/CLAUDE.md.
Keep it to a handful of lines: it loads into every session, in every repo. Edit the <placeholders>.
-->

# How I work

- I'm an experienced Python and SQL developer. Skip the basics; when you recommend something, give the reasoning and the tradeoffs.
- Keep replies short. For code changes I'll review inline, show before/after snippets of the changed lines rather than whole files.
- I'm on macOS with zsh, Homebrew and VS Code. Python environments are managed with <uv>; there's no bare `python`, only `python3` and `uv run python`.
- macOS ships BSD `sed`, `grep` and `date`: no `grep -P`, `sed -i` needs `''`, and `date -d` doesn't exist. For text transforms, prefer a short Python script.
- When you finish a task, show the evidence (the command you ran and its result) and say plainly what you couldn't verify.
- Ask before adding dependencies, pushing, or anything else that's hard to undo.
