"""Tests for scripts/install.sh: the plugin steps, the CLAUDE.md copy, the settings merge and the digest's
LaunchAgent.

    python3 -m unittest discover -s plugins/harness/tests -v

The installer runs under `sh` with a temporary HOME, CLAUDE_BIN pointing at a fake `claude` (it logs its
argv and answers `already ...` with exit 1 when a command repeats, as the real one does on a second
install) and HARNESS_LAUNCHCTL pointing at a fake `launchctl` that logs its argv. PATH is the fake folder
and the system folders, so the settings merge and harness-digest run on the system python3. Nothing here
touches the real ~/.claude or loads a LaunchAgent. The fakes are written once for the module.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
INSTALL = os.path.join(REPO, "scripts", "install.sh")
SNIPPET = os.path.join(REPO, "user", "settings.snippet.json")
DIGEST = os.path.join(REPO, "plugins", "harness", "bin", "harness-digest")
LABEL = "com.ryaneskuri.harness-digest"
W = {}

FAKE_CLAUDE = """import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.environ["CLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("9.9.9 (Claude Code, fake)")
    sys.exit(0)
marker = os.path.join(os.environ["CLAUDE_STATE"], hashlib.sha1(json.dumps(args).encode()).hexdigest())
if os.path.exists(marker):
    print("already " + " ".join(args[:2]))
    sys.exit(1)
open(marker, "w").close()
print("ok")
"""

FAKE_LAUNCHCTL = """import json, os, sys
args = sys.argv[1:]
with open(os.environ["LAUNCHCTL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args and args[0] in os.environ.get("LAUNCHCTL_FAIL", "").split(","):
    sys.stderr.write(args[0] + " failed: 5: Input/output error\\n")
    sys.exit(5)
"""

SETTINGS = {
    "theme": "dark",
    "model": "sonnet",
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "enabledPlugins": {"companion@ryan": True, "harness@ryan": True},
    "env": {"HARNESS_NOTIFY_SOUND": ""},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-install-"))
    W.update(root=root, fakebin=os.path.join(root, "fakebin"))
    for name, source in (("claude", FAKE_CLAUDE), ("launchctl", FAKE_LAUNCHCTL)):
        script = write(os.path.join(root, f"{name}.py"), source)
        executable(os.path.join(W["fakebin"], name), f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')


def tearDownModule():
    shutil.rmtree(W.get("root", ""), True)


class Install(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="test-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "claude-state")
        os.makedirs(self.state)
        os.makedirs(self.home)
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.claude_log, self.launch_log = os.path.join(self.tmp, "claude.log"), os.path.join(self.tmp, "launchctl.log")

    def install(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "CLAUDE_", "LAUNCHCTL_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([W["fakebin"], "/usr/bin", "/bin"]),
                   CLAUDE_BIN=os.path.join(W["fakebin"], "claude"), CLAUDE_LOG=self.claude_log,
                   CLAUDE_STATE=self.state, HARNESS_LAUNCHCTL=os.path.join(W["fakebin"], "launchctl"),
                   LAUNCHCTL_LOG=self.launch_log)
        env.update(extra)
        return subprocess.run(["sh", INSTALL], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=120)

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def backups(self):
        folder = os.path.dirname(self.settings)
        return sorted(n for n in os.listdir(folder) if n.startswith("settings.json.bak-"))

    def test_install_twice(self):
        write(self.settings, json.dumps(SETTINGS, indent=4))
        with open(self.settings, encoding="utf-8") as fh:
            original = fh.read()
        snippet = read_json(SNIPPET)
        today = dt.date.today().isoformat()

        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"marketplace ryan: added from {REPO}", p.stdout)
        self.assertIn("plugin harness@ryan: installed (user scope)", p.stdout)
        self.assertEqual(self.lines(self.claude_log), [["--version"], ["plugin", "marketplace", "add", REPO],
                                                       ["plugin", "install", "harness@ryan"]])
        with open(os.path.join(REPO, "user", "CLAUDE.md"), encoding="utf-8") as want, \
                open(os.path.join(self.home, ".claude", "CLAUDE.md"), encoding="utf-8") as got:
            self.assertEqual(got.read(), want.read())

        merged = read_json(self.settings)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual({k: merged[k] for k in ("model", "effortLevel", "modelSettings")},
                         {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")})
        self.assertNotIn("_comment", merged)
        self.assertEqual(merged["enabledPlugins"], {"harness@ryan": True})          # companion gone
        for k in ("theme", "permissions", "env"):                                     # the rest untouched
            self.assertEqual(merged[k], SETTINGS[k])
        with open(self.settings, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, json.dumps(merged, indent=2) + "\n")
        self.assertEqual(self.backups(), [f"settings.json.bak-{today}"])
        with open(f"{self.settings}.bak-{today}", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertIn(f'~/.claude/settings.json: set model, effortLevel; set modelSettings for '
                      f'{", ".join(snippet["modelSettings"])}; removed enabledPlugins["companion@ryan"] '
                      f'(backup settings.json.bak-', p.stdout)

        plist = os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")
        with open(plist, "rb") as fh:
            job = plistlib.load(fh)
        self.assertEqual(job["ProgramArguments"][1], os.path.realpath(DIGEST))
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 8, "Minute": 30})
        self.assertIn(["bootstrap", f"gui/{os.getuid()}", plist], self.lines(self.launch_log))
        self.assertIn("harness-digest: wrote", p.stdout)
        self.assertIn('security add-generic-password -a "$USER" -s harness-anthropic -w', p.stdout)
        self.assertIn("harness-projects discover", p.stdout)

        p = self.install()                                                          # again: nothing changes
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("marketplace ryan: already added", p.stdout)
        self.assertIn("plugin harness@ryan: already installed", p.stdout)
        self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
        with open(self.settings, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)
        self.assertEqual(self.backups(), [f"settings.json.bak-{today}"])

    def test_no_settings_yet(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        snippet = read_json(SNIPPET)
        self.assertEqual(read_json(self.settings), {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")})
        self.assertEqual(self.backups(), [])
        self.assertIn(f'~/.claude/settings.json: set model, effortLevel; set modelSettings for '
                      f'{", ".join(snippet["modelSettings"])} (created)', p.stdout)

    def test_model_settings_merge_per_model_id(self):
        snippet = read_json(SNIPPET)
        ids = list(snippet["modelSettings"])
        self.assertGreaterEqual(len(ids), 2)
        mine = {ids[0]: snippet["modelSettings"][ids[0]],                       # already as the snippet has it
                ids[1]: {"effortLevel": "low", "maxEffortLevel": "high"},       # overridden by the snippet
                "claude-haiku-4-5": {"autoCompactWindow": 200000}}              # not in the snippet: kept
        write(self.settings, json.dumps({"model": snippet["model"], "effortLevel": snippet["effortLevel"],
                                         "modelSettings": mine, "theme": "dark"}))
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        merged = read_json(self.settings)
        self.assertEqual(merged["modelSettings"], dict(snippet["modelSettings"],
                                                       **{"claude-haiku-4-5": {"autoCompactWindow": 200000}}))
        self.assertEqual(merged["theme"], "dark")
        self.assertIn(f"~/.claude/settings.json: set modelSettings for {', '.join(ids[1:])} (backup ", p.stdout)
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
        self.assertEqual(read_json(self.settings), merged)

    def test_unreadable_settings_are_left_alone_and_fail_the_install_at_the_end(self):
        write(self.settings, "{not json")
        p = self.install()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("~/.claude/settings.json: left unchanged, cannot read it", p.stderr)
        with open(self.settings, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")
        self.assertEqual(self.backups(), [])
        plist = os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")      # the later steps ran
        self.assertIn(["bootstrap", f"gui/{os.getuid()}", plist], self.lines(self.launch_log))
        self.assertIn("Done, except the settings merge, which failed", p.stdout)
        self.assertNotIn("Done. Start", p.stdout)
        self.assertIn("security add-generic-password", p.stdout)

    def test_a_failed_launchagent_does_not_fail_the_install(self):
        p = self.install(LAUNCHCTL_FAIL="bootstrap,load")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("could not load it", p.stderr)
        self.assertIn("the LaunchAgent is not loaded", p.stdout)
        self.assertIn("security add-generic-password", p.stdout)                  # the install carried on


if __name__ == "__main__":
    unittest.main()
