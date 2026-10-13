"""Tests for harness-scaffold: the python, node and none renders, kept and forced files, --no-ci,
--dry-run, the first-commit path, the adopt path through stub harness-board, harness-work and
harness-pr, --github through fakegh.py (including the 403 on rulesets), --register and `labels`.

    python3 -m unittest discover -s plugins/harness/tests -v

The tool runs as a subprocess in a temporary folder with a temporary HOME and a PATH of `fakebin`, a
folder holding only git and python3, /usr/bin and /bin, so nothing reads ~/.claude or the network,
and no uv, npm, ruff, gh or harness tool is found unless a test puts a stub in `fakebin`. Stubs and the fake gh append one
JSON line per call to the same log, so a test can check the order across tools.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
SCAFFOLD = os.path.join(PLUGIN, "bin", "harness-scaffold")
TEMPLATES = os.path.join(PLUGIN, "templates")
FAKEGH = os.path.join(TESTS, "fakegh.py")
GIT = shutil.which("git")
NO_RULESET = ("Branch protection is unavailable on this plan for a private repo; "
              "the local branch guard is the only block.")
PLACEHOLDER = re.compile(r"(?<!\$)\{\{(name|pkg|stack|verify_cmd|test_cmd|python_version|year)\}\}")

COMMON_FILES = ["CLAUDE.md", ".claude/settings.json", "scripts/verify.sh", ".github/workflows/ci.yml",
                ".github/workflows/deploy.yml", ".github/pull_request_template.md",
                ".github/ISSUE_TEMPLATE/task.md", ".github/ISSUE_TEMPLATE/epic.md", ".githooks/pre-commit",
                ".gitignore", ".worktreeinclude"]

STUB = """#!{python}
import json, os, sys
argv = sys.argv[1:]
body = None
if "--body-file" in argv:
    with open(argv[argv.index("--body-file") + 1], encoding="utf-8") as fh:
        body = fh.read()
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": {name!r}, "argv": argv, "cwd": os.getcwd(),
                         "project": os.environ.get("CLAUDE_PROJECT_DIR"), "body": body}}) + "\\n")
sys.stdout.write({outputs!r}.get(argv[0] if argv else "", ""))
sys.exit({code})
"""

# `gh repo create --source . --remote origin` adds the remote; the fake does that too, then answers.
GH_WRAPPER = """#!/bin/sh
if [ "$1" = repo ] && [ "$2" = create ]; then git remote add origin "$FAKE_ORIGIN"; fi
exec "{python}" "{fakegh}" "$@"
"""


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-scaffold-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.fakebin = self.mk("fakebin")
        self.toolbin = self.mk("toolbin")            # git and python3 only, not the rest of their folders
        os.symlink(GIT, os.path.join(self.toolbin, "git"))
        os.symlink(sys.executable, os.path.join(self.toolbin, "python3"))
        self.log = os.path.join(self.tmp, "calls.jsonl")

    # ------------------------------------------------------------ helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, path, text, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def read(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "HARNESS_GH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        path = [self.fakebin, self.toolbin, "/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(path), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        env.update(extra)
        return env

    def scaffold(self, *args, env=None):
        return subprocess.run([sys.executable, SCAFFOLD] + list(args), cwd=self.tmp, env=env or self.env(),
                              capture_output=True, text=True, timeout=120)

    def ok(self, *args, env=None):
        p = self.scaffold(*args, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def git(self, repo, *args):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def stub(self, name, outputs=None, code=0):
        """A fake sibling tool on PATH: logs its call, prints outputs[<first argument>]."""
        path = os.path.join(self.fakebin, name)
        self.write(path, STUB.format(python=sys.executable, log=self.log, name=name, outputs=outputs or {},
                                     code=code), 0o755)
        return path

    def calls(self, tool=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if tool is None or r.get("tool") == tool]

    def fake_gh(self, calls, origin):
        """HARNESS_GH pointing at fakegh.py behind a wrapper that adds `origin` on repo create."""
        scenario = os.path.join(self.tmp, "gh-scenario.json")
        self.write(scenario, json.dumps({"calls": calls}))
        wrapper = self.write(os.path.join(self.tmp, "gh"), GH_WRAPPER.format(python=sys.executable, fakegh=FAKEGH),
                             0o755)
        return {"HARNESS_GH": wrapper, "FAKEGH_SCENARIO": scenario, "FAKEGH_LOG": self.log,
                "FAKE_ORIGIN": origin}

    def lines(self, p):
        return p.stdout.splitlines()

    def assertOneLineError(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        for needle in needles:
            self.assertIn(needle, p.stderr)

    def assertExecutable(self, path):
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, f"{path} is not executable")

    def settings(self, root):
        return json.loads(self.read(root, ".claude", "settings.json"))

    def repo_with_commit(self, name="proj"):
        """A repository with one commit on main (a README) and no remote."""
        root = self.mk(name)
        self.git(root, "init", "-q", "-b", "main")
        self.write(os.path.join(root, "README.md"), "# proj\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", "First")
        return root

    def bare(self, name="origin.git"):
        path = self.mk(name)
        self.git(path, "init", "-q", "--bare", "-b", "main")
        return path


# =========================================================================== renders
class Render(ScaffoldTest):
    def test_python_render(self):
        root = os.path.join(self.tmp, "demo")
        p = self.ok("init", root, "--name", "demo-app", "--stack", "python")
        expected = COMMON_FILES + ["pyproject.toml", "src/demo_app/__init__.py", "tests/test_smoke.py"]
        for rel in expected:
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
            self.assertIn(f"created {rel}", self.lines(p))
            self.assertIsNone(PLACEHOLDER.search(self.read(root, rel)), f"unrendered placeholder in {rel}")

        ci = self.read(root, ".github/workflows/ci.yml")
        for piece in ("group: ${{ github.workflow }}-${{ github.ref }}",
                      "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
                      "branches: [main]", "pull_request:", "workflow_dispatch:", "actions/checkout@v7",
                      "astral-sh/setup-uv@v10.3.0", "enable-cache: true", "uv sync --locked --all-extras --dev",
                      "run: scripts/verify.sh"):
            self.assertIn(piece, ci)
        self.assertNotIn("setup-node", ci)
        deploy = self.read(root, ".github/workflows/deploy.yml")
        self.assertIn("scripts/deploy.sh", deploy)
        self.assertIn('echo "no deploy script"', deploy)
        self.assertNotIn("pull_request", deploy)

        claude = self.read(root, "CLAUDE.md")
        self.assertIn("# demo-app", claude)
        self.assertIn("`scripts/verify.sh`", claude)
        self.assertIn("harness-work start <issue#>", claude)
        self.assertIn("## Compact Instructions", claude)
        self.assertLess(len(claude.splitlines()), 50)

        pyproject = self.read(root, "pyproject.toml")
        self.assertIn('name = "demo-app"', pyproject)
        self.assertIn('requires-python = ">=3.10"', pyproject)
        self.assertIn('packages = ["src/demo_app"]', pyproject)
        for piece in ('"pytest', '"ruff', "[tool.ruff]", "[tool.pytest.ini_options]"):
            self.assertIn(piece, pyproject)
        self.assertIn("import demo_app", self.read(root, "tests/test_smoke.py"))
        for rel in ("src/demo_app/__init__.py", "tests/test_smoke.py"):
            compile(self.read(root, rel), rel, "exec")

        verify = os.path.join(root, "scripts", "verify.sh")
        hook = os.path.join(root, ".githooks", "pre-commit")
        self.assertExecutable(verify)
        self.assertExecutable(hook)
        self.assertEqual(self.read(verify).strip().splitlines()[-1], "echo VERIFY OK")
        for piece in ("uv run ruff check .", "uv run ruff format --check .", "uv run pytest -q"):
            self.assertIn(piece, self.read(verify))
        for piece in ("--diff-filter=ACM", "uv run --quiet ruff", "format --check", "exit 1"):
            self.assertIn(piece, self.read(hook))
        for path in (verify, hook):
            self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0, path)

        settings = self.settings(root)
        self.assertEqual(settings["env"], {"CLAUDE_VERIFY_CMD": "scripts/verify.sh",
                                           "HARNESS_TEST_CMD": "uv run pytest -q {files}"})
        allow = settings["permissions"]["allow"]
        for rule in ("Bash(scripts/verify.sh)", "Bash(git commit:*)", "Bash(git worktree:*)", "Bash(gh api:*)",
                     "Bash(harness-*:*)", "Bash(uv run pytest:*)"):
            self.assertIn(rule, allow)

        self.assertEqual(self.git(root, "config", "--get", "core.hooksPath"), ".githooks")
        self.assertIn(".claude/worktrees/", self.read(root, ".git", "info", "exclude").splitlines())
        self.assertIn(".env", self.read(root, ".worktreeinclude").splitlines())
        self.assertIn(".claude/worktrees/", self.read(root, ".gitignore").splitlines())

    def test_issue_templates_carry_the_ticket_bodies(self):
        root = os.path.join(self.tmp, "demo")
        self.ok("init", root, "--name", "demo", "--stack", "none")
        for dest, template, label in (("task.md", "ticket_task.md", '"task"'), ("epic.md", "ticket_epic.md", '"epic"')):
            text = self.read(root, ".github", "ISSUE_TEMPLATE", dest)
            self.assertTrue(text.startswith("---\nname: "), text[:40])
            front, body = text.split("\n---\n\n", 1)
            self.assertIn(label, front)
            self.assertEqual(body, self.read(TEMPLATES, template))
        task = self.read(TEMPLATES, "ticket_task.md")
        for heading in ("## Request", "## Outline", "## Definition of done", "## Tests that must pass", "## Context"):
            self.assertIn(heading, task)
        epic = self.read(TEMPLATES, "ticket_epic.md")
        for heading in ("## Goal", "## Why", "## Scope", "## Out of scope", "## Definition of done",
                        "## Verification", "## Decisions and open questions"):
            self.assertIn(heading, epic)
        pr = self.read(root, ".github", "pull_request_template.md")
        for heading in ("## Ticket", "## Summary", "## Definition of done", "## Tests", "Red run", "## Verify",
                        "## Review", "## Risks"):
            self.assertIn(heading, pr)

    def test_node_render(self):
        root = os.path.join(self.tmp, "web")
        p = self.ok("init", root, "--name", "Web-App", "--stack", "node")
        for rel in COMMON_FILES + ["package.json", "tests/smoke.test.js"]:
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
            self.assertIsNone(PLACEHOLDER.search(self.read(root, rel)), f"unrendered placeholder in {rel}")
        self.assertFalse(os.path.exists(os.path.join(root, "pyproject.toml")))
        package = json.loads(self.read(root, "package.json"))
        self.assertEqual(package["name"], "web-app")
        self.assertEqual(package["scripts"]["test"], "node --test")
        self.assertIn('"web-app"', self.read(root, "tests", "smoke.test.js"))
        ci = self.read(root, ".github/workflows/ci.yml")
        for piece in ("${{ github.workflow }}-${{ github.ref }}", "actions/checkout@v7", "actions/setup-node@v7",
                      "node-version: 24", "cache: npm", "run: npm ci", "run: scripts/verify.sh"):
            self.assertIn(piece, ci)
        self.assertNotIn("setup-uv", ci)
        self.assertIn("npm test", self.read(root, "scripts", "verify.sh"))
        self.assertIn("npm run lint --if-present", self.read(root, ".githooks", "pre-commit"))
        self.assertEqual(self.settings(root)["env"]["HARNESS_TEST_CMD"], "npm test -- {files}")
        self.assertIn("Bash(npm test:*)", self.settings(root)["permissions"]["allow"])
        self.assertIn("# Web-App", self.read(root, "CLAUDE.md"))
        self.assertTrue(any(line.startswith("note: npm is not on PATH") for line in self.lines(p)), p.stdout)
        for rel in ("scripts/verify.sh", ".githooks/pre-commit"):
            self.assertEqual(subprocess.run(["sh", "-n", os.path.join(root, rel)]).returncode, 0, rel)

    def test_none_render(self):
        root = self.mk("plain")
        self.ok("init", root, "--name", "plain")                  # an empty folder: stack none
        for rel in COMMON_FILES:
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
        for rel in ("pyproject.toml", "package.json", "tests", "src"):
            self.assertFalse(os.path.exists(os.path.join(root, rel)), rel)
        self.assertEqual(self.settings(root)["env"], {"CLAUDE_VERIFY_CMD": "scripts/verify.sh"})
        claude = self.read(root, "CLAUDE.md")
        self.assertNotIn("Named test files", claude)
        self.assertNotIn("``", claude)
        verify = self.read(root, "scripts", "verify.sh")
        self.assertIn("\ntrue\n", verify)
        self.assertEqual(verify.strip().splitlines()[-1], "echo VERIFY OK")
        ci = self.read(root, ".github/workflows/ci.yml")
        self.assertIn("run: scripts/verify.sh", ci)
        self.assertNotIn("setup-uv", ci)
        self.assertNotIn("setup-node", ci)
        for rel in ("scripts/verify.sh", ".githooks/pre-commit"):
            self.assertEqual(subprocess.run(["sh", "-n", os.path.join(root, rel)]).returncode, 0, rel)

    def test_stack_detection_and_test_command(self):
        cases = [
            ({"pyproject.toml": "[project]\nname = 'x'\n"}, "python3 -m pytest -q {files}"),
            ({"pyproject.toml": "[project]\nname = 'x'\n", "uv.lock": ""}, "uv run pytest -q {files}"),
            ({"tool.py": "print(1)\n"}, "uv run pytest -q {files}"),
            ({"package.json": '{"name": "x"}\n'}, "npm test -- {files}"),
        ]
        for n, (files, test_cmd) in enumerate(cases):
            with self.subTest(files=sorted(files)):
                root = self.mk(f"detect{n}")
                for rel, text in files.items():
                    self.write(os.path.join(root, rel), text)
                p = self.ok("init", root, "--name", f"detect{n}")
                self.assertEqual(self.settings(root)["env"]["HARNESS_TEST_CMD"], test_cmd)
                for manifest in ("pyproject.toml", "package.json"):
                    if manifest in files:                     # the project's own layout: kept, no skeleton
                        self.assertIn(f"kept {manifest}", self.lines(p))
                        self.assertEqual(self.read(root, manifest), files[manifest])
                        self.assertFalse(os.path.exists(os.path.join(root, "tests")))
                if "tool.py" in files:
                    self.assertIn("created pyproject.toml", self.lines(p))
                    self.assertTrue(os.path.isfile(os.path.join(root, "src", f"detect{n}", "__init__.py")))

    def test_custom_commands_and_quotes_stay_valid_json(self):
        root = os.path.join(self.tmp, "custom")
        self.ok("init", root, "--name", "custom", "--stack", "python", "--verify", 'make check ARGS="-x"',
                "--test-cmd", "tox -e py -- {files}")
        self.assertEqual(self.settings(root)["env"], {"CLAUDE_VERIFY_CMD": 'make check ARGS="-x"',
                                                      "HARNESS_TEST_CMD": "tox -e py -- {files}"})
        self.assertIn('`make check ARGS="-x"`', self.read(root, "CLAUDE.md"))

    # ---- --verify is written into scripts/verify.sh (ticket #5)
    def checks(self, root):
        """The command lines of scripts/verify.sh: no blanks, comments, shebang, `set -` or `cd`."""
        lines = [ln.strip() for ln in self.read(root, "scripts", "verify.sh").splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#") and not ln.startswith("set -")
                and not ln.startswith("cd ")]

    def test_verify_is_written_into_verify_sh_and_settings_point_at_it(self):
        for n, command in enumerate(["python3 -m unittest discover -s plugins/harness/tests",
                                     "make check", "./run-all-tests.sh --fast",
                                     'make check ARGS="-x" && echo \'done\'']):
            with self.subTest(verify=command):
                root = self.mk(f"verified{n}")
                self.ok("init", root, "--name", f"verified{n}", "--stack", "none", "--verify", command)
                checks = self.checks(root)
                self.assertEqual(checks, [command, "echo VERIFY OK"], checks)    # X, nothing else (no `true`)
                self.assertEqual(self.settings(root)["env"], {"CLAUDE_VERIFY_CMD": "scripts/verify.sh"})
                script = os.path.join(root, "scripts", "verify.sh")
                self.assertExecutable(script)
                self.assertEqual(subprocess.run(["sh", "-n", script]).returncode, 0)
                self.assertIn("run: scripts/verify.sh", self.read(root, ".github/workflows/ci.yml"))

    def test_verify_sh_actually_runs_the_verify_command(self):
        root = self.mk("runs")
        self.ok("init", root, "--name", "runs", "--stack", "none", "--verify", "echo ran-it > marker.txt")
        script = os.path.join(root, "scripts", "verify.sh")
        p = subprocess.run(["sh", script], cwd=self.tmp, capture_output=True, text=True)    # from any directory
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.read(root, "marker.txt").strip(), "ran-it")
        self.assertIn("VERIFY OK", p.stdout)

    def test_a_failing_verify_command_fails_verify_sh(self):
        root = self.mk("fails")
        self.ok("init", root, "--name", "fails", "--stack", "none", "--verify", "exit 3")
        p = subprocess.run(["sh", os.path.join(root, "scripts", "verify.sh")], cwd=self.tmp,
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("VERIFY OK", p.stdout)

    def test_verify_replaces_the_stack_defaults_for_python_and_node_too(self):
        for stack, default in (("python", "uv run ruff check ."), ("node", "npm test")):
            with self.subTest(stack=stack):
                root = self.mk(f"v-{stack}")
                self.ok("init", root, "--name", f"v-{stack}", "--stack", stack, "--verify", "make check-all")
                self.assertEqual(self.checks(root), ["make check-all", "echo VERIFY OK"])
                self.assertNotIn(default, self.read(root, "scripts", "verify.sh"))
                # python and node keep the command itself in settings: only stack none points at the script
                self.assertEqual(self.settings(root)["env"]["CLAUDE_VERIFY_CMD"], "make check-all")

    def test_verify_text_is_written_literally(self):
        command = r"echo $HOME {{name}} {{verify_cmd}} C:\dir 'it''s'"
        root = self.mk("literal")
        self.ok("init", root, "--name", "literal", "--stack", "none", "--verify", command)
        self.assertEqual(self.checks(root), [command, "echo VERIFY OK"])
        self.assertIn(command, self.read(root, "scripts", "verify.sh"))

    def test_without_verify_the_stack_defaults_are_unchanged(self):
        root = self.mk("defaults-py")
        self.ok("init", root, "--name", "defaults-py", "--stack", "python")
        self.assertEqual(self.checks(root)[-4:], ["uv run ruff check .", "uv run ruff format --check .",
                                                  "uv run pytest -q", "echo VERIFY OK"])
        self.assertEqual(self.settings(root)["env"]["CLAUDE_VERIFY_CMD"], "scripts/verify.sh")
        root = self.mk("defaults-none")
        self.ok("init", root, "--name", "defaults-none", "--stack", "none")
        self.assertEqual(self.checks(root), ["true", "echo VERIFY OK"])

    # ---- the tests directory in CLAUDE.md "Where things live" (ticket #5)
    def where(self, root):
        text = self.read(root, "CLAUDE.md")
        return text.split("## Where things live", 1)[1].split("\n## ", 1)[0]

    def names_dir(self, section, directory):
        return re.search(r"`" + re.escape(directory.rstrip("/")) + r"/?`", section) is not None

    def test_tests_directory_is_detected_on_disk(self):
        candidates = ("tests", "test", "plugins/harness/tests", "plugins/other/tests", "plugins/x/tests")
        for present in (("tests",), ("test",), ("plugins/harness/tests",), ("plugins/other/tests",),
                        ("tests", "plugins/x/tests"),                  # several on disk: every one is named
                        ("test", "tests"), ("test", "plugins/harness/tests", "plugins/other/tests")):
            with self.subTest(present=present):
                root = self.mk("disk-" + "+".join(d.replace("/", "-") for d in present))
                for directory in present:
                    self.write(os.path.join(root, directory, "test_a.py"), "pass\n")
                self.ok("init", root, "--name", "disk", "--stack", "none")
                section = self.where(root)
                for directory in candidates:                          # exactly the ones on disk, no guess
                    self.assertEqual(self.names_dir(section, directory), directory in present,
                                     f"{directory}: {section}")

    def test_a_file_named_like_a_tests_directory_is_not_named(self):
        for rel in ("tests", "test", "plugins/x/tests"):
            with self.subTest(file=rel):
                root = self.mk("file-" + rel.replace("/", "-"))
                self.write(os.path.join(root, rel), "not a directory\n")
                self.ok("init", root, "--name", "file", "--stack", "none")
                self.assertFalse(self.names_dir(self.where(root), rel), self.where(root))
        root = self.mk("file-and-dir")                              # a file next to a real directory
        self.write(os.path.join(root, "tests"), "not a directory\n")
        self.write(os.path.join(root, "plugins", "x", "tests", "test_a.py"), "pass\n")
        self.ok("init", root, "--name", "file", "--stack", "none")
        section = self.where(root)
        self.assertTrue(self.names_dir(section, "plugins/x/tests"), section)
        self.assertFalse(self.names_dir(section, "tests"), section)

    def test_tests_directory_comes_from_the_verify_commands_s_argument(self):
        cases = [("python3 -m unittest discover -s plugins/harness/tests", "plugins/harness/tests"),
                 ("python3 -m unittest discover -s spec -v", "spec"),
                 ("python3 -m unittest discover -s src/pkg/checks/", "src/pkg/checks")]
        for n, (command, directory) in enumerate(cases):
            with self.subTest(verify=command):
                root = self.mk(f"sflag{n}")
                self.write(os.path.join(root, directory, "test_a.py"), "pass\n")
                self.ok("init", root, "--name", f"sflag{n}", "--stack", "none", "--verify", command)
                section = self.where(root)
                self.assertTrue(self.names_dir(section, directory), section)
                self.assertFalse(self.names_dir(section, "tests"), section)    # no stale guess next to it

    def test_s_argument_forms_are_read_from_a_unittest_discover_command(self):
        cases = [("python3 -m unittest discover --start-directory spec", "spec"),
                 ('python3 -m unittest discover -s "my tests"', "my tests"),
                 ("python3 -m unittest discover -p 'test_*.py' -s checks -t . -v", "checks"),
                 ("python3 -m unittest discover -v -s suite/unit -p test_x.py", "suite/unit"),
                 # attached values, as argparse accepts them (`python3 -m unittest discover -h`)
                 ("python3 -m unittest discover --start-directory=spec", "spec"),
                 ("python3 -m unittest discover -sspec", "spec"),
                 ("python3 -m unittest discover -s=attached/eq -v", "attached/eq"),
                 ("python3 -m unittest discover -v --start-directory=suite/long -p 't*.py'", "suite/long"),
                 ("python3 -m unittest discover -ssuite/short", "suite/short"),
                 # shell quoting: the argument is the unquoted word
                 ("python3 -m unittest discover -s 'my tests'", "my tests"),
                 ("python3 -m unittest discover --start-directory 'single quoted/dir' -v", "single quoted/dir"),
                 # shell separators inside a quoted argument do not end the command
                 ("python3 -m unittest discover -p 'test_*;x.py' -s checks", "checks"),
                 ("python3 -m unittest discover -p 'a|b.py' -s piped/checks", "piped/checks"),
                 ('python3 -m unittest discover -p "t&x.py" -v -s amp/checks', "amp/checks"),
                 ("python3 -m unittest discover -p 'x && y' -s 'and checks'", "and checks"),
                 # the discover command after another one, and before another one
                 ("make lint && python3 -m unittest discover -s after/and", "after/and"),
                 ("python3 -m unittest discover -s before/semi; ls -s other", "before/semi")]
        for n, (command, directory) in enumerate(cases):
            with self.subTest(verify=command):
                root = self.mk(f"sforms{n}")
                self.ok("init", root, "--name", f"sforms{n}", "--stack", "none", "--verify", command)
                self.assertTrue(self.names_dir(self.where(root), directory), self.where(root))

    def test_s_in_other_commands_is_not_a_tests_directory(self):
        for n, (command, wrong) in enumerate([("make -s lint", "lint"), ("pytest -s tests/unit", "tests/unit"),
                                              ("make -s check && ./lint.sh -s extra", "extra"),
                                              # -s after the discover command ends belongs to another command
                                              ("python3 -m unittest discover && ls -s foo", "foo"),
                                              ("python3 -m unittest discover -v; ls -s bar", "bar"),
                                              ("python3 -m unittest discover || du -s baz", "baz"),
                                              ("python3 -m unittest discover | tee -s qux", "qux"),
                                              ("python3 -m unittest discover -s ok/dir; ls -s other", "other")]):
            with self.subTest(verify=command):
                root = self.mk(f"sfalse{n}")
                self.ok("init", root, "--name", f"sfalse{n}", "--stack", "none", "--verify", command)
                self.assertFalse(self.names_dir(self.where(root), wrong), self.where(root))

    def test_s_argument_wins_over_a_directory_on_disk(self):
        for existing in ("tests", "test"):
            with self.subTest(existing=existing):
                root = self.mk(f"win-{existing}")
                self.write(os.path.join(root, existing, "test_a.py"), "pass\n")
                self.ok("init", root, "--name", "win", "--stack", "none", "--verify",
                        "python3 -m unittest discover -s checks")
                section = self.where(root)
                self.assertTrue(self.names_dir(section, "checks"), section)
                self.assertFalse(self.names_dir(section, existing), section)

    def test_every_plugin_tests_directory_is_named(self):
        root = self.mk("plugins-many")
        for name in ("a", "b", "c"):
            self.write(os.path.join(root, "plugins", name, "tests", "test_x.py"), "pass\n")
        self.ok("init", root, "--name", "many", "--stack", "none")
        section = self.where(root)
        for name in ("a", "b", "c"):
            self.assertTrue(self.names_dir(section, f"plugins/{name}/tests"), section)

    def test_s_argument_is_named_when_the_directory_does_not_exist_yet(self):
        root = self.mk("sflag-fresh")
        self.ok("init", root, "--name", "fresh", "--stack", "none", "--verify",
                "python3 -m unittest discover -s checks")
        self.assertTrue(self.names_dir(self.where(root), "checks"), self.where(root))

    def test_stack_skeleton_tests_directory_is_named(self):
        for stack in ("python", "node"):
            with self.subTest(stack=stack):
                root = self.mk(f"skel-{stack}")
                self.ok("init", root, "--name", f"skel-{stack}", "--stack", stack)
                self.assertTrue(self.names_dir(self.where(root), "tests"), self.where(root))

    def test_no_tests_directory_is_not_guessed(self):
        root = self.mk("notests")
        self.ok("init", root, "--name", "notests", "--stack", "none")
        section = self.where(root)
        self.assertFalse(self.names_dir(section, "tests"), section)
        self.assertFalse(self.names_dir(section, "test"), section)
        self.assertIn("`scripts/verify.sh`", section)


# =========================================================================== rules
class Rules(ScaffoldTest):
    def test_existing_file_is_kept_without_force(self):
        root = self.mk("kept")
        self.write(os.path.join(root, "CLAUDE.md"), "mine\n")
        p = self.ok("init", root, "--name", "kept", "--stack", "none")
        self.assertIn("kept CLAUDE.md", self.lines(p))
        self.assertIn("created .gitignore", self.lines(p))
        self.assertEqual(self.read(root, "CLAUDE.md"), "mine\n")

    def test_force_updates_an_existing_file(self):
        root = self.mk("forced")
        self.write(os.path.join(root, "CLAUDE.md"), "mine\n")
        p = self.ok("init", root, "--name", "forced", "--stack", "none", "--force")
        self.assertIn("updated CLAUDE.md", self.lines(p))
        self.assertIn("# forced", self.read(root, "CLAUDE.md"))

    def test_second_run_keeps_every_file_and_adopts_nothing(self):
        root = os.path.join(self.tmp, "twice")
        self.ok("init", root, "--name", "twice", "--stack", "python")
        self.write(os.path.join(root, "CLAUDE.md"), "edited by hand\n")
        self.git(root, "commit", "-qam", "Edit CLAUDE.md")
        self.stub("harness-board")
        p = self.ok("init", root, "--name", "twice", "--stack", "python")
        written = [ln for ln in self.lines(p) if re.match(r"^(created|updated) ", ln)]
        self.assertEqual(written, [], p.stdout)
        for rel in COMMON_FILES + ["pyproject.toml"]:
            self.assertIn(f"kept {rel}", self.lines(p))
        self.assertIn("kept core.hooksPath .githooks", self.lines(p))
        self.assertIn("kept .git/info/exclude", self.lines(p))
        self.assertIn("nothing to adopt: every scaffold file is already here", self.lines(p))
        self.assertEqual(self.read(root, "CLAUDE.md"), "edited by hand\n")
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.calls(), [])
        exclude = self.read(root, ".git", "info", "exclude").splitlines()
        self.assertEqual(exclude.count(".claude/worktrees/"), 1)

    def test_the_projects_own_hooks_are_kept_without_force(self):
        for force in (False, True):
            with self.subTest(force=force):
                root = self.mk(f"hooked{int(force)}")
                self.git(root, "init", "-q", "-b", "main")
                self.write(os.path.join(root, ".git", "hooks", "pre-commit"), "#!/bin/sh\nexit 0\n", 0o755)
                p = self.ok("init", root, "--name", "hooked", "--stack", "none", *(["--force"] if force else []))
                hooks_path = subprocess.run([GIT, "-C", root, "config", "--get", "core.hooksPath"],
                                            env=self.env(), capture_output=True, text=True).stdout.strip()
                if force:
                    self.assertEqual(hooks_path, ".githooks")
                else:
                    self.assertEqual(hooks_path, "")
                    self.assertTrue(any(ln.startswith("kept .git/hooks (pre-commit)") for ln in self.lines(p)),
                                    p.stdout)

    def test_no_ci_writes_no_workflows(self):
        root = os.path.join(self.tmp, "noci")
        p = self.ok("init", root, "--name", "noci", "--stack", "python", "--no-ci")
        self.assertFalse(os.path.exists(os.path.join(root, ".github", "workflows")))
        self.assertNotIn("workflows", p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(root, ".github", "pull_request_template.md")))
        self.assertTrue(os.path.isfile(os.path.join(root, "scripts", "verify.sh")))

    def test_dry_run_writes_nothing(self):
        root = os.path.join(self.tmp, "dry")
        p = self.ok("init", root, "--name", "dry", "--stack", "python", "--github", "--register", "--dry-run")
        self.assertFalse(os.path.exists(root))
        for line in ("would run: git init -b main", "would create CLAUDE.md", "would create pyproject.toml",
                     "would run: uv lock", "would commit everything on main: Scaffold dry with the harness",
                     "would set core.hooksPath .githooks"):
            self.assertIn(line, self.lines(p))
        self.assertTrue(any(ln.startswith("would run: gh repo create dry --private") for ln in self.lines(p)))
        self.assertTrue(any(ln.startswith("would run: harness-projects add") for ln in self.lines(p)))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")))

    def test_dry_run_in_a_repo_with_commits_plans_the_adoption(self):
        root = self.repo_with_commit()
        self.git(root, "remote", "add", "origin", self.bare())
        head = self.git(root, "rev-parse", "HEAD")
        p = self.ok("init", root, "--name", "proj", "--stack", "none", "--dry-run")
        self.assertIn("would create CLAUDE.md", self.lines(p))
        self.assertTrue(any(ln.startswith("would run: harness-board task create") for ln in self.lines(p)))
        self.assertFalse(os.path.exists(os.path.join(root, "CLAUDE.md")))
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(self.calls(), [])

    def test_bad_invocations_are_one_line_errors(self):
        self.assertOneLineError(self.scaffold("init", self.tmp + "/x"), "--name")
        self.assertOneLineError(self.scaffold("init", self.tmp + "/x", "--name", "has space"), "--name")
        self.assertOneLineError(self.scaffold("init", self.tmp + "/x", "--name", "x", "--billable"),
                                "--register")
        self.assertOneLineError(self.scaffold("init", self.tmp + "/x", "--name", "x", "--stack", "rust"), "stack")
        self.assertOneLineError(self.scaffold(), "subcommand")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))

    def test_refuses_a_subfolder_of_another_repository(self):
        root = self.repo_with_commit()
        sub = self.mk("proj", "sub")
        self.assertOneLineError(self.scaffold("init", sub, "--name", "sub"), "inside the repository")
        self.assertEqual(os.listdir(sub), [])


# =========================================================================== commit paths
class Paths(ScaffoldTest):
    def test_first_commit_path_makes_one_commit_on_main(self):
        root = os.path.join(self.tmp, "fresh")
        p = self.ok("init", root, "--name", "fresh", "--stack", "python")
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(root, "log", "-1", "--format=%s"), "Scaffold fresh with the harness")
        self.assertEqual(self.git(root, "status", "--porcelain"), "")
        tracked = self.git(root, "ls-files", "-s").splitlines()
        self.assertTrue(any(ln.startswith("100755") and ln.endswith("\t.githooks/pre-commit") for ln in tracked))
        self.assertTrue(any(ln.startswith("100755") and ln.endswith("\tscripts/verify.sh") for ln in tracked))
        self.assertTrue(any(ln.endswith("\tCLAUDE.md") for ln in tracked))
        self.assertTrue(any(re.match(r"^committed [0-9a-f]+ on main: Scaffold fresh with the harness$", ln)
                            for ln in self.lines(p)), p.stdout)
        self.assertEqual(self.calls(), [])

    def test_unborn_master_becomes_main(self):
        root = self.mk("legacy")
        self.git(root, "init", "-q", "-b", "master")
        self.ok("init", root, "--name", "legacy", "--stack", "none")
        self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "1")

    def test_lock_file_is_created_and_committed(self):
        uv_log = os.path.join(self.tmp, "uv.log")
        self.write(os.path.join(self.fakebin, "uv"),
                   f'#!/bin/sh\necho "$*" >> "{uv_log}"\n'
                   'if [ "$1" = lock ]; then echo "version = 1" > uv.lock; exit 0; fi\nexit 1\n', 0o755)
        root = os.path.join(self.tmp, "locked")
        p = self.ok("init", root, "--name", "locked", "--stack", "python")
        self.assertIn("created uv.lock", self.lines(p))
        self.assertIn("uv.lock", self.git(root, "ls-files").splitlines())
        self.assertEqual(self.read(uv_log).splitlines(), ["lock"])
        self.assertEqual(self.settings(root)["env"]["HARNESS_TEST_CMD"], "uv run pytest -q {files}")

    def test_adopt_path_goes_through_a_ticket_and_a_pull_request(self):
        root = self.repo_with_commit()
        self.git(root, "remote", "add", "origin", self.bare())
        self.git(root, "push", "-q", "-u", "origin", "main")
        main_head = self.git(root, "rev-parse", "HEAD")
        branch = "t/7-adopt-the-harness-scaffold"
        worktree = os.path.join(root, ".claude", "worktrees", "t-7-adopt-the-harness-scaffold")
        self.git(root, "worktree", "add", "-q", worktree, "-b", branch, "origin/main")
        self.stub("harness-board", {"task": "#7 https://github.com/ryan/proj/issues/7\n"})
        self.stub("harness-work", {"start": json.dumps({"number": 7, "branch": branch, "worktree": worktree,
                                                        "tier": "sonnet", "context": "# 7"})})
        self.stub("harness-pr", {"open": json.dumps({"number": 12, "url": "https://github.com/ryan/proj/pull/12"}),
                                 "ready": "PR #12 ready for review\n"})

        p = self.ok("init", root, "--name", "proj", "--stack", "python")

        calls = self.calls()
        self.assertEqual([(c["tool"], c["argv"][0]) for c in calls],
                         [("harness-board", "task"), ("harness-work", "start"), ("harness-pr", "open"),
                          ("harness-pr", "ready")])
        board, work, pr_open, pr_ready = calls
        self.assertEqual(board["argv"][:-1], ["task", "create", "--title", "Adopt the harness scaffold", "--type",
                                              "chore", "--no-tests", "--size", "S", "--risk", "low", "--body-file"])
        for piece in ("## Request", "## Definition of done", "- [ ] `CLAUDE.md` is added", "## Tests that must pass",
                      "`scripts/verify.sh`"):
            self.assertIn(piece, board["body"])
        self.assertFalse(os.path.exists(board["argv"][-1]), "the body file is left behind")
        self.assertEqual(work["argv"], ["start", "7", "--json"])
        self.assertEqual(pr_open["argv"], ["open", "7", "--json"])
        self.assertEqual(pr_open["cwd"], worktree)
        self.assertEqual(pr_ready["argv"], ["ready", "12"])
        self.assertEqual({c["project"] for c in calls}, {root})

        # the files land in the worktree, in one commit on the ticket branch; main is untouched
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Adopt the harness scaffold (#7)")
        self.assertEqual(self.git(worktree, "rev-list", "--count", "origin/main..HEAD"), "1")
        self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
        committed = self.git(worktree, "show", "--name-only", "--format=", "HEAD").splitlines()
        for rel in COMMON_FILES + ["pyproject.toml", "src/proj/__init__.py", "tests/test_smoke.py"]:
            self.assertIn(rel, committed)
        self.assertNotIn("README.md", committed)
        self.assertFalse(os.path.exists(os.path.join(root, "CLAUDE.md")))
        self.assertEqual(self.git(root, "rev-parse", "main"), main_head)
        self.assertEqual(self.git(root, "config", "--get", "core.hooksPath"), ".githooks")
        self.assertIn(".claude/worktrees/", self.read(root, ".git", "info", "exclude").splitlines())
        for line in ("created ticket #7: Adopt the harness scaffold", "created CLAUDE.md",
                     "opened PR #12 https://github.com/ryan/proj/pull/12", "PR #12 is ready for review"):
            self.assertIn(line, self.lines(p))

    def test_adopt_path_needs_an_origin_remote(self):
        root = self.repo_with_commit()
        self.stub("harness-board")
        p = self.scaffold("init", root, "--name", "proj", "--stack", "none")
        self.assertOneLineError(p, "no origin remote", "--github")
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(root, "CLAUDE.md")))


# =========================================================================== --github, --register, labels
class GitHub(ScaffoldTest):
    SCENARIO = [
        {"match": ["repo", "create"], "stdout": "https://github.com/ryan/demo-app\n"},
        {"match": ["repo", "view"], "stdout": "ryan/demo-app\n"},
        {"match": ["repo", "edit"], "stdout": ""},
    ]
    FORBIDDEN = {"match": ["api", "-X", "POST"], "exit": 1,
                 "stderr": "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. "
                           "(HTTP 403)\n"}

    def gh_calls(self):
        return [c for c in self.calls() if "tool" not in c]

    def test_github_creates_the_repo_and_handles_the_403_on_rulesets(self):
        origin = self.bare()
        self.stub("harness-board", {"labels": "15 labels created or updated.\n"})
        env = self.env(**self.fake_gh(self.SCENARIO + [self.FORBIDDEN], origin))
        root = os.path.join(self.tmp, "demo")
        p = self.ok("init", root, "--name", "demo-app", "--stack", "python", "--github", env=env)

        rows = self.calls()
        order = [r["tool"] + " " + r["argv"][0] if "tool" in r else "gh " + " ".join(r["argv"][:2]) for r in rows]
        self.assertEqual(order, ["gh repo create", "harness-board labels", "gh repo view", "gh repo edit",
                                 "gh api -X"])
        argvs = [c["argv"] for c in self.gh_calls()]
        self.assertEqual(argvs[0], ["repo", "create", "demo-app", "--private", "--source", ".", "--remote", "origin"])
        self.assertEqual(argvs[2], ["repo", "edit", "ryan/demo-app", "--delete-branch-on-merge"])
        self.assertEqual(argvs[3], ["api", "-X", "POST", "repos/ryan/demo-app/rulesets", "--input", "-"])
        self.assertEqual(self.calls("harness-board")[0]["argv"], ["labels", "ensure"])
        self.assertEqual(self.calls("harness-board")[0]["project"], root)

        ruleset = json.loads(self.gh_calls()[3]["stdin"])
        self.assertEqual((ruleset["name"], ruleset["target"], ruleset["enforcement"]), ("main", "branch", "active"))
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual([r["type"] for r in ruleset["rules"]], ["pull_request", "non_fast_forward", "deletion"])
        self.assertEqual(ruleset["rules"][0]["parameters"]["required_approving_review_count"], 0)

        self.assertIn(NO_RULESET, self.lines(p))
        self.assertIn("15 labels created or updated.", self.lines(p))
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(root, "rev-parse", "HEAD"))
        self.assertEqual(self.git(root, "rev-parse", "--abbrev-ref", "main@{upstream}"), "origin/main")

    def test_ruleset_created_when_the_plan_allows_it(self):
        self.stub("harness-board")
        env = self.env(**self.fake_gh(self.SCENARIO + [{"match": ["api", "-X", "POST"], "stdout": "{}"}],
                                      self.bare()))
        p = self.ok("init", os.path.join(self.tmp, "demo"), "--name", "demo-app", "--stack", "none", "--github",
                    env=env)
        self.assertIn("created ruleset main on ryan/demo-app", self.lines(p))
        self.assertNotIn(NO_RULESET, p.stdout)

    def test_github_runs_before_the_adoption_when_there_is_no_origin(self):
        root = self.repo_with_commit()
        origin = self.bare()
        branch = "t/3-adopt-the-harness-scaffold"
        worktree = os.path.join(self.tmp, "wt")
        self.git(root, "worktree", "add", "-q", worktree, "-b", branch, "main")
        self.stub("harness-board", {"labels": "ok\n", "task": "#3 https://github.com/ryan/demo-app/issues/3\n"})
        self.stub("harness-work", {"start": json.dumps({"number": 3, "branch": branch, "worktree": worktree})})
        self.stub("harness-pr", {"open": json.dumps({"number": 4, "url": "u"})})
        env = self.env(**self.fake_gh(self.SCENARIO + [self.FORBIDDEN], origin))
        p = self.ok("init", root, "--name", "demo-app", "--stack", "none", "--github", env=env)
        order = [r["tool"] + " " + r["argv"][0] if "tool" in r else "gh " + " ".join(r["argv"][:2])
                 for r in self.calls()]
        self.assertEqual(order, ["gh repo create", "harness-board labels", "gh repo view", "gh repo edit", "gh api -X",
                                 "harness-board task", "harness-work start", "harness-pr open", "harness-pr ready"])
        self.assertIn(NO_RULESET, self.lines(p))
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(root, "rev-parse", "main"))
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Adopt the harness scaffold (#3)")


class Register(ScaffoldTest):
    def test_register_adds_the_project_to_the_registry(self):
        root = os.path.join(self.tmp, "billed")
        # no harness-projects on PATH: the scaffold finds the real one next to itself
        p = self.ok("init", root, "--name", "billed", "--stack", "none", "--register", "--billable", "--client", "Acme")
        with open(os.path.join(self.home, ".claude", "harness", "projects.json"), encoding="utf-8") as fh:
            projects = json.load(fh)["projects"]
        self.assertEqual([(x["path"], x["name"], x["billable"], x["client"]) for x in projects],
                         [(root, "billed", True, "Acme")])
        self.assertIn("registered billed in the project registry", self.lines(p))

    def test_register_calls_harness_projects_add(self):
        self.stub("harness-projects")
        root = os.path.join(self.tmp, "solo")
        self.ok("init", root, "--name", "solo", "--stack", "none", "--register")
        self.assertEqual(self.calls("harness-projects")[0]["argv"], ["add", root, "--name", "solo"])

    def test_labels_runs_harness_board_labels_ensure(self):
        root = self.repo_with_commit()
        self.stub("harness-board", {"labels": "15 labels created or updated.\n"})
        p = self.ok("labels", root)
        call = self.calls("harness-board")[0]
        self.assertEqual((call["argv"], call["cwd"], call["project"]), (["labels", "ensure"], root, root))
        self.assertIn("15 labels created or updated.", p.stdout)
        self.assertOneLineError(self.scaffold("labels", self.mk("not-a-repo")), "not a git repository")


if __name__ == "__main__":
    unittest.main()
