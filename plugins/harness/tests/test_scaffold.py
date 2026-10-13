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
PLACEHOLDER = re.compile(r"(?<!\$)\{\{(name|pkg|stack|verify_cmd|test_cmd|tests_dirs|python_version|year)\}\}")

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

    def test_an_existing_manifest_without_a_tests_directory_names_none(self):
        # the skeleton is skipped when the manifest exists, so there is no tests/ to name
        for stack, manifest, text in (("python", "pyproject.toml", "[project]\nname = 'x'\n"),
                                      ("node", "package.json", '{"name": "x"}\n')):
            with self.subTest(stack=stack):
                root = self.mk(f"manifest-{stack}")
                self.write(os.path.join(root, manifest), text)
                p = self.ok("init", root, "--name", f"manifest-{stack}", "--stack", stack)
                self.assertIn(f"kept {manifest}", self.lines(p))
                self.assertFalse(os.path.exists(os.path.join(root, "tests")))
                section = self.where(root)
                self.assertFalse(self.names_dir(section, "tests"), section)
                self.assertFalse(self.names_dir(section, "test"), section)
            with self.subTest(stack=stack, on_disk="test"):                 # the disk still decides
                root = self.mk(f"manifest-{stack}-test")
                self.write(os.path.join(root, manifest), text)
                self.write(os.path.join(root, "test", "test_a.py"), "pass\n")
                self.ok("init", root, "--name", f"manifest-{stack}", "--stack", stack)
                section = self.where(root)
                self.assertTrue(self.names_dir(section, "test"), section)
                self.assertFalse(self.names_dir(section, "tests"), section)

    def test_no_tests_directory_is_not_guessed(self):
        root = self.mk("notests")
        self.ok("init", root, "--name", "notests", "--stack", "none")
        section = self.where(root)
        self.assertFalse(self.names_dir(section, "tests"), section)
        self.assertFalse(self.names_dir(section, "test"), section)
        self.assertIn("`scripts/verify.sh`", section)


# =========================================================================== skeleton (ticket #28)
# The skeleton of each stack, as written into an empty folder for --name demo-app.
SKELETON_FILES = {"python": ["pyproject.toml", "src/demo_app/__init__.py", "tests/test_smoke.py"],
                  "node": ["package.json", "tests/smoke.test.js"]}
LAYOUT = re.compile(r"kept the project's own layout \(([^)]*)\)")
STATUS_LINE = re.compile(r"^(?:would )?(create|created|keep|kept|update|updated) (\S+)$")
NORMAL = {"create": "created", "keep": "kept", "update": "updated"}
MAKEFILE_TARGETS = "py:\n\tpython3 -m unittest discover -s test\n\nlint:\n\techo lint\n"


class Skeleton(ScaffoldTest):
    """The skeleton (manifest, src/<pkg>, smoke test) is written only when the project has no manifest of
    any stack, no Makefile with a test or check target and no tests directory or test file; otherwise both
    --dry-run and the real run say `kept the project's own layout (<what was found>)`."""

    # ------------------------------------------------------------ helpers
    def fixture(self, label, files):
        root = self.mk(label)
        for rel, text in files.items():
            self.write(os.path.join(root, rel), text)
        return root

    def snapshot(self, root):
        """{relative path: content} of every file under root, .git left out."""
        out = {}
        for here, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in names:
                path = os.path.join(here, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    out[os.path.relpath(path, root).replace(os.sep, "/")] = fh.read()
        return out

    def both(self, root, stack, *extra):
        """--dry-run first (it must write nothing), then the real run, on the same folder."""
        before = self.snapshot(root)
        dry = self.ok("init", root, "--name", "demo-app", "--stack", stack, *extra, "--dry-run")
        self.assertEqual(self.snapshot(root), before, "--dry-run wrote into the project")
        real = self.ok("init", root, "--name", "demo-app", "--stack", stack, *extra)
        return dry, real

    def statuses(self, p):
        """{path: created|kept|updated} for the scaffold's file lines, `would ...` normalised."""
        out = {}
        for line in self.lines(p):
            m = STATUS_LINE.match(line)
            if m and not m.group(2).startswith(".git/") and not m.group(2).startswith("core."):
                out[m.group(2)] = NORMAL.get(m.group(1), m.group(1))
        return out

    def layout(self, p):
        """The names inside `kept the project's own layout (...)`, or None when the line is absent."""
        found = [LAYOUT.search(line) for line in self.lines(p)]
        found = [m for m in found if m]
        if not found:
            return None
        self.assertEqual(len(found), 1, p.stdout)
        return {name.strip() for name in found[0].group(1).split(",") if name.strip()}

    def assertSkipped(self, root, stack, before, dry, real, names):
        """No skeleton file planned or written, the fixture untouched, the reason named in both runs."""
        for rel in SKELETON_FILES[stack]:
            if rel in before:
                continue
            self.assertFalse(os.path.exists(os.path.join(root, rel)), f"skeleton file written: {rel}")
            self.assertNotIn(f"created {rel}", self.lines(real))
            self.assertNotIn(f"would create {rel}", self.lines(dry))
        if stack == "python" and not any(rel.startswith("src/") for rel in before):
            self.assertFalse(os.path.exists(os.path.join(root, "src")), "src/<pkg> written")
        after = self.snapshot(root)
        for rel, text in before.items():
            self.assertEqual(after.get(rel), text, f"the project's {rel} changed")
        for p, which in ((dry, "--dry-run"), (real, "real run")):
            got = self.layout(p)
            self.assertIsNotNone(got, f"{which} does not say why the skeleton was skipped:\n{p.stdout}")
            for name in names:
                self.assertIn(name, got, f"{which} does not name {name}: {got}")
        self.assertEqual(self.layout(dry), self.layout(real), "--dry-run and the real run name different things")
        self.assertEqual(self.statuses(dry), self.statuses(real), "--dry-run and the real run disagree")

    def assertLayoutIs(self, dry, real, expected):
        """Both runs name exactly `expected` in the layout line: nothing missing, nothing over-reported.
        The names are joined by `, ` inside the parentheses, as in the ticket's
        `kept the project's own layout (package.json, test/)`; their order is not pinned."""
        for p, which in ((dry, "--dry-run"), (real, "real run")):
            self.assertEqual(self.layout(p), set(expected), f"{which} names the wrong things:\n{p.stdout}")
            raw = [m.group(1) for m in (LAYOUT.search(line) for line in self.lines(p)) if m][0]
            self.assertEqual(sorted(raw.split(", ")), sorted(expected),
                             f"{which} does not join the names with ', ': ({raw})")

    def assertWritten(self, root, stack, dry, real):
        for rel in SKELETON_FILES[stack]:
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), f"skeleton file missing: {rel}")
            self.assertIn(f"created {rel}", self.lines(real))
            self.assertIn(f"would create {rel}", self.lines(dry))
        for p in (dry, real):
            self.assertIsNone(self.layout(p), p.stdout)
        self.assertEqual(self.statuses(dry), self.statuses(real), "--dry-run and the real run disagree")

    def where(self, root):
        text = self.read(root, "CLAUDE.md")
        return text.split("## Where things live", 1)[1].split("\n## ", 1)[0]

    def names_dir(self, section, directory):
        return re.search(r"`" + re.escape(directory) + r"/?`", section) is not None

    # ------------------------------------------------------------ tests
    def test_the_jotform_layout_keeps_its_own_layout(self):
        # package.json, a Makefile with check/test/py targets and tests under test/ (2026-10-14)
        files = {"package.json": '{"name": "jotform-glow-sync", "scripts": {"test": "make test"}}\n',
                 "Makefile": "check: lint test\n\ntest:\n\tpython3 -m unittest discover -s test -p '*_test.py'\n\n"
                             + MAKEFILE_TARGETS,
                 "test/x_test.py": "import unittest\n",
                 "sync.py": "print('sync')\n"}
        root = self.fixture("jotform-glow-sync", files)
        before = self.snapshot(root)
        dry, real = self.both(root, "python")
        self.assertSkipped(root, "python", before, dry, real, ["package.json", "test/"])
        # exactly these three: test/x_test.py is inside test/ and not named again, sync.py is not a blocker
        self.assertLayoutIs(dry, real, {"package.json", "Makefile", "test/"})
        section = self.where(root)
        self.assertTrue(self.names_dir(section, "test"), section)
        self.assertFalse(self.names_dir(section, "tests"), section)

    def test_no_skeleton_when_another_stacks_manifest_exists(self):
        manifests = {"pyproject.toml": "[project]\nname = 'x'\n", "package.json": '{"name": "x"}\n',
                     "setup.py": "from setuptools import setup\nsetup(name='x')\n",
                     "setup.cfg": "[metadata]\nname = x\n", "requirements.txt": "requests==2.32.3\n",
                     "Cargo.toml": "[package]\nname = \"x\"\nversion = \"0.1.0\"\n", "go.mod": "module example.com/x\n"}
        cases = [("python", m) for m in manifests if m != "pyproject.toml"]
        cases += [("node", m) for m in ("pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml", "go.mod")]
        cases += [("python", "pyproject.toml"), ("node", "package.json")]        # the stack's own, as today
        for stack, manifest in cases:
            with self.subTest(stack=stack, manifest=manifest):
                root = self.fixture(f"man-{stack}-{manifest.replace('.', '-')}", {manifest: manifests[manifest]})
                before = self.snapshot(root)
                dry, real = self.both(root, stack)
                self.assertSkipped(root, stack, before, dry, real, [manifest])
                self.assertLayoutIs(dry, real, {manifest})
                section = self.where(root)                     # no skeleton, so no tests/ to name
                self.assertFalse(self.names_dir(section, "tests"), section)
        with self.subTest(several=True):
            root = self.fixture("man-several", {"go.mod": manifests["go.mod"],
                                                "requirements.txt": manifests["requirements.txt"]})
            before = self.snapshot(root)
            dry, real = self.both(root, "node")
            self.assertSkipped(root, "node", before, dry, real, ["go.mod", "requirements.txt"])
            self.assertLayoutIs(dry, real, {"go.mod", "requirements.txt"})
        with self.subTest(force=True):                             # --force overwrites files, adds no skeleton
            root = self.fixture("man-force", {"package.json": manifests["package.json"]})
            before = self.snapshot(root)
            dry, real = self.both(root, "python", "--force")
            self.assertSkipped(root, "python", before, dry, real, ["package.json"])
            self.assertLayoutIs(dry, real, {"package.json"})

    def test_no_skeleton_when_a_tests_directory_or_makefile_test_target_exists(self):
        blocking = [
            ("tests-dir", {"tests/conftest.py": "import pytest\n"}, "tests/"),
            ("test-dir", {"test/helper.js": "module.exports = {};\n"}, "test/"),
            ("spec-dir", {"spec/spec_helper.rb": "require 'rspec'\n"}, "spec/"),
            ("test-pattern-in-test", {"test/x_test.py": "import unittest\n"}, "test/"),
            ("top-test-prefix", {"test_app.py": "def test_x():\n    pass\n"}, "test_app.py"),
            ("top-test-suffix", {"app_test.py": "def test_x():\n    pass\n"}, "app_test.py"),
            ("top-js-test", {"app.test.js": "require('node:test');\n"}, "app.test.js"),
            ("make-test", {"Makefile": "test:\n\tpython3 -m unittest\n"}, "Makefile"),
            ("make-check", {"Makefile": "build:\n\techo build\n\ncheck: build\n\techo ok\n"}, "Makefile"),
        ]
        for stack in ("python", "node"):
            for label, files, name in blocking:
                with self.subTest(stack=stack, blocker=label):
                    root = self.fixture(f"block-{stack}-{label}", files)
                    before = self.snapshot(root)
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, before, dry, real, [name])
                    self.assertLayoutIs(dry, real, {name})          # a file inside the directory is not named
                    if name != "tests/":
                        section = self.where(root)
                        self.assertFalse(self.names_dir(section, "tests"), section)
                        self.assertFalse(os.path.exists(os.path.join(root, "tests")))
        # none of these is a manifest, a tests directory, a test file or a test/check target
        not_blocking = [
            ("make-no-test-target", {"Makefile": MAKEFILE_TARGETS}),
            ("make-lookalike-targets", {"Makefile": "pytest:\n\tpytest\n\nrecheck:\n\techo again\n\n"
                                                    "testing:\n\techo t\n\nci:\n\t$(MAKE) py\n"}),
            ("readme-and-docs", {"README.md": "# demo\n", "docs/notes.md": "test: notes\n"}),
            ("non-test-sources", {"tool.py": "print(1)\n", "latest.py": "print(2)\n", "attest.js": "1;\n"}),
        ]
        for stack in ("python", "node"):
            for label, files in not_blocking:
                with self.subTest(stack=stack, not_blocking=label):
                    root = self.fixture(f"free-{stack}-{label}", files)
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    for rel, text in files.items():
                        self.assertEqual(self.read(root, rel), text, rel)

    def test_test_files_matching_the_test_patterns_block_at_any_depth_and_are_named_by_path(self):
        # The HARNESS_TEST_PATTERNS default (harness-red, tests_first.py): **/test_*.py, **/*_test.py,
        # **/conftest.py, **/*_test.js, **/*.test.js, **/*.test.ts, **/*_test.ts, at any depth.
        # A file is named by its path relative to the project root, with / separators.
        cases = ["test_sync.py", "sync_test.py", "conftest.py", "widget_test.js", "widget.test.js",
                 "widget.test.ts", "widget_test.ts",
                 "src/pkg/test_core.py", "src/pkg/core_test.py", "pkg/conftest.py", "lib/a.test.js",
                 "lib/b_test.js", "web/ui/button.test.ts", "web/ui/deep/er/button_test.ts"]
        for stack in ("python", "node"):
            for rel in cases:
                with self.subTest(stack=stack, file=rel):
                    root = self.fixture(f"pat-{stack}-{rel.replace('/', '-').replace('.', '-')}",
                                        {rel: "// a test\n" if rel.endswith(("js", "ts")) else "pass\n"})
                    before = self.snapshot(root)
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, before, dry, real, [rel])
                    self.assertLayoutIs(dry, real, {rel})
                    self.assertFalse(os.path.exists(os.path.join(root, "tests")))

    def test_lookalike_names_and_files_in_vcs_or_vendored_folders_do_not_block(self):
        not_blocking = [
            ("lookalikes", {"testdata.json": "{}\n", "contest.py": "pass\n", "protest.ts": "export {};\n",
                            "data/testdata.json": "{}\n", "src/contest.py": "pass\n",
                            "lib/protest.ts": "export {};\n", "testing.py": "pass\n", "mytest.js": "1;\n",
                            "latest_tests.txt": "x\n"}),
            ("node-modules", {"node_modules/x/a.test.js": "1;\n", "node_modules/x/test_x.py": "pass\n",
                              "node_modules/x/test/index.js": "1;\n", "node_modules/y/conftest.py": "pass\n"}),
        ]
        for stack in ("python", "node"):
            for label, files in not_blocking:
                with self.subTest(stack=stack, not_blocking=label):
                    root = self.fixture(f"look-{stack}-{label}", files)
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    for rel, text in files.items():
                        self.assertEqual(self.read(root, rel), text, rel)
            with self.subTest(stack=stack, not_blocking="dot-git"):
                root = self.mk(f"look-{stack}-dot-git")                 # an unborn repository
                self.git(root, "init", "-q", "-b", "main")
                for rel in (".git/info/sample_test.py", ".git/hooks/test_hook.py", ".git/x/a.test.js",
                            ".git/conftest.py"):
                    self.write(os.path.join(root, rel), "pass\n")
                os.makedirs(os.path.join(root, ".git", "tests"), exist_ok=True)
                dry, real = self.both(root, stack)
                self.assertWritten(root, stack, dry, real)

    def test_makefile_target_syntax_that_blocks(self):
        blocking = [
            ("several-on-a-line", "lint test: deps\n\techo t\n\ndeps:\n\techo d\n"),
            ("check-second-on-a-line", "all check:\n\techo ok\n"),
            ("space-before-colon", "build:\n\techo b\n\ntest :\n\techo t\n"),
            ("space-before-colon-check", "check : build\n\techo c\n\nbuild:\n\techo b\n"),
            ("double-colon", "test::\n\techo t\n"),
            ("double-colon-check-later", "build:\n\techo b\n\nfmt lint check:: build\n\techo c\n"),
            ("after-other-rules", "build:\n\techo b\n\nlint:\n\techo l\n\ntest: lint\n\techo t\n"),
            ("tab-separated-targets", "lint\ttest: deps\n\techo t\n"),
        ]
        for stack in ("python", "node"):
            for label, text in blocking:
                with self.subTest(stack=stack, makefile=label):
                    root = self.fixture(f"mk-{stack}-{label}", {"Makefile": text})
                    before = self.snapshot(root)
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, before, dry, real, ["Makefile"])
                    self.assertLayoutIs(dry, real, {"Makefile"})

    def test_makefile_lines_that_are_not_test_or_check_targets_do_not_block(self):
        not_blocking = [
            ("recipe-line", "build:\n\techo test:\n\techo check: done\n"),
            ("variables", "VAR = test:\nX := check:\nY ?= test: check:\nbuild:\n\techo $(VAR)\n"),
            ("assignment-to-a-test-variable", "test := unit\ncheck = no\nbuild:\n\techo $(test)\n"),
            ("comment", "# test: later\n# check: soon\nbuild:\n\techo b\n"),
            ("test-as-a-prerequisite-only", "build: test check\n\techo b\n"),
        ]
        for stack in ("python", "node"):
            for label, text in not_blocking:
                with self.subTest(stack=stack, makefile=label):
                    root = self.fixture(f"mkfree-{stack}-{label}", {"Makefile": text})
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    self.assertEqual(self.read(root, "Makefile"), text)

    def test_an_empty_tests_directory_blocks_and_a_file_named_like_one_does_not(self):
        for stack in ("python", "node"):
            for directory in ("tests", "test", "spec"):
                with self.subTest(stack=stack, empty_dir=directory):
                    root = self.mk(f"emptydir-{stack}-{directory}")
                    os.makedirs(os.path.join(root, directory))
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, {}, dry, real, [directory + "/"])
                    self.assertLayoutIs(dry, real, {directory + "/"})
                    self.assertEqual(os.listdir(os.path.join(root, directory)), [])   # left empty
            for name in ("test", "spec"):                                 # a regular file, not a directory
                with self.subTest(stack=stack, file=name):
                    root = self.fixture(f"plainfile-{stack}-{name}", {name: "not a directory\n"})
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    self.assertEqual(self.read(root, name), "not a directory\n")
            with self.subTest(stack=stack, file="tests"):                 # with a manifest: only it is named
                root = self.fixture(f"plainfile-{stack}-tests", {"tests": "not a directory\n",
                                                                  "package.json": '{"name": "x"}\n'})
                before = self.snapshot(root)
                dry, real = self.both(root, stack)
                self.assertSkipped(root, stack, before, dry, real, ["package.json"])
                self.assertLayoutIs(dry, real, {"package.json"})
                self.assertEqual(self.read(root, "tests"), "not a directory\n")

    def test_every_loose_test_file_is_named_not_just_the_first(self):
        # small fixtures only; how a long list is shown is left open
        cases = [
            ("spread", ["test_a.py", "lib/b_test.js", "web/c.test.ts"]),
            ("same-dir-top", ["test_a.py", "a_test.py"]),
            ("same-dir-nested", ["lib/a.test.js", "lib/b_test.js"]),
        ]
        for stack in ("python", "node"):
            for label, rels in cases:
                with self.subTest(stack=stack, files=label):
                    root = self.fixture(f"loose-{stack}-{label}",
                                        {rel: "// a test\n" if rel.endswith(("js", "ts")) else "pass\n"
                                         for rel in rels})
                    before = self.snapshot(root)
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, before, dry, real, rels)
                    self.assertLayoutIs(dry, real, set(rels))
                    self.assertFalse(os.path.exists(os.path.join(root, "tests")))

    def test_every_kind_of_blocker_in_one_project_is_named(self):
        files = {"go.mod": "module example.com/x\n",
                 "Makefile": "build:\n\techo b\n\ncheck: build\n\techo ok\n",
                 "spec/spec_helper.rb": "require 'rspec'\n",
                 "widget.test.js": "require('node:test');\n",
                 "main.go": "package main\n"}
        expected = {"go.mod", "Makefile", "spec/", "widget.test.js"}
        for stack in ("python", "node"):
            with self.subTest(stack=stack):
                root = self.fixture(f"combo-{stack}", files)
                before = self.snapshot(root)
                dry, real = self.both(root, stack)
                self.assertSkipped(root, stack, before, dry, real, sorted(expected))
                self.assertLayoutIs(dry, real, expected)

    def test_gnu_makes_other_makefile_names(self):
        # GNU make reads GNUmakefile, makefile and Makefile. One file per fixture, so on a case-insensitive
        # filesystem `makefile` is still listed under the name it was created with, and that name is expected.
        with_target = {"test": "build:\n\techo b\n\ntest: build\n\techo t\n",
                       "check": "check:\n\techo ok\n"}
        for stack in ("python", "node"):
            for name in ("makefile", "GNUmakefile"):
                for target, text in with_target.items():
                    with self.subTest(stack=stack, makefile=name, target=target):
                        root = self.fixture(f"gnu-{stack}-{name}-{target}", {name: text})
                        self.assertEqual(os.listdir(root), [name])
                        before = self.snapshot(root)
                        dry, real = self.both(root, stack)
                        self.assertSkipped(root, stack, before, dry, real, [name])
                        self.assertLayoutIs(dry, real, {name})
                with self.subTest(stack=stack, makefile=name, target=None):
                    root = self.fixture(f"gnufree-{stack}-{name}", {name: MAKEFILE_TARGETS})
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    self.assertEqual(self.read(root, name), MAKEFILE_TARGETS)

    def test_empty_folder_still_gets_the_skeleton(self):
        for stack in ("python", "node"):
            with self.subTest(stack=stack):
                root = self.mk(f"empty-{stack}")
                dry, real = self.both(root, stack)
                self.assertWritten(root, stack, dry, real)
                expected = set(COMMON_FILES + SKELETON_FILES[stack])
                self.assertEqual(set(self.snapshot(root)), expected)        # exactly what is written today
                self.assertEqual({p for p, s in self.statuses(real).items() if s == "created"}, expected)
                self.assertTrue(self.names_dir(self.where(root), "tests"), self.where(root))
        with self.subTest(stack="python", folder="missing"):              # a path that does not exist yet
            root = os.path.join(self.tmp, "fresh-python")
            self.ok("init", root, "--name", "demo-app", "--stack", "python")
            self.assertEqual(set(self.snapshot(root)), set(COMMON_FILES + SKELETON_FILES["python"]))

    # ------------------------------------------------------------ review of PR #38
    def test_dot_directories_virtualenvs_and_pycache_are_not_walked(self):
        # The walk skips every dot-directory, node_modules, __pycache__ and any directory holding a
        # pyvenv.cfg (a virtualenv under any name). A plain script beside them still gets the skeleton.
        venv_cfg = "home = /usr/bin\nversion = 3.12.4\n"
        not_blocking = [
            ("dot-venv", {".venv/pyvenv.cfg": venv_cfg,
                          ".venv/lib/python3.12/site-packages/numpy/tests/test_abc.py": "pass\n",
                          ".venv/lib/python3.12/site-packages/numpy/conftest.py": "pass\n"}),
            ("dot-venv-no-cfg", {".venv/lib/python3.12/site-packages/pkg/test_x.py": "pass\n"}),
            ("dot-tox", {".tox/py312/lib/python3.12/site-packages/pkg/test_x.py": "pass\n",
                         ".tox/py312/lib/python3.12/site-packages/pkg/a_test.py": "pass\n"}),
            ("dot-claude-worktree", {".claude/worktrees/t-1-x/tests/test_a.py": "pass\n",
                                     ".claude/worktrees/t-1-x/src/b_test.py": "pass\n",
                                     ".claude/worktrees/t-1-x/web/c.test.js": "1;\n"}),
            ("dot-caches", {".mypy_cache/3.12/conftest.py": "pass\n",
                            ".pytest_cache/v/test_cache.py": "pass\n", ".hidden/deep/x.test.ts": "1;\n"}),
            ("venv-by-cfg", {"venv/pyvenv.cfg": venv_cfg,
                             "venv/lib/python3.12/site-packages/pkg/tests/test_x.py": "pass\n",
                             "venv/test_y.py": "pass\n"}),
            ("env-by-cfg", {"env/pyvenv.cfg": venv_cfg,
                            "env/lib/python3.12/site-packages/pkg/conftest.py": "pass\n"}),
            ("nested-venv-by-cfg", {"tools/pyenv312/pyvenv.cfg": venv_cfg,
                                    "tools/pyenv312/lib/python3.12/site-packages/q/q_test.py": "pass\n"}),
            ("pycache", {"__pycache__/test_x.py": "pass\n", "pkg/__pycache__/conftest.py": "pass\n"}),
        ]
        for stack in ("python", "node"):
            for label, files in not_blocking:
                with self.subTest(stack=stack, not_blocking=label):
                    files = dict(files, **{"analysis.py": "print(1)\n"})
                    root = self.fixture(f"skipdir-{stack}-{label}", files)
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    for rel, text in files.items():
                        self.assertEqual(self.read(root, rel), text, rel)

    def test_a_directory_named_like_a_virtualenv_without_pyvenv_cfg_still_blocks(self):
        # the name alone is not a virtualenv; skipped directories do not hide the project's own test files
        venv_cfg = "home = /usr/bin\nversion = 3.12.4\n"
        cases = [
            ("env-no-cfg", {"env/test_x.py": "pass\n"}, {"env/test_x.py"}),
            ("venv-no-cfg", {"venv/lib/pkg/x_test.py": "pass\n"}, {"venv/lib/pkg/x_test.py"}),
            ("own-beside-skipped", {".venv/pyvenv.cfg": venv_cfg,
                                    ".venv/lib/python3.12/site-packages/numpy/tests/test_abc.py": "pass\n",
                                    "env/pyvenv.cfg": venv_cfg, "env/lib/conftest.py": "pass\n",
                                    "__pycache__/test_c.py": "pass\n",
                                    ".claude/worktrees/t-1-x/test_w.py": "pass\n",
                                    "src/pkg/test_core.py": "pass\n"},
             {"src/pkg/test_core.py"}),
            ("cfg-in-a-sibling-only", {"a/pyvenv.cfg": venv_cfg, "a/test_in_venv.py": "pass\n",
                                       "b/test_real.py": "pass\n"}, {"b/test_real.py"}),
        ]
        for stack in ("python", "node"):
            for label, files, expected in cases:
                with self.subTest(stack=stack, case=label):
                    root = self.fixture(f"venvname-{stack}-{label}", files)
                    before = self.snapshot(root)
                    dry, real = self.both(root, stack)
                    self.assertSkipped(root, stack, before, dry, real, sorted(expected))
                    self.assertLayoutIs(dry, real, expected)

    def test_makefile_immediate_assignments_are_not_targets(self):
        # `::=` (POSIX) and `:::=` (GNU make 4.4) assign a variable; they define no rule
        not_blocking = [
            ("posix-immediate", "test ::= unit\nbuild:\n\techo $(test)\n"),
            ("gnu-immediate", "check:::= x\nbuild:\n\techo b\n"),
            ("both-no-spaces", "test::=unit\ncheck :::= x\nbuild: deps\n\techo b\n\ndeps:\n\techo d\n"),
        ]
        for stack in ("python", "node"):
            for label, text in not_blocking:
                with self.subTest(stack=stack, makefile=label):
                    root = self.fixture(f"mkassign-{stack}-{label}", {"Makefile": text})
                    dry, real = self.both(root, stack)
                    self.assertWritten(root, stack, dry, real)
                    self.assertEqual(self.read(root, "Makefile"), text)
            with self.subTest(stack=stack, makefile="assignment-and-a-real-test-rule"):
                text = "X ::= 1\nY :::= 2\ntest::\n\techo $(X)\n"
                root = self.fixture(f"mkassign-{stack}-real-rule", {"Makefile": text})
                before = self.snapshot(root)
                dry, real = self.both(root, stack)
                self.assertSkipped(root, stack, before, dry, real, ["Makefile"])
                self.assertLayoutIs(dry, real, {"Makefile"})

    def test_python_test_command_follows_whether_the_skeleton_is_written(self):
        # uv only when the project is a uv project or the skeleton is about to make it one
        jotform = {"package.json": '{"name": "jotform-glow-sync", "scripts": {"test": "make test"}}\n',
                   "Makefile": "check: lint test\n\ntest:\n\tpython3 -m unittest discover -s test -p '*_test.py'\n\n"
                               + MAKEFILE_TARGETS,
                   "test/x_test.py": "import unittest\n"}
        cases = [
            ("jotform", jotform, "python3 -m pytest -q {files}"),
            ("requirements-only", {"requirements.txt": "requests==2.32.3\n"}, "python3 -m pytest -q {files}"),
            ("loose-test-file", {"test_app.py": "def test_x():\n    pass\n"}, "python3 -m pytest -q {files}"),
            ("make-test-only", {"Makefile": "test:\n\tpython3 -m unittest\n"}, "python3 -m pytest -q {files}"),
            ("uv-lock-and-requirements", {"uv.lock": "version = 1\n", "requirements.txt": "requests==2.32.3\n"},
             "uv run pytest -q {files}"),
            ("pyproject-no-lock", {"pyproject.toml": "[project]\nname = 'x'\n"}, "python3 -m pytest -q {files}"),
            ("empty", {}, "uv run pytest -q {files}"),
        ]
        for label, files, expected in cases:
            with self.subTest(case=label):
                root = self.fixture(f"testcmd-{label}", files)
                self.ok("init", root, "--name", "demo-app", "--stack", "python")
                self.assertEqual(self.settings(root)["env"]["HARNESS_TEST_CMD"], expected)
                self.assertEqual(os.path.exists(os.path.join(root, "src", "demo_app")), not files, label)

    def test_a_long_list_of_loose_test_files_is_cut_after_ten(self):
        rels = [f"t/test_{n:02d}.py" for n in range(12)]
        for stack in ("python", "node"):
            with self.subTest(stack=stack):
                root = self.fixture(f"many-{stack}", {rel: "pass\n" for rel in rels})
                before = self.snapshot(root)
                dry, real = self.both(root, stack)
                self.assertSkipped(root, stack, before, dry, real, [])
                for p, which in ((dry, "--dry-run"), (real, "real run")):
                    raw = [m.group(1) for m in (LAYOUT.search(line) for line in self.lines(p)) if m]
                    self.assertEqual(len(raw), 1, p.stdout)
                    items = raw[0].split(", ")
                    self.assertEqual(len(items), 11, f"{which}: ({raw[0]})")
                    self.assertEqual(items[-1], "+2 more test files", f"{which}: ({raw[0]})")
                    shown = items[:-1]
                    self.assertEqual(len(set(shown)), 10, f"{which}: ({raw[0]})")
                    self.assertTrue(set(shown) <= set(rels), f"{which}: ({raw[0]})")

    def test_the_adopt_path_keeps_the_jotform_layout(self):
        files = {"package.json": '{"name": "jotform-glow-sync", "scripts": {"test": "make test"}}\n',
                 "Makefile": "check: lint test\n\ntest:\n\tpython3 -m unittest discover -s test -p '*_test.py'\n\n"
                             + MAKEFILE_TARGETS,
                 "test/x_test.py": "import unittest\n",
                 "sync.py": "print('sync')\n"}
        root = self.repo_with_commit("jot")
        for rel, text in files.items():
            self.write(os.path.join(root, rel), text)
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", "The project")
        self.git(root, "remote", "add", "origin", self.bare())
        self.git(root, "push", "-q", "-u", "origin", "main")
        main_head = self.git(root, "rev-parse", "HEAD")
        branch = "t/7-adopt-the-harness-scaffold"
        worktree = os.path.join(root, ".claude", "worktrees", "t-7-adopt-the-harness-scaffold")
        self.git(root, "worktree", "add", "-q", worktree, "-b", branch, "origin/main")
        self.stub("harness-board", {"task": "#7 https://github.com/ryan/jot/issues/7\n"})
        self.stub("harness-work", {"start": json.dumps({"number": 7, "branch": branch, "worktree": worktree,
                                                        "tier": "sonnet", "context": "# 7"})})
        self.stub("harness-pr", {"open": json.dumps({"number": 12, "url": "https://github.com/ryan/jot/pull/12"}),
                                 "ready": "PR #12 ready for review\n"})

        before = self.snapshot(root)
        dry = self.ok("init", root, "--name", "jot", "--stack", "python", "--dry-run")
        self.assertEqual(self.snapshot(root), before, "--dry-run wrote into the project")
        self.assertEqual(self.calls(), [])
        real = self.ok("init", root, "--name", "jot", "--stack", "python")

        self.assertLayoutIs(dry, real, {"package.json", "Makefile", "test/"})
        skeleton = ["pyproject.toml", "src/jot/__init__.py", "tests/test_smoke.py"]
        for rel in skeleton:
            self.assertNotIn(f"would create {rel}", self.lines(dry))
            self.assertNotIn(f"created {rel}", self.lines(real))
            self.assertFalse(os.path.exists(os.path.join(worktree, rel)), rel)
            self.assertFalse(os.path.exists(os.path.join(root, rel)), rel)
        committed = self.git(worktree, "show", "--name-only", "--format=", "HEAD").splitlines()
        for rel in COMMON_FILES:
            self.assertIn(rel, committed)
        for rel in skeleton + list(files):
            self.assertNotIn(rel, committed)
        self.assertFalse(os.path.exists(os.path.join(worktree, "src")))
        self.assertFalse(os.path.exists(os.path.join(worktree, "tests")))
        for rel, text in files.items():
            self.assertEqual(self.read(worktree, rel), text, rel)
        self.assertEqual(self.git(root, "rev-parse", "main"), main_head)
        self.assertEqual(self.settings(worktree)["env"]["HARNESS_TEST_CMD"], "python3 -m pytest -q {files}")


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

    def test_a_blank_verify_is_a_one_line_error_and_writes_nothing(self):
        for n, blank in enumerate(("", "   ", "\t", " \n ")):
            with self.subTest(verify=blank):
                fresh = os.path.join(self.tmp, f"blank-fresh{n}")
                self.assertOneLineError(self.scaffold("init", fresh, "--name", "blank", "--stack", "none",
                                                      "--verify", blank), "--verify")
                self.assertFalse(os.path.exists(fresh))
                for stack in ("none", "python"):
                    existing = self.mk(f"blank-{stack}{n}")
                    self.assertOneLineError(self.scaffold("init", existing, "--name", "blank", "--stack", stack,
                                                          "--verify", blank), "--verify")
                    self.assertFalse(os.path.exists(os.path.join(existing, "scripts", "verify.sh")))
                    self.assertEqual(os.listdir(existing), [])

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
