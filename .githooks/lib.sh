#!/bin/sh
# Shared by the git hooks in this directory (git config core.hooksPath .githooks): the never-on-main check.
# harness_refuse_on_main <hook name>: exit 1 with one line when HEAD is main or master and a commit exists.
# HARNESS_ALLOW_MAIN=1 skips it, as it does the branch guard hook (harness-pr revert sets it); a repository
# with no commit yet makes its first one freely (the scaffold's first commit).
harness_refuse_on_main() {
    [ "${HARNESS_ALLOW_MAIN:-}" = 1 ] && return 0
    git rev-parse --verify -q HEAD >/dev/null 2>&1 || return 0
    branch=$(git symbolic-ref --short -q HEAD)
    case "$branch" in
        main|master)
            echo "$1: refused: on $branch. Commit on a ticket branch (harness-work start <issue#>) and land through a pull request." >&2
            exit 1 ;;
    esac
    return 0
}
