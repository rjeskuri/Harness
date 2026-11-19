"""harness-work dispatch: the plan, the venues, the effort each launch carries and the routed effort.

Split from test_work.py; the fixtures are in work_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_dispatch.py" -v
"""

from __future__ import annotations

import unittest

from gitrepo_support import copy_repo, run_in_process
from work_support import *  # noqa: F401,F403


# ===================================================================== the rows of a test run in this process (#378)
class RowsInProcess:
    """A test's first `harness-work` run is a process of its own, as WorkTest.work runs it: the exit code and the
    output of the process contract. Every later run, the rows of its table and the runs that only set up a later one,
    runs the tool in this process (gitrepo_support.run_in_process) with the same argv, cwd and environment, so a row
    pays for neither a Python start nor a compile of the tool; a call of setUp inside a test (a fresh project) does not
    make the next run a process again. What the tool runs (git, gh, harness-board, the fake claude) is still its own
    process either way, and a run that starts a headless session is always a process of its own: the `claude -p` it
    launches (start_new_session) outlives the run and must not be left a child of this process."""

    ran_a_process = False            # each test is a new instance, so each test's first run is a process

    @staticmethod
    def starts_a_headless_session(args):
        """`dispatch --venue headless` or `dispatch --api`, not a dry run: it may launch `claude -p` in the background."""
        return (tuple(args[:1]) == ("dispatch",) and "--dry-run" not in args
                and any(a == "--api" or "headless" in a for a in args))

    def work(self, *args, env=None, cwd=None):
        if not self.ran_a_process or self.starts_a_headless_session(args):
            self.ran_a_process = True
            return super().work(*args, env=env, cwd=cwd)
        p = run_in_process(TOOL, args=args, cwd=cwd or self.repo, env=env or self.env())
        self.assertNotIn("Traceback", p.stderr)
        return p


# ===================================================================== the repositories, built once per class (#378)
class CopiedProject:
    """WorkTest.setUp (work_support) with its repositories built once per test class and copied for each setUp: the
    bare origin and the main checkout `proj` (README.md, a .gitignore of .env and *.log, a .worktreeinclude naming
    .env and .env.*, all committed as "First" ten days ago) pushed to it. A class whose every setUp makes more
    repositories adds them in `extend_template`, run once on the template; every folder in the template is copied.

    The template is built with git's background gc and maintenance off (on the command line and in each repository's
    config, so its copies carry it too): nothing changes under a copy while it is taken (#365). A copy's origin remote
    is rewritten to name the copied origin, as `git remote set-url` would. The rest is WorkTest.setUp's own: a fresh
    folder, home, logs and scenario, and the key of the copy's path, on every call (tests call setUp again for a
    fresh project)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-template-"))
        cls.addClassCleanup(shutil.rmtree, root, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_", "FAKECLAUDE_"))}
        env.update(HOME=os.path.join(root, "home"), PATH=os.pathsep.join(["/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")

        def git(repo, *args, **extra):
            p = subprocess.run([GIT, "-C", repo, "-c", "commit.gpgsign=false", "-c", "maintenance.auto=false",
                                "-c", "gc.auto=0", *args], env=dict(env, **extra), capture_output=True, text=True)
            if p.returncode != 0:
                raise AssertionError(f"template: git {' '.join(args)}: {p.stderr}")
            return p.stdout.strip()

        def init(repo, *flags):
            os.makedirs(repo)
            git(repo, "init", "-q", *flags, "-b", "main")
            git(repo, "config", "gc.auto", "0")
            git(repo, "config", "maintenance.auto", "false")

        origin, repo = os.path.join(root, "origin.git"), os.path.join(root, "proj")
        init(origin, "--bare")
        init(repo)
        write(os.path.join(repo, "README.md"), "# proj\n")
        write(os.path.join(repo, ".gitignore"), ".env\n*.log\n")
        write(os.path.join(repo, ".worktreeinclude"), "# copied into new worktrees\n.env\n.env.*\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "First", GIT_AUTHOR_DATE=TEN_DAYS_AGO, GIT_COMMITTER_DATE=TEN_DAYS_AGO)
        git(repo, "remote", "add", "origin", origin)
        git(repo, "push", "-q", "-u", "origin", "main")
        cls.extend_template(root, git, init)
        cls.template_root = root

    @classmethod
    def extend_template(cls, root, git, init):
        """More repositories every setUp of the class starts with, made in `root`; `git(repo, *args)` runs git on
        the template and `init(repo)` makes a repository there as the template's own are made."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-work-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.claude_log = os.path.join(self.tmp, "claude.log")
        root = type(self).template_root
        for name in sorted(os.listdir(root)):
            if name != "home":
                copy_repo(os.path.join(root, name), os.path.join(self.tmp, name))
        self.origin, self.repo = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "proj")
        config = os.path.join(self.repo, ".git", "config")
        with open(config, encoding="utf-8") as fh:
            text = fh.read()
        write(config, text.replace(os.path.join(root, "origin.git"), self.origin))
        self.key = hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12]
        self.scenario()


# =========================================================================== dispatch
class Dispatch(RowsInProcess, CopiedProject, WorkTest):
    def test_dry_run_prints_the_plan_and_starts_nothing(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertIn("venue bg, 1 live, 1 of 2 slots free", p.stdout)
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)
        self.assertIn(f"skip #13: live run {self.key}-13-", p.stdout)
        self.assertIn("skip #14: over --max 2", p.stdout)
        self.assertNotIn("would start #13", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 1)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    def test_headless_launch_bills_through_api_key_helper_and_keeps_the_key_off_argv(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent",
                       **CREDENTIALS)
        p = self.ok("dispatch", "--venue", "headless", env=env)
        self.assertIn(f"#12 headless on {BRANCH} (pid ", p.stdout)

        [record] = self.run_records()
        wt = self.worktree_path()
        self.assertRegex(record["id"], rf"^{self.key}-12-\d{{14}}$")
        self.assertEqual((record["ticket"], record["repo"], record["worktree"], record["branch"], record["venue"]),
                         (12, os.path.realpath(self.repo), wt, BRANCH, "headless"))
        self.assertIsInstance(record["pid"], int)
        self.assertNotIn("bg_id", record)
        self.assertEqual((record["ended_at"], record["exit"], record["outcome"]), (None, None, None))
        self.assertEqual(record["log"], os.path.join(self.runs_dir(), record["id"] + ".log"))
        self.assertTrue(record["started_at"].endswith("Z"))

        self.assertTrue(wait_for(lambda: '"type": "result"' in read_text(record["log"])))
        [call] = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(len(argv), 8)
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        self.assertNotIn(SECRET, json.dumps(call))          # neither argv nor the environment carries the key
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", call["env"])
        for name, value in CREDENTIALS.items():                # an exported key would bill instead of the helper
            self.assertNotIn(name, call["env"])
            self.assertNotIn(value, json.dumps(call))
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", headless)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertEqual(swept["outcome"], "ok")
        self.assertTrue(swept["ended_at"])
        self.assertIn("api: available", self.ok("budget").stdout)

    def test_headless_default_api_key_command(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", "--venue", "headless")
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
        argv = self.claude_calls()[0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"apiKeyHelper": DEFAULT_KEY_CMD})
        self.assertTrue(wait_for(lambda: pid_gone(self.run_records()[0]["pid"])))

    def test_falls_back_to_bg_while_the_api_budget_is_exhausted(self):
        self.write_budget("api", 1, "billing: credit balance is too low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"])
        for name in CREDENTIALS:                               # the subscription login bills, not an exported key
            self.assertNotIn(name, call["env"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["ended_at"]), ("bg", "abc123", None))
        self.assertNotIn("pid", record)
        comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)
        self.assertTrue(body.endswith("<!-- harness -->"), body)

        # The session is still working: the next dispatch leaves the ticket alone.
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working", "cwd": self.worktree_path()}])
        again = self.ok("dispatch", "--dry-run", env=self.env(FAKECLAUDE_AGENTS=agents))
        self.assertIn("skip #12: live run", again.stdout)
        self.assertIn("venue bg", again.stdout)

    def test_redispatches_an_in_progress_ticket_whose_run_ended_without_a_pr(self):
        self.write_run(12, pid=self.dead_pid(), ended_at=iso_ago(hours=1), outcome="billing")
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))])
        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Add the board tool (again after billing) and launch bg", p.stdout)
        self.scenario(issues=[issue(labels=("task", "status:in-progress", "size:S", "risk:low"))],
                      prs=[{"number": 7}])
        p = self.ok("dispatch", "--dry-run")
        self.assertNotIn("would start", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)

    def test_skips_a_ticket_after_three_ended_runs(self):
        in_progress = issue(labels=("task", "status:in-progress", "size:S", "risk:low"))
        for hours in (3, 2):
            self.write_run(12, id=f"{self.key}-12-2026101309{hours:02d}00", pid=self.dead_pid(),
                           started_at=iso_ago(hours=hours, minutes=30), ended_at=iso_ago(hours=hours), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # two ended runs: a third is launched
        self.assertIn("would start #12 Add the board tool (again after error)", p.stdout)

        self.write_run(12, id=f"{self.key}-12-20261013100000", pid=self.dead_pid(),
                       started_at=iso_ago(hours=1, minutes=30), ended_at=iso_ago(hours=1), outcome="error")
        self.scenario(issues=[in_progress])
        p = self.ok("dispatch", "--dry-run")                   # three: the fourth dispatch skips it
        self.assertNotIn("would start", p.stdout)
        self.assertIn("skip #12: 3 runs ended without a ready pull request in 7 days (last: error at ", p.stdout)
        self.assertIn("run it in this session with /harness:work 12, or wait for the window", p.stdout)
        self.assertNotIn("harness-board status 12 ready", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertEqual(data["plan"], [])
        self.assertEqual([s["number"] for s in data["skipped"]], [12])

        p = self.ok("dispatch")                                # for real: nothing started, the skip said
        self.assertIn("skip #12: 3 runs ended", p.stdout)
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(len(self.run_records()), 3)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])])

    # ---- ticket #50: an exhausted api budget never launches headless, whatever --venue says
    def api_until(self):
        return self.read_json(self.budgets_path())["api"]["exhausted_until"]

    def assertRefusedHeadless(self, p, until):
        """Exit non-zero, the message names when the budget is exhausted until and how to launch headless again."""
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("harness-work budget reset api", out)
        self.assertIn(until, out)
        self.assertNotIn("would start", out)
        self.assertNotIn("launching headless anyway", out)

    def assertNothingStarted(self, records_before):
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["issue", "edit"], ["issue", "comment"])],
                         self.gh_calls())
        self.assertEqual(sorted(r["id"] for r in self.run_records()), sorted(records_before))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted(self):
        # #12 is ready on the board; #15 is in progress after a billing run (the #43 case): either would be launched.
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            with self.subTest(hours=hours):
                self.write_budget("api", hours, reason)
                until = self.api_until()
                self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)

                p = self.work("dispatch", "--venue", "headless", env=self.env(**CREDENTIALS))
                self.assertRefusedHeadless(p, until)
                self.assertNotIn("headless on", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--max", "5", "--dry-run")
                self.assertRefusedHeadless(p, until)                # the dry run says the same and plans nothing
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([ended["id"]])

                p = self.work("dispatch", "--venue", "headless", "--dry-run", "--json")
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNothingStarted([ended["id"]])
                self.assertEqual(self.read_json(self.budgets_path())["api"]["exhausted_until"], until)

        # Without --venue the same board launches both on bg, as before: the refusal changed nothing.
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
            for name in CREDENTIALS:
                self.assertNotIn(name, call["env"])
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def unswept_failure(self, result):
        """The overnight #43 case from a clean slate: no budgets file; #15 in progress with a headless run whose
        process is gone, not yet swept, its log ending in `result`; #12 ready on the board. Returns the record."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.budgets_path(), self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)
        record = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(minutes=20))
        write(record["log"], CONNECTORS_WARNING + "\n" + json.dumps(result) + "\n")
        self.scenario(issues=[issue(), issue(15, labels=IN_PROGRESS, title="Billing victim")],
                      ready=[ready_row(12)])
        return record

    def test_venue_headless_is_refused_when_dispatchs_own_sweep_exhausts_the_api_budget(self):
        # The budget is judged after dispatch's sweep: the sweep finds the billing (or auth) failure, marks the api
        # budget, and --venue headless is then refused, as it must have been at 23:55:51Z on 2026-10-13.
        low = dict(API_401, api_error_status=400, result="Credit balance is too low")
        cases = [("billing", low, ()), ("auth", API_401, ()), ("billing", low, ("--dry-run",)),
                 ("auth", API_401_HELPER, ("--dry-run",))]
        for outcome, result, extra in cases:
            with self.subTest(outcome=outcome, extra=extra):
                record = self.unswept_failure(result)
                p = self.work("dispatch", "--venue", "headless", "--max", "5", *extra, env=self.env(**CREDENTIALS))
                self.assertTrue(os.path.exists(self.budgets_path()), p.stdout + p.stderr)
                api = self.read_json(self.budgets_path())["api"]
                self.iso_close_to(api["exhausted_until"], dt.timedelta(hours=24))
                self.assertTrue(api["reason"].startswith(outcome + ": "), api["reason"])
                self.assertRefusedHeadless(p, api["exhausted_until"])
                self.assertNotIn("headless on", p.stdout)
                self.assertNotIn("launch headless", p.stdout)
                self.assertNothingStarted([record["id"]])
                [swept] = self.run_records()
                self.assertEqual(swept["outcome"], outcome)
                self.assertTrue(swept["ended_at"])

        # The same board without --venue: the sweep exhausts the budget and both tickets go to bg, as before.
        record = self.unswept_failure(low)
        p = self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, rf"(?m)^#12 bg on {re.escape(BRANCH)} \(id abc123\)$")
        self.assertRegex(p.stdout, r"(?m)^#15 bg on t/15-\S+ \(id abc123\)$")
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls), ["/harness:work 12", "/harness:work 15"])
        for call in calls:
            self.assertEqual(call["argv"][:2], ["--bg", "-n"])
            self.assertNotIn("-p", call["argv"])
        self.assertEqual({r["id"]: r["outcome"] for r in self.run_records() if r["id"] == record["id"]},
                         {record["id"]: "billing"})
        self.assertEqual(sorted(r["venue"] for r in self.run_records() if not r.get("ended_at")), ["bg", "bg"])

    def test_venue_headless_is_refused_while_the_api_budget_is_exhausted_even_with_nothing_to_launch(self):
        self.write_budget("api", 5, "auth: invalid api key")
        until = self.api_until()
        self.scenario()                                                 # no ready ticket, no run on record
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                p = self.work("dispatch", "--venue", "headless", *extra)
                self.assertRefusedHeadless(p, until)
                self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg")                        # bg is still fine: nothing to do
        self.assertIn("No ready tickets.", p.stdout)
        self.assertEqual(self.claude_calls(), [])

    def test_venue_headless_launches_headless_while_the_api_budget_is_available(self):
        # Four ways the api budget is not exhausted: no budgets file, its time has passed, only the cloud budget is
        # exhausted, and `harness-work budget reset api` after an exhaustion (the refusal's advice).
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        setups = [(12, "Add the board tool", no_file), (13, "Second ticket", lambda: self.write_budget("api", -2)),
                  (14, "Third ticket", lambda: self.write_budget("cloud", 5)), (16, "Fourth ticket", reset)]
        for n, title, setup in setups:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                if os.path.exists(self.claude_log):
                    os.unlink(self.claude_log)
                setup()
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", "--venue", "headless")
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn("budget reset", p.stdout + p.stderr)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                argv = self.claude_calls()[0]["argv"]
                self.assertEqual(argv[:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                            "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": DEFAULT_KEY_CMD})
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    # ---- ticket #57: without --venue, dispatch runs on the subscription (bg) whatever the api budget says
    def budget_states(self):
        """(label, setup) for every state of the api budget: no budgets file, an exhaustion whose time has passed,
        only the cloud budget exhausted, `budget reset api` after an exhaustion, and the api budget exhausted."""
        def no_file():
            if os.path.exists(self.budgets_path()):
                os.unlink(self.budgets_path())

        def reset():
            self.write_budget("api", 6, "billing: credit balance is too low")
            self.ok("budget", "reset", "api")

        return [("no budgets file", no_file), ("expired", lambda: self.write_budget("api", -2, "billing: old")),
                ("cloud only", lambda: self.write_budget("cloud", 5, "cloud: out")), ("reset", reset),
                ("api exhausted", lambda: self.write_budget("api", 3, "billing: credit balance is too low"))]

    def fresh_logs(self):
        for record in self.run_records():                  # a headless child still running would log into the next case
            if isinstance(record.get("pid"), int):
                wait_for(lambda: pid_gone(record["pid"]))
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.gh_log, self.claude_log):
            if os.path.exists(path):
                os.unlink(path)

    def test_default_venue_is_bg_whatever_the_api_budget_says(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        key_cmd = f"cat {keyfile}"
        tickets = [(12, "Add the board tool"), (13, "Second ticket"), (14, "Third ticket"), (16, "Fourth ticket"),
                   (17, "Fifth ticket")]
        for (label, setup), (n, title) in zip(self.budget_states(), tickets):
            with self.subTest(budget=label, ticket=n):
                self.fresh_logs()
                setup()
                budgets_before = read_text(self.budgets_path())
                status_before = self.ok("budget").stdout
                bg_id = f"bg{n}{label[:2]}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                env = self.env(HARNESS_API_KEY_CMD=key_cmd, FAKECLAUDE_BG_ID=bg_id, **CREDENTIALS)
                p = self.ok("dispatch", env=env)
                self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id {bg_id}\)$")
                self.assertNotIn("headless", p.stdout + p.stderr)
                self.assertNotIn("budget reset", p.stdout + p.stderr)

                [call] = self.claude_calls()                   # one session, on the subscription login
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {title}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                self.assertNotIn(key_cmd, json.dumps(call["argv"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name, value in CREDENTIALS.items():          # no exported key or token bills instead
                    self.assertNotIn(name, call["env"])
                    self.assertNotIn(value, json.dumps(call))
                [record] = self.run_records()
                self.assertTrue(record["branch"].startswith(f"t/{n}-"), record["branch"])
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"]),
                                 (n, "bg", bg_id, None))
                self.assertNotIn("pid", record)
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                body = comment[comment.index("--body") + 1]
                self.assertTrue(body.splitlines()[0].endswith(", bg)"), body)

                # dispatch leaves the budgets as it found them: the default does not depend on them or change them
                self.assertEqual(read_text(self.budgets_path()), budgets_before)
                self.assertEqual(self.ok("budget").stdout, status_before)

    def test_dry_run_plan_names_bg_by_default_and_headless_only_on_request(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        board = dict(issues=[issue(), issue(13), issue(14, title="Third"), issue(15, title="Fourth")],
                     ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third"), ready_row(15, "Fourth")])
        for (label, setup), most in zip(self.budget_states()[:4], (2, 3, 4, 5)):    # the api budget is available
            with self.subTest(budget=label, max=most):
                setup()
                self.scenario(**board)
                free = most - 1
                planned = [n for n in (12, 14, 15)][:free]
                for venue, extra in (("bg", ()), ("bg", ("--venue", "bg")), ("headless", ("--venue", "headless"))):
                    p = self.ok("dispatch", "--dry-run", "--max", str(most), *extra)
                    self.assertIn(f"Dry run: venue {venue}, 1 live, {free} of {most} slots free.", p.stdout)
                    starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                    self.assertEqual(len(starts), len(planned), p.stdout)
                    for line in starts:
                        self.assertTrue(line.endswith(f"and launch {venue}"), line)
                    other = "headless" if venue == "bg" else "launch bg"
                    self.assertNotIn(other, p.stdout)
                    data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *extra)
                    self.assertEqual(data["venue"], venue)
                    self.assertEqual([row["number"] for row in data["plan"]], planned)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(len(self.run_records()), 1)
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_redispatched_ticket_launches_bg_by_default(self):
        # #12 ended early, #15 ended in an error, both still in progress with no PR; #16 is fresh from the board.
        # No budgets file: the api budget is available, and still every launch goes to the subscription.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="early")
        self.write_run(15, id=f"{self.key}-15-20261013080000", pid=self.dead_pid(), started_at=iso_ago(hours=5),
                       ended_at=iso_ago(hours=4), outcome="error")
        titles = {12: "Ended early", 15: "Errored", 16: "Fresh"}
        self.scenario(issues=[issue(12, labels=IN_PROGRESS, title=titles[12]),
                              issue(15, labels=IN_PROGRESS, title=titles[15]), issue(16, title=titles[16])],
                      ready=[ready_row(16, titles[16])])
        self.assertFalse(os.path.exists(self.budgets_path()))

        p = self.ok("dispatch", "--dry-run")
        self.assertIn("would start #12 Ended early (again after early) and launch bg", p.stdout.splitlines())
        self.assertIn("would start #15 Errored (again after error) and launch bg", p.stdout.splitlines())
        self.assertNotIn("headless", p.stdout)

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57", **CREDENTIALS))
        for n in (12, 15, 16):
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id redo57\)$")
        self.assertNotIn("headless", p.stdout)
        calls = {int(c["argv"][-1].split()[1]): c for c in self.claude_calls()}
        self.assertEqual(sorted(calls), [12, 15, 16])
        for n, call in calls.items():
            with self.subTest(ticket=n):
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} {titles[n]}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual(sorted((r["ticket"], r["venue"], r.get("bg_id"), "pid" in r) for r in fresh),
                         [(12, "bg", "redo57", False), (15, "bg", "redo57", False), (16, "bg", "redo57", False)])

    # ---- ticket #57: `--api` is the CLI's own alias for `--venue headless`, the only other way onto the API key
    def test_api_launches_headless_on_the_api_key_while_the_budget_is_available(self):
        tickets = [(12, "Add the board tool", ("--api",)), (13, "Second ticket", ("--api", "--venue", "headless")),
                   (14, "Third ticket", ("--venue", "headless", "--api")), (16, "Fourth ticket", ("--api",))]
        for (label, setup), (n, title, flags) in zip(self.budget_states()[:4], tickets):
            with self.subTest(budget=label, ticket=n, flags=flags):
                self.fresh_logs()
                setup()
                keyfile = write(os.path.join(self.tmp, f"key{n}.txt"), SECRET + "\n")
                key_cmd = f"cat {keyfile}"
                self.scenario(issues=[issue(n, title=title)], ready=[ready_row(n, title)])
                p = self.ok("dispatch", *flags, env=self.env(HARNESS_API_KEY_CMD=key_cmd, **CREDENTIALS))
                self.assertRegex(p.stdout, rf"(?m)^#{n} headless on t/{n}-\S+ \(pid \d+, log ")
                self.assertNotIn(" bg on ", p.stdout)
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"]), (n, "headless"))
                self.assertIsInstance(record["pid"], int)
                self.assertNotIn("bg_id", record)
                self.assertTrue(wait_for(lambda: len(self.claude_calls()) == 1))
                [call] = self.claude_calls()
                self.assertEqual(call["argv"][:7], ["-p", f"/harness:work {n}", "--permission-mode", "auto",
                                                    "--output-format", "json", "--settings"])
                self.assertEqual(json.loads(call["argv"][7]), {"apiKeyHelper": key_cmd})
                self.assertEqual(len(call["argv"]), 8)
                self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path(record["branch"]))
                self.assertNotIn(SECRET, json.dumps(call))
                for name in CREDENTIALS:
                    self.assertNotIn(name, call["env"])
                comment = next(c for c in self.gh_calls() if c[:2] == ["issue", "comment"] and c[2] == str(n))
                self.assertTrue(comment[comment.index("--body") + 1].splitlines()[0].endswith(", headless)"))
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_api_dry_run_plans_headless(self):
        self.scenario(issues=[issue(), issue(13), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])
        for most, flags in ((1, ("--api",)), (2, ("--api", "--venue", "headless")), (3, ("--api",))):
            with self.subTest(max=most, flags=flags):
                p = self.ok("dispatch", "--dry-run", "--max", str(most), *flags)
                self.assertIn(f"Dry run: venue headless, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for line in starts:
                    self.assertTrue(line.endswith("and launch headless"), line)
                self.assertNotIn("launch bg", p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json", *flags)
                self.assertEqual(data["venue"], "headless")
                self.assertEqual([row["number"] for row in data["plan"]], [12, 13, 14][:most])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_api_is_refused_while_the_api_budget_is_exhausted(self):
        ended = self.write_run(15, pid=self.dead_pid(), started_at=iso_ago(hours=2), ended_at=iso_ago(hours=1),
                               outcome="billing")
        in_progress = issue(15, labels=IN_PROGRESS, title="Billing victim")
        for hours, reason in ((1, "billing: credit balance is too low"), (30, "auth: invalid api key")):
            for flags in (("--api",), ("--api", "--venue", "headless")):
                for extra in ((), ("--dry-run",), ("--dry-run", "--json")):
                    with self.subTest(hours=hours, flags=flags, extra=extra):
                        self.write_budget("api", hours, reason)
                        until = self.api_until()
                        self.scenario(issues=[issue(), in_progress], ready=[ready_row(12)])
                        for path in (self.gh_log, self.claude_log):
                            if os.path.exists(path):
                                os.unlink(path)
                        p = self.work("dispatch", *flags, "--max", "5", *extra, env=self.env(**CREDENTIALS))
                        if "--json" in extra:
                            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                        else:
                            self.assertRefusedHeadless(p, until)
                            self.assertNotIn("launch headless", p.stdout)
                            self.assertNotIn("headless on", p.stdout)
                        self.assertNothingStarted([ended["id"]])
                        self.assertEqual(self.api_until(), until)
        # nothing to launch at all: still refused
        self.scenario()
        p = self.work("dispatch", "--api")
        self.assertRefusedHeadless(p, self.api_until())

    def test_api_with_venue_bg_is_an_error_and_starts_nothing(self):
        self.scenario(issues=[issue()], ready=[ready_row(12)])            # the api budget is available
        # --api alone is a known flag (so the error below is the conflict, not an unknown argument)
        p = self.ok("dispatch", "--api", "--dry-run")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch headless", p.stdout)
        for flags in (("--api", "--venue", "bg"), ("--venue", "bg", "--api")):
            for extra in ((), ("--dry-run",)):
                with self.subTest(flags=flags, extra=extra):
                    p = self.work("dispatch", *flags, *extra, env=self.env(**CREDENTIALS))
                    self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("--api", p.stderr)
                    self.assertNotIn("unrecognized", p.stderr)
                    self.assertNotIn("would start", p.stdout)
                    self.assertNothingStarted([])
        p = self.ok("dispatch", "--venue", "bg", "--dry-run")              # the control: the same board plans bg
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", p.stdout)


class DispatchDefaultMax(RowsInProcess, CopiedProject, WorkTest):
    """Ticket #130: `harness-work dispatch` without `--max` allows ten live runs at once (it was three)."""

    def board(self, count):
        numbers = list(range(12, 12 + count))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        return numbers

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def test_dry_run_without_max_plans_ten_and_skips_the_rest_over_max_ten(self):
        for count in (12, 15):
            with self.subTest(ready=count):
                numbers = self.board(count)
                p = self.ok("dispatch", "--dry-run")
                self.assertIn("Dry run: venue bg, 0 live, 10 of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), 10, p.stdout)
                for n in numbers[:10]:
                    self.assertIn(f"would start #{n} Ticket {n} (tier sonnet) and launch bg", p.stdout.splitlines())
                for n in numbers[10:]:
                    self.assertIn(f"skip #{n}: over --max 10", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:10])
                self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                                 [(n, "over --max 10") for n in numbers[10:]])
                self.assertEqual(self.claude_calls(), [])

    def test_dry_run_with_ten_or_fewer_ready_tickets_plans_them_all_with_nothing_skipped(self):
        for count in (4, 10):                       # four is past the old default of three
            with self.subTest(ready=count):
                numbers = self.board(count)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers)
                self.assertEqual(data["skipped"], [])
                self.assertNotIn("skip #", self.ok("dispatch", "--dry-run").stdout)

    def test_the_free_slots_are_ten_minus_the_live_runs(self):
        for live_count in (1, 4):
            with self.subTest(live=live_count):
                if os.path.isdir(self.runs_dir()):
                    shutil.rmtree(self.runs_dir())
                live_numbers = list(range(40, 40 + live_count))
                for n in live_numbers:
                    self.write_run(n, pid=self.sleeper(n))
                numbers = list(range(12, 24))
                self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers + live_numbers],
                              ready=[ready_row(n, f"Ticket {n}") for n in numbers])
                free = 10 - live_count
                p = self.ok("dispatch", "--dry-run")
                self.assertIn(f"Dry run: venue bg, {live_count} live, {free} of 10 slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), free, p.stdout)
                data = self.ok_json("dispatch", "--dry-run", "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:free])
                self.assertEqual([s["number"] for s in data["skipped"]], numbers[free:])
                self.assertTrue(all(s["why"] == "over --max 10" for s in data["skipped"]))

    def test_ten_live_runs_fill_the_default_cap(self):
        for n in range(40, 50):
            self.write_run(n, pid=self.sleeper(n))
        self.scenario(issues=[issue(12), issue(13)], ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch")
        self.assertIn("10 live runs; --max 10 reached, nothing launched.", p.stdout)
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_real_dispatch_without_max_launches_ten_of_twelve(self):
        numbers = self.board(12)
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="ten123", **CREDENTIALS))
        for n in numbers[:10]:
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id ten123\)$")
        for n in numbers[10:]:
            self.assertNotRegex(p.stdout, rf"(?m)^#{n} bg on")
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:10]))
        self.assertEqual(len([r for r in self.run_records() if r["venue"] == "bg"]), 10)

    def test_a_real_dispatch_without_max_and_four_live_runs_launches_six(self):
        for n in range(40, 44):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:6]))

    def test_an_explicit_max_still_overrides_the_default(self):
        # 3 is the old default and 10 the new one: an explicit value equal to either must be honored as given.
        for most in (2, 3, 5, 10, 12):
            with self.subTest(max=most):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", "--max", str(most))
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--max", str(most), "--json")
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_an_explicit_max_in_the_equals_form_or_abbreviated_is_honored(self):
        # `--max=3`, `--max=5` and the argparse abbreviation `--ma 3` are explicit too; none may fall back to 10.
        for spelling, most in ((("--max=3",), 3), (("--max=5",), 5), (("--max=10",), 10), (("--ma", "3"), 3),
                               (("--ma=5",), 5)):
            with self.subTest(args=" ".join(spelling)):
                numbers = self.board(12)
                p = self.ok("dispatch", "--dry-run", *spelling)
                self.assertIn(f"Dry run: venue bg, 0 live, {most} of {most} slots free.", p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual(len(starts), most, p.stdout)
                for n in numbers[most:]:
                    self.assertIn(f"skip #{n}: over --max {most}", p.stdout.splitlines())
                data = self.ok_json("dispatch", "--dry-run", "--json", *spelling)
                self.assertEqual([row["number"] for row in data["plan"]], numbers[:most])
                self.assertEqual([s["why"] for s in data["skipped"]], [f"over --max {most}"] * (12 - most))

    def test_a_real_dispatch_with_max_equals_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max=3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))

    def test_a_real_dispatch_with_an_explicit_max_of_three_launches_exactly_three(self):
        numbers = self.board(12)
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:3]))
        self.assertEqual(len(self.run_records()), 3)

    def test_a_real_dispatch_with_an_explicit_max_of_three_and_one_live_run_launches_two(self):
        self.write_run(40, pid=self.sleeper(40))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "3", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:2]))

    def test_a_real_dispatch_with_an_explicit_max_of_ten_and_three_live_runs_launches_seven(self):
        for n in range(40, 43):
            self.write_run(n, pid=self.sleeper(n))
        numbers = list(range(12, 24))
        self.scenario(issues=[issue(n, title=f"Ticket {n}") for n in numbers],
                      ready=[ready_row(n, f"Ticket {n}") for n in numbers])
        self.ok("dispatch", "--max", "10", env=self.env(**CREDENTIALS))
        self.assertEqual(sorted(c["argv"][-1] for c in self.claude_calls()),
                         sorted(f"/harness:work {n}" for n in numbers[:7]))




class DispatchEffort(RowsInProcess, CopiedProject, WorkTest):
    """Ticket #176: `harness-work dispatch` launches each session at the effort the project configures
    (HARNESS_CODE_EFFORT in the main checkout's .claude/settings.json), as `--effort <level>`; the dispatcher's own
    CLAUDE_EFFORT neither decides the flag nor reaches the child. Without the setting nothing changes. WorkTest.env
    drops every CLAUDE* and HARNESS_* variable of the real environment, so these tests set their own."""

    BG_TODAY = ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto", "/harness:work 12"]

    def configure(self, **env):
        """The main checkout's settings.json with the given env keys (HARNESS_CODE_EFFORT=...)."""
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))

    def bg_with_effort(self, name, effort, prompt):
        return ["--bg", "-n", name, "--permission-mode", "auto", "--effort", effort, prompt]

    def warnings(self, p):
        return [ln for ln in p.stderr.splitlines() if ln.startswith("harness-work:")]

    @staticmethod
    def effort_problem(raw):
        """The one warning for an invalid HARNESS_CODE_EFFORT: `harness-work: ` + load_routing's problems line."""
        return f"harness-work: HARNESS_CODE_EFFORT={raw!r} is not one of low, medium, high, xhigh, max; ignored"

    def dispatch_from(self, cwd, *args, env=None):
        p = self.work("dispatch", *args, env=env, cwd=cwd)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def headless_call(self, index=0):
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) > index), self.claude_calls())
        return self.claude_calls()[index]

    def settle(self):
        """Wait for the fake headless runs to end, so no process outlives the test."""
        for record in self.run_records():
            if record.get("pid"):
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))

    def test_bg_launch_passes_the_configured_effort(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                        "--effort", "xhigh", "/harness:work 12"])
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["bg_id"], record["effort"]), ("bg", "abc123", "xhigh"))

    def test_the_flag_follows_the_setting_and_covers_every_launched_ticket(self):
        # Two tickets in one dispatch both get the level; a later dispatch with another level gives that one. A
        # hard-coded level or a flag that ignores the setting fails one of the three launches.
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        self.ok("dispatch", "--max", "2")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": self.bg_with_effort("proj #12 Add the board tool", "low",
                                                               "/harness:work 12"),
            "proj #13 Second": self.bg_with_effort("proj #13 Second", "low", "/harness:work 13")})

        self.configure(HARNESS_CODE_EFFORT="max")
        agents = json.dumps([{"id": "abc123", "kind": "background", "state": "working",
                              "cwd": self.worktree_path("t/12-add-the-board-tool")}])
        self.scenario(issues=[issue(12), issue(13, title="Second"), issue(14, title="Third")],
                      ready=[ready_row(14, "Third")])
        self.ok("dispatch", env=self.env(FAKECLAUDE_AGENTS=agents))
        newest = self.claude_calls()[-1]["argv"]
        self.assertEqual(newest, self.bg_with_effort("proj #14 Third", "max", "/harness:work 14"))

    def test_every_valid_level_is_passed_as_given(self):
        # The level is the setting's value, lower-cased; one dispatch per level in a project of its own.
        for level, written in (("low", "low"), ("medium", "medium"), ("high", "High"), ("xhigh", "XHIGH"),
                               ("max", "max")):
            with self.subTest(level=written):
                self.setUp()                                     # a fresh project, home and logs per level
                self.configure(HARNESS_CODE_EFFORT=written)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", level,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], level)

    def test_headless_launch_passes_the_configured_effort(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        env = self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}")
        self.ok("dispatch", "--venue", "headless", env=env)
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:7], ["-p", "/harness:work 12", "--permission-mode", "auto", "--output-format", "json",
                                    "--settings"])
        self.assertEqual(json.loads(argv[7]), {"apiKeyHelper": f"cat {keyfile}"})
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.assertNotIn(SECRET, json.dumps(self.claude_calls()))
        self.settle()

    def test_without_the_setting_argv_and_record_are_unchanged(self):
        # No settings file at all, a settings file without the key, and a blank value: all three are "not set".
        cases = (("no settings file", None), ("other keys only", {"HARNESS_CODE_TIER": "opus"}),
                 ("blank value", {"HARNESS_CODE_EFFORT": "  "}))
        for label, env in cases:
            with self.subTest(label):
                self.setUp()
                if env is not None:
                    self.configure(**env)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch")
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertEqual(sorted(record), sorted(["id", "ticket", "repo", "worktree", "branch", "venue", "log",
                                                         "started_at", "ended_at", "exit", "outcome", "bg_id"]))
        with self.subTest("headless"):
            self.setUp()
            self.scenario(issues=[issue()], ready=[ready_row(12)])
            self.ok("dispatch", "--venue", "headless")
            argv = self.headless_call()["argv"]
            self.assertEqual(len(argv), 8, argv)
            self.assertNotIn("--effort", argv)
            [record] = self.run_records()
            self.assertNotIn("effort", record)
            self.settle()

    def test_dry_run_and_json_name_the_effort(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/harness:work 13"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.write_run(13, pid=live.pid)
        self.scenario(issues=[issue(), issue(13), issue(14)],
                      ready=[ready_row(12), ready_row(13, "Second"), ready_row(14, "Third")])

        self.configure(HARNESS_CODE_EFFORT="xhigh")
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "Dry run: venue bg, effort xhigh, 1 live, 1 of 2 slots free.")
        self.assertIn("would start #12 Add the board tool (tier sonnet) and launch bg", lines)
        self.assertTrue(any(ln.startswith("skip #14: over --max 2") for ln in lines), p.stdout)

        p = self.ok("dispatch", "--dry-run", "--max", "3", "--venue", "headless")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue headless, effort xhigh, 1 live, 2 of 3 slots free.")

        self.configure(HARNESS_CODE_EFFORT="Max")                # the level as launched: lower-cased
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "max"))
        self.assertEqual([row["number"] for row in data["plan"]], [12])
        self.assertEqual(self.ok("dispatch", "--dry-run", "--max", "2").stdout.splitlines()[0],
                         "Dry run: venue bg, effort max, 1 live, 1 of 2 slots free.")

        self.configure()                                         # not set: the line is today's, effort is null
        p = self.ok("dispatch", "--dry-run", "--max", "2")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 1 live, 1 of 2 slots free.")
        self.assertNotIn("effort", p.stdout)
        data = self.ok_json("dispatch", "--dry-run", "--max", "2", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        self.assertEqual(self.claude_calls(), [])

    def test_the_json_result_of_a_launch_carries_the_effort(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["venue"], data["effort"]), ("bg", "high"))
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "high"))

        self.setUp()                                             # unset: the key is there, null, the record has none
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--json")
        self.assertIn("effort", data)
        self.assertIsNone(data["effort"])
        [launched] = data["launched"]
        self.assertNotIn("effort", launched)

        self.setUp()                                             # nothing to launch: the key is still there
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario()
        data = self.ok_json("dispatch", "--json")
        self.assertEqual((data["launched"], data["effort"]), ([], "high"))

    def test_the_dispatchers_claude_effort_is_not_inherited(self):
        # The dispatching Claude Code session exports CLAUDE_EFFORT (its own level) to its Bash tool.
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.assertNotIn("max", call["argv"])

        self.setUp()                                             # headless: the same
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", "--venue", "headless", env=self.env(CLAUDE_EFFORT="max"))
        call = self.headless_call()
        self.assertEqual(call["argv"][8:], ["--effort", "low"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.settle()

        self.setUp()                                             # no setting: the dispatcher's level decides nothing
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("--effort", call["argv"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        [record] = self.run_records()
        self.assertNotIn("effort", record)

    def test_invalid_effort_warns_and_launches_without_the_flag(self):
        for bad in ("turbo", "extra-high", "5"):
            with self.subTest(bad=bad):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT=bad)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(CLAUDE_EFFORT="max"))
                self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.BG_TODAY)
                self.assertNotIn("--effort", call["argv"])
                [record] = self.run_records()
                self.assertNotIn("effort", record)
                self.assertIn(f"#12 bg on {BRANCH} (id abc123)", p.stdout)    # the ticket still launched

    def test_an_invalid_effort_is_one_warning_per_dispatch_not_per_launch(self):
        self.configure(HARNESS_CODE_EFFORT="turbo")
        self.scenario(issues=[issue(12), issue(13, title="Second")],
                      ready=[ready_row(12), ready_row(13, "Second")])
        p = self.ok("dispatch", "--max", "2")
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
        self.assertIn("#12 bg on", p.stdout)
        self.assertIn("#13 bg on", p.stdout)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls, {
            "proj #12 Add the board tool": ["--bg", "-n", "proj #12 Add the board tool", "--permission-mode", "auto",
                                            "/harness:work 12"],
            "proj #13 Second": ["--bg", "-n", "proj #13 Second", "--permission-mode", "auto", "/harness:work 13"]})
        self.assertTrue(all("effort" not in r for r in self.run_records()))

    def test_an_invalid_effort_is_warned_about_on_paths_that_launch_nothing(self):
        bad = "turbo"
        with self.subTest("dry run"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario(issues=[issue(12), issue(13, title="Second")],
                          ready=[ready_row(12), ready_row(13, "Second")])
            p = self.ok("dispatch", "--dry-run", "--max", "2")
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, 0 live, 2 of 2 slots free.")
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board"):
            self.setUp()
            self.configure(HARNESS_CODE_EFFORT=bad)
            self.scenario()
            p = self.ok("dispatch")
            self.assertIn("No ready tickets.", p.stdout)
            self.assertEqual(self.warnings(p), [self.effort_problem(bad)], p.stderr)
            self.assertEqual(p.stderr.count("HARNESS_CODE_EFFORT"), 1, p.stderr)
            self.assertEqual(self.claude_calls(), [])
        with self.subTest("empty board, invalid value from the environment"):
            self.setUp()
            self.scenario()
            p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="extra-high"))
            self.assertEqual(self.warnings(p), [self.effort_problem("extra-high")], p.stderr)

    def test_with_json_the_invalid_effort_warning_is_on_stderr_only(self):
        for label, args, scenario in (
                ("launch", ("dispatch", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("dry run", ("dispatch", "--dry-run", "--json"), dict(issues=[issue()], ready=[ready_row(12)])),
                ("empty board", ("dispatch", "--json"), {})):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="turbo")
                self.scenario(**scenario)
                p = self.ok(*args)
                self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
                self.assertNotIn("HARNESS_CODE_EFFORT", p.stdout)
                data = json.loads(p.stdout)                      # stdout stays pure JSON
                self.assertIn("effort", data)
                self.assertIsNone(data["effort"])
                if label == "launch":
                    [launched] = data["launched"]
                    self.assertNotIn("effort", launched)

    def test_a_valid_effort_is_not_warned_about(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        self.assertEqual(p.stderr.strip(), "")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "medium", "/harness:work 12"))

    def test_the_setting_is_read_from_the_main_checkout_whatever_the_cwd(self):
        # The setting lives only in the main checkout's settings.json. Dispatch run from a subdirectory of it, and
        # from another worktree of the repository (whose own settings.json says otherwise), uses the main one.
        for label in ("subdirectory", "other worktree"):
            with self.subTest(label):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="xhigh")
                cwd = self.mk("proj", "docs", "deep")
                if label == "other worktree":
                    cwd = os.path.join(self.tmp, "side-wt")
                    self.git(self.repo, "worktree", "add", "-q", "-b", "side", cwd)
                    self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.json")))
                    write(os.path.join(cwd, ".claude", "settings.json"),
                          json.dumps({"env": {"HARNESS_CODE_EFFORT": "low"}}))
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.dispatch_from(cwd)
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh",
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_the_setting_is_found_from_a_subdirectory_for_headless_and_dry_run(self):
        self.configure(HARNESS_CODE_EFFORT="medium")
        sub = self.mk("proj", "a", "b")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.dispatch_from(sub, "--dry-run", "--max", "3")
        self.assertEqual(p.stdout.splitlines()[0], "Dry run: venue bg, effort medium, 0 live, 3 of 3 slots free.")
        self.dispatch_from(sub, "--venue", "headless")
        self.assertEqual(self.headless_call()["argv"][8:], ["--effort", "medium"])
        self.settle()

    def test_the_environment_beats_the_file_when_not_blank(self):
        for label, in_env, in_file, want in (("env over file", "low", "xhigh", "low"),
                                            ("env level lower-cased", "MAX", "low", "max"),
                                            ("env padded", "  high ", "low", "high"),
                                            ("env only", "medium", None, "medium")):
            with self.subTest(label):
                self.setUp()
                if in_file is not None:
                    self.configure(HARNESS_CODE_EFFORT=in_file)
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=in_env))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", want,
                                                                   "/harness:work 12"))
                self.assertEqual(self.run_records()[0]["effort"], want)

    def test_a_blank_environment_value_falls_back_to_the_file(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                self.setUp()
                self.configure(HARNESS_CODE_EFFORT="high")
                self.scenario(issues=[issue()], ready=[ready_row(12)])
                p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT=blank))
                self.assertEqual(self.warnings(p), [])
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "high",
                                                                   "/harness:work 12"))

    def test_an_invalid_environment_value_is_warned_about_and_not_rescued_by_the_file(self):
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch", env=self.env(HARNESS_CODE_EFFORT="turbo"))
        self.assertEqual(self.warnings(p), [self.effort_problem("turbo")], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

    def test_settings_local_json_is_never_read(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        data = self.ok_json("dispatch", "--dry-run", "--json")
        self.assertIsNone(data["effort"])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [])
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)
        self.assertNotIn("effort", self.run_records()[0])

        self.setUp()                                             # nor does it override settings.json
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              json.dumps({"env": {"HARNESS_CODE_EFFORT": "max"}}))
        self.configure(HARNESS_CODE_EFFORT="low")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        self.ok("dispatch")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "low", "/harness:work 12"))

    def test_another_invalid_routing_key_is_its_own_warning_and_leaves_the_effort_alone(self):
        self.configure(HARNESS_CODE_EFFORT="xhigh", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p),
                         ["harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored"],
                         p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(self.run_records()[0]["effort"], "xhigh")

        self.setUp()                                             # three invalid keys: three warnings, routing order
        self.configure(HARNESS_CODE_EFFORT="turbo", HARNESS_REVIEW_TIER="gold", HARNESS_CODE_TIER="bogus")
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.ok("dispatch")
        self.assertEqual(self.warnings(p), [
            "harness-work: HARNESS_CODE_TIER='bogus' is not one of sonnet, opus, fable; ignored",
            self.effort_problem("turbo"),
            "harness-work: HARNESS_REVIEW_TIER='gold' is not one of sonnet, opus, fable; ignored"], p.stderr)
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.BG_TODAY)

    def test_a_redispatched_ticket_gets_the_effort_too(self):
        # #12 is in progress and its last run ended in an error; #13 is fresh from the board.
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="xhigh")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS), issue(13, title="Fresh")],
                      ready=[ready_row(13, "Fresh")])
        dry = self.ok("dispatch", "--dry-run", "--max", "3")
        self.assertIn("would start #12 Add the board tool (again after error) and launch bg", dry.stdout)
        self.assertEqual(dry.stdout.splitlines()[0], "Dry run: venue bg, effort xhigh, 0 live, 3 of 3 slots free.")

        p = self.ok("dispatch", "--max", "3", env=self.env(FAKECLAUDE_BG_ID="redo57"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo57\)$")
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        self.assertEqual(calls["proj #12 Add the board tool"],
                         self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        self.assertEqual(calls["proj #13 Fresh"], self.bg_with_effort("proj #13 Fresh", "xhigh", "/harness:work 13"))
        again = next(r for r in self.run_records() if r["ticket"] == 12 and r.get("bg_id") == "redo57")
        self.assertEqual(again["effort"], "xhigh")

    def test_a_redispatched_ticket_launches_headless_with_the_effort(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        self.configure(HARNESS_CODE_EFFORT="high")
        self.scenario(issues=[issue(12, labels=IN_PROGRESS)])
        self.ok("dispatch", "--venue", "headless")
        argv = self.headless_call()["argv"]
        self.assertEqual((argv[:2], argv[8:]), (["-p", "/harness:work 12"], ["--effort", "high"]))
        self.settle()


class DispatchRouteEffort(RowsInProcess, CopiedProject, WorkTest):
    """Ticket #239: with HARNESS_CODE_EFFORT unset, `harness-work dispatch` launches each ticket at its route's effort:
    a size:L or risk:high ticket gets `--effort xhigh` (bg and headless) and `effort: xhigh` in its run record; any
    other ticket launches as before, with no flag. HARNESS_CODE_EFFORT still wins when set; a HARNESS_CODE_TIER
    override replaces the labels' effort with its tier."""

    configure = DispatchEffort.configure
    bg_with_effort = DispatchEffort.bg_with_effort
    headless_call = DispatchEffort.headless_call
    settle = DispatchEffort.settle

    @staticmethod
    def labels_for(size, risk, kind="task", status="status:ready"):
        return (kind, status) + (("size:" + size,) if size else ()) + (("risk:" + risk,) if risk else ())

    @staticmethod
    def name(n, title):
        return f"proj #{n} {title}"

    def bg_today(self, n, title):
        return ["--bg", "-n", self.name(n, title), "--permission-mode", "auto", f"/harness:work {n}"]

    def test_each_ticket_launches_at_its_routes_effort(self):
        # One dispatch, four tickets: the route's effort per ticket (xhigh for size:L or risk:high, else no flag).
        tickets = [(12, "Small", "task", "S", "low"), (13, "Risky", "task", "M", "high"),
                   (14, "Large", "task", "L", "low"), (15, "Medium bug", "bug", "M", "low")]
        self.scenario(issues=[issue(n, labels=self.labels_for(s, r, k), title=t) for n, t, k, s, r in tickets],
                      ready=[ready_row(n, t, labels=self.labels_for(s, r, k)) for n, t, k, s, r in tickets])
        p = self.ok("dispatch", "--max", "4")
        self.assertNotIn("harness-work:", p.stderr)
        calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
        records = {r["ticket"]: r for r in self.run_records()}
        for n, title, kind, size, risk in tickets:
            effort = route_of(kind, size, risk)[1]
            with self.subTest(ticket=n, size=size, risk=risk):
                if effort:
                    self.assertEqual(calls[self.name(n, title)],
                                     self.bg_with_effort(self.name(n, title), effort, f"/harness:work {n}"))
                    self.assertEqual(records[n]["effort"], effort)
                else:
                    self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                    self.assertNotIn("effort", records[n])

    def test_a_size_l_ticket_launches_bg_with_effort_xhigh(self):
        large = self.labels_for("L", "low")
        self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
        data = self.ok_json("dispatch", "--json")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        [record] = self.run_records()
        self.assertEqual(record["effort"], "xhigh")
        [launched] = data["launched"]
        self.assertEqual((launched["ticket"], launched["effort"]), (12, "xhigh"))

    def test_a_risk_high_ticket_launches_headless_with_effort_xhigh(self):
        keyfile = write(os.path.join(self.tmp, "key.txt"), SECRET + "\n")
        risky = self.labels_for("S", "high")
        self.scenario(issues=[issue(labels=risky)], ready=[ready_row(12, labels=risky)])
        self.ok("dispatch", "--venue", "headless", env=self.env(HARNESS_API_KEY_CMD=f"cat {keyfile}"))
        argv = self.headless_call()["argv"]
        self.assertEqual(len(argv), 10, argv)
        self.assertEqual(argv[:2], ["-p", "/harness:work 12"])
        self.assertEqual(argv[8:], ["--effort", "xhigh"])
        [record] = self.run_records()
        self.assertEqual((record["venue"], record["effort"]), ("headless", "xhigh"))
        self.settle()

    def test_the_code_effort_setting_wins_over_the_route(self):
        # Unset: each ticket at its route's effort. Set (file or environment): every ticket at the setting.
        tickets = [(12, "Small", "S", "low"), (13, "Large", "L", "high")]
        for level, where in ((None, None), ("low", "settings.json"), ("max", "environment"),
                             ("medium", "settings.json")):
            with self.subTest(level=level, where=where):
                self.setUp()
                self.scenario(issues=[issue(n, labels=self.labels_for(s, r), title=t) for n, t, s, r in tickets],
                              ready=[ready_row(n, t, labels=self.labels_for(s, r)) for n, t, s, r in tickets])
                env = None
                if where == "settings.json":
                    self.configure(HARNESS_CODE_EFFORT=level)
                elif where == "environment":
                    env = self.env(HARNESS_CODE_EFFORT=level)
                self.ok("dispatch", "--max", "2", env=env)
                calls = {c["argv"][c["argv"].index("-n") + 1]: c["argv"] for c in self.claude_calls()}
                records = {r["ticket"]: r for r in self.run_records()}
                for n, title, size, risk in tickets:
                    want = level or route_of("task", size, risk)[1]
                    if want:
                        self.assertEqual(calls[self.name(n, title)],
                                         self.bg_with_effort(self.name(n, title), want, f"/harness:work {n}"))
                        self.assertEqual(records[n]["effort"], want)
                    else:
                        self.assertEqual(calls[self.name(n, title)], self.bg_today(n, title))
                        self.assertNotIn("effort", records[n])

    def test_a_tier_override_replaces_the_labels_effort(self):
        # HARNESS_CODE_TIER replaces the label rubric, its effort included: a size:L ticket launches with no flag
        # unless HARNESS_CODE_EFFORT is set too. Without the override the same ticket launches at xhigh.
        large = self.labels_for("L", "high")
        for tier in (None, "opus", "sonnet", "fable"):
            with self.subTest(tier=tier):
                self.setUp()
                if tier:
                    self.configure(HARNESS_CODE_TIER=tier)
                self.scenario(issues=[issue(labels=large)], ready=[ready_row(12, labels=large)])
                self.ok("dispatch")
                [call] = self.claude_calls()
                if tier:
                    self.assertEqual(call["argv"], self.bg_today(12, TITLE))
                    self.assertNotIn("effort", self.run_records()[0])
                else:
                    self.assertEqual(call["argv"], self.bg_with_effort(self.name(12, TITLE), "xhigh",
                                                                       "/harness:work 12"))
                    self.assertEqual(self.run_records()[0]["effort"], "xhigh")

    def test_a_redispatched_size_l_ticket_gets_xhigh(self):
        self.write_run(12, id=f"{self.key}-12-20261013090000", pid=self.dead_pid(), started_at=iso_ago(hours=3),
                       ended_at=iso_ago(hours=2), outcome="error")
        large = self.labels_for("L", "low", status="status:in-progress")
        self.scenario(issues=[issue(12, labels=large)])
        p = self.ok("dispatch", env=self.env(FAKECLAUDE_BG_ID="redo58"))
        self.assertRegex(p.stdout, r"(?m)^#12 bg on t/12-\S+ \(id redo58\)$")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], self.bg_with_effort("proj #12 Add the board tool", "xhigh", "/harness:work 12"))
        again = next(r for r in self.run_records() if r.get("bg_id") == "redo58")
        self.assertEqual(again["effort"], "xhigh")


class DispatchCrossProject(RowsInProcess, CopiedProject, WorkTest):
    """#299: `harness-work dispatch --repo B` run from a session of project A (HARNESS_SESSION_PROJECT, else
    CLAUDE_PROJECT_DIR, naming A), whose CLAUDE_VERIFY_CMD and HARNESS_TEST_CMD are exported, gives the launched
    session (bg and headless) B's own values of those two from B's settings, and no value of A's where B sets none.
    From B's own session the environment passes through unchanged. Repo A is a separate git repository."""

    NAMES = ("CLAUDE_VERIFY_CMD", "HARNESS_TEST_CMD")
    A_VERIFY = "make verify-alpha-only"
    A_TEST = "npm run alpha-tests -- {files}"
    KEEP = ("HARNESS_UNRELATED_KEEP", "kept-7c1e")      # any other exported setting still reaches the session

    @classmethod
    def extend_template(cls, root, git, init):
        """Repo A: its own repository `alpha` beside proj, its settings.json committed as "Alpha"."""
        alpha = os.path.join(root, "alpha")
        init(alpha)
        write(os.path.join(alpha, ".claude", "settings.json"),
              json.dumps({"env": {"CLAUDE_VERIFY_CMD": cls.A_VERIFY, "HARNESS_TEST_CMD": cls.A_TEST}}))
        git(alpha, "add", "-A")
        git(alpha, "commit", "-q", "-m", "Alpha")

    def setUp(self):
        super().setUp()
        self.alpha = os.path.join(self.tmp, "alpha")

    def commit_settings(self, env):
        """B's settings.json, committed and pushed."""
        write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": env}))
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "Settings")
        self.git(self.repo, "push", "-q", "origin", "main")

    def launched_env(self, venue, settings, cwd=None, **exported):
        """The environment the one session launched by `dispatch --repo <B> --venue <venue>` received."""
        self.commit_settings(settings)
        self.scenario(issues=[issue()], ready=[ready_row(12)])
        p = self.work("dispatch", "--repo", self.repo, "--venue", venue,
                      env=self.env(**dict([self.KEEP], **exported)), cwd=cwd or self.repo)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(wait_for(lambda: len(self.claude_calls()) >= 1), self.claude_calls())
        [call] = self.claude_calls()
        self.assertEqual(os.path.realpath(call["cwd"]), self.worktree_path())
        for record in self.run_records():
            if record.get("pid"):
                self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        return call["env"]

    def test_a_dispatch_for_another_repository_carries_its_commands_not_the_sessions(self):
        both = {"CLAUDE_VERIFY_CMD": self.A_VERIFY, "HARNESS_TEST_CMD": self.A_TEST}
        cases = (
            ("B both, A both, HARNESS_SESSION_PROJECT",
             {"CLAUDE_VERIFY_CMD": "make verify-widgets", "HARNESS_TEST_CMD": "pytest -q {files}"},
             "HARNESS_SESSION_PROJECT", both),
            ("B test only, A both, CLAUDE_PROJECT_DIR", {"HARNESS_TEST_CMD": "go test ./..."},
             "CLAUDE_PROJECT_DIR", both),
            ("B verify only, A test only, HARNESS_SESSION_PROJECT", {"CLAUDE_VERIFY_CMD": "tox -e py39"},
             "HARNESS_SESSION_PROJECT", {"HARNESS_TEST_CMD": self.A_TEST}),
            ("B neither, A both, CLAUDE_PROJECT_DIR", {}, "CLAUDE_PROJECT_DIR", both),
            ("B neither, A verify only, HARNESS_SESSION_PROJECT", {}, "HARNESS_SESSION_PROJECT",
             {"CLAUDE_VERIFY_CMD": self.A_VERIFY}),
        )
        for venue in ("bg", "headless"):
            for label, settings, signal, exported in cases:
                with self.subTest(venue=venue, case=label):
                    self.setUp()
                    env = self.launched_env(venue, settings, cwd=self.alpha, **{signal: self.alpha}, **exported)
                    for name in self.NAMES:
                        if name in settings:
                            self.assertEqual(env.get(name), settings[name], name)
                        else:
                            self.assertFalse((env.get(name) or "").strip(), f"{name}={env.get(name)!r}")
                    flat = json.dumps(env)
                    self.assertNotIn(self.A_VERIFY, flat)
                    self.assertNotIn("alpha-tests", flat)
                    self.assertEqual(env.get(self.KEEP[0]), self.KEEP[1])

    def test_a_dispatch_from_the_repositorys_own_session_passes_the_environment_through(self):
        settings = {"CLAUDE_VERIFY_CMD": "make verify-widgets", "HARNESS_TEST_CMD": "pytest -q {files}"}
        cases = (("no session project", None, "make exported-check", "pytest -x {files}"),
                 ("HARNESS_SESSION_PROJECT is B", "HARNESS_SESSION_PROJECT", "just verify-all", "just test {files}"),
                 ("CLAUDE_PROJECT_DIR is B", "CLAUDE_PROJECT_DIR", "nox -s verify", "nox -s tests -- {files}"))
        for venue in ("bg", "headless"):
            for label, signal, verify, test in cases:
                with self.subTest(venue=venue, case=label):
                    self.setUp()
                    exported = {"CLAUDE_VERIFY_CMD": verify, "HARNESS_TEST_CMD": test}
                    if signal:
                        exported[signal] = self.repo
                    env = self.launched_env(venue, settings, **exported)
                    self.assertEqual((env.get("CLAUDE_VERIFY_CMD"), env.get("HARNESS_TEST_CMD")), (verify, test))
                    self.assertEqual(env.get(self.KEEP[0]), self.KEEP[1])


# =========================================================================== #335: a dry run stops no session
WOULD_STOP_RE = re.compile(r"would stop session ([A-Za-z0-9_-]+) \(#(\d+) [^)\n]+\)")


class DispatchDryRunStopsNothing(RowsInProcess, CopiedProject, WorkTest):
    """#335: `dispatch --dry-run` runs dispatch's sweep without its effects: a finished ticket's bg session is not
    stopped and its record not written, a `would stop session <id> (#N <state>)` line says what a real dispatch would
    do, and the plan counts the slot as freed. A real dispatch and `runs --sweep` still stop and close."""

    # Each fixture: finished {n: (bg id, agents state, labels, issue state, PRs)}, live {n: (bg id, agents state,
    # PRs)} of status:in-progress tickets, the ready tickets in board order, and --max.
    FIXTURES = (
        {"finished": {301: ("s-fin1", "blocked", IN_REVIEW, "OPEN", ready_pr(401)),
                      302: ("s-fin2", "idle", NEEDS_INPUT, "OPEN", []),
                      303: ("s-fin3", "done", IN_REVIEW, "CLOSED", MERGED)},
         "live": {304: ("s-run4", "running", [])},
         "ready": [310, 311, 312, 313, 314, 315], "max": 3},
        {"finished": {57: ("s-q57", "blocked", IN_PROGRESS, "CLOSED", MERGED)},
         "live": {58: ("s-b58", "blocked", []), 59: ("s-i59", "idle", draft(459))},
         "ready": [60, 61, 62, 63, 64], "max": 4},
        {"finished": {88: ("s-x88", "blocked", NEEDS_INPUT, "OPEN", draft(488)),
                      89: ("s-y89", "blocked", IN_REVIEW, "OPEN", ready_pr(489))},
         "live": {},
         "ready": [90, 91, 92], "max": 2},
    )

    def setup_fixture(self, fx):
        """Unended bg records for every session, the `claude agents` listing and the gh scenario. Returns env."""
        shutil.rmtree(self.runs_dir(), True)
        for path in (self.claude_log, self.gh_log):
            if os.path.exists(path):
                os.unlink(path)
        sessions = {n: (c[0], c[1]) for n, c in fx["finished"].items()}
        sessions.update({n: (c[0], c[1]) for n, c in fx["live"].items()})
        for n, (bg_id, _state) in sessions.items():
            self.write_run(n, venue="bg", bg_id=bg_id, log=None)
        listing = [{"id": bg_id, "kind": "background", "state": state, "cwd": self.repo, "name": f"proj #{n} {TITLE}"}
                   for n, (bg_id, state) in sessions.items()]
        tickets = {n: (c[2], c[3], c[4]) for n, c in fx["finished"].items()}
        tickets.update({n: (IN_PROGRESS, "OPEN", c[2]) for n, c in fx["live"].items()})
        calls = [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, labels=labels, state=state)}
                 for n, (labels, state, _prs) in tickets.items()]
        calls += [{"match": ["issue", "view", str(n)], "stdout_json": issue(n, title=f"Ready {n}")}
                  for n in fx["ready"]]
        for n, (_labels, _state, prs) in tickets.items():
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x", "--state", "all"], "stdout_json": prs})
            calls.append({"contains": ["pr", "list", "--head", f"t/{n}-x"],
                          "stdout_json": [pr for pr in prs if pr["state"] == "OPEN"]})
        calls += [
            {"match": ["repo", "view"], "stdout_json": {"nameWithOwner": "o/r"}},
            {"match": ["issue", "list"], "stdout_json": [ready_row(n, f"Ready {n}") for n in fx["ready"]]},
            {"match": ["issue", "edit"], "stdout": ""},
            {"match": ["issue", "comment"], "stdout": "https://github.com/o/r/issues/12#issuecomment-1\n"},
            {"match": ["pr", "list"], "stdout_json": []},
            {"match": ["api", "user"], "stdout": "rjeskuri\n"},
            {"match": ["api"], "stdout_json": []},
        ]
        write(self.scenario_path, json.dumps({"calls": calls}))
        state = self.scenario_path + ".state.json"
        if os.path.exists(state):
            os.unlink(state)
        return self.env(FAKECLAUDE_AGENTS=json.dumps(listing), **CREDENTIALS)

    def runs_snapshot(self):
        """{file name: bytes} of everything in the runs directory."""
        folder = self.runs_dir()
        out = {}
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), "rb") as fh:
                out[name] = fh.read()
        return out

    def stops(self):
        """{session id: times `claude stop` was called with it}, from the fake claude's argv log."""
        counts = {}
        for call in self.claude_calls():
            if call["argv"][:1] == ["stop"]:
                for arg in call["argv"][1:]:
                    if not arg.startswith("-"):
                        counts[arg] = counts.get(arg, 0) + 1
        return counts

    def bg_launches(self):
        return sorted(c["argv"][-1] for c in self.claude_calls() if "--bg" in c["argv"])

    def record_id(self, n):
        return f"{self.key}-{n}-20261013120000"

    @staticmethod
    def expected_plan(fx):
        """What today's dry run plans: the finished sessions hold no slot, the live ones do."""
        free = fx["max"] - len(fx["live"])
        return fx["ready"][:free], fx["ready"][free:], free

    # ---- DoD 1: no `claude stop`, the runs directory byte-identical, a `would stop` line per finished session
    def test_dry_run_stops_no_session_writes_no_record_and_says_what_it_would_stop(self):
        for i, fx in enumerate(self.FIXTURES):
            for as_json in (False, True):
                with self.subTest(fixture=i, json=as_json):
                    env = self.setup_fixture(fx)
                    before = self.runs_snapshot()
                    self.assertEqual(len(before), len(fx["finished"]) + len(fx["live"]))
                    args = ["dispatch", "--dry-run", "--max", str(fx["max"])] + (["--json"] if as_json else [])
                    p = self.work(*args, env=env)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 0, out)
                    self.assertEqual(self.stops(), {}, out)
                    self.assertEqual([c["argv"] for c in self.claude_calls() if c["argv"][:1] != ["agents"]], [],
                                     out)
                    self.assertEqual(self.runs_snapshot(), before, out)
                    self.assertEqual(re.findall(STOPPED_RE, out), [], out)
                    expected = sorted((c[0], str(n)) for n, c in fx["finished"].items())
                    self.assertEqual(sorted(WOULD_STOP_RE.findall(out)), expected, out)    # none for a live one
                    if as_json:
                        json.loads(p.stdout)        # the would-stop lines leave stdout whole

    def test_a_second_dry_run_sees_the_same_sessions_and_still_stops_none(self):
        fx = self.FIXTURES[0]
        env = self.setup_fixture(fx)
        before = self.runs_snapshot()
        for _ in range(2):
            p = self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)
            self.assertEqual(sorted(WOULD_STOP_RE.findall(p.stdout + p.stderr)),
                             sorted((c[0], str(n)) for n, c in fx["finished"].items()), p.stdout + p.stderr)
        self.assertEqual(self.stops(), {})
        self.assertEqual(self.runs_snapshot(), before)
        self.assertEqual(self.bg_launches(), [])

    # ---- DoD 2: the plan and slot count of today's dry run, the finished sessions' slots freed
    def test_dry_run_plans_as_if_the_finished_sessions_had_been_stopped(self):
        for i, fx in enumerate(self.FIXTURES):
            plan, over, free = self.expected_plan(fx)
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                data = self.ok_json("dispatch", "--dry-run", "--max", str(fx["max"]), "--json", env=env)
                self.assertEqual(sorted(data["live"]), sorted(self.record_id(n) for n in fx["live"]), data)
                self.assertEqual([row["number"] for row in data["plan"]], plan, data)
                self.assertEqual([(s["number"], s["why"]) for s in data["skipped"]],
                                 [(n, f"over --max {fx['max']}") for n in over], data)

                p = self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)
                self.assertIn(f"Dry run: venue bg, {len(fx['live'])} live, {free} of {fx['max']} slots free.",
                              p.stdout)
                starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
                self.assertEqual([int(re.match(r"would start #(\d+)", ln).group(1)) for ln in starts], plan,
                                 p.stdout)
                self.assertEqual(self.stops(), {})

    # ---- DoD 3: the same fixture without --dry-run stops and closes as today, and launches into the freed slots
    def test_a_real_dispatch_stops_the_finished_sessions_and_closes_their_records(self):
        for i, fx in enumerate(self.FIXTURES):
            plan, _over, _free = self.expected_plan(fx)
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                self.ok("dispatch", "--dry-run", "--max", str(fx["max"]), env=env)    # changes nothing
                self.assertEqual(self.stops(), {})
                p = self.work("dispatch", "--max", str(fx["max"]), env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                finished_ids = sorted(c[0] for c in fx["finished"].values())
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished_ids}, out)
                self.assertEqual(sorted(re.findall(STOPPED_RE, out)), finished_ids, out)
                self.assertEqual(WOULD_STOP_RE.findall(out), [], out)
                saved = {r["id"]: r for r in self.run_records()}
                for n in fx["finished"]:
                    record = saved[self.record_id(n)]
                    self.assertTrue(record["ended_at"], record)
                    self.assertEqual(record["outcome"], "ok", record)
                for n in fx["live"]:
                    self.assertIsNone(saved[self.record_id(n)]["ended_at"], saved[self.record_id(n)])
                self.assertEqual(self.bg_launches(), sorted(f"/harness:work {n}" for n in plan), out)

    # ---- outline 2: `runs --sweep` is unchanged
    def test_runs_sweep_still_stops_the_finished_sessions_and_closes_their_records(self):
        for i, fx in enumerate(self.FIXTURES):
            with self.subTest(fixture=i):
                env = self.setup_fixture(fx)
                p = self.work("runs", "--sweep", env=env)
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 0, out)
                finished_ids = sorted(c[0] for c in fx["finished"].values())
                self.assertEqual(self.stops(), {bg_id: 1 for bg_id in finished_ids}, out)
                self.assertEqual(sorted(re.findall(STOPPED_RE, out)), finished_ids, out)
                self.assertEqual(WOULD_STOP_RE.findall(out), [], out)
                saved = {r["id"]: r for r in self.run_records()}
                for n in fx["finished"]:
                    self.assertTrue(saved[self.record_id(n)]["ended_at"])
                for n in fx["live"]:
                    self.assertIsNone(saved[self.record_id(n)]["ended_at"])


# =========================================================================== dispatch --only N[,N...] (#336)
class DispatchOnly(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --only` plans only the named tickets, through the normal path: run record, start, launch."""

    TITLES = {5: "Five", 7: "Seven", 9: "Nine", 11: "Eleven"}

    def board(self, ready=(5, 7, 9), extra_issues=()):
        titles = self.TITLES
        self.scenario(issues=[issue(n, title=titles[n]) for n in ready] + list(extra_issues),
                      ready=[ready_row(n, titles[n]) for n in ready])

    def sleeper(self, ticket):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/harness:work {ticket}"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        return live.pid

    def launched_tickets(self):
        return sorted(int(c["argv"][-1].split()[1]) for c in self.claude_calls())

    def started_tickets(self):
        """Tickets whose start comment or status edit reached the board."""
        return {int(argv[2]) for argv in self.gh_calls() if argv[:2] in (["issue", "edit"], ["issue", "comment"])}

    def assertSaysWhy(self, p, n):
        text = p.stdout + p.stderr
        self.assertNotIn("unrecognized arguments", text)    # an unknown argument is not a reason
        self.assertRegex(text, rf"#{n}\b", text)

    def test_only_one_of_three_ready_tickets_launches_that_one_with_its_run_record(self):
        self.board()
        p = self.ok("dispatch", "--only", "7", env=self.env(FAKECLAUDE_BG_ID="only7", **CREDENTIALS))
        self.assertRegex(p.stdout, r"(?m)^#7 bg on t/7-\S+ \(id only7\)$")
        self.assertNotRegex(p.stdout, r"#5\b|#9\b")
        [call] = self.claude_calls()
        self.assertEqual(call["argv"], ["--bg", "-n", "proj #7 Seven", "--permission-mode", "auto",
                                        "/harness:work 7"])
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"]),
                         (7, "bg", "only7", None))
        self.assertTrue(record["id"].startswith(f"{self.key}-7-"), record["id"])
        self.assertTrue(record["branch"].startswith("t/7-"), record["branch"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(record["worktree"]))
        self.assertEqual(self.started_tickets(), {7})     # the start comment and status edit: #7 only
        self.assertTrue(os.path.isdir(record["worktree"]))

    def test_a_different_named_ticket_is_the_one_launched(self):
        self.board()
        self.ok("dispatch", "--only", "9", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [9])
        self.assertEqual([r["ticket"] for r in self.run_records()], [9])
        self.assertEqual(self.started_tickets(), {9})

    def test_only_with_a_comma_list_launches_exactly_those_tickets(self):
        self.board(ready=(5, 7, 9, 11))
        p = self.ok("dispatch", "--only", "11,5", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [5, 11])
        self.assertEqual(sorted(r["ticket"] for r in self.run_records()), [5, 11])
        self.assertEqual(self.started_tickets(), {5, 11})
        for n in (5, 11):
            self.assertRegex(p.stdout, rf"(?m)^#{n} bg on ")
        self.assertNotRegex(p.stdout, r"#7\b|#9\b")

    def test_only_a_ticket_that_is_not_ready_launches_nothing_and_says_why(self):
        proposed = issue(8, labels=("task", "status:proposed", "size:S", "risk:low"), title="Proposed")
        waiting = issue(10, labels=NEEDS_INPUT, title="Waiting")
        working = issue(13, labels=IN_PROGRESS, title="Working")        # in progress, no run record at all
        for n in (8, 10, 13, 99):                                        # 99 is not on the board
            with self.subTest(only=n):
                self.board(extra_issues=[proposed, waiting, working])
                p = self.work("dispatch", "--only", str(n), env=self.env(**CREDENTIALS))
                self.assertSaysWhy(p, n)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(self.run_records(), [])
                self.assertEqual(self.started_tickets(), set())
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_only_a_ticket_with_a_live_run_launches_nothing_and_says_why(self):
        self.write_run(13, pid=self.sleeper(13))
        self.board(extra_issues=[issue(13, labels=IN_PROGRESS, title="Working")])
        before = self.run_records()
        p = self.work("dispatch", "--only", "13", env=self.env(**CREDENTIALS))
        self.assertSaysWhy(p, 13)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), before)
        self.assertEqual(self.started_tickets(), set())

    def test_a_ready_ticket_that_is_live_is_not_launched_again_but_the_other_named_one_is(self):
        self.write_run(5, pid=self.sleeper(5))
        self.board()
        p = self.work("dispatch", "--only", "5,7", env=self.env(**CREDENTIALS))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertSaysWhy(p, 5)
        self.assertEqual(self.launched_tickets(), [7])
        self.assertEqual(self.started_tickets(), {7})

    def test_a_not_ready_name_does_not_stop_the_ready_one_beside_it(self):
        self.board(extra_issues=[issue(8, labels=("task", "status:proposed", "size:S", "risk:low"))])
        p = self.work("dispatch", "--only", "8,9", env=self.env(**CREDENTIALS))
        self.assertEqual(self.launched_tickets(), [9])
        self.assertSaysWhy(p, 8)
        self.assertEqual([r["ticket"] for r in self.run_records()], [9])
        self.assertEqual(self.started_tickets(), {9})

    def test_only_redispatches_an_in_progress_ticket_whose_run_ended_without_a_pr(self):
        for n in (13, 14):
            self.write_run(n, pid=self.dead_pid(), started_at=iso_ago(hours=3), ended_at=iso_ago(hours=2),
                           outcome="error")
        self.board(extra_issues=[issue(13, labels=IN_PROGRESS, title="Working"),
                                 issue(14, labels=IN_PROGRESS, title="Other")])
        p = self.ok("dispatch", "--only", "13", env=self.env(**CREDENTIALS))
        self.assertRegex(p.stdout, r"(?m)^#13 bg on ")
        self.assertEqual(self.launched_tickets(), [13])
        fresh = [r for r in self.run_records() if not r.get("ended_at")]
        self.assertEqual([r["ticket"] for r in fresh], [13])     # #14 ended too, but was not named
        self.assertNotIn(14, self.started_tickets())             # nothing on the board touches #14
        self.assertTrue(os.path.isdir(fresh[0]["worktree"]))
        worktrees = os.path.join(self.repo, ".claude", "worktrees")
        self.assertEqual([d for d in os.listdir(worktrees) if d.startswith("t-14-")], [])

    def test_only_with_dry_run_plans_the_named_tickets_and_starts_nothing(self):
        self.board(ready=(5, 7, 9, 11))
        p = self.ok("dispatch", "--dry-run", "--only", "7,11")
        starts = [ln for ln in p.stdout.splitlines() if ln.startswith("would start #")]
        self.assertEqual(sorted(ln.split()[2] for ln in starts), ["#11", "#7"], p.stdout)
        for n in (5, 9):
            self.assertNotRegex(p.stdout, rf"#{n}\b")
        data = self.ok_json("dispatch", "--dry-run", "--only", "7,11", "--json")
        self.assertEqual(sorted(row["number"] for row in data["plan"]), [7, 11])
        self.assertTrue(data["dry_run"])
        data = self.ok_json("dispatch", "--dry-run", "--only", "9", "--json")
        self.assertEqual([row["number"] for row in data["plan"]], [9])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.run_records(), [])
        self.assertEqual(self.started_tickets(), set())
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_only_with_dry_run_says_why_a_named_ticket_is_not_planned(self):
        self.write_run(13, pid=self.sleeper(13))
        self.board(extra_issues=[issue(8, labels=("task", "status:proposed", "size:S", "risk:low")),
                                 issue(13, labels=IN_PROGRESS, title="Working")])
        for n in (8, 13):
            with self.subTest(only=n):
                p = self.work("dispatch", "--dry-run", "--only", f"{n},5")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn(f"would start #{n}", p.stdout)
                self.assertIn("would start #5", p.stdout)
                self.assertSaysWhy(p, n)
                data = json.loads(self.work("dispatch", "--dry-run", "--only", f"{n},5", "--json").stdout)
                self.assertEqual([row["number"] for row in data["plan"]], [5])
                self.assertIn(n, [s["number"] for s in data["skipped"]])
        self.assertEqual(self.claude_calls(), [])


# =========================================================================== dispatch resumes an answered ticket (#367)
class DispatchResume(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --only N` plans a status:needs-input ticket the owner has answered (harness-board answers N), as
    `cloud` does, and confirms each launch: the session's own `harness-work start` leaves a "Started on" comment, and
    a launch that shows none within HARNESS_START_WAIT seconds (default 60) ends its run record in error. The fake
    claude (work_support) posts that comment when it is launched, unless FAKECLAUDE_NO_START is set."""

    SKIP = "not ready, and not in progress with an ended run to redispatch"
    STARTED_EARLIER = "Started on t/{n}-first-run (host, bg)" + MARK

    def needs_input(self, n, *replies, title="Resumed"):
        comments = [comment(ME, iso_ago(hours=5), self.STARTED_EARLIER.format(n=n)),
                    comment(ME, iso_ago(hours=3), QUESTION)] + list(replies)
        self.scenario(issues=[issue(n, labels=NEEDS_INPUT, title=title, comments=comments)])

    def edits(self, n):
        return [a for a in self.gh_calls() if a[:3] == ["issue", "edit", str(n)]]

    def test_only_launches_an_answered_needs_input_ticket_with_a_run_record(self):
        cases = ((12, [comment(ME, iso_ago(hours=1), "Use main.")]),
                 (40, [comment("drive-by-user", iso_ago(hours=2), "Me too"),
                       comment(ME.upper(), iso_ago(minutes=5), "The --fast one."),
                       comment(ME, iso_ago(minutes=4), "Note to self." + MARK)]))
        for n, replies in cases:
            with self.subTest(ticket=n):
                shutil.rmtree(self.runs_dir(), True)
                for path in (self.gh_log, self.claude_log):
                    if os.path.exists(path):
                        os.unlink(path)
                self.needs_input(n, *replies, title=f"Resumed {n}")
                bg_id = f"resume{n}"
                p = self.ok("dispatch", "--only", str(n), env=self.env(FAKECLAUDE_BG_ID=bg_id, **CREDENTIALS))
                self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ \(id {bg_id}\)$")
                self.assertNotIn(self.SKIP, p.stdout + p.stderr)
                [call] = self.claude_calls()
                self.assertEqual(call["argv"], ["--bg", "-n", f"proj #{n} Resumed {n}", "--permission-mode", "auto",
                                                f"/harness:work {n}"])
                [record] = self.run_records()
                self.assertEqual((record["ticket"], record["venue"], record["bg_id"], record["ended_at"],
                                  record["outcome"]), (n, "bg", bg_id, None, None))
                self.assertTrue(record["id"].startswith(f"{self.key}-{n}-"), record["id"])
                self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(record["worktree"]))
                moved = [a for a in self.edits(n) if "status:in-progress" in a and "status:needs-input" in a]
                self.assertTrue(moved, self.edits(n))
                self.assertTrue([a for a in self.gh_calls() if a[:3] == ["issue", "comment", str(n)]
                                 and "Started on" in " ".join(a)])

    def test_only_still_skips_a_needs_input_ticket_without_an_answer_since_its_question(self):
        question = comment(ME, iso_ago(hours=3), QUESTION)
        shapes = {"no reply at all": [question],
                  "only the harness's own note": [question, comment(ME, iso_ago(hours=1), "Reminder." + MARK)],
                  "a reply before the question": [comment(ME, iso_ago(hours=4), "Use main."), question],
                  "a reply by someone else": [question, comment("drive-by-user", iso_ago(hours=1), "Use main.")]}
        for shape, comments in shapes.items():
            with self.subTest(shape):
                self.scenario(issues=[issue(12, labels=NEEDS_INPUT, comments=comments)])
                p = self.ok("dispatch", "--only", "12", env=self.env(**CREDENTIALS))
                self.assertIn(f"skip #12: {self.SKIP}", p.stdout + p.stderr)
                self.assertEqual(self.claude_calls(), [])
                self.assertEqual(self.run_records(), [])
                self.assertFalse([a for a in self.gh_calls() if a[:2] in (["issue", "edit"], ["issue", "comment"])])
                self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_a_launch_whose_session_never_starts_ends_its_record_in_error_and_the_next_dispatch_retries(self):
        self.needs_input(12, comment(ME, iso_ago(hours=1), "Use main."))   # an older "Started on" is not this launch's
        began = time.monotonic()
        p = self.work("dispatch", "--only", "12", env=self.env(FAKECLAUDE_NO_START="1", HARNESS_START_WAIT="2",
                                                               FAKECLAUDE_BG_ID="never12", **CREDENTIALS))
        took = time.monotonic() - began
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        launch, stop = self.claude_calls()                               # the launch, then the stop of that session
        self.assertEqual(launch["argv"][0], "--bg")
        self.assertEqual(stop["argv"], ["stop", "never12"])
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["outcome"]), (12, "error"))
        self.assertEqual(record["bg_id"], "never12")
        self.assertTrue(record["ended_at"])
        self.assertTrue(record.get("error") and "\n" not in record["error"], record)
        self.assertIn("#12", p.stderr)
        self.assertIn(record["error"], p.stderr)                         # it says so
        self.assertGreaterEqual(took, 1.5)                                # it waited for the session before giving up
        self.assertLess(took, 30)
        # the ended record is what the next dispatch retries: a healthy session now starts and the run goes on
        self.ok("dispatch", "--only", "12", env=self.env(HARNESS_START_WAIT="10", FAKECLAUDE_BG_ID="again12",
                                                         **CREDENTIALS))
        records = self.run_records()
        self.assertEqual(len(records), 2)
        newest = max(records, key=lambda r: r["started_at"] + r["id"])
        self.assertEqual((newest["bg_id"], newest["ended_at"], newest["outcome"]), ("again12", None, None))
        calls = self.claude_calls()                                       # launch, stop, launch again: no second stop
        self.assertEqual([c["argv"][0] for c in calls], ["--bg", "stop", "--bg"])

    def test_every_launch_that_never_starts_is_stopped_after_one_shared_wait(self):
        issues = [issue(n, labels=NEEDS_INPUT, title=f"Resumed {n}", comments=[
            comment(ME, iso_ago(hours=5), self.STARTED_EARLIER.format(n=n)), comment(ME, iso_ago(hours=3), QUESTION),
            comment(ME, iso_ago(hours=1), "Use main.")]) for n in (12, 13)]
        self.scenario(issues=issues)
        wait = 3
        began = time.monotonic()
        p = self.work("dispatch", "--only", "12,13", env=self.env(FAKECLAUDE_NO_START="1", FAKECLAUDE_BG_ID="never",
                                                                  HARNESS_START_WAIT=str(wait), **CREDENTIALS))
        took = time.monotonic() - began
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreaterEqual(took, wait - 0.5)
        self.assertLess(took, 1.7 * wait)                                 # waits in turn would take about 2 * wait
        records = self.run_records()
        self.assertEqual(sorted((r["ticket"], r["outcome"]) for r in records), [(12, "error"), (13, "error")])
        self.assertTrue(all(r["ended_at"] for r in records), records)
        calls = self.claude_calls()
        self.assertEqual(sorted(c["argv"][-1] for c in calls if c["argv"][0] == "--bg"),
                         ["/harness:work 12", "/harness:work 13"])
        self.assertEqual([c["argv"] for c in calls if c["argv"][0] == "stop"], [["stop", "never"]] * 2)
        for n in (12, 13):
            self.assertIn(f"#{n}", p.stderr)

    def test_a_headless_run_that_exits_without_starting_is_left_for_the_sweep_to_classify(self):
        self.scenario(issues=[issue(12)], ready=[ready_row(12)])
        env = self.env(FAKECLAUDE_NO_START="1", FAKECLAUDE_MODE="billing", HARNESS_START_WAIT="20", **CREDENTIALS)
        began = time.monotonic()
        p = self.work("dispatch", "--venue", "headless", env=env)
        self.assertLess(time.monotonic() - began, 15)                     # it saw the exit, it did not wait it out
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertRegex(p.stderr, r"#12 not launched: the session exited before it posted")
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["venue"], record["ended_at"], record["outcome"]),
                         (12, "headless", None, None))                    # not ended by dispatch
        self.assertEqual([c["argv"][0] for c in self.claude_calls()], ["-p"])   # nothing to stop
        self.assertTrue(wait_for(lambda: pid_gone(record["pid"])))
        [swept] = self.ok_json("runs", "--sweep", "--json")
        self.assertTrue(swept["ended_at"])
        self.assertNotEqual(swept["outcome"], "ok")
        self.assertTrue(self.read_json(self.budgets_path())["api"]["exhausted_until"])   # billing marked the budget
        self.assertIn("api: exhausted", self.ok("budget").stdout)

    def test_a_session_that_starts_is_never_failed_by_the_wait(self):
        self.scenario(issues=[issue(7, title="Seven")], ready=[ready_row(7, "Seven")])
        began = time.monotonic()
        self.ok("dispatch", "--only", "7", env=self.env(HARNESS_START_WAIT="30", **CREDENTIALS))
        self.assertLess(time.monotonic() - began, 20)                     # a seen "Started on" ends the wait at once
        [record] = self.run_records()
        self.assertEqual((record["ticket"], record["ended_at"], record["outcome"]), (7, None, None))


# =========================================================================== dispatch --after N (#389)
def blocked_row(number, title, blockers=(), labels=READY):
    """A ready row as `gh issue list` gives it, its blockers all closed (harness-board ready drops any other)."""
    return {"number": number, "title": title, "labels": [{"name": n} for n in labels],
            "blockedBy": [{"number": b, "state": "CLOSED"} for b in blockers]}


class DispatchAfter(RowsInProcess, CopiedProject, WorkTest):
    """`dispatch --after N` launches the ready tickets whose blockers include N, or else the first ready ticket
    without a live run, through --only's path (slots, run records, start comments, --dry-run)."""

    # ticket: (title, its closed blockers)
    BOARD = {5: ("Five", ()), 11: ("Eleven", (10,)), 12: ("Twelve", (8, 10)), 13: ("Thirteen", (9,))}

    def board(self, numbers=(5, 11, 12, 13), high=()):
        rows, issues = [], []
        for n in numbers:
            title, blockers = self.BOARD[n]
            labels = READY + (("priority:high",) if n in high else ())
            rows.append(blocked_row(n, title, blockers, labels))
            issues.append(issue(n, labels=labels, title=title,
                                blocked_by=[{"number": b, "state": "CLOSED"} for b in blockers]))
        self.scenario(issues=issues, ready=rows)

    sleeper = DispatchOnly.sleeper
    launched_tickets = DispatchOnly.launched_tickets
    started_tickets = DispatchOnly.started_tickets

    def assertNothingStarted(self, records_before=()):
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.started_tickets(), set())
        self.assertEqual(sorted(r["id"] for r in self.run_records()), sorted(records_before))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees")))

    def test_after_launches_unblocked_tickets(self):
        # #12 waits on #8 and #10, #13 on #9, #5 on nothing: only the tickets that #N blocked are launched
        for after, unblocked, line in ((10, [11, 12], "after #10: #11, #12 unblocked"),
                                       (9, [13], "after #9: #13 unblocked")):
            with self.subTest(after=after):
                self.setUp()
                self.board()
                p = self.ok("dispatch", "--after", str(after), env=self.env(**CREDENTIALS))
                self.assertIn(line, p.stdout.splitlines())
                for n in unblocked:
                    self.assertRegex(p.stdout, rf"(?m)^#{n} bg on t/{n}-\S+ ")
                self.assertEqual(self.launched_tickets(), unblocked)
                self.assertEqual(sorted(r["ticket"] for r in self.run_records()), unblocked)
                self.assertEqual(self.started_tickets(), set(unblocked))
            with self.subTest(after=after, json=True):
                self.setUp()
                self.board()
                data = self.ok_json("dispatch", "--after", str(after), "--json", env=self.env(**CREDENTIALS))
                self.assertEqual(data["after"], {"ticket": after, "unblocked": unblocked, "next": None})
                self.assertEqual(sorted(r["ticket"] for r in data["launched"]), unblocked)
                self.assertEqual(self.launched_tickets(), unblocked)

    def test_after_falls_back_to_next_ready(self):
        # nothing on the board waited on #20: exactly one launch, the first ready ticket in ready's order not live
        for live, high, expected in (((), (), 5), ((5,), (), 11), ((), (13,), 13)):   # priority:high leads ready
            with self.subTest(live=live, high=high):
                self.setUp()
                before = [self.write_run(n, pid=self.sleeper(n))["id"] for n in live]
                self.board((5, 11, 13), high)
                p = self.ok("dispatch", "--after", "20", env=self.env(**CREDENTIALS))
                self.assertIn(f"after #20: none unblocked; next ready #{expected}", p.stdout.splitlines())
                self.assertEqual(self.launched_tickets(), [expected])
                self.assertEqual(self.started_tickets(), {expected})
                self.assertEqual([r["ticket"] for r in self.run_records() if r["id"] not in before], [expected])
            with self.subTest(live=live, high=high, json=True):
                self.setUp()
                for n in live:
                    self.write_run(n, pid=self.sleeper(n))
                self.board((5, 11, 13), high)
                data = self.ok_json("dispatch", "--after", "20", "--json", env=self.env(**CREDENTIALS))
                self.assertEqual(data["after"], {"ticket": 20, "unblocked": [], "next": expected})
                self.assertEqual([r["ticket"] for r in data["launched"]], [expected])
        with self.subTest(ready="none"):            # no ready ticket: nothing launched, exit 0
            self.setUp()
            self.board(())
            p = self.ok("dispatch", "--after", "20", env=self.env(**CREDENTIALS))
            self.assertIn("after #20: nothing ready", p.stdout.splitlines())
            self.assertNothingStarted()
            data = self.ok_json("dispatch", "--after", "20", "--json", env=self.env(**CREDENTIALS))
            self.assertEqual(data["after"], {"ticket": 20, "unblocked": [], "next": None})
            self.assertEqual(data["launched"], [])
            self.assertNothingStarted()

    def test_after_respects_slots_and_dry_run(self):
        # --max filled by live runs on other tickets: nothing launched, and it says so, unblocked or not
        for after in (10, 20):
            with self.subTest(after=after, live=2, max=2):
                self.setUp()
                before = [self.write_run(n, pid=self.sleeper(n))["id"] for n in (40, 41)]
                self.board()
                p = self.ok("dispatch", "--after", str(after), "--max", "2", env=self.env(**CREDENTIALS))
                self.assertIn("--max 2 reached", p.stdout)
                self.assertNothingStarted(before)
        with self.subTest(after=10, live=1, max=2):         # one free slot: the first unblocked ticket only
            self.setUp()
            self.write_run(40, pid=self.sleeper(40))
            self.board()
            self.ok("dispatch", "--after", "10", "--max", "2", env=self.env(**CREDENTIALS))
            self.assertEqual(self.launched_tickets(), [11])
        # --dry-run plans what a real run would launch and launches nothing
        for after, plan, unblocked, nxt in ((10, [11, 12], [11, 12], None), (20, [5], [], 5)):
            with self.subTest(after=after, dry_run=True):
                self.setUp()
                self.board()
                p = self.ok("dispatch", "--after", str(after), "--dry-run")
                starts = sorted(int(ln.split()[2][1:]) for ln in p.stdout.splitlines()
                                if ln.startswith("would start #"))
                self.assertEqual(starts, plan, p.stdout)
                data = self.ok_json("dispatch", "--after", str(after), "--dry-run", "--json")
                self.assertTrue(data["dry_run"])
                self.assertEqual(sorted(row["number"] for row in data["plan"]), plan)
                self.assertEqual(data["after"], {"ticket": after, "unblocked": unblocked, "next": nxt})
                self.assertNothingStarted()

    def test_after_with_only_is_an_error(self):
        self.board()
        p = self.ok("dispatch", "--after", "10", "--dry-run")           # the control: --after alone is accepted
        self.assertIn("would start #11", p.stdout)
        for flags in (("--after", "10", "--only", "11"), ("--only", "11", "--after", "10")):
            for extra in ((), ("--dry-run",)):
                with self.subTest(flags=flags, extra=extra):
                    p = self.work("dispatch", *flags, *extra, env=self.env(**CREDENTIALS))
                    self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertNotIn("unrecognized", p.stderr)
                    self.assertIn("--after", p.stderr)
                    self.assertIn("--only", p.stderr)
                    self.assertNotIn("would start", p.stdout)
                    self.assertNothingStarted()


if __name__ == "__main__":
    unittest.main()
