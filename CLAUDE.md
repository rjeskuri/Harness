<!--
Maintainer notes (HTML comments are stripped before Claude sees this file, so they cost no context).
Scaffolded by harness-scaffold (none, 2026). Keep it short: only what Claude cannot infer
from the code. When Claude makes the same mistake twice, add one line here.
-->

# Harness

Ryan's Claude Code harness: one plugin (agents, skills, hooks, command-line tools) that every project on this Mac loads in place from this checkout. Correct means the hooks deny exactly what `docs/design.md` says and the tools keep its contracts.

## Commands
- Verify, the whole suite plus lint (CI and the stop hook run the same): `python3 -m unittest discover -s plugins/harness/tests`

## Where things live
- `plugins/harness/tests/`: the suite; `fakegh.py` is the scripted fake `gh` every tool test uses, and fake executables are written once per module.
- `plugins/harness/{agents,skills,hooks,bin,templates}/`: the plugin. `docs/design.md` is the contract; `docs/research/` holds the verified platform facts.
- `user/` and `scripts/install.sh`: the personal CLAUDE.md and settings snippet, and how they are installed.
- `scripts/verify.sh`: the checks behind the verify command; `.github/workflows/ci.yml` runs it.
- `.githooks/pre-commit`: format and lint checks on staged files.

## Conventions
- python3 standard library only, 3.9-compatible (`from __future__ import annotations`); tests are standalone unittest modules that drive the tools as subprocesses.
- Ask before adding a dependency; say what it does that the standard library or existing ones cannot.
- Secrets come from the environment or a gitignored `.env`; never on a command line, never committed.

## Board and branches
- Work starts from a ticket on the GitHub board: `harness-work start <issue#>` makes the branch
  `t/<issue#>-<slug>` and its worktree. Never on main; a hook blocks edits and commits there.
- Tests first: write the tests the ticket names, prove them red with `harness-red check`, then
  change the source. Never loosen a test to make it pass.
- Land through a pull request (`harness-pr open`): eligible PRs are merged by `harness-pr autoland`, the user lands the rest.

## Done means
- The ticket's definition of done holds and `python3 -m unittest discover -s plugins/harness/tests` passes. Show the command and its output.
- Say plainly what could not be verified here (production data, credentials, a GUI).

## Compact Instructions
When compacting, keep the ticket number and branch, the list of changed files, and the latest
verify or test result.
