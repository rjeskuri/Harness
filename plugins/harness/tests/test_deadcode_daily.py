"""Tests for `harness-deadcode daily` (ticket #455): the daily scan that files one manual-land refactor ticket.

    python3 -m unittest discover -s plugins/harness/tests -p test_deadcode_daily.py

Each test builds a small Harness-shaped git repository (copied from a template made once per module, with an
`origin` on github.com that is never contacted) and runs `harness-deadcode daily --repo <it>`. Every test has its
own HOME. A `fakebin` written once per module holds `gh` (a wrapper around fakegh.py that also records the body
of every `--body-file`/`--body` call, so the filed ticket's body can be read back) and a recorder for launchctl
(HARNESS_LAUNCHCTL). Each test's first run is a real process, its further runs in process
(gitrepo_support.run_in_process). The expected candidates are written out from the fixture text, never computed by
the tool; a fingerprint's sha256 is the sha of the definition's source text, as test_deadcode.py pins it.

Rulings these tests rest on (the ticket leaves them open):
- The filed ticket is the issue `gh issue create` makes; its labels are its `--label` values plus any
  `gh issue edit <N> --add-label` the run makes on it. It sits under the epic when the run POSTs it to
  `repos/<slug>/issues/<epic>/sub_issues`.
- HARNESS_DEADCODE_EPIC is a setting: the environment first, else `env` in the repository's .claude/settings.json
  (the LaunchAgent's environment holds only PATH).
- The once-a-day record lives under ~/.claude/harness/deadcode/, so a fresh HOME is a new day; a run that files
  nothing (an open ticket, nothing to file, a dry run) does not use the day up.
- A dead-code PR is one whose head branch is `t/<N>-remove-dead-code` for its ticket N (titled `Remove dead code`,
  labelled manual-land). The fingerprints a ticket listed are read back from the body the tool itself wrote, in
  any layout; rejected.json holds them with their sha256, in any JSON layout.
- A test-only candidate is one whose `refs` is `test`. A candidate's removed lines are its definition's lines.
- The ticket body names each candidate and its path, `docs/design.md` with the line number of each line naming
  a contract candidate, and, under a `Tests that must pass` heading, the test cases that reach only it.
"""
from __future__ import annotations

import hashlib
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

from gitrepo_support import GIT, copy_repo, run_in_process

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS_DIR)
TOOL = os.path.join(PLUGIN, "bin", "harness-deadcode")
FAKEGH = os.path.join(TESTS_DIR, "fakegh.py")
LABEL = "com.ryaneskuri.harness-deadcode"
SLUG = "owner/fixture"
TITLE = "Remove dead code"

P = "plugins/harness/"
CORE = P + "lib/harness_lib/core.py"
TEST_CORE = P + "tests/test_core.py"

ALPHA = '''#!/usr/bin/env python3
"""harness-alpha: the one bin tool, which reaches live_one."""
import sys

from harness_lib import core


def main():
    print(core.live_one())
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

GH_WRAPPER = '''#!{py}
import json, os, select, subprocess, sys
args = sys.argv[1:]
data = ""
try:
    if not sys.stdin.isatty():
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        if ready:
            data = sys.stdin.read()
except Exception:
    data = ""
body = None
for flag in ("--body-file", "--body", "-F", "-f"):
    if flag in args[:-1]:
        value = args[args.index(flag) + 1]
        if flag == "--body":
            body = value
        elif flag == "--body-file":
            if value == "-":
                body = data
            else:
                try:
                    with open(value, encoding="utf-8") as fh:
                        body = fh.read()
                except OSError:
                    body = None
        elif value.startswith("body="):
            body = value[5:]
        if body is not None:
            break
log = os.environ.get("BODY_LOG")
if log and body is not None:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": args, "body": body}}) + "\\n")
done = subprocess.run([sys.executable, {fakegh!r}] + args, input=data, capture_output=True, text=True)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
'''

RECORDER = '''#!{py}
import json, os, sys
log = os.environ.get("LAUNCHCTL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(["launchctl"] + sys.argv[1:]) + "\\n")
'''

W = {}


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
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-deadcode-daily-"))
    W.update(root=root, fakebin=os.path.join(root, "fakebin"), template=os.path.join(root, "template"))
    py = sys.executable
    executable(os.path.join(W["fakebin"], "gh"), GH_WRAPPER.format(py=py, fakegh=FAKEGH))
    executable(os.path.join(W["fakebin"], "launchctl"), RECORDER.format(py=py))
    os.makedirs(W["template"])
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=root)
    for args in (("init", "-q"), ("remote", "add", "origin", f"https://github.com/{SLUG}.git")):
        subprocess.run([GIT, "-C", W["template"], *args], check=True, capture_output=True, env=env)


def tearDownModule():
    shutil.rmtree(W.get("root", ""), ignore_errors=True)


# ------------------------------------------------------------------ fixture text
def fn(name, ret, extra=0):
    """A top-level function of 2 + `extra` lines, as the source text the scanner hashes."""
    return "\n".join([f"def {name}():"] + [f"    v{i} = {i}" for i in range(extra)] + [f"    return {ret}"])


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def core_py(*defs):
    return '"""Core helpers."""\n\n\n' + "\n\n\n".join((fn("live_one", 1),) + defs) + "\n"


def tests_py(*called):
    """A test module whose test_<name> case calls core.<name>, plus test_live_one for the live helper."""
    cases = "".join(f"\n    def test_{n}(self):\n        self.assertTrue(core.{n}() is not None)\n"
                    for n in called + ("live_one",))
    return f"import unittest\n\nfrom harness_lib import core\n\n\nclass CoreTest(unittest.TestCase):{cases}"


def mentions(text, name):
    return re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", text) is not None


def hexes(text):
    return set(re.findall(r"\b[0-9a-f]{64}\b", text))


class DailyCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.assertTrue(os.path.isfile(TOOL), f"{TOOL} does not exist")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.ghlog = os.path.join(self.tmp, "gh.log")
        self.bodylog = os.path.join(self.tmp, "bodies.log")
        self.launchlog = os.path.join(self.tmp, "launchctl.log")
        self.real_done = False
        self.n = 0

    # ---------------------------------------------------------------- the world
    def repo(self, files, settings_epic=None):
        self.n += 1
        root = os.path.join(self.tmp, f"repo{self.n}")
        copy_repo(W["template"], root)
        base = {P + "hooks/hooks.json": '{"hooks": {}}\n', P + "lib/harness_lib/__init__.py": "",
                P + "bin/harness-alpha": ALPHA, "docs/design.md": "# Harness design\n"}
        for rel, text in {**base, **files}.items():
            write(os.path.join(root, rel), text)
        if settings_epic is not None:
            write(os.path.join(root, ".claude", "settings.json"),
                  json.dumps({"env": {"HARNESS_DEADCODE_EPIC": str(settings_epic)}}))
        return root

    def new_home(self):
        self.n += 1
        home = os.path.join(self.tmp, f"home{self.n}")
        os.makedirs(home)
        return home

    def gh(self, open_tickets=(), prs=(), issues=None, number=501):
        calls = [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": SLUG}},
            {"match": ["issue", "create"], "stdout": f"https://github.com/{SLUG}/issues/{number}\n"},
            {"contains": ["api", "--jq", ".id"], "stdout": "987654\n"},
            {"contains": ["api", "-X", "POST"], "stdout": "{}"},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "list"], "stdout_json": list(open_tickets)},
            {"match": ["pr", "list"], "stdout_json": list(prs)},
        ]
        for n, obj in (issues or {}).items():
            calls += [{"match": ["issue", "view", str(n)], "stdout_json": obj},
                      {"contains": [f"repos/{SLUG}/issues/{n}"], "stdout_json": obj}]
        write(self.scenario, json.dumps({"calls": calls, "default": {"stdout": "[]"}}))
        self.number = number
        for path in (self.scenario + ".state.json", self.ghlog, self.bodylog):
            if os.path.exists(path):
                os.remove(path)

    def env(self, home, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_", "LAUNCHCTL_", "BODY_"))}
        dirs = [W["fakebin"], os.path.dirname(GIT), os.path.dirname(sys.executable), "/usr/bin", "/bin"]
        env.update(HOME=home, PATH=os.pathsep.join(dict.fromkeys(dirs)), HARNESS_GH=os.path.join(W["fakebin"], "gh"),
                   HARNESS_LAUNCHCTL=os.path.join(W["fakebin"], "launchctl"), HARNESS_PROJECT="",
                   FAKEGH_SCENARIO=self.scenario, FAKEGH_LOG=self.ghlog, BODY_LOG=self.bodylog,
                   LAUNCHCTL_LOG=self.launchlog, GIT_CONFIG_NOSYSTEM="1")
        env.update(extra)
        return env

    def run_tool(self, home, *args, **extra):
        env = self.env(home, **extra)
        if not self.real_done:
            self.real_done = True
            p = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env,
                               cwd=self.tmp, stdin=subprocess.DEVNULL, timeout=170)
        else:
            p = run_in_process(TOOL, args=args, env=env, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, f"{args}: exit {p.returncode}\nstdout: {p.stdout[-2000:]}\n"
                                          f"stderr: {p.stderr[-2000:]}")
        return p

    def daily(self, home, repo, *extra, **env):
        return self.run_tool(home, "daily", "--repo", repo, *extra, **env)

    # ---------------------------------------------------------------- what gh saw
    @staticmethod
    def lines(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def writes(self):
        """The gh calls that create or change anything."""
        return [c["argv"] for c in self.lines(self.ghlog)
                if c["argv"][:2] in (["issue", "create"], ["issue", "edit"], ["pr", "create"])
                or (c["argv"][:1] == ["api"] and any(m in c["argv"] for m in ("POST", "PATCH", "PUT")))]

    def created(self):
        """[(title, labels, body, parent epics)] of each issue the run created, from the gh log."""
        out = []
        bodies = [b for b in self.lines(self.bodylog) if b["argv"][:2] == ["issue", "create"]]
        log = self.lines(self.ghlog)
        creates = [c["argv"] for c in log if c["argv"][:2] == ["issue", "create"]]
        for i, argv in enumerate(creates):
            title = argv[argv.index("--title") + 1] if "--title" in argv else None
            labels = {argv[j + 1] for j, a in enumerate(argv[:-1]) if a in ("--label", "-l")}
            body = bodies[i]["body"] if i < len(bodies) else ""
            out.append([title, labels, body, set()])
        if not out:
            return out
        number = str(self.number)
        for c in log:
            argv = c["argv"]
            if argv[:3] == ["issue", "edit", number]:
                for j, a in enumerate(argv[:-1]):
                    if a in ("--add-label",):
                        out[-1][1].update(x.strip() for x in argv[j + 1].split(","))
            if argv[:1] == ["api"] and "POST" in argv:
                for a in argv:
                    m = re.match(rf"^/?repos/{re.escape(SLUG)}/issues/(\d+)/sub_issues$", a)
                    if m:
                        out[-1][3].add(int(m.group(1)))
        return [tuple(x) for x in out]

    def one_ticket(self):
        made = self.created()
        self.assertEqual(len(made), 1, f"expected one ticket, got {made}\ngh calls: {self.lines(self.ghlog)}")
        return made[0]

    def assertFiledNothing(self):
        self.assertEqual(self.writes(), [], "the run changed GitHub")

    @staticmethod
    def filed_names(body, names):
        return {n for n in names if mentions(body, n)}

    @staticmethod
    def section_of_tests(body):
        m = re.search(r"^(#+)\s*Tests that must pass[^\n]*\n", body, re.M | re.I)
        if not m:
            return None
        rest = body[m.end():]
        end = re.search(r"^#{1,%d}\s" % len(m.group(1)), rest, re.M)
        return rest[:end.start()] if end else rest


# ====================================================================== filing
class FileTicket(DailyCase):
    def test_files_one_manual_land_refactor_ticket_under_the_epic(self):
        """daily files one ticket: title, labels, epic, the candidates with their evidence."""
        design = "# Harness design\n" + "".join(f"filler line {i}\n" for i in range(2, 13)) \
            + "- `dead_named` is the helper the contract names.\n"
        repo = self.repo({CORE: core_py(fn("dead_tested", 8), fn("dead_named", 9)),
                          TEST_CORE: tests_py("dead_tested"), "docs/design.md": design}, settings_epic=439)
        self.gh()
        self.daily(self.new_home(), repo)
        title, labels, body, parents = self.one_ticket()
        self.assertEqual(title, TITLE)
        for label in ("manual-land", "refactor", "risk:low", "size:S", "status:ready"):
            self.assertIn(label, labels)
        self.assertNotIn("size:M", labels)
        self.assertNotIn("status:proposed", labels)
        self.assertEqual(parents, {439})                           # HARNESS_DEADCODE_EPIC from settings.json
        self.assertEqual(self.filed_names(body, ["dead_tested", "dead_named", "live_one"]),
                         {"dead_tested", "dead_named"})
        self.assertIn(CORE, body)
        self.assertRegex(body, r"design\.md\D{0,20}\b13\b")         # the line of docs/design.md naming dead_named
        section = self.section_of_tests(body)
        self.assertIsNotNone(section, f"no Tests that must pass section:\n{body}")
        self.assertTrue(mentions(section, "test_dead_tested"), section)   # the case that reaches only it
        self.assertFalse(mentions(section, "test_live_one"), section)     # a case that stays

        # sizes and the epic from the environment, each a new repository and a new day
        for defs, size, epic in (([fn("dead_kiwi", 3)], "size:S", 77),
                                 ([fn("dead_kiwi", 3), fn("dead_lime", 4), fn("dead_mango", 5)], "size:M", 78)):
            with self.subTest(size=size, n=len(defs)):
                repo = self.repo({CORE: core_py(*defs)})
                self.gh()
                self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC=str(epic))
                title, labels, body, parents = self.one_ticket()
                self.assertEqual(title, TITLE)
                self.assertIn(size, labels)
                self.assertIn("manual-land", labels)
                self.assertEqual(parents, {epic})
                names = [d.split("(")[0][4:] for d in defs]
                self.assertEqual(self.filed_names(body, names + ["live_one"]), set(names))

        with self.subTest("no candidates, no ticket"):
            repo = self.repo({CORE: core_py()})
            self.gh()
            self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="77")
            self.assertFiledNothing()

    def test_batch_caps_and_test_only_last(self):
        """At most 5 candidates and 150 removed non-test lines; test-only candidates go last (and so first out)."""
        none6 = ["dead_a1", "dead_a2", "dead_a3", "dead_a4", "dead_a5", "dead_a6"]
        repo = self.repo({CORE: core_py(fn("dead_tested", 0), *(fn(n, i) for i, n in enumerate(none6))),
                          TEST_CORE: tests_py("dead_tested")})
        self.gh()
        self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
        _, labels, body, _ = self.one_ticket()
        filed = self.filed_names(body, none6 + ["dead_tested"])
        self.assertEqual(len(filed), 5, filed)
        self.assertNotIn("dead_tested", filed)                   # first in the file, but test-only: left out
        self.assertIn("size:M", labels)

        with self.subTest("150 removed lines"):
            big = {"dead_big1": 101, "dead_big2": 101, "dead_small": 2}
            repo = self.repo({CORE: core_py(fn("dead_big1", 1, 99), fn("dead_big2", 2, 99), fn("dead_small", 3))})
            self.gh()
            self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
            _, _, body, _ = self.one_ticket()
            filed = self.filed_names(body, list(big))
            self.assertTrue(filed, body)
            self.assertFalse({"dead_big1", "dead_big2"} <= filed, filed)
            self.assertLessEqual(sum(big[n] for n in filed), 150, filed)

        with self.subTest("test-only after the others"):
            repo = self.repo({CORE: core_py(fn("dead_tested", 0), fn("dead_b1", 1), fn("dead_b2", 2)),
                              TEST_CORE: tests_py("dead_tested")})
            self.gh()
            self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
            _, labels, body, _ = self.one_ticket()
            self.assertEqual(self.filed_names(body, ["dead_tested", "dead_b1", "dead_b2"]),
                             {"dead_tested", "dead_b1", "dead_b2"})
            first = {n: re.search(r"(?<![\w])" + n + r"(?![\w])", body).start()
                     for n in ("dead_tested", "dead_b1", "dead_b2")}
            self.assertGreater(first["dead_tested"], max(first["dead_b1"], first["dead_b2"]), body)
            self.assertIn("size:M", labels)


# ====================================================================== dry run
class DryRun(DailyCase):
    def test_dry_run_prints_the_batch_and_files_nothing(self):
        names = ["dead_p1", "dead_p2", "dead_p3"]
        repo = self.repo({CORE: core_py(*(fn(n, i) for i, n in enumerate(names)))})
        home = self.new_home()
        self.gh()
        p = self.daily(home, repo, "--dry-run", HARNESS_DEADCODE_EPIC="439")
        for n in names:
            self.assertTrue(mentions(p.stdout, n), p.stdout)
        self.assertFalse(mentions(p.stdout, "live_one"), p.stdout)
        self.assertFiledNothing()

        self.gh()                                                  # the dry run did not use the day up
        self.daily(home, repo, HARNESS_DEADCODE_EPIC="439")
        _, _, body, _ = self.one_ticket()
        self.assertEqual(self.filed_names(body, names), set(names))


# ====================================================================== at most one
class AtMostOne(DailyCase):
    OPEN = {"number": 90, "title": TITLE, "state": "OPEN", "url": f"https://github.com/{SLUG}/issues/90",
            "labels": [{"name": n} for n in ("manual-land", "refactor", "status:in-review", "size:S", "risk:low")]}

    def test_none_while_one_is_open_and_at_most_one_a_day(self):
        repo = self.repo({CORE: core_py(fn("dead_q1", 1), fn("dead_q2", 2))})
        self.gh(open_tickets=[self.OPEN])
        self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
        self.assertFiledNothing()                                  # an open Remove dead code ticket

        home = self.new_home()
        self.gh()                                                  # none open: it files
        self.daily(home, repo, HARNESS_DEADCODE_EPIC="439")
        self.one_ticket()

        self.gh()                                                  # the same day again, none open: nothing
        self.daily(home, repo, HARNESS_DEADCODE_EPIC="439")
        self.assertFiledNothing()


# ====================================================================== rejections
class Rejections(DailyCase):
    def pr(self, ticket, merged):
        return {"number": 610, "title": TITLE, "headRefName": f"t/{ticket}-remove-dead-code",
                "state": "MERGED" if merged else "CLOSED", "mergedAt": "2026-11-23T10:00:00Z" if merged else None,
                "closedAt": "2026-11-23T10:00:00Z", "labels": [{"name": "manual-land"}],
                "body": f"Removes the batch.\n\nCloses #{ticket}\n", "closingIssuesReferences": [{"number": ticket}],
                "url": f"https://github.com/{SLUG}/pull/610"}

    def ticket(self, number, body):
        return {"number": number, "title": TITLE, "body": body, "state": "CLOSED", "stateReason": "NOT_PLANNED",
                "url": f"https://github.com/{SLUG}/issues/{number}", "comments": [],
                "createdAt": "2026-11-20T03:30:00Z", "closedAt": "2026-11-23T10:00:00Z",
                "labels": [{"name": n} for n in ("manual-land", "refactor", "status:ready", "size:S", "risk:low")]}

    def test_closed_unmerged_pr_rejects_its_candidates_until_their_source_changes(self):
        apple, banana = fn("dead_apple", 1), fn("dead_banana", 2)
        repo = self.repo({CORE: core_py(apple, banana)})
        self.gh(number=501)
        self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
        _, _, first_body, _ = self.one_ticket()
        self.assertEqual(self.filed_names(first_body, ["dead_apple", "dead_banana"]), {"dead_apple", "dead_banana"})

        # The owner closed the PR unmerged (and its ticket). dead_apple changes, dead_cherry is new.
        apple2, cherry = fn("dead_apple", 111), fn("dead_cherry", 3)
        write(os.path.join(repo, CORE), core_py(apple2, banana, cherry))
        names = ["dead_apple", "dead_banana", "dead_cherry"]

        home = self.new_home()                                     # the next day
        self.gh(prs=[self.pr(501, merged=False)], issues={501: self.ticket(501, first_body)}, number=502)
        self.daily(home, repo, HARNESS_DEADCODE_EPIC="439")
        _, _, body, _ = self.one_ticket()
        self.assertEqual(self.filed_names(body, names), {"dead_apple", "dead_cherry"})
        key = hashlib.sha1(os.path.realpath(repo).encode("utf-8")).hexdigest()[:12]
        rejected = os.path.join(home, ".claude", "harness", "deadcode", key, "rejected.json")
        self.assertTrue(os.path.isfile(rejected), f"no {rejected}")
        with open(rejected, encoding="utf-8") as fh:
            text = fh.read()
        json.loads(text)
        self.assertLessEqual({sha(banana), sha(apple)}, hexes(text))   # what ticket #501 listed
        self.assertNotIn(sha(apple2), hexes(text))
        self.assertTrue(mentions(text, "dead_banana") and CORE in text, text)

        with self.subTest("a merged dead-code PR rejects nothing"):
            self.gh(prs=[self.pr(501, merged=True)], issues={501: self.ticket(501, first_body)}, number=503)
            self.daily(self.new_home(), repo, HARNESS_DEADCODE_EPIC="439")
            _, _, body, _ = self.one_ticket()
            self.assertEqual(self.filed_names(body, names), set(names))


# ====================================================================== launchd
class Launchd(DailyCase):
    def test_install_and_uninstall_launchd(self):
        repo = self.repo({CORE: core_py(fn("dead_r1", 1))})
        home = self.new_home()
        plist = os.path.join(home, "Library", "LaunchAgents", f"{LABEL}.plist")
        log = os.path.join(home, ".claude", "harness", "deadcode", "launchd.log")
        job_path = f"/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin"
        uid = os.getuid()
        self.gh()
        self.run_tool(home, "daily", "--install-launchd", "--repo", repo)
        with open(plist, "rb") as fh:
            job = plistlib.load(fh)
        self.assertEqual(job["Label"], LABEL)
        args = job["ProgramArguments"]
        self.assertEqual(args[1:3], [os.path.realpath(TOOL), "daily"], args)
        self.assertTrue(os.access(args[0], os.X_OK), args)
        self.assertTrue(os.path.realpath(repo) in args[3:] or job.get("WorkingDirectory") == os.path.realpath(repo),
                        job)
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertIs(job["RunAtLoad"], False)
        self.assertEqual(job["EnvironmentVariables"], {"PATH": job_path})
        self.assertEqual((job["StandardOutPath"], job["StandardErrorPath"]), (log, log))
        self.assertTrue(os.path.isdir(os.path.dirname(log)))
        self.assertIn(["launchctl", "bootstrap", f"gui/{uid}", plist], self.lines(self.launchlog))
        self.assertFiledNothing()

        os.remove(self.launchlog)
        self.run_tool(home, "daily", "--uninstall-launchd")
        self.assertFalse(os.path.exists(plist))
        self.assertIn(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], self.lines(self.launchlog))


if __name__ == "__main__":
    unittest.main()
