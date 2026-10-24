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
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SHOWN_LIMIT = 160  # a refused part is displayed cut to 160 characters


def as_shown(part: str) -> str:
    """The refused part as a denial displays it: up to SHOWN_LIMIT characters,
    a longer part cut to SHOWN_LIMIT - 1 characters plus an ellipsis."""
    if len(part) <= SHOWN_LIMIT:
        return part
    return part[: SHOWN_LIMIT - 1] + "…"


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


# Ignore rules committed in BranchGuardBashExemptions' repo.
EXEMPT_GITIGNORE = ".env.local\n.venv\n*.log\ndata/\ncache/\n"
OUTSIDE_JSON = "/private/tmp/x/out.json"


def heredoc(program: str, interpreter: str = "python3 -", tag: str = "PY") -> str:
    """`interpreter <<'TAG'` reading `program` from a heredoc."""
    return f"{interpreter} <<'{tag}'\n{program}\n{tag}"


class BranchGuardBashExemptions(GuardTestCase):
    """Bash writes on main are allowed where an Edit would be: under docs/agent/ and on
    paths git ignores. Inline python and node programs that write are allowed when every
    path literal in them lies outside every protected main working tree."""

    def setUp(self) -> None:
        super().setUp()
        # under HOME, so ~/proj/... names the repo
        self.repo = self.make_repo("home/proj")
        repo = self.repo
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE)
        self.write(repo / "docs/agent/handoff.md", "# Handoff\n")
        self.write(repo / "docs/design.md", "# Design\n")
        self.write(repo / "keep.log", "tracked although *.log is ignored\n")
        self.git(repo, "add", ".gitignore", "docs")
        self.git(repo, "add", "-f", "keep.log")
        self.git(repo, "commit", "-q", "-m", "Ignore local files, add notes")
        # ignored and untracked, as they sit in a working checkout
        self.write(repo / "data/a.csv", "a,b\n1,2\n")
        self.write(repo / "cache/blob", "x\n")
        self.write(repo / ".env.local", "TOKEN_FILE=x\n")
        self.write(repo / "build.log", "ok\n")
        (self.home / ".venv").mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.write(self.outside / "notes.md", "notes\n")
        self.write(self.outside / "fix.diff", "")
        # the tree is still clean apart from ignored files
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def bash(self, command: str, cwd: Path | None = None, env=None) -> str | None:
        tool_input = {"command": command, "description": "Run it"}
        payload = self.payload("Bash", tool_input, cwd or self.repo)
        return self.decision(self.run_hook("branch_guard.py", payload, env))

    def assertAllowed(self, commands, cwd: Path | None = None) -> None:
        for command in commands:
            with self.subTest(command=command, cwd=str(cwd or self.repo)):
                self.assertIsNone(self.bash(command, cwd=cwd))

    def assertRefusedOnMain(self, reason, part: str, why: str) -> None:
        self.assertEqual(
            reason,
            f'On main in proj. Refused "{as_shown(part)}": {why}. {ADVICE}',
        )

    def assertRefusedPart(self, reason, part: str) -> None:
        """Refused on main for `part`, whatever the stated reason."""
        self.assertIsNotNone(reason)
        prefix = f'On main in proj. Refused "{as_shown(part)}": '
        self.assertTrue(reason.startswith(prefix), reason)
        self.assertTrue(reason.endswith(f". {ADVICE}"), reason)

    # ---- broad: the ticket's cases through the hook

    def test_copy_into_docs_agent_allowed(self):
        repo, outside = self.repo, self.outside
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                f"cp {outside}/notes.md docs/agent/handoff.md",
                f"cp {outside}/notes.md docs/agent/",
                f"cp {outside}/notes.md docs/agent/specs/2026-10-23-x.md",
                f"cp -R {outside} docs/agent/imported",
                f"cp {outside}/notes.md docs/agent/a.md && cp {outside}/notes.md docs/agent/b.md",
            )
        )
        self.assertAllowed(
            (
                f"cp notes.md {repo}/docs/agent/handoff.md",
                f"cd {repo} && cp {outside}/notes.md docs/agent/handoff.md",
            ),
            cwd=outside,
        )
        # the same copy anywhere else in the repo stays refused
        for command, part, why in (
            (
                "cp notes.md docs/design.md",
                "cp notes.md docs/design.md",
                "cp changes docs/design.md inside the repo",
            ),
            (
                "cp notes.md docs/agentx.md",
                "cp notes.md docs/agentx.md",
                "cp changes docs/agentx.md inside the repo",
            ),
            (
                "cp notes.md docs/",
                "cp notes.md docs/",
                "cp changes docs/ inside the repo",
            ),
            (
                "cp notes.md docs/agent/a.md && cp notes.md src/app.py",
                "cp notes.md src/app.py",
                "cp changes src/app.py inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_symlink_onto_ignored_path_allowed(self):
        self.assertAllowed(
            (
                "ln -sfn ../.venv .venv",
                f"ln -s {self.home}/.venv .venv",
                f"ln -sf {self.outside}/notes.md build.log",
                f"ln -s {self.outside}/notes.md docs/agent/linked.md",
            )
        )
        for command, part, why in (
            (
                "ln -sfn ../.venv venv",
                "ln -sfn ../.venv venv",
                "ln changes venv inside the repo",
            ),
            (
                f"ln -sf {self.outside}/notes.md src/app.py",
                f"ln -sf {self.outside}/notes.md src/app.py",
                "ln changes src/app.py inside the repo",
            ),
            (
                f"ln -sf {self.outside}/notes.md keep.log",
                f"ln -sf {self.outside}/notes.md keep.log",
                "ln changes keep.log inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_redirect_into_ignored_file_allowed(self):
        self.assertAllowed(
            (
                "echo x > .env.local",
                "echo x >> .env.local",
                "make check > build.log 2>&1",
                "make check > logs/today.log",
                "printf 'a,b\\n' > data/out.csv",
                "echo note >> docs/agent/handoff.md",
                "cd src && echo x > ../.env.local",
            )
        )
        self.assertAllowed(
            (
                f"echo x > {self.repo}/.env.local",
                f"echo x > {self.repo}/docs/agent/handoff.md",
            ),
            cwd=self.outside,
        )
        for command, target in (
            ("echo x > .env.example", ".env.example"),
            ("echo x > env.local", "env.local"),
            ("echo x > docs/design.md", "docs/design.md"),
            ("echo x > datafile.csv", "datafile.csv"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    command,
                    f"it writes to {target} through a redirection",
                )

    def test_heredoc_writing_only_outside_repos_allowed(self):
        repo, outside = self.repo, self.outside
        nested = repo / ".claude/worktrees/t-13-x"
        self.git(repo, "worktree", "add", "-q", "-b", "t/13-x", str(nested))
        self.assertAllowed(
            (
                # the ticket's case
                heredoc(
                    "import json\n"
                    f"with open('{OUTSIDE_JSON}', 'w') as f:\n"
                    "    json.dump({'ok': True}, f)"
                ),
                heredoc(f"open('{outside}/out.json', 'w').write('x')", "python3"),
                heredoc(
                    "from pathlib import Path\n"
                    f"Path('{outside}/report.txt').write_text('x')"
                ),
                # several literals, all outside: one read, two written
                heredoc(
                    f"src = open('{outside}/notes.md').read()\n"
                    f"open('{OUTSIDE_JSON}', 'w').write(src)\n"
                    f"open('{outside}/copy.md', 'a').write(src)"
                ),
                heredoc(
                    "import shutil\n"
                    f"shutil.copy('{outside}/notes.md', '/private/tmp/x/notes.md')"
                ),
                # ~ and ../ resolve outside the repo
                heredoc(
                    "import os\n"
                    "open(os.path.expanduser('~/elsewhere/out.json'), 'w')"
                ),
                heredoc("open('../../outside/rel.json', 'w')"),
                # a ticket worktree is judged by its own branch
                heredoc(f"open('{nested}/out.json', 'w').write('x')"),
            )
        )

    def test_inline_python_and_node_programs_writing_outside_allowed(self):
        outside = self.outside
        self.assertAllowed(
            (
                f"""python3 -c "open('{OUTSIDE_JSON}','w').write('x')\"""",
                f"""python3 -c "open('{outside}/a.txt', 'w')\"""",
                f"""python3 -c "import shutil; shutil.copy('{outside}/notes.md', '{outside}/b.md')\"""",
                f"""node -e "require('fs').writeFileSync('{OUTSIDE_JSON}', 'x')\"""",
                f"""node --eval="require('fs').writeFileSync('{outside}/o.txt', 'x')\"""",
                heredoc(
                    f"require('fs').writeFileSync('{OUTSIDE_JSON}', 'x')", "node", "JS"
                ),
            )
        )
        for command in (
            """python3 -c "open('./src/app.py','w').write('x')\"""",
            f"""python3 -c "open('{OUTSIDE_JSON}','w'); open('./src/app.py','w')\"""",
            """node -e "require('fs').writeFileSync('./src/app.js', 'x')\"""",
            f"""node -e "require('fs').writeFileSync('{OUTSIDE_JSON}', 'x'); require('fs').writeFileSync('app.js', 'x')\"""",
            # the repo literal first, the outside one last
            f"""python3 -c "open('./src/app.py','w'); open('{OUTSIDE_JSON}','w')\"""",
            f"""node -e "require('fs').writeFileSync('./README.md', 'x'); require('fs').writeFileSync('{OUTSIDE_JSON}', 'x')\"""",
            # and between two outside literals
            f"""python3 -c "open('{OUTSIDE_JSON}','w'); open('./src/app.py','w'); open('{outside}/c.txt','w')\"""",
            f"""node -e "require('fs').writeFileSync('{OUTSIDE_JSON}', 'x'); require('fs').writeFileSync('./README.md', 'x'); require('fs').writeFileSync('{outside}/c.txt', 'x')\"""",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)

    def test_deleting_or_moving_ignored_paths_allowed(self):
        self.assertAllowed(
            (
                "rm -rf data",
                "rm data/a.csv",
                "mv data/a.csv data/b.csv",
                "rm -f .env.local build.log",
                "rm -rf cache .venv",
                "truncate -s 0 build.log",
                f"mv .env.local {self.outside}/",
                f"mv {self.outside}/notes.md data/notes.md",
                # docs/agent/, tracked or not
                "rm docs/agent/handoff.md",
                "rm -rf docs/agent/specs",
                "mv docs/agent/handoff.md docs/agent/2026-10-23-handoff.md",
                "mv data/a.csv docs/agent/a.csv",
            )
        )
        for command, why in (
            ("rm README.md", "rm changes README.md inside the repo"),
            ("rm -rf src", "rm changes src inside the repo"),
            ("rm keep.log", "rm changes keep.log inside the repo"),
            ("rm data/a.csv src/app.py", "rm changes src/app.py inside the repo"),
            ("rm -f docs/design.md", "rm changes docs/design.md inside the repo"),
            ("truncate -s 0 keep.log", "truncate changes keep.log inside the repo"),
            ("mv data/a.csv src/a.csv", "mv changes src/a.csv inside the repo"),
            ("mv README.md data/", "mv changes README.md inside the repo"),
            ("mv src/app.py docs/agent/", "mv changes src/app.py inside the repo"),
            (
                f"mv docs/design.md {self.outside}/",
                "mv changes docs/design.md inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)
        # An ignored directory that holds a force-added tracked file: deleting or moving
        # the directory deletes the tracked file, so it is refused; the untracked,
        # ignored file beside it is still free.
        repo = self.repo
        self.write(repo / "data/tracked.csv", "t\n")
        self.git(repo, "add", "-f", "data/tracked.csv")
        self.git(repo, "commit", "-q", "-m", "Track one file under data/")
        self.assertAllowed(("rm data/a.csv", "mv data/a.csv data/b.csv"))
        for command in (
            "rm -rf data",
            f"mv data {self.outside}/",
            "rm data/tracked.csv",
            "mv data/tracked.csv data/b.csv",
        ):
            with self.subTest(command=command, fixture="data/tracked.csv tracked"):
                self.assertRefusedPart(self.bash(command), command)

    def test_heredoc_without_path_literal_still_refused(self):
        for command, part, why in (
            (
                heredoc("import sys\nopen(sys.argv[1], 'w').write('x')"),
                "python3 - <<'PY'",
                "python opens a file for writing",
            ),
            # the path given on the command line is not a literal in the program
            (
                heredoc(
                    "import sys\nopen(sys.argv[1], 'w').write('x')",
                    f"python3 - {OUTSIDE_JSON}",
                ),
                f"python3 - {OUTSIDE_JSON} <<'PY'",
                "python opens a file for writing",
            ),
            (
                heredoc("name = 'report'\nopen(name, 'w')"),
                "python3 - <<'PY'",
                "python opens a file for writing",
            ),
            (
                heredoc("import os\nos.remove(os.environ['TARGET'])"),
                "python3 - <<'PY'",
                "python writes, moves or deletes files",
            ),
            (
                f"""python3 -c "import sys; open(sys.argv[1], 'w')" {OUTSIDE_JSON}""",
                f"""python3 -c "import sys; open(sys.argv[1], 'w')" {OUTSIDE_JSON}""",
                "python opens a file for writing",
            ),
            (
                """node -e "require('fs').writeFileSync(process.argv[1], 'x')\"""",
                """node -e "require('fs').writeFileSync(process.argv[1], 'x')\"""",
                "node writes, moves or deletes files",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_heredoc_with_a_path_inside_the_repo_refused(self):
        repo = self.repo
        part = "python3 - <<'PY'"
        for program, cwd in (
            # the ticket's case: one literal outside, one in the repo
            ("open('/tmp/x', 'w').write('a')\nopen('src/app.py', 'w').write('b')", repo),
            ("open('/tmp/x', 'w').write('a')\nopen('./src/app.py', 'w').write('b')", repo),
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('{repo}/src/app.py', 'w')", repo),
            # relative literals resolve against the directory the part runs in
            ("open('out.json', 'w')", repo),
            ("open('out.json', 'w')", repo / "src"),
            ("open('../app.json', 'w')", repo / "src"),
            # ~ and ../ that lead back into the repo
            (
                f"open('{OUTSIDE_JSON}', 'w')\n"
                "import os\nopen(os.path.expanduser('~/proj/src/app.py'), 'w')",
                repo,
            ),
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('../proj/src/app.py', 'w')", repo),
            # the repo literal first, the outside one last
            ("open('src/app.py', 'w')\nopen('/private/tmp/x/out.json', 'w')", repo),
            (f"open('{repo}/src/app.py', 'w')\nopen('{OUTSIDE_JSON}', 'w')", repo),
            ("open('../app.json', 'w')\nopen('/private/tmp/x/out.json', 'w')", repo / "src"),
            # and between two outside literals
            (
                f"open('{OUTSIDE_JSON}', 'w')\n"
                "open('./README.md', 'w')\n"
                f"open('{self.outside}/c.txt', 'w')",
                repo,
            ),
        ):
            with self.subTest(program=program, cwd=str(cwd)):
                self.assertRefusedPart(self.bash(heredoc(program), cwd=cwd), part)
        # the part runs in the repo after cd, though the session is elsewhere
        command = f"cd {repo} && " + heredoc("open('out.json', 'w').write('x')")
        self.assertRefusedPart(self.bash(command, cwd=self.outside), part)

    def test_tracked_file_redirect_still_refused(self):
        for command, part, target in (
            ("echo x > README.md", "echo x > README.md", "README.md"),
            ("echo x >> keep.log", "echo x >> keep.log", "keep.log"),
            ("echo x > .gitignore", "echo x > .gitignore", ".gitignore"),
            ("echo x > src/app.py", "echo x > src/app.py", "src/app.py"),
            (
                "echo x > .env.local && echo y > src/app.py",
                "echo y > src/app.py",
                "src/app.py",
            ),
            (
                "echo x > docs/agent/handoff.md; echo y >> README.md",
                "echo y >> README.md",
                "README.md",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    part,
                    f"it writes to {target} through a redirection",
                )
        target = f"{self.repo}/README.md"
        self.assertRefusedOnMain(
            self.bash(f"echo x > {target}", cwd=self.outside),
            f"echo x > {target}",
            f"it writes to {target} through a redirection",
        )

    # ---- specific: each write form, exempt target against tracked target

    def test_each_write_form_follows_the_exemptions(self):
        outside = self.outside
        for allowed, refused, part, why in (
            (
                "ls | tee build.log",
                "ls | tee README.md",
                "tee README.md",
                "tee changes README.md inside the repo",
            ),
            (
                "ls | tee -a docs/agent/handoff.md",
                "ls | tee -a src/app.py",
                "tee -a src/app.py",
                "tee changes src/app.py inside the repo",
            ),
            (
                f"install -m 644 {outside}/notes.md docs/agent/notes.md",
                f"install -m 644 {outside}/notes.md src/app.py",
                f"install -m 644 {outside}/notes.md src/app.py",
                "install changes src/app.py inside the repo",
            ),
            (
                f"rsync -a {outside}/ docs/agent/",
                f"rsync -a {outside}/ src/",
                f"rsync -a {outside}/ src/",
                "rsync changes src/ inside the repo",
            ),
            (
                "dd if=/dev/zero of=data/blob.bin count=1",
                "dd if=/dev/zero of=README.md count=1",
                "dd if=/dev/zero of=README.md count=1",
                "dd changes README.md inside the repo",
            ),
            (
                "touch .env.local docs/agent/new.md",
                "touch .env.local src/new.py",
                "touch .env.local src/new.py",
                "touch changes src/new.py inside the repo",
            ),
            (
                "chmod 600 .env.local",
                "chmod 600 src/app.py",
                "chmod 600 src/app.py",
                "chmod changes src/app.py inside the repo",
            ),
            (
                "sed -i '' 's/x/y/' .env.local",
                "sed -i '' 's/x/y/' README.md",
                "sed -i '' 's/x/y/' README.md",
                "sed -i edits files in place",
            ),
            (
                "perl -pi -e 's/x/y/' docs/agent/handoff.md",
                "perl -pi -e 's/x/y/' docs/design.md",
                "perl -pi -e 's/x/y/' docs/design.md",
                "perl -i edits files in place",
            ),
            (
                "awk -i inplace 1 build.log",
                "awk -i inplace 1 keep.log",
                "awk -i inplace 1 keep.log",
                "awk -i inplace edits files in place",
            ),
            (
                "printf '1d\\nw\\n' | ed -s docs/agent/handoff.md",
                "printf '1d\\nw\\n' | ed -s README.md",
                "ed -s README.md",
                "ed changes README.md inside the repo",
            ),
            (
                f"patch docs/agent/handoff.md {outside}/fix.diff",
                f"patch src/app.py {outside}/fix.diff",
                f"patch src/app.py {outside}/fix.diff",
                "patch changes src/app.py inside the repo",
            ),
            (
                f"cp {outside}/notes.md data/",
                f"cp {outside}/notes.md src/",
                f"cp {outside}/notes.md src/",
                "cp changes src/ inside the repo",
            ),
        ):
            with self.subTest(command=allowed):
                self.assertIsNone(self.bash(allowed))
            with self.subTest(command=refused):
                self.assertRefusedOnMain(self.bash(refused), part, why)

    def test_exempt_writes_do_not_cover_git_on_main(self):
        for command, part, why in (
            (
                "echo x > .env.local && git commit -am x",
                "git commit -am x",
                "git commit changes main",
            ),
            (
                "cp notes.md docs/agent/handoff.md && git push",
                "git push",
                "git push without a refspec pushes main",
            ),
            (
                "rm -rf data && git reset --hard HEAD~1",
                "git reset --hard HEAD~1",
                "git reset changes main",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    # ---- ignore rules are git's own, docs/agent/ stands alone

    def test_ignore_rules_are_gits_own(self):
        """Globs, negations, a nested .gitignore and .git/info/exclude, as git reads them;
        names the setUp rules ignored are refused once no rule ignores them."""
        repo = self.repo
        self.write(
            repo / ".gitignore", "secrets/*.pem\ndata/*\n!data/keep.csv\n/top-only.txt\n"
        )
        self.write(repo / "sub/.gitignore", "*.tmp\n")
        git_dir = Path(self.git(repo, "rev-parse", "--absolute-git-dir"))
        self.write(git_dir / "info/exclude", "scratch-*.txt\n")
        self.write(repo / "data/keep.csv", "k\n")
        self.assertAllowed(
            (
                "echo x > secrets/k.pem",
                f"cp {self.outside}/notes.md secrets/deploy.pem",
                "echo x > data/x.csv",
                "rm data/a.csv",
                "echo x > sub/a.tmp",
                "echo x > sub/deep/b.tmp",
                "echo x > scratch-1.txt",
                "touch scratch-2.txt",
                "echo x > top-only.txt",
            )
        )
        for command, target in (
            ("echo x > .env.local", ".env.local"),
            ("echo x > build.log", "build.log"),
            ("echo x > cache/blob", "cache/blob"),
            ("echo x > secrets/k.txt", "secrets/k.txt"),
            ("echo x > secrets/sub/k.pem", "secrets/sub/k.pem"),
            ("echo x > data/keep.csv", "data/keep.csv"),
            ("echo x > a.tmp", "a.tmp"),
            ("echo x > scratch.txt", "scratch.txt"),
            ("echo x > sub/top-only.txt", "sub/top-only.txt"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    command,
                    f"it writes to {target} through a redirection",
                )
        for command, why in (
            ("rm data/keep.csv", "rm changes data/keep.csv inside the repo"),
            ("ln -sfn ../.venv .venv", "ln changes .venv inside the repo"),
            ("rm -rf cache", "rm changes cache inside the repo"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)

    def test_docs_agent_allowed_without_any_ignore_rules(self):
        repo, outside = self.repo, self.outside
        (repo / ".gitignore").unlink()
        git_dir = Path(self.git(repo, "rev-parse", "--absolute-git-dir"))
        self.write(git_dir / "info/exclude", "")
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                "echo x >> docs/agent/new.md",
                "touch docs/agent/specs/a.md",
                "rm docs/agent/handoff.md",
                f"mv {outside}/notes.md docs/agent/n.md",
            )
        )
        for command, why in (
            ("echo x > .env.local", "it writes to .env.local through a redirection"),
            ("echo x > build.log", "it writes to build.log through a redirection"),
            ("rm -rf data", "rm changes data inside the repo"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)

    # ---- inline programs: every literal, every protected main tree

    def assertRefusedIn(self, reason, part: str, names=("proj",)) -> None:
        """Refused on main for `part`, in one of the repos `names`, whatever the reason."""
        self.assertIsNotNone(reason)
        alternatives = "|".join(re.escape(n) for n in names)
        pattern = (
            f"On main in ({alternatives})\\. Refused \"{re.escape(as_shown(part))}\": "
            f".+\\. {re.escape(ADVICE)}"
        )
        self.assertRegex(reason, re.compile(f"^{pattern}$", re.S))

    def test_outside_literals_vary_and_every_protected_tree_counts(self):
        repo = self.repo
        tmp_out = Path(
            tempfile.mkdtemp(prefix=f"hg{uuid.uuid4().hex[:6]}-", dir="/tmp")
        )
        self.addCleanup(shutil.rmtree, tmp_out, True)
        name = "d" + uuid.uuid4().hex[:8]
        home_dir = self.home / name
        home_dir.mkdir()
        elsewhere = self.tmp / ("w" + uuid.uuid4().hex[:8])
        elsewhere.mkdir()
        other = self.make_repo("home/other")
        # a repo whose main checkout is on another branch is not protected
        feature = self.make_repo("home/feat")
        self.git(feature, "switch", "-q", "-c", "feature")
        self.assertAllowed(
            (
                heredoc(f"open('{tmp_out}/out.json', 'w').write('x')"),
                heredoc(f"open('{feature}/notes.txt', 'w').write('x')"),
                heredoc(f"open('{tmp_out}/a.json', 'w')\nopen('{feature}/src/new.py', 'w')"),
                f"""python3 -c "open('{feature}/README.md','w').write('x')\"""",
                heredoc(
                    "import shutil\n"
                    f"shutil.copy('{tmp_out}/a.md', '{home_dir}/b.md')"
                ),
                heredoc(
                    "import os\n"
                    f"open(os.path.expanduser('~/{name}/c.json'), 'w').write('x')"
                ),
                heredoc(f"open('../{name}/f.json', 'w').write('x')"),
                f"""python3 -c "open('{home_dir}/d.txt','w').write('x')\"""",
                f"""node -e "require('fs').writeFileSync('{tmp_out}/e.txt', 'x')\"""",
            )
        )
        # a bare name with an extension, run outside every repo
        self.assertAllowed(
            (heredoc("open('report.csv', 'w').write('x')"),), cwd=elsewhere
        )
        part = "python3 - <<'PY'"
        for program, cwd in (
            # a second protected repo on main counts as much as the one the part runs in
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('{other}/src/new.py', 'w')", repo),
            (f"open('{tmp_out}/a.json', 'w')\nopen('{other}/notes.txt', 'w')", repo),
            (f"open('{tmp_out}/a.json', 'w')\nopen('{repo}/README.md', 'w')", other),
            # the protected literal first, the outside one last
            (f"open('{other}/notes.txt', 'w')\nopen('{tmp_out}/a.json', 'w')", repo),
            (f"open('{repo}/README.md', 'w')\nopen('{home_dir}/b.md', 'w')", other),
            # and the feature-branch repo beside a protected one does not excuse it
            (f"open('{feature}/notes.txt', 'w')\nopen('{other}/notes.txt', 'w')", repo),
            # a directory named outside inside the repo is still the repo
            (f"open('{repo}/outside/x.json', 'w')", repo),
            # repo targets of every literal form, each beside an allowed literal
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('./README.md', 'w')", repo),
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('{repo}/notes.txt', 'w')", repo),
            (f"open('{OUTSIDE_JSON}', 'w')\nopen('docs/x.md', 'w')", repo),
            (f"open('{tmp_out}/a.json', 'w')\nopen('notes.txt', 'w')", repo),
            (
                f"open('{home_dir}/a.json', 'w')\n"
                "import os\nopen(os.path.expanduser('~/proj/notes.txt'), 'w')",
                repo,
            ),
        ):
            with self.subTest(program=program, cwd=str(cwd)):
                self.assertRefusedIn(
                    self.bash(heredoc(program), cwd=cwd), part, ("proj", "other")
                )
        command = (
            f"""node -e "require('fs').writeFileSync('{OUTSIDE_JSON}', 'x'); """
            """require('fs').writeFileSync('./README.md', 'x')\""""
        )
        self.assertRefusedIn(self.bash(command), command)

    # ---- fix round 1: what a write word reaches, not how it is spelled

    def test_glob_words_are_never_exempt(self):
        """The shell expands a glob before the command runs, so the word git is asked
        about is not the file written: `*.log` reaches the tracked keep.log."""
        for command in (
            "rm -f *.log",
            "truncate -s 0 *.log",
            "rm -rf cach?",
            "rm -rf data/[ab].csv",
            "rm -f {build,keep}.log",
            "echo x > *.log",
            "rm -f build.log *.log",
            "touch .env.local data/*.csv",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # the same targets spelled out stay free
        self.assertAllowed(("rm -f build.log", "rm -rf cache", "rm -rf data/a.csv"))

    def test_dot_git_is_never_exempt(self):
        """A broad rule such as `.*` matches `.git` itself; writes under .git stay refused."""
        repo = self.repo
        self.write(repo / ".gitignore", ".*\n!.gitignore\n")
        for command in (
            "echo x > .git/HEAD",
            "rm -rf .git",
            "touch .git/info/x",
            "rm -rf .git/hooks",
            "echo x > .env.local && rm -rf .git",
        ):
            with self.subTest(command=command):
                reason = self.bash(command)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith("On main in proj. Refused "), reason)
                self.assertTrue(reason.endswith(f". {ADVICE}"), reason)
                self.assertIn(".git", reason)
        self.assertAllowed(
            ("echo x > .env.local", "echo x > .other-local", "rm -f .env.local")
        )

    def test_ignored_symlink_to_a_tracked_file_is_not_exempt(self):
        """The write lands where the symlink points: an ignored or docs/agent/ name that
        links to a tracked file is judged by that file."""
        repo = self.repo
        (repo / ".env.local").unlink()
        os.symlink("README.md", repo / ".env.local")
        os.symlink("../../README.md", repo / "docs/agent/x.md")
        os.symlink("../src/app.py", repo / "cache/app")
        os.symlink("keep.log", repo / "other.log")  # keep.log is ignored by name, tracked
        os.symlink("build.log", repo / "alias.log")  # an ignored, untracked target
        os.symlink(str(self.home / ".venv"), repo / ".venv")  # points outside the repo
        for command in (
            "echo x >> .env.local",
            "echo x > .env.local",
            "echo x > docs/agent/x.md",
            "echo x >> cache/app",
            f"cp {self.outside}/notes.md cache/app",
            "truncate -s 0 other.log",
            "echo x > other.log",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        self.assertAllowed(
            (
                "echo x > alias.log",
                "rm .venv",
                "ln -sfn ../.venv .venv",
                "echo note >> docs/agent/handoff.md",
            )
        )

    def test_other_case_spelling_of_a_tracked_file_is_not_exempt(self):
        repo = self.repo
        spelled = repo / "KEEP.LOG"
        if not (spelled.exists() and os.path.samefile(spelled, repo / "keep.log")):
            self.skipTest("another spelling of one name needs a case-insensitive file system")
        for command in ("rm KEEP.LOG", "truncate -s 0 Keep.Log", "echo x > KEEP.log"):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # an ignored, untracked file in another spelling is still only that file
        self.assertAllowed(("rm BUILD.LOG", "echo x > Build.Log"))

    def test_literal_joined_to_a_run_time_path_refused(self):
        """A literal concatenated with a value known only at run time does not say where
        the program writes, however absolute the literal looks."""
        repo = self.repo
        part = "python3 - <<'PY'"
        for program, cwd in (
            ("import os\nopen(os.getcwd() + '/out.json', 'w')", repo),
            ("import os\nopen(os.path.join(os.getcwd(), '/out.json'), 'w')", repo),
            ('import os\nopen(os.getcwd() + "/src/app.py", "w").write("x")', repo / "src"),
            ("import os\nopen(f'{os.getcwd()}/notes.txt', 'w')", repo),
            ("import os\nbase = os.getcwd()\nopen(base + '/README.md', 'w')", repo),
        ):
            with self.subTest(program=program, cwd=str(cwd)):
                self.assertRefusedPart(self.bash(heredoc(program), cwd=cwd), part)
        command = """node -e "require('fs').writeFileSync(process.cwd() + '/out.json', 'x')\""""
        self.assertRefusedPart(self.bash(command), command)
        # plain outside literals stay allowed, beside run-time values that build no path
        self.assertAllowed(
            (
                heredoc(f"import os\nopen('{OUTSIDE_JSON}', 'w')\nprint(os.getcwd())"),
                heredoc(f"n = 1 + 2\nopen('{OUTSIDE_JSON}', 'w').write(str(n))"),
                heredoc(f"open('{self.outside}/a.txt', 'w').write('x' + 'y')"),
            )
        )

    # ---- fix round 2: expanding sources into a directory, run-time joins

    def test_expanding_sources_copied_into_a_directory_refused(self):
        """A brace, glob or bracket source is expanded by the shell first, so
        `{keep,x}.log .` reaches the tracked keep.log; into a directory that is not itself
        exempt it is refused. Into an exempt directory every copy lands there, so the
        directory decides (ruling 2026-10-24)."""
        outside = self.outside
        for name in ("keep.log", "x.log", "a.log", "b.log", "xa.log"):
            self.write(outside / name, "from outside\n")
        for command in (
            f"cp {outside}/{{keep,x}}.log .",
            f"cp {outside}/{{keep,x}}.log ./",
            f"mv {outside}/{{keep,x}}.log .",
            f"cp {outside}/x[ab].log .",
            f"install {outside}/{{a,b}}.log .",
            f"ln -s {outside}/{{keep,x}}.log .",
            # the repo root is not exempt, whatever the basename of the source
            f"cp {outside}/x.log .",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # into an ignored directory, plain or expanding sources stay free
        self.assertAllowed(
            (
                f"cp {outside}/notes.md cache/",
                f"cp {outside}/x.log data/",
                f"cp {outside}/*.log cache/",
            )
        )

    # ---- fix round 3: a copy into a directory is judged by that directory

    def test_copy_into_a_directory_is_judged_by_the_directory(self):
        """BSD `cp -R src/ dest` copies the contents of src into dest, so `dest/src` is not
        what is written. Ruling 2026-10-24: a cp, rsync, install or ln into a directory is
        exempt only when that directory itself is under docs/agent/ or ignored by git; the
        basename of the source no longer counts."""
        repo, outside = self.repo, self.outside
        # the scenario is real: data/ and .venv/ hold a README.md that would land on the
        # tracked README.md at the repo root
        self.write(outside / "data/README.md", "clobbered\n")
        self.write(outside / ".venv/README.md", "clobbered\n")
        self.write(outside / "x.log", "from outside\n")
        self.write(outside / ".env.local", "from outside\n")
        for name in ("a.log", "b.log"):
            self.write(outside / name, "from outside\n")
        for command in (
            f"cp -R {outside}/data/ .",
            f"cp -R {outside}/.venv/ .",
            f"rsync -a {outside}/data/ .",
            f"cp {outside}/x.log .",
            f"cp {outside}/x.log ./",
            f"install {outside}/x.log .",
            f"ln -s {outside}/x.log .",
            f"cp {outside}/notes.md src/",
            # the source basename is ignored at the destination (*.log, data/, .env.local),
            # but the destination directory is tracked
            f"cp {outside}/x.log src/",
            f"cp {outside}/x.log src",
            f"cp {outside}/.env.local .",
            f"cp -R {outside}/data/ docs/",
            f"cp -R {outside}/data .",
            f"rsync -a {outside}/data/ src/",
            f"install -m 644 {outside}/x.log docs/",
            f"ln -sf {outside}/x.log tests/",
            f"cp {outside}/{{a,b}}.log .",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # an exempt copy beside one into the root: the root copy is the part refused
        self.assertRefusedPart(
            self.bash(f"cp {outside}/x.log data/ && cp {outside}/x.log ."),
            f"cp {outside}/x.log .",
        )
        # the repo root named absolutely, from outside the repo
        for command in (f"cp -R {outside}/data/ {repo}", f"cp {outside}/x.log {repo}/"):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        # the destination directory itself is exempt: every copy lands inside it
        self.assertAllowed(
            (
                f"cp {outside}/notes.md docs/agent/",
                f"cp -R {outside}/data/ docs/agent/specs/",
                f"rsync -a {outside}/ docs/agent/imported/",
                f"cp {outside}/x.log data/",
                f"cp {outside}/{{a,b}}.log cache/",
                f"cp -R {outside}/data/ docs/agent/",
                f"cp {outside}/x.log data",
                f"install {outside}/x.log cache/",
                f"ln -s {outside}/x.log data/",
                f"rsync -a {outside}/data/ docs/agent/",
            )
        )
        self.assertAllowed(
            (f"cp -R {outside}/data/ {repo}/docs/agent/", f"cp {outside}/x.log {repo}/data"),
            cwd=outside,
        )
        # named file destinations are judged by the name, as before
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                "ln -sfn ../.venv .venv",
                f"cp {outside}/x.log build.log",
            )
        )

    # ---- fix round 4: recursive copies into exempt directories, the destination's repo

    def workspace_fixture(self) -> Path:
        """node_modules/ ignored, holding @scope/pkg: a workspace symlink to the tracked
        packages/pkg. Returns a restore tree outside the repo shaped like node_modules/."""
        repo = self.repo
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE + "node_modules/\n")
        self.write(repo / "packages/pkg/index.js", "module.exports = 1;\n")
        self.git(repo, "add", ".gitignore", "packages")
        self.git(repo, "commit", "-q", "-m", "A workspace package")
        (repo / "node_modules/@scope").mkdir(parents=True)
        os.symlink("../../packages/pkg", repo / "node_modules/@scope/pkg")
        self.assertEqual(
            os.path.realpath(repo / "node_modules/@scope/pkg/index.js"),
            str(repo / "packages/pkg/index.js"),
        )
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        restore = self.outside / "restore"
        self.write(restore / "node_modules/@scope/pkg/index.js", "clobbered\n")
        self.write(self.outside / "data/README.md", "from outside\n")
        self.write(self.outside / "x.log", "from outside\n")
        for name in ("a.log", "b.log"):
            self.write(self.outside / name, "from outside\n")
        return restore

    def test_recursive_copy_into_an_ignored_directory_refused(self):
        """Ruling 2026-10-24: a recursive copy (cp -R/-r/-a, rsync -r/-a and their
        combined and long forms) into an exempt directory is refused unless the directory
        is under docs/agent/: an ignored directory may hold a symlink into tracked files
        (rsync -aK writes through node_modules/@scope/pkg into packages/pkg)."""
        repo, outside = self.repo, self.outside
        restore = self.workspace_fixture()
        nm = f"{restore}/node_modules/"
        for command in (
            f"cp -R {nm} node_modules/",
            f"cp -r {nm} node_modules/",
            f"cp -a {nm} node_modules/",
            f"cp -Rf {nm} node_modules/",
            f"cp -av {nm} node_modules/",
            f"cp -pR {nm} node_modules/",
            f"cp -f -R {nm} node_modules/",
            f"cp -v -a {nm} node_modules/",
            f"cp -R {nm} node_modules",
            f"cp -R {restore}/node_modules/@scope/ node_modules/@scope/",
            f"rsync -a {nm} node_modules/",
            f"rsync -r {nm} node_modules/",
            f"rsync -avz {nm} node_modules/",
            f"rsync -aK {nm} node_modules/",
            f"rsync -rlt {nm} node_modules/",
            f"rsync --archive {nm} node_modules/",
            f"rsync --recursive {nm} node_modules/",
            f"rsync -v --archive --delete {nm} node_modules/",
            # an ignored directory without a symlink in it today: the rule is the same
            f"cp -R {outside}/data/ cache/",
            f"cp -R {outside}/data cache",
            f"cp -a {outside}/data/ data/",
            f"rsync -a {outside}/ cache/",
            f"rsync -r {outside}/data/ data",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # beside an allowed write, the recursive copy is the part refused
        for command, part in (
            (
                f"echo x > .env.local && cp -R {nm} node_modules/",
                f"cp -R {nm} node_modules/",
            ),
            (
                f"cp {outside}/x.log data/; rsync -a {nm} node_modules/",
                f"rsync -a {nm} node_modules/",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), part)
        # the ignored directory named absolutely, or reached by cd, from outside
        for command, part in (
            (f"cp -R {nm} {repo}/node_modules/", f"cp -R {nm} {repo}/node_modules/"),
            (f"rsync -a {outside}/ {repo}/cache/", f"rsync -a {outside}/ {repo}/cache/"),
            (f"cp -R {outside}/data/ {repo}/cache/", f"cp -R {outside}/data/ {repo}/cache/"),
            (f"cd {repo} && cp -a {nm} node_modules/", f"cp -a {nm} node_modules/"),
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), part)

    def test_non_recursive_and_docs_agent_copies_stay_allowed(self):
        """Non-recursive copies into an ignored directory and recursive copies into
        docs/agent/ stay free; so does a recursive copy wholly outside the repo."""
        repo, outside = self.repo, self.outside
        restore = self.workspace_fixture()
        self.assertAllowed(
            (
                # not recursive: options other than -R/-r/-a, into ignored directories
                f"cp {outside}/x.log data/",
                f"cp -f {outside}/x.log cache/",
                f"cp -pv {outside}/x.log cache/",
                f"cp {outside}/notes.md node_modules/",
                f"cp {outside}/{{a,b}}.log cache/",
                f"install {outside}/x.log cache/",
                f"install -m 644 {outside}/x.log data/",
                f"ln -s {outside}/x.log data/",
                f"ln -sf {outside}/x.log cache/",
                f"rsync -t {outside}/x.log cache/",
                f"rsync -v {outside}/x.log data/",
                # recursive, into docs/agent/
                f"cp -R {outside}/data/ docs/agent/",
                f"cp -a {outside}/data/ docs/agent/imported/",
                f"cp -Rf {outside}/data docs/agent/",
                f"cp -R {restore}/node_modules/ docs/agent/",
                f"rsync -a {outside}/data/ docs/agent/",
                f"rsync --recursive {outside}/data/ docs/agent/specs/",
                f"rsync -avz {outside}/ docs/agent/",
                # recursive, wholly outside the repo
                f"cp -R {restore}/node_modules/ {outside}/nm-copy/",
                f"rsync -a {repo}/src/ {outside}/backup/",
            )
        )
        self.assertAllowed(
            (
                f"cp -R {outside}/data/ {repo}/docs/agent/",
                f"rsync -a {outside}/data/ {repo}/docs/agent/",
                f"cp -v {outside}/x.log {repo}/cache/",
            ),
            cwd=outside,
        )

    def test_copy_into_a_directory_lands_in_the_destination_directorys_repo(self):
        """Ruling 2026-10-24: the repo a copy into a directory lands in is judged by the
        destination directory, not by dest/<basename of the source>. `cp -R w/worktrees/
        .claude/` writes .claude/ of the main checkout; `cp -R x/link/ .` writes the repo
        root though the tracked `link` points outside every repo."""
        repo, outside = self.repo, self.outside
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.write(repo / ".claude/settings.json", "{}\n")
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE + ".claude/worktrees/\n")
        os.symlink(str(elsewhere), repo / "link")
        self.git(repo, "add", ".claude", ".gitignore", "link")
        self.git(repo, "commit", "-q", "-m", "Settings and a link outside")
        nested = repo / ".claude/worktrees/t-13-x"
        self.git(repo, "worktree", "add", "-q", "-b", "t/13-x", str(nested))
        linked = self.worktree(repo, "t/14-y", "wt14")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        # the sources really would clobber tracked files at the destination
        self.write(outside / "worktrees/settings.json", '{"clobbered": true}\n')
        self.write(outside / "link/README.md", "clobbered\n")
        self.write(outside / "stuff/README.md", "from outside\n")
        self.write(outside / "t-13-x/README.md", "from outside\n")
        for command in (
            # the contents land in .claude/worktrees/ of the main checkout, not in the
            # ticket worktree t-13-x that dest/<basename> names
            f"cp -R {outside}/t-13-x/ .claude/worktrees/",
            f"cp -a {outside}/t-13-x/ {repo}/.claude/worktrees/",
            f"cp -R {outside}/worktrees/ .claude/",
            f"cp -a {outside}/worktrees/ .claude/",
            f"cp -R {outside}/worktrees/ .claude",
            f"cp -R {outside}/worktrees .claude/",
            f"cp -Rf {outside}/worktrees/ ./.claude/",
            f"cp -R {outside}/link/ .",
            f"cp -R {outside}/link/ ./",
            f"cp -a {outside}/link .",
            f"cp -R {outside}/link/ {repo}",
            f"ln -sf {outside}/link .",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        for command in (
            f"cp -R {outside}/worktrees/ {repo}/.claude/",
            f"cp -R {outside}/link/ {repo}",
            f"cp -a {outside}/link/ {repo}/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        command = f"cd {repo} && cp -R {outside}/worktrees/ .claude/"
        self.assertRefusedPart(
            self.bash(command, cwd=outside), f"cp -R {outside}/worktrees/ .claude/"
        )
        # the destination directory really is a ticket worktree or outside every repo
        self.assertAllowed(
            (
                f"cp -R {outside}/worktrees/ .claude/worktrees/t-13-x/",
                f"cp -R {outside}/stuff/ .claude/worktrees/t-13-x/docs/",
                f"cp -R {outside}/stuff/ {linked}/",
                f"rsync -a {outside}/stuff/ {linked}/src/",
                f"cp -R {outside}/stuff/ link/",
                f"cp -R {outside}/link/ {outside}/copy/",
                f"cp -R {outside}/link/ {elsewhere}/",
            )
        )
        self.assertAllowed(
            (
                f"cp -R {outside}/link/ {linked}/",
                f"cp -R {outside}/worktrees/ {nested}/",
                f"cp -R {outside}/stuff/ {elsewhere}/",
            ),
            cwd=outside,
        )

    def test_copy_into_an_exempt_directory_onto_a_symlink_out_of_it_refused(self):
        """Ruling 2026-10-24: a cp, install, ln, rsync or mv into an exempt directory is
        refused when dest/<basename of a source> already exists and resolves outside the
        exemption (BSD cp writes through the ignored cache/app.py into the tracked
        src/app.py). A name that does not exist yet, or a plain file inside the exempt
        directory, keeps the directory rule."""
        repo, outside = self.repo, self.outside
        os.symlink("../src/app.py", repo / "cache/app.py")
        os.symlink("../README.md", repo / "data/README.md")
        os.symlink("../keep.log", repo / "data/keep.log")  # tracked, ignored by name
        os.symlink("../../README.md", repo / "docs/agent/x.md")
        os.symlink("../build.log", repo / "cache/alias.log")  # ignored and untracked
        self.assertEqual(
            os.path.realpath(repo / "cache/app.py"), str(repo / "src/app.py")
        )
        for name in (
            "app.py", "README.md", "keep.log", "x.md", "x.log",
            "new.py", "blob", "a.csv", "alias.log", "handoff.md",
        ):
            self.write(outside / name, "from outside\n")
        for command in (
            f"cp {outside}/app.py cache/",
            f"cp -f {outside}/app.py cache",
            f"install {outside}/app.py cache/",
            f"install -m 644 {outside}/README.md data/",
            f"ln -sf {outside}/app.py cache/",
            f"rsync -t {outside}/app.py cache/",
            f"mv {outside}/app.py cache/",
            f"cp {outside}/keep.log data/",
            # one source of several reaches a tracked file
            f"cp {outside}/x.log {outside}/README.md data/",
            f"cp {outside}/blob {outside}/app.py cache/",
            # docs/agent/ is exempt, but x.md in it leads to the tracked README.md
            f"cp {outside}/x.md docs/agent/",
            f"mv {outside}/x.md docs/agent/",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        for command in (
            f"cp {outside}/app.py {repo}/cache/",
            f"install {outside}/README.md {repo}/data/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        # absent names, plain files in the directory, links that stay inside it
        self.assertAllowed(
            (
                f"cp {outside}/new.py cache/",
                f"cp {outside}/blob cache/",
                f"cp -f {outside}/a.csv data/",
                f"cp {outside}/blob {outside}/new.py cache/",
                f"install {outside}/blob cache/",
                f"ln -sf {outside}/blob cache/",
                f"rsync -t {outside}/a.csv data/",
                f"mv {outside}/new.py data/",
                f"cp {outside}/alias.log cache/",
                f"cp {outside}/handoff.md docs/agent/",
                f"cp {outside}/new.py docs/agent/",
            )
        )
        self.assertAllowed(
            (f"cp {outside}/blob {repo}/cache/", f"cp {outside}/new.py {repo}/data/"),
            cwd=outside,
        )

    def test_recursive_copy_to_a_new_name_keeps_the_named_rule(self):
        """A recursive copy to a name that does not exist yet creates it; it is a named
        destination, not a copy into a directory, and keeps the name's rule."""
        outside = self.outside
        self.write(outside / "data/README.md", "from outside\n")
        self.assertFalse((self.repo / "cache/restored").exists())
        self.assertAllowed(
            (
                f"cp -R {outside}/data cache/restored",
                f"cp -a {outside}/data data/copy",
            )
        )
        for command in (f"cp -R {outside}/data src/restored", f"cp -a {outside}/data newdir"):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)

    def test_literal_joined_at_run_time_through_join_or_concat_refused(self):
        """`.join(` and `.concat(` build a path at run time as `+` does: a literal beside a
        run-time value in them does not say where the program writes."""
        repo = self.repo
        part = "python3 - <<'PY'"
        for program in (
            "import os\nopen(''.join([os.getcwd(), '/out.json']), 'w')",
            # 'out.json' is a bare name and resolves into the repo on its own as well
            "import os\nopen('/'.join([os.getcwd(), 'out.json']), 'w')",
        ):
            with self.subTest(program=program):
                self.assertRefusedPart(self.bash(heredoc(program), cwd=repo), part)
        command = (
            """node -e "require('fs').writeFileSync(process.cwd().concat('/out.json'), 'x')\""""
        )
        self.assertRefusedPart(self.bash(command), command)
        # joins of literals only, and joins that build no path, stay allowed
        self.assertAllowed(
            (
                heredoc("open(''.join(['/private/tmp/x', '/out.json']), 'w')"),
                heredoc(
                    "import sys\nnames = sys.argv[1:]\nprint(', '.join(names))\n"
                    f"open('{OUTSIDE_JSON}', 'w').write('x')"
                ),
            )
        )


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


if __name__ == "__main__":
    unittest.main()
