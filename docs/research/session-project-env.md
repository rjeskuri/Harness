# Research notes: how a session's Bash calls know the session's project

Gathered 2026-10-27 (Claude Code 2.1.310) for #205, from `https://code.claude.com/docs/en/hooks.md`, the Claude Code changelog (`CHANGELOG.md` in anthropics/claude-code) and a live check in a session. This note keeps what `session_brief.py` and `harness_lib.settings.other_project` rely on.

## Verified

- **`CLAUDE_PROJECT_DIR` reaches hook processes, not the Bash tool.** hooks.md: both hook command forms "export them as the environment variables `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, and `CLAUDE_PLUGIN_DATA` on the spawned process"; it is "the project root where the session started" and also reaches stdio MCP servers and plugin LSP servers. A session's Bash calls do not get it (checked live), so a tool run from Bash, such as harness-red, cannot tell the session's project from it.
- **`CLAUDE_ENV_FILE` carries a variable from a hook to later Bash calls.** hooks.md: "SessionStart hooks have access to the `CLAUDE_ENV_FILE` environment variable, which provides a file path where you can persist environment variables for the shell commands Claude runs later in the session." "Each Bash command runs the file's contents as shell code before the command itself". "To set individual environment variables, write `export` statements to `CLAUDE_ENV_FILE`. Use append (`>>`) to preserve variables set by other hooks". The value must therefore be shell-quoted, and the file never truncated.
- **Which hooks get it.** hooks.md: "`CLAUDE_ENV_FILE` is available for SessionStart, Setup, CwdChanged, and FileChanged hooks. Other hook events don't have access to this variable, and neither does a hook that runs in PowerShell".
- **SessionStart sources.** hooks.md lists the matcher values `startup`, `resume`, `clear`, `compact` and `fork` (`source`: "`\"fork\"` for a new session forked from an existing one"). The harness's SessionStart entry matches all five so every session start exports.
- **Changelog fixes the export depends on.** 2.1.136: "Fixed env vars from `CLAUDE_ENV_FILE` SessionStart hooks going stale after `/resume` or `/clear`". 2.1.295: "Fixed variables a SessionStart hook writes to `CLAUDE_ENV_FILE` not reaching the Bash tool after an in-app `/resume` or `/branch`". 2.1.108: a file ending in a `#` comment line once left the Bash tool with no output, so each line the hook writes ends in a newline.

## Not verified

- Nothing left from #205. Whether subagents' Bash calls and background sessions (`--bg`) source the session's `CLAUDE_ENV_FILE`, which the docs do not say, was seen live on 2026-11-16 (Claude Code 2.1.325): both do (next section).

## A claimed background spare keeps another project's environment (#345)

Seen 2026-11-16 on Claude Code 2.1.325 (macOS), in the #345 `/harness:work` job for Harness, itself a background session, and first hit 2026-11-15 in the #337 job (`harness-red check` ran `npm test -- {files}` in Harness and stopped on `npm error enoent Could not read package.json`). session_brief's drop of the strays (docs/design.md, Settings and Hooks) rests on this section.

### Observed live

- **A daemon started by `claude --bg` runs with the caller's environment.** `ps -axo pid,lstart,command` showed pid 35696, started 2026-11-15 14:48, as `claude daemon run --origin transient --spawned-by {"label":"claude --bg","cwd":"/Users/ryaneskuri/Projects/OkinawaDream/.claude/wo…"}`: a `claude --bg` run from the Bash of an OkinawaDream worktree session. Its exec-time environment (`ps eww`) was that session's: `PWD=/Users/ryaneskuri/projects/okinawadream`, OkinawaDream's settings `env` (`HARNESS_TEST_CMD=npm test -- {files}`, `CLAUDE_VERIFY_CMD=scripts/verify.sh`, `HARNESS_ULTRAREVIEW=off`), the user settings' `HARNESS_PROJECT`, and `HARNESS_SESSION_PROJECT=/Users/ryaneskuri/Projects/OkinawaDream`, the export session_brief had written to that session's `CLAUDE_ENV_FILE`. The daemon was still running and serving spares the next day.
- **Every spare inherits the daemon's environment.** The daemon keeps spares as `claude bg-pty-host --bg-pty-host /tmp/cc-daemon-501/<id>/spare/<id>.pty.sock … --bg-spare …`, each running `claude bg-spare --bg-spare /tmp/cc-daemon-501/<id>/spare/<id>.claim.sock`; a spare's `ps eww` showed the same keys as the daemon's.
- **A claimed spare overlays the new project's settings `env`, and keeps the keys only the old project set.** The Harness job that claimed such a spare had, in its Bash calls, Harness's own `CLAUDE_VERIFY_CMD=python3 scripts/run_suite.py` and `HARNESS_TEST_MAP` (from Harness's `.claude/settings.json`, whose `env` holds `CLAUDE_VERIFY_CMD`, `HARNESS_TEST_MAP` and `HARNESS_ULTRAREVIEW`) and the user settings' `HARNESS_PROJECT`, but also `HARNESS_TEST_CMD=npm test -- {files}`, which only OkinawaDream's `.claude/settings.json` sets and no Harness settings file (user, project, local) does.
- **A background session's Bash calls source `CLAUDE_ENV_FILE`.** The job's env file, `~/.claude/session-env/<session id>/sessionstart-hook-0.sh`, held session_brief's `export HARNESS_SESSION_PROJECT=<this worktree>`, and the job's Bash calls showed `HARNESS_SESSION_PROJECT=<this worktree>`, not OkinawaDream. So in Bash `other_project()` was False and `project_setting` took the stray.
- **A subagent's Bash calls source it too.** The test-author subagent's first Bash call printed `SP=<this worktree> TC=npm test -- {files}`; the coder subagent's Bash calls showed the same `HARNESS_SESSION_PROJECT` and the stray `HARNESS_TEST_CMD`.
- **Hook processes do not source it.** The SessionStart hook of the claimed session still had the inherited `HARNESS_SESSION_PROJECT` (OkinawaDream) next to `CLAUDE_PROJECT_DIR` (the Harness worktree), which is the fact session_brief's drop decides on.

### Inferred, not observed

- That any `claude --bg` run from a session's Bash, in any project, starts the daemon with that session's whole environment (one daemon was seen), and that a daemon started from a plain terminal carries no `HARNESS_SESSION_PROJECT` and no project's settings `env`.
- That the overlay happens when the spare is claimed (only its result was seen), and that it sets each key of the new project's user, project and local settings `env` without unsetting the rest.
- That restarting the daemon from a plain terminal clears the strays from later spares, and how long a `--origin transient` daemon lives.
- That the hook processes of a claimed session (tests_first, verify_on_stop and the rest) see the same strays, since they get the process environment and never source the file; their own settings reads are not covered by the drop (follow-up to #345).
