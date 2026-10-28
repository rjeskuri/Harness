"""Tests for `harness-work budget calls [--hours H] [--json]` (ticket #188, epic #185): the report of the gh call
log that #187 writes to ~/.claude/harness/gh/calls-<UTC date>.jsonl.

The contract these tests fix:
- The window is the last H hours (H > 0, default 24) of the real clock: since = until - H hours, both
  `%Y-%m-%dT%H:%M:%SZ`; lines with `at` before since, lines that are not a JSON object with a readable `at`, and
  files that are not day files count for nothing (ghlimit.read_calls).
- --json (after `calls` or after `budget`) prints exactly the keys since, until, hours, total, graphql, rest,
  refused, failed, waited_seconds, peak_minute, by_tool, by_command, by_ticket and by_hour. failed: exit not 0
  (null included) and not refused. waited_seconds: the sum of waited_ms / 1000. peak_minute:
  {"minute": "<YYYY-MM-DDTHH:MM>:00Z", "calls"} of the busiest UTC minute, null with no lines. by_tool, by_command
  and by_ticket: calls descending, then by name (tool, cmd) or by repo and number (9 before 130); by_ticket only
  lines with a ticket; by_hour ascending. by_command in JSON holds every command; only the text table stops at 15.
- Text: line 1 is `gh calls in the last <H> h: <total> (graphql <g>, rest <r>), <n> refused, <n> failed, <s> s
  waiting; peak <n> in one minute (<YYYY-MM-DDTHH:MM>Z)`, then the TOOL, COMMAND (top 15) and TICKET tables
  (harness_lib.cli.table: left-aligned, two spaces apart, the last column unpadded). The ticket's example (12.5 s
  waited) prints `12 s waiting`. With no line in the window: `No gh calls logged in the last <H> h.`, exit 0.
- --hours 0, a negative value or a non-number (nan included): one line on stderr naming --hours, exit 1.
- No gh call (FAKEGH_LOG stays empty, no line is added to the call log); budgets.json is not touched.
- The harness-work usage text and design.md's harness-work section name `budget calls`.

harness-work has no clock override, so the log lines are seeded relative to the real clock, anchored on the
current UTC hour (a line at `hour - 2 h + 1 min`), which keeps every bucket deterministic. Expected values are
written out here, never computed by the code under test. TZ is set far from UTC so a local-time slip shows.
Each test has a temporary HOME; the fake gh (fakegh.py behind HARNESS_GH and on PATH) is written once per module.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_budget_calls.py" -v
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
TOOL = os.path.join(PLUGIN, "bin", "harness-work")
FAKEGH = os.path.join(HERE, "fakegh.py")
DESIGN = os.path.join(os.path.dirname(os.path.dirname(PLUGIN)), "docs", "design.md")
UTC = dt.timezone.utc
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
KEYS = {"since", "until", "hours", "total", "graphql", "rest", "refused", "failed", "waited_seconds",
        "peak_minute", "by_tool", "by_command", "by_ticket", "by_hour"}
ACTIONS = "api repos/:owner/:repo/actions/runs"

BINS: dict = {}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-budget-calls-bin-"))
    fakebin = os.path.join(root, "fakebin")
    executable(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    BINS.update(root=root, fakebin=fakebin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


def stamp(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_of(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def short_minute(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def hour_of(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00Z")


def this_hour():
    """The current UTC hour, on the hour."""
    return dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def call(at, tool, cmd, kind, repo="Harness", ticket=None, exit=0, ms=120, waited_ms=0, refused=False):
    """One log line as #187 writes it."""
    return {"at": stamp(at), "tool": tool, "cmd": cmd, "kind": kind, "repo": repo, "ticket": ticket,
            "exit": exit, "ms": ms, "waited_ms": waited_ms, "refused": refused}


class CallsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-budget-calls-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "elsewhere")       # not a repository: budget needs none
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.scenario_path = write(os.path.join(self.tmp, "scenario.json"),
                                   json.dumps({"calls": [], "default": {"stdout": "", "exit": 0}}))

    # ----------------------------------------------------------- helpers
    def gh_dir(self):
        return os.path.join(self.home, ".claude", "harness", "gh")

    def budgets_path(self):
        return os.path.join(self.home, ".claude", "harness", "budgets.json")

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_", "FAKECLAUDE_")) and k != "PWD"}
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["fakebin"], "/usr/bin", "/bin"]),
                   HARNESS_GH=os.path.join(BINS["fakebin"], "gh"), FAKEGH_SCENARIO=self.scenario_path,
                   FAKEGH_LOG=self.gh_log, TZ="Pacific/Kiritimati")
        return env

    def seed(self, lines):
        """Append each line (a dict, or raw text for a bad line given as (day, text)) to its UTC day file."""
        for line in lines:
            if isinstance(line, tuple):
                day, text = line
            else:
                day, text = line["at"][:10], json.dumps(line)
            path = os.path.join(self.gh_dir(), f"calls-{day}.jsonl")
            os.makedirs(self.gh_dir(), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")

    def day_files(self):
        if not os.path.isdir(self.gh_dir()):
            return []
        return sorted(n for n in os.listdir(self.gh_dir()) if n.startswith("calls-") and n.endswith(".jsonl"))

    def day_file_texts(self):
        out = {}
        for name in self.day_files():
            with open(os.path.join(self.gh_dir(), name), encoding="utf-8") as fh:
                out[name] = fh.read()
        return out

    def work(self, *args):
        p = subprocess.run([sys.executable, TOOL] + list(args), cwd=self.cwd, env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def ok(self, *args):
        p = self.work(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def report(self, *args):
        """`budget calls <args> --json`, checked for shape and a window that ends now."""
        before = dt.datetime.now(UTC).replace(microsecond=0)
        data = json.loads(self.ok("budget", "calls", *args, "--json").stdout)
        after = dt.datetime.now(UTC)
        self.assertIsInstance(data, dict)
        self.assertEqual(set(data), KEYS, data)
        for key in ("since", "until"):
            self.assertRegex(data[key], STAMP)
        until = dt.datetime.strptime(data["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        since = dt.datetime.strptime(data["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertTrue(before <= until <= after, (before, data["until"], after))
        self.assertIsInstance(data["hours"], float)
        self.assertEqual(until - since, dt.timedelta(hours=data["hours"]), data)
        return data

    def gh_calls(self):
        try:
            with open(self.gh_log, encoding="utf-8") as fh:
                return [line for line in fh if line.strip()]
        except OSError:
            return []


# =========================================================================== the JSON report
class Report(CallsTest):
    def test_counts_by_tool_command_kind_ticket_and_hour(self):
        h = this_hour()
        a0 = h - dt.timedelta(hours=30)      # always another UTC day than b0 (28 h apart), inside 48 h
        b0 = h - dt.timedelta(hours=2)
        b1 = h - dt.timedelta(hours=1)
        m = lambda base, minutes, seconds=0: base + dt.timedelta(minutes=minutes, seconds=seconds)  # noqa: E731
        older_day = [
            call(m(a0, 0, 30), "harness-work", ACTIONS, "rest", repo="jotform"),
            call(m(a0, 5, 10), "harness-pr", "pr view", "graphql", ticket=130),
            call(m(a0, 5, 40), "harness-pr", "pr view", "graphql", ticket=130),
            call(m(a0, 20), "harness-board", "issue view", "graphql", ticket=9),
        ]
        newer_day = [                      # the later hour first: the log is not promised in time order
            call(m(b1, 5), "harness-work", ACTIONS, "rest", repo="jotform"),
            call(m(b1, 10), "harness-work", "run view", "rest", ticket=9),
            call(m(b0, 1, 0), "harness-pr", "pr view", "graphql", ticket=130),
            call(m(b0, 1, 15), "harness-pr", "run list", "rest"),
            call(m(b0, 1, 45), "harness-board", "issue view", "graphql", ticket=9),
            call(m(b0, 30, 0), "harness-board", "issue list", "graphql", repo="jotform", ticket=4),
            call(m(b0, 30, 20), "session_brief.py", "pr list", "graphql"),
        ]
        self.seed(older_day + newer_day)
        self.assertEqual(len(self.day_files()), 2, self.day_files())

        data = self.report("--hours", "48")
        self.assertEqual(data["hours"], 48.0)
        self.assertEqual((data["total"], data["graphql"], data["rest"]), (11, 7, 4))
        self.assertEqual((data["refused"], data["failed"]), (0, 0))
        self.assertEqual(data["waited_seconds"], 0)
        self.assertEqual(data["by_tool"], [
            {"tool": "harness-pr", "calls": 4, "graphql": 3, "rest": 1, "refused": 0},
            {"tool": "harness-board", "calls": 3, "graphql": 3, "rest": 0, "refused": 0},
            {"tool": "harness-work", "calls": 3, "graphql": 0, "rest": 3, "refused": 0},
            {"tool": "session_brief.py", "calls": 1, "graphql": 1, "rest": 0, "refused": 0},
        ])
        self.assertEqual(data["by_command"], [
            {"cmd": "pr view", "kind": "graphql", "calls": 3},
            {"cmd": ACTIONS, "kind": "rest", "calls": 2},
            {"cmd": "issue view", "kind": "graphql", "calls": 2},
            {"cmd": "issue list", "kind": "graphql", "calls": 1},
            {"cmd": "pr list", "kind": "graphql", "calls": 1},
            {"cmd": "run list", "kind": "rest", "calls": 1},
            {"cmd": "run view", "kind": "rest", "calls": 1},
        ])
        self.assertEqual(data["by_ticket"], [
            {"repo": "Harness", "ticket": 9, "calls": 3, "graphql": 2},
            {"repo": "Harness", "ticket": 130, "calls": 3, "graphql": 3},
            {"repo": "jotform", "ticket": 4, "calls": 1, "graphql": 1},
        ])
        self.assertEqual(data["by_hour"], [
            {"hour": hour_of(a0), "calls": 4, "graphql": 3, "rest": 1},
            {"hour": hour_of(b0), "calls": 5, "graphql": 4, "rest": 1},
            {"hour": hour_of(b1), "calls": 2, "graphql": 0, "rest": 2},
        ])
        self.assertEqual(data["peak_minute"], {"minute": minute_of(m(b0, 1)), "calls": 3})

        # --json given before the action works the same way, and 24 h drops the older day's four calls.
        day = json.loads(self.ok("budget", "--json", "calls").stdout)
        self.assertEqual(day["hours"], 24.0)
        self.assertEqual((day["total"], day["graphql"], day["rest"]), (7, 4, 3))
        self.assertEqual([r["hour"] for r in day["by_hour"]], [hour_of(b0), hour_of(b1)])
        self.assertEqual(day["by_tool"], [
            {"tool": "harness-board", "calls": 2, "graphql": 2, "rest": 0, "refused": 0},
            {"tool": "harness-pr", "calls": 2, "graphql": 1, "rest": 1, "refused": 0},
            {"tool": "harness-work", "calls": 2, "graphql": 0, "rest": 2, "refused": 0},
            {"tool": "session_brief.py", "calls": 1, "graphql": 1, "rest": 0, "refused": 0},
        ])

    def test_window_excludes_older_lines_and_skips_bad_lines(self):
        h = this_hour()
        b0 = h - dt.timedelta(hours=2)                 # inside a 3 h window whatever the minute now
        old = h - dt.timedelta(hours=3, minutes=10)    # always before now - 3 h
        today, b0_day = stamp(h)[:10], stamp(b0)[:10]
        five_days = h - dt.timedelta(days=5)
        lines = [
            call(old, "harness-pr", "pr view", "graphql", ticket=130),
            call(five_days, "harness-pr", "pr view", "graphql", ticket=130),
            call(h - dt.timedelta(hours=26), "harness-board", "issue view", "graphql", ticket=9),
            call(b0 + dt.timedelta(minutes=10), "harness-pr", "pr view", "graphql", ticket=130),
            (b0_day, "this is not json"),
            (b0_day, ""),
            (b0_day, "[1, 2, 3]"),
            (b0_day, '"a string"'),
            (b0_day, json.dumps({"tool": "harness-pr", "cmd": "pr view", "kind": "graphql", "ticket": 130})),
            (b0_day, json.dumps({"at": "yesterday", "tool": "harness-pr", "cmd": "pr view", "kind": "graphql"})),
            (b0_day, json.dumps({"at": 12345, "tool": "harness-pr", "cmd": "pr view", "kind": "graphql"})),
            (b0_day, '{"at": "' + stamp(b0 + dt.timedelta(minutes=11)) + '", "tool": "harness-pr", "cmd'),
            call(b0 + dt.timedelta(minutes=40), "harness-board", "issue view", "graphql", ticket=9),
            call(b0 + dt.timedelta(minutes=80), "harness-work", "run list", "rest"),
            (today, "{not json either"),
        ]
        self.seed(lines)
        write(os.path.join(self.gh_dir(), "notes.txt"), json.dumps(call(b0, "harness-pr", "pr view", "graphql")) + "\n")
        write(os.path.join(self.gh_dir(), "calls-garbage.jsonl"),
              json.dumps(call(b0, "harness-pr", "pr view", "graphql")) + "\n")

        data = self.report("--hours", "3")
        self.assertEqual(data["hours"], 3.0)
        self.assertEqual((data["total"], data["graphql"], data["rest"]), (3, 2, 1))
        self.assertEqual(data["by_tool"], [
            {"tool": "harness-board", "calls": 1, "graphql": 1, "rest": 0, "refused": 0},
            {"tool": "harness-pr", "calls": 1, "graphql": 1, "rest": 0, "refused": 0},
            {"tool": "harness-work", "calls": 1, "graphql": 0, "rest": 1, "refused": 0},
        ])
        self.assertEqual(data["by_command"], [
            {"cmd": "issue view", "kind": "graphql", "calls": 1},
            {"cmd": "pr view", "kind": "graphql", "calls": 1},
            {"cmd": "run list", "kind": "rest", "calls": 1},
        ])
        self.assertEqual(data["by_ticket"], [
            {"repo": "Harness", "ticket": 9, "calls": 1, "graphql": 1},
            {"repo": "Harness", "ticket": 130, "calls": 1, "graphql": 1},
        ])
        self.assertEqual(data["by_hour"], [
            {"hour": hour_of(b0), "calls": 2, "graphql": 2, "rest": 0},
            {"hour": hour_of(b0 + dt.timedelta(hours=1)), "calls": 1, "graphql": 0, "rest": 1},
        ])

        # Widening the window to 27 h takes in the 3 h 10 min and 26 h old lines, never the 5-day-old one.
        wide = self.report("--hours", "27")
        self.assertEqual((wide["total"], wide["graphql"], wide["rest"]), (5, 4, 1))
        self.assertEqual(wide["by_ticket"], [
            {"repo": "Harness", "ticket": 9, "calls": 2, "graphql": 2},
            {"repo": "Harness", "ticket": 130, "calls": 2, "graphql": 2},
        ])

    def test_refused_failed_and_waited_totals(self):
        b0 = this_hour() - dt.timedelta(hours=2)
        at = lambda minutes: b0 + dt.timedelta(minutes=minutes)  # noqa: E731
        self.seed([
            call(at(1), "harness-pr", "pr view", "graphql", ticket=130, exit=0),
            call(at(2), "harness-pr", "pr view", "graphql", ticket=130, exit=1),                 # failed
            call(at(3), "harness-pr", "pr checks", "graphql", ticket=130, exit=None),            # failed (null)
            call(at(4), "harness-pr", "pr view", "graphql", ticket=130, exit=1, refused=True, waited_ms=2000),
            call(at(5), "harness-board", "issue view", "graphql", ticket=9, exit=None, refused=True),
            call(at(6), "harness-board", "issue view", "graphql", ticket=9, exit=0, waited_ms=9250),
            call(at(7), "harness-work", "run list", "rest", exit=4, waited_ms=1250),               # failed
            call(at(8), "harness-work", "run list", "rest", exit=0),
        ])
        data = self.report()
        self.assertEqual(data["hours"], 24.0)
        self.assertEqual((data["total"], data["graphql"], data["rest"]), (8, 6, 2))
        self.assertEqual(data["refused"], 2)
        self.assertEqual(data["failed"], 3)
        self.assertAlmostEqual(data["waited_seconds"], 12.5, places=6)
        self.assertEqual(data["by_tool"], [
            {"tool": "harness-pr", "calls": 4, "graphql": 4, "rest": 0, "refused": 1},
            {"tool": "harness-board", "calls": 2, "graphql": 2, "rest": 0, "refused": 1},
            {"tool": "harness-work", "calls": 2, "graphql": 0, "rest": 2, "refused": 0},
        ])

        # Two more lines move every total: a refused rest call that waited, and a failed graphql one.
        self.seed([
            call(at(9), "harness-work", "run list", "rest", exit=None, refused=True, waited_ms=30500),
            call(at(10), "harness-board", "issue view", "graphql", exit=2, waited_ms=125),
        ])
        more = self.report()
        self.assertEqual((more["total"], more["refused"], more["failed"]), (10, 3, 4))
        self.assertAlmostEqual(more["waited_seconds"], 43.125, places=6)
        self.assertEqual(more["by_tool"], [
            {"tool": "harness-pr", "calls": 4, "graphql": 4, "rest": 0, "refused": 1},
            {"tool": "harness-board", "calls": 3, "graphql": 3, "rest": 0, "refused": 1},
            {"tool": "harness-work", "calls": 3, "graphql": 0, "rest": 3, "refused": 1},
        ])

    def test_peak_minute(self):
        b0 = this_hour() - dt.timedelta(hours=2)
        at = lambda minutes, seconds: b0 + dt.timedelta(minutes=minutes, seconds=seconds)  # noqa: E731
        lines = [
            call(at(7, 5), "harness-pr", "pr view", "graphql"),
            call(at(7, 50), "harness-pr", "pr view", "graphql"),
            call(at(30, 59), "harness-pr", "pr view", "graphql"),       # minute 30, a second before 31
            call(at(31, 0), "harness-board", "issue view", "graphql"),
            call(at(31, 15), "harness-board", "issue view", "graphql"),
            call(at(31, 30), "harness-work", "run list", "rest"),
            call(at(31, 59), "harness-pr", "pr view", "graphql"),
            call(at(32, 0), "harness-pr", "pr view", "graphql"),
            call(at(32, 1), "harness-pr", "pr view", "graphql"),
            call(at(32, 40), "harness-pr", "pr view", "graphql"),
        ]
        self.seed(lines)
        data = self.report()
        self.assertEqual(data["peak_minute"], {"minute": minute_of(at(31, 0)), "calls": 4})
        self.assertTrue(data["peak_minute"]["minute"].endswith(":00Z"))

        self.seed([call(at(32, 50), "harness-pr", "pr view", "graphql"),
                   call(at(32, 55), "harness-board", "issue view", "graphql")])
        self.assertEqual(self.report()["peak_minute"], {"minute": minute_of(at(32, 0)), "calls": 5})


# =========================================================================== the text form
class Text(CallsTest):
    def test_summary_line_and_tables(self):
        b0 = this_hour() - dt.timedelta(hours=2)
        at = lambda minutes, seconds=0: b0 + dt.timedelta(minutes=minutes, seconds=seconds)  # noqa: E731
        self.seed([                                   # the ticket's example: 6 calls, 12.5 s waited
            call(at(1, 5), "harness-pr", "pr view", "graphql", ticket=130),
            call(at(1, 20), "harness-pr", "pr view", "graphql", ticket=130, exit=1, refused=True, waited_ms=10000),
            call(at(1, 50), "harness-pr", "pr view", "graphql", ticket=130, waited_ms=2500),
            call(at(15), "harness-pr", "run list", "rest", ticket=130, exit=1),
            call(at(40), "harness-board", "issue view", "graphql", ticket=130),
            call(at(50), "harness-work", ACTIONS, "rest", repo="jotform", ticket=4),
        ])
        out = self.ok("budget", "calls").stdout
        lines = out.splitlines()
        self.assertEqual(lines[0], "gh calls in the last 24 h: 6 (graphql 4, rest 2), 1 refused, 1 failed, "
                                   f"12 s waiting; peak 3 in one minute ({short_minute(at(1))})")
        tool_table = "\n".join([
            "TOOL           CALLS  GRAPHQL  REST  REFUSED",
            "harness-pr     4      3        1     1",
            "harness-board  1      1        0     0",
            "harness-work   1      0        1     0",
        ])
        command_table = "\n".join([
            "COMMAND                              KIND     CALLS",
            "pr view                              graphql  3",
            "api repos/:owner/:repo/actions/runs  rest     1",
            "issue view                           graphql  1",
            "run list                             rest     1",
        ])
        ticket_table = "\n".join([
            "TICKET        CALLS  GRAPHQL",
            "Harness #130  5      4",
            "jotform #4    1      0",
        ])
        where = [out.find(t) for t in (tool_table, command_table, ticket_table)]
        self.assertNotIn(-1, where, out)
        self.assertEqual(where, sorted(where), out)
        self.assertGreater(where[0], len(lines[0]), out)

        # --hours shows in the summary line.
        self.assertTrue(self.ok("budget", "calls", "--hours", "6").stdout.startswith(
            "gh calls in the last 6 h: 6 (graphql 4, rest 2), 1 refused, 1 failed, 12 s waiting; peak 3 in one "
            f"minute ({short_minute(at(1))})\n"))

    def test_command_table_shows_the_top_15(self):
        b0 = this_hour() - dt.timedelta(hours=2)
        lines, minute = [], 0
        # 17 commands: `pr view` 3 calls, `issue view` 2, then 15 with one call each, named c01..c15 in reverse
        # order of writing; the table keeps pr view, issue view and c01..c13.
        for cmd, count in [("pr view", 3), ("issue view", 2)] + [(f"api c{i:02d}", 1) for i in range(15, 0, -1)]:
            for _ in range(count):
                lines.append(call(b0 + dt.timedelta(minutes=minute), "harness-pr", cmd, "graphql"))
                minute += 1
        self.seed(lines)
        out = self.ok("budget", "calls").stdout
        start = out.index("COMMAND")
        rows = []
        for row in out[start:].splitlines()[1:]:
            if not row.strip() or row.startswith("TICKET"):
                break
            rows.append(row.split("  ")[0].strip())
        self.assertEqual(rows, ["pr view", "issue view"] + [f"api c{i:02d}" for i in range(1, 14)], out)
        self.assertNotIn("api c14", out)
        self.assertNotIn("api c15", out)
        data = self.report()
        self.assertEqual(len(data["by_command"]), 17)
        self.assertEqual([r["cmd"] for r in data["by_command"]],
                         ["pr view", "issue view"] + [f"api c{i:02d}" for i in range(1, 16)])

    def test_empty_window_message(self):
        p = self.ok("budget", "calls")                    # no gh folder at all
        self.assertEqual(p.stdout, "No gh calls logged in the last 24 h.\n")
        self.assertEqual(p.stderr, "")

        h = this_hour()
        self.seed([call(h - dt.timedelta(hours=7), "harness-pr", "pr view", "graphql", ticket=130),
                   (stamp(h)[:10], "not json")])
        p = self.ok("budget", "calls", "--hours", "6")    # only an older line and a bad one
        self.assertEqual(p.stdout, "No gh calls logged in the last 6 h.\n")

        data = self.report("--hours", "6")
        self.assertEqual(data["hours"], 6.0)
        for key in ("total", "graphql", "rest", "refused", "failed", "waited_seconds"):
            self.assertEqual(data[key], 0, key)
        self.assertIsNone(data["peak_minute"])
        for key in ("by_tool", "by_command", "by_ticket", "by_hour"):
            self.assertEqual(data[key], [], key)


# =========================================================================== arguments and side effects
class Args(CallsTest):
    def test_hours_must_be_a_positive_number(self):
        bad = [["--hours", "0"], ["--hours", "0.0"], ["--hours=-1"], ["--hours", "-1"], ["--hours=-0.5"],
               ["--hours", "abc"], ["--hours", "nan"], ["--hours="], ["--hours", "0", "--json"]]
        for args in bad:
            with self.subTest(args=args):
                p = self.work("budget", "calls", *args)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
                self.assertIn("--hours", p.stderr)
                self.assertEqual(p.stdout, "")
        for value in ("0.5", "1", "72"):
            with self.subTest(hours=value):
                self.assertEqual(self.ok("budget", "calls", "--hours", value).stdout,
                                 f"No gh calls logged in the last {value} h.\n")

    def test_makes_no_gh_call(self):
        b0 = this_hour() - dt.timedelta(hours=2)
        self.seed([call(b0 + dt.timedelta(minutes=5), "harness-pr", "pr view", "graphql", ticket=130),
                   call(b0 + dt.timedelta(minutes=6), "harness-work", "run list", "rest")])
        files = self.day_file_texts()
        for args, code in ((["budget", "calls"], 0), (["budget", "calls", "--json"], 0),
                           (["budget", "--json", "calls"], 0), (["budget", "calls", "--hours", "1"], 0),
                           (["budget", "calls", "--hours", "0"], 1)):
            with self.subTest(args=args):
                p = self.work(*args)
                self.assertEqual(p.returncode, code, p.stdout + p.stderr)
                self.assertEqual(self.gh_calls(), [])
                self.assertFalse(os.path.exists(self.gh_log))
        after = self.day_file_texts()
        self.assertEqual(after, files)            # no call of ours was logged either
        self.assertFalse(os.path.exists(self.budgets_path()))

    def test_budgets_are_left_alone(self):
        until = stamp(dt.datetime.now(UTC) + dt.timedelta(hours=3))
        text = json.dumps({"api": {"exhausted_until": until, "reason": "billing"},
                           "cloud": {"exhausted_until": None, "reason": ""}})
        write(self.budgets_path(), text)
        self.ok("budget", "calls")
        self.ok("budget", "calls", "--json")
        with open(self.budgets_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)
        self.assertEqual(self.ok("budget").stdout.splitlines(),
                         [f"api: exhausted until {until} (billing)", "cloud: available"])
        status = json.loads(self.ok("budget", "status", "--json").stdout)
        self.assertEqual(set(status), {"api", "cloud"})


# =========================================================================== docs
class Docs(unittest.TestCase):
    def test_usage_and_design_name_the_command(self):
        with open(TOOL, encoding="utf-8") as fh:
            source = fh.read()
        docstring = source.split('"""', 2)[1]
        usage = docstring.split("Usage:", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(usage, r"harness-work budget[^\n]*\bcalls\b[^\n]*--hours")
        with open(DESIGN, encoding="utf-8") as fh:
            design = fh.read()
        section = design.split("\n### harness-work\n", 1)[1].split("\n### ", 1)[0]
        self.assertRegex(section, r"`budget[^`]*\bcalls\b[^`]*--hours[^`]*`")


if __name__ == "__main__":
    unittest.main()
