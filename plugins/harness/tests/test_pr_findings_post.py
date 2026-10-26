"""Tests for `harness-pr findings` posting the review and ticketing out-of-scope findings (ticket #257, epic #249).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_post.py -v

Fixture (the FindingsTest pattern of test_pr_findings.py): a temporary HOME, a bare origin, the main checkout
`proj` pushed to it, and the ticket worktree on t/12-x branched from origin/main. On main `src/app.py` has 30
lines `V<i> = <i>`; the branch rewrites lines 3 and 25 and adds the 4-line `tests/test_app.py`. After the branch
point main changes `README.md` and `lib/x.py`, so those are outside the ticket's files. The command runs from the
main checkout with `--worktree <wt>`.

gh is fakegh.py behind a wrapper. `gh pr list` answers the open PR (or none) with a `headRefOid` the test picks;
`gh pr diff P` answers the real `git diff <merge-base> HEAD` of the worktree, so the lines on the diff are known
from the fixture: every changed line and 3 lines of context either side (git's default). harness-board is a stub
in the same fakebin: `show N` prints the ticket JSON the test sets (its `parent` is the epic), `task create` reads
its body from stdin, logs it, and prints `{"number", ...}` with numbers 501, 508, 515, ... (step 7, so a guessed
`+1` fails), or fails for a title holding a string the test names.

Expected values are derived here: the split from the touched set and the documented severity table, the inline
set from the changed lines and the context rule, the counts line from the findings, and the notes lines in the
#256 form.

Choices the ticket left open, fixed here (each a `Ruling:` for the PR):
- a refusal (the PR head differs from HEAD) exits 1;
- the no-findings post is byte-equal to what `harness-pr review P --findings <[]>` posts with its default --checked;
- the notes line of a ticketed finding is the #256 line followed by ` -> #<n>`;
- the ticket keys are kept outside the round entries (test_pr_findings' --latest key set is unchanged);
- a failed create warns with one `harness-pr: warning:` line naming the finding's path, and records no key, so the
  next round tries that finding again;
- the title's sentence part is the body's first sentence with or without its closing period; a longer one is cut
  to at most 60 characters, a prefix of the sentence (a trailing ellipsis allowed);
- the ticket body's Context names `path:line` (the path alone without a line) and the normalised severity.
Left open (not pinned): whether out-of-scope tickets are made when no PR is open; `review_id` after a no-findings
post; whether `tickets` in --json repeats the numbers of earlier calls.
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

    def open_pr(self, number, head=None, review_id=None, wt=None):
        """An open PR #number for the branch at `head` (default: the worktree's HEAD)."""
        head = head or self.head(wt)
        self.scenario([
            REPO_VIEW,
            {"contains": ["pr", "list"], "stdout_json": [
                {"number": number, "url": f"https://github.com/o/r/pull/{number}", "headRefOid": head}]},
            {"match": ["pr", "diff", str(number)], "stdout": self.branch_diff(wt)},
            {"match": ["api", "-X", "POST", f"repos/o/r/pulls/{number}/reviews"],
             "stdout_json": {"id": review_id, "node_id": "PRR_kwDO", "state": "COMMENTED"}},
            {"match": ["pr", "review", str(number)], "stdout": ""}])

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


# =========================================================================== posting
POST_R1 = [
    {"path": "src/app.py", "line": 3, "severity": "high", "body": "V3 is scaled twice."},
    {"path": "src/app.py", "line": 6, "severity": "medium", "body": "The context line six reads stale state."},
    {"path": "src/app.py", "line": 12, "severity": "high", "body": "Line twelve is off the diff."},
    {"path": "src/app.py", "line": 28, "severity": "minor", "body": "Trailing context at 28."},
    {"path": "src/app.py", "line": 29, "severity": "low", "body": "Just past the hunk at 29."},
    {"path": "src/app.py", "severity": "major", "body": "No line given for this one."},
    {"path": "tests/test_app.py", "line": 2, "severity": "critical", "body": "The test asserts nothing."},
    {"path": "tests/test_app.py", "line": 4, "severity": "nit", "body": "Name the test better."},
    {"path": "README.md", "line": 1, "severity": "high", "body": "The README names the wrong flag."},
    {"path": "lib/x.py", "line": 2, "severity": "low", "body": "Z could be a constant."},
    {"path": "src/other.py", "line": 1, "severity": "medium", "body": "X is never read."},
]


class Post(PostTest):
    EPIC = 87

    def assertPosted(self, sent, findings, model, app_changes):
        """The POST payload for one round, derived: inline comments for the review findings on the diff, the rest
        listed in the body, outside findings nowhere."""
        blocking, low, outside = split(findings)
        review = blocking + low
        lines = on_diff(app_changes)
        inline = [f for f in review if f["line"] and (f["path"], f["line"]) in lines]
        off = [f for f in review if f not in inline]
        self.assertTrue(inline and off, "the fixture must exercise both routes")
        self.assertEqual(sent["event"], "COMMENT")
        self.assertEqual(by_place(sent["comments"]), by_place([comment(f) for f in inline]))
        body_lines = sent["body"].splitlines()
        self.assertEqual(body_lines[0], counts_line(model, review))
        self.assertEqual(body_lines[-1], MARKER)
        self.assertIn("Not on a changed line:", body_lines)
        listed = [line for line in body_lines if line.startswith("- ")]
        self.assertEqual(sorted(listed), sorted(entry(f) for f in off))
        for f in outside:
            self.assertNotIn(f["body"], sent["body"])
            self.assertFalse(any(f["body"] in c["body"] for c in sent["comments"]))

    def test_findings_posts_blocking_and_low_as_one_harness_review(self):
        self.board_conf(show={"12": epic_ticket(12, self.EPIC)})
        head = self.head()
        self.open_pr(34, review_id=7300112)
        out, _p = self.findings(POST_R1, model="sonnet")
        self.assertEqual(len(self.posts()), 1, self.gh_calls())
        self.assertEqual(self.comment_reviews(), [])
        post = self.posts()[0]
        self.assertEqual(post["argv"], ["api", "-X", "POST", "repos/o/r/pulls/34/reviews", "--input", "-"])
        sent = json.loads(post["stdin"])
        self.assertPosted(sent, POST_R1, "sonnet", BRANCH_CHANGES)
        self.assertTrue(any(c["argv"][:2] == ["pr", "list"] and "--head" in c["argv"]
                            and c["argv"][c["argv"].index("--head") + 1] == BRANCH for c in self.gh_calls()))
        self.assertEqual((out["round"], out["head"], out["pr"], out["review_id"]), (1, head, 34, 7300112))
        rounds = self.load_rounds()
        self.assertEqual([(r["round"], r["pr"], r["review_id"]) for r in rounds], [(1, 34, 7300112)])

        # Exactly what `harness-pr review` posts for round-1-review.json with the same model.
        review_json = os.path.join(self.reviews_dir(), "round-1-review.json")
        p = self.pr("review", "34", "--findings", review_json, "--model", "sonnet")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(json.loads(self.posts()[1]["stdin"]), sent)

        # The two outside high/medium findings were ticketed under the epic (the OutOfScope tests go further).
        tickets = ticketable(POST_R1)
        self.assertEqual(len(self.creates()), len(tickets))
        self.assertEqual(sorted(out["tickets"]), sorted(self.number_for(f) for f in tickets))

        # Round 2: a new commit, the PR moved to it, the default model, another review id.
        changes = BRANCH_CHANGES + (14,)
        head2 = self.commit("src/app.py", app_text(changes))
        self.open_pr(34, review_id=7300999)
        r2 = [{"path": "src/app.py", "line": 14, "severity": "high", "body": "Fourteen is now on the diff."},
              {"path": "src/app.py", "line": 9, "severity": "medium", "body": "Nine sits between the hunks."}]
        before = len(self.posts())
        out, _p = self.findings(r2)
        self.assertEqual(len(self.posts()), before + 1)
        self.assertPosted(json.loads(self.posts()[-1]["stdin"]), r2, "opus", changes)
        self.assertEqual((out["round"], out["head"], out["pr"], out["review_id"]), (2, head2, 34, 7300999))
        self.assertEqual([(r["round"], r["pr"], r["review_id"]) for r in self.load_rounds()],
                         [(1, 34, 7300112), (2, 34, 7300999)])

    def test_empty_round_posts_no_findings(self):
        # What `harness-pr review` posts for an empty list, as the reference.
        empty = write(os.path.join(self.tmp, "empty.json"), "[]")
        self.open_pr(52)
        p = self.pr("review", "52", "--findings", empty)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        reference = self.comment_reviews()[0]["argv"]
        self.assertEqual(reference[:5], ["pr", "review", "52", "--comment", "-b"])
        self.assertTrue(reference[5].startswith("Harness review: no findings. Checked: "), reference)
        self.assertTrue(reference[5].endswith(MARKER), reference)

        cases = [("[]", "[]\n\nVerdict: approve.\n"),
                 # outside findings only: round-<k>-review.json is [] and the post is the same
                 ("only outside", [{"path": "README.md", "line": 1, "severity": "low", "body": "Outside low."},
                                   {"path": "lib/x.py", "line": 2, "severity": "medium", "body": "Outside mid."}])]
        for k, (name, review) in enumerate(cases, 1):
            with self.subTest(case=name):
                if k > 1:
                    self.commit("src/app.py", app_text(BRANCH_CHANGES + (10 + k,)))
                    self.open_pr(52)
                calls = len(self.comment_reviews())
                out, _p = self.findings(review)
                self.assertEqual(out["round"], k)
                self.assertEqual(self.posts(), [])
                self.assertEqual(len(self.comment_reviews()), calls + 1)
                self.assertEqual(self.comment_reviews()[-1]["argv"], reference)
                with open(os.path.join(self.reviews_dir(), f"round-{k}-review.json"), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh), [])
                self.assertEqual(out["pr"], 52)
                self.assertEqual(self.load_rounds()[-1]["pr"], 52)

    def test_pr_head_behind_the_worktree_is_refused_and_records_nothing(self):
        old = self.head()
        findings = [{"path": "src/app.py", "line": 3, "severity": "high", "body": "Blocking."},
                    {"path": "README.md", "line": 1, "severity": "high", "body": "Outside and ticketable."}]
        for pr_number, pr_head in ((34, None), (57, "f" * 40)):
            with self.subTest(pr=pr_number):
                head = self.commit("src/app.py", app_text(BRANCH_CHANGES + (pr_number % 7 + 10,)))
                pr_head = pr_head or old                   # behind: the commit before HEAD; else an unrelated sha
                self.open_pr(pr_number, head=pr_head)
                before, posts, creates = self.snapshot(), len(self.posts()), len(self.creates())
                p = self.run_findings(findings)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn(f"PR #{pr_number} is at {pr_head[:7]}, the worktree at {head[:7]}; push first",
                              p.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.posts()), posts)
                self.assertEqual(self.comment_reviews(), [])
                self.assertEqual(len(self.creates()), creates)
                old = head

        # After a recorded round: the next commit not pushed yet is refused, the round stays as it was.
        self.open_pr(34)
        out, _p = self.findings(findings)
        self.assertEqual(out["round"], 1)
        rounds = self.load_rounds()
        pushed = self.head()
        head = self.commit("tests/test_app.py", "# rewritten\n")
        self.open_pr(34, head=pushed)
        before, posts, creates = self.snapshot(), len(self.posts()), len(self.creates())
        p = self.run_findings(findings)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"PR #34 is at {pushed[:7]}, the worktree at {head[:7]}; push first", p.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.load_rounds(), rounds)
        self.assertEqual((len(self.posts()), len(self.creates())), (posts, creates))

    def test_without_an_open_pr_it_records_and_warns(self):
        # A second ticket worktree on another branch, so the branch and the ticket in the warning vary.
        wt2 = os.path.join(self.main, ".claude", "worktrees", "t-31-y")
        self.git(self.main, "fetch", "-q", "origin")
        self.git(self.main, "worktree", "add", "-q", "-b", "t/31-y", wt2, "origin/main")
        self.commit("src/other.py", "X = 2\n", wt=wt2)
        self.board_conf(show={"12": epic_ticket(12, self.EPIC), "31": epic_ticket(31, None)})
        cases = [("12", self.wt, BRANCH,
                  [{"path": "src/app.py", "line": 3, "severity": "high", "body": "Blocking here."},
                   {"path": "tests/test_app.py", "line": 1, "severity": "low", "body": "Low here."},
                   {"path": "lib/x.py", "line": 1, "severity": "low", "body": "Outside low."}]),
                 ("31", wt2, "t/31-y",
                  [{"path": "src/other.py", "line": 1, "severity": "medium", "body": "Blocking there."}])]
        for ticket, wt, branch, review in cases:
            with self.subTest(ticket=ticket):
                self.no_pr()
                out, p = self.findings(review, ticket=ticket, wt=wt)
                self.assertIn(f"no open PR for {branch}; nothing posted (harness-pr open {ticket} first)", p.stderr)
                self.assertTrue(any(c["argv"][:2] == ["pr", "list"] and "--head" in c["argv"]
                                    and c["argv"][c["argv"].index("--head") + 1] == branch
                                    and "--state" in c["argv"]
                                    and c["argv"][c["argv"].index("--state") + 1] == "open"
                                    for c in self.gh_calls()), self.gh_calls())
                self.assertFalse(any(c["argv"][:2] in (["pr", "diff"], ["pr", "review"])
                                     or c["argv"][:3] == ["api", "-X", "POST"] for c in self.gh_calls()))
                blocking, low, _outside = split(review, touched={"src/other.py"} if ticket == "31" else TOUCHED)
                self.assertEqual((out["round"], len(out["blocking"]), len(out["low"])), (1, len(blocking), len(low)))
                self.assertEqual((out["pr"], out["review_id"]), (None, None))
                rounds = self.load_rounds(int(ticket))
                self.assertEqual([(r["round"], r["head"], r["pr"], r["review_id"]) for r in rounds],
                                 [(1, self.head(wt), None, None)])
                self.assertTrue(os.path.exists(os.path.join(self.reviews_dir(int(ticket)), "round-1-review.json")))


# =========================================================================== out of scope
LONG = ("This helper silently drops every record whose timestamp falls exactly on a leap second boundary. "
        "Fix the comparison.")
OOS_R1 = [
    {"path": "README.md", "line": 1, "severity": "high", "body": "The README claims a flag that does not exist."},
    {"path": "src/other.py", "line": 1, "severity": "medium", "body": "Other is never imported.\nRemove it."},
    {"path": "lib/x.py", "line": 2, "severity": "critical", "body": LONG},
    {"path": "docs/guide.md", "severity": "major", "body": "The guide skips the install step"},
    {"path": "lib/x.py", "line": 1, "severity": "low", "body": "Y wants a docstring."},
    {"path": "src/app.py", "line": 3, "severity": "high", "body": "Blocking on the ticket's file."},
    {"path": "src/app.py", "line": 4, "severity": "low", "body": "Low on the ticket's file."},
]


class OutOfScope(PostTest):
    def assertOutOfScopeNotes(self, numbered, plain=(), ticket=12):
        """notes.md's Out of scope section: each (finding, number) with ` -> #n`, each plain finding without."""
        expected = [entry(f, True) + f" -> #{n}" for f, n in numbered] + [entry(f, True) for f in plain]
        self.assertEqual(sorted(sections(self.notes(ticket)).get("Out of scope", [])), sorted(set(expected)),
                         self.notes(ticket))

    def test_high_and_medium_outside_findings_become_proposed_tickets_once(self):
        self.open_pr(41, review_id=88)
        out, _p = self.findings(OOS_R1)
        tickets = ticketable(OOS_R1)
        self.assertEqual(len(tickets), 4)
        creates = self.creates()
        self.assertEqual(len(creates), 4, [c["argv"] for c in creates])
        for f in tickets:
            mine = [c for c in creates if self.title_names(create_options(c["argv"]).get("title"), f)]
            self.assertEqual(len(mine), 1, f)
            self.assertTicketCreate(mine[0], f, self.EPIC, 41)
        numbers = {f["body"]: self.number_for(f) for f in tickets}
        self.assertEqual(len(set(numbers.values())), 4)
        self.assertEqual(sorted(out["tickets"]), sorted(numbers.values()))
        self.assertOutOfScopeNotes([(f, numbers[f["body"]]) for f in tickets])
        lows = sections(self.notes())["Low findings"]
        self.assertIn(entry(norm(OOS_R1[4]), True), lows)          # the outside low: in the notes, no ticket
        self.assertFalse(any("->" in line for line in lows), lows)

        # Round 2: the README finding again (same path and body, another line), another body on src/other.py.
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (17,)))
        self.open_pr(41, review_id=89)
        again = dict(OOS_R1[0], line=3)
        fresh = {"path": "src/other.py", "line": 1, "severity": "high", "body": "Other shadows a builtin."}
        r2 = [again, fresh, OOS_R1[3]]
        out, _p = self.findings(r2)
        self.assertEqual(len(self.creates()), 5, [c["argv"] for c in self.creates()])
        self.assertTicketCreate(self.creates()[-1], norm(fresh), self.EPIC, 41)
        n_fresh = self.number_for(norm(fresh))
        self.assertNotIn(n_fresh, numbers.values())
        self.assertIn(n_fresh, out["tickets"])
        numbered = [(f, numbers[f["body"]]) for f in tickets] + [(norm(again), numbers[again["body"]]),
                                                                 (norm(fresh), n_fresh)]
        self.assertOutOfScopeNotes(numbered)

        # Round 2 again at the same HEAD (it replaces the round): nothing new is created, the notes keep the numbers.
        notes = self.notes()
        self.findings(r2)
        self.assertEqual(len(self.creates()), 5)
        self.assertEqual(self.notes(), notes)

    def test_a_ticket_without_an_epic_gets_tickets_without_a_parent(self):
        self.board_conf(show={"12": epic_ticket(12, None)})
        self.open_pr(44, review_id=1)
        f = {"path": "README.md", "line": 1, "severity": "medium", "body": "No epic above this ticket."}
        out, _p = self.findings([f])
        self.assertEqual(len(self.creates()), 1)
        self.assertNotIn("--parent", self.creates()[0]["argv"])
        self.assertTicketCreate(self.creates()[0], norm(f), None, 44)
        self.assertEqual(out["tickets"], [self.number_for(norm(f))])

    def test_low_outside_findings_never_become_tickets(self):
        lows = [{"path": "README.md", "line": 1, "severity": "low", "body": "Readme typo."},
                {"path": "lib/x.py", "line": 2, "severity": "minor", "body": "Minor outside."},
                {"path": "src/other.py", "line": 1, "severity": "nit", "body": "Nit outside."},
                {"path": "docs/guide.md", "severity": "info", "body": "Info outside."},
                {"path": "lib/x.py", "line": 1, "severity": "whatever", "body": "Unknown is low."}]
        touched = [{"path": "src/app.py", "line": 3, "severity": "high", "body": "Blocking."},
                   {"path": "tests/test_app.py", "line": 2, "severity": "low", "body": "Touched low."}]
        self.open_pr(45, review_id=2)
        out, _p = self.findings(lows + touched)
        self.assertEqual(self.creates(), [])
        self.assertEqual(out.get("tickets"), [])
        notes = sections(self.notes())
        self.assertEqual(sorted(line for line in notes["Low findings"] if OUTSIDE in line),
                         sorted(entry(norm(f), True) for f in lows))
        self.assertNotIn("Out of scope", notes)
        self.assertFalse("->" in self.notes(), self.notes())

        # Round 2: the same lows again, one more low, and one high outside: only the high becomes a ticket, and a
        # finding that was low in round 1 is not "already ticketed" when it comes back as major.
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (19,)))
        self.open_pr(45, review_id=3)
        promoted = dict(lows[0], severity="major")
        high = {"path": "lib/x.py", "line": 2, "severity": "high", "body": "Z is read before it is set."}
        out, _p = self.findings(lows[1:] + [{"path": "README.md", "line": 2, "severity": "low", "body": "More."},
                                            promoted, high])
        self.assertEqual(len(self.creates()), 2, [c["argv"] for c in self.creates()])
        for f in (promoted, high):
            self.assertTicketCreate(next(c for c in self.creates()
                                         if self.title_names(create_options(c["argv"]).get("title"), norm(f))),
                                    norm(f), self.EPIC, 45)
        self.assertEqual(sorted(out["tickets"]), sorted(self.number_for(norm(f)) for f in (promoted, high)))
        self.assertOutOfScopeNotes([(norm(f), self.number_for(norm(f))) for f in (promoted, high)])
        # the lows stay unnumbered (round 1's README low shares the promoted finding's key, so it is not pinned)
        low_lines = sections(self.notes())["Low findings"]
        for f in lows[1:] + [{"path": "README.md", "line": 2, "severity": "low", "body": "More."}]:
            self.assertIn(entry(norm(f), True), low_lines)

    def test_a_failed_create_warns_and_the_posting_goes_on(self):
        self.board_conf(fail_titles=["README.md"])
        self.open_pr(63, review_id=6301)
        bad = {"path": "README.md", "line": 1, "severity": "high", "body": "The README lists a removed command."}
        good = {"path": "src/other.py", "line": 1, "severity": "medium", "body": "Other is dead code."}
        on_file = {"path": "src/app.py", "line": 3, "severity": "high", "body": "Blocking and on the diff."}
        out, p = self.findings([bad, good, on_file])           # the failing one first: the loop must go on
        warnings = [line for line in p.stderr.splitlines() if line.startswith("harness-pr: warning:")]
        self.assertTrue(any("README.md" in line for line in warnings), p.stderr)
        self.assertEqual(len(self.creates()), 2, [c["argv"] for c in self.creates()])
        n_good = self.number_for(norm(good))
        self.assertEqual(out["tickets"], [n_good])
        self.assertEqual(len(self.posts()), 1)
        sent = json.loads(self.posts()[0]["stdin"])
        self.assertEqual(sent["comments"], [comment(norm(on_file))])
        self.assertEqual((out["round"], out["pr"], out["review_id"]), (1, 63, 6301))
        self.assertEqual([(r["pr"], r["review_id"]) for r in self.load_rounds()], [(63, 6301)])
        self.assertOutOfScopeNotes([(norm(good), n_good)], plain=[norm(bad)])

        # Round 2, the board working again: the finding whose create failed is ticketed now, the other is not again.
        self.board_conf()
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (21,)))
        self.open_pr(63, review_id=6302)
        out, p = self.findings([bad, good])
        self.assertEqual(len(self.creates()), 3, [c["argv"] for c in self.creates()])
        self.assertTicketCreate(self.creates()[-1], norm(bad), self.EPIC, 63)
        n_bad = self.number_for(norm(bad))
        self.assertIn(n_bad, out["tickets"])
        self.assertOutOfScopeNotes([(norm(good), n_good), (norm(bad), n_bad)])

    # ------------------------------------------------------------- fix round #257: earlier rounds are retried
    def assertCreatedOnce(self, f, epic, pr):
        mine = [c for c in self.creates() if not c.get("failed")
                and self.title_names(create_options(c["argv"]).get("title"), norm(f))]
        self.assertEqual(len(mine), 1, [c["argv"] for c in self.creates()])
        self.assertTicketCreate(mine[0], norm(f), epic, pr)
        return mine[0]["number"]

    def test_a_failed_create_is_retried_when_a_later_review_does_not_repeat_it(self):
        # Round 1: two outside findings whose creates fail, one that works.
        self.board_conf(fail_titles=["README.md", "docs/guide.md"])
        self.open_pr(71, review_id=7101)
        lost = {"path": "README.md", "line": 4, "severity": "high", "body": "The README documents a dead flag."}
        repeated = {"path": "docs/guide.md", "severity": "major", "body": "The guide skips the login step."}
        good = {"path": "src/other.py", "line": 1, "severity": "medium", "body": "Other is dead code."}
        out, _p = self.findings([lost, repeated, good,
                                 {"path": "src/app.py", "line": 3, "severity": "high", "body": "On the diff."}])
        n_good = self.number_for(norm(good))
        self.assertEqual(out["tickets"], [n_good])
        self.assertOutOfScopeNotes([(norm(good), n_good)], plain=[norm(lost), norm(repeated)])

        # Round 2, the board working: a fix-round review that repeats only one of them. Both are ticketed now,
        # each once, and the good one is not filed again.
        self.board_conf()
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (13,)))
        self.open_pr(71, review_id=7102)
        out, _p = self.findings([repeated, {"path": "src/app.py", "line": 13, "severity": "low", "body": "Nit."}])
        n_lost = self.assertCreatedOnce(lost, self.EPIC, 71)
        n_repeated = self.assertCreatedOnce(repeated, self.EPIC, 71)
        self.assertEqual(len([c for c in self.creates() if not c.get("failed")]), 3)
        self.assertEqual(sorted(out["tickets"]), sorted([n_lost, n_repeated]))
        self.assertOutOfScopeNotes([(norm(good), n_good), (norm(lost), n_lost), (norm(repeated), n_repeated)])

        # Round 3 with nothing outside: nothing more is created.
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (13, 15)))
        self.open_pr(71, review_id=7103)
        out, _p = self.findings([])
        self.assertEqual(len([c for c in self.creates() if not c.get("failed")]), 3)
        self.assertEqual(out["tickets"], [])

    def test_a_round_whose_epic_lookup_failed_is_ticketed_in_a_later_round(self):
        self.board_conf(show={})                               # harness-board show fails in round 1
        self.open_pr(72, review_id=7201)
        lost = {"path": "lib/x.py", "line": 2, "severity": "critical", "body": "Z is read before it is set."}
        out, _p = self.findings([lost])
        self.assertEqual(self.creates(), [])
        self.assertEqual(out["tickets"], [])
        self.assertOutOfScopeNotes([], plain=[norm(lost)])

        self.board_conf()
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (11,)))
        self.open_pr(72, review_id=7202)
        out, _p = self.findings([])
        n = self.assertCreatedOnce(lost, self.EPIC, 72)
        self.assertEqual(out["tickets"], [n])
        self.assertOutOfScopeNotes([(norm(lost), n)])

    def test_a_round_without_a_pr_is_ticketed_once_a_pr_is_open(self):
        self.no_pr()
        lost = {"path": "README.md", "line": 2, "severity": "medium", "body": "The README omits the install step."}
        self.findings([lost, {"path": "src/app.py", "line": 3, "severity": "high", "body": "On the diff."}])

        # Round 2 with the PR open: the review does not repeat the outside finding, and it is ticketed now.
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (12,)))
        self.open_pr(73, review_id=7301)
        before = len(self.creates())
        out, _p = self.findings([{"path": "src/app.py", "line": 12, "severity": "medium", "body": "Twelve."}])
        n = self.assertCreatedOnce(lost, self.EPIC, 73)
        self.assertEqual(len(self.creates()), before + 1)
        self.assertEqual(out["tickets"], [n])
        self.assertOutOfScopeNotes([(norm(lost), n)])

        # Round 3: not again.
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (12, 14)))
        self.open_pr(73, review_id=7302)
        out, _p = self.findings([lost])
        self.assertEqual(len(self.creates()), before + 1)
        self.assertOutOfScopeNotes([(norm(lost), n)])

    def test_a_ticket_created_whose_linking_failed_keeps_its_number(self):
        self.board_conf(link_fail_titles=["README.md"])
        self.open_pr(74, review_id=7401)
        f = {"path": "README.md", "line": 1, "severity": "high", "body": "The README links a dead page."}
        other = {"path": "src/other.py", "line": 1, "severity": "medium", "body": "Other leaks a handle."}
        out, p = self.findings([f, other])
        self.assertEqual(len(self.creates()), 2)
        n = self.assertCreatedOnce(f, self.EPIC, 74)
        n_other = self.number_for(norm(other))
        warnings = [line for line in p.stderr.splitlines() if line.startswith("harness-pr: warning:")]
        self.assertTrue(any(f"#{n}" in line for line in warnings), p.stderr)
        self.assertEqual(sorted(out["tickets"]), sorted([n, n_other]))
        self.assertOutOfScopeNotes([(norm(f), n), (norm(other), n_other)])

        # A later round, the board linking again, the same finding reported again: no duplicate is filed.
        self.board_conf()
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (16,)))
        self.open_pr(74, review_id=7402)
        out, _p = self.findings([f])
        self.assertEqual(len(self.creates()), 2)
        self.assertOutOfScopeNotes([(norm(f), n), (norm(other), n_other)])


if __name__ == "__main__":
    unittest.main()
