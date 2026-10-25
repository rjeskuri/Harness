# How I work

- I am a solo developer across several repositories, experienced in Python and SQL. Skip the basics; when you recommend something, give the reasoning and the tradeoffs.
- Keep replies short. For code changes I will review inline, show before/after snippets of the changed lines rather than whole files.
- I am on macOS with zsh, Homebrew and VS Code. macOS ships BSD `sed`, `grep` and `date`: no `grep -P`, `sed -i` needs `''`, and `date -d` does not exist. There is no bare `python`, only `python3` or the project's own interpreter.
- When you finish a task, show the evidence (the command you ran and its result) and say plainly what you could not verify.
- Ask before adding dependencies or doing anything else hard to undo, except what the loop below allows. Never put a secret on a command line; read it from the environment, a file or the keychain.

## The board is the context

- Every piece of work is a GitHub issue on the project's board: an epic with task sub-issues, each with a request, an outline, a definition of done and the tests that must pass. Read the ticket (`harness-board show N --markdown`) and its epic before working. Status changes go through `harness-board`; `harness-board board` shows the whole board.
- A question the ticket, the epic and the code cannot answer becomes `harness-board needs-input N --question ...` when it is about product behavior, is hard to undo, or touches security, credentials or personal data; never a guess. A reversible implementation choice they leave open is decided and recorded as a `Ruling:` line under `### Rulings` in the PR's Notes. I answer on the ticket; the next `/harness:work N` reads the answer.
- New work: `/harness:plan <request>`. Tickets to pull requests: `/harness:work [N]`. Landing: I land with `/harness:land [PR#]` or by approving the land prompt; `harness-pr autoland` is the epic #132 plan. A new or existing repository joins the board with `/harness:new-project`.

## Never on main

- Work happens on a ticket branch `t/<issue>-<slug>` in its own worktree, created by `harness-work start N`. A hook denies edits, commits and pushes on main; do not look for a way around it.
- Agents may push `t/*` branches and open or update pull requests without asking; a session does not land them. I land each with `/harness:land` or by approving the land prompt; `harness-pr autoland` is the epic #132 plan. Nobody pushes main, force-pushes or deletes a branch by hand.

## Tests first

- On a ticket, tests are written before code by `harness:test-author`, proven red by `harness-red check`, challenged by `harness:test-breaker`, and only then implemented by `harness:coder`. A hook enforces the roles: authors edit tests only, coders never edit tests. Broad behavior tests through the public interface come first; specific cases after.
- Never delete, skip or loosen a test to get green. A wrong test goes back to the test-author with the reason.

## Model routing

- Default: Opus 5.5 at high effort, for the main session and every agent. `harness-board tier N` maps a ticket's labels to a tier: size:S and risk:low on a non-bug gives `sonnet`; size:L or risk:high gives `fable`; otherwise `opus`. Pass that as `model` on the Agent call. The reviewer's model comes from `harness-board tier N --stage review`: Opus 5.5, and Fable only when the ticket is risk:high. When a stage escalates to Fable, or the review runs on a risk:high ticket, pass `model: fable` and `effort: xhigh` on the Agent call.
- Escalation is by failure, recorded with `harness-work attempt`: a stage's first failure retries at the same tier, the second re-runs one tier up, the third stops with needs-input.
- Standard reviews run on Opus 5.5. `harness:reviewer` runs on Fable when the ticket is risk:high or when the review stage escalates by failure: record the stage with `harness-work attempt N review pass|fail --tier <the output of harness-board tier N --stage review>`, and its second failure says `escalate fable`. `harness:planner` is Opus at xhigh. `harness:researcher` answers from primary sources. `harness:quick` (Sonnet) does trivial mechanical tasks outside tickets.
- Parallel work: `/harness:work` with no number dispatches a session per ready ticket. A Workflow is yours to launch when it pays off, Opus for coding and judgment stages, Fable per the tier and escalation rules. Say what you launched and why.

## Budgets

- Interactive sessions and dispatched tickets run on the subscription by default: `harness-work dispatch` launches background sessions. The API key is spent only on request: `/harness:work --api` (or `harness-work dispatch --venue headless`) launches headless runs billed to it. While the API budget is exhausted, `--api` and `--venue headless` are refused. `risk:high` pull requests get `claude ultrareview` on usage credits. `harness-work budget status` shows what is exhausted; report it, do not fight it.

## Across projects

- Each project gets its own session; `/harness:standup` is the cross-project view, the digest arrives daily, and `claude agents` in a terminal is the console for background sessions.
- Write `docs/agent/handoff.md` with `/harness:handoff` before clearing context mid-task; the session brief reads it on the next start.
- On a billable project, run `/harness:worklog` at the end of a working session.

## Compact Instructions

When compacting, keep the ticket number and branch, the worktree path, the list of files changed this session, the latest test, red-run or verify result, and any decision I made in the conversation.
