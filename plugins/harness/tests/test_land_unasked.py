"""Tests for ticket #385 (epic #383): land_ask.py is retired, so no hook asks before a land.

A pull request lands on its own once it is green and current with main; nothing prompts Ryan for it. The contract
these tests read from the ticket:
  - hooks.json wires no land_ask.py, and every other hook keeps its event, matcher, place, command and timeout;
  - no hook hooks.json wires for Bash (the list read from hooks.json itself) answers ask or deny for
    `harness-pr land 57`, `cd <worktree> && harness-pr land 57` or `harness-pr land 57 --accept-findings`, whatever
    risk label the ticket PR 57 closes carries (risk:high, risk:low, none), from a ticket worktree or from the main
    checkout; each hook exits 0 with nothing on stderr (exit 2 blocks the call, and a hook failing open is silent
    for the wrong reason);
  - land_ask.py stays, for sessions that started before the change, as a silent stub: on any stdin (empty, a land
    event, text that is not JSON) it prints nothing, exits 0 and runs no gh.

Hooks run as Claude Code runs them: `python3 <hook>`, the PreToolUse event on stdin, a temporary HOME and temporary
git repos (guards_support.GuardTestCase). gh is fakegh.py behind a wrapper written once per module, given as
HARNESS_GH and first on PATH as `gh`, so every gh call any hook makes is logged to FAKEGH_LOG. Each hook's first row
is its own process; its further rows run in this process (gitrepo_support.run_in_process), held to the same contract.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_land_unasked.py -v
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

import guards_support
from guards_support import SYSTEM_PATH, GuardTestCase

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOKS = PLUGIN / "hooks"
HOOKS_JSON = HOOKS / "hooks.json"
STUB = HOOKS / "land_ask.py"
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"

PR = 57
TICKET = 40
TICKET_BRANCH = f"t/{TICKET}-fix-login"
# The labels of the ticket PR 57 closes, by the risk it carries.
RISKS = {
    "risk:high": ("task", "risk:high", "size:M"),
    "risk:low": ("task", "risk:low", "size:M"),
    "no risk label": ("task", "size:M"),
}
# The guards that must stay wired for Bash once land_ask is gone.
KEPT_BASH_GUARDS = ("readonly_bash.py", "branch_guard.py", "tests_first.py", "backup_gate.py")


def wired(name: str, timeout: int) -> tuple:
    """A hook entry as hooks.json writes it: (type, command, args, timeout)."""
    return ("command", "python3", [f"{PLUGIN_ROOT}/hooks/{name}"], timeout)


# Every hook's wiring before #385, land_ask.py's line taken out, and readonly_allow.py last for Bash since #392:
# event -> [(matcher, [hook, ...]), ...] in order.
EXPECTED_WIRING = {
    "Notification": [
        ("permission_prompt|idle_prompt|agent_needs_input|agent_completed", [wired("notify.py", 30)]),
    ],
    "SessionStart": [
        ("startup|resume|clear|compact|fork", [wired("session_brief.py", 20)]),
    ],
    "PreToolUse": [
        ("Bash", [wired("readonly_bash.py", 10), wired("branch_guard.py", 10), wired("tests_first.py", 10),
                  wired("backup_gate.py", 30), wired("readonly_allow.py", 10)]),
        ("Edit|Write|NotebookEdit", [wired("branch_guard.py", 10), wired("tests_first.py", 10)]),
    ],
    "PostToolUse": [
        ("Edit|Write|NotebookEdit|Bash", [wired("after_edit.py", 30)]),
    ],
    "Stop": [
        (None, [wired("verify_on_stop.py", 420)]),
    ],
}

# The fake gh: fakegh.py run under this interpreter, so FAKEGH_SCENARIO and FAKEGH_LOG work as they always do.
WRAPPER = """#!@PYTHON@ -IS
import runpy
import sys

FAKEGH = @FAKEGH@
sys.argv[0] = FAKEGH
runpy.run_path(FAKEGH, run_name="__main__")
"""

_GH: dict = {}


def setUpModule() -> None:
    """guards_support's shared folder, and the fake gh once for every test: a new executable costs time on macOS."""
    guards_support.setUpModule()
    gh = guards_support._SHARED["base"] / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        WRAPPER.replace("@PYTHON@", sys.executable).replace("@FAKEGH@", repr(str(TESTS / "fakegh.py"))),
        encoding="utf-8",
    )
    gh.chmod(0o755)
    _GH["gh"] = gh


def tearDownModule() -> None:
    guards_support.tearDownModule()


def hooks_json() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def wiring(data: dict) -> dict:
    """event -> [(matcher, [(type, command, args, timeout), ...]), ...] as `data` (hooks.json) wires them."""
    return {
        event: [
            (entry.get("matcher"),
             [(h.get("type"), h.get("command"), list(h.get("args", [])), h.get("timeout")) for h in entry.get("hooks", [])])
            for entry in entries
        ]
        for event, entries in data["hooks"].items()
    }


def bash_hooks() -> list:
    """The hook scripts hooks.json runs before a Bash call, in order, as paths in this checkout."""
    found = []
    for entry in hooks_json()["hooks"].get("PreToolUse", []):
        matcher = entry.get("matcher")
        if matcher not in (None, "", "*") and not re.fullmatch(matcher, "Bash"):
            continue
        for hook in entry.get("hooks", []):
            words = [str(hook.get("command", ""))] + [str(a) for a in hook.get("args", [])]
            scripts = [w for w in words if w.endswith(".py")]
            if scripts:
                found.append(Path(scripts[0].replace(PLUGIN_ROOT, str(PLUGIN))))
    return found


def pr_answer(pr: int, ticket: int, head: str) -> dict:
    """gh pr view `pr` closing `ticket`, in gh's own shape."""
    ref = {
        "id": f"I_kwDO{ticket:04d}",
        "number": ticket,
        "url": f"https://github.com/o/proj/issues/{ticket}",
        "repository": {"id": "R_kgDO1", "name": "proj", "owner": {"id": "U_1", "login": "o"}},
    }
    return {"contains": ["pr", "view", str(pr)],
            "stdout_json": {"closingIssuesReferences": [ref], "headRefName": head}}


def issue_answer(ticket: int, labels) -> dict:
    """gh issue view `ticket` answering its labels, in gh's own shape."""
    return {
        "contains": ["issue", "view", str(ticket)],
        "stdout_json": {"labels": [{"id": f"LA_{ticket}_{i}", "name": name, "description": "", "color": "ededed"}
                                   for i, name in enumerate(labels)]},
    }


class LandUnaskedTests(GuardTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo("proj")
        self.git(self.repo, "remote", "add", "origin", "https://github.com/o/proj.git")
        # A ticket worktree where harness-work puts it, on the ticket's branch.
        self.ticket_worktree = self.repo / ".claude" / "worktrees" / f"t-{TICKET}-fix-login"
        self.git(self.repo, "worktree", "add", "-q", "-b", TICKET_BRANCH, str(self.ticket_worktree))
        self.gh_log = self.tmp / "gh.log"
        self.scenarios = {}
        for risk, labels in RISKS.items():
            path = self.tmp / f"scenario-{risk.replace(':', '-').replace(' ', '-')}.json"
            path.write_text(json.dumps({"calls": [pr_answer(PR, TICKET, TICKET_BRANCH), issue_answer(TICKET, labels)]}),
                            encoding="utf-8")
            self.scenarios[risk] = path

    def gh_env(self, risk: str, project: Path) -> dict:
        """The hook's environment beyond the test's: the fake gh answering for `risk`, on HARNESS_GH and on PATH,
        and the plugin and project directories Claude Code gives a plugin hook."""
        gh = _GH["gh"]
        return {
            "HARNESS_GH": str(gh),
            "PATH": str(gh.parent) + os.pathsep + SYSTEM_PATH,
            "FAKEGH_SCENARIO": str(self.scenarios[risk]),
            "FAKEGH_LOG": str(self.gh_log),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
            "CLAUDE_PROJECT_DIR": str(project),
        }

    def gh_calls(self) -> list:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def run_script(self, script: Path, stdin: str, env: dict, process: bool):
        """`python3 <script>` given `stdin`: its own process when `process`, else run in this process."""
        if not process:
            return self.run_in_process(script, stdin, env=env)
        return subprocess.run([sys.executable, str(script)], input=stdin, capture_output=True, text=True,
                              env=dict(self.env, **env), cwd=str(self.tmp), timeout=60)

    def assert_no_ask_or_deny(self, r, label: str) -> None:
        """The hook let the call through on its own terms: exit 0 (2 blocks it), nothing on stderr (a hook failing
        open is silent for the wrong reason), and stdout empty or a JSON object that neither asks, denies nor blocks."""
        self.assertEqual(r.returncode, 0, f"{label}: exit {r.returncode} (2 blocks the call); stderr {r.stderr!r}")
        self.assertEqual(r.stderr, "", f"{label}: the hook wrote to stderr, so its silence proves nothing")
        out = r.stdout.strip()
        if not out:
            return
        try:
            data = json.loads(out)
        except ValueError:
            self.fail(f"{label}: stdout is neither empty nor JSON: {out!r}")
        self.assertIsInstance(data, dict, f"{label}: {out}")
        specific = data.get("hookSpecificOutput")
        decision = specific.get("permissionDecision") if isinstance(specific, dict) else None
        self.assertNotIn(decision, ("ask", "deny"), f"{label} answers {decision}: {out}")
        self.assertNotEqual(data.get("decision"), "block", f"{label} blocks the call: {out}")

    # ---- no Bash hook stands between a session and a land

    def test_no_bash_hook_asks_before_a_land(self):
        hooks = bash_hooks()
        names = [h.name for h in hooks]
        for name in KEPT_BASH_GUARDS:
            self.assertIn(name, names, f"hooks.json no longer runs {name} before Bash: {names}")
        worktree = shlex.quote(str(self.ticket_worktree))
        commands = (
            f"harness-pr land {PR}",
            f"cd {worktree} && harness-pr land {PR}",
            f"harness-pr land {PR} --accept-findings",
        )
        places = {"ticket worktree": self.ticket_worktree, "main checkout": self.repo}
        for hook in hooks:
            first = True  # each hook's first row is its own process, the rest run in this process
            for risk in RISKS:
                for where, cwd in places.items():
                    for command in commands:
                        with self.subTest(hook=hook.name, risk=risk, where=where, command=command):
                            stdin = json.dumps(self.payload("Bash", {"command": command}, cwd))
                            r = self.run_script(hook, stdin, self.gh_env(risk, cwd), process=first)
                            first = False
                            self.assert_no_ask_or_deny(r, f"{hook.name} on {command!r} in the {where} ({risk})")

    # ---- hooks.json

    def test_hooks_json_wires_no_land_ask(self):
        data = hooks_json()
        land_ask = [
            (event, entry.get("matcher"))
            for event, entries in data["hooks"].items()
            for entry in entries
            for hook in entry.get("hooks", [])
            if any("land_ask" in str(w) for w in [hook.get("command", "")] + list(hook.get("args", [])))
        ]
        self.assertEqual(land_ask, [], "hooks.json still runs land_ask.py")
        # Every other hook keeps its event, matcher, place in its list, command and timeout.
        self.assertEqual(wiring(data), EXPECTED_WIRING)

    # ---- the stub left for sessions that started before the change

    def test_land_ask_stub_is_silent(self):
        land = self.payload("Bash", {"command": f"harness-pr land {PR}"}, self.repo)
        stdins = {
            "empty": "",
            "a land event": json.dumps(land),
            "not JSON": f"harness-pr land {PR} {{not json",
        }
        for kind, stdin in stdins.items():
            with self.subTest(stdin=kind):
                if self.gh_log.exists():
                    self.gh_log.unlink()
                # The PR closes a risk:high ticket: the retired hook would look it up and ask.
                r = self.run_script(STUB, stdin, self.gh_env("risk:high", self.repo), process=True)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""),
                                 f"the stub must print nothing and exit 0 on {kind} stdin")
                self.assertEqual(self.gh_calls(), [], f"the stub ran gh on {kind} stdin")


if __name__ == "__main__":
    unittest.main()
