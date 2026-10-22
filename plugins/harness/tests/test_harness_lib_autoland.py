"""Tests for harness_lib/autoland.py and settings.committed_env (ticket #140, epic #132): the
auto-land gate's ten rules over gathered facts, and the settings it reads.

- evaluate(facts, settings) -> {verdict, code, reason, rule}: rules 1 to 10 in order, the
  first failing rule decides; all passing gives land/land (rule 10) naming the head sha7.
  Verdict, code and rule are asserted exactly; reasons loosely (the incident number, the
  sha7s, counts, names), so the wording stays the coder's.
- gate_settings(main, ref="origin/main") -> {autoland, sizes, manual_paths, smoke_local}, read
  only from `git show <ref>:.claude/settings.json` in temp repositories: the process
  environment, the working tree and settings.local.json never count. HARNESS_MANUAL_PATHS
  extends DEFAULT_MANUAL_PATHS (defaults first, de-duplicated); HARNESS_AUTOLAND_SIZES
  defaults to S,M and a value holding anything but S, M or L is ignored.
- settings.committed_env(main, ref="origin/main"): the committed `env` object, {} when the
  file is missing, not JSON or not an object.
- autoland.py imports only the standard library and harness_lib.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_autoland.py" -v
"""

from __future__ import annotations

import ast
import copy
import importlib
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
LIB = PLUGIN / "lib"
AUTOLAND_PY = LIB / "harness_lib" / "autoland.py"
GIT = shutil.which("git")
GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.invalid",
}

# The ticket's list, written out here (not read from the module).
DEFAULTS = (
    ".github/**", ".claude/**", "scripts/deploy.sh", "scripts/smoke.sh", "scripts/deploy_guard.py",
    "scripts/verify.sh", "**/credentials/**", "**/.env*", "**/*.pem",
    "pyproject.toml", "package.json", "**/conftest.py", "pytest.ini",
)

HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OLD = "0ff1ce0beef1234567890abcdef1234567890abc"


def lib(name):
    """harness_lib.<name>, imported from this checkout's lib/ (the tools load the same one)."""
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    return importlib.import_module(f"harness_lib.{name}")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "GIT_"))}
    env.update(extra)
    return env


# ---------------------------------------------------------------- facts and settings
def base_facts(**over):
    """A pull request every rule passes: ticket #140 on t/140-..., everything green at HEAD."""
    facts = {
        "pr": 151,
        "state": "OPEN",
        "draft": False,
        "head_ref": "t/140-add-harness-lib-autoland",
        "head_sha": HEAD,
        "author": "rjeskuri",
        "login": "rjeskuri",
        "closing": [140],
        "pr_labels": [],
        "incidents": [],
        "deploy": {"status": "completed", "conclusion": "success"},
        "ticket": {"number": 140, "state": "OPEN", "labels": ["task", "size:M", "risk:low", "status:in-review"]},
        "files": ["src/app/models.py", "tests/test_models.py"],
        "review": {"commit_id": HEAD, "at_head": True, "high": 0, "medium": 0, "low": 0},
        "unresolved_threads": 0,
        "owner_comments": 0,
        "behind_by": 0,
        "mergeable": "MERGEABLE",
        "checks": "success",
        "failed_checks": [],
        "pending_checks": [],
        "nontest_files": 1,
        "nontest_lines": 120,
    }
    facts.update(over)
    return facts


def base_settings(**over):
    s = {"autoland": True, "sizes": ["S", "M"], "manual_paths": list(DEFAULTS), "smoke_local": False}
    s.update(over)
    return s


def labels(facts, add=(), drop=()):
    facts["ticket"]["labels"] = [x for x in facts["ticket"]["labels"] if x not in drop] + list(add)


def set_(**kw):
    def apply(f, s):
        f.update(copy.deepcopy(kw))
    return apply


def set_settings(**kw):
    def apply(f, s):
        s.update(copy.deepcopy(kw))
    return apply


def ticket(**kw):
    def apply(f, s):
        f["ticket"].update(kw)
    return apply


def ticket_labels(add=(), drop=()):
    def apply(f, s):
        labels(f, add, drop)
    return apply


def review(**kw):
    def apply(f, s):
        f["review"].update(kw)
    return apply


def add_files(*paths):
    def apply(f, s):
        f["files"] = f["files"] + list(paths)
    return apply


# One row per failing condition of the rule table:
# (label, mutation, verdict, code, rule, substrings the reason must hold)
ROWS = [
    ("autoland off", set_settings(autoland=False), "manual", "off", 1, []),
    ("state MERGED", set_(state="MERGED"), "manual", "pr", 2, []),
    ("state CLOSED", set_(state="CLOSED"), "manual", "pr", 2, []),
    ("draft", set_(draft=True), "manual", "pr", 2, ["draft"]),
    ("head not a ticket branch", set_(head_ref="feature/140-autoland"), "manual", "pr", 2, []),
    ("head is main", set_(head_ref="main"), "manual", "pr", 2, []),
    ("head t/ without a number", set_(head_ref="t/abc-autoland"), "manual", "pr", 2, []),
    ("head of another ticket", set_(head_ref="t/141-other-thing"), "manual", "pr", 2, []),
    ("author not login", set_(author="mallory"), "manual", "pr", 2, ["mallory", "rjeskuri"]),
    ("closes nothing", set_(closing=[]), "manual", "pr", 2, ["140"]),
    ("closes another issue", set_(closing=[141]), "manual", "pr", 2, ["140"]),
    ("closes two issues", set_(closing=[140, 141]), "manual", "pr", 2, ["140"]),
    ("open incident #40", set_(incidents=[{"number": 40, "title": "prod down"}]), "wait", "incident", 3, ["40"]),
    ("open incident #57", set_(incidents=[{"number": 57, "title": "queue stuck"}]), "wait", "incident", 3, ["57"]),
    ("deploy in progress", set_(deploy={"status": "in_progress", "conclusion": None}), "wait", "deploy", 3, []),
    ("deploy queued", set_(deploy={"status": "queued", "conclusion": None}), "wait", "deploy", 3, []),
    ("deploy failed", set_(deploy={"status": "completed", "conclusion": "failure"}), "wait", "deploy", 3, []),
    ("deploy cancelled", set_(deploy={"status": "completed", "conclusion": "cancelled"}), "wait", "deploy", 3, []),
    ("ticket closed", ticket(state="CLOSED"), "manual", "ticket", 4, []),
    ("no risk label", ticket_labels(drop=["risk:low"]), "manual", "risk", 4, []),
    ("risk:high", ticket_labels(add=["risk:high"], drop=["risk:low"]), "manual", "risk", 4, []),
    ("no size label", ticket_labels(drop=["size:M"]), "manual", "size", 4, []),
    ("size:L not in default sizes", ticket_labels(add=["size:L"], drop=["size:M"]), "manual", "size", 4, []),
    ("size:M not in sizes S", set_settings(sizes=["S"]), "manual", "size", 4, []),
    ("manual-land on the PR", set_(pr_labels=["manual-land"]), "manual", "label", 4, []),
    ("manual-land on the ticket", ticket_labels(add=["manual-land"]), "manual", "label", 4, []),
    ("protected path", add_files(".github/workflows/ci.yml"), "manual", "path", 5, [".github/workflows/ci.yml"]),
    ("protected path .claude", add_files(".claude/settings.json"), "manual", "path", 5, [".claude/settings.json"]),
    ("too many non-test files", set_(nontest_files=9), "manual", "too large", 5, []),
    ("too many non-test lines", set_(nontest_lines=401), "manual", "too large", 5, []),
    ("no review", set_(review=None), "manual", "review", 6, []),
    ("review not at head", review(commit_id=OLD, at_head=False), "wait", "re-check", 6, [OLD[:7], HEAD[:7]]),
    ("a high finding", review(high=1), "fix", "findings", 6, ["1 high"]),
    ("medium findings", review(medium=2), "fix", "findings", 6, ["2 medium"]),
    ("unresolved thread", set_(unresolved_threads=1), "manual", "comment", 7, []),
    ("owner comments", set_(owner_comments=2), "manual", "comment", 7, []),
    ("conflicting", set_(mergeable="CONFLICTING"), "fix", "conflict", 8, []),
    ("behind by 3", set_(behind_by=3), "wait", "behind", 8, ["3"]),
    ("behind by 17", set_(behind_by=17), "wait", "behind", 8, ["17"]),
    ("checks pending", set_(checks="pending", pending_checks=["verify"]), "wait", "ci", 9, []),
    ("checks failed", set_(checks="failure", failed_checks=["verify", "lint-py"]), "fix", "ci", 9, ["verify", "lint-py"]),
    ("checks failed other", set_(checks="failure", failed_checks=["deploy-stub"]), "fix", "ci", 9, ["deploy-stub"]),
    ("no checks", set_(checks="none"), "manual", "checks", 9, []),
]

# One failing mutation per rule 1-9, on disjoint keys, for the pairwise ordering test.
ONE_PER_RULE = {
    1: (set_settings(autoland=False), "manual", "off"),
    2: (set_(draft=True), "manual", "pr"),
    3: (set_(incidents=[{"number": 40, "title": "prod down"}]), "wait", "incident"),
    4: (ticket_labels(add=["risk:high"], drop=["risk:low"]), "manual", "risk"),
    5: (add_files(".github/workflows/ci.yml"), "manual", "path"),
    6: (set_(review=None), "manual", "review"),
    7: (set_(owner_comments=1), "manual", "comment"),
    8: (set_(mergeable="CONFLICTING"), "fix", "conflict"),
    9: (set_(checks="failure", failed_checks=["verify"]), "fix", "ci"),
}


class Evaluate(unittest.TestCase):
    def run_gate(self, facts, settings):
        out = lib("autoland").evaluate(facts, settings)
        self.assertIsInstance(out, dict)
        self.assertTrue({"verdict", "code", "reason", "rule"} <= set(out), out)
        self.assertIsInstance(out["reason"], str)
        self.assertTrue(out["reason"].strip(), out)
        return out

    def check(self, out, verdict, code, rule, *in_reason):
        self.assertEqual((out["verdict"], out["code"], out["rule"]), (verdict, code, rule), out)
        for text in in_reason:
            self.assertIn(text, out["reason"], out)

    def mutated(self, *mutations):
        f, s = base_facts(), base_settings()
        for m in mutations:
            m(f, s)
        return f, s

    def test_all_rules_pass_gives_land(self):
        other_sha = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a291807"
        variants = [
            ("base", base_facts(), base_settings()),
            ("another ticket, sha and login",
             base_facts(pr=9, head_ref="t/7-fix-login", head_sha=other_sha, author="octo", login="octo", closing=[7],
                        ticket={"number": 7, "state": "OPEN", "labels": ["bug", "risk:low", "size:S"]},
                        review={"commit_id": other_sha, "at_head": True, "high": 0, "medium": 0, "low": 0}),
             base_settings()),
            ("no deploy workflow", base_facts(deploy=None), base_settings()),
            ("size:L when the project allows L",
             base_facts(ticket={"number": 140, "state": "OPEN", "labels": ["size:L", "risk:low"]}),
             base_settings(sizes=["S", "M", "L"])),
            ("only S allowed and size:S", base_facts(ticket={"number": 140, "state": "OPEN", "labels": ["risk:low", "size:S"]}),
             base_settings(sizes=["S"])),
            ("unrelated PR labels and smoke_local", base_facts(pr_labels=["enhancement"]), base_settings(smoke_local=True)),
            ("at the size backstop", base_facts(nontest_files=8, nontest_lines=400), base_settings()),
            ("many test files", base_facts(files=[f"tests/test_{i}.py" for i in range(30)] + ["src/a.py"]), base_settings()),
        ]
        for label, facts, settings in variants:
            with self.subTest(label):
                out = self.run_gate(facts, settings)
                self.check(out, "land", "land", 10, facts["head_sha"][:7])
                if facts["head_sha"] != HEAD:
                    self.assertNotIn(HEAD[:7], out["reason"])

    def test_each_rule_failing_alone_gives_its_verdict(self):
        for label, mutation, verdict, code, rule, in_reason in ROWS:
            with self.subTest(label):
                f, s = self.mutated(mutation)
                self.check(self.run_gate(f, s), verdict, code, rule, *in_reason)

    def test_first_failing_rule_decides(self):
        f, s = self.mutated(set_(incidents=[{"number": 40, "title": "prod down"}]),
                            ticket_labels(add=["risk:high"], drop=["risk:low"]))
        self.check(self.run_gate(f, s), "wait", "incident", 3, "40")
        # every pair of failing rules: the earlier one is reported
        for a, b in itertools.combinations(sorted(ONE_PER_RULE), 2):
            with self.subTest(rules=(a, b)):
                f, s = self.mutated(ONE_PER_RULE[a][0], ONE_PER_RULE[b][0])
                _, verdict, code = ONE_PER_RULE[a]
                self.check(self.run_gate(f, s), verdict, code, a)
        # every rule failing from n up: n is reported
        for n in sorted(ONE_PER_RULE):
            with self.subTest(from_rule=n):
                f, s = self.mutated(*(ONE_PER_RULE[r][0] for r in sorted(ONE_PER_RULE) if r >= n))
                _, verdict, code = ONE_PER_RULE[n]
                self.check(self.run_gate(f, s), verdict, code, n)

    def test_low_findings_at_head_land_medium_is_fix(self):
        for low in (1, 3, 12):
            with self.subTest(low=low):
                f, s = self.mutated(review(low=low))
                self.check(self.run_gate(f, s), "land", "land", 10, HEAD[:7])
        cases = [  # (high, medium, low, substrings)
            (0, 1, 0, ["1 medium"]),
            (0, 1, 5, ["1 medium"]),
            (1, 0, 0, ["1 high"]),
            (1, 2, 0, ["1 high", "2 medium"]),
            (3, 0, 4, ["3 high"]),
            (0, 4, 0, ["4 medium"]),
        ]
        for high, medium, low, in_reason in cases:
            with self.subTest(high=high, medium=medium, low=low):
                f, s = self.mutated(review(high=high, medium=medium, low=low))
                self.check(self.run_gate(f, s), "fix", "findings", 6, *in_reason)

    def test_review_not_at_head_waits_and_no_review_is_manual(self):
        f, s = self.mutated(set_(review=None))
        self.check(self.run_gate(f, s), "manual", "review", 6)
        other_head = "fedcba9876543210fedcba9876543210fedcba98"
        for review_sha, head_sha in ((OLD, HEAD), ("1234567abcdef1234567abcdef1234567abcdef1", other_head)):
            with self.subTest(review=review_sha[:7], head=head_sha[:7]):
                f, s = self.mutated(set_(head_sha=head_sha), review(commit_id=review_sha, at_head=False))
                self.check(self.run_gate(f, s), "wait", "re-check", 6, review_sha[:7], head_sha[:7])
        # a stale review with findings still waits: its findings are not about the head
        f, s = self.mutated(review(commit_id=OLD, at_head=False, high=2, medium=1))
        self.check(self.run_gate(f, s), "wait", "re-check", 6, OLD[:7], HEAD[:7])

    def test_manual_land_label_on_pr_or_ticket(self):
        cases = [
            ("on the PR", set_(pr_labels=["manual-land"])),
            ("on the PR among others", set_(pr_labels=["enhancement", "manual-land", "docs"])),
            ("on the ticket", ticket_labels(add=["manual-land"])),
            ("on both", set_(pr_labels=["manual-land"]), ticket_labels(add=["manual-land"])),
        ]
        for label, *mutations in cases:
            with self.subTest(label):
                f, s = self.mutated(*mutations)
                self.check(self.run_gate(f, s), "manual", "label", 4)
        # a similar label is not manual-land
        for other in ("manual", "land", "manual-landing-notes"):
            with self.subTest(other=other):
                f, s = self.mutated(set_(pr_labels=[other]), ticket_labels(add=[other]))
                self.check(self.run_gate(f, s), "land", "land", 10)

    def test_protected_path_and_renamed_from_protected_path(self):
        protected = [
            ".github/workflows/ci.yml", ".github/CODEOWNERS", ".claude/settings.json", ".claude/agents/x.md",
            "scripts/deploy.sh", "scripts/smoke.sh", "scripts/deploy_guard.py", "scripts/verify.sh",
            "credentials/token.txt", "app/credentials/key.json", ".env", ".env.local", "config/.env.production",
            "certs/server.pem", "server.pem",
        ]
        for path in protected:
            with self.subTest(path=path):
                f, s = self.mutated(add_files(path))
                self.check(self.run_gate(f, s), "manual", "path", 5, path)
        # near misses of the defaults are not protected
        free = ["src/github/x.yml", "github/workflows/ci.yml", "scripts/build.sh", "docs/scripts/deploy.md",
                "src/env.py", "docs/credentials.md", "src/pem.txt", "scripts/deploy.sh.md", "README.md"]
        f, s = self.mutated(set_(files=free))
        self.check(self.run_gate(f, s), "land", "land", 10)
        # a rename lists both paths: from a protected path is protected
        for old, new in ((".github/workflows/old.yml", "ci/old.yml"), ("scripts/verify.sh", "tools/verify.sh")):
            with self.subTest(renamed_from=old):
                f, s = self.mutated(set_(files=["src/app/models.py", new, old]))
                self.check(self.run_gate(f, s), "manual", "path", 5, old)
        # the settings' manual_paths decide, project patterns included
        project = base_settings(manual_paths=list(DEFAULTS) + ["src/payments/**", "docs/legal/*.md"])
        for path in ("src/payments/charge.py", "src/payments/deep/refund.py", "docs/legal/terms.md"):
            with self.subTest(project_path=path):
                f = base_facts(files=["src/app/models.py", path])
                self.check(self.run_gate(f, project), "manual", "path", 5, path)
                self.check(self.run_gate(f, base_settings()), "land", "land", 10)
        f = base_facts(files=["docs/legal/sub/terms.md", "src/paymentsx/a.py"])
        self.check(self.run_gate(f, project), "land", "land", 10)

    def test_size_backstop_and_test_config_are_manual(self):
        # NOTE: the ticket's test line says "manual:protected" for test config; the rule
        # table's code for a protected path is `path`, which is asserted here.
        for nf, nl in ((9, 120), (1, 401), (20, 50), (9, 401), (3, 5000)):
            with self.subTest(nontest_files=nf, nontest_lines=nl):
                f, s = self.mutated(set_(nontest_files=nf, nontest_lines=nl))
                self.check(self.run_gate(f, s), "manual", "too large", 5)
        for nf, nl in ((8, 400), (0, 0), (8, 1), (1, 400)):
            with self.subTest(nontest_files=nf, nontest_lines=nl):
                f, s = self.mutated(set_(nontest_files=nf, nontest_lines=nl))
                self.check(self.run_gate(f, s), "land", "land", 10)
        for path in ("pyproject.toml", "package.json", "conftest.py", "tests/conftest.py",
                     "plugins/x/tests/conftest.py", "pytest.ini"):
            with self.subTest(path=path):
                f, s = self.mutated(add_files(path))
                self.check(self.run_gate(f, s), "manual", "path", 5, path)


# ---------------------------------------------------------------- settings from git
def git(repo, *args):
    return subprocess.run([GIT, "-C", repo, *args], env=clean_env(HOME=repo, **GIT_IDENTITY),
                          check=True, capture_output=True, text=True).stdout.strip()


def write(repo, rel, content):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content if isinstance(content, str) else json.dumps(content))


@unittest.skipIf(GIT is None, "git not found")
class GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-autoland-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def repo(self):
        self.n += 1
        repo = os.path.join(self.tmp, f"repo{self.n}")
        subprocess.run([GIT, "-c", "init.defaultBranch=main", "init", "-q", repo],
                       env=clean_env(HOME=self.tmp, **GIT_IDENTITY), check=True, capture_output=True)
        write(repo, "README.md", "x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        return repo

    def commit(self, repo, files, ref="refs/remotes/origin/main"):
        """Commit `files` ({rel: content}) on main and point `ref` at the commit."""
        for rel, content in files.items():
            write(repo, rel, content)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", "change")
        if ref:
            git(repo, "update-ref", ref, "HEAD")
        return git(repo, "rev-parse", "HEAD")

    def committed_settings(self, env):
        """A repo whose origin/main holds .claude/settings.json with `env` (None: no env key)."""
        repo = self.repo()
        data = {"permissions": {"allow": []}}
        if env is not None:
            data["env"] = env
        self.commit(repo, {".claude/settings.json": data})
        return repo

    def gate(self, repo, *args):
        out = lib("autoland").gate_settings(repo, *args)
        self.assertIsInstance(out, dict)
        return out

    def assert_off_defaults(self, out):
        self.assertIs(out["autoland"], False, out)
        self.assertEqual(out["sizes"], ["S", "M"], out)
        self.assertEqual(list(out["manual_paths"]), list(DEFAULTS), out)
        self.assertIs(out["smoke_local"], False, out)


class GateSettings(GitCase):
    def test_reads_committed_origin_main_only(self):
        repo = self.committed_settings({"HARNESS_AUTOLAND_SIZES": "S"})
        # local main moves ahead of origin/main with autoland on; the tree and local file say on too
        self.commit(repo, {".claude/settings.json": {"env": {"HARNESS_AUTOLAND": "1", "HARNESS_SMOKE_LOCAL": "1",
                                                             "HARNESS_AUTOLAND_SIZES": "S,M,L"}}}, ref=None)
        write(repo, ".claude/settings.local.json",
              {"env": {"HARNESS_AUTOLAND": "1", "HARNESS_SMOKE_LOCAL": "1", "HARNESS_MANUAL_PATHS": "src/**"}})
        loud = {"HARNESS_AUTOLAND": "1", "HARNESS_SMOKE_LOCAL": "1", "HARNESS_AUTOLAND_SIZES": "S,M,L",
                "HARNESS_MANUAL_PATHS": "src/**"}
        with mock.patch.dict(os.environ, loud):
            for args in ((), ("origin/main",)):
                with self.subTest(args=args):
                    out = self.gate(repo, *args)
                    self.assertIs(out["autoland"], False, out)
                    self.assertIs(out["smoke_local"], False, out)
                    self.assertEqual(out["sizes"], ["S"], out)
                    self.assertEqual(list(out["manual_paths"]), list(DEFAULTS), out)
            # the ref is honoured: local main holds the "on" commit
            out = self.gate(repo, "main")
            self.assertIs(out["autoland"], True, out)
            self.assertIs(out["smoke_local"], True, out)
            self.assertEqual(sorted(out["sizes"]), ["L", "M", "S"], out)
        # origin/main says on while the working tree and the environment say off
        self.commit(repo, {".claude/settings.json": {"env": {"HARNESS_AUTOLAND": "1", "HARNESS_SMOKE_LOCAL": "1"}}})
        write(repo, ".claude/settings.json", {"env": {"HARNESS_AUTOLAND": "0", "HARNESS_SMOKE_LOCAL": "0"}})
        write(repo, ".claude/settings.local.json", {"env": {"HARNESS_AUTOLAND": "0"}})
        with mock.patch.dict(os.environ, {"HARNESS_AUTOLAND": "0", "HARNESS_SMOKE_LOCAL": "0"}):
            out = self.gate(repo)
        self.assertIs(out["autoland"], True, out)
        self.assertIs(out["smoke_local"], True, out)
        self.assertEqual(out["sizes"], ["S", "M"], out)

    def test_project_manual_paths_extend_the_defaults(self):
        self.assertEqual(set(lib("autoland").DEFAULT_MANUAL_PATHS), set(DEFAULTS))
        self.assertEqual(len(list(lib("autoland").DEFAULT_MANUAL_PATHS)), len(DEFAULTS))
        cases = [  # (HARNESS_MANUAL_PATHS or None, the project's patterns after the defaults)
            (None, []),
            ("", []),
            ("src/payments/**", ["src/payments/**"]),
            (" src/payments/** , docs/legal/*.md ,", ["src/payments/**", "docs/legal/*.md"]),
            (".github/**,src/x/**,.claude/**", ["src/x/**"]),
            ("src/a/**,src/a/**,lib/b.py", ["src/a/**", "lib/b.py"]),
            ("plugins/harness/lib/harness_lib/autoland.py", ["plugins/harness/lib/harness_lib/autoland.py"]),
        ]
        for value, extra in cases:
            with self.subTest(value=value):
                repo = self.committed_settings({} if value is None else {"HARNESS_MANUAL_PATHS": value})
                out = self.gate(repo)
                self.assertEqual(list(out["manual_paths"]), list(DEFAULTS) + extra, out)
        # nothing the project writes removes a default
        for value in ("!.github/**", "-.claude/**", ".github/**", "none"):
            with self.subTest(value=value):
                repo = self.committed_settings({"HARNESS_MANUAL_PATHS": value})
                paths = list(self.gate(repo)["manual_paths"])
                self.assertEqual(paths[:len(DEFAULTS)], list(DEFAULTS), paths)
        # end to end: the gate's settings protect the project's paths and still the defaults
        repo = self.committed_settings({"HARNESS_AUTOLAND": "1", "HARNESS_MANUAL_PATHS": "src/payments/**"})
        settings = self.gate(repo)
        evaluate = lib("autoland").evaluate
        for path in ("src/payments/charge.py", ".github/workflows/ci.yml", "tests/conftest.py"):
            with self.subTest(path=path):
                out = evaluate(base_facts(files=["src/app/models.py", path]), settings)
                self.assertEqual((out["verdict"], out["code"], out["rule"]), ("manual", "path", 5), out)
        out = evaluate(base_facts(), settings)
        self.assertEqual((out["verdict"], out["code"], out["rule"]), ("land", "land", 10), out)

    def test_sizes_default_to_s_m_and_invalid_values_are_ignored(self):
        cases = [  # (HARNESS_AUTOLAND_SIZES or None, expected sizes, sorted)
            (None, ["M", "S"]),
            ("", ["M", "S"]),
            ("S,M", ["M", "S"]),
            ("S", ["S"]),
            ("L", ["L"]),
            ("S,M,L", ["L", "M", "S"]),
            (" S , L ", ["L", "S"]),
            ("S,X", ["M", "S"]),
            ("XL", ["M", "S"]),
            ("small,medium", ["M", "S"]),
            ("S,M,L,XL", ["M", "S"]),
            ("s,m", ["M", "S"]),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                repo = self.committed_settings({} if value is None else {"HARNESS_AUTOLAND_SIZES": value})
                out = self.gate(repo)
                self.assertIsInstance(out["sizes"], list, out)
                self.assertEqual(sorted(out["sizes"]), expected, out)

    def test_missing_or_invalid_settings_mean_off(self):
        cases = {}
        repo = self.repo()
        self.commit(repo, {"src/a.py": "x = 1\n"})
        cases["no settings file at the ref"] = repo
        repo = self.repo()
        self.commit(repo, {".claude/settings.json": "{not json"})
        cases["not JSON"] = repo
        repo = self.repo()
        self.commit(repo, {".claude/settings.json": '["HARNESS_AUTOLAND", "1"]'})
        cases["JSON not an object"] = repo
        cases["env not an object"] = self.committed_settings(["HARNESS_AUTOLAND=1"])
        cases["no env key"] = self.committed_settings(None)
        cases["HARNESS_AUTOLAND 0"] = self.committed_settings({"HARNESS_AUTOLAND": "0"})
        cases["HARNESS_AUTOLAND 2"] = self.committed_settings({"HARNESS_AUTOLAND": "2"})
        repo = self.repo()
        self.commit(repo, {".claude/settings.json": {"env": {"HARNESS_AUTOLAND": "1"}}}, ref=None)
        cases["no origin/main at all"] = repo
        repo = self.committed_settings({})
        write(repo, ".claude/settings.json", {"env": {"HARNESS_AUTOLAND": "1"}})
        cases["on only in the working tree"] = repo
        plain = os.path.join(self.tmp, "not-a-repo")
        write(plain, ".claude/settings.json", {"env": {"HARNESS_AUTOLAND": "1"}})
        cases["not a git repository"] = plain
        for label, repo in cases.items():
            with self.subTest(label):
                self.assert_off_defaults(self.gate(repo))
        # and the same file shape with "1" is on (the cases above are off for their reason)
        self.assertIs(self.gate(self.committed_settings({"HARNESS_AUTOLAND": "1"}))["autoland"], True)


class CommittedEnv(GitCase):
    def test_the_committed_env_object(self):
        env = {"HARNESS_AUTOLAND": "1", "HARNESS_TEST_PATTERNS": "tests/**", "N": 3}
        repo = self.committed_settings(env)
        write(repo, ".claude/settings.json", {"env": {"HARNESS_AUTOLAND": "0"}})
        write(repo, ".claude/settings.local.json", {"env": {"OTHER": "x"}})
        with mock.patch.dict(os.environ, {"HARNESS_AUTOLAND": "0"}):
            self.assertEqual(lib("settings").committed_env(repo), env)
            self.assertEqual(lib("settings").committed_env(repo, "origin/main"), env)
            self.assertEqual(lib("settings").committed_env(repo, ref="origin/main"), env)
        self.commit(repo, {".claude/settings.json": {"env": {"B": "2"}}}, ref="refs/heads/other")
        self.assertEqual(lib("settings").committed_env(repo, "other"), {"B": "2"})
        self.assertEqual(lib("settings").committed_env(repo), env)

    def test_missing_or_invalid_is_empty(self):
        committed_env = lib("settings").committed_env
        repo = self.repo()
        self.commit(repo, {"src/a.py": "x\n"})
        self.assertEqual(committed_env(repo), {})
        for text in ("{not json", "[1, 2]", '"text"', '{"env": [1]}', '{"env": "x"}', "{}"):
            with self.subTest(text=text):
                repo = self.repo()
                self.commit(repo, {".claude/settings.json": text})
                self.assertEqual(committed_env(repo), {})
        repo = self.repo()
        self.commit(repo, {".claude/settings.json": {"env": {"A": "1"}}}, ref=None)
        self.assertEqual(committed_env(repo), {})
        self.assertEqual(committed_env(repo, "main"), {"A": "1"})


# ---------------------------------------------------------------- imports
def is_stdlib(name: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
    origin = os.path.realpath(spec.origin)
    return origin.startswith(stdlib + os.sep) and "site-packages" not in origin


class Imports(unittest.TestCase):
    def test_imports_only_the_standard_library_and_harness_lib(self):
        self.assertTrue(AUTOLAND_PY.is_file(), f"{AUTOLAND_PY} does not exist")
        tree = ast.parse(AUTOLAND_PY.read_text(encoding="utf-8"), filename=str(AUTOLAND_PY))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # from . import settings: inside harness_lib
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in ("__future__", "harness_lib") or is_stdlib(top):
                    continue
                bad.append(f"line {node.lineno}: {name}")
        self.assertEqual(bad, [])

    def test_parses_as_python_3_9(self):
        self.assertTrue(AUTOLAND_PY.is_file(), f"{AUTOLAND_PY} does not exist")
        ast.parse(AUTOLAND_PY.read_text(encoding="utf-8"), filename=str(AUTOLAND_PY), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
