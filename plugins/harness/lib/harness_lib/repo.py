"""The repository core harness-board, harness-pr, harness-work and harness-red build their Repo on
(repo_facts), the main-checkout lookup of harness-worklog and the hooks session_brief.py and
tests_first.py (main_checkout), and the one `git worktree list --porcelain` parser harness-work,
harness-pr, harness-digest and harness-standup share (worktree_entries). The main working tree is
the parent of git's common dir, so a linked worktree and its main checkout share one state key.
See docs/design.md (repo-key).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import NamedTuple, Optional

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


class Worktree(NamedTuple):
    """One entry of `git worktree list --porcelain`: the folder, the branch's short name (None when
    detached or bare), the head commit (None for a bare repository) and the bare, locked and prunable
    flags (a lock's or a prune's reason is not kept)."""

    path: str
    branch: Optional[str]
    head: Optional[str]
    bare: bool
    locked: bool
    prunable: bool


def worktree_entries(main, git):
    """The worktrees of the repository holding `main` as Worktree entries, the main working tree
    first. `git(main, "worktree", "list", "--porcelain")` runs git for the caller and returns the
    output text, or an object carrying it as .stdout; the caller's own failures (a non-zero exit, a
    timeout, git missing) stay its own: whatever its git raises reaches it untouched."""
    out = git(main, "worktree", "list", "--porcelain")
    text = out if isinstance(out, str) else out.stdout
    entries, current = [], None
    for line in text.splitlines():
        if not line.strip():
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "branch": None, "head": None, "bare": False, "locked": False,
                       "prunable": False}
            entries.append(current)
        elif current is None:
            continue
        elif key == "branch":
            current["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key == "HEAD":
            current["head"] = value
        elif key in ("bare", "locked", "prunable"):
            current[key] = True
    return [Worktree(**entry) for entry in entries]


def main_checkout(path, timeout=TIMEOUT, raise_timeout=False):
    """The canonical spelling of the main working tree of the repository holding `path` (from a
    subdirectory or a linked worktree too), with state under an earlier spelling's key moved to
    its key as repo_facts does; None outside a repository, without git, when git fails, or when
    the whole lookup (both git calls, one deadline) takes longer than `timeout` seconds. A hook
    passes its own git budget, which sits inside the timeout hooks.json gives it; with
    `raise_timeout`, running out of it raises subprocess.TimeoutExpired instead of answering None."""
    deadline = time.monotonic() + timeout

    def git(cwd, *args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(["git", "-C", cwd, *args], timeout)
        return subprocess.run(["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape", timeout=left)

    try:
        return repo_facts(path, git, str.strip)[2]
    except subprocess.TimeoutExpired:
        if raise_timeout:
            raise
        return None
    except (RepoError, OSError, subprocess.SubprocessError):
        return None
