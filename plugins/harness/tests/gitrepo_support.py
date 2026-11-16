"""A template git repository built once per test module, and its per-test copies.

Not a test module (no `test_` prefix). A plain `shutil.copytree` of a fresh repository once
failed a CI run (#365): the template's commit had started git's detached background
maintenance, which removed `.git/objects/maintenance.lock` mid-copy. So `make_template` turns
auto gc and auto maintenance off in the repository's own config before its commit (every copy
carries that config), and `copy_repo` skips git's transient `*.lock` files inside `.git`
instead of copying a stale lock or failing on a vanished one. #318 should reuse these.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def make_template(path: Path, env: dict) -> None:
    """A repository on main with one commit "Initial commit" of README.md, git run with env."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("symbolic-ref", "HEAD", "refs/heads/main"),
        ("config", "gc.auto", "0"),
        ("config", "maintenance.auto", "false"),
        ("add", "README.md"),
        ("commit", "-q", "-m", "Initial commit"),
    ):
        if args[0] == "add":
            (path / "README.md").write_text("hello\n")
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, env=env
        )


def copy_repo(src: Path, dst: Path) -> None:
    """Copy a repository with symlinks kept, leaving out every `*.lock` under `.git`."""
    git_dir = os.path.join(os.path.abspath(src), ".git")

    def git_locks(directory: str, names: list) -> set:
        here = os.path.abspath(directory)
        if here != git_dir and not here.startswith(git_dir + os.sep):
            return set()
        return {name for name in names if name.endswith(".lock")}

    shutil.copytree(src, dst, symlinks=True, ignore=git_locks)
