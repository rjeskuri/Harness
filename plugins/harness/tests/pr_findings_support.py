"""Shared by test_pr_findings_post and test_pr_findings_brief (and the later findings modules): the fixtures, the
expectations derived by hand and the base class PostTest of the `harness-pr findings` posting tests. Not a test
module (no `test_` prefix): discovery and scripts/run_suite.py skip it. Each test module imports everything here,
`setUpModule` and `tearDownModule` included, so each still builds its fake executables once per process.

Fixture (the FindingsTest pattern of test_pr_findings.py): a temporary HOME, a bare origin, the main checkout
`proj` pushed to it, and the ticket worktree on t/12-x branched from origin/main. On main `src/app.py` has 30
lines `V<i> = <i>`; the branch rewrites lines 3 and 25 and adds the 4-line `tests/test_app.py`. After the branch
point main changes `README.md` and `lib/x.py`, so those are outside the ticket's files. The command runs from the
main checkout with `--worktree <wt>`.

gh is fakegh.py behind a wrapper. `gh pr list` answers the open PR (or none) with a `headRefOid` the test picks;
`gh pr diff P` answers the real `git diff <merge-base> HEAD` of the worktree, so the lines on the diff are known
from the fixture: every changed line and 3 lines of context either side (git's default). With a PR open (open_pr),
`gh api user` answers the owner's login and fakegh's REST world serves ticket 12's comments and the PR's (none unless
the test gives some), which the GPT check reads for the owner's answers (#514) and findings for the owner's
dismissals (#517). harness-board is a stub in the same fakebin:
`show N` prints the ticket JSON the test sets (its `parent` is the epic), `task create` reads its body from stdin,
logs it, and prints `{"number", ...}` with numbers 501, 508, 515, ... (step 7, so a guessed `+1` fails), or fails
for a title holding a string the test names. With the PR open, `GET pulls/P/reviews` lists the harness reviews the
test gives (none by default), which a GPT triage reads to reconcile an interrupted post (#534).

The two-step GPT check (#534): `findings N --head H [--review F] --gpt-check --json` stops at a triage result
naming the candidates file, `{"ticket", "head", "result": "triage", "candidates_file"}`; the file holds
`{"ticket", "head", "candidates": [{"id", "path", "line", "body"}], "prior": [{"id", "path", "line", "severity",
"body", "sol": "fixed"|"unresolved"}]}`. `findings N --head H --gpt-triage V --json` reads Opus's verdicts,
`{"candidates": [{"id", "verdict": "accept", "severity"} | {"id", "verdict": "set-aside", "reason"}], "prior":
[{"id", "verdict": "fixed"|"unresolved", "reason"?}]}`, and publishes and records as a --gpt-check call did before;
`opus_verdicts` builds such a file from a candidates file, and PostTest.triage runs the call.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

import fakegh  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-pr")
LIB = os.path.join(os.path.dirname(HERE), "lib")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")
MARKER = "<!-- harness-review -->"
BRANCH = "t/12-x"
OUTSIDE = "(outside the files this ticket changed)"
TOUCHED = {"src/app.py", "tests/test_app.py"}
ALIASES = {"critical": "high", "blocker": "high", "major": "medium", "minor": "low", "nit": "low", "info": "low"}
APP_LINES = 30
BRANCH_CHANGES = (3, 25)             # src/app.py lines the branch rewrites
TEST_LINES = 4                       # tests/test_app.py, new on the branch
CONTEXT = 3
REPO_VIEW = {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}}
VALUE_FLAGS = {"--proposed", "--json", "--no-tests", "--needs-local", "--priority"}
HEADINGS = ["Request", "Outline", "Definition of done", "Tests that must pass", "Context"]
OWNER = "rjeskuri"                   # what `gh api user --jq .login` answers by default

BOARD = r'''import json, os, select, sys
argv = sys.argv[1:]
def stdin_text():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        return sys.stdin.read() if ready else ""
    except Exception:
        return ""
text = stdin_text()
conf_path = os.environ["BOARD_CONF"]
with open(conf_path, encoding="utf-8") as fh:
    conf = json.load(fh)
entry = {"argv": argv, "stdin": text}
code, out, err = 0, "", ""
if argv[:1] == ["show"]:
    ticket = conf.get("show", {}).get(argv[1] if len(argv) > 1 else "")
    if ticket is None:
        code, err = 1, "harness-board: no such ticket\n"
    else:
        out = json.dumps(ticket)
elif argv[:2] == ["task", "create"]:
    title = argv[argv.index("--title") + 1] if "--title" in argv else ""
    if any(s in title for s in conf.get("fail_titles", [])):
        code, err = 1, "harness-board: gh issue create failed: HTTP 502 Bad Gateway\n"
        entry["failed"] = True
    else:
        counter = conf_path + ".next"
        n = conf.get("first", 501)
        if os.path.exists(counter):
            with open(counter, encoding="utf-8") as fh:
                n = int(fh.read())
        with open(counter, "w", encoding="utf-8") as fh:
            fh.write(str(n + conf.get("step", 7)))
        entry["number"] = n
        parent = int(argv[argv.index("--parent") + 1]) if "--parent" in argv else None
        linked = not any(s in title for s in conf.get("link_fail_titles", []))
        out = json.dumps({"number": n, "url": "https://github.com/o/r/issues/%d" % n, "parent": parent,
                          "linked": (True if linked else False) if parent else None, "blocked_by": []})
        if not linked:     # as the real harness-board: the issue exists, its JSON is printed, the exit is 1
            code = 1
            err = ("harness-board: created #%d (https://github.com/o/r/issues/%d) but linking it under #%s failed: "
                   "HTTP 502 Bad Gateway\n" % (n, n, parent))
            entry["link_failed"] = True
with open(os.environ["BOARD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")
with open(os.environ["ORDER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(["harness-board"] + argv) + "\n")
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
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
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-post-bin-"))
    py = sys.executable
    fakebin = os.path.join(root, "fakebin")
    board = write(os.path.join(root, "board.py"), BOARD)
    executable(os.path.join(fakebin, "gh"),
               f'#!/bin/sh\nprintf \'%s\\n\' "gh $*" >> "$ORDER_LOG"\nexec "{py}" "{FAKEGH}" "$@"\n')
    executable(os.path.join(fakebin, "git"),
               f'#!/bin/sh\nprintf \'%s\\n\' "git $*" >> "$ORDER_LOG"\nexec "{GIT}" "$@"\n')
    executable(os.path.join(fakebin, "harness-board"), f'#!/bin/sh\nexec "{py}" "{board}" "$@"\n')
    os.symlink(py, os.path.join(fakebin, "python3"))
    BINS.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


# --------------------------------------------------------------- expectations, derived here
def app_text(changes=()):
    """src/app.py: 30 lines `V<i> = <i>`, the lines in `changes` rewritten to `V<i> = <i * 10>`."""
    return "".join(f"V{i} = {i * 10 if i in changes else i}\n" for i in range(1, APP_LINES + 1))


def on_diff(app_changes):
    """(path, line) of every line `gh pr diff` shows: changed lines with 3 lines of context, and the new test."""
    found = set()
    for c in app_changes:
        found |= {("src/app.py", n) for n in range(max(1, c - CONTEXT), min(APP_LINES, c + CONTEXT) + 1)}
    return found | {("tests/test_app.py", n) for n in range(1, TEST_LINES + 1)}


def norm(f):
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


def ticketable(findings, touched=TOUCHED):
    """The outside findings that become tickets: high or medium."""
    return [f for f in split(findings, touched)[2] if f["severity"] in ("high", "medium")]


def loc(f):
    return f"{f['path']}:{f['line']}" if f["line"] else f["path"]


def entry(f, outside=False):
    """A notes (or review body) line; the body on one line, as #256's notes keep it."""
    return f"- `{loc(f)}` [{f['severity']}] {' '.join(f['body'].split())}" + (f" {OUTSIDE}" if outside else "")


def counts_line(model, findings):
    n = len(findings)
    c = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}
    return (f"Harness review ({model}): {n} finding{'' if n == 1 else 's'}, {c['high']} high, "
            f"{c['medium']} medium, {c['low']} low.")


def comment(f):
    return {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": f"[{f['severity']}] {f['body']}\n\n{MARKER}"}


def by_place(comments):
    return sorted(comments, key=lambda c: (c["path"], c["line"], c["body"]))


def sections(text, prefix="### "):
    """{heading: [non-empty lines]} for the sections of a markdown text at one heading level."""
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith(prefix):
            current = line[len(prefix):].strip()
            out.setdefault(current, [])
        elif current is not None and line.strip():
            out[current].append(line.rstrip())
    return out


def create_options(argv):
    """{option: value} of a `task create` argv (flags map to True), whatever the order."""
    opts, i = {}, 2
    while i < len(argv):
        name = argv[i]
        if name in VALUE_FLAGS:
            opts[name[2:]] = True
        elif name.startswith("--"):
            opts[name[2:]] = argv[i + 1] if i + 1 < len(argv) else None
            i += 1
        i += 1
    return opts


def first_sentence(body):
    text = " ".join(body.split())
    for i, ch in enumerate(text):
        if ch in ".!?" and (i + 1 == len(text) or text[i + 1] == " "):
            return text[:i]
    return text


SET_ASIDE = "Not a problem on any path the repository runs."


def unrated(findings):
    """Sol's findings as harness-gpt check prints them since #534: path, line and body, no severity."""
    return [{k: f[k] for k in ("path", "line", "body") if k in f} for f in findings]


def opus_verdicts(cands, accept=None, prior=None, reason=SET_ASIDE):
    """Opus's verdicts file (#534) for a candidates file: each candidate whose body `accept` maps to a severity
    accepted at it, every other one set aside with `reason`; each prior blocker whose body `prior` maps to
    (verdict, reason) judged so, every other one as Sol called it, with no reason."""
    accept, prior = accept or {}, prior or {}
    out = {"candidates": [], "prior": []}
    for c in cands.get("candidates") or []:
        severity = accept.get(c["body"])
        out["candidates"].append({"id": c["id"], "verdict": "accept", "severity": severity} if severity else
                                 {"id": c["id"], "verdict": "set-aside", "reason": reason})
    for b in cands.get("prior") or []:
        verdict, why = prior.get(b["body"], (b["sol"], None))
        out["prior"].append(dict({"id": b["id"], "verdict": verdict}, **({"reason": why} if why else {})))
    return out


def epic_ticket(number, epic):
    parent = {"number": epic, "title": "The epic", "goal": "A goal."} if epic else None
    return {"number": number, "title": "The ticket", "url": f"https://github.com/o/r/issues/{number}",
            "type": "task", "status": "in-progress", "size": "M", "risk": "low", "flags": [], "parent": parent,
            "blocked_by": [], "labels": ["task", "status:in-progress", "size:M", "risk:low"], "body": "...",
            "comments": [], "sections": {}}


class PostTest(unittest.TestCase):
    EPIC = 249

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-pr-post-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.order_log = os.path.join(self.tmp, "order.log")
        self.board_log = os.path.join(self.tmp, "board.log")
        self.board_conf_path = os.path.join(self.tmp, "board.json")
        self.n = 0
        self.board_conf()
        self.no_pr()
        self.make_repos()

    def make_repos(self):
        self.origin = self.mk("origin.git")
        self.git(self.origin, "init", "-q", "--bare", "-b", "main")
        self.main = self.mk("proj")
        self.git(self.main, "init", "-q", "-b", "main")
        write(os.path.join(self.main, "README.md"), "# proj\n")
        write(os.path.join(self.main, "src", "app.py"), app_text())
        write(os.path.join(self.main, "src", "other.py"), "X = 1\n")
        write(os.path.join(self.main, "lib", "x.py"), "Y = 1\nZ = 1\n")
        write(os.path.join(self.main, ".git", "info", "exclude"), ".claude/worktrees/\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "remote", "add", "origin", self.origin)
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "t-12-x")
        self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, self.wt, "origin/main")
        write(os.path.join(self.wt, "src", "app.py"), app_text(BRANCH_CHANGES))
        write(os.path.join(self.wt, "tests", "test_app.py"), "".join(f"# test line {i}\n"
                                                                     for i in range(1, TEST_LINES + 1)))
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "Change the app")
        write(os.path.join(self.main, "README.md"), "# proj\n\nMore.\n")
        write(os.path.join(self.main, "lib", "x.py"), "Y = 2\nZ = 1\n")
        self.git(self.main, "commit", "-q", "-am", "Main moves on")
        self.git(self.main, "push", "-q", "origin", "main")
        self.merge_base = self.git(self.wt, "merge-base", "HEAD", "origin/main")

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
                   FAKEGH_SCENARIO=self.scenario_path, FAKEGH_LOG=self.gh_log, ORDER_LOG=self.order_log,
                   BOARD_LOG=self.board_log, BOARD_CONF=self.board_conf_path)
        return env

    def git(self, repo, *args):
        p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def head(self, wt=None):
        return self.git(wt or self.wt, "rev-parse", "HEAD")

    def commit(self, name, text, wt=None):
        wt = wt or self.wt
        write(os.path.join(wt, name), text)
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-q", "-m", f"Change {name}")
        return self.head(wt)

    def branch_diff(self, wt=None):
        """What `gh pr diff` shows for the branch: the real diff from the merge-base to HEAD."""
        wt = wt or self.wt
        base = self.git(wt, "merge-base", "HEAD", "origin/main")
        p = subprocess.run([GIT, "-C", wt, "diff", base, "HEAD"], env=self.env(), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def scenario(self, calls):
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)

    def no_pr(self):
        self.scenario([REPO_VIEW, {"contains": ["pr", "list"], "stdout_json": []}])

    def open_pr(self, number, head=None, review_id=None, wt=None, comments=(), login=OWNER, calls=(), pr_comments=(),
                reviews=()):
        """An open PR #number for the branch at `head` (default: the worktree's HEAD). `gh api user` answers `login`
        and the REST world holds ticket 12 with `comments` and the PR itself (an issue with pull_request) with
        `pr_comments` (each oldest first, in Issues.comments' shape), which the owner's dismissals are read from
        (#517), and `GET pulls/<number>/reviews` lists `reviews`; `calls` go first, so a test can answer any read its
        own way or add issues of its own."""
        head = head or self.head(wt)
        self.scenario(list(calls) + [
            REPO_VIEW,
            {"contains": ["pr", "list"], "stdout_json": [
                {"number": number, "url": f"https://github.com/o/r/pull/{number}", "headRefOid": head}]},
            {"match": ["pr", "diff", str(number)], "stdout": self.branch_diff(wt)},
            {"match": ["api", "-X", "POST", f"repos/o/r/pulls/{number}/reviews"],
             "stdout_json": {"id": review_id, "node_id": "PRR_kwDO", "state": "COMMENTED"}},
            {"match": ["pr", "review", str(number)], "stdout": ""},
            {"match": ["api", "user"], "stdout": f"{login}\n"},
            {"issue": 12, "data": {"number": 12, "title": "The ticket", "comments": list(comments)}},
            {"issue": number, "data": {"number": number, "title": "The PR", "pull_request": True,
                                       "url": f"https://github.com/o/r/pull/{number}",
                                       "comments": list(pr_comments)}},
            fakegh.rest_entry(f"pulls/{number}/reviews", "GET", exact=True, stdout_json=list(reviews))])

    def board_conf(self, show=None, fail_titles=(), link_fail_titles=()):
        if show is None:
            show = {"12": epic_ticket(12, self.EPIC)}
        write(self.board_conf_path, json.dumps({"show": show, "fail_titles": list(fail_titles),
                                                "link_fail_titles": list(link_fail_titles),
                                                "first": 501, "step": 7}))

    def review_file(self, content):
        self.n += 1
        text = content if isinstance(content, str) else json.dumps(content, indent=1) + "\n\nVerdict: see above.\n"
        return write(os.path.join(self.tmp, "reviews-in", f"review-{self.n}.txt"), text)

    def pr(self, *args, cwd=None):
        return subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.main, env=self.env(),
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def run_findings(self, review, ticket="12", wt=None, model=None):
        args = ["findings", ticket, "--worktree", wt or self.wt, "--review", self.review_file(review), "--json"]
        if model:
            args += ["--model", model]
        return self.pr(*args)

    def findings(self, review, **kw):
        p = self.run_findings(review, **kw)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout), p

    # ----------------------------------------------------------- the GPT triage (#534)
    def candidates(self, out):
        """The candidates file a `--gpt-check` triage result names, read."""
        self.assertEqual(out.get("result"), "triage", out)
        path = out.get("candidates_file")
        self.assertTrue(isinstance(path, str) and os.path.isabs(path) and os.path.isfile(path), out)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def triage(self, verdicts, head=None, ticket="12", wt=None):
        """`findings 12 --head H --gpt-triage V --json` with Opus's `verdicts` written to V."""
        self.n += 1
        path = write(os.path.join(self.tmp, "verdicts-in", f"verdicts-{self.n}.json"), json.dumps(verdicts))
        return self.pr("findings", ticket, "--worktree", wt or self.wt, "--head", head or self.head(wt),
                       "--gpt-triage", path, "--json")

    def triaged(self, verdicts, **kw):
        p = self.triage(verdicts, **kw)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout), p

    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def gh_calls(self):
        return self.jsonl(self.gh_log)

    def posts(self):
        return [c for c in self.gh_calls() if c["argv"][:3] == ["api", "-X", "POST"]]

    def comment_reviews(self):
        return [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "review"]]

    def creates(self):
        return [c for c in self.jsonl(self.board_log) if c["argv"][:2] == ["task", "create"]]

    def created(self):
        """(title, number) of each successful create."""
        return [(create_options(c["argv"]).get("title"), c.get("number")) for c in self.creates()
                if not c.get("failed")]

    def key(self):
        return hashlib.sha1(os.path.realpath(self.main).encode()).hexdigest()[:12]

    def reviews_dir(self, ticket=12):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key(), "reviews", str(ticket))

    def notes(self, ticket=12):
        with open(os.path.join(self.reviews_dir(ticket), "notes.md"), encoding="utf-8") as fh:
            return fh.read()

    def snapshot(self):
        """Every file under ~/.claude with its content, except harness/gh/: harness_lib.gh logs one line per gh
        call there (ghlimit's contract), and the refusal needs its `gh pr list`."""
        root = os.path.join(self.home, ".claude")
        gh_log = os.path.join(root, "harness", "gh")
        out = {}
        for folder, _dirs, files in os.walk(root):
            if folder == gh_log or folder.startswith(gh_log + os.sep):
                continue
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, root)] = fh.read()
        return out

    def load_rounds(self, ticket=12):
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        import importlib
        importlib.invalidate_caches()
        reviews = importlib.import_module("harness_lib.reviews")
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        try:
            return reviews.load_rounds(self.key(), ticket)
        finally:
            os.environ["HOME"] = old

    def number_for(self, f):
        """The number of the one successful create whose title names this finding (path and first sentence)."""
        hits = [n for title, n in self.created() if self.title_names(title, f)]
        self.assertEqual(len(hits), 1, f"creates for {f}: {self.created()}")
        return hits[0]

    def title_names(self, title, f):
        prefix = f"{f['path']}: "
        if not title or not title.startswith(prefix):
            return False
        part = title[len(prefix):]
        sentence = first_sentence(f["body"])
        if part in (sentence, sentence + "."):
            return True
        cut = part.rstrip("…").rstrip(".").rstrip()
        return len(part) <= 60 and len(sentence) > 60 and len(cut) >= 30 and sentence.startswith(cut)

    def assertTicketCreate(self, c, f, epic, pr, ticket=12):
        """One `task create` call for finding f: the flags, the title and the body in the task template."""
        opts = create_options(c["argv"])
        self.assertEqual(c["argv"][:2], ["task", "create"])
        self.assertEqual(opts.get("parent"), str(epic) if epic else None, c["argv"])
        self.assertIs(opts.get("proposed"), True, c["argv"])
        self.assertIs(opts.get("json"), True, c["argv"])
        self.assertEqual((opts.get("type"), opts.get("size"), opts.get("risk"), opts.get("body-file")),
                         ("bug", "S", "low", "-"), c["argv"])
        self.assertEqual(os.path.realpath(opts.get("repo") or ""), os.path.realpath(self.main), c["argv"])
        title = opts.get("title")
        self.assertNotIn("\n", title)
        self.assertTrue(self.title_names(title, f), f"title {title!r} for {f}")
        body = c["stdin"]
        heads = [line[3:].strip() for line in body.splitlines() if line.startswith("## ")]
        self.assertEqual(heads, HEADINGS, body)
        parts = sections(body, "## ")
        request = "\n".join(parts["Request"])
        self.assertIn(f["body"].splitlines()[0], request)
        self.assertIn(f"found in the review of PR #{pr} for #{ticket}", request)
        for name in ("Outline", "Tests that must pass"):
            self.assertTrue(parts[name], f"{name} is empty:\n{body}")
        self.assertTrue(any(line.lstrip().startswith("- [ ]") for line in parts["Definition of done"]), body)
        context = "\n".join(parts["Context"])
        self.assertIn(loc(f), context)
        self.assertIn(f["severity"], context)
