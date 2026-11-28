"""Tests for the brief `harness-pr findings --gpt-check` sends GPT-6.2 Sol (ticket #514, epic #512).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_brief.py -v

The brief harness-gpt reads on stdin is
    {"binding": {"request", "outline", "definition_of_done", "tests", "owner_answers"},
     "context": {"ticket_context", "epic_goal", "rulings"}, "omitted": [...], "review": {"mode", "base"},
     "prior_blockers": [...]}
from `harness-board show 12 --json` (`sections`, `parent.goal`, "" without a parent), the rulings saved by earlier
findings calls plus this call's --rulings lines (first-seen order, each once), and every needs-input round of the
ticket's comments as harness_lib.owner_comments.answer_rounds gives them. `review` is {"mode": "full", "base":
<merge-base>} when the check reviews from the fork, else {"mode": "fix", "base": <previous reviewed head>}. The text
sent is at most 64000 characters; binding, review and prior_blockers are never trimmed, and binding alone over the
limit is an incomplete check. A secret-shaped value anywhere refuses before the check; so does a failed read of the
owner's login or the ticket's comments. HARNESS_GPT_OMIT_PRIVATE (the environment, else `env` in the main checkout's
.claude/settings.json) keeps the ticket's Context section and the owner's answers out.

Fixture: pr_findings_support's PostTest (the repositories, fakegh, the harness-board stub). harness-gpt is a fake in
the module's fakebin: it logs its argv and the exact text it read on stdin, one JSON line per run, and prints
{"head": <--head>, "model": "gpt-6.2-sol", "findings": [], "prior_fixed": [<fixed>] * len(prior_blockers)}, `fixed`
from a file the test writes (true by default). Expected briefs are built here from the ticket the stub serves and
the comments the REST world serves; the owner's answers are written out by hand, never computed by the code under
test. Each test's first harness-pr run is a process of its own; later runs (table rows and setup rounds) run the
tool in this process (gitrepo_support.run_in_process), with git, gh, harness-board and harness-gpt still their own
processes.

Choices the ticket left open, fixed here (each a `Ruling:` for the PR):
- `omitted` names each trim by the brief key it trimmed (`rulings`, `ticket_context`, `epic_goal`, `owner_answers`)
  somewhere in its entry; the entries' wording and type are not pinned;
- trimming keeps context.rulings a suffix of the full list (the oldest dropped, whole rulings only, no marker
  inside the list), and keeps at least the newest ruling when binding, review and prior_blockers leave room;
- how a cut ticket context or epic goal is cut is not pinned, only that `omitted` names it;
- the 64000 characters are counted on the exact text harness-gpt reads;
- HARNESS_GPT_OMIT_PRIVATE is read like HARNESS_SMOKE_TIMEOUT: the environment first ("0" there wins over a "true"
  in the file), else the main checkout's .claude/settings.json `env`; "1" and "true" are true;
- a failed login or comment read exits 1; its message is not pinned.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

sys.dont_write_bytecode = True

import pr_findings_support as support  # noqa: E402
from pr_findings_support import (BINS, BRANCH_CHANGES, OWNER, TOOL, PostTest, app_text, epic_ticket,  # noqa: E402
                                 executable, norm, tearDownModule, write)  # noqa: F401  (tearDownModule: unittest's)
from gitrepo_support import run_in_process  # noqa: E402

LIMIT = 64000
BRIEF_KEYS = ["binding", "context", "omitted", "prior_blockers", "review"]
BINDING_KEYS = ["definition_of_done", "outline", "owner_answers", "request", "tests"]
CONTEXT_KEYS = ["epic_goal", "rulings", "ticket_context"]
EPIC = 512
MARK = "\n\n<!-- harness -->"
REPLY = "Reply in a comment; /harness:work 12 picks it up."
BOT = "github-actions[bot]"
GUARD_Q = "The deploy of #12 failed its live check and was reverted. Retry, or change the approach?"
F1 = {"path": "src/app.py", "line": 3, "severity": "high", "body": "V3 is scaled twice."}

GPT = r'''import json, os, sys
argv = sys.argv[1:]
text = sys.stdin.read()
with open(os.environ["GPT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": text}) + "\n")
try:
    with open(os.environ["GPT_CONF"], encoding="utf-8") as fh:
        conf = json.load(fh)
except OSError:
    conf = {}
prior = json.loads(text).get("prior_blockers") or []
print(json.dumps({"head": argv[argv.index("--head") + 1], "model": "gpt-6.2-sol", "findings": [],
                  "prior_fixed": [conf.get("fixed", True)] * len(prior)}))
'''


def setUpModule():
    support.setUpModule()
    executable(os.path.join(BINS["fakebin"], "harness-gpt"), f"#!{sys.executable}\n" + GPT)


# --------------------------------------------------------------- the ticket, the comments and the brief, derived here
def compact(value):
    return json.dumps(value, separators=(",", ":"))


def filler(n, word):
    """At most `n` characters of plain words ending in a period: no secret shape, no `Ruling:`, no line break."""
    unit = f"{word} keeps the field order stable, "
    return (unit * (n // len(unit) + 1))[:n - 1].rstrip(" ,") + "."


def rulings(first, count):
    """`count` distinct one-line rulings of about 1000 characters each, numbered from `first`."""
    return [f"Ruling: r{i:03d} " + filler(987, f"ruling{i}") for i in range(first, first + count)]


def ticket(request, outline, done, tests, context, goal=None):
    """Ticket 12 as `harness-board show 12 --json` prints it; `goal` None: no parent epic."""
    t = epic_ticket(12, EPIC if goal is not None else None)
    if goal is not None:
        t["parent"]["goal"] = goal
    t["sections"] = {"request": request, "outline": outline, "definition_of_done": list(done), "tests": list(tests),
                     "context": context}
    return t


def small(**change):
    fields = dict(request="Add a CSV export.", outline="1. Write the export.", done=["It writes one row per record."],
                  tests=["tests/test_export.py::test_rows"], context="The report page is src/app.py.",
                  goal="Owners can take their data out.")
    fields.update(change)
    return ticket(**fields)


def binding_of(t, answers=()):
    s = t["sections"]
    return {"request": s["request"], "outline": s["outline"], "definition_of_done": s["definition_of_done"],
            "tests": s["tests"], "owner_answers": list(answers)}


def context_of(t, given=()):
    return {"ticket_context": t["sections"]["context"], "epic_goal": t["parent"]["goal"] if t["parent"] else "",
            "rulings": list(given)}


def brief_of(t, answers=(), given=(), review=None, prior=()):
    """The untrimmed brief for ticket `t`."""
    return {"binding": binding_of(t, answers), "context": context_of(t, given), "omitted": [], "review": review,
            "prior_blockers": list(prior)}


def said(login, at, body):
    """A comment on ticket 12 in Issues.comments' shape; login None is a comment without an author."""
    return {"author": {"login": login} if login else None, "body": body, "createdAt": f"2026-11-20T{at}:00Z",
            "url": f"https://github.com/o/r/issues/12#issuecomment-{at.replace(':', '')}"}


def asked(question):
    """A needs-input question as harness-board posts it, written out by hand."""
    return "\n\n".join(["**Needs your input**", question, REPLY]) + MARK


def round_(question, at, answers):
    return {"question": question, "asked_at": f"2026-11-20T{at}:00Z",
            "answers": [{"at": f"2026-11-20T{a}:00Z", "body": b} for a, b in answers]}


def secrets():
    """Three secret-shaped values, built here so this file carries none."""
    return ("gh" + "p_" + "Z9y8" * 9 + "Q", "sk" + "-proj-" + "a1B2c3D4" * 3, 'password = "' + "q7" * 14 + '"')


class BriefTest(PostTest):
    EPIC = EPIC
    extra = {}                       # more environment for the tool's runs
    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    @property
    def gpt_log(self):
        return os.path.join(self.tmp, "gpt.log")

    @property
    def gpt_conf(self):
        return os.path.join(self.tmp, "gpt.json")

    def env(self):
        env = super().env()
        env.update(GPT_LOG=self.gpt_log, GPT_CONF=self.gpt_conf, **self.extra)
        return env

    def pr(self, *args, cwd=None):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().pr(*args, cwd=cwd)
        return run_in_process(TOOL, args=args, cwd=cwd or self.main, env=self.env())

    # ----------------------------------------------------------- the world
    def show(self, t):
        self.board_conf(show={"12": t})

    def serve(self, comments=(), login=OWNER, calls=()):
        """The PR #34 open at the worktree's HEAD, `gh api user` answering `login`, ticket 12 holding `comments`."""
        self.served = (list(comments), login, list(calls))
        self.open_pr(34, comments=comments, login=login, calls=calls)

    def advance(self):
        """A new commit on the branch, the PR moved to it with the same world; the new head."""
        self.k = getattr(self, "k", 0) + 1
        head = self.commit("src/app.py", app_text(BRANCH_CHANGES + (10 + self.k,)))
        comments, login, calls = getattr(self, "served", ((), OWNER, ()))
        self.serve(comments, login, calls)
        return head

    def gpt_fixed(self, fixed):
        write(self.gpt_conf, json.dumps({"fixed": fixed}))

    def fresh(self):
        """Forget ticket 12's review rounds, receipts and rulings, so the next check is a first review again."""
        shutil.rmtree(self.reviews_dir(), True)

    # ----------------------------------------------------------- runs
    def rulings_file(self, lines):
        self.n += 1
        return write(os.path.join(self.tmp, "rulings-in", f"rulings-{self.n}.md"), "\n".join(lines) + "\n")

    def check(self, review=None, given=None):
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json", "--head", self.head()]
        if review is not None:
            args += ["--review", self.review_file(review)]
        if given is not None:
            args += ["--rulings", self.rulings_file(given)]
        return self.pr(*args)

    def checked(self, **kw):
        p = self.check(**kw)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout)

    def plain(self, review, given):
        """A findings call without the GPT check, with --rulings."""
        p = self.pr("findings", "12", "--worktree", self.wt, "--review", self.review_file(review),
                    "--rulings", self.rulings_file(given), "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def gpt_runs(self):
        return self.jsonl(self.gpt_log)

    def sent(self):
        """The text harness-gpt read on its last run."""
        runs = self.gpt_runs()
        self.assertTrue(runs, "harness-gpt never ran")
        return runs[-1]["stdin"]

    def brief(self):
        """The brief harness-gpt read last, its shape checked first."""
        got = json.loads(self.sent())
        self.assertEqual(sorted(got), BRIEF_KEYS, got.keys())
        self.assertEqual(sorted(got["binding"]), BINDING_KEYS)
        self.assertEqual(sorted(got["context"]), CONTEXT_KEYS)
        return got

    def published(self):
        return len(self.posts()) + len(self.comment_reviews())

    def assertRefused(self, p, before, says=None):
        """Exit 1, harness-gpt not run, nothing posted, no round recorded."""
        runs, posted, rounds = before
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertTrue(p.stderr.strip())
        if says:
            self.assertIn(says, p.stderr)
        self.assertEqual(len(self.gpt_runs()), runs)
        self.assertEqual(self.published(), posted)
        self.assertEqual(self.load_rounds(), rounds)

    def state(self):
        return len(self.gpt_runs()), self.published(), self.load_rounds()

    def assertNamed(self, omitted, name):
        self.assertTrue(any(name in json.dumps(entry) for entry in omitted), f"{name} not named in {omitted}")


# =========================================================================== the brief
class Brief(BriefTest):
    def test_brief_carries_binding_and_context_from_the_ticket(self):
        rows = [
            ("an epic and one answered question",
             ticket("Add a CSV export to the report page.", "1. Add the writer.\n2. Wire the button to it.",
                    ["The export writes one row per record.", "The header names every column."],
                    ["tests/test_export.py::test_rows", "tests/test_export.py::test_header"],
                    "The report page lives in src/app.py; keep the JSON route as it is.",
                    goal="Owners can take their data out of the app."),
             [said(OWNER, "09:00", asked("Comma or semicolon?")), said(OWNER, "10:00", "Comma.")],
             [round_("Comma or semicolon?", "09:00", [("10:00", "Comma.")])]),
            ("no epic and no comments",
             ticket("Rename the flag and keep the old one working.", "Alias --old to --new.",
                    ["--old still works.", "--new is documented.", "Ünïcode names survive."], ["tests/test_flag.py"],
                    "The flag parser is in src/other.py.", goal=None),
             [], []),
            ("an empty context section and a drive-by comment",
             ticket("Speed up the sort.", "1. Profile.\n2. Replace the bubble sort.\n3. Measure.",
                    ["Sorting 10k rows takes under 1 s."], [], "", goal="Pages load fast."),
             [said("someone", "09:00", "drive-by")], []),
        ]
        for name, t, comments, answers in rows:
            with self.subTest(name):
                self.fresh()
                self.show(t)
                self.serve(comments)
                self.checked(review=[])
                self.assertEqual(self.brief(), brief_of(t, answers, review={"mode": "full", "base": self.merge_base}))

    def test_owner_answers_cover_every_question_round(self):
        history = [
            said(OWNER, "08:00", "A note before any question."),
            said(OWNER, "09:00", asked("Which base should the export use?")),
            said("RJeskuri", "09:30", "Use develop."),
            said("someone", "09:40", "drive-by"),
            said(BOT, "09:45", "A bot note."),
            said(OWNER, "09:50", "Started on t/12-x (mac.local, interactive)" + MARK),
            said(OWNER, "10:00", "And keep main protected."),
            said(BOT, "11:00", asked(GUARD_Q)),
            said(OWNER, "12:00", "Retry once."),
            said("someone", "12:30", "I would retry too."),
            said(OWNER, "13:00", asked("What should the flag be called?")),
            said(OWNER, "13:30", "Landed in abc1234"),
            said(OWNER, "14:00", "Call it --export.\n\nAnd document it."),
            said(OWNER, "15:00", asked("Which date format?")),
        ]
        epic = {"issue": EPIC, "data": {"number": EPIC, "comments": [
            said(OWNER, "08:30", asked("Epic-wide question?")), said(OWNER, "08:45", "Epic-wide answer.")]}}
        rows = [
            ("the owner, the login in another case", history, "RJeskuri",
             [round_("Which base should the export use?", "09:00",
                     [("09:30", "Use develop."), ("10:00", "And keep main protected.")]),
              round_(GUARD_Q, "11:00", [("12:00", "Retry once.")]),
              round_("What should the flag be called?", "13:00", [("14:00", "Call it --export.\n\nAnd document it.")]),
              round_("Which date format?", "15:00", [])]),
            ("another login: only the deploy guard's question is one", history, "someone",
             [round_(GUARD_Q, "11:00", [("12:30", "I would retry too.")])]),
            ("no comments", [], OWNER, []),
        ]
        t = small()
        for name, comments, login, answers in rows:
            with self.subTest(name):
                self.fresh()
                self.show(t)
                self.serve(comments, login=login, calls=[epic])
                self.checked(review=[])
                got = self.brief()
                self.assertEqual(got["binding"]["owner_answers"], answers)
                self.assertEqual(got["binding"], binding_of(t, answers))

    def test_saved_and_given_rulings_reach_this_calls_brief(self):
        t = small()
        self.show(t)
        self.serve()
        # A findings call without the check saves its rulings.
        self.plain([], ["Notes first.", "Ruling: the export keeps the JSON route.", "- Ruling: commas, not semicolons."])
        saved = ["Ruling: the export keeps the JSON route.", "Ruling: commas, not semicolons."]
        # The first check: the saved ones, then this call's new ones, each once, in this call's own brief.
        self.checked(review=[], given=["Ruling: commas, not semicolons.", "  Ruling: dates in ISO 8601.  ",
                                       "Ruling: the export keeps the JSON route.", "Ruling: dates in ISO 8601."])
        first = saved + ["Ruling: dates in ISO 8601."]
        self.assertEqual(self.brief()["context"]["rulings"], first)
        # A fix re-check with more.
        self.advance()
        self.checked(given=["Ruling: the header row is always written.", "Ruling: dates in ISO 8601.",
                            "- Ruling: an empty report writes the header alone."])
        second = first + ["Ruling: the header row is always written.", "Ruling: an empty report writes the header alone."]
        self.assertEqual(self.brief()["context"]["rulings"], second)
        # Without --rulings: the saved ones.
        self.advance()
        self.checked()
        got = self.brief()
        self.assertEqual(got["context"], context_of(t, second))
        self.assertEqual(got["omitted"], [])

    def test_review_mode_is_full_then_fix(self):
        self.show(small())
        self.serve()
        self.gpt_fixed(False)                           # F1 stays unresolved, so every later round carries it
        heads = [self.head()]
        self.checked(review=[F1])
        steps = [("a first review", {"mode": "full", "base": self.merge_base}, [])]
        for _ in range(2):
            heads.append(self.advance())
            self.checked()
            steps.append(("a fix re-check", {"mode": "fix", "base": heads[-2]}, [norm(F1)]))
        # An amended commit: the previous head is no ancestor, so the check reviews from the fork again.
        write(os.path.join(self.wt, "src", "app.py"), app_text(BRANCH_CHANGES + (27,)))
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "--amend", "-m", "Amended")
        self.serve()
        self.checked()
        steps.append(("after an amend", {"mode": "full", "base": self.merge_base}, [norm(F1)]))
        runs = self.gpt_runs()
        self.assertEqual(len(runs), len(steps))
        for (name, review, prior), run in zip(steps, runs):
            with self.subTest(name, base=review["base"][:7]):
                got = json.loads(run["stdin"])
                self.assertEqual(got.get("review"), review)
                self.assertEqual(got.get("prior_blockers"), prior)
                self.assertEqual(run["argv"][run["argv"].index("--base") + 1], review["base"])


# =========================================================================== the bound
class Bound(BriefTest):
    def test_brief_is_trimmed_to_the_limit_keeping_binding_whole(self):
        mixed = ticket(filler(12000, "request"), filler(8000, "outline"),
                       [filler(2000, f"done{i}") for i in range(5)], [f"tests/test_{i}.py::test_it" for i in range(5)],
                       filler(15000, "context"), goal=filler(3000, "goal"))
        heavy = ticket(filler(10000, "request"), filler(6000, "outline"),
                       [filler(2000, f"done{i}") for i in range(4)], ["tests/test_x.py"],
                       "The parser is in src/other.py.", goal="Short goal.")
        rows = [
            # name, ticket, rulings saved by the first round, rulings given to the trimmed one, the first review
            ("a long ticket and twenty rulings", mixed, rulings(1, 10), rulings(11, 10), [F1]),
            ("forty-five rulings and a short context", heavy, rulings(1, 30), rulings(31, 15), []),
        ]
        for name, big, before, given, review in rows:
            with self.subTest(name):
                self.fresh()
                self.gpt_fixed(False)
                self.show(small())
                self.serve()
                first = self.head()
                self.checked(review=review, given=before)
                self.assertEqual(self.brief()["omitted"], [])           # under the limit: nothing trimmed
                self.assertEqual(self.brief()["context"]["rulings"], before)
                self.advance()
                self.show(big)
                self.checked(given=given)
                full = brief_of(big, (), before + given, {"mode": "fix", "base": first}, [norm(f) for f in review])
                self.assertGreater(len(compact(full)), LIMIT, "the fixture must need trimming")
                text = self.sent()
                self.assertLessEqual(len(text), LIMIT)
                got = self.brief()
                self.assertEqual(got["binding"], full["binding"])
                self.assertEqual(got["prior_blockers"], full["prior_blockers"])
                self.assertEqual(got["review"], full["review"])
                kept = got["context"]["rulings"]
                everything = full["context"]["rulings"]
                self.assertTrue(kept, "the newest ruling fits and must stay")
                self.assertEqual(kept, everything[len(everything) - len(kept):], "only the oldest are dropped")
                trimmed = [key for key in ("ticket_context", "epic_goal") if got["context"][key] != full["context"][key]]
                if len(kept) < len(everything):
                    trimmed.append("rulings")
                self.assertTrue(trimmed)
                for key in trimmed:
                    self.assertNamed(got["omitted"], key)
                if big is heavy:                    # cutting its short context cannot make room
                    self.assertLess(len(kept), len(everything))

    def test_binding_over_the_limit_is_an_incomplete_check(self):
        long_answer = [said(OWNER, "09:00", asked("Which format?")), said(OWNER, "10:00", filler(LIMIT + 500, "answer"))]
        rows = [
            ("a long request on a first review", small(request=filler(LIMIT + 2000, "request")), [], False),
            ("a long definition of done on a fix re-check",
             small(done=[filler(2000, f"item{i}") for i in range(40)]), [], True),
            ("a long owner answer", small(), long_answer, False),
        ]
        for name, t, comments, fix in rows:
            with self.subTest(name):
                self.fresh()
                self.show(small())
                self.serve()
                if fix:
                    self.checked(review=[])
                    self.advance()
                self.show(t)
                self.serve(comments)
                before = self.state()
                p = self.check(review=None if fix else [])
                self.assertRefused(p, before, "GPT check incomplete")


# =========================================================================== refusals
class Refusals(BriefTest):
    def test_a_secret_in_an_answer_or_ruling_refuses_before_the_check(self):
        gh, sk, pw = secrets()
        question = said(OWNER, "09:00", asked("Which key?"))
        rows = [
            # name, ticket, comments, this call's rulings, rulings saved by an earlier findings call
            ("an owner answer", small(), [question, said(OWNER, "10:00", f"Use {gh} for now.")], None, None),
            ("a ruling given to this call", small(), [], [f"Ruling: call the API with {sk} in the header."], None),
            ("a ruling saved earlier", small(), [], None, [f"Ruling: log in with {pw} on staging."]),
            ("the outline", small(outline=f"1. Call the API with {gh}."), [], None, None),
            ("the ticket's context", small(context=f"The staging key is {sk}."), [], None, None),
            ("the epic goal", small(goal=f"Rotate {pw} out of the code."), [], None, None),
        ]
        for name, t, comments, given, saved in rows:
            with self.subTest(name):
                self.fresh()
                self.show(t)
                self.serve(comments)
                if saved:
                    self.plain([], saved)
                before = self.state()
                p = self.check(review=[], given=given)
                self.assertRefused(p, before, "secret-shaped")

    def test_a_failed_comment_read_is_an_incomplete_check(self):
        login_fails = [{"match": ["api", "user"], "stderr": "gh: HTTP 401: Bad credentials\n", "exit": 1}]
        no_login = [{"match": ["api", "user"], "stdout": "\n"}]
        comments_fail = [{"rest": "issues/12/comments", "stderr": "gh: HTTP 502: Bad Gateway\n", "exit": 1}]
        question = [said(OWNER, "09:00", asked("Which base?")), said(OWNER, "10:00", "Use develop.")]
        self.show(small())
        self.serve(question, calls=login_fails)         # a first review
        before = self.state()
        self.assertRefused(self.check(review=[]), before)
        self.serve(question)
        self.checked(review=[])
        self.advance()
        for name, calls in (("the login fails", login_fails), ("no login", no_login),
                            ("the comments fail", comments_fail)):
            with self.subTest(name):                    # a fix re-check
                self.serve(question, calls=calls)
                before = self.state()
                self.assertRefused(self.check(), before)


# =========================================================================== the private setting
class Private(BriefTest):
    def test_private_setting_keeps_context_and_answers_out(self):
        t = small(context="Customer Jane Roe, account 4411, asked for this.")
        comments = [said(OWNER, "09:00", asked("Which account format?")), said(OWNER, "10:00", "Use 4411-style ids.")]
        answers = [round_("Which account format?", "09:00", [("10:00", "Use 4411-style ids.")])]
        given = ["Ruling: account ids stay strings."]
        settings = os.path.join(self.main, ".claude", "settings.json")
        rows = [
            ("the environment says 1", {"HARNESS_GPT_OMIT_PRIVATE": "1"}, None, True),
            ("the main checkout's settings say true", {}, "true", True),
            ("the environment's 0 wins over the settings", {"HARNESS_GPT_OMIT_PRIVATE": "0"}, "true", False),
            ("unset", {}, None, False),
        ]
        for name, env, in_file, omit in rows:
            with self.subTest(name):
                self.fresh()
                if in_file is None:
                    if os.path.exists(settings):
                        os.unlink(settings)
                else:
                    write(settings, json.dumps({"env": {"HARNESS_GPT_OMIT_PRIVATE": in_file}}))
                self.extra = env
                self.show(t)
                self.serve(comments)
                self.checked(review=[], given=given)
                got = self.brief()
                review = {"mode": "full", "base": self.merge_base}
                if not omit:
                    self.assertEqual(got, brief_of(t, answers, given, review))
                    continue
                self.assertEqual(got["binding"], binding_of(t, []))
                self.assertEqual(got["context"], dict(context_of(t, given), ticket_context=""))
                self.assertEqual((got["review"], got["prior_blockers"]), (review, []))
                for key in ("ticket_context", "owner_answers"):
                    self.assertNamed(got["omitted"], key)
                # The owner's dismissals are still read (#517), so the comments are; nothing private reaches Sol.
                for private in ("Jane Roe", "account 4411", "4411-style"):
                    self.assertNotIn(private, self.sent())


if __name__ == "__main__":
    unittest.main()
