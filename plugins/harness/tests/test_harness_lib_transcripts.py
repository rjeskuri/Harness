"""Tests for harness_lib/transcripts.py (ticket #109, epic #108): the reader under the cost metric.

- session_dirs(main): the main checkout's own folder under ~/.claude/projects first (whether it
  exists or not), then every worktree folder of that repository in name order; the same list
  harness-worklog's transcripts_dirs(main) returns for the same HOME, computed without loading
  harness-worklog (a later ticket makes worklog call this module).
- sessions(dirs): one per <dir>/<id>.jsonl in dir order then by id, with its subagents
  (<dir>/<id>/subagents/agent-<aid>.jsonl, by id) and their parsed meta ({} when missing,
  broken or not an object).
- read_transcript(path): one message per message.id with each usage field the largest among
  that id's entries; start from the api-request entry its requestRef names, else the latest
  user entry before it, else its first entry; end its last entry; segment counted by
  coordinator continuations; branches in order of first appearance; cost_state from the last
  cost-state entry, tokens and API time only, no dollar figure anywhere.
- first_branch(path): the first gitBranch, without reading the rest of the file (a FIFO whose
  writer never closes proves it).

Every fixture is written here by the writers below, in a temporary HOME; the real ~/.claude is
never read. Each rule is checked on at least two hand-written fixtures with different ids, times
and values, every message field asserted, and on sessions generated from a fresh random seed
per run (the `Story` builder records, as it writes each entry, what the reader must return; the
seed is in every failure message). Nothing expected is computed by the code under test.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_transcripts.py" -v
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib and the tool must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
LIB = PLUGIN / "lib"
UTC = dt.timezone.utc

DEF = "t/82-harness-lib-cli-py-and-text-py"
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-5"
FABLE = "claude-fable-5"
MODELS = (OPUS, SONNET, HAIKU, FABLE)
BRANCHES = ("main", "t/3-alpha", "t/14-beta-gamma", "t/101-x", "feature/y", DEF)
BROKEN_LINES = ("{not json", "", "   ", "[1, 2]", "null", "42", '"text"', "}{", b"\xff\xfe{\"type\":",
                '{"type": "assistant", "timestamp": "2026-01-01T00:00:00.000Z", "message": {"id": "msg_cut"',
                '{"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", ')
RANDOM_RUNS = 6


def load_transcripts() -> types.ModuleType:
    """harness_lib.transcripts imported from the checkout's lib/; the import error while the
    module is missing is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.transcripts")


def load_worklog() -> types.ModuleType:
    loader = SourceFileLoader("harness_worklog_transcripts_test", str(BIN / "harness-worklog"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def encode(path):
    """Claude Code's session folder name for a project path, from first principles."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def fresh_seed():
    return random.SystemRandom().randrange(1 << 32)


# ------------------------------------------------------------------ fixture writers
def usage(inp=0, out=0, cw=0, cr=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": out, "cache_creation": {"ephemeral_5m_input_tokens": cw,
                                                     "ephemeral_1h_input_tokens": 0}}


def assistant(ts, mid, use=None, model=OPUS, branch=DEF, ref=None, block="text", **extra):
    """One content block of an API response, the shape CLI 2.1.296/2.1.301 writes."""
    entry = {"type": "assistant", "sessionId": "3f0c1d2e-0000-4000-8000-000000000001",
             "isSidechain": True, "agentId": "aca98b252087cd315", "requestId": "req_" + mid[-6:],
             "message": {"id": mid, "model": model, "usage": use if use is not None else usage(),
                         "content": [{"type": block}]}}
    if ts is not None:
        entry["timestamp"] = ts
    if branch is not None:
        entry["gitBranch"] = branch
    if ref is not None:
        entry["requestRef"] = ref
    entry.update(extra)
    return entry


def api_request(ts, rid, **extra):
    entry = {"type": "api-request", "id": rid, "timestamp": ts,
             "sessionId": "3f0c1d2e-0000-4000-8000-000000000001", "agentId": "aca98b252087cd315",
             "querySource": "agent:custom:harness:test-author"}
    entry.update(extra)
    return entry


def tool_result(ts, **extra):
    entry = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
    if ts is not None:
        entry["timestamp"] = ts
    entry.update(extra)
    return entry


def continuation(ts, kind="coordinator", **extra):
    entry = {"type": "user", "isMeta": True, "origin": {"kind": kind}, "timestamp": ts,
             "message": {"content": "The coordinator sent a message while you were working: go on"}}
    entry.update(extra)
    return entry


def cost_state(api_ms, model_usage, total_cost=45.25, total_duration=19667406):
    """The last line of an ended session (it carries no timestamp)."""
    return {"type": "cost-state", "totalCostUSD": total_cost, "totalAPIDuration": api_ms,
            "totalDuration": total_duration, "modelUsage": model_usage}


def model_usage(inp, out, think, cr, cw, cost):
    return {"inputTokens": inp, "outputTokens": out, "thinkingTokens": think,
            "cacheReadInputTokens": cr, "cacheCreationInputTokens": cw, "costUSD": cost}


def write_jsonl(path, entries):
    """Each entry a JSON line; a str is written as the line itself, bytes as raw bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for entry in entries:
            if isinstance(entry, bytes):
                fh.write(entry + b"\n")
            elif isinstance(entry, str):
                fh.write(entry.encode("utf-8") + b"\n")
            else:
                fh.write(json.dumps(entry).encode("utf-8") + b"\n")
    return str(path)


def write_meta(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def M(mid, start, end, use=(0, 0, 0, 0), model=OPUS, branch=DEF, segment=1):
    """An expected message, every field written out."""
    i, o, w, r = use
    return {"id": mid, "model": model, "start": start, "end": end, "branch": branch, "segment": segment,
            "usage": {"input": i, "output": o, "cache_write": w, "cache_read": r}}


def C(api_ms, **models):
    """An expected cost_state: model=(input, output, thinking, cache_read, cache_write)."""
    return {"api_ms": api_ms, "models": {
        name.replace("_", "-"): {"input": v[0], "output": v[1], "thinking": v[2], "cache_read": v[3], "cache_write": v[4]}
        for name, v in models.items()}}


def walk(value):
    """Every key and every leaf value in a nested result."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield ("key", k)
            yield from walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from walk(v)
    else:
        yield ("value", value)


# ------------------------------------------------------------------ generated sessions
class Story:
    """A transcript written entry by entry from a seed; as it writes, it records the messages,
    branches and cost_state the reader must return, by construction of each entry."""

    FIELDS = (("input_tokens", "input"), ("output_tokens", "output"),
              ("cache_creation_input_tokens", "cache_write"), ("cache_read_input_tokens", "cache_read"))

    def __init__(self, seed):
        self.seed = seed
        self.rng = r = random.Random(seed)
        self.clock = dt.datetime(r.randint(2025, 2027), r.randint(1, 12), r.randint(1, 28),
                                 r.randint(0, 23), r.randint(0, 59), r.randint(0, 59), tzinfo=UTC)
        self.entries = []
        self.messages = []
        self.branches = []
        self.last_user = None
        self.segment = 1
        self.cost = None

    def stamp(self):
        self.clock += dt.timedelta(milliseconds=self.rng.randint(1, 120000))
        c = self.clock
        return c.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (c.microsecond // 1000)

    def hexid(self, n):
        return "".join(self.rng.choice("0123456789abcdef") for _ in range(n))

    def add(self, entry):
        self.entries.append(entry)
        branch = entry.get("gitBranch") if isinstance(entry, dict) else None
        if branch is not None and branch not in self.branches:
            self.branches.append(branch)

    def branch(self):
        return self.rng.choice(BRANCHES + (None,))

    def user(self, coordinator=False):
        ts = self.stamp()
        entry = continuation(ts) if coordinator else tool_result(ts)
        branch = self.branch()
        if branch is not None:
            entry["gitBranch"] = branch
        self.add(entry)
        self.last_user = ts
        if coordinator:
            self.segment += 1

    def noise(self):
        """An entry that changes nothing the reader returns."""
        r = self.rng
        kind = r.randrange(5)
        if kind == 0:
            self.add(r.choice(BROKEN_LINES))
        elif kind == 1:
            self.add(tool_result(None))                                  # no timestamp: never a start
        elif kind == 2:
            self.add({"type": r.choice(("system", "attachment", "summary")), "timestamp": self.stamp()})
        elif kind == 3:
            self.add(assistant(None, "msg_" + self.hexid(18), usage(9, 9, 9, 9), branch=None))  # no message
        else:
            self.add({"type": "system", "origin": {"kind": "coordinator"}, "timestamp": self.stamp()})

    def block_usage(self):
        """A usage object and what each field counts for (missing or not an int counts 0)."""
        r = self.rng
        use, counts = {}, {}
        for raw, name in self.FIELDS:
            value = r.randint(0, 10 ** r.randint(0, 7))
            roll = r.random()
            if roll < 0.08:
                counts[name] = 0                                         # missing
                continue
            if roll < 0.16:
                use[raw] = r.choice((str(value + 1), None, value + 0.5, [value]))
                counts[name] = 0
                continue
            use[raw] = value
            counts[name] = value
        return use, counts

    def responses(self, starts):
        """One response per item of `starts` ("request", "user" or "first"), blocks interleaved."""
        r = self.rng
        pending = []
        for how in starts:
            m = {"id": "msg_01" + self.hexid(22), "model": r.choice(MODELS), "how": how, "ref": None,
                 "start": None, "blocks": r.randint(1, 4), "seen": 0, "segment": self.segment,
                 "usage": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}}
            if how == "request":
                m["ref"] = "%s-%s-%s" % (self.hexid(8), self.hexid(4), self.hexid(12))
                ts = self.stamp()
                entry = api_request(ts, m["ref"])
                branch = self.branch() if r.random() < 0.3 else None
                if branch is not None:
                    entry["gitBranch"] = branch
                self.add(entry)
                m["start"] = ts
            elif how == "user":
                assert self.last_user is not None
                m["start"] = self.last_user
            else:
                assert self.last_user is None, "a first-entry start needs no user entry before it"
            pending.append(m)
        order = [m for m in pending for _ in range(m["blocks"])]
        r.shuffle(order)
        for m in order:
            ts = self.stamp()
            use, counts = self.block_usage()
            branch = self.branch()
            self.add(assistant(ts, m["id"], use, model=m["model"], branch=branch, ref=m["ref"],
                               block=r.choice(("thinking", "text", "tool_use"))))
            if m["seen"] == 0:
                m["branch"] = branch
                if m["how"] == "first":
                    m["start"] = ts
                self.messages.append(m)
            m["seen"] += 1
            m["end"] = ts
            for name, value in counts.items():
                m["usage"][name] = max(m["usage"][name], value)
            if r.random() < 0.2:                                         # no timestamp: not counted
                self.add(assistant(None, m["id"], usage(10 ** 9, 10 ** 9, 10 ** 9, 10 ** 9),
                                   model=m["model"], branch=None, ref=m["ref"]))

    def cost_entry(self):
        r = self.rng
        api_ms = r.randint(1000, 10 ** 8)
        raw, want = {}, {}
        for model in r.sample(MODELS, r.randint(1, 3)):
            v = [r.randint(0, 10 ** 8) for _ in range(5)]
            raw[model] = model_usage(v[0], v[1], v[2], v[3], v[4], round(r.uniform(0.01, 99), 2))
            want[model] = {"input": v[0], "output": v[1], "thinking": v[2], "cache_read": v[3], "cache_write": v[4]}
        self.add(cost_state(api_ms, raw, total_cost=round(r.uniform(1, 500), 2),
                            total_duration=r.randint(api_ms, 2 * api_ms)))
        self.cost = {"api_ms": api_ms, "models": want}

    def expected_messages(self):
        return [{"id": m["id"], "model": m["model"], "start": m["start"], "end": m["end"], "branch": m["branch"],
                 "segment": m["segment"], "usage": dict(m["usage"])} for m in self.messages]

    @classmethod
    def whole_session(cls, seed):
        s = cls(seed)
        r = s.rng
        s.responses(["first"] * r.randint(1, 2))
        s.noise()
        s.user()
        s.responses(r.sample(["user", "request", "request"], r.randint(2, 3)))
        s.cost_entry()                                                   # the session ended once ...
        s.user(coordinator=True)
        s.responses(["request", "request", "user"])
        for _ in range(r.randint(2, 6)):
            roll = r.random()
            if roll < 0.2:
                s.user(coordinator=True)
            elif roll < 0.6:
                s.user()
            else:
                s.noise()
            s.responses([r.choice(("user", "request")) for _ in range(r.randint(1, 3))])
        s.user(coordinator=True)
        s.user(coordinator=True)
        s.responses(["user"])
        s.cost_entry()                                                   # ... and again: the last wins
        return s


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-transcripts-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".claude" / "projects").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t = load_transcripts()

    def path(self, name="session.jsonl"):
        return str(self.tmp / name)

    def assert_no_dollars(self, result, msg=None):
        for kind, item in walk(result):
            if kind == "key":
                self.assertNotRegex(str(item), r"(?i)cost(?!_state)|usd|dollar|price", msg or "a dollar key")
            else:
                self.assertNotIsInstance(item, float, msg or "a float value: dollars are the only floats written")
        text = json.dumps(result)
        self.assertNotIn("costUSD", text)
        self.assertNotIn("totalCostUSD", text)
        self.assertNotIn("totalDuration", text)

    def check(self, name, entries, messages, branches, cost=None):
        """read_transcript of `entries` is exactly the given messages, branches and cost_state."""
        path = write_jsonl(self.path(name), entries)
        result = self.t.read_transcript(path)
        self.assertEqual(result, {"messages": messages, "branches": branches, "cost_state": cost}, name)
        self.assert_no_dollars(result, name)
        return path


# ------------------------------------------------------------------ read_transcript
class ReadTranscript(Base):

    def test_one_message_per_id_with_the_largest_usage(self):
        self.check("a.jsonl", [
            tool_result("2026-10-16T02:32:00.000Z"),
            # one response in three content blocks: output 8, 15660, 15660 -> 15660
            assistant("2026-10-16T02:32:08.575Z", "msg_A", usage(2, 8, 31104, 0), block="thinking"),
            assistant("2026-10-16T02:32:20.100Z", "msg_A", usage(2, 15660, 31104, 0)),
            assistant("2026-10-16T02:32:21.900Z", "msg_A", usage(2, 15660, 31104, 0), block="tool_use"),
            tool_result("2026-10-16T02:32:30.000Z"),
            # each field's largest comes from a different block
            assistant("2026-10-16T02:32:31.000Z", "msg_B", usage(7, 40, 0, 900), model=SONNET),
            assistant("2026-10-16T02:32:32.000Z", "msg_B", usage(3, 512, 220, 100), model=SONNET),
            assistant("2026-10-16T02:32:33.000Z", "msg_B", usage(5, 300, 17, 46133), model=SONNET),
            tool_result("2026-10-16T02:32:40.000Z"),
            # missing fields and non-int fields count 0
            assistant("2026-10-16T02:32:41.000Z", "msg_C",
                      {"input_tokens": "50", "output_tokens": 11, "cache_read_input_tokens": None}),
            assistant("2026-10-16T02:32:42.000Z", "msg_C", {"output_tokens": 9.5, "cache_creation_input_tokens": 64}),
            # no usage at all
            {"type": "assistant", "timestamp": "2026-10-16T02:32:43.000Z", "gitBranch": "main",
             "message": {"id": "msg_D", "model": HAIKU, "content": [{"type": "text"}]}},
            # not messages: an assistant entry without message.id, entries of other types
            {"type": "assistant", "timestamp": "2026-10-16T02:32:44.000Z", "message": {"model": "x"}},
            {"type": "system", "timestamp": "2026-10-16T02:32:45.000Z",
             "message": {"id": "msg_SYS", "usage": usage(1, 1, 1, 1)}},
        ], [
            M("msg_A", "2026-10-16T02:32:00.000Z", "2026-10-16T02:32:21.900Z", (2, 15660, 31104, 0)),
            M("msg_B", "2026-10-16T02:32:30.000Z", "2026-10-16T02:32:33.000Z", (7, 512, 220, 46133), SONNET),
            M("msg_C", "2026-10-16T02:32:40.000Z", "2026-10-16T02:32:42.000Z", (0, 11, 64, 0)),
            M("msg_D", "2026-10-16T02:32:40.000Z", "2026-10-16T02:32:43.000Z", (0, 0, 0, 0), HAIKU, "main"),
        ], [DEF, "main"])
        self.check("b.jsonl", [
            tool_result("2026-09-30T23:59:58.010Z", gitBranch="t/5-five"),
            assistant("2026-09-30T23:59:59.999Z", "msg_01XyZ", usage(40, 3, 0, 70000), FABLE, "t/5-five"),
            assistant("2026-10-01T00:00:03.250Z", "msg_01XyZ", usage(40, 9001, 0, 70000), FABLE, "t/5-five"),
            assistant("2026-10-01T00:00:04.000Z", "msg_01XyZ", usage(41, 120, 5, 69999), FABLE, "t/6-other"),
            assistant("2026-10-01T00:00:04.500Z", "msg_01AbC", usage(6, 77, 1200, 3), HAIKU, None),
            assistant("2026-10-01T00:00:09.000Z", "msg_01AbC",
                      {"input_tokens": 5, "output_tokens": 78, "cache_creation_input_tokens": "1300",
                       "cache_read_input_tokens": 2}, HAIKU, None),
        ], [
            M("msg_01XyZ", "2026-09-30T23:59:58.010Z", "2026-10-01T00:00:04.000Z", (41, 9001, 5, 70000), FABLE, "t/5-five"),
            M("msg_01AbC", "2026-09-30T23:59:58.010Z", "2026-10-01T00:00:09.000Z", (6, 78, 1200, 3), HAIKU, None),
        ], ["t/5-five", "t/6-other"])

    def test_usage_takes_the_largest_of_each_field_over_varied_blocks(self):
        # a table of block usages; the expected maxima are written out by hand
        cases = [
            ([(1, 2, 3, 4)], (1, 2, 3, 4)),
            ([(9, 0, 0, 0), (0, 9, 0, 0), (0, 0, 9, 0), (0, 0, 0, 9)], (9, 9, 9, 9)),
            ([(100, 1, 5000, 20), (3, 777, 4999, 21), (99, 776, 1, 19)], (100, 777, 5000, 21)),
            ([(0, 0, 0, 0), (0, 0, 0, 0)], (0, 0, 0, 0)),
            ([(5, 6, 7, 8), (4, 5, 6, 7), (6, 4, 8, 5)], (6, 6, 8, 8)),
        ]
        entries, want = [], []
        for n, (blocks, maxima) in enumerate(cases):
            entries.append(tool_result(f"2026-10-17T10:{n:02d}:00.000Z"))
            for b, (i, o, w, r) in enumerate(blocks):
                entries.append(assistant(f"2026-10-17T10:{n:02d}:{b + 1:02d}.000Z", f"msg_T{n}", usage(i, o, w, r)))
            want.append(M(f"msg_T{n}", f"2026-10-17T10:{n:02d}:00.000Z",
                          f"2026-10-17T10:{n:02d}:{len(blocks):02d}.000Z", maxima))
        self.check("table.jsonl", entries, want, [DEF])

    def test_model_time_starts_at_the_api_request_entry(self):
        self.check("a.jsonl", [
            tool_result("2026-10-16T02:32:00.000Z"),
            api_request("2026-10-16T02:32:07.145Z", "32b5e029-aaaa"),
            api_request("2026-10-16T02:32:07.900Z", "77c1d3f0-bbbb"),   # a second request in flight
            assistant("2026-10-16T02:32:08.575Z", "msg_A", usage(2, 8, 31104, 0), ref="32b5e029-aaaa"),
            assistant("2026-10-16T02:32:09.010Z", "msg_B", usage(1, 4, 0, 0), ref="77c1d3f0-bbbb"),
            assistant("2026-10-16T02:32:21.900Z", "msg_A", usage(2, 15660, 31104, 0), ref="32b5e029-aaaa"),
            assistant("2026-10-16T02:32:25.250Z", "msg_B", usage(1, 90, 0, 0), ref="77c1d3f0-bbbb"),
            tool_result("2026-10-16T02:33:00.000Z"),
            api_request("2026-10-16T02:33:01.500Z", "c0ffee00-cccc"),
            assistant("2026-10-16T02:33:04.000Z", "msg_C", usage(1, 1, 0, 0), ref="c0ffee00-cccc"),
        ], [
            M("msg_A", "2026-10-16T02:32:07.145Z", "2026-10-16T02:32:21.900Z", (2, 15660, 31104, 0)),
            M("msg_B", "2026-10-16T02:32:07.900Z", "2026-10-16T02:32:25.250Z", (1, 90, 0, 0)),
            M("msg_C", "2026-10-16T02:33:01.500Z", "2026-10-16T02:33:04.000Z", (1, 1, 0, 0)),
        ], [DEF])
        # three requests in flight, answered in another order than they were sent
        self.check("b.jsonl", [
            continuation("2026-11-02T14:00:00.000Z", kind="human"),
            api_request("2026-11-02T14:00:01.111Z", "aaaa0001"),
            api_request("2026-11-02T14:00:02.222Z", "bbbb0002"),
            api_request("2026-11-02T14:00:03.333Z", "cccc0003"),
            assistant("2026-11-02T14:00:04.444Z", "msg_r3", usage(3, 30, 300, 3000), SONNET, "t/9-nine", ref="cccc0003"),
            assistant("2026-11-02T14:00:05.555Z", "msg_r1", usage(1, 10, 100, 1000), SONNET, "t/9-nine", ref="aaaa0001"),
            assistant("2026-11-02T14:00:06.666Z", "msg_r3", usage(3, 31, 300, 3000), SONNET, "t/9-nine", ref="cccc0003"),
            assistant("2026-11-02T14:00:07.777Z", "msg_r2", usage(2, 20, 200, 2000), SONNET, "t/9-nine", ref="bbbb0002"),
            assistant("2026-11-02T14:00:08.888Z", "msg_r1", usage(1, 11, 100, 1000), SONNET, "t/9-nine", ref="aaaa0001"),
        ], [
            M("msg_r3", "2026-11-02T14:00:03.333Z", "2026-11-02T14:00:06.666Z", (3, 31, 300, 3000), SONNET, "t/9-nine"),
            M("msg_r1", "2026-11-02T14:00:01.111Z", "2026-11-02T14:00:08.888Z", (1, 11, 100, 1000), SONNET, "t/9-nine"),
            M("msg_r2", "2026-11-02T14:00:02.222Z", "2026-11-02T14:00:07.777Z", (2, 20, 200, 2000), SONNET, "t/9-nine"),
        ], ["t/9-nine"])

    def test_without_api_request_the_start_is_the_preceding_user_entry(self):
        # 2.1.296: no api-request entries and no requestRef
        self.check("a.jsonl", [
            continuation("2026-10-15T08:00:00.000Z", kind="human"),
            tool_result("2026-10-15T08:00:01.000Z"),
            assistant("2026-10-15T08:00:05.000Z", "msg_1", usage(1, 2, 3, 4), block="thinking"),
            tool_result("2026-10-15T08:00:06.000Z"),     # after msg_1's first entry: not its start
            assistant("2026-10-15T08:00:09.000Z", "msg_1", usage(1, 20, 3, 4)),
            {"type": "system", "timestamp": "2026-10-15T08:00:10.000Z", "subtype": "info"},
            tool_result("2026-10-15T08:00:12.000Z"),
            {"type": "attachment", "timestamp": "2026-10-15T08:00:13.000Z"},
            assistant("2026-10-15T08:00:15.500Z", "msg_2", usage(1, 2, 3, 4)),
            assistant("2026-10-15T08:00:30.250Z", "msg_2", usage(1, 2, 3, 4)),
            assistant("2026-10-15T08:00:31.000Z", "msg_3", usage(1, 2, 3, 4)),   # no user entry since msg_2
            # a requestRef with no api-request entry in the file falls back the same way
            tool_result("2026-10-15T08:01:00.000Z"),
            assistant("2026-10-15T08:01:02.000Z", "msg_4", usage(1, 2, 3, 4), ref="no-such-request"),
        ], [
            M("msg_1", "2026-10-15T08:00:01.000Z", "2026-10-15T08:00:09.000Z", (1, 20, 3, 4)),
            M("msg_2", "2026-10-15T08:00:12.000Z", "2026-10-15T08:00:30.250Z", (1, 2, 3, 4)),
            M("msg_3", "2026-10-15T08:00:12.000Z", "2026-10-15T08:00:31.000Z", (1, 2, 3, 4)),
            M("msg_4", "2026-10-15T08:01:00.000Z", "2026-10-15T08:01:02.000Z", (1, 2, 3, 4)),
        ], [DEF])
        self.check("b.jsonl", [
            tool_result("2026-03-04T05:06:07.080Z"),
            tool_result("2026-03-04T05:06:07.090Z"),            # the latest one counts
            assistant("2026-03-04T05:06:10.000Z", "msg_u1", usage(4, 44, 0, 0), HAIKU, "main"),
            tool_result("2026-03-04T05:07:00.500Z", gitBranch="t/2-two"),
            assistant("2026-03-04T05:07:02.000Z", "msg_u2", usage(5, 55, 0, 0), HAIKU, "t/2-two"),
            assistant("2026-03-04T05:07:03.000Z", "msg_u2", usage(5, 56, 0, 0), HAIKU, "t/2-two"),
        ], [
            M("msg_u1", "2026-03-04T05:06:07.090Z", "2026-03-04T05:06:10.000Z", (4, 44, 0, 0), HAIKU, "main"),
            M("msg_u2", "2026-03-04T05:07:00.500Z", "2026-03-04T05:07:03.000Z", (5, 56, 0, 0), HAIKU, "t/2-two"),
        ], ["main", "t/2-two"])

    def test_without_a_user_entry_the_start_is_the_first_entry(self):
        self.check("a.jsonl", [
            {"type": "system", "timestamp": "2026-10-15T09:00:00.000Z"},
            assistant("2026-10-15T09:00:04.125Z", "msg_x", usage(1, 1, 1, 1)),
            assistant("2026-10-15T09:00:07.000Z", "msg_x", usage(1, 5, 1, 1)),
            assistant("2026-10-15T09:00:07.500Z", "msg_y", usage(1, 5, 1, 1)),
        ], [
            M("msg_x", "2026-10-15T09:00:04.125Z", "2026-10-15T09:00:07.000Z", (1, 5, 1, 1)),
            M("msg_y", "2026-10-15T09:00:07.500Z", "2026-10-15T09:00:07.500Z", (1, 5, 1, 1)),
        ], [DEF])
        self.check("b.jsonl", [
            assistant("2026-12-31T23:59:59.900Z", "msg_f1", usage(8, 1, 0, 0), FABLE, None),
            assistant("2027-01-01T00:00:00.100Z", "msg_f1", usage(8, 2, 0, 0), FABLE, "t/4-four"),
            tool_result("2027-01-01T00:00:05.000Z"),
            assistant("2027-01-01T00:00:06.000Z", "msg_f2", usage(9, 9, 9, 9), FABLE, "t/4-four"),
        ], [
            M("msg_f1", "2026-12-31T23:59:59.900Z", "2027-01-01T00:00:00.100Z", (8, 2, 0, 0), FABLE, None),
            M("msg_f2", "2027-01-01T00:00:05.000Z", "2027-01-01T00:00:06.000Z", (9, 9, 9, 9), FABLE, "t/4-four"),
        ], ["t/4-four"])

    def test_continuation_prompt_starts_a_new_segment(self):
        self.check("a.jsonl", [
            tool_result("2026-10-16T01:00:00.000Z"),
            assistant("2026-10-16T01:00:01.000Z", "msg_1"),
            tool_result("2026-10-16T01:00:02.000Z"),
            assistant("2026-10-16T01:00:03.000Z", "msg_2"),
            continuation("2026-10-16T01:10:00.000Z"),
            assistant("2026-10-16T01:10:01.000Z", "msg_3"),
            tool_result("2026-10-16T01:10:02.000Z"),
            assistant("2026-10-16T01:10:03.000Z", "msg_4"),
            continuation("2026-10-16T01:10:04.000Z", kind="human"),          # not the coordinator
            {"type": "user", "isMeta": True, "timestamp": "2026-10-16T01:10:05.000Z",
             "message": {"content": "meta without origin"}},
            {"type": "system", "origin": {"kind": "coordinator"}, "timestamp": "2026-10-16T01:10:06.000Z"},
            assistant("2026-10-16T01:10:07.000Z", "msg_5", origin={"kind": "coordinator"}),  # not a user entry
            continuation("2026-10-16T01:20:00.000Z"),
            continuation("2026-10-16T01:20:01.000Z"),
            assistant("2026-10-16T01:20:02.000Z", "msg_6"),
            continuation("2026-10-16T01:30:00.000Z"),
            assistant("2026-10-16T01:30:01.000Z", "msg_7"),
        ], [
            M("msg_1", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:01.000Z", segment=1),
            M("msg_2", "2026-10-16T01:00:02.000Z", "2026-10-16T01:00:03.000Z", segment=1),
            M("msg_3", "2026-10-16T01:10:00.000Z", "2026-10-16T01:10:01.000Z", segment=2),
            M("msg_4", "2026-10-16T01:10:02.000Z", "2026-10-16T01:10:03.000Z", segment=2),
            M("msg_5", "2026-10-16T01:10:05.000Z", "2026-10-16T01:10:07.000Z", segment=2),
            M("msg_6", "2026-10-16T01:20:01.000Z", "2026-10-16T01:20:02.000Z", segment=4),
            M("msg_7", "2026-10-16T01:30:00.000Z", "2026-10-16T01:30:01.000Z", segment=5),
        ], [DEF])
        # a continuation before the first message; api-request starts within segments
        self.check("b.jsonl", [
            continuation("2026-05-05T05:05:05.005Z"),
            assistant("2026-05-05T05:05:06.000Z", "msg_s1", usage(1, 1, 1, 1), SONNET, "t/77-s"),
            api_request("2026-05-05T05:06:00.000Z", "req-s2"),
            assistant("2026-05-05T05:06:01.000Z", "msg_s2", usage(2, 2, 2, 2), SONNET, "t/77-s", ref="req-s2"),
            continuation("2026-05-05T05:07:00.000Z"),
            api_request("2026-05-05T05:07:01.000Z", "req-s3"),
            assistant("2026-05-05T05:07:02.000Z", "msg_s3", usage(3, 3, 3, 3), SONNET, "t/77-s", ref="req-s3"),
        ], [
            M("msg_s1", "2026-05-05T05:05:05.005Z", "2026-05-05T05:05:06.000Z", (1, 1, 1, 1), SONNET, "t/77-s", 2),
            M("msg_s2", "2026-05-05T05:06:00.000Z", "2026-05-05T05:06:01.000Z", (2, 2, 2, 2), SONNET, "t/77-s", 2),
            M("msg_s3", "2026-05-05T05:07:01.000Z", "2026-05-05T05:07:02.000Z", (3, 3, 3, 3), SONNET, "t/77-s", 3),
        ], ["t/77-s"])

    def test_cost_state_keeps_tokens_and_api_time_only(self):
        self.check("a.jsonl", [
            tool_result("2026-10-16T01:00:00.000Z"),
            assistant("2026-10-16T01:00:01.000Z", "msg_1", usage(2, 3, 4, 5)),
            cost_state(1111, {SONNET: model_usage(1, 2, 3, 4, 5, 7.13)}, total_cost=12.75),
            tool_result("2026-10-16T02:00:00.000Z"),     # the session was resumed
            assistant("2026-10-16T02:00:01.000Z", "msg_2", usage(2, 3, 4, 5)),
            cost_state(4384078, {
                OPUS: model_usage(79180, 218152, 74088, 26257320, 1745877, 18.99),
                HAIKU: model_usage(512, 64, 0, 9000, 300, 3.5),
            }),
        ], [
            M("msg_1", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:01.000Z", (2, 3, 4, 5)),
            M("msg_2", "2026-10-16T02:00:00.000Z", "2026-10-16T02:00:01.000Z", (2, 3, 4, 5)),
        ], [DEF], C(4384078, claude_opus_5_5=(79180, 218152, 74088, 26257320, 1745877),
                    claude_haiku_5=(512, 64, 0, 9000, 300)))
        self.check("b.jsonl", [
            tool_result("2026-08-08T08:08:08.008Z"),
            assistant("2026-08-08T08:08:09.000Z", "msg_c1", usage(11, 22, 33, 44), FABLE, "t/31-c"),
            cost_state(222, {FABLE: model_usage(5, 6, 7, 8, 9, 0.42)}, total_cost=0.42),
            cost_state(98765, {FABLE: model_usage(11, 22, 1, 44, 33, 1.25)}, total_cost=1.25, total_duration=100000),
        ], [
            M("msg_c1", "2026-08-08T08:08:08.008Z", "2026-08-08T08:08:09.000Z", (11, 22, 33, 44), FABLE, "t/31-c"),
        ], ["t/31-c"], C(98765, claude_fable_5=(11, 22, 1, 44, 33)))

    def test_no_cost_state_entry_gives_none(self):
        self.check("a.jsonl", [tool_result("2026-10-16T01:00:00.000Z"),
                               assistant("2026-10-16T01:00:01.000Z", "msg_1")],
                   [M("msg_1", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:01.000Z")], [DEF])
        self.check("b.jsonl", [
            tool_result("2026-06-01T12:00:00.250Z", gitBranch="t/60-n"),
            api_request("2026-06-01T12:00:01.000Z", "n-1"),
            assistant("2026-06-01T12:00:03.000Z", "msg_n1", usage(6, 60, 600, 6000), SONNET, "t/60-n", ref="n-1"),
            {"type": "summary", "timestamp": "2026-06-01T12:00:04.000Z", "totalCostUSD": 9.5},
        ], [M("msg_n1", "2026-06-01T12:00:01.000Z", "2026-06-01T12:00:03.000Z", (6, 60, 600, 6000), SONNET, "t/60-n")],
            ["t/60-n"])

    def test_branches_in_order_of_first_appearance(self):
        a = self.check("a.jsonl", [
            tool_result("2026-10-16T01:00:00.000Z", gitBranch="main"),
            assistant("2026-10-16T01:00:01.000Z", "msg_1", branch="main"),
            tool_result("2026-10-16T01:00:02.000Z"),                        # no gitBranch
            api_request("2026-10-16T01:00:03.000Z", "r-1", gitBranch="t/9-zeta"),
            assistant("2026-10-16T01:00:04.000Z", "msg_2", branch="t/9-zeta", ref="r-1"),
            assistant("2026-10-16T01:00:05.000Z", "msg_2", branch="t/3-alpha", ref="r-1"),
            tool_result("2026-10-16T01:00:06.000Z", gitBranch="main"),
            assistant("2026-10-16T01:00:07.000Z", "msg_3", branch=None),
            assistant("2026-10-16T01:00:08.000Z", "msg_4", branch="t/3-alpha"),
            tool_result("2026-10-16T01:00:09.000Z", gitBranch="t/12-mid"),
        ], [
            M("msg_1", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:01.000Z", branch="main"),
            M("msg_2", "2026-10-16T01:00:03.000Z", "2026-10-16T01:00:05.000Z", branch="t/9-zeta"),
            M("msg_3", "2026-10-16T01:00:06.000Z", "2026-10-16T01:00:07.000Z", branch=None),
            M("msg_4", "2026-10-16T01:00:06.000Z", "2026-10-16T01:00:08.000Z", branch="t/3-alpha"),
        ], ["main", "t/9-zeta", "t/3-alpha", "t/12-mid"])
        self.assertEqual(self.t.first_branch(a), "main")
        b = self.check("b.jsonl", [
            api_request("2026-02-02T02:02:02.002Z", "q1", gitBranch="t/50-b"),
            assistant("2026-02-02T02:02:03.000Z", "msg_b1", usage(1, 2, 3, 4), ref="q1", branch="t/50-b"),
            tool_result("2026-02-02T02:03:00.000Z", gitBranch="t/50-b"),
            assistant("2026-02-02T02:03:01.000Z", "msg_b2", usage(4, 3, 2, 1), branch="main"),
            tool_result("2026-02-02T02:04:00.000Z", gitBranch="t/50-b"),
        ], [
            M("msg_b1", "2026-02-02T02:02:02.002Z", "2026-02-02T02:02:03.000Z", (1, 2, 3, 4), branch="t/50-b"),
            M("msg_b2", "2026-02-02T02:03:00.000Z", "2026-02-02T02:03:01.000Z", (4, 3, 2, 1), branch="main"),
        ], ["t/50-b", "main"])
        self.assertEqual(self.t.first_branch(b), "t/50-b")

    def test_broken_lines_and_missing_files_are_skipped(self):
        self.check("a.jsonl", [
            "{not json at all",
            "",
            "   ",
            "[1, 2, 3]",
            '"just a string"',
            "42",
            "null",
            b"\xff\xfe{\"type\": \"assistant\"",                                        # undecodable bytes
            tool_result("2026-10-16T01:00:00.000Z"),
            assistant("2026-10-16T01:00:01.000Z", "msg_1", usage(1, 10, 100, 1000)),
            {"type": "assistant", "timestamp": "2026-10-16T01:00:02.000Z", "message": "not an object"},
            {"type": "assistant", "timestamp": "2026-10-16T01:00:02.500Z",
             "message": {"id": "msg_n", "model": HAIKU, "usage": None}},
            assistant(None, "msg_1", usage(9, 99999, 9, 9), branch=None),    # no timestamp: skipped
            assistant(None, "msg_ghost", usage(5, 5, 5, 5), branch=None),    # no timestamp: no message at all
            tool_result(None),                                               # no timestamp: not a start
            assistant("2026-10-16T01:00:03.000Z", "msg_1", usage(1, 12, 100, 1000)),
            assistant("2026-10-16T01:00:04.000Z", "msg_2", usage(2, 2, 2, 2)),
            '{"type": "assistant", "timestamp": "2026-10-16T01:00:05.000Z", "message": {"id": "msg_3"',  # truncated
        ], [
            M("msg_1", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:03.000Z", (1, 12, 100, 1000)),
            M("msg_n", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:02.500Z", (0, 0, 0, 0), HAIKU, None),
            M("msg_2", "2026-10-16T01:00:00.000Z", "2026-10-16T01:00:04.000Z", (2, 2, 2, 2)),
        ], [DEF])
        self.check("b.jsonl", [
            b"\x80\x81\x82",
            tool_result("2026-07-07T07:00:00.000Z"),
            '{"type": "user", "timestamp": "2026-07-07T07:00:30.000Z", ',             # truncated: not a start
            assistant("2026-07-07T07:01:00.000Z", "msg_k1", usage(3, 3, 3, 3), SONNET, "t/8-k"),
            "}{",
            assistant(None, "msg_k1", usage(100, 100, 100, 100), SONNET, None),
            assistant("2026-07-07T07:01:05.000Z", "msg_k1", usage(3, 4, 3, 3), SONNET, "t/8-k"),
            '{"type": "cost-state", "totalAPIDuration": 5, "modelUsage": {',            # truncated: no cost_state
        ], [M("msg_k1", "2026-07-07T07:00:00.000Z", "2026-07-07T07:01:05.000Z", (3, 4, 3, 3), SONNET, "t/8-k")],
            ["t/8-k"])
        nothing = {"messages": [], "branches": [], "cost_state": None}
        for name in ("no-such-session.jsonl", "gone/also-missing.jsonl"):
            self.assertEqual(self.t.read_transcript(self.path(name)), nothing)
            self.assertIsNone(self.t.first_branch(self.path(name)))
        self.assertEqual(self.t.read_transcript(write_jsonl(self.path("empty.jsonl"), [])), nothing)
        self.assertEqual(self.t.read_transcript(write_jsonl(self.path("junk.jsonl"), list(BROKEN_LINES))), nothing)

    def test_a_whole_session_with_every_rule(self):
        path = self.check("whole.jsonl", [
            assistant("2026-10-18T09:00:00.100Z", "msg_w1", usage(3, 10, 500, 0), OPUS, "main"),   # first-entry start
            assistant("2026-10-18T09:00:02.000Z", "msg_w1", usage(3, 250, 500, 0), OPUS, "main"),
            "{broken",
            tool_result("2026-10-18T09:00:05.000Z", gitBranch="main"),
            assistant("2026-10-18T09:00:07.500Z", "msg_w2", usage(1, 40, 0, 900), OPUS, "main"),  # user-entry start
            cost_state(9000, {OPUS: model_usage(4, 290, 12, 900, 500, 2.5)}, total_cost=2.5),
            continuation("2026-10-18T10:00:00.000Z"),                                             # segment 2
            api_request("2026-10-18T10:00:00.800Z", "w-req-3", gitBranch="t/120-w"),
            assistant("2026-10-18T10:00:02.000Z", "msg_w3", usage(2, 5, 7000, 900), SONNET, "t/120-w", ref="w-req-3"),
            assistant(None, "msg_w3", usage(99, 99999, 99, 99), SONNET, None, ref="w-req-3"),     # no timestamp
            assistant("2026-10-18T10:00:09.000Z", "msg_w3", usage(2, 600, 7000, 900), SONNET, "t/120-w", ref="w-req-3"),
            tool_result("2026-10-18T10:00:10.000Z", gitBranch="t/120-w"),
            assistant("2026-10-18T10:00:11.000Z", "msg_w4", usage(9, 9, 0, 7900), SONNET, "t/120-w", ref="w-missing"),
            cost_state(25000, {OPUS: model_usage(4, 290, 12, 900, 500, 2.5),
                               SONNET: model_usage(13, 614, 40, 9700, 7000, 3.75)}, total_cost=6.25),
        ], [
            M("msg_w1", "2026-10-18T09:00:00.100Z", "2026-10-18T09:00:02.000Z", (3, 250, 500, 0), OPUS, "main", 1),
            M("msg_w2", "2026-10-18T09:00:05.000Z", "2026-10-18T09:00:07.500Z", (1, 40, 0, 900), OPUS, "main", 1),
            M("msg_w3", "2026-10-18T10:00:00.800Z", "2026-10-18T10:00:09.000Z", (2, 600, 7000, 900), SONNET, "t/120-w", 2),
            M("msg_w4", "2026-10-18T10:00:10.000Z", "2026-10-18T10:00:11.000Z", (9, 9, 0, 7900), SONNET, "t/120-w", 2),
        ], ["main", "t/120-w"],
            C(25000, claude_opus_5_5=(4, 290, 12, 900, 500), claude_sonnet_5=(13, 614, 40, 9700, 7000)))
        self.assertEqual(self.t.first_branch(path), "main")

    def test_generated_sessions(self):
        for run in range(RANDOM_RUNS):
            seed = fresh_seed()
            story = Story.whole_session(seed)
            with self.subTest(run=run, seed=seed):
                path = write_jsonl(self.path(f"gen-{run}.jsonl"), story.entries)
                result = self.t.read_transcript(path)
                msg = f"seed {seed}"
                self.assertEqual(result["messages"], story.expected_messages(), msg)
                self.assertEqual(result["branches"], story.branches, msg)
                self.assertEqual(result["cost_state"], story.cost, msg)
                self.assertEqual(set(result), {"messages", "branches", "cost_state"}, msg)
                self.assert_no_dollars(result, msg)
                self.assertEqual(self.t.first_branch(path), story.branches[0] if story.branches else None, msg)

    def test_first_branch_is_the_first_entry_that_has_one(self):
        cases = [
            ([tool_result("2026-10-16T01:00:00.000Z"),
              assistant("2026-10-16T01:00:01.000Z", "m", branch="t/40-forty"),
              assistant("2026-10-16T01:00:02.000Z", "n", branch="main")], "t/40-forty"),
            (["{not json", "[1, 2]", "",
              tool_result("2026-10-16T01:00:00.000Z", gitBranch="t/41-x"),
              tool_result("2026-10-16T01:00:01.000Z", gitBranch="t/42-y")], "t/41-x"),
            ([api_request("2026-10-16T01:00:00.000Z", "r", gitBranch="feature/z"),
              assistant("2026-10-16T01:00:01.000Z", "m", branch="main")], "feature/z"),
            ([tool_result("2026-10-16T01:00:00.000Z"), assistant("2026-10-16T01:00:01.000Z", "m", branch=None)], None),
            ([], None),
        ]
        for n, (entries, want) in enumerate(cases):
            with self.subTest(case=n):
                self.assertEqual(self.t.first_branch(write_jsonl(self.path(f"fb{n}.jsonl"), entries)), want)
        self.assertIsNone(self.t.first_branch(self.path("missing.jsonl")))

    def test_first_branch_does_not_read_the_rest_of_the_file(self):
        # A FIFO whose writer stays open: reading to the end of the file would block until the
        # writer closes, so first_branch must return while the rest is still unwritten.
        for n, (first, later) in enumerate((("t/7-first", "t/8-later"), ("main", "t/9-other"))):
            with self.subTest(case=n):
                self._first_branch_on_a_fifo(n, first, later)

    def _first_branch_on_a_fifo(self, n, first, later):
        fifo = self.path(f"live-{n}.jsonl")
        os.mkfifo(fifo)
        release = threading.Event()
        head = (json.dumps(tool_result("2026-10-16T01:00:00.000Z")) + "\n"
                + json.dumps(assistant("2026-10-16T01:00:01.000Z", f"msg_{n}a", branch=first)) + "\n"
                + json.dumps(assistant("2026-10-16T01:00:02.000Z", f"msg_{n}b", branch=later)) + "\n")
        writer_opened = threading.Event()

        def writer():
            try:
                with open(fifo, "w", encoding="utf-8") as fh:
                    writer_opened.set()
                    fh.write(head)
                    fh.flush()
                    release.wait(15)
                    fh.write("never read\n")
            except OSError:        # the reader closed early (BrokenPipeError): that is the point
                pass

        result = {}

        def reader():
            try:
                result["branch"] = self.t.first_branch(fifo)
            except BaseException as err:   # reported below
                result["error"] = err

        wt = threading.Thread(target=writer, daemon=True)
        rt = threading.Thread(target=reader, daemon=True)
        wt.start()
        rt.start()
        rt.join(5)
        finished = not rt.is_alive()
        release.set()
        if not writer_opened.is_set():    # the reader never opened the FIFO: unblock the writer
            fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            writer_opened.wait(5)
            os.close(fd)
        rt.join(5)
        wt.join(5)
        self.assertNotIn("error", result)
        self.assertTrue(finished, "first_branch kept reading after the first gitBranch")
        self.assertEqual(result.get("branch"), first)


# ------------------------------------------------------------------ sessions
class Sessions(Base):

    def test_each_session_with_its_subagents_and_metas(self):
        d1 = self.tmp / "projects-a"
        d2 = self.tmp / "projects-b"
        missing = self.tmp / "projects-gone"
        # d2 comes first in the list although its ids sort after d1's: dir order, then id order
        write_jsonl(d2 / "zz-main.jsonl", [tool_result("2026-10-16T01:00:00.000Z")])
        write_jsonl(d2 / "aa-other.jsonl", [tool_result("2026-10-16T01:00:00.000Z")])
        write_jsonl(d1 / "0001.jsonl", [tool_result("2026-10-16T01:00:00.000Z")])
        (d1 / "notes.txt").write_text("not a session\n", encoding="utf-8")
        (d1 / "memory").mkdir()                                          # a folder without a .jsonl
        write_jsonl(d1 / "memory" / "subagents" / "agent-zzz.jsonl", [])   # not a session's subagent
        # zz-main's subagents: valid, missing, broken, non-object and unreadable metas
        sub = d2 / "zz-main" / "subagents"
        write_jsonl(sub / "agent-c3.jsonl", [])
        write_meta(sub / "agent-c3.meta.json", {"agentType": "harness:coder", "description": "#82 code",
                                                "toolUseId": "toolu_01", "model": "opus", "effort": "high"})
        write_jsonl(sub / "agent-a1.jsonl", [])                          # no meta
        write_jsonl(sub / "agent-b2.jsonl", [])
        write_meta(sub / "agent-b2.meta.json", "{broken")
        write_jsonl(sub / "agent-d4.jsonl", [])
        write_meta(sub / "agent-d4.meta.json", ["harness:reviewer"])
        write_jsonl(sub / "agent-e5.jsonl", [])
        (sub / "agent-e5.meta.json").mkdir()                             # cannot be read as a file
        write_jsonl(sub / "agent-f6.jsonl", [])
        write_meta(sub / "agent-f6.meta.json", '"harness:planner"')
        write_meta(sub / "agent-g7.meta.json", {"agentType": "orphan"})  # a meta without its transcript
        (sub / "notes.txt").write_text("x", encoding="utf-8")
        write_jsonl(sub / "other.jsonl", [])                             # not agent-<aid>.jsonl
        # aa-other's subagent: one meta
        write_jsonl(d2 / "aa-other" / "subagents" / "agent-0x.jsonl", [])
        write_meta(d2 / "aa-other" / "subagents" / "agent-0x.meta.json", {"agentType": "harness:test-author"})

        got = self.t.sessions([str(d2), str(missing), str(d1)])

        def agent(aid, meta, session="zz-main", folder=d2):
            return {"id": aid, "path": str(folder / session / "subagents" / f"agent-{aid}.jsonl"), "meta": meta}

        self.assertEqual(got, [
            {"id": "aa-other", "path": str(d2 / "aa-other.jsonl"),
             "agents": [agent("0x", {"agentType": "harness:test-author"}, "aa-other")]},
            {"id": "zz-main", "path": str(d2 / "zz-main.jsonl"), "agents": [
                agent("a1", {}),
                agent("b2", {}),
                agent("c3", {"agentType": "harness:coder", "description": "#82 code",
                             "toolUseId": "toolu_01", "model": "opus", "effort": "high"}),
                agent("d4", {}),
                agent("e5", {}),
                agent("f6", {}),
            ]},
            {"id": "0001", "path": str(d1 / "0001.jsonl"), "agents": []},
        ])

    def test_missing_or_empty_folders_give_nothing(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.t.sessions([]), [])
        self.assertEqual(self.t.sessions([str(self.tmp / "nope"), str(empty)]), [])

    def test_sessions_varied_ids_sorted_within_a_folder(self):
        d = self.tmp / "projects-c"
        ids = ["f0e1", "0b2c", "a-1", "9", "a-0", "B7"]
        for sid in ids:
            write_jsonl(d / f"{sid}.jsonl", [])
        got = self.t.sessions([str(d)])
        self.assertEqual(got, [{"id": sid, "path": str(d / f"{sid}.jsonl"), "agents": []} for sid in sorted(ids)])

    def test_generated_layouts(self):
        for run in range(RANDOM_RUNS):
            seed = fresh_seed()
            rng = random.Random(seed)
            root = self.tmp / f"gen-{run}"
            hexid = lambda n: "".join(rng.choice("0123456789abcdef") for _ in range(n))  # noqa: E731
            folders = [root / f"p{hexid(6)}" for _ in range(rng.randint(1, 4))]
            want = []
            for folder in folders:
                folder.mkdir(parents=True, exist_ok=True)
                sids = sorted({f"{hexid(8)}-{hexid(4)}" for _ in range(rng.randint(0, 4))})
                for sid in sids:
                    write_jsonl(folder / f"{sid}.jsonl", [tool_result("2026-10-16T01:00:00.000Z")])
                    agents = []
                    for aid in sorted({hexid(rng.randint(5, 17)) for _ in range(rng.randint(0, 4))}):
                        sub = folder / sid / "subagents"
                        write_jsonl(sub / f"agent-{aid}.jsonl", [])
                        kind = rng.randrange(5)
                        meta = {}
                        if kind == 0:
                            meta = {"agentType": rng.choice(("harness:coder", "harness:reviewer", "Explore")),
                                    "description": f"#{rng.randint(1, 999)} {hexid(4)}", "model": rng.choice(("opus", "sonnet"))}
                            write_meta(sub / f"agent-{aid}.meta.json", meta)
                        elif kind == 1:
                            write_meta(sub / f"agent-{aid}.meta.json", "{" + hexid(3))
                        elif kind == 2:
                            write_meta(sub / f"agent-{aid}.meta.json", [hexid(3)])
                        elif kind == 3:
                            write_meta(sub / f"agent-{aid}.meta.json", str(rng.randint(0, 99)))
                        agents.append({"id": aid, "path": str(sub / f"agent-{aid}.jsonl"), "meta": meta})
                    want.append({"id": sid, "path": str(folder / f"{sid}.jsonl"), "agents": agents})
                (folder / f"{hexid(5)}.txt").write_text("x", encoding="utf-8")
            rng.shuffle(folders)                     # dir order is the order given, not sorted
            want.sort(key=lambda s: folders.index(Path(s["path"]).parent))
            with self.subTest(run=run, seed=seed):
                got = self.t.sessions([str(f) for f in folders] + [str(root / "missing")])
                self.assertEqual(got, want, f"seed {seed}")


# ------------------------------------------------------------------ session_dirs
class SessionDirs(Base):

    def setUp(self):
        super().setUp()
        self.projects = self.home / ".claude" / "projects"
        self.main = str(self.tmp / "client_repo.v2")
        os.makedirs(self.main)

    def folder(self, path):
        d = self.projects / encode(path)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def wt(self, name, repo=None):
        return os.path.join(repo or self.main, ".claude", "worktrees", name)

    def populate(self):
        own = self.folder(self.main)
        live = self.wt("t-2-two")
        os.makedirs(live)                                   # a worktree still on disk
        mine = [self.folder(self.wt(n)) for n in ("t-2-two", "t-10-ten", "scratch", "t-1-one", "feature_x")]
        # the removed worktrees above (all but t-2-two) are not on disk; their folders still count
        sibling = str(self.tmp / "client_repo.v2-other")
        elsewhere = str(self.tmp / "elsewhere" / "client_repo.v2")
        for path in (sibling, self.wt("t-3-theirs", sibling), self.wt("t-1-one", sibling),
                     elsewhere, self.wt("t-4-x", elsewhere),
                     os.path.join(self.main, "sub"),
                     os.path.join(self.main, ".claude", "other", "t-5-x"),
                     os.path.join(self.main, ".claude", "worktrees")):
            self.folder(path)
        # a plain file named like a worktree folder is not a folder
        (self.projects / (encode(self.wt("t-99-file")))).write_text("x", encoding="utf-8")
        return own, sorted(mine)

    def test_own_folder_first_then_worktree_folders_in_order(self):
        own, mine = self.populate()
        got = self.t.session_dirs(self.main)
        self.assertEqual([os.path.normpath(d) for d in got], [own] + mine)
        self.assertEqual([os.path.basename(d) for d in got[1:]], sorted(os.path.basename(d) for d in mine))
        self.assertEqual(os.path.basename(got[0]), encode(self.main))

    def test_own_folder_comes_first_even_when_it_does_not_exist(self):
        wt = self.folder(self.wt("t-7-seven"))
        own = str(self.projects / encode(self.main))
        got = [os.path.normpath(d) for d in self.t.session_dirs(self.main)]
        self.assertFalse(os.path.exists(own))
        self.assertEqual(got, [own, wt])

    def test_no_projects_folder_gives_the_own_folder_only(self):
        shutil.rmtree(self.projects)
        got = [os.path.normpath(d) for d in self.t.session_dirs(self.main)]
        self.assertEqual(got, [str(self.projects / encode(self.main))])

    def test_other_main_paths(self):
        # the result depends on the path given: another repo's own folder and worktrees
        other = str(self.tmp / "work" / "api-server")
        os.makedirs(other)
        self.populate()
        own = self.folder(other)
        wts = sorted(self.folder(self.wt(n, other)) for n in ("t-5-a", "t-6-b"))
        got = [os.path.normpath(d) for d in self.t.session_dirs(other)]
        self.assertEqual(got, [own] + wts)

    def generated(self, seed, main):
        """Folders for `main` and its look-alikes from `seed`; the expected list, built by hand."""
        rng = random.Random(seed)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_."
        name = lambda: rng.choice("abcdefghijklmnopqrstuvwxyz") + "".join(  # noqa: E731
            rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        own = str(self.projects / encode(main))
        if rng.random() < 0.7:
            self.folder(main)
        mine = {self.folder(self.wt(name(), main)) for _ in range(rng.randint(0, 6))}
        for _ in range(rng.randint(1, 4)):                     # look-alikes that are not this repo's
            sibling = main + rng.choice(("-", "_", ".", "")) + name()
            self.folder(sibling)
            self.folder(self.wt(name(), sibling))
            self.folder(os.path.join(main, name()))
        (self.projects / (encode(self.wt(name(), main)) + "x")).write_text("a file, not a folder", encoding="utf-8")
        return [own] + sorted(mine, key=os.path.basename)

    def test_generated_layouts(self):
        for run in range(RANDOM_RUNS):
            seed = fresh_seed()
            main = str(self.tmp / f"repo{run}_{seed % 1000}.x")
            os.makedirs(main)
            want = self.generated(seed, main)
            with self.subTest(run=run, seed=seed):
                got = [os.path.normpath(d) for d in self.t.session_dirs(main)]
                self.assertEqual(got, want, f"seed {seed}")

    def test_same_list_as_harness_worklog(self):
        self.populate()
        other = str(self.tmp / "work" / "api-server")
        os.makedirs(other)
        self.folder(self.wt("t-5-a", other))
        seed = fresh_seed()
        gen = str(self.tmp / "gen_repo")
        os.makedirs(gen)
        self.generated(seed, gen)
        wl = load_worklog()
        for main in (self.main, other, gen, str(self.tmp / "client_repo.v2-other"), str(self.tmp / "never-seen")):
            with self.subTest(main=main, seed=seed):
                self.assertEqual(self.t.session_dirs(main), wl.transcripts_dirs(main), f"seed {seed}")

    def test_session_dirs_does_not_use_harness_worklog(self):
        """A later ticket makes harness-worklog call this module, so this module must not load it.
        A copy of lib/ alone (no bin/ beside it) is imported in a fresh interpreter that records
        every file opened; session_dirs must give the expected list with no worklog file opened."""
        own, mine = self.populate()
        iso = self.tmp / "isolated"
        shutil.copytree(LIB, iso / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        script = (
            "import json, sys\n"
            "opened = []\n"
            "def hook(event, args):\n"
            "    if event in ('open', 'os.listdir', 'os.scandir', 'exec', 'subprocess.Popen', 'os.system'):\n"
            "        for a in args:\n"
            "            if isinstance(a, (str, bytes)) and 'worklog' in str(a).lower():\n"
            "                opened.append((event, str(a)[:200]))\n"
            "sys.addaudithook(hook)\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import harness_lib.transcripts as t\n"
            "dirs = t.session_dirs(sys.argv[2])\n"
            "print(json.dumps({'dirs': dirs, 'file': t.__file__, 'opened': opened,\n"
            "                  'loaded': sorted(m for m in sys.modules if 'worklog' in m.lower())}))\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-I", "-c", script, str(iso / "lib"), self.main],
                           capture_output=True, text=True, env=env, cwd=str(iso), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(out["file"].startswith(str(iso / "lib")), out["file"])
        self.assertEqual(out["opened"], [], "transcripts touched a worklog file")
        self.assertEqual(out["loaded"], [], "transcripts loaded a worklog module")
        self.assertEqual([os.path.normpath(d) for d in out["dirs"]], [own] + mine)


if __name__ == "__main__":
    unittest.main()
