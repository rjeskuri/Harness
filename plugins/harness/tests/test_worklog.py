"""Tests for harness-worklog counting interactive sessions started in ticket worktrees (ticket #26).

    python3 -m unittest discover -s plugins/harness/tests -p "test_worklog.py" -v

An interactive session started inside <repo>/.claude/worktrees/t-N-<slug> keeps its transcript under
~/.claude/projects/<that path, every non-alphanumeric as '-'>, not under the main checkout's folder.
harness-worklog must read those folders too, by listing ~/.claude/projects (a removed worktree still has its
transcripts), and must count a session id found twice once. Sibling repos and their worktrees stay out.

The tool runs as a subprocess in its default mode (no --transcripts), with HOME in a temporary folder, so the
real ~/.claude/projects is never read. Expected blocks and hours are worked out by hand from the fixture times.
The one in-process test imports the tool by path, for what a subprocess cannot show: identical events merge
into one block, so a transcript counted twice gives the same hours as once and only the event list differs.
"""
from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

sys.dont_write_bytecode = True

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
UTC = dt.timezone.utc
GIT = shutil.which("git")


def load_tool(name):
    loader = SourceFileLoader(name.replace("-", "_"), os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def encode(path):
    """Claude Code's session folder name for a path: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class WorktreeSessions(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-worklog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.repo = self.mk("client-repo")
        self.wl = load_tool("harness-worklog")
        self.tz, name, _ = self.wl.system_zone()
        now = dt.datetime.now(UTC)
        self.today = now.astimezone(self.tz).date()
        self.day = self.today - dt.timedelta(days=2)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        self.env.update(HOME=self.home, GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1")

    # ---------------------------------------------------------------- helpers
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def projects(self, path):
        """The folder Claude Code keeps the sessions of the project at `path` in (the fake HOME's)."""
        return os.path.join(self.home, ".claude", "projects", encode(path))

    def worktree(self, name, repo=None):
        return os.path.join(repo or self.repo, ".claude", "worktrees", name)

    def at(self, hhmm):
        """Clock time `hhmm` on the fixture day, on the Mac's clock."""
        hour, minute = (int(x) for x in hhmm.split(":"))
        return dt.datetime.combine(self.day, dt.time(hour, minute), self.tz)

    def session(self, folder, sid, start, end, extra=()):
        """<folder>/<sid>.jsonl: the user's message at `start`, the agent's replies until `end`. `extra`
        are further clock times of agent replies, so the block is one run (no gap over 30 minutes)."""
        a, b = self.at(start), self.at(end)
        entries = [("user", a, "work on it")]
        entries += [("assistant", self.at(t), None) for t in extra]
        entries.append(("assistant", b, None))
        lines = []
        for kind, when, text in entries:
            entry = {"type": kind, "sessionId": sid, "timestamp": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            if text is not None:
                entry["message"] = {"content": text}
            lines.append(json.dumps(entry))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + ["not json"]) + "\n")
        return path

    def run_wl(self, *args, repo=None):
        return subprocess.run([sys.executable, os.path.join(BIN, "harness-worklog"), "--repo", repo or self.repo, *args],
                              cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=60)

    def start(self):
        p = self.run_wl("init", "--since", (self.today - dt.timedelta(days=20)).isoformat())
        self.assertEqual(p.returncode, 0, p.stderr)

    def rows(self):
        with open(os.path.join(self.repo, "worklog", "worklog.csv"), encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def update(self):
        self.start()
        p = self.run_wl("update")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def day_row(self):
        return next((r for r in self.rows() if r["date"] == self.day.isoformat()), None)

    # ------------------------------------------------------------------ tests
    def test_sessions_in_ticket_worktrees_count(self):
        self.session(self.projects(self.repo), "main-1111", "10:00", "10:45", extra=["10:20"])
        self.session(self.projects(self.worktree("t-26-harness-worklog")), "wt-2222", "14:00", "14:30", extra=["14:15"])
        # a worktree that was removed since (nothing at its path, only its transcripts) still counts
        self.session(self.projects(self.worktree("t-31-gone")), "gone-3333", "16:00", "16:15")
        # a worktree whose name is not a ticket's (any name under .claude/worktrees is this repo's)
        self.session(self.projects(self.worktree("scratch")), "scratch-1212", "18:00", "18:30", extra=["18:15"])
        p = self.update()
        row = self.day_row()
        self.assertIsNotNone(row, p.stdout)
        # 45 + 30 + 15 + 30 minutes = 2.0 h; leaving out any one of them gives 1.25, 1.5 or 1.75
        self.assertEqual((row["blocks"], row["hours"], row["source"]),
                         ("10:00-10:45, 14:00-14:30, 16:00-16:15, 18:00-18:30", "2.00", "auto"))
        self.assertIn(f"{self.day}   2.00 h   10:00-10:45, 14:00-14:30, 16:00-16:15, 18:00-18:30", p.stdout)

    def test_a_session_only_in_a_worktree_counts_when_the_main_checkout_has_none(self):
        self.session(self.projects(self.worktree("t-7-only-here")), "solo-4444", "09:00", "09:40", extra=["09:20"])
        self.assertFalse(os.path.exists(self.projects(self.repo)))
        p = self.update()
        row = self.day_row()
        self.assertIsNotNone(row, p.stdout)
        self.assertEqual((row["blocks"], row["hours"]), ("09:00-09:40", "0.75"))   # 40 min -> 0.75

    def test_worktree_sessions_on_another_day_make_their_own_row(self):
        other = self.day - dt.timedelta(days=1)
        self.session(self.projects(self.repo), "main-1111", "10:00", "10:30")
        folder = self.projects(self.worktree("t-9-yesterday"))
        stamp = lambda hh, mm: dt.datetime.combine(other, dt.time(hh, mm), self.tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        os.makedirs(folder)
        with open(os.path.join(folder, "prev-5555.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": "prev-5555", "timestamp": stamp(11, 0),
                                 "message": {"content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "sessionId": "prev-5555", "timestamp": stamp(11, 30)}) + "\n")
            fh.write(json.dumps({"type": "assistant", "sessionId": "prev-5555", "timestamp": stamp(12, 0)}) + "\n")
        self.update()
        by_day = {r["date"]: r for r in self.rows()}
        got = lambda d, k: by_day.get(d.isoformat(), {}).get(k)
        self.assertEqual((got(self.day, "hours"), got(other, "hours"), got(other, "blocks")),
                         ("0.50", "1.00", "11:00-12:00"))

    def test_describe_finds_a_day_worked_only_in_a_worktree(self):
        self.session(self.projects(self.worktree("t-12-describe")), "wt-6666", "13:00", "13:50", extra=["13:25"])
        self.start()
        p = self.run_wl("describe", "--date", self.day.isoformat(), "--text", "Wrote the thing")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0.25 h", p.stdout)
        row = self.day_row()
        self.assertEqual((row["blocks"], row["description"], row["source"]), ("13:00-13:50", "Wrote the thing", "auto"))

    def test_zone_sees_a_worktree_session_when_checking_a_switch(self):
        self.session(self.projects(self.worktree("t-14-zone")), "wt-7777", "10:00", "10:20")
        self.start()
        inside = self.at("10:10").astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        p = self.run_wl("zone", "--since", inside, "--zone", "+09:00")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("falls inside a session", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_a_duplicated_transcript_counts_once(self):
        main_dir = self.projects(self.repo)
        wt_dir = self.projects(self.worktree("t-26-harness-worklog"))
        original = self.session(main_dir, "same-8888", "10:00", "10:30", extra=["10:15"])       # 3 entries
        os.makedirs(wt_dir)
        copy = os.path.join(wt_dir, "same-8888.jsonl")                                            # the copy
        shutil.copy(original, copy)
        # a copy under another file name: the session id inside the entries is the key, not the name
        renamed = os.path.join(wt_dir, "copied-by-hand.jsonl")
        shutil.copy(original, renamed)
        # a different session whose entries happen to be identical (other id): it is not a duplicate
        twin = self.session(wt_dir, "twin-9999", "10:00", "10:30", extra=["10:15"])
        # a session that really is only in the worktree
        self.session(wt_dir, "late-0000", "15:00", "15:30", extra=["15:15"])
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            dirs = self.wl.transcripts_dirs(self.repo)
            paths = sorted(p for d in dirs for p in self._jsonl(d))
            self.assertEqual(len(paths), 5)                         # main, copy, renamed copy, twin, late
            evts = self.wl.events(paths)
        # same-8888 once (3), twin-9999 (3), late-0000 (3): 9 events, not 12
        self.assertEqual(len(evts), 9)
        self.assertEqual(sum(1 for _, human in evts if human), 3)
        for order in ([original, copy], [copy, original], [renamed, original], [original, renamed],
                      [renamed, copy], [copy, renamed, original]):          # whichever copy is read first
            with self.subTest(order=[os.path.relpath(p, self.tmp) for p in order]):
                self.assertEqual(len(self.wl.events(order)), 3)
        # end to end: the duplicate adds nothing, the twin and the late session are all there
        p = self.update()
        row = self.day_row()
        self.assertEqual((row["blocks"], row["hours"]), ("10:00-10:30, 15:00-15:30", "1.00"))
        self.assertTrue(os.path.exists(twin))

    def entries_file(self, folder, name, sid, entries):
        """<folder>/<name>.jsonl holding `entries`, each (type, hh:mm on the fixture day, user text or None)."""
        lines = []
        for kind, hhmm, text in entries:
            entry = {"type": kind, "sessionId": sid,
                     "timestamp": self.at(hhmm).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            if text is not None:
                entry["message"] = {"content": text}
            lines.append(json.dumps(entry))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + ["not json"]) + "\n")
        return path

    def test_a_resumed_session_in_two_files_keeps_every_entry_once(self):
        # one session in two files, one longer (resumed later): the shorter file's entries are a prefix of it
        first = [("user", "10:00", "start"), ("assistant", "10:15", None), ("assistant", "10:30", None)]
        later = [("assistant", "10:50", None), ("user", "11:10", "go on"), ("assistant", "11:20", None)]
        short = self.entries_file(self.projects(self.repo), "res-1111", "res-1111", first)
        long_ = self.entries_file(self.projects(self.worktree("t-26-harness-worklog")), "res-1111", "res-1111",
                                  first + later)
        union = sorted((self.at(t).astimezone(UTC), kind == "user") for kind, t, _ in first + later)
        for order in ([short, long_], [long_, short]):                 # whichever file is read first
            with self.subTest(order=[os.path.relpath(p, self.tmp) for p in order]):
                self.assertEqual(sorted(self.wl.events(order)), union)   # 6 events: none lost, none twice
        # with another session read between the two copies
        other = self.entries_file(self.mk("elsewhere"), "solo-2222", "solo-2222",
                                  [("user", "13:00", "x"), ("assistant", "13:20", None)])
        expected = sorted(union + [(self.at("13:00").astimezone(UTC), True), (self.at("13:20").astimezone(UTC), False)])
        for order in ([short, other, long_], [long_, other, short], [other, long_, short]):
            with self.subTest(order=[os.path.relpath(p, self.tmp) for p in order]):
                self.assertEqual(sorted(self.wl.events(order)), expected)

    def test_resumed_sessions_count_in_full_end_to_end(self):
        # two sessions, each split across the main checkout and a worktree with the longer copy on opposite
        # sides: skipping whichever copy comes second loses the tail of one of them, whatever the read order
        main_dir = self.projects(self.repo)
        wt_dir = self.projects(self.worktree("feature-x"))
        p_first = [("user", "10:00", "start"), ("assistant", "10:15", None), ("assistant", "10:30", None)]
        p_later = [("assistant", "10:50", None), ("user", "11:10", "go on"), ("assistant", "11:20", None)]
        q_first = [("user", "14:00", "start"), ("assistant", "14:15", None), ("assistant", "14:30", None)]
        q_later = [("assistant", "14:50", None), ("user", "15:10", "go on"), ("assistant", "15:20", None)]
        self.entries_file(main_dir, "p-3333", "p-3333", p_first)
        self.entries_file(wt_dir, "p-3333", "p-3333", p_first + p_later)
        self.entries_file(main_dir, "q-4444", "q-4444", q_first + q_later)
        self.entries_file(wt_dir, "q-4444", "q-4444", q_first)
        self.update()
        row = self.day_row()
        # 80 + 80 minutes = 2.75 h; with a tail lost one block ends at :30 and the day is 1.75 h
        self.assertEqual((row["blocks"], row["hours"]), ("10:00-11:20, 14:00-15:20", "2.75"))

    def _jsonl(self, folder):
        return [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.endswith(".jsonl")]

    def test_transcripts_dirs_lists_this_repos_worktree_folders_only(self):
        main_dir = self.projects(self.repo)
        wt1 = self.projects(self.worktree("t-1-one"))
        wt2 = self.projects(self.worktree("t-2-two"))
        wt3 = self.projects(self.worktree("scratch"))           # worktree names need not start with t-
        wt4 = self.projects(self.worktree("feature-x"))
        for folder, sid in ((main_dir, "a"), (wt1, "b"), (wt2, "c"), (wt3, "d"), (wt4, "e")):
            self.session(folder, sid, "10:00", "10:10")
        outside = [
            self.projects(os.path.join(self.tmp, "client-repo-other")),                              # a sibling repo
            self.projects(self.worktree("t-3-theirs", os.path.join(self.tmp, "client-repo-other"))),   # its worktree
            self.projects(os.path.join(self.tmp, "elsewhere", "client-repo")),                       # same name, other path
            self.projects(self.worktree("t-4-theirs", os.path.join(self.tmp, "elsewhere", "client-repo"))),
            self.projects(os.path.join(self.repo, "sub")),                                           # a folder inside it
            self.projects(os.path.join(self.repo, ".claude", "other", "t-5-x")),                     # not a worktrees folder
            self.projects(os.path.join(self.repo, ".claude", "worktrees")),                          # no worktree name
        ]
        for folder in outside:
            self.session(folder, "x", "10:00", "10:10")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            dirs = self.wl.transcripts_dirs(self.repo)
        self.assertCountEqual([os.path.normpath(d) for d in dirs], [main_dir, wt1, wt2, wt3, wt4])

    def test_another_repos_worktree_is_not_read(self):
        self.session(self.projects(self.repo), "main-1111", "10:00", "10:30")
        self.session(self.projects(self.worktree("t-26-mine")), "mine-2222", "11:10", "11:40")   # 40 min after the first: a new block
        sibling = self.mk("client-repo-other")                       # its encoding starts with this repo's
        elsewhere = os.path.join(self.mk("elsewhere"), "client-repo")
        os.makedirs(elsewhere)
        for n, (path, start) in enumerate([
                (sibling, "12:00"),                                              # the sibling's main checkout
                (self.worktree("t-1-x", sibling), "13:00"),                      # the sibling's worktree
                (elsewhere, "14:00"),                                            # same name, other parent
                (self.worktree("t-2-y", elsewhere), "15:00"),
                (os.path.join(self.repo, "sub"), "16:00"),                       # a project inside this one
                (os.path.join(self.repo, ".claude", "other", "t-3-z"), "17:00"),
                (os.path.join(self.repo, ".claude", "worktrees"), "18:00")]):
            hh, mm = start.split(":")
            self.session(self.projects(path), f"out-{n}", start, f"{hh}:{int(mm) + 20}")
        self.update()
        row = self.day_row()
        # only this repo's two sessions: 30 + 30 minutes = 1.00 h (the others would add blocks and hours)
        self.assertEqual((row["blocks"], row["hours"]), ("10:00-10:30, 11:10-11:40", "1.00"))
        # and the sibling is its own repo: its worktree session is theirs, with its own log
        self.assertEqual(self.run_wl("init", "--since", (self.today - dt.timedelta(days=20)).isoformat(),
                                     repo=sibling).returncode, 0)
        p = self.run_wl("update", repo=sibling)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(sibling, "worklog", "worklog.csv"), encoding="utf-8", newline="") as fh:
            theirs = next(r for r in csv.DictReader(fh) if r["date"] == self.day.isoformat())
        self.assertEqual((theirs["blocks"], theirs["hours"]), ("12:00-12:20, 13:00-13:20", "0.75"))   # 40 min

    def test_an_explicit_transcripts_folder_is_read_alone(self):
        self.session(self.projects(self.worktree("t-26-wt")), "wt-2222", "14:00", "14:30")
        folder = self.mk("custom")
        self.session(folder, "custom-1", "10:00", "10:30")
        self.start()
        p = self.run_wl("update", "--transcripts", folder)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.day_row()["blocks"], self.day_row()["hours"]), ("10:00-10:30", "0.50"))


if __name__ == "__main__":
    unittest.main()
