"""Tests for `harness-red verify` (ticket #254): the full verify at HEAD, run in the background, waited in slices.

    python3 -m unittest plugins/harness/tests/test_red_verify.py -v

Every case builds, with real git under a temporary folder, a bare "origin", a clone with `main`, and the branch
t/254-verify in a linked worktree; HOME is temporary. The tool is driven as a subprocess. The verify commands
append one line per start and per finish to a counter file outside the worktree (inside it the tree would be
dirty), so "ran nothing" and "ran again" are told apart by the file, never by the tool's own word. Each line
carries the shell's pid, so tearDown can stop whatever a failing implementation leaves running, besides the
process group the record names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
TOOL = os.path.join(BIN, "harness-red")
GIT = shutil.which("git")
BRANCH, SLUG = "t/254-verify", "t-254-verify"
UNSET = "no verify command (CLAUDE_VERIFY_CMD unset); nothing run"
RUNNING_RE = re.compile(r"verify running for \d+ s; run harness-red verify --wait again")
RUNNING_FOR = re.compile(r"verify running for (\d+) s")
JSON_KEYS = {"state", "head", "cmd", "exit", "log", "started_at", "finished_at", "tail"}
DEADLINE = 60.0                     # generous: polling stops as soon as the condition holds


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def stamp(seconds_ago=0.0):
    """A `started_at` as the harness writes them (harness-work's iso): UTC, whole seconds, a Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def parent_of(pid):
    p = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    text = p.stdout.strip()
    return int(text) if p.returncode == 0 and text.isdigit() else None


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


class Verify(unittest.TestCase):

    # ------------------------------------------------------------ fixture
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-red-verify-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.stop_everything)          # runs before the rmtree (cleanups are LIFO)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        fakebin = os.path.join(self.tmp, "fakebin")
        python3 = write(os.path.join(fakebin, "python3"), f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(python3, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "GIT_"))
               and k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_VERIFY_CMD", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")}
        env.update(HOME=self.home, PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost")
        self.env = env
        self.counter = os.path.join(self.tmp, "counter.txt")
        self.pids = set()
        self.repo, self.wt = self.new_repo()
        self.state = os.path.join(self.home, ".claude", "harness", "repos",
                                  hashlib.sha1(os.path.realpath(self.repo).encode("utf-8")).hexdigest()[:12])
        self.record_path = os.path.join(self.state, "verify", SLUG + ".json")
        self.log_path = os.path.join(self.state, "verify", SLUG + ".log")

    def stop_everything(self):
        """Kill every process group a record named and every shell the counter saw; never our own group."""
        verify_dir = os.path.join(self.state, "verify") if hasattr(self, "state") else None
        if verify_dir and os.path.isdir(verify_dir):
            for name in os.listdir(verify_dir):
                if name.endswith(".json"):
                    try:
                        pid = json.loads(read(os.path.join(verify_dir, name))).get("pid")
                    except (ValueError, AttributeError):
                        pid = None
                    if isinstance(pid, int) and pid > 0:
                        self.pids.add(pid)
        for line in read(self.counter).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                self.pids.add(int(parts[1]))
        mine = os.getpgrp()
        for pid in self.pids:
            for target in (pid, self.group_of(pid)):
                if target and target != mine and target > 1:
                    try:
                        os.killpg(target, signal.SIGKILL)
                    except OSError:
                        pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def group_of(pid):
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", cwd, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                              env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def new_repo(self):
        root = os.path.join(self.tmp, "case")
        origin, repo = os.path.join(root, "origin.git"), os.path.join(root, "repo")
        os.makedirs(root)
        self.git(root, "init", "--bare", "-q", origin)
        self.git(root, "clone", "-q", origin, repo)
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        write(os.path.join(repo, "README.md"), "# project\n")
        write(os.path.join(repo, "src", "app.py"), "VALUE = 1\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "main")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        with open(os.path.join(repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write(".claude/settings.local.json\n")
        wt = os.path.join(root, "wt")
        self.git(repo, "worktree", "add", "-q", wt, "-b", BRANCH, "origin/main")
        return repo, wt

    def commit(self, rel, text):
        write(os.path.join(self.wt, rel), text)
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", f"change {rel}")
        return self.head()

    def head(self):
        return self.git(self.wt, "rev-parse", "HEAD")

    # ------------------------------------------------------------ commands
    def counted(self, tag, body, sleep=0):
        """A verify command: `start <pid> <tag> <head>` to the counter, then `body`, after `sleep` seconds,
        then `end <pid> <tag> <head>`, then the body's exit."""
        c = shlex.quote(self.counter)
        return (f'h=$(git rev-parse HEAD); echo "start $$ {tag} $h" >> {c}; '
                + (f"sleep {sleep}; " if sleep else "")
                + f'( {body} ); rc=$?; echo "end $$ {tag} $h" >> {c}; exit $rc')

    def late(self, tag, seconds):
        """A body fragment that sleeps inside the command's subshell, then writes `late <pid> <tag> <head>`: a stop
        that kills only the outer shell's pid, not the process group, leaves this line to appear."""
        return f'sleep {seconds}; echo "late $$ {tag} $h" >> {shlex.quote(self.counter)}'

    def settled_record(self, what, deadline=10.0):
        """Read the record file directly (no tool call) until it leaves `running`; the background writes it."""
        box = {}

        def done():
            try:
                with open(self.record_path, encoding="utf-8") as fh:
                    box["rec"] = json.load(fh)
            except (OSError, ValueError):
                return False
            if isinstance(box["rec"].get("pid"), int):
                self.pids.add(box["rec"]["pid"])
            return box["rec"].get("state") != "running"
        stop = time.monotonic() + deadline
        while time.monotonic() < stop and not done():
            time.sleep(0.1)
        self.assertIn("rec", box, f"{what}: no record at {self.record_path}")
        self.assertNotEqual(box["rec"].get("state"), "running",
                            f"{what}: the record still says running {deadline:.0f} s after the run ended, with no "
                            f"harness-red call to finish it: {box['rec']}")
        return box["rec"]

    def lines(self, kind=None):
        out = [ln.split() for ln in read(self.counter).splitlines() if ln.strip()]
        return [ln for ln in out if kind is None or ln[0] == kind]

    def starts(self):
        return self.lines("start")

    def ends(self):
        return self.lines("end")

    # ------------------------------------------------------------ the tool
    def verify(self, *args, cmd=None, **extra):
        env = dict(self.env, **extra)
        if cmd is not None:
            env["CLAUDE_VERIFY_CMD"] = cmd
        p = subprocess.run([sys.executable, TOOL, "verify", *args], cwd=self.wt, env=env, capture_output=True,
                           text=True, timeout=180)
        self.assertNotIn("Traceback", p.stderr, p.stdout + p.stderr)
        self.note_record()
        return p

    def verify_json(self, *args, **kw):
        p = self.verify("--json", *args, **kw)
        try:
            data = json.loads(p.stdout)
        except ValueError:
            self.fail(f"--json printed no JSON (exit {p.returncode}): {p.stdout!r} {p.stderr!r}")
        return p, data

    def record(self):
        text = read(self.record_path)
        self.assertTrue(text, f"no record at {self.record_path}")
        data = json.loads(text)
        if isinstance(data.get("pid"), int):
            self.pids.add(data["pid"])
        return data

    def note_record(self):
        try:
            pid = json.loads(read(self.record_path)).get("pid")
        except (ValueError, AttributeError):
            return
        if isinstance(pid, int) and pid > 0:
            self.pids.add(pid)

    def wait_for(self, what, condition, deadline=DEADLINE, gave_up=None):
        """Poll `condition`; fail at the deadline, or at once when `gave_up()` returns a reason."""
        stop = time.monotonic() + deadline
        while time.monotonic() < stop:
            if condition():
                return
            reason = gave_up() if gave_up else None
            if reason:
                self.fail(f"{what}: {reason}")
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}; counter: {read(self.counter)!r}")

    def sleep_until(self, moment):
        while time.monotonic() < moment:
            time.sleep(0.1)

    def out(self, p):
        return p.stdout + p.stderr

    def hand_record(self, cmd, pid, started_at):
        """A record that says running, for this HEAD and `cmd`, written by hand as a crashed run would leave it."""
        write(self.record_path, json.dumps({
            "branch": BRANCH, "head": self.head(), "cmd": cmd, "state": "running", "pid": pid,
            "started_at": started_at, "finished_at": None, "exit": None, "log": self.log_path}))

    def kill_run(self, shell_pid):
        """SIGKILL a run whole, as a crash or a reboot would: the command's shell, every ancestor of it short of
        launchd and of this test (the supervisor, however many layers), the record's pid, each one's process group,
        and any process whose command line names this case's folder. Then wait for them to be gone."""
        mine, pid = set(), os.getpid()
        while pid and pid > 1 and pid not in mine:
            mine.add(pid)
            pid = parent_of(pid)
        victims, pid = set(), shell_pid
        while pid and pid > 1 and pid not in mine and pid not in victims:
            victims.add(pid)
            pid = parent_of(pid)
        try:
            rec_pid = json.loads(read(self.record_path)).get("pid")
        except (ValueError, AttributeError):
            rec_pid = None
        if isinstance(rec_pid, int) and rec_pid > 1 and rec_pid not in mine:
            victims.add(rec_pid)
        listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        for line in listing.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and self.tmp in parts[1] and int(parts[0]) not in mine:
                victims.add(int(parts[0]))
        groups = {self.group_of(v) for v in victims} - {None, os.getpgrp()}
        for g in groups:
            if g > 1:
                try:
                    os.killpg(g, signal.SIGKILL)
                except OSError:
                    pass
        for v in victims:
            try:
                os.kill(v, signal.SIGKILL)
            except OSError:
                pass
        self.pids.update(victims)

        def gone():
            for v in victims:
                try:
                    os.kill(v, 0)
                except ProcessLookupError:
                    continue
                except OSError:
                    pass
                return False
            return True
        stop = time.monotonic() + 10
        while time.monotonic() < stop and not gone():
            time.sleep(0.1)
        return victims

    # ------------------------------------------------------------ tests
    def test_pass_fail_and_reuse_at_the_same_head(self):
        # A passing command, waited: exit 0, the record says passed at HEAD, the log holds the output.
        good = self.counted("good", 'echo "ok from $(pwd) bytecode=$PYTHONDONTWRITEBYTECODE"', sleep=1)
        head = self.head()
        p = self.verify("--wait", "--timeout", "60", cmd=good)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual(len(self.starts()), 1, read(self.counter))
        self.assertEqual(len(self.ends()), 1, read(self.counter))
        self.assertEqual(self.starts()[0][3], head)
        rec = self.record()
        self.assertEqual(rec["state"], "passed")
        self.assertEqual(rec["head"], head)
        self.assertEqual(rec["cmd"], good)
        self.assertEqual(rec["branch"], BRANCH)
        self.assertEqual(rec["exit"], 0)
        self.assertEqual(os.path.realpath(rec["log"]), os.path.realpath(self.log_path))
        self.assertTrue(rec["started_at"] and rec["finished_at"], rec)
        log = read(self.log_path)
        self.assertIn(f"ok from {self.wt} bytecode=1", log)     # from the worktree root, without bytecode

        # The same HEAD and command again, waited or not: runs nothing, exit 0.
        for args in (("--wait", "--timeout", "60"), ()):
            p = self.verify(*args, cmd=good)
            self.assertEqual(p.returncode, 0, self.out(p))
            self.assertEqual(len(self.starts()), 1, f"{args}: ran again: {read(self.counter)}")
            self.assertEqual(self.record()["state"], "passed")

        # A failing command at the same HEAD: exit 1 with the last 60 log lines, line21..line80: line30 is there
        # and line15 is not (room for a few lines of the tool's own).
        bad = self.counted("bad", "i=1; while [ $i -le 80 ]; do echo line$i; i=$((i+1)); done; exit 3")
        p = self.verify("--wait", "--timeout", "60", cmd=bad)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertIn("line80", self.out(p))
        self.assertIn("line30", self.out(p))
        self.assertIsNone(re.search(r"\bline15\b", self.out(p)), self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["good", "bad"])
        rec = self.record()
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["exit"], 3)
        self.assertEqual(rec["cmd"], bad)
        self.assertEqual(rec["head"], head)

        # Asked again: the failed result is reported, nothing runs.
        p = self.verify("--wait", "--timeout", "60", cmd=bad)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertIn("line80", self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["good", "bad"])

        # --rerun starts a fresh run of the same command.
        p = self.verify("--wait", "--timeout", "60", "--rerun", cmd=bad)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["good", "bad", "bad"])
        self.assertEqual(self.record()["state"], "failed")

    def test_wait_slices_report_running_then_the_result(self):
        cmd = self.counted("slow", "echo slow-done", sleep=7)
        began = time.monotonic()
        p = self.verify("--wait", "--timeout", "1", cmd=cmd)
        self.assertEqual(p.returncode, 8, self.out(p))
        self.assertRegex(self.out(p), RUNNING_RE)
        self.assertEqual(len(self.starts()), 1, read(self.counter))
        self.assertEqual(self.ends(), [], "the call waited for the whole run instead of its slice")
        self.assertEqual(self.record()["state"], "running")

        # Without --wait: the state, at once, still 8; nothing new starts.
        p = self.verify(cmd=cmd)
        self.assertEqual(p.returncode, 8, self.out(p))
        self.assertRegex(self.out(p), RUNNING_RE)
        self.assertEqual(self.ends(), [])

        # A one-second slice on a run that began at least 4 s earlier: the seconds count from the run's started_at,
        # not from this call's start (which would say 0 or 1).
        self.sleep_until(began + 4)
        p = self.verify("--wait", "--timeout", "1", cmd=cmd)
        self.assertEqual(p.returncode, 8, self.out(p))
        m = RUNNING_FOR.search(self.out(p))
        self.assertTrue(m, self.out(p))
        self.assertGreaterEqual(int(m.group(1)), 3, f"counted from the call, not the run: {self.out(p)}")
        self.assertEqual(self.ends(), [])

        # A later --wait attaches and returns the result; still one run.
        p = self.verify("--wait", "--timeout", "60", cmd=cmd)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual(len(self.starts()), 1, read(self.counter))
        self.assertEqual(len(self.ends()), 1, read(self.counter))
        self.assertEqual(self.record()["state"], "passed")
        self.assertIn("slow-done", read(self.log_path))

        # The same shape for a failing command: 8, then 1 with the tail.
        bad = self.counted("slowbad", "echo slow-boom; exit 2", sleep=3)
        p = self.verify("--wait", "--timeout", "1", cmd=bad)
        self.assertEqual(p.returncode, 8, self.out(p))
        p = self.verify("--wait", "--timeout", "60", cmd=bad)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertIn("slow-boom", self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["slow", "slowbad"])

        # Nobody waits: started without --wait and never asked again, the run itself writes its result. Any reader
        # of the record (status, a stop hook) sees passed or failed, not running forever.
        head = self.head()
        for tag, body, state, code in (("bgok", "echo bg-ok", "passed", 0),
                                       ("bgbad", "echo bg-boom; exit 5", "failed", 5)):
            cmd = self.counted(tag, body, sleep=1)
            p = self.verify(cmd=cmd)
            self.assertEqual(p.returncode, 8, self.out(p))
            self.wait_for(f"the {tag} run to end", lambda: tag in [e[2] for e in self.ends()])
            rec = self.settled_record(tag)
            self.assertEqual(rec["state"], state, rec)
            self.assertEqual(rec["exit"], code, rec)
            self.assertEqual(rec["cmd"], cmd)
            self.assertEqual(rec["head"], head)
            self.assertTrue(rec["finished_at"], rec)
        self.assertEqual([s[2] for s in self.starts()], ["slow", "slowbad", "bgok", "bgbad"])

        # A record that says running is not taken on trust (Ruling: a running record whose process is gone, or whose
        # pid's start time does not match started_at, is dead, and the call starts a fresh run). Each one is for this
        # HEAD and command, so only a liveness check tells it from a live run; trusting it would wait out the slice
        # and say 8, forever.
        stale = self.counted("stale", "echo fresh-ok")
        exited = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        exited.wait()
        recycled = subprocess.Popen(["/bin/sleep", "120"], start_new_session=True)   # an unrelated live process
        self.addCleanup(lambda: (recycled.poll() is None and recycled.kill(), recycled.wait()))
        cases = [("exited", exited.pid, stamp(2)),          # the pid is gone; the start is recent
                 ("recycled", recycled.pid, stamp(3600))]   # the pid lives, but started an hour after the run
        for n, (name, pid, started_at) in enumerate(cases, 1):
            with self.subTest(name):
                self.hand_record(stale, pid, started_at)
                p = self.verify("--wait", "--timeout", "20", cmd=stale)
                self.assertNotEqual(p.returncode, 8, f"{name}: trusted a dead record: {self.out(p)}")
                self.assertEqual(p.returncode, 0, self.out(p))
                self.assertEqual([s[2] for s in self.starts()].count("stale"), n, read(self.counter))
                rec = self.record()
                self.assertEqual(rec["state"], "passed", rec)
                self.assertEqual(rec["cmd"], stale)
                self.assertEqual(rec["head"], head)
        self.assertIsNone(recycled.poll(), "the unrelated process behind a recycled pid was stopped")

    def test_a_new_commit_starts_a_fresh_run_and_stops_the_old_one(self):
        # The long part sleeps inside the subshell, so stopping only the outer shell's pid is caught by `late`.
        cmd = self.counted("run", self.late("run", 3) + "; echo done")
        old = self.head()
        began = time.monotonic()
        p = self.verify(cmd=cmd)                       # starts in the background
        self.assertEqual(p.returncode, 8, self.out(p))
        self.wait_for("the first run to start", lambda: len(self.starts()) == 1)

        new = self.commit("src/app.py", "VALUE = 2\n")
        p = self.verify("--wait", "--timeout", "60", cmd=cmd)
        self.assertEqual(p.returncode, 0, self.out(p))
        rec = self.record()
        self.assertEqual(rec["state"], "passed")
        self.assertEqual(rec["head"], new)
        self.assertEqual([s[3] for s in self.starts()], [old, new])
        self.assertIn(new, [e[3] for e in self.ends()])

        # Well after the old run would have finished, it never did: it was stopped.
        self.sleep_until(began + 3 + 3)
        self.assertNotIn(old, [e[3] for e in self.ends()], read(self.counter))
        self.assertNotIn(old, [ln[3] for ln in self.lines("late")],
                         f"the old run's subshell kept going (its process group was not stopped): {read(self.counter)}")

        # A finished run for the old HEAD is not reused for the new one either.
        third = self.commit("README.md", "# project, again\n")
        p = self.verify("--wait", "--timeout", "60", cmd=cmd)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[3] for s in self.starts()], [old, new, third])
        self.assertEqual(self.record()["head"], third)

    def test_a_changed_command_starts_a_fresh_run_and_stops_the_old_one(self):
        first = self.counted("first", self.late("first", 3) + "; echo first")     # long part inside the subshell
        second = self.counted("second", "echo second")
        began = time.monotonic()
        p = self.verify(cmd=first)
        self.assertEqual(p.returncode, 8, self.out(p))
        self.wait_for("the first run to start", lambda: len(self.starts()) == 1)
        p = self.verify("--wait", "--timeout", "60", cmd=second)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["first", "second"])
        self.assertEqual(self.record()["cmd"], second)
        self.sleep_until(began + 3 + 3)
        self.assertEqual([e[2] for e in self.ends()], ["second"], read(self.counter))
        self.assertEqual(self.lines("late"), [],
                         f"the old run's subshell kept going (its process group was not stopped): {read(self.counter)}")

    def test_verify_timeout_stops_and_records_timeout(self):
        cmd = self.counted("hang", "echo begun; sleep 5; echo never")
        began = time.monotonic()
        p = self.verify("--wait", "--timeout", "60", cmd=cmd, HARNESS_VERIFY_TIMEOUT="2")
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertIn("begun", self.out(p))                       # the log's tail
        rec = self.record()
        self.assertEqual(rec["state"], "timeout")
        self.assertEqual(rec["head"], self.head())
        # Stopped, not merely abandoned: the rest of the command never runs.
        self.sleep_until(began + 5 + 3)
        self.assertEqual(self.ends(), [], read(self.counter))
        self.assertNotIn("never", read(self.log_path))
        # A timeout is a finished result: asked again, reported (exit 1), not rerun.
        p = self.verify("--wait", "--timeout", "60", cmd=cmd, HARNESS_VERIFY_TIMEOUT="2")
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertEqual(len(self.starts()), 1, read(self.counter))
        self.assertEqual(self.record()["state"], "timeout")

        # The deadline belongs to the run, not to a waiting caller: started without --wait and never asked again,
        # it is still stopped at HARNESS_VERIFY_TIMEOUT and the record says timeout.
        unwatched = self.counted("unwatched", "echo begun-again; sleep 5; echo never-again")
        began = time.monotonic()
        p = self.verify(cmd=unwatched, HARNESS_VERIFY_TIMEOUT="2")
        self.assertEqual(p.returncode, 8, self.out(p))
        self.wait_for("the unwatched run to start", lambda: "unwatched" in [s[2] for s in self.starts()])
        self.sleep_until(began + 5 + 3)
        self.assertNotIn("unwatched", [e[2] for e in self.ends()], f"ran past its deadline: {read(self.counter)}")
        self.assertNotIn("never-again", read(self.log_path))
        rec = self.settled_record("unwatched")
        self.assertEqual(rec["state"], "timeout", rec)
        self.assertEqual(rec["cmd"], unwatched)
        self.assertTrue(rec["finished_at"], rec)

        # HARNESS_VERIFY_TIMEOUT is read like the other settings: here from the committed .claude/settings.json,
        # with nothing in the environment. Under the 3600 s default this command would pass at 5 s.
        configured_head = self.commit(".claude/settings.json", json.dumps({"env": {"HARNESS_VERIFY_TIMEOUT": "2"}}))
        configured = self.counted("configured", "echo begun-configured; sleep 5; echo never-configured")
        p = self.verify("--wait", "--timeout", "60", cmd=configured)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertIn("begun-configured", self.out(p))
        rec = self.record()
        self.assertEqual(rec["state"], "timeout", rec)
        self.assertEqual(rec["head"], configured_head)
        self.assertNotIn("configured", [e[2] for e in self.ends()], read(self.counter))

    def test_unset_command_runs_nothing(self):
        # Nothing in the environment, no settings: the message, exit 0, state none.
        for args in ((), ("--wait", "--timeout", "5")):
            p = self.verify(*args)
            self.assertEqual(p.returncode, 0, self.out(p))
            self.assertIn(UNSET, p.stdout)
        p, data = self.verify_json()
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual(data["state"], "none")
        # A blank value is unset too; and it is checked before the tree: a dirty tree still exits 0.
        write(os.path.join(self.wt, "untracked.txt"), "x\n")
        p = self.verify("--wait", "--timeout", "5", cmd="   ")
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertIn(UNSET, p.stdout)
        self.assertFalse(os.path.exists(self.counter))
        self.assertFalse(os.path.exists(self.log_path))

    def test_command_comes_from_the_environment_then_settings(self):
        # settings.json (committed), then settings.local.json over it, then the environment over both.
        tracked = self.counted("tracked", "echo tracked")
        local = self.counted("local", "echo local")
        env_cmd = self.counted("env", "echo env")
        self.commit(".claude/settings.json", json.dumps({"env": {"CLAUDE_VERIFY_CMD": tracked}}))
        p = self.verify("--wait", "--timeout", "60")
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["tracked"])
        write(os.path.join(self.wt, ".claude", "settings.local.json"),
              json.dumps({"env": {"CLAUDE_VERIFY_CMD": local}}))
        p = self.verify("--wait", "--timeout", "60")
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["tracked", "local"])
        self.assertEqual(self.record()["cmd"], local)
        p = self.verify("--wait", "--timeout", "60", cmd=env_cmd)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["tracked", "local", "env"])
        self.assertEqual(self.record()["cmd"], env_cmd)

    # ------------------------------------------------------------ another project's session (ticket #205)
    def verify_from(self, cwd, *args, **extra):
        """harness-red verify run in `cwd` (args may carry --worktree), with `extra` over the environment."""
        p = subprocess.run([sys.executable, TOOL, "verify", *args], cwd=cwd, env=dict(self.env, **extra),
                           capture_output=True, text=True, timeout=180)
        self.assertNotIn("Traceback", p.stderr, p.stdout + p.stderr)
        self.note_record()
        return p

    def other_projects(self):
        """A repository whose settings name a verify command of their own, and a folder outside any repository."""
        okinawa = os.path.join(self.tmp, "OkinawaDream")
        os.makedirs(okinawa)
        self.git(okinawa, "init", "-q")
        write(os.path.join(okinawa, ".claude", "settings.json"),
              json.dumps({"env": {"CLAUDE_VERIFY_CMD": self.counted("okinawa-settings", "echo okinawa")}}))
        plain = os.path.join(self.tmp, "notes")
        os.makedirs(plain)
        return [okinawa, plain]

    def test_another_projects_verify_cmd_in_the_environment_does_not_reach_this_worktree(self):
        # CLAUDE_VERIFY_CMD from a session of another project: this worktree has none, so nothing runs.
        foreign = self.counted("foreign", "echo foreign")
        projects = self.other_projects()
        for project in projects:
            for cwd, args in ((project, ("--worktree", self.wt)), (self.wt, ())):
                with self.subTest(project=project, cwd=cwd):
                    p = self.verify_from(cwd, *args, "--wait", "--timeout", "5", CLAUDE_PROJECT_DIR=project,
                                         CLAUDE_VERIFY_CMD=foreign)
                    self.assertEqual(p.returncode, 0, self.out(p))
                    self.assertIn(UNSET, p.stdout)
                    self.assertEqual(self.starts(), [], read(self.counter))
                    self.assertFalse(os.path.exists(self.record_path), "a run was recorded")
        # With a command of its own (committed settings.json), that one runs, not the other project's.
        own = self.counted("own", "echo own")
        self.commit(".claude/settings.json", json.dumps({"env": {"CLAUDE_VERIFY_CMD": own}}))
        p = self.verify_from(projects[0], "--worktree", self.wt, "--wait", "--timeout", "60",
                             CLAUDE_PROJECT_DIR=projects[0], CLAUDE_VERIFY_CMD=foreign)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["own"], read(self.counter))
        self.assertEqual(self.record()["cmd"], own)

    def test_the_environment_verify_cmd_still_wins_in_its_own_projects_session(self):
        # As today: from a session of this repository (its main checkout, this worktree, or none named), the
        # environment's command wins over the worktree's settings.
        tracked = self.counted("tracked", "true")
        self.commit(".claude/settings.json", json.dumps({"env": {"CLAUDE_VERIFY_CMD": tracked}}))
        tags = []
        for tag, project in (("env-unset", None), ("env-main", self.repo), ("env-worktree", self.wt)):
            with self.subTest(tag):
                extra = {"CLAUDE_VERIFY_CMD": self.counted(tag, f"echo {tag}")}
                if project is not None:
                    extra["CLAUDE_PROJECT_DIR"] = project
                p = self.verify_from(self.wt, "--worktree", self.wt, "--wait", "--timeout", "60", **extra)
                self.assertEqual(p.returncode, 0, self.out(p))
                tags.append(tag)
                self.assertEqual([s[2] for s in self.starts()], tags, read(self.counter))
                self.assertEqual(self.record()["cmd"], extra["CLAUDE_VERIFY_CMD"])

    # A session's Bash calls never see CLAUDE_PROJECT_DIR (Claude Code sets it for hooks only); they see the
    # HARNESS_SESSION_PROJECT the harness's SessionStart hook exported through CLAUDE_ENV_FILE.
    def test_another_projects_verify_cmd_does_not_reach_this_worktree_from_a_sessions_bash(self):
        foreign = self.counted("foreign", "echo foreign")
        projects = self.other_projects()
        cases = [(project, None) for project in projects] + [(projects[0], self.repo)]
        for project, claude_dir in cases:
            for cwd, args in ((project, ("--worktree", self.wt)), (self.wt, ())):
                with self.subTest(project=project, claude_dir=claude_dir, cwd=cwd):
                    extra = {"HARNESS_SESSION_PROJECT": project, "CLAUDE_VERIFY_CMD": foreign}
                    if claude_dir is not None:
                        extra["CLAUDE_PROJECT_DIR"] = claude_dir
                    p = self.verify_from(cwd, *args, "--wait", "--timeout", "5", **extra)
                    self.assertEqual(p.returncode, 0, self.out(p))
                    self.assertIn(UNSET, p.stdout)
                    self.assertEqual(self.starts(), [], read(self.counter))
                    self.assertFalse(os.path.exists(self.record_path), "a run was recorded")
        # With a command of its own (committed settings.json), that one runs, not the other project's.
        own = self.counted("own", "echo own")
        self.commit(".claude/settings.json", json.dumps({"env": {"CLAUDE_VERIFY_CMD": own}}))
        p = self.verify_from(projects[0], "--worktree", self.wt, "--wait", "--timeout", "60",
                             HARNESS_SESSION_PROJECT=projects[0], CLAUDE_VERIFY_CMD=foreign)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()], ["own"], read(self.counter))
        self.assertEqual(self.record()["cmd"], own)

    def test_the_environment_verify_cmd_still_wins_when_the_session_is_this_repositorys(self):
        # HARNESS_SESSION_PROJECT naming this repository (its main checkout or this worktree) keeps today's order,
        # whatever CLAUDE_PROJECT_DIR says: the environment's command wins over the worktree's settings.
        tracked = self.counted("tracked", "true")
        self.commit(".claude/settings.json", json.dumps({"env": {"CLAUDE_VERIFY_CMD": tracked}}))
        okinawa = self.other_projects()[0]
        tags = []
        for tag, session, claude_dir in (("session-main", self.repo, None), ("session-worktree", self.wt, None),
                                         ("session-main-over-foreign", self.repo, okinawa),
                                         ("session-worktree-over-foreign", self.wt, okinawa)):
            with self.subTest(tag):
                extra = {"CLAUDE_VERIFY_CMD": self.counted(tag, f"echo {tag}"), "HARNESS_SESSION_PROJECT": session}
                if claude_dir is not None:
                    extra["CLAUDE_PROJECT_DIR"] = claude_dir
                p = self.verify_from(self.wt, "--worktree", self.wt, "--wait", "--timeout", "60", **extra)
                self.assertEqual(p.returncode, 0, self.out(p))
                tags.append(tag)
                self.assertEqual([s[2] for s in self.starts()], tags, read(self.counter))
                self.assertEqual(self.record()["cmd"], extra["CLAUDE_VERIFY_CMD"])

    def test_dirty_tree_is_refused(self):
        cmd = self.counted("dirty", "echo ran")
        cases = [
            ("untracked", lambda: write(os.path.join(self.wt, "new_file.py"), "x = 1\n")),
            ("modified", lambda: write(os.path.join(self.wt, "src", "app.py"), "VALUE = 99\n")),
            ("staged", lambda: (write(os.path.join(self.wt, "README.md"), "# staged\n"),
                                self.git(self.wt, "add", "README.md"))),
        ]
        for name, dirty in cases:
            with self.subTest(name):
                dirty()
                for args in ((), ("--wait", "--timeout", "5")):
                    p = self.verify(*args, cmd=cmd)
                    self.assertEqual(p.returncode, 1, self.out(p))
                    self.assertIn(f"uncommitted changes in {self.wt}; commit them first", self.out(p))
                    self.assertFalse(os.path.exists(self.counter), f"{name}: ran on a dirty tree")
                    self.assertFalse(os.path.exists(self.record_path), f"{name}: recorded a run")
                self.git(self.wt, "reset", "-q", "--hard")
                self.git(self.wt, "clean", "-q", "-fd")
        # Clean again: it runs.
        p = self.verify("--wait", "--timeout", "60", cmd=cmd)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual(len(self.starts()), 1)

    def test_the_run_survives_the_call_that_started_it(self):
        # Killed by the signal that unwinds harness-red (SIGTERM), then by one that cannot be caught (SIGKILL),
        # each sent to the whole process group of the call, as a Bash tool timeout would.
        for sig, tag in ((signal.SIGTERM, "term"), (signal.SIGKILL, "kill")):
            with self.subTest(tag):
                if tag == "kill":
                    self.commit("src/app.py", "VALUE = 3\n")
                cmd = self.counted(tag, f"echo survived-{tag}", sleep=3)
                env = dict(self.env, CLAUDE_VERIFY_CMD=cmd)
                call_out = os.path.join(self.tmp, f"call-{tag}.out")
                with open(call_out, "w") as out:
                    call = subprocess.Popen([sys.executable, TOOL, "verify", "--wait", "--timeout", "60"],
                                            cwd=self.wt, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        self.wait_for(f"the {tag} run to start",
                                      lambda: [s[2] for s in self.starts()].count(tag) == 1,
                                      gave_up=lambda: call.poll() is not None and [s[2] for s in self.starts()]
                                      .count(tag) == 0 and f"the call exited {call.returncode} before the run "
                                      f"started: {read(call_out)!r}")
                        self.note_record()
                        os.killpg(call.pid, sig)
                        call.wait(timeout=30)
                    finally:
                        if call.poll() is None:
                            os.killpg(call.pid, signal.SIGKILL)
                            call.wait(timeout=30)
                self.wait_for(f"the {tag} run to finish on its own",
                              lambda: [e[2] for e in self.ends()].count(tag) == 1)
                p = self.verify("--wait", "--timeout", "60", cmd=cmd)
                self.assertEqual(p.returncode, 0, self.out(p))
                self.assertEqual([s[2] for s in self.starts()].count(tag), 1, read(self.counter))
                self.assertEqual(self.record()["state"], "passed")
                self.assertIn(f"survived-{tag}", read(self.log_path))

        # Killing the run itself, supervisor and all, leaves a record that says running with nobody behind it. The
        # next --wait sees it is dead (Ruling: a running record whose process is gone, or whose pid's start time does
        # not match started_at, is dead) and starts a fresh run, rather than reporting 8 forever.
        victim = self.counted("victim", self.late("victim", 3) + "; echo victim-ok")
        p = self.verify(cmd=victim)
        self.assertEqual(p.returncode, 8, self.out(p))
        self.wait_for("the victim run to start", lambda: "victim" in [s[2] for s in self.starts()])
        self.note_record()
        shell = int([s for s in self.starts() if s[2] == "victim"][0][1])
        self.kill_run(shell)
        p = self.verify("--wait", "--timeout", "20", cmd=victim)
        self.assertNotEqual(p.returncode, 8, f"trusted the record of a killed run: {self.out(p)}")
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual([s[2] for s in self.starts()].count("victim"), 2, read(self.counter))
        self.assertEqual([e[2] for e in self.ends()].count("victim"), 1, read(self.counter))
        rec = self.record()
        self.assertEqual(rec["state"], "passed", rec)
        self.assertEqual(rec["cmd"], victim)

    def test_json_shape(self):
        head = self.head()
        slow = self.counted("slow", "echo json-ok", sleep=3)
        # Running: exit 8, the shape, state running, no exit or finish yet.
        p, data = self.verify_json(cmd=slow)
        self.assertEqual(p.returncode, 8, self.out(p))
        self.assertEqual(set(data), JSON_KEYS)
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["head"], head)
        self.assertEqual(data["cmd"], slow)
        self.assertIsNone(data["exit"])
        self.assertIsNone(data["finished_at"])
        self.assertTrue(data["started_at"])
        self.assertEqual(os.path.realpath(data["log"]), os.path.realpath(self.log_path))

        # Passed: exit 0, the tail holds the output.
        p, data = self.verify_json("--wait", "--timeout", "60", cmd=slow)
        self.assertEqual(p.returncode, 0, self.out(p))
        self.assertEqual(set(data), JSON_KEYS)
        self.assertEqual(data["state"], "passed")
        self.assertEqual(data["exit"], 0)
        self.assertEqual(data["head"], head)
        self.assertTrue(data["finished_at"])
        self.assertIn("json-ok", data["tail"])

        # Failed: exit 1, the record's exit status, the last 60 lines.
        bad = self.counted("bad", "i=1; while [ $i -le 70 ]; do echo row$i; i=$((i+1)); done; exit 4")
        p, data = self.verify_json("--wait", "--timeout", "60", cmd=bad)
        self.assertEqual(p.returncode, 1, self.out(p))
        self.assertEqual(set(data), JSON_KEYS)
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["exit"], 4)
        self.assertEqual(data["cmd"], bad)
        self.assertIn("row70", data["tail"])
        self.assertIn("row20", data["tail"])
        self.assertIsNone(re.search(r"\brow5\b", data["tail"]), data["tail"])
        self.assertLessEqual(len([ln for ln in data["tail"].splitlines() if ln.strip()]), 60)


if __name__ == "__main__":
    unittest.main()
