"""tests_first.py: the role rules, the red record, no-tests and refactor tickets, Bash writes and bulk git forms.

Split from test_guards.py; the fixtures are in guards_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_tests_first.py" -v
"""

from __future__ import annotations

import random
import string
import unittest
import uuid

from guards_support import *  # noqa: F401,F403

# #292: the denial of a red record without a failure kind, word for word as docs/design.md's
# tests_first step 4 quotes it: like the stale denial (CHANGED), it names reprove.
NO_KIND = (
    "The red record for t/12 does not say how the tests failed; run harness-red check "
    "again, or harness-red reprove once the branch has source changes."
)
# Names a real project has above its tests/ (none of the fixtures' literal parents, so a made-up path never repeats
# one). Mixed with the made-up shapes below: a pattern that names the real ones (src/tests/**) misses the made-up
# ones, one that names a made-up shape (d*/**/tests/**) misses the real ones; only every tests/ directory passes.
REAL_PARENTS = ("src", "app", "lib", "services", "packages", "internal", "core", "api", "cli", "backend", "server")


def real_parent() -> str:
    """One directory name a real project uses, drawn for this run."""
    return random.choice(REAL_PARENTS)


def segment() -> str:
    """One directory name made up for this run, in no fixed shape: a real project's, one with a suffix, or hex digits
    after any letter, upper or lower, or a digit, with a - or . inside. None contains "test", "spec" or "docs", so no
    pattern matches one by its name, and no list or shape written in advance names them all."""
    h = uuid.uuid4().hex[:8]
    return random.choice((
        real_parent(),
        real_parent() + "_" + h[:4],
        random.choice(string.ascii_lowercase) + h,
        random.choice(string.ascii_uppercase) + h[:6],
        random.choice(string.digits) + h[:3] + "-" + h[3:],
        "v" + random.choice(string.digits) + "." + h[:4],
    ))


def generated(n: int) -> str:
    """`n` directory names made up for this run (see segment), /-joined ("" for none)."""
    return "/".join(segment() for _ in range(n))


# #348: files below a tests/ directory under the top (support modules, fakes, helpers, fixtures), Harness's own
# layout first, then parents made up for this run. Under the default patterns each is a test.
NESTED_TESTS = (
    "plugins/harness/tests/pr_support.py",
    "plugins/harness/tests/guards_support.py",
    "plugins/harness/tests/fakegh.py",
    "pkg/tests/x_support.py",
    "pkg/tests/fakegh.py",
    "svc/api/tests/helpers/builders.py",
    "svc/api/tests/fixtures/rows.json",
    f"{generated(1)}/tests/fakegh.py",
    f"{generated(3)}/tests/{generated(2)}/y_support.py",
    f"{real_parent()}/tests/x_support.py",  # a real name right above tests/
    f"{generated(1)}/{real_parent()}/tests/{generated(1)}/fakegh.py",
    f"{generated(random.randint(15, 40))}/tests/deep_support.py",  # a depth drawn per run, past any fixed list
)
# Source outside every tests/ directory, whatever the name says: a _support.py or a fakegh.py elsewhere included.
NOT_UNDER_TESTS = (
    "plugins/harness/lib/x.py",
    "plugins/harness/lib/harness_lib/x_support.py",
    "plugins/harness/fakegh.py",
    "pkg/testsuite/x_support.py",
    "pkg/tests_helper.py",
    "src/compat_support.py",
    f"{generated(2)}/testsuite/x_support.py",
    f"{generated(1)}/tests_helper.py",
    f"{generated(3)}/{generated(1)}_support.py",
    f"{real_parent()}/{generated(1)}_support.py",
    f"{real_parent()}/{real_parent()}/testsuite/fakegh.py",
)
# This checkout: its tracked settings and its own support modules and fake, by their real names.
CHECKOUT = HOOKS.parent.parent.parent
CHECKOUT_TESTS = HOOKS.parent / "tests"


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
                self.assertEqual(self.edit("src/app.py", "harness:coder"), NO_KIND)
                self.assertEqual(self.edit("src/app.py"), NO_KIND)  # the main session too
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

    # ---- test configuration on a no-tests ticket (#281)

    def change(self, tool: str, rel: str, agent=None, root: Path | None = None, env=None):
        """The deny reason for an Edit or a Write of `rel` under the worktree (or `root`)."""
        path = str((root or self.wt) / rel)
        if tool == "Write":
            tool_input = {"file_path": path, "content": "{}\n"}
        else:
            tool_input = {"file_path": path, "old_string": "a", "new_string": "b"}
        payload = self.payload(tool, tool_input, self.repo, agent)
        return self.decision(self.run_hook("tests_first.py", payload, env))

    def test_coder_may_edit_test_configuration_on_a_no_tests_ticket(self):
        # no red record: test configuration is not source, and no-tests needs none anyway
        for n, fields in enumerate(NO_TESTS_CACHES):
            self.cache(**fields)
            rels = CONFIG_NOT_TESTS if n == 0 else (
                ".claude/settings.json", "pyproject.toml", "web/package.json"
            )
            for rel in rels:
                for tool in ("Edit", "Write"):
                    with self.subTest(cache=fields, rel=rel, tool=tool):
                        self.assertIsNone(self.change(tool, rel, "harness:coder"))
            for agent in ("coder", "someone:else:coder"):  # the role is the last part
                with self.subTest(cache=fields, agent=agent):
                    self.assertIsNone(self.change("Edit", ".claude/settings.json", agent))

    def test_no_tests_flag_comes_from_the_cache_of_the_branchs_own_ticket(self):
        rel = ".claude/settings.json"
        expected = CODER_ON_CONFIG.format(rel=rel)
        command = "echo '{}' > .claude/settings.json"
        tickets = self.state / "tickets"
        flagless = {"type": "task", "flags": [], "labels": ["task", "status:in-progress"]}
        # each branch's ticket is flagged its own way: in flags, in labels only, or both
        for number, slug, flagged in (
            (34, "x", {"type": "chore", "flags": ["no-tests"], "labels": ["chore"]}),
            (281, "exempt-config", {"type": "chore", "flags": [], "labels": ["no-tests"]}),
            (7, "y", {"type": "task", "flags": ["no-tests"], "labels": ["task", "no-tests"]}),
        ):
            wt = self.worktree(self.repo, branch=f"t/{number}-{slug}", name=f"wt{number}")
            own, twelve = tickets / f"{number}.json", tickets / "12.json"
            for own_fields, twelve_fields, allowed in (
                (flagged, None, True),  # ticket 12 has no cache at all
                (flagged, flagless, True),
                (flagless, flagged, False),  # ticket 12's flag is not this branch's
                (None, flagged, False),  # no cache for this branch's ticket
            ):
                for path, fields in ((own, own_fields), (twelve, twelve_fields)):
                    if fields is None:
                        if path.exists():
                            path.unlink()
                    else:
                        self.write(path, json.dumps(dict(fields, number=int(path.stem))))
                case = dict(branch=f"t/{number}-{slug}", own=own_fields, t12=twelve_fields)
                for tool in ("Edit", "Write"):
                    with self.subTest(tool=tool, **case):
                        self.assertEqual(
                            self.change(tool, rel, "harness:coder", root=wt),
                            None if allowed else expected,
                        )
                with self.subTest(tool="Bash", **case):
                    self.assertEqual(
                        self.bash(command, "harness:coder", cwd=wt),
                        None if allowed else refusal(command, expected),
                    )
        # the cache is read as JSON: an escaped spelling of the flag is the flag
        (tickets / "12.json").unlink()
        self.write(
            tickets / "34.json",
            '{"number": 34, "type": "chore", "flags": ["no\\u002dtests"], "labels": []}',
        )
        wt34 = self.tmp / "wt34"
        with self.subTest(cache="escaped"):
            self.assertIsNone(self.change("Edit", rel, "harness:coder", root=wt34))
            self.assertIsNone(self.bash(command, "harness:coder", cwd=wt34))

    def test_no_tests_config_edit_does_not_depend_on_the_red_record(self):
        self.cache(flags=["no-tests"], type="chore")
        rel = ".claude/settings.json"
        command = "echo '{}' > .claude/settings.json"
        for state in ("assertion", "green", "stale"):
            if state == "stale":  # a red record whose tests changed since
                self.red("assertion")
                self.write(self.wt / "tests/test_app.py", "def test_login():\n    assert 0\n")
            else:
                self.red(state)
            for tool in ("Edit", "Write"):
                with self.subTest(red=state, tool=tool):
                    self.assertIsNone(self.change(tool, rel, "harness:coder"))
            with self.subTest(red=state, tool="Bash"):
                self.assertIsNone(self.bash(command, "harness:coder"))

    def test_coder_may_write_test_configuration_through_bash_on_a_no_tests_ticket(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        source = self.write(outside / "settings.json", '{"env": {}}\n')
        wt = self.wt
        self.write(wt / "web/index.js", "module.exports = {};\n")  # web/ exists, untracked
        commands = (
            "echo '{}' > .claude/settings.json",
            "printf '{}\\n' >> .claude/settings.json",
            "cat > .claude/settings.json <<'EOF'\n{\"env\": {}}\nEOF",
            f"cp {source} .claude/settings.json",
            "sed -i '' 's/a/b/' .claude/settings.json",
            "echo '[tool.pytest]' | tee pyproject.toml",
            "touch pytest.ini",
            "git checkout -- package.json",
            "echo '{}' > web/package.json",
        )
        for fields in NO_TESTS_CACHES[:2]:
            self.cache(**fields)
            for command in commands:
                with self.subTest(cache=fields, command=command):
                    self.assertIsNone(self.bash(command, "harness:coder"))
            # through cd, and by absolute path, from the main checkout
            for cwd, command in (
                (self.repo, f"cd {wt} && echo '{{}}' > .claude/settings.json"),
                (self.repo, f"echo '{{}}' > {wt}/.claude/settings.json"),
                (wt, "cd web && echo '{}' > package.json"),
            ):
                with self.subTest(cache=fields, command=command, cwd=str(cwd)):
                    self.assertIsNone(self.bash(command, "harness:coder", cwd=cwd))

    def test_bulk_git_forms_over_test_configuration_follow_the_no_tests_flag(self):
        files = {
            "README.md": "hi\n",
            "src/app.py": "def login():\n    return None\n",
            ".claude/settings.json": "{}\n",
            "pyproject.toml": "[project]\nname = 'p'\n",
            "package.json": "{}\n",
        }
        commands = (
            "git stash",
            "git stash pop",
            "git reset --hard",
            "git checkout -- .",
            "git restore .",
            "git clean -fd",
        )
        # a no-tests ticket whose worktree holds test configuration and no tests
        bare = self.other_ticket_worktree("configonly", files)
        for command in commands:
            with self.subTest(command=command, agent="harness:coder"):
                self.assertIsNone(self.bash(command, "harness:coder", cwd=bare))
            with self.subTest(command=command, agent="harness:test-breaker"):
                self.assertEqual(
                    self.bash(command, "harness:test-breaker", cwd=bare),
                    refusal(command, BREAKER_ON_TEST),
                )
            with self.subTest(command=command, agent="harness:test-author"):
                self.assertIsNone(self.bash(command, "harness:test-author", cwd=bare))
        # a test file beside the configuration is still the coder's to leave alone
        test = {"tests/test_app.py": "def test_login():\n    pass\n"}
        mixed = self.other_ticket_worktree("mixed", dict(files, **test))
        for command in commands:
            with self.subTest(command=command, where="mixed"):
                self.assertEqual(
                    self.bash(command, "harness:coder", cwd=mixed),
                    refusal(command, CODER_ON_TEST),
                )
        # the same worktree on a ticket without no-tests stays refused, as today
        flagless = self.other_ticket_worktree("flagless", files)
        key = hashlib.sha1(str((self.tmp / "flagless").resolve()).encode()).hexdigest()[:12]
        ticket = {"number": 12, "type": "task", "flags": [], "labels": ["task"]}
        self.write(
            self.home / ".claude/harness/repos" / key / "tickets/12.json", json.dumps(ticket)
        )
        configs = (".claude/settings.json", "pyproject.toml", "package.json")
        for command in commands:
            with self.subTest(command=command, where="flagless"):
                reason = self.bash(command, "harness:coder", cwd=flagless)
                expected = {refusal(command, CODER_ON_CONFIG.format(rel=r)) for r in configs}
                self.assertIn(reason, expected)

    def test_bulk_git_forms_read_the_no_tests_flag_of_the_branchs_own_ticket(self):
        files = {"README.md": "hi\n", "src/app.py": "pass\n", ".claude/settings.json": "{}\n"}
        flagged = {"type": "chore", "flags": ["no-tests"], "labels": ["chore"]}
        flagless = {"type": "task", "flags": [], "labels": ["task"]}
        commands = ("git checkout -- .", "git reset --hard", "git stash")
        expected = CODER_ON_CONFIG.format(rel=".claude/settings.json")
        # t/34 flagged, and no cache for ticket 12 in that repo
        own = self.other_ticket_worktree("own", files, 34, dict(flagged, number=34))
        self.assertFalse((self.repo_state(self.tmp / "own") / "tickets/12.json").exists())
        # t/34 flagless, ticket 12 flagged in the same repo
        wrong = self.other_ticket_worktree("wrong", files, 34, dict(flagless, number=34))
        self.write(
            self.repo_state(self.tmp / "wrong") / "tickets/12.json",
            json.dumps(dict(flagged, number=12)),
        )
        # t/12 whose no-tests is not a member of a flags or labels list
        shape = {"number": 12, "flags": "no-tests", "labels": [], "status": "no-tests"}
        misshapen = self.other_ticket_worktree("misshapen", files, 12, shape)
        for command in commands:
            with self.subTest(command=command, where="t/34 flagged"):
                self.assertIsNone(self.bash(command, "harness:coder", cwd=own))
            with self.subTest(command=command, where="t/34 flagless, t/12 flagged"):
                self.assertEqual(
                    self.bash(command, "harness:coder", cwd=wrong), refusal(command, expected)
                )
            with self.subTest(command=command, where="t/12 misshapen"):
                self.assertEqual(
                    self.bash(command, "harness:coder", cwd=misshapen),
                    refusal(command, expected),
                )

    def test_coder_is_refused_test_configuration_without_the_no_tests_flag(self):
        self.red()  # a current red run: only the role rules can refuse
        rels = (".claude/settings.json", "pyproject.toml", "web/package.json")
        for fields in (
            {},  # a task without flags
            {"type": "chore"},
            {"flags": ["needs-local", "priority:high"], "labels": ["task", "needs-local"]},
            {"title": "Drop the no-tests flag from the scaffold"},
        ):
            self.cache(**fields)
            for rel in rels:
                expected = CODER_ON_CONFIG.format(rel=rel)
                for tool in ("Edit", "Write"):
                    with self.subTest(cache=fields, rel=rel, tool=tool):
                        self.assertEqual(self.change(tool, rel, "harness:coder"), expected)
                command = f"echo '{{}}' > {rel}"
                with self.subTest(cache=fields, rel=rel, tool="Bash"):
                    self.assertEqual(
                        self.bash(command, "harness:coder"), refusal(command, expected)
                    )
        # no-tests counts only as a member of the flags or labels list, as source_refusal
        # reads it: not as another field's value, not as a flags or labels string or object,
        # and not as part of a longer flag
        rel = ".claude/settings.json"
        expected = CODER_ON_CONFIG.format(rel=rel)
        command = "echo '{}' > .claude/settings.json"
        for fields in (
            {"flags": [], "labels": ["task"], "status": "no-tests"},
            {"flags": "no-tests", "labels": []},
            {"flags": [], "labels": "no-tests"},
            {"flags": {"no-tests": True}, "labels": ["task"]},
            {"flags": [["no-tests"]], "labels": ["task"]},
            {"flags": ["no-tests-later", "not-no-tests"], "labels": ["no-tests:skip"]},
            {"type": "no-tests", "title": "no-tests", "flags": [], "labels": []},
        ):
            self.cache(**fields)
            with self.subTest(cache=fields, tool="Edit"):
                self.assertEqual(self.change("Edit", rel, "harness:coder"), expected)
            with self.subTest(cache=fields, tool="Bash"):
                self.assertEqual(self.bash(command, "harness:coder"), refusal(command, expected))
        # another ticket's no-tests flag does not reach t/12
        (self.state / "tickets/12.json").unlink()
        other = {"number": 13, "type": "chore", "flags": ["no-tests"], "labels": ["no-tests"]}
        self.write(self.state / "tickets/13.json", json.dumps(other))
        rel = ".claude/settings.json"
        self.assertEqual(
            self.change("Edit", rel, "harness:coder"), CODER_ON_CONFIG.format(rel=rel)
        )

    def test_coder_is_still_refused_test_files_on_a_no_tests_ticket(self):
        for fields in NO_TESTS_CACHES[:2]:
            self.cache(**fields)
            for rel in (
                "tests/test_app.py",
                "tests/test_new.py",
                "tests/conftest.py",
                "conftest.py",  # test configuration that the default patterns call a test
                "src/pkg/test_thing.py",
                "tests/fixtures/data.json",
                "web/app.test.js",
            ):
                for tool in ("Edit", "Write"):
                    with self.subTest(cache=fields, rel=rel, tool=tool):
                        self.assertEqual(self.change(tool, rel, "harness:coder"), CODER_ON_TEST)
                command = f"echo x > {rel}"
                with self.subTest(cache=fields, rel=rel, tool="Bash"):
                    self.assertEqual(
                        self.bash(command, "harness:coder"), refusal(command, CODER_ON_TEST)
                    )
        # a configuration write passes and the test write beside it is refused
        command = "echo '{}' > .claude/settings.json && echo x > tests/test_app.py"
        with self.subTest(command=command):
            self.assertEqual(
                self.bash(command, "harness:coder"),
                refusal("echo x > tests/test_app.py", CODER_ON_TEST),
            )
        # configuration that the project's own patterns call tests is a test
        env = {"HARNESS_TEST_PATTERNS": "checks/**,pyproject.toml"}
        for rel, expected in (
            ("pyproject.toml", CODER_ON_TEST),
            ("checks/check_app.py", CODER_ON_TEST),
            (".claude/settings.json", None),
        ):
            with self.subTest(rel=rel, env=env):
                self.assertEqual(self.change("Edit", rel, "harness:coder", env=env), expected)

    def test_coder_is_refused_test_configuration_without_a_ticket_cache(self):
        cache = self.state / "tickets/12.json"
        rels = (".claude/settings.json", "pytest.ini", "package.json")
        # no cache, a corrupt one, and JSON that is not a ticket object: all count as absent
        for content in (None, "{not json", json.dumps(["no-tests"]), json.dumps("no-tests")):
            if content is not None:
                self.write(cache, content)
            self.assertEqual(cache.exists(), content is not None)
            for rel in rels:
                expected = CODER_ON_CONFIG.format(rel=rel)
                for tool in ("Edit", "Write"):
                    with self.subTest(content=content, rel=rel, tool=tool):
                        self.assertEqual(self.change(tool, rel, "harness:coder"), expected)
                command = f"echo '{{}}' > {rel}"
                with self.subTest(content=content, rel=rel, tool="Bash"):
                    self.assertEqual(
                        self.bash(command, "harness:coder"), refusal(command, expected)
                    )

    def test_breaker_and_author_keep_their_rules_on_a_no_tests_ticket(self):
        self.cache(flags=["no-tests"], type="chore")
        for rel in (".claude/settings.json", "pyproject.toml", "package.json", "tests/test_app.py"):
            command = f"echo x > {rel}"
            with self.subTest(rel=rel, agent="harness:test-breaker"):
                for tool in ("Edit", "Write"):
                    self.assertEqual(self.change(tool, rel, "harness:test-breaker"), BREAKER_ON_TEST)
                self.assertEqual(
                    self.bash(command, "harness:test-breaker"), refusal(command, BREAKER_ON_TEST)
                )
            with self.subTest(rel=rel, agent="harness:test-author"):
                self.assertIsNone(self.change("Edit", rel, "harness:test-author"))
                self.assertIsNone(self.bash(command, "harness:test-author"))
        self.assertEqual(self.change("Edit", "src/app.py", "harness:test-author"), AUTHOR_ON_SOURCE)
        # the breaker's scratch worktree names no ticket: configuration stays fixed there
        scratch = self.state / "scratch" / "t-12-fix-login"
        scratch.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), BRANCH)
        self.assertEqual(
            self.change("Edit", ".claude/settings.json", "harness:test-breaker", root=scratch),
            BREAKER_ON_TEST,
        )

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

    def repo_state(self, repo: Path) -> Path:
        """~/.claude/harness/repos/<key> for the main checkout `repo`."""
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        return self.home / ".claude/harness/repos" / key

    def other_ticket_worktree(
        self, name: str, files: dict, number: int = 12, ticket: dict | None = None
    ) -> Path:
        """A repo `name` holding `files`, its worktree on ticket `number`'s branch (t/12 is
        BRANCH, any other t/<number>-<name>), and that ticket's cache: `ticket`, by default
        a no-tests chore so that only the role rules can refuse there."""
        repo = self.make_repo(name, commit=False)
        for rel, text in files.items():
            self.write(repo / rel, text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "Initial commit")
        branch = BRANCH if number == 12 else f"t/{number}-{name}"
        wt = self.worktree(repo, branch=branch, name=f"{name}-wt")
        if ticket is None:
            ticket = {"number": number, "type": "chore", "flags": ["no-tests"]}
        self.write(self.repo_state(repo) / f"tickets/{number}.json", json.dumps(ticket))
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

    # ---- test files under a nested tests/ directory (#348)

    def test_files_under_nested_tests_directories_are_tests_for_every_role(self):
        self.cache()
        self.red()  # a current red run: only the role rules can refuse
        for rel in NESTED_TESTS:
            with self.subTest(rel=rel, agent="harness:test-author"):
                self.assertIsNone(self.change("Edit", rel, "harness:test-author"))
            with self.subTest(rel=rel, agent="harness:coder"):
                for tool in ("Edit", "Write"):
                    self.assertEqual(self.change(tool, rel, "harness:coder"), CODER_ON_TEST)
            with self.subTest(rel=rel, agent="harness:test-breaker"):
                self.assertEqual(self.change("Edit", rel, "harness:test-breaker"), BREAKER_ON_TEST)
            command = f"echo x > {rel}"  # the deep path can cross the shown limit: the denial cuts it as as_shown does
            with self.subTest(rel=rel, tool="Bash"):
                self.assertEqual(self.bash(command, "harness:coder"), refusal(as_shown(command), CODER_ON_TEST))

    def test_nested_test_files_need_no_red_run_and_source_elsewhere_still_does(self):
        self.cache()
        for rel in NESTED_TESTS:  # before any red run: a test edit passes
            with self.subTest(rel=rel):
                self.assertIsNone(self.edit(rel))
        for rel in NOT_UNDER_TESTS:  # source: the red run first, and never the test-author's
            with self.subTest(rel=rel, red=False):
                self.assertEqual(self.edit(rel), NO_RED)
                self.assertEqual(self.edit(rel, "harness:test-author"), AUTHOR_ON_SOURCE)
        self.red()
        for rel in NOT_UNDER_TESTS:  # after it, the coder's
            with self.subTest(rel=rel, red=True):
                self.assertIsNone(self.edit(rel, "harness:coder"))

    def test_bulk_forms_reach_test_files_under_nested_tests_directories(self):
        # no test_*.py and no root tests/: the only tests are a fake and a support module below tests/ directories
        # whose parents are made up for this run
        files = {
            "README.md": "hi\n",
            "plugins/harness/bin/tool": "echo tool\n",
            f"{real_parent()}/tests/fakegh.py": "ANSWERS = []\n",
            f"{generated(2)}/tests/{generated(1)}/x_support.py": "WIDTH = 3\n",
        }
        wt = self.other_ticket_worktree("nested", files)  # a no-tests chore: only the role rules refuse
        for command in ("git stash", "git reset --hard", "git checkout -- .", "git restore .", "git clean -fd"):
            with self.subTest(command=command, agent="harness:coder"):
                self.assertRefusedAs(self.bash(command, "harness:coder", cwd=wt), command, CODER_ON_TEST)
            with self.subTest(command=command, agent="harness:test-breaker"):
                self.assertRefusedAs(
                    self.bash(command, "harness:test-breaker", cwd=wt), command, BREAKER_ON_TEST
                )
            with self.subTest(command=command, agent="harness:test-author"):
                self.assertIsNone(self.bash(command, "harness:test-author", cwd=wt))

    def test_this_checkouts_settings_make_its_support_modules_tests(self):
        support = sorted(p.name for p in CHECKOUT_TESTS.glob("*_support.py")) + ["fakegh.py"]
        self.assertIn("pr_support.py", support)  # the layout #348 is about
        self.assertGreaterEqual(len(support), 4)
        tests = {f"plugins/harness/tests/{name}": "X = 1\n" for name in support}
        sources = {
            "plugins/harness/lib/harness_lib/settings.py": "X = 1\n",
            "plugins/harness/hooks/tests_first.py": "X = 1\n",
            "plugins/harness/bin/harness-red": "X = 1\n",
        }
        settings = (CHECKOUT / ".claude/settings.json").read_text(encoding="utf-8")
        files = dict(tests, **sources, **{".claude/settings.json": settings, "README.md": "hi\n"})
        wt = self.other_ticket_worktree("harness", files)  # a no-tests chore: only the role rules refuse
        for rel in tests:
            with self.subTest(rel=rel):
                self.assertIsNone(self.change("Edit", rel, "harness:test-author", root=wt))
                self.assertEqual(self.change("Edit", rel, "harness:coder", root=wt), CODER_ON_TEST)
        for rel in sources:
            with self.subTest(rel=rel):
                self.assertEqual(self.change("Edit", rel, "harness:test-author", root=wt), AUTHOR_ON_SOURCE)
                self.assertIsNone(self.change("Edit", rel, "harness:coder", root=wt))

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
