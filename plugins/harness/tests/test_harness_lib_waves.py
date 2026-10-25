"""Tests for harness_lib/waves.py (ticket #179, epic #172): wave records, progress and the review marker.

A wave is the set of tickets one `harness-work dispatch` launched. Its record lives at
~/.claude/harness/repos/<key>/waves/<id>.json:
    {"id": "w20261022140859", "repo": "<main>", "created_at": "2026-10-22T14:08:59Z",
     "tickets": [12, 13], "dropped": [], "tier": "fable", "effort": "xhigh", "review": None}
- create_wave(key, main, tickets, tier, effort, at=None): tickets sorted and unique; the id is `w` plus
  the UTC yyyymmddHHMMSS of `at` (default now); written with text.py's write_json; never overwrites a
  wave: a second wave in the same second is `w<stamp>-2`, then `-3`, ...
- load_wave / load_waves (every readable wave, oldest first; broken or non-object files skipped) /
  save_wave; wave_of (newest wave listing the ticket in `tickets`, dropped not counted, else None).
- ticket_statuses(key, tickets) from repos/<key>/tickets/<n>.json's `status`; progress(wave, statuses):
  in-review and closed are done, anything else (no cache included) waits; complete when the wave has
  tickets and none waits. wave_state: empty, working, due, reviewing, reviewed.
- drop_ticket moves a ticket from `tickets` to `dropped`; wave_marker / has_wave_review agree on
  `<!-- harness-wave-review <id> -->`.

Every fixture is written here, in a temporary HOME. Expected values are computed here from the schema
(ids from the UTC time of `at`, statuses from the fixture table), never by the code under test.

Ordering choice for "oldest first": by created_at, then the numeric id suffix (no suffix = 1), so
`w<s>` < `w<s>-2` < `w<s>-10`. Two hand-written waves have a created_at that disagrees with their id
stamp; the order follows created_at.

Contract points from the Outline and Context that a shortcut could skip:
- the id is reserved with O_CREAT|O_EXCL: separate processes released together in one second still get
  distinct ids and every file holds its own record (a lock inside one process is not enough);
- records go through text.py's write_json (create_wave, save_wave, drop_ticket), checked by wrapping it;
- wave_state looks at completeness before the review: a ticket still waiting is `working` whatever the
  review says, and no tickets is `empty` whatever the review says;
- drop_ticket on a ticket already dropped leaves the wave unchanged; on a ticket the wave does not list,
  `tickets` and the other fields stay as they were and `dropped` stays sorted without duplicates.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_waves.py" -v
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import importlib
import importlib.util
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"
WAVES_PY = LIB / "harness_lib" / "waves.py"

UTC = dt.timezone.utc
STATUSES = ("ready", "in-progress", "in-review", "blocked", "needs-input", "closed", None)
DONE_STATUSES = ("in-review", "closed")
PUBLIC = ("waves_dir", "wave_path", "create_wave", "load_wave", "load_waves", "save_wave", "wave_of",
          "ticket_statuses", "progress", "wave_state", "drop_ticket", "wave_marker", "has_wave_review")


def load_waves_module() -> types.ModuleType:
    """harness_lib.waves imported from the checkout's lib/; the import error while the module is missing
    is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.waves")


# --------------------------------------------------------------- expectations, from the schema
def at(y, mo, d, h, mi, s, us=0, offset_hours=0):
    return dt.datetime(y, mo, d, h, mi, s, us, tzinfo=dt.timezone(dt.timedelta(hours=offset_hours)))


def stamp(when: dt.datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def iso(when: dt.datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wave_id(when: dt.datetime, n: int = 1) -> str:
    return "w" + stamp(when) + ("" if n == 1 else f"-{n}")


def record(main, tickets, tier, effort, when, n=1, dropped=(), review=None) -> dict:
    return {"id": wave_id(when, n), "repo": main, "created_at": iso(when), "tickets": sorted(set(tickets)),
            "dropped": list(dropped), "tier": tier, "effort": effort, "review": review}


def order_key(wave: dict):
    """Oldest first: created_at, then the numeric suffix of the id (none = 1)."""
    ident = wave["id"]
    n = int(ident.split("-", 1)[1]) if "-" in ident else 1
    return (wave["created_at"], n)


def marker(ident: str) -> str:
    return f"<!-- harness-wave-review {ident} -->"


def expected_progress(tickets, statuses):
    done, waiting = [], []
    for n in sorted(tickets):
        status = statuses.get(n)
        if status in DONE_STATUSES:
            done.append(n)
        else:
            waiting.append({"number": n, "status": status})
    return {"done": done, "waiting": waiting, "complete": bool(tickets) and not waiting}


def cache_record(number, status):
    """A ticket cache as harness-board writes it (repos/<key>/tickets/<n>.json)."""
    return {"number": number, "title": f"Ticket {number}", "url": f"https://github.com/o/r/issues/{number}",
            "type": "task", "status": status, "size": "M", "risk": "low", "flags": [],
            "labels": ["task"] + ([f"status:{status}"] if status and status != "closed" else []),
            "tier": "opus", "parent": {"number": 172, "title": "Epic", "goal": "g"}, "blocked_by": [],
            "body": "", "sections": {}, "comments": [], "fetched_at": "2026-10-22T10:00:00Z"}


# --------------------------------------------------------------- base
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="harness-waves-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = str(self.tmp / "home")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = load_waves_module()
        self.key = "Users-me-proj-abc123"
        self.main = str(self.tmp / "proj")

    def dir_of(self, key=None) -> str:
        return os.path.join(self.home, ".claude", "harness", "repos", key or self.key, "waves")

    def file_of(self, ident, key=None) -> str:
        return os.path.join(self.dir_of(key), ident + ".json")

    def write_raw(self, name, text, key=None):
        os.makedirs(self.dir_of(key), exist_ok=True)
        with open(os.path.join(self.dir_of(key), name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def write_wave(self, wave, key=None):
        self.write_raw(wave["id"] + ".json", json.dumps(wave, indent=2), key)

    def read_file(self, ident, key=None):
        with open(self.file_of(ident, key), encoding="utf-8") as fh:
            return fh.read()

    def wave_files(self, key=None):
        folder = self.dir_of(key)
        if not os.path.isdir(folder):
            return []
        return sorted(f for f in os.listdir(folder) if f.endswith(".json") and not f.startswith("."))

    def write_cache(self, number, payload, key=None):
        folder = os.path.join(self.home, ".claude", "harness", "repos", key or self.key, "tickets")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{number}.json"), "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload, indent=2))

    @contextlib.contextmanager
    def write_json_calls(self):
        """Every call to text.py's write_json while the block runs, as (real path, data), still writing.
        The real function is wrapped under every name waves.py could reach it by: harness_lib.text's
        attribute and any name in waves.py bound to the same function (`from .text import write_json`)."""
        text = importlib.import_module("harness_lib.text")
        original = text.write_json
        calls = []

        def recording(path, data, *args, **kwargs):
            calls.append((os.path.realpath(path), json.loads(json.dumps(data))))
            return original(path, data, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(text, "write_json", recording))
            for name, value in list(vars(self.w).items()):
                if value is original:
                    stack.enter_context(mock.patch.object(self.w, name, recording))
            yield calls

    def race(self, key, main, when, count):
        """`count` separate python3 processes, each importing harness_lib.waves from the checkout's lib/
        with HOME set to the temporary home, all released at once by a go file, each calling
        create_wave(key, main, [its number], "opus", "high", at=when). Returns the records they print."""
        gate = Path(tempfile.mkdtemp(prefix="gate-", dir=str(self.tmp)))
        go = gate / "go"
        env = dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        procs = []
        try:
            for n in range(1, count + 1):
                ready = gate / f"ready-{n}"
                procs.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", RACER, str(LIB), str(go), str(ready), key, main, str(n),
                     when.isoformat()],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 60
            while not all((gate / f"ready-{n}").exists() for n in range(1, count + 1)):
                if any(p.poll() is not None for p in procs) or time.monotonic() > deadline:
                    break
                time.sleep(0.005)
            go.touch()
            results = []
            for p in procs:
                out, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
                results.append(json.loads(out.strip().splitlines()[-1]))
            return results
        finally:
            go.touch()
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.communicate()


# One racing writer: import first, say ready, spin until the go file appears, then create the wave.
RACER = textwrap.dedent("""
    import datetime, json, os, sys, time
    sys.dont_write_bytecode = True
    lib, go, ready, key, main, n, when = sys.argv[1:8]
    sys.path.insert(0, lib)
    from harness_lib import waves
    open(ready, "w").close()
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            sys.exit("the go file never appeared")
    record = waves.create_wave(key, main, [int(n)], "opus", "high", at=datetime.datetime.fromisoformat(when))
    print(json.dumps(record))
""")


# =============================================================== create_wave
class CreateWave(Base):
    def test_writes_the_record(self):
        cases = [
            # (key, main, tickets, tier, effort, at)
            (self.key, self.main, [12, 13], "fable", "xhigh", at(2026, 10, 22, 14, 8, 59)),
            (self.key, self.main, [13, 12, 13, 12], "opus", "high", at(2026, 10, 22, 14, 9, 0)),
            (self.key, "/w/other main", (7,), "sonnet", "medium", at(2026, 1, 2, 3, 4, 5)),
            # +11:00 local 01:08:59 on the 23rd is 14:08:58 UTC on the 22nd
            (self.key, "/w/café", [205, 31, 9, 31], "fable", "max", at(2026, 10, 23, 1, 8, 58, 0, 11)),
            # -07:00, and the fraction of a second does not round up
            ("Other-key-9", self.main, [1, 100, 10], "opus", "xhigh", at(2025, 12, 31, 20, 59, 59, 999999, -7)),
            ("Other-key-9", self.main, [], "opus", "low", at(2027, 2, 28, 23, 59, 59)),
        ]
        made = {}
        for key, main, tickets, tier, effort, when in cases:
            with self.subTest(key=key, tickets=tickets, at=when.isoformat()):
                want = record(main, tickets, tier, effort, when)
                with self.write_json_calls() as calls:
                    got = self.w.create_wave(key, main, tickets, tier, effort, at=when)
                self.assertEqual(got, want)
                # the record went to disk through text.py's write_json, at the wave's path
                self.assertIn((self.file_of(want["id"], key), want), calls)
                self.assertTrue(self.w.WAVE_ID.fullmatch(got["id"]), got["id"])
                self.assertEqual(self.w.waves_dir(key), self.dir_of(key))
                self.assertEqual(self.w.wave_path(key, want["id"]), self.file_of(want["id"], key))
                text = self.read_file(want["id"], key)
                self.assertEqual(json.loads(text), want)
                # written with text.py's write_json: indented by 2, non-ASCII kept, one trailing newline
                self.assertEqual(text, json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n")
                self.assertEqual(self.w.load_wave(key, want["id"]), want)
                made.setdefault(key, []).append(want["id"] + ".json")
        for key, files in made.items():
            self.assertEqual(self.wave_files(key), sorted(files))
        # the expected UTC conversions, spelled out
        self.assertIn("w20261022140858.json", made[self.key])
        self.assertIn("w20260101035959.json", made["Other-key-9"])

        # default `at`: now, in UTC
        before = dt.datetime.now(UTC).replace(microsecond=0)
        got = self.w.create_wave(self.key, self.main, [42, 41], "opus", "high")
        after = dt.datetime.now(UTC).replace(microsecond=0)
        self.assertEqual(got["tickets"], [41, 42])
        self.assertTrue(iso(before) <= got["created_at"] <= iso(after), got["created_at"])
        created = dt.datetime.strptime(got["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertEqual(got["id"], wave_id(created))
        self.assertEqual(json.loads(self.read_file(got["id"])), got)

    def test_same_second_ids_do_not_collide(self):
        when = at(2026, 10, 22, 14, 8, 59)
        firsts = []
        for i, tickets in enumerate(([12, 13], [14], [15, 16, 17], [18])):
            got = self.w.create_wave(self.key, self.main, tickets, "fable", "xhigh", at=when)
            want = record(self.main, tickets, "fable", "xhigh", when, n=i + 1)
            self.assertEqual(got, want)
            firsts.append(want)
        self.assertEqual([w["id"] for w in firsts],
                         ["w20261022140859", "w20261022140859-2", "w20261022140859-3", "w20261022140859-4"])
        for want in firsts:             # none overwritten
            self.assertEqual(json.loads(self.read_file(want["id"])), want)
        # a neighbouring second starts again without a suffix
        nxt = at(2026, 10, 22, 14, 9, 0)
        self.assertEqual(self.w.create_wave(self.key, self.main, [1], "opus", "high", at=nxt)["id"],
                         "w20261022140900")
        # the same second in another repository's key does not collide with this one
        self.assertEqual(self.w.create_wave("Another-key", self.main, [1], "opus", "high", at=when)["id"],
                         "w20261022140859")

        # a wave file already on disk (not made by this process) is never overwritten
        other = at(2026, 11, 5, 9, 0, 0)
        existing = record("/elsewhere", [99], "sonnet", "low", other)
        existing["review"] = {"launched_at": "2026-11-05T10:00:00Z", "run": "r-1", "reviewed_at": None}
        self.write_wave(existing)
        got = self.w.create_wave(self.key, self.main, [5], "opus", "high", at=other)
        self.assertEqual(got["id"], "w20261105090000-2")
        self.assertEqual(json.loads(self.read_file(existing["id"])), existing)

        # many writers at once in one second: every one gets its own id
        burst = at(2026, 12, 1, 0, 0, 0)
        results, errors = [], []

        def make(n):
            try:
                results.append(self.w.create_wave(self.key, self.main, [n], "opus", "high", at=burst))
            except Exception as err:            # pragma: no cover - reported below
                errors.append(err)

        threads = [threading.Thread(target=make, args=(n,)) for n in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["id"] for r in results),
                         sorted(["w20261201000000"] + [f"w20261201000000-{n}" for n in range(2, 9)]))
        for r in results:
            self.assertEqual(json.loads(self.read_file(r["id"])), r)

        # separate processes released together in one second (the id is reserved with O_CREAT|O_EXCL,
        # so a lock inside one process is not what keeps them apart): every process gets its own id,
        # the ids are the base and -2 .. -N, and each file holds the record its process returned.
        # Several rounds, one starting on a second this process already used.
        key = "Race-key"
        held = at(2027, 3, 4, 5, 6, 7)
        first = self.w.create_wave(key, self.main, [500], "fable", "xhigh", at=held)
        self.assertEqual(first["id"], "w20270304050607")
        before = set(self.wave_files(key))
        rounds = [(at(2027, 3, 4, 5, 6, 6), 0), (held, 1), (at(2027, 3, 4, 5, 6, 8, 0, 2), 0)]
        count = 8
        for when, taken in rounds:
            with self.subTest(race=when.isoformat()):
                raced = self.race(key, self.main, when, count)
                ids = [r["id"] for r in raced]
                self.assertEqual(len(set(ids)), count, ids)
                base = wave_id(when)
                self.assertEqual(sorted(ids), sorted(wave_id(when, n) for n in range(1 + taken, count + 1 + taken)),
                                 f"{base}: {sorted(ids)}")
                for r in raced:
                    self.assertEqual(r["created_at"], iso(when))
                    self.assertEqual(len(r["tickets"]), 1)
                    self.assertEqual(json.loads(self.read_file(r["id"], key)), r)
                self.assertEqual(sorted(r["tickets"][0] for r in raced), list(range(1, count + 1)))
                before |= {i + ".json" for i in ids}
                self.assertEqual(set(self.wave_files(key)), before)
        self.assertEqual(self.w.load_wave(key, first["id"]), first)       # the held wave untouched


# =============================================================== load_waves, load_wave, save_wave
class LoadWaves(Base):
    def test_oldest_first_and_broken_files_skipped(self):
        self.assertEqual(self.w.load_waves(self.key), [])          # no folder yet
        self.assertIsNone(self.w.load_wave(self.key, "w20261022140859"))

        times = [at(2026, 10, 22, 14, 8, 59), at(2026, 10, 21, 9, 0, 0), at(2026, 10, 22, 14, 9, 0),
                 at(2026, 9, 30, 23, 59, 59), at(2026, 10, 22, 14, 8, 58), at(2027, 1, 1, 0, 0, 0)]
        rng = random.Random(179)
        plan = []
        for i, when in enumerate(times):
            plan.append((when, [10 + i, 20 + i]))
        same = at(2026, 10, 15, 12, 0, 0)          # ten in one second: -2 .. -10
        plan += [(same, [100 + j]) for j in range(10)]
        rng.shuffle(plan)

        made = []
        seen = {}
        for when, tickets in plan:
            n = seen[when] = seen.get(when, 0) + 1
            want = record(self.main, tickets, "opus", "high", when, n=n)
            self.assertEqual(self.w.create_wave(self.key, self.main, tickets, "opus", "high", at=when), want)
            made.append(want)

        # a wave written by hand is read too; broken and non-object files are skipped
        by_hand = record("/by/hand", [7], "sonnet", "low", at(2026, 1, 1, 0, 0, 0))
        self.write_wave(by_hand)
        made.append(by_hand)
        # created_at, not the id stamp, says how old a wave is: the oldest id here is the newest wave,
        # and the newest id the oldest
        late = dict(record("/by/hand", [8], "sonnet", "low", at(2020, 1, 1, 0, 0, 0)),
                    created_at="2027-06-01T00:00:00Z")
        early = dict(record("/by/hand", [9], "sonnet", "low", at(2099, 12, 31, 0, 0, 0)),
                     created_at="2025-02-03T04:05:06Z")
        for wave in (late, early):
            self.write_wave(wave)
            made.append(wave)
        broken = {"w20261021000000.json": "{not json", "w20261021000001.json": "[1, 2]",
                  "w20261021000002.json": "null", "w20261021000003.json": "",
                  "w20261021000004.json": '"a string"', "w20261021000005.json": "42",
                  "w20261021000006.json": '{"id": "w20261021000006", '}
        for name, text in broken.items():
            self.write_raw(name, text)

        got = self.w.load_waves(self.key)
        want = sorted(made, key=order_key)
        self.assertEqual([w["id"] for w in got], [w["id"] for w in want])
        self.assertEqual(got, want)
        # -10 comes after -9, not after the base id
        ids = [w["id"] for w in got]
        self.assertLess(ids.index("w20261015120000-9"), ids.index("w20261015120000-10"))
        self.assertLess(ids.index("w20261015120000"), ids.index("w20261015120000-2"))
        self.assertEqual(ids[0], "w20991231000000")          # created 2025-02-03
        self.assertEqual(ids[1], by_hand["id"])
        self.assertEqual(ids[-2], "w20270101000000")
        self.assertEqual(ids[-1], "w20200101000000")         # created 2027-06-01

        for name in broken:
            self.assertIsNone(self.w.load_wave(self.key, name[:-len(".json")]), name)
        self.assertEqual(self.w.load_wave(self.key, by_hand["id"]), by_hand)
        self.assertEqual(self.w.load_waves("Unknown-key"), [])

        # save_wave writes the record back where load_wave finds it; the order holds
        target = dict(want[3])
        target["review"] = {"launched_at": "2026-10-22T15:00:00Z", "run": "run-77", "reviewed_at": None}
        with self.write_json_calls() as calls:
            self.w.save_wave(self.key, target)
        self.assertIn((self.file_of(target["id"]), target), calls)          # through text.py's write_json
        self.assertEqual(self.w.load_wave(self.key, target["id"]), target)
        self.assertEqual(json.loads(self.read_file(target["id"])), target)
        again = self.w.load_waves(self.key)
        self.assertEqual([w["id"] for w in again], [w["id"] for w in want])
        self.assertEqual(again[3], target)


# =============================================================== wave_of
class WaveOf(Base):
    def test_newest_wave_listing_the_ticket_and_dropped_excluded(self):
        a = record(self.main, [12, 13], "opus", "high", at(2026, 10, 20, 8, 0, 0))
        b = record(self.main, [13, 14], "opus", "high", at(2026, 10, 21, 8, 0, 0))
        c = record(self.main, [15], "fable", "xhigh", at(2026, 10, 22, 8, 0, 0), dropped=[12, 14])
        d1 = record(self.main, [16, 17], "opus", "high", at(2026, 10, 22, 9, 0, 0))
        d2 = record(self.main, [16], "opus", "high", at(2026, 10, 22, 9, 0, 0), n=2)
        d10 = record(self.main, [17], "opus", "high", at(2026, 10, 22, 9, 0, 0), n=10)
        d9 = record(self.main, [17, 18], "opus", "high", at(2026, 10, 22, 9, 0, 0), n=9)
        for wave in (c, a, d10, b, d2, d9, d1):          # written out of order
            self.write_wave(wave)
        self.write_raw("w20991231235959.json", "{broken")
        self.write_raw("w20991231235958.json", "[12, 13, 14, 15, 16, 17]")

        cases = {12: a, 13: b, 14: b, 15: c, 16: d2, 17: d10, 18: d9, 99: None, 0: None}  # 17: -10 is newer than -9
        for ticket, want in cases.items():
            with self.subTest(ticket=ticket):
                self.assertEqual(self.w.wave_of(self.key, ticket), want)
        self.assertIsNone(self.w.wave_of("Unknown-key", 12))

        # waves made through create_wave count the same way, and a newer one takes over
        newest = self.w.create_wave(self.key, self.main, [13, 99], "sonnet", "low", at=at(2026, 10, 23, 0, 0, 0))
        self.assertEqual(self.w.wave_of(self.key, 13), newest)
        self.assertEqual(self.w.wave_of(self.key, 99), newest)
        self.assertEqual(self.w.wave_of(self.key, 14), b)
        self.assertEqual(self.w.wave_of(self.key, 12), a)


# =============================================================== progress and ticket_statuses
class Progress(Base):
    def test_in_review_and_closed_are_done(self):
        wave = record(self.main, [12, 13], "fable", "xhigh", at(2026, 10, 22, 14, 8, 59))
        self.assertEqual(self.w.progress(wave, {12: "in-review", 13: "in-progress"}),
                         {"done": [12], "waiting": [{"number": 13, "status": "in-progress"}], "complete": False})
        self.assertEqual(self.w.progress(wave, {12: "in-review", 13: "closed"}),
                         {"done": [12, 13], "waiting": [], "complete": True})
        self.assertEqual(self.w.progress(wave, {12: None, 13: "closed"}),
                         {"done": [13], "waiting": [{"number": 12, "status": None}], "complete": False})
        self.assertEqual(self.w.DONE, ("in-review", "closed"))

        # every assignment of every status to three tickets
        tickets = [3, 7, 21]
        wave = record(self.main, tickets, "opus", "high", at(2026, 10, 22, 0, 0, 0))
        for combo in itertools.product(STATUSES, repeat=len(tickets)):
            statuses = dict(zip(tickets, combo))
            with self.subTest(statuses=statuses):
                self.assertEqual(self.w.progress(wave, statuses), expected_progress(tickets, statuses))

        # a ticket absent from the statuses has no cache: it waits
        wave = record(self.main, [5, 6, 8], "opus", "high", at(2026, 10, 22, 0, 0, 1))
        self.assertEqual(self.w.progress(wave, {5: "closed", 6: "in-review"}),
                         {"done": [5, 6], "waiting": [{"number": 8, "status": None}], "complete": False})

        # dropped tickets do not count, done or waiting
        wave = record(self.main, [12], "opus", "high", at(2026, 10, 22, 0, 0, 2), dropped=[13])
        self.assertEqual(self.w.progress(wave, {12: "closed", 13: "in-progress"}),
                         {"done": [12], "waiting": [], "complete": True})

        # a wave without tickets is never complete
        for dropped in ([], [4, 9]):
            wave = record(self.main, [], "opus", "high", at(2026, 10, 22, 0, 0, 3), dropped=dropped)
            with self.subTest(empty_with_dropped=dropped):
                self.assertEqual(self.w.progress(wave, {4: "closed", 9: "closed"}),
                                 {"done": [], "waiting": [], "complete": False})

    def test_statuses_from_the_ticket_caches(self):
        self.write_cache(12, cache_record(12, "in-review"))
        self.write_cache(13, cache_record(13, "in-progress"))
        self.write_cache(14, cache_record(14, "closed"))
        # 15: no cache at all
        self.write_cache(16, "{not json")
        self.write_cache(17, cache_record(17, None))
        self.write_cache(18, "[1, 2, 3]")
        self.write_cache(19, cache_record(19, "needs-input"))
        self.write_cache(20, {"number": 20, "title": "no status key"})
        # the same numbers in another key say something else
        self.write_cache(13, cache_record(13, "closed"), key="Other-key")
        self.write_cache(15, cache_record(15, "in-review"), key="Other-key")

        tickets = list(range(12, 21))
        want = {12: "in-review", 13: "in-progress", 14: "closed", 15: None, 16: None, 17: None, 18: None,
                19: "needs-input", 20: None}
        self.assertEqual(self.w.ticket_statuses(self.key, tickets), want)
        self.assertEqual(self.w.ticket_statuses("Other-key", [13, 15, 12]), {13: "closed", 15: "in-review", 12: None})
        self.assertEqual(self.w.ticket_statuses(self.key, []), {})

        wave = self.w.create_wave(self.key, self.main, tickets, "fable", "xhigh", at=at(2026, 10, 22, 14, 8, 59))
        statuses = self.w.ticket_statuses(self.key, wave["tickets"])
        self.assertEqual(self.w.progress(wave, statuses), expected_progress(tickets, want))
        self.assertEqual(self.w.wave_state(wave, statuses), "working")

        # the caches move on: every ticket in review or closed makes the wave complete, and due
        for n, status in zip(tickets, itertools.cycle(("closed", "in-review", "in-review"))):
            self.write_cache(n, cache_record(n, status))
        statuses = self.w.ticket_statuses(self.key, wave["tickets"])
        self.assertEqual(set(statuses.values()), {"closed", "in-review"})
        self.assertEqual(self.w.progress(wave, statuses),
                         {"done": tickets, "waiting": [], "complete": True})
        self.assertEqual(self.w.wave_state(wave, statuses), "due")

        # one goes back to in-progress
        self.write_cache(17, cache_record(17, "in-progress"))
        statuses = self.w.ticket_statuses(self.key, wave["tickets"])
        self.assertEqual(self.w.progress(wave, statuses),
                         {"done": [n for n in tickets if n != 17],
                          "waiting": [{"number": 17, "status": "in-progress"}], "complete": False})
        self.assertEqual(self.w.wave_state(wave, statuses), "working")


# =============================================================== wave_state
class WaveState(Base):
    def test_each_state(self):
        launched = {"launched_at": "2026-10-22T15:00:00Z", "run": "run-1", "reviewed_at": None}
        launched_no_key = {"launched_at": "2026-10-22T15:00:00Z", "run": "run-2"}
        reviewed = {"launched_at": "2026-10-22T15:00:00Z", "run": "run-3", "reviewed_at": "2026-10-22T16:30:00Z"}
        when = at(2026, 10, 22, 14, 8, 59)
        cases = [
            # (tickets, dropped, statuses, review, state)
            ([], [], {}, None, "empty"),
            ([], [12, 13], {12: "closed", 13: "in-review"}, None, "empty"),
            ([12, 13], [], {12: "in-review", 13: "in-progress"}, None, "working"),
            ([12, 13], [], {12: None, 13: None}, None, "working"),
            ([12, 13], [], {}, None, "working"),
            ([4], [], {4: "needs-input"}, None, "working"),
            ([4, 5, 6], [7], {4: "closed", 5: "closed", 6: "blocked", 7: "closed"}, None, "working"),
            ([12, 13], [], {12: "in-review", 13: "closed"}, None, "due"),
            ([4], [], {4: "closed"}, None, "due"),
            ([4, 5, 6], [7], {4: "in-review", 5: "in-review", 6: "in-review", 7: "in-progress"}, None, "due"),
            ([12, 13], [], {12: "in-review", 13: "closed"}, launched, "reviewing"),
            ([9], [], {9: "in-review"}, launched_no_key, "reviewing"),
            ([12, 13], [], {12: "in-review", 13: "closed"}, reviewed, "reviewed"),
            ([3, 30, 300], [], {3: "closed", 30: "closed", 300: "closed"}, reviewed, "reviewed"),
            # not complete is working whatever the review says (a ticket went back after the review began)
            ([12, 13], [], {12: "in-review", 13: "in-progress"}, launched, "working"),
            ([12, 13], [], {12: "in-review", 13: "in-progress"}, launched_no_key, "working"),
            ([12, 13], [], {12: "closed", 13: "in-progress"}, reviewed, "working"),
            ([12, 13], [], {12: "closed", 13: None}, reviewed, "working"),
            ([4], [], {}, launched, "working"),
            ([4, 5], [6], {4: "in-review", 5: "needs-input", 6: "closed"}, reviewed, "working"),
            # no tickets is empty whatever the review says
            ([], [], {}, launched, "empty"),
            ([], [], {}, reviewed, "empty"),
            ([], [5], {5: "closed"}, reviewed, "empty"),
            ([], [5, 6], {5: "in-review", 6: "closed"}, launched_no_key, "empty"),
        ]
        for tickets, dropped, statuses, review, state in cases:
            wave = record(self.main, tickets, "fable", "xhigh", when, dropped=dropped, review=review)
            with self.subTest(tickets=tickets, dropped=dropped, statuses=statuses, review=review):
                self.assertEqual(self.w.wave_state(wave, statuses), state)

        # every status assignment for two tickets, under each review: complete first, then the review
        after_complete = {None: "due", "launched": "reviewing", "launched_no_key": "reviewing", "reviewed": "reviewed"}
        reviews = {None: None, "launched": launched, "launched_no_key": launched_no_key, "reviewed": reviewed}
        for (name, review), combo in itertools.product(reviews.items(), itertools.product(STATUSES, repeat=2)):
            statuses = dict(zip([40, 41], combo))
            wave = record(self.main, [40, 41], "opus", "high", when, review=review)
            want = after_complete[name] if all(s in DONE_STATUSES for s in combo) else "working"
            with self.subTest(statuses=statuses, review=name):
                self.assertEqual(self.w.wave_state(wave, statuses), want)


# =============================================================== marker
class Marker(Base):
    def test_marker_and_has_wave_review(self):
        ids = ["w20261022140859", "w20261022140859-2", "w20270101000000-10", "w19991231235959"]
        for ident in ids:
            with self.subTest(ident=ident):
                self.assertEqual(self.w.wave_marker(ident), marker(ident))

        base, second = "w20261022140859", "w20261022140859-2"
        reviews = [
            {"body": "Looks fine.", "state": "COMMENTED", "author": {"login": "rjeskuri"}},
            {"body": None, "state": "APPROVED"},
            {"state": "COMMENTED"},
            {"body": f"## Wave review\n\nFindings below.\n\n{marker(second)}\n", "state": "COMMENTED"},
        ]
        self.assertTrue(self.w.has_wave_review(reviews, second))
        self.assertFalse(self.w.has_wave_review(reviews, base))           # -2's marker is not the base's
        self.assertFalse(self.w.has_wave_review(reviews, "w20261022140859-20"))
        self.assertFalse(self.w.has_wave_review([], base))
        self.assertFalse(self.w.has_wave_review(reviews[:3], second))

        with_base = reviews + [{"body": f"prefix {marker(base)} suffix", "state": "COMMENTED"}]
        self.assertTrue(self.w.has_wave_review(with_base, base))
        self.assertTrue(self.w.has_wave_review(with_base, second))
        self.assertFalse(self.w.has_wave_review([{"body": f"prefix {marker(base)} suffix"}], second))

        # what wave_marker writes, has_wave_review finds, for each id and only that id
        for ident in ids:
            body = f"Wave {ident} review.\n{self.w.wave_marker(ident)}"
            for other in ids:
                with self.subTest(ident=ident, other=other):
                    self.assertEqual(self.w.has_wave_review([{"body": body}], other), other == ident)
        # the id alone, or a near-miss spelling, is not the marker
        self.assertFalse(self.w.has_wave_review([{"body": f"reviewed {base}"}], base))
        self.assertFalse(self.w.has_wave_review([{"body": f"<!-- harness-wave-review {base}-->"}], base))
        self.assertFalse(self.w.has_wave_review([{"body": f"<!-- harness-review {base} -->"}], base))


# =============================================================== drop_ticket
class Drop(Base):
    def test_drop_moves_the_ticket_and_wave_of_forgets_it(self):
        old = self.w.create_wave(self.key, self.main, [13, 20], "opus", "high", at=at(2026, 10, 20, 8, 0, 0))
        wave = self.w.create_wave(self.key, self.main, [14, 12, 13], "fable", "xhigh", at=at(2026, 10, 22, 14, 8, 59))
        self.assertEqual(self.w.wave_of(self.key, 13), wave)

        with self.write_json_calls() as calls:
            got = self.w.drop_ticket(self.key, wave["id"], 13)
        want = dict(wave, tickets=[12, 14], dropped=[13])
        self.assertEqual(got, want)
        self.assertIn((self.file_of(wave["id"]), want), calls)              # through text.py's write_json
        # dropping it again changes nothing: no second 13 in dropped
        self.assertEqual(self.w.drop_ticket(self.key, wave["id"], 13), want)
        self.assertEqual(self.w.load_wave(self.key, wave["id"]), want)
        self.assertEqual(json.loads(self.read_file(wave["id"])), want)
        self.assertEqual(self.w.load_wave(self.key, old["id"]), old)          # the other wave untouched
        # the newer wave no longer lists 13: wave_of falls back to the older one
        self.assertEqual(self.w.wave_of(self.key, 13), old)
        self.assertEqual(self.w.wave_of(self.key, 12), want)
        self.assertEqual(self.w.wave_of(self.key, 14), want)

        # a dropped ticket no longer holds the wave
        statuses = {12: "in-review", 13: "needs-input", 14: "closed"}
        self.assertEqual(self.w.progress(want, statuses), {"done": [12, 14], "waiting": [], "complete": True})
        self.assertEqual(self.w.wave_state(want, statuses), "due")

        got = self.w.drop_ticket(self.key, wave["id"], 14)
        self.assertEqual(got["tickets"], [12])
        self.assertEqual(got["dropped"], [13, 14])
        self.assertEqual({k: v for k, v in got.items() if k not in ("tickets", "dropped")},
                         {k: v for k, v in wave.items() if k not in ("tickets", "dropped")})
        self.assertEqual(self.w.load_wave(self.key, wave["id"]), got)
        self.assertIsNone(self.w.wave_of(self.key, 14))

        got = self.w.drop_ticket(self.key, wave["id"], 12)
        self.assertEqual(got["tickets"], [])
        self.assertEqual(got["dropped"], [12, 13, 14])                     # sorted, whatever the drop order
        self.assertEqual(self.w.load_wave(self.key, wave["id"]), got)
        self.assertIsNone(self.w.wave_of(self.key, 12))
        self.assertEqual(self.w.wave_state(got, statuses), "empty")
        self.assertFalse(self.w.progress(got, statuses)["complete"])
        self.assertEqual(self.wave_files(), sorted([old["id"] + ".json", wave["id"] + ".json"]))

        # every ticket already dropped: dropping any of them again leaves the wave as it is, on disk too
        for n in (14, 12, 13, 13):
            with self.subTest(again=n):
                self.assertEqual(self.w.drop_ticket(self.key, wave["id"], n), got)
                self.assertEqual(self.w.load_wave(self.key, wave["id"]), got)
        self.assertEqual(self.w.load_wave(self.key, old["id"]), old)

        # a ticket the wave never listed: tickets and every other field unchanged, dropped sorted and
        # unique (whether the stray number is recorded there is not part of the contract)
        third = self.w.create_wave(self.key, self.main, [31, 33, 32], "sonnet", "low", at=at(2026, 10, 23, 9, 0, 0))
        third = self.w.drop_ticket(self.key, third["id"], 33)
        self.assertEqual((third["tickets"], third["dropped"]), ([31, 32], [33]))
        for stray in (40, 30, 40, 13, 30):
            with self.subTest(stray=stray):
                got = self.w.drop_ticket(self.key, third["id"], stray)
                self.assertEqual({k: v for k, v in got.items() if k != "dropped"},
                                 {k: v for k, v in third.items() if k != "dropped"})
                self.assertEqual(got["dropped"], sorted(set(got["dropped"])))
                self.assertLessEqual({33}, set(got["dropped"]))
                self.assertLessEqual(set(got["dropped"]), {33, 40, 30, 13})
                self.assertEqual(self.w.load_wave(self.key, third["id"]), got)
        self.assertEqual(self.w.wave_of(self.key, 13), old)                  # a stray drop is not a listing
        self.assertEqual(self.w.load_wave(self.key, old["id"]), old)


# =============================================================== the module
class Module(unittest.TestCase):
    def test_imports_only_the_standard_library_and_harness_lib(self):
        self.assertTrue(WAVES_PY.is_file(), f"{WAVES_PY} does not exist")
        w = load_waves_module()
        self.assertEqual(os.path.realpath(w.__file__), os.path.realpath(str(WAVES_PY)))
        for name in PUBLIC:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(w, name, None)), name)
        self.assertEqual(w.DONE, ("in-review", "closed"))

        good = ["w20261022140859", "w20261022140859-2", "w20261022140859-10", "w00000000000000-123"]
        bad = ["w2026102214085", "w202610221408590", "x20261022140859", "W20261022140859", "20261022140859",
               "w20261022140859-", "w20261022140859-a", "w20261022140859 ", " w20261022140859",
               "w20261022140859-2-3", "w2026102214085a", "w20261022140859.json", ""]
        for s in good:
            with self.subTest(good=s):
                self.assertIsNotNone(w.WAVE_ID.fullmatch(s))
        for s in bad:
            with self.subTest(bad=s):
                self.assertIsNone(w.WAVE_ID.fullmatch(s))

        tree = ast.parse(WAVES_PY.read_text(encoding="utf-8"))
        futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        self.assertTrue(any(a.name == "annotations" for n in futures for a in n.names),
                        "waves.py lacks `from __future__ import annotations`")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        imported -= {"__future__", "harness_lib"}
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:  # 3.9 has no list to check against: every import must at least resolve from outside lib/
            for name in imported:
                spec = importlib.util.find_spec(name)
                self.assertIsNotNone(spec, name)
                self.assertNotIn("site-packages", str(getattr(spec, "origin", "") or ""), name)
            return
        self.assertEqual(sorted(imported - set(stdlib)), [])


if __name__ == "__main__":
    unittest.main()
