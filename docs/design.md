# Harness design

Ryan's personal Claude Code harness. One repository, two plugins, installed once at user scope, applied to every project. Written 2026-10-06 against Claude Code 2.1.285 (CLI) and 2.1.289 (VS Code extension).

## Principles

1. **Additive, never invasive.** The harness never replaces a project's own `CLAUDE.md`, hooks, rules or skills. jotform-glow-sync and HKTrack keep their bespoke guardrails untouched; the harness adds a shared layer beside them.
2. **Model routing is the main efficiency lever.** Ryan is on a subscription with five-hour and seven-day windows. Fable (the main session) designs, orchestrates and reviews; Opus implements; Sonnet does trivial mechanical work. Effort defaults to xhigh everywhere and is adjustable per agent.
3. **Deterministic where it must be, advisory where it can be.** Hooks enforce (secret scan, verify before stopping); CLAUDE.md and skills advise.
4. **Fail open.** A hook that crashes never traps a session: every hook exits 0 on its own errors.
5. **Hooks run outside the sandbox.** Whatever a hook executes runs with the user's full permissions and without a prompt, so the verify command must be a test command the user trusts, and hook scripts never run code from the edited files.
6. **Standard library only.** Hooks and tools run on the `python3` from Xcode's command line tools (3.9) or newer; nothing to install.

## Layout

```
Harness/
  .claude-plugin/marketplace.json      marketplace "ryan": plugins harness and companion
  plugins/harness/                      plugin "harness"
    .claude-plugin/plugin.json
    agents/   coder.md quick.md reviewer.md researcher.md
    skills/   spec build ship handoff learn standup worklog   (one SKILL.md each)
    hooks/    hooks.json notify.py session_brief.py after_edit.py verify_on_stop.py readonly_bash.py
    bin/      harness-standup harness-worklog harness-projects   (executable python3 scripts, on the Bash PATH while enabled)
    tests/    test_*.py   (python3 -m unittest discover -s plugins/harness/tests)
  plugins/companion/                    plugin "companion" (a mod)
    .claude-plugin/plugin.json          names types/index.d.ts
    hooks/hooks.json                    { "modules": ["./register.tsx"] }
    hooks/register.tsx
    types/index.d.ts
    tests/companion.test.ts             run with: claude plugin test plugins/companion
  user/CLAUDE.md                        copied to ~/.claude/CLAUDE.md
  user/settings.snippet.json            merged into ~/.claude/settings.json
  docs/design.md                        this file
  docs/verification.md                  claim-by-claim check of the starter kit
  claude-code-starter-macos/            the starter kit the harness was built from, kept for reference
```

Install (once):

```sh
claude plugin marketplace add /Users/ryaneskuri/portfolio_projects/Harness
claude plugin install harness@ryan
claude plugin install companion@ryan
cp user/CLAUDE.md ~/.claude/CLAUDE.md
```

A marketplace added from a local path loads its plugins in place, so edits in this repo take effect at the next session start or `/reload-plugins`.

## Model routing

| Role | Agent | Model | Effort | Tools |
|---|---|---|---|---|
| Design, orchestration, final review | the main session | Fable (settings `model`) | xhigh | all |
| Implementation | `harness:coder` | opus | xhigh | all |
| Trivial mechanical tasks | `harness:quick` | sonnet | xhigh | all |
| Fresh-context review | `harness:reviewer` | fable | xhigh | read-only |
| Fact-checking, research | `harness:researcher` | opus | xhigh | read-only + web |

The rule itself lives in `user/CLAUDE.md` so the orchestrator applies it in every project. Workflow scripts follow the same tiers: Opus workers, Fable judges.

Facts that shape the routing (verified 2026-10-06, see docs/research/model-routing.md):
- Delegation is Claude's judgment, steered by each agent's `description` and by naming the agent in CLAUDE.md or a skill; an `@`-mention of the agent in a prompt guarantees it. Ryan is on Claude Max 20x, so the Pro-plan instruction that holds subagents back does not apply.
- Plugin subagents ignore the `hooks`, `mcpServers` and `permissionMode` frontmatter fields. The reviewer's and researcher's read-only guard is therefore a plugin-level PreToolUse hook on `Bash` (`hooks/readonly_bash.py`) that acts only when the hook's stdin names `agent_type` as one of those agents and exits 0 for everything else.
- The Agent tool's per-call `model` is an alias (sonnet, opus, haiku, fable); full IDs go in agent frontmatter. `max` effort cannot be saved in settings; `xhigh` can. A top-level `effortLevel` is ignored by Opus 5.5 and Sonnet 5.5, so the per-model `modelSettings` entries carry the defaults.
- `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` would force one model on every subagent and defeat the tiers; it is not used.

## Harness plugin

### Hooks

All hooks: read JSON from stdin, never raise, exit 0 on their own errors (the one deliberate non-zero exit is the secret scan's exit 2). State lives outside the repo under `$TMPDIR/claude-harness/<sha1 of repo path, 12 hex>/`.

**notify.py** (Notification: `permission_prompt|idle_prompt|agent_needs_input|agent_completed`). A macOS notification naming the repo (basename of `cwd`) and the message. Order: `terminal-notifier` if on PATH (`-title`, `-subtitle <repo>`, `-message`, `-group claude-code-<session_id>`, `-activate $HARNESS_NOTIFY_APP` default `com.microsoft.VSCode`, `-sound $HARNESS_NOTIFY_SOUND` default `Glass`, empty disables); else `osascript display notification`; else print `{"terminalSequence": "\u0007"}`. `title` from stdin or "Claude Code". Always exit 0.

**session_brief.py** (SessionStart: `startup|resume|compact`). Prints JSON `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<brief>", "sessionTitle": "<repo name>"}}` (omit `sessionTitle` unless `source` is `startup`). The brief, at most 40 lines:
- `## Session brief: <repo> on <branch>, <n> uncommitted, last commit <relative age>: <subject>`
- If `docs/agent/handoff.md` exists: its **Next steps** section (first 12 lines of it), else the file's first 20 lines.
- Else if `docs/PROGRESS.md` exists: the last `## ` block's lines that start with `Next` (max 6 lines).
- If `docs/agent/specs/` has files: `Latest spec: <path>`.
- If `docs/plans/feature_list.json` exists and is a list with `passes` fields: the first three `passes: false` items.
- If `worklog/worklog.csv` exists: `Hours this month: <sum of this month's rows>`.
Skip entirely (print nothing) when the project's `.claude/settings.json` declares its own `SessionStart` hook, so a project with a bespoke brief is not briefed twice. Not a git repo: the heading carries no git facts, and the sections are still read from the directory. The PROGRESS.md scan ignores lines inside code fences, as the handoff parser does.

**after_edit.py** (PostToolUse: `Edit|Write|NotebookEdit|Bash`). Hook matchers never see files changed through the shell, so for a `Bash` call the hook takes the files `git status` lists (modified or untracked, regular, under 2 MB, newer than the session's last Bash scan or the last 10 minutes, at most 100) and gives them the same two jobs as an edited file; outside a git repo a Bash call does nothing. Two jobs:
1. Append the edited file's absolute path (`tool_input.file_path` or `notebook_path`) to `<state>/dirty-<session_id>` so the Stop hook knows a verify run is due.
2. Secret scan of that file when it exists, is under 2 MB and decodes as UTF-8. High-confidence patterns only: AWS access key id `AKIA[0-9A-Z]{16}`; GitHub tokens `gh[pousr]_[A-Za-z0-9]{36,}` and `github_pat_[A-Za-z0-9_]{22,}`; Slack `xox[baprs]-[A-Za-z0-9-]{10,}`; Google `AIza[0-9A-Za-z_-]{35}`; OpenAI `sk-(proj-)?[A-Za-z0-9_-]{20,}`; Anthropic `sk-ant-[A-Za-z0-9_-]{20,}`; Stripe `sk_live_[A-Za-z0-9]{16,}`; private key blocks `-----BEGIN [A-Z ]*PRIVATE KEY-----`; a Jotform-style `apiKey=[0-9a-f]{32}`; and a generic `(api[_-]?key|secret|token|password)\s*[:=]\s*["'][A-Za-z0-9_\-]{24,}["']`. The Anthropic, OpenAI and Stripe patterns carry a `(?<![A-Za-z0-9_-])` lookbehind so kebab-case words ending in `sk` (`task-`, `risk-`) are not keys; the generic assignment pattern is compiled case-insensitively so `API_KEY = "..."` and `apiKey: "..."` are caught, while the provider patterns stay case-sensitive. Skip a line that contains `example`, `placeholder`, `dummy`, `xxxx`, `<`, `${`, `your_` or `REDACTED` (any case), and skip entirely when `HARNESS_SECRET_SCAN=0` or the file contains `harness: allow-secrets`. On a hit: stderr `SECRET pattern in <path>:<line> (<pattern name>): <first 4 chars>… - remove it before continuing; read secrets from the environment or a file that is gitignored.` and exit 2. Otherwise exit 0 silently.

**verify_on_stop.py** (Stop). Opt-in per project through `CLAUDE_VERIFY_CMD`, read from the environment first and then from `env` in the project's `.claude/settings.json` and `.claude/settings.local.json` (so it works whether or not settings `env` is exported to hooks). Behavior:
- Exit 0 silently when there is no verify command, `background_tasks` is non-empty, or the project's `.claude/settings.json` declares a `Stop` hook of its own (a project with its own verify-on-stop, such as FantasyHockey with the starter kit, is not checked twice). Without a `dirty-<session_id>` file it still watches the working tree: a hash of `git status --porcelain` is stored at every stop; the first stop of a session only stores it, a changed tree counts as dirty and its files join the dirty list, an unchanged tree exits 0.
- Reset the consecutive-block counter when `stop_hook_active` is false.
- Fingerprint = sha256 of `git rev-parse HEAD` plus the contents of the dirty files and of every file `git status` lists; if it equals the last green fingerprint, exit 0. The last red or timed-out fingerprint is remembered too, together with the command that produced it: a fresh stop (not a continuation) whose fingerprint and command both match skips the run and says so in one `systemMessage` line, so a Q&A turn after a failure does not re-run the suite, while a corrected `CLAUDE_VERIFY_CMD` runs at once; the two in-turn retries still happen.
- The command inherits the hook's environment plus `PYTHONDONTWRITEBYTECODE=1` (no stale `.pyc` after a same-second edit). A verify command that leaves a background child holding its stdout is reported as a timeout; such children must redirect their output.
- Run the command through the shell from the repo root with `CLAUDE_VERIFY_TIMEOUT` seconds (default 300). On timeout or a command that cannot start: `{"systemMessage": "verify_on_stop: ..."}` and exit 0.
- Pass: record the fingerprint, delete the dirty file, print `{"systemMessage": "verify_on_stop: <command> passed."}`.
- Fail: at most 2 consecutive blocks: `{"decision": "block", "reason": "<the command> failed:\n<last 60 non-empty lines>\n\nFix the root cause and re-run. Don't delete, skip or loosen tests. If a failure predates your change or is out of scope, say so explicitly."}`. After 2 blocks in a row: `{"systemMessage": "verify_on_stop: still failing after 2 automatic retries; review before trusting this turn."}` and exit 0.

**readonly_bash.py** (PreToolUse: `Bash`). Acts only when the hook's stdin names `agent_type` as `harness:reviewer`, `reviewer`, `harness:researcher` or `researcher`; everyone else gets an immediate exit 0. For those agents it splits the command on shell separators and refuses, with `permissionDecision: deny` and the offending part named, anything that writes files or changes git, package or process state: redirections, `sed -i`, `rm`, `mv`, `cp`, `tee`, `touch`, `chmod`, git subcommands outside the read-only set, installs, Python write calls, `kill`. It is not a sandbox: a test or a script can still write; it closes the routine paths. Plugin subagents ignore frontmatter hooks, which is why the guard lives here.

### bin tools

Executable `python3` scripts (shebang `#!/usr/bin/env python3`, mode 755), `argparse`, standard library. Each prints a one-line error and exits 1 on a bad invocation, never a traceback.

**harness-projects**: the registry at `~/.claude/harness/projects.json`, schema `{"version": 1, "projects": [{"path": "/abs/path", "name": "HKTrack", "billable": false, "client": "", "notes": ""}]}`.
- `list` (default): a table of name, path, billable, client.
- `add <path> [--name N] [--billable] [--client C] [--notes T]`: path resolved to absolute; name defaults to the basename; refuses a duplicate path.
- `set <name|path> [--billable|--no-billable] [--client C] [--notes T] [--name N]`.
- `remove <name|path>`.
- `discover`: candidates from the directory names under `~/.claude/projects/` (Claude Code encodes a path by replacing every non-alphanumeric character with `-`; recover the path by trying every split of the hyphens into `/` and `-` and keeping the one that exists as a directory; skip names containing `-claude-worktrees-`). Print the candidates that exist, are not registered, and have a `.git` or a `CLAUDE.md`, with their last session time.
- `--json` on every subcommand.

**harness-standup**: the cross-project brief. For each registered project (in registry order; `harness-standup <name>` limits to one):
- heading `## <name>` with the path;
- git: branch, uncommitted count, ahead/behind the upstream if any, last commit age and subject; "not a git repo" otherwise;
- last session: the newest mtime among `~/.claude/projects/<encoded path>/*.jsonl`, as a relative age;
- next: the same sources and order as the session brief (handoff Next steps, PROGRESS.md Next lines, feature_list.json pending items), at most 5 lines;
- latest spec path when `docs/agent/specs/` exists;
- hours this month from `worklog/worklog.csv` when the project is billable;
- sessions: entries of `claude agents --json` whose `cwd` is this path (or under it), as `<name> <status> (<waitingFor>)`; the `claude` binary is looked up on PATH, then `~/.local/bin/claude`; if it cannot run, say so once.
Then `## Fleet`: every session from `claude agents --json`, waiting ones first, with its cwd basename. Output is Markdown; `--json` prints the raw structure. The fleet call and the projects' git calls each get a 4-second budget, so the whole run stays under 5 seconds; a project that cannot be read gets one line saying so and never stops the others.

**harness-worklog**: a generalized copy of `~/jotform-glow-sync/tools/worklog.py` (read it; keep its algorithm, messages and file formats exactly: 30-minute gap blocks, only blocks containing a human message count, day assignment on the Mac's clock at the time, quarter-hour rounding, the time-zone timeline in `worklog/timezones.csv`, descriptions kept once written, manual rows never touched, the window and expiry rules). Changes:
- The repo is `--repo PATH`, else `CLAUDE_PROJECT_DIR`, else `git rev-parse --show-toplevel` from the cwd, else the cwd. The log is `<repo>/worklog/worklog.csv`, the timeline `<repo>/worklog/timezones.csv`. Messages that mention `tools/worklog.py` say `harness-worklog` instead; "Ryan" becomes "the user" in docstrings (`is_ryans_message` becomes `is_human_message`).
- New `init [--since YYYY-MM-DD]`: creates `worklog/timezones.csv` with one row, the Mac's current zone from `--since` (default: the first day of the previous month) with evidence `recorded <today> by harness-worklog init`; refuses to overwrite an existing timeline. `update` without a timeline points at `init` instead of at a RUNBOOK.
- New `describe --date YYYY-MM-DD --text "..."`: sets the description of that day's auto row (creates the row with 0.25 h only if the day has blocks computed; otherwise refuses and suggests `add`).
- `export` unchanged; `add` unchanged; `zone` unchanged, except one inherited bug fixed in the copy: `append_zone`'s end-of-file newline check always said a newline was needed, so every appended zone row was preceded by a blank line (the original reads `fh.seek(-1, os.SEEK_END) or fh.read(1)`; the copy seeks, then reads). Tests use a neutral client name, never a real one.

### Tests (plugins/harness/tests)

`python3 -m unittest discover -s plugins/harness/tests -v` passes, offline, under 30 seconds. Cover at least: the secret scan (a hit, a skipped placeholder line, a clean file), the Bash-edit scan and the stop-time tree check, the read-only guard (deny and allow cases, and that only the two agents are guarded), the dirty-file bookkeeping, verify_on_stop's decisions with a fake command (pass, fail, block limit, no command), session_brief's output shape and the skip rule, the registry round trip and `discover`'s path recovery, standup's rendering with a fake registry and a fake `claude agents --json` payload, and worklog's `init` and `describe` plus one block/hours computation on a synthetic transcript. Tests run the scripts as subprocesses or import them by path; they must not touch `~/.claude` (use a temporary HOME and temporary repos).

## Companion plugin (the mod)

A Game Boy style robot that lives in the band above the prompt and reacts to the session. Surfaces: the band is raised in the terminal (a `Raster`) and in the desktop app (an `Svg`). The VS Code chat panel does not raise the band, so in VS Code the robot appears only when `claude` runs in the integrated terminal; the `Svg` branch also serves the test kit's vscode and mobile mounts. Hidden with the `/companion` command (toggle) or its Hide button; the choice persists for the session in `$.state`.

### State (types/index.d.ts)

```ts
export type Mood = 'idle' | 'thinking' | 'working' | 'waiting' | 'happy' | 'sad'
declare module 'claude-code' {
  interface PluginState {
    companion: { mood: Mood; note: string; frame: number; isHidden: boolean }
  }
}
```

### Events

- `session.start`: register the command `companion` ("Show or hide the companion sprite"); set mood `idle`, note `""`; start a ticker with `$.clock.every(500, ...)` that increments `frame` while the sprite is visible (skip the write while hidden).
- `prompt.submit`: mood `thinking`, note `""`; `next(e)`.
- `tool.call`: mood `working`, note from the tool: Bash "running a command"; Edit, Write, NotebookEdit "editing <basename of file_path>"; Read "reading <basename>"; Grep, Glob "searching"; Agent "delegating"; Workflow "orchestrating"; WebFetch, WebSearch "browsing"; otherwise the tool name. Then `const r = await next(e)`; if `r` has `isError` true: mood `sad`, note "that failed", and `$.clock.after(2500, ...)` back to `working` if still sad. Otherwise, on the next tick, a `waiting` look left by this call's permission prompt reverts to `working` (the prompt has been answered by then). Return `r` unchanged.
- `tool.check`: `const r = await next(e)`; if `r.decision === 'ask'`: mood `waiting`, note "needs you: <tool>". Return `r` unchanged.
- `turn.complete`: reason `answer`: mood `happy`, note "done", and after 6 s back to `idle` if still happy; `error` or `refusal`: mood `sad`, note "error"; `aborted`: `idle`. `next(e)`.
- `command.run` `{ command: 'companion' }`: toggle `isHidden`, then set the look to idle (a local command starts no turn, so nothing else would clear a `thinking` left by its `prompt.submit`); return `{ text: 'Companion hidden; /companion shows it again.' }` or `{ text: 'Companion shown.' }`.
- `ui.render` `{ component: 'AbovePrompt' }`: if `e.props.hasSurvey` or hidden, `next(e)`. Otherwise a `Box` row: the sprite, then a column with `Text bold` "ROBO · <mood>" and `Text dimColor` note, then a `Button key="hide" label="Hide"` whose press sets `isHidden`. When `e.props.bodyColumns < 36`, draw the sprite alone. Read state with `read($, atom)` so the drawing re-runs on writes; write only from handlers and other events with `update($, atom, fn)`.

### Sprite

14 pixels wide, 12 tall. Palette (Game Boy): `.` transparent (terminal default background; no rect in SVG), `1` `#9bbc0f`, `2` `#8bac0f`, `3` `#306230`, `4` `#0f380f`. Every row below is exactly 14 characters.

Base frame:

```
......4.......
......4.......
...44444444...
..4111111114..
..4144114414..
..4144114414..
..4111111114..
...44444444...
.442232232244.
44.22222222.44
...42222224...
...44....44...
```

Frames per mood, each a 12-row array of 14-character strings. "Base with row N = X" means the base frame with that one row replaced.

- idle: A = base. B (blink) = base with row 4 = `..4111111114..`. Show B on one tick in eight, A otherwise.
- thinking: A = base. B = base with row 0 = `......2.......` and row 1 = `......3.......` (the antenna glows). Alternate every tick.
- working: A = base. B = base with row 8 = `44422322322444` and row 9 = `...22222222...` (arms pumped up), and with the chest lights swapped: in B the `3` pixels of row 8 become `2` and the `2` pixels directly beside them become `3`. Alternate every tick.
- waiting: A = base with row 4 = `..4141114114..` (pupils left). B = base with row 4 = `..4114111414..` (pupils right). Row 5 stays as in the base. Alternate every tick.
- happy: A = base with row 4 = `..4141111414..` and row 5 = `..4114114114..` (smiling arcs). B = A shifted up one row (drop row 0, append a blank row of 14 dots), a bounce. Alternate every tick.
- sad: one frame: base with row 0 = `..............`, row 1 = `......44......` (the antenna droops), row 4 = `..4111111114..` (eyes low, pupils only on row 5).

Terminal encoding: one `Raster` with `key: "sprite"`, `columns: 14`, `rows: 6`. Cell (column c, row r) covers pixels (c, 2r) top and (c, 2r+1) bottom. Both opaque: glyph `▀` (upper half block), foreground = top color, background = bottom color. Top only: `▀` with background `0x01000000` (the terminal default). Bottom only: `▄` (lower half block), foreground = bottom color, background default. Neither: a space with both default. Colors are `0x00RRGGBB`. `cells` is standard padded base64 of little-endian u32 triplets `[codePoint, fg, bg]`, row-major, built with a `Uint32Array` and `new Uint8Array(words.buffer).toBase64()`.

SVG encoding (desktop, vscode, mobile): `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 12" width="56" height="48" shape-rendering="crispEdges">` with one `<rect x y width="1" height="1" fill>` per opaque pixel; `alt` = "ROBO, <mood>". Pass `width={56}` and `height={48}`.

### Tests (tests/companion.test.ts)

With `claude-code/testing`: mount `AbovePrompt` on `terminal` and assert the tree holds a `Raster` with key `sprite`; mount on `vscode` and assert an `Svg`; press `hide` and assert the next mount yields nothing of the plugin's; mount with `props.hasSurvey: true` and assert the plugin yields. Run with `claude plugin test plugins/companion`; `claude plugin validate plugins/companion` must pass.

## Daily use

- **Console**: `claude agents` in a terminal lists every session across repos, lets you dispatch new background sessions (`claude --bg` in a repo), peek, attach, rename and stop them. `claude --bg -n "<name>" "<task>"` starts one.
- **Morning**: open any session, run `/harness:standup`.
- **A change**: `/harness:spec` (interview, spec file) then `/harness:build <spec>` (Opus implements, Fable reviews) then `/harness:ship [pr]`.
- **Context hygiene**: `/harness:handoff` before `/clear`; the session brief restores the thread on the next start.
- **Hours**: `/harness:worklog` at the end of a session on a billable project.
- **Learning**: `/harness:learn` when a session needed corrections.
- **Companion**: `/companion` toggles the sprite.

## Open items

- `docs/verification.md`: the claim-by-claim check of the starter kit, written when the verification workflow finishes.
- A per-project `verify` convention: FantasyHockey sets `CLAUDE_VERIFY_CMD`; HKTrack could set `scripts/verify.sh --fast`; jotform-glow-sync stays as is (its `check` skill and `verifier` agent run `make check` on demand).
