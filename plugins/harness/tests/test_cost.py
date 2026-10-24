"""Tests for `harness-work cost N [--pr P] [--json | --markdown] [--repo PATH]` (ticket #113, epic #108):
a ticket's tokens and model time per stage and model, read from the transcripts (harness_lib/cost.py's
ticket_usage) and its attempts (the ticket cache), printed and saved as
~/.claude/harness/repos/<key>/cost/<N>.json. Tokens and time only: no dollar figure anywhere.

The contract under test (the ticket's Context):
- The record: ticket, title, repo, computed_at, type, size, risk, tier, pr, rows, totals, by_model,
  session_totals, sessions, rounds, needs_input, lines, elapsed, ci, landed_at, coverage. rows to sessions are
  ticket_usage's; pr is --pr or null; the five task-6 fields stay null; title to tier and rounds are null
  without a ticket cache.
- Rounds: per stage, in attempt order, a fail extends that stage's run of failures; its first fail is a retry,
  the second an escalate, the third and later needs-input (fix: retry, then needs-input), as `harness-work
  attempt` answers; a pass ends the run; each stage counts on its own. breaker_rerounds = breaker fails,
  fix_rounds = fix attempts.
- Number formats: tokens below 1,000 as the integer, below 1,000,000 as 12.3k, else 12.3M; durations 45s, 12m,
  1h05m; model names without a leading `claude-`.
- --markdown: the Cost block, rows aggregated by stage and model (rounds counted, times summed), stages in
  ticket_usage's order, lines without data left out. The text output shows the same rows.

Each test has a temporary HOME holding the transcripts (~/.claude/projects/<folder>) and the ticket cache,
written by the fixture writers below, and a real `git init` main checkout. HARNESS_GH is fakegh.py with a
scenario whose default exits 1 (task 6 adds gh calls to this command), so stderr is never asserted empty.
Expected figures are written out by hand from the fixtures, or summed here from the same message specs the
writers wrote; the rounds are also tallied from the real `harness-work attempt` answers. The fake gh wrapper
is written once for the module.

    python3 -m unittest discover -s plugins/harness/tests -p test_cost.py -v
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "bin", "harness-work")
FAKEGH = os.path.join(HERE, "fakegh.py")
GIT = shutil.which("git")

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-5"
FABLE = "claude-fable-5"

STAGES = ("tests", "breaker", "code", "review", "fix", "other", "orchestrator")
RECORD_KEYS = {"ticket", "title", "repo", "computed_at", "type", "size", "risk", "tier", "pr", "rows", "totals",
               "by_model", "session_totals", "sessions", "rounds", "needs_input", "lines", "elapsed", "ci",
               "landed_at", "coverage"}
TASK6_KEYS = ("needs_input", "lines", "elapsed", "ci", "landed_at")
CACHE_KEYS = ("title", "type", "size", "risk", "tier")
TOTAL_KEYS = ("input", "output", "cache_write", "cache_read", "total", "messages", "model_ms")

# Dollar figures the fixtures carry in cost-state and must never come out
DOLLARS = ("4321.75", "17.25", "18.25", "19.25")
MONEY = re.compile(r"(?i)\$|usd|price")

HEADER = "**Cost** (tokens and model time from the transcripts, a lower bound)"
TABLE_HEAD = "| Stage | Model | Rounds | Tokens | Output | Model time | Agent time |"
TABLE_RULE = "|---|---|---:|---:|---:|---:|---:|"


# ------------------------------------------------------------------ time
def T(clock, day=20):
    """A Claude Code timestamp on 2026-10-<day>: T("10:00:03.500")."""
    return f"2026-10-{day:02d}T{clock}Z"


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (when.microsecond // 1000)


def parse(ts):
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")


def ms(start, end):
    d = parse(end) - parse(start)
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


# ------------------------------------------------------------------ fixture writers
# One API response: a user entry at `start`, then an assistant entry at `end`; `use` is
# (input, output, cache_write, cache_read).
Msg = namedtuple("Msg", "mid start end use model branch", defaults=(OPUS, None))


def usage(inp=0, out=0, cw=0, cr=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": out, "cache_creation": {"ephemeral_5m_input_tokens": cw,
                                                     "ephemeral_1h_input_tokens": 0}}


def user_entry(ts, branch):
    entry = {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result"}]}}
    if branch is not None:
        entry["gitBranch"] = branch
    return entry


def assistant_entry(ts, mid, use, model, branch):
    entry = {"type": "assistant", "timestamp": ts, "requestId": "req_" + mid[-8:],
             "message": {"id": mid, "model": model, "usage": usage(*use), "content": [{"type": "text"}]}}
    if branch is not None:
        entry["gitBranch"] = branch
    return entry


def turns(msgs, branch):
    out = []
    for m in msgs:
        b = m.branch or branch
        out.append(user_entry(m.start, b))
        out.append(assistant_entry(m.end, m.mid, m.use, m.model, b))
    return out


def cost_state(api_ms, models, total_cost=4321.75):
    """The last line of an ended session: models {model: (input, output, thinking, cache_read, cache_write)},
    each with a costUSD that must never reach the output."""
    return {"type": "cost-state", "totalCostUSD": total_cost, "totalAPIDuration": api_ms,
            "totalDuration": api_ms * 3,
            "modelUsage": {model: {"inputTokens": v[0], "outputTokens": v[1], "thinkingTokens": v[2],
                                   "cacheReadInputTokens": v[3], "cacheCreationInputTokens": v[4],
                                   "costUSD": 17.25 + i}
                           for i, (model, v) in enumerate(models.items())}}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_jsonl(path, entries):
    write(path, "".join(json.dumps(e) + "\n" for e in entries))


def write_session(folder, session_id, entries, agents=(), cost=None):
    """<folder>/<id>.jsonl (cost-state last when given) and each agent (aid, meta, entries) as
    <folder>/<id>/subagents/agent-<aid>.jsonl with its .meta.json."""
    write_jsonl(os.path.join(folder, f"{session_id}.jsonl"), list(entries) + ([cost] if cost is not None else []))
    for aid, meta_value, agent_entries in agents:
        sub = os.path.join(folder, session_id, "subagents")
        write_jsonl(os.path.join(sub, f"agent-{aid}.jsonl"), agent_entries)
        write(os.path.join(sub, f"agent-{aid}.meta.json"), json.dumps(meta_value))


def meta(agent_type, description):
    return {"toolUseId": "toolu_01", "agentType": agent_type, "description": description}


def aid(n, k):
    return "%017x" % (n * 1000 + k)


# ------------------------------------------------------------------ expected values
def row(stage, rnd, agent, model, msgs):
    """The expected ticket_usage row for these message specs, summed here from the specs."""
    i, o, w, r = (sum(m.use[k] for m in msgs) for k in range(4))
    wall = None
    if stage != "orchestrator":
        wall = ms(min((m.start for m in msgs), key=parse), max((m.end for m in msgs), key=parse))
    return {"stage": stage, "round": rnd, "agent": agent, "model": model, "input": i, "output": o,
            "cache_write": w, "cache_read": r, "total": i + o + w + r, "messages": len(msgs),
            "model_ms": sum(max(0, ms(m.start, m.end)) for m in msgs), "wall_ms": wall}


def sort_rows(rows):
    return sorted(rows, key=lambda x: (STAGES.index(x["stage"]), x["round"], x["agent"], x["model"]))


def sums(rows):
    totals = {k: 0 for k in TOTAL_KEYS}
    by_model = {}
    for x in rows:
        per = by_model.setdefault(x["model"], {k: 0 for k in TOTAL_KEYS})
        for k in TOTAL_KEYS:
            totals[k] += x[k]
            per[k] += x[k]
    return totals, by_model


def block(rows, total, *lines):
    """The Cost block: rows (stage, model, rounds, tokens, output, model time, agent time), the total row
    (tokens, output, model time) and the lines after the table."""
    out = [HEADER, "", TABLE_HEAD, TABLE_RULE]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    out.append("| total | | | %s | %s | %s | |" % total)
    if lines:
        out += [""] + list(lines)
    return "\n".join(out)


def walk_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_keys(v)


# ------------------------------------------------------------------ the widget fixture
# Ticket N's own session (in a worktree folder, its main transcript on t/N-..., user entries only, with a
# cost-state) holding the tests x2, breaker, code (two models), review, fix x2 subagents; a shared session on
# main (orchestrator messages not counted, cost-state not counted) holding a second `#N review`; and another
# ticket's own session that must not count. Every figure below is written out by hand from these specs.
OWN = "0a1b2c3d-1111-4000-8000-000000000001"
SHARED = "4b318caa-2222-4000-8000-000000000002"
OTHER = "9e9e9e9e-3333-4000-8000-000000000003"

WIDGET_ROWS = [
    ("tests", "opus-5-5", "2", "9.0M", "20.1k", "4m", "15m"),
    ("breaker", "sonnet-5", "1", "999", "900", "45s", "45s"),
    ("code", "opus-5-5", "1", "12.3M", "50.0k", "12m", "12m"),
    ("code", "sonnet-5", "1", "500", "140", "20s", "20s"),
    ("review", "opus-5-5", "2", "150.0k", "5.0k", "6m", "6m"),
    ("fix", "opus-5-5", "2", "2.0M", "51.0k", "1h05m", "1h15m"),
]
WIDGET_TOTAL = ("23.5M", "127.1k", "1h28m")
WIDGET_SESSION = "Session total (Claude Code's count): 23.7M tokens, 306.0k output, 3.0k thinking, 1h45m API time"
WIDGET_ROUNDS = "Rounds: breaker re-rounds 1, fix rounds 2; retries 3, escalations 1, needs-input 0"
WIDGET_NOT_COUNTED = "Not counted: the orchestrator of shared session 4b318caa"
WIDGET_ATTEMPTS = [("tests", "pass"), ("breaker", "fail"), ("breaker", "pass"), ("code", "pass"),
                   ("review", "pass"), ("fix", "fail"), ("fix", "pass"), ("ci", "fail"), ("ci", "fail")]


def stage_counts(runs=0, fails=0, retry=0, escalate=0, needs_input=0):
    return {"runs": runs, "fails": fails, "retry": retry, "escalate": escalate, "needs_input": needs_input}


WIDGET_ROUNDS_RECORD = {
    "stages": {"tests": stage_counts(1), "breaker": stage_counts(2, 1, retry=1), "code": stage_counts(1),
               "review": stage_counts(1), "fix": stage_counts(2, 1, retry=1),
               "ci": stage_counts(2, 2, retry=1, escalate=1)},
    "breaker_rerounds": 1, "fix_rounds": 2}

# the own session's cost-state: (input, output, thinking, cache_read, cache_write)
WIDGET_COST = {OPUS: (1000, 300000, 2000, 20000000, 3000000), SONNET: (500, 6000, 1000, 400000, 32500)}
WIDGET_SESSION_TOTALS = {
    "models": {OPUS: {"input": 1000, "output": 300000, "thinking": 2000, "cache_read": 20000000,
                      "cache_write": 3000000},
               SONNET: {"input": 500, "output": 6000, "thinking": 1000, "cache_read": 400000,
                        "cache_write": 32500}},
    "api_ms": 6300000, "sessions": 1}

# (total tokens, shown, output, shown, model ms, shown, agent ms, shown, model, shown): one code row each
FORMAT_CASES = [
    (999, "999", 7, "7", 45000, "45s", 45000, "45s", OPUS, "opus-5-5"),
    (1000, "1.0k", 999, "999", 59000, "59s", 60000, "1m", SONNET, "sonnet-5"),
    (12340, "12.3k", 1000, "1.0k", 720000, "12m", 3900000, "1h05m", HAIKU, "haiku-5"),
    (999900, "999.9k", 139000, "139.0k", 3600000, "1h00m", 36000000, "10h00m", FABLE, "fable-5"),
    (1000000, "1.0M", 20140, "20.1k", 45400, "45s", 740000, "12m", OPUS, "opus-5-5"),
    (31400000, "31.4M", 999900, "999.9k", 4345000, "1h12m", 4345000, "1h12m", "opus-local", "opus-local"),
    (9140000, "9.1M", 1000000, "1.0M", 1000, "1s", 2000, "2s", SONNET, "sonnet-5"),
]

BINS = {}


def setUpModule():
    """fakebin: gh (a wrapper around fakegh.py); toolbin: git and python3 only (symlinks)."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-cost-bin-"))
    fakebin, toolbin = os.path.join(root, "fakebin"), os.path.join(root, "toolbin")
    gh = write(os.path.join(fakebin, "gh"), f'#!/bin/sh\nexec "{sys.executable}" "{FAKEGH}" "$@"\n')
    os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.makedirs(toolbin)
    os.symlink(GIT, os.path.join(toolbin, "git"))
    os.symlink(sys.executable, os.path.join(toolbin, "python3"))
    BINS.update(root=root, fakebin=fakebin, toolbin=toolbin)


def tearDownModule():
    shutil.rmtree(BINS.get("root", ""), True)


class CostCommand(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-cost-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        done = subprocess.run([GIT, "init", "-q", "-b", "main", self.repo], env=self.env(), capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.main = os.path.realpath(self.repo)
        self.key = hashlib.sha1(self.main.encode("utf-8")).hexdigest()[:12]
        self.projects = os.path.join(self.home, ".claude", "projects")
        self.scenario_path = write(os.path.join(self.tmp, "scenario.json"), json.dumps(
            {"calls": [], "default": {"stderr": "fakegh: no network in this test\n", "exit": 1}}))

    # ----------------------------------------------------------- helpers
    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "FAKEGH_"))}
        env.update(HOME=self.home, PATH=os.pathsep.join([BINS["fakebin"], BINS["toolbin"], "/usr/bin", "/bin"]),
                   GIT_CONFIG_NOSYSTEM="1", HARNESS_GH=os.path.join(BINS["fakebin"], "gh"),
                   FAKEGH_SCENARIO=os.path.join(self.tmp, "scenario.json"),
                   FAKEGH_LOG=os.path.join(self.tmp, "gh.log"))
        return env

    def work(self, *args):
        done = subprocess.run([sys.executable, TOOL] + [str(a) for a in args], cwd=self.tmp, env=self.env(),
                              capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", done.stderr)
        return done

    def cost(self, n, *extra):
        """`harness-work cost N ... --repo <main>`, which must exit 0; its stdout."""
        done = self.work("cost", n, *extra, "--repo", self.repo)
        self.assertEqual(done.returncode, 0, f"cost {n} {' '.join(extra)}: {done.stdout}{done.stderr}")
        return done.stdout

    def attempt(self, n, stage, result):
        done = self.work("attempt", n, stage, result, "--repo", self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.strip()

    def state(self, *parts):
        return os.path.join(self.home, ".claude", "harness", "repos", self.key, *parts)

    def record_path(self, n):
        return self.state("cost", f"{n}.json")

    def read_record(self, n):
        with open(self.record_path(n), encoding="utf-8") as fh:
            return json.load(fh)

    def main_folder(self):
        return os.path.join(self.projects, re.sub(r"[^A-Za-z0-9]", "-", self.main))

    def worktree_folder(self, name):
        wt = os.path.join(self.main, ".claude", "worktrees", name)
        return os.path.join(self.projects, re.sub(r"[^A-Za-z0-9]", "-", wt))

    def write_cache(self, n, title="Add the widget", type_="task", size="M", risk="low", tier="opus",
                    attempts=()):
        labels = [type_, "status:in-progress"] + ([f"size:{size}"] if size else []) + (
            [f"risk:{risk}"] if risk else [])
        cache = {"number": n, "title": title, "url": f"https://github.com/o/r/issues/{n}", "type": type_,
                 "status": "in-progress", "size": size, "risk": risk, "flags": [], "labels": labels, "tier": tier,
                 "parent": {"number": 108, "title": "Measure", "goal": "Tokens only."}, "blocked_by": [],
                 "body": "## Request\nAdd it.\n", "sections": {"request": "Add it."}, "comments": [],
                 "fetched_at": "2026-10-20T08:00:00Z", "branch": f"t/{n}-add-the-widget", "worktree": None,
                 "started_at": "2026-10-20T08:00:00Z",
                 "attempts": [{"stage": s, "tier": tier, "result": r, "at": "2026-10-20T%02d:00:00Z" % (9 + i)}
                              for i, (s, r) in enumerate(attempts)]}
        write(self.state("tickets", f"{n}.json"), json.dumps(cache, indent=2))

    def widget_fixture(self, n):
        """Write the widget sessions for ticket n; returns the expected rows, session_totals and sessions."""
        branch = f"t/{n}-add-the-widget"
        a1 = [Msg(f"msg_{n}_a1_1", T("10:00:00.000"), T("10:00:30.000"), (10, 4000, 500000, 1500000)),
              Msg(f"msg_{n}_a1_2", T("10:11:00.000"), T("10:12:00.000"), (5, 6000, 0, 2000000))]
        a2 = [Msg(f"msg_{n}_a2_1", T("11:00:00.000"), T("11:01:00.000"), (20, 5100, 1000000, 2000000)),
              Msg(f"msg_{n}_a2_2", T("11:01:30.000"), T("11:03:00.000"), (0, 5000, 0, 2000000))]
        a3 = [Msg(f"msg_{n}_a3_1", T("12:00:00.000"), T("12:00:45.000"), (3, 900, 0, 96), SONNET)]
        a4 = [Msg(f"msg_{n}_a4_1", T("13:00:00.000"), T("13:12:00.000"), (100, 50000, 2000000, 10290000)),
              Msg(f"msg_{n}_a4_2", T("13:15:00.000"), T("13:15:20.000"), (1, 140, 0, 359), SONNET)]
        a5 = [Msg(f"msg_{n}_a5_1", T("14:00:00.000"), T("14:05:00.000"), (7, 3000, 40000, 57000))]
        r2 = [Msg(f"msg_{n}_r2_1", T("15:00:00.000"), T("15:01:05.000"), (3, 2000, 0, 47990))]
        a6 = [Msg(f"msg_{n}_a6_1", T("16:00:00.000"), T("16:30:00.000"), (50, 20000, 300000, 600000)),
              Msg(f"msg_{n}_a6_2", T("16:40:00.000"), T("17:10:00.000"), (50, 30000, 0, 1000000))]
        a7 = [Msg(f"msg_{n}_a7_1", T("17:30:00.000"), T("17:35:00.000"), (0, 1000, 0, 48900))]
        write_session(self.worktree_folder(f"t-{n}-add-the-widget"), OWN,
                      [user_entry(T("09:59:00.000"), branch), user_entry(T("17:40:00.000"), branch)], agents=[
                          (aid(n, 1), meta("harness:test-author", f"#{n} tests"), turns(a1, branch)),
                          (aid(n, 2), meta("harness:test-author", f"#{n} tests"), turns(a2, branch)),
                          (aid(n, 3), meta("harness:test-breaker", f"#{n} breaker"), turns(a3, branch)),
                          (aid(n, 4), meta("harness:coder", f"#{n} code"), turns(a4, branch)),
                          (aid(n, 5), meta("harness:reviewer", f"#{n} review"), turns(a5, branch)),
                          (aid(n, 6), meta("harness:coder", f"#{n} fix"), turns(a6, branch)),
                          (aid(n, 7), meta("harness:reviewer", f"#{n} fix"), turns(a7, branch)),
                      ], cost=cost_state(6300000, WIDGET_COST))
        # shared session on main: its orchestrator is left out, its cost-state too
        orch = [Msg(f"msg_{n}_s_1", T("14:50:00.000"), T("14:51:00.000"), (100, 70000, 600000, 5000000)),
                Msg(f"msg_{n}_s_2", T("15:10:00.000"), T("15:12:00.000"), (100, 30000, 0, 5000000), FABLE)]
        write_session(self.main_folder(), SHARED, turns(orch, "main"), agents=[
            (aid(n, 8), meta("harness:reviewer", f"#{n} review"), turns(r2, "main")),
        ], cost=cost_state(999000, {OPUS: (5, 5, 5, 9000000, 5)}))
        # another ticket's own session
        other = f"t/{n + 1}-other-ticket"
        write_session(self.main_folder(), OTHER, turns(
            [Msg(f"msg_{n}_x_1", T("18:00:00.000"), T("18:20:00.000"), (9, 9000, 90000, 900000))], other),
            agents=[(aid(n, 9), meta("harness:coder", f"#{n + 1} code"), turns(
                [Msg(f"msg_{n}_x_2", T("18:30:00.000"), T("18:50:00.000"), (9, 9000, 90000, 900000))], other))],
            cost=cost_state(4000000, {OPUS: (7, 7, 7, 7000000, 7)}))
        rows = sort_rows([
            row("tests", 1, "harness:test-author", OPUS, a1), row("tests", 2, "harness:test-author", OPUS, a2),
            row("breaker", 1, "harness:test-breaker", SONNET, a3),
            row("code", 1, "harness:coder", OPUS, a4[:1]), row("code", 1, "harness:coder", SONNET, a4[1:]),
            row("review", 1, "harness:reviewer", OPUS, a5), row("review", 2, "harness:reviewer", OPUS, r2),
            row("fix", 1, "harness:coder", OPUS, a6), row("fix", 2, "harness:reviewer", OPUS, a7)])
        sessions = [{"id": OWN, "own": True, "cost_state": True}, {"id": SHARED, "own": False, "cost_state": None}]
        return {"rows": rows, "session_totals": WIDGET_SESSION_TOTALS, "sessions": sessions}

    def own_session(self, n, slug, agents=(), orchestrator=(), cost=None, session_id=None, folder=None):
        """Ticket n's own session in its worktree folder: a user entry on t/n-<slug>, the orchestrator messages,
        and agents [(agentType, description, msgs)]."""
        branch = f"t/{n}-{slug}"
        entries = [user_entry(T("08:00:00.000"), branch)] + turns(orchestrator, branch)
        write_session(folder or self.worktree_folder(f"t-{n}-{slug}"),
                      session_id or "%08x-0000-4000-8000-%012x" % (n, n), entries,
                      agents=[(aid(n, 100 + k), meta(t, d), turns(m, branch)) for k, (t, d, m) in enumerate(agents)],
                      cost=cost)
        return branch

    def check_record(self, record, n, expected, cache, pr):
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual(record["ticket"], n)
        self.assertEqual(record["repo"], self.main)
        for key in CACHE_KEYS:
            self.assertEqual(record[key], cache[key] if cache else None, key)
        self.assertEqual(record["pr"], pr)
        self.assertEqual(record["rows"], expected["rows"])
        totals, by_model = sums(expected["rows"])
        self.assertEqual(record["totals"], totals)
        self.assertEqual(record["by_model"], by_model)
        self.assertEqual(record["session_totals"], expected["session_totals"])
        self.assertEqual(sorted(record["sessions"], key=lambda s: s["id"]),
                         sorted(expected["sessions"], key=lambda s: s["id"]))
        self.assertEqual(record["rounds"], expected.get("rounds"))
        for key in TASK6_KEYS:
            self.assertIsNone(record[key], key)
        self.assertIsInstance(record["coverage"], str)
        self.assertRegex(record["coverage"], r"(?i)lower bound")

    def assertComputedAt(self, record, before, after):
        self.assertRegex(record["computed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        when = dt.datetime.strptime(record["computed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertLessEqual(before.replace(microsecond=0), when)
        self.assertLessEqual(when, after)

    @staticmethod
    def markdown_lines(text):
        return [line for line in text.splitlines() if line.strip()]

    # ----------------------------------------------------------- broad: the three outputs
    def test_prints_the_stage_by_model_table_and_saves_the_record(self):
        expected = self.widget_fixture(12)
        cache = {"title": "Add the widget", "type": "task", "size": "M", "risk": "low", "tier": "opus"}
        self.write_cache(12, attempts=WIDGET_ATTEMPTS, **{("type_" if k == "type" else k): v for k, v in cache.items()})
        # an earlier record is replaced, not merged
        write(self.record_path(12), json.dumps({"ticket": 12, "stale": True, "rows": [{"stage": "old"}]}))
        before = dt.datetime.now(dt.timezone.utc)
        out = self.cost(12)
        after = dt.datetime.now(dt.timezone.utc)

        lines = out.splitlines()
        cells = [line.split() for line in lines]
        at = []
        for r in WIDGET_ROWS:
            self.assertIn(list(r), cells, f"no line for {r} in:\n{out}")
            at.append(cells.index(list(r)))
        total = ["total", *WIDGET_TOTAL]
        self.assertIn(total, cells, out)
        at.append(cells.index(total))
        self.assertEqual(at, sorted(at), f"rows out of stage order:\n{out}")

        session = [line for line in lines if "Session total" in line]
        self.assertEqual(len(session), 1, out)
        for piece in ("23.7M tokens", "306.0k output", "3.0k thinking", "1h45m API time"):
            self.assertIn(piece, session[0])
        rounds = [line for line in lines if "re-rounds" in line]
        self.assertEqual(len(rounds), 1, out)
        for piece in ("breaker re-rounds 1", "fix rounds 2", "retries 3", "escalations 1", "needs-input 0"):
            self.assertIn(piece, rounds[0])
        not_counted = [line for line in lines if "Not counted" in line]
        self.assertEqual(len(not_counted), 1, out)
        self.assertIn(SHARED[:8], not_counted[0])
        self.assertNotIn(OWN[:8], not_counted[0])
        self.assertNotIn(OTHER[:8], out)
        self.assertNotRegex(out, r"(?i)no transcripts", "transcripts were found")

        record = self.read_record(12)
        self.assertNotIn("stale", record)
        self.check_record(record, 12, dict(expected, rounds=WIDGET_ROUNDS_RECORD), cache, None)
        self.assertComputedAt(record, before, after)
        self.assertIn(record["coverage"], out, "the lower-bound note")
        path = self.record_path(12)
        self.assertTrue(path in out or path.replace(self.home, "~", 1) in out, f"the saved path in:\n{out}")

        # a rerun after more work replaces the record again: a new #12 other agent shows up in it
        extra = [Msg("msg_12_more", T("19:00:00.000"), T("19:00:45.000"), (1, 98, 0, 900), HAIKU)]
        write_session(self.worktree_folder("t-12-add-the-widget"), "0a1b2c3d-1111-4000-8000-0000000000ff",
                      [user_entry(T("18:59:00.000"), "t/12-add-the-widget")],
                      agents=[(aid(12, 50), meta("general-purpose", "#12 other"),
                               turns(extra, "t/12-add-the-widget"))])
        out = self.cost(12)
        self.assertIn(["other", "haiku-5", "1", "999", "98", "45s", "45s"], [line.split() for line in out.splitlines()])
        again = self.read_record(12)
        self.assertEqual(again["rows"],
                         sort_rows(expected["rows"] + [row("other", 1, "general-purpose", HAIKU, extra)]))
        self.assertEqual(again["totals"]["total"], record["totals"]["total"] + 999)

    def test_json_prints_the_saved_record(self):
        expected = self.widget_fixture(47)
        cache = {"title": "Fix the flaky sweep", "type": "bug", "size": "S", "risk": "high", "tier": "fable"}
        self.write_cache(47, title=cache["title"], type_="bug", size="S", risk="high", tier="fable",
                         attempts=WIDGET_ATTEMPTS)
        before = dt.datetime.now(dt.timezone.utc)
        out = self.cost(47, "--json", "--pr", "34")
        after = dt.datetime.now(dt.timezone.utc)
        printed = json.loads(out)
        self.assertEqual(printed, self.read_record(47))
        self.check_record(printed, 47, dict(expected, rounds=WIDGET_ROUNDS_RECORD), cache, 34)
        self.assertComputedAt(printed, before, after)

        # another ticket, no --pr: pr is null, its own figures
        self.own_session(49, "small", agents=[("harness:coder", "#49 code", [
            Msg("msg_49_1", T("09:00:00.000"), T("09:00:10.000"), (1, 2, 3, 4), SONNET)])])
        self.write_cache(49, title="Small one", type_="chore", size="S", risk="low", tier="sonnet")
        printed = json.loads(self.cost(49, "--json"))
        self.assertEqual(printed, self.read_record(49))
        self.assertIsNone(printed["pr"])
        self.assertEqual((printed["ticket"], printed["title"], printed["type"], printed["tier"]),
                         (49, "Small one", "chore", "sonnet"))
        self.assertEqual(printed["totals"]["total"], 10)

    def test_markdown_is_the_resolution_block(self):
        self.widget_fixture(12)
        self.write_cache(12, attempts=WIDGET_ATTEMPTS)
        out = self.cost(12, "--markdown")
        self.assertEqual(out.strip("\n"), block(WIDGET_ROWS, WIDGET_TOTAL, WIDGET_SESSION, WIDGET_ROUNDS,
                                                WIDGET_NOT_COUNTED))
        self.assertTrue(os.path.isfile(self.record_path(12)), "--markdown saves the record too")
        self.assertEqual(self.read_record(12)["rows"][0]["stage"], "tests")

        # the number formats over a spread of values: one own session per ticket, one code row, no cache,
        # no cost-state, no shared session, so the block is the table alone
        base = dt.datetime(2026, 10, 21, 9, 0, 0)
        for i, (total, total_s, output, output_s, model_ms, model_s, wall_ms, wall_s, model, model_shown) in \
                enumerate(FORMAT_CASES):
            n = 301 + i
            with self.subTest(case=FORMAT_CASES[i]):
                a = model_ms // 2
                b = model_ms - a
                gap = wall_ms - b
                self.assertGreaterEqual(gap, a, "fixture: agent time below model time")
                rest = total - output - 1
                use = (1, output, rest // 2, rest - rest // 2)
                start = base + dt.timedelta(hours=i)
                msgs = [Msg(f"msg_{n}_1", stamp(start), stamp(start + dt.timedelta(milliseconds=a)), use, model),
                        Msg(f"msg_{n}_2", stamp(start + dt.timedelta(milliseconds=gap)),
                            stamp(start + dt.timedelta(milliseconds=gap + b)), (0, 0, 0, 0), model)]
                self.assertEqual(row("code", 1, "harness:coder", model, msgs)["total"], total)
                self.assertEqual(row("code", 1, "harness:coder", model, msgs)["wall_ms"], wall_ms)
                self.own_session(n, "fmt", agents=[("harness:coder", f"#{n} code", msgs)])
                out = self.cost(n, "--markdown")
                self.assertEqual(out.strip("\n"), block(
                    [("code", model_shown, "1", total_s, output_s, model_s, wall_s)], (total_s, output_s, model_s)))

    # ----------------------------------------------------------- rounds
    def test_rounds_replay_the_attempt_store(self):
        def tally(seq, answers):
            """The rounds as the real `harness-work attempt` answered them."""
            stages = {}
            for (stage, result), answer in zip(seq, answers):
                s = stages.setdefault(stage, stage_counts())
                s["runs"] += 1
                s["fails"] += result == "fail"
                s["retry"] += answer == "retry"
                s["escalate"] += answer.startswith("escalate")
                s["needs_input"] += answer == "needs-input"
            return {"stages": stages, "breaker_rerounds": stages.get("breaker", stage_counts())["fails"],
                    "fix_rounds": stages.get("fix", stage_counts())["runs"]}

        # #82's real sequence: the breaker failed three times with tests passes between, then code, review, ci
        seq82 = [("tests", "pass"), ("breaker", "fail"), ("tests", "pass"), ("breaker", "fail"), ("tests", "pass"),
                 ("breaker", "fail"), ("code", "pass"), ("review", "pass"), ("ci", "pass")]
        # order matters: a pass ends a run (code), fix never escalates, review's 4th fail is needs-input again
        seq83 = [("fix", "fail"), ("fix", "fail"), ("code", "fail"), ("code", "pass"), ("code", "fail"),
                 ("review", "fail"), ("review", "fail"), ("review", "fail"), ("review", "fail"), ("ci", "fail"),
                 ("tests", "pass")]
        cases = [
            (82, "opus", seq82, {"stages": {"tests": stage_counts(3), "breaker": stage_counts(3, 3, 1, 1, 1),
                                            "code": stage_counts(1), "review": stage_counts(1),
                                            "ci": stage_counts(1)},
                                 "breaker_rerounds": 3, "fix_rounds": 0},
             "Rounds: breaker re-rounds 3, fix rounds 0; retries 1, escalations 1, needs-input 1"),
            (83, "sonnet", seq83, {"stages": {"fix": stage_counts(2, 2, retry=1, needs_input=1),
                                              "code": stage_counts(3, 2, retry=2),
                                              "review": stage_counts(4, 4, 1, 1, 2), "ci": stage_counts(1, 1, 1),
                                              "tests": stage_counts(1)},
                                   "breaker_rerounds": 0, "fix_rounds": 2},
             "Rounds: breaker re-rounds 0, fix rounds 2; retries 5, escalations 1, needs-input 3"),
        ]
        for n, tier, seq, expected, line in cases:
            with self.subTest(ticket=n):
                self.own_session(n, "rounds", agents=[("harness:coder", f"#{n} code", [
                    Msg(f"msg_{n}_1", T("09:00:00.000"), T("09:00:45.000"), (1, 98, 0, 900))])])
                self.write_cache(n, tier=tier)
                answers = [self.attempt(n, stage, result) for stage, result in seq]
                if n == 82:
                    self.assertEqual([a for (s, _), a in zip(seq, answers) if s == "breaker"],
                                     ["retry", "escalate fable", "needs-input"])
                self.assertEqual(tally(seq, answers), expected, "fixture: the attempt answers")
                record = json.loads(self.cost(n, "--json"))
                self.assertEqual(record["rounds"], expected)
                md = self.cost(n, "--markdown")
                self.assertEqual(md.strip("\n"), block([("code", "opus-5-5", "1", "999", "98", "45s", "45s")],
                                                       ("999", "98", "45s"), line))
                text = self.cost(n)
                rounds = [x for x in text.splitlines() if "re-rounds" in x]
                self.assertEqual(len(rounds), 1, text)
                for piece in line[len("Rounds: "):].replace(";", ",").split(", "):
                    self.assertIn(piece, rounds[0])

    # ----------------------------------------------------------- sessions
    def test_session_total_line_only_for_own_sessions(self):
        # 21: only a shared session, with a cost-state of its own: no session total
        write_session(self.main_folder(), "21212121-0000-4000-8000-000000000021",
                      turns([Msg("msg_21_o", T("08:00:00.000"), T("08:01:00.000"), (1, 1, 1, 1))], "main"), agents=[
                          (aid(21, 1), meta("harness:coder", "#21 code"), turns(
                              [Msg("msg_21_1", T("08:10:00.000"), T("08:10:30.000"), (1, 2, 3, 4))], "main"))],
                      cost=cost_state(7200000, {OPUS: (9, 9, 9, 50000000, 9)}))
        self.write_cache(21)
        # 22: two own sessions with a cost-state each, summed; the shared one's is left out
        shared_cost = cost_state(7200000, {OPUS: (9, 9, 9, 50000000, 9)})
        oa = [Msg("msg_22_oa", T("09:00:00.000"), T("09:00:30.000"), (10, 2000, 0, 10000))]
        ob = [Msg("msg_22_ob", T("10:00:00.000"), T("10:00:15.000"), (0, 1000, 0, 0))]
        code = [Msg("msg_22_c", T("09:10:00.000"), T("09:10:40.000"), (5, 500, 0, 495))]
        review = [Msg("msg_22_r", T("11:00:00.000"), T("11:00:20.000"), (2, 200, 0, 798), SONNET)]
        self.own_session(22, "totals", orchestrator=oa, agents=[("harness:coder", "#22 code", code)],
                         session_id="22aa0000-0000-4000-8000-000000000022",
                         cost=cost_state(1800000, {OPUS: (1000, 100000, 500, 5000000, 1000000)}))
        self.own_session(22, "totals", orchestrator=ob, session_id="22bb0000-0000-4000-8000-000000000022",
                         folder=self.worktree_folder("t-22-totals-resumed"),
                         cost=cost_state(2100000, {OPUS: (2000, 200000, 1000, 3000000, 1000000),
                                                   HAIKU: (100, 1000, 0, 200000, 99900)}))
        write_session(self.main_folder(), "22cc0000-0000-4000-8000-000000000022",
                      turns([Msg("msg_22_so", T("10:30:00.000"), T("10:31:00.000"), (1, 1, 1, 1))], "main"), agents=[
                          (aid(22, 1), meta("harness:reviewer", "#22 review"), turns(review, "main"))],
                      cost=shared_cost)
        self.write_cache(22)
        # 23: an own session without a cost-state: no session total
        self.own_session(23, "unended", orchestrator=[
            Msg("msg_23_o", T("09:00:00.000"), T("09:00:05.000"), (1, 1, 1, 1))])
        self.write_cache(23)

        for n in (21, 23):
            with self.subTest(ticket=n):
                text, md = self.cost(n), self.cost(n, "--markdown")
                self.assertNotRegex(text, r"(?i)session total")
                self.assertNotRegex(md, r"(?i)session total")
                self.assertIsNone(self.read_record(n)["session_totals"])

        md = self.cost(22, "--markdown")
        self.assertIn("Session total (Claude Code's count): 10.6M tokens, 301.0k output, 1.5k thinking, "
                      "1h05m API time", self.markdown_lines(md))
        record = self.read_record(22)
        self.assertEqual(record["session_totals"], {
            "models": {OPUS: {"input": 3000, "output": 300000, "thinking": 1500, "cache_read": 8000000,
                              "cache_write": 2000000},
                       HAIKU: {"input": 100, "output": 1000, "thinking": 0, "cache_read": 200000,
                               "cache_write": 99900}},
            "api_ms": 3900000, "sessions": 2})
        self.assertEqual(record["rows"], sort_rows([row("code", 1, "harness:coder", OPUS, code),
                                                    row("review", 1, "harness:reviewer", SONNET, review),
                                                    row("orchestrator", 1, "main", OPUS, oa + ob)]))
        text = self.cost(22)
        session = [x for x in text.splitlines() if "Session total" in x]
        self.assertEqual(len(session), 1, text)
        for piece in ("10.6M tokens", "301.0k output", "1.5k thinking", "1h05m API time"):
            self.assertIn(piece, session[0])
        # the own sessions' orchestrator is a row of its own: 13,010 tokens, 3,000 output, 45 s
        orchestrator = [x.split() for x in text.splitlines() if x.split()[:2] == ["orchestrator", "opus-5-5"]]
        self.assertEqual(len(orchestrator), 1, text)
        cells = orchestrator[0]
        self.assertLess(cells.index("13.0k"), cells.index("3.0k"))
        self.assertLess(cells.index("3.0k"), cells.index("45s"))

    def test_shared_session_orchestrator_is_named_not_counted(self):
        first, second, unrelated, own = ("4b318caa-5555-4000-8000-000000000031", "77ee99aa-6666-4000-8000-000000000031",
                                         "c0ffee00-7777-4000-8000-000000000031", "0d0d0d0d-8888-4000-8000-000000000031")
        code = [Msg("msg_31_c", T("09:10:00.000"), T("09:10:50.000"), (5, 500, 0, 4495))]
        review = [Msg("msg_31_r", T("09:40:00.000"), T("09:40:10.000"), (1, 99, 0, 900), SONNET)]
        orch_own = [Msg("msg_31_o", T("10:00:00.000"), T("10:00:20.000"), (2, 300, 0, 698))]
        write_session(self.main_folder(), first, turns([
            Msg("msg_31_s1", T("09:00:00.000"), T("09:05:00.000"), (100, 50000, 0, 5000000))], "main"),
            agents=[(aid(31, 1), meta("harness:coder", "#31 code"), turns(code, "main"))],
            cost=cost_state(3000000, {OPUS: (1, 1, 1, 1, 1)}))
        write_session(self.main_folder(), second, turns([
            Msg("msg_31_s2", T("09:30:00.000"), T("09:31:00.000"), (1, 1000, 0, 2000000))], "main"),
            agents=[(aid(31, 2), meta("harness:reviewer", "#31 review"), turns(review, "main"))])
        write_session(self.main_folder(), unrelated, turns([
            Msg("msg_31_s3", T("09:50:00.000"), T("09:51:00.000"), (1, 1000, 0, 2000000))], "main"),
            agents=[(aid(31, 3), meta("harness:coder", "#32 code"), turns(
                [Msg("msg_31_z", T("09:52:00.000"), T("09:53:00.000"), (1, 1, 1, 1))], "main"))])
        self.own_session(31, "shared", orchestrator=orch_own, session_id=own)
        self.write_cache(31)
        # 33: only its own session, nothing left out
        self.own_session(33, "alone", agents=[("harness:coder", "#33 code", [
            Msg("msg_33_c", T("09:00:00.000"), T("09:00:10.000"), (1, 1, 1, 1))])])
        self.write_cache(33)

        rows = sort_rows([row("code", 1, "harness:coder", OPUS, code),
                          row("review", 1, "harness:reviewer", SONNET, review),
                          row("orchestrator", 1, "main", OPUS, orch_own)])
        for out in (self.cost(31), self.cost(31, "--markdown")):
            named = [x for x in out.splitlines() if "Not counted" in x]
            self.assertEqual(len(named), 1, out)
            if out.lstrip().startswith("**Cost**"):
                self.assertTrue(named[0].startswith("Not counted:"), named[0])
            for sid_ in (first, second):
                self.assertIn(sid_[:8], named[0])
            for sid_ in (unrelated, own):
                self.assertNotIn(sid_[:8], named[0])
        record = self.read_record(31)
        self.assertEqual(record["rows"], rows)
        self.assertEqual(record["totals"]["total"], 7000)
        self.assertEqual(sorted((s["id"], s["own"]) for s in record["sessions"]),
                         sorted([(first, False), (second, False), (own, True)]))
        for out in (self.cost(33), self.cost(33, "--markdown")):
            self.assertNotIn("Not counted", out)

    # ----------------------------------------------------------- missing inputs
    def test_without_transcripts_the_record_is_empty_and_says_so(self):
        self.write_cache(41, title="Nothing ran yet", attempts=[("tests", "pass")])
        self.assertFalse(os.path.exists(self.projects))
        out = self.cost(41)
        self.assertRegex(out, r"(?i)no transcripts")
        record = self.read_record(41)
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual((record["ticket"], record["title"]), (41, "Nothing ran yet"))
        self.assertEqual(record["rows"], [])
        self.assertEqual(record["sessions"], [])
        self.assertEqual(record["by_model"], {})
        self.assertIsNone(record["session_totals"])
        self.assertEqual(record["totals"], {k: 0 for k in TOTAL_KEYS})
        # the ticket cache is still read: its labels and its rounds
        self.assertEqual((record["type"], record["size"], record["risk"], record["tier"]), ("task", "M", "low", "opus"))
        self.assertEqual(record["rounds"], {"stages": {"tests": stage_counts(1)}, "breaker_rerounds": 0,
                                            "fix_rounds": 0})
        path = self.record_path(41)
        self.assertTrue(path in out or path.replace(self.home, "~", 1) in out, f"the saved path in:\n{out}")
        self.assertEqual(json.loads(self.cost(41, "--json"))["rows"], [])

    def test_without_a_ticket_cache_labels_and_rounds_are_null(self):
        tests = [Msg("msg_51_t", T("09:00:00.000"), T("09:12:00.000"), (4, 1200, 0, 10796))]
        orch = [Msg("msg_51_o", T("08:30:00.000"), T("08:30:45.000"), (1, 99, 0, 900), SONNET)]
        self.own_session(51, "nocache", orchestrator=orch, agents=[("harness:test-author", "#51 tests", tests)])
        self.assertFalse(os.path.exists(self.state("tickets", "51.json")))
        text = self.cost(51)
        self.assertNotIn("re-rounds", text)
        self.assertIn(["tests", "opus-5-5", "1", "12.0k", "1.2k", "12m", "12m"],
                      [x.split() for x in text.splitlines()])
        record = self.read_record(51)
        self.check_record(record, 51, {"rows": sort_rows([row("tests", 1, "harness:test-author", OPUS, tests),
                                                          row("orchestrator", 1, "main", SONNET, orch)]),
                                       "session_totals": None,
                                       "sessions": [{"id": "%08x-0000-4000-8000-%012x" % (51, 51), "own": True,
                                                     "cost_state": False}],
                                       "rounds": None}, None, None)
        md = self.cost(51, "--markdown")
        lines = md.strip("\n").splitlines()
        self.assertEqual(lines[:4], [HEADER, "", TABLE_HEAD, TABLE_RULE])
        self.assertIn("| tests | opus-5-5 | 1 | 12.0k | 1.2k | 12m | 12m |", lines)
        self.assertFalse([x for x in lines if x.startswith("Rounds")], md)

    # ----------------------------------------------------------- tokens only
    def test_no_dollar_figures_anywhere(self):
        self.widget_fixture(12)
        self.write_cache(12, attempts=WIDGET_ATTEMPTS)
        outputs = {"text": self.cost(12), "json": self.cost(12, "--json"), "markdown": self.cost(12, "--markdown")}
        with open(self.record_path(12), encoding="utf-8") as fh:
            outputs["record"] = fh.read()
        for name, text in outputs.items():
            scrubbed = text.replace(self.tmp, "<tmp>")
            self.assertNotRegex(scrubbed, MONEY, name)
            for figure in DOLLARS:
                self.assertNotIn(figure, scrubbed, name)
        for key in walk_keys(json.loads(outputs["record"])):
            self.assertNotRegex(key, r"(?i)usd|price|dollar", key)

    # ----------------------------------------------------------- bad invocations
    def test_bad_invocations_are_one_line(self):
        self.widget_fixture(12)
        self.write_cache(12, attempts=WIDGET_ATTEMPTS)
        for args, needles in ((("cost", "12", "--json", "--markdown"), ("--json", "--markdown")),
                              (("cost", "12", "--markdown", "--json"), ("--json", "--markdown")),
                              (("cost",), ()),
                              (("cost", "--json"), ())):
            with self.subTest(args=args):
                done = self.work(*args, "--repo", self.repo)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(done.stdout, "")
                lines = [x for x in done.stderr.splitlines() if x.strip()]
                self.assertEqual(len(lines), 1, done.stderr)
                self.assertNotIn("invalid choice", lines[0], "the cost subcommand exists")
                for needle in needles:
                    self.assertIn(needle, lines[0])
                self.assertFalse(os.path.exists(self.record_path(12)), "a bad invocation saves nothing")
        # and the good ones next to them work
        self.assertEqual(json.loads(self.cost(12, "--json"))["ticket"], 12)
        self.assertTrue(self.cost(12, "--markdown").startswith(HEADER))


if __name__ == "__main__":
    unittest.main()
