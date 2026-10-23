"""Tests for harness_lib/cost.py (ticket #110, epic #108): transcript usage attributed to a ticket.

ticket_usage(main, number, dirs=None) -> {"rows", "totals", "by_model", "session_totals", "sessions"}:
- N's own session (its main transcript's branches other than HEAD non-empty and all `^t/N-`):
  every message counts, orchestrator and subagents alike, except a subagent whose description
  names another ticket.
- An orchestrator message counts when its own branch is `^t/N-`, in any session.
- A subagent's ticket: the number in its meta description `^#(\\d+)\\b` (leading spaces ignored),
  else in its first_branch `^t/(\\d+)-`, else none. Its agent is meta agentType, or `unknown`.
- Stage: the word after `#N` when it is tests, breaker, code, review or fix; else the agent type
  after its last `:` (test-author tests, test-breaker breaker, coder code, reviewer review,
  anything else other); orchestrator messages are orchestrator (agent `main`, round 1).
- One row per stage, round, agent and model; rounds number a stage's agents by first start from
  1; rows sort by stage (tests, breaker, code, review, fix, other, orchestrator), round, agent,
  model; total = input + output + cache_write + cache_read; model_ms the sum of each message's
  end minus start (never negative); wall_ms the row's latest end minus earliest start, None on
  orchestrator rows.
- A continued agent's transcript is split into segments at each coordinator `user` entry (ticket #111):
  the ticket's first review segment is the earliest-starting segment whose stage is review, its end the
  latest end among its messages; every code or review segment starting after that end is fix, except
  segment 1 of an agent whose description names its stage; rounds number a stage's segments by start;
  each segment's model_ms and wall_ms leave out the wait before the next continuation.
- One message id across files counts once: each usage field the largest, the earliest start,
  the latest end.
- session_totals sums the cost-state of N's own sessions that have one; None when none has.
- No dollar figure anywhere.

Every fixture is written here, in a temporary HOME; the real ~/.claude is never read. Expected
values are written out by hand or computed by the helpers below from the same message specs the
fixture writers wrote, never by the code under test. The generated test draws a fresh seed per
run (replay with HARNESS_TEST_SEED).

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_cost.py" -v
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-5"
FABLE = "claude-fable-5"
MODELS = (OPUS, SONNET, HAIKU, FABLE)

B12 = "t/12-add-harness-lib-cost-py"
B13 = "t/13-other-ticket"
B120 = "t/120-prefix-lookalike"
B1 = "t/1-short"

STAGES = ("tests", "breaker", "code", "review", "fix", "other", "orchestrator")
STAGE_WORDS = STAGES[:5]
TYPE_STAGE = {"test-author": "tests", "test-breaker": "breaker", "coder": "code", "reviewer": "review"}
ROW_KEYS = {"stage", "round", "agent", "model", "input", "output", "cache_write", "cache_read",
            "total", "messages", "model_ms", "wall_ms"}
TOTAL_KEYS = ("input", "output", "cache_write", "cache_read", "total", "messages", "model_ms")

SEED_ENV = "HARNESS_TEST_SEED"
RANDOM_RUNS = 6


def load_cost() -> types.ModuleType:
    """harness_lib.cost imported from the checkout's lib/; the import error while the module is
    missing is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.cost")


def encode(path):
    """Claude Code's session folder name for a project path, from first principles."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def seeds():
    fixed = os.environ.get(SEED_ENV, "").strip()
    if fixed:
        return [int(fixed)]
    rng = random.SystemRandom()
    return [rng.randrange(1 << 32) for _ in range(RANDOM_RUNS)]


def replay(seed):
    return (f"seed {seed}; replay with: {SEED_ENV}={seed} python3 -m unittest discover "
            f"-s plugins/harness/tests -p test_harness_lib_cost.py")


# ------------------------------------------------------------------ time
def T(clock, day=20):
    """A Claude Code timestamp on 2026-10-<day>: T("10:00:03.500")."""
    return f"2026-10-{day:02d}T{clock}Z"


def ms(start, end):
    """Whole milliseconds from start to end (negative when end is earlier)."""
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    d = dt.datetime.strptime(end, fmt) - dt.datetime.strptime(start, fmt)
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


def parse(ts):
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")


def shift(ts, millis):
    """A timestamp `millis` later (earlier when negative), in the same form."""
    t = parse(ts) + dt.timedelta(milliseconds=millis)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (t.microsecond // 1000)


def sid(n):
    return f"5e55{n:04x}-0000-4000-8000-{n:012x}"


# ------------------------------------------------------------------ fixture writers
# One API response: written as a user entry at `start` then an assistant entry at `end`, so
# read_transcript gives it exactly that start and end. `branch` overrides the transcript's.
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


def assistant_entry(ts, mid, use, model, branch, ref=None):
    entry = {"type": "assistant", "timestamp": ts, "requestId": "req_" + mid[-6:],
             "message": {"id": mid, "model": model, "usage": usage(*use), "content": [{"type": "text"}]}}
    if branch is not None:
        entry["gitBranch"] = branch
    if ref is not None:
        entry["requestRef"] = ref
    return entry


def api_request(ts, rid, branch):
    return {"type": "api-request", "id": rid, "timestamp": ts, "gitBranch": branch}


def turns(msgs, branch):
    out = []
    for m in msgs:
        b = m.branch or branch
        out.append(user_entry(m.start, b))
        out.append(assistant_entry(m.end, m.mid, m.use, m.model, b))
    return out


def cost_state(api_ms, models, total_cost=31.75):
    """The last line of an ended session: models {model: (input, output, thinking, cache_read,
    cache_write)}, each with a costUSD that must never reach the result."""
    return {"type": "cost-state", "totalCostUSD": total_cost, "totalAPIDuration": api_ms,
            "totalDuration": api_ms * 3,
            "modelUsage": {model: {"inputTokens": v[0], "outputTokens": v[1], "thinkingTokens": v[2],
                                   "cacheReadInputTokens": v[3], "cacheCreationInputTokens": v[4],
                                   "costUSD": 1.25 + i}
                           for i, (model, v) in enumerate(models.items())}}


def write_jsonl(path, entries):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")


def write_session(folder, session_id, entries, agents=(), cost=None):
    """<folder>/<id>.jsonl (cost-state last when given) and each agent (aid, meta, entries) as
    <folder>/<id>/subagents/agent-<aid>.jsonl with its .meta.json (none when meta is None; a str
    is written as the file's text)."""
    folder = Path(folder)
    write_jsonl(folder / f"{session_id}.jsonl", list(entries) + ([cost] if cost is not None else []))
    for aid, meta, agent_entries in agents:
        sub = folder / session_id / "subagents"
        write_jsonl(sub / f"agent-{aid}.jsonl", agent_entries)
        if meta is not None:
            (sub / f"agent-{aid}.meta.json").write_text(
                meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")


def meta(agent_type=None, description=None):
    value = {"toolUseId": "toolu_01"}
    if agent_type is not None:
        value["agentType"] = agent_type
    if description is not None:
        value["description"] = description
    return value


# ------------------------------------------------------------------ expected values
def row(stage, rnd, agent, model, msgs):
    """The expected row for these message specs, summed here from the specs themselves."""
    i = sum(m.use[0] for m in msgs)
    o = sum(m.use[1] for m in msgs)
    w = sum(m.use[2] for m in msgs)
    r = sum(m.use[3] for m in msgs)
    wall = None
    if stage != "orchestrator":
        first = min(msgs, key=lambda m: dt.datetime.strptime(m.start, "%Y-%m-%dT%H:%M:%S.%fZ")).start
        last = max(msgs, key=lambda m: dt.datetime.strptime(m.end, "%Y-%m-%dT%H:%M:%S.%fZ")).end
        wall = ms(first, last)
    return {"stage": stage, "round": rnd, "agent": agent, "model": model, "input": i, "output": o,
            "cache_write": w, "cache_read": r, "total": i + o + w + r, "messages": len(msgs),
            "model_ms": sum(max(0, ms(m.start, m.end)) for m in msgs), "wall_ms": wall}


def sort_rows(rows):
    return sorted(rows, key=lambda x: (STAGES.index(x["stage"]), x["round"], x["agent"], x["model"]))


def sums(rows):
    """(totals, by_model) of expected rows."""
    totals = {k: 0 for k in TOTAL_KEYS}
    by_model = {}
    for x in rows:
        per = by_model.setdefault(x["model"], {k: 0 for k in TOTAL_KEYS})
        for k in TOTAL_KEYS:
            totals[k] += x[k]
            per[k] += x[k]
    return totals, by_model


def walk(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield ("key", k)
            yield from walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from walk(v)
    else:
        yield ("value", value)


# ------------------------------------------------------------------ generated tickets
class Generated:
    """Sessions written from a seed, with the result ticket_usage must return recorded as each
    session is written, by the rules in the module docstring."""

    TYPES = ("harness:test-author", "harness:test-breaker", "harness:coder", "harness:reviewer",
             "Explore", "general-purpose", "other-plugin:coder", None)
    WORDS = STAGE_WORDS + ("lunch", "other", "orchestrator")

    def __init__(self, seed, root):
        r = self.rng = random.Random(seed)
        self.n = n = r.choice((r.randint(2, 9), r.randint(10, 99), r.randint(100, 999)))
        others = (n * 10 + r.randint(0, 9), n + r.randint(1, 50))
        self.clock = dt.datetime(2026, r.randint(1, 12), r.randint(1, 28), r.randint(0, 23), r.randint(0, 59))
        self.dirs = [str(root / "gen-a"), str(root / "gen-b")]
        # (agent key, stage, agent, Msg, description names the stage, segment number; 0: orchestrator)
        counted = []
        self.sessions = {}                # id -> (own, cost_state flag or None)
        totals = None
        for _ in range(r.randint(2, 5)):
            # mixed: on t/N- but with one entry on t/M-, so not N's own
            kind = r.choice(("own", "own", "other", "shared", "mixed"))
            m = r.choice(others)
            slug = "".join(r.choice("abcdefghij") for _ in range(r.randint(3, 9)))
            branch = {"own": f"t/{n}-{slug}", "other": f"t/{m}-{slug}", "shared": "main",
                      "mixed": f"t/{n}-{slug}"}[kind]
            session = self.session_id()
            folder = r.choice(self.dirs)
            entries = [user_entry(self.stamp(), branch)]
            contributed = False
            pick = {"own": (branch, branch, branch, "HEAD"), "other": (branch,),
                    "shared": ("main", "main", f"t/{n}-{slug}", f"t/{m}-{slug}"),
                    "mixed": (branch, branch, "HEAD")}[kind]
            orch = [r.choice(pick) for _ in range(r.randint(1 if kind == "own" else 0, 3))]
            if kind == "mixed":
                orch.insert(r.randint(0, len(orch)), f"t/{m}-{slug}")
            resumed = []                  # copies of this session's messages for a resumed session
            for i, b in enumerate(orch):
                msg = self.msg(f"msg_{session}_o{i}", b)
                if r.random() < 0.25:
                    # its api-request stamped after the response: it starts there, model time 0
                    late = shift(msg.end, r.randint(1, 60000))
                    entries += [user_entry(msg.start, b), api_request(late, f"req-{session}-{i}", b),
                                assistant_entry(msg.end, msg.mid, msg.use, msg.model, b, ref=f"req-{session}-{i}")]
                    msg = msg._replace(start=late)
                else:
                    entries += turns([msg], branch)
                if kind == "own" or b.startswith(f"t/{n}-"):
                    if kind == "own" and r.random() < 0.35:
                        # the same id in another file, other usage and times: counted once, per
                        # field the largest, the earliest start, the latest end
                        copy = Msg(msg.mid, shift(msg.start, r.randint(-5000, 5000)),
                                   shift(msg.end, r.randint(-5000, 5000)),
                                   tuple(max(0, x + r.randint(-3000, 3000)) for x in msg.use), msg.model, None)
                        resumed.append(copy)
                        msg = Msg(msg.mid, min(msg.start, copy.start, key=parse), max(msg.end, copy.end, key=parse),
                                  tuple(max(a, c) for a, c in zip(msg.use, copy.use)), msg.model, b)
                    counted.append(("main", "orchestrator", "main", msg, False, 0))
                    contributed = True
            if resumed:
                copy_id = self.session_id()
                write_session(self.dirs[1 - self.dirs.index(folder)], copy_id,
                              turns(resumed, f"t/{n}-{slug}-resumed"))
                self.sessions[copy_id] = (True, False)
            agents = []
            for j in range(r.randint(0, 3)):
                aid = "%017x" % r.getrandbits(68)
                choice = r.randrange(7)
                word = r.choice(self.WORDS)
                if choice == 0:
                    desc, ticket = f"#{n} {word}", n
                elif choice == 1:
                    desc, ticket, word = f"#{n}", n, None
                elif choice == 2:
                    desc, ticket = f"  #{n} {word}", n
                elif choice == 3:
                    desc, ticket = f"#{m} {word}", m
                else:
                    desc = r.choice(("Write the tests for the ticket", "", None, f"Implement ticket {n}"))
                    word = None
                    # in a mixed session the subagent's first branch, t/N-, decides
                    ticket = n if kind in ("own", "mixed") else None
                atype = r.choice(self.TYPES)
                value = meta(atype, desc)
                if desc is None and atype is None and r.random() < 0.5:
                    value = None
                # a continued agent: a coordinator entry between some of its messages starts a new
                # segment; seg_of[k] is message k's segment number (1 for an agent never continued)
                msgs = [self.msg(f"msg_{aid}_{k}", None) for k in range(r.randint(1, 4))]
                seg_of, agent_entries, seg = [], [], 1
                for k, x in enumerate(msgs):
                    if k and r.random() < 0.4:
                        agent_entries.append(coordinator_entry(shift(x.start, -1), branch))
                        seg += 1
                    seg_of.append(seg)
                    agent_entries += turns([x], branch)
                agents.append((aid, value, agent_entries))
                if ticket == n:
                    stage = word if word in STAGE_WORDS else TYPE_STAGE.get((atype or "").rsplit(":", 1)[-1], "other")
                    counted += [((session, aid), stage, atype or "unknown", x, word in STAGE_WORDS, g)
                                for x, g in zip(msgs, seg_of)]
                    contributed = True
            cost, models = None, {}
            if r.random() < 0.5:
                models = {mod: tuple(r.randint(0, 90000) for _ in range(5))
                          for mod in r.sample(MODELS, r.randint(1, 3))}
                cost = cost_state(r.randint(1000, 9000000), models)
                if kind == "own":
                    totals = totals or {"models": {}, "api_ms": 0, "sessions": 0}
                    totals["api_ms"] += cost["totalAPIDuration"]
                    totals["sessions"] += 1
                    for mod, v in models.items():
                        per = totals["models"].setdefault(mod, {"input": 0, "output": 0, "thinking": 0,
                                                                "cache_read": 0, "cache_write": 0})
                        for key, x in zip(("input", "output", "thinking", "cache_read", "cache_write"), v):
                            per[key] += x
            if contributed:
                self.sessions[session] = (kind == "own", (cost is not None) if kind == "own" else None)
            write_session(folder, session, entries, agents, cost)
        self.session_totals = totals
        self.rows = self.expected_rows(counted)

    def session_id(self):
        r = self.rng
        return "%08x-%04x-4000-8000-%012x" % (r.getrandbits(32), r.getrandbits(16), r.getrandbits(48))

    def stamp(self):
        self.clock += dt.timedelta(milliseconds=self.rng.randint(1, 90000))
        c = self.clock
        return c.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (c.microsecond // 1000)

    def msg(self, mid, branch):
        r = self.rng
        start = self.stamp()
        return Msg(mid, start, self.stamp(), tuple(r.randint(0, 50000) for _ in range(4)), r.choice(MODELS), branch)

    @staticmethod
    def expected_rows(counted):
        """A continued agent is split at its coordinator entries into segments, each a unit of its
        own: the ticket's first review is the earliest-starting review segment; every code or
        review segment starting after its last end is a fix round, except segment 1 of an agent
        whose description names its stage; a stage's rounds number its segments by first start."""
        first, last, named, stage_of = {}, {}, {}, {}
        for key, stage, _, m, is_named, seg in counted:
            if key != "main":
                unit = (key, seg)
                first[unit] = min(first.get(unit, m.start), m.start, key=parse)
                last[unit] = max(last.get(unit, m.end), m.end, key=parse)
                named[unit] = is_named
                stage_of[unit] = stage
        reviews = [u for u, s in stage_of.items() if s == "review"]
        if reviews:
            end = parse(last[min(reviews, key=lambda u: parse(first[u]))])
            for u, s in list(stage_of.items()):
                if s in ("code", "review") and not (named[u] and u[1] == 1) and parse(first[u]) > end:
                    stage_of[u] = "fix"
        rounds = {}
        for stage in STAGES:
            units = sorted((u for u, s in stage_of.items() if s == stage), key=lambda u: parse(first[u]))
            for i, u in enumerate(units, 1):
                rounds[u] = i
        groups = {}
        for key, stage, agent, m, _, seg in counted:
            if key == "main":
                rnd = 1
            else:
                stage, rnd = stage_of[(key, seg)], rounds[(key, seg)]
            groups.setdefault((stage, rnd, agent, m.model), []).append(m)
        return sort_rows([row(s, rnd, a, mod, msgs) for (s, rnd, a, mod), msgs in groups.items()])


# ------------------------------------------------------------------ tests
class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-cost-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".claude" / "projects").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.main = str(self.tmp / "repo")
        self.c = load_cost()

    def folder(self, name):
        path = self.tmp / "projects" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def usage_of(self, number, dirs, main=None):
        result = self.c.ticket_usage(main or self.main, number, dirs)
        self.assert_no_dollars(result)
        return result

    def assert_no_dollars(self, result, msg=None):
        for kind, item in walk(result):
            if kind == "key":
                self.assertNotRegex(str(item), r"(?i)cost(?!_state)|usd|dollar|price|\$", msg or "a dollar key")
            else:
                self.assertNotIsInstance(item, float, msg or "a float value: only dollars are floats")
        text = json.dumps(result)
        for word in ("costUSD", "totalCostUSD", "totalDuration", "31.75", "1.25"):
            self.assertNotIn(word, text, msg)

    def check(self, result, rows, sessions, session_totals=None, msg=None):
        """rows exactly (in order), totals and by_model summed from them, session_totals, and the
        contributing sessions: {id: (own, cost_state flag)}; the flag is checked on own ones."""
        for x in result["rows"]:
            self.assertEqual(set(x), ROW_KEYS, msg)
        self.assertEqual(result["rows"], rows, msg)
        totals, by_model = sums(rows)
        self.assertEqual(result["totals"], totals, msg)
        self.assertEqual(result["by_model"], by_model, msg)
        self.assertEqual(result["session_totals"], session_totals, msg)
        listed = [s["id"] for s in result["sessions"]]
        self.assertEqual(len(listed), len(set(listed)), msg or "a session listed twice")
        self.assertEqual(sorted(listed), sorted(sessions), msg)
        for s in result["sessions"]:
            own, flag = sessions[s["id"]]
            self.assertIs(s["own"], own, msg or s["id"])
            if own:
                self.assertEqual(s, {"id": s["id"], "own": True, "cost_state": flag}, msg)


class TicketUsage(Base):

    # ---------------------------------------------------------- broad: the named rules
    def test_dispatched_session_belongs_wholly_to_its_ticket(self):
        d = self.folder("own")
        orch = [Msg("msg_o1", T("10:00:00.000"), T("10:00:03.500"), (3, 120, 4000, 0)),
                Msg("msg_o2", T("10:00:10.000"), T("10:00:12.250"), (1, 80, 0, 4000)),
                # a detached HEAD entry in N's own session still counts
                Msg("msg_o3", T("11:00:00.000"), T("11:00:01.000"), (2, 10, 100, 4100), SONNET, "HEAD")]
        author = [Msg("msg_t1", T("10:01:00.000"), T("10:01:05.000"), (2, 1500, 9000, 0)),
                  Msg("msg_t2", T("10:01:06.000"), T("10:01:20.000"), (1, 3000, 200, 9000))]
        coder = [Msg("msg_c1", T("10:30:00.000"), T("10:30:30.000"), (5, 7000, 12000, 0)),
                 Msg("msg_c2", T("10:31:00.000"), T("10:31:02.500"), (4, 250, 0, 12000))]
        review13 = [Msg("msg_r13", T("10:40:00.000"), T("10:40:09.000"), (6, 600, 6000, 60))]
        write_session(d, sid(1), turns(orch, B12), agents=[
            ("a1b2c3d4e5f600001", meta("harness:test-author", "Write the tests for cost.py"), turns(author, B12)),
            ("a1b2c3d4e5f600002", meta("harness:coder", "Implement cost.py"), turns(coder, B12)),
            # names another ticket: never N's, even in N's own session
            ("a1b2c3d4e5f600003", meta("harness:reviewer", "#13 review"), turns(review13, B12)),
        ])
        # ticket 13's own session and a prefix lookalike's (t/120-)
        # (13's coder starts before its review ends at 10:40:09, so it stays code, not fix)
        orch13 = [Msg("msg_x13", T("10:30:00.000"), T("10:30:02.000"), (9, 900, 90, 9000))]
        coder13 = [Msg("msg_y13", T("10:35:00.000"), T("10:35:04.000"), (8, 800, 80, 8000), HAIKU)]
        write_session(d, sid(2), turns(orch13, B13), agents=[
            ("b0000000000000001", meta("harness:coder", "Implement it"), turns(coder13, B13))])
        orch120 = [Msg("msg_x120", T("13:00:00.000"), T("13:00:05.000"), (7, 700, 70, 7000))]
        write_session(d, sid(3), turns(orch120, B120), agents=[
            ("c0000000000000001", meta("harness:coder", "Implement it"),
             turns([Msg("msg_y120", T("13:10:00.000"), T("13:10:01.000"), (1, 1, 1, 1))], B120))])

        got = self.usage_of(12, [d])
        # every figure written out by hand from the fixture above
        rows = [
            {"stage": "tests", "round": 1, "agent": "harness:test-author", "model": OPUS, "input": 3,
             "output": 4500, "cache_write": 9200, "cache_read": 9000, "total": 22703, "messages": 2,
             "model_ms": 19000, "wall_ms": 20000},
            {"stage": "code", "round": 1, "agent": "harness:coder", "model": OPUS, "input": 9,
             "output": 7250, "cache_write": 12000, "cache_read": 12000, "total": 31259, "messages": 2,
             "model_ms": 32500, "wall_ms": 62500},
            {"stage": "orchestrator", "round": 1, "agent": "main", "model": OPUS, "input": 4,
             "output": 200, "cache_write": 4000, "cache_read": 4000, "total": 8204, "messages": 2,
             "model_ms": 5750, "wall_ms": None},
            {"stage": "orchestrator", "round": 1, "agent": "main", "model": SONNET, "input": 2,
             "output": 10, "cache_write": 100, "cache_read": 4100, "total": 4212, "messages": 1,
             "model_ms": 1000, "wall_ms": None},
        ]
        self.assertEqual(got["rows"], rows)
        self.assertEqual(got["totals"], {"input": 18, "output": 11960, "cache_write": 25300, "cache_read": 29100,
                                         "total": 66378, "messages": 7, "model_ms": 58250})
        self.assertEqual(got["by_model"], {
            OPUS: {"input": 16, "output": 11950, "cache_write": 25200, "cache_read": 25000,
                   "total": 62166, "messages": 6, "model_ms": 57250},
            SONNET: {"input": 2, "output": 10, "cache_write": 100, "cache_read": 4100,
                     "total": 4212, "messages": 1, "model_ms": 1000}})
        self.check(got, rows, {sid(1): (True, False)})

        # the same folder for ticket 13: its own session, plus the #13 subagent in 12's session
        self.check(self.usage_of(13, [d]), [
            row("code", 1, "harness:coder", HAIKU, coder13),
            row("review", 1, "harness:reviewer", OPUS, review13),
            row("orchestrator", 1, "main", OPUS, orch13),
        ], {sid(1): (False, None), sid(2): (True, False)})
        # and for 120: the lookalike is its own ticket
        self.check(self.usage_of(120, [d]), [
            row("code", 1, "harness:coder", OPUS,
                [Msg("msg_y120", T("13:10:00.000"), T("13:10:01.000"), (1, 1, 1, 1))]),
            row("orchestrator", 1, "main", OPUS, orch120),
        ], {sid(3): (True, False)})

    def test_shared_session_counts_named_subagents_only(self):
        d = self.folder("shared")
        orch = [Msg("msg_m1", T("09:00:00.000"), T("09:00:04.000"), (50, 5000, 50000, 500000)),
                Msg("msg_m2", T("09:59:00.000"), T("09:59:09.000"), (40, 4000, 40000, 400000), FABLE)]
        tests = [Msg("msg_s1", T("09:10:00.000"), T("09:10:07.000"), (2, 2000, 3000, 0)),
                 Msg("msg_s2", T("09:10:08.000"), T("09:10:09.500"), (1, 100, 0, 3000))]
        code = [Msg("msg_s3", T("09:20:00.000"), T("09:20:30.000"), (3, 6000, 7000, 100), SONNET)]
        review = [Msg("msg_s4", T("09:40:00.000"), T("09:40:02.000"), (4, 40, 400, 4000))]
        review13 = [Msg("msg_s5", T("09:30:00.000"), T("09:30:05.000"), (5, 50, 500, 5000))]
        plain = [Msg("msg_s6", T("09:05:00.000"), T("09:05:06.000"), (6, 60, 600, 6000))]
        tests120 = [Msg("msg_s7", T("09:06:00.000"), T("09:06:01.000"), (7, 70, 700, 7000))]
        code1 = [Msg("msg_s8", T("09:07:00.000"), T("09:07:01.000"), (8, 80, 800, 8000))]
        write_session(d, sid(10), turns(orch, "main"), agents=[
            ("0000000000000000a", meta("harness:test-author", "#12 tests"), turns(tests, "main")),
            ("0000000000000000b", meta("harness:coder", "#12 code"), turns(code, "main")),
            ("0000000000000000c", meta("harness:reviewer", "#13 review"), turns(review13, "main")),
            ("0000000000000000d", meta("harness:test-author", "Write tests for ticket 12"), turns(plain, "main")),
            ("0000000000000000e", meta("harness:test-author", "#120 tests"), turns(tests120, "main")),
            ("0000000000000000f", meta("harness:coder", "#1 code"), turns(code1, "main")),
            ("00000000000000010", meta("harness:reviewer", "   #12 review"), turns(review, "main")),
        ], cost=cost_state(5000000, {OPUS: (1, 2, 3, 4, 5)}))

        got = self.usage_of(12, [d])
        self.check(got, [
            row("tests", 1, "harness:test-author", OPUS, tests),
            row("code", 1, "harness:coder", SONNET, code),
            row("review", 1, "harness:reviewer", OPUS, review),
        ], {sid(10): (False, None)})
        self.assertNotIn("orchestrator", [x["stage"] for x in got["rows"]])
        self.assertEqual([s["own"] for s in got["sessions"]], [False])

        # the other numbers in the same session
        self.check(self.usage_of(13, [d]), [row("review", 1, "harness:reviewer", OPUS, review13)],
                   {sid(10): (False, None)})
        self.check(self.usage_of(120, [d]), [row("tests", 1, "harness:test-author", OPUS, tests120)],
                   {sid(10): (False, None)})
        self.check(self.usage_of(1, [d]), [row("code", 1, "harness:coder", OPUS, code1)],
                   {sid(10): (False, None)})

    def test_orchestrator_entry_on_the_ticket_branch_counts(self):
        d = self.folder("mixed")
        on_main = Msg("msg_b1", T("14:00:00.000"), T("14:00:02.000"), (10, 100, 1000, 10000), OPUS, "main")
        on12a = Msg("msg_b2", T("14:01:00.000"), T("14:01:03.250"), (2, 220, 2200, 22000), OPUS, B12)
        on13 = Msg("msg_b3", T("14:02:00.000"), T("14:02:04.000"), (3, 330, 3300, 33000), OPUS, B13)
        on120 = Msg("msg_b4", T("14:03:00.000"), T("14:03:05.000"), (4, 440, 4400, 44000), OPUS, B120)
        on12b = Msg("msg_b5", T("14:04:00.000"), T("14:04:01.500"), (5, 550, 5500, 55000), SONNET, "t/12-another-slug")
        on_head = Msg("msg_b6", T("14:05:00.000"), T("14:05:06.000"), (6, 660, 6600, 66000), OPUS, "HEAD")
        on1 = Msg("msg_b7", T("14:06:00.000"), T("14:06:07.000"), (7, 770, 7700, 77000), OPUS, B1)
        on12c = Msg("msg_b8", T("14:07:00.000"), T("14:07:00.750"), (8, 880, 8800, 88000), OPUS, B12)
        code = [Msg("msg_b9", T("14:10:00.000"), T("14:10:10.000"), (9, 990, 9900, 99000))]
        plain = [Msg("msg_b10", T("14:20:00.000"), T("14:20:01.000"), (1, 1, 1, 1))]
        write_session(d, sid(20), turns([on_main, on12a, on13, on120, on12b, on_head, on1, on12c], "main"), agents=[
            ("1000000000000000a", meta("harness:coder", "#12 code"), turns(code, "main")),
            ("1000000000000000b", meta("harness:coder", "Fix the build"), turns(plain, "main")),
        ], cost=cost_state(777000, {OPUS: (9, 9, 9, 9, 9)}))

        self.check(self.usage_of(12, [d]), [
            row("code", 1, "harness:coder", OPUS, code),
            row("orchestrator", 1, "main", OPUS, [on12a, on12c]),
            row("orchestrator", 1, "main", SONNET, [on12b]),
        ], {sid(20): (False, None)})
        self.check(self.usage_of(13, [d]), [row("orchestrator", 1, "main", OPUS, [on13])], {sid(20): (False, None)})
        self.check(self.usage_of(1, [d]), [row("orchestrator", 1, "main", OPUS, [on1])], {sid(20): (False, None)})
        self.check(self.usage_of(120, [d]), [row("orchestrator", 1, "main", OPUS, [on120])], {sid(20): (False, None)})

    def test_stage_word_wins_over_agent_type(self):
        d = self.folder("stages")
        specs = [  # (aid, agentType, description, start clock, stage expected)
            ("20000000000000001", "harness:coder", "#12 fix", "15:50:00.000", "fix"),
            ("20000000000000002", "harness:coder", "#12 lunch", "15:30:00.000", "code"),
            ("20000000000000003", "harness:coder", "#12 review", "15:40:00.000", "review"),
            ("20000000000000004", "general-purpose", "#12 tests", "15:00:00.000", "tests"),
            ("20000000000000005", "harness:reviewer", "#12 breaker", "15:20:00.000", "breaker"),
            ("20000000000000006", "harness:test-breaker", "#12", "15:10:00.000", "breaker"),
            ("20000000000000007", "Explore", "#12 code the reader", "15:25:00.000", "code"),
            ("20000000000000008", "harness:test-author", "#12 other", "15:05:00.000", "tests"),
            ("20000000000000009", "harness:coder", "#12 orchestrator", "15:35:00.000", "code"),
            ("2000000000000000a", "harness:reviewer", "#12 fix the findings", "15:55:00.000", "fix"),
        ]
        agents, by_stage = [], {}
        for n, (aid, atype, desc, clock, stage) in enumerate(specs):
            m = Msg(f"msg_w{n}", T(clock), T(clock[:6] + "%02d.%03d" % (n + 1, 100 * n)), (n + 1, 10 * n, 100 * n, 1000 * n))
            agents.append((aid, meta(atype, desc), turns([m], "main")))
            by_stage.setdefault(stage, []).append((m.start, atype, m))
        write_session(d, sid(30), turns([Msg("msg_w_main", T("14:59:00.000"), T("14:59:01.000"), (1, 1, 1, 1))], "main"),
                      agents=agents)
        # an own session: the word wins there too
        fix_own = [Msg("msg_w_own", T("16:00:00.000"), T("16:00:03.000"), (11, 111, 1111, 11111))]
        # starts before the first review (the 15:40 one) ends, so it is code, not fix
        code_own = [Msg("msg_w_own2", T("15:38:00.000"), T("15:38:02.000"), (12, 122, 1222, 12222))]
        orch_own = [Msg("msg_w_own3", T("15:57:00.000"), T("15:57:01.000"), (13, 133, 1333, 13333))]
        write_session(d, sid(31), turns(orch_own, B12), agents=[
            ("2100000000000000b", meta("harness:reviewer", "#12 fix"), turns(fix_own, B12)),
            ("2100000000000000a", meta("harness:coder", "Implement the reader"), turns(code_own, B12))])
        by_stage.setdefault("fix", []).append((fix_own[0].start, "harness:reviewer", fix_own[0]))
        by_stage.setdefault("code", []).append((code_own[0].start, "harness:coder", code_own[0]))

        rows = []
        for stage, items in by_stage.items():
            for rnd, (_, atype, m) in enumerate(sorted(items, key=lambda x: x[0]), 1):
                rows.append(row(stage, rnd, atype, m.model, [m]))
        rows.append(row("orchestrator", 1, "main", OPUS, orch_own))
        got = self.usage_of(12, [d])
        self.check(got, sort_rows(rows), {sid(30): (False, None), sid(31): (True, False)})
        # the stages and rounds, written out
        self.assertEqual([(x["stage"], x["round"], x["agent"]) for x in got["rows"]], [
            ("tests", 1, "general-purpose"), ("tests", 2, "harness:test-author"),
            ("breaker", 1, "harness:test-breaker"), ("breaker", 2, "harness:reviewer"),
            ("code", 1, "Explore"), ("code", 2, "harness:coder"), ("code", 3, "harness:coder"),
            ("code", 4, "harness:coder"),
            ("review", 1, "harness:coder"),
            ("fix", 1, "harness:coder"), ("fix", 2, "harness:reviewer"), ("fix", 3, "harness:reviewer"),
            ("orchestrator", 1, "main")])

    def test_unknown_agent_type_is_other(self):
        d = self.folder("types")
        specs = [  # (aid, meta, start clock, agent, stage); aid order is not time order
            ("3000000000000000f", meta("Explore", "Find the cost code"), "08:00:00.000", "Explore", "other"),
            ("3000000000000000e", meta(None, "#12 something"), "08:10:00.000", "unknown", "other"),
            ("3000000000000000d", None, "08:20:00.000", "unknown", "other"),
            ("3000000000000000c", meta("general-purpose", ""), "08:05:00.000", "general-purpose", "other"),
            ("3000000000000000b", meta("harness:coder-helper", "Help"), "08:15:00.000", "harness:coder-helper", "other"),
            ("3000000000000000a", meta("someone:else:coder", "Implement"), "08:25:00.000", "someone:else:coder", "code"),
            ("30000000000000009", meta("reviewer", "Review"), "08:30:00.000", "reviewer", "review"),
            ("30000000000000008", "[1, 2, 3]", "08:35:00.000", "unknown", "other"),
            ("30000000000000007", meta("harness:test-author", ""), "08:40:00.000", "harness:test-author", "tests"),
            ("30000000000000006", meta("test-breaker", None), "08:45:00.000", "test-breaker", "breaker"),
        ]
        agents, items = [], {}
        for n, (aid, value, clock, agent, stage) in enumerate(specs):
            m = Msg(f"msg_u{n}", T(clock), T(clock[:6] + "%02d.%03d" % (n + 2, 7 * n)),
                    (n, 3 * n + 1, 50 * n, 700 * n), (OPUS, SONNET)[n % 2])
            agents.append((aid, value, turns([m], B12)))
            items.setdefault(stage, []).append((m.start, agent, m))
        orch = [Msg("msg_u_main", T("07:59:00.000"), T("07:59:02.000"), (1, 2, 3, 4))]
        write_session(d, sid(40), turns(orch, B12), agents=agents)

        rows = [row("orchestrator", 1, "main", OPUS, orch)]
        for stage, found in items.items():
            for rnd, (_, agent, m) in enumerate(sorted(found, key=lambda x: x[0]), 1):
                rows.append(row(stage, rnd, agent, m.model, [m]))
        got = self.usage_of(12, [d])
        self.check(got, sort_rows(rows), {sid(40): (True, False)})
        self.assertEqual([(x["stage"], x["round"], x["agent"]) for x in got["rows"] if x["stage"] == "other"], [
            ("other", 1, "Explore"), ("other", 2, "general-purpose"), ("other", 3, "unknown"),
            ("other", 4, "harness:coder-helper"), ("other", 5, "unknown"), ("other", 6, "unknown")])

    def test_rows_split_by_round_and_model(self):
        d = self.folder("rounds")
        # two test-authors in the ticket's own session (the later one has the smaller id) and a
        # #12 tests agent of another type in a shared session between them
        late = [Msg("msg_r1", T("09:10:00.000"), T("09:10:20.000"), (1, 100, 1000, 0), SONNET),
                Msg("msg_r2", T("09:11:00.000"), T("09:11:04.000"), (1, 50, 0, 1000), SONNET)]
        early = [Msg("msg_r3", T("09:00:00.000"), T("09:00:09.000"), (2, 200, 2000, 0))]
        middle = [Msg("msg_r4", T("09:05:00.000"), T("09:05:03.000"), (3, 300, 0, 3000))]
        # one coder on two models: a row per model, each with its own wall time
        coder = [Msg("msg_r5", T("09:20:00.000"), T("09:20:10.000"), (4, 400, 4000, 0)),
                 Msg("msg_r6", T("09:21:00.000"), T("09:21:30.000"), (5, 500, 0, 5000), FABLE),
                 Msg("msg_r7", T("09:22:00.000"), T("09:22:05.000"), (6, 600, 0, 6000))]
        coder2 = [Msg("msg_r8", T("09:30:00.000"), T("09:30:01.000"), (7, 700, 70, 7), SONNET)]
        orch = [Msg("msg_r9", T("08:59:00.000"), T("08:59:30.000"), (8, 80, 800, 8000)),
                Msg("msg_r10", T("09:40:00.000"), T("09:40:02.000"), (9, 90, 900, 9000), FABLE)]
        write_session(d, sid(50), turns(orch, B12), agents=[
            ("0a000000000000000", meta("harness:test-author", "Write the tests"), turns(late, B12)),
            ("ff000000000000000", meta("harness:test-author", "#12 tests"), turns(early, B12)),
            ("0b000000000000000", meta("harness:coder", "#12 code"), turns(coder, B12)),
            ("01000000000000000", meta("harness:coder", "#12 code"), turns(coder2, B12)),
        ])
        write_session(d, sid(51), turns([Msg("msg_r11", T("08:00:00.000"), T("08:00:01.000"), (1, 1, 1, 1))], "main"),
                      agents=[("05000000000000000", meta("general-purpose", "#12 tests"), turns(middle, "main"))])

        got = self.usage_of(12, [d])
        expected = [
            row("tests", 1, "harness:test-author", OPUS, early),
            row("tests", 2, "general-purpose", OPUS, middle),
            row("tests", 3, "harness:test-author", SONNET, late),
            row("code", 1, "harness:coder", FABLE, [coder[1]]),
            row("code", 1, "harness:coder", OPUS, [coder[0], coder[2]]),
            row("code", 2, "harness:coder", SONNET, coder2),
            row("orchestrator", 1, "main", FABLE, [orch[1]]),
            row("orchestrator", 1, "main", OPUS, [orch[0]]),
        ]
        self.check(got, expected, {sid(50): (True, False), sid(51): (False, None)})
        code_rows = [x for x in got["rows"] if x["stage"] == "code" and x["round"] == 1]
        self.assertEqual([(x["model"], x["messages"], x["model_ms"], x["wall_ms"]) for x in code_rows],
                         [(FABLE, 1, 30000, 30000), (OPUS, 2, 15000, 125000)])

    def test_message_counted_once_across_files(self):
        d1, d2 = self.folder("checkout"), self.folder("worktree")
        # the same response in a session and in its resumed copy, in two folders: per field the
        # largest (1, 120, 50, 10), the earliest start 10:00:00, the latest end 10:00:06
        write_session(d1, sid(60), [
            user_entry(T("10:00:00.000"), B12),
            assistant_entry(T("10:00:04.000"), "msg_dup", (1, 100, 50, 0), OPUS, B12),
            user_entry(T("10:00:10.000"), B12),
            assistant_entry(T("10:00:11.000"), "msg_a", (2, 20, 0, 150), OPUS, B12),
        ])
        write_session(d2, sid(61), [
            user_entry(T("10:00:02.000"), B12),
            assistant_entry(T("10:00:03.000"), "msg_dup", (1, 90, 40, 10), OPUS, B12),
            assistant_entry(T("10:00:06.000"), "msg_dup", (1, 120, 40, 10), OPUS, B12),
            user_entry(T("10:05:00.000"), B12),
            assistant_entry(T("10:05:00.500"), "msg_b", (3, 30, 300, 0), OPUS, B12),
        ])
        rows = [{"stage": "orchestrator", "round": 1, "agent": "main", "model": OPUS, "input": 6,
                 "output": 170, "cache_write": 350, "cache_read": 160, "total": 686, "messages": 3,
                 "model_ms": 7500, "wall_ms": None}]
        expected_sessions = {sid(60): (True, False), sid(61): (True, False)}
        self.check(self.usage_of(12, [d1, d2]), rows, expected_sessions)
        self.check(self.usage_of(12, [d2, d1]), rows, expected_sessions)

        # a subagent's response repeated in a second subagent file of the same session: counted once
        d3 = self.folder("subagent-copy")
        first = [Msg("msg_sub1", T("11:00:00.000"), T("11:00:05.000"), (4, 40, 400, 4000)),
                 Msg("msg_sub2", T("11:00:06.000"), T("11:00:08.000"), (5, 50, 500, 5000))]
        write_session(d3, sid(62), turns([Msg("msg_m", T("10:59:00.000"), T("10:59:01.000"), (1, 1, 1, 1))], B12),
                      agents=[("40000000000000001", meta("harness:coder", "#12 code"), turns(first, B12)),
                              ("40000000000000002", meta("harness:coder", "#12 code"), turns(first, B12))])
        got = self.usage_of(12, [d3])
        self.assertEqual(got["totals"]["messages"], 3)
        self.assertEqual(got["totals"]["total"], (4 + 40 + 400 + 4000) + (5 + 50 + 500 + 5000) + 4)
        self.assertEqual(got["totals"]["model_ms"], 5000 + 2000 + 1000)

    def test_model_time_and_agent_time_per_row(self):
        d = self.folder("time")
        write_session(d, sid(70), [
            # across midnight: 23:59:59.900 to 00:00:00.150 is 250 ms
            user_entry(T("23:59:59.900", 20), B12),
            assistant_entry(T("00:00:00.150", 21), "msg_t_o1", (1, 10, 100, 1000), OPUS, B12),
            # a long gap before the next orchestrator message: no wall time on orchestrator rows
            user_entry(T("03:00:00.000", 21), B12),
            assistant_entry(T("03:00:00.007", 21), "msg_t_o2", (2, 20, 200, 2000), OPUS, B12),
        ], agents=[("50000000000000001", meta("harness:coder", "#12 code"), [
            # starts at its api-request (08:00:00.000), not at the user entry before it
            user_entry(T("07:59:50.000", 21), B12),
            api_request(T("08:00:00.000", 21), "req-1", B12),
            assistant_entry(T("08:00:00.400", 21), "msg_t_c1", (3, 30, 300, 3000), OPUS, B12, ref="req-1"),
            assistant_entry(T("08:00:02.123", 21), "msg_t_c1", (3, 31, 300, 3000), OPUS, B12, ref="req-1"),
            # an api-request stamped after the response: its model time is 0, never negative
            user_entry(T("08:05:00.000", 21), B12),
            api_request(T("08:05:09.000", 21), "req-2", B12),
            assistant_entry(T("08:05:04.000", 21), "msg_t_c2", (4, 40, 400, 4000), OPUS, B12, ref="req-2"),
            # one millisecond
            user_entry(T("08:10:00.000", 21), B12),
            assistant_entry(T("08:10:00.001", 21), "msg_t_c3", (5, 50, 500, 5000), OPUS, B12),
        ])])
        got = self.usage_of(12, [d])
        rows = [
            {"stage": "code", "round": 1, "agent": "harness:coder", "model": OPUS, "input": 12,
             "output": 121, "cache_write": 1200, "cache_read": 12000, "total": 13333, "messages": 3,
             "model_ms": 2124, "wall_ms": 600001},
            {"stage": "orchestrator", "round": 1, "agent": "main", "model": OPUS, "input": 3,
             "output": 30, "cache_write": 300, "cache_read": 3000, "total": 3333, "messages": 2,
             "model_ms": 257, "wall_ms": None},
        ]
        self.check(got, rows, {sid(70): (True, False)})
        for x in got["rows"]:
            self.assertIs(type(x["model_ms"]), int)
            self.assertIn(type(x["wall_ms"]), (int, type(None)))
        self.assertIs(type(got["totals"]["model_ms"]), int)

    def test_session_totals_from_own_sessions_only(self):
        d = self.folder("totals")
        one = lambda mid, clock, use=(1, 2, 3, 4): [Msg(mid, T(clock), T(clock[:6] + "30.000"), use)]
        # own, with a cost-state
        write_session(d, sid(80), turns(one("msg_a", "10:00:00.000"), B12),
                      cost=cost_state(1000000, {OPUS: (10, 2000, 300, 50000, 7000), SONNET: (1, 2, 3, 4, 5)}))
        # own (a HEAD entry does not change that), with a cost-state
        write_session(d, sid(81), turns(one("msg_b", "10:01:00.000") +
                                        [Msg("msg_b2", T("10:02:00.000"), T("10:02:01.000"), (1, 1, 1, 1), OPUS, "HEAD")], B12),
                      cost=cost_state(234567, {OPUS: (5, 1000, 0, 25000, 3000), FABLE: (7, 70, 0, 700, 77)}))
        # own, still running: no cost-state
        write_session(d, sid(82), turns(one("msg_c", "10:03:00.000"), B12))
        # shared: main plus a message on t/12-: counts, its cost-state does not
        write_session(d, sid(83), turns(one("msg_d", "10:04:00.000") +
                                        [Msg("msg_d2", T("10:05:00.000"), T("10:05:01.000"), (2, 2, 2, 2), OPUS, B12)], "main"),
                      cost=cost_state(999, {OPUS: (100, 100, 100, 100, 100)}))
        # t/12- and t/120-: not own
        write_session(d, sid(84), turns(one("msg_e", "10:06:00.000") +
                                        [Msg("msg_e2", T("10:07:00.000"), T("10:07:01.000"), (3, 3, 3, 3), OPUS, B120)], B12),
                      cost=cost_state(888, {OPUS: (200, 200, 200, 200, 200)}))
        # another ticket's own session
        write_session(d, sid(85), turns(one("msg_f", "10:08:00.000"), B13),
                      cost=cost_state(777, {OPUS: (300, 300, 300, 300, 300)}))
        # main with a #12 subagent
        write_session(d, sid(86), turns(one("msg_g", "10:09:00.000"), "main"),
                      agents=[("60000000000000001", meta("harness:coder", "#12 code"),
                               turns(one("msg_g2", "10:10:00.000"), "main"))],
                      cost=cost_state(666, {OPUS: (400, 400, 400, 400, 400)}))

        got = self.usage_of(12, [d])
        self.assertEqual(got["session_totals"], {
            "models": {OPUS: {"input": 15, "output": 3000, "thinking": 300, "cache_read": 75000, "cache_write": 10000},
                       SONNET: {"input": 1, "output": 2, "thinking": 3, "cache_read": 4, "cache_write": 5},
                       FABLE: {"input": 7, "output": 70, "thinking": 0, "cache_read": 700, "cache_write": 77}},
            "api_ms": 1234567, "sessions": 2})
        self.check(got, sort_rows([
            row("code", 1, "harness:coder", OPUS, one("msg_g2", "10:10:00.000")),
            row("orchestrator", 1, "main", OPUS, one("msg_a", "10:00:00.000") + one("msg_b", "10:01:00.000") +
                [Msg("msg_b2", T("10:02:00.000"), T("10:02:01.000"), (1, 1, 1, 1))] +
                one("msg_c", "10:03:00.000") +
                [Msg("msg_d2", T("10:05:00.000"), T("10:05:01.000"), (2, 2, 2, 2))] + one("msg_e", "10:06:00.000")),
        ]), {sid(80): (True, True), sid(81): (True, True), sid(82): (True, False), sid(83): (False, None),
             sid(84): (False, None), sid(86): (False, None)}, got["session_totals"])

        # None when no own session has a cost-state, however many others have one
        d2 = self.folder("totals-none")
        write_session(d2, sid(87), turns(one("msg_h", "11:00:00.000"), B12))
        # shared: an entry on main, then one on t/12- that counts; its cost-state does not
        msg_i = Msg("msg_i", T("11:01:00.000"), T("11:01:01.000"), (1, 1, 1, 1), OPUS, B12)
        write_session(d2, sid(88), turns(one("msg_i0", "11:00:40.000") + [msg_i], "main"),
                      cost=cost_state(555, {OPUS: (5, 5, 5, 5, 5)}))
        write_session(d2, sid(89), turns(one("msg_j", "11:02:00.000"), B13), cost=cost_state(444, {OPUS: (4, 4, 4, 4, 4)}))
        got = self.usage_of(12, [d2])
        self.assertIsNone(got["session_totals"])
        self.assertEqual({s["id"]: s["own"] for s in got["sessions"]}, {sid(87): True, sid(88): False})
        self.check(got, [row("orchestrator", 1, "main", OPUS, one("msg_h", "11:00:00.000") + [msg_i])],
                   {sid(87): (True, False), sid(88): (False, None)})
        # and for 13 the same folder has a total of its own
        self.assertEqual(self.usage_of(13, [d2])["session_totals"], {
            "models": {OPUS: {"input": 4, "output": 4, "thinking": 4, "cache_read": 4, "cache_write": 4}},
            "api_ms": 444, "sessions": 1})

    # ---------------------------------------------------------- broad: generated
    def test_generated_tickets(self):
        for run, seed in enumerate(seeds()):
            with self.subTest(seed=seed):
                gen = Generated(seed, self.tmp / f"gen-{run}")
                got = self.usage_of(gen.n, gen.dirs)
                self.check(got, gen.rows, gen.sessions, gen.session_totals, replay(seed))

    # ---------------------------------------------------------- specific cases
    def test_subagent_without_a_ticket_number_follows_its_first_branch(self):
        d = self.folder("first-branch")
        cases = [  # (aid, description, its branch, counts for 12)
            ("70000000000000001", "Explore the board", B12, True),
            ("70000000000000002", "Explore the board", B120, False),
            ("70000000000000003", "Explore the board", B13, False),
            ("70000000000000004", "Explore the board", "main", False),
            ("70000000000000005", "#13 code", B12, False),        # the description wins
            ("70000000000000006", "#12 code", B13, True),
            ("70000000000000007", None, B12, True),
            ("70000000000000008", "Fix #12 now", B13, False),       # not at the start
        ]
        agents, rows = [], []
        for n, (aid, desc, branch, counts) in enumerate(cases):
            m = Msg(f"msg_fb{n}", T("12:%02d:00.000" % n), T("12:%02d:0%d.000" % (n, n + 1)), (n, n + 1, n + 2, n + 3))
            agents.append((aid, meta("Explore" if desc != "#12 code" else "harness:coder", desc), turns([m], branch)))
            if counts:
                rows.append((m, desc))
        write_session(d, sid(90), turns([Msg("msg_fb_main", T("11:00:00.000"), T("11:00:01.000"), (9, 9, 9, 9))], "main"),
                      agents=agents)
        expected = [row("other", 1, "Explore", OPUS, [rows[0][0]]),
                    row("code", 1, "harness:coder", OPUS, [rows[1][0]]),
                    row("other", 2, "Explore", OPUS, [rows[2][0]])]
        self.check(self.usage_of(12, [d]), sort_rows(expected), {sid(90): (False, None)})

    def test_ticket_number_in_the_description_ends_at_a_word_boundary(self):
        d = self.folder("boundary")
        cases = [  # (aid, agentType, description, counts for 12 as stage)
            ("71000000000000001", "harness:coder", "#12abc", None),
            ("71000000000000002", "harness:coder", "#12_fix", None),
            ("71000000000000003", "harness:coder", "#120", None),
            ("71000000000000004", "harness:coder", "#12-lunch", "code"),
            ("71000000000000005", "harness:test-author", "#12:", "tests"),
            ("71000000000000006", "harness:reviewer", "#12.", "review"),
            ("71000000000000007", "harness:test-breaker", "#12, then lunch", "breaker"),
        ]
        agents, rows = [], []
        for n, (aid, atype, desc, stage) in enumerate(cases):
            m = Msg(f"msg_wb{n}", T("13:%02d:00.000" % n), T("13:%02d:0%d.250" % (n, n + 1)), (n + 1, 2 * n, 3 * n, 4 * n))
            agents.append((aid, meta(atype, desc), turns([m], "main")))
            if stage is not None:
                rows.append(row(stage, 1, atype, OPUS, [m]))
        write_session(d, sid(91), turns([Msg("msg_wb_main", T("12:59:00.000"), T("12:59:01.000"), (9, 9, 9, 9))], "main"),
                      agents=agents)
        # #12abc names no ticket, so its first branch decides: on t/13- it is not 12's
        write_session(d, sid(92), turns([Msg("msg_wb_m2", T("14:00:00.000"), T("14:00:01.000"), (1, 1, 1, 1))], "main"),
                      agents=[("71000000000000008", meta("harness:coder", "#12abc"),
                               turns([Msg("msg_wb8", T("14:01:00.000"), T("14:01:02.000"), (1, 2, 3, 4))], B13))])
        self.check(self.usage_of(12, [d]), sort_rows(rows), {sid(91): (False, None)})

    def test_speed_rules_skip_transcripts_that_cannot_count(self):
        """Ticket Context, Speed: a main transcript whose text has no `t/12-` is never parsed (its
        subagents are still considered); a subagent is parsed only when its description starts
        `#12`, or starts with no `#<digits>` and its first branch is t/12-. Proven through which
        paths reach transcripts.read_transcript, with the result still right."""
        d = Path(self.folder("speed"))
        parsed, allowed, forbidden = [], set(), {}
        mid = iter(range(1000))

        def one(clock, branch=None, model=OPUS):
            n = next(mid)
            return [Msg(f"msg_sp{n}", T(clock), T(clock[:6] + "%02d.%03d" % (n % 50 + 1, n)),
                        (n + 1, 10 * n + 3, 100 * n, 1000 * n + 7), model, branch)]

        def main_path(s):
            return str(d / f"{sid(s)}.jsonl")

        def agent_path(s, aid):
            return str(d / sid(s) / "subagents" / f"agent-{aid}.jsonl")

        # S1, on main with no t/12- anywhere: never parsed, but its #12 subagent and its unnamed
        # subagent on t/12- still count; #13, #120 and unnamed ones on main or t/13- are not parsed
        s1_code, s1_explore = one("08:00:00.000"), one("08:05:00.000")
        write_session(d, sid(130), turns(one("07:59:00.000"), "main"), agents=[
            ("a0000000000000001", meta("harness:coder", "#12 code"), turns(s1_code, "main")),
            ("a0000000000000002", meta("harness:reviewer", "#13 review"), turns(one("08:01:00.000"), "main")),
            ("a0000000000000003", meta("Explore", "Explore the board"), turns(one("08:02:00.000"), "main")),
            ("a0000000000000004", meta("Explore", "Explore the board"), turns(s1_explore, B12)),
            ("a0000000000000005", meta("harness:test-author", "#120 tests"), turns(one("08:03:00.000"), "main")),
            ("a0000000000000006", meta("Explore", None), turns(one("08:04:00.000"), B13)),
        ])
        forbidden[main_path(130)] = "S1 main: no t/12- in its text"
        for aid, why in (("a0000000000000002", "#13"), ("a0000000000000003", "unnamed on main"),
                         ("a0000000000000005", "#120"), ("a0000000000000006", "unnamed on t/13-")):
            forbidden[agent_path(130, aid)] = "S1 subagent " + why
        allowed |= {agent_path(130, "a0000000000000001"), agent_path(130, "a0000000000000004")}

        # S2, ticket 13's own session: its main is never parsed for 12; its #12 subagent counts
        s2_tests = one("09:00:00.000", model=SONNET)
        write_session(d, sid(131), turns(one("08:59:00.000"), B13), agents=[
            ("b0000000000000001", meta("harness:test-author", "#12 tests"), turns(s2_tests, B13)),
            ("b0000000000000002", meta("harness:coder", "#13 code"), turns(one("09:01:00.000"), B13)),
            ("b0000000000000003", meta("harness:coder", "Implement it"), turns(one("09:02:00.000"), B13)),
        ])
        forbidden[main_path(131)] = "S2 main: on t/13-"
        forbidden[agent_path(131, "b0000000000000002")] = "S2 subagent #13"
        forbidden[agent_path(131, "b0000000000000003")] = "S2 subagent unnamed on t/13-"
        allowed.add(agent_path(131, "b0000000000000001"))

        # S3, the prefix lookalike t/120-: its text has no t/12-, so it is never parsed
        write_session(d, sid(132), turns(one("10:00:00.000"), B120), agents=[
            ("c0000000000000001", meta("harness:coder", "Implement it"), turns(one("10:01:00.000"), B120))])
        forbidden[main_path(132)] = "S3 main: on t/120-"
        forbidden[agent_path(132, "c0000000000000001")] = "S3 subagent unnamed on t/120-"

        # S4, 12's own session: main parsed; unnamed and #12 subagents count, #13 is never parsed
        s4_orch, s4_coder, s4_fix = one("11:00:00.000"), one("11:01:00.000"), one("11:02:00.000", model=FABLE)
        write_session(d, sid(133), turns(s4_orch, B12), agents=[
            ("d0000000000000001", meta("harness:coder", "Implement it"), turns(s4_coder, B12)),
            ("d0000000000000002", meta("harness:reviewer", "#13 review"), turns(one("11:03:00.000"), B12)),
            ("d0000000000000003", meta("harness:reviewer", "#12 fix"), turns(s4_fix, B12)),
        ])
        forbidden[agent_path(133, "d0000000000000002")] = "S4 subagent #13 in 12's own session"
        allowed |= {main_path(133), agent_path(133, "d0000000000000001"), agent_path(133, "d0000000000000003")}

        # S5, on main with one message on t/12-: main parsed; its unnamed subagent on main and its
        # #13 subagent are not
        s5_12 = one("12:01:00.000", B12)
        write_session(d, sid(134), turns(one("12:00:00.000") + s5_12, "main"), agents=[
            ("e0000000000000001", meta("Explore", "Look around"), turns(one("12:02:00.000"), "main")),
            ("e0000000000000002", meta("harness:coder", " #13 code"), turns(one("12:03:00.000"), "main")),
        ])
        forbidden[agent_path(134, "e0000000000000001")] = "S5 subagent unnamed on main"
        forbidden[agent_path(134, "e0000000000000002")] = "S5 subagent #13"
        allowed.add(main_path(134))

        for path in list(forbidden) + sorted(allowed):
            self.assertTrue(os.path.isfile(path), path)   # the fixture paths are the real ones

        transcripts = sys.modules["harness_lib.transcripts"]
        real = transcripts.read_transcript

        def spy(path):
            parsed.append(os.path.realpath(str(path)))
            return real(path)

        with mock.patch.object(transcripts, "read_transcript", spy):
            got = self.usage_of(12, [str(d)])

        short = lambda paths: sorted(os.path.relpath(p, str(d)) for p in paths)
        self.assertEqual(sorted({forbidden[p] for p in parsed if p in forbidden}), [],
                         "parsed a transcript that cannot count for 12")
        self.assertEqual(short(set(parsed) - allowed), [], "parsed a transcript outside the expected set")
        self.assertEqual(short(allowed - set(parsed)), [], "a transcript that counts for 12 was not parsed")

        # two coders in the code stage: S1's (08:00) is round 1, S4's (11:01) round 2
        self.check(got, [row("tests", 1, "harness:test-author", SONNET, s2_tests),
                         row("code", 1, "harness:coder", OPUS, s1_code),
                         row("code", 2, "harness:coder", OPUS, s4_coder),
                         row("fix", 1, "harness:reviewer", FABLE, s4_fix),
                         row("other", 1, "Explore", OPUS, s1_explore),
                         row("orchestrator", 1, "main", OPUS, s4_orch + s5_12)],
                   {sid(130): (False, None), sid(131): (False, None), sid(133): (True, False), sid(134): (False, None)})

    def test_default_dirs_are_the_repositorys_session_folders(self):
        main = str(self.tmp / "client_repo.v2")
        projects = self.home / ".claude" / "projects"
        own_dir = projects / encode(main)
        wt_dir = projects / encode(os.path.join(main, ".claude", "worktrees", "t-12-add-cost"))
        elsewhere = projects / encode(str(self.tmp / "elsewhere"))
        orch = [Msg("msg_dd1", T("10:00:00.000"), T("10:00:02.000"), (1, 10, 100, 1000))]
        coder = [Msg("msg_dd2", T("10:10:00.000"), T("10:10:03.000"), (2, 20, 200, 2000))]
        write_session(own_dir, sid(100), turns(orch, B12))
        write_session(wt_dir, sid(101), [user_entry(T("10:09:00.000"), B12)], agents=[
            ("80000000000000001", meta("harness:coder", "#12 code"), turns(coder, B12))])
        write_session(wt_dir, sid(102), turns([Msg("msg_dd4", T("10:20:00.000"), T("10:20:01.000"), (1, 1, 1, 1))], B12))
        write_session(elsewhere, sid(103), turns([Msg("msg_dd3", T("10:00:00.000"), T("10:00:09.000"), (5, 5, 5, 5))], B12))
        got = self.c.ticket_usage(main, 12)
        self.assert_no_dollars(got)
        self.check(got, [row("code", 1, "harness:coder", OPUS, coder),
                         row("orchestrator", 1, "main", OPUS,
                             orch + [Msg("msg_dd4", T("10:20:00.000"), T("10:20:01.000"), (1, 1, 1, 1))])],
                   {sid(100): (True, False), sid(101): (True, False), sid(102): (True, False)})

    def test_no_sessions_for_the_ticket_give_an_empty_result(self):
        d = self.folder("empty")
        write_session(d, sid(110), turns([Msg("msg_n1", T("10:00:00.000"), T("10:00:01.000"), (1, 1, 1, 1))], B13),
                      cost=cost_state(10, {OPUS: (1, 1, 1, 1, 1)}))
        for dirs in ([d], [], [str(self.tmp / "missing")]):
            got = self.usage_of(12, dirs)
            self.assertEqual(got["rows"], [])
            self.assertEqual(got["totals"], {k: 0 for k in TOTAL_KEYS})
            self.assertEqual(got["by_model"], {})
            self.assertIsNone(got["session_totals"])
            self.assertEqual(got["sessions"], [])

    def test_no_dollar_figure_anywhere(self):
        d = self.folder("dollars")
        write_session(d, sid(120), turns([Msg("msg_d1", T("10:00:00.000"), T("10:00:01.000"), (1, 2, 3, 4)),
                                          Msg("msg_d2", T("10:00:02.000"), T("10:00:03.000"), (5, 6, 7, 8), FABLE)], B12),
                      agents=[("90000000000000001", meta("harness:coder", "#12 code"),
                               turns([Msg("msg_d3", T("10:01:00.000"), T("10:01:01.000"), (9, 9, 9, 9))], B12))],
                      cost=cost_state(4242, {OPUS: (1, 2, 3, 4, 5), FABLE: (6, 7, 8, 9, 10)}, total_cost=88.5))
        got = self.usage_of(12, [d])
        self.assertIsNotNone(got["session_totals"])
        text = json.dumps(got)
        self.assertNotIn("88.5", text)
        self.assertNotRegex(text, r"(?i)usd|dollar|price|\$")
        self.assertEqual(set(got["session_totals"]), {"models", "api_ms", "sessions"})
        for per in got["session_totals"]["models"].values():
            self.assertEqual(set(per), {"input", "output", "thinking", "cache_read", "cache_write"})


# ------------------------------------------------------------------ fix rounds (ticket #111)
def coordinator_entry(ts, branch):
    """A continuation: the coordinator's message to a running or resumed agent."""
    return {"type": "user", "timestamp": ts, "gitBranch": branch, "origin": {"kind": "coordinator"},
            "message": {"content": "The coordinator sent a message while you were working: please go on"}}


def segmented(segments, branch):
    """One agent transcript from message lists, a continuation before every list but the first."""
    out = []
    for i, seg in enumerate(segments):
        if i:
            out.append(coordinator_entry(shift(seg[0].start, -500), branch))
        out += turns(seg, branch)
    return out


def mk(n, start, end, model=OPUS):
    """A message with a unique id and usage figures that differ per n."""
    return Msg(f"msg_fr{n}", start, end, (n, n * 100 + 7, n * 10, n * 1000 + 3), model)


class FixRounds(Base):
    """A continued agent's segments, and the first review deciding which are fix rounds."""

    ORCH = Msg("msg_fr_orch", T("09:00:00.000"), T("09:00:02.000"), (1, 10, 100, 0))
    CODER, REVIEWER, AUTHOR, BREAKER = ("harness:coder", "harness:reviewer", "harness:test-author",
                                        "harness:test-breaker")

    def expect(self, agents, rows):
        """Write N's own session with the orchestrator message and `agents` [(aid, meta,
        [segment message lists])], and check the result is exactly `rows` plus the orchestrator's."""
        d = self.folder("fix")
        write_session(d, sid(1), turns([self.ORCH], B12),
                      agents=[(aid, mt, segmented(segs, B12)) for aid, mt, segs in agents])
        got = self.usage_of(12, [d])
        self.check(got, sort_rows(rows + [row("orchestrator", 1, "main", OPUS, [self.ORCH])]),
                   {sid(1): (True, False)})
        return got

    # ---------------------------------------------------------- broad: the named rules
    def test_continued_coder_after_the_first_review_is_a_fix_round(self):
        author = [mk(1, T("10:00:00.000"), T("10:00:05.000"))]
        code1 = [mk(2, T("10:10:00.000"), T("10:10:20.000")), mk(3, T("10:10:30.000"), T("10:10:40.000"))]
        review = [mk(4, T("10:20:00.000"), T("10:20:30.000")), mk(5, T("10:20:40.000"), T("10:21:00.000"))]
        code2 = [mk(6, T("10:30:00.000"), T("10:30:15.000")), mk(7, T("10:30:20.000"), T("10:30:25.000"), SONNET)]
        code3 = [mk(8, T("10:40:00.000"), T("10:40:30.000")), mk(9, T("10:41:00.000"), T("10:41:02.000"))]
        self.expect([
            ("a0000000000000001", meta(self.AUTHOR, "Write the tests"), [author]),
            ("a0000000000000002", meta(self.CODER, "Implement it"), [code1, code2, code3]),
            ("a0000000000000003", meta(self.REVIEWER, "Review the change"), [review]),
        ], [
            row("tests", 1, self.AUTHOR, OPUS, author),
            row("code", 1, self.CODER, OPUS, code1),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, code2[:1]),
            row("fix", 1, self.CODER, SONNET, code2[1:]),
            row("fix", 2, self.CODER, OPUS, code3),
        ])

    def test_reviewer_recheck_is_fix_and_the_first_review_stays_review(self):
        code = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review = [mk(2, T("10:20:00.000"), T("10:20:30.000")), mk(3, T("10:20:40.000"), T("10:21:00.000"))]
        recheck = [mk(4, T("10:40:00.000"), T("10:40:45.000"))]
        recheck2 = [mk(5, T("11:20:00.000"), T("11:20:10.000"), FABLE)]
        got = self.expect([
            ("b0000000000000001", meta(self.CODER, "Implement it"), [code]),
            ("b0000000000000002", meta(self.REVIEWER, "Review the change"), [review, recheck, recheck2]),
        ], [
            row("code", 1, self.CODER, OPUS, code),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.REVIEWER, OPUS, recheck),
            row("fix", 2, self.REVIEWER, FABLE, recheck2),
        ])
        self.assertEqual([x["round"] for x in got["rows"] if x["stage"] == "review"], [1])

    def test_unnamed_coder_after_the_first_review_is_fix(self):
        before = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review = [mk(2, T("10:20:00.000"), T("10:20:30.000")), mk(3, T("10:20:40.000"), T("10:21:00.000"))]
        during = [mk(4, T("10:20:10.000"), T("10:20:50.000"))]       # starts before the review ends
        named = [mk(5, T("10:35:00.000"), T("10:35:10.000"))]        # names its stage: stays code
        late_coder = [mk(6, T("10:40:00.000"), T("10:40:10.000"))]
        explorer = [mk(7, T("10:45:00.000"), T("10:45:05.000"))]     # neither code nor review
        late_reviewer = [mk(8, T("10:50:00.000"), T("10:50:30.000"))]
        self.expect([
            ("c0000000000000001", meta(self.CODER, "Implement it"), [before]),
            ("c0000000000000002", meta(self.REVIEWER, "Review the change"), [review]),
            ("c0000000000000003", meta(self.CODER, "Implement the rest"), [during]),
            ("c0000000000000004", meta(self.CODER, "#12 code"), [named]),
            ("c0000000000000005", meta(self.CODER, "Fix the findings"), [late_coder]),
            ("c0000000000000006", meta("Explore", "Look around"), [explorer]),
            ("c0000000000000007", meta(self.REVIEWER, "Check it again"), [late_reviewer]),
        ], [
            row("code", 1, self.CODER, OPUS, before),
            row("code", 2, self.CODER, OPUS, during),
            row("code", 3, self.CODER, OPUS, named),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, late_coder),
            row("fix", 2, self.REVIEWER, OPUS, late_reviewer),
            row("other", 1, "Explore", OPUS, explorer),
        ])

    def test_continued_test_author_gives_a_second_tests_round(self):
        first = [mk(1, T("10:00:00.000"), T("10:00:30.000")), mk(2, T("10:01:00.000"), T("10:01:20.000"))]
        breaker = [mk(3, T("10:10:00.000"), T("10:10:40.000"))]
        second = [mk(4, T("10:30:00.000"), T("10:30:50.000")), mk(5, T("10:31:00.000"), T("10:31:09.000"), SONNET)]
        review = [mk(6, T("10:40:00.000"), T("10:41:00.000"))]
        third = [mk(7, T("11:00:00.000"), T("11:00:12.000"))]        # tests are never fix
        self.expect([
            ("d0000000000000001", meta(self.AUTHOR, "Write the tests"), [first, second, third]),
            ("d0000000000000002", meta(self.BREAKER, "Break the tests"), [breaker]),
            ("d0000000000000003", meta(self.REVIEWER, "Review the change"), [review]),
        ], [
            row("tests", 1, self.AUTHOR, OPUS, first),
            row("tests", 2, self.AUTHOR, OPUS, second[:1]),
            row("tests", 2, self.AUTHOR, SONNET, second[1:]),
            row("tests", 3, self.AUTHOR, OPUS, third),
            row("breaker", 1, self.BREAKER, OPUS, breaker),
            row("review", 1, self.REVIEWER, OPUS, review),
        ])

    def test_named_first_segment_keeps_its_stage(self):
        review = [mk(1, T("10:20:00.000"), T("10:20:30.000"))]
        coder1 = [mk(2, T("10:30:00.000"), T("10:30:20.000"))]
        coder2 = [mk(3, T("11:00:00.000"), T("11:00:25.000"))]
        named_review1 = [mk(4, T("10:40:00.000"), T("10:40:20.000"))]
        named_review2 = [mk(5, T("11:10:00.000"), T("11:10:15.000"))]
        self.expect([
            ("e0000000000000001", meta(self.REVIEWER, "Review the change"), [review]),
            ("e0000000000000002", meta(self.CODER, "#12 code"), [coder1, coder2]),
            ("e0000000000000003", meta(self.REVIEWER, "#12 review"), [named_review1, named_review2]),
        ], [
            row("code", 1, self.CODER, OPUS, coder1),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("review", 2, self.REVIEWER, OPUS, named_review1),
            row("fix", 1, self.CODER, OPUS, coder2),
            row("fix", 2, self.REVIEWER, OPUS, named_review2),
        ])

    def test_segments_are_timed_apart(self):
        review = [mk(1, T("10:20:00.000"), T("10:20:30.000"))]
        first = [mk(2, T("10:00:00.000"), T("10:00:04.000")), mk(3, T("10:00:05.000"), T("10:00:10.000"))]
        # the first segment starts before the review ends (code); then four hours of waiting and
        # the continuation, which starts after it (fix)
        second = [mk(4, T("14:00:00.000"), T("14:00:06.000")), mk(5, T("14:00:08.000"), T("14:00:20.000"))]
        got = self.expect([
            ("f0000000000000001", meta(self.REVIEWER, "Review the change"), [review]),
            ("f0000000000000002", meta(self.CODER, "Implement it"), [first, second]),
        ], [
            row("code", 1, self.CODER, OPUS, first),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, second),
        ])
        by_stage = {x["stage"]: x for x in got["rows"]}
        self.assertEqual((by_stage["code"]["model_ms"], by_stage["code"]["wall_ms"]), (9000, 10000))
        self.assertEqual((by_stage["fix"]["model_ms"], by_stage["fix"]["wall_ms"]), (18000, 20000))

    # ---------------------------------------------------------- the coordinator entry is the boundary
    def test_only_a_coordinator_entry_splits_a_segment(self):
        review = [mk(1, T("10:05:00.000"), T("10:05:30.000"))]
        # one segment, its messages eight minutes apart (a long tool call): it stays one code segment
        first = [mk(2, T("10:00:00.000"), T("10:00:10.000")), mk(3, T("10:08:00.000"), T("10:08:20.000"))]
        # the continuation's first message starts 1.5 s after the last one ended: still a new segment
        second = [mk(4, T("10:08:21.500"), T("10:08:30.000")), mk(5, T("10:08:35.000"), T("10:08:50.000"))]
        got = self.expect([
            ("h0000000000000001", meta(self.REVIEWER, "Review the change"), [review]),
            ("h0000000000000002", meta(self.CODER, "Implement it"), [first, second]),
        ], [
            row("code", 1, self.CODER, OPUS, first),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, second),
        ])
        by_stage = {x["stage"]: x for x in got["rows"]}
        self.assertEqual((by_stage["code"]["messages"], by_stage["code"]["wall_ms"]), (2, 500000))   # 10:00:00 to 10:08:20
        self.assertEqual((by_stage["fix"]["messages"], by_stage["fix"]["wall_ms"]), (2, 28500))

    def test_continued_coder_segment_starting_before_the_review_ends_stays_code(self):
        code1 = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review = [mk(2, T("10:20:00.000"), T("10:20:30.000")), mk(3, T("10:20:40.000"), T("10:21:00.000"))]
        code2 = [mk(4, T("10:20:30.000"), T("10:20:50.000"))]      # before 10:21:00: still code
        code3 = [mk(5, T("10:30:00.000"), T("10:30:10.000"))]      # after: fix
        self.expect([
            ("i0000000000000001", meta(self.CODER, "Implement it"), [code1, code2, code3]),
            ("i0000000000000002", meta(self.REVIEWER, "Review the change"), [review]),
        ], [
            row("code", 1, self.CODER, OPUS, code1),
            row("code", 2, self.CODER, OPUS, code2),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, code3),
        ])

    def test_named_coder_continued_before_the_review_ends_stays_code(self):
        review = [mk(1, T("10:20:00.000"), T("10:20:30.000")), mk(2, T("10:20:40.000"), T("10:21:00.000"))]
        code1 = [mk(3, T("10:12:00.000"), T("10:12:20.000"))]
        code2 = [mk(4, T("10:20:45.000"), T("10:20:55.000"))]      # before the review ends
        code3 = [mk(5, T("10:40:00.000"), T("10:40:10.000"))]      # after it
        self.expect([
            ("j0000000000000001", meta(self.CODER, "#12 code"), [code1, code2, code3]),
            ("j0000000000000002", meta(self.REVIEWER, "Review the change"), [review]),
        ], [
            row("code", 1, self.CODER, OPUS, code1),
            row("code", 2, self.CODER, OPUS, code2),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, code3),
        ])

    def test_a_segment_starting_exactly_when_the_review_ends_is_not_fix(self):
        code1 = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review = [mk(2, T("10:20:00.000"), T("10:20:30.000")), mk(3, T("10:20:40.000"), T("10:21:00.000"))]
        at_end = [mk(4, T("10:21:00.000"), T("10:21:10.000"))]     # exactly the review's end
        just_after = [mk(5, T("10:21:00.001"), T("10:21:10.000"))]  # one millisecond later
        self.expect([
            ("k0000000000000001", meta(self.CODER, "Implement it"), [code1, at_end]),
            ("k0000000000000002", meta(self.REVIEWER, "Review the change"), [review]),
            ("k0000000000000003", meta(self.CODER, "Implement more"), [just_after]),
        ], [
            row("code", 1, self.CODER, OPUS, code1),
            row("code", 2, self.CODER, OPUS, at_end),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, just_after),
        ])

    def test_fix_rounds_share_one_sequence_by_start(self):
        code1 = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review1 = [mk(2, T("10:20:00.000"), T("10:21:00.000"))]
        recheck1 = [mk(3, T("10:30:00.000"), T("10:30:30.000"))]   # first fix, a reviewer's
        code2 = [mk(4, T("10:40:00.000"), T("10:40:20.000"))]      # second fix, the coder's
        recheck2 = [mk(5, T("10:50:00.000"), T("10:50:05.000"))]   # third, the reviewer's again
        self.expect([
            ("l0000000000000001", meta(self.CODER, "Implement it"), [code1, code2]),
            ("l0000000000000002", meta(self.REVIEWER, "Review the change"), [review1, recheck1, recheck2]),
        ], [
            row("code", 1, self.CODER, OPUS, code1),
            row("review", 1, self.REVIEWER, OPUS, review1),
            row("fix", 1, self.REVIEWER, OPUS, recheck1),
            row("fix", 2, self.CODER, OPUS, code2),
            row("fix", 3, self.REVIEWER, OPUS, recheck2),
        ])

    def test_a_named_review_agent_is_the_first_review(self):
        # the only review is named `#12 review` (and is no reviewer by type): it sets the first review's end
        before = [mk(1, T("10:10:00.000"), T("10:10:20.000"))]
        review = [mk(2, T("10:20:00.000"), T("10:20:30.000")), mk(3, T("10:20:40.000"), T("10:21:00.000"))]
        recheck = [mk(4, T("10:45:00.000"), T("10:45:10.000"))]
        after = [mk(5, T("10:30:00.000"), T("10:30:10.000"))]
        self.expect([
            ("m0000000000000001", meta(self.CODER, "Implement it"), [before]),
            ("m0000000000000002", meta("general-purpose", "#12 review"), [review, recheck]),
            ("m0000000000000003", meta(self.CODER, "Implement more"), [after]),
        ], [
            row("code", 1, self.CODER, OPUS, before),
            row("review", 1, "general-purpose", OPUS, review),
            row("fix", 1, self.CODER, OPUS, after),
            row("fix", 2, "general-purpose", OPUS, recheck),
        ])

    def test_fix_rule_spans_sessions(self):
        # the first review is the ticket's, whichever session holds it: a coder in session 2 that
        # starts after the review (session 1) ended is fix; one that started before it ended is code
        review = [mk(1, T("10:20:00.000"), T("10:20:30.000")), mk(2, T("10:20:40.000"), T("10:21:00.000"))]
        orch2 = Msg("msg_fr_orch2", T("10:00:00.000"), T("10:00:01.000"), (2, 20, 200, 0))
        early = [mk(3, T("10:20:50.000"), T("10:21:30.000"))]      # starts before 10:21:00
        late = [mk(4, T("10:40:00.000"), T("10:40:20.000")), mk(5, T("10:40:30.000"), T("10:40:35.000"))]
        late_review = [mk(6, T("10:50:00.000"), T("10:50:10.000"))]  # a second review: fix too
        d = self.folder("fix2")
        write_session(d, sid(1), turns([self.ORCH], B12), agents=[
            ("n0000000000000001", meta(self.REVIEWER, "Review the change"), segmented([review], B12))])
        write_session(d, sid(2), turns([orch2], B12), agents=[
            ("n0000000000000002", meta(self.CODER, "Implement it"), segmented([early], B12)),
            ("n0000000000000003", meta(self.CODER, "Fix the findings"), segmented([late], B12)),
            ("n0000000000000004", meta(self.REVIEWER, "Check it again"), segmented([late_review], B12))])
        got = self.usage_of(12, [d])
        self.check(got, sort_rows([
            row("code", 1, self.CODER, OPUS, early),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, late),
            row("fix", 2, self.REVIEWER, OPUS, late_review),
            row("orchestrator", 1, "main", OPUS, [self.ORCH, orch2]),
        ]), {sid(1): (True, False), sid(2): (True, False)})

    def test_no_review_means_no_fix(self):
        author = [mk(1, T("10:00:00.000"), T("10:00:05.000"))]
        breaker = [mk(2, T("10:05:00.000"), T("10:05:30.000"))]
        code1 = [mk(3, T("10:10:00.000"), T("10:10:20.000"))]
        code2 = [mk(4, T("10:30:00.000"), T("10:30:15.000"))]
        late_coder = [mk(5, T("10:50:00.000"), T("10:50:10.000"))]
        explorer = [mk(6, T("11:00:00.000"), T("11:00:05.000"))]
        got = self.expect([
            ("g0000000000000001", meta(self.AUTHOR, "Write the tests"), [author]),
            ("g0000000000000002", meta(self.BREAKER, "Break the tests"), [breaker]),
            ("g0000000000000003", meta(self.CODER, "Implement it"), [code1, code2]),
            ("g0000000000000004", meta(self.CODER, "Implement more"), [late_coder]),
            ("g0000000000000005", meta("Explore", "Look around"), [explorer]),
        ], [
            row("tests", 1, self.AUTHOR, OPUS, author),
            row("breaker", 1, self.BREAKER, OPUS, breaker),
            row("code", 1, self.CODER, OPUS, code1),
            row("code", 2, self.CODER, OPUS, code2),
            row("code", 3, self.CODER, OPUS, late_coder),
            row("other", 1, "Explore", OPUS, explorer),
        ])
        self.assertNotIn("fix", {x["stage"] for x in got["rows"]})

    # ---------------------------------------------------------- segments after cross-file dedup
    def test_dedup_of_a_resumed_copy_does_not_change_which_segment_is_the_first(self):
        # `#12 code` in two files: the short copy holds segment 1, the resumed copy (another
        # folder) holds segments 1-2. Segment 1's message is counted once, in whichever file
        # comes first; segment 2 is not segment 1 of the named agent, so it is fix, in either
        # order of the folders.
        review = [mk(1, T("10:20:00.000"), T("10:21:00.000"))]
        seg1 = [mk(2, T("10:10:00.000"), T("10:10:20.000"))]
        seg2 = [mk(3, T("10:30:00.000"), T("10:30:20.000"))]
        short, long = self.folder("short-copy"), self.folder("resumed-copy")
        main = [user_entry(T("09:00:00.000"), B12)]
        write_session(short, sid(1), main, agents=[
            ("r0000000000000001", meta(self.REVIEWER, "Review the change"), segmented([review], B12)),
            ("r0000000000000002", meta(self.CODER, "#12 code"), segmented([seg1], B12))])
        write_session(long, sid(2), main, agents=[
            ("r0000000000000003", meta(self.CODER, "#12 code"), segmented([seg1, seg2], B12))])
        rows = sort_rows([row("code", 1, self.CODER, OPUS, seg1),
                          row("review", 1, self.REVIEWER, OPUS, review),
                          row("fix", 1, self.CODER, OPUS, seg2)])
        sessions = {sid(1): (True, False), sid(2): (True, False)}
        for dirs in ([short, long], [long, short]):
            with self.subTest(dirs=[Path(x).name for x in dirs]):
                self.check(self.usage_of(12, dirs), rows, sessions)

    # ---------------------------------------------------------- min and max, not listed order
    def test_review_end_is_the_latest_end_when_a_copy_stretches_the_first_message(self):
        # the first review's earlier-listed message r1 ends last, through a copy of it (same id)
        # in another folder that ends at 10:25:00; r2, listed last, ends at 10:21:00
        r1 = mk(1, T("10:20:00.000"), T("10:20:30.000"))
        r2 = mk(2, T("10:20:40.000"), T("10:21:00.000"))
        stretched = r1._replace(end=T("10:25:00.000"))
        code = [mk(3, T("10:10:00.000"), T("10:10:20.000"))]
        between = [mk(4, T("10:23:00.000"), T("10:23:30.000"))]   # after r2's end, before r1's: code
        after = [mk(5, T("10:26:00.000"), T("10:26:10.000"))]     # after every end: fix
        first, second = self.folder("review"), self.folder("review-copy")
        main = [user_entry(T("09:00:00.000"), B12)]
        write_session(first, sid(1), main, agents=[
            ("s0000000000000001", meta(self.REVIEWER, "Review the change"), segmented([[r1, r2]], B12))])
        write_session(second, sid(2), main, agents=[
            ("s0000000000000002", meta(self.REVIEWER, "Review the change"), segmented([[stretched]], B12)),
            ("s0000000000000003", meta(self.CODER, "Implement it"), segmented([code], B12)),
            ("s0000000000000004", meta(self.CODER, "Implement more"), segmented([between], B12)),
            ("s0000000000000005", meta(self.CODER, "Fix the findings"), segmented([after], B12))])
        got = self.usage_of(12, [first, second])
        self.check(got, sort_rows([
            row("code", 1, self.CODER, OPUS, code),
            row("code", 2, self.CODER, OPUS, between),
            row("review", 1, self.REVIEWER, OPUS, [stretched, r2]),
            row("fix", 1, self.CODER, OPUS, after),
        ]), {sid(1): (True, False), sid(2): (True, False)})

    def test_review_end_is_the_latest_end_even_when_listed_first(self):
        # r1, listed first, ends at 10:25:00; r2, listed last, ends at 10:21:00
        r1 = mk(1, T("10:20:00.000"), T("10:25:00.000"))
        r2 = mk(2, T("10:20:40.000"), T("10:21:00.000"))
        code = [mk(3, T("10:10:00.000"), T("10:10:20.000"))]
        between = [mk(4, T("10:23:00.000"), T("10:23:30.000"))]
        after = [mk(5, T("10:26:00.000"), T("10:26:10.000"))]
        self.expect([
            ("t0000000000000001", meta(self.REVIEWER, "Review the change"), [[r1, r2]]),
            ("t0000000000000002", meta(self.CODER, "Implement it"), [code]),
            ("t0000000000000003", meta(self.CODER, "Implement more"), [between]),
            ("t0000000000000004", meta(self.CODER, "Fix the findings"), [after]),
        ], [
            row("code", 1, self.CODER, OPUS, code),
            row("code", 2, self.CODER, OPUS, between),
            row("review", 1, self.REVIEWER, OPUS, [r1, r2]),
            row("fix", 1, self.CODER, OPUS, after),
        ])

    def test_segment_start_is_its_earliest_start_whatever_the_listed_order(self):
        # the review ends at 10:20:00. Each coder segment has one message before that and one
        # after, so its true start (the earliest) is before the end: code, in either listed order
        review = [mk(1, T("10:10:00.000"), T("10:20:00.000"))]
        late_first = [mk(2, T("10:30:00.000"), T("10:30:10.000")), mk(3, T("10:15:00.000"), T("10:15:10.000"))]
        early_first = [mk(4, T("10:16:00.000"), T("10:16:10.000")), mk(5, T("10:30:00.000"), T("10:30:10.000"))]
        control = [mk(6, T("10:40:00.000"), T("10:40:10.000")), mk(7, T("10:41:00.000"), T("10:41:10.000"))]
        self.expect([
            ("u0000000000000001", meta(self.REVIEWER, "Review the change"), [review]),
            ("u0000000000000002", meta(self.CODER, "Implement it"), [late_first]),
            ("u0000000000000003", meta(self.CODER, "Implement more"), [early_first]),
            ("u0000000000000004", meta(self.CODER, "Fix the findings"), [control]),
        ], [
            row("code", 1, self.CODER, OPUS, late_first),
            row("code", 2, self.CODER, OPUS, early_first),
            row("review", 1, self.REVIEWER, OPUS, review),
            row("fix", 1, self.CODER, OPUS, control),
        ])


if __name__ == "__main__":
    unittest.main()
