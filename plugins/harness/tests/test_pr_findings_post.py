"""Tests for `harness-pr findings` posting the review and ticketing out-of-scope findings (ticket #257, epic #249).

    python3 -m unittest discover -s plugins/harness/tests -p test_pr_findings_post.py -v

The fixture, its fakes and the helpers below (PostTest, split, norm, ...) are in pr_findings_support.py (#514), shared
with test_pr_findings_brief.py: a temporary HOME, a bare origin, the main checkout `proj` and the ticket worktree on
t/12-x, gh as fakegh.py and harness-board as a stub.

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

import json
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True

from pr_findings_support import *  # noqa: E402,F401,F403  (setUpModule and tearDownModule too)


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

    def test_independent_check_before_single_post_and_after_fix(self):
        self.board_conf(show={"12": epic_ticket(12, self.EPIC)})
        self.open_pr(34)
        exe = os.path.join(BINS["fakebin"], "harness-gpt")
        self.addCleanup(lambda: os.path.exists(exe) and os.unlink(exe))
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json"]
        first_head = self.head()
        initial = ["--head", first_head, "--review", self.review_file(POST_R1[:1])]
        executable(exe, "#!/bin/sh\nexit 1\n")
        self.assertNotEqual(self.pr(*args, *initial).returncode, 0)
        self.assertEqual(self.posts(), [])
        executable(exe, f"#!{sys.executable}\nimport json, sys\n"
                        "head = sys.argv[sys.argv.index('--head') + 1]\n"
                        "brief = json.load(sys.stdin)\n"
                        f"if brief['prior_blockers']: assert sys.argv[sys.argv.index('--base') + 1] == {first_head!r}\n"
                        "print(json.dumps(dict(head=head, model='gpt-6.2-sol', findings=[], prior_fixed=[True]*len(brief['prior_blockers']))))\n")
        first = self.pr(*args, *initial)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.posts(), [])                     # the check stops at triage (#534)
        verdicts = opus_verdicts(self.candidates(json.loads(first.stdout)))
        published, _p = self.triaged(verdicts)
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(published["blocking"])
        scenario = json.loads(Path(self.scenario_path).read_text())
        posted_body = json.loads(self.posts()[0]["stdin"])["body"]
        scenario["calls"].append({"match": ["api", "--paginate", "repos/o/r/pulls/34/reviews"],
                                  "stdout_json": [{"id": 55, "body": posted_body, "commit_id": first_head,
                                                   "submitted_at": "2026-01-01T00:00:00Z"}]})
        self.scenario(scenario["calls"])
        retry = self.pr(*args, "--head", self.head())
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.triaged(opus_verdicts(self.candidates(json.loads(retry.stdout))))
        self.assertEqual(len(self.posts()), 1)
        self.commit("src/app.py", "fixed = True\n")
        self.open_pr(34)
        second = self.pr(*args, "--head", self.head())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.posts()), 1)
        fixed, _p = self.triaged(opus_verdicts(self.candidates(json.loads(second.stdout))))
        self.assertEqual(fixed["record"], {"stage": "fix", "result": "pass"})
        self.assertEqual(len(self.posts()), 2)

    def test_unresolved_finding_survives_a_rename(self):
        self.open_pr(34)
        exe = os.path.join(BINS["fakebin"], "harness-gpt")
        self.addCleanup(lambda: os.path.exists(exe) and os.unlink(exe))
        executable(exe, f"#!{sys.executable}\nimport json, sys\n"
                        "prior = json.load(sys.stdin)['prior_blockers']\n"
                        "print(json.dumps(dict(head=sys.argv[sys.argv.index('--head')+1], model='gpt-6.2-sol', "
                        "prior_fixed=[False]*len(prior), findings=[])))\n")
        old, new = "tests/test_app.py", "tests/test_renamed.py"
        finding = {"path": old, "line": 2, "severity": "medium", "body": "assertion is unconditional"}
        args = ["findings", "12", "--worktree", self.wt, "--gpt-check", "--json"]
        first = self.pr(*args, "--head", self.head(), "--review", self.review_file([finding]))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.triaged(opus_verdicts(self.candidates(json.loads(first.stdout))))
        self.git(self.wt, "mv", old, new)
        self.git(self.wt, "commit", "-qm", "rename")
        self.open_pr(34)
        second = self.pr(*args, "--head", self.head())
        self.assertEqual(second.returncode, 0, second.stderr)
        out, _p = self.triaged(opus_verdicts(self.candidates(json.loads(second.stdout))))   # Opus agrees: unresolved
        self.assertEqual(out["blocking"], [dict(finding, path=new, line=None)])

    def test_ticket_secrets_rejected_before_encoding(self):
        ticket = epic_ticket(12, self.EPIC)
        ticket["sections"]["tests"] = ['token = "' + "a" * 32 + '"']
        self.board_conf(show={"12": ticket})
        self.open_pr(34)
        p = self.pr("findings", "12", "--worktree", self.wt, "--gpt-check", "--head", self.head(),
                    "--review", self.review_file([]))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("secret-shaped", p.stderr)
        self.assertEqual(self.posts(), [])

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

    def test_pinned_review_only_posts_for_the_reviewed_clean_head(self):
        head = self.head()
        self.open_pr(52)
        args = ["findings", "12", "--worktree", self.wt, "--review", self.review_file([]), "--head"]
        stale = self.pr(*args, self.merge_base)
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(self.posts(), [])
        posted = self.pr(*args, head)
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(json.loads(self.posts()[0]["stdin"])["commit_id"], head)
        write(os.path.join(self.wt, "dirty.txt"), "new input")
        self.assertNotEqual(self.pr(*args, head).returncode, 0)
        self.assertEqual(len(self.posts()), 1)

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
