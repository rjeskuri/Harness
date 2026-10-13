# Harness design, v2

Ryan's personal Claude Code harness. One repository, one plugin, installed once at user scope, applied to every project. v2 written 2026-10-13 against Claude Code 2.1.287 (stable), gh 2.99.0, git 2.39. v1 (2026-10-06) is in git history; what changed and why is in `docs/research/` and in the interview notes at the end of this file.

## Principles

1. **The board is the context.** Every unit of work is a GitHub issue: an epic with sub-issue tasks, each carrying the request, an outline, a definition of done and the tests that must pass. Agents read the ticket and its epic before working and write status back. The user's job is to answer questions on tickets and review pull requests.
2. **Tests before code, proven.** A test-author writes tests from the ticket; a tool proves they fail against the unchanged code; an adversary tries to pass them with a trivial implementation; only then does a coder touch source. A hook denies source edits until the proof exists.
3. **Never on main.** Each ticket gets its own branch and worktree. A hook denies edits, commits and pushes on main in every repository. GitHub Free does not protect branches on private repos, so the hook is the only block; landing goes through a pull request the user merges.
4. **Route by labels, escalate by failure.** Opus 5.5 at high effort is the default everywhere. The planner labels each task with size and risk; the labels pick the tier (Sonnet, Opus, Fable). A second failure re-runs one tier up; a third stops and asks on the ticket.
5. **Spend credits before the subscription.** Interactive sessions use the Max subscription. Dispatched ticket work runs headless and bills the API key; high-risk pull requests get ultrareview, which bills claude.ai usage credits. When a budget is exhausted the dispatcher falls back one step and remembers for a day.
6. **Additive, never invasive.** Projects keep their own `CLAUDE.md`, hooks, rules and skills. The harness adds the board, the guards and the loop beside them, in every project including jotform-glow-sync.
7. **Deterministic where it must be, advisory where it can be.** Hooks and tools enforce; prompts advise.
8. **Fail open, except where a guard is sure.** A hook that hits its own error exits 0 and says so on stderr. The guards deny only when they have established the branch and the path.
9. **Standard library only.** `python3` 3.9 or newer, `git`, `gh`. Nothing to install.
10. **Hooks run outside the sandbox**, with the user's permissions and no prompt. The verify and test commands must be commands the user trusts.

## Layout

```
Harness/
  .claude-plugin/marketplace.json      marketplace "ryan": plugin harness
  plugins/harness/
    .claude-plugin/plugin.json
    agents/    planner.md test-author.md test-breaker.md coder.md reviewer.md researcher.md quick.md
    skills/    plan work land new-project handoff learn standup worklog     (one SKILL.md each)
    hooks/     hooks.json notify.py session_brief.py after_edit.py verify_on_stop.py readonly_bash.py
               branch_guard.py tests_first.py
    bin/       harness-board harness-work harness-red harness-pr harness-scaffold harness-digest
               harness-projects harness-standup harness-worklog        (python3, mode 755, on the Bash PATH)
    templates/ scaffold templates (see harness-scaffold)
    tests/     test_*.py, fakegh.py       python3 -m unittest discover -s plugins/harness/tests
  user/CLAUDE.md                        copied to ~/.claude/CLAUDE.md
  user/settings.snippet.json            merged into ~/.claude/settings.json by scripts/install.sh
  scripts/install.sh
  docs/design.md docs/guide.md docs/verification.md docs/research/
```

Every bin script is standalone (no imports between scripts): argparse, standard library, finds nothing through `CLAUDE_PLUGIN_ROOT` (that variable is not set for Bash). Scripts compose by calling each other on PATH and exchanging JSON (`--json`). A script that must call another finds it next to itself (`Path(__file__).resolve().parent`) when it is not on PATH. Each prints one line and exits 1 on a bad invocation or a failed precondition, never a traceback. `gh` is resolved as `os.environ.get("HARNESS_GH", "gh")` so tests can substitute a fake.

Install (once): `sh scripts/install.sh`. A marketplace added from a local path loads the plugin in place, so edits take effect at the next session start or `/reload-plugins`.

## Platform facts this design relies on

Verified 2026-10-13 from the Claude Code docs, the gh source at v2.99.0 and the GitHub docs; details in `docs/research/`.

- Plugin agents support `model` (alias, full id or `inherit`) and `effort`; they ignore `hooks` and `permissionMode`. The Agent tool takes `model` per call; its per-call `effort` parameter needs Claude Code 2.1.292, which the stable channel has not reached. An agent that omits `effort` runs at the session's level.
- `"model": "opus"` in settings resolves to Opus 5.5. `modelSettings` is keyed by canonical ids (`claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5`); a top-level `effortLevel` does not apply to Opus 5.5.
- PreToolUse receives `cwd`, `session_id`, `agent_type` (`harness:coder` for plugin agents), `tool_name`, `tool_input` (`file_path` absolute for Edit and Write, `notebook_path` for NotebookEdit, `command` for Bash). It denies with `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`; deny wins over every other hook. SessionStart carries no branch; its `cwd` is the worktree root when the session runs in one.
- `claude -w <name>` creates `.claude/worktrees/<name>` on branch `worktree-<name>` and cannot pick a branch name; `git worktree add` is the way to control the branch. `claude --bg -n <name> "<prompt>"` starts a background session in the cwd with the supervisor's stored login; trust is inherited from the main checkout; a session already in a linked worktree does not self-isolate. `claude rm` leaves a user-created worktree in place.
- Billing: `ANTHROPIC_API_KEY` and `apiKeyHelper` outrank the subscription login; in `-p` mode the key is used without a prompt. Background sessions cannot be billed separately. Cloud sessions and routines bill the subscription, need a TTY to start, and do not load plugins. `claude ultrareview <PR> --post --json` runs in the cloud, bills usage credits (about 5 to 25 dollars a run), and prints findings.
- gh: `gh issue create --parent` links a sub-issue (not idempotent on retry); `gh api repos/O/R/issues/N/sub_issues` lists sub-issues with labels; `--blocked-by`, `--add-blocked-by` and `--json blockedBy` are native; a parent never auto-closes. `gh label create --force` updates. `gh pr checks --watch` exits 0, 1 (failure) or 8 (pending) and counts cancelled runs as not failed. The PR author can post a `COMMENT` review with inline comments through `POST /repos/O/R/pulls/N/reviews`; `APPROVE` is refused. `gh pr merge -R O/R --squash --delete-branch` deletes only the remote branch; local cleanup is ours. Branch protection and rulesets return 403 on this account's private repos. Issue types are organization-only; labels it is.

## Settings

`user/settings.snippet.json` (merged into `~/.claude/settings.json` by the installer; keys replace, nothing else is touched):

```json
{
  "model": "opus",
  "effortLevel": "high",
  "modelSettings": {
    "claude-opus-5-5": { "effortLevel": "high" },
    "claude-fable-5-1": { "effortLevel": "xhigh" },
    "claude-sonnet-5-5": { "effortLevel": "high" }
  }
}
```

Per project, in `.claude/settings.json` under `env` (read by hooks and tools the way `CLAUDE_VERIFY_CMD` is today: environment first, then `.claude/settings.json`, then `.claude/settings.local.json`):

| Variable | Meaning | Default |
|---|---|---|
| `CLAUDE_VERIFY_CMD` | the whole suite plus lint; verify-on-stop and the coder run it | none (verify-on-stop off) |
| `HARNESS_TEST_CMD` | runs named test files; `{files}` is replaced by space-separated repo-relative paths | detected: `uv run pytest -q {files}` when `uv.lock` exists, `python3 -m pytest -q {files}` when `pyproject.toml` or `pytest.ini` names pytest, `python3 -m unittest {files}` for other `test_*.py` or `*_test.py`, `npm test -- {files}` when `package.json` has a `test` script, `node --test {files}` for `*.test.js` |
| `HARNESS_TEST_PATTERNS` | comma-separated globs that mark a path as a test file | `tests/**,test/**,**/test_*.py,**/*_test.py,**/conftest.py,**/*_test.js,**/*.test.js,**/*.test.ts,**/*_test.ts,spec/**` |
| `HARNESS_SOURCE_EXEMPT` | globs never gated by tests-first | `docs/**,**/*.md,.claude/**,.github/**,.githooks/**,**/*.toml,**/*.cfg,**/*.ini,**/*.json,**/*.yaml,**/*.yml,**/*.txt,**/*.lock,.gitignore,.worktreeinclude,LICENSE*` |
| `HARNESS_WORKTREE_SETUP` | command run in a fresh worktree | detected: `uv sync` when `uv.lock`, `npm ci` when `package-lock.json`; always followed by copying gitignored files that match `.worktreeinclude` |
| `HARNESS_ALLOW_MAIN` | `1` disables the branch guard for this project | unset |
| `HARNESS_TEST_TIMEOUT` | seconds for a red run | 300 |

Machine-wide, under `env` in `~/.claude/settings.json`: `HARNESS_API_KEY_CMD` (default `security find-generic-password -s harness-anthropic -w`), `HARNESS_NOTIFY_APP`, `HARNESS_NOTIFY_SOUND`, `HARNESS_SECRET_SCAN`, `CLAUDE_VERIFY_TIMEOUT` as before.

Globs match against the repo-relative path with `/` separators; `**` matches any number of segments including none; a pattern without `/` matches the basename. One matcher function, specified here so every script agrees: split the pattern on `/`, walk segments, `**` consumes zero or more, other segments use `fnmatch`.

## State

- `~/.claude/harness/projects.json`: the registry (unchanged, v1 schema).
- `~/.claude/harness/budgets.json`: `{"api": {"exhausted_until": ISO-8601 or null, "reason": ""}, "cloud": {...}}`.
- `~/.claude/harness/runs/<repo-key>-<issue>-<yyyymmddHHMMSS>.json`: one headless or background run (schema under harness-work).
- `~/.claude/harness/digest/<yyyy-mm-dd>.md`: the daily digest.
- `~/.claude/harness/repos/<repo-key>/`: durable per-repository state: `tickets/<issue>.json` (ticket cache), `red/<branch-slug>.json` (red proofs), `scratch/<branch-slug>/` (the adversary's working copy), `ultra/<pr>.json`.
- `$TMPDIR/claude-harness/<repo-key>/`: the v1 ephemeral state (dirty lists, fingerprints), unchanged.

`repo-key` is the first 12 hex digits of the sha1 of the real path of the **main** working tree, found as the parent of `git rev-parse --git-common-dir` resolved to an absolute path. A linked worktree and its main checkout therefore share one state directory. `branch-slug` is the branch name with `/` replaced by `-`.

Ticket cache `tickets/<N>.json`:

```json
{"number": 12, "title": "...", "url": "...", "type": "task|bug|chore|refactor|epic",
 "status": "ready|in-progress|in-review|blocked|needs-input|closed", "size": "S|M|L|null", "risk": "low|high|null",
 "flags": ["no-tests", "needs-local", "priority:high"], "labels": ["..."], "tier": "sonnet|opus|fable",
 "parent": {"number": 3, "title": "...", "goal": "first paragraph of the epic's Goal section"} | null,
 "blocked_by": [{"number": 9, "state": "OPEN|CLOSED"}],
 "body": "...", "sections": {"request": "...", "outline": "...", "definition_of_done": ["..."], "tests": ["..."], "context": "..."},
 "comments": [{"author": "rjeskuri", "created_at": "...", "body": "..."}],
 "fetched_at": "...", "branch": "t/12-slug" | null, "worktree": "/abs/path" | null, "started_at": "..." | null,
 "attempts": [{"stage": "tests|breaker|code|review|ci", "tier": "opus", "result": "pass|fail", "at": "..."}]}
```

Red record `red/<branch-slug>.json`:

```json
{"branch": "t/12-slug", "base_sha": "...", "test_files": ["tests/test_x.py"], "test_hash": "sha256 of the sorted files' contents",
 "cmd": "...", "exit": 1, "kind": "assertion|import|error|green", "tail": "last 40 lines", "at": "..."}
```

## The board

GitHub Issues in each project's private repository. An **epic** is an issue labeled `epic`; its **tasks** are sub-issues. Dependencies use GitHub's native blocked-by. Status, type, size, risk and flags are labels. Done is closed.

Labels (name, color, meaning), created idempotently by `harness-board labels ensure`:

| Group | Labels |
|---|---|
| type | `epic` 5319E7, `task` 0E8A16, `bug` D73A4A, `chore` BFD4F2, `refactor` C2E0C6 |
| status | `status:ready` 0052CC, `status:in-progress` FBCA04, `status:in-review` D4C5F9, `status:blocked` B60205, `status:needs-input` E99695 |
| size | `size:S` C5DEF5 (one sitting, one or two files), `size:M` BFDADC, `size:L` F9D0C4 (the cap: about 5 files or 300 lines; anything bigger is split) |
| risk | `risk:low` E6E6E6, `risk:high` B60205 (data loss, money, live systems, security, minors' data) |
| flags | `no-tests` EDEDED (docs or config only; exempt from tests-first), `needs-local` FEF2C0 (needs this Mac's files or credentials), `priority:high` FF7F0E |

Tier rubric, implemented once in `harness-board tier`: `size:S` and `risk:low` and type not `bug` gives `sonnet`; `size:L` or `risk:high` gives `fable`; everything else gives `opus`. Escalation order is sonnet, opus, fable.

Task body template (`templates/ticket_task.md`; the planner fills it, the scaffold installs it as a GitHub issue template too):

```markdown
## Request
<what was asked, in the requester's words, one paragraph>

## Outline
1. <step>

## Definition of done
- [ ] <observable, testable statement>

## Tests that must pass
Broad behavior first, specific cases after. One line each: where, and what it proves.
- `tests/test_<area>.py::test_<behavior>`: <the contract it pins down>
The existing suite (`<verify command>`) stays green.

## Context
- `<path>`: <why it matters>
- Must not change: <...>
```

Epic body template (`templates/ticket_epic.md`): `## Goal`, `## Why`, `## Scope` and `## Out of scope`, `## Definition of done`, `## Verification` (the end-to-end commands), `## Decisions and open questions`. Tasks are the sub-issues; no task list in the body.

## Agents

| Agent | Model, effort | Tools | Job |
|---|---|---|---|
| `planner` | opus, xhigh | Read, Grep, Glob, Bash (read-only guard) | Turns an interviewed request into an epic and tasks in the templates, with sizes, risks, flags and blocked-by, under the size cap. Returns a proposal; creates nothing. |
| `test-author` | opus (tier override per call), effort inherited | all | Writes the tests the ticket names, broad behavior first, against the public interface, and runs them to show them red. The tests-first hook denies it any source edit. |
| `test-breaker` | opus (tier override), inherited | all | In the scratch copy, writes the cheapest implementation that could pass the new tests (constants, special-casing the fixtures) and runs them. If it passes, the tests are too narrow. Denied test edits by the hook. |
| `coder` | opus (tier override), inherited | all | Makes the tests and the verify command green with the smallest coherent change, commits on the ticket branch, reports evidence. Denied test edits by the hook; stops and reports when the change outgrows the size cap. |
| `reviewer` | fable, xhigh | Read, Grep, Glob, Bash (read-only guard) | Reviews the branch against the ticket's definition of done; returns findings as JSON lines for the PR. |
| `researcher` | opus, inherited | WebFetch, WebSearch, Read, Grep, Glob, Bash (read-only guard) | Primary-source fact checking, as in v1. |
| `quick` | sonnet, inherited | all | Trivial mechanical tasks outside the ticket loop, as in v1. |

The orchestrator passes `model` on the Agent call from `harness-board tier` (and `effort` too once the Agent tool offers it: `high` by default, `xhigh` when escalating to Fable). The read-only guard (`readonly_bash.py`) now covers `planner`, `reviewer` and `researcher`.

## Skills

| Skill | Replaces | What it does |
|---|---|---|
| `plan [request]` | `spec` | Reads the code, interviews the user with AskUserQuestion, hands the answers to `planner`, shows the proposal (epic, tasks, sizes, risks, dependencies, tests) as a compact table, and on approval creates the issues with `harness-board`. |
| `work [issue#]` | `build` | With a number: runs that ticket to a ready pull request in this session (the loop below). Without: `harness-work dispatch`, one headless or background session per ready ticket. |
| `land [PR#]` | new | Without a number: the PRs awaiting the user. With one: summary (ticket, definition of done, review findings, checks), one confirmation, then `harness-pr land`. |
| `new-project [path]` | new | Short interview (name, stack, verify command, GitHub yes or no, billable), then `harness-scaffold`, confirmation before the repository is created and pushed, then suggests `/harness:plan`. |
| `learn` | changed | Same analysis as v1; every proposal becomes a `chore` ticket (body: the proposed diff and the reasoning) via `harness-board task create`, no edits on the spot. |
| `standup` | changed | Adds per project: board counts, PRs awaiting review, needs-input tickets, stale worktrees, failed runs; and the budget line. |
| `handoff`, `worklog` | unchanged | |

### The work loop (one ticket, in-session)

```
harness-work start N            ticket context, branch t/N-slug, worktree, status in-progress, tier
test-author (tier)              tests only; must show them red
harness-red check               proof: tests fail, source untouched; record written
test-breaker (tier)             scratch copy; cheat attempt; pass means tests go back to the author (max 2 rounds)
coder (tier)                    source only; new tests and CLAUDE_VERIFY_CMD green; commits
reviewer (fable)                findings JSON; high and medium go back to the coder (max 2 rounds)
harness-pr open N               push, draft PR from the template (Closes #N, DoD, red proof, verify tail)
harness-pr review P --findings  inline review comments from the reviewer's JSON
harness-pr checks P --wait      CI; failures go back to the coder (max 2 rounds)
[harness-pr ultra P]            when risk:high: ultrareview, posted to the PR
harness-pr ready P              ready for review, status in-review, notification
```

Failure policy, enforced by `harness-work attempt`: the first failure of a stage retries at the same tier; the second re-runs one tier up; the third calls `harness-board needs-input` with the evidence and stops. A stage is `tests` (red cannot be reached), `breaker` (tests still cheatable after two rounds: proceed, but say so in the PR), `code` (verify not green), `review` (findings not fixed), `ci` (checks red). Any question the agents cannot answer from the ticket, the epic or the code also ends in `needs-input`. A revisit of a ticket with `status:needs-input` and a newer comment by the repository owner passes that comment to the agents as the answer. A revisit of a ticket in `status:in-review` with unresolved review threads from the owner runs the coder on those comments only, then checks and ready again.

## Hooks

`hooks.json` wires: Notification to `notify.py`; SessionStart to `session_brief.py`; PreToolUse `Bash` to `readonly_bash.py` and `branch_guard.py`; PreToolUse `Edit|Write|NotebookEdit` to `branch_guard.py` and `tests_first.py`; PostToolUse `Edit|Write|NotebookEdit|Bash` to `after_edit.py`; Stop to `verify_on_stop.py`. Timeouts 10 s for the guards.

**branch_guard.py** (PreToolUse `Edit|Write|NotebookEdit|Bash`). Fail open. Exempt when `HARNESS_ALLOW_MAIN` is `1` (environment or project settings).
- Edit, Write, NotebookEdit: take `file_path` or `notebook_path`; walk up to an existing directory; `git -C <dir> symbolic-ref --short HEAD`; if the branch is `main` or `master` and `git rev-parse --verify HEAD` succeeds (the repo has a commit), deny: `On main in <repo basename>. Work happens on a ticket branch: harness-work start <issue#> for a ticket, or git switch -c t/<n>-<slug> for untracked work.` Outside a repo, on a detached HEAD, or on an empty repo: allow.
- Bash: resolve the repo from `cwd` (and from `git -C <path>` when present). Only when that repo is on main or master with a commit: split the command on `;`, `&&`, `||`, `|` and newlines and deny when any part is: `git commit`, `git merge` (unless `--ff-only`), `git rebase`, `git cherry-pick`, `git revert`, `git am`, `git stash pop|apply`, `git reset`, `git push` with no refspec or with a refspec naming `main` or `master` (`main`, `HEAD:main`, `refs/heads/main`), any `git push` with `--force`, `-f` or `--force-with-lease` (on any branch, any repo state), `git branch -D|-d main|master`, `git push <remote> --delete|:` anything; `sed -i`, `perl -i`, a redirection (`>`, `>>`, `tee`) to a path inside the repo, `cp`, `mv`, `touch`, `rm` with a destination or target inside the repo (a path is inside the repo when it is relative or starts with the repo root). Reason text names the part. Allowed on main: everything that reads, `git fetch`, `git pull`, `git switch`, `git checkout <branch>`, `git worktree ...`, `gh ...`, `harness-* ...`, the verify command.
- Never deny inside a linked worktree (its branch is never main), never deny for a different repo than the one on main.

**tests_first.py** (PreToolUse `Edit|Write|NotebookEdit`). Fail open.
1. Resolve the file's repo and branch as above. Not a repo, detached, or branch not matching `^t/(\d+)-` → allow.
2. Classify the repo-relative path: test (`HARNESS_TEST_PATTERNS`), exempt (`HARNESS_SOURCE_EXEMPT`), else source.
3. Role rules from `agent_type` (compare the part after the last `:`): `test-author` editing source → deny `You write tests only on this ticket. Report the source change the implementation needs; the coder makes it.`; `coder` editing a test → deny `Tests are fixed for this ticket. If a test is wrong, stop and report it; the orchestrator sends it back to the test-author.`; `test-breaker` editing a test → deny.
4. Source edit by anyone: read `tickets/<N>.json`. `no-tests` in flags → allow. Load `red/<branch-slug>.json`; missing → deny `Tests first on t/N: write the tests, then run harness-red check. (No red run recorded.)`; type `refactor` → allow when a record exists (green is fine); otherwise require `kind` not `green` and `test_hash` equal to the sha256 of the record's test files as they are on disk now → allow; mismatch → deny `Tests changed since the red run; run harness-red check again.` No ticket cache → treat as a task without flags (deny until red) and say `harness-work start N` writes the cache.
5. Test and exempt edits → allow silently.

**readonly_bash.py**: GUARDED gains `harness:planner` and `planner`. Otherwise unchanged.

**session_brief.py**: after the heading, when the branch matches `^t/(\d+)-`: `Ticket #N: <title> (<status>, tier <tier>)`, up to six definition-of-done lines, `Red run: <kind> at <time>` or `Red run: none`, from the cache files only (no network). Otherwise, when the repo has an `origin` remote and `gh` answers within 3 s: `Board: <r> ready, <p> in progress, <v> awaiting your review, <q> needs input`. When `budgets.json` marks a budget exhausted: `Budget: api exhausted until <time> (<reason>)`. Still at most 40 lines; the rest as in v1.

`notify.py`, `after_edit.py`, `verify_on_stop.py`: unchanged.

## bin tools

### harness-board

`harness-board <sub> [--repo PATH] [--json]`. Repo: `--repo`, else `CLAUDE_PROJECT_DIR`, else the toplevel of the cwd. `OWNER/REPO` from `gh repo view --json nameWithOwner`; no remote or no `gh` login → one line, exit 1.

- `labels ensure`: the label table above, `gh label create <name> --color <hex> --description <text> --force`. Prints how many were created or updated.
- `epic create --title T --body-file F [--size S] [--risk R]`: labels `epic`, `status:ready`, size and risk when given. Prints `#N <url>`.
- `task create --title T --body-file F [--parent N] [--type task|bug|chore|refactor] [--size S|M|L] [--risk low|high] [--blocked-by N[,N]] [--no-tests] [--needs-local] [--priority]`: creates the issue without `--parent`, then links it with `gh api -X POST repos/O/R/issues/<parent>/sub_issues -F sub_issue_id=<numeric id from gh issue view --json id? no: REST .id>` (the numeric database id, from `gh api repos/O/R/issues/N --jq .id`); a failed link is retried once and then reported with the issue number so no duplicate is ever created. Then `gh issue edit N --add-blocked-by`. Labels: type (default `task`), `status:ready`, size, risk, flags.
- `show N [--markdown]`: `gh issue view N --json number,title,url,body,labels,state,parent,blockedBy,comments`; when the issue is an epic, `gh api repos/O/R/issues/N/sub_issues` for the tasks with labels and state; parses the body sections (headings `## Request`, `## Outline`, `## Definition of done` as checkbox lines, `## Tests that must pass`, `## Context`); writes the cache (preserving `branch`, `worktree`, `started_at`, `attempts` from an existing cache); prints JSON, or with `--markdown` the ticket context, at most 120 lines: title and labels, the sections, the epic's goal paragraph, blocked-by numbers with state, the last 10 comments (author, date, first 20 lines each).
- `ready`: open issues with `status:ready` and not `epic`, whose `blockedBy` are all closed, `priority:high` first, then lowest number first. Table or JSON (number, title, type, size, risk, tier, flags).
- `status N <ready|in-progress|in-review|blocked|needs-input> [--comment TEXT]`: removes every `status:*` label, adds the new one, comments when asked.
- `comment N (--body TEXT | --body-file F)`.
- `needs-input N --question TEXT [--evidence-file F]`: `status needs-input`, a comment `**Needs your input**` with the question, the evidence (fenced, at most 60 lines) and the line `Reply in a comment; /harness:work N picks it up.`, then a macOS notification `<repo> #N needs input` (terminal-notifier, else osascript; same lookup as notify.py). Prints the URL.
- `answers N [--since ISO]`: comments by the authenticated user (`gh api user --jq .login`) newer than `--since` (default: the cache's `fetched_at`), JSON.
- `close N [--comment TEXT]`; `epic-check N`: when `subIssuesSummary.completed == total` and total > 0, comment `All tasks landed.` and close the epic.
- `board`: markdown by status, counts in the headings, `#N title (type, size, risk)` lines; `--json` the same structure.
- `tier N`: `sonnet`, `opus` or `fable` per the rubric; from the cache when present, else fetched.

### harness-work

- `start N [--repo PATH]`: `harness-board show N` (cache); refuse an epic, a closed issue, a blocked ticket (open blockers listed) or `status:blocked`; `git fetch origin`; branch `t/N-<slug>` (slug: title lower-cased, non `[a-z0-9]` runs to `-`, trimmed, at most 30 chars); if `origin/t/N-*` exists reuse that branch, else `git worktree add <main>/.claude/worktrees/t-N-<slug> -b t/N-<slug> origin/main` (when the worktree already exists, reuse it); ensure `.git/info/exclude` lists `.claude/worktrees/`; run `HARNESS_WORKTREE_SETUP` or the detected setup in the worktree, then copy gitignored files matching `.worktreeinclude` from the main checkout; `harness-board status N in-progress --comment "Started on t/N-<slug> (<hostname>, <venue>)"` unless already in progress; record `branch`, `worktree`, `started_at` in the cache. Prints JSON `{"number", "branch", "worktree", "tier", "context": "<markdown>"}`; without `--json` prints the worktree path and then the markdown context.
- `attempt N <stage> <pass|fail> [--tier T]`: appends to the cache and prints the next action for that stage on failure: `retry` (one failure so far), `escalate <tier>` (two failures; the tier above the last used, Fable stays Fable), `needs-input` (three). Prints `done` on pass.
- `dispatch [--max 3] [--venue headless|bg] [--dry-run]`: first `runs --sweep` (below); then `harness-board ready`; skip tickets that have a live run; for each remaining up to `--max` minus live runs: `start N`, then launch:
  - venue `headless` (default when `budgets.json` shows `api` not exhausted): in the worktree, `claude -p "/harness:work N" --permission-mode auto --output-format json --settings <json>` where the settings JSON is `{"apiKeyHelper": "<HARNESS_API_KEY_CMD>"}` (the key itself never appears on a command line or in the environment), detached with `nohup`/`setsid`-equivalent (`subprocess.Popen(start_new_session=True)`), stdout and stderr to `~/.claude/harness/runs/<id>.log`;
  - venue `bg` (fallback): in the worktree, `claude --bg -n "<repo> #N <title>" "/harness:work N"`; parse the id from `backgrounded · <id>`.
  - Write the run record `{"id", "ticket", "repo", "worktree", "branch", "venue", "pid" | "bg_id", "log", "started_at", "ended_at": null, "exit": null, "outcome": null}`.
  - `--dry-run` prints the plan and launches nothing.
- `runs [--sweep] [--all]`: lists run records with state (alive, exited N, done). `--sweep` updates records whose process has exited: reads the log tail, sets `exit` and `outcome` (`ok`, `billing`, `auth`, `error`): `billing` when the tail matches `credit balance|insufficient.*credit|billing|payment required|402`; `auth` on `invalid.*api.key|authentication_error|401`. A `billing` or `auth` outcome on a headless run marks `api` exhausted for 24 h with the matching line as reason, and the next `dispatch` uses `bg`. A ticket whose run ended without a PR and is still `status:in-progress` is re-dispatched by the next `dispatch`.
- `budget [status | exhaust <api|cloud> --reason TEXT [--hours 24] | reset <api|cloud>]`.
- `worktrees [--stale DAYS]`: every `.claude/worktrees/*` of the repo with branch, last commit age, PR number and state (from `gh pr list --head`), flagged stale when older than `--stale` (default 7) and without an open PR. `worktree remove <branch> [--discard]`: `git worktree remove` (refuses a dirty tree unless `--discard`) then `git branch -D`.

Cloud dispatch is out of scope for v2: cloud sessions bill the subscription, need a TTY and do not load plugins. `needs-local` is kept on tickets so a future cloud venue can respect it.

### harness-red

Runs in a ticket worktree (cwd, or `--worktree PATH`).

- `check [--base origin/main]`:
  1. `merge-base HEAD <base>`; changed paths = `git diff --name-only <merge-base>` plus `git status --porcelain` untracked and modified. Test files = those matching `HARNESS_TEST_PATTERNS`; none → `No test changes on this branch; write the tests first.` exit 1.
  2. Source files changed (not test, not exempt) → `Source already changed before the red run: <list>. Revert them or move them to the coder step.` exit 1.
  3. Run `HARNESS_TEST_CMD` with `{files}` = the test files, `cwd` = worktree, timeout `HARNESS_TEST_TIMEOUT`, `PYTHONDONTWRITEBYTECODE=1`.
  4. kind: `green` when exit 0; else `import` when the output matches `ModuleNotFoundError|ImportError|No module named|Cannot find module|ERR_MODULE_NOT_FOUND`; `assertion` when it matches `AssertionError|assert |FAILED|failed|✗|not ok`; else `error`.
  5. Write the record; print `red (<kind>): <n> test files, exit <code>` and the tail; exit 0 when red. On `green`: `Tests pass before the implementation; they do not test this change.` exit 1 (the record is still written so a refactor ticket can proceed).
- `status`: the record for the current branch, and `current: yes|no` (test_hash against the files now).
- `scratch create`: a detached worktree of `HEAD` at `~/.claude/harness/repos/<key>/scratch/<branch-slug>` (plus uncommitted test files copied in), prints the path; `scratch run`: `HARNESS_TEST_CMD` on the record's test files in the scratch, prints `cheat passed` (exit 0) or `cheat failed` (exit 1) and the tail; `scratch remove`.

### harness-pr

- `open N [--worktree PATH] [--verify-log F] [--review F]`: in the ticket's worktree: refuse with the list when the tree is dirty; `git push -u origin <branch>`; body from `templates/pr_body.md` with the title, `Closes #N`, the definition-of-done checkboxes copied from the ticket, the tests list, the red proof (kind, exit, 15 lines of tail), the verify tail (20 lines of `--verify-log`), the review summary (counts by severity and the high findings from `--review`), a risks line for `risk:high`; `gh pr create --draft --base main --head <branch> --title "<title> (#N)" --body-file`; if a PR for the branch exists, `gh pr edit --body-file` instead; `harness-board comment N` with the PR URL; prints `{"number", "url"}`.
- `review P --findings F`: findings JSON list `[{"path", "line", "severity": "high|medium|low", "body"}]`; `gh pr diff P` is parsed to the set of (path, right-side line) in the diff; findings on those lines become inline comments (`path`, `line`, `side: RIGHT`), the rest are listed in the review body; `gh api repos/O/R/pulls/P/reviews --input` with `event: COMMENT` and body `Harness review (<model>): <n> findings, <h> high, <m> medium, <l> low.` followed by the off-diff list and the marker `<!-- harness-review -->`. Zero findings: `gh pr review P --comment -b "Harness review: no findings. Checked: <text from --checked>"`.
- `checks P [--wait] [--timeout 1200]`: poll `gh pr view P --json statusCheckRollup` every 20 s: no checks after 120 s → `no checks registered` exit 0 with a warning; any `PENDING|QUEUED|IN_PROGRESS` → keep waiting; any `FAILURE|CANCELLED|TIMED_OUT|ERROR|ACTION_REQUIRED|STARTUP_FAILURE` → exit 1 with the names and, for GitHub Actions runs (details URL `/actions/runs/<id>`), the last 60 lines of `gh run view <id> --log-failed`; all `SUCCESS|SKIPPED|NEUTRAL` → exit 0. Without `--wait` one poll.
- `ready P`: `gh pr ready P`; ticket number from `closingIssuesReferences` (or `--ticket N`); `harness-board status N in-review --comment "PR #P ready for review: <url>"`; notification `<repo> PR #P ready for review: <title>`.
- `comments P [--since ISO] [--unresolved]`: review threads (GraphQL `reviewThreads` with `isResolved`, comments with author, path, line, body, createdAt), reviews and issue comments on the PR, excluding bodies containing `<!-- harness-review -->`; `--unresolved` keeps unresolved threads only. JSON or markdown.
- `awaiting [--all-projects]`: open, non-draft PRs by the authenticated user: number, title, ticket, age, checks state (one poll), unresolved thread count. `--all-projects` walks the registry.
- `ultra P [--post]`: `claude ultrareview P --post --json --timeout 30` (post only with `--post`); saves stdout to `ultra/<P>.json`; a non-zero exit whose output mentions credits, billing or balance marks `cloud` exhausted for 24 h; prints the finding count and the highest severities.
- `land P [--yes] [--discard]`: preconditions: not a draft, `mergeable` not `CONFLICTING` (retry twice on `UNKNOWN`), checks green by one poll (cancelled counts as red), `closingIssuesReferences` non-empty (warn and continue when empty). Then `gh pr merge P -R O/R --squash --delete-branch`; locate the worktree for the head branch in `git worktree list --porcelain`; `git worktree remove` (`--force` only with `--discard`; a dirty worktree otherwise stops after the merge with instructions); `git branch -D <branch>`; in the main checkout `git fetch --prune` and, when it is on main, `git pull --ff-only`; confirm the ticket closed (else `gh issue close`), remove its `status:*` labels, comment `Landed in <merge sha>`; `harness-board epic-check <parent>` when there is a parent. Prints a four-line summary.

### harness-scaffold

Templates in `plugins/harness/templates/`, rendered by plain `{{name}}` substitution (`name`, `pkg`, `stack`, `verify_cmd`, `test_cmd`, `python_version`, `year`). Never overwrites an existing file unless `--force`; reports each path as created, kept or updated.

- `init [PATH] --name NAME [--stack python|node|none] [--verify CMD] [--test-cmd CMD] [--github] [--register [--billable --client C]] [--no-ci] [--dry-run]`:
  1. `mkdir -p`, `git init -b main` when not a repo.
  2. Files: `CLAUDE.md` (lean: commands, where things live, done means, board and branch rules), `.claude/settings.json` (`env`: `CLAUDE_VERIFY_CMD`, `HARNESS_TEST_CMD`; `permissions.allow`: `Bash(scripts/verify.sh)`, `Bash(git status:*)`, `Bash(git diff:*)`, `Bash(git log:*)`, `Bash(git add:*)`, `Bash(git commit:*)`, `Bash(git push:*)`, `Bash(git fetch:*)`, `Bash(git worktree:*)`, `Bash(gh issue:*)`, `Bash(gh pr:*)`, `Bash(gh api:*)`, `Bash(harness-*:*)`, stack test runners), `scripts/verify.sh` (python: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`, ending with `echo VERIFY OK`; node: `npm test`; none: a comment and `true`), `.github/workflows/ci.yml` (`push` on main, `pull_request`, `workflow_dispatch`; concurrency per ref; `actions/checkout@v7`; python: `astral-sh/setup-uv@v10.3.0` with cache and `uv sync --locked --all-extras --dev`; node: `actions/setup-node@v7`, `node-version: 24`, `cache: npm`, `npm ci`; then `scripts/verify.sh`), `.github/workflows/deploy.yml` (`push` on main and `workflow_dispatch`; runs `scripts/deploy.sh` when it exists, else prints `no deploy script`), `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/task.md` and `epic.md` (the ticket templates), `.githooks/pre-commit` (python: `ruff format --check` and `ruff check` on staged `.py`; node: `npm run lint --if-present`; exits non-zero with the file list) plus `git config core.hooksPath .githooks`, `.gitignore`, `.worktreeinclude` (`.env`, `.env.*`, `credentials/`), `.git/info/exclude` with `.claude/worktrees/`. Python also: `pyproject.toml` (project, `requires-python >= 3.10`, dev dependencies `pytest` and `ruff`, `[tool.ruff]`, `[tool.pytest.ini_options]`), `src/<pkg>/__init__.py`, `tests/test_smoke.py`. Node also: `package.json` with `"test": "node --test"`, `tests/smoke.test.js`. `--no-ci` skips both workflows (a project with its own CI, such as jotform-glow-sync).
  3. A repo with no commits: commit everything on main as `Scaffold <name> with the harness`. A repo that already has commits: create the chore issue `Adopt the harness scaffold` (`no-tests`, `size:S`, `risk:low`), `harness-work start N`, write the files in that worktree, commit, `harness-pr open N`, `harness-pr ready P`; this needs a GitHub remote, so `--github` runs first when there is none.
  4. `--github`: `gh repo create <name> --private --source . --remote origin` (no push), `git push -u origin main`, `harness-board labels ensure`, `gh repo edit --delete-branch-on-merge`, then a ruleset attempt (`POST repos/O/R/rulesets`: target `main`, rules `pull_request`, `non_fast_forward`, `deletion`); a 403 prints `Branch protection is unavailable on this plan for a private repo; the local branch guard is the only block.` and continues.
  5. `--register`: `harness-projects add`.
- `labels`: `harness-board labels ensure` for an existing repo (alias).

### harness-digest

Across the registry: PRs awaiting review (`harness-pr awaiting --all-projects`), `status:needs-input` tickets with the first line of the question, `status:in-progress` tickets older than 3 days without a PR, stale worktrees, runs with outcome `billing|auth|error` since the last digest, budgets. Writes `~/.claude/harness/digest/<date>.md`, prints it, and sends one notification `Harness digest: <p> PRs to review, <q> need input, <s> stale` (nothing when all zero). `--install-launchd` writes `~/Library/LaunchAgents/com.ryaneskuri.harness-digest.plist` (daily at 08:30, `python3 <absolute path of this script>`, `PATH` including `/opt/homebrew/bin` and `~/.local/bin`, stdout and stderr to `~/.claude/harness/digest/launchd.log`) and loads it with `launchctl bootstrap gui/<uid>`; `--uninstall-launchd` reverses it. Each project's calls get a 6 s budget; a project that cannot be read gets one line.

### harness-standup, harness-projects, harness-worklog

`harness-standup` adds, per project and inside the existing time budget (gh calls run in threads, 6 s): `Board: r ready, p in progress, v in review, q needs input`; `PRs awaiting you:` lines; `Needs input:` lines (title, first line of the question); `Stale worktrees:` lines; and after the fleet section a `## Budgets` line when any budget is exhausted and a `## Runs` section for headless runs alive or failed in the last day. `harness-projects` and `harness-worklog` are unchanged.

## Templates

`templates/`: `ticket_task.md`, `ticket_epic.md`, `pr_body.md`, `CLAUDE.md`, `settings.json`, `verify.sh`, `ci.yml`, `deploy.yml`, `pull_request_template.md`, `pre-commit`, `gitignore`, `worktreeinclude`, `pyproject.toml`, `package.json`, `test_smoke.py`, `smoke.test.js`.

`pr_body.md`:

```markdown
Closes #{{number}}

## Summary
{{summary}}

## Definition of done
{{dod_checklist}}

## Tests
{{tests_list}}

Red run before implementation ({{red_kind}}, exit {{red_exit}}):
```
{{red_tail}}
```

Verify:
```
{{verify_tail}}
```

## Review
{{review_summary}}

## Risks
{{risks}}
```

## user/CLAUDE.md

Rewritten around the loop. Sections: how I work (unchanged lines), **Routing** (Opus high default; `harness-board tier` picks the agent model from the ticket labels; pass `model`, and `effort` when the Agent tool offers it; escalate once on a second failure, needs-input on a third; Fable only for review, L and high-risk), **The board is the context** (read the ticket and epic before any work; status changes go through `harness-board`; questions become `needs-input`, never guesses), **Never on main** (ticket branch and worktree via `harness-work start`; agents push `t/*` branches and open PRs without asking; never merge, never push main, never force-push, never delete a branch: the user lands with `/harness:land`), **Tests first** (test-author, red proof, breaker, then coder; never loosen a test), **Budgets** (dispatch runs headless on the API key, falls back to the subscription; ultrareview on risk:high), **Across projects** (as v1, with `/harness:plan` and `/harness:work`), **Compact instructions** (add the ticket number and branch).

## Install

`scripts/install.sh`: marketplace add, plugin install (harness only), `~/.claude/CLAUDE.md` with a dated backup, settings merge (python3: load `~/.claude/settings.json`, set `model`, `effortLevel`, `modelSettings` from the snippet, drop `enabledPlugins["companion@ryan"]`, write back with a dated backup), `harness-digest --install-launchd`, and a reminder to store the API key once: `security add-generic-password -a "$USER" -s harness-anthropic -w` (interactive prompt; the key never touches a command line), and to run `harness-projects discover`.

## Tests (plugins/harness/tests)

`python3 -m unittest discover -s plugins/harness/tests -v`, offline, under 60 s. `fakegh.py` is an executable stand-in for `gh`: it reads a scenario JSON (path in `FAKEGH_SCENARIO`) mapping argv prefixes to stdout, exit code and optional stdin capture, appends every call to `FAKEGH_LOG`, and exits 1 with `fakegh: no match` otherwise. Tests put it on PATH as `gh` or set `HARNESS_GH`. Tests never touch `~/.claude` (temporary HOME) and use temporary repos with real `git`.

Cover at least: the glob matcher; branch_guard (deny edit on main with a commit; allow on a `t/` branch, in a linked worktree, on an empty repo, outside a repo, with `HARNESS_ALLOW_MAIN`; Bash denies for commit, push without refspec, push to main, force push, `sed -i`, redirect into the repo; allows for fetch, pull, status, a push on a ticket branch); tests_first (role rules for each agent type; deny source without a red record; allow after a current red record; deny after test files changed; `no-tests` exemption; `refactor` with a green record; test and exempt paths pass); session_brief's ticket lines and board line with a fake `gh`; harness-board (labels ensure calls, task create with link retry and no duplicate, show's section parsing and cache merge, ready's blocked-by filtering and ordering, status label swap, needs-input comment text, tier rubric for each combination); harness-work (start creates the branch and worktree from a temporary origin, reuses an existing remote branch, writes the exclude line, refuses an epic and a blocked ticket; attempt's retry, escalate and needs-input sequence; dispatch dry-run plan; runs sweep classifies billing output and marks the budget; budget status, exhaust, reset); harness-red (red by assertion, red by import, green refused, source-changed refused, no tests refused, scratch create and run); harness-pr (open's body rendering and idempotent edit, review's diff-line routing to inline vs body, checks' decisions over success, pending, failure and cancelled rollups, land's merge then cleanup sequence with a dirty worktree refusal); harness-scaffold (python and node renders in a temporary dir, kept files, `--no-ci`, the first-commit path, the adopt path's calls in order with a fake gh); harness-digest (rendering with a fake registry and fake gh, launchd plist contents); harness-standup's new sections. Keep the v1 tests passing.

## Daily use

- **New project**: `/harness:new-project ~/Projects/<name>`.
- **Plan**: `/harness:plan <request>` interviews you and writes the epic and tasks on the board.
- **Work**: `/harness:work` dispatches every ready ticket; `/harness:work 12` runs one here. Notifications tell you when a PR is ready or a ticket needs you.
- **Answer**: reply on the ticket; the next `/harness:work 12` reads it.
- **Land**: `/harness:land` lists PRs waiting; `/harness:land 7` merges one and cleans up.
- **Morning**: `/harness:standup`; the digest arrives at 08:30 on its own.
- **Lessons**: `/harness:learn` files chore tickets. **Context**: `/harness:handoff` before `/clear`. **Hours**: `/harness:worklog`.

## Interview decisions, 2026-10-13

Companion removed (tokens). Board on GitHub Issues with sub-issues, no Projects board, no local files. Main session on Opus 5.5 high; planner Opus xhigh; Fable only for review. Tiers from size and risk labels, escalation once, then needs-input. Tests-first with a hook gate and a red proof, plus the adversarial test review. Worktree per ticket. Agents push ticket branches and open PRs freely; the user merges from the CLI with `land`. CI on PRs and main plus a deploy stub; branch protection when the plan allows (it does not, today). Manual kick-off, background sessions for parallel tickets. Every project adopts the flow, including jotform-glow-sync. Extras: reviewer findings on the PR, CI wait-and-fix, needs-input loop, lessons as tickets, size cap, pre-commit and PR template, daily digest. Budgets: credits first (API for dispatch, usage credits for ultrareview), subscription as the fallback once a budget is exhausted. Harness, Kalshi and PilatesAttendance get private GitHub repos during the v2 rollout, with confirmation before each push.
