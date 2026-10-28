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
import random
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
RUFF = shutil.which("ruff")      # the real ruff, looked up on the PATH the suite runs with, never the fakebin
NO_RULESET = ("Branch protection is unavailable on this plan for a private repo; "
              "the local branch guard is the only block.")
PLACEHOLDER = re.compile(r"(?<!\$)\{\{(name|pkg|stack|verify_cmd|test_cmd|tests_dirs|python_version|year)\}\}")

COMMON_FILES = ["CLAUDE.md", ".claude/settings.json", "scripts/verify.sh", ".github/workflows/ci.yml",
                ".github/workflows/deploy.yml", ".github/pull_request_template.md",
                ".github/ISSUE_TEMPLATE/task.md", ".github/ISSUE_TEMPLATE/epic.md", ".githooks/pre-commit",
                ".gitignore", ".worktreeinclude", "scripts/smoke.sh", "scripts/deploy_guard.py"]

# The allow entries every render starts with (ticket #137): the twelve git, gh and verify entries, then one
# narrow rule per harness tool in this order. The old wildcard spelling may be dropped by auto mode.
BASE_ALLOW = ["Bash(scripts/verify.sh)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
              "Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)", "Bash(git fetch:*)",
              "Bash(git worktree:*)", "Bash(gh issue:*)", "Bash(gh pr:*)", "Bash(gh api:*)"]
HARNESS_NINE = [f"Bash(harness-{tool}:*)" for tool in ("pr", "board", "work", "red", "scaffold", "digest",
                                                       "standup", "projects", "worklog")]
WILDCARD = "Bash(harness-*:*)"
OLD_SPELLING_WARN = ("warn: .claude/settings.json allow rule Bash(harness-*:*) matches no harness tool "
                     "(a ':*' rule is a literal prefix); replace it with the nine harness-<tool> rules: "
                     + ", ".join(HARNESS_NINE))
REPO = os.path.dirname(os.path.dirname(PLUGIN))

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
               if k not in ("CLAUDE_PROJECT_DIR", "HARNESS_SESSION_PROJECT", "HARNESS_GH", "GIT_DIR", "GIT_WORK_TREE",
                            "GIT_INDEX_FILE")}
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
        for rule in ["Bash(scripts/verify.sh)", "Bash(git commit:*)", "Bash(git worktree:*)", "Bash(gh api:*)",
                     "Bash(uv run pytest:*)"] + HARNESS_NINE:
            self.assertIn(rule, allow)
        self.assertNotIn(WILDCARD, allow)

        self.assertEqual(self.git(root, "config", "--get", "core.hooksPath"), ".githooks")
        self.assertIn(".claude/worktrees/", self.read(root, ".git", "info", "exclude").splitlines())
        self.assertIn(".env", self.read(root, ".worktreeinclude").splitlines())
        self.assertIn(".claude/worktrees/", self.read(root, ".gitignore").splitlines())

    def test_settings_allow_the_nine_harness_tools_not_the_wildcard(self):
        """Every stack renders the twelve base entries, then the nine harness-<tool> rules in order, then
        only the stack's own extras; the wildcard Bash(harness-*:*) appears nowhere (ticket #137)."""
        extras = {"none": [], "python": ["Bash(uv run pytest:*)", "Bash(uv run ruff:*)", "Bash(python3 -m pytest:*)"],
                  "node": ["Bash(npm test:*)", "Bash(npm run lint:*)", "Bash(node --test:*)"]}
        for stack, own in extras.items():
            with self.subTest(stack=stack):
                root = os.path.join(self.tmp, f"nine-{stack}")
                self.ok("init", root, "--name", "nine", "--stack", stack)
                allow = self.settings(root)["permissions"]["allow"]
                self.assertNotIn(WILDCARD, allow)
                self.assertNotIn(WILDCARD, self.read(root, ".claude", "settings.json"))
                self.assertEqual([r for r in allow if r.startswith("Bash(harness-")], HARNESS_NINE)
                self.assertEqual(allow, BASE_ALLOW + HARNESS_NINE + own)
        # the template itself, which kept-settings checks compare against, holds the same
        template = json.loads(self.read(TEMPLATES, "settings.json"))["permissions"]["allow"]
        self.assertEqual(template, BASE_ALLOW + HARNESS_NINE)

    def test_harness_own_settings_allow_the_nine_not_the_wildcard(self):
        """Harness's own .claude/settings.json follows the template: the nine rules, no wildcard.

        Its env holds exactly the verify command, the test map (#264) and ultrareview off (#283); any other
        key fails."""
        data = json.loads(self.read(REPO, ".claude", "settings.json"))
        allow = data["permissions"]["allow"]
        self.assertNotIn(WILDCARD, allow)
        self.assertEqual(allow, BASE_ALLOW + HARNESS_NINE)
        env = data["env"]
        self.assertEqual(set(env), {"CLAUDE_VERIFY_CMD", "HARNESS_TEST_MAP", "HARNESS_ULTRAREVIEW"})
        self.assertEqual(env["HARNESS_ULTRAREVIEW"], "off")
        self.assertEqual(env["CLAUDE_VERIFY_CMD"], "python3 -m unittest discover -s plugins/harness/tests")
        pairs = env["HARNESS_TEST_MAP"].split(",")
        for pair in ("plugins/harness/hooks/branch_guard.py=plugins/harness/tests/test_guards.py",
                     "plugins/harness/skills/**=plugins/harness/tests/test_work.py"):
            self.assertIn(pair, pairs)

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

    def test_ci_writes_smoke_and_deploy_guard_executable(self):
        guard = self.read(TEMPLATES, "deploy_guard.py")
        for stack in ("python", "node", "none"):
            with self.subTest(stack=stack):
                root = os.path.join(self.tmp, f"live-{stack}")
                p = self.ok("init", root, "--name", f"live-{stack}", "--stack", stack)
                for rel in ("scripts/smoke.sh", "scripts/deploy_guard.py"):
                    self.assertIn(f"created {rel}", self.lines(p))
                    self.assertExecutable(os.path.join(root, rel))
                with open(os.path.join(root, "scripts", "deploy_guard.py"), "rb") as fh, \
                        open(os.path.join(TEMPLATES, "deploy_guard.py"), "rb") as tmpl:
                    self.assertEqual(fh.read(), tmpl.read())      # byte-identical: no placeholder touched
                self.assertEqual(self.read(root, "scripts", "deploy_guard.py"), guard)
                smoke = os.path.join(root, "scripts", "smoke.sh")
                text = self.read(smoke)
                self.assertTrue(text.startswith("#!/bin/sh\n"), text)
                for piece in ("0 healthy", "1 big failure", "revert", "2 degraded"):
                    self.assertIn(piece, text)
                self.assertEqual(text.strip().splitlines()[-1], "exit 0")
                self.assertEqual(subprocess.run(["sh", "-n", smoke]).returncode, 0)
                self.assertEqual(subprocess.run([smoke], cwd=root).returncode, 0)
                deploy = self.read(root, ".github/workflows/deploy.yml")
                self.assertEqual(deploy, self.read(TEMPLATES, "deploy.yml"))       # v2, as is
                self.assertIn('python3 scripts/deploy_guard.py run --sha "$GITHUB_SHA"', deploy)
                self.assertIn("${{ github.token }}", deploy)

    def test_no_ci_skips_smoke_and_deploy_guard(self):
        root = os.path.join(self.tmp, "noci-live")
        p = self.ok("init", root, "--name", "noci-live", "--stack", "python", "--no-ci")
        for rel in ("scripts/smoke.sh", "scripts/deploy_guard.py", ".github/workflows/deploy.yml"):
            self.assertFalse(os.path.exists(os.path.join(root, rel)), rel)
            self.assertNotIn(rel, p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(root, "scripts", "verify.sh")))

    def test_existing_smoke_script_is_kept(self):
        root = self.mk("own-smoke")
        own = "#!/bin/sh\ncurl -fsS https://example.invalid/health\n"
        self.write(os.path.join(root, "scripts", "smoke.sh"), own, 0o700)
        p = self.ok("init", root, "--name", "own-smoke", "--stack", "none")
        self.assertIn("kept scripts/smoke.sh", self.lines(p))
        self.assertIn("created scripts/deploy_guard.py", self.lines(p))
        self.assertEqual(self.read(root, "scripts", "smoke.sh"), own)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, "scripts", "smoke.sh")).st_mode), 0o700)

    def test_dry_run_lists_the_new_scripts(self):
        root = os.path.join(self.tmp, "dry-live")
        p = self.ok("init", root, "--name", "dry-live", "--stack", "python", "--dry-run")
        for rel in ("scripts/smoke.sh", "scripts/deploy_guard.py", ".github/workflows/deploy.yml"):
            self.assertIn(f"would create {rel}", self.lines(p))
        self.assertFalse(os.path.exists(root))

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


# =========================================================================== kept settings (ticket #31)
with open(os.path.join(TEMPLATES, "settings.json"), encoding="utf-8") as _fh:
    _TEMPLATE_SETTINGS = json.load(_fh)
TEMPLATE_ALLOW = list(_TEMPLATE_SETTINGS["permissions"]["allow"])      # what stack none renders
TEMPLATE_ENV = list(_TEMPLATE_SETTINGS["env"])                          # what stacks python and node render
# One rule for each command the loop needs; each is also a template allow entry.
LOOP_RULES = ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)", "Bash(git fetch:*)",
              "Bash(git worktree:*)", "Bash(gh issue:*)", "Bash(gh pr:*)", "Bash(gh api:*)",
              "Bash(harness-pr:*)", "Bash(scripts/verify.sh)"]
# Rules are Bash command prefixes at word boundaries: a rule that covers a loop command, or sits inside
# one, blocks the loop. As an ask or deny, the old wildcard spelling counts for every harness tool.
BROADER_RULES = ["Bash", "Bash(*)", "Bash(git:*)", "Bash(gh:*)", "Bash(harness-*:*)"]
NARROWER_RULES = ["Bash(git commit -m:*)", "Bash(git push --force:*)", "Bash(git push origin main)",
                  "Bash(git worktree add:*)", "Bash(gh pr create:*)", "Bash(harness-work:*)"]
# Share a prefix or a substring with a loop command without being one; non-Bash rules are not commands.
NEAR_MISSES = ["Bash(git address-book:*)", "Bash(git committer:*)", "Bash(git pushd:*)",
               "Bash(npm run verify-docs:*)", "Bash(my-gh-pr-tool:*)", "Bash(npm run harness-docs:*)",
               "Read(./secrets/**)", "Read(./git commit notes/**)", "WebFetch", "Edit(./src/**)"]

# Generated cases: the loop commands as words, pools to extend them with, and words that sit next to them.
# No pool word is a kind word (ask, deny, allow, env), so a warn line's kind stays unambiguous.
LOOP_COMMANDS = [["git", "add"], ["git", "commit"], ["git", "push"], ["git", "fetch"], ["git", "worktree"],
                 ["gh", "issue"], ["gh", "pr"], ["gh", "api"], ["scripts/verify.sh"]]
SUFFIX_POOL = ["-m", "--amend", "--force", "--no-verify", "origin", "main", "feature/x", "t/7-fix", "-u", "--all",
               "HEAD~1", "src/app.py", ".", "-q", "--dry-run", "repos/x/y", "create", "view", "list", "42",
               "--json", "number,title", "-b", "../wt", "--title", "Fix", "-p", "prune", "remove", "upstream",
               "-v", "docs/a.md", "--state", "open", "-F", "body.md", "--fast", "tests/unit"]
APPENDED = ["x", "s", "er", "-all", "-tool", "2", "_old", "d"]
SIBLINGS = {"git": ["status", "diff", "log", "show", "blame", "tag", "stash", "config", "clone", "init",
                    "rev-parse", "describe", "shortlog", "grep", "notes", "bisect"],
            "gh": ["repo", "run", "release", "workflow", "gist", "search", "secret", "browse", "codespace", "cache"],
            "": ["scripts/lint.sh", "scripts/verify-docs.sh", "scripts/deploy.sh"]}
HARNESS_TOOLS = ["board", "work", "pr", "red", "scaffold", "worklog", "new-tool", "zz"]
FIRST_ARGS = ["-x", "--collect-only", "run", "lint", "--slow", "-c", "install", "clean", "build", "fmt"]


def _bash(words, star):
    return f"Bash({' '.join(words)}{':*' if star else ''})"


def _words(rule):
    """The command words of a Bash(...) rule, without its trailing :*."""
    inner = rule[len("Bash("):-1]
    return (inner[:-2] if inner.endswith(":*") else inner).split()


def _unique(rules):
    return list(dict.fromkeys(rules))


def blocking_rules(rng, words):
    """Rules that stop the command `words`: it exactly and as a prefix, every shorter prefix of it, and it
    extended by random suffixes, each with and without :*."""
    rules = [_bash(words, False), _bash(words, True)] + [_bash(words[:i], True) for i in range(1, len(words))]
    for _ in range(3):
        tail = rng.sample(SUFFIX_POOL, rng.randint(1, 3))
        rules += [_bash(words + tail, False), _bash(words + tail, True)]
    return _unique(rules)


def near_miss_rules(rng, words):
    """Rules next to the command `words` that do not stop it: its last word with letters appended, sibling
    subcommands, the words after another program, and non-Bash rules naming it."""
    head, last = words[:-1], words[-1]
    rules = []
    for extra in rng.sample(APPENDED, 3):
        rules += [_bash(head + [last + extra], True), _bash(head + [last + extra, rng.choice(SUFFIX_POOL)], False)]
    rules += [_bash(head + [other], True) for other in rng.sample(SIBLINGS[" ".join(head)], 3)]
    rules += [_bash(["echo"] + words, True), _bash(["x" + words[0]] + words[1:], True),
              f"Read(./{last}/**)", f"Write(./notes/{last}.md)"]
    return _unique(rules)


def harness_rules(rng):
    """(blocking, near misses) for the harness-* tools."""
    blocking, misses = ["Bash(harness-*:*)"], ["Bash(harness:*)"]
    for tool in rng.sample(HARNESS_TOOLS, 3):
        blocking += blocking_rules(rng, [f"harness-{tool}"])
        misses += [_bash(["harness", tool], True), _bash([f"harnessx-{tool}"], True),
                   _bash([f"xharness-{tool}"], True), _bash([f"my-harness-{tool}"], True),
                   _bash(["echo", f"harness-{tool}"], True), f"Read(./harness-{tool}/**)"]
    return _unique(blocking), _unique(misses)


def verify_rules(rng, verify):
    """(blocking, near misses) for a verify command: exact, as a prefix, every shorter prefix down to the
    program alone, extended with more arguments; and the same program with another first argument or a
    longer first word, another program, and the command after another program."""
    words = verify.split()
    tail = rng.sample(SUFFIX_POOL, 2)
    blocking = _unique([_bash(words, False), _bash(words, True), _bash(words[:1], True)]
                       + [_bash(words[:i], True) for i in range(2, len(words))]
                       + [_bash(words + tail[:1], False), _bash(words + tail, True)])
    first = rng.choice([a for a in FIRST_ARGS if a != words[1]])
    misses = [_bash([words[0], first], True), _bash([words[0], first] + words[2:], False),
              _bash([words[0], words[1] + "x"], True), _bash([words[0] + "x"], True), _bash(["echo"] + words, True)]
    return blocking, _unique(misses)


class KeptSettings(ScaffoldTest):
    """A kept .claude/settings.json is never edited, but init says what in it stops a dispatched run
    (a headless run cannot answer an ask): every ask or deny rule that covers or overlaps a command the
    loop needs, every allow entry of the stack's rendered template that no allow rule covers, every env
    key of that template that is absent. Each is one line starting `warn:` that names the rule, entry or
    key verbatim and says its kind (ask, deny, allow, env). A file that cannot be read as settings gets
    one warn line."""

    def reference(self, stack, verify=None):
        """(allow list, env) of the settings.json the scaffold itself renders for this stack and verify."""
        cache = self.__dict__.setdefault("_reference", {})
        if (stack, verify) not in cache:
            root = os.path.join(self.tmp, f"reference-{len(cache)}")
            self.ok("init", root, "--name", "ref", "--stack", stack, *(["--verify", verify] if verify else []))
            data = self.settings(root)
            cache[(stack, verify)] = (list(data["permissions"]["allow"]), dict(data["env"]))
        return cache[(stack, verify)]

    def kept(self, name, stack="none", verify=None, ask=(), deny=(), allow=None, env=None, text=None, raw=None):
        """A folder holding the project's own settings.json (by default the stack's complete one, plus
        the asks and denies given); returns (root, the file's bytes)."""
        ref_allow, ref_env = self.reference(stack, verify)
        data = {"permissions": {"allow": list(ref_allow) if allow is None else list(allow)},
                "env": dict(ref_env) if env is None else dict(env)}
        if ask:
            data["permissions"]["ask"] = list(ask)
        if deny:
            data["permissions"]["deny"] = list(deny)
        root = self.mk(name)
        path = os.path.join(root, ".claude", "settings.json")
        self.write(path, text if text is not None else json.dumps(data, indent=2) + "\n")
        if raw is not None:                          # bytes that need not be UTF-8
            with open(path, "wb") as fh:
                fh.write(raw)
        with open(path, "rb") as fh:
            return root, fh.read()

    def prepare(self, root, path, mode, stack, verify):
        """Put the folder on one of init's paths: fresh (no repository), unborn (a repository without
        commits), adopt (a repository with commits and an origin) or again (a scaffolded project)."""
        if path == "unborn":
            self.git(root, "init", "-q", "-b", "main")
        elif path == "adopt":
            self.git(root, "init", "-q", "-b", "main")
            self.write(os.path.join(root, "README.md"), "# kept\n")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "First")
            self.git(root, "remote", "add", "origin", self.bare(f"origin-{os.path.basename(root)}.git"))
            self.git(root, "push", "-q", "-u", "origin", "main")
            if mode == "real":
                branch = "t/7-adopt-the-harness-scaffold"
                worktree = os.path.join(root, ".claude", "worktrees", "t-7-adopt-the-harness-scaffold")
                self.git(root, "worktree", "add", "-q", worktree, "-b", branch, "origin/main")
                self.stub("harness-board", {"task": "#7 https://github.com/ryan/kept/issues/7\n"})
                self.stub("harness-work", {"start": json.dumps({"number": 7, "branch": branch, "worktree": worktree,
                                                                "tier": "sonnet", "context": "# 7"})})
                self.stub("harness-pr", {"open": json.dumps({"number": 12, "url": "https://x/pull/12"}),
                                         "ready": "PR #12 ready for review\n"})
        elif path == "again":
            self.ok("init", root, "--name", "kept", "--stack", stack, *(["--verify", verify] if verify else []))
            self.stub("harness-board")

    def both_runs(self, name, stack="none", verify=None, path="fresh", **kw):
        """Yield (mode, process) for the dry run and the real run, each over its own copy of the same kept
        file, checking that the file is untouched and still reported as kept."""
        for mode in ("dry-run", "real"):
            root, before = self.kept(f"{name}-{mode}", stack, verify, **kw)
            self.prepare(root, path, mode, stack, verify)
            args = ["init", root, "--name", "kept", "--stack", stack] + (["--verify", verify] if verify else [])
            p = self.ok(*args, *(["--dry-run"] if mode == "dry-run" else []))
            with open(os.path.join(root, ".claude", "settings.json"), "rb") as fh:
                self.assertEqual(fh.read(), before, f"{mode}: the kept file changed")
            self.assertTrue(any(ln in ("kept .claude/settings.json", "would keep .claude/settings.json")
                                for ln in self.lines(p)), p.stdout)
            yield mode, p

    def warns(self, p):
        return [ln for ln in (p.stdout + p.stderr).splitlines() if ln.startswith("warn:")]

    def assertWarns(self, mode, p, expected):
        """Exactly one warn line per (needle, kind) in `expected`, no others; each line names its needle
        verbatim and carries its kind as a word."""
        warns = self.warns(p)
        self.assertEqual(len(warns), len(expected), f"{mode}: {warns}\n{p.stdout}")
        for needle, kind in expected:
            lines = [w for w in warns if needle in w]
            self.assertEqual(len(lines), 1, f"{mode} {needle!r}: {warns}")
            self.assertRegex(lines[0], rf"\b{kind}\b")
            opposite = {"ask": "deny", "deny": "ask"}.get(kind)
            if opposite:
                self.assertNotRegex(lines[0], rf"\b{opposite}\b")

    # ---------------------------------------------------------------- blocking asks and denies
    def test_asks_that_stop_a_dispatched_run_are_reported(self):
        n = 0
        for kind in ("ask", "deny"):
            for rule in LOOP_RULES + BROADER_RULES + NARROWER_RULES:
                n += 1
                with self.subTest(rule=rule, kind=kind):
                    for mode, p in self.both_runs(f"block{n}", **{kind: [rule]}):
                        self.assertWarns(mode, p, [(rule, kind)])
            for rule in NEAR_MISSES:
                n += 1
                with self.subTest(near_miss=rule, kind=kind):
                    for mode, p in self.both_runs(f"miss{n}", **{kind: [rule]}):
                        self.assertWarns(mode, p, [])

        # several offenders at once: one line each, each naming only its own rule and its own kind
        ask = ["Bash(git commit:*)", "Bash(git push --force:*)", "Bash(harness-*:*)", "Read(./notes/**)"]
        deny = ["Bash(gh pr:*)", "Bash(git add:*)", "Bash(my-gh-pr-tool:*)"]
        expected = [(r, "ask") for r in ask[:3]] + [(r, "deny") for r in deny[:2]]
        with self.subTest(kept="many"):
            for mode, p in self.both_runs("many", ask=ask, deny=deny):
                self.assertWarns(mode, p, expected)

        # the ticket's example: an ask on git commit and no harness-pr allow is exactly two warnings
        allow = [r for r in TEMPLATE_ALLOW if r != "Bash(harness-pr:*)"]
        with self.subTest(kept="jotform"):
            for mode, p in self.both_runs("jotform", ask=["Bash(git commit:*)"], allow=allow):
                self.assertWarns(mode, p, [("Bash(git commit:*)", "ask"), ("Bash(harness-pr:*)", "allow")])

        # the verify command is the project's own (the rendered CLAUDE_VERIFY_CMD), not a fixed one
        table = [("make check", ["Bash(make check:*)", "Bash(make check)", "Bash(make:*)"],
                  ["Bash(make checkout:*)", "Bash(tox:*)"]),
                 ("tox -q", ["Bash(tox -q)", "Bash(tox:*)"], ["Bash(make check:*)", "Bash(make:*)"])]
        for verify, blocking, harmless in table:
            for kind in ("ask", "deny"):
                for rule in blocking + harmless:
                    n += 1
                    with self.subTest(verify=verify, rule=rule, kind=kind):
                        for mode, p in self.both_runs(f"verify{n}", "python", verify, **{kind: [rule]}):
                            self.assertWarns(mode, p, [(rule, kind)] if rule in blocking else [])

        # generated: for every loop command, rules built from it with random suffixes, with and without :*,
        # and every shorter prefix of it all block; near misses mixed into the same file do not. One file
        # per command and kind: N blocking rules give exactly N warn lines.
        rng = random.Random(31)
        groups = [(" ".join(words), blocking_rules(rng, words), near_miss_rules(rng, words))
                  for words in LOOP_COMMANDS]
        groups.append(("harness-*",) + harness_rules(rng))
        # the cheats' exact cases ride along with the generated ones
        groups.append(("examples", ["Bash(git commit --amend:*)", "Bash(git push origin feature/x)",
                                    "Bash(git fetch origin:*)", "Bash(gh issue create:*)", "Bash(gh api repos/x/y:*)",
                                    "Bash(harness-board show:*)", "Bash(git commit)", "Bash(*)"],
                       ["Bash(git status:*)", "Bash(gh repo:*)", "Bash(git commitx:*)", "Bash(harness:*)",
                        "Bash(git push-all:*)"]))
        for label, blocking, misses in groups:
            self.assertFalse(set(blocking) & set(misses), label)
            for kind in ("ask", "deny"):
                n += 1
                rules = list(blocking + misses)
                rng.shuffle(rules)
                with self.subTest(generated=label, kind=kind, blocking=blocking, misses=misses):
                    for mode, p in self.both_runs(f"gen{n}", **{kind: rules}):
                        self.assertWarns(mode, p, [(r, kind) for r in blocking])

        # generated: the verify command varies; its exact form, its :* form, the program alone with :*
        # (broader) and the command with more arguments (narrower) all block; the same program with another
        # first argument does not
        verifies = [("pytest -q", "python"), ("npm test", "node"), ("./ci/run.sh --fast", "python"),
                    ("python3 -m unittest discover -s tests", "python")]
        for _ in range(2):
            program = rng.choice(["make", "tox", "just", "bin/check", "./scripts/ci.sh", "cargo", "nox", "rake"])
            verifies.append((" ".join([program] + rng.sample(["check", "lint", "ci", "py311", "--all", "quick"],
                                                              rng.randint(1, 2))), "python"))
        for verify, stack in verifies:
            self.assertEqual(self.reference(stack, verify)[1]["CLAUDE_VERIFY_CMD"], verify)
            blocking, misses = verify_rules(rng, verify)
            for kind in ("ask", "deny"):
                n += 1
                rules = blocking + misses
                rng.shuffle(rules)
                with self.subTest(verify=verify, kind=kind, blocking=blocking, misses=misses):
                    for mode, p in self.both_runs(f"genverify{n}", stack, verify, **{kind: rules}):
                        self.assertWarns(mode, p, [(r, kind) for r in blocking])

        # stack none renders CLAUDE_VERIFY_CMD as scripts/verify.sh even with --verify: that is the command
        # a run asks for, and the --verify text (which verify.sh runs) is not
        self.assertEqual(self.reference("none", "make check")[1]["CLAUDE_VERIFY_CMD"], "scripts/verify.sh")
        with self.subTest(verify="make check", stack="none"):
            for mode, p in self.both_runs("verify-none", "none", "make check",
                                          ask=["Bash(make check:*)", "Bash(make:*)", "Bash(scripts/verify.sh)"]):
                self.assertWarns(mode, p, [("Bash(scripts/verify.sh)", "ask")])

        # a `*` word anywhere in a rule matches one or more words: the rule stops the loop when it could
        # match a loop command (or something narrower than one); globs after any other program, or after a
        # git or gh subcommand the loop does not use, cannot
        globs = ["Bash(gh * create)", "Bash(git * --force)", "Bash(git * main)", "Bash(* commit)",
                 "Bash(* push origin)", "Bash(gh * view 42)", "Bash(git * -m Fix)"]
        harmless = ["Bash(npm * test)", "Bash(docker * create)", "Bash(make * check)", "Bash(git status * main)",
                    "Bash(gh repo * create)", "Bash(docker * commit)", "Bash(npm * push origin)"]
        # pins: an exact `git` or `gh` alone is not a loop command; a trailing ` *` is a prefix rule like `:*`
        pins = ["Bash(git commit *)", "Bash(gh *)"]
        pin_harmless = ["Bash(git)", "Bash(gh)"]
        for label, blocking, misses in (("mid-pattern-glob", globs, harmless), ("pins", pins, pin_harmless)):
            for kind in ("ask", "deny"):
                n += 1
                rules = blocking + misses
                rng.shuffle(rules)
                with self.subTest(label=label, kind=kind):
                    for mode, p in self.both_runs(f"glob{n}", "python", **{kind: rules}):
                        self.assertWarns(mode, p, [(r, kind) for r in blocking])

    # ---------------------------------------------------------------- missing allows and env keys
    def test_missing_template_allows_and_env_keys_are_reported(self):
        # each template allow entry missing on its own: one warning naming exactly it
        for rule in TEMPLATE_ALLOW:
            with self.subTest(missing_allow=rule):
                for mode, p in self.both_runs("allow", allow=[r for r in TEMPLATE_ALLOW if r != rule]):
                    self.assertWarns(mode, p, [(rule, "allow")])

        # no allow entries at all, and no permissions section at all: every entry is named
        for label, kw in (("empty", {"allow": []}), ("no-permissions", {"text": '{"env": {}}\n'})):
            with self.subTest(kept=label):
                for mode, p in self.both_runs(label, **kw):
                    self.assertWarns(mode, p, [(r, "allow") for r in TEMPLATE_ALLOW]
                                     + ([("CLAUDE_VERIFY_CMD", "env")] if label == "no-permissions" else []))

        # an entry counts as present when an allow rule covers it: the same rule, a broader one, bare Bash;
        # a narrower rule, another command, a shorter word, or a non-Bash rule does not
        not_git = [r for r in TEMPLATE_ALLOW if not r.startswith("Bash(git ")]
        not_gh = [r for r in TEMPLATE_ALLOW if not r.startswith("Bash(gh ")]
        without = lambda gone: [r for r in TEMPLATE_ALLOW if r != gone]       # noqa: E731
        table = [
            (["Bash"], []),
            (["Bash(git:*)", "Bash(gh:*)"] + HARNESS_NINE + ["Bash(scripts/verify.sh)"], []),
            (without("Bash(harness-pr:*)") + ["Bash(harness-pr open:*)", "Bash(harness-p:*)", "Bash(harness:*)",
                                               "Bash(harness-prx:*)"], ["Bash(harness-pr:*)"]),
            ([r for r in TEMPLATE_ALLOW if r not in HARNESS_NINE] + ["Bash(harness-board show:*)"], HARNESS_NINE),
            (without("Bash(git commit:*)") + ["Bash(git commit -m:*)"], ["Bash(git commit:*)"]),
            (without("Bash(gh api:*)") + ["Bash(gh:*)"], []),
            (["Bash(git:*)"], not_git),
            (["Bash(gh:*)"], not_gh),
            (["Bash(g:*)", "Bash(gi:*)", "Bash(ghx:*)"], TEMPLATE_ALLOW),
            (["Read(./**)", "WebFetch", "Edit(./git commit/**)"], TEMPLATE_ALLOW),
        ]
        for i, (allow, missing) in enumerate(table):
            with self.subTest(allow=allow):
                for mode, p in self.both_runs(f"cover{i}", allow=allow):
                    self.assertWarns(mode, p, [(r, "allow") for r in missing])

        # the reference list is the template rendered for the stack, its own extras included
        for stack in ("python", "node"):
            ref_allow, _ = self.reference(stack)
            extras = [r for r in ref_allow if r not in TEMPLATE_ALLOW]
            self.assertTrue(extras, f"stack {stack} renders no extra allow entries")
            for extra in extras:
                with self.subTest(stack=stack, missing_extra=extra):
                    for mode, p in self.both_runs(f"extra-{stack}", stack, allow=[r for r in ref_allow if r != extra]):
                        self.assertWarns(mode, p, [(extra, "allow")])
            with self.subTest(stack=stack, template_only=True):
                for mode, p in self.both_runs(f"template-{stack}", stack, allow=TEMPLATE_ALLOW):
                    self.assertWarns(mode, p, [(r, "allow") for r in extras])
        python_allow, _ = self.reference("python")
        node_extras = [r for r in self.reference("node")[0] if r not in TEMPLATE_ALLOW]
        with self.subTest(cross_stack=True):         # another stack's extras are neither needed nor enough
            for mode, p in self.both_runs("cross", "node", allow=python_allow):
                self.assertWarns(mode, p, [(r, "allow") for r in node_extras])
            for mode, p in self.both_runs("cross-none", "none", allow=python_allow):
                self.assertWarns(mode, p, [])

        # a trailing ` *` is a prefix rule in allow too: Bash(git *) covers every git entry, and nothing else
        git_entries = [r for r in TEMPLATE_ALLOW if r.startswith("Bash(git ")]
        for allow, missing in (([r for r in TEMPLATE_ALLOW if r not in git_entries] + ["Bash(git *)"], []),
                               (["Bash(git *)"], [r for r in TEMPLATE_ALLOW if r not in git_entries]),
                               (without("Bash(git commit:*)") + ["Bash(git commit *)"], [])):
            with self.subTest(space_star=allow):
                for mode, p in self.both_runs(f"space-star{len(allow)}", allow=allow):
                    self.assertWarns(mode, p, [(r, "allow") for r in missing])

        # Bash(*) covers every entry of every stack; so do one broad rule per program
        for stack in ("none", "python", "node"):
            ref_allow, _ = self.reference(stack)
            programs = _unique(_bash(_words(r)[:1], True) for r in ref_allow)
            for label, allow in (("star", ["Bash(*)"]), ("programs", programs)):
                with self.subTest(stack=stack, allow=allow):
                    for mode, p in self.both_runs(f"broad-{label}-{stack}", stack, allow=allow):
                        self.assertWarns(mode, p, [])

        # a broader rule of more than one word covers the extras it prefixes (derived from the rendered
        # extras, e.g. Bash(uv run:*) for Bash(uv run pytest:*)), and only those
        multiword = 0
        for stack in ("python", "node"):
            ref_allow, _ = self.reference(stack)
            extras = [r for r in ref_allow if r not in TEMPLATE_ALLOW]
            prefixes = _unique(tuple(_words(e)[:k]) for e in extras for k in range(1, len(_words(e))))
            for i, prefix in enumerate(prefixes):
                covered = [e for e in extras if tuple(_words(e)[:len(prefix)]) == prefix]
                broad = _bash(list(prefix), True)
                multiword += len(prefix) > 1
                allow = [r for r in ref_allow if r not in covered] + [broad]
                with self.subTest(stack=stack, broader=broad, covers=covered):
                    for mode, p in self.both_runs(f"prefix-{stack}{i}", stack, allow=allow):
                        self.assertWarns(mode, p, [])
                if len(prefix) > 1:          # the same broad rule alone leaves the other extras missing
                    allow = [r for r in ref_allow if r not in extras] + [broad]
                    with self.subTest(stack=stack, broader=broad, alone=True):
                        for mode, p in self.both_runs(f"prefix-alone-{stack}{i}", stack, allow=allow):
                            self.assertWarns(mode, p, [(e, "allow") for e in extras if e not in covered])
            # the exact command, a narrower rule, a sibling and a longer word cover nothing
            decoys = []
            for e in extras:
                w = _words(e)
                decoys += [_bash(w, False), _bash(w + ["-q"], True), _bash(w[:-1] + [w[-1] + "x"], True),
                           _bash(w[:-1] + ["other"], True), _bash(["x" + w[0]], True)]
                if len(w) > 2:
                    decoys += [_bash(w[:1] + [w[1] + "x"], True), _bash(w[:1] + ["other"], True)]
            decoys = [d for d in _unique(decoys) if d not in ref_allow]
            with self.subTest(stack=stack, decoys=decoys):
                for mode, p in self.both_runs(f"decoys-{stack}", stack,
                                              allow=[r for r in ref_allow if r not in extras] + decoys):
                    self.assertWarns(mode, p, [(e, "allow") for e in extras])
        self.assertTrue(multiword, "no extra has a prefix of two words or more")

        # the same for the template's own entries: their exact command, a narrower rule or a longer word
        # does not cover them
        two_word = [r for r in TEMPLATE_ALLOW if r.startswith(("Bash(git ", "Bash(gh "))]
        decoys = []
        for r in two_word:
            w = _words(r)
            decoys += [_bash(w, False), _bash(w + ["origin"], True), _bash(w[:-1] + [w[-1] + "s"], True)]
        with self.subTest(template_decoys=decoys):
            allow = [r for r in TEMPLATE_ALLOW if r not in two_word] + decoys
            for mode, p in self.both_runs("template-decoys", allow=allow):
                self.assertWarns(mode, p, [(r, "allow") for r in two_word])

        # env keys the template sets (stacks python and node set both), missing one at a time, both, or
        # with no env section at all
        values = self.reference("python")[1]
        self.assertEqual(sorted(values), sorted(TEMPLATE_ENV))
        for missing in ([TEMPLATE_ENV[0]], [TEMPLATE_ENV[1]], list(TEMPLATE_ENV)):
            with self.subTest(missing_env=missing):
                env = {k: v for k, v in values.items() if k not in missing}
                for mode, p in self.both_runs("env", "python", env=env):
                    self.assertWarns(mode, p, [(k, "env") for k in missing])
        with self.subTest(no_env_section=True):
            text = json.dumps({"permissions": {"allow": self.reference("python")[0]}}, indent=2) + "\n"
            for mode, p in self.both_runs("noenv", "python", text=text):
                self.assertWarns(mode, p, [(k, "env") for k in TEMPLATE_ENV])

        # stack none does not set HARNESS_TEST_CMD, so its absence is not reported there
        with self.subTest(stack="none"):
            for mode, p in self.both_runs("none-env", "none", env={}):
                self.assertWarns(mode, p, [("CLAUDE_VERIFY_CMD", "env")])

        # allow, env and ask problems in one file are all reported
        allow = [r for r in python_allow if r != "Bash(gh pr:*)"]
        for mode, p in self.both_runs("all", "python", ask=["Bash(git push:*)"], allow=allow, env={}):
            self.assertWarns(mode, p, [("Bash(git push:*)", "ask"), ("Bash(gh pr:*)", "allow")]
                             + [(k, "env") for k in TEMPLATE_ENV])

    # ---------------------------------------------------------------- nothing to report
    def test_a_complete_kept_file_reports_nothing_and_is_unchanged(self):
        python_allow, python_env = self.reference("python")
        # the stack's own rendered entries; other people's asks and denies, formatted by hand; allow
        # rules broader than the entries
        odd = ('{"permissions":{"allow":%s,"ask":["Bash(rm -rf:*)"],"deny":["Bash(curl:*)"]},\n'
               '    "env":{"CLAUDE_VERIFY_CMD":"make check","HARNESS_TEST_CMD":"make test FILES={files}"}}'
               % json.dumps(python_allow + ["Bash(make check:*)"]))
        broad = ["Bash(git:*)", "Bash(gh:*)"] + HARNESS_NINE + ["Bash(scripts/verify.sh)", "Bash(uv:*)",
                                                                "Bash(python3:*)"]
        variants = [("none", "none", {}), ("python", "python", {}), ("node", "node", {}),
                    ("odd", "python", {"text": odd}), ("bare-bash", "python", {"allow": ["Bash"]}),
                    ("broad", "python", {"allow": broad}),
                    ("noise", "python", {"ask": NEAR_MISSES, "deny": NEAR_MISSES})]
        # generated near misses of every loop command as asks and denies; multi-word broader allows
        rng = random.Random(3131)
        noise = _unique([r for words in LOOP_COMMANDS for r in near_miss_rules(rng, words)] + harness_rules(rng)[1])
        multiword = [r for r in python_allow if r not in ("Bash(uv run pytest:*)", "Bash(uv run ruff:*)",
                                                          "Bash(python3 -m pytest:*)")]
        variants += [("generated-noise", "none", {"ask": noise[::2], "deny": noise[1::2]}),
                     ("star", "node", {"allow": ["Bash(*)"]}),
                     ("multiword", "python", {"allow": multiword + ["Bash(uv run:*)", "Bash(python3 -m:*)"]})]
        for label, stack, kw in variants:
            with self.subTest(kept=label):
                for mode, p in self.both_runs(f"complete-{label}", stack=stack, **kw):
                    self.assertWarns(mode, p, [])

        # control: the same complete file with one blocking ask is no longer silent, so "nothing" above
        # means the check ran and found nothing
        with self.subTest(kept="control"):
            for mode, p in self.both_runs("control", stack="python", ask=["Bash(git commit:*)"]):
                self.assertWarns(mode, p, [("Bash(git commit:*)", "ask")])

        # --force still overwrites the kept file with the template, and there is nothing left to warn about
        root, _ = self.kept("forced", "python", ask=["Bash(git commit:*)"], allow=[], env={})
        p = self.ok("init", root, "--name", "forced", "--stack", "python", "--force")
        self.assertIn("updated .claude/settings.json", self.lines(p))
        self.assertEqual(self.warns(p), [], p.stdout)
        settings = self.settings(root)
        self.assertEqual(settings["env"], python_env)
        self.assertEqual(settings["permissions"]["allow"], python_allow)
        self.assertNotIn("ask", settings["permissions"])

    # ---------------------------------------------------------------- a file that is not settings
    def test_a_kept_file_that_is_not_usable_settings_gets_one_warning(self):
        allow = list(TEMPLATE_ALLOW)
        env = {"CLAUDE_VERIFY_CMD": "scripts/verify.sh"}
        texts = ["{not json", "", "   \n", '{"permissions": ', "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n",
                 "just words\n"]
        shapes = [{"permissions": None, "env": env},
                  {"permissions": {"allow": allow, "ask": "Bash(git commit:*)"}, "env": env},
                  {"permissions": {"allow": allow, "deny": {"Bash(git push:*)": True}}, "env": env},
                  {"permissions": {"allow": "Bash"}, "env": env},
                  {"permissions": {"allow": allow, "ask": [42, None, {"rule": "x"}]}, "env": env},
                  {"permissions": {"allow": allow + [7]}, "env": env},
                  # the top level is not an object; a section of the wrong type
                  [], ["Bash(git commit:*)"], "settings", 42, None, True,
                  {"env": []}, {"permissions": []}, {"permissions": {"allow": allow}, "env": ["CLAUDE_VERIFY_CMD"]},
                  {"permissions": {"allow": allow}, "env": "CLAUDE_VERIFY_CMD=scripts/verify.sh"}]
        cases = [("text", {"text": t}) for t in texts] + [("shape", {"text": json.dumps(s, indent=2) + "\n"})
                                                          for s in shapes]
        # bytes that are not UTF-8
        cases += [("bytes", {"raw": b'{"env": {"CLAUDE_VERIFY_CMD": "caf\xe9"}}\n'}),
                  ("bytes", {"raw": b"\xff\x80\x00{}"}),
                  ("bytes", {"raw": json.dumps({"permissions": {"allow": allow}, "env": env}).encode() + b"\xc3"})]
        for i, (label, kw) in enumerate(cases):
            with self.subTest(label=label, kept=kw):
                for mode, p in self.both_runs(f"unusable{i}", **kw):
                    self.assertEqual(len(self.warns(p)), 1, f"{mode}: {self.warns(p)}\n{p.stdout}")

    # ---------------------------------------------------------------- every init path, no duplicates
    def test_each_warning_appears_once_on_every_init_path(self):
        allow = [r for r in self.reference("python")[0] if r != "Bash(harness-work:*)"]
        env = {k: v for k, v in self.reference("python")[1].items() if k != "HARNESS_TEST_CMD"}
        expected = [("Bash(git commit:*)", "ask"), ("Bash(gh api:*)", "deny"), ("Bash(harness-work:*)", "allow"),
                    ("HARNESS_TEST_CMD", "env")]
        for path in ("fresh", "unborn", "adopt", "again"):
            with self.subTest(path=path):
                for mode, p in self.both_runs(f"path-{path}", "python", path=path, ask=["Bash(git commit:*)"],
                                              deny=["Bash(gh api:*)"], allow=allow, env=env):
                    self.assertWarns(mode, p, expected)

    # ---------------------------------------------------------------- firm or soft
    def test_a_rule_that_only_overlaps_the_loop_may_stop_it_and_one_that_covers_it_stops_it(self):
        """A rule that covers a loop command (a prefix of it, or exactly the verify command, which runs
        verbatim) stops the run; a narrower rule, a mid-pattern glob or an exact rule equal to a loop command
        that always carries arguments only may stop it. Only the phrase `may stop` is pinned."""
        firm = ["Bash(git commit:*)", "Bash(git:*)", "Bash(*)", "Bash(harness-*:*)"]
        soft = ["Bash(git push --force:*)", "Bash(git commit -m:*)", "Bash(gh * create)", "Bash(git * main)",
                "Bash(git commit)", "Bash(git add)", "Bash(gh pr)", "Bash(* commit)"]
        # (label, stack, verify, firm rules, soft rules); bare Bash names every rule, so it has a file alone
        groups = [("loop", "none", None, firm + ["Bash(scripts/verify.sh)"], soft),
                  ("bare-bash", "none", None, ["Bash"], []),
                  ("default-verify", "python", None, ["Bash(scripts/verify.sh)"], []),
                  ("own-verify", "python", "make check", ["Bash(make check)"], [])]
        rng = random.Random(6)
        for label, stack, verify, hard, maybe in groups:
            for kind in ("ask", "deny"):
                rules = hard + maybe
                rng.shuffle(rules)
                with self.subTest(label=label, kind=kind):
                    for mode, p in self.both_runs(f"firm-{label}-{kind}", stack, verify, **{kind: rules}):
                        self.assertWarns(mode, p, [(r, kind) for r in rules])
                        lines = self.warns(p)
                        wrong = [r for r in rules
                                 if ("may stop" in next(w for w in lines if r in w)) != (r in maybe)]
                        self.assertEqual(wrong, [], f"{mode}: firm or soft is wrong for these\n{lines}")

    # ---------------------------------------------------------------- round 3 of the review of PR #41
    def test_the_kept_files_own_verify_command_is_the_one_checked(self):
        """A dispatched run issues the kept file's CLAUDE_VERIFY_CMD (verify_on_stop and the coder read
        it from settings), so an ask or deny on that command stops the run for sure even when init is not
        given --verify; the rendered default counts only when the kept file sets no verify command."""
        _, ref_env = self.reference("python")
        own = dict(ref_env, CLAUDE_VERIFY_CMD="make check")
        for kind in ("ask", "deny"):
            with self.subTest(case="own verify blocked", kind=kind):
                rules = ["Bash(make:*)", "Bash(make check)", "Bash(make check:*)"]
                for mode, p in self.both_runs(f"own-{kind}", "python", env=own, **{kind: rules}):
                    self.assertWarns(mode, p, [(r, kind) for r in rules])
                    self.assertEqual([w for w in self.warns(p) if "may stop" in w], [], f"{mode}: all are firm")
            with self.subTest(case="the rendered default is not what this project runs", kind=kind):
                for mode, p in self.both_runs(f"rendered-{kind}", "python", env=own,
                                              **{kind: ["Bash(scripts/verify.sh)"]}):
                    self.assertWarns(mode, p, [])
            with self.subTest(case="no verify in the kept file falls back to the rendered one", kind=kind):
                env = {k: v for k, v in ref_env.items() if k != "CLAUDE_VERIFY_CMD"}
                for mode, p in self.both_runs(f"fallback-{kind}", "python", env=env,
                                              **{kind: ["Bash(scripts/verify.sh)"]}):
                    self.assertWarns(mode, p, [("Bash(scripts/verify.sh)", kind), ("CLAUDE_VERIFY_CMD", "env")])

    def test_an_allow_with_a_star_word_covers_the_entries_it_matches(self):
        """A `*` word in an allow rule matches one or more words, as it does in an ask or deny: `Bash(git *:*)`
        covers every git entry, `Bash(* commit:*)` only the commit one, and a glob that needs more words than
        an entry has covers none of them."""
        git = [r for r in TEMPLATE_ALLOW if r.startswith("Bash(git ")]
        not_git = [r for r in TEMPLATE_ALLOW if r not in git]
        table = [(not_git + ["Bash(git *:*)"], []),
                 (not_git + ["Bash(* commit:*)"], [r for r in git if r != "Bash(git commit:*)"]),
                 (not_git + ["Bash(git * --amend:*)"], git)]
        for i, (allow, missing) in enumerate(table):
            with self.subTest(allow=allow[len(not_git):]):
                for mode, p in self.both_runs(f"star-allow{i}", allow=allow):
                    self.assertWarns(mode, p, [(r, "allow") for r in missing])

    def test_a_missing_env_key_warn_names_the_value_to_add(self):
        """The warn line for an absent env key gives the exact line to add: the key and the value the
        template renders for this stack and its --verify or default."""
        for stack, verify in (("python", None), ("node", "npm run check")):
            _, ref_env = self.reference(stack, verify)
            with self.subTest(stack=stack, verify=verify):
                for mode, p in self.both_runs(f"env-value-{stack}", stack, verify, env={}):
                    self.assertWarns(mode, p, [(k, "env") for k in ref_env])
                    for key, value in ref_env.items():
                        line = next(w for w in self.warns(p) if key in w)
                        self.assertIn(json.dumps({key: value})[1:-1], line, line)

    # ---------------------------------------------------------------- the nine harness rules (ticket #137)
    def no_harness(self, stack, verify=None):
        """The stack's rendered allow list without any harness rule, wildcard or narrow."""
        return [r for r in self.reference(stack, verify)[0] if r != WILDCARD and r not in HARNESS_NINE]

    def assertOldSpellingOnly(self, mode, p, others=()):
        """The old-spelling line exactly once, verbatim; no `allow entry Bash(harness-...) is missing` line;
        and besides it exactly the (needle, kind) warnings in `others`."""
        warns = self.warns(p)
        self.assertEqual(warns.count(OLD_SPELLING_WARN), 1, f"{mode}: {warns}\n{p.stdout}")
        self.assertEqual(len([w for w in warns if "matches no harness tool" in w]), 1, f"{mode}: {warns}")
        self.assertEqual([w for w in warns if "auto mode may drop" in w], [], f"{mode}: {warns}")
        self.assertEqual([w for w in warns if re.search(r"allow entry Bash\(harness-[^)]*\) is missing", w)], [],
                         f"{mode}: {warns}")
        rest = [w for w in warns if w != OLD_SPELLING_WARN]
        self.assertEqual(len(rest), len(others), f"{mode}: {rest}")
        for needle, kind in others:
            lines = [w for w in rest if needle in w]
            self.assertEqual(len(lines), 1, f"{mode} {needle!r}: {rest}")
            self.assertRegex(lines[0], rf"\b{kind}\b")

    def test_old_wildcard_spelling_warns_once(self):
        """A kept allow list holding Bash(harness-*:*) gets the one old-spelling line, in init and in
        --dry-run, in place of nine `allow entry ... is missing` lines; for every stack, wherever the
        wildcard sits, written twice, next to some of the nine, and beside other warnings."""
        rng = random.Random(137)
        n = 0
        for stack in ("none", "python", "node"):
            base = self.no_harness(stack)
            for where in (0, len(base) // 2, len(base)):
                n += 1
                allow = base[:where] + [WILDCARD] + base[where:]
                with self.subTest(stack=stack, at=where):
                    for mode, p in self.both_runs(f"old{n}", stack, allow=allow):
                        self.assertOldSpellingOnly(mode, p)
        base = self.no_harness("none")
        cases = [("twice", base + [WILDCARD, WILDCARD], ()),
                 ("with-three-of-nine", base + [WILDCARD] + rng.sample(HARNESS_NINE, 3), ()),
                 ("with-other-problems", [r for r in base if r != "Bash(gh pr:*)"] + [WILDCARD],
                  (("Bash(gh pr:*)", "allow"), ("Bash(git commit:*)", "ask")))]
        for label, allow, others in cases:
            with self.subTest(case=label):
                ask = ["Bash(git commit:*)"] if others else ()
                for mode, p in self.both_runs(f"old-{label}", allow=allow, ask=ask):
                    self.assertOldSpellingOnly(mode, p, others)
        # the other init paths print it the same way
        for path in ("unborn", "again"):
            with self.subTest(path=path):
                for mode, p in self.both_runs(f"old-path-{path}", "python", path=path,
                                              allow=self.no_harness("python") + [WILDCARD]):
                    self.assertOldSpellingOnly(mode, p)

    def test_nine_rules_need_no_allow_warning(self):
        """A kept file with the nine harness-<tool> rules (and no wildcard) gets no allow warning, in any
        order or position, for every stack; and never the old-spelling line."""
        rng = random.Random(1370)
        n = 0
        for stack in ("none", "python", "node"):
            base = self.no_harness(stack)
            orders = [list(HARNESS_NINE), list(reversed(HARNESS_NINE)), rng.sample(HARNESS_NINE, 9)]
            for order in orders:
                for where in (0, len(base)):
                    n += 1
                    allow = base[:where] + order + base[where:]
                    with self.subTest(stack=stack, order=order, at=where):
                        for mode, p in self.both_runs(f"nine{n}", stack, allow=allow):
                            self.assertWarns(mode, p, [])
                            self.assertFalse([w for w in self.warns(p) if "wildcard" in w], p.stdout)
        # interleaved with the other entries
        mixed = self.no_harness("python") + list(HARNESS_NINE)
        rng.shuffle(mixed)
        with self.subTest(order="interleaved"):
            for mode, p in self.both_runs("nine-mixed", "python", allow=mixed):
                self.assertWarns(mode, p, [])

    def test_one_missing_tool_rule_is_named(self):
        """Each of the nine left out on its own gives one `allow entry <it> is missing` line naming it and
        no other warning; a narrower rule, a shorter or longer word, or a non-Bash rule for it does not
        count; two left out give two lines."""
        rng = random.Random(7137)
        base = self.no_harness("none")
        for gone in HARNESS_NINE:
            tool = _words(gone)[0]
            decoys = [_bash([tool, rng.choice(["open", "show", "land", "--json"])], True), _bash([tool], False),
                      _bash([tool[:-1]], True), _bash([tool + "x"], True), f"Read(./{tool}/**)"]
            for label, extra in (("alone", []), ("decoys", decoys)):
                allow = base + [r for r in HARNESS_NINE if r != gone] + extra
                with self.subTest(missing=gone, case=label):
                    for mode, p in self.both_runs(f"miss-{tool}-{label}", allow=allow):
                        self.assertWarns(mode, p, [(gone, "allow")])
                        line = next(w for w in self.warns(p) if gone in w)
                        self.assertIn(f"allow entry {gone} is missing", line)
                        self.assertNotIn("wildcard", line)
        for stack in ("python", "node"):
            gone = rng.sample(HARNESS_NINE, 2)
            allow = self.no_harness(stack) + [r for r in HARNESS_NINE if r not in gone]
            with self.subTest(stack=stack, missing=gone):
                for mode, p in self.both_runs(f"miss-two-{stack}", stack, allow=allow):
                    self.assertWarns(mode, p, [(r, "allow") for r in gone])
                    for r in gone:
                        self.assertTrue([w for w in self.warns(p) if f"allow entry {r} is missing" in w], p.stdout)

    def test_adopt_path_prints_the_old_spelling_warning(self):
        """A repository with commits and an origin (the adopt path, real and --dry-run) prints the same
        old-spelling line once, for each stack, alongside its other warnings."""
        for stack in ("none", "python"):
            with self.subTest(stack=stack):
                for mode, p in self.both_runs(f"adopt-old-{stack}", stack, path="adopt",
                                              allow=self.no_harness(stack) + [WILDCARD]):
                    self.assertOldSpellingOnly(mode, p)
        with self.subTest(case="with an ask"):
            for mode, p in self.both_runs("adopt-old-ask", "python", path="adopt", ask=["Bash(git push:*)"],
                                          allow=[WILDCARD] + self.no_harness("python")):
                self.assertOldSpellingOnly(mode, p, (("Bash(git push:*)", "ask"),))


# =========================================================================== commit paths
class NoDropClaim(unittest.TestCase):
    """Ticket #226: auto mode never drops Bash(harness-*:*) (docs/research/auto-mode-classifier.md, #139);
    the rule matches nothing because a ':*' rule is a literal prefix. No plugin text or design.md says otherwise."""

    def test_no_text_says_auto_mode_drops_the_wildcard_rule(self):
        files = [os.path.join(REPO, "docs", "design.md")]
        for root, dirs, names in os.walk(PLUGIN):
            dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
            files += [os.path.join(root, n) for n in names]
        verb = r"(?:drop|remov|strip|discard)\w*"
        mode = r"auto[\s-]+mode"
        # Either order, within 60 characters, any wording ("may drop", "can drop", "dropped by auto mode", ...),
        # and about the wildcard rule: harness-* or "wildcard" within 200 characters of the match.
        claim = re.compile(rf"{mode}\b.{{0,60}}?\b{verb}|\b{verb}.{{0,60}}?{mode}", re.IGNORECASE)
        subject = re.compile(r"harness-\*|wildcard", re.IGNORECASE)
        hits = []
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            # Join the lines with comment markers removed, so a claim wrapped over two comment lines is found.
            text = " ".join(re.sub(r"^\s*(#|//|\*|>)\s?", "", line) for line in lines)
            if any(subject.search(text[max(0, m.start() - 200):m.end() + 200]) for m in claim.finditer(text)):
                hits.append(os.path.relpath(path, REPO))
        self.assertEqual(hits, [], "still claims auto mode drops (or may drop) the wildcard rule")

    def test_design_md_quotes_the_new_old_wildcard_warning(self):
        prefix = OLD_SPELLING_WARN[:OLD_SPELLING_WARN.index("rules: ") + len("rules: ")]
        with open(os.path.join(REPO, "docs", "design.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        # assertTrue, not assertIn: assertIn would print the whole of design.md on failure.
        self.assertTrue(prefix in text, f"docs/design.md does not quote the new warning: {prefix!r}")


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


# =========================================================================== the python stack's own lint
class PythonLint(ScaffoldTest):
    """A python-stack scaffold passes the lint and format checks it sets up (ticket #152's review finding):
    scripts/verify.sh runs `uv run ruff check .` and `uv run ruff format --check .` under the scaffolded
    pyproject.toml, and the scaffolded .githooks/pre-commit runs ruff on the staged files."""

    def pyproject_line_length(self, root):
        """line-length under [tool.ruff] in the scaffolded pyproject.toml; ruff's default 88 when unset."""
        section = None
        for raw in self.read(root, "pyproject.toml").splitlines():
            line = raw.split("#", 1)[0].strip()
            header = re.match(r"^\[([^\]]+)\]$", line)
            if header:
                section = header.group(1).strip()
                continue
            found = re.match(r"^line-length\s*=\s*(\d+)$", line)
            if found and section == "tool.ruff":
                return int(found.group(1))
        return 88

    def ruff_exemption(self, root, guard):
        """How the scaffolded project exempts scripts/deploy_guard.py from ruff, or None: the pyproject names
        it (an exclude), or the file carries a file-level `# ruff: noqa` and `# fmt: off`."""
        if "deploy_guard.py" in self.read(root, "pyproject.toml"):
            return "pyproject.toml names scripts/deploy_guard.py"
        lines = [ln.strip() for ln in guard.splitlines()]
        if "# ruff: noqa" in lines and "# fmt: off" in lines:
            return "file-level # ruff: noqa and # fmt: off"
        return None

    def run_ruff(self, root, *args):
        return subprocess.run([RUFF] + list(args), cwd=root, env=self.env(), capture_output=True, text=True,
                              timeout=120)

    def test_deploy_guard_fits_the_scaffolded_line_length_or_is_exempted(self):
        """Needs no ruff, so it holds in CI too: every line of the written scripts/deploy_guard.py is within the
        scaffolded pyproject's [tool.ruff] line-length, unless the project exempts that file from ruff."""
        for name in ("demo-app", "guarded"):
            with self.subTest(name=name):
                root = os.path.join(self.tmp, f"len-{name}")
                self.ok("init", root, "--name", name, "--stack", "python")
                limit = self.pyproject_line_length(root)
                guard = self.read(root, "scripts", "deploy_guard.py")
                if self.ruff_exemption(root, guard):
                    continue
                long_lines = [f"{n}: {len(line)}" for n, line in enumerate(guard.splitlines(), 1)
                              if len(line) > limit]
                self.assertEqual(long_lines, [], f"{len(long_lines)} lines of scripts/deploy_guard.py are over "
                                                 f"line-length = {limit}, and no ruff exemption")

    @unittest.skipUnless(RUFF, "ruff is not installed (CI may not have it); the line-length test still runs")
    def test_a_fresh_python_scaffold_passes_its_own_ruff_check_and_format(self):
        """Runs the real ruff (found outside the fakebin; skipped when ruff is not installed, as CI may not have
        it) as the scaffolded scripts/verify.sh does, in the new project under its own pyproject.toml settings."""
        root = os.path.join(self.tmp, "linted")
        self.ok("init", root, "--name", "linted", "--stack", "python")
        verify = self.read(root, "scripts", "verify.sh").splitlines()
        for args in (["check", "."], ["format", "--check", "."],
                     ["check", "scripts/deploy_guard.py"], ["format", "--check", "scripts/deploy_guard.py"]):
            with self.subTest(ruff=" ".join(args)):
                if args[-1] == ".":
                    self.assertIn("uv run ruff " + " ".join(args), verify)     # what verify.sh runs
                p = self.run_ruff(root, *(args[:-1] + ["--no-cache", args[-1]]))
                self.assertEqual(p.returncode, 0, f"ruff {' '.join(args)}:\n{p.stdout[-3000:]}{p.stderr[-2000:]}")

    @unittest.skipUnless(RUFF, "ruff is not installed (CI may not have it); the line-length test still runs")
    def test_the_adopt_commit_passes_the_scaffolded_pre_commit_hook_with_ruff_on_path(self):
        """With the real ruff (found outside the fakebin; skipped when ruff is not installed, as CI may not have
        it) on PATH, the scaffolded .githooks/pre-commit checks the adoption commit's Python files and lets it
        through."""
        os.symlink(RUFF, os.path.join(self.fakebin, "ruff"))
        root = self.repo_with_commit()
        self.git(root, "remote", "add", "origin", self.bare())
        self.git(root, "push", "-q", "-u", "origin", "main")
        branch = "t/9-adopt-the-harness-scaffold"
        worktree = os.path.join(root, ".claude", "worktrees", "t-9-adopt-the-harness-scaffold")
        self.git(root, "worktree", "add", "-q", worktree, "-b", branch, "origin/main")
        self.stub("harness-board", {"task": "#9 https://github.com/ryan/proj/issues/9\n"})
        self.stub("harness-work", {"start": json.dumps({"number": 9, "branch": branch, "worktree": worktree,
                                                        "tier": "sonnet", "context": "# 9"})})
        self.stub("harness-pr", {"open": json.dumps({"number": 14, "url": "https://github.com/ryan/proj/pull/14"}),
                                 "ready": "PR #14 ready for review\n"})

        p = self.scaffold("init", root, "--name", "proj", "--stack", "python")

        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-4000:])
        self.assertEqual(self.git(root, "config", "--get", "core.hooksPath"), ".githooks")
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Adopt the harness scaffold (#9)")
        committed = self.git(worktree, "show", "--name-only", "--format=", "HEAD").splitlines()
        for rel in ("scripts/deploy_guard.py", ".githooks/pre-commit", "pyproject.toml"):
            self.assertIn(rel, committed)


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
