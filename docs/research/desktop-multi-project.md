# Research notes: desktop multi project

Gathered 2026-10-06 by an Opus research agent from the official Claude Code docs, the changelog and the installed binary. Quotes are the sources' words; judgments are the agent's and were read by the harness designer before use.

## Findings

## 0. What's on this Mac (checked read-only, 2026-10-06)

- **No `claude` on PATH.** `zsh -lic 'which claude'` returns `claude not found`. The only CLI is the copy bundled inside the VS Code extension: `~/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude`, and `--version` prints `2.1.289 (Claude Code)`. The VS Code docs explain why: "Installing the extension does not put `claude` on your shell PATH." The newest version in the GitHub CHANGELOG is **2.1.291**. To get CLI-only features (agent view, `--worktree`, `claude remote-control` server mode, `claude doctor`), install the standalone CLI: `curl -fsSL https://claude.ai/install.sh | bash` or `brew install --cask claude-code` (https://code.claude.com/docs/en/setup).
- **The Claude Desktop app is not installed.** There is no Claude.app in /Applications or ~/Applications, and `mdfind` for `com.anthropic.claudefordesktop` finds nothing. VS Code runs from `~/Desktop/Visual Studio Code.app`. Because of this, every Desktop claim below comes from the docs only. None of it could be tried here.
- **Auth variables are unset** (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`). That matters because CHANGELOG 2.1.139 says: "Remote Control, `/schedule`, claude.ai MCP connectors, and notification preferences are now disabled when `ANTHROPIC_API_KEY` / `apiKeyHelper` / `ANTHROPIC_AUTH_TOKEN` is set".
- **Current `~/.claude` state:** `settings.json` has `agentPushNotifEnabled: true`. It has no `inputNeededNotifEnabled` and no `remoteControlAtStartup`. There is no `~/.claude/CLAUDE.md`, and no `rules/`, `agents/`, `hooks/` or `workflows/` folder yet.

## 1. Desktop sidebar and parallel sessions across repos: **confirmed**
Source: https://code.claude.com/docs/en/desktop
- "In the Code tab, each conversation is a **session**: it has its own chat history and project folder, independent of any other session. The sidebar lists your sessions and lets you run several in parallel."
- "Use the controls at the top of the sidebar to filter sessions by status, project, or environment, and to group sessions by project."
- Split view: "hold **Cmd** ... and click a session in the sidebar. The session opens in a second pane". Switching: "Press **Ctrl+Tab** and **Ctrl+Shift+Tab** to cycle through sessions".
- Working across sessions has a limit: "Claude sees only the sessions the desktop app runs itself: local, SSH, and WSL sessions in the Code tab. Claude doesn't see cloud sessions, or sessions you started from the terminal CLI or the VS Code extension ... By default it sees the 20 most recently active sessions".
- Desktop and CLI together: "You can run both simultaneously on the same machine, even on the same project. Each keeps its own session list". To move a session over, run `/desktop` or `claude --desktop` (needs v2.1.285 or later). From inside Desktop, `/resume` picks up CLI sessions.
- VS Code equivalent (https://code.claude.com/docs/en/vs-code):
  - "Use **Open in New Tab** or **Open in New Window** ... to work on different tasks in parallel".
  - Session groups need v2.1.229 or later, but "The extension saves groups per workspace folder". There is no list that spans all your repos.
  - Status filters need v2.1.271 or later.

## 2. Worktrees, `.claude/worktrees/` and `.worktreeinclude`: **confirmed, with caveats**
Source: https://code.claude.com/docs/en/worktrees
- "By default, the worktree is created under `.claude/worktrees/<name>/` at your repository root, on a new branch named `worktree-<name>`". The `--worktree`/`-w` flag dates from 2.1.49.
- `.worktreeinclude`: "uses `.gitignore` syntax. Only files that match a pattern and are also gitignored are copied". It "applies to every worktree Claude Code creates with git: `--worktree` worktrees, subagent worktrees, and parallel sessions in the desktop app."
  - Caveat: when a `WorktreeCreate` hook is set, ".worktreeinclude is not processed".
- Desktop: "select the **worktree** option next to the branch name". Also: "Worktrees are stored in `<project-root>/.claude/worktrees/` by default. You can change this to a custom directory in Settings → Claude Code under 'Worktree location'". There is also a branch prefix option, plus an archive icon and auto-archive on PR merge.
  - Caveat: if you move the worktree location, a `.claude/worktrees/` line in `.gitignore` no longer covers it.
- Other settings:
  - `worktree.baseRef`: `"fresh"` (the default) or `"head"`.
  - `worktree.bgIsolation: "none"` (since 2.1.143).
- Shared across worktrees: the `.git` directory, project-scope plugins, "don't ask again" approvals (saved to the main checkout's `.claude/settings.local.json`), and untracked skills, agents and commands.
- Agent view runs each background session in "an isolated git worktree under `.claude/worktrees/`" before it edits anything.
- The local binary contains `.worktreeinclude` (4 hits), `bgIsolation` (12), `EnterWorktree` (20) and `ExitWorktree` (12).
- VS Code's `package.json` registers a command, `claude-vscode.createWorktree` ("Claude Code: Create Worktree"). It only shows when a `claude-vscode.createWorktreeEnabled` context flag is set, and it isn't documented anywhere. Don't rely on it.

## 3. Cloud sessions, Remote Control, and handing off to web/mobile: **confirmed**
- **Cloud** (https://code.claude.com/docs/en/claude-code-on-the-web): "available on Pro, Max, and Team plans". Moving sessions between surfaces has limits:
  - CLI: "session handoff is one-way: you can pull cloud sessions into your terminal with `--teleport`, but you can't push an existing terminal session to the cloud." `--cloud "<task>"` creates a *new* cloud session.
  - Desktop can send a local session to the cloud with **Open in > Cloud**, "with your conversation carried over as a summary".
  - Desktop cloud sessions can include several repos: "click the **+** button next to the selected repository".
  - VS Code session history has a **Web** tab. Resuming from it "downloads a copy of the conversation history; changes don't sync back to claude.ai."
- **Remote Control** (https://code.claude.com/docs/en/remote-control):
  - "available on Pro, Max, Team, and Enterprise plans. API keys are not supported."
  - Start it with `claude remote-control` (server mode), `claude --remote-control`, or `/remote-control`. In VS Code and Desktop it's the slash command.
  - Server mode options: `--spawn worktree` ("each on-demand session gets its own git worktree") and `--capacity <N>` ("Default is 32").
  - Auto-connect: `remoteControlAtStartup: true` in `~/.claude/settings.json`, or Desktop **Settings > Claude Code > Connect new sessions to Remote Control**.
  - Limitation: "If you close the terminal, quit the Desktop app or VS Code, or otherwise stop the `claude` process, the session goes offline".
- **Mobile** (https://code.claude.com/docs/en/mobile):
  - "Claude Code doesn't have a separate mobile app". Sessions live in the Claude app's **Code** tab.
  - "you can't select Auto for a Remote Control session."
  - Dispatch (phone to Desktop) "requires a Pro or Max plan".
- **Caveat for your Fable rule:** "The Fable usage-credits consent prompt isn't forwarded". If a Fable request needs that consent while you're driving the session from your phone, the turn can end without running.

## 4. Routines and scheduled agents: **confirmed** (routines are a research preview)
There are three tiers. All quotes are from https://code.claude.com/docs/en/desktop-scheduled-tasks and https://code.claude.com/docs/en/scheduled-tasks unless noted.

**Cloud routines** (https://code.claude.com/docs/en/routines)
- "Routines are in research preview. Behavior, limits, and the API surface may change."
- Available on Pro, Max, Team and Enterprise.
- Where to create them: claude.ai/code/routines, Desktop **Routines** (choose Cloud), or "Run `/schedule` in any session ... also available under the alias `/routines`."
  - The binary contains the description "Create, update, list, or run scheduled cloud agents (routines) that execute on a cron schedule." with `aliases:["routines"]`.
- Schedule: "The minimum interval is one hour". Custom cron expressions are set with `/schedule update`.
- Routines run with no permission prompts. They start from a fresh repo clone and get MCP access only through your claude.ai connectors.
- Limits: 100 scheduled runs per hour per account.

**Desktop local scheduled tasks**
- "only fires while the app is open and your computer is awake".
- Each task gets its own permission mode and an optional worktree toggle.
- Missed runs: one catch-up run covering up to 7 days.
- The prompt is stored at `~/.claude/scheduled-tasks/<task-name>/SKILL.md`.
- Minimum interval is 1 minute.
- The Desktop-only `update_scheduled_task` tool is absent from the CLI binary (0 hits).

**`/loop`**
- "Tasks are session-scoped".
- "Recurring tasks automatically expire 7 days after creation."
- Can be turned off with `CLAUDE_CODE_DISABLE_CRON=1`.

## 5. Push notifications and the Notification hook: **partially confirmed**
The setting is real, but on its own it does nothing for you right now.

**`agentPushNotifEnabled`** (https://code.claude.com/docs/en/settings-reference)
- "pushes arrive while Remote Control is connected. Appears in `/config` as **Push when Claude decides**." The default is `false`. Added in CHANGELOG 2.1.110.
- Its sibling, `inputNeededNotifEnabled` ("Push when actions required"), is also `false` by default.
- "Beyond the two on/off toggles below, there is no per-event configuration."
- `CLAUDE_CLIENT_PRESENCE_FILE` (added 2.1.181) suppresses pushes while you're at the machine.
- The binary has the strings "Push when Claude decides" (4), "Push when actions required" (2) and "No mobile registered" (2).
- **Fix for your setup:** you have `agentPushNotifEnabled: true`, but you get no pushes unless Remote Control is connected. Add `"inputNeededNotifEnabled": true` and `"remoteControlAtStartup": true` to `~/.claude/settings.json`, or run `/remote-control` per session.

**Built-in Desktop notifications** (no hook needed)
- "The desktop app sends an OS notification when a Code session finishes a task and you aren't currently viewing that session."
- Scheduled tasks: "When a task fires, you get a desktop notification".
- Projects have desktop-only notifications.

**Notification hook** (https://code.claude.com/docs/en/hooks)
- Matchers include `permission_prompt`, `idle_prompt`, `agent_needs_input` and `agent_completed`. The last two fire "only while agent view is open in a terminal".
- How Desktop and VS Code differ: "the Agent SDK's `canUseTool` callback, which is how Claude Desktop and the VS Code extension host Claude Code". In those hosts, `permission_prompt` fires about 6 seconds after the request (since v2.1.233).
- Bell fallback: `terminalSequence` "only in an interactive session ... in the Agent SDK, it ignores the field."
  - My inference: the kit's BEL fallback probably does nothing in the VS Code panel or Desktop. An `osascript` call (the hooks guide's own example) is the reliable route. Note the guide's warning that if Script Editor lacks notification permission, "the command fails silently".
- If you switch to Desktop, the kit's `ACTIVATE_APP` should be `com.anthropic.claudefordesktop`. Evidence: it's the MDM preference domain the docs name, and the binary contains the macOS sign-in redirect `msauth.com.anthropic.claudefordesktop://auth`.
- Expect duplicate alerts in Desktop, since the app already notifies you itself (inference).

## 6. `/workflows`, "FleetView", `/artifacts` and artifacts

**`/workflows`: confirmed**, but it is not a dashboard (https://code.claude.com/docs/en/workflows)
- "Run `/workflows` at any time to list running and completed workflows, then select one to open its progress view". It covers dynamic-workflow runs in the current session only.
- Workflows are available in "the CLI, the Desktop app, the IDE extensions". "On Pro, turn them on from the Dynamic workflows row in `/config`."
- You can save a workflow script to `~/.claude/workflows/`.

**"FleetView": not documented anywhere**
- There are 0 hits in the docs (including env-vars), the docs changelog, the GitHub CHANGELOG, VS Code's `extension.js` and the webview.
- The 2.1.289 binary does contain internal names: `FleetViewScreen`, `isAgentsFleetEnabled`, `fleet_view_group_mode`, `fleet_view_pin_toggle`, `CLAUDE_CODE_FLEETVIEW_SIMPLE`, and "Set model for this FleetView session (not persisted)". They sit right next to the string `unavailable_in_agent_view`.
- My inference: FleetView is the internal code name for **agent view** (`claude agents`). Don't design around the name or its undocumented environment variable.

**Agent view** (https://code.claude.com/docs/en/agent-view)
- Research preview since v2.1.139. It runs in the terminal only.
- "every background session you've started, across all your projects". `Ctrl+S` groups the list by directory.
- From a parent folder, dispatch into a child repo by putting `@<repo>` in the prompt.
- `claude agents --json` "is the supported way to read session state from outside Claude Code". `claude agents --help` here confirms `--json`, `--all`, `--cwd` and `--model`.

**`/artifacts`: confirmed** (https://code.claude.com/docs/en/artifacts)
- "Run `/artifacts` in Claude Code to list every artifact you own and every artifact shared with you". Needs v2.1.208 or later.
- The gallery is a web page: "your gallery at claude.ai/code/artifacts".
- Availability is listed as "Claude Code CLI, or the Claude desktop app version 1.13576.0 or later". It needs a Pro, Max, Team or Enterprise plan, a claude.ai login, and the Anthropic API.
- VS Code: **partial**. It isn't in the availability table, but docs changelog 2.1.268 says "[VSCode] Changed some artifact permission prompts", so artifacts do work in the panel.

## 7. User-level config shared across projects
| Path | Verdict | Evidence |
|---|---|---|
| `~/.claude/CLAUDE.md` | confirmed | "Personal preferences for all projects" (https://code.claude.com/docs/en/memory). Also, CLAUDE.md files in parent folders above where you launch "are loaded at launch", so a `~/portfolio_projects/CLAUDE.md` would cover every repo under it |
| `~/.claude/settings.json` | confirmed | "You, in every project on this machine" (https://code.claude.com/docs/en/settings). Desktop: "Settings in `~/.claude.json` and `~/.claude/settings.json` are shared". Caveat: "Cloud sessions don't read your local `~/.claude/settings.json`" |
| `~/.claude/agents/` | confirmed | "`~/.claude/agents/` \| All your projects \| 4" (https://code.claude.com/docs/en/sub-agents) |
| `~/.claude/skills/` | confirmed | "All your projects on this machine, but not Cowork or cloud sessions" (https://code.claude.com/docs/en/skills) |
| `~/.claude/rules/` | **confirmed, it exists** | "Personal rules in `~/.claude/rules/` apply to every project on your machine ... Neither set overrides the other". Rules support dates from 2.0.64 |
| `~/.claude/hooks/` | **partially** | It isn't a folder Claude Code reads on its own. Hook locations are settings files, plugin `hooks/hooks.json`, and skill or subagent frontmatter. It works only as a place to keep scripts that `~/.claude/settings.json` points to |
| User-scope plugins | confirmed | "The entry goes in `enabledPlugins` in `~/.claude/settings.json`" and "a plugin you install at user scope in any of them is available in the other two" (terminal, Desktop local, VS Code). Not available in cloud sessions |
| `--add-dir` | partially | It grants file access. It loads skills, commands, agents, and only the `enabledPlugins` and `extraKnownMarketplaces` settings. CLAUDE.md and rules load only with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. Directories in `permissions.additionalDirectories` "grant file access only". Desktop lists `--add-dir` as cloud sessions' "+" multi-repo button only |
| Desktop Cowork tab | note | It pulls skills, plugins and connectors from claude.ai's **Customize** sync, "not from the CLI's `~/.claude` directory" |

## 8. Dashboards and the "projects" concept
- **Desktop sidebar:** filter and group by project (most likely meaning the session's project folder; the docs don't define it). Plus split view and cross-session messaging.
- **Projects** (https://code.claude.com/docs/en/claude-projects):
  - "Projects are in public beta on Pro and Max plans and rolling out gradually"; "not in the terminal CLI, the VS Code extension".
  - An **Overview** pane groups threads as Ready for review, Waiting on you, Working, Landing, Idle and Resolved.
  - "You can't add a session you started yourself on your machine to a project."
  - Threads need the Claude GitHub App on github.com.
  - **Coordinator model** and **Thread model** settings map directly onto your Fable-orchestrates, Opus-codes rule. "A new project runs Opus everywhere" by default.
- **Agent view:** the terminal dashboard that spans all your projects.
- **`/resume` picker:** `Ctrl+A` shows "sessions from all projects on this machine".
- **There is no single dashboard covering Desktop, CLI and VS Code sessions together.**

## 9. Checkpoints and rewind with several sessions on one repo
Source: https://code.claude.com/docs/en/checkpointing
- "Every prompt you send that starts a turn creates a new checkpoint". Snapshots are kept for the 100 most recent checkpoints and swept after about 30 days (`cleanupPeriodDays`).
- The key limit for parallel work: "Manual changes you make to files outside of Claude Code and edits from other concurrent sessions are normally not captured, unless they happen to modify the same files as the current session."
- Also not undone by rewind: Bash file changes, and edits from background subagents ("Use git to revert them").
- My inference: two sessions sharing one checkout and rewinding can overwrite each other. Use one worktree per session and commit often.
- VS Code: **confirmed**. "The VS Code extension supports checkpoints" with three rewind options.
- Desktop: **unverifiable**. The Desktop page never mentions rewind or checkpoints, the Desktop changelog URL just serves the same page, and the app isn't installed here.

## Things in the starter kit to fix
1. **README step 6** ("`claude doctor` in the repo", "Start `claude` in the repo once") fails on this Mac. Replace with: "Install the standalone CLI first (`curl -fsSL https://claude.ai/install.sh | bash` or `brew install --cask claude-code`). The VS Code extension's bundled CLI is not on your PATH."
2. **`notify.py` fallback 3** should read: "The terminal bell via `terminalSequence` works only in an interactive terminal session. Claude Code ignores it in the Desktop app and the VS Code chat panel, so rely on terminal-notifier or osascript there."
3. **Notification matcher:** add `agent_needs_input|agent_completed` if you use agent view.
4. **Version note:** the README says it was checked against 2.1.283. You have 2.1.289 and the latest is 2.1.291.

## Also relevant to your goals
- **Custom retro pixel art: Mods** (https://code.claude.com/docs/en/plugins/mods/overview). "only the terminal and the Desktop app show a mod's panes, bands, and replaced rows". The VS Code chat panel shows **no** mod drawing.
- **Status line:** the docs never say whether it shows in Desktop or VS Code.

## Reported as not existing

- "FleetView" as a user-facing feature, command, or setting. It has zero hits in the docs, both changelogs and the VS Code extension. It appears only as internal names in the 2.1.289 binary (e.g. FleetViewScreen, CLAUDE_CODE_FLEETVIEW_SIMPLE) and is most likely agent view's internal code name. Use `claude agents` instead.
- A single dashboard covering Desktop, terminal CLI and VS Code sessions together. Desktop's cross-session view covers only Desktop sessions (20 most recent). Agent view covers terminal background sessions. The VS Code session list is per workspace folder.
- Agent view inside Desktop or the VS Code panel. It is a terminal tool (`claude agents`) in research preview. Agent teams are explicitly not available in Desktop either.
- A `/workflows` view that spans projects. `/workflows` shows only dynamic-workflow runs in the current session.
- An artifacts gallery inside the Desktop app. The gallery is the web page claude.ai/code/artifacts; `/artifacts` is a list command in the CLI.
- A `~/.claude/hooks/` folder that Claude Code reads on its own. Hooks must be registered in settings.json, a plugin's hooks/hooks.json, or skill/subagent frontmatter. The folder is only a conventional place for script files.
- Mobile push notifications without Remote Control connected (agentPushNotifEnabled and inputNeededNotifEnabled both need it), and any per-event push settings: "there is no per-event configuration".
- Pushing an existing terminal session to the cloud from the CLI. Handoff is one-way (`--teleport` pulls in); `--cloud` only creates a new session. Desktop can do it via Open in > Cloud.
- Cloud routines that run more often than hourly ("The minimum interval is one hour"), Desktop local tasks that run while the app is closed or the Mac is asleep, and `/loop` tasks that last past 7 days.
- Personal ~/.claude skills, ~/.claude/settings.json hooks, or Desktop-installed plugins in cloud sessions, routines, or Project cloud threads.
- Adding a local session to a Project. Also, Projects on Team or Enterprise plans, or in the CLI or VS Code.
- `--add-dir` loading CLAUDE.md, rules, or hooks/permissions by default. CLAUDE.md needs CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1, and `permissions.additionalDirectories` grants file access only. Desktop documents a multi-repo button for cloud sessions only.
- Checkpoints that capture Bash file changes, edits from other sessions running at the same time, or background-subagent edits.
- A documented rewind/checkpoint feature in the Desktop Code tab. The docs are silent and the app isn't installed here to test.
- Mod panes and drawing in the VS Code chat panel (only the terminal and Desktop show them), and a documented custom status line in Desktop or VS Code.
- On this Mac today: a `claude` command on PATH (the extension doesn't add one) and the Claude Desktop app (not installed).
- A documented VS Code "Create Worktree" command. It is in package.json but hidden behind the `claude-vscode.createWorktreeEnabled` context flag and undocumented.

## Open questions

- Does `/rewind` (or Esc-Esc) work in the Desktop Code tab? The docs never mention it and the app isn't installed here to test.
- Does `/add-dir <path>` work in a local Desktop session? The docs list multi-repo only for cloud sessions via the + button.
- Does the Desktop sidebar's "group sessions by project" mean the session's project folder, or the Projects (public beta) feature? The docs don't define it.
- Is the hook `terminalSequence` field really ignored in the VS Code panel and Desktop? The docs say only "in the Agent SDK, it ignores the field" and that Desktop and VS Code host Claude Code through the SDK's canUseTool. Needs a test with the kit's notify.py BEL fallback.
- Does the `idle_prompt` Notification type fire in Desktop and VS Code sessions? Its timing is documented in terms of terminal typing.
- Does `claude agents --json` list sessions in the VS Code panel or Desktop as `interactive` entries? Not run here, because it may start the background supervisor and write ~/.claude/daemon state.
- Does the Desktop "Use this computer from your phone and claude.ai" setting let you start general remote sessions in the listed folders, or only Project threads? It is documented only on the Projects page.
- Which plan is the user on (Pro or Max)? This decides Dispatch, Projects availability and rollout (a waitlist is possible), and whether Fable bills to usage credits. The Fable consent prompt isn't forwarded over Remote Control.
- Has the Projects rollout reached this account? It only applies if the **Projects** item appears in the Desktop or claude.ai/code sidebar.

## Sources

- https://code.claude.com/docs/en/desktop
- https://code.claude.com/docs/en/desktop-quickstart
- https://code.claude.com/docs/en/desktop-scheduled-tasks
- https://code.claude.com/docs/en/worktrees
- https://code.claude.com/docs/en/agent-view
- https://code.claude.com/docs/en/agents
- https://code.claude.com/docs/en/claude-projects
- https://code.claude.com/docs/en/remote-control
- https://code.claude.com/docs/en/mobile
- https://code.claude.com/docs/en/claude-code-on-the-web
- https://code.claude.com/docs/en/routines
- https://code.claude.com/docs/en/scheduled-tasks
- https://code.claude.com/docs/en/settings-reference
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/hooks-guide
- https://code.claude.com/docs/en/terminal-config
- https://code.claude.com/docs/en/workflows
- https://code.claude.com/docs/en/artifacts
- https://code.claude.com/docs/en/commands
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/claude-directory
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/permissions
- https://code.claude.com/docs/en/plugins/install
- https://code.claude.com/docs/en/plugins/mods/overview
- https://code.claude.com/docs/en/checkpointing
- https://code.claude.com/docs/en/vs-code
- https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/cross-session-messaging
- https://code.claude.com/docs/en/platforms
- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/setup
- https://code.claude.com/docs/en/changelog
- https://code.claude.com/docs/llms.txt
- https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
- https://startdebugging.net/2026/06/claude-code-2-1-191-rewind-past-clear/
- /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude (--version, --help, agents --help, strings grep)
- /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/package.json
- /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/extension.js
- /Users/ryaneskuri/.claude/settings.json (key names and notification values only)
- /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/README.md
- /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/user/settings.json
- /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/user/hooks/notify.py
- /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/project/gitignore-additions.txt
- /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/project/.worktreeinclude
