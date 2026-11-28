"""Tests for `harness-pr findings` applying the owner's dismissals when it records a round (ticket #517, epic #512).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_dismiss.py -v

With an open PR, findings reads the owner's `dismiss <path>:<line> <why>` lines on ticket 12 and on the PR
(harness_lib.dismissals.read), resolves each against the ticket's local rounds (each round's `at` with its blocking,
low, outside and dismissed findings) or, with no local round, against the PR's harness reviews on GitHub, and takes
every finding whose [path, body] a dismissal holds out of blocking, low and outside: the round keeps it under
`dismissed` (the finding plus `from`, `why` and `url`, the key only when something was dismissed), the posted review
and round-k-review.json leave it out, notes.md lists it under `### Dismissed`, and no out-of-scope ticket is made for
it. A selector that dismisses nothing is one warning naming it; a failed read is one warning and dismisses nothing.
Without an open PR nothing is read.

Fixture: pr_findings_support's PostTest (the repositories, fakegh with ticket 12's and PR #34's comments in its REST
world, the harness-board stub), plus a fake harness-gpt in the module's fakebin that logs its argv and the brief it
read and prints {"head", "model": "gpt-6.2-sol", "findings": <conf findings, unrated>, "prior_fixed": [<conf fixed>]
* len(prior_blockers)} from a file the test writes; a GPT check that records its round is the check and then the
`--gpt-triage` call with Opus accepting Sol's findings at the conf's severities (#534). Each test's first harness-pr
run is a process of its own; later
runs run the tool in this process (gitrepo_support.run_in_process), git, gh, harness-board and harness-gpt still
their own processes.

The timeline. A round is recorded now (2026-11-28 or later). A round the owner's comments must come after is set
back, once recorded, to 10:00 on 2026-11-20 in rounds.json (`stamp`: the time it was recorded, as `at` holds it);
the comments are on that day, 11:00 and later, so every later round falls after them. A comment at 09:00 comes
before the round. Expected values are written out by hand: the split from the touched files, the dismissed entries
from the findings and the comments, the posted payload from the diff's lines (pr_findings_support.on_diff).

Choices the ticket left open, fixed here (each a `Ruling:` for the PR):
- the order of `dismissed` (and of blocking, low and outside) is not pinned; each dismissed entry is exactly the
  normalised finding plus `from`, `why` and `url`, in the round and in --json alike;
- an `unmatched` entry in --json is pinned by its `selector`, `reason` and `url` only; the reasons are
  harness_lib.dismissals' words (`no reason given`, `no finding at <selector> before the comment`, `ambiguous: <n>
  findings at <selector>`), and a warning line holds the selector, the reason and the comment's URL;
- the `### Dismissed` line carries no outside marker, and the section sits between Out of scope and Rulings;
- a dismissal made before an earlier same-head record keeps applying when that head is recorded again (a receipt
  retry after a second dismissal still drops the first): the replaced round's findings stay a snapshot from before
  the comment;
- under HARNESS_GPT_OMIT_PRIVATE the dismissals are still read (nothing of them reaches Sol but fewer prior
  blockers); test_pr_findings_brief's private-setting test checks what reaches Sol, not which reads ran.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import unittest

sys.dont_write_bytecode = True

import fakegh  # noqa: E402
import pr_findings_support as support  # noqa: E402
from pr_findings_support import (BINS, BRANCH_CHANGES, MARKER, OWNER, TOOL, PostTest, app_text, comment,  # noqa: E402
                                 counts_line, create_options, entry, executable, norm, on_diff, sections,
                                 tearDownModule, write)  # noqa: F401  (tearDownModule: unittest's)
from gitrepo_support import run_in_process  # noqa: E402

DAY = "2026-11-20"
PAL = "pal"
BOT = "github-actions[bot]"
HARNESS = "\n\n<!-- harness -->"
WHY1 = "out of scope: #500 owns the scaling"
ROUND_KEYS = {"round", "at", "head", "base", "blocking", "low", "outside", "pr", "review_id"}
JSON_KEYS = {"ticket", "round", "head", "base", "review_base", "blocking", "low", "outside", "files", "record", "pr",
             "review_id", "tickets"}

F1 = {"path": "src/app.py", "line": 3, "severity": "high", "body": "V3 is scaled twice."}
F2 = {"path": "tests/test_app.py", "line": 2, "severity": "medium", "body": "The test asserts nothing."}
F12 = {"path": "src/app.py", "line": 12, "severity": "high", "body": "Line twelve is off the diff."}
K20 = {"path": "src/app.py", "line": 20, "severity": "medium", "body": "V20 is read before it is set."}
L26 = {"path": "src/app.py", "line": 26, "severity": "low", "body": "Name V26 after what it counts."}
L29 = {"path": "src/app.py", "line": 29, "severity": "low", "body": "Just past the hunk at 29."}
S26 = {"path": "src/app.py", "line": 26, "severity": "medium", "body": "The cap at V26 is never checked."}
O1 = {"path": "README.md", "line": 1, "severity": "high", "body": "The README names the wrong flag."}
O2 = {"path": "lib/x.py", "line": 2, "severity": "low", "body": "Z could be a constant."}
OL2 = {"path": "README.md", "line": 1, "severity": "low", "body": "The README has a typo."}

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
found = [{k: f[k] for k in ("path", "line", "body") if k in f} for f in conf.get("findings", [])]   # unrated (#534)
print(json.dumps({"head": argv[argv.index("--head") + 1], "model": "gpt-6.2-sol", "findings": found,
                  "prior_fixed": [conf.get("fixed", True)] * len(prior)}))
'''


def setUpModule():
    support.setUpModule()
    executable(os.path.join(BINS["fakebin"], "harness-gpt"), f"#!{sys.executable}\n" + GPT)


# --------------------------------------------------------------- the comments and the expectations, derived here
def note(login, at, body, number=12):
    """A comment on ticket 12 or PR #34 at `at` on DAY (or a full ISO time), in Issues.comments' shape; login None is
    a comment without an author."""
    when = at if "T" in at else f"{DAY}T{at}:00Z"
    kind = "pull" if number == 34 else "issues"
    digits = "".join(c for c in when if c.isdigit())
    return {"author": {"login": login} if login else None, "body": body, "createdAt": when,
            "url": f"https://github.com/o/r/{kind}/{number}#issuecomment-{number}{digits}"}


def dismiss(f, why):
    return f"dismiss {f['path']}:{f['line']} {why}"


def at(f, line=None, **change):
    """Finding f raised again, at another line (None: no line) or with other fields."""
    out = dict(f, **change)
    if line is None:
        out.pop("line", None)
    else:
        out["line"] = line
    return out


def gone(f, source, why, by):
    """The round's entry for finding f dismissed from `source` by comment `by`."""
    return dict(norm(f), **{"from": source, "why": why, "url": by["url"]})


def bag(items):
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def noted(f, why):
    """A `### Dismissed` line."""
    return f"{entry(norm(f))} (dismissed: {why})"


def later(hours=1):
    """An ISO time `hours` after the real now: a comment made after a round recorded now."""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def github_review(rid, when, inline, listed, login=OWNER):
    """(a harness review as harness-pr posts it, its inline comments as GET pulls/{n}/comments lists them): the
    counts line, the `listed` findings under `Not on a changed line:`, the marker; each inline comment with its
    pull_request_review_id, at its posted line (original_line), `line` where a later push moved it."""
    found = [norm(f) for f in list(inline) + list(listed)]
    body = "\n\n".join([counts_line("opus", found),
                        "\n".join(["Not on a changed line:"] + [entry(norm(f)) for f in listed]), MARKER])
    review = {"id": rid, "user": {"login": login}, "body": body, "state": "COMMENTED", "commit_id": "c" * 40,
              "submitted_at": when if "T" in when else f"{DAY}T{when}:00Z"}
    comments = [{"id": rid * 100 + k, "pull_request_review_id": rid, "user": {"login": login}, "path": f["path"],
                 "line": f["line"] + 2, "original_line": f["line"], "side": "RIGHT",
                 "body": f"[{f['severity']}] {f['body']}\n\n{MARKER}"} for k, f in enumerate(map(norm, inline), 1)]
    return review, comments


def github_calls(review, comments, pr=34):
    """The PR's reviews and that review's comments, as REST answers as GitHub gives them (#533): the review's own
    pulls/{n}/reviews/{id}/comments with line and original_line null and only a diff position (never the line), the
    PR's pulls/{n}/comments with their lines and pull_request_review_id."""
    unlined = [dict(c, line=None, original_line=None, position=c["original_line"] + 4) for c in comments]
    return [fakegh.rest_entry(f"pulls/{pr}/reviews/{review['id']}/comments", "GET", exact=True, stdout_json=unlined),
            fakegh.rest_entry(f"pulls/{pr}/comments", "GET", exact=True, stdout_json=comments),
            fakegh.rest_entry(f"pulls/{pr}/reviews", "GET", exact=True, stdout_json=[review])]


def failing(route):
    return fakegh.rest_entry(route, "GET", exact=True, stderr="gh: HTTP 502: Bad Gateway\n", exit=1)


class DismissTest(PostTest):
    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    def setUp(self):
        super().setUp()
        self.changes = BRANCH_CHANGES
        self.world = {}

    @property
    def gpt_log(self):
        return os.path.join(self.tmp, "gpt.log")

    def env(self):
        env = super().env()
        env.update(GPT_LOG=self.gpt_log, GPT_CONF=os.path.join(self.tmp, "gpt.json"))
        return env

    def pr(self, *args, cwd=None):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().pr(*args, cwd=cwd)
        return run_in_process(TOOL, args=args, cwd=cwd or self.main, env=self.env())

    # ----------------------------------------------------------- the world
    def serve(self, ticket=(), pr=(), calls=(), login=OWNER, review_id=None):
        """PR #34 open at the worktree's HEAD, ticket 12 holding `ticket`, the PR `pr`."""
        self.world = dict(ticket=list(ticket), pr=list(pr), calls=list(calls), login=login, review_id=review_id)
        self.open_pr(34, comments=ticket, pr_comments=pr, login=login, calls=calls, review_id=review_id)

    def advance(self, changes=None, text=None):
        """A new commit on the branch (src/app.py's `changes` on top of the branch's, or `text`), the PR moved to it
        with the same world; the new head."""
        if text is None:
            if changes is None:
                changes = (14,) if self.changes != BRANCH_CHANGES + (14,) else (16,)
            self.changes = BRANCH_CHANGES + tuple(changes)
            text = app_text(self.changes)
        else:
            self.changes = None
        head = self.commit("src/app.py", text)
        self.serve(**self.world)
        return head

    def fresh(self):
        """Forget ticket 12's rounds, receipts, rulings and tickets: the next round is a first one."""
        shutil.rmtree(self.reviews_dir(), True)

    def stamp(self, hour="10:00"):
        """Set the latest round's `at` back to `hour` on DAY: it was recorded before the comments."""
        path = os.path.join(self.reviews_dir(), "rounds.json")
        with open(path, encoding="utf-8") as fh:
            rounds = json.load(fh)
        rounds[-1]["at"] = f"{DAY}T{hour}:00Z"
        write(path, json.dumps(rounds))

    def sol(self, findings=(), fixed=True):
        """Sol raises `findings` (printed unrated) and calls every prior blocker `fixed`; Opus's triage (`paired`)
        accepts each at the severity given here."""
        self.raised = list(findings)
        write(os.path.join(self.tmp, "gpt.json"), json.dumps({"findings": list(findings), "fixed": fixed}))

    # ----------------------------------------------------------- runs
    def record(self, review, pinned=False, rulings=None):
        """`harness-pr findings 12 --review F --json` (with `--head HEAD` when pinned): (the JSON, the process)."""
        args = ["findings", "12", "--worktree", self.wt, "--review", self.review_file(review), "--json"]
        if pinned:
            args += ["--head", self.head()]
        if rulings:
            args += ["--rulings", write(os.path.join(self.tmp, "rulings-in", f"r-{self.n}.md"), "\n".join(rulings))]
        p = self.pr(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout), p

    def check(self, review=None):
        """`harness-pr findings 12 --gpt-check --head HEAD --json` (with --review F when given)."""
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json", "--head", self.head()]
        if review is not None:
            args += ["--review", self.review_file(review)]
        p = self.pr(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout), p

    def paired(self, review=None):
        """The GPT check, then Opus's triage (#534) accepting each of Sol's findings at the severity `sol` gave it
        and confirming Sol's calls: (the triage call's JSON, its process), the call that records the round."""
        out, _p = self.check(review)
        accept = {f["body"]: norm(f)["severity"] for f in getattr(self, "raised", [])}
        return self.triaged(support.opus_verdicts(self.candidates(out), accept=accept))

    def gpt_runs(self):
        return self.jsonl(self.gpt_log)

    def warnings(self, p):
        return [line for line in p.stderr.splitlines() if line.startswith("harness-pr: warning:")]

    def review_json(self, out):
        """round-<k>-review.json of the round just recorded: what was posted."""
        with open(os.path.join(self.reviews_dir(), f"round-{out['round']}-review.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def stage_blocking(self):
        """What `harness-work stage` reads: reviews.blocking of the latest round."""
        rnd = self.load_rounds()[-1]
        import importlib
        return importlib.import_module("harness_lib.reviews").blocking(rnd)

    def assertRecorded(self, out, blocking=(), low=(), outside=(), dismissed=None):
        """The latest round and --json hold exactly these splits; `dismissed` None: neither has the key."""
        rnd = self.load_rounds()[-1]
        for name, want in (("blocking", blocking), ("low", low), ("outside", outside)):
            self.assertEqual(bag(rnd.get(name, [])), bag(map(norm, want)), f"round {name}")
            self.assertEqual(bag(out.get(name, [])), bag(map(norm, want)), f"--json {name}")
        if dismissed is None:
            self.assertNotIn("dismissed", rnd)
            self.assertNotIn("dismissed", out)
        else:
            self.assertEqual(bag(rnd.get("dismissed", [])), bag(dismissed), "round dismissed")
            self.assertEqual(bag(out.get("dismissed", [])), bag(dismissed), "--json dismissed")
        return rnd

    def assertCreatedNever(self, f):
        titles = [create_options(c["argv"]).get("title") for c in self.creates() if not c.get("failed")]
        self.assertFalse([t for t in titles if self.title_names(t, norm(f))], titles)


# =========================================================================== leaving blocking, reaching the notes
class Dismissed(DismissTest):
    def test_an_owner_dismissal_leaves_blocking_and_reaches_the_notes(self):
        # The ticket's example: round 1 at H1, the owner's ticket comment, round 2 at H2 with a line inserted above.
        self.serve()
        self.record([F1, F2, L26, O1, O2])
        self.stamp()
        by = note(OWNER, "11:00", "Looked at the review.\n" + dismiss(F1, WHY1) + "\n"
                  + dismiss(L26, "naming is the owner's call") + "\n" + dismiss(O2, "lib/x.py is not this ticket's"))
        self.serve(ticket=[by])
        self.advance(text="# header\n" + app_text(BRANCH_CHANGES))
        shifted, low = at(F1, 4), at(L26, 27)
        out, _p = self.record([shifted, F2, low, O1, O2], rulings=["Ruling: keep the scale factor."])
        self.assertRecorded(out, blocking=[F2], outside=[O1],
                            dismissed=[gone(shifted, "blocking", WHY1, by),
                                       gone(low, "low", "naming is the owner's call", by),
                                       gone(O2, "outside", "lib/x.py is not this ticket's", by)])
        self.assertEqual(self.stage_blocking(), [norm(F2)])
        self.assertEqual(out["record"], {"stage": "fix", "result": "fail"})
        self.assertNotIn("unmatched", out)
        notes = self.notes()
        heads = [line[4:].strip() for line in notes.splitlines() if line.startswith("### ")]
        self.assertEqual(heads, ["Low findings", "Out of scope", "Dismissed", "Rulings"], notes)
        self.assertEqual(sorted(sections(notes).get("Dismissed", [])),
                         sorted([noted(shifted, WHY1), noted(low, "naming is the owner's call"),
                                 noted(O2, "lib/x.py is not this ticket's")]), notes)

        # Every blocker dismissed, over two comments: the round passes and the stage reads nothing blocking.
        self.fresh()
        self.serve()
        self.record([F1, F2])
        self.stamp()
        first = note(OWNER, "11:00", dismiss(F1, WHY1))
        second = note(OWNER, "11:30", dismiss(F2, "the assertion lives in the fixture"))
        self.serve(ticket=[first, second])
        self.advance()
        out, _p = self.record([F1, F2])
        self.assertRecorded(out, dismissed=[gone(F1, "blocking", WHY1, first),
                                            gone(F2, "blocking", "the assertion lives in the fixture", second)])
        self.assertEqual(self.stage_blocking(), [])
        self.assertEqual(out["record"], {"stage": "fix", "result": "pass"})
        self.assertEqual(sorted(sections(self.notes()).get("Dismissed", [])),
                         sorted([noted(F1, WHY1), noted(F2, "the assertion lives in the fixture")]))

        # The same comment with no open PR: nothing is read, F1 still blocks, the only gh call is `gh pr list`.
        self.fresh()
        self.serve()
        self.record([F1])
        self.stamp()
        self.commit("src/app.py", app_text(BRANCH_CHANGES + (18,)))
        self.scenario([support.REPO_VIEW, {"contains": ["pr", "list"], "stdout_json": []},
                       {"match": ["api", "user"], "stdout": f"{OWNER}\n"},
                       {"issue": 12, "data": {"number": 12, "comments": [first]}},
                       {"issue": 34, "data": {"number": 34, "pull_request": True, "comments": [first]}}])
        calls = len(self.gh_calls())
        out, _p = self.record([F1])
        rnd = self.assertRecorded(out, blocking=[F1])
        self.assertEqual(set(rnd), ROUND_KEYS)
        self.assertEqual([c["argv"] for c in self.gh_calls()[calls:] if c["argv"][:2] != ["pr", "list"]], [])

    def test_a_pr_comment_dismisses_like_a_ticket_comment(self):
        on_pr = note(OWNER, "11:00", dismiss(F1, WHY1), 34)
        on_ticket = note(OWNER, "11:00", dismiss(F1, WHY1), 12)
        f2_pr = note(OWNER, "11:20", dismiss(F2, "covered by the fixture's own check"), 34)
        pr_first, ticket_later = (note(OWNER, "11:00", dismiss(F1, "the PR's reason"), 34),
                                  note(OWNER, "11:30", dismiss(F1, "the ticket's reason"), 12))
        ticket_first, pr_later = (note(OWNER, "11:05", dismiss(F1, "the ticket said it first"), 12),
                                  note(OWNER, "11:40", dismiss(F1, "the PR said it later"), 34))
        styled = note(OWNER, "11:00", "DISMISS `./src/app.py:3` " + WHY1, 34)
        rows = [  # name, ticket comments, PR comments, the dismissed entries
            ("on the PR", [], [on_pr], [gone(F1, "blocking", WHY1, on_pr)]),
            ("one on the ticket, one on the PR", [on_ticket], [f2_pr],
             [gone(F1, "blocking", WHY1, on_ticket),
              gone(F2, "blocking", "covered by the fixture's own check", f2_pr)]),
            ("both, the PR's first", [ticket_later], [pr_first], [gone(F1, "blocking", "the PR's reason", pr_first)]),
            ("both, the ticket's first", [ticket_first], [pr_later],
             [gone(F1, "blocking", "the ticket said it first", ticket_first)]),
            ("any case, in backticks, a leading ./", [], [styled], [gone(F1, "blocking", WHY1, styled)]),
        ]
        for name, ticket, pr, dismissed in rows:
            with self.subTest(name):
                self.fresh()
                self.serve()
                self.record([F1, F2])
                self.stamp()
                self.serve(ticket=ticket, pr=pr)
                self.advance()
                out, _p = self.record([F1, F2])
                kept = [f for f in (F1, F2) if all(d["body"] != f["body"] for d in dismissed)]
                self.assertRecorded(out, blocking=kept, dismissed=dismissed)
                self.assertEqual(bag(self.stage_blocking()), bag(map(norm, kept)))


# =========================================================================== the posted review
class Posted(DismissTest):
    def assertPayload(self, sent, kept, gone_):
        """One POST: inline comments for the kept findings on the diff, the rest of them listed in the body, the
        dismissed ones nowhere."""
        lines = on_diff(self.changes)
        inline = [f for f in kept if f["line"] and (f["path"], f["line"]) in lines]
        off = [f for f in kept if f not in inline]
        self.assertTrue(inline and off, "the fixture must exercise both routes")
        self.assertEqual(support.by_place(sent["comments"]), support.by_place([comment(f) for f in inline]))
        body = sent["body"].splitlines()
        self.assertEqual(body[0], counts_line("opus", kept))
        self.assertEqual(sorted(line for line in body if line.startswith("- ")), sorted(entry(f) for f in off))
        for f in gone_:
            self.assertNotIn(f["body"], sent["body"])
            self.assertFalse([c for c in sent["comments"] if f["body"] in c["body"]])

    def test_dismissed_findings_are_not_posted(self):
        review = [F1, F12, L26, L29, F2, K20, O2, OL2]
        by = note(OWNER, "11:00", "\n".join([dismiss(F1, WHY1), dismiss(F12, "twelve is read once by design"),
                                             dismiss(L29, "the name is fine"),
                                             dismiss(OL2, "the typo is in a quoted error message")]))
        gone_ = [norm(f) for f in (F1, F12, L29, OL2)]
        rows = [  # name, pinned, the commit's changed line, the findings published
            ("a review", False, (8,), [F2, K20, L26]),
            ("a pinned review with the outside lows", True, (7,), [F2, K20, L26, O2]),
        ]
        for name, pinned, changes, kept in rows:
            with self.subTest(name):
                self.fresh()
                self.serve()
                self.record(review, pinned=pinned)
                self.stamp()
                self.serve(ticket=[by])
                self.advance(changes)
                posts = len(self.posts())
                out, _p = self.record(review, pinned=pinned)
                self.assertEqual(len(self.posts()), posts + 1)
                sent = json.loads(self.posts()[-1]["stdin"])
                self.assertPayload(sent, [norm(f) for f in kept], gone_)
                if pinned:
                    self.assertEqual(sent["commit_id"], self.head())
                self.assertEqual(bag(self.review_json(out)), bag(map(norm, kept)))
                self.assertEqual(len(out.get("dismissed", [])), 4)

        # Every finding it would post dismissed: the no-findings comment, and an empty round-k-review.json.
        self.fresh()
        self.serve()
        self.record([F1, F12, L29, O1])
        self.stamp()
        by = note(OWNER, "11:00", "\n".join([dismiss(F1, WHY1), dismiss(F12, "read once"), dismiss(L29, "fine")]))
        self.serve(ticket=[by])
        self.advance((6,))
        posts, reviews = len(self.posts()), len(self.comment_reviews())
        out, _p = self.record([F1, F12, L29, O1])
        self.assertEqual(len(self.posts()), posts)
        self.assertEqual(len(self.comment_reviews()), reviews + 1)
        argv = self.comment_reviews()[-1]["argv"]
        self.assertEqual(argv[:5], ["pr", "review", "34", "--comment", "-b"])
        self.assertTrue(argv[5].startswith("Harness review: no findings."), argv)
        self.assertEqual(self.review_json(out), [])
        self.assertRecorded(out, outside=[O1], dismissed=[gone(F1, "blocking", WHY1, by),
                                                          gone(F12, "blocking", "read once", by),
                                                          gone(L29, "low", "fine", by)])


# =========================================================================== matching by path and body
class Matching(DismissTest):
    def test_an_exact_reraise_on_a_shifted_line_stays_dismissed(self):
        self.serve()
        self.record([F1, F2])
        self.stamp()
        by = note(OWNER, "11:00", dismiss(F1, WHY1), 34)
        self.serve(pr=[by])
        rows = [  # the re-raise, where it lands
            (at(F1, 4), "blocking"),
            (at(F1, 17, severity="major"), "blocking"),
            (at(F1, None), "blocking"),
            (at(F1, 5, severity="low"), "low"),
            (at(F1, 4), "blocking"),
        ]
        for again, source in rows:
            with self.subTest(line=again.get("line"), severity=again["severity"]):
                self.advance()
                out, _p = self.record([again, F2])
                self.assertRecorded(out, blocking=[F2], dismissed=[gone(again, source, WHY1, by)])
                self.assertEqual(self.stage_blocking(), [norm(F2)])
                self.assertIn(noted(again, WHY1), sections(self.notes()).get("Dismissed", []))
        self.assertEqual(sections(self.notes()).get("Dismissed", []).count(noted(at(F1, 4), WHY1)), 1)

    def test_a_reworded_reraise_blocks(self):
        self.serve()
        self.record([F1, F2])
        self.stamp()
        by = note(OWNER, "11:00", dismiss(F1, WHY1))
        self.serve(ticket=[by])
        self.advance()
        reworded = [at(F1, 4, body="V3 is scaled twice"),
                    at(F1, 5, severity="medium", body="V3 is scaled twice!"),
                    at(F1, 6, body="v3 is scaled twice."),
                    at(F1, 7, body="V3 is scaled twice. It doubles the output."),
                    dict(F1, path="tests/test_app.py")]
        out, _p = self.record([F1] + reworded + [F2])
        self.assertRecorded(out, blocking=reworded + [F2], dismissed=[gone(F1, "blocking", WHY1, by)])
        self.assertEqual(out["record"], {"stage": "fix", "result": "fail"})

        # Only rewordings: nothing is dismissed, so the round and --json have no `dismissed`, and nothing is unmatched.
        self.advance()
        out, p = self.record(reworded[:2] + [F2])
        rnd = self.assertRecorded(out, blocking=reworded[:2] + [F2])
        self.assertEqual(set(rnd), ROUND_KEYS)
        self.assertEqual(set(out), JSON_KEYS)
        self.assertEqual(self.warnings(p), [])


# =========================================================================== out-of-scope tickets
class Outside(DismissTest):
    def test_a_dismissed_outside_finding_gets_no_ticket(self):
        o3 = {"path": "docs/guide.md", "severity": "major", "body": "The guide skips the install step."}
        o5 = {"path": "lib/x.py", "line": 1, "severity": "medium", "body": "Y is read before it is set."}
        by = note(OWNER, "11:00", dismiss(O1, "the README is generated from the CLI help"))

        # Round 1's creates fail; the owner dismisses one; round 2, the board working, does not repeat either.
        self.board_conf(fail_titles=["README.md", "docs/guide.md"])
        self.serve()
        self.record([O1, o3, F1])
        self.assertEqual(self.created(), [])
        self.stamp()
        self.board_conf()
        self.serve(ticket=[by])
        self.advance()
        out, _p = self.record([F1])
        n3 = self.number_for(norm(o3))
        self.assertEqual(out["tickets"], [n3])
        self.assertCreatedNever(O1)
        self.assertFalse([line for line in self.notes().splitlines() if O1["body"] in line and " -> #" in line],
                         self.notes())

        # The dismissed finding raised again on another line: dismissed from outside, still no ticket.
        self.fresh()
        self.board_conf(fail_titles=["README.md"])
        self.serve()
        self.record([O1, F1])
        self.stamp()
        self.board_conf()
        self.serve(ticket=[by])
        self.advance()
        again = at(O1, 2)
        out, _p = self.record([again, F1])
        self.assertRecorded(out, blocking=[F1],
                            dismissed=[gone(again, "outside", "the README is generated from the CLI help", by)])
        self.assertEqual(out["tickets"], [])
        self.assertCreatedNever(O1)

        # Round 1 without a PR made no ticket; with the PR open, only the finding not dismissed is ticketed.
        self.fresh()
        self.no_pr()
        self.record([O1, o5, F1])
        self.stamp()
        self.serve(ticket=[by])
        self.advance()
        out, _p = self.record([F1])
        n5 = self.number_for(norm(o5))
        self.assertEqual(out["tickets"], [n5])
        self.assertCreatedNever(O1)


# =========================================================================== whose lines count
class Ignored(DismissTest):
    def test_foreign_bot_and_harness_dismiss_lines_are_ignored(self):
        self.serve()
        self.record([F1, F2])
        self.stamp()
        ticket = [note(PAL, "11:00", dismiss(F1, "fine by me")),
                  note(BOT, "11:05", dismiss(F1, "the bot agrees")),
                  note(OWNER, "11:10", dismiss(F1, "recorded by the harness") + HARNESS),
                  note(OWNER, "11:15", "Started on t/12-x (mac.local, interactive)\n" + dismiss(F1, WHY1)),
                  note(None, "11:20", dismiss(F1, "nobody wrote this"))]
        pr = [note(OWNER, "11:25", "Please dismiss src/app.py:3 if it is noise.", 34),
              note(OWNER, "11:30", "> " + dismiss(F1, WHY1), 34),
              note(PAL, "11:35", dismiss(F2, "pal on the PR"), 34),
              note(BOT, "11:36", dismiss(F2, "the bot on the PR") + HARNESS, 34)]
        self.serve(ticket=ticket, pr=pr)
        self.advance()
        out, p = self.record([F1, F2])
        rnd = self.assertRecorded(out, blocking=[F1, F2])
        self.assertEqual(set(rnd), ROUND_KEYS)
        self.assertEqual(set(out), JSON_KEYS)
        self.assertEqual(self.warnings(p), [])
        self.assertEqual(bag(self.stage_blocking()), bag(map(norm, [F1, F2])))

        # The same comments and the owner's own line: that one dismisses, and only it.
        own = note(OWNER, "11:40", dismiss(F1, WHY1), 34)
        self.serve(ticket=ticket, pr=pr + [own])
        self.advance()
        out, p = self.record([F1, F2])
        rnd = self.assertRecorded(out, blocking=[F2], dismissed=[gone(F1, "blocking", WHY1, own)])
        self.assertEqual(set(rnd), ROUND_KEYS | {"dismissed"})
        self.assertEqual(set(out), JSON_KEYS | {"dismissed"})
        self.assertEqual(self.warnings(p), [])

    def test_unmatched_selectors_are_reported_and_change_nothing(self):
        a6 = {"path": "src/app.py", "line": 6, "severity": "medium", "body": "V6 is read twice."}
        b6 = {"path": "src/app.py", "line": 6, "severity": "high", "body": "V6 leaks the handle."}
        review = [F1, a6, b6, F2]
        self.serve()
        self.record(review)
        self.stamp()
        asked = [  # the comment, the selector, the reason
            (note(OWNER, "11:00", "dismiss src/app.py:5 nothing is wrong here", 12), "src/app.py:5",
             "no finding at src/app.py:5 before the comment"),
            (note(OWNER, "11:05", "dismiss src/app.py:3", 34), "src/app.py:3", "no reason given"),
            (note(OWNER, "11:10", "dismiss src/app.py:6 both are noise", 12), "src/app.py:6",
             "ambiguous: 2 findings at src/app.py:6"),
            (note(OWNER, "09:00", "dismiss tests/test_app.py:2 too early to tell", 34), "tests/test_app.py:2",
             "no finding at tests/test_app.py:2 before the comment"),
            (note(OWNER, "11:15", "dismiss src/app.py the whole file is fine", 12), "src/app.py",
             "no finding at src/app.py before the comment"),
        ]
        self.serve(ticket=[c for c, _s, _r in asked if "/issues/" in c["url"]],
                   pr=[c for c, _s, _r in asked if "/pull/" in c["url"]])
        self.advance()
        out, p = self.record(review)
        rnd = self.assertRecorded(out, blocking=review)
        self.assertEqual(set(rnd), ROUND_KEYS)
        self.assertEqual(set(out), JSON_KEYS | {"unmatched"})
        self.assertEqual(sorted((u.get("selector"), u.get("reason"), u.get("url")) for u in out.get("unmatched", [])),
                         sorted((s, r, c["url"]) for c, s, r in asked))
        warnings = self.warnings(p)
        self.assertEqual(len(warnings), len(asked), p.stderr)
        for c, selector, reason in asked:
            self.assertEqual(len([w for w in warnings if selector in w and reason in w and c["url"] in w]), 1,
                             (selector, p.stderr))

        # A failed read dismisses nothing, whatever the comments say, and is one warning; the round is as today.
        on_ticket = note(OWNER, "11:30", dismiss(F1, WHY1), 12)
        on_pr = note(OWNER, "11:35", dismiss(F2, "covered elsewhere"), 34)
        rows = [  # name, the failing read, a first round of the ticket
            ("the login", {"match": ["api", "user"], "stderr": "gh: HTTP 401: Bad credentials\n", "exit": 1}, False),
            ("the ticket's comments", failing("issues/12/comments"), False),
            ("the PR's comments", failing("issues/34/comments"), False),
            ("the PR's reviews, with no local round", failing("pulls/34/reviews"), True),
        ]
        for name, broken, first in rows:
            with self.subTest(name):
                if first:
                    self.fresh()
                self.serve(ticket=[on_ticket], pr=[on_pr], calls=[broken])
                self.advance()
                out, p = self.record([F1, F2])
                rnd = self.assertRecorded(out, blocking=[F1, F2])
                self.assertEqual(set(rnd), ROUND_KEYS)
                self.assertEqual(set(out), JSON_KEYS)
                self.assertEqual(len(self.warnings(p)), 1, p.stderr)


# =========================================================================== GitHub state
class FromGitHub(DismissTest):
    def test_without_local_rounds_github_reviews_resolve_the_selector(self):
        on_ticket = note(OWNER, "11:00", dismiss(F1, WHY1), 12)
        on_pr = note(OWNER, "11:30", dismiss(F12, "the owner accepts the off-diff read"), 34)
        shifted = at(F1, 4)
        rows = [  # name, the review's submitted time, the dismissed entries
            ("the owner's review before the comments", "10:00",
             [gone(shifted, "blocking", WHY1, on_ticket),
              gone(F12, "blocking", "the owner accepts the off-diff read", on_pr)]),
            ("a review submitted after them", "12:00", None),
        ]
        for name, submitted, dismissed in rows:
            with self.subTest(name):
                self.fresh()
                review, comments = github_review(9001, submitted, inline=[F1, F2], listed=[F12])
                self.serve(ticket=[on_ticket], pr=[on_pr], calls=github_calls(review, comments))
                out, p = self.record([shifted, F12, F2])
                self.assertEqual(out["round"], 1)
                if dismissed:
                    self.assertRecorded(out, blocking=[F2], dismissed=dismissed)
                    self.assertNotIn("unmatched", out)
                    self.assertEqual(self.warnings(p), [])
                    continue
                self.assertRecorded(out, blocking=[shifted, F12, F2])
                self.assertEqual(sorted(u.get("selector") for u in out.get("unmatched", [])),
                                 ["src/app.py:12", "src/app.py:3"])
                self.assertEqual(len(self.warnings(p)), 2, p.stderr)


# =========================================================================== the GPT check
class GptCheck(DismissTest):
    def test_a_receipt_retry_applies_a_newer_dismissal_without_sol(self):
        self.sol([S26])
        self.serve(review_id=7001)
        head = self.head()
        out, _p = self.paired([F1, F2])
        self.assertRecorded(out, blocking=[F1, F2, S26])
        self.stamp()
        self.assertEqual((len(self.gpt_runs()), len(self.posts())), (1, 1))
        posted = {"id": 7001, "user": {"login": OWNER}, "body": json.loads(self.posts()[0]["stdin"])["body"],
                  "commit_id": head, "submitted_at": f"{DAY}T10:00:00Z", "state": "COMMENTED"}
        github = [fakegh.rest_entry("pulls/34/reviews", "GET", exact=True, stdout_json=[posted])]

        first = note(OWNER, "11:00", dismiss(F1, WHY1), 34)
        second = note(OWNER, later(), dismiss(S26, "the caller checks the cap"), 12)
        steps = [  # name, ticket comments, PR comments, blocking, dismissed
            ("a dismissal after the post", [], [first], [F2, S26], [gone(F1, "blocking", WHY1, first)]),
            ("a newer one after that retry", [second], [first], [F2],
             [gone(F1, "blocking", WHY1, first), gone(S26, "blocking", "the caller checks the cap", second)]),
            ("both comments deleted", [], [], [F1, F2, S26], None),
        ]
        for name, ticket, pr, blocking, dismissed in steps:
            with self.subTest(name):
                self.serve(ticket=ticket, pr=pr, calls=github, review_id=7001)
                out, _p = self.paired([F1, F2])
                self.assertEqual(out["head"], head)
                self.assertEqual(out["round"], 1)
                self.assertRecorded(out, blocking=blocking, dismissed=dismissed)
                self.assertEqual(bag(self.stage_blocking()), bag(map(norm, blocking)))
                self.assertEqual(len(self.gpt_runs()), 1, "harness-gpt ran again")
                self.assertEqual((len(self.posts()), len(self.comment_reviews())), (1, 0))

    def test_dismissed_prior_blockers_are_not_sent_to_sol(self):
        f1_ticket = note(OWNER, "11:00", dismiss(F1, WHY1), 12)
        f2_pr = note(OWNER, "11:10", dismiss(F2, "covered by the fixture's own check"), 34)
        early = note(OWNER, "09:00", dismiss(F1, WHY1), 12)
        rows = [  # name, ticket, PR, Sol's new findings, prior blockers sent, blocking, dismissed, warnings
            ("dismissed on the ticket", [f1_ticket], [], [], [F2], [F2], None, 0),
            ("dismissed on the PR, Sol raising it again", [], [f2_pr], [F2], [F1], [F1],
             [gone(F2, "blocking", "covered by the fixture's own check", f2_pr)], 0),
            ("both dismissed", [f1_ticket], [f2_pr], [], [], [], None, 0),
            ("a comment older than the review", [early], [], [], [F1, F2], [F1, F2], None, 1),
        ]
        for name, ticket, pr, raised, prior, blocking, dismissed, warned in rows:
            with self.subTest(name):
                self.fresh()
                self.sol([], fixed=False)
                self.serve()
                self.paired([F1, F2])
                self.stamp()
                self.sol(raised, fixed=False)
                self.serve(ticket=ticket, pr=pr)
                self.advance()
                runs = len(self.gpt_runs())
                out, p = self.paired()
                self.assertEqual(len(self.gpt_runs()), runs + 1)
                brief = json.loads(self.gpt_runs()[-1]["stdin"])
                self.assertEqual(bag(brief["prior_blockers"]), bag(map(norm, prior)))
                rnd = self.load_rounds()[-1]
                self.assertEqual(bag(rnd["blocking"]), bag(map(norm, blocking)))
                self.assertEqual(bag(out["blocking"]), bag(map(norm, blocking)))
                self.assertEqual(out["record"], {"stage": "fix", "result": "fail" if blocking else "pass"})
                if dismissed is not None:
                    self.assertEqual(bag(rnd.get("dismissed", [])), bag(dismissed))
                self.assertEqual(len(self.warnings(p)), warned, p.stderr)
                if warned:
                    self.assertIn("src/app.py:3", self.warnings(p)[0])
                    self.assertIn(early["url"], self.warnings(p)[0])


if __name__ == "__main__":
    unittest.main()
