"""The repository core harness-board, harness-pr, harness-work and harness-red build their Repo on
(repo_facts), and the main-checkout lookup of harness-worklog and the hooks session_brief.py and
tests_first.py (main_checkout). The main working tree is the parent of git's common dir, so a
linked worktree and its main checkout share one state key. See docs/design.md (repo-key).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import subprocess
import time

from harness_lib.paths import adopt_old_state, canonical_path

TIMEOUT = 10  # seconds main_checkout's whole lookup (both git calls) may take by default


class RepoError(Exception):
    """repo_facts' refusal, one line: the tool raises it again as its own Problem."""


class NotARepository(RepoError):
    """`path` (made absolute) is a directory outside any git working tree."""

    def __init__(self, message, path):
        super().__init__(message)
        self.path = path


def repo_facts(path, git, pick_line):
    """(root, common_dir, main, key) of the repository holding `path`: the toplevel git reports,
    the absolute common git dir, the canonical main working tree and its state key under
    ~/.claude/harness/repos (state under an earlier spelling's key is moved there first).
    `git(path, *args)` runs git for the caller, so its own failures (git missing, a timeout, a
    raising git) stay the caller's; `pick_line(stderr)` picks the line a failing
    `rev-parse --git-common-dir` is reported with. Raises RepoError."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise RepoError(f"not a directory: {path}")
    top = git(path, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        raise NotARepository(f"not a git repository: {path}", path)
    root = top.stdout.strip()
    common = git(root, "rev-parse", "--git-common-dir")
    if common.returncode != 0 or not common.stdout.strip():
        raise RepoError(f"git rev-parse --git-common-dir failed in {root}: {pick_line(common.stderr)}")
    common_dir = os.path.normpath(os.path.join(root, common.stdout.strip()))
    reported = os.path.realpath(os.path.dirname(common_dir))
    main = canonical_path(reported)
    repos = os.path.join(os.path.expanduser("~"), ".claude", "harness", "repos")
    return root, common_dir, main, adopt_old_state(repos, main, reported, path)


def main_checkout(path, timeout=TIMEOUT):
    """The canonical spelling of the main working tree of the repository holding `path` (from a
    subdirectory or a linked worktree too), with state under an earlier spelling's key moved to
    its key as repo_facts does; None outside a repository, without git, when git fails, or when
    the whole lookup (both git calls, one deadline) takes longer than `timeout` seconds. A hook
    passes its own git budget, which sits inside the timeout hooks.json gives it."""
    deadline = time.monotonic() + timeout

    def git(cwd, *args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(["git", "-C", cwd, *args], timeout)
        return subprocess.run(["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape", timeout=left)

    try:
        return repo_facts(path, git, str.strip)[2]
    except (RepoError, OSError, subprocess.SubprocessError):
        return None
