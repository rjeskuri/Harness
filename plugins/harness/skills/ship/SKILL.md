---
name: ship
description: Verify, review and commit the current change, and optionally push it and open a pull request. Use when the user runs /harness:ship or asks to wrap up and commit finished work.
argument-hint: "[pr]"
disable-model-invocation: true
allowed-tools:
  - Bash(git add:*)
  - Bash(git commit:*)
---
# Ship the current change

Unstaged changes:
!`git diff --stat`

Staged changes:
!`git diff --cached --stat`

1. **Verify.** Run the project's verify command (CLAUDE.md, or `CLAUDE_VERIFY_CMD` in the project settings), plus lint and type checks where the project has them. If anything fails, stop and report it. Do not commit failing work.
2. **Review.** Have the `harness:reviewer` subagent review the diff in a fresh context, against the spec or plan under `docs/agent/` or `docs/plans/` if there is one. Fix correctness findings (through `harness:coder` for anything beyond a one-liner). List everything else for the user instead of acting on it.
3. **Commit.** Stage only files that belong to this change: no scratch files, notebook outputs, data or secrets. Write the message as an imperative subject line under 72 characters, then a body that explains why the change was made and which checks passed.
4. **Pull request**, only if the arguments include `pr` (here: "$ARGUMENTS"). Push the branch and open a PR with `gh pr create`: a title, why the change exists, the verification evidence, and any risks. Pushing and PR creation ask for approval first; that is intended.
5. **Report** the commit hash, what was verified and how, and anything left undone. On a billable project, remind the user to run `/harness:worklog` before ending the session.
