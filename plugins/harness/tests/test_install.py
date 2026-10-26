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
# #282: the "Sensitive remote targets:" entry earlier installs wrote, byte for byte. jotform-glow-sync's live gate
# (live_gate.sh, rjeskuri/jotform-glow-sync#146) now passes writes there, so the snippet carries a reworded entry
# and the installer replaces this exact string (never another entry by its opening words) in autoMode.environment.
REMOTE_TARGETS = "Sensitive remote targets:"
RETIRED_REMOTE_TARGETS = ("Sensitive remote targets: the Jotform forms, the Global G.L.O.W. Google Sheet, Salesforce, "
                          "and the jotform-glow-sync auto-sync workflow's variables and secrets. Every write there "
                          "needs my explicit go in the session.")
# Unchanged by #282, byte for byte.
DATA_LOCATIONS = ("Sensitive data locations & audiences: /Users/ryaneskuri/jotform-glow-sync data/, backups/ and "
                  "credentials/ hold minors' personal data; share with nothing outside that project's own tools.")
# How the summary line reports the replacement (beside "added ..."; the new entry is not also counted as added).
REPLACED_ENV = "replaced 1 autoMode.environment"


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

    # Jotform live writes through the live gate (#282). The classifier reads autoMode.environment: the remote-targets
    # entry says writes there go through once jotform-glow-sync's live gate has passed them (a backup of what is
    # written first, the write logged and notified), and deletes and --prune still need Ryan's explicit go. No synonym
    # list can catch every way of saying "writes still wait for me", so the checks are structural: the entry's shape
    # (assert_live_gate_policy) and two rules on every sentence (assert_writes_wait_for_nobody).

    # A person word names Ryan or his session; a gating word makes something wait. "hold" is left out ("credentials/
    # hold minors' personal data"), "held" and "on hold" are in; "Ryan" is a whole word (not /Users/ryaneskuri).
    PERSON = re.compile(r"\b(my|me|mine|myself|i|ryan|session|human|manual\w*|explicit\w*)\b", re.I)
    GATING = re.compile(r"\b(need\w*|requir\w*|wait\w*|must|until|only|held|on hold|pending|subject|clear\w*|"
                        r"review\w*|after)\b", re.I)
    WRITE = re.compile(r"\bwrit\w*", re.I)
    DELETES = re.compile(r"\bdelet\w*|--prune", re.I)
    GATE_NAME = re.compile(r"live[ _-]?gate\b|live_gate\.sh", re.I)
    # The live-gate sentence says writes go through, without hedging to staging, drafting or a dry run.
    GO_THROUGH = re.compile(r"\b(allowed|allows?|go(es)? through|proceeds?|permitted|automatic(ally)?)\b", re.I)
    NOT_ALLOWED = re.compile(r"(\b(not|never|no longer)|n't)\W+(\w+\W+){0,2}?(allow\w*|go(es)? through|proceed\w*|"
                             r"permitted)\b", re.I)
    HEDGE = re.compile(r"\b(stag\w*|prepar\w*|queu\w*|draft\w*|propos\w*|dry[- ]?run\w*|simulat\w*|preview\w*|"
                       r"plan\w*|suggest\w*)\b", re.I)

    @staticmethod
    def clauses(text):
        """`text` split after . ; ! or ? (not after a lone capital, so "G.L.O.W. Google" stays whole), backticks
        removed."""
        return [s for s in re.split(r"(?<=[.;!?])(?<!\b[A-Z]\.)\s+", text.replace("`", "")) if s.strip()]

    def gated_only_by_the_live_gate(self, sentence):
        """The sentence names the live gate, and every gating word in it leads to the live gate within five words
        ("allowed only after jotform-glow-sync's live gate has passed them")."""
        if not self.GATE_NAME.search(sentence):
            return False
        return all(re.match(r"\W+(\S+\s+){0,4}?\S*?(live[ _-]?gate)", sentence[m.end():], re.I)
                   for m in self.GATING.finditer(sentence))

    def assert_writes_wait_for_nobody(self, sentence):
        """A sentence naming Ryan (or his explicit go) is about deletes or --prune and names no write; a sentence
        with a gating word is about deletes or --prune, or is the live-gate sentence with the gate its only
        condition."""
        if self.PERSON.search(sentence):
            self.assertRegex(sentence, self.DELETES, "names Ryan, not about deletes or --prune")
            self.assertNotRegex(sentence, self.WRITE, "names Ryan beside a write")
        if self.GATING.search(sentence) and not self.DELETES.search(sentence):
            self.assertTrue(self.gated_only_by_the_live_gate(sentence),
                            f"makes something wait on more than the live gate: {sentence}")

    def assert_live_gate_policy(self, entry):
        """The entry's shape: the target list, the live-gate sentence (writes go through once the gate passed them),
        at most two sentences more on the backup, the log and the notification, then the deletes sentence. Nothing
        else, and every sentence passes assert_writes_wait_for_nobody."""
        self.assertTrue(entry.startswith(REMOTE_TARGETS), entry)
        self.assertNotEqual(entry, RETIRED_REMOTE_TARGETS)
        parts = self.clauses(entry)
        self.assertIn(len(parts), (3, 4, 5), f"not targets, gate, up to two on the record, deletes: {parts}")
        targets, gate, record, deletes = parts[0], parts[1], parts[2:-1], parts[-1]
        with self.subTest(part="targets", sentence=targets[:70]):
            for target in ("jotform", "google sheet", "salesforce", "auto-sync"):      # still the same targets
                self.assertIn(target, targets.lower())
            for pattern in (self.WRITE, self.GATE_NAME, self.PERSON, self.GATING, self.DELETES):
                self.assertNotRegex(targets, pattern)
        with self.subTest(part="gate", sentence=gate[:70]):
            self.assertRegex(gate, self.GATE_NAME)
            self.assertRegex(gate, self.WRITE)
            self.assertRegex(gate, self.GO_THROUGH)
            self.assertNotRegex(gate, self.NOT_ALLOWED)
            self.assertNotRegex(gate, self.HEDGE)
            self.assertNotRegex(gate, self.DELETES)
        for sentence in record:
            with self.subTest(part="record", sentence=sentence[:70]):
                self.assertRegex(sentence, re.compile(r"\bbackup|\blog(ged|s)?\b|\bnotif\w*", re.I))
                for pattern in (self.PERSON, self.GATING, self.HEDGE, self.DELETES):
                    self.assertNotRegex(sentence, pattern)
        said = " ".join([gate] + record)
        self.assertTrue(any(re.search(r"\bbackup", s, re.I) and re.search(r"\b(first|before\w*|already)\b", s, re.I)
                            for s in [gate] + record), f"no sentence says a backup exists first: {entry}")
        self.assertRegex(said, re.compile(r"\blog(ged|s)?\b", re.I))
        self.assertRegex(said, re.compile(r"\bnotif\w*", re.I))
        with self.subTest(part="deletes", sentence=deletes[:70]):
            self.assertRegex(deletes, re.compile(r"\bdelet\w*", re.I))
            self.assertIn("--prune", deletes)
            self.assertRegex(deletes, re.compile(r"explicit\s+go", re.I))
            self.assertNotRegex(deletes, self.WRITE)
        for sentence in parts:
            with self.subTest(sentence=sentence[:70]):
                self.assert_writes_wait_for_nobody(sentence)

    def remote_targets(self, entries):
        """The one "Sensitive remote targets:" entry of `entries`, after checking it says the live-gate policy."""
        found = [e for e in entries if e.startswith(REMOTE_TARGETS)]
        self.assertEqual(len(found), 1, found)
        self.assert_live_gate_policy(found[0])
        return found[0]

    def test_snippet_remote_targets_entry_allows_live_gate_writes_and_asks_for_deletes(self):
        wanted = snippet_lists(self)                     # the seven openings, in order, "$defaults" first
        env = wanted["autoMode", "environment"]
        self.assertNotIn(RETIRED_REMOTE_TARGETS, env)
        self.remote_targets(env)
        self.assertEqual([e for e in env if e.startswith("Sensitive data locations & audiences:")], [DATA_LOCATIONS])

    def test_fresh_home_gets_the_live_gate_line_and_not_the_retired_one(self):
        wanted = snippet_lists(self)
        new = self.remote_targets(wanted["autoMode", "environment"])
        for name, start in (("no settings file", None), ("settings without autoMode", SETTINGS),
                            ("an empty autoMode.environment", {"autoMode": {"environment": []}})):
            with self.subTest(start=name):
                if start is None:
                    if os.path.exists(self.settings):
                        os.remove(self.settings)
                else:
                    self.reset(start)
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                env = read_json(self.settings)["autoMode"]["environment"]
                self.assertNotIn(RETIRED_REMOTE_TARGETS, env)
                self.assertEqual(env.count(new), 1, env)
                self.assertEqual(self.remote_targets(env), new)
                self.assertEqual([e for e in env if e.startswith("Sensitive data locations")], [DATA_LOCATIONS])
                self.assertEqual(env[:1], [DEFAULTS])
                line = settings_line(p.stdout)
                self.assertIn(f"{len(ENVIRONMENT_OPENINGS)} autoMode.environment", line)   # the new one is added
                self.assertNotIn("replaced", line)                                      # nothing was there to replace

    def test_reinstall_replaces_the_retired_line_in_place(self):
        wanted = snippet_lists(self)
        env = wanted["autoMode", "environment"]
        new = self.remote_targets(env)
        same_label = "Sensitive remote targets: my own staging bucket at s3://mine; writes there need nothing."
        near_miss = RETIRED_REMOTE_TARGETS.replace("Salesforce, ", "")       # mine: not the retired string exactly
        last_install = [RETIRED_REMOTE_TARGETS if e == new else e for e in env]  # what the previous install wrote
        self.assertEqual(last_install.count(RETIRED_REMOTE_TARGETS), 1)
        cases = {
            "the last install's list, then mine": last_install + ["Mine."],
            "mid-list beside my own remote-targets entries": [DEFAULTS, "Mine A.", RETIRED_REMOTE_TARGETS, same_label,
                                                              near_miss, "Mine B."],
            "alone, no sentinel": [RETIRED_REMOTE_TARGETS],
            "before a mid-list sentinel": ["Mine A.", RETIRED_REMOTE_TARGETS, DEFAULTS, env[1], "Mine B."],
            "the new line already there too": [DEFAULTS, new, "Mine A.", RETIRED_REMOTE_TARGETS, env[2]],
            "twice": [RETIRED_REMOTE_TARGETS, "Mine A.", RETIRED_REMOTE_TARGETS],
        }
        snippet = read_json(SNIPPET)
        for name, own in cases.items():
            with self.subTest(case=name):
                # The retired string takes the new entry in its place; when the new entry is already there, the
                # retired one is only dropped. Derived from the fixture, not from the installer.
                replaced, have_new = [], new in own
                for entry in own:
                    if entry != RETIRED_REMOTE_TARGETS:
                        replaced.append(entry)
                    elif not have_new:
                        replaced.append(new)
                        have_new = True
                # Everything else already as the snippet has it, so the line names only autoMode.environment.
                mine = {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}
                mine.update(theme="dark", permissions={k: wanted["permissions", k] for k in ("allow", "ask")},
                            autoMode={"environment": own, "allow": wanted["autoMode", "allow"],
                                      "soft_deny": [DEFAULTS, "Never push to main."]})
                before = self.reset(mine)
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                got = merged["autoMode"]["environment"]
                self.assertNotIn(RETIRED_REMOTE_TARGETS, got)
                self.assertEqual(got.count(new), 1, got)
                self.assert_merged(got, replaced, env, sentinel=True)      # mine stay, in order; new in old's place
                for entry in own:
                    if entry != RETIRED_REMOTE_TARGETS:
                        self.assertIn(entry, got)
                self.assertEqual(merged["autoMode"]["allow"], wanted["autoMode", "allow"])
                self.assertEqual(merged["autoMode"]["soft_deny"], [DEFAULTS, "Never push to main."])
                self.assertEqual(merged["permissions"], {k: wanted["permissions", k] for k in ("allow", "ask")})
                self.assertEqual(merged["theme"], "dark")

                line = settings_line(p.stdout)
                self.assertTrue(line.startswith("~/.claude/settings.json: "), line)
                self.assertNotIn("already up to date", line)
                if name == "twice":
                    self.assertRegex(line, r"\breplaced [12] autoMode\.environment\b")
                else:
                    self.assertIn(REPLACED_ENV, line)
                added = [e for e in env if e != DEFAULTS and e not in replaced]
                if added:
                    self.assertIn(f"added {len(added)} autoMode.environment", line)
                else:
                    self.assertNotIn("added", line)
                if own[:1] == [DEFAULTS] and own.count(DEFAULTS) == 1:            # the sentinel did not move
                    self.assertNotIn("$defaults", line)
                self.assertNotRegex(line, r"permissions\.|autoMode\.allow")
                backups = self.backups()
                self.assertEqual(len(backups), 1)
                with open(os.path.join(os.path.dirname(self.settings), backups[0]), "rb") as fh:
                    self.assertEqual(fh.read(), before)
                with open(self.settings, "rb") as fh:
                    after_first = fh.read()

                p = self.install()                                         # again: nothing left to replace
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
                self.assertNotIn("replaced", settings_line(p.stdout))
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), after_first)
                self.assertEqual(self.backups(), backups)

        with self.subTest(case="the last install's list, exactly"):          # the common case, spelled out
            self.reset({"autoMode": {"environment": last_install}})
            p = self.install()
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(read_json(self.settings)["autoMode"]["environment"], env)

    def test_user_text_does_not_say_every_jotform_write_needs_an_explicit_go(self):
        # No file under user/ keeps the retired sentence. Every sentence there asking for Ryan's explicit go, and every
        # sentence of a line naming the remote targets (Jotform, Salesforce, G.L.O.W., "remote targets"), passes
        # assert_writes_wait_for_nobody: only deletes and --prune wait for Ryan, or for anything but the live gate.
        # Sentences are read per line, so one in a JSON string does not run on into the next entry.
        targets = re.compile(r"jotform|salesforce|g\.l\.o\.w|remote targets", re.I)
        for name, text in self.user_files().items():
            with self.subTest(file=name):
                self.assertFalse("every write there needs my explicit go" in text.lower() or
                                 RETIRED_REMOTE_TARGETS in text, f"user/{name} keeps the retired sentence")
            for line in text.splitlines():
                for sentence in self.clauses(line):
                    if not (targets.search(line) or re.search(r"explicit\s+go", sentence, re.I)):
                        continue
                    with self.subTest(file=name, sentence=sentence.strip()[:70]):
                        self.assert_writes_wait_for_nobody(sentence)


if __name__ == "__main__":
    unittest.main()
