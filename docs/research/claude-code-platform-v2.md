# Research notes: Claude Code platform facts for v2

Gathered 2026-10-13 by an Opus research agent from the raw markdown of the official docs (`https://code.claude.com/docs/en/<page>.md`), the CHANGELOG (newest entry 2.1.296) and the installed binary (`~/.local/bin/claude`, 2.1.287, via `--help` and `strings`). Quotes are the sources' words; judgments are the agent's and were read by the harness designer before use. The installed stable build is nine releases behind the docs, so a few documented features are not installed yet.

## Sub-agents and the Agent tool
- Plugin agent frontmatter supports `name`, `description`, `model`, `effort`, `maxTurns`, `tools`, `disallowedTools`, `skills`, `memory`, `background`, `omitClaudeMd`, `isolation`, `color`, `experimental.cacheTtl`; it ignores `permissionMode`, `hooks`, `mcpServers`, `initialPrompt`.
- `model`: "`sonnet`, `opus`, `haiku`, `fable`, a full model ID such as `claude-opus-5-5`, or `inherit`." A family alias "resolves to the main conversation's model instead of the version the alias points to" when the main model is in that family.
- `effort`: "Options: `low`, `medium`, `high`, `xhigh`, `max`; available levels depend on the model." It "Overrides the session effort level, but not the `CLAUDE_CODE_EFFORT_LEVEL` environment variable." Omitting it gives the session level; whether a subagent on another model re-resolves `modelSettings` for its own model is not documented.
- Per-call effort: "it can also pass an `effort` parameter for that invocation… requires Claude Code v2.1.292 or later." The 2.1.287 Agent schema has `model` (aliases only), `run_in_background`, `name`, `isolation`, `cwd`; no `effort`.

## Settings
- `"model": "opus"` resolves to Opus 5.5 since 2.1.280. `modelSettings` entries are written "under the model's canonical name, such as `claude-opus-5-5`"; `effortLevel` there accepts low, medium, high, xhigh (not max). "a top-level `effortLevel` in your user settings file doesn't count for Opus 5.5".

## Hooks
- 34 events, including SessionStart, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop, WorktreeCreate, WorktreeRemove, CwdChanged, SessionEnd.
- Common stdin fields: `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `permission_mode`, `effort.level`, `hook_event_name`, `agent_id`, `agent_type` ("Present when the session uses `--agent` or the hook fires inside a subagent"; plugin agents appear as `my-plugin:reviewer`).
- PreToolUse gets `tool_name`, `tool_input`, `tool_use_id`; Write `file_path`+`content`; Edit `file_path`, `old_string`, `new_string`, `replace_all`; Bash `command`, `description`, `timeout`, `run_in_background`; "For the file tools `Write`, `Edit`, and `Read`, `tool_input.file_path` is always absolute"; NotebookEdit uses `notebook_path` (absolute).
- Deny: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}`; "precedence is `deny` > `defer` > `ask` > `allow`"; exit 2 routes like deny; top-level `decision` is deprecated for PreToolUse.
- WorktreeCreate *replaces* worktree creation ("The hook must return the path"; stdin only `name`; `.worktreeinclude` is then not processed) and does not fire for `git worktree add` run through Bash. No post-create setup hook exists.
- SessionStart input: `source`, `model`, `agent_type`, `session_title`; no branch or worktree field; "the `cwd` field… is the worktree root after Claude enters a worktree".
- SubagentStart gets `agent_id`, `agent_type`, can add context, cannot block. SubagentStop gets `agent_transcript_path`, `last_assistant_message`, etc.

## Worktrees and background sessions
- `claude -w <name>`: "created under `.claude/worktrees/<name>/` at your repository root, on a new branch named `worktree-<name>`"; base `"fresh"` branches from the remote default branch; "You can't set `worktree.baseRef` to a branch name. To start a worktree from a specific existing branch, create it with git directly."
- In a worktree session "Claude Code blocks an `Edit`, `Write`, or `NotebookEdit` that targets a path in the main checkout", but "a command that writes into the main checkout without running git there, such as `cp` or a shell redirect, isn't refused".
- `claude --bg -n <name> "<task>"`: the prompt is positional; prints `backgrounded · <id> · <name>`; in a script an untrusted workspace fails with `Workspace not trusted`; "In a worktree, it uses the main checkout's root" for trust. Background sessions self-isolate into `.claude/worktrees/` unless "already inside a linked git worktree". No `--cwd`: "cd into the directory and run `claude --bg`".
- `claude agents --json`: `cwd`, `kind` (`interactive`|`background`), `startedAt`; background: `id`, `state` (`working`, `blocked`, `done`, `failed`, `stopped`), `pid`, `status` (`busy`, `waiting`, `idle`), `waitingFor`, `sessionId`, `name`.
- `claude rm <id>`: removes "a worktree Claude created for it when that's safe"; "A worktree you created yourself and started the session inside is left in place either way."
- EnterWorktree takes `name` or `path`, no branch parameter.

## Plugins and skills
- `${CLAUDE_PLUGIN_ROOT}` is substituted in skill bodies and hooks, but "The variables aren't present in the environment of commands Claude runs through the Bash tool". `bin/` files "are on the `PATH` of the Bash tool's shell while the plugin is enabled", after the user's own PATH.
- Local-path marketplace plugins load in place; edits apply at the next session or `/reload-plugins`. The stale cache copy under `~/.claude/plugins/cache/ryan/harness/0.1.0` is not what runs.
- Skills frontmatter confirmed: `disable-model-invocation`, `allowed-tools`, `argument-hint`, `$ARGUMENTS`, `` !`cmd` ``, plus `model`, `effort`, `context: fork`, `agent`, `paths`, `user-invocable`.

## Scheduling, billing, cloud
- Routines "execute on Anthropic-managed cloud infrastructure"; "Access to local files | No (fresh clone)"; they run only skills committed to the repository or enabled on claude.ai; plugins do not load; "Routines draw down subscription usage the same way interactive sessions do". Daily presets exist; minimum interval one hour.
- Auth precedence: cloud provider, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, profiles, subscription OAuth. "In non-interactive mode (`-p`), the key is always used when present." Background sessions: "The supervisor and its sessions authenticate with the same stored credentials as your interactive sessions." `forceLoginMethod` is an admin restriction, not a selector. `--bare` never reads OAuth but also skips plugins and hooks.
- Cloud sessions: "The cloud VM clones your current directory's GitHub remote at your current branch"; "counts toward your plan's usage limits"; "Cloud sessions always use your subscription credentials"; "Without an interactive terminal, --cloud can only send the prompt to an existing cloud session"; "A cloud session doesn't install the plugins a repository turns on under `enabledPlugins`"; the GitHub proxy rejects `gh pr` and `gh issue` (GraphQL), REST via `gh api` works.
- `claude ultrareview [target] [--post] [--json] [--timeout min]` "runs as a cloud session on Anthropic's infrastructure", "bills against usage credits rather than your plan's included usage" (three free runs, then $5 to $25 each), "blocks until the remote review finishes, and prints the findings to stdout"; `--post` posts one PR comment; exit codes 0, 1, 130.

## What it meant for the design
- Per-call effort waits for 2.1.292 on stable; routing uses `model` per call and frontmatter `effort` where fixed.
- Deny-on-main is a PreToolUse hook on Edit|Write|NotebookEdit and Bash, resolving the branch from the file's directory, never from `cwd` alone.
- Worktree per ticket: `git worktree add .claude/worktrees/<ticket> -b <branch> origin/main`, copy `.worktreeinclude` files ourselves, `cd` and start the session there; clean up with `git worktree remove`.
- Dispatch billed to the API key: headless `claude -p … --settings '{"apiKeyHelper": "…"}'`, not `--bg`. Cloud dispatch is out of scope (subscription billing, TTY, no plugins). Ultrareview is the scriptable consumer of usage credits.
- The daily digest is a local launchd job, not a routine.
