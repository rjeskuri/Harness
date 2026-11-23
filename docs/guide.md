# Your Claude Code harness: first-run guide

Written 2026-10-13 for harness v2, Claude Code 2.1.296 and gh 2.99.0. The design and every contract live in [design.md](design.md).

## What you have now

The harness turns each project's GitHub issues into the context for every session. You describe work once, it becomes tickets, agents carry tickets to landed pull requests, and you decide what runs next. Each piece has one job.

| Piece | What it is | What it does for you |
| --- | --- | --- |
| Personal instructions, `~/.claude/CLAUDE.md` | A short file Claude reads at the start of every session | Carries your preferences and the loop's rules: read the ticket first, never work on main, tests first, route by tier |
| The board | GitHub Issues in each project's private repo: epics, task sub-issues, labels for status, size and risk | The single record of what is asked, what is in progress and what needs you. Agents read it before working and write status back |
| The `harness` plugin | Seven agents, ten skills and seven hooks, installed once for your user | Agents do the work, skills are the procedures you start with `/harness:<name>`, hooks enforce the rules whatever the model thinks |
| The `harness-*` tools | Nine command-line tools, on the Bash PATH in every session | The deterministic parts: board edits, worktrees, the red proof, PRs, budgets. Outside a session they live in `~/Projects/Harness/plugins/harness/bin/` |
| The digest | A launchd job that runs `harness-digest` daily at 08:30 | One notification with PRs to review, tickets that need input and stale work, across every project |
| Hourly dispatch (opt-in) | A launchd job you install yourself, `harness-work dispatch --install-launchd` | Every hour, the ready tickets of the projects you opted in start in the background, up to 8 live runs across all projects (Hourly dispatch, below) |
| The Harness repo, `~/Projects/Harness` | The source of all of the above, under git | Edit a skill, an agent or a hook there, run `/reload-plugins`, and every project picks it up |

Each project keeps its own `CLAUDE.md`, hooks, rules and skills. The harness adds the board, the guards and the loop beside them, in every project including jotform-glow-sync.

## Day one

In a terminal, in this order.

1. **Install.** `sh ~/Projects/Harness/scripts/install.sh`. It installs the plugin, your `CLAUDE.md`, the model settings and the daily digest, and backs up what it replaces. `claude --version` should print 2.1.296 or newer.
2. **Store the API key.** `security add-generic-password -a "$USER" -s harness-anthropic -w` prompts for the key and keeps it in the keychain. Only `/harness:work --api` (or `--venue headless`) runs read it, from there, so it never appears on a command line or in the environment.
3. **Check GitHub.** `gh auth status` should show your account. The board needs it.
4. **Register your projects.** `harness-projects discover` lists repos with Claude Code sessions. Add each with `harness-projects add <path>`. Mark client work billable: `harness-projects add ~/Projects/jotform-glow-sync --billable --client "Global G.L.O.W."`.
5. **Put the first repo on the board.** `cd` into it, start `claude`, and trust the folder when asked. Run `/harness:new-project`. It interviews you, shows the scaffold plan, and asks before it creates the private GitHub repository and pushes. A repo that already has commits gets the scaffold through a chore ticket and a PR: the first PR to land.
6. **Plan once.** `/harness:plan <a small, real request>`. Answer the interview, read the proposed table of tasks, approve it. The epic and tasks appear on GitHub.
7. **Work one ticket here.** `/harness:work <n>` runs the whole loop in this session, so you see each stage. Later, `/harness:work` with no number dispatches every ready ticket in the background.
8. **Answer a ticket.** When a notification says `<repo> #N needs input`, open the issue, reply in a comment, then run `/harness:work N`. When one says `<repo> #N parked`, read the **Parked** comment on the issue and choose: promote its follow-up, unpark the ticket, or land its PR yourself (Your jobs, below).
9. **Land.** Every ticket's run lands its own PR, at any risk, with `harness-pr land --catch-up` once CI is green, the branch is current with main and no high or medium finding is open. Nothing waits for you: read a landed PR on GitHub when you like. `/harness:land` lists the PRs of parked tickets, and `/harness:land <P>` lands one you choose.

## A working day

| Step | Command | What you get |
| --- | --- | --- |
| Digest | arrives at 08:30; `harness-digest` runs it now | PRs to review, tickets that need input, stale work, failed runs, budgets |
| Standup | `/harness:standup` | Per project: git state, board counts, PRs awaiting you, needs-input tickets, stale worktrees, next steps, hours. Then a proposed order for the day |
| Plan | `/harness:plan <request>` | New work on the board as an epic and tasks |
| Work | `/harness:work` or `/harness:work <n>` | Ready tickets dispatched, or one run here |
| Land | `/harness:land`, then `/harness:land <P>` | Only for a parked ticket's PR you choose to land: merged with no question, worktree and branch cleaned up, ticket closed. Every other PR lands at the end of its run, at any risk |
| Handoff | `/harness:handoff`, then `/clear` | `docs/agent/handoff.md`, which the next session's brief reads |
| Worklog | `/harness:worklog` | The day's hours and a client note on billable projects |
| Invoice | `/harness:invoice [--month yyyy-mm]` | A month's hours by ticket and epic, as CSV and markdown, for the invoice |
| Cloud | `/harness:cloud <n>` | One ticket started in a Claude cloud session; review, the PR, CI and landing stay on this Mac |

One session per project. `claude agents` in a terminal lists every session, including the background ones dispatch starts.

## How a ticket flows

| Stage | Who | What happens |
| --- | --- | --- |
| Start | `harness-work start N` | Branch `t/N-<slug>`, a worktree under `.claude/worktrees/`, label `status:in-progress`, a comment on the ticket |
| Tests | `test-author` at the ticket's tier | Writes the tests the ticket names, broad behavior first, and runs them |
| Red proof | `harness-red check` | Runs the new tests against unchanged source. They must fail; the record is written |
| Break | `test-breaker` | In a scratch copy, tries once to pass the tests with a cheat, only on a risk:high ticket that changes a hook's decision path or a tool that destroys or rewrites data. Its gaps go to the coder as requirements and to the reviewer as check items |
| Code | `coder` at the tier | Changes source only, until the new tests and the ticket's own tests pass, and commits |
| Review | `reviewer`, Opus (xhigh for risk:high), unless the project sets `HARNESS_REVIEW_TIER` | Checks the branch against the definition of done. High and medium findings go back to the coder, at most twice |
| PR | `harness-pr open`, `harness-pr review` | Pushes the branch, opens a draft PR, posts the findings as a review |
| CI | `harness-pr checks <P> --wait` | Waits for checks. Failures go back to the coder, at most twice |
| Ultrareview | `harness-pr ultra <P> --post` | Only on `risk:high`; posts a cloud review to the PR. With `HARNESS_ULTRAREVIEW=off` in the project's settings it prints one line saying ultrareview is off and runs nothing |
| Ready | `reviewer`, `harness-pr ready <P>` | When commits other than clean merges of main came after the latest harness review (CI, ultrareview or revise fixes, or a merge of main that conflicted), the reviewer reviews the head again first, since `harness-pr land` refuses a head no review covers. A merge counts as clean, whoever made it, as `harness-pr land` judges it: `git merge-tree` of its two parents merges without a conflict into the merge's own tree. Then marks the PR ready, sets `status:in-review`, notifies you, and goes straight on to Land |
| Land | `harness-pr land <P> --catch-up` | At any risk, with no question to you: merges main into the branch when it is behind and that merge is clean, pushes and waits for CI, then merges the PR, removes the worktree and branch, closes the ticket and runs the canary. A conflict with main goes to the coder (after `harness-red reprove` on a branch with no red record, such as one from the cloud), then CI and the head review again. A land it still cannot finish parks the ticket |

Every stage reports pass or fail to `harness-work attempt`. A first failure retries at the same tier and effort. A second re-runs one step up: Sonnet goes to Opus, Opus goes to Opus at effort xhigh, and a second failure already at Opus xhigh (or max), or on an explicitly named Fable run, parks the ticket instead. A third parks it: `status:blocked`, a **Parked** comment with the stage, the reason and the evidence, a proposed follow-up, and a notification; then the loop launches the next ready ticket. A land that hits its caps, or a refusal the loop cannot handle, parks the same way. No escalation goes to Fable.

The hooks refuse three kinds of action, so the rules hold even when a prompt is ignored:

| Hook | Refuses | Why |
| --- | --- | --- |
| Branch guard | Edits, commits, merges, resets and pushes to main in a repo on main or master. Force pushes on any branch | GitHub Free does not protect branches on private repos. The git hooks the scaffold installs (`.githooks/pre-commit`, `pre-push`) are the block git enforces; this hook refuses earlier, in the session, with the way forward. `docs/agent/` and gitignored files stay editable on main |
| Tests first | On a `t/N-` branch: any source edit before a current red run; the test-author editing source; the coder or breaker editing tests | The tests are the contract, written and proven before the code. A `no-tests` ticket is exempt; a `refactor` ticket accepts a green record |
| Read-only guard | Writes from the planner, reviewer and researcher | They advise; they never change files |

On GitHub you see the work in two places. The ticket gets comments as it moves: started on its branch, the PR link, ready for review, landed with the merge commit. The PR body carries the red proof, and the reviewer's findings arrive as a review titled `Harness review (<model>): <n> findings`, inline on the changed lines, with any finding outside the diff listed in the review body.

## Your jobs

**Answer needs-input tickets.** The agents stop and ask only when the ticket, the epic and the code cannot answer a question that is about direction or product behavior, is hard to undo, or touches security, credentials or personal data. A ticket that keeps failing is parked, never asked about. A reversible implementation choice the ticket leaves open is decided and recorded as a `Ruling:` line under `### Rulings` in the PR's Notes; overrule one in a review comment on the PR. For a question, the ticket gets `status:needs-input` and a comment headed **Needs your input** with the question and the evidence, and you get a notification. Reply in a comment on the issue. `/harness:work N` reads your newest comment as the answer and carries on.

**Decide what runs next.** Plans, review findings outside a ticket's files, size-cap splits, park follow-ups, reverted PRs' bugs and `/harness:learn` chores all arrive as `status:proposed` tickets, under their epic and never blocking another ticket. Nothing runs them until you do: `harness-board promote <N>` makes a proposed task ready, or an epic and its proposed tasks.

**Unpark a ticket.** A parked ticket is `status:blocked` and keeps its branch, worktree and any PR. Its **Parked** comment names the stage, what still fails, the evidence and the follow-up. Promote the follow-up and let it land first, or fix the cause yourself, then unpark it. Then `harness-board status N ready`, and the next `/harness:work` dispatch, or `/harness:work N`, resumes from the branch and its PR: committed source work, the open PR's harness review findings, CI and the head review, then land, never the tests step again; a ticket with no branch work yet starts at the beginning. A stage's failure count is not reset, so one more failure of the stage that parked it parks the ticket again at once. When you left review comments on the PR, move it with `harness-board status N in-review` instead: `/harness:work N` then takes the PR from where it stands, as under **Review pull requests** below. When its PR is already right, `/harness:land <P>` lands it as it is.

**Review pull requests.** Agents open PRs under your GitHub account, so GitHub will not let you approve them, and nothing waits for your approval: every PR lands at the end of its own run, at any risk, once CI is green, the branch is current with main and the harness review's high and medium findings are fixed. Read landed PRs when you like; `harness-pr revert <P>` undoes one, parks its ticket and files a proposed bug. On a PR that is still open, a parked ticket's, leave review comments where something should change, unpark the ticket with `harness-board status N in-review` and run `/harness:work N`: it gives the coder your unresolved threads, pushes, waits for checks and answers each thread with what changed, has the reviewer review the new head (`harness-pr land` refuses a head no harness review covers), then lands the PR without waiting for you again (your comments were your review).

The PR body is laid out the same way every time:

| Section | What it holds |
| --- | --- |
| `Closes #N` | The ticket, so merging closes it |
| Summary | What changed |
| Definition of done | The ticket's checklist |
| Tests | The tests added, then the red run: its kind, exit code and the tail of the failing output |
| Verify | Empty unless a log was attached with `harness-pr open --verify-log`; CI runs the whole suite on the pull request |
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
| `/harness:work` dispatch (the default), and the hourly dispatch | The Max subscription: one background session per ticket | A background session that hits the usage limit marks the `subscription` budget exhausted until the reset its refusal names (else 5 hours after it), found by the next sweep. Until then background dispatch, the hourly one included, starts nothing and says until when; `--api` still runs |
| `/harness:work --api` (or `--venue headless`) dispatch | The API key in the keychain | A billing or auth failure marks the `api` budget exhausted for 24 hours. Until then `--api` and `--venue headless` are refused; dispatch without them still runs on the subscription |
| Ultrareview on `risk:high` PRs | claude.ai usage credits, about 5 to 25 dollars a run | A credits failure marks the `cloud` budget exhausted for 24 hours |

`harness-work budget status` shows the three budgets. After a top-up, `harness-work budget reset api` clears the mark; `harness-work budget reset subscription` clears the usage-limit hold early (the same refusal never marks it again; a new one does). `harness-work budget exhaust api --reason <text>` marks it exhausted by hand, which stops `--api` runs. The session brief and the standup name an exhausted budget.

## Hourly dispatch

Off until you opt a project in and install the job. Once both are done, minute 0 of every hour runs `harness-work dispatch --all-projects --max 8`: each registered project that opted in and has no open issue labelled `incident` gets the same dispatch as `/harness:work`, in the background on the subscription, topped up to 8 live runs across all projects (runs you started count too). A project with an open incident, or not opted in, is skipped with one line in the log. An opted-in project's ready ops tickets are launched too, the way any background dispatch launches them (in its main checkout, no worktree): opting in is per project, and an ops run still waits for your OK before it deletes records or updates more than 25.

1. **Opt a project in.** In that project's `.claude/settings.json` (not `settings.local.json`), under `env`, set `"HARNESS_AUTODISPATCH": "1"`, then commit and push it to main. Only the file committed at `origin/main` counts, so a setting in your shell or a working tree opts nothing in. Opt out by committing `"0"` or removing the line.
2. **Install the job, once.** `harness-work dispatch --install-launchd` writes `~/Library/LaunchAgents/com.ryaneskuri.harness-dispatch.plist` and loads it; it dispatches nothing itself. The install script does not install it. `harness-work dispatch --all-projects --dry-run` shows what the next hour would start.
3. **Read the log.** `~/.claude/harness/dispatch/launchd.log` holds each tick's output: the projects skipped and why, the sessions launched, and a project that failed (one line; the others still run).
4. **Uninstall.** `harness-work dispatch --uninstall-launchd` unloads the job and removes the file.

The Mac must be awake for a tick to run: lid open on power, or closed with an external display attached. Hours missed while it slept run once when it wakes (the job uses `StartCalendarInterval`), not once per missed hour. While the `subscription` budget is exhausted each tick starts nothing, until the reset.

A project that sets `HARNESS_ULTRAREVIEW=off` gets no ultrareview: `harness-pr ultra` prints one line and runs nothing, so its pull requests spend no usage credits.

## Changing the loop itself

A change to `plugins/harness/skills/work/SKILL.md` or `plugins/harness/agents/*.md` gets a loop smoke run against its branch before it lands. The run works on one public sandbox repository, `rjeskuri/harness-sandbox`, reused each time. It resets the sandbox (closing any `Loop smoke` issue left open, with its pull requests and worktrees), creates a `Loop smoke <stamp>` ticket asking `stamp()` to return that stamp, and runs `claude -p "/harness:work N"` headless in the sandbox. It then checks that the issue is closed, a pull request from `t/N-*` is merged and no worktree of it is left. It is a manual check, not a CI gate.

- On main: `python3 scripts/loop_smoke.py run --sandbox ~/portfolio_projects/harness-sandbox`
- Against a branch: add `--plugin <worktree>/plugins/harness`. That session loads the branch's plugin in place of the installed one, with the branch's `bin/` first on PATH.

A run takes about 20 minutes and uses some subscription quota. `claude -p` with no `apiKeyHelper` runs on your login (`apiKeySource` is `none`), so it does not use the API key. The harness's own headless dispatch adds `apiKeyHelper`; this run does not. `--timeout` (minutes, default 45) kills a session that runs too long, and the session's output is in `~/.claude/harness/smoke/<stamp>.log`. The run never pushes to the sandbox and never adds it to `projects.json`.

One-time setup, as done on 2026-11-23 after your OK:

1. Create the folder with the package files first: `pyproject.toml`, `uv.lock`, `src/harness_sandbox/{__init__,stamp}.py` and `tests/{test_smoke,test_stamp}.py`. A simple way is to scaffold a throwaway folder and copy its `pyproject.toml`, `uv.lock`, `src` and `tests`. They must exist before the scaffold runs: the scaffold keeps a layout it finds, and once it has committed, the branch guard refuses edits on main.
2. `harness-scaffold init <path> --name harness-sandbox --stack python`, with no `--github` and no `--register`.
3. `HARNESS_ALLOW_MAIN=1 gh repo create harness-sandbox --public --source <path> --push`. Without the variable, the pre-push hook refuses this first push to main.
4. `harness-board labels ensure --repo <path>`.
5. `git config user.email <your GitHub noreply address>` in the clone, so smoke commits do not carry a personal address.
6. Run `claude` in the folder once and accept the trust dialog. A `claude -p` session in a folder that was never trusted ignores the project's `permissions.allow` (`docs/research/plugin-dir-smoke.md`).

The sandbox stays out of `projects.json`.

## When something looks wrong

| Symptom | Check |
| --- | --- |
| A guard refused an edit: `On main in <repo>` | You are in the main checkout on main. `harness-work start N` for a ticket, or `git switch -c t/<n>-<slug>` for untracked work. `HARNESS_ALLOW_MAIN=1` under `env` in the project's `.claude/settings.local.json` turns the guard off there |
| Tests first refused: `No red run recorded` | Write the tests, then `harness-red check`. `harness-red status` shows the record and whether it is current. `Tests changed since the red run` means run `harness-red check` again, or `harness-red reprove` once the branch has source changes. A docs or config ticket needs the `no-tests` label |
| `harness-red check` says the tests pass | They do not test the change. Back to the test-author; only a `refactor` ticket proceeds on green |
| `no checks registered` from `harness-pr checks` | The repo has no CI on pull requests. The scaffold's `.github/workflows/ci.yml` is missing, or `--no-ci` skipped it and the project's own CI does not run on PRs |
| A headless run died | `harness-work runs --sweep` shows each run's exit and outcome: `ok`, `billing`, `auth` or `error`. The log is in `~/.claude/harness/runs/`. The next `/harness:work` re-dispatches a ticket still in progress without a PR |
| Budget exhausted | `harness-work budget status`. Top up, then `harness-work budget reset api`. Until then `--api` and `--venue headless` are refused; dispatch runs on the subscription, its default, either way. `subscription` exhausted: the usage limit; background dispatch waits for the reset time it names, or `harness-work budget reset subscription` |
| Old worktrees pile up | `harness-work worktrees` lists them and flags stale ones; `harness-work worktree remove <branch>` removes one |
| Standup says the fleet could not be read | The `claude` binary was not found. The tool looks on PATH, then in `~/.local/bin` |
| `/hooks` lists nothing from the harness | Trust the folder, then `/reload-plugins`. `claude plugin validate ~/Projects/Harness` checks the plugin. `claude --debug` logs every hook run with its exit code to `~/.claude/debug/<session-id>.txt` |
| An edit was flagged for a secret | Move the value to the environment, the keychain or a gitignored file. A fixtures file with fake tokens can carry the line `harness: allow-secrets` |
| No notifications | `brew list terminal-notifier`; allow it under System Settings, Notifications; check that a Focus mode is not hiding banners |
| Everything misbehaves | `"disableAllHooks": true` in the project's `.claude/settings.local.json` pauses its hooks. `claude doctor` checks the install |

## Habits

- Put work on the board before it starts. Anything bigger than a quick fix goes through `/harness:plan`.
- Run unfamiliar or risky tickets with `/harness:work <n>` in a session you watch. Dispatch the rest.
- Answer needs-input tickets and decide on parked ones the same day. Each waiting ticket holds a worktree.
- One session per project. `/clear` between unrelated tasks, with `/harness:handoff` first when you stop mid-task.
- Keep each project's `CLAUDE.md` under about 60 lines. `/harness:learn` turns a session's corrections into proposed chore tickets; promote the ones you agree with.
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
| Land | Squash-merge a PR, remove its worktree and branch, close the ticket, always through `harness-pr land`: the loop runs it with `--catch-up` for every ticket, at any risk, and you run `/harness:land <P>` only for a parked ticket's PR you choose to land |
| Park | What the loop does with a ticket still stuck after its capped rounds: `status:blocked`, a **Parked** comment with the evidence, a proposed follow-up, a notification, and on to the next ready ticket. `harness-board status N ready` unparks it |
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
