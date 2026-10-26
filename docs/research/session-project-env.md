# Research notes: how a session's Bash calls know the session's project

Gathered 2026-10-27 (Claude Code 2.1.310) for #205, from `https://code.claude.com/docs/en/hooks.md`, the Claude Code changelog (`CHANGELOG.md` in anthropics/claude-code) and a live check in a session. This note keeps what `session_brief.py` and `harness_lib.settings.other_project` rely on.

## Verified

- **`CLAUDE_PROJECT_DIR` reaches hook processes, not the Bash tool.** hooks.md: both hook command forms "export them as the environment variables `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, and `CLAUDE_PLUGIN_DATA` on the spawned process"; it is "the project root where the session started" and also reaches stdio MCP servers and plugin LSP servers. A session's Bash calls do not get it (checked live), so a tool run from Bash, such as harness-red, cannot tell the session's project from it.
- **`CLAUDE_ENV_FILE` carries a variable from a hook to later Bash calls.** hooks.md: "SessionStart hooks have access to the `CLAUDE_ENV_FILE` environment variable, which provides a file path where you can persist environment variables for the shell commands Claude runs later in the session." "Each Bash command runs the file's contents as shell code before the command itself". "To set individual environment variables, write `export` statements to `CLAUDE_ENV_FILE`. Use append (`>>`) to preserve variables set by other hooks". The value must therefore be shell-quoted, and the file never truncated.
- **Which hooks get it.** hooks.md: "`CLAUDE_ENV_FILE` is available for SessionStart, Setup, CwdChanged, and FileChanged hooks. Other hook events don't have access to this variable, and neither does a hook that runs in PowerShell".
- **SessionStart sources.** hooks.md lists the matcher values `startup`, `resume`, `clear`, `compact` and `fork` (`source`: "`\"fork\"` for a new session forked from an existing one"). The harness's SessionStart entry matches all five so every session start exports.
- **Changelog fixes the export depends on.** 2.1.136: "Fixed env vars from `CLAUDE_ENV_FILE` SessionStart hooks going stale after `/resume` or `/clear`". 2.1.295: "Fixed variables a SessionStart hook writes to `CLAUDE_ENV_FILE` not reaching the Bash tool after an in-app `/resume` or `/branch`". 2.1.108: a file ending in a `#` comment line once left the Bash tool with no output, so each line the hook writes ends in a newline.

## Not verified

- Whether subagents' Bash calls and background sessions (`--bg`) source the session's `CLAUDE_ENV_FILE`: the docs do not say. Without the export, `other_project` falls back to `CLAUDE_PROJECT_DIR`, then to this repository's own session.
