# Your Claude Code harness: first-run guide

Written 2026-10-13 for harness v2, Claude Code 2.1.296 and gh 2.99.0. The design and every contract live in [design.md](design.md).

## What you have now

The harness turns each project's GitHub issues into the context for every session. You describe work once, it becomes tickets, agents carry tickets to pull requests, and you merge. Each piece has one job.

| Piece | What it is | What it does for you |
| --- | --- | --- |
| Personal instructions, `~/.claude/CLAUDE.md` | A short file Claude reads at the start of every session | Carries your preferences and the loop's rules: read the ticket first, never work on main, tests first, route by tier |
| The board | GitHub Issues in each project's private repo: epics, task sub-issues, labels for status, size and risk | The single record of what is asked, what is in progress and what needs you. Agents read it before working and write status back |
| The `harness` plugin | Seven agents, eight skills and seven hooks, installed once for your user | Agents do the work, skills are the procedures you start with `/harness:<name>`, hooks enforce the rules whatever the model thinks |
| The `harness-*` tools | Nine command-line tools, on the Bash PATH in every session | The deterministic parts: board edits, worktrees, the red proof, PRs, budgets. Outside a session they live in `~/portfolio_projects/Harness/plugins/harness/bin/` |
| The digest | A launchd job that runs `harness-digest` daily at 08:30 | One notification with PRs to review, tickets that need input and stale work, across every project |
| The Harness repo, `~/portfolio_projects/Harness` | The source of all of the above, under git | Edit a skill, an agent or a hook there, run `/reload-plugins`, and every project picks it up |

Each project keeps its own `CLAUDE.md`, hooks, rules and skills. The harness adds the board, the guards and the loop beside them, in every project including jotform-glow-sync.

## Day one

In a terminal, in this order.

1. **Install.** `sh ~/portfolio_projects/Harness/scripts/install.sh`. It installs the plugin, your `CLAUDE.md`, the model settings and the daily digest, and backs up what it replaces. `claude --version` should print 2.1.296 or newer.
2. **Store the API key.** `security add-generic-password -a "$USER" -s harness-anthropic -w` prompts for the key and keeps it in the keychain. Only `/harness:work --api` (or `--venue headless`) runs read it, from there, so it never appears on a command line or in the environment.
3. **Check GitHub.** `gh auth status` should show your account. The board needs it.
4. **Register your projects.** `harness-projects discover` lists repos with Claude Code sessions. Add each with `harness-projects add <path>`. Mark client work billable: `harness-projects add ~/jotform-glow-sync --billable --client "Global G.L.O.W."`.
5. **Put the first repo on the board.** `cd` into it, start `claude`, and trust the folder when asked. Run `/harness:new-project`. It interviews you, shows the scaffold plan, and asks before it creates the private GitHub repository and pushes. A repo that already has commits gets the scaffold through a chore ticket and a PR: your first PR to land.
6. **Plan once.** `/harness:plan <a small, real request>`. Answer the interview, read the proposed table of tasks, approve it. The epic and tasks appear on GitHub.
7. **Work one ticket here.** `/harness:work <n>` runs the whole loop in this session, so you see each stage. Later, `/harness:work` with no number dispatches every ready ticket in the background.
8. **Answer a ticket.** When a notification says `<repo> #N needs input`, open the issue, reply in a comment, then run `/harness:work N`.
9. **Land.** When a notification says a PR is ready, read it on GitHub, then `/harness:land` to list what is waiting and `/harness:land <P>` to merge one.

## A working day

| Step | Command | What you get |
| --- | --- | --- |
| Digest | arrives at 08:30; `harness-digest` runs it now | PRs to review, tickets that need input, stale work, failed runs, budgets |
| Standup | `/harness:standup` | Per project: git state, board counts, PRs awaiting you, needs-input tickets, stale worktrees, next steps, hours. Then a proposed order for the day |
| Plan | `/harness:plan <request>` | New work on the board as an epic and tasks |
| Work | `/harness:work` or `/harness:work <n>` | Ready tickets dispatched, or one run here |
| Land | `/harness:land`, then `/harness:land <P>` | Reviewed PRs merged, worktrees and branches cleaned up, tickets closed |
| Handoff | `/harness:handoff`, then `/clear` | `docs/agent/handoff.md`, which the next session's brief reads |
| Worklog | `/harness:worklog` | The day's hours and a client note on billable projects |

One session per project. `claude agents` in a terminal lists every session, including the background ones dispatch starts.

## How a ticket flows

| Stage | Who | What happens |
| --- | --- | --- |
| Start | `harness-work start N` | Branch `t/N-<slug>`, a worktree under `.claude/worktrees/`, label `status:in-progress`, a comment on the ticket |
| Tests | `test-author` at the ticket's tier | Writes the tests the ticket names, broad behavior first, and runs them |
| Red proof | `harness-red check` | Runs the new tests against unchanged source. They must fail; the record is written |
| Break | `test-breaker` | In a scratch copy, tries to pass the tests with a cheat. If it can, the tests go back to the author, at most twice |
| Code | `coder` at the tier | Changes source only, until the new tests and `CLAUDE_VERIFY_CMD` pass, and commits |
| Review | `reviewer`, Opus (xhigh for risk:high), unless the project sets `HARNESS_REVIEW_TIER` | Checks the branch against the definition of done. High and medium findings go back to the coder, at most twice |
| PR | `harness-pr open`, `harness-pr review` | Pushes the branch, opens a draft PR, posts the findings as a review |
| CI | `harness-pr checks <P> --wait` | Waits for checks. Failures go back to the coder, at most twice |
| Ultrareview | `harness-pr ultra <P> --post` | Only on `risk:high`; posts a cloud review to the PR. With `HARNESS_ULTRAREVIEW=off` in the project's settings it prints one line saying ultrareview is off and runs nothing |
| Ready | `harness-pr ready <P>` | Marks the PR ready, sets `status:in-review`, notifies you |

Every stage reports pass or fail to `harness-work attempt`. A first failure retries at the same tier, a second re-runs one tier up, a third posts a needs-input question on the ticket and stops.

The hooks refuse three kinds of action, so the rules hold even when a prompt is ignored:

| Hook | Refuses | Why |
| --- | --- | --- |
| Branch guard | Edits, commits, merges, resets and pushes to main in a repo on main or master. Force pushes on any branch | GitHub Free does not protect branches on private repos, so this is the only block. `docs/agent/` and gitignored files stay editable on main |
| Tests first | On a `t/N-` branch: any source edit before a current red run; the test-author editing source; the coder or breaker editing tests | The tests are the contract, written and proven before the code. A `no-tests` ticket is exempt; a `refactor` ticket accepts a green record |
| Read-only guard | Writes from the planner, reviewer and researcher | They advise; they never change files |

On GitHub you see the work in two places. The ticket gets comments as it moves: started on its branch, the PR link, ready for review, landed with the merge commit. The PR body carries the red proof, and the reviewer's findings arrive as a review titled `Harness review (<model>): <n> findings`, inline on the changed lines, with any finding outside the diff listed in the review body.

## Your two jobs

**Answer needs-input tickets.** When the agents cannot answer a question from the ticket, the epic or the code, they stop and ask. The ticket gets `status:needs-input` and a comment headed **Needs your input** with the question and the evidence, and you get a notification. Reply in a comment on the issue. `/harness:work N` reads your newest comment as the answer and carries on.

**Review pull requests.** Agents open PRs under your GitHub account, so GitHub will not let you approve them; landing is the approval. Read the PR, leave review comments where something should change, and run `/harness:work N` again: it gives the coder your unresolved threads, pushes, waits for checks and answers each thread with what changed. When it is right, `/harness:land <P>`.

The PR body is laid out the same way every time:

| Section | What it holds |
| --- | --- |
| `Closes #N` | The ticket, so merging closes it |
| Summary | What changed |
| Definition of done | The ticket's checklist |
| Tests | The tests added, then the red run: its kind, exit code and the tail of the failing output |
| Verify | The tail of `CLAUDE_VERIFY_CMD` on the finished branch |
| Review | Finding counts by severity and the high findings |
| Risks | Filled for `risk:high` tickets |

## Projects board

Optional. A GitHub Projects board shows your tickets as a kanban with each epic as a swim lane. It is a mirror: the `status:*` and `priority:high` labels stay authoritative, `harness-board` writes the board, and no ticket state is ever taken from it; only `project check` and `project sync` read it, to compare it with the labels. Dragging a card to another column changes nothing on the ticket. For an open issue, the next status change on it, or the next `harness-board project sync`, puts the card back from the labels. A closed issue's card dragged out of Done stays there until auto-archive removes it, and a drag between epic lanes may change the issue's parent. To change a ticket's status, use `harness-board status` or the loop, not the board.

One-time setup:

1. **Scope.** `gh auth refresh -h github.com -s project`. Without the `project` scope every board call fails with a warning; the tools keep working.
2. **Project.** In the GitHub web UI, create a new private user project (separate from the existing "Claude Kanban") from a blank Board layout, not the Kanban template: the template already has a Priority field with P0 to P2 and other Status options. Note its owner and number from the URL, `github.com/users/<owner>/projects/<number>`.
3. **Status options.** Rename or replace the Status field's options so they are exactly Ready, In progress, In review, Blocked, Needs input, Done. The tools match options by name, so a renamed option is a failed write.
4. **Priority field.** Add a single-select field named Priority with the options High and Normal.
5. **Views.** A board view grouped by Parent issue (enable that field first), so the epics are the swim lanes; a Priority view; a Blocked view filtered by `is:blocked` (if the filter is available; only a 2025-08-21 GitHub changelog entry mentions it). Turn on auto-archive for Done items so the board stays small: `project sync` reads at most 1000 items and adds nothing to a board larger than that.
6. **Point the harness at it.** In `~/.claude/settings.json`, under `env`, set `"HARNESS_PROJECT": "<owner>/<number>"`.
7. **Check.** `harness-board project check` names every missing Status or Priority option and exits 1, or prints one line and exits 0.
8. **Back-fill.** `harness-board project sync --all-projects --dry-run` prints each change it would make, one line per change (an add and its Status write are two lines), then a summary; run it again without `--dry-run` to add the open issues of every registered project and set their Status and Priority from the labels. After the back-fill most cards have no Priority: an unset Priority counts as Normal, and only `priority:high` tickets get High, so the Priority view's "No Priority" group is expected. Run `harness-board project sync` (no `--all-projects`) to do one repository.

From then on `epic create`, `task create` and every status change (including `harness-work start`, `needs-input` and `harness-pr ready`) update the board. A closed issue becomes Done through the project's built-in workflow. A failed board call is one `harness-board: warning:` line, shown by `harness-work` and `harness-pr` too, and never stops the command; run `project sync` to repair the drift. `project check` and `project sync` themselves report failures and exit 1.

Turn it off by removing `HARNESS_PROJECT` from `~/.claude/settings.json`, or for one run with `HARNESS_PROJECT=""` in the environment; with it unset or empty no `gh project` call is made.

## Budgets and quota

| Work | Bills to | When it runs out |
| --- | --- | --- |
| Your interactive sessions, including `/harness:work N` | The Max subscription | Wait for the window; `/usage` shows the five-hour and seven-day limits |
| `/harness:work` dispatch (the default) | The Max subscription: one background session per ticket | Wait for the window, as for interactive sessions |
| `/harness:work --api` (or `--venue headless`) dispatch | The API key in the keychain | A billing or auth failure marks the `api` budget exhausted for 24 hours. Until then `--api` and `--venue headless` are refused; dispatch without them still runs on the subscription |
| Ultrareview on `risk:high` PRs | claude.ai usage credits, about 5 to 25 dollars a run | A credits failure marks the `cloud` budget exhausted for 24 hours |

`harness-work budget status` shows both budgets. After a top-up, `harness-work budget reset api` clears the mark. `harness-work budget exhaust api --reason <text>` marks it exhausted by hand, which stops `--api` runs. The session brief and the standup name an exhausted budget.

A project that sets `HARNESS_ULTRAREVIEW=off` gets no ultrareview: `harness-pr ultra` prints one line and runs nothing, so its pull requests spend no usage credits.

## When something looks wrong

| Symptom | Check |
| --- | --- |
| A guard refused an edit: `On main in <repo>` | You are in the main checkout on main. `harness-work start N` for a ticket, or `git switch -c t/<n>-<slug>` for untracked work. `HARNESS_ALLOW_MAIN=1` under `env` in the project's `.claude/settings.local.json` turns the guard off there |
| Tests first refused: `No red run recorded` | Write the tests, then `harness-red check`. `harness-red status` shows the record and whether it is current. `Tests changed since the red run` means run `harness-red check` again. A docs or config ticket needs the `no-tests` label |
| `harness-red check` says the tests pass | They do not test the change. Back to the test-author; only a `refactor` ticket proceeds on green |
| `no checks registered` from `harness-pr checks` | The repo has no CI on pull requests. The scaffold's `.github/workflows/ci.yml` is missing, or `--no-ci` skipped it and the project's own CI does not run on PRs |
| A headless run died | `harness-work runs --sweep` shows each run's exit and outcome: `ok`, `billing`, `auth` or `error`. The log is in `~/.claude/harness/runs/`. The next `/harness:work` re-dispatches a ticket still in progress without a PR |
| Budget exhausted | `harness-work budget status`. Top up, then `harness-work budget reset api`. Until then `--api` and `--venue headless` are refused; dispatch runs on the subscription, its default, either way |
| Old worktrees pile up | `harness-work worktrees` lists them and flags stale ones; `harness-work worktree remove <branch>` removes one |
| Standup says the fleet could not be read | The `claude` binary was not found. The tool looks on PATH, then in `~/.local/bin` |
| `/hooks` lists nothing from the harness | Trust the folder, then `/reload-plugins`. `claude plugin validate ~/portfolio_projects/Harness` checks the plugin. `claude --debug` logs every hook run with its exit code to `~/.claude/debug/<session-id>.txt` |
| An edit was flagged for a secret | Move the value to the environment, the keychain or a gitignored file. A fixtures file with fake tokens can carry the line `harness: allow-secrets` |
| No notifications | `brew list terminal-notifier`; allow it under System Settings, Notifications; check that a Focus mode is not hiding banners |
| Everything misbehaves | `"disableAllHooks": true` in the project's `.claude/settings.local.json` pauses its hooks. `claude doctor` checks the install |

## Habits

- Put work on the board before it starts. Anything bigger than a quick fix goes through `/harness:plan`.
- Run unfamiliar or risky tickets with `/harness:work <n>` in a session you watch. Dispatch the rest.
- Answer needs-input tickets and land PRs the same day. Each waiting ticket holds a worktree.
- One session per project. `/clear` between unrelated tasks, with `/harness:handoff` first when you stop mid-task.
- Keep each project's `CLAUDE.md` under about 60 lines. `/harness:learn` turns a session's corrections into chore tickets; land the ones you agree with.
- Never loosen a test to get a PR through. A wrong test goes back to the test-author with the reason.
- Keep secrets out of commands. The API key lives in the keychain for that reason.
- End every billable session with `/harness:worklog`, and run it at least every two weeks: Claude Code deletes a transcript about 30 days after its last activity, and the hours come from transcripts.

## Glossary

| Term | Meaning here |
| --- | --- |
| Ticket | A GitHub issue on the board: request, outline, definition of done, tests that must pass, context |
| Epic | An issue labeled `epic` whose tasks are its sub-issues. It closes when the last task lands |
| Board | A project's GitHub issues and their labels. `harness-board board` prints it |
| Tier | The model a ticket's agents run on: Sonnet, Opus or Fable. A project's `HARNESS_CODE_TIER` decides it for the test-author, breaker and coder, and its `HARNESS_REVIEW_TIER` for the reviewer; otherwise the size and risk labels decide (Sonnet or Opus, never Fable), and the review runs on Opus. Fable runs when you launch it yourself or a setting names it. `harness-board tier N --json` names which decided |
| Red run | `harness-red check` proving the new tests fail before any source changes. Its record unlocks source edits |
| Breaker | The `test-breaker` agent, which tries to cheat the tests to show they are too narrow |
| Worktree | A second checkout of the repo in another folder. Each ticket gets one under `.claude/worktrees/` |
| Land | Squash-merge a PR, remove its worktree and branch, close the ticket: `/harness:land <P>` |
| Headless run | A `claude -p` session that dispatch starts per ticket only with `--api` or `--venue headless`, billed to the API key, logged under `~/.claude/harness/runs/` |
| Budget | The API key or the usage credits. An exhausted one is skipped for 24 hours |
| Digest | The daily summary across projects, written to `~/.claude/harness/digest/` |
| Session | One conversation with Claude Code in one folder, with its own context window |
| Context window | What the model can see right now. It fills up, which is why compaction and handoffs exist |
| Compaction | Claude Code summarizing the conversation to free space; manual with `/compact` |
| CLAUDE.md | Instructions loaded at session start. `~/.claude/CLAUDE.md` applies everywhere; a project's own file adds to it |
| Rule | A markdown file under `.claude/rules/` that loads only when matching files are touched |
| Hook | A script Claude Code runs at an event, such as before a tool call. Deterministic, unlike instructions |
| Skill | A procedure in a SKILL.md, started with `/name`. The harness ones are `/harness:<name>` |
| Subagent | A separate Claude with its own context, model and tools, given one task |
| Plugin | A package of agents, skills and hooks installed as one unit. `ryan` is your marketplace |
| Effort | How much reasoning a model spends per turn. Set per model under `modelSettings`; a project's `HARNESS_CODE_EFFORT` sets it for the test-author, breaker and coder and for the sessions dispatch launches; without it they take the route's effort: `xhigh` for a size:L or risk:high ticket, a risk:high review and a stage after `escalate opus xhigh`, else none. A Fable review, set by `HARNESS_REVIEW_TIER`, gets `xhigh` too |
| Fleet, console | `claude agents`: every session on the machine |
| Handoff | `docs/agent/handoff.md`, the note a session writes so the next one can continue |
