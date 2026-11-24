"""Tests for #388: `harness-board park N --reason TEXT [--evidence-file F] [--follow-up M[,M...]] [--json]`.

    python3 -m unittest discover -s plugins/harness/tests -p test_board_park.py

Park marks a stuck ticket status:blocked, files one status:proposed follow-up task under its epic (or names the
follow-ups given), posts one Parked comment with the reason and the evidence, and notifies once. It refuses a closed
issue, an epic and an empty reason without changing anything, and `answers` never counts its comment as a reply.

Self-contained (no sibling import but gitrepo_support): each test has its own temporary HOME and git repository with an
`origin` remote (never contacted). `gh` is the scripted fake (fakegh.py) behind a wrapper written once for the module
that also records the text of every `--body-file PATH` it is given (the tool may write a body to a temporary file), so
the bodies are checked whichever way the tool hands them to gh. terminal-notifier and osascript are recorders. Each
test's first run of the tool is a real process; its further rows go through gitrepo_support.run_in_process.
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

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-board")
FAKEGH = os.path.join(HERE, "fakegh.py")
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
OWNER = {"match": ["api", "user"], "stdout": "rjeskuri\n"}
MARKER = "<!-- harness -->"
DROPPED = ("CLAUDE_PROJECT_DIR", "HARNESS_GH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "HARNESS_NOTIFY_APP",
           "HARNESS_NOTIFY_SOUND", "HARNESS_PROJECT", "HARNESS_SESSION_PROJECT", "HARNESS_CODE_TIER",
           "HARNESS_CODE_EFFORT", "HARNESS_REVIEW_TIER", "HARNESS_WAVE_REVIEW")

TASK_BODY = """## Request
Make the red check pass on CI.

## Outline
1. Fix it.

## Definition of done
- [ ] It passes

## Tests that must pass
- `tests/test_x.py::test_y`

## Context
- `bin/x`
"""

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
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-park-bin-"))
    py = sys.executable
    fakebin = os.path.join(root, "fakebin")
    wrapper = write(os.path.join(root, "ghwrap.py"), GH_WRAPPER.replace("@HERE@", repr(HERE)))
    if " " in py:
        executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{py}" "{wrapper}" "$@"\n')
    else:
        executable(os.path.join(fakebin, "gh"), f"#!{py} -SB\n" + GH_WRAPPER.replace("@HERE@", repr(HERE)))
    recorder = write(os.path.join(root, "recorder.py"),
                     "import json, os, sys\n"
                     "with open(os.environ['NOTIFY_LOG'], 'a') as fh:\n"
                     "    fh.write(json.dumps([os.path.basename(sys.argv[1])] + sys.argv[2:]) + '\\n')\n")
    for name in ("terminal-notifier", "osascript"):
        executable(os.path.join(fakebin, name), f'#!/bin/sh\nexec "{py}" "{recorder}" "$0" "$@"\n')
    BINS.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def issue(number, labels, parent=None, state="OPEN", comments=(), body=TASK_BODY, title="Make the red check pass"):
    """A `gh issue view --json ...` answer carrying every field a caller may ask for."""
    return {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}", "body": body,
            "labels": [{"name": n} for n in labels], "state": state,
            "parent": ({"number": parent, "title": "The epic", "url": f"https://github.com/o/r/issues/{parent}"}
                       if parent else None),
            "blockedBy": [], "comments": list(comments)}


def option_values(argv, *names):
    """Every value of the options `names` in argv (`--opt v` or `--opt=v`), comma lists split."""
    out = []
    for i, tok in enumerate(argv):
        value = None
        if tok in names and i + 1 < len(argv):
            value = argv[i + 1]
        else:
            for name in names:
                if name.startswith("--") and tok.startswith(name + "="):
                    value = tok[len(name) + 1:]
        if value is not None:
            out += [v.strip() for v in value.split(",") if v.strip()]
    return out


def first_option(argv, *names):
    for i, tok in enumerate(argv):
        if tok in names and i + 1 < len(argv):
            return argv[i + 1]
        for name in names:
            if name.startswith("--") and tok.startswith(name + "="):
                return tok[len(name) + 1:]
    return None


class ParkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-board-park-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.log = os.path.join(self.tmp, "gh.log")
        self.body_log = os.path.join(self.tmp, "bodies.log")
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.ran_a_process = False
        subprocess.run([GIT, "-C", self.repo, "init", "-q"], env=self.env(), capture_output=True, check=True)
        subprocess.run([GIT, "-C", self.repo, "remote", "add", "origin", "https://github.com/o/r.git"],
                       env=self.env(), capture_output=True, check=True)

    # ----------------------------------------------------------- helpers
    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in DROPPED}
        paths = [BINS["fakebin"]] + ([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(paths), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.log,
                   BODY_LOG=self.body_log, NOTIFY_LOG=self.notify_log)
        return env

    def scenario(self, calls, default=None):
        data = {"calls": calls}
        if default is not None:
            data["default"] = default
        write(self.scenario_path, json.dumps(data))
        for leftover in (self.scenario_path + ".state.json", self.log, self.body_log, self.notify_log):
            if os.path.exists(leftover):
                os.unlink(leftover)

    def board(self, *args):
        if self.ran_a_process:
            return run_in_process(TOOL, args, cwd=self.repo, env=self.env())
        self.ran_a_process = True
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=self.repo, env=self.env(),
                              capture_output=True, text=True, timeout=60)

    def entries(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def calls(self):
        return [e["argv"] for e in self.entries()]

    def notifications(self):
        if not os.path.exists(self.notify_log):
            return []
        with open(self.notify_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def body_of(self, entry):
        """The body a logged gh call carried: --body/-b, --body-file - (stdin) or --body-file PATH (recorded)."""
        argv = entry["argv"]
        text = first_option(argv, "--body", "-b")
        if text is not None:
            return text
        path = first_option(argv, "--body-file")
        if path == "-":
            return entry["stdin"]
        if path is not None and os.path.exists(self.body_log):
            with open(self.body_log, encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec["argv"] == argv:
                        return rec["text"]
        self.fail(f"no body found for gh {argv}")

    def park_scenario(self, n, labels, parent=None, m=None, state="OPEN"):
        calls = [REPO_VIEW, OWNER,
                 {"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels, parent, state)},
                 {"match": ["issue", "comment", str(n)],
                  "stdout": f"https://github.com/o/r/issues/{n}#issuecomment-77\n"}]
        if m is not None:
            calls += [{"match": ["issue", "create"], "stdout": f"https://github.com/o/r/issues/{m}\n"},
                      {"match": ["api", f"repos/o/r/issues/{m}"], "stdout": f"{9000 + m}\n"}]
        self.scenario(calls, default={"stdout": ""})

    def final_labels(self, n, initial):
        """N's labels after applying every `gh issue edit N --add-label/--remove-label` the tool made."""
        names = list(initial)
        for argv in self.calls():
            if argv[:3] == ["issue", "edit", str(n)]:
                for name in option_values(argv, "--remove-label"):
                    names = [x for x in names if x != name]
                for name in option_values(argv, "--add-label"):
                    if name not in names:
                        names.append(name)
        return names

    def comments_on(self, n):
        return [self.body_of(e) for e in self.entries() if e["argv"][:3] == ["issue", "comment", str(n)]]

    def creates(self):
        return [e for e in self.entries() if e["argv"][:2] == ["issue", "create"]]

    def sub_issue_posts(self):
        return [argv for argv in self.calls()
                if argv[:1] == ["api"] and "POST" in argv and any(t.endswith("/sub_issues") for t in argv)]

    def assertParkedComment(self, body, reason, evidence_lines, follow_ups):
        lines = body.strip().splitlines()
        self.assertEqual(lines[0].strip(), "**Parked**", body)
        self.assertEqual(lines[-1].strip(), MARKER, body)
        self.assertIn(reason.strip(), body)
        for line in evidence_lines:
            self.assertIn(line, body)
        for m in follow_ups:
            self.assertIn(f"#{m}", body)
        if evidence_lines:
            self.assertIn("```", body)

    def assertOneNotification(self, n, reason_first):
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        message = " ".join(notes[0][1:])
        for needle in ("proj", f"#{n}", reason_first[:50]):
            self.assertIn(needle, message)

    # ----------------------------------------------------------- the tests
    def test_park_blocks_comments_files_follow_up_and_notifies(self):
        rows = [
            # N with an epic and risk:high; a long evidence file; plain output
            dict(n=12, labels=["task", "status:in-progress", "size:M", "risk:high"], parent=3, m=13,
                 reason="The red check times out on CI\nIt hangs in the gh fake after 60 s.",
                 evidence=[f"trace-{i:02d}" for i in range(1, 51)], json=False, risk="risk:high"),
            # N with another epic and risk:low; a first reason line longer than 60 characters; --json
            dict(n=27, labels=["bug", "status:in-progress", "status:ready", "size:S", "risk:low"], parent=8, m=40,
                 reason="The sub-issue link keeps failing with HTTP 422 Validation Failed on every single retry",
                 evidence=["boom-1", "boom-2", "boom-3"], json=True, risk="risk:low"),
            # N without an epic and without a risk label; no evidence file; --json
            dict(n=31, labels=["chore", "status:in-review", "size:S"], parent=None, m=52,
                 reason="Waiting on a token with the project scope", evidence=None, json=True, risk="risk:low"),
        ]
        for row in rows:
            with self.subTest(n=row["n"]):
                n, m, parent = row["n"], row["m"], row["parent"]
                reason_first = row["reason"].splitlines()[0]
                self.park_scenario(n, row["labels"], parent, m)
                args = ["park", str(n), "--reason", row["reason"]]
                if row["evidence"]:
                    args += ["--evidence-file",
                             write(os.path.join(self.tmp, f"ev{n}.txt"), "\n".join(row["evidence"]) + "\n")]
                if row["json"]:
                    args.append("--json")
                p = self.board(*args)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

                # N: status:blocked and no other status label
                final = self.final_labels(n, row["labels"])
                self.assertEqual([x for x in final if x.startswith("status:")], ["status:blocked"], final)

                # exactly one proposed task, with the labels, the title and a body show parses
                creates = self.creates()
                self.assertEqual(len(creates), 1, self.calls())
                argv = creates[0]["argv"]
                self.assertEqual(sorted(option_values(argv, "--label", "-l")),
                                 sorted(["task", "status:proposed", "size:S", row["risk"]]))
                title = first_option(argv, "--title", "-t")
                prefix = f"Unstick #{n}: "
                self.assertTrue(title.startswith(prefix), title)
                self.assertLessEqual(len(title) - len(prefix), 60, title)
                self.assertTrue(title[len(prefix):].startswith(reason_first[:50]), title)
                task_body = self.body_of(creates[0])

                # linked under N's epic, and only then; never a blocked-by link
                posts = self.sub_issue_posts()
                if parent:
                    self.assertEqual(len(posts), 1, posts)
                    self.assertIn(f"repos/o/r/issues/{parent}/sub_issues", posts[0])
                    self.assertTrue(any(t.endswith(f"sub_issue_id={9000 + m}") for t in posts[0]), posts[0])
                else:
                    self.assertEqual(posts, [])
                self.assertFalse(any("--add-blocked-by" in t or "--blocked-by" in t
                                     for c in self.calls() for t in c), self.calls())

                # one Parked comment on N: reason, evidence, follow-up number
                comments = self.comments_on(n)
                self.assertEqual(len(comments), 1, comments)
                self.assertParkedComment(comments[0], row["reason"], row["evidence"] or [], [m])

                # one notification naming the repo, N and the reason
                self.assertOneNotification(n, reason_first)

                if row["json"]:
                    out = json.loads(p.stdout)
                    self.assertEqual(out["number"], n)
                    self.assertEqual(out["follow_up"], [m])
                    self.assertIn("url", out)
                    self.assertIs(out["notified"], True)
                else:
                    self.assertIn(f"#{n}", p.stdout)
                    self.assertIn(f"#{m}", p.stdout)

                # the follow-up's body parses with harness-board show into every ticket section
                self.scenario([REPO_VIEW, OWNER,
                               {"match": ["issue", "view", str(m)],
                                "stdout_json": issue(m, ["task", "status:proposed", "size:S", row["risk"]],
                                                     body=task_body, title=title)}], default={"stdout": ""})
                shown = self.board("show", str(m), "--json")
                self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
                sections = json.loads(shown.stdout)["sections"]
                self.assertIn(reason_first, sections["request"])
                self.assertTrue(sections["outline"].strip(), task_body)
                self.assertTrue(sections["definition_of_done"], task_body)
                self.assertTrue(sections["tests"], task_body)
                self.assertIn(f"#{n}", sections["context"])
                if row["evidence"] and len(row["evidence"]) > 40:   # the last 40 lines only
                    self.assertIn(row["evidence"][-1], sections["context"])
                    self.assertIn(row["evidence"][-39], sections["context"])
                    for dropped in row["evidence"][:10]:
                        self.assertNotIn(dropped, task_body)
                elif row["evidence"]:
                    for line in row["evidence"]:
                        self.assertIn(line, sections["context"])

    def test_park_with_follow_up_creates_no_task(self):
        labels = ["task", "status:in-progress", "size:S", "risk:low", "priority:high"]
        self.park_scenario(12, labels, parent=3)
        reason = "Needs the deploy key rotated first"
        p = self.board("park", "12", "--reason", reason, "--follow-up", "41,42", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.sub_issue_posts(), [])
        final = self.final_labels(12, labels)
        self.assertEqual([x for x in final if x.startswith("status:")], ["status:blocked"], final)
        comments = self.comments_on(12)
        self.assertEqual(len(comments), 1, comments)
        self.assertParkedComment(comments[0], reason, [], [41, 42])
        out = json.loads(p.stdout)
        self.assertEqual(out["number"], 12)
        self.assertEqual(out["follow_up"], [41, 42])
        self.assertOneNotification(12, reason)

    def test_park_refuses_closed_epic_or_empty_reason(self):
        # the one-line error names its cause, so an unknown subcommand (also exit 1) cannot pass for a refusal
        rows = [
            ("closed", ["task", "status:in-progress", "size:S", "risk:low"], "CLOSED", "The check hangs",
             ("#12", "closed")),
            ("epic", ["epic", "status:ready", "size:L"], "OPEN", "The check hangs", ("#12", "epic")),
            ("empty reason", ["task", "status:in-progress", "size:S", "risk:low"], "OPEN", "   ", ("reason",)),
        ]
        for what, labels, state, reason, needles in rows:
            with self.subTest(what):
                self.park_scenario(12, labels, parent=None, m=13, state=state)
                p = self.board("park", "12", "--reason", reason)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertNotIn("invalid choice", p.stderr)
                for needle in needles:
                    self.assertIn(needle, p.stderr)
                changed = [argv for argv in self.calls()
                           if argv[:2] in (["issue", "edit"], ["issue", "comment"], ["issue", "create"],
                                           ["issue", "close"], ["project", "item-edit"], ["project", "item-add"])
                           or (argv[:1] == ["api"] and ("POST" in argv or "PATCH" in argv))]
                self.assertEqual(changed, [])
                self.assertEqual(self.notifications(), [])

    def test_answers_skips_parked_comment(self):
        labels = ["task", "status:needs-input", "size:S", "risk:low"]
        self.park_scenario(12, labels, parent=3, m=13)
        evidence = write(os.path.join(self.tmp, "ev.txt"), "Error: the base branch is gone\n")
        p = self.board("park", "12", "--reason", "No answer on the base branch yet", "--evidence-file", evidence)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        parked = self.comments_on(12)
        self.assertEqual(len(parked), 1)

        def c(when, body):
            return {"author": {"login": "rjeskuri"}, "createdAt": when, "body": body,
                    "url": f"https://github.com/o/r/issues/12#issuecomment-{when[11:13]}"}

        comments = [c("2026-11-01T10:00:00Z", "**Needs your input**\n\nWhich base branch?\n\n" + MARKER),
                    c("2026-11-01T11:00:00Z", "Use main."),
                    c("2026-11-01T12:00:00Z", parked[0]),
                    c("2026-11-01T13:00:00Z", "Retry it now.")]
        self.scenario([OWNER, {"match": ["issue", "view", "12"], "stdout_json": {"comments": comments}}])
        a = self.board("answers", "12")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        self.assertEqual([x["body"] for x in json.loads(a.stdout)], ["Use main.", "Retry it now."])


if __name__ == "__main__":
    unittest.main()
