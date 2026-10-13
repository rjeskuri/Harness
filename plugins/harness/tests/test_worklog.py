"""Tests for which transcript entries harness-worklog counts as the user's own messages (ticket #25).

    python3 -m unittest discover -s plugins/harness/tests -p "test_worklog.py" -v

A block of activity is billed only when it holds a message the user typed. Besides plain text, the user
types three kinds of entry that Claude Code stores starting with a tag: a message that is entirely a paste
("\\n\\n<pasted_content id=...>"), a `!` shell command (<bash-input>) and a slash command (<command-name>,
or <command-message> with a <command-name> after it). Those count when the entry's text leads with that
tag, matched by its exact name, whatever follows. Task notifications, system reminders, local command
output and caveats, the output of a `!` command (<bash-stdout>), skill loads (isMeta) and compaction
summaries (isCompactSummary) do not; text that starts with '<' but not with a tag ("<3 thanks") is plain.

Everything is proven through the tool's public interface: `harness-worklog update` runs as a subprocess
with a temporary HOME holding the transcripts, and a day appears in worklog/worklog.csv (and on stdout)
exactly when one of its blocks holds a counted message. Each case sits on its own day, so the set of
days written says which entries counted. Nothing reads or writes the real ~/.claude.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
TOOL = os.path.join(BIN, "harness-worklog")
GIT = shutil.which("git")
UTC = dt.timezone.utc


def encode(path):
    """Claude Code's session folder name for a project path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def parse_zone(text):
    """'+09:00' or an IANA name -> tzinfo (read here, not through the tool)."""
    text = text.strip()
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", text)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return dt.timezone(sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    from zoneinfo import ZoneInfo
    return ZoneInfo(text)


def offset_text(when):
    minutes = int(when.utcoffset().total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def stamp(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(when, content, **extra):
    entry = {"type": "user", "timestamp": stamp(when), "message": {"role": "user", "content": content}}
    entry.update(extra)
    return entry


def assistant(when):
    return {"type": "assistant", "timestamp": stamp(when),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on it."}]}}


def tool_result(when):
    return user(when, [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}])


# ------------------------------------------------------------------ realistic entry contents
PASTE = "\n\n<pasted_content id=\"1\">Sync the Glow orders into the sheet every night and flag duplicates.\n" \
        "Orders older than 30 days stay put.</pasted_content>"
PASTE_BLOCK = [{"type": "text", "text": "\n\n<pasted_content id=\"2\">Traceback (most recent call last):\n"
                                          "  File \"sync.py\", line 12, in <module>\nKeyError: 'sku'"
                                          "</pasted_content>"}]
BASH = "<bash-input>ls</bash-input>"
BASH_BLOCK = [{"type": "text", "text": "  <bash-input>git status</bash-input>"}]
SLASH = ("<command-name>/harness:work</command-name>\n<command-message>harness:work</command-message>\n"
         "<command-args>25</command-args>")
SLASH_BLOCK = [{"type": "text", "text": "<command-name>/clear</command-name>\n"
                                        "<command-message>clear</command-message>\n<command-args></command-args>"}]

# What real transcripts mostly hold for a typed slash command: <command-message> first, then <command-name>.
SLASH_MESSAGE_FIRST = ("<command-message>harness:work</command-message>\n<command-name>/harness:work</command-name>\n"
                       "<command-args>11</command-args>")
SLASH_MESSAGE_FIRST_BLOCK = [{"type": "text", "text": "<command-message>harness:plan</command-message>\n"
                                                      "<command-name>/harness:plan</command-name>\n"
                                                      "<command-args>sync the orders</command-args>"}]

TASK_NOTIFICATION = ("<task-notification>\n<task-id>b7f1c2</task-id>\n<status>completed</status>\n"
                     "<summary>Background command \"npm test\" completed (exit code 0)</summary>\n"
                     "</task-notification>")
SYSTEM_REMINDER = [{"type": "text", "text": "<system-reminder>\nThe user opened the file sync.py in the IDE.\n"
                                            "</system-reminder>"}]
LOCAL_STDOUT = "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"
LOCAL_CAVEAT = ("<local-command-caveat>Caveat: The messages below were generated by the user while running "
                "local commands. DO NOT respond to these messages or otherwise consider them in your response "
                "unless the user explicitly asks you to.</local-command-caveat>")
BASH_STDOUT = "<bash-stdout>sync.py\nREADME.md</bash-stdout><bash-stderr></bash-stderr>"
SKILL_LOAD = "Base directory for this skill: /Users/me/.claude/skills/worklog\n\nRun harness-worklog update."
COMPACT_SUMMARY = ("This session is being continued from a previous conversation that ran out of context. "
                   "The conversation is summarized below:\nAnalysis: the user asked for the sync job.")


class HumanMessages(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-worklog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "client-repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        p = self.run_wl("init")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(self.zones_path(), encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.mac_zone_text = rows[0]["zone"]          # the Mac's zone, as init recorded it
        self.tz = parse_zone(self.mac_zone_text)
        self.today = dt.datetime.now(UTC).astimezone(self.tz).date()

    # ----------------------------------------------------------------- helpers
    def env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        path = ([os.path.dirname(GIT)] if GIT else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(path), GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1")
        return env

    def run_wl(self, *args):
        return subprocess.run([sys.executable, TOOL, "--repo", self.repo] + list(args), cwd=self.tmp,
                              env=self.env(), capture_output=True, text=True, timeout=60)

    def zones_path(self):
        return os.path.join(self.repo, "worklog", "timezones.csv")

    def session(self, name, entries):
        folder = os.path.join(self.home, ".claude", "projects", encode(self.repo))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def at(self, day, hh, mm, tz=None):
        return dt.datetime.combine(day, dt.time(hh, mm), tz or self.tz)

    def worked(self, name, start, content, minutes, **extra):
        """One session: the user entry at `start`, a tool result, then the agent until start + minutes
        (never more than 10 minutes between entries, so it is one block if the user entry counts)."""
        entries = [user(start, content, **extra), assistant(start + dt.timedelta(minutes=1)),
                   tool_result(start + dt.timedelta(minutes=2))]
        m = 2
        while m < minutes:
            m = min(m + 10, minutes)
            entries.append(assistant(start + dt.timedelta(minutes=m)))
        self.session(name, entries)

    def update(self):
        p = self.run_wl("update")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        log = os.path.join(self.repo, "worklog", "worklog.csv")
        rows = []
        if os.path.exists(log):
            with open(log, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        return p, {r["date"]: r for r in rows}

    # A bare `update` recomputes the last 14 days (today - 14 on); every fixture day here is at most
    # MAX_BACK days back, so a correct implementation never misses a case for falling outside the window.
    MAX_BACK = 13

    def assert_counted(self, cases):
        """cases: (name, content, (hh, mm), minutes of work, expected hours) - each on its own day, with its
        own clock time and length, so the block each day shows is its own. Every case must be written,
        with exactly its block, and nothing else."""
        self.assertLessEqual(len(cases), self.MAX_BACK)
        expected = {}
        for i, (name, content, (hh, mm), minutes, hours) in enumerate(cases, start=1):
            day = self.today - dt.timedelta(days=i)
            start = self.at(day, hh, mm)
            self.worked(f"{name}.jsonl", start, content, minutes)
            end = start + dt.timedelta(minutes=minutes)
            expected[day.isoformat()] = (name, hours, f"{start:%H:%M}-{end:%H:%M}", offset_text(start))
        p, rows = self.update()
        for date, (name, hours, blocks, zone) in expected.items():
            with self.subTest(case=name, date=date):
                self.assertIn(date, rows, f"the {name} message was not counted; log: {sorted(rows)}")
                row = rows[date]
                self.assertEqual((row["hours"], row["blocks"], row["source"], row["zone"]),
                                 (hours, blocks, "auto", zone))
                self.assertRegex(p.stdout, rf"{date}\s+{re.escape(hours)} h\s+{blocks}")
        self.assertEqual(sorted(rows), sorted(expected))

    def assert_excluded(self, excluded, controls):
        """excluded: (name, content, extra) - each the only user entry of its day; none may be written.
        controls: (name, content, (hh, mm), minutes, block) on the most recent days - each must be written
        with exactly its block, proving the same run counts what it should."""
        self.assertLessEqual(len(controls) + len(excluded), self.MAX_BACK)
        control_days = {}
        for i, (name, content, (hh, mm), minutes, block) in enumerate(controls, start=1):
            day = self.today - dt.timedelta(days=i)
            self.worked(f"control-{name}.jsonl", self.at(day, hh, mm), content, minutes)
            control_days[day.isoformat()] = block
        excluded_days = {}
        for i, (name, content, extra) in enumerate(excluded, start=len(controls) + 1):
            day = self.today - dt.timedelta(days=i)
            self.worked(f"{name}.jsonl", self.at(day, 10, 0), content, 40, **extra)
            excluded_days[day.isoformat()] = name
        p, rows = self.update()
        counted = sorted(name for date, name in excluded_days.items() if date in rows)
        self.assertEqual(counted, [], f"excluded entries were counted as the user's: {counted}")
        self.assertEqual(sorted(rows), sorted(control_days))
        for date, block in control_days.items():
            self.assertEqual(rows[date]["blocks"], block)

    # ----------------------------------------------------------------- tests
    def test_pasted_bash_and_slash_messages_count(self):
        self.assert_counted([
            ("paste-string", PASTE, (21, 6), 13, "0.25"),
            ("paste-list", PASTE_BLOCK, (9, 40), 40, "0.75"),
            ("bash-string", BASH, (14, 15), 25, "0.50"),
            ("bash-list", BASH_BLOCK, (7, 5), 52, "0.75"),
            ("slash-string", SLASH, (16, 30), 70, "1.25"),
            ("slash-list", SLASH_BLOCK, (11, 0), 20, "0.25"),
        ])

    def test_real_slash_order_trailing_text_and_plain_angle_text_count(self):
        # Real transcripts mostly store a typed slash command message-first (<command-message> then
        # <command-name>); the human tag is the leading one whatever follows it (text after a paste, a `!`
        # command's own output, a slash command without args or ending in another tag); and a message that
        # starts with '<' but not with a tag is plain text.
        self.assert_counted([
            ("slash-message-first-string", SLASH_MESSAGE_FIRST, (8, 15), 35, "0.50"),
            ("slash-message-first-list", SLASH_MESSAGE_FIRST_BLOCK, (19, 5), 47, "0.75"),
            ("paste-then-text", "<pasted_content id=\"4\">x</pasted_content>\nplease fix this",
             (12, 40), 9, "0.25"),
            ("bash-input-then-stdout", "<bash-input>ls</bash-input><bash-stdout>a</bash-stdout>",
             (15, 3), 61, "1.00"),
            ("slash-without-args",
             "<command-name>/compact</command-name>\n<command-message>compact</command-message>",
             (6, 50), 28, "0.50"),
            ("slash-ending-in-other-tag",
             [{"type": "text", "text": "<command-name>/clear</command-name>\n<command-message>clear"
                                       "</command-message>\n<skill-format>true</skill-format>"}],
             (20, 20), 83, "1.50"),
            ("less-than-three", "<3 thanks, that fixed it", (10, 10), 17, "0.25"),
            ("less-than-space-list", [{"type": "text", "text": "< 5 rows came back, expected 12"}],
             (13, 33), 100, "1.75"),
        ])

    def test_notifications_reminders_and_tool_output_do_not_count(self):
        # Each excluded entry is the only user entry of its day; two control days (a plain message and
        # a paste) prove the run counts what it should, so the set of days written is exactly the controls.
        self.assert_excluded([
            ("task-notification", TASK_NOTIFICATION, {}),
            ("system-reminder", SYSTEM_REMINDER, {}),
            ("local-command-stdout", LOCAL_STDOUT, {}),
            ("local-command-caveat", LOCAL_CAVEAT, {}),
            ("bash-stdout", BASH_STDOUT, {}),
            ("skill-load-isMeta", SKILL_LOAD, {"isMeta": True}),
            ("compact-summary", COMPACT_SUMMARY, {"isCompactSummary": True}),
            ("tool-result-only", [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "done"}], {}),
        ], controls=[
            ("plain", "fix the totals please", (15, 10), 25, "15:10-15:35"),
            ("paste", PASTE, (8, 30), 13, "08:30-08:43"),
        ])

    def test_only_the_human_tags_count_matched_by_exact_name(self):
        # The rule is an allow-list on the leading tag, matched by its exact name: a tag off the list does
        # not count (so a deny-list of known machine tags fails), nor does a tag whose name merely starts
        # with a human tag's name (so a prefix match fails), nor <command-message> with no <command-name>
        # after it, nor a slash command stored as isMeta (a skill load).
        self.assert_excluded([
            ("user-prompt-submit-hook",
             "<user-prompt-submit-hook>Remember to run the tests.</user-prompt-submit-hook>", {}),
            ("bash-stderr-alone", "<bash-stderr>ls: nope: No such file or directory</bash-stderr>", {}),
            ("command-message-without-name",
             "<command-message>harness:work</command-message>\n<command-args>25</command-args>", {}),
            ("made-up-tag", "<some-new-tag>whatever Claude Code adds next</some-new-tag>", {}),
            ("made-up-tag-list", [{"type": "text", "text": "<agent-status>idle</agent-status>"}], {}),
            ("command-names", "<command-names>x</command-names>", {}),
            ("bash-inputs-list", [{"type": "text", "text": "<bash-inputs>x</bash-inputs>"}], {}),
            ("pasted-contents", "<pasted_contents>x</pasted_contents>", {}),
            ("isMeta-slash-message-first", SLASH_MESSAGE_FIRST, {"isMeta": True}),
        ], controls=[
            ("plain", "now the refund report", (13, 20), 32, "13:20-13:52"),
            ("slash", SLASH, (18, 45), 25, "18:45-19:10"),
        ])

    def test_a_human_tag_counts_only_when_it_leads(self):
        # An excluded entry that quotes or ends in a human tag does not count (so a "contains the tag
        # anywhere" or "matches the trailing tag" rule fails); isMeta and empty text never count.
        self.assert_excluded([
            ("task-notification-quoting-command-name",
             "<task-notification>\n<task-id>c3d9e0</task-id>\n<status>completed</status>\n"
             "<summary>Session for <command-name>/harness:work</command-name> finished</summary>\n"
             "</task-notification>", {}),
            ("system-reminder-quoting-paste",
             [{"type": "text", "text": "<system-reminder>\nEarlier the user sent "
                                       "<pasted_content id=\"3\">a log</pasted_content>\n</system-reminder>"}],
             {}),
            ("system-reminder-ending-in-paste",
             "<system-reminder>The user opened sync.py.</system-reminder><pasted_content id=\"5\">x</pasted_content>",
             {}),
            ("local-stdout-quoting-bash-input",
             "<local-command-stdout>history: <bash-input>ls</bash-input></local-command-stdout>", {}),
            ("caveat-then-command-name-list",
             [{"type": "text", "text": "<local-command-caveat>Caveat: local commands below.</local-command-caveat>"
                                       "<command-name>/clear</command-name>"}], {}),
            ("isMeta-command-name", "<command-name>/x</command-name>\n<command-message>x</command-message>",
             {"isMeta": True}),
            ("isMeta-paste", PASTE, {"isMeta": True}),
            ("whitespace-string", "   \n\t  ", {}),
            ("whitespace-list", [{"type": "text", "text": " \n \n"}], {}),
        ], controls=[
            ("plain", "ship the export", (11, 25), 44, "11:25-12:09"),
            ("slash-message-first", SLASH_MESSAGE_FIRST, (17, 0), 19, "17:00-17:19"),
        ])

    def test_a_block_opened_by_a_paste_is_counted(self):
        # The jotform-glow-sync shape: on +09:00, a paste-only request at 21:06 and about 13 minutes of the
        # agent working (tool results in between), then at 22:30 a block holding only a task notification.
        plus9 = dt.timezone(dt.timedelta(hours=9))
        day = self.today - dt.timedelta(days=4)
        # The timeline: +09:00 over that day, then the Mac's zone again from two days later.
        back = dt.datetime.combine(day + dt.timedelta(days=2), dt.time(), UTC)
        with open(self.zones_path(), "w", encoding="utf-8", newline="") as fh:
            fh.write("since,zone,evidence\n")
            fh.write(f"{dt.datetime.combine(day - dt.timedelta(days=10), dt.time(), UTC).isoformat()},"
                     "+09:00,trip\n")
            fh.write(f"{back.isoformat()},{self.mac_zone_text},home\n")

        def t(hh, mm):
            return self.at(day, hh, mm, plus9)

        self.session("glow.jsonl", [
            user(t(21, 6), PASTE),
            assistant(t(21, 7)),
            tool_result(t(21, 8)),
            assistant(t(21, 10)),
            tool_result(t(21, 12)),
            assistant(t(21, 15)),
            tool_result(t(21, 17)),
            assistant(t(21, 19)),
            user(t(22, 30), TASK_NOTIFICATION),
            assistant(t(22, 31)),
            assistant(t(22, 40)),
        ])
        p, rows = self.update()
        self.assertIn(day.isoformat(), rows, f"the paste-opened block was not counted; stdout: {p.stdout}")
        row = rows[day.isoformat()]
        self.assertEqual((row["hours"], row["blocks"], row["source"], row["zone"]),
                         ("0.25", "21:06-21:19", "auto", "+09:00"))
        self.assertRegex(p.stdout, rf"{day.isoformat()}\s+0\.25 h\s+21:06-21:19")
        self.assertNotIn("22:30", p.stdout)
        self.assertEqual(sorted(rows), [day.isoformat()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
