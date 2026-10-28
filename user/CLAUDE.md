# How I work

- I am a solo developer across several repositories, experienced in Python and SQL. Skip the basics; when you recommend something, give the reasoning and the tradeoffs.
- Keep replies short. For code changes I will review inline, show before/after snippets of the changed lines rather than whole files.
- I am on macOS with zsh, Homebrew and VS Code. macOS ships BSD `sed`, `grep` and `date`: no `grep -P`, `sed -i` needs `''`, and `date -d` does not exist. There is no bare `python`, only `python3` or the project's own interpreter.
- When you finish a task, show the evidence (the command you ran and its result) and say plainly what you could not verify.
- Ask before adding dependencies or doing anything else hard to undo, except what the loop below allows. Never put a secret on a command line; read it from the environment, a file or the keychain.

## The board is the context

- Ticketed work is a GitHub issue on the project's board: an epic with task sub-issues, each with a request, an outline, a definition of done and the tests that must pass. Read the ticket (`harness-board show N --markdown`) and its epic before working; status changes go through `harness-board`.
- A question the ticket, the epic and the code cannot answer becomes `harness-board needs-input N --question ...` when it is about product behavior, is hard to undo, or touches security, credentials or personal data. A reversible implementation choice is decided and recorded as a `Ruling:` line under `### Rulings` in the PR's Notes. I answer on the ticket; the next `/harness:work N` reads the answer.
- New work: `/harness:plan <request>`. Tickets to pull requests: `/harness:work [N]`. I land every pull request, with `/harness:land [PR#]` or by approving the land prompt; a session pushes `t/*` branches and opens PRs without asking but never merges. A quick fix outside a ticket goes on a branch too (`git switch -c t/<n>-<slug>`).

## Tests first

- On a ticket, the test-author writes the tests, `harness-red check` proves them red, and only then the coder changes source; a hook enforces the roles on ticket branches, and git hooks refuse commits and pushes on main. Never delete, skip or loosen a test to get green: a wrong test goes back to the test-author with the reason.
- The ticket's own tests run at every stage (`harness-red run`); CI runs the whole suite on the pull request. Do not run the full verify command by hand on a ticket branch.

## Model routing

- Opus 5.5 at high effort is the default. For each agent in the loop, `harness-board tier N --json` (test-author, breaker, coder) and `harness-board tier N --stage review --json` (reviewer) print the `tier` to pass as `model` and the `effort` to pass when it is not null; `harness-work attempt` names the tier and effort of a retry or escalation. Fable runs only when I launch it or a project setting names it.
- `/harness:work` with no number dispatches one background session per ready ticket. Say what you launched and why.

## Budgets

- Dispatch runs on the subscription by default; `/harness:work --api` spends the API key and is refused while that budget is exhausted. `harness-work budget status` shows what is exhausted; report it, do not fight it.

## Across projects

- Each project gets its own session; `/harness:standup` is the cross-project view and `claude agents` the console for background sessions.
- Remind me to run `/harness:handoff` before clearing context mid-task, and `/harness:worklog` at the end of a working session on a billable project.

## Compact Instructions

When compacting, keep the ticket number and branch, the worktree path, the list of files changed this session, the latest test or red-run result, and any decision I made in the conversation.
