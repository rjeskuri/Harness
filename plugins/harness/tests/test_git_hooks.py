"""The git hooks the scaffold installs (templates/pre-commit*, templates/pre-push) and Harness's own
(.githooks/): the deterministic never-on-main layer git runs whatever the command was spelled as.

- pre-commit refuses a commit on main or master, lets a ticket branch commit, lets a repository with
  no commit yet make its first one, and skips the check with HARNESS_ALLOW_MAIN=1.
- pre-push refuses a push to main or master, a non-fast-forward push and a branch deletion, lets a
  fast-forward push of a ticket branch through, and skips every check with HARNESS_ALLOW_MAIN=1.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_git_hooks.py" -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
ROOT = PLUGIN.parent.parent
TEMPLATES = PLUGIN / "templates"
OWN = ROOT / ".githooks"

HOOK_SETS = {
    "template plain": (TEMPLATES / "pre-commit", TEMPLATES / "pre-push"),
    "template python": (TEMPLATES / "pre-commit-python", TEMPLATES / "pre-push"),
    "template node": (TEMPLATES / "pre-commit-node", TEMPLATES / "pre-push"),
    "harness own": (OWN / "pre-commit", OWN / "pre-push"),
}


class GitHookCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-githooks-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@localhost",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@localhost",
            "LANG": "C",
        }
        (self.tmp / "home").mkdir()

    def git(self, repo: Path, *args: str, env: dict | None = None, check: bool = True):
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           env=dict(self.env, **(env or {})), timeout=60)
        if check:
            self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p

    def repo(self, pre_commit: Path, pre_push: Path, first_commit: bool = True, name: str = "proj") -> Path:
        """A repo on main with a bare origin, the two hooks installed through core.hooksPath."""
        origin = self.tmp / f"{name}-origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        repo = self.tmp / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        hooks = repo / ".githooks"
        hooks.mkdir()
        for src, name in ((pre_commit, "pre-commit"), (pre_push, "pre-push")):
            shutil.copy(src, hooks / name)
            os.chmod(hooks / name, 0o755)
        self.git(repo, "config", "core.hooksPath", ".githooks")
        self.git(repo, "remote", "add", "origin", str(origin))
        if first_commit:
            (repo / "README.md").write_text("# proj\n")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-q", "-m", "First")  # no HEAD yet: the hook lets it through
            self.git(repo, "push", "-q", "-u", "origin", "main", env={"HARNESS_ALLOW_MAIN": "1"})
        return repo

    def change(self, repo: Path, name: str = "x.txt") -> None:
        (repo / name).write_text("x\n")
        self.git(repo, "add", "-A")


class PreCommitTests(GitHookCase):
    def test_refuses_main_and_master_lets_a_ticket_branch_commit(self):
        for label, (pre_commit, pre_push) in HOOK_SETS.items():
            with self.subTest(hooks=label):
                repo = self.repo(pre_commit, pre_push, name=label.replace(' ', '-'))
                self.change(repo)
                p = self.git(repo, "commit", "-q", "-m", "on main", check=False)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("pre-commit: refused: on main", p.stderr)
                self.assertIn("harness-work start", p.stderr)
                self.git(repo, "switch", "-q", "-c", "master")
                self.assertEqual(self.git(repo, "commit", "-q", "-m", "x", check=False).returncode, 1)
                self.git(repo, "switch", "-q", "-c", "t/12-widget")
                self.git(repo, "commit", "-q", "-m", "on a ticket branch")
                self.assertEqual(self.git(repo, "log", "--oneline").stdout.count("\n"), 2)

    def test_first_commit_and_harness_allow_main_pass(self):
        pre_commit, pre_push = HOOK_SETS["template plain"]
        repo = self.repo(pre_commit, pre_push, first_commit=False)
        (repo / "README.md").write_text("# proj\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "First")  # no commit yet: allowed
        self.change(repo)
        self.assertEqual(self.git(repo, "commit", "-q", "-m", "x", check=False).returncode, 1)
        self.git(repo, "commit", "-q", "-m", "x", env={"HARNESS_ALLOW_MAIN": "1"})
        self.assertEqual(self.git(repo, "log", "--oneline").stdout.count("\n"), 2)

    def test_project_checks_still_run_after_the_branch_check(self):
        # The python and node templates' own checks come after the branch check: on a ticket branch
        # with no staged .py or .js file they exit 0 before looking for ruff or npm.
        for label in ("template python", "template node"):
            with self.subTest(hooks=label):
                pre_commit, pre_push = HOOK_SETS[label]
                repo = self.repo(pre_commit, pre_push, name=label.replace(' ', '-'))
                self.git(repo, "switch", "-q", "-c", "t/3-x")
                self.change(repo)
                self.git(repo, "commit", "-q", "-m", "x")


class PrePushTests(GitHookCase):
    def test_refuses_main_force_and_delete_lets_a_ticket_branch_through(self):
        for label, (pre_commit, pre_push) in HOOK_SETS.items():
            with self.subTest(hooks=label):
                repo = self.repo(pre_commit, pre_push, name=label.replace(' ', '-'))
                # a commit on main through the bypass, then a plain push of main: refused
                self.change(repo)
                self.git(repo, "commit", "-q", "-m", "x", env={"HARNESS_ALLOW_MAIN": "1"})
                p = self.git(repo, "push", "-q", "origin", "main", check=False)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("pre-push: refused: refs/heads/main changes only through a pull request", p.stderr)
                p = self.git(repo, "push", "-q", "origin", "HEAD:master", check=False)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("refs/heads/master", p.stderr)
                # a ticket branch: a fast-forward push passes, a rewrite does not, a delete does not
                self.git(repo, "switch", "-q", "-c", "t/12-widget")
                self.change(repo, "y.txt")
                self.git(repo, "commit", "-q", "-m", "y")
                self.git(repo, "push", "-q", "-u", "origin", "t/12-widget")
                self.git(repo, "commit", "-q", "--amend", "-m", "y again")
                p = self.git(repo, "push", "-q", "--force", "origin", "t/12-widget", check=False)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("would not fast-forward", p.stderr)
                p = self.git(repo, "push", "-q", "origin", "--delete", "t/12-widget", check=False)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("deleting refs/heads/t/12-widget", p.stderr)
                # the bypass lets each through
                self.git(repo, "push", "-q", "--force", "origin", "t/12-widget", env={"HARNESS_ALLOW_MAIN": "1"})
                self.git(repo, "push", "-q", "origin", "main", env={"HARNESS_ALLOW_MAIN": "1"})

    def test_a_new_branch_and_a_fast_forward_after_a_merge_of_main_pass(self):
        pre_commit, pre_push = HOOK_SETS["harness own"]
        repo = self.repo(pre_commit, pre_push)
        self.git(repo, "switch", "-q", "-c", "t/5-x")
        self.change(repo, "a.txt")
        self.git(repo, "commit", "-q", "-m", "a")
        self.git(repo, "push", "-q", "-u", "origin", "t/5-x")  # remote sha is zero: a new branch
        self.git(repo, "switch", "-q", "main")
        self.change(repo, "m.txt")
        self.git(repo, "commit", "-q", "-m", "m", env={"HARNESS_ALLOW_MAIN": "1"})
        self.git(repo, "switch", "-q", "t/5-x")
        self.git(repo, "merge", "-q", "--no-edit", "main")
        self.git(repo, "push", "-q", "origin", "t/5-x")  # a fast-forward: the hook is silent


if __name__ == "__main__":
    unittest.main()
