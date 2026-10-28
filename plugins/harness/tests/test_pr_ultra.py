"""harness-pr ultra and HARNESS_ULTRAREVIEW, and the Cost block harness-pr land asks harness-work for.

Split from test_pr.py; the fixtures are in pr_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_ultra.py" -v
"""

from __future__ import annotations

import unittest

from pr_support import *  # noqa: F401,F403


# =========================================================================== ultra
class Ultra(PrTest):
    def claude(self, stdout="", stderr="", code=0):
        return {"CLAUDE_STDOUT": write(os.path.join(self.tmp, "claude.out"), stdout),
                "CLAUDE_STDERR": write(os.path.join(self.tmp, "claude.err"), stderr), "CLAUDE_EXIT": str(code)}

    def budgets_path(self):
        return os.path.join(self.home, ".claude", "harness", "budgets.json")

    def test_ultra_saves_the_json_and_counts_findings(self):
        out = json.dumps({"summary": "done", "findings": [{"severity": "high"}, {"severity": "medium"},
                                                          {"severity": "high"}, {"severity": "low"}]})
        p = self.ok("ultra", "34", "--post", **self.claude(stdout=out))
        self.assertEqual(self.stub_calls("claude"), [["ultrareview", "34", "--json", "--timeout", "30", "--post"]])
        with open(self.state_path("ultra", "34.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), out)
        self.assertIn("4 findings (2 high, 1 medium, 1 low)", p.stdout)
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_ultra_billing_error_marks_the_cloud_budget_exhausted(self):
        write(self.budgets_path(), json.dumps({"api": {"exhausted_until": None, "reason": ""}}))
        p = self.pr("ultra", "34", **self.claude(stderr="Starting\nError: Your credit balance is too low.\n", code=1))
        self.assertOneLineError(p, "credit balance is too low", "exhausted until")
        self.assertEqual(self.stub_calls("claude"), [["ultrareview", "34", "--json", "--timeout", "30"]])
        with open(self.budgets_path(), encoding="utf-8") as fh:
            budgets = json.load(fh)
        self.assertEqual(budgets["api"], {"exhausted_until": None, "reason": ""})
        self.assertEqual(budgets["cloud"]["reason"], "Error: Your credit balance is too low.")
        until = dt.datetime.fromisoformat(budgets["cloud"]["exhausted_until"].replace("Z", "+00:00"))
        expected = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        self.assertLess(abs((until - expected).total_seconds()), 300)

    def test_ultra_other_failure_leaves_the_budget_alone(self):
        p = self.pr("ultra", "34", **self.claude(stderr="network unreachable\n", code=1))
        self.assertOneLineError(p, "network unreachable")
        self.assertFalse(os.path.exists(self.budgets_path()))


class UltraSetting(UltraSettingBase, PrTest):
    def test_off_runs_nothing_and_says_so(self):
        # Every spelling of off, in the settings file, for a spread of PR numbers; the PR number is in the line.
        for i, value in enumerate(["off", "OFF", "0", "false", "False", "no", " off "]):
            number = 7 + 27 * i
            with self.subTest(value=value):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                self.assertNothingRan(p, number)
                self.assertEqual(self.warnings(p), [])
        # A JSON number 0 in settings.json is off too (the settings reader turns it into "0").
        self.assertNothingRan(self.go(52, {"HARNESS_ULTRAREVIEW": 0}), 52)

    def test_off_with_post_runs_nothing_and_leaves_state_and_budget_alone(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"})
        old = write(self.state_path("ultra", "34.json"), '{"findings": []}')
        budgets = write(self.budgets_path(), json.dumps({"api": {"exhausted_until": None, "reason": ""}}))
        with open(budgets, "rb") as fh:
            before = fh.read()
        # A claude that would hit the billing error if it were called.
        p = self.pr("ultra", "34", "--post", **self.claude(stderr="Error: Your credit balance is too low.\n", code=1))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), off_line(34))
        self.assertEqual(self.stub_calls("claude"), [])
        self.assertEqual(self.gh_argv(), [])
        with open(old, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"findings": []}')
        with open(budgets, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_force_runs_it_anyway(self):
        for number, flags, setting in ((34, (), "off"), (91, ("--post",), "off"), (5, (), "0"),
                                       (12, ("--post",), "false")):
            with self.subTest(number=number, flags=flags, setting=setting):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": setting}, flags=("--force", *flags))
                self.assertRan(p, number, *flags)
                self.assertEqual(self.warnings(p), [])
        # --force with the environment saying off, too.
        p = self.go(63, environ={"HARNESS_ULTRAREVIEW": "off"}, flags=("--force",))
        self.assertRan(p, 63)

    def test_force_with_off_in_the_environment_and_the_settings_file_still_runs(self):
        p = self.go(48, {"HARNESS_ULTRAREVIEW": "off"}, environ={"HARNESS_ULTRAREVIEW": "false"},
                    flags=("--force",))
        self.assertRan(p, 48)
        self.assertEqual(self.warnings(p), [])
        p = self.go(49, {"HARNESS_ULTRAREVIEW": "no"}, environ={"HARNESS_ULTRAREVIEW": "0"},
                    flags=("--post", "--force"))
        self.assertRan(p, 49, "--post")
        self.assertEqual(self.warnings(p), [])

    def test_force_with_invalid_values_still_warns_and_runs(self):
        file_env = {"HARNESS_ULTRAREVIEW": "maybe", "HARNESS_CODE_TIER": "gigantic"}
        p = self.go(34, file_env, flags=("--force",))
        self.assertRan(p, 34)
        self.assertWarnings(p, [bad("HARNESS_CODE_TIER", "gigantic"), bad("HARNESS_ULTRAREVIEW", "maybe")])
        environ = {"HARNESS_ULTRAREVIEW": "sometimes", "HARNESS_WAVE_REVIEW": "huge"}
        p = self.go(35, {"HARNESS_ULTRAREVIEW": "off"}, environ=environ, flags=("--force", "--post"))
        self.assertRan(p, 35, "--post")
        self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", "sometimes"), bad("HARNESS_WAVE_REVIEW", "huge")], environ)

    def test_unset_and_on_run_as_today(self):
        cases = [("unset", None, None, 34, ()), ("settings on", {"HARNESS_ULTRAREVIEW": "on"}, None, 41, ("--post",)),
                 ("settings 1", {"HARNESS_ULTRAREVIEW": "1"}, None, 8, ()),
                 ("settings true", {"HARNESS_ULTRAREVIEW": "True"}, None, 19, ()),
                 ("settings yes", {"HARNESS_ULTRAREVIEW": "yes"}, None, 77, ("--post",)),
                 ("blank is unset", {"HARNESS_ULTRAREVIEW": "  "}, None, 23, ()),
                 ("other keys only", {"HARNESS_CODE_TIER": "opus"}, None, 6, ()),
                 ("environment on", None, "on", 15, ()),
                 ("environment on beats settings off", {"HARNESS_ULTRAREVIEW": "off"}, "on", 29, ("--post",))]
        for label, file_env, environment, number, flags in cases:
            with self.subTest(label):
                extra = {"HARNESS_ULTRAREVIEW": environment} if environment else {}
                p = self.go(number, file_env, environ=extra, flags=flags)
                self.assertRan(p, number, *flags)
                self.assertEqual(self.warnings(p), [])

    def test_invalid_value_warns_and_runs(self):
        for number, value in ((34, "maybe"), (58, "of"), (9, "2")):
            with self.subTest(value=value):
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                self.assertRan(p, number)
                self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", value)])
        # From the environment, the same: one warning, and it runs.
        environ = {"HARNESS_ULTRAREVIEW": "sometimes"}
        p = self.go(71, environ=environ)
        self.assertRan(p, 71)
        self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", "sometimes")], environ)

    def test_every_problem_is_one_warning_line_with_load_routings_text(self):
        # (label, file env, environment, expected problems in load_routing's order, whether it is off)
        cases = [
            ("two invalid keys, off",
             {"HARNESS_ULTRAREVIEW": "off", "HARNESS_CODE_TIER": "gigantic", "HARNESS_CODE_EFFORT": "11"}, {},
             [bad("HARNESS_CODE_TIER", "gigantic"), bad("HARNESS_CODE_EFFORT", "11")], True),
            ("review tier, wave and switch invalid, switch ignored so it runs",
             {"HARNESS_REVIEW_TIER": "Huge", "HARNESS_WAVE_REVIEW": "sometimes", "HARNESS_ULTRAREVIEW": "perhaps"}, {},
             [bad("HARNESS_REVIEW_TIER", "Huge"), bad("HARNESS_ULTRAREVIEW", "perhaps"),
              bad("HARNESS_WAVE_REVIEW", "sometimes")], False),
            ("JSON numbers become their text",
             {"HARNESS_CODE_EFFORT": 11, "HARNESS_WAVE_REVIEW": 2.5, "HARNESS_ULTRAREVIEW": "off"}, {},
             [bad("HARNESS_CODE_EFFORT", "11"), bad("HARNESS_WAVE_REVIEW", "2.5")], True),
            ("environment values are stripped and reported as given",
             {"HARNESS_CODE_EFFORT": "extreme", "HARNESS_ULTRAREVIEW": "no"}, {"HARNESS_WAVE_REVIEW": "  xl  "},
             [bad("HARNESS_CODE_EFFORT", "extreme"), bad("HARNESS_WAVE_REVIEW", "xl")], True),
            ("environment keys",
             {"HARNESS_ULTRAREVIEW": "off"}, {"HARNESS_CODE_TIER": "Ultra", "HARNESS_REVIEW_TIER": "tiny"},
             [bad("HARNESS_CODE_TIER", "Ultra"), bad("HARNESS_REVIEW_TIER", "tiny")], True),
            ("all five invalid",
             {"HARNESS_CODE_TIER": "t1", "HARNESS_CODE_EFFORT": "e2", "HARNESS_REVIEW_TIER": "t3",
              "HARNESS_ULTRAREVIEW": "maybe", "HARNESS_WAVE_REVIEW": "w5"}, {},
             [bad("HARNESS_CODE_TIER", "t1"), bad("HARNESS_CODE_EFFORT", "e2"), bad("HARNESS_REVIEW_TIER", "t3"),
              bad("HARNESS_ULTRAREVIEW", "maybe"), bad("HARNESS_WAVE_REVIEW", "w5")], False),
            ("a single invalid wave value next to off",
             {"HARNESS_WAVE_REVIEW": "opuss", "HARNESS_ULTRAREVIEW": "0"}, {},
             [bad("HARNESS_WAVE_REVIEW", "opuss")], True),
        ]
        for i, (label, file_env, environ, problems, off) in enumerate(cases):
            number = 100 + 13 * i
            with self.subTest(label):
                p = self.go(number, file_env, environ=environ)
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, problems, environ)

    def test_other_invalid_keys_warn_while_ultrareview_is_unset_and_it_runs(self):
        cases = [({"HARNESS_CODE_TIER": "gigantic"}, [bad("HARNESS_CODE_TIER", "gigantic")]),
                 ({"HARNESS_REVIEW_TIER": "zzz", "HARNESS_ULTRAREVIEW": "on"}, [bad("HARNESS_REVIEW_TIER", "zzz")]),
                 ({"HARNESS_WAVE_REVIEW": "x", "HARNESS_CODE_EFFORT": "y"},
                  [bad("HARNESS_CODE_EFFORT", "y"), bad("HARNESS_WAVE_REVIEW", "x")])]
        for i, (file_env, problems) in enumerate(cases):
            with self.subTest(file_env=file_env):
                p = self.go(30 + i, file_env)
                self.assertRan(p, 30 + i)
                self.assertWarnings(p, problems)

    def test_valid_values_of_the_other_keys_are_quiet_whatever_their_case(self):
        file_env = {"HARNESS_CODE_TIER": "Opus", "HARNESS_CODE_EFFORT": " XHIGH ", "HARNESS_REVIEW_TIER": "FABLE",
                    "HARNESS_WAVE_REVIEW": "Off"}
        p = self.go(14, dict(file_env, HARNESS_ULTRAREVIEW="ON"))
        self.assertRan(p, 14)
        self.assertEqual(self.warnings(p), [])
        p = self.go(16, {"HARNESS_WAVE_REVIEW": "0", "HARNESS_CODE_EFFORT": "max", "HARNESS_CODE_TIER": "sonnet"})
        self.assertRan(p, 16)
        self.assertEqual(self.warnings(p), [])

    def test_valid_other_keys_do_not_hide_an_off(self):
        file_env = {"HARNESS_CODE_TIER": "Opus", "HARNESS_CODE_EFFORT": " XHIGH ", "HARNESS_REVIEW_TIER": "FABLE"}
        p = self.go(15, dict(file_env, HARNESS_WAVE_REVIEW="sonnet", HARNESS_ULTRAREVIEW="No"))
        self.assertNothingRan(p, 15)
        self.assertEqual(self.warnings(p), [])
        p = self.go(17, dict(file_env, HARNESS_WAVE_REVIEW="Off", HARNESS_ULTRAREVIEW=" FALSE"))
        self.assertNothingRan(p, 17)
        self.assertEqual(self.warnings(p), [])

    def test_json_booleans_and_other_non_text_values_are_not_values(self):
        # A JSON true/false/null/list/object is "unset", not on and not off: it runs and says nothing.
        for i, value in enumerate([True, False, None, ["off"], {"v": "off"}]):
            with self.subTest(value=value):
                p = self.go(60 + i, {"HARNESS_ULTRAREVIEW": value, "HARNESS_CODE_TIER": value})
                self.assertRan(p, 60 + i)
                self.assertEqual(self.warnings(p), [])

    def test_json_numbers_are_their_text(self):
        # A JSON number that is not a spelling of on or off is invalid, reported as its text.
        for i, (value, shown, off) in enumerate([(0.0, "0.0", False), (2, "2", False), (1, None, False),
                                                 (0, None, True)]):
            with self.subTest(number=value):
                number = 80 + i
                p = self.go(number, {"HARNESS_ULTRAREVIEW": value})
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, [bad("HARNESS_ULTRAREVIEW", shown)] if shown else [])

    def test_broken_or_oddly_shaped_settings_count_as_no_file(self):
        texts = ["{not json", "", "[]", '"off"', "null", '{"env": "off"}', '{"env": ["HARNESS_ULTRAREVIEW"]}',
                 '{"env": null}', '{"env": {"HARNESS_ULTRAREVIEW": "off"}', '{"other": {"HARNESS_ULTRAREVIEW": "off"}}']
        for i, text in enumerate(texts):
            with self.subTest(text=text):
                p = self.go(120 + i, raw=text)
                self.assertRan(p, 120 + i)
                self.assertEqual(self.warnings(p), [])

    def test_a_broken_settings_file_does_not_hide_the_environment(self):
        # Broken file, but the environment says off: off.
        p = self.go(140, raw="{not json", environ={"HARNESS_ULTRAREVIEW": "off"})
        self.assertNothingRan(p, 140)
        # A broken file with a bad environment value: only the environment's warning.
        environ = {"HARNESS_CODE_EFFORT": "wild"}
        p = self.go(141, raw="[1, 2", environ=environ)
        self.assertRan(p, 141)
        self.assertWarnings(p, [bad("HARNESS_CODE_EFFORT", "wild")], environ)

    def test_the_environment_wins_even_when_invalid_and_blank_falls_through(self):
        # (settings value, environment value, expected problems, off)
        cases = [
            ("off", "perhaps", [bad("HARNESS_ULTRAREVIEW", "perhaps")], False),
            ("maybe", "sometimes", [bad("HARNESS_ULTRAREVIEW", "sometimes")], False),
            ("maybe", "off", [], True),
            ("off", "   ", [], True),
            ("on", " OFF ", [], True),
            ("maybe", "on", [], False),
            ("maybe", "", [bad("HARNESS_ULTRAREVIEW", "maybe")], False),
            ("no", "yes", [], False),
        ]
        for i, (file_value, env_value, problems, off) in enumerate(cases):
            with self.subTest(file=file_value, environment=env_value):
                number = 150 + i
                environ = {"HARNESS_ULTRAREVIEW": env_value}
                p = self.go(number, {"HARNESS_ULTRAREVIEW": file_value}, environ=environ)
                if off:
                    self.assertNothingRan(p, number)
                else:
                    self.assertRan(p, number)
                self.assertWarnings(p, problems, environ)

    def test_environment_off_wins_and_settings_local_is_ignored(self):
        # The environment says off while settings.json says on: off.
        self.settings({"HARNESS_ULTRAREVIEW": "on"})
        self.assertNothingRan(self.pr("ultra", "34", HARNESS_ULTRAREVIEW="off", **self.claude()), 34)
        # The environment alone says off, with no settings files at all.
        os.unlink(os.path.join(self.main, ".claude", "settings.json"))
        self.assertNothingRan(self.pr("ultra", "35", HARNESS_ULTRAREVIEW="false", **self.claude()), 35)
        # settings.local.json saying off does not turn it off.
        p = self.go(36, local={"HARNESS_ULTRAREVIEW": "off"})
        self.assertRan(p, 36)
        self.assertEqual(self.warnings(p), [])
        # ...and settings.local.json saying on does not undo an off in settings.json.
        p = self.go(37, {"HARNESS_ULTRAREVIEW": "off"}, local={"HARNESS_ULTRAREVIEW": "on"})
        self.assertNothingRan(p, 37)

    def test_settings_local_never_adds_warnings(self):
        local = {"HARNESS_CODE_TIER": "gigantic", "HARNESS_ULTRAREVIEW": "maybe", "HARNESS_WAVE_REVIEW": "q"}
        p = self.go(44, {"HARNESS_REVIEW_TIER": "tiny"}, local=local)
        self.assertRan(p, 44)
        self.assertWarnings(p, [bad("HARNESS_REVIEW_TIER", "tiny")])


class UltraSettingFromWorktree(UltraSettingBase, WorktreeTest):
    """The switch is the main checkout's, also when harness-pr runs in the ticket worktree."""

    def test_main_checkout_settings_apply_in_the_worktree(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"})
        self.assertNothingRan(self.pr("ultra", "34", cwd=self.wt, **self.claude()), 34)
        self.scenario([])
        p = self.pr("ultra", "35", "--force", cwd=self.wt, **self.claude())
        self.assertRan(p, 35)

    def test_the_worktrees_own_settings_file_does_not_count(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off"}, root=self.wt)
        self.assertRan(self.pr("ultra", "34", cwd=self.wt, **self.claude()), 34)

    def test_warnings_come_from_the_main_checkouts_settings_only(self):
        self.settings({"HARNESS_ULTRAREVIEW": "off", "HARNESS_CODE_TIER": "gigantic"})
        self.settings({"HARNESS_REVIEW_TIER": "wrong", "HARNESS_WAVE_REVIEW": "nope"}, root=self.wt)
        p = self.pr("ultra", "34", cwd=self.wt, **self.claude())
        self.assertNothingRan(p, 34)
        self.assertWarnings(p, [bad("HARNESS_CODE_TIER", "gigantic")])


class LandCost(WorktreeTest):
    """`land` runs `harness-work cost N --pr P --markdown --repo <main>` for each ticket whose Resolution it posts and
    adds the stdout, stripped, to that ticket's Resolution after a blank line. The harness-work stub answers per
    ticket (`cost N`), and the harness-board stub logs each comment's body, so a test reads the Resolution posted
    for every ticket. Two PRs, each closing two tickets, so neither a PR number nor a ticket can be hard-coded."""

    CASES = ((FIXTURE_A, [12, 13]), (FIXTURE_B, [21, 12]))

    def setUp(self):
        super().setUp()
        for n in (12, 13, 21):
            write(self.state_path("tickets", f"{n}.json"),
                  json.dumps({"number": n, "parent": {"number": 3, "title": "Widgets", "goal": ""}}))
        self.costs({})

    def costs(self, answers):
        """The harness-work stub's answer to `cost N` for each ticket N in `answers`; nothing, exit 0, for others."""
        self.outputs(**{"harness-board": {"epic-check": {"stdout": EPIC_SAID}},
                        "harness-work": {f"cost {n}": answer for n, answer in answers.items()}})

    def cost_calls(self, fx, tickets, state="OPEN", comments=None):
        """The land conversation for the fixture's PR closing `tickets`; `comments`: {ticket: [bodies]} on it."""
        pr, sha = str(fx["pr"]), fx["sha"]
        url = f"https://github.com/o/r/pull/{pr}"
        comments = comments or {}
        calls = gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}") + [
            {"match": ["pr", "view", pr, "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}},
            {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
             "stdout_json": land_view(url=url, state=state,
                                      closingIssuesReferences=[{"number": n} for n in tickets])},
            REPO_VIEW,
            {"match": ["pr", "merge", pr], "stdout": ""}]
        for n in tickets:
            issue = {"state": "CLOSED" if state == "MERGED" else "OPEN",
                     "labels": [{"name": "task"}, {"name": "status:in-review"}],
                     "comments": [{"author": {"login": "rjeskuri"}, "body": b + "\n\n" + HARNESS_MARK,
                                   "createdAt": f"2026-10-13T1{k}:00:00Z"} for k, b in enumerate(comments.get(n, []))]}
            calls += [{"match": ["issue", "view", str(n), "--json", "state,labels,comments"], "stdout_json": issue},
                      {"match": ["issue", "edit", str(n)], "stdout": ""}]
        return calls + [{"match": ["pr", "view", pr], "stdout_json": pr_data(fx)}, *files_calls(pr, fx["files"])]

    def fresh_worktree(self):
        """The ticket worktree and branch again, as before the first land, so every land cleans up alike."""
        if not os.path.isdir(self.wt):
            self.git(self.main, "worktree", "prune")
            self.git(self.main, "worktree", "add", "-q", "-b", BRANCH, self.wt, "origin/main")
            write(os.path.join(self.wt, "widget.py"), "def frob():\n    return 2\n")
            self.git(self.wt, "add", "-A")
            self.git(self.wt, "commit", "-q", "-m", "Add the widget")

    def land(self, fx, tickets, cwd=None, **scenario):
        self.fresh_worktree()
        self.scenario(self.cost_calls(fx, tickets, **scenario))
        p = self.pr("land", str(fx["pr"]), "--yes", cwd=cwd or self.main)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def comments_on(self, n):
        """The bodies of the comments the land posted on ticket `n` through harness-board, in order."""
        return [a[a.index("--body") + 1] for a in self.stub_calls("harness-board")
                if a[:2] == ["comment", str(n)] and "--body" in a]

    def resolution_of(self, n):
        found = [b for b in self.comments_on(n) if b.lstrip().startswith(RESOLUTION)]
        self.assertEqual(len(found), 1, f"want one Resolution on #{n}, got {self.comments_on(n)}")
        return found[0]

    def baseline(self, fx, tickets):
        """A land with no Cost output at all: (the process, the Resolution text, before any block)."""
        self.costs({})
        p = self.land(fx, tickets)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        texts = {self.resolution_of(n) for n in tickets}
        self.assertEqual(len(texts), 1, "the Resolution without a Cost block is the PR's, the same for each ticket")
        base = texts.pop()
        self.assertEqual(base.rstrip().splitlines()[-1], f"Squash commit: {fx['sha']}", base)
        return p, base

    def assertLandedFollows(self, n, sha):
        bodies = self.comments_on(n)
        at = [i for i, b in enumerate(bodies) if b.lstrip().startswith(RESOLUTION)]
        self.assertEqual(bodies[at[0] + 1:], [f"Landed in {sha}"], f"`Landed in` follows the Resolution on #{n}")

    def no_cost_warnings(self, stderr):
        return [line for line in stderr.splitlines() if "no cost block" in line]

    def isolated_land(self, fx, tickets, work_next_to_tool):
        """A land by a copy of harness-pr in a plugin of its own (its lib and templates linked to the real ones),
        with no harness-work on PATH: the fakes but harness-work, the stubs and the system. With
        `work_next_to_tool`, the harness-work stub sits next to the copy, where a sibling tool is looked up after
        PATH; without it, there is no harness-work anywhere the land could find one."""
        plugin = self.mk("plugin-copy")
        bindir = os.path.join(plugin, "bin")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
            shutil.copy2(TOOL, os.path.join(bindir, "harness-pr"))
            real = os.path.dirname(os.path.dirname(TOOL))
            for part in ("lib", "templates"):
                os.symlink(os.path.join(real, part), os.path.join(plugin, part))
        stub = os.path.join(bindir, "harness-work")
        if os.path.lexists(stub):
            os.remove(stub)
        if work_next_to_tool:
            os.symlink(os.path.join(BINS["fakebin"], "harness-work"), stub)
        nowork = self.mk("fakebin-without-harness-work")
        for name in os.listdir(BINS["fakebin"]):
            if name != "harness-work" and not os.path.lexists(os.path.join(nowork, name)):
                os.symlink(os.path.join(BINS["fakebin"], name), os.path.join(nowork, name))
        path = os.pathsep.join([BINS["stubbin"], nowork, "/usr/bin", "/bin"])
        self.fresh_worktree()
        self.scenario(self.cost_calls(fx, tickets))
        p = subprocess.run([sys.executable, os.path.join(bindir, "harness-pr"), "land", str(fx["pr"]), "--yes"],
                           cwd=self.main, env=self.env(PATH=path), capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr)
        return p

    # ------------------------------------------------------------------ broad
    def test_land_adds_each_tickets_cost_block_to_its_resolution(self):
        for fx, tickets in self.CASES:
            with self.subTest(pr=fx["pr"], tickets=tickets):
                _, base = self.baseline(fx, tickets)
                blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
                # stdout with blank lines around it and trailing spaces: the block is the stdout stripped
                self.costs({n: {"stdout": "\n" + b + "  \n\n"} for n, b in blocks.items()})
                p = self.land(fx, tickets)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                for n in tickets:
                    body = self.resolution_of(n)
                    self.assertEqual(body, base + "\n\n" + blocks[n],
                                     f"#{n}: the Resolution, a blank line, then its own Cost block")
                    for other in tickets:
                        if other != n:
                            self.assertNotIn(blocks[other], body, f"#{n} carries #{other}'s block")
                    lines = body.splitlines()
                    self.assertIn(f"PR: https://github.com/o/r/pull/{fx['pr']}", lines, "the PR: line stays")
                    squash = lines.index(f"Squash commit: {fx['sha']}")
                    self.assertEqual(lines[squash + 1:squash + 3], ["", blocks[n].splitlines()[0]],
                                     "the block follows the Squash commit line after one blank line")
                    self.assertLandedFollows(n, fx["sha"])
                self.assertEqual(self.no_cost_warnings(p.stderr), [])

    def test_cost_runs_after_the_merge_with_the_pr_and_the_main_checkout(self):
        for (fx, tickets), cwd in zip(self.CASES, (self.wt, self.main)):     # from the worktree too: --repo is main
            with self.subTest(pr=fx["pr"], tickets=tickets, cwd=cwd):
                self.costs({n: {"stdout": cost_block(n, fx["pr"]) + "\n"} for n in tickets})
                p = self.land(fx, tickets, cwd=cwd)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                pr = str(fx["pr"])
                self.assertEqual(self.stub_calls("harness-work"),
                                 [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in tickets],
                                 "one cost run per ticket, with the PR and the main checkout")
                merge = self.position(f"gh pr merge {pr}")
                for n in tickets:
                    cost = self.position(f"harness-work cost {n} --pr {pr} --markdown --repo {self.main}")
                    posted = self.position(f"harness-board comment {n} --body {RESOLUTION}")
                    self.assertLess(merge, cost, f"the cost of #{n} runs after the merge")
                    self.assertLess(cost, posted, f"and before #{n}'s Resolution is posted")

        fx, tickets = FIXTURE_B, [21, 12]
        pr = str(fx["pr"])
        with self.subTest("harness-work next to harness-pr, not on PATH: the sibling tool is run from there"):
            blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
            self.costs({n: {"stdout": b + "\n"} for n, b in blocks.items()})
            p = self.isolated_land(fx, tickets, work_next_to_tool=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(self.no_cost_warnings(p.stderr), [], p.stderr)
            self.assertEqual(self.stub_calls("harness-work"),
                             [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in tickets])
            for n in tickets:
                self.assertTrue(self.resolution_of(n).endswith("\n\n" + blocks[n]), self.resolution_of(n))

        with self.subTest("the cost run gets no stdin: what the land was given is not passed on"):
            seen = os.path.join(self.tmp, "cost-stdin.jsonl")
            self.costs({n: {"stdout": cost_block(n, fx["pr"]) + "\n", "read_stdin": seen} for n in tickets})
            self.fresh_worktree()
            self.scenario(self.cost_calls(fx, tickets))
            p = subprocess.run([sys.executable, TOOL, "land", pr, "--yes"], cwd=self.main, env=self.env(),
                               input="typed at the land\n", capture_output=True, text=True, timeout=120)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(self.jsonl(seen), ["" for _ in tickets], "each cost run reads an empty stdin")

    # ------------------------------------------------------------------ specific
    def test_a_failing_cost_run_warns_and_lands_without_a_block(self):
        """#13's cost run fails; #12's works. One warning line for #13, its Resolution as without cost, `Landed
        in` after it, #12 with its block, and the land's stdout and exit as a land with no Cost output."""
        fx, tickets = FIXTURE_A, [12, 13]
        before, base = self.baseline(fx, tickets)
        good = cost_block(12, fx["pr"])
        cases = [("exit 1", {"stderr": "harness-work: no session transcripts for #13\n", "exit": 1},
                  "no session transcripts for #13"),
                 ("exit 2 with stdout", {"stdout": "**Cost** partial table\n",
                                         "stderr": "harness-work: cannot read the attempt store\n", "exit": 2},
                  "cannot read the attempt store"),
                 # a many-line stderr still makes one warning line, from its first line; the rest is not shown
                 ("exit 1 with a many-line stderr",
                  {"stderr": "harness-work: first trouble\ntrace detail one\n  trace detail two\n", "exit": 1},
                  "first trouble"),
                 # nothing said at all: the reason is the exit status
                 ("exit 3 saying nothing", {"exit": 3}, "exit 3"),
                 # only stdout said something: stdout is not a reason, so it falls back to the exit status,
                 # and none of the stdout reaches the warning, the stderr or the Resolution
                 ("exit 4 with only a partial table", {"stdout": "**Cost** partial table\n", "exit": 4}, "exit 4")]
        for name, answer, reason in cases:
            with self.subTest(name):
                self.costs({12: {"stdout": good + "\n"}, 13: answer})
                p = self.land(fx, tickets)
                self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
                self.assertEqual(p.stdout, before.stdout, "the land's report is as without the cost")
                warned = self.no_cost_warnings(p.stderr)
                self.assertEqual(len(warned), 1, p.stderr)
                prefix = "harness-pr: warning: no cost block for #13: "
                self.assertTrue(warned[0].startswith(prefix), warned[0])
                self.assertTrue(warned[0][len(prefix):].strip(), f"the warning gives a reason: {warned[0]!r}")
                self.assertIn(reason, warned[0], "the warning gives the reason")
                self.assertNotIn("trace detail", p.stderr, "one line: the rest of harness-work's stderr is not shown")
                self.assertNotIn("partial table", p.stderr, "harness-work's stdout is never the reason")
                self.assertEqual([line for line in p.stderr.splitlines() if line not in warned],
                                 before.stderr.splitlines(), "no other stderr than a land without cost")
                self.assertEqual(self.resolution_of(13), base, "#13's Resolution goes out without a block")
                self.assertNotIn("partial table", "\n".join(self.comments_on(13)))
                self.assertLandedFollows(13, fx["sha"])
                self.assertEqual(self.resolution_of(12), base + "\n\n" + good)
                self.assertLandedFollows(12, fx["sha"])

        with self.subTest("harness-work cannot be run"):
            broken = self.mk("brokenbin")
            executable(os.path.join(broken, "harness-work"), "#!/nonexistent/harness-interpreter\n")
            path = os.pathsep.join([broken, BINS["stubbin"], BINS["fakebin"], "/usr/bin", "/bin"])
            self.fresh_worktree()
            self.scenario(self.cost_calls(fx, tickets))
            p = self.pr("land", str(fx["pr"]), "--yes", cwd=self.main, PATH=path)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
            self.assertEqual(p.stdout, before.stdout)
            warned = self.no_cost_warnings(p.stderr)
            for n in tickets:
                mine = [line for line in warned if line.startswith(f"harness-pr: warning: no cost block for #{n}: ")]
                self.assertEqual(len(mine), 1, f"one warning for #{n}:\n{p.stderr}")
                self.assertTrue(mine[0].split(f"#{n}: ", 1)[1].strip(), "the warning gives a reason")
                self.assertEqual(self.resolution_of(n), base)
                self.assertLandedFollows(n, fx["sha"])
            self.assertEqual(len(warned), len(tickets), p.stderr)

        with self.subTest("no harness-work on PATH or next to harness-pr"):
            p = self.isolated_land(fx, tickets, work_next_to_tool=False)
            self.assertEqual(p.returncode, before.returncode, p.stdout + p.stderr)
            self.assertEqual(p.stdout, before.stdout)
            self.assertEqual(self.stub_calls("harness-work"), [], "no harness-work was run")
            warned = self.no_cost_warnings(p.stderr)
            for n in tickets:
                mine = [line for line in warned if line.startswith(f"harness-pr: warning: no cost block for #{n}: ")]
                self.assertEqual(len(mine), 1, f"one warning for #{n}:\n{p.stderr}")
                self.assertTrue(mine[0].split(f"#{n}: ", 1)[1].strip(), "the warning gives a reason")
                self.assertEqual(self.resolution_of(n), base)
                self.assertLandedFollows(n, fx["sha"])
            self.assertEqual(len(warned), len(tickets), p.stderr)
            self.assertEqual([line for line in p.stderr.splitlines() if line not in warned],
                             before.stderr.splitlines(), "no other stderr than a land without cost")

    def test_empty_cost_output_adds_nothing(self):
        for fx, tickets in self.CASES:
            for name, stdout in (("empty", ""), ("only blank lines", "\n  \n\n")):
                with self.subTest(pr=fx["pr"], output=name):
                    before, base = self.baseline(fx, tickets)
                    self.costs({n: {"stdout": stdout} for n in tickets})
                    p = self.land(fx, tickets)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual([a[:2] for a in self.stub_calls("harness-work")],
                                     [["cost", str(n)] for n in tickets], "the cost ran for each ticket")
                    for n in tickets:
                        self.assertEqual(self.resolution_of(n), base, f"#{n}: no block, not even a blank line")
                        self.assertLandedFollows(n, fx["sha"])
                    self.assertEqual(self.no_cost_warnings(p.stderr), [], "empty output is no warning")
                    self.assertEqual(p.stderr, before.stderr)
                    self.assertEqual(p.stdout, before.stdout)

    def test_a_resumed_land_with_a_resolution_runs_no_cost(self):
        """The first land posted each ticket's Resolution with its Cost block (and `Landed in`). A resumed land
        recognises that Resolution by its `PR:` line: no cost run and no second Resolution for a ticket that has
        it; a ticket without it still gets its Resolution, with its block, and only its cost runs."""
        for fx, tickets in self.CASES:
            pr, sha = str(fx["pr"]), fx["sha"]
            blocks = {n: cost_block(n, fx["pr"]) for n in tickets}
            self.costs({n: {"stdout": b + "\n"} for n, b in blocks.items()})
            p = self.land(fx, tickets)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            first = {n: self.resolution_of(n) for n in tickets}
            for n in tickets:
                self.assertTrue(first[n].endswith("\n\n" + blocks[n]), f"the first land's Resolution of #{n}")
            done, rest = tickets
            cases = [("each ticket has its Resolution and Landed in",
                      {n: [first[n], f"Landed in {sha}"] for n in tickets}, [], {}),
                     ("each ticket has only its Resolution",
                      {n: [first[n]] for n in tickets}, [], {n: [f"Landed in {sha}"] for n in tickets}),
                     (f"#{done} has its Resolution, #{rest} nothing",
                      {done: [first[done]]}, [rest],
                      {done: [f"Landed in {sha}"], rest: [first[rest], f"Landed in {sha}"]})]
            for name, comments, ran, posted in cases:
                with self.subTest(pr=fx["pr"], case=name):
                    p = self.land(fx, tickets, state="MERGED", comments=comments)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertFalse(any(a[:2] == ["pr", "merge"] for a in self.gh_argv()), "no second merge")
                    self.assertEqual(self.stub_calls("harness-work"),
                                     [["cost", str(n), "--pr", pr, "--markdown", "--repo", self.main] for n in ran],
                                     "the cost runs only for a ticket whose Resolution is posted")
                    for n in tickets:
                        self.assertEqual(self.comments_on(n), posted.get(n, []), f"what the resume posts on #{n}")


if __name__ == "__main__":
    unittest.main()
