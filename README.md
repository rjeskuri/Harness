# Harness

Ryan's personal Claude Code harness, v2: one plugin installed once at user scope, applied to every project. The board is the context: every unit of work is a GitHub issue, an epic with task sub-issues, and agents read the ticket before they work and write status back to it. Tests come first and are proven to fail before any source changes. Nothing happens on main: each ticket gets its own branch and worktree, and work lands through a pull request. Your part is to plan, answer tickets that need input, and land pull requests.

Built 2026-10-13 against Claude Code 2.1.296 and gh 2.99.0. New to the CLI? Start with the [first-run guide](docs/guide.md). The design and every contract live in [docs/design.md](docs/design.md).

## What is in here

| Path | What it is |
|---|---|
| `.claude-plugin/marketplace.json` | The marketplace `ryan`, with one plugin: `harness` |
| `plugins/harness/agents/` | planner, test-author, test-breaker, coder, reviewer, researcher, quick |
| `plugins/harness/skills/` | plan, work, land, new-project, learn, standup, handoff, worklog |
| `plugins/harness/hooks/` | The hook scripts and `hooks.json` |
| `plugins/harness/bin/` | The nine `harness-*` tools, on the Bash PATH in every session |
| `plugins/harness/templates/` | Ticket, PR body and project scaffold templates |
| `plugins/harness/tests/` | Offline tests for the hooks and tools, with a fake `gh` |
| `user/CLAUDE.md` | Personal instructions for every project; installed at `~/.claude/CLAUDE.md` |
| `user/settings.snippet.json` | Model and effort settings merged into `~/.claude/settings.json` |
| `scripts/install.sh` | The installer |
| `docs/` | Design, first-run guide, verification report, research notes |

## Install

```sh
sh scripts/install.sh
```

It adds the marketplace, installs the plugin, copies `user/CLAUDE.md` to `~/.claude/CLAUDE.md` with a dated backup, merges the model settings into `~/.claude/settings.json` with a dated backup, and installs the daily digest with `harness-digest --install-launchd`. Safe to run again. The marketplace loads from this folder in place, so an edit here applies at the next session or after `/reload-plugins`.

Once, store the API key that `/harness:work --api` runs bill to. The command prompts for the key, so it never touches a command line:

```sh
security add-generic-password -a "$USER" -s harness-anthropic -w
```

Then register your projects: `harness-projects discover`, and `harness-projects add <path> [--billable --client <name>]` for each one.

## The loop

1. `/harness:new-project [path]` interviews you, scaffolds the repo, creates the private GitHub repository and labels, and registers it.
2. `/harness:plan <request>` interviews you, has the planner propose an epic and tasks, and creates the issues once you approve.
3. `/harness:work` dispatches one background session on the subscription per ready ticket (`/harness:work --api`: headless runs on the API key); `/harness:work N` runs ticket N in this session.
4. A ticket that needs you gets a comment and a notification; reply in a comment, then `/harness:work N` picks up the answer.
5. `/harness:land` lists PRs awaiting you; `/harness:land P` summarizes one, asks once, squash-merges, cleans up and closes the ticket.

## Agents and routing

| Agent | Model | Job |
|---|---|---|
| `planner` | Opus, xhigh | Turns an interviewed request into an epic and tasks; creates nothing |
| `test-author` | tier | Writes the tests a ticket names and shows them red; may not edit source |
| `test-breaker` | tier | Tries to pass the new tests with a cheat in a scratch copy; may not edit tests |
| `coder` | tier | Makes the tests and the verify command green, commits; may not edit tests |
| `reviewer` | Opus (Fable, xhigh, for risk:high), unless the project sets `HARNESS_REVIEW_TIER` | Reviews the branch against the definition of done; findings go on the PR |
| `researcher` | Opus | Answers from primary sources |
| `quick` | Sonnet | Trivial mechanical tasks outside the ticket loop |

The main session runs Opus 5.5 at high effort. `harness-board tier N` picks the tier from the ticket's labels: `size:S` and `risk:low` on a non-bug gives Sonnet; `size:L` or `risk:high` gives Fable; anything else gives Opus. A project can replace that rubric in its `.claude/settings.json`: `HARNESS_CODE_TIER` and `HARNESS_CODE_EFFORT` set the tier and effort of the test-author, breaker and coder, and `HARNESS_REVIEW_TIER` sets the reviewer's tier; the labels decide when they are unset. `harness-board tier N --json` says which decided and prints the effort, which the work loop passes on the Agent call when it is not null. Reviews run on Opus: by the labels, `harness-board tier N --stage review` gives Fable only for a `risk:high` ticket, and the review stage is recorded with `harness-work attempt N review pass|fail --tier <that output>`, so its second failure re-runs the review on Fable. Escalation is by failure, recorded with `harness-work attempt`: a stage's first failure retries at the same tier, the second re-runs one tier up, the third stops and asks on the ticket.

## Skills

| Skill | What it does |
|---|---|
| `/harness:new-project [path]` | Interview, scaffold, private GitHub repo, labels, registry entry |
| `/harness:plan [request]` | Interview, planner proposal, issues on the board after approval |
| `/harness:work [issue#]` | One ticket to a ready PR here, or dispatch every ready ticket |
| `/harness:land [PR#]` | PRs awaiting you, or one PR merged after one confirmation |
| `/harness:standup` | Every project: git state, board counts, PRs, needs-input tickets, stale worktrees, budgets |
| `/harness:learn` | Lessons from the session become `chore` tickets, not edits |
| `/harness:handoff` | Writes `docs/agent/handoff.md` before `/clear` |
| `/harness:worklog` | Hours and a client note on a billable project |

## Hooks

| Hook | When | What it enforces |
|---|---|---|
| notify | permission prompt, idle, a background agent needs input or finished | A macOS notification naming the repo |
| session brief | start, resume, compaction | Branch and last commit; on a ticket branch the ticket, its definition of done and the red run; otherwise the board counts; exhausted budgets |
| secret scan | after every edit and Bash call | Flags a high-confidence secret in a changed file so Claude removes it |
| verify on stop | when Claude ends a turn after edits | Runs `CLAUDE_VERIFY_CMD` and hands failures back, at most twice |
| branch guard | before edits and Bash | Refuses edits, commits and pushes on main or master; refuses force pushes everywhere |
| tests first | before edits | On a `t/N-` branch, refuses source edits until a current red run exists; the test-author edits tests only, the coder and breaker never edit tests |
| read-only guard | before Bash | Keeps planner, reviewer and researcher from changing files |

Hooks run outside the sandbox with your permissions. Point `CLAUDE_VERIFY_CMD` and `HARNESS_TEST_CMD` only at commands you trust.

## Tools

| Tool | What it does |
|---|---|
| `harness-board` | The board: labels, epics, tasks, status, comments, needs-input, tier |
| `harness-work` | Start a ticket's branch and worktree, record attempts, dispatch runs, budgets, worktrees |
| `harness-red` | Prove the branch's tests fail before the implementation; the breaker's scratch copy |
| `harness-pr` | Open, review, check, mark ready, ultrareview and land pull requests |
| `harness-scaffold` | Set up a new or existing repo for the harness |
| `harness-digest` | The daily digest across projects; installs its launchd job |
| `harness-projects` | The project registry |
| `harness-standup` | The cross-project standup behind `/harness:standup` |
| `harness-worklog` | Hours from session transcripts for invoicing |

## Per-project settings

Under `env` in the project's `.claude/settings.json`; `.claude/settings.local.json` overrides it, and the process environment overrides both. The exception is `HARNESS_TEST_PATTERNS` and `HARNESS_SOURCE_EXEMPT`: they are read from the environment, then the main checkout's tracked `.claude/settings.json` only, never from a worktree or a `settings.local.json`.

| Variable | Meaning | Default |
|---|---|---|
| `CLAUDE_VERIFY_CMD` | Whole suite plus lint; verify on stop and the coder run it | none, verify on stop off |
| `HARNESS_TEST_CMD` | Runs named test files; `{files}` becomes the paths | detected from `uv.lock`, `pyproject.toml`, `package.json` |
| `HARNESS_TEST_PATTERNS` | Globs that mark a path as a test | `tests/**`, `test_*.py`, `*.test.js` and similar |
| `HARNESS_SOURCE_EXEMPT` | Globs tests-first never gates | docs, markdown, config, lock files |
| `HARNESS_WORKTREE_SETUP` | Command run in a fresh worktree | `uv sync` or `npm ci`, then `.worktreeinclude` files copied |
| `HARNESS_ALLOW_MAIN` | `1` turns the branch guard off for this project | unset |
| `HARNESS_TEST_TIMEOUT` | Seconds for a red run | 300 |

Machine-wide, under `env` in `~/.claude/settings.json`: `HARNESS_API_KEY_CMD` (default reads the keychain entry above), `HARNESS_NOTIFY_APP`, `HARNESS_NOTIFY_SOUND`, `HARNESS_SECRET_SCAN`, `CLAUDE_VERIFY_TIMEOUT`.

## Budgets

| Work | Bills to |
|---|---|
| Interactive sessions, including `/harness:work N` | The Max subscription |
| Dispatched runs, `/harness:work` (the default) | The Max subscription |
| Dispatched headless runs, `/harness:work --api` or `--venue headless` | The API key, read from the keychain |
| Ultrareview on `risk:high` PRs, unless the project sets `HARNESS_ULTRAREVIEW=off` | claude.ai usage credits, about 5 to 25 dollars a run |

When a headless run fails on billing or auth, the API budget is marked exhausted for 24 hours, and until then `--api` and `--venue headless` are refused; dispatch without them still runs on the subscription. A credits failure on ultrareview marks the cloud budget the same way. `harness-work budget status` shows the state; `harness-work budget reset api` clears it after a top-up.

## Troubleshooting

- `claude plugin validate .` checks the marketplace and the plugin.
- `python3 -m unittest discover -s plugins/harness/tests -v` runs the hook and tool tests.
- `claude --debug` logs every hook run with its exit code to `~/.claude/debug/<session-id>.txt`; nothing prints in the terminal. `/plugin` shows load errors on its Errors tab.
- `HARNESS_ALLOW_MAIN=1` under `env` in a project's `.claude/settings.local.json` turns the branch guard off there. It is the escape hatch, not a habit.
- `"disableAllHooks": true` in `.claude/settings.local.json` pauses every hook in that project.
