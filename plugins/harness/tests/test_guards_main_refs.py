"""branch_guard.py on Bash: moving main or origin/main is refused from a ticket branch and from a linked worktree (#268).

harness-red's proof accepts a merge of main (#231), so a main or origin/main the agent moved
itself could pass the branch's own test edit off as main's. The hook refuses each way to move
them, or to forge what they resolve to, wherever the agent works; fetching from origin and
merging origin/main stay allowed. Each family's table also carries the same command on another
ref, so only the refs that matter are refused.

Places (main_refs_fixture in guards_support.py, built once for the class):
  ticket: a main working tree on a ticket branch;
  linked: a linked worktree of that checkout, under its .claude/worktrees/;
  parked: a linked worktree of a checkout parked on main, as the harness keeps it (git commands
          only: a Bash write into its common git dir is already refused there, by rule (a)).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_main_refs.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403

URL = "https://example.invalid/fork.git"


class BranchGuardMainRefsTests(ClassFixtureGuardCase):
    """Rows are (place, command, denied); the first row of each test is a real hook process,
    the rest run in process."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.places = main_refs_fixture(cls.builder)

    def bash(self, command: str, cwd: Path) -> str | None:
        payload = self.payload("Bash", {"command": command, "description": "Run it"}, cwd)
        return self.decision(self.run_hook("branch_guard.py", payload))

    def assertDecisions(self, rows) -> None:
        for place, command, denied in rows:
            with self.subTest(place=place, command=command):
                reason = self.bash(command, self.places[place])
                if denied:
                    self.assertIsNotNone(reason, f"allowed in {place}: {command}")
                else:
                    self.assertIsNone(reason, f"refused in {place}: {command}")

    def test_update_ref_of_main_origin_main_or_a_name_shadowing_them(self):
        self.assertDecisions(
            (
                ("ticket", "git update-ref main HEAD", True),
                ("ticket", "git update-ref refs/heads/main HEAD", True),
                # the reflog message #231 reads, forged
                (
                    "linked",
                    "git update-ref -m 'fetch origin: fast-forward' refs/remotes/origin/main HEAD",
                    True,
                ),
                ("linked", "git update-ref refs/tags/origin/main HEAD", True),
                ("parked", "git update-ref refs/heads/main HEAD", True),
                # the ticket's own branch
                ("ticket", f"git update-ref refs/heads/{MAIN_REFS_BRANCH} HEAD", False),
                ("linked", f"git update-ref refs/heads/{MAIN_REFS_OTHER} HEAD", False),
            )
        )

    def test_fetch_into_main_or_origin_while_fetch_pull_and_merge_of_origin_run(self):
        self.assertDecisions(
            (
                ("ticket", "git fetch . HEAD:refs/remotes/origin/main", True),
                ("ticket", "git fetch --update-head-ok origin", True),
                (
                    "linked",
                    f"git fetch origin {MAIN_REFS_OTHER}:refs/remotes/origin/main",
                    True,
                ),
                ("linked", "git fetch . HEAD:main", True),
                ("parked", "git fetch . +refs/heads/main:refs/remotes/origin/main", True),
                ("ticket", "git fetch origin", False),
                ("ticket", "git fetch", False),
                ("ticket", "git pull origin main", False),
                ("ticket", "git merge origin/main", False),
                ("linked", "git fetch origin", False),
                ("linked", "git pull", False),
                ("linked", "git merge origin/main", False),
                ("parked", "git fetch origin", False),
                ("parked", "git merge origin/main", False),
            )
        )

    def test_branch_and_tag_named_main_or_origin_main(self):
        self.assertDecisions(
            (
                ("ticket", "git branch -f main HEAD", True),
                ("ticket", "git tag main", True),
                ("linked", f"git branch -M {MAIN_REFS_OTHER} main", True),
                ("linked", "git tag origin/main", True),
                ("parked", "git tag main HEAD", True),
                ("ticket", "git branch -f t/1-scratch HEAD", False),
                ("linked", "git tag v1", False),
            )
        )

    def test_remote_and_config_retargeting_origin(self):
        self.assertDecisions(
            (
                ("ticket", f"git remote add origin {URL}", True),
                ("ticket", "git remote rename origin old-origin", True),
                ("ticket", f"git config remote.origin.url {URL}", True),
                ("linked", f"git remote set-url origin {URL}", True),
                ("linked", "git remote rename upstream origin", True),
                (
                    "linked",
                    "git config remote.origin.fetch "
                    f"+refs/heads/{MAIN_REFS_OTHER}:refs/remotes/origin/main",
                    True,
                ),
                ("parked", f"git config remote.origin.url {URL}", True),
                ("ticket", "git remote add upstream https://example.invalid/up.git", False),
                ("linked", "git config user.name Test", False),
            )
        )

    def test_bash_writes_to_ref_files(self):
        common = shlex.quote(str(self.places["common"]))
        self.assertDecisions(
            (
                ("ticket", "echo 0000 > .git/refs/heads/main", True),
                ("ticket", "echo x | tee -a .git/logs/refs/remotes/origin/main", True),
                ("ticket", "mv packed-refs.new .git/packed-refs", True),
                # the linked worktree's common git dir
                (
                    "linked",
                    f"cp {common}/refs/heads/{MAIN_REFS_OTHER} {common}/refs/heads/main",
                    True,
                ),
                ("linked", f"echo x >> {common}/logs/refs/heads/main", True),
                ("ticket", "cat .git/refs/heads/main", False),
                ("ticket", "echo hi > notes.txt", False),
                ("linked", f"cat {common}/refs/remotes/origin/main", False),
            )
        )


if __name__ == "__main__":
    unittest.main()
