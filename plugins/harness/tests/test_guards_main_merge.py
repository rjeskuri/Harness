"""tests_first.py and harness-red status after a merge of main: when the red record stays current, and the hook's time budget.

Split from test_guards.py; the fixtures are in guards_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_main_merge.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403


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

    def ticket(
        self, name: str, base: str = BASE_TESTS, author=None, object_format: str | None = None
    ) -> tuple[Path, Path]:
        """(main checkout, ticket worktree) after the red run and the coder's commit.
        Main's test files hold `base`; the test-author applies `author` to each (default:
        test_case_5's assert becomes BRANCH_LINE_5). `object_format` "sha256" makes the
        repo a SHA-256 one."""
        if author is None:
            author = lambda t: replaced(t, MAIN_LINE_5, BRANCH_LINE_5)  # noqa: E731
        repo = self.make_repo(name, object_format=object_format)
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

    # ---- #275: a fix round's test commit, re-proved with harness-red reprove

    def test_harness_red_reprove_after_a_fix_round_test_commit_allows_source_again(self):
        # PR #274's shape: after the coder's source commit, a fix round commits a test of
        # the branch's own (coverage that passes at HEAD). The record is stale and check
        # refuses (source changed); reprove proves the tests as they are now against
        # main's code at the merge base, and source edits are allowed again.
        repo, wt = self.ticket("reprove")
        path = wt / "tests/test_app.py"
        self.write(path, path.read_text() + "\n\ndef test_login_again():\n    assert login() == 'ok'\n")
        self.commit_all(wt, "coverage for login")
        self.assertEqual(self.status(wt), "current: no")
        self.assertSourceDenied(wt)
        r = self.harness_red(wt, "check")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Source already changed before the red run", r.stderr)
        r = self.harness_red(wt, "reprove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("red (assertion)", r.stdout)
        record = json.loads(self.record_file(repo).read_bytes())
        self.assertEqual(record["base_sha"], self.git(wt, "merge-base", "HEAD", "main"))
        self.assertEqual(record["test_files"], sorted(MERGE_TEST_FILES))
        self.assertEqual(record["test_hash"], content_hash(wt, record["test_files"]))
        self.assertEqual(self.status(wt), "current: yes")
        self.assertSourceAllowed(wt)

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

    # ---- re-check round (#231): ort vs Myers, and records without a side file

    def test_a_clean_merge_that_myers_would_conflict_on_keeps_source_allowed(self):
        repo, wt = self.ticket("diverge", DIVERGE_TESTS, diverge_author)
        self.on_main(repo, {"tests/test_app.py": diverge_main})
        self.merge_cleanly(wt)
        path = wt / "tests/test_app.py"
        self.assertEqual(path.read_text(), DIVERGE_MERGED)
        # the same three inputs through Myers merge-file do not give that file
        code, myers = myers_merge(
            self.tmp, diverge_author(DIVERGE_TESTS), DIVERGE_TESTS, diverge_main(DIVERGE_TESTS), self.env
        )
        self.assertFalse(code == 0 and myers == DIVERGE_MERGED, (code, myers))
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")
        # a test change of the branch's own after that merge still counts
        head = self.git(wt, "rev-parse", "HEAD")
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.git(wt, "reset", "-q", "--hard", head)
                self.write(path, replaced(DIVERGE_MERGED, BRANCH_LINE_5, "    assert login() == 'hi'\n"))
                if committed:
                    self.commit_all(wt, "the branch edits its test")
                self.assertSourceDenied(wt)
                self.assertEqual(self.status(wt), "current: no")
        self.git(wt, "reset", "-q", "--hard", head)
        self.assertSourceAllowed(wt)

    def side_file(self, repo: Path) -> Path:
        return self.record_file(repo).parent.parent / "red-files" / self.record_file(repo).name

    def test_a_record_without_its_side_file_survives_a_merge_of_main(self):
        # a record from before the side file: the bytes are recovered from the merge
        repo, wt = self.ticket("no-side-file")
        side = self.side_file(repo)
        self.assertTrue(side.is_file(), "harness-red check writes the side file")
        side.unlink()
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        self.assertFalse(side.exists())
        self.assertSourceAllowed(wt)
        self.assertEqual(self.status(wt), "current: yes")
        head = self.git(wt, "rev-parse", "HEAD")
        path = wt / "tests/test_app.py"
        own_test = "def test_case_5():\n" + BRANCH_LINE_5
        for name, change in (
            ("own-line", lambda t: replaced(t, BRANCH_LINE_5, "    assert login() == 'hi'\n")),
            ("own-test-moved", lambda t: moved_to_end(t, own_test)),
        ):
            for committed in (False, True):
                with self.subTest(case=name, committed=committed):
                    self.git(wt, "reset", "-q", "--hard", head)
                    self.write(path, change(path.read_text()))
                    if committed:
                        self.commit_all(wt, "the branch edits its tests")
                    self.assertSourceDenied(wt)
                    self.assertEqual(self.status(wt), "current: no")
        self.git(wt, "reset", "-q", "--hard", head)
        self.assertSourceAllowed(wt)

    def test_a_hand_written_record_keeps_the_whole_file_check_across_a_merge(self):
        # a regression guard: base_sha "0" * 40 and no side file, as before #231
        repo, wt = self.ticket("hand-written")
        self.side_file(repo).unlink()
        record = json.loads(self.record_file(repo).read_text())
        record["base_sha"] = "0" * 40
        self.write(self.record_file(repo), json.dumps(record))
        self.assertSourceAllowed(wt)  # the files still hash to test_hash
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        self.assertSourceDenied(wt)
        self.assertEqual(self.status(wt), "current: no")

    # ---- ultrareview round (#231): SHA-256 repositories, and the hook's deadline

    def require_sha256(self) -> None:
        probe = self.tmp / "sha256-probe"
        r = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", str(probe)],
            capture_output=True,
            env=self.env,
        )
        if r.returncode != 0:
            self.skipTest("this git cannot make a SHA-256 repository")

    def test_a_merge_of_main_keeps_source_allowed_in_a_sha256_repo(self):
        self.require_sha256()
        own_test = "def test_case_5():\n" + BRANCH_LINE_5
        variants = {
            # main edits other hunks of both files: merge-file replays it
            "other-hunks": (
                BASE_TESTS,
                None,
                {
                    "tests/test_app.py": lambda t: replaced(
                        t, "assert 0 + 0 == 0", "assert 0 * 0 == 0"
                    ),
                    "tests/test_util.py": lambda t: "# main's header\n" + t,
                },
                (
                    ("own-line", lambda t: replaced(t, BRANCH_LINE_5, "    assert login() == 'hi'\n")),
                    ("own-test-moved", lambda t: moved_to_end(t, own_test)),
                ),
            ),
            # a merge only git's own (ort) gives: the synthetic commit, in SHA-256
            "ort-only": (
                DIVERGE_TESTS,
                diverge_author,
                {"tests/test_app.py": diverge_main, "tests/test_util.py": diverge_main},
                (
                    ("own-line", lambda t: replaced(t, BRANCH_LINE_5, "    assert login() == 'hi'\n")),
                ),
            ),
        }
        for name, (base, author, changes, branch_edits) in variants.items():
            with self.subTest(variant=name):
                repo, wt = self.ticket(f"sha256-{name}", base, author, object_format="sha256")
                self.assertEqual(self.git(wt, "rev-parse", "--show-object-format"), "sha256")
                record = json.loads(self.record_file(repo).read_text())
                self.assertRegex(record["base_sha"], r"^[0-9a-f]{64}$")
                self.on_main(repo, changes)
                self.merge_cleanly(wt)
                # the merge brought main's edits in: the files no longer hash to test_hash
                self.assertNotEqual(
                    content_hash(wt, record["test_files"]), record["test_hash"]
                )
                self.assertSourceAllowed(wt)
                self.assertEqual(self.status(wt), "current: yes")
                head = self.git(wt, "rev-parse", "HEAD")
                path = wt / "tests/test_app.py"
                for edit, change in branch_edits:
                    for committed in (False, True):
                        with self.subTest(variant=name, edit=edit, committed=committed):
                            self.git(wt, "reset", "-q", "--hard", head)
                            self.write(path, change(path.read_text()))
                            if committed:
                                self.commit_all(wt, "the branch edits its tests")
                            self.assertSourceDenied(wt)
                            self.assertEqual(self.status(wt), "current: no")
                self.git(wt, "reset", "-q", "--hard", head)
                self.assertSourceAllowed(wt)

    def hook_with_slow_git(self, payload: dict, delay: float, hang=None):
        """(deny reason or None, seconds the hook took, git calls) for tests_first.py run as
        its own __main__, in this process, with every git call taking `delay` seconds of a
        fake clock (SlowGit) and the calls `hang` picks never answering. harness_lib is
        imported afresh under the fake clock."""
        ours = ("harness_lib", "branch_guard")
        saved = {
            k: m for k, m in sys.modules.items() if k in ours or k.startswith("harness_lib.")
        }
        for k in saved:
            del sys.modules[k]
        slow = SlowGit(delay, hang)
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
                stack.enter_context(mock.patch.object(subprocess, "run", slow.run))
                stack.enter_context(mock.patch.object(time, "monotonic", slow.monotonic))
                stack.enter_context(mock.patch.object(time, "perf_counter", slow.perf_counter))
                stack.enter_context(mock.patch.object(time, "time", slow.time))
                stack.enter_context(mock.patch.object(sys, "stdin", stdin))
                stack.enter_context(mock.patch.object(sys, "dont_write_bytecode", True))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                start = slow.monotonic()
                try:
                    runpy.run_path(str(HOOKS / "tests_first.py"), run_name="__main__")
                except SystemExit as exc:
                    self.assertIn(exc.code, (0, None), err.getvalue())
                took = slow.monotonic() - start
        finally:
            for k in [k for k in sys.modules if k in ours or k.startswith("harness_lib.")]:
                del sys.modules[k]
            sys.modules.update(saved)
        r = types.SimpleNamespace(stdout=out.getvalue(), stderr=err.getvalue())
        return self.decision(r), took, slow.calls

    def test_the_hook_decides_within_its_timeout_when_every_git_call_is_slow(self):
        timeouts = hook_timeouts("tests_first.py")
        self.assertEqual(set(timeouts), {"Bash", "Edit|Write|NotebookEdit"}, timeouts)
        # a stale record after a merge of main: the branch moved its own test afterwards
        repo, wt = self.ticket("slow-git")
        self.on_main(repo, {"tests/test_app.py": lambda t: "# main's header\n" + t})
        self.merge_cleanly(wt)
        current_head = self.git(wt, "rev-parse", "HEAD")
        path = wt / "tests/test_app.py"
        self.write(path, moved_to_end(path.read_text(), "def test_case_5():\n" + BRANCH_LINE_5))
        self.commit_all(wt, "the branch moves its test")
        stale_head = self.git(wt, "rev-parse", "HEAD")
        calls = {
            "Edit|Write|NotebookEdit": self.payload(
                "Edit",
                {"file_path": str(wt / "src/app.py"), "old_string": "a", "new_string": "b"},
                wt,
                "harness:coder",
            ),
            "Bash": self.payload(
                "Bash", {"command": "printf '\\n' >> src/app.py"}, wt, "harness:coder"
            ),
        }
        # with git at full speed the in-process run gives what the hook process gives
        for matcher, payload in calls.items():
            with self.subTest(matcher=matcher, delay=0):
                reason, took, _ = self.hook_with_slow_git(payload, 0.0)
                self.assertIsNotNone(reason)
                self.assertIn(CHANGED, reason)
                self.assertEqual(
                    reason, self.decision(self.run_hook("tests_first.py", payload))
                )
        # Every git call slow, none timing out: the repo lookup succeeds, so the hook knows
        # the branch, and it must deny the stale record before hooks.json's timeout.
        # (2.9 is the slowest at which main_checkout's two calls still fit its one budget.)
        for delay in (0.5, 1.0, 1.4, 2.0, 2.5, 2.9):
            for matcher, payload in calls.items():
                limit = timeouts[matcher] - STARTUP_ALLOWANCE
                with self.subTest(record="stale", matcher=matcher, delay=delay):
                    reason, took, git_calls = self.hook_with_slow_git(payload, delay)
                    self.assertGreaterEqual(len(git_calls), 2, git_calls)
                    self.assertLess(took, limit, git_calls)
                    self.assertIsNotNone(reason, git_calls)  # never let through
        # a record that is current after the merge: allowed or denied, but decided in time
        self.git(wt, "reset", "-q", "--hard", current_head)
        self.assertNotEqual(current_head, stale_head)
        for delay in (0.5, 1.4, 2.0, 2.9):
            for matcher, payload in calls.items():
                limit = timeouts[matcher] - STARTUP_ALLOWANCE
                with self.subTest(record="current", matcher=matcher, delay=delay):
                    _, took, git_calls = self.hook_with_slow_git(payload, delay)
                    self.assertLess(took, limit, git_calls)

    # ---- a git call that never answers (#285)

    def source_writes(self, wt: Path) -> dict:
        """name -> (payload, hooks.json matcher) for source writes in `wt`: Edit of a tracked
        and of a new source file, by the main session and by the coder, and the coder's Bash
        append to a source file."""
        writes = {}
        for agent in (None, "harness:coder"):
            for rel in ("src/app.py", "src/new_module.py"):
                tool_input = {"file_path": str(wt / rel), "old_string": "a", "new_string": "b"}
                writes[f"Edit {rel} as {agent}"] = (
                    self.payload("Edit", tool_input, wt, agent), "Edit|Write|NotebookEdit"
                )
        writes["Bash append to src/app.py as harness:coder"] = (
            self.payload("Bash", {"command": "printf '\\n' >> src/app.py"}, wt, "harness:coder"),
            "Bash",
        )
        return writes

    def assertTimedOutAsPicked(self, git_calls: list) -> None:
        """The run's git calls: at least one cut at its timeout, and every one carried one."""
        statuses = [status for _, status in git_calls]
        self.assertIn("timed out", statuses, git_calls)
        self.assertNotIn("hung, no timeout", statuses, git_calls)

    def assertRefusedAsTooSlow(self, reason, git_calls: list) -> None:
        self.assertIsNotNone(reason, git_calls)  # never let through
        self.assertIn("too slow", reason.lower(), git_calls)
        self.assertIn("retry", reason.lower(), git_calls)

    def test_a_ticket_branch_refuses_a_source_write_when_the_main_checkout_lookup_times_out(self):
        """Once the first git call has named a ticket branch, the branch is known: the main
        checkout's lookup (or any later git call) running out of time refuses the write, with
        a retry, instead of letting it through (docs/design.md: only the repository lookup that
        names the branch fails open). The record is current, so at full speed every write is
        allowed: a refusal here is the timeout's alone."""
        timeouts = hook_timeouts("tests_first.py")
        repo, wt = self.ticket("slow-lookup")
        writes = self.source_writes(wt)
        for name, (payload, matcher) in writes.items():
            hangs = {
                "rev-parse --git-common-dir": lambda argv, i: "--git-common-dir" in argv,
                "every call after the first": lambda argv, i: i >= 1,
            }
            limit = timeouts[matcher] - STARTUP_ALLOWANCE
            # at full speed, and slow but in time: the current record allows the write
            for delay in (0.0, 1.0):
                with self.subTest(write=name, hang=None, delay=delay):
                    reason, _, git_calls = self.hook_with_slow_git(payload, delay)
                    self.assertIsNone(reason, git_calls)
                    self.assertGreaterEqual(len(git_calls), 2, git_calls)
            _, _, fast_calls = self.hook_with_slow_git(payload, 0.0)
            # each git call after the branch lookup, alone, never answering
            for k in range(1, len(fast_calls)):
                hangs[f"call {k} ({' '.join(fast_calls[k][0][3:6])})"] = (
                    lambda argv, i, k=k: i == k
                )
            for hang_name, hang in hangs.items():
                for delay in (0.0, 1.0, 2.0):
                    with self.subTest(write=name, hang=hang_name, delay=delay):
                        reason, took, git_calls = self.hook_with_slow_git(payload, delay, hang)
                        self.assertTimedOutAsPicked(git_calls)
                        self.assertRefusedAsTooSlow(reason, git_calls)
                        self.assertLess(took, limit, git_calls)

    def test_the_branch_lookup_timing_out_still_allows(self):
        """The first git call names the branch; when it does not answer the branch is unknown,
        and the hook fails open (docs/design.md), on a ticket branch with a current record and
        with a stale one alike."""
        repo, wt = self.ticket("slow-branch-lookup")
        first = lambda argv, i: i == 0  # noqa: E731
        everything = lambda argv, i: True  # noqa: E731
        for record in ("current", "stale"):
            if record == "stale":  # the branch edits its own test after the red run
                path = wt / "tests/test_app.py"
                edited = BRANCH_LINE_5.rstrip("\n") + "  # edited\n"
                self.write(path, replaced(path.read_text(), BRANCH_LINE_5, edited))
                self.commit_all(wt, "the branch edits its test")
            for name, (payload, _) in self.source_writes(wt).items():
                for hang_name, hang in (("the first call", first), ("every call", everything)):
                    with self.subTest(record=record, write=name, hang=hang_name):
                        reason, _, git_calls = self.hook_with_slow_git(payload, 0.0, hang)
                        self.assertTimedOutAsPicked(git_calls)
                        self.assertEqual(git_calls[0][1], "timed out", git_calls)
                        self.assertIsNone(reason, git_calls)

    def test_off_a_ticket_branch_a_timed_out_main_checkout_lookup_still_allows(self):
        """Off a ticket branch the rules do not apply, so a later git call that never answers
        changes nothing: the write is allowed, in a linked worktree on another branch and in
        the main checkout on main."""
        repo = self.make_repo("slow-other")
        wt = self.worktree(repo, branch="feature/login", name="slow-other-wt")
        hangs = {
            "rev-parse --git-common-dir": lambda argv, i: "--git-common-dir" in argv,
            "every call after the first": lambda argv, i: i >= 1,
        }
        for where, root in (("feature/login worktree", wt), ("main checkout", repo)):
            for name, (payload, _) in self.source_writes(root).items():
                with self.subTest(where=where, write=name, hang=None):
                    reason, _, git_calls = self.hook_with_slow_git(payload, 0.0)
                    self.assertIsNone(reason, git_calls)
                for hang_name, hang in hangs.items():
                    for delay in (0.0, 2.0):
                        with self.subTest(where=where, write=name, hang=hang_name, delay=delay):
                            reason, _, git_calls = self.hook_with_slow_git(payload, delay, hang)
                            # (a hook that skips the lookup off a ticket branch is fine too)
                            self.assertNotIn("hung, no timeout", [s for _, s in git_calls])
                            self.assertIsNone(reason, git_calls)


if __name__ == "__main__":
    unittest.main()
