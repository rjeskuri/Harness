# Harness

Ryan's personal Claude Code harness: one repository that installs the same agents, skills, hooks and a companion sprite into every project, without touching any project's own guardrails.

Built 2026-10-06 against Claude Code 2.1.285 (CLI) and 2.1.289 (VS Code extension). New to the CLI or to harnesses? Start with the [first-run guide](docs/guide.md). The design and every contract live in [docs/design.md](docs/design.md). The starter kit this grew out of is kept under [claude-code-starter-macos/](claude-code-starter-macos/), and its claims were checked one by one in [docs/verification.md](docs/verification.md).

## What is in here

| Path | What it is |
|---|---|
| `.claude-plugin/marketplace.json` | The marketplace `ryan`, with the two plugins below |
| `plugins/harness/` | Agents (`coder`, `quick`, `reviewer`, `researcher`), skills (`spec`, `build`, `ship`, `handoff`, `learn`, `standup`, `worklog`), hooks (notify, session brief, secret scan, verify on stop), command-line tools (`harness-standup`, `harness-worklog`, `harness-projects`) |
| `plugins/companion/` | The Game Boy robot that lives above the prompt |
| `user/CLAUDE.md` | Personal instructions for every project, including the model routing rule; installed at `~/.claude/CLAUDE.md` |
| `user/settings.snippet.json` | The model and effort settings merged into `~/.claude/settings.json` |

## Install

```sh
claude plugin marketplace add /Users/ryaneskuri/portfolio_projects/Harness
claude plugin install harness@ryan
claude plugin install companion@ryan
cp user/CLAUDE.md ~/.claude/CLAUDE.md
```

Both plugins install at user scope, so they apply in every project on this machine. A marketplace added from a local path loads its plugins in place: edit a file here, then `/reload-plugins` in an open session or start a new one.

Check it: `claude plugin list` shows both plugins; in a session `/hooks` lists Notification, SessionStart, PostToolUse and Stop, `/` shows the `/harness:` skills, and the robot appears above the prompt.

## Model routing

| Role | Who | Model |
|---|---|---|
| Design decisions, orchestration, final review | the main session | Fable |
| Implementation | `harness:coder` | Opus |
| Trivial mechanical tasks | `harness:quick` | Sonnet |
| Fresh-context review | `harness:reviewer` | Fable |
| Fact-checking and research | `harness:researcher` | Opus |

Effort is xhigh for every tier. Change it per agent with the `effort` field in the agent's frontmatter, or per model under `modelSettings` in `~/.claude/settings.json`. The rule that makes the main session delegate is in `user/CLAUDE.md`.

## Daily use

- **Console.** In a terminal, `claude agents` lists every session across every repo, waiting ones first, and lets you peek, attach, rename and stop them. `claude --bg -n "<name>" "<task>"` in a repo starts a background session; `claude attach <id>` opens one.
- **Morning.** In any session, `/harness:standup`: one section per registered project with git state, next steps, hours this month on billable work, and the sessions that are running or waiting. First time: `harness-projects discover`, then `harness-projects add <path> [--billable --client <name>]`.
- **A change.** `/harness:spec <idea>` interviews you and writes `docs/agent/specs/<date>-<slug>.md`. `/harness:build <spec>` has Opus implement it and Fable review it. `/harness:ship [pr]` verifies, reviews, commits and optionally opens a PR.
- **Context.** `/harness:handoff` writes `docs/agent/handoff.md` before you `/clear`; the next session is briefed from it automatically.
- **Hours.** `/harness:worklog` at the end of a session on a billable project recomputes the day's hours from the session transcripts and writes a client-facing description.
- **Lessons.** `/harness:learn` turns a session's corrections into proposed edits to CLAUDE.md, rules, skills or hooks.
- **Companion.** `/companion` hides or shows the robot.

## Hooks

| Hook | When | What |
|---|---|---|
| notify | a permission prompt, idle, a background agent needs input or finished | macOS notification naming the repo (terminal-notifier, else AppleScript, else the terminal bell) |
| session brief | session start, resume, after compaction | Branch, uncommitted count, last commit, next steps from `docs/agent/handoff.md` or `docs/PROGRESS.md`, pending items from `docs/plans/feature_list.json`, hours this month. Skipped in a project that has its own SessionStart hook |
| secret scan | after every edit or write | Blocks the edit when the file holds a high-confidence secret pattern (cloud, GitHub, Slack, OpenAI, Anthropic, Stripe, private keys, Jotform-style keys) and tells Claude to remove it |
| verify on stop | when Claude tries to end a turn after editing | Runs the project's `CLAUDE_VERIFY_CMD` and hands failures back, at most twice in a row. Opt-in per project |

## Per-project setup

A project needs nothing to benefit from the harness. To get more out of it:

- A lean `CLAUDE.md` with the commands, where things live, conventions and "done means".
- `CLAUDE_VERIFY_CMD` in `.claude/settings.json` under `env` to turn on verify-on-stop, for example `"CLAUDE_VERIFY_CMD": "scripts/verify.sh --fast"`.
- `docs/agent/` for specs and the handoff note.
- `worklog/` on a billable project; `harness-worklog init` creates the time-zone timeline.

Projects with their own hooks, skills and agents keep them; the harness adds to them.

## Customizing

- Notifications: `HARNESS_NOTIFY_APP` (bundle id to bring forward, default VS Code) and `HARNESS_NOTIFY_SOUND` (a macOS sound name, empty for silent) in `~/.claude/settings.json` under `env`.
- Secret scan: `HARNESS_SECRET_SCAN=0` turns it off; a file containing `harness: allow-secrets` is skipped.
- Verify on stop: `CLAUDE_VERIFY_TIMEOUT` seconds (default 300).

## Troubleshooting

- `claude plugin validate .` checks the marketplace and both plugins; `claude plugin test plugins/companion` runs the mod's tests; `python3 -m unittest discover -s plugins/harness/tests -v` runs the hook and tool tests.
- `claude --debug hooks` shows every hook run with its exit code and output. `/plugin` in a session shows load errors on its Errors tab.
- A hook that misbehaves can be paused for one session with `"disableAllHooks": true` in `.claude/settings.local.json`.
