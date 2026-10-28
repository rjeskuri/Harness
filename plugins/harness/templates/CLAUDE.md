<!--
Maintainer notes (HTML comments are stripped before Claude sees this file, so they cost no context).
Scaffolded by harness-scaffold ({{stack}}, {{year}}). Keep it short: only what Claude cannot infer
from the code. The loop's rules (board, branches, tests first) live in ~/.claude/CLAUDE.md and the
hooks, not here. When Claude makes the same mistake twice, add one line here.
-->

# {{name}}

<One sentence: what this project does, who uses its output, and what "correct" means for it.>

## Commands
- Verify, the whole suite plus lint (CI runs the same): `{{verify_cmd}}`
- Named test files: `{{test_cmd}}`, with `{files}` replaced by their paths

## Where things live
- {{tests_dirs}}: the test suite.
- `scripts/verify.sh`: the checks behind the verify command; `.github/workflows/ci.yml` runs it.
- `.githooks/`: `pre-commit` (format and lint on staged files, no commits on main) and `pre-push` (no pushes to main, no force pushes).
- `<path>`: <what lives there>

## Gotchas
- <A place where the code is misleading, a variable the tests need, a decision that is not in the repo.>
