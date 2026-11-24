"""Tests for #72, ops tickets on the board: `harness-board task create --type ops`, `show` of an ops ticket's
sections, the ops body template, and `harness-board retype N --type T --reason TEXT` (an ops ticket that turns out to
need code goes back to a normal ticket).

    python3 -m unittest discover -s plugins/harness/tests -p test_board_ops.py

An ops ticket is a record update with no code change (Salesforce, Jotform, a project script). It carries the `ops`
type label and always `needs-local` (it needs this Mac's connectors and credentials), and its body has the sections
Request, Change, Source of truth, Apply, Verify, Rollback and Context.

Self-contained (no sibling import but gitrepo_support): each test has its own temporary HOME and git repository with an
`origin` remote (never contacted). `gh` is the scripted fake (fakegh.py) behind a wrapper written once for the module
that also records the text of every `--body-file PATH` it is given, so bodies are checked whichever way the tool hands
them to gh. Each test's first run of the tool is a real process; its further rows go through
gitrepo_support.run_in_process.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True  # loading the lib and the tool must not leave __pycache__ in the checkout

from gitrepo_support import GIT, run_in_process  # noqa: E402
import fakegh  # noqa: E402  (#447: the gh issue fixtures move to its REST layer)

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-board")
TEMPLATE = os.path.join(os.path.dirname(HERE), "templates", "ticket_ops.md")
FAKEGH = os.path.join(HERE, "fakegh.py")
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
OWNER = {"match": ["api", "user"], "stdout": "rjeskuri\n"}
DROPPED = ("CLAUDE_PROJECT_DIR", "HARNESS_GH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "HARNESS_NOTIFY_APP",
           "HARNESS_NOTIFY_SOUND", "HARNESS_SESSION_PROJECT", "HARNESS_CODE_TIER",
           "HARNESS_CODE_EFFORT", "HARNESS_REVIEW_TIER", "HARNESS_WAVE_REVIEW")
OPS_KEYS = ("request", "change", "source_of_truth", "apply", "verify", "rollback", "context")

# Two ops bodies with different text in every section; the second has Rollback before Verify and a heading in
# another case, so only parsing by heading gets both right.
OPS_BODY = """## Request
Fill in the blank region on the imported contacts.

## Change
- Contact.Region__c on 14 records: blank -> "North"

## Source of truth
SELECT Id FROM Contact WHERE Region__c = null AND LeadSource = 'Web form'

## Apply
The Salesforce connector, one update per record.

## Verify
Run the query again: 0 rows. Read back Region__c on the 14 ids.

## Rollback
Set Region__c back to blank on the ids in before.json.

## Context
- The form mapping sets Region__c since last week.
"""
OPS_BODY_2 = """## Request
Close the duplicate form submissions.

## Change
- Submission status on 3 submissions: ACTIVE -> DELETED

## SOURCE OF TRUTH
The three ids listed in the comment of 2026-11-01.

## Apply
The Jotform API, one call per submission.

## Rollback
Set the status back to ACTIVE on the ids in before.json.

## Verify
Read each submission back: status DELETED.

## Context
- Duplicates came from a double click on submit.
"""
EXPECTED = {
    OPS_BODY: {"request": "Fill in the blank region on the imported contacts.",
               "change": '- Contact.Region__c on 14 records: blank -> "North"',
               "source_of_truth": "SELECT Id FROM Contact WHERE Region__c = null AND LeadSource = 'Web form'",
               "apply": "The Salesforce connector, one update per record.",
               "verify": "Run the query again: 0 rows. Read back Region__c on the 14 ids.",
               "rollback": "Set Region__c back to blank on the ids in before.json.",
               "context": "- The form mapping sets Region__c since last week."},
    OPS_BODY_2: {"request": "Close the duplicate form submissions.",
                 "change": "- Submission status on 3 submissions: ACTIVE -> DELETED",
                 "source_of_truth": "The three ids listed in the comment of 2026-11-01.",
                 "apply": "The Jotform API, one call per submission.",
                 "verify": "Read each submission back: status DELETED.",
                 "rollback": "Set the status back to ACTIVE on the ids in before.json.",
                 "context": "- Duplicates came from a double click on submit."},
}
OPS_HEADINGS = ("## Request", "## Change", "## Source of truth", "## Apply", "## Verify", "## Rollback", "## Context")

# gh: records the text of a `--body-file PATH` (not `-`: fakegh logs stdin itself) to $BODY_LOG, then runs fakegh.
GH_WRAPPER = r'''
import json, os, sys
sys.path.insert(0, @HERE@)
argv = sys.argv[1:]
for i, tok in enumerate(argv):
    path = None
    if tok == "--body-file" and i + 1 < len(argv):
        path = argv[i + 1]
    elif tok.startswith("--body-file="):
        path = tok[len("--body-file="):]
    if path and path != "-" and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(os.environ["BODY_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "text": text}) + "\n")
import fakegh
sys.exit(fakegh.main())
'''

BINS = {}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-ops-bin-"))
    py = sys.executable
    fakebin = os.path.join(root, "fakebin")
    wrapper = write(os.path.join(root, "ghwrap.py"), GH_WRAPPER.replace("@HERE@", repr(HERE)))
    if " " in py:
        executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{py}" "{wrapper}" "$@"\n')
    else:
        executable(os.path.join(fakebin, "gh"), f"#!{py} -SB\n" + GH_WRAPPER.replace("@HERE@", repr(HERE)))
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), "#!/bin/sh\nexit 0\n")
    BINS.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def issue(number, labels, body=OPS_BODY, title="Fill in the blank regions"):
    """A `gh issue view --json ...` answer carrying every field a caller may ask for."""
    return {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}", "body": body,
            "labels": [{"name": n} for n in labels], "state": "OPEN", "parent": None, "blockedBy": [],
            "comments": []}


def option_values(argv, *names):
    """Every value of the options `names` in argv (`--opt v` or `--opt=v`), comma lists split."""
    out = []
    for i, tok in enumerate(argv):
        value = None
        if tok in names and i + 1 < len(argv):
            value = argv[i + 1]
        else:
            for name in names:
                if tok.startswith(name + "="):
                    value = tok[len(name) + 1:]
        if value is not None:
            out += [v.strip() for v in value.split(",") if v.strip()]
    return out


class OpsBoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-ops-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.log = os.path.join(self.tmp, "gh.log")
        self.body_log = os.path.join(self.tmp, "bodies.log")
        self.ran_a_process = False
        subprocess.run([GIT, "-C", self.repo, "init", "-q"], env=self.env(), capture_output=True, check=True)
        subprocess.run([GIT, "-C", self.repo, "remote", "add", "origin", "https://github.com/o/r.git"],
                       env=self.env(), capture_output=True, check=True)

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in DROPPED}
        paths = [BINS["fakebin"]] + ([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(paths), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.log,
                   BODY_LOG=self.body_log, NOTIFY_LOG=os.path.join(self.tmp, "notify.log"))
        return env

    def scenario(self, calls, default=None):
        data = {"calls": fakegh.as_rest(calls)}     # #447: issue reads and writes through gh api REST
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.log, self.body_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def board(self, *args):
        if self.ran_a_process:
            return run_in_process(TOOL, args, cwd=self.repo, env=self.env())
        self.ran_a_process = True
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=self.repo, env=self.env(),
                              capture_output=True, text=True, timeout=60)

    def ok(self, *args):
        p = self.board(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def entries(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def calls(self):
        """Each gh call's argv, a REST issue write as the gh command it stands for (fakegh.gh_view)."""
        return fakegh.gh_view(self.entries(), self.scenario_path)

    def body_of(self, entry):
        """The body a logged gh call carried: --body/-b, --body-file - (stdin) or --body-file PATH (recorded)."""
        argv = entry["argv"]
        for name in ("--body", "-b"):
            values = [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == name]
            values += [tok[len(name) + 1:] for tok in argv if tok.startswith(name + "=")]
            if values:
                return values[0]
        paths = option_values(argv, "--body-file")
        if paths == ["-"]:
            return entry["stdin"]
        if paths and os.path.exists(self.body_log):
            with open(self.body_log, encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec["argv"] == argv:
                        return rec["text"]
        self.fail(f"no body found for gh {argv}")

    def comments_on(self, n):
        return [argv[4] for argv in self.calls() if argv[:4] == ["issue", "comment", str(n), "--body"]]

    def label_changes(self, n):
        """(added, removed) label names over every `gh issue edit N` the tool made."""
        added, removed = [], []
        for argv in self.calls():
            if argv[:3] == ["issue", "edit", str(n)]:
                added += option_values(argv, "--add-label")
                removed += option_values(argv, "--remove-label")
        return added, removed


# ======================================================================= task create --type ops
class CreateOps(OpsBoardTest):
    def test_task_create_type_ops_labels_ops_and_needs_local_and_keeps_the_body(self):
        body = write(os.path.join(self.tmp, "ops.md"), OPS_BODY)
        rows = [
            (("--size", "M", "--risk", "low"), {"ops", "status:ready", "size:M", "risk:low", "needs-local"}),
            (("--size", "S", "--risk", "high", "--needs-local", "--priority"),
             {"ops", "status:ready", "size:S", "risk:high", "needs-local", "priority:high"}),
            (("--proposed",), {"ops", "status:proposed", "needs-local"}),
        ]
        for i, (extra, expected) in enumerate(rows):
            n = 50 + i
            with self.subTest(extra=extra):
                self.scenario([REPO_VIEW, OWNER,
                               {"match": ["issue", "create"], "stdout": f"https://github.com/o/r/issues/{n}\n"}],
                              default={"stdout": ""})
                p = self.ok("task", "create", "--title", "Fill in the blank regions", "--body-file", body,
                            "--type", "ops", *extra)
                self.assertEqual(p.stdout.strip(), f"#{n} https://github.com/o/r/issues/{n}")
                creates = [e for e in self.entries() if e["argv"][:2] == ["issue", "create"]]
                self.assertEqual(len(creates), 1, self.calls())
                labels = option_values(creates[0]["argv"], "--label")
                self.assertEqual(set(labels), expected)
                self.assertEqual(labels.count("needs-local"), 1, labels)   # never twice, asked for or not
                self.assertEqual(self.body_of(creates[0]), OPS_BODY)       # the body file as given


# ======================================================================= show
class ShowOps(OpsBoardTest):
    LABELS = ("ops", "status:in-progress", "size:S", "risk:low", "needs-local")

    def test_show_parses_the_ops_sections_and_reads_type_ops(self):
        for n, body in ((40, OPS_BODY), (41, OPS_BODY_2)):
            with self.subTest(ticket=n):
                self.scenario([{"match": ["issue", "view", str(n)], "stdout_json": issue(n, self.LABELS, body)},
                               OWNER])
                record = json.loads(self.ok("show", str(n)).stdout)
                self.assertEqual(record["type"], "ops")
                self.assertIn("needs-local", record["flags"])
                self.assertEqual({k: record["sections"].get(k) for k in OPS_KEYS}, EXPECTED[body])

    def test_show_markdown_prints_the_ops_sections(self):
        for n, body in ((40, OPS_BODY), (41, OPS_BODY_2)):
            with self.subTest(ticket=n):
                self.scenario([{"match": ["issue", "view", str(n)], "stdout_json": issue(n, self.LABELS, body)},
                               OWNER])
                lines = self.ok("show", str(n), "--markdown").stdout.splitlines()
                for heading in OPS_HEADINGS:
                    self.assertIn(heading, lines)
                for key, text in EXPECTED[body].items():
                    self.assertIn(text.splitlines()[0], lines, key)
                # each section's text sits under its own heading
                for heading, key in zip(OPS_HEADINGS, OPS_KEYS):
                    at = lines.index(heading)
                    self.assertIn(EXPECTED[body][key].splitlines()[0], lines[at + 1:at + 4], heading)

    def test_the_ops_template_has_every_section_show_parses(self):
        self.assertTrue(os.path.isfile(TEMPLATE), TEMPLATE)
        with open(TEMPLATE, encoding="utf-8") as fh:
            template = fh.read()
        self.scenario([{"match": ["issue", "view", "42"], "stdout_json": issue(42, self.LABELS, template)}, OWNER])
        sections = json.loads(self.ok("show", "42").stdout)["sections"]
        for key in OPS_KEYS:
            self.assertTrue((sections.get(key) or "").strip(), f"the template's {key} section is empty or missing")


# ======================================================================= retype
class Retype(OpsBoardTest):
    def retype_scenario(self, n, labels):
        self.scenario([REPO_VIEW, OWNER,
                       {"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels)},
                       {"match": ["issue", "comment", str(n)],
                        "stdout": f"https://github.com/o/r/issues/{n}#issuecomment-9\n"}],
                      default={"stdout": ""})

    def test_retype_swaps_ops_for_the_task_type_keeps_the_rest_and_comments_the_reason(self):
        rows = [
            (40, "task", "It needs a new sync script, not a record update.",
             ("ops", "status:in-progress", "size:M", "risk:low", "needs-local")),
            (41, "bug", "The export script crashes on blank regions; fix the code first.",
             ("ops", "status:ready", "size:S", "risk:high", "needs-local", "priority:high")),
            (43, "chore", "The mapping file must change in the repository.",
             ("ops", "status:in-progress", "size:S", "risk:low", "needs-local")),
        ]
        for n, kind, reason, labels in rows:
            with self.subTest(ticket=n, type=kind):
                self.retype_scenario(n, labels)
                self.ok("retype", str(n), "--type", kind, "--reason", reason)
                added, removed = self.label_changes(n)
                self.assertIn(kind, added)
                self.assertIn("ops", removed)
                self.assertNotIn("ops", added)
                for kept in labels[1:]:                    # status, size, risk, needs-local, priority stay
                    self.assertNotIn(kept, removed)
                comments = self.comments_on(n)
                self.assertTrue(any(reason in c for c in comments), comments)
                others = [argv for argv in self.calls()
                          if argv[:2] in (["issue", "edit"], ["issue", "comment"]) and argv[2] != str(n)]
                self.assertEqual(others, [])

    def test_retype_without_a_reason_changes_nothing(self):
        self.retype_scenario(40, ("ops", "status:in-progress", "size:M", "risk:low", "needs-local"))
        p = self.board("retype", "40", "--type", "task")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("--reason", p.stderr)                # refused for the missing reason, not an unknown command
        self.assertFalse([argv for argv in self.calls() if argv[:2] in (["issue", "edit"], ["issue", "comment"])])


if __name__ == "__main__":
    unittest.main()
