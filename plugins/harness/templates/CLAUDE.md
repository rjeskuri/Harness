<!--
Maintainer notes (HTML comments are stripped before Claude sees this file, so they cost no context).
Scaffolded by harness-scaffold ({{stack}}, {{year}}). Keep it short: only what Claude cannot infer
from the code. When Claude makes the same mistake twice, add one line here.
-->

# {{name}}

<One sentence: what this project does, who uses its output, and what "correct" means for it.>

## Commands
- Verify, the whole suite plus lint (CI and the stop hook run the same): `{{verify_cmd}}`
- Named test files: `{{test_cmd}}`, with `{files}` replaced by their paths

## Where things live
- {{tests_dirs}}: the test suite.
- `scripts/verify.sh`: the checks behind the verify command; `.github/workflows/ci.yml` runs it.
- `.githooks/pre-commit`: format and lint checks on staged files.
- `<path>`: <what lives there>

## Conventions
- Ask before adding a dependency; say what it does that the standard library or existing ones cannot.
- Secrets come from the environment or a gitignored `.env`; never on a command line, never committed.

## Board and branches
- Work starts from a ticket on the GitHub board: `harness-work start <issue#>` makes the branch
  `t/<issue#>-<slug>` and its worktree. Never on main; a hook blocks edits and commits there.
- Tests first: write the tests the ticket names, prove them red with `harness-red check`, then
  change the source. Never loosen a test to make it pass.
- Land through a pull request (`harness-pr open`): the user lands PRs; `harness-pr autoland` for eligible ones is the epic #132 plan.

## Done means
- The ticket's definition of done holds and `{{verify_cmd}}` passes. Show the command and its output.
- Say plainly what could not be verified here (production data, credentials, a GUI).

## Compact Instructions
When compacting, keep the ticket number and branch, the list of changed files, and the latest
verify or test result.
