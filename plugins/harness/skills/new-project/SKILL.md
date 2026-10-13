---
name: new-project
description: Set up a new or existing repository for the harness loop - scaffold (CLAUDE.md, settings, verify script, CI, deploy stub, PR and issue templates, pre-commit), private GitHub repository, labels, registry entry. Use when the user runs /harness:new-project, starts a project, or asks to put a repository on the board.
argument-hint: "[path] [--stack python|node|none]"
disable-model-invocation: true
---
# New project

Arguments: $ARGUMENTS

1. **Look** at the path (default: the current directory): git repo or not, commits, remote, stack (`pyproject.toml`, `package.json`), its own CI under `.github/workflows`, an existing verify command. Run `harness-scaffold init <path> --dry-run` with the stack you detect and show the plan.
2. **Interview** with AskUserQuestion, one round: name (default the basename), stack, the verify command if the project already has one, GitHub repository yes or no (default yes, private under the user's account), billable and client, and `--no-ci` when the project has its own CI.
3. **Confirm** the irreversible part explicitly: creating the GitHub repository and pushing. Then `harness-scaffold init <path> --name ... --stack ... [--verify ...] [--test-cmd ...] [--no-ci] --github --register [--billable --client ...]`. For a repository that already has commits, the scaffold goes through a chore ticket, a branch and a pull request; say so and give the PR URL.
4. **Report** the files created or kept, the repository URL, the labels, whether branch protection could be enabled (not on GitHub Free private repositories; the local guard is the block), and the next step: `/harness:plan <first request>`.
