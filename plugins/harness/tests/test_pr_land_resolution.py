"""harness-pr land: the Resolution comment it leaves on each ticket it lands, posted through the real harness-board.

Split from test_pr_land.py (#447: that module alone ran past the 180 s a module may take); the fixtures are in
pr_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_land_resolution.py" -v
"""

from __future__ import annotations

import unittest

from pr_support import *  # noqa: F401,F403


# (the fixture as JSON, no_sha): the bodies its land posts on a fresh ticket; LandResolution.fresh_landing fills it.
FRESH_LANDINGS = {}


class LandResolution(LandBase):
    """`land` leaves one `**Resolution**` comment on the ticket, posted through `harness-board comment` before the
    `Landed in` comment. These run the real harness-board (no stubs), so every comment reaches the fake gh's log."""

    def resolution_calls(self, fx, state="OPEN", issue_state="OPEN", comments=(), view_files=None, after=None,
                         no_sha=False):
        """The land conversation for the fixture's PR. `view_files`: the files `gh pr view --json` holds (all of
        the fixture's by default; GitHub stops at 100), while the files endpoint always lists them all; `after`:
        the answer to the PR read after the merge (`gh pr view P --json body,...`), in place of the PR's data;
        `no_sha`: GitHub reports no merge commit (True) or the read fails ("fail"), so the land knows no sha and
        comments `Landed in PR #N`."""
        pr, sha = str(fx["pr"]), fx["sha"]
        if no_sha:
            fx, sha = dict(fx, sha=""), ""
        view = pr_data(dict(fx, files=fx["files"] if view_files is None else view_files))
        url = f"https://github.com/o/r/pull/{pr}"
        issue = {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12",
                 "body": TICKET_12_BODY, "state": issue_state,
                 "labels": [{"name": "task"}, {"name": "status:in-review"}], "comments": list(comments)}
        rest = [{"body": c["body"], "user": {"login": c["author"]["login"]}, "created_at": c["createdAt"]}
                for c in comments]
        # the review gate's reads: no harness review, so the gate lets the land through
        gate = land_gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}")
        return gate + [
                {"match": ["pr", "view", pr, "--json", "mergeCommit"],
                 **({"stderr": "GraphQL: timeout\n", "exit": 1} if no_sha == "fail"
                    else {"stdout_json": {"mergeCommit": {"oid": sha} if sha else None}})},
                {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
                 "stdout_json": land_view(url=url, state=state)},
                REPO_VIEW,
                {"match": ["pr", "merge", pr], "stdout": ""},
                {"match": ["pr", "view", pr], **(after or {"stdout_json": view})},
                *files_calls(pr, fx["files"]),
                {"match": ["issue", "view", "12"], "stdout_json": issue},
                {"match": ["issue", "edit", "12"], "stdout": ""},
                {"match": ["issue", "close", "12"], "stdout": ""},
                {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#issuecomment-9\n"},
                {"contains": ["repos/o/r/issues/12/comments", "POST"], "stdout_json": {"id": 9}},
                {"contains": ["repos/o/r/issues/12/comments"], "stdout_json": rest},
                {"match": ["issue", "view", "3"],
                 "stdout_json": {"state": "OPEN", "subIssuesSummary": {"total": 2, "completed": 1}}}]

    def land(self, fx, **scenario):
        self.scenario(self.resolution_calls(fx, **scenario))
        return self.ok("land", str(fx["pr"]), cwd=self.main, stubs=False)

    def posted(self, number=None):
        """(index in the gh log, issue number, body) of every comment posted: `gh issue comment N`, `gh issue close
        N --comment` and `gh api repos/o/r/issues/N/comments` with a body."""
        out = []
        for i, call in enumerate(self.gh_calls()):
            a, body, n = call["argv"], None, None
            if a[:2] in (["issue", "comment"], ["issue", "close"]) and len(a) > 2:
                n = a[2]
                for flag in ("--body", "-b", "--comment", "-c"):
                    if flag in a[:-1]:
                        body = a[a.index(flag) + 1]
                for flag in ("--body-file", "-F"):
                    if flag in a[:-1]:
                        path = a[a.index(flag) + 1]
                        if path == "-":
                            body = call["stdin"]
                        elif os.path.exists(path):
                            with open(path, encoding="utf-8") as fh:
                                body = fh.read()
            elif a[:1] == ["api"]:
                write = fakegh.issue_write(fakegh.parse_api(a, call.get("stdin") or ""))   # #447: any REST form
                if write and write[0] == "comment":
                    n, body = write[1], write[2]
            if body is not None and (number is None or n == str(number)):
                out.append((i, n, body))
        return out

    def resolutions(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith(RESOLUTION)]

    def landed(self, number=None):
        return [(i, n, b) for i, n, b in self.posted(number) if b.lstrip().startswith("Landed in")]

    def as_comments(self, bodies):
        return [{"author": {"login": "rjeskuri"}, "body": b, "createdAt": f"2026-10-13T1{k}:00:00Z"}
                for k, b in enumerate(bodies)]

    def first(self, lines, pred, what):
        for i, line in enumerate(lines):
            if pred(line):
                return i
        self.fail(f"{what} missing from the Resolution:\n" + "\n".join(lines))

    def assertFrame(self, body, fx):
        """The heading, the harness marker last, the PR link and the full squash sha; the body's lines."""
        self.assertTrue(body.lstrip().startswith(RESOLUTION), body)
        lines = body.rstrip().splitlines()
        self.assertEqual(lines[-1].strip(), HARNESS_MARK, body)
        self.assertIn(f"https://github.com/o/r/pull/{fx['pr']}", body)
        self.assertIn(fx["sha"], body, "the full squash sha missing from the Resolution")
        return lines

    def file_positions(self, lines, fx):
        """The index of each changed file's list line: exactly one of its own per file."""
        file_lines = {}
        for i, line in enumerate(lines):
            m = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if m:
                text = m.group(1).replace("`", "").strip()
                for path in fx["files"]:
                    if text == path or text.startswith(path + " "):
                        file_lines.setdefault(path, []).append(i)
        for path in fx["files"]:
            self.assertEqual(len(file_lines.get(path, [])), 1,
                             f"changed file {path} wants exactly one list line of its own:\n" + "\n".join(lines))
        return [i for found in file_lines.values() for i in found]

    def assertRedKind(self, lines, fx):
        """The red-run kind on exactly one line, and that line is about the red run."""
        kind = re.compile(r"\b" + fx["red_kind"] + r"\b")
        kind_lines = [line for line in lines if kind.search(line)]
        self.assertEqual(len(kind_lines), 1, f"want one red-run kind line, got {kind_lines}")
        self.assertRegex(kind_lines[0], r"(?i)\bred\b", "the kind belongs on a line about the red run")

    def assertVerifyResult(self, lines, fx):
        """The last line of the verify fence: once, outside any code fence; the earlier lines left out."""
        verify = [line for line in fx["verify"] if line.strip()]
        fenced, where = False, []
        for line in lines:
            if line.strip().startswith("```"):
                fenced = not fenced
            elif verify[-1] in line:
                where.append(fenced)
        self.assertEqual(where, [False],
                         f"the verify result line {verify[-1]!r} wants one unfenced line:\n" + "\n".join(lines))
        for text in verify[:-1]:
            self.assertNotIn(text, "\n".join(lines), f"the Resolution copies the earlier verify line {text!r}")

    def assertResolution(self, body, fx):
        """The Resolution carries the PR's summary, every changed file, the checklist as landed, the red-run
        kind and the verify result from the PR body, the PR link and the squash sha, and the harness marker."""
        lines = self.assertFrame(body, fx)

        # the summary, then the changed files (one list line each), then the checklist as landed
        at_summary = self.first(lines, lambda line: fx["summary"] in line, "the PR summary")
        at_files = self.file_positions(lines, fx)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in fx["dod"]]
        self.assertLess(at_summary, min(at_files), "the summary comes before the files")
        self.assertLess(max(at_files), min(at_dod), "the files come before the checklist")
        self.assertEqual(at_dod, sorted(at_dod), "the checklist keeps its order")

        self.assertRedKind(lines, fx)
        self.assertVerifyResult(lines, fx)

        # what the Resolution leaves out of the PR body: the red tail, the tests section, the review, the
        # risks and the closing line
        left_out = fx["red_tail"] + [
            fx["test_file"], "::test_main", "Test files run", fx["review"], "## Review", "## Risks", "None noted.",
            "Closes #12", "## Tests"]
        for text in left_out:
            self.assertNotIn(text, body, f"the Resolution copies {text!r} from the PR body")

    # ------------------------------------------------------------------ fresh lands
    def test_land_posts_one_resolution_after_the_merge(self):
        self.land(FIXTURE_A)
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        index, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_A)
        for other in FIXTURE_B["files"] + [FIXTURE_B["summary"], FIXTURE_B["sha"][:12]]:
            self.assertNotIn(other, body)
        calls = self.gh_calls()
        merge = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["pr", "merge"])
        self.assertGreater(index, merge, "the Resolution is posted after the merge")
        posted = fakegh.gh_view([calls[index]], self.scenario_path)
        self.assertEqual([a[:3] for a in posted], [["issue", "comment", "12"]],
                         "posted as a comment on the ticket (harness-board comment, #447: through REST)")
        # the Landed comment is unchanged: one, `Landed in <sha>`, as harness-board posts it, after the Resolution
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in, so a resume never misses it")

    def test_land_resolution_follows_the_pr_it_lands(self):
        """Another PR, another sha, more files, an unchecked box, an import red run; GitHub closed the ticket."""
        self.land(FIXTURE_B, issue_state="CLOSED")
        found = self.resolutions()
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        _, number, body = found[0]
        self.assertEqual(number, "12")
        self.assertResolution(body, FIXTURE_B)
        for other in FIXTURE_A["files"][2:] + [FIXTURE_A["summary"], FIXTURE_A["sha"][:12], "OK (skipped=1)"]:
            self.assertNotIn(other, body)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.landed(12)],
                         [f"Landed in {FIXTURE_B['sha']}"])

    # ------------------------------------------------------------------ resumed lands
    def land_then_resume(self, fx, keep):
        """Land once, then land again on the merged PR with the ticket showing the first run's comments that
        `keep` selects. Returns (Resolutions of the first run, Resolutions and Landed of the second)."""
        self.land(fx)
        first = self.resolutions(12)
        bodies = [b for _, _, b in self.posted(12) if keep(b)]
        self.land(fx, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        return first, self.resolutions(), self.landed()

    def test_resumed_land_adds_no_second_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: True)
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "a resumed land must not post a second Resolution")
        self.assertEqual(landed, [], "nor a second Landed comment")

    def test_resumed_land_with_only_the_resolution_posts_the_landed_comment(self):
        first, again, landed = self.land_then_resume(FIXTURE_B, keep=lambda b: b.lstrip().startswith(RESOLUTION))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "the Resolution already there is not repeated")
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                         [f"Landed in {FIXTURE_B['sha']}"])

    def test_resumed_land_after_the_landed_comment_posts_no_resolution(self):
        first, again, landed = self.land_then_resume(FIXTURE_A, keep=lambda b: b.lstrip().startswith("Landed in"))
        self.assertEqual(len(first), 1, "the first land posts the Resolution")
        self.assertEqual(again, [], "once Landed in is on the ticket the land is recorded; no Resolution")
        self.assertEqual(landed, [])

    # ------------------------------------------------------------------ a reopened ticket's second landing (#24)
    REOPENED = "Reopened: the first pass broke the hooks."

    def fresh_landing(self, fx, no_sha=False):
        """The bodies a land of `fx` (GitHub reporting no merge commit when `no_sha`) posts on a ticket with no
        comments yet, in order: what a first pass leaves on the ticket, or this landing's own first run. They depend
        on nothing but the fixture's PR (resolution_body, `Landed in`), so each is landed once per module run and
        kept for the tests that start from it (#378)."""
        key = (json.dumps(fx, sort_keys=True), no_sha)
        if key not in FRESH_LANDINGS:
            self.land(fx, no_sha=no_sha)
            FRESH_LANDINGS[key] = [b for _, _, b in self.posted(12)]
        return list(FRESH_LANDINGS[key])

    @staticmethod
    def split_landing(bodies):
        """(the Resolutions, the `Landed in` comments) among `bodies`, as resolutions() and landed() tell them."""
        return ([b for b in bodies if b.lstrip().startswith(RESOLUTION)],
                [b for b in bodies if b.lstrip().startswith("Landed in")])

    def first_pass(self, fx):
        """Land `fx` on a fresh ticket and return the bodies it posted: its Resolution and `Landed in`."""
        bodies = self.fresh_landing(fx)
        self.assertEqual(len(bodies), 2, f"the first pass posts a Resolution and Landed in: {bodies}")
        return bodies

    def old_resolution(self, pr_line, sha):
        """A Resolution in the land's format, with the given `PR:` line and squash sha (None: no such line)."""
        closing = ([pr_line] if pr_line else []) + ([f"Squash commit: {sha}"] if sha else [])
        return "\n".join([RESOLUTION, "", "A first pass that was reopened.", "", "Files changed (1):",
                          "- `old/first_pass.py`", "", *closing, "", HARNESS_MARK])

    def assertSecondLanding(self, fx, old_texts):
        """This land of `fx` posted exactly two comments on #12: a Resolution of `fx` (naming its PR, not an old
        one) and then `Landed in <fx's sha>`."""
        posted = self.posted(12)
        self.assertEqual(len(posted), 2, f"want a Resolution and Landed in for PR #{fx['pr']}, got {posted}")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
        index, _, body = found[0]
        self.assertResolution(body, fx)
        for text in old_texts:
            self.assertNotIn(text, body, "the new Resolution is about this PR only")
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_second_landing_of_a_reopened_ticket_comments_resolution_and_landed_in(self):
        """A ticket landed once (its Resolution and `Landed in <old sha>` on it), reopened, and landed again by
        another PR: this landing gets its own Resolution, naming the new PR, and `Landed in <new sha>`. Several
        pairs, so neither the old nor the new fixture's text can be hard-coded; GitHub has or has not closed it."""
        for old, new, issue_state in ((FIXTURE_A, FIXTURE_B, "CLOSED"), (FIXTURE_B, FIXTURE_A, "OPEN"),
                                      (FIXTURE_C, FIXTURE_A, "CLOSED"), (FIXTURE_A, FIXTURE_C, "OPEN")):
            with self.subTest(old=old["pr"], new=new["pr"]):
                bodies = self.first_pass(old) + [self.REOPENED]
                p = self.land(new, issue_state=issue_state, comments=self.as_comments(bodies))
                self.assertSecondLanding(new, [f"https://github.com/o/r/pull/{old['pr']}", old["sha"],
                                               old["summary"]])
                ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
                self.assertEqual(len(ticket), 1, p.stdout)
                self.assertIn("commented", ticket[0])
                self.assertNotIn("already there", ticket[0])

    def test_second_landing_ignores_every_shape_of_an_earlier_passs_comments(self):
        """Only this landing's own text counts as already landed: `Landed in <this sha>` (a whole token) or
        `Landed in PR #<this PR>`, and a Resolution whose `PR:` line is this PR's URL. An older pass's comments
        in any shape do not, including ones that share a prefix with this sha, PR number or URL. A Resolution
        with no `PR:` line names no PR, so it is not this landing's either. Landing FIXTURE_B (PR #57)."""
        new = FIXTURE_B
        sha = new["sha"]
        old_sha = FIXTURE_A["sha"]
        cases = [
            ("Landed in PR #34 and its Resolution",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", old_sha), "Landed in PR #34"]),
            ("a Resolution with no PR line",
             [self.old_resolution(None, old_sha), f"Landed in {old_sha}"]),
            ("PR #570 and pull/570, which start with this PR's number and URL",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570"]),
            ("a different sha sharing this sha's first 12 characters",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:12] + "0" * 28),
              f"Landed in {sha[:12]}{'0' * 28}"]),
            ("an abbreviated sha that is a prefix of this one",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha[:7]), f"Landed in {sha[:7]}"]),
            ("a longer hex token that starts with this sha",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha + "abc"), f"Landed in {sha}abc"]),
            ("Landed in PR #570 followed by more text",
             [self.old_resolution("PR: https://github.com/o/r/pull/570", old_sha), "Landed in PR #570, see notes"]),
            # a Resolution belongs to the PR its `PR:` line names, whatever sha it quotes
            ("another PR's Resolution quoting this sha, and no Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), "Reverted by hand."]),
            ("another PR's Resolution quoting this sha, and its Landed in",
             [self.old_resolution("PR: https://github.com/o/r/pull/34", sha), f"Landed in {old_sha}"]),
        ]
        for name, old in cases:
            with self.subTest(name):
                bodies = [old[0], old[1] + "\n\n" + HARNESS_MARK, self.REOPENED]
                self.land(new, issue_state="CLOSED", comments=self.as_comments(bodies))
                self.assertSecondLanding(new, ["A first pass that was reopened.", "old/first_pass.py"])

    def test_resumed_land_posts_only_what_is_missing(self):
        """A reopened ticket's second land, resumed: the ticket carries the first pass's two comments and some of
        this landing's. With this PR's Resolution and no `Landed in <this sha>`, only `Landed in` is posted; with
        both (or `Landed in` alone, which records the land), nothing is; `Landed in PR #<this PR>` counts as this
        landing's Landed comment too. Run with the fixtures both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            res, mine = self.split_landing(self.fresh_landing(new))   # this landing's own comments, as posted
            self.assertEqual((len(res), len(mine)), (1, 1))
            by_pr = f"Landed in PR #{new['pr']}\n\n{HARNESS_MARK}"
            cases = [("this Resolution only", res, [f"Landed in {new['sha']}"]),
                     ("this Resolution and Landed in", res + mine, []),
                     ("this Landed in only", mine, []),
                     ("this Resolution and Landed in PR #", res + [by_pr], [])]
            for name, kept, wanted in cases:
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                    self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "no second merge")
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_counts_this_prs_resolution_by_its_pr_line(self):
        """A Resolution is this landing's when its `PR:` line is this PR's URL, whatever sha it quotes or none:
        then only `Landed in <this sha>` is posted, never a second Resolution. A `Landed in` for this PR with
        trailing text after the whole number (`PR #57, see notes`) records the land; nothing is posted."""
        new, old = FIXTURE_B, FIXTURE_A
        url = f"PR: https://github.com/o/r/pull/{new['pr']}"
        first = self.first_pass(old) + [self.REOPENED]
        cases = [("this PR's Resolution without a Squash commit line",
                  [self.old_resolution(url, None)],
                  [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution quoting another sha",
                  [self.old_resolution(url, FIXTURE_C["sha"])], [f"Landed in {new['sha']}"]),
                 ("this PR's Resolution and Landed in PR #57 with more text",
                  [self.old_resolution(url, new["sha"]), f"Landed in PR #{new['pr']}, see notes\n\n{HARNESS_MARK}"],
                  [])]
        for name, kept, wanted in cases:
            with self.subTest(name):
                self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept))
                self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_land_without_a_merge_sha_on_a_reopened_ticket(self):
        """GitHub reports no merge commit, so the land comments `Landed in PR #<pr>`. A second landing still gets
        its own Resolution (naming this PR) and `Landed in PR #<pr>` despite the first pass's comments and an old
        `Landed in PR #570`; a resume with this PR's Resolution posts only `Landed in PR #<pr>`; with both,
        nothing; never a second Resolution. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            by_pr = f"Landed in PR #{new['pr']}"
            first = self.first_pass(old) + [f"Landed in PR #{new['pr']}0\n\n{HARNESS_MARK}", self.REOPENED]
            res, mine = self.split_landing(self.fresh_landing(new, no_sha=True))   # this landing's own, as posted
            self.assertEqual(len(res), 1)
            self.assertNotIn("Squash commit", res[0], "no merge sha, no squash line")
            with self.subTest(old=old["pr"], new=new["pr"], case="second landing"):
                self.land(new, issue_state="CLOSED", comments=self.as_comments(first), no_sha=True)
                posted = self.posted(12)
                self.assertEqual(len(posted), 2, f"want a Resolution and {by_pr}, got {posted}")
                found = self.resolutions(12)
                self.assertEqual(len(found), 1, f"want one new Resolution, got {posted}")
                index, _, body = found[0]
                self.assertIn(f"PR: https://github.com/o/r/pull/{new['pr']}", body)
                self.assertIn(new["summary"], body)
                for text in (f"https://github.com/o/r/pull/{old['pr']}", old["sha"], old["summary"]):
                    self.assertNotIn(text, body, "the new Resolution is about this PR only")
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [by_pr])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")
            for name, kept, wanted in (("this Resolution only", res, [by_pr]), ("both", res + mine, [])):
                with self.subTest(old=old["pr"], new=new["pr"], case=name):
                    self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                              no_sha=True)
                    self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                    self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)], wanted)

    def test_resumed_land_without_a_sha_knows_its_own_landed_comment_by_the_resolutions_squash_sha(self):
        """The first run knew the sha and posted this PR's Resolution (`Squash commit: S`) and `Landed in S`; the
        resume cannot read the merge commit (null, or the read fails). `Landed in S` is still this landing's,
        found through the Squash commit of the Resolution naming this PR, so nothing is posted. An older pass's
        `Landed in <its sha>`, whose Resolution names another PR, is not: with only this PR's Resolution and
        that, only `Landed in PR #<pr>` is posted. Both ways round."""
        for old, new in ((FIXTURE_A, FIXTURE_B), (FIXTURE_B, FIXTURE_A)):
            first = self.first_pass(old) + [self.REOPENED]
            res, mine = self.split_landing(self.fresh_landing(new))   # the first run of this landing, sha known
            self.assertIn(f"Squash commit: {new['sha']}", res[0])
            self.assertEqual([b.replace(HARNESS_MARK, "").strip() for b in mine], [f"Landed in {new['sha']}"])
            for how in (True, "fail"):
                for name, kept, wanted in (("both", res + mine, []),
                                           ("this Resolution only", res, [f"Landed in PR #{new['pr']}"])):
                    with self.subTest(old=old["pr"], new=new["pr"], merge_commit=how, case=name):
                        self.land(new, state="MERGED", issue_state="CLOSED", comments=self.as_comments(first + kept),
                                  no_sha=how)
                        self.assertEqual(self.resolutions(12), [], "this PR's Resolution is there; none again")
                        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in self.posted(12)],
                                         wanted)

    # ------------------------------------------------------------------ the PR body's shape
    def one_resolution(self):
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"want one Resolution comment, got {len(found)}: {self.posted()}")
        return found[0]

    def test_land_resolution_carries_every_line_of_a_longer_summary(self):
        """A Summary of two paragraphs and a bullet list (FIXTURE_C, edited by hand): every line of it is copied,
        in order, ahead of the files; the Risks section stays out."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        self.assertResolution(body, FIXTURE_C)
        lines = body.rstrip().splitlines()
        at = []
        for text in FIXTURE_C["summary_lines"]:
            wanted = text[2:] if text.startswith("- ") else text
            at.append(self.first(lines, lambda line, w=wanted: w in line, f"summary line {wanted!r}"))
        self.assertEqual(at, sorted(at), "the summary keeps its order")
        self.assertLess(max(at), min(self.file_positions(lines, FIXTURE_C)), "the whole summary precedes the files")
        for text in FIXTURE_C["risks"] + [FIXTURE_A["summary"], FIXTURE_B["summary"]]:
            self.assertNotIn(text, body)

    def test_land_resolution_reads_the_checklist_from_the_definition_of_done_only(self):
        """FIXTURE_C again: the `- [ ]` follow-up under Risks is not part of the checklist, while the wrapped
        item's continuation line is, and stays with its item."""
        self.land(FIXTURE_C)
        _, _, body = self.one_resolution()
        lines = self.assertFrame(body, FIXTURE_C)
        for text in FIXTURE_C["risks"]:
            self.assertNotIn(text, body, "a checkbox outside the Definition of done is not copied")
        self.assertNotIn("nightly", body)
        at_dod = [self.first(lines, lambda line, item=item: item in line, f"checklist line {item!r}")
                  for item in FIXTURE_C["dod"]]
        wrapped = [i for i, line in enumerate(lines) if FIXTURE_C["dod_wrapped"] in line]
        self.assertEqual(len(wrapped), 1, f"the wrapped item's continuation line wants one line:\n{body}")
        self.assertTrue(at_dod[1] <= wrapped[0] < at_dod[2],
                        f"the continuation line belongs with its item, between {at_dod[1]} and {at_dod[2]}:\n{body}")

    def test_land_resolution_finds_the_verify_result_whatever_the_fence_layout(self):
        """The verify fence need not follow its label directly: a blank line between, a ```text fence, or the
        `## Verify` heading of the PR template harness-scaffold installs as .github/pull_request_template.md. The
        last line of the fence is the result either way."""
        variants = [("a blank line before the fence", "Verify:\n\n```\n",
                     ["Ran 7 tests in 0.512s", "", "OK (expected failures=1)"]),
                    ("a text fence", "Verify:\n```text\n",
                     ["lint: clean", "Ran 52 tests in 1.907s", "", "OK (skipped=3)"]),
                    ("the PR template's heading", "## Verify\n```\n",
                     ["python3 -m unittest: 61 tests, all green", "VERIFY OK"])]
        for name, label, verify in variants:
            with self.subTest(name):
                fx = dict(FIXTURE_A, verify=verify)
                rendered = pr_body_text(fx)
                self.assertEqual(rendered.count("Verify:\n```\n"), 1)
                fx["body"] = rendered.replace("Verify:\n```\n", label)
                self.land(fx)
                _, _, body = self.one_resolution()
                self.assertResolution(body, fx)

    def test_land_resolution_without_a_red_run_line_has_no_red_line(self):
        """A body with no red-run paragraph at all (written by hand on a no-tests ticket): the land finishes,
        the Resolution says nothing about a red run and invents no kind."""
        fx = dict(FIXTURE_B, body=pr_body_text(FIXTURE_B, red=False))
        self.assertNotIn("Red run", fx["body"])
        self.land(fx)
        index, _, body = self.one_resolution()
        lines = self.assertFrame(body, fx)
        self.file_positions(lines, fx)
        self.assertVerifyResult(lines, fx)
        self.assertEqual([line for line in lines if re.search(r"(?i)\bred\b", line)], [],
                         f"no red run in the PR body, so no red line in the Resolution:\n{body}")
        self.assertIsNone(re.search(r"\bimport\b", body), f"a red-run kind invented:\n{body}")
        self.assertNotIn("ModuleNotFoundError", body)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0])

    def test_land_without_summary_or_verify_still_posts_the_resolution_and_lands(self):
        """A hand-written body missing its Summary and/or its Verify block: the land finishes, one Resolution
        still names every file, the checklist, the PR and the sha, nothing is invented, and `Landed in` follows."""
        cases = [("no Summary", FIXTURE_A, {"summary": False}),
                 ("no Verify", FIXTURE_B, {"verify": False}),
                 ("neither", FIXTURE_B, {"summary": False, "verify": False})]
        for name, base, drop in cases:
            with self.subTest(name):
                fx = dict(base, body=pr_body_text(base, **drop))
                self.land(fx)
                index, number, body = self.one_resolution()
                self.assertEqual(number, "12")
                lines = self.assertFrame(body, fx)
                self.file_positions(lines, fx)
                for item in fx["dod"]:
                    self.assertIn(item, body, f"checklist line {item!r} missing:\n{body}")
                self.assertRedKind(lines, fx)
                if drop.get("summary") is False:
                    self.assertNotIn(fx["summary"], body, "no Summary in the PR body, none to copy")
                else:
                    self.assertIn(fx["summary"], body)
                if drop.get("verify") is False:
                    for text in fx["verify"]:
                        if text.strip():
                            self.assertNotIn(text, body, "no Verify in the PR body, none to copy")
                else:
                    self.assertVerifyResult(lines, fx)
                for text in fx["red_tail"] + ["## Tests", "## Review", "## Risks", "Closes #12"]:
                    self.assertNotIn(text, body)
                landed = self.landed(12)
                self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed],
                                 [f"Landed in {fx['sha']}"])
                self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    # ------------------------------------------------------------------ every changed file
    def files_listed(self, body):
        """(N of the one `Files changed (N):` line, the paths on the list lines that follow it)."""
        lines = body.splitlines()
        heads = [i for i, line in enumerate(lines) if line.strip().startswith("Files changed")]
        self.assertEqual(len(heads), 1, f"want one `Files changed (N):` line:\n{body}")
        m = re.match(r"^Files changed \((\d+)\):$", lines[heads[0]].strip())
        self.assertIsNotNone(m, f"the files line reads `Files changed (N):`, got {lines[heads[0]]!r}")
        paths = []
        for line in lines[heads[0] + 1:]:
            item = re.match(r"^\s*[-*+]\s+(.*)$", line)
            if not item:
                break
            paths.append(item.group(1).replace("`", "").strip().split(" ")[0])
        return int(m.group(1)), paths

    def test_land_resolution_lists_every_changed_file_past_the_first_hundred(self):
        """`gh pr view --json files` stops at 100 files. A PR touching 130 gets all 130 in its Resolution, from
        the paginated files endpoint, and the count says 130; the rest of the Resolution and `Landed in` as
        usual."""
        fx = FIXTURE_WIDE
        self.land(fx, view_files=fx["files"][:100])
        index, number, body = self.one_resolution()
        self.assertEqual(number, "12")
        count, listed = self.files_listed(body)
        for nth in (101, 130):
            path = fx["files"][nth - 1]
            self.assertTrue(path in listed, f"changed file no. {nth}, {path}, missing from the Resolution, which "
                                            f"lists {len(listed)} files under `Files changed ({count}):`")
        self.assertEqual(count, 130, "want `Files changed (130):`")
        self.assertEqual(sorted(listed), sorted(fx["files"]), "every changed file, each listed once")
        self.assertResolution(body, fx)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {fx['sha']}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")

    def test_land_resolution_lists_a_short_prs_files_once(self):
        """Three files, listed alike by the PR view and the files endpoint: `Files changed (3):` and each path
        once, whichever the land reads."""
        self.land(FIXTURE_A)
        _, _, body = self.one_resolution()
        count, listed = self.files_listed(body)
        self.assertEqual(sorted(listed), sorted(FIXTURE_A["files"]), f"each changed file once:\n{body}")
        self.assertEqual(count, 3, f"want `Files changed (3):`:\n{body}")
        self.assertResolution(body, FIXTURE_A)

    # ------------------------------------------------------------------ a failed read after the merge
    def test_land_posts_nothing_when_the_pr_read_after_the_merge_fails_and_the_resume_posts_both(self):
        """The merge goes through, then reading the PR for the Resolution (`gh pr view P --json body,...`) fails:
        no comment at all on the ticket, since `Landed in` alone would keep a resume from adding the Resolution,
        and land fails naming the step. A second land on the merged PR posts one Resolution, then `Landed in`."""
        self.scenario(self.resolution_calls(FIXTURE_A, after={"stderr": "GraphQL: something\n", "exit": 1}))
        p = self.pr("land", "34", cwd=self.main, stubs=False)
        self.assertOneLineError(p, "PR #34 is merged", "#12", "gh pr view 34", "GraphQL: something")
        self.assertRegex(p.stderr, r"(?i)\bresolution\b", "the error names the step that failed: the Resolution")
        argv = self.gh_argv()
        self.assertTrue(any(a[:2] == ["pr", "merge"] for a in argv), "the merge went through")
        self.assertTrue(any(a[:3] == ["pr", "view", "34"] and "--json" in a[:-1]
                            and "body" in a[a.index("--json") + 1].split(",") for a in argv),
                        "the land read the PR's body after the merge, and that read failed")
        self.assertEqual([a for a in argv if a[:2] == ["issue", "comment"]], [], "no issue comment at all")
        self.assertEqual(self.posted(), [], "no comment of any kind on the ticket")
        ticket = [line for line in p.stdout.splitlines() if line.startswith("Ticket:")]
        self.assertEqual(len(ticket), 1, p.stdout)
        self.assertNotIn("commented", ticket[0])

        bodies = [b for _, _, b in self.posted(12)]        # what the ticket shows the resume: nothing
        self.land(FIXTURE_A, state="MERGED", issue_state="CLOSED", comments=self.as_comments(bodies))
        self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "the resume does not merge again")
        found = self.resolutions(12)
        self.assertEqual(len(found), 1, f"the resume posts one Resolution, got {len(found)}: {self.posted()}")
        index, _, body = found[0]
        self.assertResolution(body, FIXTURE_A)
        landed = self.landed(12)
        self.assertEqual([b.replace(HARNESS_MARK, "").strip() for _, _, b in landed], [f"Landed in {SHA}"])
        self.assertLess(index, landed[0][0], "the Resolution goes before Landed in")


if __name__ == "__main__":
    unittest.main()
