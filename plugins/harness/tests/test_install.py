"""Tests for scripts/install.sh: the plugin steps, the CLAUDE.md copy, the settings merge and the digest's
LaunchAgent.

    python3 -m unittest discover -s plugins/harness/tests -v

The installer runs under `sh` with a temporary HOME, CLAUDE_BIN pointing at a fake `claude` (it logs its
argv and answers `already ...` with exit 1 when a command repeats, as the real one does on a second
install) and HARNESS_LAUNCHCTL pointing at a fake `launchctl` that logs its argv. PATH is the fake folder
and the system folders, so the settings merge and harness-digest run on the system python3. Nothing here
touches the real ~/.claude or loads a LaunchAgent. The fakes are written once for the module.

Two more modes of the fake `claude`, for the concurrent-install test (#244): with CLAUDE_FAIL_STEP set to a
`plugin` subcommand (`marketplace` or `install`) that step writes CLAUDE_FAIL_MESSAGE to stderr and exits 1;
with CLAUDE_NESTED set to a folder and CLAUDE_NESTED_STEP naming a step, that step, after writing its own answer
and before exiting, runs the installer named by CLAUDE_INSTALL to completion in that folder with a fresh HOME,
state and logs (and neither mode), recording its output in `out.txt` and its exit code in `rc` there. That is a
second install running at the same moment, between the outer step's write of its output and the installer's read.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import re
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
TEMPLATE = os.path.join(REPO, "plugins", "harness", "templates", "settings.json")
LABEL = "com.ryaneskuri.harness-digest"
W = {}

FAKE_CLAUDE = """import hashlib, json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["CLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("9.9.9 (Claude Code, fake)")
    sys.exit(0)
step = args[1] if len(args) > 1 else ""
marker = os.path.join(os.environ["CLAUDE_STATE"], hashlib.sha1(json.dumps(args).encode()).hexdigest())
if step and step == os.environ.get("CLAUDE_FAIL_STEP"):
    sys.stderr.write(os.environ["CLAUDE_FAIL_MESSAGE"] + "\\n")
    code = 1
elif os.path.exists(marker):
    print("already " + " ".join(args[:2]))
    code = 1
else:
    open(marker, "w").close()
    print("ok")
    code = 0
sys.stdout.flush()
sys.stderr.flush()
folder = os.environ.get("CLAUDE_NESTED")
if folder and step == os.environ.get("CLAUDE_NESTED_STEP"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_NESTED", "CLAUDE_NESTED_STEP", "CLAUDE_FAIL_STEP", "CLAUDE_FAIL_MESSAGE")}
    env.update(HOME=os.path.join(folder, "home"), CLAUDE_STATE=os.path.join(folder, "state"),
               CLAUDE_LOG=os.path.join(folder, "claude.log"), LAUNCHCTL_LOG=os.path.join(folder, "launchctl.log"))
    os.makedirs(env["HOME"], exist_ok=True)
    os.makedirs(env["CLAUDE_STATE"], exist_ok=True)
    with open(os.path.join(folder, "out.txt"), "w", encoding="utf-8") as out:
        rc = subprocess.run(["sh", os.environ["CLAUDE_INSTALL"]], cwd=folder, env=env, stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT).returncode
    with open(os.path.join(folder, "rc"), "w", encoding="utf-8") as fh:
        fh.write(str(rc))
sys.exit(code)
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


# The snippet's permission and autoMode lists, pinned independently of the installer: the nine harness rules of
# the project template, the three ask rules (a raw merge and both landing commands: sessions may not land, #240),
# and the two autoMode lists led by the "$defaults" sentinel (the prose entries are pinned by their opening words,
# from the ticket's Context; no autoMode.allow entry allows landing).
LAND_ASK_RULES = ["Bash(harness-pr land *)", "Bash(harness-pr autoland *)"]
ASK_RULES = ["Bash(gh pr merge *)"] + LAND_ASK_RULES
ENVIRONMENT_OPENINGS = ["Organization:", "Source control:", "CI/CD deploy targets:", "Review convention:",
                        "Key internal services:", "Sensitive data locations & audiences:", "Sensitive remote targets:"]
AUTO_ALLOW_OPENINGS = ["Writing or loading a com.ryaneskuri.harness-* LaunchAgent"]
DEFAULTS = "$defaults"
LISTS = (("permissions", "allow"), ("permissions", "ask"), ("autoMode", "environment"), ("autoMode", "allow"))
# What a merge into a file holding none of the snippet's entries reports: every entry counted, "$defaults" not.
ADDED_ALL = (f"added 9 permissions.allow, {len(ASK_RULES)} permissions.ask, "
             f"{len(ENVIRONMENT_OPENINGS)} autoMode.environment, {len(AUTO_ALLOW_OPENINGS)} autoMode.allow")


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


def snippet_lists(test):
    """The snippet's four lists, after checking they carry what the ticket says (so no merge test can pass on
    an empty snippet list)."""
    snippet = read_json(SNIPPET)
    with open(TEMPLATE, encoding="utf-8") as fh:
        nine = [r for r in json.load(fh)["permissions"]["allow"] if r.startswith("Bash(harness-")]
    test.assertEqual(len(nine), 9)
    test.assertIn("permissions", snippet, "the snippet carries no permissions")
    test.assertIn("autoMode", snippet, "the snippet carries no autoMode")
    perms, auto = snippet["permissions"], snippet["autoMode"]
    test.assertEqual(sorted(perms.get("allow", [])), sorted(nine))
    test.assertEqual(perms.get("ask"), ASK_RULES)
    test.assertEqual(set(perms), {"allow", "ask"})
    test.assertEqual(set(auto), {"environment", "allow"})
    for key, openings in (("environment", ENVIRONMENT_OPENINGS), ("allow", AUTO_ALLOW_OPENINGS)):
        entries = auto[key]
        test.assertEqual(entries[0], DEFAULTS)
        test.assertEqual(len(entries), 1 + len(openings), entries)
        for entry, opening in zip(entries[1:], openings):
            test.assertTrue(entry.startswith(opening), (entry, opening))
        test.assertEqual(len(set(entries)), len(entries))
    return {(obj, key): list(snippet[obj][key]) for obj, key in LISTS}


def settings_line(stdout):
    lines = [ln for ln in stdout.splitlines() if ln.startswith("~/.claude/settings.json:")]
    return lines[0] if len(lines) == 1 else "\n".join(lines)


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
        for k in ("theme", "env"):                                                    # the rest untouched
            self.assertEqual(merged[k], SETTINGS[k])
        wanted = snippet_lists(self)                                                  # the lists gain the snippet's
        self.assertEqual(merged["permissions"]["deny"], [])
        self.assertEqual(merged["permissions"]["allow"][0], "Bash(ls:*)")
        self.assertEqual(sorted(merged["permissions"]["allow"][1:]), sorted(wanted["permissions", "allow"]))
        self.assertEqual(merged["permissions"]["ask"], wanted["permissions", "ask"])
        self.assertEqual(merged["autoMode"], {"environment": wanted["autoMode", "environment"],
                                              "allow": wanted["autoMode", "allow"]})
        with open(self.settings, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, json.dumps(merged, indent=2) + "\n")
        self.assertEqual(self.backups(), [f"settings.json.bak-{today}"])
        with open(f"{self.settings}.bak-{today}", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        line = settings_line(p.stdout)                                              # one line names every change
        for piece in ("~/.claude/settings.json: set model, effortLevel; ",
                      f'set modelSettings for {", ".join(snippet["modelSettings"])}',
                      'removed enabledPlugins["companion@ryan"]', ADDED_ALL,
                      "added 9 permissions.allow, 3 permissions.ask, 7 autoMode.environment, 1 autoMode.allow",
                      f"(backup settings.json.bak-{today})"):
            self.assertIn(piece, line)

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
        wanted = snippet_lists(self)
        expected = {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}
        expected["permissions"] = {"allow": wanted["permissions", "allow"], "ask": wanted["permissions", "ask"]}
        expected["autoMode"] = {"environment": wanted["autoMode", "environment"], "allow": wanted["autoMode", "allow"]}
        self.assertEqual(read_json(self.settings), expected)
        self.assertEqual(self.backups(), [])
        line = settings_line(p.stdout)
        for piece in ("~/.claude/settings.json: set model, effortLevel; ",
                      f'set modelSettings for {", ".join(snippet["modelSettings"])}',
                      ADDED_ALL,
                      "added 9 permissions.allow, 3 permissions.ask, 7 autoMode.environment, 1 autoMode.allow"):
            self.assertIn(piece, line)
        self.assertTrue(line.endswith(" (created)"), line)

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
        line = settings_line(p.stdout)                      # model and effortLevel already matched: not named
        self.assertTrue(line.startswith("~/.claude/settings.json: "), line)
        self.assertNotIn("set model,", line)
        self.assertNotIn("effortLevel", line)
        self.assertIn(f"set modelSettings for {', '.join(ids[1:])}", line)
        self.assertIn(" (backup ", line)
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

    # Sessions may not land (#240): after an install, both landing commands prompt and nothing in autoMode.allow
    # lets the classifier wave a landing through. The rules are spelled out here, not read from the snippet.

    def test_sessions_may_not_land_after_install(self):
        starts = {
            "fresh HOME, no settings": None,
            "settings with none of the lists": SETTINGS,
            "own ask and autoMode.allow": {"permissions": {"allow": ["Bash(ls:*)"],
                                                           "ask": ["Bash(git push --force:*)"]},
                                           "autoMode": {"allow": ["Running make test is allowed."]}},
            "the raw-merge ask rule already there": {"permissions": {"ask": ["Bash(gh pr merge *)"]},
                                                     "autoMode": {"allow": [DEFAULTS], "environment": []}},
            "one land rule already there": {"permissions": {"ask": ["Bash(harness-pr autoland *)", "Bash(x)"]}},
        }
        for name, start in starts.items():
            with self.subTest(start=name):
                if start is None:
                    if os.path.exists(self.settings):
                        os.remove(self.settings)
                else:
                    self.reset(start)
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                ask = merged["permissions"]["ask"]
                for rule in ("Bash(gh pr merge *)", "Bash(harness-pr land *)", "Bash(harness-pr autoland *)"):
                    self.assertEqual(ask.count(rule), 1, (rule, ask))
                self.assertIn("Bash(harness-pr:*)", merged["permissions"]["allow"])  # allowed, but landing asks
                for rule in ("Bash(harness-pr land *)", "Bash(harness-pr autoland *)"):
                    self.assertNotIn(rule, merged["permissions"]["allow"])
                landing = [e for e in merged["autoMode"]["allow"]
                           if re.search(r"\bland|autoland|harness-pr|\bmerg", e, re.IGNORECASE)]
                self.assertEqual(landing, [], "an autoMode.allow entry mentions landing")
                self.assertEqual(merged["autoMode"]["allow"][0], DEFAULTS)
                self.assertGreaterEqual(len(merged["autoMode"]["allow"]), 2, "the LaunchAgent entry is gone too")

    def test_the_snippet_asks_before_landing_and_never_allows_it(self):
        snippet = read_json(SNIPPET)
        ask = snippet["permissions"]["ask"]
        self.assertEqual(ask[:1], ["Bash(gh pr merge *)"])                       # the land rules come after it
        for rule in ("Bash(harness-pr land *)", "Bash(harness-pr autoland *)"):
            self.assertEqual(ask.count(rule), 1, (rule, ask))
            self.assertGreater(ask.index(rule), 0)
        for entry in snippet["autoMode"]["allow"]:
            self.assertIsNone(re.search(r"\bland|autoland|harness-pr", entry, re.IGNORECASE), entry)

    # Neither `harness-pr autoland` nor an autoland LaunchAgent exists yet (epic #132). The text checks below read
    # each sentence (a line split after . ; ! or ?) with backticks removed. A line that is only a permission rule
    # string, such as "Bash(harness-pr autoland *)", is a rule, not a description, and is exempt.
    RULE_ONLY = re.compile(r'^\s*"[A-Za-z]+\([^"]*\)",?\s*$')
    # Said of autoland, these fail a sentence whatever else it says: merge or land wording reaching autoland
    # through via/through/by/with (no parenthesis or clause break between), autoland followed by a merge or land
    # verb, "Merging: autoland", and autoland in the same sentence as "eligible" (the auto-land rubric).
    AUTOLAND_CLAIMS = re.compile(
        r"\b(merg\w*|land\w*)\b[^.;()]{0,30}?\b(via|through|by|with)\b[^.;()]{0,40}?autoland"
        r"|autoland\b[^.;()]{0,30}?\b(merges|lands|merged|landed|merging|landing)\b"
        r"|\bmerging:\s*(harness-pr\W+)?autoland"
        r"|\beligible\b[^.;]*autoland|autoland[^.;]*\beligible\b", re.IGNORECASE)
    MERGE_OR_LAND = re.compile(r"\b(merg\w*|land|lands|landed|landing)\b", re.IGNORECASE)
    # Planned means explicit planning wording within a few words of autoland or of epic #132 ("once", "will",
    # "later" do not count: they read as present-tense conditions).
    _PLAN_WORD = r"(not yet|planned|plan|does not exist yet|is not built)"
    PLANNED = re.compile(rf"(autoland|\b132)\b\W+(\w+\W+){{0,4}}?{_PLAN_WORD}\b"
                         rf"|\b{_PLAN_WORD}\W+(\w+\W+){{0,4}}?(\S*autoland|#?132)\b", re.IGNORECASE)

    def user_files(self):
        folder = os.path.join(REPO, "user")
        names = sorted(os.listdir(folder))
        self.assertIn("CLAUDE.md", names)
        self.assertIn("settings.snippet.json", names)
        out = {}
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    out[name] = fh.read()
        return out

    @staticmethod
    def sentences(text):
        return [s for s in re.split(r"(?<=[.;!?])\s+", text.replace("`", "")) if s.strip()]

    def test_user_text_does_not_say_autoland_merges_today(self):
        # Nothing under user/ says autoland merges or lands pull requests today: no sentence pairs autoland with
        # merge or land wording (AUTOLAND_CLAIMS), and a sentence naming autoland beside merge or land wording frames it
        # as planned. Every line naming autoland also names epic #132.
        for name, text in self.user_files().items():
            for number, line in enumerate(text.splitlines(), 1):
                if "autoland" not in line.lower() or self.RULE_ONLY.match(line):
                    continue
                with self.subTest(file=name, line=number):
                    self.assertRegex(line, r"#132\b|epic 132\b")
                for sentence in self.sentences(line):
                    if "autoland" not in sentence.lower():
                        continue
                    with self.subTest(file=name, line=number, sentence=sentence.strip()[:70]):
                        self.assertIsNone(self.AUTOLAND_CLAIMS.search(sentence), "says autoland merges today")
                        if self.MERGE_OR_LAND.search(sentence):
                            self.assertRegex(sentence, self.PLANNED, "autoland beside merge/land wording, "
                                                                     "not framed as planned")

    def test_auto_mode_environment_names_autoland_only_as_planned(self):
        # The classifier context does not list autoland among what exists today (the LaunchAgents, the merge
        # path): every sentence of an autoMode.environment entry that names autoland frames it as planned.
        env = read_json(SNIPPET)["autoMode"]["environment"]
        services = [e for e in env if e.startswith("Key internal services:")]
        self.assertEqual(len(services), 1)
        self.assertIn("com.ryaneskuri.harness-", services[0])                      # still describes the jobs
        # The LaunchAgents that exist today: autoland is in no parenthesised list there, and no sentence naming
        # the LaunchAgents or scheduled jobs names autoland.
        for group in re.findall(r"\(([^()]*)\)", services[0]):
            with self.subTest(group=group[:70]):
                self.assertNotIn("autoland", group.lower())
        for sentence in self.sentences(services[0]):
            if re.search(r"launchagent|scheduled", sentence, re.IGNORECASE):
                with self.subTest(sentence=sentence[:70]):
                    self.assertNotIn("autoland", sentence.lower())
        for entry in env:
            for sentence in self.sentences(entry):
                if "autoland" in sentence.lower():
                    with self.subTest(sentence=sentence[:70]):
                        self.assertIsNone(self.AUTOLAND_CLAIMS.search(sentence))
                        self.assertRegex(sentence, self.PLANNED)

    def test_user_claude_md_says_how_landing_works_today(self):
        # Ryan lands with /harness:land or approves the land prompt: one sentence of user/CLAUDE.md says both.
        text = self.user_files()["CLAUDE.md"]
        both = [s for s in self.sentences(text)
                if "/harness:land" in s and re.search(r"approv\w*\W+(\w+\W+){0,4}?(the\W+)?(land|prompt)", s, re.I)]
        self.assertTrue(both, "no sentence names /harness:land and approving the land prompt")

    # The permission and autoMode merge. Expected lists are derived from the test's own fixture and the
    # snippet's entries (checked against the ticket by snippet_lists), never from the installer.

    def reset(self, settings):
        """A fresh settings file (an object is written as JSON with indent=4, a string as is) and no backups."""
        for name in self.backups() if os.path.isdir(os.path.dirname(self.settings)) else []:
            os.remove(os.path.join(os.path.dirname(self.settings), name))
        write(self.settings, settings if isinstance(settings, str) else json.dumps(settings, indent=4))
        with open(self.settings, "rb") as fh:
            return fh.read()

    def assert_merged(self, got, own, snippet_entries, sentinel):
        """`got` is `own` with every snippet entry it lacked appended once; with `sentinel`, "$defaults" leads
        it exactly once and the rest keeps `own`'s order."""
        self.assertIsInstance(got, list)
        if sentinel:
            self.assertEqual(got[:1], [DEFAULTS], got)
            self.assertEqual(got.count(DEFAULTS), 1, got)
            own = [e for e in own if e != DEFAULTS]
            got = got[1:]
            snippet_entries = [e for e in snippet_entries if e != DEFAULTS]
        self.assertEqual(got[:len(own)], own, "the target's own entries changed or moved")
        self.assertEqual(sorted(got[len(own):]), sorted(e for e in snippet_entries if e not in own))
        for entry in snippet_entries:
            self.assertEqual(got.count(entry), 1, entry)

    def test_permissions_and_auto_mode_are_merged_once(self):
        wanted = snippet_lists(self)
        allow, ask = wanted["permissions", "allow"], wanted["permissions", "ask"]
        env, auto_allow = wanted["autoMode", "environment"], wanted["autoMode", "allow"]
        own = {
            ("permissions", "allow"): ["Bash(ls:*)", allow[3], "Read(~/notes/**)", allow[0], "Bash(make test)"],
            ("permissions", "ask"): ["Bash(git push --force:*)", ask[2]],
            ("autoMode", "environment"): ["My own context line.", env[2], DEFAULTS, "Another of mine."],
            ("autoMode", "allow"): [auto_allow[1], "Running make test is allowed."],
        }
        mine = {"theme": "light", "model": "opus",
                "permissions": {"allow": own["permissions", "allow"], "ask": own["permissions", "ask"],
                                "deny": ["Bash(rm -rf:*)", "Read(./.env)"]},
                "autoMode": {"environment": own["autoMode", "environment"], "allow": own["autoMode", "allow"],
                             "soft_deny": ["$defaults", "Never push to main."]}}
        self.reset(mine)
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        merged = read_json(self.settings)
        for obj, key in LISTS:
            with self.subTest(list=f"{obj}.{key}"):
                self.assert_merged(merged[obj][key], own[obj, key], wanted[obj, key], sentinel=obj == "autoMode")
        self.assertEqual(merged["permissions"]["deny"], mine["permissions"]["deny"])
        self.assertEqual(merged["autoMode"]["soft_deny"], mine["autoMode"]["soft_deny"])
        self.assertEqual(merged["theme"], "light")
        line = settings_line(p.stdout)
        # The own lists already hold ask[2], env[2] (and the sentinel) and auto_allow[1]: one fewer of the first two
        # is added, and autoMode.allow, already holding its one prose entry, gains nothing and gets no count (only its sentinel moves).
        self.assertIn(f"added {len(allow) - 2} permissions.allow, {len(ask) - 1} permissions.ask, "
                      f"{len(ENVIRONMENT_OPENINGS) - 1} autoMode.environment", line)
        self.assertIn("added 7 permissions.allow, 2 permissions.ask, 6 autoMode.environment", line)
        self.assertNotRegex(line, r"\d+ autoMode\.allow")
        self.assertIn(" (backup settings.json.bak-", line)
        self.assertEqual(len(self.backups()), 1)

        # A second, different file in the same place: every list already holds some snippet entries, others none.
        own = {
            ("permissions", "allow"): list(reversed(allow[4:])) + ["Bash(npm test)"],
            ("permissions", "ask"): ask + ["Bash(git reset --hard:*)"],
            ("autoMode", "environment"): list(env[3:]),
            ("autoMode", "allow"): [],
        }
        self.reset({"permissions": {"allow": own["permissions", "allow"], "ask": own["permissions", "ask"]},
                    "autoMode": {"environment": own["autoMode", "environment"], "allow": []}})
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        merged = read_json(self.settings)
        for obj, key in LISTS:
            with self.subTest(list=f"{obj}.{key}", file="second"):
                self.assert_merged(merged[obj][key], own[obj, key], wanted[obj, key], sentinel=obj == "autoMode")
        line = settings_line(p.stdout)
        self.assertIn(f"added {len(allow[:4])} permissions.allow, {len(env[1:3])} autoMode.environment, "
                      f"{len(AUTO_ALLOW_OPENINGS)} autoMode.allow", line)               # env[3:] held already
        self.assertIn("added 4 permissions.allow, 2 autoMode.environment, 1 autoMode.allow", line)
        self.assertNotIn("permissions.ask", line)                                  # nothing added there

    def test_defaults_sentinel_leads_each_auto_mode_list(self):
        wanted = snippet_lists(self)
        env, auto_allow = wanted["autoMode", "environment"], wanted["autoMode", "allow"]
        cases = {
            "autoMode absent": None,
            "lists absent": {},
            "absent from the lists": {"environment": ["Mine A."], "allow": ["Mine B.", "Mine C."]},
            "mid-list": {"environment": ["Mine A.", DEFAULTS, "Mine C."], "allow": ["Mine B.", DEFAULTS]},
            "last, and first": {"environment": [env[1], env[4], DEFAULTS], "allow": [DEFAULTS, "Mine B."]},
            "twice": {"environment": [DEFAULTS, "Mine A.", DEFAULTS], "allow": ["Mine B.", DEFAULTS, DEFAULTS]},
            "only the sentinel out of place": {"environment": env[1:] + [DEFAULTS],
                                               "allow": [auto_allow[1], DEFAULTS]},
        }
        snippet = read_json(SNIPPET)
        for name, auto in cases.items():
            with self.subTest(case=name):
                # Everything else already as the snippet has it, so autoMode is the only thing left to change.
                mine = {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}
                mine.update(theme="dark", permissions={k: wanted["permissions", k] for k in ("allow", "ask")})
                if auto is not None:
                    mine["autoMode"] = auto
                self.reset(mine)
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                for key in ("environment", "allow"):
                    self.assert_merged(merged["autoMode"][key], (auto or {}).get(key, []),
                                       wanted["autoMode", key], sentinel=True)
                self.assertNotIn("already up to date", settings_line(p.stdout))

    def test_second_run_is_a_no_op(self):
        wanted = snippet_lists(self)
        snippet = read_json(SNIPPET)
        # Merged by hand already, in an order and layout of its own: the first run changes nothing at all.
        by_hand = {"permissions": {"deny": ["Bash(rm -rf:*)"],
                                   "allow": ["Bash(ls:*)"] + list(reversed(wanted["permissions", "allow"])),
                                   "ask": ["Bash(git push --force:*)"] + wanted["permissions", "ask"]},
                   "autoMode": {"allow": [DEFAULTS, "Mine."] + list(reversed(wanted["autoMode", "allow"][1:])),
                                "environment": wanted["autoMode", "environment"] + ["Mine too."]},
                   "modelSettings": snippet["modelSettings"], "effortLevel": snippet["effortLevel"],
                   "model": snippet["model"], "theme": "dark"}
        before = self.reset(by_hand)
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.backups(), [])

        # Needing only the new merge (the sentinel mid-list too): the first run writes, the next change nothing.
        own_allow, own_env = ["Bash(ls:*)", wanted["permissions", "allow"][5]], ["Mine.", DEFAULTS]
        self.reset({"permissions": {"allow": own_allow}, "autoMode": {"environment": own_env, "soft_deny": ["Never."]},
                    "theme": "dark", **{k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}})
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("already up to date", settings_line(p.stdout))
        merged = read_json(self.settings)
        self.assert_merged(merged["permissions"]["allow"], own_allow, wanted["permissions", "allow"], sentinel=False)
        self.assert_merged(merged["autoMode"]["environment"], own_env, wanted["autoMode", "environment"], sentinel=True)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        with open(self.settings, "rb") as fh:
            after_first = fh.read()
        for second in (1, 2):
            with self.subTest(run=second + 1):
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), after_first)
                self.assertEqual(self.backups(), backups)

    def test_deny_default_mode_and_soft_deny_untouched(self):
        wanted = snippet_lists(self)
        present = {"permissions": {"defaultMode": "acceptEdits", "additionalDirectories": ["/tmp/shared"],
                                   "deny": ["Bash(rm -rf:*)", wanted["permissions", "allow"][8], "Read(./.env)"],
                                   "allow": ["Bash(ls:*)"]},
                   "autoMode": {"soft_deny": [DEFAULTS, wanted["autoMode", "allow"][1], "Never push to main."],
                                "environment": ["Mine."]},
                   "theme": "dark", "env": {"FOO": "1"}}
        cases = {
            "present": present,
            "objects absent": {"theme": "dark"},
            "keys absent": {"permissions": {"allow": ["Bash(ls:*)"]}, "autoMode": {"allow": ["Mine."]}},
            "empty objects": {"permissions": {}, "autoMode": {}},
        }
        for name, mine in cases.items():
            with self.subTest(case=name):
                self.reset(mine)
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                for obj, merged_keys in (("permissions", {"allow", "ask"}), ("autoMode", {"environment", "allow"})):
                    theirs = mine.get(obj, {})
                    self.assertEqual(set(merged[obj]), set(theirs) | merged_keys)        # nothing added beside
                    for key in set(theirs) - merged_keys:                                # deny, defaultMode, ...
                        self.assertEqual(merged[obj][key], theirs[key], key)
                    for key in merged_keys:                                              # and the merge did run
                        self.assert_merged(merged[obj][key], theirs.get(key, []), wanted[obj, key],
                                           sentinel=obj == "autoMode")
                for key in set(mine) - {"permissions", "autoMode"}:
                    self.assertEqual(merged[key], mine[key], key)

    def test_malformed_settings_untouched_by_the_new_merge(self):
        wanted = snippet_lists(self)
        malformed = {"not JSON": "{not json", "truncated": '{"permissions": {"allow": ["Bash(ls:*)"]',
                     "a list": json.dumps(["Bash(ls:*)"]), "a string": '"permissions"', "null": "null",
                     "empty": ""}
        for name, text in malformed.items():
            with self.subTest(case=name):
                before = self.reset(text)
                p = self.install()
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("~/.claude/settings.json: left unchanged", p.stderr)
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), before)
                self.assertEqual(sorted(n for n in os.listdir(os.path.dirname(self.settings))
                                        if n.startswith("settings")), ["settings.json"])      # no backup, no temp
                self.assertIn("Done, except the settings merge, which failed", p.stdout)

        with self.subTest(case="unreadable"):
            before = self.reset({"permissions": {"allow": ["Bash(ls:*)"]}})
            os.chmod(self.settings, 0)
            self.addCleanup(os.chmod, self.settings, 0o600)
            p = self.install()
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("~/.claude/settings.json: left unchanged", p.stderr)
            os.chmod(self.settings, 0o600)
            with open(self.settings, "rb") as fh:
                self.assertEqual(fh.read(), before)
            self.assertEqual(self.backups(), [])

        # The same place with a readable object: the merge does run there, so the cases above are not vacuous.
        self.reset({"permissions": {"allow": ["Bash(ls:*)"]}})
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_merged(read_json(self.settings)["permissions"]["allow"], ["Bash(ls:*)"],
                           wanted["permissions", "allow"], sentinel=False)

    def test_wrong_types_are_refused_and_leave_the_file_alone(self):
        # Each case would otherwise change model and drop companion@ryan, so an untouched file means the whole
        # merge was refused, not merely the bad list skipped. An explicit null is a wrong type like any other.
        objects = {
            "permissions a list": {"permissions": ["Bash(ls:*)"]},
            "permissions a string": {"permissions": "x"},
            "permissions null": {"permissions": None},
            "autoMode a string": {"autoMode": "x"},
            "autoMode a list": {"autoMode": [DEFAULTS]},
            "autoMode a number": {"autoMode": 0},
            "autoMode null": {"autoMode": None},
        }
        lists = {
            "permissions.allow of numbers": {"permissions": {"allow": [1]}},
            "permissions.allow a string": {"permissions": {"allow": "Bash(ls:*)"}},
            "permissions.allow with a null entry": {"permissions": {"allow": ["Bash(ls:*)", None]}},
            "permissions.allow null": {"permissions": {"allow": None}},
            "permissions.ask an object": {"permissions": {"ask": {"Bash(x)": True}}},
            "permissions.ask null": {"permissions": {"ask": None, "allow": ["Bash(ls:*)"]}},
            "autoMode.environment a string": {"autoMode": {"environment": "x"}},
            "autoMode.environment with a nested list": {"autoMode": {"environment": [DEFAULTS, ["Mine."]]}},
            "autoMode.environment null": {"autoMode": {"environment": None}},
            "autoMode.allow true": {"autoMode": {"allow": True}},
            "autoMode.allow null": {"autoMode": {"allow": None, "environment": [DEFAULTS]}},
        }
        cases = [(name, bad, f"{next(iter(bad))} is not a JSON object") for name, bad in objects.items()]
        cases += [(name, bad, f"{name.split(' ')[0]} is not a list of strings") for name, bad in lists.items()]
        for name, bad, says in cases:
            with self.subTest(case=name):
                before = self.reset(dict({"model": "sonnet", "theme": "dark",
                                          "enabledPlugins": {"companion@ryan": True}}, **bad))
                p = self.install()
                out = p.stdout + p.stderr
                self.assertNotEqual(p.returncode, 0, out)
                self.assertNotIn("Traceback", out)
                self.assertIn(f"~/.claude/settings.json: left unchanged, {says}", p.stderr)
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), before)
                self.assertEqual(sorted(n for n in os.listdir(os.path.dirname(self.settings))
                                        if n.startswith("settings")), ["settings.json"])      # no backup, no temp
                self.assertIn("Done, except the settings merge, which failed", p.stdout)
                self.assertNotIn("Done. Start", p.stdout)

        # The same base with the lists well formed: the merge runs and writes, so the refusals above are not vacuous.
        self.reset({"model": "sonnet", "theme": "dark", "enabledPlugins": {"companion@ryan": True},
                    "permissions": {"allow": ["Bash(ls:*)"]}, "autoMode": {"environment": []}})
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        merged = read_json(self.settings)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["enabledPlugins"], {})

    # #244: install.sh wrote every `claude plugin ...` step's output to one fixed path and then read it back, so a
    # second install running at the same moment (parallel test runs, two terminals) overwrote it in between and the
    # first install judged its step by the other's output. The fake runs a whole second install inside the step,
    # after the step has written its own answer, so the check is deterministic rather than a race.

    def test_concurrent_install_keeps_each_step_output_its_own(self):
        labels = {"marketplace": ("marketplace ryan: already added", "marketplace ryan: add failed:"),
                  "install": ("plugin harness@ryan: already installed", "plugin harness@ryan: install failed:")}
        for step in ("marketplace", "install"):
            for outcome in ("already", "fails"):
                with self.subTest(step=step, outcome=outcome):
                    case = os.path.join(self.tmp, f"{step}-{outcome}")
                    home, state, nested = (os.path.join(case, n) for n in ("home", "state", "nested"))
                    for folder in (home, state, nested):
                        os.makedirs(folder)
                    own = dict(HOME=home, CLAUDE_STATE=state, CLAUDE_LOG=os.path.join(case, "claude.log"),
                               LAUNCHCTL_LOG=os.path.join(case, "launchctl.log"))
                    if outcome == "already":                                # a first install, alone
                        p = self.install(**own)
                        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    message = f"error: {step} refused ({os.urandom(6).hex()})"
                    extra = dict(own, CLAUDE_NESTED=nested, CLAUDE_NESTED_STEP=step, CLAUDE_INSTALL=INSTALL)
                    if outcome == "fails":
                        extra.update(CLAUDE_FAIL_STEP=step, CLAUDE_FAIL_MESSAGE=message)
                    p = self.install(**extra)
                    out = p.stdout + p.stderr

                    # The second install ran inside the step, alone and to a clean finish.
                    with open(os.path.join(nested, "rc"), encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), "0", out)
                    with open(os.path.join(nested, "out.txt"), encoding="utf-8") as fh:
                        nested_out = fh.read()
                    self.assertIn(f"marketplace ryan: added from {REPO}", nested_out)
                    self.assertIn("plugin harness@ryan: installed (user scope)", nested_out)

                    # The first install judged its step by its own output, not the second one's.
                    already, failed = labels[step]
                    if outcome == "already":
                        self.assertEqual(p.returncode, 0, out)
                        self.assertIn(already, p.stdout)
                        self.assertNotIn("failed:", p.stdout)
                        self.assertIn("plugin harness@ryan: already installed", p.stdout)
                    else:
                        self.assertEqual(p.returncode, 1, out)
                        lines = p.stdout.splitlines()
                        self.assertIn(failed, lines, out)
                        self.assertEqual(lines[lines.index(failed) + 1:], [message], out)
                        self.assertNotIn(already, p.stdout)
                        steps = [a[:2] for a in self.lines(own["CLAUDE_LOG"]) if a[:1] == ["plugin"]]
                        self.assertEqual(steps[-1], ["plugin", step], "the install went on past its failed step")


if __name__ == "__main__":
    unittest.main()
