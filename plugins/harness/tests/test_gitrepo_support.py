"""Tests for gitrepo_support: the shared template repository and its per-test copies.

A test module that copies one prepared git repository per test (test_hooks.py today) builds
it with ``make_template(path, env)`` and copies it with ``copy_repo(src, dst)``. The copy
must never fail on a transient ``.git/**/*.lock`` (git's own background maintenance once
finished mid-copy and failed a CI run, #365), and no background maintenance may run in a
template or its copies in the first place.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_gitrepo_support.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gitrepo_support import copy_repo, make_template

SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}
# Locks git takes (and drops) inside .git; each would break a copy if it vanished mid-copy,
# and each but the first would break a commit in a copy that kept it.
PLANTED_LOCKS = (
    ".git/objects/maintenance.lock",
    ".git/index.lock",
    ".git/refs/heads/main.lock",
    ".git/gc.pid.lock",
)
# What a git trace shows when a command starts background maintenance.
MAINTENANCE_MARKERS = ("maintenance run", "gc --auto")


class _Listing:
    """A finished directory listing that behaves like the iterator os.scandir returns."""

    def __init__(self, entries):
        self._entries = list(entries)
        self._it = iter(self._entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        pass


def vanishing_scandir(root: Path, names: set):
    """An os.scandir that deletes each file under root named in names right after listing it.

    That is the race from #365: the copy lists a lock, git's maintenance finishes and removes
    it, and the copy then tries to read a file that is gone.
    """
    real_scandir = os.scandir
    root = str(root)
    gone: list = []

    def fake(*args, **kwargs):
        it = real_scandir(*args, **kwargs)
        try:
            entries = list(it)
        finally:
            it.close()
        for entry in entries:
            path = os.path.abspath(entry.path)
            if entry.name in names and path.startswith(root + os.sep) and os.path.exists(path):
                os.remove(path)
                gone.append(path)
        return _Listing(entries)

    return fake, gone


class GitRepoSupportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-gitrepo-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(
            GIT_IDENTITY,
            HOME=str(self.home),
            PATH=SYSTEM_PATH,
            LANG="en_US.UTF-8",
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )

    def git(self, repo: Path, *args: str, env: dict | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
        )
        self.assertEqual(
            proc.returncode, 0, f"git {' '.join(args)} in {repo} failed: {proc.stderr}"
        )
        return proc.stdout

    def template(self, name: str = "template") -> Path:
        path = self.tmp / name
        make_template(path, dict(self.env))
        return path

    def assert_working_repo(self, repo: Path, subjects: list) -> None:
        """The repository answers git, is clean, sits on main and has exactly these commits."""
        self.assertEqual(self.git(repo, "symbolic-ref", "HEAD").strip(), "refs/heads/main")
        self.assertEqual(self.git(repo, "log", "--format=%s").splitlines(), subjects)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        self.assertEqual(self.git(repo, "show", "HEAD:README.md"), "hello\n")
        self.git(repo, "fsck", "--no-progress")

    def assert_no_maintenance(self, trace: Path) -> None:
        text = trace.read_text() if trace.exists() else ""
        for marker in MAINTENANCE_MARKERS:
            self.assertNotIn(marker, text, f"background maintenance started:\n{text}")

    def commit_new_file(self, repo: Path, env: dict | None = None) -> str:
        subject = "change " + uuid.uuid4().hex[:8]
        (repo / f"{subject.split()[1]}.txt").write_text(subject + "\n")
        self.git(repo, "add", "-A", env=env)
        self.git(repo, "commit", "-q", "-m", subject, env=env)
        return subject


class MakeTemplateTests(GitRepoSupportTestCase):
    def test_template_is_one_commit_of_readme_on_main(self) -> None:
        repo = self.template()
        self.assert_working_repo(repo, ["Initial commit"])
        self.assertEqual(self.git(repo, "ls-files").splitlines(), ["README.md"])
        self.assertEqual((repo / "README.md").read_text(), "hello\n")

    def test_template_config_turns_background_maintenance_off(self) -> None:
        repo = self.template()
        # Read the way git reads it: the home and system configs are empty here, so this is
        # the repository's own config, which every copy carries with it.
        self.assertEqual(self.git(repo, "config", "--get", "gc.auto").strip(), "0")
        self.assertEqual(
            self.git(repo, "config", "--bool", "--get", "maintenance.auto").strip(), "false"
        )

    def test_building_the_template_starts_no_background_maintenance(self) -> None:
        trace = self.tmp / "build.trace"
        path = self.tmp / "template"
        make_template(path, dict(self.env, GIT_TRACE=str(trace)))
        self.assertTrue(trace.exists(), "the env given to make_template must reach git")
        self.assertIn("commit", trace.read_text())
        self.assert_no_maintenance(trace)
        self.assertFalse((path / ".git" / "gc.log").exists())
        self.assertFalse((path / ".git" / "objects" / "maintenance.lock").exists())

    def test_a_commit_in_the_template_starts_no_background_maintenance(self) -> None:
        repo = self.template()
        trace = self.tmp / "commit.trace"
        subject = self.commit_new_file(repo, env={"GIT_TRACE": str(trace)})
        self.assertIn("commit", trace.read_text())
        self.assert_no_maintenance(trace)
        self.assertFalse((repo / ".git" / "gc.log").exists())
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s").strip(), subject)

    def test_the_control_a_plain_repo_does_start_maintenance(self) -> None:
        # Shows the trace check above can fail: a repository without the config starts
        # `git maintenance run --auto` after a commit.
        repo = self.tmp / "plain"
        repo.mkdir()
        self.git(repo, "init", "-q")
        trace = self.tmp / "plain.trace"
        self.commit_new_file(repo, env={"GIT_TRACE": str(trace)})
        text = trace.read_text()
        self.assertTrue(any(m in text for m in MAINTENANCE_MARKERS), text)


class CopyRepoTests(GitRepoSupportTestCase):
    def test_copy_of_a_template_with_planted_locks_is_a_working_repository(self) -> None:
        for lock in PLANTED_LOCKS:
            with self.subTest(lock=lock):
                src = self.template("template-" + Path(lock).stem.replace(".", "-"))
                subject = self.commit_new_file(src)
                (src / lock).write_text("held\n")
                dst = self.tmp / ("copy-" + src.name)
                copy_repo(src, dst)
                self.assertTrue((src / lock).exists(), "the copy must leave the source alone")
                self.assertFalse((dst / lock).exists(), "a stale lock must not reach the copy")
                self.assertEqual(
                    self.git(dst, "log", "--format=%s").splitlines(),
                    [subject, "Initial commit"],
                )
                self.assertEqual(self.git(dst, "symbolic-ref", "HEAD").strip(), "refs/heads/main")
                self.assertEqual(self.git(dst, "status", "--porcelain"), "")
                # A lock the copy kept would make this fail ("Unable to create ... .lock").
                later = self.commit_new_file(dst)
                self.assertEqual(self.git(dst, "log", "-1", "--format=%s").strip(), later)
                # The copy is independent: the source did not move.
                self.assertEqual(self.git(src, "log", "-1", "--format=%s").strip(), subject)

    def test_copy_of_a_clean_template_is_the_same_repository(self) -> None:
        src = self.template()
        dst = self.tmp / "proj"
        copy_repo(src, dst)
        self.assert_working_repo(dst, ["Initial commit"])
        self.assertEqual(
            self.git(dst, "rev-parse", "HEAD"), self.git(src, "rev-parse", "HEAD")
        )

    def test_copy_carries_the_maintenance_off_config(self) -> None:
        dst = self.tmp / "proj"
        copy_repo(self.template(), dst)
        self.assertEqual(self.git(dst, "config", "--get", "gc.auto").strip(), "0")
        self.assertEqual(
            self.git(dst, "config", "--bool", "--get", "maintenance.auto").strip(), "false"
        )
        trace = self.tmp / "copy.trace"
        self.commit_new_file(dst, env={"GIT_TRACE": str(trace)})
        self.assertIn("commit", trace.read_text())
        self.assert_no_maintenance(trace)

    def test_a_lock_that_vanishes_during_the_copy_does_not_fail_it(self) -> None:
        for lock in PLANTED_LOCKS:
            with self.subTest(lock=lock):
                src = self.template("vanish-" + Path(lock).stem.replace(".", "-"))
                subject = self.commit_new_file(src)
                (src / lock).write_text("held\n")
                dst = self.tmp / ("copy-" + src.name)
                fake, gone = vanishing_scandir(src / ".git", {Path(lock).name})
                with mock.patch("os.scandir", fake):
                    copy_repo(src, dst)
                self.assertEqual(gone, [str(src / lock)], "the simulated race did not happen")
                self.assertEqual(
                    self.git(dst, "log", "--format=%s").splitlines(),
                    [subject, "Initial commit"],
                )
                self.assertEqual(self.git(dst, "status", "--porcelain"), "")
                self.commit_new_file(dst)

    def test_a_non_lock_file_that_vanishes_during_the_copy_still_fails_it(self) -> None:
        # Only git's transient locks are forgiven; any other file lost mid-copy is a real
        # error and must not be swallowed.
        for rel in (".git/objects/stray-object", ".git/description", "notes.txt"):
            with self.subTest(file=rel):
                src = self.template("stray-" + Path(rel).name.replace(".", "-"))
                (src / rel).write_text("data\n")
                dst = self.tmp / ("copy-" + src.name)
                fake, gone = vanishing_scandir(src, {Path(rel).name})
                with mock.patch("os.scandir", fake):
                    with self.assertRaises(OSError):
                        copy_repo(src, dst)
                self.assertEqual(gone, [str(src / rel)])

    def test_lock_files_outside_git_are_copied(self) -> None:
        src = self.template()
        body = "lock " + uuid.uuid4().hex + "\n"
        (src / "Cargo.lock").write_text(body)
        (src / "sub").mkdir()
        (src / "sub" / "poetry.lock").write_text(body * 2)
        dst = self.tmp / "proj"
        copy_repo(src, dst)
        self.assertEqual((dst / "Cargo.lock").read_text(), body)
        self.assertEqual((dst / "sub" / "poetry.lock").read_text(), body * 2)

    def test_symlinks_are_copied_as_symlinks(self) -> None:
        src = self.template()
        os.symlink("README.md", src / "link-to-readme")
        os.symlink("/nonexistent/target-" + uuid.uuid4().hex, src / "dangling")
        dst = self.tmp / "proj"
        copy_repo(src, dst)
        self.assertTrue((dst / "link-to-readme").is_symlink())
        self.assertEqual(os.readlink(dst / "link-to-readme"), "README.md")
        self.assertTrue((dst / "dangling").is_symlink())
        self.assertEqual(os.readlink(dst / "dangling"), os.readlink(src / "dangling"))


if __name__ == "__main__":
    unittest.main()
