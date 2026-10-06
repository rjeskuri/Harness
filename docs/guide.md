# Your Claude Code harness: first-run guide

Written 2026-10-06. The living copy, with the drawing of the daily loop, is the Claude doc at https://claude.ai/code/artifact/2950d172-2435-48a1-9dde-9670fa499c0e

## What you have now

The harness is a set of files that shape every Claude Code session, so you stop repeating yourself and Claude stops guessing your rules. Five pieces, each with one job.

| Piece | What it is | What it does for you |
| --- | --- | --- |
| Personal instructions, `~/.claude/CLAUDE.md` | A short text file Claude reads at the start of every session, in every repo | Carries your preferences and the routing rule: Fable designs and reviews, Opus writes the code, Sonnet does trivial jobs |
| The `harness` plugin | Four agents, seven slash commands and four hooks, installed once for your user | Agents are specialists Claude delegates to. Slash commands are procedures you start by typing `/harness:<name>`. Hooks are small scripts Claude Code itself runs at fixed moments, whatever the model thinks |
| The `companion` plugin | A mod that draws the Game Boy robot above the prompt | Shows at a glance whether Claude is idle, thinking, working, waiting on you, done or stuck |
| The `claude` command | The native command-line program, now on your PATH | Runs sessions in a terminal and opens the console that lists every session across your repos |
| The Harness repo, `~/portfolio_projects/Harness` | The source of all of the above, under git | Edit a skill, an agent or a hook there, run `/reload-plugins`, and every project picks it up |

Each project keeps its own `CLAUDE.md`, hooks, rules and skills. The harness adds to them and never replaces them: jotform-glow-sync's live gate and HKTrack's rules stay exactly as they are.

## Day one: finish the install and open your first session

Eight steps, about twenty minutes, all in a terminal.

1. **Wait for the build to land.** The Harness session will report that the hook scripts, the tools and the robot passed their tests and that both plugins are installed. To redo the install at any later time, run `sh ~/portfolio_projects/Harness/scripts/install.sh`.
2. **Open a new terminal window.** The PATH change applies only to terminals started after today. `claude --version` prints 2.1.285.
3. **Start a session in one repo.** `cd ~/Projects/HKTrack`, or any project, then `claude`. The first time in a folder Claude Code asks whether you trust it; answer yes for your own repos, because project hooks run only in trusted folders.
4. **Look around.** Type `/hooks` and expect Notification, SessionStart, PostToolUse and Stop entries from the harness plugin. Type `/plugin`, open the Installed tab, and see harness and companion. Type `/` and scroll to the `/harness:` commands. The robot sits above the prompt; `/companion` hides it, and again shows it.
5. **Register your projects.** In that session, ask Claude to run `harness-projects discover`, then `harness-projects add <path>` for each repo it found. Mark the client work billable: `harness-projects add ~/jotform-glow-sync --billable --client "Global G.L.O.W."`. Outside a session the tool lives at `~/portfolio_projects/Harness/plugins/harness/bin/harness-projects`.
6. **Run the first brief.** `/harness:standup` lists each project's state, its next steps and the sessions that are open.
7. **VS Code needs nothing extra.** The extension reads the same settings files, so the agents, commands and hooks are there too, and the robot draws as an SVG in the panel.
8. **Tidy FantasyHockey when convenient.** Its repo has the kit installed but no commits yet. A session there can commit it, and its verify-on-stop is already configured through `CLAUDE_VERIFY_CMD`.

## The console: several projects at once

`claude agents`, run in a terminal, is the console. It lists every Claude session on this Mac, whether started in a terminal or in VS Code, with its repo and its status: running, idle, or waiting on a permission answer. From there you peek at a session's output, attach to it, rename it, stop it, or dispatch a new one into a repo.

| You want to | Run |
| --- | --- |
| See every session | `claude agents` (`--json` for scripts, `--cwd <path>` to filter by repo) |
| Start a session that works while you do something else | `cd <repo>` then `claude --bg -n "<name>" "<task>"`; it prints a short id |
| Read a background session's recent output | `claude logs <id>` |
| Take over a background session in this terminal | `claude attach <id>` |
| Stop one but keep its conversation | `claude stop <id>`; later `claude attach <id>` or `claude --resume` |
| Delete one, and its worktree when safe | `claude rm <id>` |
| Let two sessions work on one repo without colliding | `claude --worktree` (or `-w <name>`) starts in a fresh git worktree |

Notifications arrive on their own: a macOS banner names the repo when a session needs a permission answer, has gone idle waiting for you, or a background session needs input or has finished. Clicking it brings VS Code forward. To bring a terminal forward instead, set `HARNESS_NOTIFY_APP` to that app's bundle id under `env` in `~/.claude/settings.json`.

Rule of thumb: one session per project, and two sessions never edit the same checkout at the same time unless one of them runs in a worktree.

## A working day

1. **Standup**: `/harness:standup`, where things stand, pick one project.
2. **Open the project**: `cd` into the repo, then `claude`; one session per repo; the brief loads itself.
3. **Spec the change**: `/harness:spec`, an interview then a spec file; skip for small fixes.
4. **Build**: `/harness:build`, Opus implements, Fable reviews, repeated until the reviewer passes it.
5. **Ship** when tests are green: `/harness:ship [pr]`, verify, review, commit, optional pull request.
6. **Hand off** when stopping mid-task: `/harness:handoff`, then `/clear`.
7. **Log hours** on billable projects: `/harness:worklog`, hours and a client note.
8. **Next project**: back to the brief, or done for today.

Build repeats until the reviewer passes the change; a small fix skips the spec and goes straight to build or to a plain request. Each box is one slash command, so a day is the loop run once per project you touch.

## The routing rule and the robot, in practice

For anything beyond a small edit, the main session should say it is handing the work to `harness:coder`, a subagent line appears, and the result comes back as a short report with the commands it ran. Before saying done, it should call `harness:reviewer`. The rule is advice in your CLAUDE.md, not enforcement: if a session starts writing a large change itself, say "use the coder", and run `/harness:learn` at the end so the lesson gets recorded.

The robot tells you the session's state without reading the transcript.

| Robot | Meaning |
| --- | --- |
| Blinks now and then | Idle, nothing running |
| Antenna glows | Thinking: your prompt is being worked on, no tool yet |
| Arms pump, the note names the tool | Working: running a command, editing a file, searching |
| Eyes look left and right, the note says "needs you" | A permission prompt is waiting for your answer |
| Bounces with smiling eyes | The turn finished |
| Antenna droops | A tool call failed or the turn ended in an error |

The VS Code panel and the desktop app draw it as an SVG. A terminal draws colored cells: VS Code's terminal, iTerm2 and Ghostty show its four greens exactly, Terminal.app the nearest of 256 colors.

Quota is the thing to watch on a subscription. `/usage` shows the five-hour and seven-day windows, `/cost` what this session has used, `/context` what fills the window. Fable is the most expensive tier, so the routing rule keeps most tokens on Opus. For a simple session you can run the orchestrator itself on Opus with `/model opus`, and `/effort` changes the reasoning effort for the session. The defaults live under `modelSettings` in `~/.claude/settings.json`.

## When something looks wrong

Most problems have a one-command check.

| Symptom | Check |
| --- | --- |
| `/hooks` lists nothing from the harness | Trust the folder (start `claude` there and accept), then `/reload-plugins`. `claude --debug hooks` shows every hook run with its exit code |
| A plugin shows an error | `/plugin`, Errors tab. In a terminal, `claude plugin list` and `claude plugin validate ~/portfolio_projects/Harness` |
| No robot above the prompt | `/companion` toggles it. A survey can hold the band for a while, and a window under 36 columns shows the sprite alone. `claude --debug` writes a line starting `ui.render (AbovePrompt)` when a drawing was refused |
| Claude keeps working after you expected it to stop | Verify-on-stop ran the project's `CLAUDE_VERIFY_CMD` and it failed; the reason is in the transcript. It gives up after two tries. Raise `CLAUDE_VERIFY_TIMEOUT`, or remove the `env` entry to turn it off |
| An edit was blocked for a secret | Move the value to the environment or a gitignored file. A fixtures file with fake tokens can carry the line `harness: allow-secrets` |
| No notifications | `brew list terminal-notifier`; allow terminal-notifier under System Settings, Notifications; check that a Focus mode is not hiding banners |
| Standup says the fleet could not be read | The `claude` binary was not found. The tool looks on PATH, then in `~/.local/bin` |
| Everything misbehaves | `claude --safe-mode` starts with every customization off, to narrow it down. `"disableAllHooks": true` in a project's `.claude/settings.local.json` pauses hooks there. `claude doctor` checks the install |

## Habits that keep it efficient

- One session per project. `/clear` between unrelated tasks, and `/harness:handoff` first when you stop mid-task. `/resume` reopens an earlier conversation.
- Let the harness delegate. Ask for `harness:coder` when a session starts coding a big change itself, and for `harness:reviewer` before you accept "done".
- Spec anything that will take more than an hour with `/harness:spec`. Small fixes go straight to a request.
- Keep each project's CLAUDE.md under about 60 lines. File-specific rules go in `.claude/rules/`. After a session with corrections, `/harness:learn` proposes the diffs; apply only the ones you agree with.
- Watch `/context`. Compaction summarizes the conversation on its own near the limit; `/compact <what to keep>` does it early when the window is full of noise. Esc interrupts a turn that is heading the wrong way.
- Long tasks run in the background with `claude --bg`, and the notifications call you back.
- Check `/usage` once a day. If the five-hour window runs short, lower Opus or Sonnet effort under `modelSettings`, or run a simple session on `/model opus`.
- Keep secrets out of commands. The scan blocks them in files, but a key typed on a command line still lands in transcripts and allow rules.
- End every billable session with `/harness:worklog`, and run it at least every two weeks: Claude Code deletes a session's transcript about 30 days after its last activity, and the hours come from those transcripts.

## Glossary

| Term | Meaning here |
| --- | --- |
| Session | One conversation with Claude Code in one folder, with its own context window |
| Context window | What the model can see right now: your messages, files it read, tool output. It fills up, which is why compaction and handoffs exist |
| Compaction | Claude Code summarizing the conversation to free space. Automatic near the limit, manual with `/compact` |
| CLAUDE.md | Instructions loaded at session start. `~/.claude/CLAUDE.md` applies in every repo; a project's own file adds to it |
| Rule | A markdown file under `.claude/rules/` that loads only when matching files are touched |
| Hook | A script Claude Code runs at an event, such as before a tool call, after an edit or when Claude tries to stop. Deterministic, unlike instructions |
| Skill, slash command | A procedure written in a SKILL.md and started by typing `/name`. The harness ones are `/harness:<name>` |
| Subagent | A separate Claude with its own context, model and tools, given one task, returning a report. `harness:coder` and `harness:reviewer` are subagents |
| Plugin | A package of agents, skills, hooks and mods installed as one unit. A marketplace is a catalog of plugins; `ryan` is yours |
| Mod | A plugin module that draws in the interface. The robot is one |
| Worktree | A second checkout of a git repo in another folder, so two sessions can edit without colliding |
| Permission mode | How Claude Code decides what runs without asking: allow, ask and deny rules, plan mode, auto mode |
| Effort | How much reasoning the model spends per turn, from low to max. Set per model or per agent |
| Fleet, console | `claude agents`: the list of every session on the machine |
| Handoff | `docs/agent/handoff.md`, the note a session writes so the next one can continue |
| Spec | `docs/agent/specs/<date>-<slug>.md`, the contract for a change before it is built |
| Workflow | A script that runs many subagents with fixed control flow. The orchestrator may launch one for large or parallel work |
