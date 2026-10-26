"""Tests for `harness-pr findings` (ticket #256, epic #249): split a review by the ticket's touched files,
record the round, and keep the notes file.

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings.py -v

Fixture (the WorktreeTest pattern of test_pr.py, copied so this module stays off test_pr.py): a temporary HOME,
a bare origin, the main checkout `proj` pushed to it, and the ticket worktree on t/12-x branched from
origin/main. The branch changes `src/app.py` and adds `tests/test_app.py` (the ticket's worked example). After
the branch point main gets a commit that changes `README.md` and `lib/x.py`, pushed to origin, so the merge-base
differs from origin/main and a diff against origin/main (instead of the merge-base) would wrongly count those
two files as touched. The command runs from the main checkout with `--worktree <wt>`, so a tool that diffed the
cwd would see nothing touched.

gh is fakegh.py behind a wrapper with an empty scenario: any gh call fails and is logged.

Expected values are derived here: the touched set from the fixture, the normalisation from the documented
severity table (critical and blocker to high, major to medium, minor and nit to low, unknown to low; a leading
`./` dropped), the merge-base and heads from real git.

Choices the ticket left open, fixed here:
- the `blocking`, `low` and `outside` fields of `--json` are lists of normalised findings (as in the round);
- `ticket` in `--json` is the int N; the `files` paths are absolute (compared by realpath);
- list order inside a split is not pinned (compared as sorted multisets);
- `### Low findings` lines use the Out of scope form: ``- `path:line` [low] body``, plus the outside marker;
- a `Ruling:` line is one that starts with `Ruling:`, optionally after `- `; rulings are compared by their text;
- the text output's three paths may be absolute or `~/`-shortened.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-pr")
LIB = os.path.join(os.path.dirname(HERE), "lib")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
BRANCH = "t/12-x"
OUTSIDE = "(outside the files this ticket changed)"
TOUCHED = {"src/app.py", "tests/test_app.py"}
ALIASES = {"critical": "high", "blocker": "high", "major": "medium", "minor": "low", "nit": "low", "info": "low"}

# The ticket's worked example.
EXAMPLE = [{"path": "src/app.py", "line": 3, "severity": "high", "body": "A"},
           {"path": "src/app.py", "line": 9, "severity": "low", "body": "B"},
           {"path": "README.md", "line": 1, "severity": "medium", "body": "C"},
           {"path": "lib/x.py", "line": 2, "severity": "low", "body": "D"}]

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
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-findings-bin-"))
    py = sys.executable
    fakebin = os.path.join(root, "fakebin")
    executable(os.path.join(fakebin, "gh"),
               f'#!/bin/sh\nprintf \'%s\\n\' "gh $*" >> "$ORDER_LOG"\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "git"),
               f'#!/bin/sh\nprintf \'%s\\n\' "git $*" >> "$ORDER_LOG"\nexec "{GIT}" "$@"\n')
    os.symlink(py, os.path.join(fakebin, "python3"))
    BINS.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


# --------------------------------------------------------------- expectations, derived here
def norm(f):
    """A finding as load_findings normalises it, computed from the documented table."""
    path = str(f.get("path") or "").strip()
    if path.startswith("./"):
        path = path[2:]
    sev = str(f.get("severity") or "").strip().lower()
    sev = ALIASES.get(sev, sev)
    if sev not in ("high", "medium", "low"):
        sev = "low"
    line = f.get("line")
    return {"path": path, "line": line if isinstance(line, int) and line > 0 else None, "severity": sev,
            "body": str(f.get("body") or "").strip()}


def split(findings, touched=TOUCHED):
    blocking, low, outside = [], [], []
    for f in map(norm, findings):
        if f["path"] in touched:
            (blocking if f["severity"] in ("high", "medium") else low).append(f)
        else:
            outside.append(f)
    return blocking, low, outside


def bag(findings):
    return sorted(json.dumps(f, sort_keys=True) for f in findings)


def entry(f, outside=False):
    loc = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
    return f"- `{loc}` [{f['severity']}] {f['body']}" + (f" {OUTSIDE}" if outside else "")


def sections(text):
    """{heading: [non-empty lines]} for the `### ` sections of a notes file."""
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            out.setdefault(current, [])
        elif current is not None and line.strip():
            out[current].append(line.rstrip())
    return out


class FindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-findings-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = write(os.path.join(self.tmp, "scenario.json"), json.dumps({"calls": []}))
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.order_log = os.path.join(self.tmp, "order.log")
        self.n = 0
        self.make_repos()

    def make_repos(self):
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, "src", "app.py"), "def app():\n    return 1\n")
        write(os.path.join(self.main, "src", "other.py"), "X = 1\n")
        write(os.path.join(self.main, "lib", "x.py"), "Y = 1\n")
        write(os.path.join(self.main, ".git", "info", "exclude"), ".claude/worktrees/\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "t-12-x")
        self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, self.wt, "origin/main")
        write(os.path.join(self.wt, "src", "app.py"), "def app():\n    return 2\n")
        write(os.path.join(self.wt, "tests", "test_app.py"), "def test_app():\n    assert True\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Change the app")
        # main moves on after the branch point: README.md and lib/x.py change there, not on the branch
        write(os.path.join(self.main, "README.md"), "# proj\n\nMore.\n")
        write(os.path.join(self.main, "lib", "x.py"), "Y = 2\n")
        self.git(self.main, "commit", "-q", "-am", "Main moves on")
        self.git(self.main, "push", "-q", "origin", "main")
        self.merge_base = self.git(self.wt, "merge-base", "HEAD", "origin/main")
        assert self.merge_base != self.git(self.main, "rev-parse", "origin/main")

    # ----------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "FAKEGH_", "GIT_", "CLAUDE_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["fakebin"], "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test",
                   GIT_AUTHOR_EMAIL="test@localhost", GIT_COMMITTER_NAME="Test",
                   GIT_COMMITTER_EMAIL="test@localhost", HARNESS_GH=os.path.join(BINS["fakebin"], "gh"),
                   HARNESS_CLAUDE=os.path.join(self.tmp, "no-claude"), HARNESS_POLL_SECONDS="0.01",
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log, ORDER_LOG=self.order_log)
        return env

    def git(self, repo, *args):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def head(self):
        return self.git(self.wt, "rev-parse", "HEAD")

    def commit(self, name, text):
        write(os.path.join(self.wt, name), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", f"Change {name}")
        return self.head()

    def review_file(self, content):
        """A review file: a list is written as JSON followed by the reviewer's verdict text."""
        self.n += 1
        text = content if isinstance(content, str) else json.dumps(content, indent=1) + "\n\nVerdict: see above.\n"
        return write(os.path.join(self.tmp, "reviews-in", f"review-{self.n}.txt"), text)

    def rulings_file(self, text):
        self.n += 1
        return write(os.path.join(self.tmp, "rulings-in", f"rulings-{self.n}.md"), text)

    def pr(self, *args, cwd=None):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.main, env=self.env(),
                              capture_output=True, text=True, timeout=120)

    def findings(self, review, rulings=None, as_json=True, ticket="12"):
        """Run `harness-pr findings` on a review (list or raw text); the parsed --json output (or the process)."""
        args = ["findings", ticket, "--worktree", self.wt, "--review", self.review_file(review)]
        if rulings is not None:
            args += ["--rulings", self.rulings_file(rulings)]
        if as_json:
            args.append("--json")
        p = self.pr(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout) if as_json else p

    def key(self):
        return hashlib.sha1(os.path.realpath(self.main).encode()).hexdigest()[:12]

    def reviews_dir(self, ticket=12):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key(), "reviews", str(ticket))

    def read(self, *parts):
        with open(os.path.join(self.reviews_dir(), *parts), encoding="utf-8") as fh:
            return fh.read()

    def read_json(self, *parts):
        return json.loads(self.read(*parts))

    def notes(self):
        return self.read("notes.md")

    def snapshot(self):
        """Every file under the harness state folder, with its content."""
        root = os.path.join(self.home, ".claude")
        out = {}
        for folder, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, root)] = fh.read()
        return out

    def load_rounds(self):
        """The rounds through harness_lib.reviews, with HOME pointed at the test's."""
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        import importlib
        importlib.invalidate_caches()
        reviews = importlib.import_module("harness_lib.reviews")
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        try:
            return reviews.load_rounds(self.key(), 12)
        finally:
            os.environ["HOME"] = old

    def gh_calls(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as fh:
            return [line for line in fh if line.strip()]

    def assertSplit(self, out, findings):
        blocking, low, outside = split(findings)
        self.assertEqual(bag(out["blocking"]), bag(blocking), "blocking")
        self.assertEqual(bag(out["low"]), bag(low), "low")
        self.assertEqual(bag(out["outside"]), bag(outside), "outside")

    def assertShownPath(self, text, path):
        shown = {os.path.realpath(path), "~" + os.path.realpath(path)[len(os.path.realpath(self.home)):]}
        self.assertTrue(any(s in text for s in shown), f"{path} not shown in:\n{text}")


# =========================================================================== split
SPREAD = [
    {"path": "src/app.py", "line": 3, "severity": "high", "body": "touched high"},
    {"path": "src/app.py", "line": 4, "severity": "medium", "body": "touched medium"},
    {"path": "src/app.py", "line": 5, "severity": "low", "body": "touched low"},
    {"path": "tests/test_app.py", "line": 1, "severity": "critical", "body": "test critical"},
    {"path": "tests/test_app.py", "line": 2, "severity": "major", "body": "test major"},
    {"path": "tests/test_app.py", "line": 3, "severity": "nit", "body": "test nit"},
    {"path": "./src/app.py", "line": 6, "severity": "blocker", "body": "dot-slash blocker"},
    {"path": "./tests/test_app.py", "line": 7, "severity": "minor", "body": "dot-slash minor"},
    {"path": "src/app.py", "line": None, "severity": "whatever", "body": "unknown severity is low"},
    {"path": "README.md", "line": 1, "severity": "high", "body": "readme high, changed on main only"},
    {"path": "lib/x.py", "line": 2, "severity": "medium", "body": "lib medium, changed on main only"},
    {"path": "lib/x.py", "line": 3, "severity": "low", "body": "lib low"},
    {"path": "src/other.py", "line": 1, "severity": "high", "body": "untouched file in the tree"},
    {"path": "", "line": 4, "severity": "high", "body": "empty path"},
    {"path": "src", "line": None, "severity": "medium", "body": "a folder, not a touched file"},
    {"path": "app.py", "line": 3, "severity": "high", "body": "a basename, not the touched path"},
    {"path": "src/app.py.orig", "line": 1, "severity": "high", "body": "a longer name with the touched prefix"},
    {"path": "tests/test_app.py", "line": 9, "severity": "low", "body": "test low"},
]


class Split(FindingsTest):
    def test_findings_split_by_touched_files_and_severity(self):
        # The worked example, text output first.
        p = self.findings(EXAMPLE, as_json=False)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(lines[0], "#12 review round 1: 1 blocking, 1 low, 2 outside; "
                                   "record: harness-work stage 12 review pass", p.stdout)
        rest = "\n".join(lines[1:])
        for name in ("round-1-blocking.json", "round-1-review.json", "notes.md"):
            self.assertShownPath(rest, os.path.join(self.reviews_dir(), name))
        a, b, c, d = map(norm, EXAMPLE)
        self.assertEqual(self.read_json("round-1-blocking.json"), [a])
        self.assertEqual(bag(self.read_json("round-1-review.json")), bag([a, b]))
        notes = sections(self.notes())
        self.assertEqual(sorted(notes["Low findings"]), sorted([entry(b), entry(d, outside=True)]))
        self.assertEqual(notes["Out of scope"], [entry(c, outside=True)])

        # The JSON shape, at the same HEAD (the round is replaced, so it stays round 1).
        out = self.findings(EXAMPLE)
        head = self.head()
        self.assertEqual(set(out), {"ticket", "round", "head", "base", "review_base", "blocking", "low", "outside",
                                    "files", "record"})
        self.assertEqual((out["ticket"], out["round"], out["head"], out["base"], out["review_base"]),
                         (12, 1, head, self.merge_base, head))
        self.assertEqual(out["record"], {"stage": "review", "result": "pass"})
        self.assertEqual(out["blocking"], [a])
        self.assertEqual(out["low"], [b])
        self.assertEqual(bag(out["outside"]), bag([c, d]))
        self.assertEqual({k: os.path.realpath(v) for k, v in out["files"].items()},
                         {k: os.path.realpath(os.path.join(self.reviews_dir(), name)) for k, name in
                          (("blocking", "round-1-blocking.json"), ("review", "round-1-review.json"),
                           ("notes", "notes.md"))})

        # A spread of every severity on and off the touched files, then the same review reordered.
        orders = [SPREAD, list(reversed(SPREAD)), SPREAD[1::2] + SPREAD[0::2]]
        expected_blocking, expected_low, _ = split(SPREAD)
        for k, review in enumerate(orders, 2):
            with self.subTest(order=k):
                self.commit("src/app.py", f"def app():\n    return {k + 10}\n")
                out = self.findings(review)
                self.assertEqual(out["round"], k)
                self.assertSplit(out, SPREAD)
                self.assertEqual(bag(self.read_json(f"round-{k}-blocking.json")), bag(expected_blocking))
                self.assertEqual(bag(self.read_json(f"round-{k}-review.json")),
                                 bag(expected_blocking + expected_low))
        # spot checks of the derivation itself, so a wrong table here cannot hide behind the tool
        bodies = lambda fs: {f["body"] for f in fs}
        blocking, low, outside = split(SPREAD)
        self.assertEqual(bodies(blocking), {"touched high", "touched medium", "test critical", "test major",
                                            "dot-slash blocker"})
        self.assertEqual(bodies(low), {"touched low", "test nit", "dot-slash minor", "unknown severity is low",
                                       "test low"})
        self.assertIn("empty path", bodies(outside))

    def test_unusable_review_records_nothing_and_an_empty_array_is_a_pass(self):
        unusable = ["The reviewer timed out before writing anything.\n",
                    "",
                    "Verdict: fine [see the notes] but no JSON here.\n",
                    '"just a string"\n']
        for text in unusable:
            with self.subTest(review=text):
                path = self.review_file(text)
                before = self.snapshot()
                p = self.pr("findings", "12", "--worktree", self.wt, "--review", path, "--json")
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(p.stderr.strip(), f"{path}: no findings array; the review stage failed")
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(os.path.exists(os.path.join(self.reviews_dir(), "rounds.json")))
                self.assertFalse(os.path.exists(os.path.join(self.reviews_dir(), "round-1-blocking.json")))

        # [] (with the verdict after it) is a usable review with no findings: round 1, review pass.
        out = self.findings("[]\n\nVerdict: approve, nothing to fix.\n")
        self.assertEqual((out["round"], out["blocking"], out["low"], out["outside"]), (1, [], [], []))
        self.assertEqual(out["record"], {"stage": "review", "result": "pass"})
        self.assertEqual(self.read_json("round-1-blocking.json"), [])
        self.assertEqual(self.read_json("round-1-review.json"), [])
        self.assertEqual(len(self.load_rounds()), 1)
        p = self.findings("[]", as_json=False)
        self.assertEqual(p.stdout.splitlines()[0],
                         "#12 review round 1: 0 blocking, 0 low, 0 outside; record: harness-work stage 12 review pass")
        notes = os.path.join(self.reviews_dir(), "notes.md")
        if os.path.exists(notes):                    # nothing to note: no section heading at all
            with open(notes, encoding="utf-8") as fh:
                self.assertNotIn("#", fh.read())

        # An unusable review after a recorded round changes nothing either.
        self.commit("src/app.py", "def app():\n    return 3\n")
        before = self.snapshot()
        path = self.review_file("no array at all\n")
        p = self.pr("findings", "12", "--worktree", self.wt, "--review", path)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stderr.strip(), f"{path}: no findings array; the review stage failed")
        self.assertEqual(self.snapshot(), before)


# =========================================================================== rounds
class Rounds(FindingsTest):
    def test_rounds_number_up_and_the_same_head_replaces_the_latest(self):
        h1 = self.head()
        self.assertEqual(self.findings(EXAMPLE)["round"], 1)
        h2 = self.commit("src/app.py", "def app():\n    return 5\n")
        first = [{"path": "src/app.py", "line": 1, "severity": "high", "body": "replaced high"},
                 {"path": "src/app.py", "line": 8, "severity": "low", "body": "only in the replaced round"}]
        self.assertEqual(self.findings(first)["round"], 2)
        replacement = [{"path": "tests/test_app.py", "line": 2, "severity": "medium", "body": "the replacement"},
                       {"path": "tests/test_app.py", "line": 3, "severity": "low", "body": "a replacement low"}]
        out = self.findings(replacement)
        self.assertEqual(out["round"], 2)
        rounds = self.load_rounds()
        self.assertEqual([r["round"] for r in rounds], [1, 2])
        self.assertEqual([r["head"] for r in rounds], [h1, h2])
        self.assertEqual(rounds[1]["blocking"], [norm(replacement[0])])
        self.assertEqual(self.read_json("round-2-blocking.json"), [norm(replacement[0])])
        self.assertEqual(bag(self.read_json("round-2-review.json")), bag(map(norm, replacement)))
        self.assertNotIn("only in the replaced round", self.notes())
        self.assertIn("a replacement low", self.notes())
        # round 1's files stay as they were
        self.assertEqual(self.read_json("round-1-blocking.json"), [norm(EXAMPLE[0])])

        h3 = self.commit("tests/test_app.py", "def test_app():\n    assert 1\n")
        self.assertEqual(self.findings([])["round"], 3)
        self.commit("src/app.py", "def app():\n    return 6\n")
        self.assertEqual(self.findings(EXAMPLE)["round"], 4)
        self.assertEqual([r["round"] for r in self.load_rounds()], [1, 2, 3, 4])
        self.assertEqual(self.load_rounds()[2]["head"], h3)
        for k in (1, 2, 3, 4):
            self.assertTrue(os.path.exists(os.path.join(self.reviews_dir(), f"round-{k}-blocking.json")))
            self.assertTrue(os.path.exists(os.path.join(self.reviews_dir(), f"round-{k}-review.json")))

    def test_base_is_the_merge_base_then_the_previous_head(self):
        h1 = self.head()
        out = self.findings(EXAMPLE)
        self.assertEqual((out["base"], out["head"], out["review_base"]), (self.merge_base, h1, h1))
        h2 = self.commit("src/app.py", "def app():\n    return 7\n")
        out = self.findings(EXAMPLE)
        self.assertEqual((out["base"], out["head"], out["review_base"]), (h1, h2, h2))
        out = self.findings([])                      # same head: replaced, base still the previous round's head
        self.assertEqual((out["round"], out["base"], out["head"]), (2, h1, h2))
        h3 = self.commit("tests/test_app.py", "def test_app():\n    assert 2\n")
        out = self.findings(EXAMPLE)
        self.assertEqual((out["round"], out["base"], out["head"], out["review_base"]), (3, h2, h3, h3))
        self.assertEqual([(r["base"], r["head"]) for r in self.load_rounds()],
                         [(self.merge_base, h1), (h1, h2), (h2, h3)])

    def test_record_is_review_pass_then_fix_pass_or_fail(self):
        blocking = [{"path": "src/app.py", "line": 2, "severity": "medium", "body": "still wrong"}]
        only_low_and_outside = [{"path": "src/app.py", "line": 2, "severity": "low", "body": "nit"},
                                {"path": "README.md", "line": 1, "severity": "high", "body": "outside"}]
        steps = [(blocking, ("review", "pass")),             # round 1 is review pass even with blocking findings
                 (blocking, ("fix", "fail")),
                 (only_low_and_outside, ("fix", "pass")),
                 ([], ("fix", "pass")),
                 (EXAMPLE, ("fix", "fail"))]
        for k, (review, (stage, result)) in enumerate(steps, 1):
            with self.subTest(round=k):
                if k > 1:
                    self.commit("src/app.py", f"def app():\n    return {k + 20}\n")
                p = self.findings(review, as_json=False)
                b, low, outside = split(review)
                self.assertEqual(p.stdout.splitlines()[0],
                                 f"#12 review round {k}: {len(b)} blocking, {len(low)} low, {len(outside)} outside; "
                                 f"record: harness-work stage 12 {stage} {result}")
                out = self.findings(review)          # same head: replaces, same round and record
                self.assertEqual((out["round"], out["record"]), (k, {"stage": stage, "result": result}))


# =========================================================================== notes
class Notes(FindingsTest):
    def test_notes_gather_low_out_of_scope_and_rulings_once(self):
        r1 = "Some prose.\nRuling: x is a list.\n- Ruling: y is sorted.\nThe text mentions Ruling: not a line.\n"
        self.findings(EXAMPLE, rulings=r1)
        _a, b, c, d = map(norm, EXAMPLE)
        notes = sections(self.notes())
        self.assertEqual(list(notes), ["Low findings", "Out of scope", "Rulings"])
        self.assertEqual(sorted(notes["Low findings"]), sorted([entry(b), entry(d, outside=True)]))
        self.assertEqual(notes["Out of scope"], [entry(c, outside=True)])
        rulings = lambda: [re.sub(r"^- ", "", line) for line in sections(self.notes())["Rulings"]]
        self.assertEqual(rulings(), ["Ruling: x is a list.", "Ruling: y is sorted."])

        # Round 2 repeats B, C and D, adds a touched low, an outside high and an outside medium with no line.
        self.commit("src/app.py", "def app():\n    return 9\n")
        e = {"path": "tests/test_app.py", "line": 4, "severity": "nit", "body": "E"}
        f = {"path": "src/other.py", "line": 8, "severity": "critical", "body": "F"}
        g = {"path": "docs/guide.md", "line": 2, "severity": "major", "body": "G"}
        r2 = "## Rulings\n- Ruling: y is sorted.\nRuling: z is unique.\n"
        self.findings([EXAMPLE[2], e, EXAMPLE[1], f, EXAMPLE[3], g], rulings=r2)
        notes = sections(self.notes())
        self.assertEqual(sorted(notes["Low findings"]),
                         sorted([entry(b), entry(d, outside=True), entry(norm(e))]))
        self.assertEqual(sorted(notes["Out of scope"]),
                         sorted([entry(c, outside=True), entry(norm(f), outside=True),
                                 entry(norm(g), outside=True)]))
        self.assertEqual(rulings(), ["Ruling: x is a list.", "Ruling: y is sorted.", "Ruling: z is unique."])

        # A third call at the same head with no rulings file: nothing doubles, the rulings stay.
        self.findings([EXAMPLE[1], EXAMPLE[2]])
        notes = sections(self.notes())
        self.assertEqual(sorted(notes["Low findings"]), sorted([entry(b), entry(d, outside=True)]))
        self.assertEqual(notes["Out of scope"], [entry(c, outside=True)])
        self.assertEqual(rulings(), ["Ruling: x is a list.", "Ruling: y is sorted.", "Ruling: z is unique."])
        self.assertNotIn("`src/app.py:3` [high] A", self.notes())     # blocking findings stay out of the notes

    def test_notes_have_no_second_level_heading(self):
        # Empty sections are left out: only an outside medium finding gives only Out of scope.
        only_out = [{"path": "README.md", "line": 3, "severity": "medium", "body": "Only outside."}]
        self.findings(only_out)
        self.assertEqual(list(sections(self.notes())), ["Out of scope"])

        self.commit("src/app.py", "def app():\n    return 4\n")
        lows = [{"path": "src/app.py", "line": 2, "severity": "low", "body": "First line.\n## Details\nSecond line."},
                {"path": "README.md", "line": 1, "severity": "high", "body": "Outside.\n# Title\nmore"}]
        self.findings(lows, rulings="# Notes\n## Rulings\nRuling: keep headings out.\n")
        text = self.notes()
        self.assertIn("First line.", text)
        self.assertIn("Outside.", text)
        self.assertIn("Ruling: keep headings out.", text)
        bad = [line for line in text.splitlines() if re.match(r"^#{1,2}(\s|$)", line)]
        self.assertEqual(bad, [], text)
        self.assertEqual(set(sections(text)), {"Low findings", "Out of scope", "Rulings"})
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("### ")]), 3, text)


# =========================================================================== latest
class Latest(FindingsTest):
    def test_latest_prints_the_recorded_round_or_exits_one(self):
        before = self.snapshot()
        for args in (("--latest",), ("--latest", "--json")):
            p = self.pr("findings", "12", *args)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.stderr.strip(), "no review round recorded for #12")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.reviews_dir()))

        h1 = self.head()
        self.findings(EXAMPLE)
        h2 = self.commit("src/app.py", "def app():\n    return 8\n")
        second = [{"path": "tests/test_app.py", "line": 5, "severity": "high", "body": "round two"}]
        self.findings(second)
        rounds = self.load_rounds()
        before = self.snapshot()
        p = self.pr("findings", "12", "--latest", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        latest = json.loads(p.stdout)
        self.assertEqual(latest, rounds[-1])
        self.assertEqual(set(latest), {"round", "at", "head", "base", "blocking", "low", "outside", "pr",
                                       "review_id"})
        self.assertEqual((latest["round"], latest["head"], latest["base"], latest["pr"], latest["review_id"]),
                         (2, h2, h1, None, None))
        self.assertEqual(latest["blocking"], [norm(second[0])])
        p = self.pr("findings", "12", "--latest", cwd=self.wt)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip())
        self.assertEqual(self.snapshot(), before)
        # another ticket of the same repository has no round
        p = self.pr("findings", "13", "--latest")
        self.assertEqual((p.returncode, p.stderr.strip()), (1, "no review round recorded for #13"))

    def test_no_gh_call_is_made(self):
        self.findings(EXAMPLE, rulings="Ruling: one.\n")
        self.findings(EXAMPLE, as_json=False)
        self.commit("src/app.py", "def app():\n    return 11\n")
        self.findings([])
        self.pr("findings", "12", "--worktree", self.wt, "--review", self.review_file("nothing\n"))
        self.pr("findings", "12", "--latest")
        self.pr("findings", "12", "--latest", "--json")
        self.pr("findings", "13", "--latest")
        self.assertEqual(len(self.load_rounds()), 2)
        self.assertEqual(self.gh_calls(), [])
        if os.path.exists(self.order_log):
            with open(self.order_log, encoding="utf-8") as fh:
                self.assertEqual([line for line in fh if line.startswith("gh ")], [])


if __name__ == "__main__":
    unittest.main()
