"""Tests for the guard hooks: branch_guard.py (never on main) and tests_first.py (tests
first, proven), plus the glob matcher they share with the bin tools.

Each hook runs as a subprocess of the interpreter running the tests, fed PreToolUse JSON
on stdin exactly as Claude Code sends it, under a temporary HOME, in temporary git repos
made with `git init -b main`, with linked worktrees from `git worktree add` on t/12-...
branches. Nothing here touches ~/.claude. The glob matcher is also called directly, by
loading the hook's source without running its main().

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403


# ---------------------------------------------------------------- glob matcher


class GlobMatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook("tests_first")

    def test_globs_from_the_spec(self):
        match = self.hook.glob_match
        for pattern, path, expected in (
            ("tests/**", "tests/a/b.py", True),
            ("tests/**", "tests/x.py", True),
            ("tests/**", "src/tests/x.py", False),
            ("tests/**", "testsuite/x.py", False),
            ("**/test_*.py", "test_a.py", True),
            ("**/test_*.py", "src/pkg/test_a.py", True),
            ("**/test_*.py", "src/pkg/a_test.py", False),
            ("**/test_*.py", "src/test_a.pyc", False),
            ("*.md", "README.md", True),
            ("*.md", "docs/x.md", True),
            ("*.md", "docs/x.mdx", False),
            ("docs/**", "docs/x.md", True),
            ("docs/**", "docs/a/b/c.py", True),
            ("docs/**", "src/docs/x.md", False),
            ("docs/**", "docsy/x.md", False),
            ("docs/**", "README.md", False),
            ("**/conftest.py", "conftest.py", True),
            ("**/conftest.py", "a/b/conftest.py", True),
            ("src/**/x.py", "src/x.py", True),
            ("src/**/x.py", "src/a/b/x.py", True),
            ("src/*/x.py", "src/a/b/x.py", False),
            ("LICENSE*", "LICENSE", True),
            ("LICENSE*", "vendor/LICENSE.txt", True),
            (".gitignore", ".gitignore", True),
            (".github/**", ".github/workflows/ci.yml", True),
        ):
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(match(pattern, path), expected)

    def test_default_classification(self):
        classify = self.hook.classify
        for path, kind in (
            ("tests/test_app.py", "test"),
            ("tests/fixtures/data.json", "test"),
            ("src/pkg/test_app.py", "test"),
            ("src/pkg/app_test.py", "test"),
            ("web/app.test.ts", "test"),
            ("spec/models/user_spec.rb", "test"),
            ("docs/guide.md", "exempt"),
            ("README.md", "exempt"),
            ("pyproject.toml", "exempt"),
            ("package.json", "exempt"),
            (".github/workflows/ci.yml", "exempt"),
            ("uv.lock", "exempt"),
            ("src/app.py", "source"),
            ("web/app.ts", "source"),
            ("Makefile", "source"),
        ):
            with self.subTest(path=path):
                self.assertEqual(classify(path, {}), kind)


# ---------------------------------------------------------------- branch_guard.py


class BranchGuardEditTests(GuardTestCase):
    def edit(self, path: Path, tool: str = "Edit", cwd: Path | None = None, env=None):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        tool_input = {key: str(path)}
        if tool == "Edit":
            tool_input.update(old_string="a", new_string="b", replace_all=False)
        elif tool == "Write":
            tool_input["content"] = "x = 1\n"
        payload = self.payload(tool, tool_input, cwd or path.parent)
        return self.decision(self.run_hook("branch_guard.py", payload, env))

    def test_denies_edits_on_main_with_a_commit(self):
        repo = self.make_repo()
        expected = f"On main in proj. {ADVICE}"
        self.assertEqual(self.edit(repo / "src/app.py"), expected)
        # a file in directories that do not exist yet: the nearest existing one decides
        self.assertEqual(
            self.edit(repo / "new/deep/mod.py", "Write", cwd=repo), expected
        )
        self.assertEqual(self.edit(repo / "nb.ipynb", "NotebookEdit"), expected)

    def test_allows_agent_notes_on_main(self):
        repo = self.make_repo()
        self.assertIsNone(self.edit(repo / "docs/agent/handoff.md", "Write", cwd=repo))
        self.assertIsNone(
            self.edit(repo / "docs/agent/specs/2026-10-13-x.md", "Write", cwd=repo)
        )
        expected = f"On main in proj. {ADVICE}"
        self.assertEqual(
            self.edit(repo / "docs/design.md", "Write", cwd=repo), expected
        )

    def test_allows_gitignored_files_on_main(self):
        repo = self.make_repo()
        self.write(repo / ".gitignore", ".env\n.claude/settings.local.json\n*.log\n")
        self.git(repo, "add", ".gitignore")
        self.git(repo, "commit", "-q", "-m", "Ignore local files")
        self.assertIsNone(self.edit(repo / ".env", "Write", cwd=repo))
        self.assertIsNone(
            self.edit(repo / ".claude/settings.local.json", "Write", cwd=repo)
        )
        # a tracked file is not ignored, whatever the patterns say
        self.write(repo / "keep.log", "x\n")
        self.git(repo, "add", "-f", "keep.log")
        self.git(repo, "commit", "-q", "-m", "Track a log")
        expected = f"On main in proj. {ADVICE}"
        self.assertEqual(self.edit(repo / "keep.log"), expected)
        self.assertEqual(self.edit(repo / ".gitignore"), expected)

    def test_source_stays_denied_on_main(self):
        repo = self.make_repo()
        self.write(repo / ".gitignore", ".env\n")
        self.assertEqual(
            self.edit(repo / "src/x.py", "Write", cwd=repo),
            f"On main in proj. {ADVICE}",
        )

    def test_denies_on_master_too(self):
        repo = self.make_repo(branch="master")
        self.assertEqual(self.edit(repo / "src/app.py"), f"On master in proj. {ADVICE}")

    def test_allows_on_a_ticket_branch(self):
        repo = self.make_repo()
        self.git(repo, "switch", "-q", "-c", BRANCH)
        self.assertIsNone(self.edit(repo / "src/app.py"))

    def test_allows_in_a_linked_worktree(self):
        repo = self.make_repo()
        wt = self.worktree(repo)
        self.assertIsNone(self.edit(wt / "src/app.py", cwd=repo))
        self.assertIsNone(self.edit(wt / "src/new.py", "Write", cwd=repo))
        # never inside a linked worktree, even one that has main checked out
        self.git(repo, "switch", "-q", "-c", "t/13-other")
        other = self.tmp / "wt-main"
        self.git(repo, "worktree", "add", "-q", str(other), "main")
        self.assertIsNone(self.edit(other / "src/app.py"))

    def test_allows_an_empty_repo_outside_a_repo_and_a_detached_head(self):
        empty = self.make_repo("empty", commit=False)
        self.assertIsNone(self.edit(empty / "first.py", "Write"))
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(self.edit(plain / "x.py", "Write"))
        repo = self.make_repo()
        self.git(repo, "switch", "-q", "--detach", "HEAD")
        self.assertIsNone(self.edit(repo / "src/app.py"))

    def test_harness_allow_main_from_environment_or_project_settings(self):
        repo = self.make_repo()
        target = repo / "src/app.py"
        self.assertIsNone(self.edit(target, env={"HARNESS_ALLOW_MAIN": "1"}))
        settings = repo / ".claude/settings.json"
        self.write(settings, json.dumps({"env": {"HARNESS_ALLOW_MAIN": "1"}}))
        self.assertIsNone(self.edit(target))
        # the environment comes first
        self.assertIsNotNone(self.edit(target, env={"HARNESS_ALLOW_MAIN": "0"}))
        settings.unlink()
        local = repo / ".claude/settings.local.json"
        self.write(local, json.dumps({"env": {"HARNESS_ALLOW_MAIN": 1}}))
        self.assertIsNone(self.edit(target))
        self.write(local, json.dumps({"env": {"HARNESS_ALLOW_MAIN": "0"}}))
        self.assertIsNotNone(self.edit(target))

    def test_bad_input_fails_open(self):
        for stdin in (
            "{not json",
            json.dumps({"tool_name": "Edit"}),
            json.dumps({"tool_name": "Edit", "tool_input": {"file_path": 3}}),
            json.dumps({"tool_name": "Bash", "tool_input": "git commit"}),
        ):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_hook("branch_guard.py", stdin).stdout, "")


class BranchGuardBashTests(GuardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def bash(self, command: str, cwd: Path | None = None, env=None) -> str | None:
        tool_input = {"command": command, "description": "Run it"}
        payload = self.payload("Bash", tool_input, cwd or self.repo)
        return self.decision(self.run_hook("branch_guard.py", payload, env))

    def assertRefusedOnMain(self, reason, part: str, why: str, name="proj") -> None:
        self.assertEqual(
            reason, f'On main in {name}. Refused "{part}": {why}. {ADVICE}'
        )

    def test_denies_commands_that_change_main(self):
        repo = self.repo
        for command, part, why in (
            ("git commit -m x", "git commit -m x", "git commit changes main"),
            (
                "git add -A && git commit -qm x",
                "git commit -qm x",
                "git commit changes main",
            ),
            ("git push", "git push", "git push without a refspec pushes main"),
            (
                "git push -u origin",
                "git push -u origin",
                "git push without a refspec pushes main",
            ),
            (
                "git push origin main",
                "git push origin main",
                "git push main updates main",
            ),
            (
                "git push origin HEAD:main",
                "git push origin HEAD:main",
                "git push HEAD:main updates main",
            ),
            (
                "git push origin refs/heads/main",
                "git push origin refs/heads/main",
                "git push refs/heads/main updates main",
            ),
            (
                "git push origin HEAD",
                "git push origin HEAD",
                "git push HEAD updates main",
            ),
            (
                "git push origin --delete t/3-old",
                "git push origin --delete t/3-old",
                "git push --delete deletes a remote branch",
            ),
            (
                "git push origin :t/3-old",
                "git push origin :t/3-old",
                "a git push refspec starting with : deletes a remote branch",
            ),
            (
                "git merge feature",
                "git merge feature",
                "git merge without --ff-only changes main",
            ),
            (
                "git rebase origin/main",
                "git rebase origin/main",
                "git rebase changes main",
            ),
            (
                "git cherry-pick abc123",
                "git cherry-pick abc123",
                "git cherry-pick changes main",
            ),
            ("git revert HEAD", "git revert HEAD", "git revert changes main"),
            ("git am < patch.mbox", "git am < patch.mbox", "git am changes main"),
            ("git stash pop", "git stash pop", "git stash pop changes files on main"),
            (
                "git stash apply",
                "git stash apply",
                "git stash apply changes files on main",
            ),
            (
                "git reset --hard HEAD~1",
                "git reset --hard HEAD~1",
                "git reset changes main",
            ),
            (
                "git branch -D master",
                "git branch -D master",
                "git branch -D master deletes master",
            ),
            (
                "sed -i '' 's/a/b/' README.md",
                "sed -i '' 's/a/b/' README.md",
                "sed -i edits files in place",
            ),
            (
                "perl -pi -e 's/a/b/' README.md",
                "perl -pi -e 's/a/b/' README.md",
                "perl -i edits files in place",
            ),
            (
                "echo hi > notes.txt",
                "echo hi > notes.txt",
                "it writes to notes.txt through a redirection",
            ),
            (
                f"echo hi >> {repo}/notes.txt",
                f"echo hi >> {repo}/notes.txt",
                f"it writes to {repo}/notes.txt through a redirection",
            ),
            ("ls | tee out.txt", "tee out.txt", "tee changes out.txt inside the repo"),
            (
                f"cp {self.outside}/a.py src/",
                f"cp {self.outside}/a.py src/",
                "cp changes src/ inside the repo",
            ),
            (
                f"mv README.md {self.outside}/",
                f"mv README.md {self.outside}/",
                "mv changes README.md inside the repo",
            ),
            (
                "touch src/new.py",
                "touch src/new.py",
                "touch changes src/new.py inside the repo",
            ),
            ("rm -rf src", "rm -rf src", "rm changes src inside the repo"),
            ("bash -c 'git commit -m x'", "git commit -m x", "git commit changes main"),
            ("echo $(git commit -m x)", "git commit -m x", "git commit changes main"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_the_repo_comes_from_cd_and_git_dash_c(self):
        repo = self.repo
        sub = repo / "src"
        self.assertRefusedOnMain(
            self.bash(f"cd {sub} && git commit -m x", cwd=self.outside),
            "git commit -m x",
            "git commit changes main",
        )
        self.assertRefusedOnMain(
            self.bash(f"git -C {repo} commit -m x", cwd=self.outside),
            f"git -C {repo} commit -m x",
            "git commit changes main",
        )
        self.assertRefusedOnMain(
            self.bash("git -C src commit -m x", cwd=repo),
            "git -C src commit -m x",
            "git commit changes main",
        )

    def test_allows_reading_and_moving_off_main(self):
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff HEAD~1 > /dev/null",
            f"git diff > {self.outside}/x.patch",
            "git fetch origin",
            "git pull --ff-only",
            "git merge --ff-only origin/main",
            "git switch -c t/12-fix-login",
            "git checkout t/12-fix-login",
            "git worktree add .claude/worktrees/t-12 -b t/12-fix origin/main",
            "git push -u origin t/12-fix-login",
            "git push origin HEAD:t/12-fix-login",
            "git stash",
            "git branch -D t/3-old",
            "gh pr list",
            "harness-work start 12",
            "python3 -m pytest -q 2>&1 | tail -20",
            "grep -rn 'a > b' src",
            f"cp README.md {self.outside}/",
            f"rm -f {self.outside}/x",
            "echo done >&2",
            "cat <<'EOF'\nrm -rf src\nEOF",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))

    def test_allows_work_in_a_linked_worktree_and_other_repos(self):
        wt = self.worktree(self.repo)
        for command, cwd in (
            ("git commit -am x && git push", wt),
            ("echo hi > notes.txt && rm README.md", wt),
            (f"cd {wt} && git commit -m x && git push -u origin {BRANCH}", self.repo),
            (f"git -C {wt} commit -m x", self.repo),
            (f"echo hi > {self.outside}/x.txt", self.repo),
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, cwd=cwd))
        other = self.make_repo("other")
        self.git(other, "switch", "-q", "-c", "t/5-thing")
        self.assertIsNone(self.bash(f"git -C {other} commit -m x"))
        # an empty repo on main, and a directory outside any repo
        empty = self.make_repo("empty", commit=False)
        self.assertIsNone(self.bash("git commit -m first", cwd=empty))
        self.assertIsNone(self.bash("echo x > y.txt", cwd=self.outside))

    def test_force_push_is_refused_on_every_branch(self):
        wt = self.worktree(self.repo)
        for command, cwd in (
            ("git push --force", self.repo),
            (f"git push -f origin {BRANCH}", wt),
            (f"git push --force-with-lease origin {BRANCH}", wt),
            (f"git push --force-with-lease=x:y origin {BRANCH}", wt),
            (f"git push origin +{BRANCH}", wt),
            ("git push -uf origin x", self.outside),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.bash(command, cwd=cwd),
                    f'Refused "{command}": a force push rewrites history. {FORCE_ADVICE}',
                )

    def test_harness_allow_main_and_the_verify_command(self):
        self.assertIsNone(self.bash("git commit -m x", env={"HARNESS_ALLOW_MAIN": "1"}))
        self.assertIsNone(
            self.bash("git push --force", env={"HARNESS_ALLOW_MAIN": "1"})
        )
        verify = "make check > build.log 2>&1"
        self.write(
            self.repo / ".claude/settings.json",
            json.dumps({"env": {"CLAUDE_VERIFY_CMD": verify}}),
        )
        self.assertIsNone(self.bash(verify))
        self.assertIsNotNone(self.bash("make lint > build.log"))
        self.write(
            self.repo / ".claude/settings.local.json",
            json.dumps({"env": {"HARNESS_ALLOW_MAIN": "1"}}),
        )
        self.assertIsNone(self.bash("git commit -m x"))


    def test_pushes_naming_main_are_refused_from_a_ticket_branch(self):
        wt = self.worktree(self.repo)
        for command, why in (
            ("git push origin HEAD:main", "git push HEAD:main updates main"),
            (
                f"git push origin {BRANCH}:main",
                f"git push {BRANCH}:main updates main",
            ),
            (
                "git push origin HEAD:refs/heads/master",
                "git push HEAD:refs/heads/master updates master",
            ),
            ("git push origin main", "git push main updates main"),
            ("git push origin :main", "git push :main deletes main"),
            (
                "git push --mirror origin",
                "git push --mirror pushes every branch, main included",
            ),
            (
                "git push --all origin",
                "git push --all pushes every branch, main included",
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.bash(command, cwd=wt),
                    f'Refused "{command}": {why}. {PUSH_ADVICE}',
                )
        for command in (
            f"git push -u origin {BRANCH}",
            "git push origin HEAD",
            "git push",
            "git push origin --delete t/3-old",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, cwd=wt))

    def test_writes_are_judged_by_the_worktree_that_holds_them(self):
        repo = self.repo
        nested = repo / ".claude/worktrees/t-13-nested"
        self.git(repo, "worktree", "add", "-q", "-b", "t/13-nested", str(nested))
        scratch = self.write(self.outside / "scratch.txt", "a\n")
        # from the main checkout, on main: a ticket worktree, or outside the repo
        for command in (
            f"sed -i '' 's/a/b/' {nested}/src/app.py",
            f"echo hi > {nested}/src/app.py",
            f"tee {nested}/notes.txt",
            f"cp {scratch} .claude/worktrees/t-13-nested/src/",
            "cd .claude/worktrees/t-13-nested && echo hi > notes.txt",
            f"sed -i '' 's/a/b/' {scratch}",
            "git diff > ../outside/x.patch",
            "make check | tee ../outside/log",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))
        # from a ticket worktree into the main checkout, which is on main
        wt = self.worktree(repo)
        for command, why in (
            (
                f"echo hi > {repo}/src/app.py",
                f"it writes to {repo}/src/app.py through a redirection",
            ),
            (f"sed -i '' 's/a/b/' {repo}/README.md", "sed -i edits files in place"),
            ("cp src/app.py ../proj/src/", "cp changes ../proj/src/ inside the repo"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command, cwd=wt), command, why)
        self.assertIsNone(self.bash("echo hi > src/app.py", cwd=wt))

    def test_the_directory_follows_the_shell(self):
        refused = ("git commit -m x", "git commit changes main")
        for command in (
            "( cd .. ); git commit -m x",
            f"(cd {self.outside} && ls); git commit -m x",
            "echo $(cd ..); git commit -m x",
            "cd ''; git commit -m x",
            f"cd {self.outside}/missing; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), *refused)
        self.assertRefusedOnMain(
            self.bash('git -C "" commit -m x'),
            'git -C "" commit -m x',
            "git commit changes main",
        )
        # a cd that fails before && stops the chain; one made earlier in it succeeds
        for command in (
            f"cd {self.outside}/missing && git commit -m x",
            "git worktree add .claude/worktrees/t-14 -b t/14-x && "
            "cd .claude/worktrees/t-14 && git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))

    def test_more_ways_to_change_main(self):
        for command, why in (
            ("git --git-dir=.git --work-tree=. commit -m x", "git commit changes main"),
            ("git -c alias.ci=commit ci -m x", "git commit changes main"),
            ("git apply ../x.diff", "git apply changes files on main"),
            ("git rm src/app.py", "git rm changes files on main"),
            ("git restore src/app.py", "git restore changes files on main"),
            (
                "git checkout -- src/app.py",
                "git checkout of paths changes files on main",
            ),
            ("git checkout -B main origin/x", "git checkout -B main resets main"),
            ("git switch -C main HEAD~1", "git switch -C main resets main"),
            (
                "git update-ref refs/heads/main HEAD~1",
                "git update-ref refs/heads/main moves main",
            ),
            ("dd if=/dev/zero of=src/x count=1", "dd changes src/x inside the repo"),
            ("truncate -s 0 src/app.py", "truncate changes src/app.py inside the repo"),
            ("perl -0pi -e 's/a/b/' src/app.py", "perl -i edits files in place"),
            ("awk -i inplace 1 src/app.py", "awk -i inplace edits files in place"),
            (
                "echo 0 > .git/refs/heads/main",
                "it writes to .git/refs/heads/main through a redirection",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)
        for command in (
            "mkdir -p src/new",
            "git symbolic-ref --short HEAD",
            "git apply --check ../x.diff",
            "git restore --staged src/app.py",
            "git checkout -b t/14-x origin/main",
            """python3 -c "print(open('src/app.py').read())\"""",
            "sed -n 1,5p src/app.py",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))


class BranchGuardOtherSpellingTests(GuardTestCase):
    """The main checkout on main, reached through another spelling of its path on a case-insensitive file
    system (macOS): branch_guard decides exactly as for the on-disk spelling. git reports the top-level and
    the absolute git dir in the on-disk spelling but --git-common-dir relative to the spelled directory, so
    a guard that compares the two by realpath misreads the main checkout as a linked worktree; one that
    takes paths relative to the on-disk top-level by realpath finds a spelled path outside the repo."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.write(self.repo / ".gitignore", ".env\n")
        self.git(self.repo, "add", ".gitignore")
        self.git(self.repo, "commit", "-q", "-m", "Ignore .env")
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.nested = self.repo / ".claude/worktrees/t-13-nested"
        self.git(self.repo, "worktree", "add", "-q", "-b", "t/13-nested", str(self.nested))
        spellings = [self.tmp / "PROJ", self.tmp / "Proj", swapped_below(self.repo)]
        lowered = Path(str(self.repo).lower())  # the whole path lowercased, as in the live finding
        if str(lowered) != str(self.repo) and lowered.exists() and os.path.samefile(lowered, self.repo):
            spellings.append(lowered)  # only when the temp root really has upper case to lose
        if not (spellings[0].exists() and os.path.samefile(spellings[0], self.repo)):
            self.skipTest("two spellings of one path need a case-insensitive file system")
        for spelled in spellings:
            self.assertFalse(listed(spelled), spelled)  # each really is another spelling
            self.assertTrue(os.path.samefile(spelled, self.repo), spelled)
        self.spellings = spellings
        self.expected = f"On main in proj. {ADVICE}"

    def edit(self, path, cwd: Path, tool: str = "Edit") -> str | None:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        tool_input = {key: str(path)}
        if tool == "Edit":
            tool_input.update(old_string="a", new_string="b", replace_all=False)
        elif tool == "Write":
            tool_input["content"] = "x = 1\n"
        payload = self.payload(tool, tool_input, cwd)
        return self.decision(self.run_hook("branch_guard.py", payload))

    def bash(self, command: str, cwd: Path) -> str | None:
        payload = self.payload("Bash", {"command": command, "description": "Run it"}, cwd)
        return self.decision(self.run_hook("branch_guard.py", payload))

    def refused(self, part: str, why: str) -> str:
        return f'On main in proj. Refused "{part}": {why}. {ADVICE}'

    # ---- Edit, Write, NotebookEdit

    def test_edits_in_the_main_checkout_are_denied_in_every_spelling(self):
        self.assertEqual(self.edit(self.repo / "src/app.py", self.repo), self.expected)
        for spelled in self.spellings:
            for path, cwd, tool in (
                (spelled / "src/app.py", spelled, "Edit"),
                (spelled / "README.md", self.outside, "Edit"),
                (spelled / "new/deep/mod.py", spelled, "Write"),
                (spelled / "nb.ipynb", spelled, "NotebookEdit"),
                ("src/app.py", spelled, "Edit"),  # relative to a spelled cwd
                (f"../{spelled.name}/README.md", spelled.parent / "outside", "Write"),
            ):
                with self.subTest(spelling=str(spelled), path=str(path), tool=tool):
                    self.assertEqual(self.edit(path, cwd, tool), self.expected)

    def test_edits_under_a_subdirectory_spelled_differently_are_denied(self):
        for top in [self.repo] + self.spellings:
            for path, cwd, tool in (
                (top / "SRC/app.py", top, "Edit"),
                (top / "Src/app.py", top / "SRC", "Edit"),
                (top / "TESTS/new_test.py", self.outside, "Write"),
                (top / "Src/Deep/New.py", top, "Write"),
            ):
                with self.subTest(top=str(top), path=str(path)):
                    self.assertEqual(self.edit(path, cwd, tool), self.expected)

    def test_notes_ignored_files_and_worktrees_stay_allowed_in_every_spelling(self):
        for spelled in self.spellings:
            with self.subTest(spelling=str(spelled)):
                # source is denied in this spelling, while the exceptions keep holding
                self.assertEqual(self.edit(spelled / "src/app.py", spelled), self.expected)
                self.assertIsNone(self.edit(spelled / "docs/agent/handoff.md", spelled, "Write"))
                self.assertIsNone(self.edit(spelled / ".env", spelled, "Write"))
                nested = spelled / ".claude/worktrees/t-13-nested"
                self.assertIsNone(self.edit(nested / "src/app.py", nested))
                self.assertEqual(self.edit(spelled / "docs/design.md", spelled, "Write"), self.expected)

    # ---- Bash

    def test_bash_writes_and_commits_from_a_spelled_cwd_are_refused(self):
        commands = (
            ("echo hi > notes.txt", "it writes to notes.txt through a redirection"),
            ("touch src/new.py", "touch changes src/new.py inside the repo"),
            ("sed -i '' 's/a/b/' README.md", "sed -i edits files in place"),
            ("rm -rf src", "rm changes src inside the repo"),
            ("git commit -m x", "git commit changes main"),
        )
        for command, why in commands:  # the on-disk spelling, as a baseline
            self.assertEqual(self.bash(command, self.repo), self.refused(command, why))
        for spelled in self.spellings:
            for command, why in commands:
                with self.subTest(spelling=str(spelled), command=command):
                    self.assertEqual(self.bash(command, spelled), self.refused(command, why))
            with self.subTest(spelling=str(spelled), cwd="SRC"):
                self.assertEqual(
                    self.bash("touch new.py", spelled / "SRC"),
                    self.refused("touch new.py", "touch changes new.py inside the repo"),
                )

    def test_bash_writes_into_a_spelled_main_checkout_from_elsewhere_are_refused(self):
        wt = self.worktree(self.repo)
        for spelled in self.spellings:
            for command, why, cwd in (
                (
                    f"echo hi > {spelled}/src/app.py",
                    f"it writes to {spelled}/src/app.py through a redirection",
                    self.outside,
                ),
                (f"sed -i '' 's/a/b/' {spelled}/README.md", "sed -i edits files in place", wt),
                (f"touch {spelled}/Src/new.py", f"touch changes {spelled}/Src/new.py inside the repo", wt),
                (f"git -C {spelled} commit -m x", "git commit changes main", self.outside),
            ):
                with self.subTest(spelling=str(spelled), command=command):
                    self.assertEqual(self.bash(command, cwd), self.refused(command, why))
            command = f"cd {spelled}/SRC && echo hi > notes.txt"
            with self.subTest(spelling=str(spelled), command=command):
                self.assertEqual(
                    self.bash(command, self.outside),
                    self.refused("echo hi > notes.txt", "it writes to notes.txt through a redirection"),
                )

    def test_bash_writes_into_ticket_worktrees_stay_allowed_in_every_spelling(self):
        for spelled in self.spellings:
            nested = spelled / ".claude/worktrees/t-13-nested"
            with self.subTest(spelling=str(spelled)):
                # the main checkout itself is refused in this spelling ...
                self.assertEqual(
                    self.bash("echo hi > README.md", spelled),
                    self.refused("echo hi > README.md", "it writes to README.md through a redirection"),
                )
                # ... while its ticket worktrees, and places outside it, are judged on their own
                self.assertIsNone(self.bash(f"echo hi > {nested}/src/app.py", self.outside))
                self.assertIsNone(self.bash(f"sed -i '' 's/a/b/' {nested}/README.md", spelled))
                self.assertIsNone(self.bash("echo hi > src/app.py", nested))
                self.assertIsNone(self.bash(f"git diff > {self.outside}/x.patch", spelled))


class ReadonlyPlannerTests(GuardTestCase):
    """readonly_bash.py now keeps the planner read-only too."""

    def guard(self, command: str, agent: str) -> str | None:
        payload = self.payload("Bash", {"command": command}, self.tmp, agent)
        return self.decision(self.run_hook("readonly_bash.py", payload))

    def test_planner_is_read_only(self):
        for agent in ("harness:planner", "planner"):
            with self.subTest(agent=agent):
                reason = self.guard("echo hi > x.txt", agent)
                self.assertTrue(reason.startswith('Refused "echo hi > x.txt": '))
                self.assertIn("The planner only reads, searches, diffs", reason)
                self.assertIsNone(self.guard("git log --oneline -5", agent))


if __name__ == "__main__":
    unittest.main()
