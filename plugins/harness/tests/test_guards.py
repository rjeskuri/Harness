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

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
FORCE_ADVICE = (
    "Force pushes are refused on every branch; push new commits instead, or ask the user "
    "to rewrite history by hand."
)
AUTHOR_ON_SOURCE = (
    "You write tests only on this ticket. Report the source change the implementation "
    "needs; the coder makes it."
)
CODER_ON_TEST = (
    "Tests are fixed for this ticket. If a test is wrong, stop and report it; the "
    "orchestrator sends it back to the test-author."
)
PUSH_ADVICE = (
    "main and master change only through a pull request: push the ticket branch and "
    "open one (harness-pr open <issue#>)."
)
CODER_ON_CONFIG = (
    "{rel} decides which tests run and how, so it is fixed for this ticket like the "
    "tests. If it has to change, stop and ask the orchestrator."
)
BREAKER_ON_TEST = (
    "Tests are fixed for this ticket: find the cheapest source change that passes them, "
    "and if there is none, report that."
)
# git forms that rewrite the working tree wholesale: they touch every test file.
BULK_GIT_FORMS = (
    "git checkout -- .",
    "git checkout .",
    "git checkout HEAD -- .",
    "git checkout -- src",
    "git checkout HEAD src",
    "git restore .",
    "git restore -- .",
    "git restore --source=HEAD --worktree .",
    "git restore --staged --worktree .",
    "git restore src",
    "git reset --hard",
    "git reset --hard HEAD",
    "git reset --merge",
    "git reset --merge ORIG_HEAD",
    "git stash",
    "git stash -u",
    "git stash push",
    "git stash push -m wip",
    "git stash pop",
    "git stash apply",
    "git stash apply stash@{0}",
    "git stash drop",
    "git clean -fd",
    "git clean -fdx",
)
# Writers whose paths cannot be seen: patch reading the files from the diff, find -exec.
# (command, the part the reason quotes)
HIDDEN_WRITER_FORMS = (
    ("patch -p1 < fix.diff", "patch -p1 < fix.diff"),
    ("patch -R -p1 < fix.diff", "patch -R -p1 < fix.diff"),
    ("patch -p0 -i fix.diff", "patch -p0 -i fix.diff"),
    ("patch -d tests -p1 < fix.diff", "patch -d tests -p1 < fix.diff"),
    ("git diff HEAD | patch -p1", "patch -p1"),
    (
        "find . -name '*.py' -exec sed -i '' 's/pass/assert 0/' {} +",
        "find . -name '*.py' -exec sed -i '' 's/pass/assert 0/' {} +",
    ),
    ("find tests -type f -exec rm {} \\;", "find tests -type f -exec rm {} \\;"),
    (
        "find . -name '*.py' -execdir perl -pi -e 's/a/b/' {} +",
        "find . -name '*.py' -execdir perl -pi -e 's/a/b/' {} +",
    ),
    ("find . -type f -exec touch {} +", "find . -type f -exec touch {} +"),
    (
        "find . -name 'test_*.py' -exec cp /dev/null {} \\;",
        "find . -name 'test_*.py' -exec cp /dev/null {} \\;",
    ),
    ("find . -exec mv {} /tmp \\;", "find . -exec mv {} /tmp \\;"),
    ("find . -exec sh -c 'echo > {}' \\;", "find . -exec sh -c 'echo > {}' \\;"),
    (
        "find . -name '*.py' -exec sed --in-place 's/a/b/' {} +",
        "find . -name '*.py' -exec sed --in-place 's/a/b/' {} +",
    ),
    ("find . -exec tee {} \\;", "find . -exec tee {} \\;"),
    # -i / --input names the diff, not a file to change
    ("patch -p1 -i fix.py", "patch -p1 -i fix.py"),
    ("patch -p1 --input=fix.py", "patch -p1 --input=fix.py"),
)
# checkout or restore of any directory is bulk, whatever the directory holds; the
# fixture (commit_directories) makes lib/ (source only), pkg/ (source only) and docs/
# (no tests at all).
DIRECTORY_FORMS = (
    "git checkout -- lib",
    "git checkout HEAD -- lib/",
    "git restore pkg/",
    "git restore --source=HEAD pkg",
    "git checkout -- docs",
    "git restore docs",
)
INLINE = "Write files with Edit or Write so the tests-first rules can see the path."
NO_RED = (
    "Tests first on t/12: write the tests, then run harness-red check. "
    "(No red run recorded.)"
)
NO_CACHE = " There is no ticket cache either; harness-work start 12 writes it."
CHANGED = "Tests changed since the red run; run harness-red check again."
BRANCH = "t/12-fix-login"

_SHARED: dict = {}


def setUpModule() -> None:
    base = Path(tempfile.mkdtemp(prefix="harness-guards-shared-")).resolve()
    _SHARED["base"] = base
    _SHARED["pycache"] = str(base / "pycache")


def tearDownModule() -> None:
    base = _SHARED.pop("base", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


def load_hook(name: str) -> types.ModuleType:
    """Import a hook script by path without running its main() or writing bytecode."""
    path = HOOKS / f"{name}.py"
    module = types.ModuleType(f"harness_hook_{name.replace('-', '_')}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-guards-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "tmp").mkdir()
        # Built from scratch, so nothing leaks in from a Claude Code session running the
        # tests (CLAUDE_PROJECT_DIR, CLAUDE_VERIFY_CMD, HARNESS_*).
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            TMPDIR=str(self.tmp / "tmp"),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            PYTHONPYCACHEPREFIX=_SHARED["pycache"],
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )

    # ---- repos

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return out.stdout.strip()

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_repo(
        self, name: str = "proj", commit: bool = True, branch: str = "main"
    ) -> Path:
        """A repo on `branch` with README.md, src/app.py and tests/test_app.py committed
        (or nothing, with commit=False)."""
        repo = self.tmp / name
        subprocess.run(
            ["git", "init", "-q", "-b", branch, str(repo)],
            check=True,
            capture_output=True,
            env=self.env,
        )
        if commit:
            self.write(repo / "README.md", "hello\n")
            self.write(repo / "src/app.py", "def login():\n    return None\n")
            self.write(repo / "tests/test_app.py", "def test_login():\n    pass\n")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-q", "-m", "Initial commit")
        return repo

    def worktree(self, repo: Path, branch: str = BRANCH, name: str = "wt") -> Path:
        path = self.tmp / name
        self.git(repo, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    # ---- running a hook

    def run_hook(self, name: str, payload, env: dict | None = None):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        r = subprocess.run(
            [sys.executable, str(HOOKS / name)],
            input=stdin,
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            cwd=str(self.tmp),
            timeout=60,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r

    def payload(self, tool: str, tool_input: dict, cwd: Path, agent=None) -> dict:
        data = {
            "session_id": "sess-1",
            "transcript_path": "/dev/null",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_01",
        }
        if agent is not None:  # present only inside a subagent
            data.update(agent_id="agent-1", agent_type=agent)
        return data

    def decision(self, r) -> str | None:
        """The deny reason, or None when the hook allowed the call (printed nothing)."""
        if not r.stdout.strip():
            return None
        out = json.loads(r.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"])
        d = out["hookSpecificOutput"]
        self.assertEqual(
            [d["hookEventName"], d["permissionDecision"]], ["PreToolUse", "deny"]
        )
        return d["permissionDecisionReason"]


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
            (
                """python3 -c "open('src/x','w').write('x')\"""",
                "python opens a file for writing",
            ),
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
        self.assertRefusedOnMain(
            self.bash("python3 - <<'PY'\nopen('src/x', 'w')\nPY"),
            "python3 - <<'PY'",
            "python opens a file for writing",
        )
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


TEMP_ROOT = os.path.realpath(tempfile.gettempdir())


def swapped_below(path: Path, base: str = TEMP_ROOT) -> Path:
    """`path` with the case of every component below `base` swapped (the fixture's own folders too)."""
    rel = os.path.relpath(str(path), base)
    assert not rel.startswith(os.pardir), (path, base)
    return Path(base, *[part.swapcase() for part in rel.split(os.sep)])


def listed(path: Path) -> bool:
    """True when every component of the absolute `path` is spelled as its parent lists it."""
    parent = os.sep
    for part in str(path).strip(os.sep).split(os.sep):
        try:
            if part not in os.listdir(parent):
                return False
        except OSError:
            return False
        parent = os.path.join(parent, part)
    return True


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


# ---------------------------------------------------------------- tests_first.py


class TestsFirstTests(GuardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()
        self.wt = self.worktree(self.repo)
        key = hashlib.sha1(str(self.repo.resolve()).encode()).hexdigest()[:12]
        self.state = self.home / ".claude/harness/repos" / key

    def edit(self, rel: str, agent=None, root: Path | None = None, env=None):
        path = (root or self.wt) / rel
        tool_input = {"file_path": str(path), "old_string": "a", "new_string": "b"}
        payload = self.payload("Edit", tool_input, self.repo, agent)
        return self.decision(self.run_hook("tests_first.py", payload, env))

    def cache(self, **fields) -> None:
        ticket = {
            "number": 12,
            "title": "Fix login",
            "type": "task",
            "status": "in-progress",
            "flags": [],
            "labels": ["task", "status:in-progress"],
            "tier": "opus",
        }
        ticket.update(fields)
        self.write(self.state / "tickets/12.json", json.dumps(ticket))

    def red(self, kind: str = "assertion", files=("tests/test_app.py",), root=None):
        root = root or self.wt
        h = hashlib.sha256()
        for name in sorted(files):
            h.update(name.encode() + b"\n" + (root / name).read_bytes())
        record = {
            "branch": BRANCH,
            "base_sha": "0" * 40,
            "test_files": list(files),
            "test_hash": h.hexdigest(),
            "cmd": "python3 -m unittest tests/test_app.py",
            "exit": 0 if kind == "green" else 1,
            "kind": kind,
            "tail": "AssertionError",
            "at": "2026-10-13T10:00:00Z",
        }
        self.write(self.state / "red/t-12-fix-login.json", json.dumps(record))

    def test_role_rules_for_each_agent_type(self):
        self.cache()
        self.red()  # a current red run: only the role rules can refuse
        for agent, rel, expected in (
            ("harness:test-author", "src/app.py", AUTHOR_ON_SOURCE),
            ("test-author", "src/app.py", AUTHOR_ON_SOURCE),
            ("harness:test-author", "tests/test_app.py", None),
            ("harness:test-author", "docs/notes.md", None),
            ("harness:coder", "tests/test_app.py", CODER_ON_TEST),
            ("someone:else:coder", "tests/test_more.py", CODER_ON_TEST),
            ("harness:coder", "src/app.py", None),
            ("harness:coder", "README.md", None),
            ("harness:test-breaker", "tests/test_app.py", "breaker"),
            ("harness:test-breaker", "src/app.py", None),
            ("harness:reviewer", "src/app.py", None),
            (None, "src/app.py", None),
        ):
            with self.subTest(agent=agent, rel=rel):
                reason = self.edit(rel, agent)
                if expected == "breaker":
                    self.assertIsNotNone(reason)
                    self.assertTrue(reason.startswith("Tests are fixed"), reason)
                else:
                    self.assertEqual(reason, expected)

    def test_role_rules_apply_in_the_breaker_scratch_worktree(self):
        self.red()
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        self.assertIsNotNone(self.edit("tests/test_app.py", breaker, root=scratch))
        self.assertIsNone(self.edit("src/app.py", breaker, root=scratch))
        # elsewhere a detached HEAD is left alone
        other = self.tmp / "detached"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(other), BRANCH)
        self.assertIsNone(self.edit("tests/test_app.py", breaker, root=other))
        self.assertIsNone(self.edit("src/app.py", root=other))

    def test_source_edit_needs_a_red_record(self):
        self.assertEqual(self.edit("src/app.py"), NO_RED + NO_CACHE)
        self.cache()
        self.assertEqual(self.edit("src/app.py"), NO_RED)
        self.assertEqual(self.edit("src/new_module.py", "harness:coder"), NO_RED)

    def test_allows_source_after_a_current_red_record(self):
        self.cache()
        self.red()
        self.assertIsNone(self.edit("src/app.py", "harness:coder"))
        self.assertIsNone(self.edit("src/app.py"))
        # no ticket cache: a task without flags, and the red run is there
        (self.state / "tickets/12.json").unlink()
        self.assertIsNone(self.edit("src/app.py"))

    def test_denies_after_the_test_files_changed(self):
        self.cache()
        self.write(self.wt / "tests/test_more.py", "def test_more():\n    assert 0\n")
        self.red(files=("tests/test_app.py", "tests/test_more.py"))
        self.assertIsNone(self.edit("src/app.py"))
        self.write(self.wt / "tests/test_more.py", "def test_more():\n    pass\n")
        self.assertEqual(self.edit("src/app.py"), CHANGED)
        (self.wt / "tests/test_more.py").unlink()
        self.assertEqual(self.edit("src/app.py"), CHANGED)

    def test_green_record_is_not_proof_for_a_task(self):
        self.cache()
        self.red("green")
        reason = self.edit("src/app.py")
        self.assertIsNotNone(reason)
        self.assertIn("passed before any source change", reason)

    def test_no_tests_flag_exempts_the_ticket(self):
        self.cache(flags=["no-tests"], type="chore")
        self.assertIsNone(self.edit("src/app.py"))
        self.assertIsNone(self.edit("src/app.py", "harness:coder"))
        # the role rules still hold
        self.assertEqual(
            self.edit("src/app.py", "harness:test-author"), AUTHOR_ON_SOURCE
        )

    def test_refactor_proceeds_with_a_green_record(self):
        self.cache(type="refactor")
        self.assertEqual(self.edit("src/app.py"), NO_RED)
        self.red("green")
        self.assertIsNone(self.edit("src/app.py"))
        self.write(self.wt / "tests/test_app.py", "changed\n")
        self.assertIsNone(self.edit("src/app.py"))  # any record will do

    def test_test_and_exempt_paths_pass_without_a_red_record(self):
        for rel in (
            "tests/test_app.py",
            "tests/helpers/data.json",
            "src/pkg/test_thing.py",
            "conftest.py",
            "docs/design.md",
            "README.md",
            "pyproject.toml",
            ".github/workflows/ci.yml",
            ".claude/settings.json",
            "LICENSE",
        ):
            with self.subTest(rel=rel):
                self.assertIsNone(self.edit(rel))

    def test_patterns_from_environment_and_project_settings(self):
        self.cache()
        settings = {"env": {"HARNESS_TEST_PATTERNS": "checks/**"}}
        # the worktree's own settings are the agent's to edit, so they do not count
        self.write(self.wt / ".claude/settings.json", json.dumps(settings))
        self.assertEqual(self.edit("checks/check_app.py"), NO_RED)
        self.write(self.repo / ".claude/settings.json", json.dumps(settings))
        self.assertIsNone(self.edit("checks/check_app.py"))
        self.assertEqual(self.edit("tests/test_app.py"), NO_RED)  # now source
        env = {"HARNESS_TEST_PATTERNS": "tests/**", "HARNESS_SOURCE_EXEMPT": "src/**"}
        self.assertIsNone(self.edit("src/app.py", env=env))
        self.assertEqual(self.edit("checks/check_app.py", env=env), NO_RED)

    def test_patterns_ignore_settings_local_json(self):
        self.cache()
        local = {"env": {"HARNESS_TEST_PATTERNS": "nothing/**"}}
        self.write(self.repo / ".claude/settings.local.json", json.dumps(local))
        self.write(self.wt / ".claude/settings.local.json", json.dumps(local))
        self.assertIsNone(self.edit("tests/test_app.py"))  # still a test
        self.assertEqual(self.edit("tests/test_app.py", "harness:coder"), CODER_ON_TEST)
        tracked = {"env": {"HARNESS_TEST_PATTERNS": "checks/**"}}
        self.write(self.repo / ".claude/settings.json", json.dumps(tracked))
        self.assertIsNone(self.edit("checks/check_app.py"))
        self.assertEqual(self.edit("tests/test_app.py"), NO_RED)  # now source

    def test_test_configuration_is_fixed_for_the_coder(self):
        self.cache()
        self.red()
        for rel in (
            ".claude/settings.json",
            "pyproject.toml",
            "pytest.ini",
            "package.json",
        ):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.edit(rel, "harness:coder"), CODER_ON_CONFIG.format(rel=rel)
                )
                self.assertTrue(
                    self.edit(rel, "harness:test-breaker").startswith("Tests are fixed")
                )
                self.assertIsNone(self.edit(rel, "harness:test-author"))
                self.assertIsNone(self.edit(rel))

    def test_a_red_record_needs_a_failure_kind(self):
        self.cache()
        self.red()
        record_path = self.state / "red/t-12-fix-login.json"
        record = json.loads(record_path.read_text())
        for kind in (None, "", "passed"):
            with self.subTest(kind=kind):
                if kind is None:
                    record.pop("kind", None)
                else:
                    record["kind"] = kind
                self.write(record_path, json.dumps(record))
                reason = self.edit("src/app.py", "harness:coder")
                self.assertIsNotNone(reason)
                self.assertIn("does not say how the tests failed", reason)
        for kind in ("assertion", "import", "error"):
            with self.subTest(kind=kind):
                record["kind"] = kind
                self.write(record_path, json.dumps(record))
                self.assertIsNone(self.edit("src/app.py", "harness:coder"))

    def bash(self, command: str, agent=None, cwd: Path | None = None):
        payload = self.payload("Bash", {"command": command}, cwd or self.wt, agent)
        return self.decision(self.run_hook("tests_first.py", payload))

    def test_bash_writes_follow_the_same_rules(self):
        self.cache()
        self.red()
        outside = self.tmp / "outside"
        outside.mkdir()
        source = self.write(outside / "x.py", "x = 1\n")
        wt = self.wt
        for command in (
            "sed -i '' 's/pass/assert 0/' tests/test_app.py",
            "cat > tests/test_app.py <<'EOF'\ndef test_login():\n    pass\nEOF",
            f"cp {source} tests/test_app.py",
            f"cp {source} tests/",
            "git checkout -- tests/test_app.py",
            "echo x | tee -a tests/test_more.py",
        ):
            with self.subTest(command=command):
                part = command.split("\n", 1)[0].split(" | ")[-1]
                self.assertEqual(
                    self.bash(command, "harness:coder"),
                    f'Refused "{part}": {CODER_ON_TEST}',
                )
                self.assertIsNone(self.bash(command, "harness:test-author"))
        # by path from the main checkout, and through cd
        for command, part in (
            (
                f"sed -i '' 's/a/b/' {wt}/tests/test_app.py",
                f"sed -i '' 's/a/b/' {wt}/tests/test_app.py",
            ),
            (f"cd {wt} && echo x > tests/test_x.py", "echo x > tests/test_x.py"),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.bash(command, "harness:coder", cwd=self.repo),
                    f'Refused "{part}": {CODER_ON_TEST}',
                )
        self.assertEqual(
            self.bash("echo x > src/app.py", "harness:test-author"),
            f'Refused "echo x > src/app.py": {AUTHOR_ON_SOURCE}',
        )
        self.assertIsNone(self.bash("echo x > src/app.py", "harness:coder"))
        run = f"python3 -m pytest -q > {outside}/log 2>&1"
        self.assertIsNone(self.bash(run, "harness:coder"))
        # a source write still needs the red run
        (self.state / "red/t-12-fix-login.json").unlink()
        self.assertEqual(
            self.bash("echo x > src/app.py"), f'Refused "echo x > src/app.py": {NO_RED}'
        )

    def test_bash_may_write_what_git_ignores(self):
        self.write(
            self.wt / ".gitignore", ".pytest_cache/\n*.log\n.claude/settings.local.json\n"
        )
        (self.wt / ".pytest_cache").mkdir()
        author = "harness:test-author"
        self.assertIsNone(self.bash("rm -rf .pytest_cache", author))
        self.assertIsNone(self.bash("python3 -m pytest 2>&1 | tee run.log", author))
        self.assertEqual(
            self.bash("rm -rf build", author), f'Refused "rm -rf build": {AUTHOR_ON_SOURCE}'
        )
        # test configuration stays fixed for the coder, ignored or not
        rel = ".claude/settings.local.json"
        self.assertEqual(
            self.bash(f"echo '{{}}' > {rel}", "harness:coder"),
            f'Refused "echo \'{{}}\' > {rel}": ' + CODER_ON_CONFIG.format(rel=rel),
        )

    def test_inline_programs_that_write_are_refused_to_the_roles(self):
        self.cache()
        self.red()
        command = """python3 -c "open('tests/test_app.py', 'w').write('')\""""
        for agent in ("harness:coder", "harness:test-author", "harness:test-breaker"):
            with self.subTest(agent=agent):
                self.assertEqual(
                    self.bash(command, agent),
                    f'Refused "{command}": python opens a file for writing. {INLINE}',
                )
        heredoc = "node <<'JS'\nrequire('fs').writeFileSync('src/app.py', '')\nJS"
        reason = self.bash(heredoc, "harness:coder")
        self.assertIsNotNone(reason)
        self.assertTrue(reason.endswith(INLINE), reason)
        self.assertIsNone(self.bash(command))  # the main session
        self.assertIsNone(self.bash(command, "harness:reviewer"))
        reads = """python3 -c "print(open('src/app.py').read())\""""
        self.assertIsNone(self.bash(reads, "harness:coder"))
        self.assertIsNone(self.bash(command, "harness:coder", cwd=self.tmp))  # no repo

    # ---- forms that touch every test file, and writers whose paths cannot be seen

    def commit_test_under_src(self) -> None:
        """A test file inside src/ on the ticket branch, so a checkout of the src
        directory reaches a test however the directory is read."""
        self.write(self.wt / "src/pkg/test_thing.py", "def test_thing():\n    pass\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add a test under src")

    def assertRefusedAs(self, reason, part: str, why: str) -> None:
        self.assertIsNotNone(reason, f"{part!r} was allowed")
        self.assertTrue(reason.startswith(f'Refused "{part}": '), reason)
        self.assertIn(why, reason)

    def test_bulk_git_forms_are_refused_to_the_coder_and_breaker(self):
        self.commit_test_under_src()
        self.cache()
        self.red()  # a current red run: only the role rules can refuse
        for command in BULK_GIT_FORMS:
            with self.subTest(command=command, agent="harness:coder"):
                self.assertRefusedAs(
                    self.bash(command, "harness:coder"), command, CODER_ON_TEST
                )
            with self.subTest(command=command, agent="harness:test-breaker"):
                self.assertRefusedAs(
                    self.bash(command, "harness:test-breaker"), command, BREAKER_ON_TEST
                )

    def test_bulk_git_forms_are_allowed_to_the_test_author(self):
        self.commit_test_under_src()
        self.cache()
        self.red()
        # a directory that holds source stays refused to the author, as it is today
        source_dirs = [c for c in BULK_GIT_FORMS if c.endswith(" src")]
        self.assertEqual(len(source_dirs), 3)
        for agent in ("harness:test-author", "test-author"):
            for command in BULK_GIT_FORMS:
                with self.subTest(command=command, agent=agent):
                    reason = self.bash(command, agent)
                    if command in source_dirs:
                        self.assertEqual(
                            reason, f'Refused "{command}": {AUTHOR_ON_SOURCE}'
                        )
                    else:
                        self.assertIsNone(reason)

    def test_bulk_git_forms_follow_the_directory(self):
        self.commit_test_under_src()
        self.cache()
        self.red()
        wt = self.wt
        for cwd, command, part in (
            (wt, "cd src && git checkout -- .", "git checkout -- ."),
            (wt, "cd src && git restore .", "git restore ."),
            (self.repo, f"git -C {wt} checkout -- .", f"git -C {wt} checkout -- ."),
            (self.repo, f"git -C {wt} reset --hard", f"git -C {wt} reset --hard"),
            (self.repo, f"cd {wt} && git stash", "git stash"),
            (self.repo, f"(cd {wt} && git clean -fd)", "git clean -fd"),
            (self.repo, f"cd {wt} && patch -p1 < fix.diff", "patch -p1 < fix.diff"),
        ):
            with self.subTest(command=command):
                why = INLINE if "patch" in command else CODER_ON_TEST
                self.assertRefusedAs(self.bash(command, "harness:coder", cwd), part, why)

    def test_bulk_git_forms_are_refused_in_the_breaker_scratch_worktree(self):
        self.red()
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        breaker = "harness:test-breaker"
        for command in (
            "git checkout -- .",
            "git reset --hard",
            "git stash",
            "git clean -fd",
        ):
            with self.subTest(command=command):
                self.assertRefusedAs(
                    self.bash(command, breaker, cwd=scratch), command, BREAKER_ON_TEST
                )

    def commit_directories(self) -> None:
        """lib/ and pkg/ with source only, docs/ with no tests, on the ticket branch."""
        self.write(self.wt / "lib/util.py", "def util():\n    return 1\n")
        self.write(self.wt / "pkg/mod.py", "def mod():\n    return 2\n")
        self.write(self.wt / "docs/notes.md", "notes\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Add directories")

    def test_any_directory_checkout_is_bulk(self):
        self.commit_directories()
        self.cache()
        self.red()
        for command in DIRECTORY_FORMS:
            with self.subTest(command=command, agent="harness:coder"):
                self.assertRefusedAs(
                    self.bash(command, "harness:coder"), command, CODER_ON_TEST
                )
            with self.subTest(command=command, agent="harness:test-breaker"):
                self.assertRefusedAs(
                    self.bash(command, "harness:test-breaker"), command, BREAKER_ON_TEST
                )
        part = "git checkout -- ."
        self.assertRefusedAs(
            self.bash(f"cd lib && {part}", "harness:coder"), part, CODER_ON_TEST
        )

    def other_ticket_worktree(self, name: str, files: dict) -> Path:
        """A repo `name` holding `files`, its t/12 worktree, and a no-tests ticket cache
        so that only the role rules can refuse there."""
        repo = self.make_repo(name, commit=False)
        for rel, text in files.items():
            self.write(repo / rel, text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        wt = self.worktree(repo, name=f"{name}-wt")
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        ticket = {"number": 12, "type": "chore", "flags": ["no-tests"]}
        self.write(
            self.home / ".claude/harness/repos" / key / "tickets/12.json",
            json.dumps(ticket),
        )
        return wt

    def test_bulk_forms_reach_tests_wherever_they_live(self):
        source = "def login():\n    return None\n"
        test = "def test_login():\n    pass\n"
        # no tests/ directory: the only test is lib/test_other.py
        elsewhere = self.other_ticket_worktree(
            "elsewhere",
            {"README.md": "hi\n", "src/app.py": source, "lib/test_other.py": test},
        )
        # tests are checks/** by pattern; tests/test_app.py is plain source here
        patterned = self.other_ticket_worktree(
            "patterned",
            {
                "README.md": "hi\n",
                "src/app.py": source,
                "tests/test_app.py": source,
                "checks/check_app.py": test,
            },
        )
        env = {"HARNESS_TEST_PATTERNS": "checks/**"}
        # the pattern took effect: tests/test_app.py is the coder's to edit there
        edit = {"file_path": str(patterned / "tests/test_app.py")}
        payload = self.payload("Edit", edit, patterned, "harness:coder")
        self.assertIsNone(self.decision(self.run_hook("tests_first.py", payload, env)))
        for wt, run_env in ((elsewhere, {}), (patterned, env)):
            for command in (
                "git stash",
                "git stash pop",
                "git reset --hard",
                "git checkout -- .",
                "git restore .",
                "git clean -fd",
            ):
                with self.subTest(repo=wt.name, command=command):
                    payload = self.payload(
                        "Bash", {"command": command}, wt, "harness:coder"
                    )
                    reason = self.decision(
                        self.run_hook("tests_first.py", payload, run_env)
                    )
                    self.assertRefusedAs(reason, command, CODER_ON_TEST)

    def test_named_paths_are_judged_as_edits_of_them(self):
        self.cache()
        self.red()
        refused = "refused"
        for command, coder in (
            ("git stash push -- src/app.py", None),
            ("git clean -f src/app.py", None),
            ("patch -p1 README.md < fix.diff", None),
            ("git stash push -- tests/test_app.py", refused),
            ("git clean -f tests/test_new.py", refused),
            ("patch -p1 tests/fixtures/data.txt < fix.diff", refused),
        ):
            with self.subTest(command=command):
                reason = self.bash(command, "harness:coder")
                if coder is None:
                    self.assertIsNone(reason)
                else:
                    self.assertRefusedAs(reason, command, CODER_ON_TEST)
        for command in (
            "patch -p1 README.md < fix.diff",
            "patch -p1 tests/fixtures/data.txt < fix.diff",
        ):
            with self.subTest(command=command, agent="harness:test-author"):
                self.assertIsNone(self.bash(command, "harness:test-author"))

    def test_hidden_writers_are_refused_to_the_roles(self):
        self.cache()
        self.red()
        for command, part in HIDDEN_WRITER_FORMS:
            for agent in ("harness:coder", "harness:test-author", "harness:test-breaker"):
                with self.subTest(command=command, agent=agent):
                    reason = self.bash(command, agent)
                    self.assertRefusedAs(reason, part, INLINE)
                    self.assertTrue(reason.endswith(INLINE), reason)
            # the main session, other agents and places outside a repo are left alone
            with self.subTest(command=command, agent=None):
                self.assertIsNone(self.bash(command))
                self.assertIsNone(self.bash(command, "harness:reviewer"))
                self.assertIsNone(self.bash(command, "harness:coder", cwd=self.tmp))

    def test_an_unresolvable_directory_judges_the_cwd(self):
        self.cache()
        self.red()
        # the directory is not known until the shell runs it: the worktree is judged
        for command in (
            'patch -d "$(pwd)" -p1 < fix.diff',
            "patch -d $D -p1 < fix.diff",
            "patch -d ${UNSET_DIR}/sub -p1 < fix.diff",
            "find $D -type f -exec rm {} \\;",
            # one directory resolves outside any repo, another is unknown
            "find /tmp $D -exec rm {} \\;",
            "find $D /tmp -exec rm {} \\;",
        ):
            for agent in ("harness:coder", "harness:test-author", "harness:test-breaker"):
                with self.subTest(command=command, agent=agent):
                    reason = self.bash(command, agent)
                    self.assertRefusedAs(reason, command, INLINE)
                    self.assertTrue(reason.endswith(INLINE), reason)
            with self.subTest(command=command, agent=None):
                self.assertIsNone(self.bash(command))
            with self.subTest(command=command, where="outside any repo"):
                self.assertIsNone(self.bash(command, "harness:coder", cwd=self.tmp))

    def test_a_directory_outside_the_repo_does_not_judge_the_cwd(self):
        self.cache()
        self.red()
        outside = self.tmp / "outside_dir"
        outside.mkdir()
        # every directory it writes in resolved outside any repo: the worktree it
        # runs from is not judged
        for command in (
            "find /tmp -exec rm {} \\;",
            f"find {outside} -type f -exec touch {{}} +",
            f"find {outside} /tmp -name '*.py' -exec rm {{}} \\;",
            f"patch -d {outside} -p1 < fix.diff",
            "patch -d /tmp -p1 < fix.diff",
        ):
            for agent in ("harness:coder", "harness:test-author", "harness:test-breaker"):
                with self.subTest(command=command, agent=agent):
                    self.assertIsNone(self.bash(command, agent))
        # the same writers aimed into the worktree are still refused
        for command in (
            f"find /tmp {self.wt} -exec rm {{}} \\;",
            f"patch -d {self.wt} -p1 < fix.diff",
        ):
            with self.subTest(command=command, where="in the worktree"):
                self.assertRefusedAs(self.bash(command, "harness:coder"), command, INLINE)

    def test_reads_and_narrow_forms_stay_allowed(self):
        self.cache()
        self.red()
        for command in (
            "git stash list",
            "git stash show",
            "git stash show -p stash@{0}",
            "git checkout -- src/app.py",
            "git restore src/app.py",
            "git restore --staged .",
            "git checkout -b spike",
            "git reset --soft HEAD",
            "git reset -q HEAD -- tests/test_app.py",
            "git clean -n",
            "git clean --dry-run",
            "git clean -n -d",
            "find . -name '*.py'",
            "find . -name 'x.i' -exec sed -n 1p {} \\;",
            "find tests -type f -print",
            "find . -name '*.py' -exec grep -l login {} +",
            "find . -exec sed -n 1p {} \\;",
            "find . -name '*.py' -exec wc -l {} +",
            "patch --dry-run -p1 < fix.diff",
            "patch -p1 src/app.py < fix.diff",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, "harness:coder"))

    def test_bulk_and_hidden_forms_are_left_alone_off_a_ticket_branch(self):
        self.commit_test_under_src()
        self.commit_directories()
        commands = (
            list(BULK_GIT_FORMS)
            + list(DIRECTORY_FORMS)
            + [c for c, _ in HIDDEN_WRITER_FORMS]
        )
        roles = ("harness:coder", "harness:test-breaker", "harness:test-author")
        for command in commands:  # the main checkout, on main
            for agent in roles:
                with self.subTest(command=command, agent=agent, where="main"):
                    self.assertIsNone(self.bash(command, agent, cwd=self.repo))
        self.git(self.wt, "switch", "-q", "-c", "feature/login")
        for command in commands:
            with self.subTest(command=command, where="feature/login"):
                self.assertIsNone(self.bash(command, "harness:coder"))

    def test_other_branches_and_places_are_left_alone(self):
        self.assertIsNone(self.edit("src/app.py", root=self.repo))  # main
        self.git(self.wt, "switch", "-q", "-c", "feature/login")
        self.assertIsNone(self.edit("src/app.py"))
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(self.edit("app.py", root=plain))

    def test_ticket_branch_in_the_main_checkout_shares_the_state(self):
        repo = self.make_repo("solo")
        self.git(repo, "switch", "-q", "-c", "t/12-solo")
        key = hashlib.sha1(str(repo).encode()).hexdigest()[:12]
        state = self.home / ".claude/harness/repos" / key
        self.assertEqual(self.edit("src/app.py", root=repo), NO_RED + NO_CACHE)
        h = hashlib.sha256(
            b"tests/test_app.py\n" + (repo / "tests/test_app.py").read_bytes()
        )
        record = {
            "kind": "assertion",
            "test_files": ["tests/test_app.py"],
            "test_hash": h.hexdigest(),
        }
        self.write(state / "red/t-12-solo.json", json.dumps(record))
        self.assertIsNone(self.edit("src/app.py", root=repo))

    def test_bad_input_fails_open(self):
        for stdin in (
            "{not json",
            json.dumps({"tool_name": "Write"}),
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": ["x"]}}),
            json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        ):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_hook("tests_first.py", stdin).stdout, "")


# ---------------------------------------------------------------- a merge of main (#231)

RED_TOOL = HOOKS.parent / "bin" / "harness-red"
# The test command harness-red runs: it fails as an assertion would, whatever the files.
FAILING_RUN = "import sys; print('AssertionError: login is not built yet'); sys.exit(1)"
# Ten small tests, three lines apart, so edits to the first, the middle and the last
# are separate hunks that git merges cleanly.
BASE_TESTS = "\n".join(
    f"def test_case_{i}():\n    assert {i} + {i} == {2 * i}\n" for i in range(10)
)
MERGE_TEST_FILES = ("tests/test_app.py", "tests/test_util.py")
# The branch's own test change, in the middle of each file.
MAIN_LINE_5 = "    assert 5 + 5 == 10\n"
BRANCH_LINE_5 = "    assert login() == 'ok'\n"


MAIN_LINE_5_REWRITTEN = "    assert 5 * 2 == 10\n"  # main's conflicting rewrite
RED_KINDS = ("assertion", "import", "error")


def replaced(text: str, old: str, new: str) -> str:
    """`text` with the one occurrence of `old` replaced; fails loudly when it is absent."""
    assert text.count(old) == 1, (old, text)
    return text.replace(old, new)


def swapped(text: str, a: str, b: str) -> str:
    """`text` with the one occurrence each of `a` and `b` swapped: same lines, new order."""
    marker = "\0swap\0"
    return replaced(replaced(replaced(text, a, marker), b, a), marker, b)


def case_line(i: int) -> str:
    """The assert line of test_case_i as main first wrote it."""
    return f"    assert {i} + {i} == {2 * i}\n"


def moved_to_end(text: str, block: str) -> str:
    """`text` with `block` (a test and its blank separator) moved after the last test:
    the same lines, in another order."""
    return replaced(text, block + "\n", "") + "\n" + block


def line_moved_into(text: str, line: str, anchor: str) -> str:
    """`text` with `line` taken out of its test and put after `anchor`, in another one."""
    return replaced(replaced(text, line, ""), anchor, anchor + line)


# Repeated context: ten identical top-level tests with PEP 8's two blank lines between
# them, so every gap between tests has the same surroundings, however many lines of
# context a diff shows; only the position tells the gaps apart. The branch's own change
# is a whole new test inserted into gap 5.
SAME_TEST = "def test_case():\n    assert ok()"
LOGIN_TEST = "def test_login():\n    assert login() == 'ok'"
MAIN_LOGIN_TEST = "def test_login_main():\n    assert login() is not None"


def pep8(tests) -> str:
    return "\n\n\n".join(tests) + "\n"


def pep8_tests(text: str) -> list:
    assert text.endswith("\n") and not text.endswith("\n\n"), text
    return text[:-1].split("\n\n\n")


def put_at(text: str, gap: int, test: str) -> str:
    """`text` with `test` inserted as the gap-th top-level item (0: first)."""
    tests = pep8_tests(text)
    tests.insert(gap, test)
    return pep8(tests)


def moved_to_gap(text: str, test: str, gap: int) -> str:
    """`text` with its one `test` moved, unchanged, to another gap."""
    tests = pep8_tests(text)
    assert tests.count(test) == 1, text
    tests.remove(test)
    tests.insert(gap, test)
    return pep8(tests)


def with_test(text: str, index: int, test: str) -> str:
    """`text` with its index-th top-level item replaced by `test`."""
    tests = pep8_tests(text)
    tests[index] = test
    return pep8(tests)


# A unique first item (the imports) and last test bound the repeated run: main's clean
# edits go there, where git's alignment is unambiguous (an edit inside the identical
# run can be aligned to another copy, and the merge then moves the branch's test).
# Items: 0 the imports, 1-10 the repeated tests, 11 the last test; the branch's test
# goes in as item 6, after the fifth repeated test.
REPEATED_TESTS = pep8(["import pytest", *[SAME_TEST] * 10, "def test_last():\n    assert last()"])
LOGIN_GAP = 6


# Blank-line runs (#231 review): main trims the blank lines after the imports, as black
# does, and appends a test; the branch adds a line after the first blank line of
# test_login's body. git merges it cleanly (ort, and merge-file's Myers alike), but
# taking main's change back out of the merged file aligns the runs of blank lines
# differently and lands the branch's line in the wrong place.
TRIM_TESTS = (
    "import pytest\n\n\n\n\ndef test_login():\n\n\n    assert True\n\n\n"
    "def test_other():\n    assert True\n"
)
TRIM_ANCHOR = "def test_login():\n\n"


def trim_author(text: str) -> str:
    return replaced(text, TRIM_ANCHOR, TRIM_ANCHOR + BRANCH_LINE_5)


def trim_main(text: str) -> str:
    return (
        replaced(text, "import pytest\n\n\n\n\n", "import pytest\n\n\n")
        + "\n\ndef test_more():\n    assert True\n"
    )


def content_hash(root: Path, files) -> str:
    """The red record's test_hash, from first principles: sha256 over each file's path,
    a newline and its bytes, in sorted path order."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\n" + (root / name).read_bytes())
    return h.hexdigest()


class TestsFirstMainMergeTests(GuardTestCase):
    """A red record survives a merge of main that edits the branch's test files in other
    hunks; a test change the branch makes itself (an edit, a commit, or a merge
    resolution of its own test lines) still invalidates it.

    Each scenario is real git: main with two test files, the ticket branch in a linked
    worktree, the test-author's commit editing the middle of both, the red record written
    by harness-red check itself, the coder's source commit, then commits on main and
    `git merge main` in the worktree."""

    def setUp(self) -> None:
        super().setUp()
        self.env["HARNESS_TEST_CMD"] = (
            shlex.quote(sys.executable) + " -c " + shlex.quote(FAILING_RUN) + " {files}"
        )

    # ---- the scenario

    def commit_all(self, root: Path, message: str) -> None:
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", message)

    def harness_red(self, wt: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(RED_TOOL), *args],
            cwd=str(wt),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )

    def ticket(self, name: str, base: str = BASE_TESTS, author=None) -> tuple[Path, Path]:
        """(main checkout, ticket worktree) after the red run and the coder's commit.
        Main's test files hold `base`; the test-author applies `author` to each (default:
        test_case_5's assert becomes BRANCH_LINE_5)."""
        if author is None:
            author = lambda t: replaced(t, MAIN_LINE_5, BRANCH_LINE_5)  # noqa: E731
        repo = self.make_repo(name)
        for rel in MERGE_TEST_FILES:
            self.write(repo / rel, base)
        self.commit_all(repo, "the tests so far")
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        ticket = {"number": 12, "title": "Fix login", "type": "task", "flags": []}
        ticket["labels"] = ["task", "status:in-progress"]
        self.write(
            self.home / ".claude/harness/repos" / key / "tickets/12.json",
            json.dumps(ticket),
        )
        wt = self.worktree(repo, name=f"{name}-wt")
        for rel in MERGE_TEST_FILES:  # the test-author
            path = wt / rel
            self.write(path, author(path.read_text()))
        self.commit_all(wt, "tests for login")
        r = self.harness_red(wt, "check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("red (assertion)", r.stdout)
        self.assertIsNone(self.edit(wt, "src/app.py"))  # the record is current
        self.write(wt / "src/app.py", "def login():\n    return 'ok'\n")  # the coder
        self.commit_all(wt, "login")
        self.assertIsNone(self.edit(wt, "src/app.py", "harness:coder"))
        return repo, wt

    def on_main(self, repo: Path, changes: dict, message: str = "main moves on") -> None:
        """Commit on main: each path gets a function of its old text (None when it is
        new) or a literal text."""
        for rel, change in changes.items():
            path = repo / rel
            old = path.read_text() if path.exists() else None
            self.write(path, change(old) if callable(change) else change)
        self.commit_all(repo, message)

    def merge_main(self, wt: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(wt), "merge", "--no-edit", "main"],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def merge_cleanly(self, wt: Path) -> None:
        r = self.merge_main(wt)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        parents = self.git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3, "a merge commit, not a fast-forward")

    def edit(self, wt: Path, rel: str, agent=None):
        tool_input = {"file_path": str(wt / rel), "old_string": "a", "new_string": "b"}
        payload = self.payload("Edit", tool_input, wt, agent)
        return self.decision(self.run_hook("tests_first.py", payload))

    def bash(self, wt: Path, command: str, agent=None):
        payload = self.payload("Bash", {"command": command}, wt, agent)
        return self.decision(self.run_hook("tests_first.py", payload))

    def assertSourceAllowed(self, wt: Path) -> None:
        for agent in (None, "harness:coder"):
            with self.subTest(agent=agent):
                self.assertIsNone(self.edit(wt, "src/app.py", agent))
                self.assertIsNone(self.edit(wt, "src/new_module.py", agent))
        self.assertIsNone(self.bash(wt, "printf '\\n' >> src/app.py", "harness:coder"))

    def assertSourceDenied(self, wt: Path) -> None:
        for agent in (None, "harness:coder"):
            with self.subTest(agent=agent):
                self.assertEqual(self.edit(wt, "src/app.py", agent), CHANGED)
                self.assertEqual(self.edit(wt, "src/new_module.py", agent), CHANGED)
        reason = self.bash(wt, "printf '\\n' >> src/app.py", "harness:coder")
        self.assertIsNotNone(reason)
        self.assertIn(CHANGED, reason)

    def record_file(self, repo: Path) -> Path:
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        return self.home / ".claude/harness/repos" / key / "red/t-12-fix-login.json"

    def status(self, wt: Path) -> str:
        r = self.harness_red(wt, "status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.splitlines()[-1]

    def assertRecordHolds(self, repo: Path, wt: Path, before: bytes) -> None:
        """The proof is still good: the record is the one written before, or a new red
        record whose test_hash is the files' content now; status says current either way."""
        now = self.record_file(repo).read_bytes()
        if now != before:
            record = json.loads(now)
            self.assertIn(record.get("kind"), RED_KINDS)
            self.assertEqual(record["test_hash"], content_hash(wt, record["test_files"]))
        self.assertEqual(self.status(wt), "current: yes")

    # ---- DoD 1: main's edits to the branch's test files keep source edits allowed

    def test_a_merge_of_main_editing_the_branchs_test_files_keeps_source_allowed(self):
        variants = {
            # main changes the first test of one file, in place
            "first-test": {
                "tests/test_app.py": lambda t: replaced(
                    t, "assert 0 + 0 == 0", "assert 0 * 0 == 0"
                ),
            },
            # main inserts lines above the branch's hunk, shifting it down
            "inserted-above": {
                "tests/test_app.py": lambda t: "import os\n\n\n" + t,
            },
            # main removes the last test of one, appends to the other, and brings in
            # source and a test file the branch never touched
            "both-files-and-more": {
                "tests/test_app.py": lambda t: replaced(
                    t, "\ndef test_case_9():\n    assert 9 + 9 == 18\n", ""
                ),
                "tests/test_util.py": lambda t: t
                + "\n\ndef test_case_10():\n    assert 10 + 10 == 20\n",
                "src/other.py": "VALUE = 1\n",
                "tests/test_other.py": "def test_other():\n    pass\n",
            },
        }
        for name, changes in variants.items():
            with self.subTest(variant=name):
                repo, wt = self.ticket(name)
                self.on_main(repo, changes)
                self.merge_cleanly(wt)
                # the merge brought main's edits in and kept the branch's own line
                for rel in changes:
                    expected = (repo / rel).read_text()
                    if rel in MERGE_TEST_FILES:
                        expected = replaced(expected, MAIN_LINE_5, BRANCH_LINE_5)
                    self.assertEqual((wt / rel).read_text(), expected)
                self.assertSourceAllowed(wt)

    def test_repeated_merges_of_main_keep_source_allowed(self):
        repo, wt = self.ticket("repeated")
        self.on_main(
            repo,
            {"tests/test_app.py": lambda t: replaced(t, "0 + 0 == 0", "0 - 0 == 0")},
        )
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)
        self.on_main(
            repo,
            {"tests/test_util.py": lambda t: replaced(t, "9 + 9 == 18", "9 * 2 == 18")},
            "main moves again",
        )
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)

    def test_harness_red_check_after_a_merge_keeps_the_proof_and_never_a_stale_one(self):
        repo, wt = self.ticket("recheck")
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        before = self.record_file(repo).read_bytes()
        r = self.harness_red(wt, "check")
        self.assertNotIn("Traceback", r.stderr)
        # either the record is untouched or it is a new red one for the files as they are
        self.assertRecordHolds(repo, wt, before)
        self.assertSourceAllowed(wt)
        # then a real branch-side test change, one that keeps every line: test_case_5
        # (the branch's own test) moves to the end of the file
        proven = self.record_file(repo).read_bytes()
        path = wt / "tests/test_app.py"
        self.write(path, moved_to_end(path.read_text(), "def test_case_5():\n" + BRANCH_LINE_5))
        self.commit_all(wt, "the branch moves its test")
        self.assertSourceDenied(wt)
        self.assertEqual(self.status(wt), "current: no")
        r = self.harness_red(wt, "check")
        self.assertNotIn("Traceback", r.stderr)
        after = self.record_file(repo).read_bytes()
        record = json.loads(after)
        fresh = content_hash(wt, record["test_files"])
        if r.returncode == 0:  # a new red record, for these files: never the stale one
            self.assertNotEqual(after, proven)
            self.assertIn(record.get("kind"), RED_KINDS)
            self.assertEqual(record["test_hash"], fresh)
            self.assertEqual(self.status(wt), "current: yes")
            self.assertSourceAllowed(wt)
        else:  # refused, in one line that says why
            lines = r.stderr.strip().splitlines()
            self.assertEqual(len(lines), 1, r.stderr)
            self.assertTrue(lines[0].startswith("harness-red: "), r.stderr)
            if after == proven:  # the stale record is still not proof
                self.assertEqual(self.status(wt), "current: no")
                self.assertSourceDenied(wt)
            else:
                self.assertEqual(record["test_hash"], fresh)

    def test_main_moving_without_a_merge_keeps_source_allowed(self):
        # a regression guard: it passes before #231 too
        repo, wt = self.ticket("unmerged")
        self.on_main(
            repo,
            {
                "tests/test_app.py": lambda t: replaced(t, case_line(0), "    assert 0 * 0 == 0\n"),
                "tests/test_util.py": lambda t: t + "\n\ndef test_case_10():\n    assert 1\n",
                "src/other.py": "VALUE = 1\n",
            },
        )
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")

    # ---- DoD 2: the branch's own test edits after the red run still deny

    def test_a_branch_test_edit_after_a_merge_of_main_denies_source(self):
        own_test = "def test_case_5():\n" + BRANCH_LINE_5
        cases = {
            # the branch's own line, edited again
            "own-line": lambda t: replaced(t, BRANCH_LINE_5, "    assert login() == 'hi'\n"),
            # a line main brought in, edited by the branch
            "mains-line": lambda t: replaced(t, "assert 0 * 0 == 0", "assert 0 ** 2 == 0"),
            # a new test appended by the branch
            "appended": lambda t: t + "\n\ndef test_more():\n    assert login()\n",
            # the same lines in another order: the branch's own test moved below test_case_9
            "own-test-moved": lambda t: moved_to_end(t, own_test),
            # the same lines in another order: two existing tests swap their asserts
            "asserts-swapped": lambda t: swapped(t, case_line(1), case_line(2)),
            # the branch's line swapped with the next test's
            "own-line-swapped": lambda t: swapped(t, BRANCH_LINE_5, case_line(6)),
            # the branch's own line moved from test_case_5 into test_case_6
            "own-line-into-6": lambda t: line_moved_into(t, BRANCH_LINE_5, case_line(6)),
        }
        repo, wt = self.ticket("branch-edits")
        self.on_main(
            repo,
            {"tests/test_app.py": lambda t: replaced(t, "assert 0 + 0 == 0", "assert 0 * 0 == 0")},
        )
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)
        merged = self.git(wt, "rev-parse", "HEAD")
        path = wt / "tests/test_app.py"
        for name, change in cases.items():
            for committed in (False, True):
                with self.subTest(case=name, committed=committed):
                    self.write(path, change(path.read_text()))
                    if committed:
                        self.commit_all(wt, "the branch edits its tests")
                    self.assertSourceDenied(wt)
                    self.assertEqual(self.status(wt), "current: no")
                    # undone, the record holds again
                    if committed:
                        self.git(wt, "reset", "-q", "--hard", merged)
                    else:
                        self.git(wt, "checkout", "--", "tests/test_app.py")
                    self.assertSourceAllowed(wt)

    def test_a_branch_commit_to_a_recorded_test_main_never_touched_denies_source(self):
        repo, wt = self.ticket("untouched")
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)
        path = wt / "tests/test_util.py"  # the other recorded file, which main never touched
        self.write(path, replaced(path.read_text(), "1 + 1 == 2", "1 + 1 == 3"))
        self.commit_all(wt, "the branch edits its other test file")
        self.assertSourceDenied(wt)

    # ---- DoD 3: a merge resolution that changes the branch's own test lines denies

    def test_a_conflict_resolution_changing_the_branchs_test_lines_denies_source(self):
        main_line = MAIN_LINE_5_REWRITTEN
        resolutions = {
            # a third value for the branch's line
            "third-value": lambda t: replaced(t, main_line, "    assert login() == 'welcome'\n"),
            # main's side: the branch's line is gone
            "mains-side": lambda t: t,
            # main's line in test_case_5, the branch's line verbatim but in test_case_8
            "branch-line-into-8": lambda t: replaced(t, case_line(8), case_line(8) + BRANCH_LINE_5),
            # main's line in test_case_5, the branch's line verbatim but first in test_case_0
            "branch-line-into-0": lambda t: replaced(t, case_line(0), BRANCH_LINE_5 + case_line(0)),
            # the branch's line kept in test_case_5, main's line moved to the end of the file
            "mains-line-moved": lambda t: replaced(t, main_line, BRANCH_LINE_5)
            + "\n\ndef test_case_5b():\n" + main_line,
        }
        repo, wt = self.ticket("conflict")
        # first a clean merge: the record holds across it
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)
        before = self.git(wt, "rev-parse", "HEAD")
        # then main rewrites the branch's own line: a conflict
        self.on_main(
            repo,
            {"tests/test_app.py": lambda t: replaced(t, MAIN_LINE_5, main_line)},
            "main rewrites test_case_5",
        )
        theirs = (repo / "tests/test_app.py").read_text()
        for name, resolve in resolutions.items():
            with self.subTest(resolution=name):
                r = self.merge_main(wt)
                self.assertNotEqual(r.returncode, 0, "the merge must conflict")
                self.write(wt / "tests/test_app.py", resolve(theirs))
                self.git(wt, "add", "tests/test_app.py")
                self.git(wt, "commit", "-q", "--no-edit")
                self.assertEqual(len(self.git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
                self.assertSourceDenied(wt)
                self.assertEqual(self.status(wt), "current: no")
                self.git(wt, "reset", "-q", "--hard", before)

    # ---- main's edits close to the branch's hunk (DoD 1)
    # git conflicts on an edit to the line right next to the branch's change, so the
    # closest clean edits are two lines away.

    def test_a_merge_of_main_editing_close_to_the_branchs_hunk_keeps_source_allowed(self):
        login_in = lambda t: put_at(t, LOGIN_GAP, LOGIN_TEST)  # noqa: E731
        variants = (
            # (name, main's base, the test-author's change, main's change)
            (
                "def-two-lines-after",
                BASE_TESTS,
                None,
                lambda t: replaced(t, "def test_case_6():\n", "def test_case_6_renamed():\n"),
            ),
            (
                "assert-two-lines-before",
                BASE_TESTS,
                None,
                lambda t: replaced(t, case_line(4), "    assert 4 * 2 == 8\n"),
            ),
            (
                "repeated-assert-after-the-gap",
                REPEATED_TESTS,
                login_in,
                lambda t: with_test(t, LOGIN_GAP, "def test_case():\n    assert ok(5)"),
            ),
            (
                "repeated-assert-before-the-gap",
                REPEATED_TESTS,
                login_in,
                lambda t: with_test(t, LOGIN_GAP - 1, "def test_case():\n    assert ok(4)"),
            ),
        )
        for name, base, author, change in variants:
            with self.subTest(variant=name):
                repo, wt = self.ticket(f"near-{name}", base, author)
                self.on_main(repo, {"tests/test_app.py": change})
                self.merge_cleanly(wt)
                main_text = (repo / "tests/test_app.py").read_text()
                expected = (author or (lambda t: replaced(t, MAIN_LINE_5, BRANCH_LINE_5)))(main_text)
                self.assertEqual((wt / "tests/test_app.py").read_text(), expected)
                self.assertSourceAllowed(wt)
                self.assertEqual(self.status(wt), "current: yes")

    # ---- repeated context: the branch's test, unchanged, in another gap

    def repeated_ticket(self, name: str) -> tuple[Path, Path, str]:
        """The repeated-context ticket after a clean merge of main (which edits the imports
        and the last test, and appends one): (main checkout, worktree, the merge commit)."""
        repo, wt = self.ticket(name, REPEATED_TESTS, lambda t: put_at(t, LOGIN_GAP, LOGIN_TEST))
        self.on_main(
            repo,
            {
                "tests/test_app.py": lambda t: pep8(
                    ["import os\nimport pytest", *pep8_tests(t)[1:-1],
                     "def test_last():\n    assert last(1)", "def test_more():\n    assert ok(1)"]
                )
            },
        )
        self.merge_cleanly(wt)
        expected = put_at((repo / "tests/test_app.py").read_text(), LOGIN_GAP, LOGIN_TEST)
        self.assertEqual((wt / "tests/test_app.py").read_text(), expected)
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")
        return repo, wt, self.git(wt, "rev-parse", "HEAD")

    def test_a_merge_of_main_keeps_source_allowed_with_repeated_context(self):
        repo, wt, _ = self.repeated_ticket("repeated-merge")
        self.on_main(
            repo,
            {"tests/test_util.py": lambda t: with_test(t, -1, "def test_last():\n    assert last(2)")},
            "main moves again",
        )
        self.merge_cleanly(wt)
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")

    def test_a_branch_moving_its_test_to_another_gap_denies_source(self):
        repo, wt, merged = self.repeated_ticket("repeated-move")
        path = wt / "tests/test_app.py"
        for gap in (9, 3, 1, LOGIN_GAP + 1, LOGIN_GAP - 1):  # never its own gap
            for committed in (False, True):
                with self.subTest(gap=gap, committed=committed):
                    self.write(path, moved_to_gap(path.read_text(), LOGIN_TEST, gap))
                    if committed:
                        self.commit_all(wt, "the branch moves its test")
                    self.assertSourceDenied(wt)
                    self.assertEqual(self.status(wt), "current: no")
                    if committed:
                        self.git(wt, "reset", "-q", "--hard", merged)
                    else:
                        self.git(wt, "checkout", "--", "tests/test_app.py")
                    self.assertSourceAllowed(wt)

    def test_a_conflict_resolution_moving_the_branchs_test_to_another_gap_denies_source(self):
        repo, wt, before = self.repeated_ticket("repeated-conflict")
        # main adds its own test in the gap where the branch put its own: a conflict
        self.on_main(
            repo,
            {"tests/test_app.py": lambda t: put_at(t, LOGIN_GAP, MAIN_LOGIN_TEST)},
            "main adds its own login test",
        )
        theirs = (repo / "tests/test_app.py").read_text()
        without_mains = pep8([t for t in pep8_tests(theirs) if t != MAIN_LOGIN_TEST])
        for name, resolved in (
            # main's test in that gap, the branch's test unchanged but later
            ("branch-test-later", put_at(theirs, 10, LOGIN_TEST)),
            # main's test in that gap, the branch's test unchanged but earlier
            ("branch-test-earlier", put_at(theirs, 3, LOGIN_TEST)),
            # main's test dropped, the branch's test unchanged but one gap later
            ("mains-dropped-branch-later", put_at(without_mains, LOGIN_GAP + 1, LOGIN_TEST)),
        ):
            with self.subTest(resolution=name):
                r = self.merge_main(wt)
                self.assertNotEqual(r.returncode, 0, "the merge must conflict")
                self.write(wt / "tests/test_app.py", resolved)
                self.git(wt, "add", "tests/test_app.py")
                self.git(wt, "commit", "-q", "--no-edit")
                self.assertEqual(len(self.git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
                self.assertSourceDenied(wt)
                self.assertEqual(self.status(wt), "current: no")
                self.git(wt, "reset", "-q", "--hard", before)

    # ---- review round (#231): an older base, a merge in progress, blank-line runs, a
    # branch commit before the merge, and the "ours" ruling

    def test_an_older_merge_base_does_not_let_the_branch_revert_mains_edit(self):
        # The red run was made against a stacked parent that carries main commit X (an
        # edit of a recorded test, away from the branch's hunk); main itself is older.
        # The merge base with main is then an ancestor of the record's base: undoing X's
        # lines on the branch is the branch's own test change.
        repo = self.make_repo("older-base")
        for rel in MERGE_TEST_FILES:
            self.write(repo / rel, BASE_TESTS)
        self.commit_all(repo, "the tests so far")
        self.git(repo, "switch", "-q", "-c", "parent")
        x_line = "    assert 0 * 0 == 0\n"
        self.write(repo / "tests/test_app.py", replaced(BASE_TESTS, case_line(0), x_line))
        self.commit_all(repo, "X: the parent edits test_case_0")
        self.git(repo, "switch", "-q", "main")
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        ticket = {"number": 12, "title": "Fix login", "type": "task", "flags": [], "labels": ["task"]}
        self.write(self.home / ".claude/harness/repos" / key / "tickets/12.json", json.dumps(ticket))
        wt = self.tmp / "older-base-wt"
        self.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(wt), "parent")
        for rel in MERGE_TEST_FILES:  # the test-author
            self.write(wt / rel, replaced((wt / rel).read_text(), MAIN_LINE_5, BRANCH_LINE_5))
        self.commit_all(wt, "tests for login")
        r = self.harness_red(wt, "check", "--base", "parent")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            json.loads(self.record_file(repo).read_text())["base_sha"],
            self.git(repo, "rev-parse", "parent"),
        )
        self.write(wt / "src/app.py", "def login():\n    return 'ok'\n")  # the coder
        self.commit_all(wt, "login")
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")
        head = self.git(wt, "rev-parse", "HEAD")
        path = wt / "tests/test_app.py"
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.git(wt, "reset", "-q", "--hard", head)
                self.write(path, replaced(path.read_text(), x_line, case_line(0)))  # X undone
                if committed:
                    self.commit_all(wt, "the branch undoes X")
                self.assertSourceDenied(wt)
                self.assertEqual(self.status(wt), "current: no")
                self.git(wt, "reset", "-q", "--hard", head)
                self.assertSourceAllowed(wt)

    def test_source_edits_are_allowed_while_a_merge_of_main_is_in_conflict(self):
        repo, wt = self.ticket("mid-merge")
        self.on_main(
            repo,
            {
                "tests/test_app.py": lambda t: "# main's header\n" + t,  # clean
                "src/app.py": "def login():\n    return 'main'\n",  # conflicts with the coder's
            },
        )
        r = self.merge_main(wt)
        self.assertNotEqual(r.returncode, 0, "the merge must conflict in src/app.py")
        self.git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")  # still merging
        self.assertEqual(self.git(wt, "diff", "--name-only", "--diff-filter=U"), "src/app.py")
        merged = replaced((repo / "tests/test_app.py").read_text(), MAIN_LINE_5, BRANCH_LINE_5)
        self.assertEqual((wt / "tests/test_app.py").read_text(), merged)
        # resolving the source conflict needs source edits
        self.assertSourceAllowed(wt)
        for command in ("git checkout --theirs src/app.py", "git checkout --ours -- src/app.py"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(wt, command, "harness:coder"))
        self.assertEqual(self.status(wt), "current: yes")
        # a test change of the branch's own, mid-merge, still counts
        path = wt / "tests/test_app.py"
        self.write(path, replaced(merged, BRANCH_LINE_5, "    assert login() == 'hi'\n"))
        self.assertSourceDenied(wt)
        self.assertEqual(self.status(wt), "current: no")
        self.write(path, merged)
        self.assertSourceAllowed(wt)

    def test_a_merge_of_main_trimming_blank_lines_keeps_source_allowed(self):
        repo, wt = self.ticket("trim", TRIM_TESTS, trim_author)
        self.on_main(repo, {"tests/test_app.py": trim_main, "tests/test_util.py": trim_main})
        self.merge_cleanly(wt)
        for rel in MERGE_TEST_FILES:  # main's trim and test_more, and the branch's line
            self.assertEqual((wt / rel).read_text(), trim_author(trim_main(TRIM_TESTS)))
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")

    def test_a_branch_test_commit_before_a_merge_of_main_denies_source(self):
        # a regression guard: it passes before the review round too
        repo, wt = self.ticket("commit-then-merge")
        path = wt / "tests/test_app.py"
        self.write(path, replaced(path.read_text(), case_line(8), "    assert 8 * 2 == 16\n"))
        self.commit_all(wt, "the branch edits test_case_8")
        self.on_main(
            repo,
            {"tests/test_app.py": lambda t: replaced(t, case_line(0), "    assert 0 * 0 == 0\n")},
        )
        self.merge_cleanly(wt)
        self.assertIn("    assert 0 * 0 == 0\n", path.read_text())
        self.assertSourceDenied(wt)
        self.assertEqual(self.status(wt), "current: no")

    def test_an_ours_resolution_of_the_branchs_own_line_keeps_source_allowed(self):
        # a regression guard: it passes before the review round too
        for how in ("merge -X ours", "by hand"):
            with self.subTest(resolution=how):
                repo, wt = self.ticket("ours-" + how.split()[-1])
                # main edits the top cleanly and rewrites the branch's own line
                self.on_main(
                    repo,
                    {"tests/test_app.py": lambda t: "# main's header\n"
                     + replaced(t, MAIN_LINE_5, MAIN_LINE_5_REWRITTEN)},
                )
                theirs = (repo / "tests/test_app.py").read_text()
                ours = replaced(theirs, MAIN_LINE_5_REWRITTEN, BRANCH_LINE_5)
                if how == "by hand":
                    r = self.merge_main(wt)
                    self.assertNotEqual(r.returncode, 0, "the merge must conflict")
                    self.write(wt / "tests/test_app.py", ours)
                    self.git(wt, "add", "tests/test_app.py")
                    self.git(wt, "commit", "-q", "--no-edit")
                else:
                    self.git(wt, "merge", "-q", "--no-edit", "-X", "ours", "main")
                self.assertEqual((wt / "tests/test_app.py").read_text(), ours)
                self.assertEqual(len(self.git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
                self.assertSourceAllowed(wt)
                self.assertEqual(self.status(wt), "current: yes")
                # and a later merge of main stays allowed
                self.on_main(
                    repo,
                    {"tests/test_app.py": lambda t: replaced(t, case_line(9), "    assert 9 * 2 == 18\n")},
                    "main moves again",
                )
                self.merge_cleanly(wt)
                self.assertSourceAllowed(wt)
                self.assertEqual(self.status(wt), "current: yes")


if __name__ == "__main__":
    unittest.main()
