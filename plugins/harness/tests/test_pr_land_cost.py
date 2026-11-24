"""The Cost block harness-pr land asks harness-work for.

Split from test_pr.py (it lived in test_pr_ultra.py until #443 removed ultrareview); the fixtures are in
pr_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_land_cost.py" -v
"""

from __future__ import annotations

import unittest

from pr_support import *  # noqa: F401,F403


class LandCost(WorktreeTest):
    """`land` runs `harness-work cost N --pr P --markdown --landed-by loop|hand --repo <main>` for each ticket whose
    Resolution it posts (loop under `land --loop`, else hand, #426) and adds the stdout, stripped, to that ticket's
    Resolution after a blank line. The harness-work stub answers per
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
        # the head checks (#337): the head, HEAD_SHA, level with main and with no harness review
        calls = land_gate_calls(commits=[pr_commit("a", at(9))], pull=f"repos/o/r/pulls/{pr}") + [
            {"match": ["pr", "view", pr, "--json", "mergeCommit"], "stdout_json": {"mergeCommit": {"oid": sha}}},
            {"match": ["pr", "view", pr, "--json", LAND_FIELDS],
             "stdout_json": land_view(url=url, state=state, closingIssuesReferences=[{"number": n} for n in tickets])},
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

    def land(self, fx, tickets, cwd=None, flags=(), **scenario):
        self.fresh_worktree()
        self.scenario(self.cost_calls(fx, tickets, **scenario))
        p = self.pr("land", str(fx["pr"]), "--yes", *flags, cwd=cwd or self.main)
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

    def cost_runs(self):
        """The harness-work calls that are cost runs: a land also runs `dispatch --after N` (#390), not pinned here."""
        return [a for a in self.stub_calls("harness-work") if a[:1] == ["cost"]]

    def without_dispatch_warnings(self, lines):
        """The stderr lines but the `dispatch after #N` warnings of #390, which a missing harness-work adds."""
        return [line for line in lines if not line.startswith("harness-pr: warning: dispatch after #")]

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
                self.assertEqual(self.cost_runs(),
                                 [["cost", str(n), "--pr", pr, "--markdown", "--landed-by", "hand", "--repo",
                                   self.main] for n in tickets],
                                 "one cost run per ticket, with the PR and the main checkout")
                merge = self.position(f"gh pr merge {pr}")
                for n in tickets:
                    cost = self.position(f"harness-work cost {n} --pr {pr} --markdown --landed-by hand --repo "
                                         f"{self.main}")
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
            self.assertEqual(self.cost_runs(),
                             [["cost", str(n), "--pr", pr, "--markdown", "--landed-by", "hand", "--repo", self.main]
                              for n in tickets])
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

    def test_land_tells_cost_whether_the_loop_landed_it(self):
        """`land P --loop` asks for `--landed-by loop`, a land without it for `--landed-by hand` (a missing flag is a
        person's land); the Resolution carries the block the cost run printed, its `Landed by` line included."""
        for fx, tickets in self.CASES:
            pr = str(fx["pr"])
            _, base = self.baseline(fx, tickets)
            for flags, who in ((("--loop",), "loop"), ((), "hand")):
                with self.subTest(pr=fx["pr"], tickets=tickets, flags=flags):
                    blocks = {n: cost_block(n, fx["pr"]) + f"\n\nLanded by: {who}" for n in tickets}
                    self.costs({n: {"stdout": b + "\n"} for n, b in blocks.items()})
                    p = self.land(fx, tickets, flags=flags)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual(self.cost_runs(),
                                     [["cost", str(n), "--pr", pr, "--markdown", "--landed-by", who, "--repo",
                                       self.main] for n in tickets])
                    for n in tickets:
                        body = self.resolution_of(n)
                        self.assertEqual(body, base + "\n\n" + blocks[n])
                        self.assertIn(f"Landed by: {who}", body.splitlines())
                        self.assertLandedFollows(n, fx["sha"])
                    self.assertEqual(self.no_cost_warnings(p.stderr), [])

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
            self.assertEqual(self.cost_runs(), [], "no cost run happened")
            warned = self.no_cost_warnings(p.stderr)
            for n in tickets:
                mine = [line for line in warned if line.startswith(f"harness-pr: warning: no cost block for #{n}: ")]
                self.assertEqual(len(mine), 1, f"one warning for #{n}:\n{p.stderr}")
                self.assertTrue(mine[0].split(f"#{n}: ", 1)[1].strip(), "the warning gives a reason")
                self.assertEqual(self.resolution_of(n), base)
                self.assertLandedFollows(n, fx["sha"])
            self.assertEqual(len(warned), len(tickets), p.stderr)
            others = [line for line in p.stderr.splitlines() if line not in warned]
            self.assertEqual(self.without_dispatch_warnings(others), before.stderr.splitlines(),
                             "no other stderr than a land without cost")

    def test_empty_cost_output_adds_nothing(self):
        for fx, tickets in self.CASES:
            for name, stdout in (("empty", ""), ("only blank lines", "\n  \n\n")):
                with self.subTest(pr=fx["pr"], output=name):
                    before, base = self.baseline(fx, tickets)
                    self.costs({n: {"stdout": stdout} for n in tickets})
                    p = self.land(fx, tickets)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertEqual([a[:2] for a in self.cost_runs()],
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
                    self.assertEqual(self.cost_runs(),
                                     [["cost", str(n), "--pr", pr, "--markdown", "--landed-by", "hand", "--repo",
                                       self.main] for n in ran],
                                     "the cost runs only for a ticket whose Resolution is posted")
                    for n in tickets:
                        self.assertEqual(self.comments_on(n), posted.get(n, []), f"what the resume posts on #{n}")


if __name__ == "__main__":
    unittest.main()
