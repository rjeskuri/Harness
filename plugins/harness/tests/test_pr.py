"""Tests for harness-pr, driven by the fake gh (fakegh.py), stub sibling tools and a fake claude.

    python3 -m unittest discover -s plugins/harness/tests -v

Each test has a temporary HOME and real git repositories: a light one (a repository with an `origin` remote
that is never contacted) for the subcommands that only talk to gh, and for open and land a bare origin, a
main checkout pushed to it and a ticket worktree on t/12-add-the-widget. `fakebin` holds gh (fakegh.py
behind a wrapper), git (a wrapper that logs and runs the real git), claude, terminal-notifier and osascript
fakes, a harness-work stub and python3; `stubbin` holds harness-board and harness-red stubs. The stubs log their
calls and print canned output, keyed by their first argument or their first two (`cost 12`); given a
`read_stdin` path they record what they read on stdin. gh, git and the stubs also append one line per call to
ORDER_LOG, so a test can check the order across tools. The fake executables are written once for the module: macOS checks a new executable on
its first run, which costs about half a second each time.
"""

from __future__ import annotations

import unittest

from pr_support import *  # noqa: F401,F403


class Open(WorktreeTest):
    def setUp(self):
        super().setUp()
        self.verify_log = write(os.path.join(self.tmp, "verify.log"),
                                "\n".join(f"verify line {i}" for i in range(1, 31)) + "\nVERIFY OK\n\n")
        self.review = write(os.path.join(self.tmp, "findings.json"), json.dumps(FINDINGS))

    def remote_head(self):
        p = subprocess.run([GIT, "--git-dir", self.origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
                           capture_output=True, text=True, env=self.env())
        return p.stdout.strip() if p.returncode == 0 else None

    def test_open_pushes_and_creates_a_draft_with_the_rendered_body(self):
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("high"))},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "12", "--verify-log", self.verify_log, "--review", self.review, cwd=self.wt)
        self.assertEqual(p.stdout, f"#34 {PR_URL}\n")
        self.assertEqual(self.remote_head(), self.git(self.wt, "rev-parse", "HEAD"))
        self.assertEqual(self.gh_argv(), [
            ["pr", "list", "--head", BRANCH, "--state", "open", "--json", "number,url"],
            ["pr", "create", "--draft", "--base", "main", "--head", BRANCH, "--title", "Add the widget (#12)",
             "--body-file", "-"]])
        self.assertLess(self.position(f"git -C {self.wt} push -u origin {BRANCH}"), self.position("gh pr create"))
        body = self.gh_calls()[1]["stdin"]
        lines = body.splitlines()
        self.assertEqual(lines[0], "Closes #12")
        self.assertIn("## Summary\nAdd a widget that frobs. It must frob twice.\n", body)
        self.assertNotIn("second paragraph", body)
        self.assertIn("## Definition of done\n- [ ] The widget frobs twice\n- [x] The docs mention it\n", body)
        self.assertIn("- `tests/test_widget.py::test_frob`: frobs twice\n"
                      "- `tests/test_widget.py::test_empty`: empty input", body)
        self.assertIn("Test files run: `tests/test_widget.py`", lines)
        self.assertIn("Red run before implementation (assertion, exit 1):", lines)
        self.assertIn("red line 26", lines)
        self.assertIn("red line 40", lines)
        self.assertNotIn("red line 25", lines)
        self.assertIn("verify line 12", lines)
        self.assertIn("VERIFY OK", lines)
        self.assertNotIn("verify line 11", lines)
        self.assertIn("4 findings: 1 high, 1 medium, 2 low.", lines)
        self.assertIn("- `widget.py:2`: Frobs only once on empty input.", lines)
        self.assertNotIn("Name is vague.", body)
        self.assertEqual(lines[-1], HIGH_RISK)
        self.assertNotIn("{{", body)
        self.assertEqual(self.stub_calls("harness-board"), [
            ["show", "12", "--json", "--repo", self.main],
            ["comment", "12", "--body", f"Pull request: {PR_URL}", "--repo", self.main]])
        self.assertEqual(self.stub_calls("harness-red"), [["status", "--json", "--worktree", self.wt]])

    def test_open_edits_the_existing_pr_and_prints_json(self):
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(board_ticket("low"))}},
                        "harness-red": {"status": {"stderr": "harness-red: No red run recorded\n", "exit": 1}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": [{"number": 34, "url": PR_URL}]},
                       {"match": ["pr", "edit", "34"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "#12", "--json", cwd=self.wt)
        self.assertEqual(json.loads(p.stdout), {"number": 34, "url": PR_URL})
        argv = self.gh_argv()
        self.assertEqual(argv[1], ["pr", "edit", "34", "--body-file", "-"])
        self.assertFalse(any(a[:2] == ["pr", "create"] for a in argv))
        body = self.gh_calls()[1]["stdin"]
        self.assertTrue(body.startswith("Closes #12\n"))
        self.assertIn("Red run before implementation (none, exit -):", body)
        self.assertIn("No red run recorded on this branch.", body)
        self.assertIn("No verify log attached.", body)
        self.assertIn("No review attached.", body)
        self.assertEqual(body.splitlines()[-1], "None noted.")
        self.assertEqual(self.stub_calls("harness-board"), [["show", "12", "--json", "--repo", self.main]])

    def test_open_shows_the_warning_lines_of_every_board_call(self):
        """#69: warnings pass through from every harness-board call, not only `status`: open's `show` and `comment`
        each exit 0 with a warning among other stderr; open's stderr is exactly those warnings, once each, in call
        order, and its stdout is unchanged."""
        show_warning = "harness-board: warning: the Projects board acme/3 was not updated: show (token-n1)"
        comment_warning = "harness-board: warning: the Projects board acme/3 was not updated: comment (token-p2)"
        self.outputs(**{"harness-board": {
            "show": {"stdout": json.dumps(board_ticket("low")),
                     "stderr": f"harness-board: note: chatter (token-q3)\n{show_warning}\n"},
            "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n",
                        "stderr": f"{comment_warning}\nwarning: chatter without the prefix (token-r4)\n"}},
            "harness-red": {"status": {"stdout": json.dumps(RED_RECORD)}}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        p = self.ok("open", "12", cwd=self.wt)
        self.assertEqual(p.stdout, f"#34 {PR_URL}\n")
        self.assertEqual(p.stderr.splitlines(), [show_warning, comment_warning], p.stderr)
        self.assertEqual([a[0] for a in self.stub_calls("harness-board")], ["show", "comment"])

    def test_open_refuses_a_dirty_tree(self):
        write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 3\n")
        write(os.path.join(self.wt, "notes.txt"), "scratch\n")
        p = self.pr("open", "12", cwd=self.wt)
        self.assertOneLineError(p, "uncommitted changes", "widget.py", "notes.txt")
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.stub_calls("harness-board"), [])
        self.assertIsNone(self.remote_head())

    def test_open_refuses_a_branch_that_is_not_the_tickets(self):
        self.git(self.wt, "switch", "-q", "-c", "feature/widget")
        self.assertOneLineError(self.pr("open", "12", cwd=self.wt), "not a ticket branch")
        self.git(self.wt, "switch", "-q", "-c", "t/13-other")
        self.assertOneLineError(self.pr("open", "12", "--worktree", self.wt), "ticket #13", "not #12")
        self.assertEqual(self.gh_calls(), [])

    def test_open_with_the_real_board_and_red_tools(self):
        """No stubs on PATH: harness-pr finds harness-board and harness-red next to itself."""
        write(self.state_path("red", "t-12-add-the-widget.json"), json.dumps(dict(RED_RECORD, current=None)))
        body = ("## Request\nAdd a widget that frobs.\n\n## Definition of done\n- [ ] The widget frobs twice\n\n"
                "## Tests that must pass\n- `tests/test_widget.py::test_frob`: frobs twice\n")
        issue = {"number": 12, "title": "Add the widget", "url": "https://github.com/o/r/issues/12", "body": body,
                 "labels": [{"name": "task"}, {"name": "risk:low"}], "state": "OPEN", "parent": None,
                 "blockedBy": [], "comments": []}
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue},
                       {"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#c1\n"}])
        p = self.ok("open", "12", "--json", cwd=self.wt, stubs=False)
        self.assertEqual(json.loads(p.stdout), {"number": 34, "url": PR_URL})
        calls = self.gh_calls()
        create = next(c for c in calls if c["argv"][:2] == ["pr", "create"])
        self.assertIn("- [ ] The widget frobs twice", create["stdin"])
        self.assertIn("- `tests/test_widget.py::test_frob`: frobs twice", create["stdin"])
        self.assertIn("Red run before implementation (assertion, exit 1):", create["stdin"])
        self.assertIn(["issue", "comment", "12", "--body", f"Pull request: {PR_URL}\n\n<!-- harness -->"],
                      [c["argv"] for c in calls])
        self.assertTrue(os.path.isfile(self.state_path("tickets", "12.json")))


class OpenNoTests(WorktreeTest):
    """A ticket flagged no-tests has no red run; the body says why instead of "No red run recorded"."""

    def open_body(self, flags, red_status):
        ticket = dict(board_ticket("low"), flags=list(flags), labels=["task", "status:in-progress", "risk:low"]
                      + list(flags))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": red_status}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        self.ok("open", "12", cwd=self.wt)
        return next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])["stdin"]

    def test_the_red_block_says_the_ticket_is_no_tests(self):
        # harness-red status on a no-tests ticket prints its message and exits 0 (not JSON), or has no record
        # and exits 1; either way the ticket's flag decides what the body says.
        for label, red_status in (("message", {"stdout": "ticket 12 is no-tests: no red run needed\n"}),
                                  ("refusal", {"stderr": "harness-red: No red run recorded\n", "exit": 1})):
            with self.subTest(red=label):
                body = self.open_body(["no-tests"], red_status)
                lines = body.splitlines()
                self.assertIn(NO_TESTS_RED, lines)
                self.assertNotIn(NO_RED, body)
                at = lines.index(NO_TESTS_RED)
                self.assertTrue(lines[at - 1] == "```" and lines[at + 1] == "```", "\n".join(lines[at - 2:at + 3]))
                self.assertTrue(lines[at - 2].startswith("Red run before implementation"), lines[at - 2])
                self.assertEqual(lines[0], "Closes #12")
                self.assertIn("## Definition of done\n- [ ] The widget frobs twice", body)
                self.assertIn("No verify log attached.", body)
                self.assertNotIn("{{", body)

    def open_body_labels(self, labels, flags, red_status):
        ticket = dict(board_ticket("low"), flags=list(flags), labels=["task", "status:in-progress", "risk:low"]
                      + list(labels))
        self.outputs(**{"harness-board": {"show": {"stdout": json.dumps(ticket)},
                                          "comment": {"stdout": "https://github.com/o/r/issues/12#c1\n"}},
                        "harness-red": {"status": red_status}})
        self.scenario([{"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"}])
        self.ok("open", "12", cwd=self.wt)
        return next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])["stdin"]

    def test_the_no_tests_json_object_from_harness_red_is_not_a_red_record(self):
        # `harness-red status --json` on a no-tests ticket prints {"no_tests": true, ...} and exits 0.
        base = {"no_tests": True, "ticket": 12, "message": "ticket 12 is no-tests: no red run needed"}
        for label, obj in (("plain", base),
                           ("with files", dict(base, run_files=["tests/leak.py"], test_files=["tests/leak2.py"]))):
            with self.subTest(obj=label):
                body = self.open_body(["no-tests"], {"stdout": json.dumps(obj)})
                self.assertIn(NO_TESTS_RED, body.splitlines())
                self.assertNotIn(NO_RED, body)
                self.assertNotIn("Test files run", body)
                self.assertNotIn("tests/leak", body)
                self.assertNotIn("unknown", body)
                self.assertNotIn("exit None", body)

    def test_the_body_trusts_flags_not_labels(self):
        # A `no-tests` label with empty flags: the body keeps the ordinary line.
        body = self.open_body_labels(["no-tests"], [], {"stderr": "harness-red: No red run recorded\n", "exit": 1})
        self.assertIn(NO_RED, body)
        self.assertNotIn(NO_TESTS_RED, body)

    def test_other_flags_keep_the_no_red_run_line(self):
        for flags in ([], ["needs-local"], ["priority:high", "needs-local"]):
            with self.subTest(flags=flags):
                body = self.open_body(flags, {"stderr": "harness-red: No red run recorded\n", "exit": 1})
                self.assertIn(NO_RED, body)
                self.assertNotIn("no-tests", body)

    def test_a_recorded_red_run_still_prints_its_tail_on_an_ordinary_ticket(self):
        body = self.open_body(["needs-local"], {"stdout": json.dumps(RED_RECORD)})
        self.assertIn("Red run before implementation (assertion, exit 1):", body.splitlines())
        self.assertIn("red line 40", body.splitlines())
        self.assertNotIn(NO_TESTS_RED, body)
        self.assertNotIn(NO_RED, body)

    def test_with_the_real_board_tool_the_no_tests_label_reaches_the_body(self):
        """No stubs: harness-board turns the `no-tests` label into the flag and harness-red has no record."""
        body = ("## Request\nFix the docs.\n\n## Definition of done\n- [ ] The guide is correct\n\n"
                "## Tests that must pass\n- None: docs only\n")
        issue = {"number": 12, "title": "Fix the docs", "url": "https://github.com/o/r/issues/12", "body": body,
                 "labels": [{"name": "task"}, {"name": "risk:low"}, {"name": "no-tests"}], "state": "OPEN",
                 "parent": None, "blockedBy": [], "comments": []}
        self.scenario([{"match": ["issue", "view", "12"], "stdout_json": issue},
                       {"match": ["pr", "list"], "stdout_json": []},
                       {"match": ["pr", "create"], "stdout": PR_URL + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": "https://github.com/o/r/issues/12#c1\n"}])
        self.ok("open", "12", "--json", cwd=self.wt, stubs=False)
        create = next(c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"])
        self.assertIn(NO_TESTS_RED, create["stdin"].splitlines())
        self.assertNotIn(NO_RED, create["stdin"])


class Review(PrTest):
    def findings(self, data):
        return write(os.path.join(self.tmp, "findings.json"), json.dumps(data))

    def test_review_routes_on_diff_findings_inline_and_the_rest_to_the_body(self):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
        # #67: the default model is now opus; this test is about routing, so it names the model it expects in the body.
        p = self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS), "--model", "fable")
        self.assertIn("7 findings (4 inline, 3 in the body)", p.stdout)
        calls = self.gh_calls()
        self.assertEqual([c["argv"] for c in calls], [
            ["repo", "view", "--json", "nameWithOwner"], ["pr", "diff", "34"],
            ["api", "-X", "POST", "repos/o/r/pulls/34/reviews", "--input", "-"]])
        sent = json.loads(calls[2]["stdin"])
        self.assertEqual(sent["event"], "COMMENT")
        self.assertEqual(sent["comments"], [
            {"path": "widget.py", "line": 2, "side": "RIGHT", "body": f"[high] Added line, wrong value.\n\n{MARKER}"},
            {"path": "widget.py", "line": 4, "side": "RIGHT", "body": f"[medium] Context line.\n\n{MARKER}"},
            {"path": "widget.py", "line": 22, "side": "RIGHT",
             "body": f"[low] A line whose text starts with ++ (not a header).\n\n{MARKER}"},
            {"path": "docs/new.md", "line": 2, "side": "RIGHT", "body": f"[low] New file.\n\n{MARKER}"}])
        self.assertEqual(sent["body"], "Harness review (fable): 7 findings, 2 high, 2 medium, 3 low.\n\n"
                                       "Not on a changed line:\n"
                                       "- `widget.py:10` [low] Between the hunks.\n"
                                       "- `old.py:1` [medium] The file is deleted.\n"
                                       "- `README.md` [high] No line given.\n\n" + MARKER)

    def test_review_falls_back_to_the_body_when_github_refuses_inline_comments(self):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST"], "responses": [
                           {"stderr": "gh: Unprocessable Entity (HTTP 422)\n", "exit": 1}, {"stdout": "{}"}]}])
        p = self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS[:2]), "--model", "opus")
        self.assertIn("refused the inline comments", p.stderr)
        posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(json.loads(posts[0]["stdin"])["comments"]), 2)
        second = json.loads(posts[1]["stdin"])
        self.assertEqual(second["comments"], [])
        self.assertTrue(second["body"].startswith("Harness review (opus): 2 findings, 1 high, 1 medium, 0 low."))
        self.assertIn("- `widget.py:2` [high] Added line, wrong value.", second["body"])

    def posted_body(self, *extra):
        self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                       {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
        self.ok("review", "34", "--findings", self.findings(REVIEW_FINDINGS[:2]), *extra)
        posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
        self.assertEqual(len(posts), 1)
        return json.loads(posts[0]["stdin"])["body"]

    def test_review_without_model_names_opus_in_the_body(self):
        # Ticket #67: the default model is opus, so the body names the model that actually ran.
        body = self.posted_body()
        self.assertTrue(body.startswith("Harness review (opus): 2 findings, 1 high, 1 medium, 0 low."), body)
        self.assertNotIn("fable", body)

    def test_review_model_option_is_named_in_the_body(self):
        for model in ("fable", "opus", "sonnet"):
            with self.subTest(model=model):
                body = self.posted_body("--model", model)
                self.assertTrue(body.startswith(f"Harness review ({model}): 2 findings, "), body)

    def test_review_summary_parses_with_autoland_to_the_counts_it_declares(self):
        """#280: the summary line `harness-pr review` posts is one autoland.review_counts (the single parser of the
        per-severity counts) reads back as the high, medium and low it declared, for several mixes and models."""
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        from harness_lib import autoland
        mixes = [{"high": 1, "medium": 0, "low": 0}, {"high": 0, "medium": 3, "low": 1},
                 {"high": 2, "medium": 1, "low": 4}, {"high": 0, "medium": 0, "low": 2}]
        for model in ("fable", "opus", "sonnet"):
            for mix in mixes:
                with self.subTest(model=model, **mix):
                    sevs = [sev for sev in mix for _ in range(mix[sev])]
                    data = [{"path": "widget.py", "line": 2 + i, "severity": sev, "body": f"Finding {sev} {i}."}
                            for i, sev in enumerate(sevs)]
                    self.scenario([REPO_VIEW, {"match": ["pr", "diff", "34"], "stdout": DIFF},
                                   {"match": ["api", "-X", "POST", "repos/o/r/pulls/34/reviews"], "stdout": "{}"}])
                    self.ok("review", "34", "--findings", self.findings(data), "--model", model)
                    posts = [c for c in self.gh_calls() if c["argv"][0] == "api"]
                    body = json.loads(posts[-1]["stdin"])["body"]
                    self.assertEqual(autoland.review_counts(body), mix, body)

    def test_review_with_no_findings_posts_a_plain_comment_review(self):
        self.scenario([{"match": ["pr", "review", "34"], "stdout": ""}])
        self.ok("review", "34", "--findings", self.findings([]), "--checked", "the red tests and the DoD")
        body = f"Harness review: no findings. Checked: the red tests and the DoD\n\n{MARKER}"
        self.assertEqual(self.gh_argv(), [["pr", "review", "34", "--comment", "-b", body]])


class Checks(PrTest):
    def rollup(self, *responses):
        view = {"match": ["pr", "view", "34", "--json", "statusCheckRollup"]}
        if len(responses) == 1:
            view["stdout_json"] = {"statusCheckRollup": responses[0]}
        else:
            view["responses"] = [{"stdout_json": {"statusCheckRollup": r}} for r in responses]
        return view

    def polls(self):
        return sum(1 for a in self.gh_argv() if a[:2] == ["pr", "view"])

    def test_green_rollup_exits_0(self):
        self.scenario([self.rollup(GREEN)])
        p = self.ok("checks", "34")
        self.assertIn("checks green", p.stdout)
        self.assertEqual(self.gh_argv(), [["pr", "view", "34", "--json", "statusCheckRollup"]])

    def test_pending_without_wait_is_one_poll_and_exit_8(self):
        self.scenario([self.rollup(PENDING)])
        p = self.fails(8, "checks", "34", "--json")
        data = json.loads(p.stdout)
        self.assertEqual(data["state"], "pending")
        self.assertEqual(data["pending"], ["build", "ci/legacy"])
        self.assertEqual(self.polls(), 1)

    def test_wait_polls_until_green(self):
        self.scenario([self.rollup(PENDING, PENDING, GREEN)])
        self.ok("checks", "34", "--wait")
        self.assertEqual(self.polls(), 3)

    def test_wait_gives_up_pending_at_the_timeout(self):
        self.scenario([self.rollup(PENDING)])
        p = self.fails(8, "checks", "34", "--wait", "--timeout", "0.05")
        self.assertIn("checks pending after", p.stdout)
        self.assertGreaterEqual(self.polls(), 1)

    def test_failure_names_the_checks_and_prints_the_failed_run_log(self):
        failing = [check_run("test", conclusion="FAILURE", url="https://github.com/o/r/actions/runs/987/job/654"),
                   status_context("ci/legacy", "ERROR"), check_run("lint"),
                   check_run("slow", status="QUEUED", conclusion="")]
        log = "\n".join(f"log line {i}" for i in range(1, 71)) + "\n"
        self.scenario([self.rollup(failing), {"match": ["run", "view", "987", "--log-failed"], "stdout": log}])
        p = self.fails(1, "checks", "34", "--wait")
        self.assertEqual(self.polls(), 1)
        self.assertIn(["run", "view", "987", "--log-failed"], self.gh_argv())
        self.assertIn("checks failed: test (failure), ci/legacy (error)", p.stdout)
        lines = p.stdout.splitlines()
        self.assertIn("log line 11", lines)
        self.assertIn("log line 70", lines)
        self.assertNotIn("log line 10", lines)
        self.assertIn("checks failed on PR #34: test, ci/legacy", p.stderr)

    def test_cancelled_counts_as_failed(self):
        self.scenario([self.rollup([check_run("build", conclusion="CANCELLED"), check_run("lint")])])
        p = self.fails(1, "checks", "34", "--wait")
        self.assertIn("build (cancelled)", p.stdout)
        self.assertEqual(self.polls(), 1)
        self.assertFalse(any(a[:2] == ["run", "view"] for a in self.gh_argv()))

    def test_no_checks_after_six_polls_warns_and_exits_0(self):
        self.scenario([self.rollup([])])
        p = self.ok("checks", "34", "--wait")
        self.assertIn("no checks registered", p.stderr)
        self.assertEqual(self.polls(), 6)


# =========================================================================== ready
class Ready(PrTest):
    def view(self, closing):
        return {"match": ["pr", "view", "34", "--json", "url,title,closingIssuesReferences"],
                "stdout_json": {"url": PR_URL, "title": "Add the widget (#12)",
                                "closingIssuesReferences": [{"number": n, "url": f"https://github.com/o/r/issues/{n}"}
                                                            for n in closing]}}

    def scenario(self, calls, default=None):
        super().scenario(calls + [REPO_VIEW] + gate_calls(commits=[pr_commit("a", at(9))]), default)

    def test_ready_marks_the_pr_and_the_closing_ticket_and_notifies(self):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
        p = self.ok("ready", "34")
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        self.assertEqual([a for a in self.gh_argv() if a[0] not in ("api", "repo")],     # the gate's reads aside
                         [["pr", "ready", "34"], ["pr", "view", "34", "--json", "url,title,closingIssuesReferences"]])
        self.assertEqual(self.stub_calls("harness-board"), [
            ["status", "12", "in-review", "--comment", f"PR #34 ready for review: {PR_URL}", "--repo", self.main]])
        sent = self.notifications()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "terminal-notifier")
        self.assertIn("proj PR #34 ready for review: Add the widget (#12)", sent[0])

    def test_ready_takes_the_ticket_from_the_flag(self):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([])])
        self.ok("ready", "34", "--ticket", "7")
        self.assertEqual([a[:4] for a in self.stub_calls("harness-board")], [["status", "7", "in-review", "--comment"]])

    def test_ready_reports_a_failed_status_change(self):
        self.outputs(**{"harness-board": {"status": {"stderr": "harness-board: gh issue edit failed\n", "exit": 1}}})
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
        p = self.pr("ready", "34")
        self.assertOneLineError(p, "PR #34 is ready, but", "gh issue edit failed")
        self.assertEqual(len(self.notifications()), 1)

    def test_ready_with_the_real_board_mirrors_in_review_to_the_configured_project(self):
        """#64: no stubs, HARNESS_PROJECT set: the real harness-board sets the ticket's project item to "In review",
        on the URL gh reports for the issue (not one built from the remote)."""
        url = "https://ghe.example.com/o/r/issues/12"
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12]),
                       {"match": ["issue", "view", "12"], "stdout_json": {
                           "number": 12, "url": url, "labels": [{"name": "task"}, {"name": "status:in-progress"}]}},
                       {"match": ["issue", "edit", "12"], "stdout": url + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": url + "#issuecomment-1\n"},
                       {"match": ["project"], "stdout": ""}])
        p = self.ok("ready", "34", stubs=False, HARNESS_PROJECT="rjeskuri/7")
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        argvs = self.gh_argv()
        self.assertIn(["issue", "edit", "12", "--add-label", "status:in-review", "--remove-label",
                       "status:in-progress"], argvs)
        projects = [a for a in argvs if a[:1] == ["project"]]
        self.assertEqual(len(projects), 1, projects)
        self.assertEqual(project_argv(projects[0]), ("item-edit", ["7"], {"owner": "rjeskuri", "url": url,
                                                                           "field": "Status", "value": "In review"}))

    # ------------------------------------------------- #69: harness-board's Projects warning reaches the user
    def real_board_scenario(self, project_call):
        """ready 34 closing #12 with the real harness-board; `project_call` answers any `gh project ...`."""
        url = "https://github.com/o/r/issues/12"
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12]),
                       {"match": ["issue", "view", "12"], "stdout_json": {
                           "number": 12, "url": url, "labels": [{"name": "task"}, {"name": "status:in-progress"}]}},
                       {"match": ["issue", "edit", "12"], "stdout": url + "\n"},
                       {"match": ["issue", "comment", "12"], "stdout": url + "#issuecomment-1\n"},
                       dict(project_call, match=["project"])])

    def test_ready_passes_the_board_warning_through_when_the_project_item_edit_fails(self):
        """Real harness-board, a failing `gh project item-edit`: ready still exits 0 with its usual stdout, and
        harness-board's one warning line, naming that board and gh's error, is on ready's stderr exactly once. The
        board comes from the environment in one case and from ~/.claude/settings.json in the other."""
        cases = [("rjeskuri/7", "HTTP 403: missing the project scope (token-d4)", "env"),
                 ("some-org/42", "could not find the item (token-e5)", "settings")]
        for project, why, source in cases:
            with self.subTest(project=project, source=source):
                self.real_board_scenario({"stderr": why + "\n", "exit": 1})
                settings = os.path.join(self.home, ".claude", "settings.json")
                if source == "settings":
                    write(settings, json.dumps({"env": {"HARNESS_PROJECT": project}}))
                    p = self.ok("ready", "34", stubs=False)
                else:
                    p = self.ok("ready", "34", stubs=False, HARNESS_PROJECT=project)
                owner, number = project.split("/")
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertEqual(p.stderr.splitlines(), [f"harness-board: warning: the Projects board {owner}/{number} "
                                                         f"was not updated: gh project item-edit {number} failed: "
                                                         f"{why}"], p.stderr)
                self.assertIn(["issue", "edit", "12", "--add-label", "status:in-review", "--remove-label",
                               "status:in-progress"], self.gh_argv(), "the labels still changed")
                self.assertEqual(len(self.notifications()), 1 if source == "env" else 2)

    def test_ready_shows_only_the_warning_lines_of_a_successful_board_call(self):
        """A board that exits 0 with stderr: each `harness-board: warning:` line reaches ready's stderr once, in
        order; its other stderr lines stay hidden. A board with only other stderr leaves ready's stderr empty."""
        warnings = ["harness-board: warning: the Projects board acme/3 was not updated: gh project item-edit 3 "
                    "failed: stub says no (token-f6)",
                    "harness-board: warning: HARNESS_PROJECT='x' is not OWNER/NUMBER; the Projects board is not "
                    "updated (token-g7)"]
        chatter = ["harness-board: note: chatter (token-h8)", "warning: chatter without the prefix (token-i9)",
                   "gh: some progress line (token-j0)"]
        runs = [([chatter[0], warnings[0], chatter[1]], [warnings[0]]),
                ([warnings[1], chatter[2], warnings[0]], [warnings[1], warnings[0]]),
                (chatter, [])]
        for stderr, shown in runs:
            with self.subTest(stderr=stderr):
                self.outputs(**{"harness-board": {"status": {"stderr": "\n".join(stderr) + "\n", "exit": 0}}})
                self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, self.view([12])])
                p = self.ok("ready", "34")
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertEqual(p.stderr.splitlines(), shown, p.stderr)
                self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])

    def test_ready_stderr_is_unchanged_without_the_project_setting(self):
        """No HARNESS_PROJECT anywhere, real harness-board: no project call and ready's stderr is empty as before."""
        self.real_board_scenario({"stderr": "should never run\n", "exit": 1})
        p = self.ok("ready", "34", stubs=False)
        self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
        self.assertEqual(p.stderr, "")
        self.assertEqual([a for a in self.gh_argv() if a[:1] == ["project"]], [])


class ReadyGate(GateAsserts, PrTest):
    """`harness-pr ready P` refuses a PR whose latest harness review has findings and no commit after it."""

    def run_case(self, reviews, commits, *flags):
        self.scenario([{"match": ["pr", "ready", "34"], "stdout": ""}, Ready.view(self, [12]), REPO_VIEW]
                      + gate_calls(reviews, commits))
        if os.path.exists(self.notify_log):
            os.unlink(self.notify_log)
        return self.pr("ready", "34", *flags)

    def assertRefused(self, p):
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse([a for a in self.gh_argv() if a[:2] == ["pr", "ready"]], self.gh_argv())   # not marked ready
        self.assertEqual(self.stub_calls("harness-board"), [])                                      # no status change
        self.assertEqual(self.notifications(), [])

    def test_ready_follows_the_latest_harness_review_and_the_commits_after_it(self):
        for name, (reviews, commits, named, not_named) in GATE_CASES.items():
            with self.subTest(name):
                p = self.run_case(reviews, commits)
                if named is None:
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn(["pr", "ready", "34"], self.gh_argv())
                    self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])
                    if name in GATE_QUIET:
                        self.assertEqual(p.stderr, "")
                else:
                    self.assertRefused(p)
                    self.assertNamesFindings(p, named, not_named)
                    self.assertNotListedCount(name, p)

    def test_ready_refusal_counts_the_high_and_medium_findings_and_says_what_to_do(self):
        """The refusal: "PR #P: the latest harness review (<submitted_at>) has <n> high or medium findings and no
        commit after it: <each>; fix them, push, then harness-pr ready P", n counting only high and medium, listed or
        counted by the summary beyond those listed."""
        for name, when, n in GATE_REFUSAL_COUNTS:
            with self.subTest(name):
                reviews, commits, _named, _ = GATE_CASES[name]
                p = self.run_case(reviews, commits)
                self.assertRefused(p)
                self.assertRegex(p.stderr, re.escape(f"PR #34: the latest harness review ({when}) has {n} high or "
                                                     "medium finding") + r"s? and no commit after it: ")
                self.assertIn("; fix them, push, then harness-pr ready 34", p.stderr)

    def test_ready_says_how_many_high_or_medium_findings_the_review_does_not_list(self):
        """K is the summary's high plus medium less the high and medium listed; extra low ones in the summary never
        add the line, and the low ones listed are not counted against it."""
        cases = [  # (inline, body_only, summary (total, high, medium, low), the model named, K or 0 for no line)
            ([FROBS], [NOTES], (3, 2, 0, 1), "fable", 1),
            ([OLD_NIT], [NOTES], (5, 1, 2, 2), "opus", 3),
            ([TOKEN], [NOTES, TYPO], (8, 3, 2, 3), "sonnet", 4),
            ([FROBS, OLD_NIT], [CAP], (4, 1, 2, 1), "opus", 1),
            ([FROBS], [NOTES], (6, 1, 0, 5), "opus", 0),
            ([], [], (1, 1, 0, 0), "sonnet", 1),
            ([], [], (1, 0, 1, 0), "fable", 1),
            ([], [], (2, 0, 2, 0), "opus", 2),
        ]
        for inline, body_only, summary, model, k in cases:
            with self.subTest(summary=summary, model=model):
                reviews = [harness_review(101, at(10), inline=inline, body_only=body_only, summary=summary,
                                          model=model)]
                p = self.run_case(reviews, [pr_commit("a", at(9))])
                self.assertRefused(p)
                listed = [f for f in inline + body_only if f[2] in ("high", "medium")]
                lows = [f for f in inline + body_only if f[2] == "low"]
                self.assertNamesFindings(p, listed, lows)
                if k:
                    self.assertIn(f"{k} more high or medium not listed (see the review)", p.stderr)
                else:
                    self.assertNotIn("not listed", p.stderr)

    def test_ready_warns_about_the_low_findings_it_lets_through(self):
        """Only low findings and no commit since: ready marks the PR, sets in-review and notifies as usual, and
        prints one line on stderr: "harness-pr: warning: PR #P: <n> low findings left for the owner: <each>"."""
        cases = [([OLD_NIT], [NOTES]), ([], [TYPO]), ([OLD_NIT, NOTES], [TYPO]), ([NOTES], [])]
        for inline, body_only in cases:
            lows = inline + body_only
            with self.subTest(lows=[f[0] for f in lows]):
                p = self.run_case([harness_review(101, at(10), inline=inline, body_only=body_only)],
                                  [pr_commit("a", at(9))])
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(p.stdout, f"PR #34 ready for review: {PR_URL}\n")
                self.assertIn(["pr", "ready", "34"], self.gh_argv())
                self.assertEqual([a[:3] for a in self.stub_calls("harness-board")], [["status", "12", "in-review"]])
                self.assertEqual(len(self.notifications()), 1)
                lines = p.stderr.splitlines()
                self.assertEqual(len(lines), 1, p.stderr)
                self.assertRegex(lines[0], r"^harness-pr: warning: PR #34: " + str(len(lows))
                                 + r" low findings? left for the owner: ")
                for path, line, _s, body in lows:
                    self.assertIn(f"{path}:{line}", lines[0])
                    self.assertIn(body[:20], lines[0])

    def test_ready_has_no_override(self):
        reviews, commits, named, _ = GATE_CASES["findings and no commit since"]
        p = self.run_case(reviews, commits)
        self.assertRefused(p)
        self.assertNamesFindings(p, named, [])
        self.assertRefused(self.run_case(reviews, commits, "--accept-findings"))

    def test_ready_fails_closed_when_a_gate_read_fails(self):
        # The PR would pass the gate (a commit after the findings), so only the failed read can refuse it.
        reviews, commits, named, _ = GATE_CASES["a commit after the findings"]
        self.assertIsNone(named)
        for what, failing in GATE_READ_FAILURES.items():
            with self.subTest(what):
                self.scenario([failing, {"match": ["pr", "ready", "34"], "stdout": ""}, Ready.view(self, [12]),
                               REPO_VIEW] + gate_calls(reviews, commits))
                if os.path.exists(self.notify_log):
                    os.unlink(self.notify_log)
                p = self.pr("ready", "34")
                self.assertRefused(p)
                self.assertIn(GATE_FAILURE_TEXT, p.stderr)
                self.assertTrue(any(a[:1] == ["api"] or a[:2] == ["repo", "view"] for a in self.gh_argv()))


class Comments(PrTest):
    def setUp(self):
        super().setUp()
        self.scenario([REPO_VIEW, {"match": ["api", "graphql"], "stdout_json": CONVERSATION}])

    def test_comments_drop_the_harness_reviews(self):
        data = json.loads(self.ok("comments", "34", "--json").stdout)
        self.assertEqual([[c["body"] for c in t["comments"]] for t in data["threads"]],
                         [["Rename this."], ["Old nit."]])
        self.assertEqual([t["resolved"] for t in data["threads"]], [False, True])
        self.assertEqual(data["threads"][0]["path"], "widget.py")
        self.assertEqual(data["threads"][0]["line"], 3)
        self.assertEqual([r["body"] for r in data["reviews"]], ["Looks fine overall."])
        self.assertEqual([c["body"] for c in data["comments"]], ["Please also update the docs."])
        query = self.gh_argv()[1]
        self.assertEqual(query[:2], ["api", "graphql"])
        for piece in ("owner=o", "repo=r", "number=34"):
            self.assertIn(piece, query)
        self.assertEqual(query[query.index("number=34") - 1], "-F")
        text = next(a for a in query if a.startswith("query="))
        for field in ("reviewThreads(first: 100)", "isResolved", "reviews(first: 50)", "comments(first: 100)"):
            self.assertIn(field, text)

    def test_unresolved_keeps_unresolved_threads_only(self):
        data = json.loads(self.ok("comments", "34", "--unresolved", "--json").stdout)
        self.assertEqual([t["comments"][0]["body"] for t in data["threads"]], ["Rename this."])
        self.assertEqual(data["reviews"], [])
        self.assertEqual(data["comments"], [])

    def test_since_keeps_newer_items(self):
        data = json.loads(self.ok("comments", "34", "--since", "2026-10-11T00:00:00Z", "--json").stdout)
        self.assertEqual([t["comments"][0]["body"] for t in data["threads"]], ["Rename this."])
        self.assertEqual([r["body"] for r in data["reviews"]], ["Looks fine overall."])
        self.assertEqual(data["comments"], [])

    def test_markdown(self):
        out = self.ok("comments", "34").stdout
        self.assertIn("## Unresolved threads (1)", out)
        self.assertIn("`widget.py:3`", out)
        self.assertIn("**rjeskuri** (2026-10-12): Rename this.", out)
        self.assertIn("## Reviews (1)", out)
        self.assertNotIn(MARKER, out)
        self.assertNotIn("Frobs once", out)


class Awaiting(PrTest):
    def prs(self):
        return [{"number": 33, "title": "Draft work", "isDraft": True, "createdAt": iso_ago(hours=1),
                 "url": "https://github.com/o/r/pull/33", "headRefName": "t/14-draft", "closingIssuesReferences": [],
                 "statusCheckRollup": []},
                {"number": 34, "title": "Add the widget (#12)", "isDraft": False, "createdAt": iso_ago(days=2, hours=1),
                 "url": PR_URL, "headRefName": BRANCH, "closingIssuesReferences": [{"number": 12}],
                 "statusCheckRollup": GREEN},
                {"number": 35, "title": "Fix the thing", "isDraft": False, "createdAt": iso_ago(hours=5),
                 "url": "https://github.com/o/r/pull/35", "headRefName": "t/15-fix-the-thing",
                 "closingIssuesReferences": [], "statusCheckRollup": PENDING}]

    def calls(self):
        return [REPO_VIEW, {"match": ["pr", "list"], "stdout_json": self.prs()},
                {"contains": ["graphql", "number=34"], "stdout_json": CONVERSATION},
                {"contains": ["graphql", "number=35"], "stdout_json": graphql()}]

    def test_awaiting_lists_non_draft_prs_with_ticket_age_checks_and_threads(self):
        self.scenario(self.calls())
        rows = json.loads(self.ok("awaiting", "--json").stdout)
        self.assertEqual([(r["number"], r["ticket"], r["age"], r["checks"], r["unresolved"]) for r in rows],
                         [(34, 12, "2d", "green", 1), (35, 15, "5h", "pending", 0)])
        argv = self.gh_argv()
        self.assertIn(["pr", "list", "--author", "@me", "--state", "open", "--json",
                       "number,title,isDraft,createdAt,url,headRefName,closingIssuesReferences,statusCheckRollup"],
                      argv)
        self.assertFalse(any("number=33" in a for a in argv))
        out = self.ok("awaiting").stdout
        self.assertIn("UNRESOLVED", out.splitlines()[0])
        self.assertIn("#34", out)
        self.assertNotIn("#33", out)

    def test_awaiting_all_projects_walks_the_registry(self):
        self.scenario(self.calls())
        bare = self.mk("no-remote")
        self.git(bare, "init", "-q")
        write(os.path.join(self.home, ".claude", "harness", "projects.json"), json.dumps({"version": 1, "projects": [
            {"path": self.main, "name": "proj"}, {"path": bare, "name": "no-remote"},
            {"path": os.path.join(self.tmp, "gone"), "name": "gone"}]}))
        rows = json.loads(self.ok("awaiting", "--all-projects", "--json", cwd=self.tmp).stdout)
        self.assertEqual([(r["project"], r["number"]) for r in rows], [("proj", 34), ("proj", 35)])
        self.assertEqual(sum(1 for a in self.gh_argv() if a[:2] == ["pr", "list"]), 1)


if __name__ == "__main__":
    unittest.main()
