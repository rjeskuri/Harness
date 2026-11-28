"""Exercise the GPT entry point with a real repository and a fake Codex executable."""
from __future__ import annotations

import json
import os
import runpy
from unittest.mock import patch
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
TOOL = PLUGIN / "bin" / "harness-gpt"


class Gpt(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.log = self.root / "call.json"
        fake = self.root / "codex"
        fake.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path.cwd()
receipt = {"args": args, "input": sys.stdin.read(), "files": sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())}
if (root / "changes.diff").exists():
    receipt["patch"] = (root / "changes.diff").read_bytes().decode()
if "--output-schema" in args:
    receipt["schema"] = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
pathlib.Path(__file__).with_name("call.json").write_text(json.dumps(receipt))
if "--json" in args:
    print(json.dumps({"type": "thread.started", "thread_id": "550e8400-e29b-41d4-a716-446655440000"}))
reply = pathlib.Path(__file__).with_name("reply.json")
checked = reply.read_text() if reply.exists() else '{"findings":[],"prior_fixed":[]}'
pathlib.Path(args[args.index("-o") + 1]).write_text(checked if "--output-schema" in args else "Architecture advice")
""")
        fake.chmod(0o755)
        # A fresh HOME and no HARNESS_SOL_CHECK, so the owner's Sol switch on this machine does not reach the tool.
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"], HOME=str(self.root / "owner"))
        self.env.pop("HARNESS_SOL_CHECK", None)
        self.git("init", "-q", "-b", "main")
        for name in ("app.py", ".env", "credentials/example.py", "secrets.yaml", "aws-credentials.toml", "service-account-prod-us.yaml"):
            p = self.repo / name
            p.parent.mkdir(exist_ok=True)
            p.write_text("example = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.git("switch", "-qc", "t/1-example")

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                                        "-c", "commit.gpgsign=false", *args], cwd=self.repo, text=True).strip()

    def call(self, *args):
        return subprocess.run([str(TOOL), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def test_repo_review_is_astra_and_only_sees_snapshot(self):
        (self.repo / "private.py").write_text("ignored by snapshot")
        p = self.call("repo-review")
        self.assertEqual(p.returncode, 0, p.stderr)
        call = json.loads(self.log.read_text())
        self.assertIn("source/app.py", call["files"])
        self.assertFalse(any(".env" in x or "credentials" in x or "service-account" in x or "secrets.yaml" in x or "private.py" in x for x in call["files"]))
        self.assertIn("gpt-6-astra", call["args"])
        self.assertIn("never", call["args"])

    def test_snapshot_rejects_credential_documents_before_reading_blobs(self):
        module = runpy.run_path(str(TOOL))
        original = module["git"]
        forbidden = set()
        names = ["prod-credentials.md", "service-account-prod-us.yaml"]
        for name in names:
            (self.repo / name).write_text("test fixture for " + name)
            self.git("add", name)
            forbidden.add(self.git("rev-parse", ":" + name))
        self.git("commit", "-qm", "credential filename fixtures")

        def traced(repo, *argv):
            if argv[:2] == ("cat-file", "blob"):
                self.assertNotIn(argv[2], forbidden)
            return original(repo, *argv)

        with patch.dict(module["snapshot"].__globals__, git=traced):
            files, _ = module["snapshot"](self.repo, self.git("rev-parse", "HEAD"), self.root / "copy", [])
        self.assertIn("app.py", files)
        self.assertTrue(set(names).isdisjoint(files))

    def test_architect_for_a_new_directory(self):
        p = self.call("architect", "--repo", str(self.root / "future"), "--brief", "Python task tracker")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Python task tracker", json.loads(self.log.read_text())["input"])
        self.assertFalse((self.root / "future").exists())

    def test_cmux_architect_continues_its_session_only(self):
        cmux = self.root / "cmux"
        cmux.write_text("#!/usr/bin/env python3\nimport json, pathlib, sys\n"
                        "pathlib.Path(__file__).with_name('split.json').write_text(json.dumps(sys.argv[1:]))")
        cmux.chmod(0o755)
        self.env.update(CMUX_WORKSPACE_ID="workspace:17", CMUX_SURFACE_ID="surface:30", TMPDIR=str(self.root))
        p = self.call("architect", "--terminal", "--brief", "A project; touch nope")
        self.assertEqual(p.returncode, 0, p.stderr)
        argv = json.loads((self.root / "split.json").read_text())
        self.assertIn("workspace:17", argv)
        self.assertIn("surface:30", argv)
        command = argv[argv.index("--command") + 1]
        self.assertNotIn("touch nope", command)
        folder = Path(shlex.split(command)[1])
        reply = subprocess.run(["/bin/sh", "-c", command], env=dict(self.env, TMPDIR="/tmp"), input="What about storage?\n/exit\n",
                               capture_output=True, text=True)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        call = json.loads(self.log.read_text())
        self.assertIn("--ignore-user-config", call["args"])
        self.assertNotIn("--ephemeral", call["args"])
        self.assertEqual(call["args"][call["args"].index("resume") + 1], "550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(call["input"], "What about storage?")
        self.assertFalse(folder.exists())

    def test_committed_sensitive_rule_survives_worktree_edit(self):
        settings = self.repo / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {"HARNESS_SENSITIVE_PATHS": "app.py"}}))
        self.git("add", ".claude/settings.json")
        self.git("commit", "-qm", "protected source")
        settings.write_text("{}")
        p = self.call("repo-review")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("source/app.py", json.loads(self.log.read_text())["files"])

    def test_check_preserves_missing_final_newline(self):
        (self.repo / ".gitattributes").write_text("app.py -diff\n")
        self.git("add", ".gitattributes")
        (self.repo / "app.py").write_bytes(b"first")
        self.git("commit", "-qam", "no final newline")
        base = self.git("rev-parse", "HEAD")
        (self.repo / "app.py").write_bytes(b"second")
        self.git("commit", "-qam", "change")
        p = self.call("check", "--head", self.git("rev-parse", "HEAD"), "--base", base)
        self.assertEqual(p.returncode, 0, p.stderr)
        patch = json.loads(self.log.read_text())["patch"]
        self.assertIn("-first\n", patch)
        self.assertIn("+second\n", patch)
        self.assertEqual(patch.count("No newline at end of file"), 2)

    def test_check_keeps_five_unrated_findings_and_passes_prior_fixed(self):
        """#534: Sol's findings carry no severity (the schema Codex is given asks for path, line and body), at most
        five of them are kept in Sol's order, and prior_fixed is passed through as Sol gave it."""
        (self.repo / "app.py").write_text("".join(f"v{i} = {i}\n" for i in range(1, 9)))
        self.git("commit", "-qam", "eight values")
        head, base = self.git("rev-parse", "HEAD"), self.git("rev-parse", "main")

        def found(i):
            return {"path": "app.py", "line": i, "body": f"v{i} is read before it is set, so the export is wrong."}

        rows = [  # name, Sol's findings, its prior-blocker calls
            ("seven findings", [found(i) for i in (4, 1, 7, 2, 8, 3, 6)], [True, False, True]),
            ("three findings", [found(i) for i in (6, 2, 5)], [False]),
            ("five findings", [found(i) for i in (3, 1, 4, 5, 2)], [False, False]),
        ]
        for name, findings, prior in rows:
            with self.subTest(name):
                (self.root / "reply.json").write_text(json.dumps({"findings": findings, "prior_fixed": prior}))
                p = self.call("check", "--head", head, "--base", base)
                self.assertEqual(p.returncode, 0, p.stderr)
                out = json.loads(p.stdout)
                self.assertEqual((out["head"], out["model"]), (head, "gpt-6.2-sol"))
                self.assertEqual(out["findings"], findings[:5])
                self.assertEqual(out["prior_fixed"], prior)
                item = json.loads(self.log.read_text())["schema"]["properties"]["findings"]["items"]
                self.assertNotIn("severity", item["properties"])
                self.assertEqual(sorted(item["required"]), ["body", "line", "path"])

    def test_sol_check_off_calls_no_codex_and_clears_prior_blockers(self):
        (self.repo / "app.py").write_text("example = 2\n")
        self.git("commit", "-qam", "change")
        brief = json.dumps({"request": "r", "prior_blockers": [{"path": "app.py", "line": 1, "severity": "medium",
                                                                 "body": "b"}] * 2})
        home = self.root / "home"
        for label, env in (("env switch", dict(self.env, HARNESS_SOL_CHECK="off")),
                           ("file switch", dict(self.env, HOME=str(home)))):
            with self.subTest(label):
                if label == "file switch":
                    (home / ".claude" / "harness").mkdir(parents=True)
                    (home / ".claude" / "harness" / "sol-check-off").write_text("")
                p = subprocess.run([str(TOOL), "check", "--head", self.git("rev-parse", "HEAD"),
                                    "--base", self.git("rev-parse", "main"), "--brief-stdin"],
                                   cwd=self.repo, env=env, input=brief, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout), {"head": self.git("rev-parse", "HEAD"), "model": "gpt-6.2-sol",
                                                        "findings": [], "prior_fixed": [True, True]})
                self.assertFalse(self.log.exists())

    def test_check_uses_sol_and_refuses_unreviewable_change(self):
        (self.repo / "app.py").write_text("example = 2\n")
        self.git("commit", "-qam", "change")
        p = self.call("check", "--head", self.git("rev-parse", "HEAD"), "--base", self.git("rev-parse", "main"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["findings"], [])
        self.assertIn("gpt-6.2-sol", json.loads(self.log.read_text())["args"])
        (self.repo / ".env").write_text("fixture changed")
        self.git("commit", "-qam", "excluded")
        self.log.unlink()
        p = self.call("check", "--head", self.git("rev-parse", "HEAD"), "--base", self.git("rev-parse", "main"))
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.log.exists())
