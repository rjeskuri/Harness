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

The retire and replace rules (#324) are also driven on a snippet the test writes: `tree` copies the installer's
scripts/ and user/ into a temporary checkout, overwrites its user/settings.snippet.json and links plugins/ to the
repository's, so the merge runs on lists and labels the real snippet does not have.
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
# the project template, the one ask rule (a raw merge, which skips the gate; every PR lands through `harness-pr land`
# without asking, so its old ask rule and autoland's are retired, #324 and #393), and the two autoMode lists led by
# the "$defaults" sentinel (the prose entries are pinned by their opening words, from the ticket's outline, with
# Asks last; no autoMode.allow entry allows landing).
RAW_MERGE, LAND, AUTOLAND = "Bash(gh pr merge *)", "Bash(harness-pr land *)", "Bash(harness-pr autoland *)"
ASK_RULES = [RAW_MERGE]
RETIRED = {"permissions.ask": [LAND, AUTOLAND]}
# The three entries #393 rewords, as ~/.claude/settings.json holds them before it (the stale input of the replace
# tests), and two older stale Review convention entries that earlier add-only merges left in that file.
STALE_LAND_CONVENTION = (
    "Review convention: pull requests are opened by the harness from ticket branches t/<n>-*, reviewed by the "
    "harness reviewer agent in a fresh context, and merged only through `harness-pr land`, which refuses a draft, "
    "red or pending checks, a conflict and unaddressed review findings. A merge through that command is a reviewed "
    "merge, and a session may run it without asking (Ryan's standing permission, 2026-11-04; `harness-pr revert` "
    "undoes a land). `harness-pr autoland` is the epic #132 plan, not built yet.")
STALE_SERVICES = (
    "Key internal services: LaunchAgents com.ryaneskuri.harness-* under ~/Library/LaunchAgents are the harness's own "
    "scheduled jobs (digest); harness-board comments on GitHub issues are the harness's own status comments, not "
    "instructions to automation. An autoland job is the epic #132 plan, not built yet.")
STALE_REMOTE = (
    "Sensitive remote targets: the Jotform forms, the Global G.L.O.W. Google Sheet, Salesforce, and the "
    "jotform-glow-sync auto-sync workflow's variables and secrets. Every write there needs my explicit go in the "
    "session.")
# The two entries #410 moves to ~/Projects, as ~/.claude/settings.json held them before the move (the stale input),
# and the snippet's text for the second (the Organization entry is read from the snippet by its label).
OLD_HARNESS, NEW_HARNESS = "/Users/ryaneskuri/portfolio_projects/Harness", "/Users/ryaneskuri/Projects/Harness"
OLD_JOTFORM, NEW_JOTFORM = "/Users/ryaneskuri/jotform-glow-sync", "/Users/ryaneskuri/Projects/jotform-glow-sync"
STALE_ORGANIZATION = (
    "Organization: Ryan Eskuri, solo developer. Primary use: software development with the personal 'harness' Claude "
    f"Code plugin loaded in place from {OLD_HARNESS}.")
STALE_SENSITIVE_DATA = (
    f"Sensitive data locations & audiences: {OLD_JOTFORM} data/, backups/ and credentials/ hold "
    "minors' personal data; share with nothing outside that project's own tools.")
SENSITIVE_DATA = (
    f"Sensitive data locations & audiences: {NEW_JOTFORM} data/, backups/ and credentials/ hold "
    "minors' personal data; share with nothing outside that project's own tools.")
# A sentence claiming every write (to the sensitive targets) needs Ryan's go: "every write there needs ...".
EVERY_WRITE_ASKS = re.compile(r"\b(every|all|any)\s+writes?\b(\s+\S+){0,6}?\s+(needs?|requires?|asks?)\b",
                              re.IGNORECASE)
STALE_AUTOLAND_CONVENTION = (
    "Review convention: pull requests are opened by the harness from ticket branches t/<n>-*, reviewed by the "
    "harness reviewer agent in a fresh context, and merged only through `harness-pr land` or `harness-pr autoland`, "
    "which refuse a draft, red or pending checks, a conflict and unaddressed review findings. A merge through those "
    "commands is a reviewed merge.")
STALE_ASKING_CONVENTION = (
    "Review convention: pull requests are opened by the harness from ticket branches t/<n>-*, reviewed by the "
    "harness reviewer agent in a fresh context, and merged only through `harness-pr land`, which refuses a draft, "
    "red or pending checks, a conflict and unaddressed review findings. A merge through that command is a reviewed "
    "merge, and a session asks before running it. `harness-pr autoland` is the epic #132 plan, not built yet.")
ENVIRONMENT_OPENINGS = ["Organization:", "Source control:", "CI/CD deploy targets:", "Review convention:",
                        "Key internal services:", "Sensitive data locations & audiences:", "Sensitive remote targets:",
                        "Asks:"]
AUTO_ALLOW_OPENINGS = ["Writing or loading a com.ryaneskuri.harness-* LaunchAgent"]
DEFAULTS = "$defaults"
LISTS = (("permissions", "allow"), ("permissions", "ask"), ("autoMode", "environment"), ("autoMode", "allow"))
# What a merge into a file holding none of the snippet's entries reports: every entry counted, "$defaults" not.
ADDED_ALL = (f"added 11 permissions.allow, {len(ASK_RULES)} permissions.ask, "
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
    test.assertEqual(sorted(perms.get("allow", [])), sorted(nine + ["Bash(harness-gpt architect *)", "Bash(harness-gpt repo-review *)"]))
    test.assertEqual(perms.get("ask"), ASK_RULES)
    test.assertEqual(snippet.get("retired"), RETIRED)
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


SUMMARY_VERBS = ("added", "removed", "replaced", "set", "put")


def counts(line, verb):
    """{"obj.key": N} summed over every `<verb> N obj.key, M obj.key` clause of the settings line (a clause ends at
    `;`, `(` or the next verb), so `removed enabledPlugins[...]` counts nothing."""
    out = {}
    stop = "|".join(SUMMARY_VERBS)
    for clause in re.findall(rf"\b{verb}\b((?:(?!\b(?:{stop})\b)[^;(])*)", line):
        for n, obj, key in re.findall(r"\b(\d+) (permissions|autoMode)\.(\w+)\b", clause):
            out[f"{obj}.{key}"] = out.get(f"{obj}.{key}", 0) + int(n)
    return out


def label(entry):
    """An autoMode.environment entry's label: the text before its first `:` (None without one, as "$defaults")."""
    return entry.split(":", 1)[0] if ":" in entry else None


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

    def install(self, script=INSTALL, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "CLAUDE_", "LAUNCHCTL_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([W["fakebin"], "/usr/bin", "/bin"]),
                   CLAUDE_BIN=os.path.join(W["fakebin"], "claude"), CLAUDE_LOG=self.claude_log,
                   CLAUDE_STATE=self.state, HARNESS_LAUNCHCTL=os.path.join(W["fakebin"], "launchctl"),
                   LAUNCHCTL_LOG=self.launch_log)
        env.update(extra)
        return subprocess.run(["sh", script], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=120)

    def tree(self, snippet):
        """A checkout of the installer whose user/settings.snippet.json is `snippet`: scripts/ and user/ are copies,
        plugins/ and .claude-plugin/ link to the repository's. Returns its scripts/install.sh."""
        root = os.path.join(self.tmp, "tree")
        shutil.copytree(os.path.join(REPO, "scripts"), os.path.join(root, "scripts"))
        shutil.copytree(os.path.join(REPO, "user"), os.path.join(root, "user"))
        write(os.path.join(root, "user", "settings.snippet.json"), json.dumps(snippet, indent=2))
        for name in ("plugins", ".claude-plugin"):
            os.symlink(os.path.join(REPO, name), os.path.join(root, name))
        return os.path.join(root, "scripts", "install.sh")

    def assert_line_counts(self, line, **verbs):
        """Each verb's `N obj.key` counts on the settings line are exactly the given dict (an empty one: none)."""
        for verb, want in verbs.items():
            self.assertEqual(counts(line, verb), want, f"{verb}: {line}")

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
        self.assertNotIn("retired", merged)                                           # the merge's, not a setting
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
                      "added 11 permissions.allow, 1 permissions.ask, 8 autoMode.environment, 1 autoMode.allow",
                      f"(backup settings.json.bak-{today})"):
            self.assertIn(piece, line)
        self.assert_line_counts(line, removed={}, replaced={})                       # nothing retired or stale

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
                      "added 11 permissions.allow, 1 permissions.ask, 8 autoMode.environment, 1 autoMode.allow"):
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

    # Sessions may land but never merge raw (#324, Ryan 2026-11-04; #393): after an install a raw `gh pr merge`
    # prompts, neither `harness-pr land` nor `harness-pr autoland` does (their retired ask rules are gone,
    # `Bash(harness-pr:*)` allows them), and the classifier is told so. The rules are spelled out here, not read
    # from the snippet.

    def test_sessions_may_land_but_never_merge_raw_after_install(self):
        snippet_convention = [e for e in read_json(SNIPPET)["autoMode"]["environment"]
                              if label(e) == "Review convention"]
        self.assertEqual(len(snippet_convention), 1, snippet_convention)
        starts = {      # name: (settings or None, the user's own ask rules, in the order they must keep)
            "fresh HOME, no settings": (None, []),
            "settings with none of the lists": (SETTINGS, []),
            "own ask and autoMode.allow": ({"permissions": {"allow": ["Bash(ls:*)"],
                                                            "ask": ["Bash(git push --force:*)", "Bash(npm publish:*)"]},
                                            "autoMode": {"allow": ["Running make test is allowed."]}},
                                           ["Bash(git push --force:*)", "Bash(npm publish:*)"]),
            "the raw-merge ask rule already there": ({"permissions": {"ask": [RAW_MERGE]},
                                                      "autoMode": {"allow": [DEFAULTS], "environment": []}}, []),
            "the land and autoland rules and the old convention there": (
                {"permissions": {"ask": [AUTOLAND, LAND, "Bash(x)"]},
                 "autoMode": {"environment": [DEFAULTS, STALE_LAND_CONVENTION]}}, ["Bash(x)"]),
            "both rules twice, around own rules": ({"permissions": {"ask": ["Bash(rm -rf build)", LAND, RAW_MERGE,
                                                                            AUTOLAND, "Bash(x)", LAND, AUTOLAND]}},
                                                   ["Bash(rm -rf build)", "Bash(x)"]),
        }
        for name, (start, own) in starts.items():
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
                self.assertEqual(ask.count(RAW_MERGE), 1, ask)
                self.assertEqual([e for e in ask if re.search(r"harness-pr (auto)?land", e)], [], ask)
                self.assertEqual([e for e in ask if e != RAW_MERGE], own, "own ask rules moved")
                self.assertIn("Bash(harness-pr:*)", merged["permissions"]["allow"])  # so landing runs unasked
                for rule in (LAND, AUTOLAND):
                    self.assertNotIn(rule, merged["permissions"]["allow"])
                landing = [e for e in merged["autoMode"]["allow"]
                           if re.search(r"\bland|autoland|harness-pr|\bmerg", e, re.IGNORECASE)]
                self.assertEqual(landing, [], "an autoMode.allow entry mentions landing")
                self.assertEqual(merged["autoMode"]["allow"][0], DEFAULTS)
                self.assertGreaterEqual(len(merged["autoMode"]["allow"]), 2, "the LaunchAgent entry is gone too")
                conventions = [e for e in merged["autoMode"]["environment"] if label(e) == "Review convention"]
                self.assertEqual(conventions, snippet_convention)

    def test_the_snippet_asks_only_for_raw_merge(self):
        snippet = read_json(SNIPPET)
        self.assertEqual(snippet["permissions"]["ask"], [RAW_MERGE])
        self.assertEqual(snippet.get("retired"), {"permissions.ask": [LAND, AUTOLAND]})
        for entry in snippet["permissions"]["allow"]:
            self.assertIsNone(re.search(r"\bland\b|autoland|\bmerg", entry, re.IGNORECASE), entry)
        for entry in snippet["autoMode"]["allow"]:
            self.assertIsNone(re.search(r"\bland|autoland|harness-pr", entry, re.IGNORECASE), entry)
        # Replacing by label needs one label per entry: every prose entry has one, and no two share it.
        labels = [label(e) for e in snippet["autoMode"]["environment"] if e != DEFAULTS]
        self.assertNotIn(None, labels)
        self.assertEqual(len(set(labels)), len(labels), labels)

    def test_the_snippet_environment_asks_only_before_the_irreversible(self):
        # #393: every PR lands through `harness-pr land` unasked and `harness-pr revert` undoes a land; autoland is
        # retired, not planned; writes to the sensitive remote targets run unasked while deleting records there and
        # changing that workflow's secrets still need Ryan's go; the sensitive data entry stays as it was. The
        # wording is the coder's: only these facts are pinned (Asks by its label, in snippet_lists).
        auto = read_json(SNIPPET)["autoMode"]
        env = auto["environment"]
        by_label = {}
        for entry in env:
            by_label.setdefault(label(entry), []).append(entry.replace("`", ""))
        for name in ("Review convention", "Key internal services", "Sensitive remote targets", "Asks"):
            self.assertEqual(len(by_label.get(name, [])), 1, (name, by_label.get(name)))
        review, services, remote = (by_label[n][0] for n in ("Review convention", "Key internal services",
                                                               "Sensitive remote targets"))
        self.assertIn("harness-pr land", review)
        self.assertIn("harness-pr revert", review)
        self.assertRegex(review, r"(?i)\bwithout\b[^.;]*\bask|\bunasked\b")
        for name, entry in (("Review convention", review), ("Key internal services", services)):
            with self.subTest(entry=name):
                self.assertNotRegex(entry, r"(?i)autoland|#132\b|epic 132\b")
        self.assertRegex(remote, r"(?i)\bdelet")
        self.assertRegex(remote, r"(?i)\bsecret")
        for entry in env + auto["allow"]:
            for sentence in self.sentences(entry):
                with self.subTest(sentence=sentence[:70]):
                    self.assertIsNone(EVERY_WRITE_ASKS.search(sentence), "says every write needs Ryan's go")
        self.assertEqual([e for e in env if label(e) == "Sensitive data locations & audiences"], [SENSITIVE_DATA])

    # Neither `harness-pr autoland` nor an autoland LaunchAgent exists yet (epic #132). The text checks below read
    # each sentence (a line split after . ; ! or ?) with backticks removed. A quoted permission rule string, such as
    # "Bash(harness-pr autoland *)" on its own line or in a `retired` list, is a rule, not a description, and is
    # exempt: it is taken out of the line before the line is read.
    RULE = re.compile(r'"[A-Za-z]+\([^"]*\)"')
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
                line = self.RULE.sub("", line)
                if "autoland" not in line.lower():
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
            ("permissions", "ask"): ["Bash(git push --force:*)", LAND, AUTOLAND],
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
                kept = [e for e in own[obj, key] if e not in (LAND, AUTOLAND)]    # the retired rules go
                self.assert_merged(merged[obj][key], kept, wanted[obj, key], sentinel=obj == "autoMode")
        self.assertEqual(merged["permissions"]["deny"], mine["permissions"]["deny"])
        self.assertEqual(merged["autoMode"]["soft_deny"], mine["autoMode"]["soft_deny"])
        self.assertEqual(merged["theme"], "light")
        line = settings_line(p.stdout)
        # The own lists already hold allow[3] and allow[0], env[2] (and the sentinel) and auto_allow[1]: fewer of the
        # first two are added, the ask list loses both retired rules and gains the raw-merge one, and autoMode.allow,
        # already holding its one prose entry, gains nothing and gets no count (only its sentinel moves).
        self.assertIn(f"added {len(allow) - 2} permissions.allow, {len(ask)} permissions.ask, "
                      f"{len(ENVIRONMENT_OPENINGS) - 1} autoMode.environment", line)
        self.assertIn("added 9 permissions.allow, 1 permissions.ask, 7 autoMode.environment", line)
        self.assertNotRegex(line, r"\d+ autoMode\.allow")
        self.assert_line_counts(line, removed={"permissions.ask": 2}, replaced={})
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
        self.assert_line_counts(line, removed={}, replaced={})                     # nor retired, nor stale

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
                                "environment": wanted["autoMode", "environment"] + ["Mine too.", "Team: just me.",
                                                                                     "Team: and a dog."]},
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

        # Needing only the retire and the replace, as the live file does before #393 (the three entries it rewords
        # as they were, no Asks, the land and autoland rules asked, an older stale Review convention later): the
        # first run replaces the three in place, adds Asks and removes both rules; the next change nothing.
        env = wanted["autoMode", "environment"]
        stale = {label(e): e for e in (STALE_LAND_CONVENTION, STALE_SERVICES, STALE_REMOTE)}
        live = [stale.get(label(e), e) for e in env if label(e) != "Asks"]
        stale_env = live + ["Mine too.", STALE_ASKING_CONVENTION]
        stale_ask = [RAW_MERGE, LAND, AUTOLAND, "Bash(git push --force:*)"]
        before = self.reset({"permissions": {"allow": wanted["permissions", "allow"], "ask": stale_ask},
                             "autoMode": {"environment": stale_env, "allow": wanted["autoMode", "allow"]},
                             "theme": "dark", **{k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}})
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = settings_line(p.stdout)
        self.assertNotIn("already up to date", line)
        self.assert_line_counts(line, added={"autoMode.environment": 1}, replaced={"autoMode.environment": 3})
        self.assertEqual(counts(line, "removed").get("permissions.ask"), 2, line)
        merged = read_json(self.settings)
        self.assertEqual(merged["permissions"]["ask"], [RAW_MERGE, "Bash(git push --force:*)"])
        self.assertEqual(merged["autoMode"]["environment"], env[:-1] + ["Mine too.", env[-1]])   # Asks appended
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        with open(os.path.join(os.path.dirname(self.settings), backups[0]), "rb") as fh:
            self.assertEqual(fh.read(), before)
        with open(self.settings, "rb") as fh:
            after_first = fh.read()
        for again in (2, 3):
            with self.subTest(run=again, start="retire and replace"):
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), after_first)
                self.assertEqual(self.backups(), backups)

    def assert_list(self, got, prefix, appended):
        """`got` is `prefix` exactly, then `appended` in any order (each entry as often as it is listed)."""
        self.assertEqual(got[:len(prefix)], prefix, got)
        self.assertEqual(sorted(got[len(prefix):]), sorted(appended), got)

    def test_retired_rule_is_removed_wherever_it_appears(self):
        # Every list but permissions.ask is already as the snippet has it, so the line names the ask list only.
        wanted = snippet_lists(self)
        snippet = read_json(SNIPPET)
        base = {k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}
        own_allow = ["Bash(ls:*)"] + wanted["permissions", "allow"]
        auto = {"environment": wanted["autoMode", "environment"] + ["Mine."], "allow": wanted["autoMode", "allow"]}
        cases = {   # name: (ask before, ask after up to the appended entries, appended, removed count)
            "one copy, alone": ([LAND], [], [RAW_MERGE], 1),
            "two copies of each around the snippet's rule": ([LAND, AUTOLAND, RAW_MERGE, AUTOLAND, LAND], [RAW_MERGE],
                                                             [], 4),
            "one of each between own rules": (["Bash(git push --force:*)", LAND, "Bash(npm publish:*)", AUTOLAND],
                                              ["Bash(git push --force:*)", "Bash(npm publish:*)"], [RAW_MERGE], 2),
            "copies beside rules that only resemble them": (
                [LAND, "Bash(harness-pr land 42)", RAW_MERGE, LAND, "Bash(git reset --hard:*)", AUTOLAND, LAND,
                 "Bash(harness-pr land:*)"],
                ["Bash(harness-pr land 42)", RAW_MERGE, "Bash(git reset --hard:*)", "Bash(harness-pr land:*)"],
                [], 4),
            "none to remove": (["Bash(git push --force:*)", RAW_MERGE],
                               ["Bash(git push --force:*)", RAW_MERGE], [], 0),
        }
        for name, (ask, kept, appended, removed) in cases.items():
            with self.subTest(case=name):
                before = self.reset(dict(base, theme="dark",
                                         permissions={"allow": own_allow, "ask": ask, "deny": [LAND]},
                                         autoMode=auto))
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                self.assert_list(merged["permissions"]["ask"], kept, appended)
                self.assertEqual(merged["permissions"]["allow"], own_allow)
                self.assertEqual(merged["permissions"]["deny"], [LAND])                # deny is never merged
                self.assertEqual(merged["autoMode"], auto)
                line = settings_line(p.stdout)
                if not removed:
                    self.assertIn("~/.claude/settings.json: already up to date", line)
                    with open(self.settings, "rb") as fh:
                        self.assertEqual(fh.read(), before)
                    self.assertEqual(self.backups(), [])
                    continue
                self.assert_line_counts(line, removed={"permissions.ask": removed}, replaced={},
                                        added={"permissions.ask": len(appended)} if appended else {})
                self.assertEqual(len(self.backups()), 1)

    def test_environment_entry_is_replaced_by_its_label(self):
        # Every other list is already as the snippet has it, so the line names autoMode.environment only.
        wanted = snippet_lists(self)
        snippet = read_json(SNIPPET)
        env = wanted["autoMode", "environment"]
        snip = {label(e): e for e in env if label(e)}
        base = dict({k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}, theme="dark",
                    permissions={"allow": wanted["permissions", "allow"], "ask": wanted["permissions", "ask"]})
        org, source, ci, review = (snip["Organization"], snip["Source control"], snip["CI/CD deploy targets"],
                                   snip["Review convention"])
        services, data, remote, asks = (snip["Key internal services"], snip["Sensitive data locations & audiences"],
                                        snip["Sensitive remote targets"], snip["Asks"])
        cases = {   # name: (environment before, after up to the appended entries, appended, replaced count or None)
            "two stale Review convention entries": (
                [DEFAULTS, org, "Team: just me, no staff.", source, STALE_AUTOLAND_CONVENTION, ci,
                 "A line of my own without a label.", STALE_ASKING_CONVENTION,
                 "Note: the Review convention: below is mine.", services, data, remote, "Deploy: by hand on Fridays."],
                [DEFAULTS, org, "Team: just me, no staff.", source, review, ci,
                 "A line of my own without a label.", "Note: the Review convention: below is mine.", services, data,
                 remote, "Deploy: by hand on Fridays."],
                [asks], 1),
            "three stale labels, four missing, the sentinel out of place": (
                ["Organization: someone else, long ago.", "Mine: a line of my own.", DEFAULTS,
                 "Review convention: merged by hand after a look.", source, "Sensitive remote targets: none yet.",
                 "Organization: a second, older line.", STALE_LAND_CONVENTION, "Mine: another line of my own."],
                [DEFAULTS, org, "Mine: a line of my own.", review, source, remote,
                 "Mine: another line of my own."],
                [ci, services, data, asks], 3),
            "the snippet's entry first, a stale one later": (
                env + ["Mine.", STALE_AUTOLAND_CONVENTION, "Source control:github.com/someone-else"],
                env + ["Mine."], [], None),
        }
        for name, (before, kept, appended, replaced) in cases.items():
            with self.subTest(case=name):
                self.reset(dict(base, autoMode={"environment": before, "allow": wanted["autoMode", "allow"]}))
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                self.assert_list(merged["autoMode"]["environment"], kept, appended)
                self.assertEqual(merged["autoMode"]["allow"], wanted["autoMode", "allow"])
                self.assertEqual(merged["permissions"], base["permissions"])
                line = settings_line(p.stdout)
                self.assertNotIn("already up to date", line)
                self.assertEqual(len(self.backups()), 1)
                self.assertEqual({k for k in counts(line, "removed") if k != "autoMode.environment"}, set(), line)
                self.assert_line_counts(line, added={"autoMode.environment": len(appended)} if appended else {})
                if replaced is not None:
                    self.assert_line_counts(line, replaced={"autoMode.environment": replaced})

    def test_the_snippet_and_the_install_files_name_only_the_new_checkout_paths(self):
        # #410: Harness and jotform-glow-sync moved under ~/Projects. No file that tells where a checkout lives
        # (user/, scripts/, docs/guide.md) names the old place, in either spelling.
        env = read_json(SNIPPET)["autoMode"]["environment"]
        org = [e for e in env if label(e) == "Organization"]
        data = [e for e in env if label(e) == "Sensitive data locations & audiences"]
        self.assertEqual(len(org), 1)
        self.assertEqual(len(data), 1)
        self.assertIn(f"loaded in place from {NEW_HARNESS}", org[0])
        self.assertIn(NEW_JOTFORM, data[0])
        files = [os.path.join(REPO, "docs", "guide.md")]
        for folder in ("user", "scripts"):
            for name in sorted(os.listdir(os.path.join(REPO, folder))):
                if os.path.isfile(os.path.join(REPO, folder, name)):
                    files.append(os.path.join(REPO, folder, name))
        for path in files:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(file=os.path.relpath(path, REPO)):
                for old in ("portfolio_projects", "ryaneskuri/jotform-glow-sync", "~/jotform-glow-sync"):
                    self.assertNotIn(old, text)

    def test_entries_naming_the_old_checkout_paths_are_replaced_and_a_second_install_changes_nothing(self):
        wanted = snippet_lists(self)
        snippet = read_json(SNIPPET)
        env = wanted["autoMode", "environment"]
        snip = {label(e): e for e in env if label(e)}
        org, data = snip["Organization"], snip["Sensitive data locations & audiences"]
        self.assertNotEqual(org, STALE_ORGANIZATION)
        self.assertNotEqual(data, STALE_SENSITIVE_DATA)
        base = dict({k: snippet[k] for k in ("model", "effortLevel", "modelSettings")}, theme="dark",
                    permissions={"allow": wanted["permissions", "allow"], "ask": wanted["permissions", "ask"]})
        moved = ("Organization", "Sensitive data locations & audiences")
        rest = [e for e in env if label(e) not in moved][1:]   # the others, without "$defaults"

        def swapped(old):   # the snippet's list with the given labels' entries as the stale text
            return [old.get(label(e), e) for e in env]

        cases = {   # name: (environment before, environment after, replaced count or None)
            "both old, as the file held them before the move": (
                swapped({"Organization": STALE_ORGANIZATION,
                         "Sensitive data locations & audiences": STALE_SENSITIVE_DATA}), env, 2),
            "both old, beside lines of the file's own": (
                [DEFAULTS, "Mine first.", STALE_ORGANIZATION, "Mine: second.", STALE_SENSITIVE_DATA] + rest
                + ["Mine last."],
                [DEFAULTS, "Mine first.", org, "Mine: second.", data] + rest + ["Mine last."], 2),
            "only the Organization entry old": (swapped({"Organization": STALE_ORGANIZATION}), env, 1),
            "only the Sensitive data entry old": (
                swapped({"Sensitive data locations & audiences": STALE_SENSITIVE_DATA}), env, 1),
            "old entries added beside the new ones": (
                env + [STALE_ORGANIZATION, "Mine.", STALE_SENSITIVE_DATA], env + ["Mine."], None),
        }
        for name, (before, after, replaced) in cases.items():
            with self.subTest(case=name):
                self.reset(dict(base, autoMode={"environment": before, "allow": wanted["autoMode", "allow"]}))
                p = self.install()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                got = merged["autoMode"]["environment"]
                self.assertEqual(got, after)
                self.assertEqual([e for e in got if label(e) == "Organization"], [org])
                self.assertEqual([e for e in got if label(e) == "Sensitive data locations & audiences"], [data])
                with open(self.settings, encoding="utf-8") as fh:
                    raw = fh.read()
                for old in (OLD_HARNESS, OLD_JOTFORM):
                    self.assertNotIn(old, raw)
                self.assertEqual(merged["autoMode"]["allow"], wanted["autoMode", "allow"])
                self.assertEqual(merged["permissions"], base["permissions"])
                line = settings_line(p.stdout)
                self.assertNotIn("already up to date", line)
                if replaced is not None:
                    self.assert_line_counts(line, replaced={"autoMode.environment": replaced}, added={})
                backups = self.backups()
                self.assertEqual(len(backups), 1)
                with open(self.settings, "rb") as fh:
                    after_first = fh.read()
                for again in (2, 3):
                    p = self.install()
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
                    with open(self.settings, "rb") as fh:
                        self.assertEqual(fh.read(), after_first, f"install {again} changed the file")
                    self.assertEqual(self.backups(), backups)

    def test_retire_and_replace_follow_the_snippet_list_by_list(self):
        # A snippet of the test's own: a retired string goes from the list the snippet retires it under, every
        # copy, and stays in every other list; labels are replaced in autoMode.environment only.
        script = self.tree({
            "permissions": {"allow": ["Bash(alpha:*)", "Bash(beta:*)"], "ask": ["Bash(gamma *)"]},
            "autoMode": {"environment": [DEFAULTS, "Alpha: the new alpha.", "Beta: the new beta.",
                                         "Gamma: the new gamma.", "A line with no label."],
                         "allow": [DEFAULTS, "Delta: the new delta is allowed."]},
            "retired": {"permissions.allow": ["Bash(old-allow:*)"],
                        "permissions.ask": ["Bash(old ask *)", "Bash(older ask *)"],
                        "autoMode.environment": ["A retired context line."],
                        "autoMode.allow": ["A retired allow line."]},
        })
        deny, soft_deny = ["Bash(old ask *)", "Bash(old-allow:*)"], [DEFAULTS, "A retired allow line."]
        first = {
            "permissions": {"allow": ["Bash(old-allow:*)", "Bash(ls:*)", "Bash(alpha:status)", "Bash(old-allow:*)",
                                      "Bash(old ask *)"],
                            "ask": ["Bash(older ask *)", "Bash(mine *)", "Bash(old ask *)", "Bash(gamma *)",
                                    "Bash(old ask *)"],
                            "deny": deny},
            "autoMode": {"environment": ["Gamma: a stale gamma.", "A retired context line.", DEFAULTS,
                                         "Mine: keep me.", "Alpha: a stale alpha.", "A retired allow line.",
                                         "A retired context line."],
                         "allow": ["A retired allow line.", "Delta: mine, kept.", DEFAULTS, "A retired context line."],
                         "soft_deny": soft_deny},
            "theme": "dark",
        }
        second = {"permissions": {"ask": ["Bash(old ask *)"]},
                  "autoMode": {"environment": ["Beta: a stale beta.", "Beta: an older beta.", "Alpha: the new alpha.",
                                               "Beta: the oldest beta."]}}
        expect = {  # name: (settings, {list: (after up to the appended entries, appended)}, removed, replaced, added)
            "first": (first, {
                ("permissions", "allow"): (["Bash(ls:*)", "Bash(alpha:status)", "Bash(old ask *)"],
                                           ["Bash(alpha:*)", "Bash(beta:*)"]),
                ("permissions", "ask"): (["Bash(mine *)", "Bash(gamma *)"], []),
                ("autoMode", "environment"): ([DEFAULTS, "Gamma: the new gamma.", "Mine: keep me.",
                                               "Alpha: the new alpha.", "A retired allow line."],
                                              ["Beta: the new beta.", "A line with no label."]),
                ("autoMode", "allow"): ([DEFAULTS, "Delta: mine, kept.", "A retired context line."],
                                        ["Delta: the new delta is allowed."]),
            }, {"permissions.allow": 2, "permissions.ask": 3, "autoMode.environment": 2, "autoMode.allow": 1},
                {"autoMode.environment": 2},
                {"permissions.allow": 2, "autoMode.environment": 2, "autoMode.allow": 1}),
            "second": (second, {
                ("permissions", "allow"): ([], ["Bash(alpha:*)", "Bash(beta:*)"]),
                ("permissions", "ask"): ([], ["Bash(gamma *)"]),
                ("autoMode", "environment"): ([DEFAULTS, "Beta: the new beta.", "Alpha: the new alpha."],
                                              ["Gamma: the new gamma.", "A line with no label."]),
                ("autoMode", "allow"): ([DEFAULTS], ["Delta: the new delta is allowed."]),
            }, {"permissions.ask": 1}, {"autoMode.environment": 1},
                {"permissions.allow": 2, "permissions.ask": 1, "autoMode.environment": 2, "autoMode.allow": 1}),
        }
        for name, (start, lists, removed, replaced, added) in expect.items():
            with self.subTest(start=name):
                before = self.reset(start)
                p = self.install(script)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                merged = read_json(self.settings)
                for (obj, key), (kept, appended) in lists.items():
                    with self.subTest(list=f"{obj}.{key}"):
                        self.assert_list(merged[obj][key], kept, appended)
                line = settings_line(p.stdout)
                self.assert_line_counts(line, replaced=replaced, added=added)
                got = counts(line, "removed")
                if name == "second":          # later entries of a replaced label may be counted as removed or not
                    got.pop("autoMode.environment", None)
                self.assertEqual(got, removed, line)
                backups = self.backups()
                self.assertEqual(len(backups), 1)
                with open(os.path.join(os.path.dirname(self.settings), backups[0]), "rb") as fh:
                    self.assertEqual(fh.read(), before)
                if name == "first":
                    self.assertEqual(merged["permissions"]["deny"], deny)
                    self.assertEqual(merged["autoMode"]["soft_deny"], soft_deny)
                    self.assertEqual(merged["theme"], "dark")
                    with open(self.settings, "rb") as fh:
                        after_first = fh.read()
                    p = self.install(script)                                       # again: nothing changes
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
                    with open(self.settings, "rb") as fh:
                        self.assertEqual(fh.read(), after_first)
                    self.assertEqual(self.backups(), backups)

        # Retired strings only where they are not retired, labels of the user's own, the snippet's entries in
        # place: nothing to do, so nothing is written.
        with self.subTest(start="up to date"):
            before = self.reset({
                "permissions": {"allow": ["Bash(old ask *)", "Bash(beta:*)", "Bash(alpha:*)", "Bash(alpha:status)"],
                                "ask": ["Bash(old-allow:*)", "Bash(gamma *)"], "deny": deny},
                "autoMode": {"environment": [DEFAULTS, "Gamma: the new gamma.", "A retired allow line.",
                                             "Mine: one.", "Alpha: the new alpha.", "Mine: two.", "Beta: the new beta.",
                                             "A line with no label."],
                             "allow": [DEFAULTS, "A retired context line.", "Delta: mine, kept.",
                                       "Delta: the new delta is allowed."],
                             "soft_deny": soft_deny}})
            p = self.install(script)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("~/.claude/settings.json: already up to date", p.stdout)
            with open(self.settings, "rb") as fh:
                self.assertEqual(fh.read(), before)
            self.assertEqual(self.backups(), [])

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
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                self.skipTest("root reads a mode-0 file")
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
