"""Tests for harness_lib/reviews.py (ticket #256, epic #249): the one reader and writer of review rounds.

- reviews_dir(key, ticket) is ~/.claude/harness/repos/<key>/reviews/<ticket>/;
- load_rounds(key, ticket) reads rounds.json, [] when it is missing or broken;
- save_round(key, ticket, rnd) appends the round and returns it numbered; a round whose head equals the
  latest round's replaces it and keeps its number (only the latest round is compared);
- latest_round(key, ticket) is the last round or None; blocking(rnd) is the round's blocking findings.

Choice the ticket left open, fixed here: save_round numbers the round itself (the caller's dict carries no
`round`), so the number is the latest's plus one, or the latest's own on a replacement.

Every fixture is written here, in a temporary HOME.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_reviews.py" -v
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

LIB = Path(__file__).resolve().parent.parent / "lib"


def load_reviews_module():
    """harness_lib.reviews from the checkout's lib/; the import error while it is missing is the red state."""
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    importlib.invalidate_caches()
    return importlib.import_module("harness_lib.reviews")


def finding(path, line, severity, body):
    return {"path": path, "line": line, "severity": severity, "body": body}


def rnd(head, base, blocking=(), low=(), outside=(), at="2026-10-26T10:00:00Z"):
    return {"at": at, "head": head, "base": base, "blocking": list(blocking), "low": list(low),
            "outside": list(outside), "pr": None, "review_id": None}


def numbered(r, k):
    return dict(r, round=k)


class Reviews(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-reviews-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.r = load_reviews_module()
        self.key = "0123456789ab"

    def dir_of(self, ticket, key=None):
        return os.path.join(self.home, ".claude", "harness", "repos", key or self.key, "reviews", str(ticket))

    def test_save_load_latest_and_replace(self):
        r = self.r
        for ticket, key in ((12, self.key), (7, "fedcba987654")):
            self.assertEqual(os.path.realpath(os.fspath(r.reviews_dir(key, ticket))),
                             os.path.realpath(self.dir_of(ticket, key)))

        # Missing: no rounds, no latest.
        self.assertEqual(r.load_rounds(self.key, 12), [])
        self.assertIsNone(r.latest_round(self.key, 12))

        high = finding("src/app.py", 3, "high", "A")
        medium = finding("tests/test_app.py", 2, "medium", "M")
        low = finding("src/app.py", 9, "low", "B")
        out = finding("README.md", 1, "medium", "C")

        r1 = rnd("a" * 40, "m" * 40, blocking=[high], low=[low], outside=[out])
        saved = r.save_round(self.key, 12, r1)
        self.assertEqual(saved, numbered(r1, 1))
        self.assertEqual(r.load_rounds(self.key, 12), [numbered(r1, 1)])
        self.assertEqual(r.latest_round(self.key, 12), numbered(r1, 1))
        self.assertTrue(os.path.isfile(os.path.join(self.dir_of(12), "rounds.json")))

        r2 = rnd("b" * 40, "a" * 40, blocking=[medium, high], at="2026-10-26T11:00:00Z")
        self.assertEqual(r.save_round(self.key, 12, r2), numbered(r2, 2))
        self.assertEqual(r.load_rounds(self.key, 12), [numbered(r1, 1), numbered(r2, 2)])
        self.assertEqual(r.blocking(r.latest_round(self.key, 12)), [medium, high])

        # Same head as the latest: replaced, number kept.
        r2b = rnd("b" * 40, "a" * 40, low=[low], at="2026-10-26T11:30:00Z")
        self.assertEqual(r.save_round(self.key, 12, r2b), numbered(r2b, 2))
        self.assertEqual(r.load_rounds(self.key, 12), [numbered(r1, 1), numbered(r2b, 2)])
        self.assertEqual(r.latest_round(self.key, 12), numbered(r2b, 2))
        self.assertEqual(r.blocking(r.latest_round(self.key, 12)), [])

        # A head equal to an older round's (not the latest's) is a new round.
        r3 = rnd("a" * 40, "b" * 40, blocking=[out], at="2026-10-26T12:00:00Z")
        self.assertEqual(r.save_round(self.key, 12, r3)["round"], 3)
        rounds = r.load_rounds(self.key, 12)
        self.assertEqual([x["round"] for x in rounds], [1, 2, 3])
        self.assertEqual([x["head"] for x in rounds], ["a" * 40, "b" * 40, "a" * 40])
        self.assertEqual(r.blocking(rounds[0]), [high])
        self.assertEqual(r.blocking(rounds[2]), [out])

        # Other tickets and other repositories are separate.
        self.assertEqual(r.load_rounds(self.key, 13), [])
        self.assertEqual(r.load_rounds("fedcba987654", 12), [])
        other = rnd("c" * 40, "m" * 40)
        self.assertEqual(r.save_round(self.key, 13, other)["round"], 1)
        self.assertEqual(len(r.load_rounds(self.key, 12)), 3)

        # Persisted as JSON in the rounds file, readable by a fresh import.
        with open(os.path.join(self.dir_of(12), "rounds.json"), encoding="utf-8") as fh:
            json.load(fh)
        sys.modules.pop("harness_lib.reviews", None)
        self.assertEqual(load_reviews_module().latest_round(self.key, 12), numbered(r3, 3))

        # Broken: [] and no latest; saving starts again at round 1.
        for broken in ("{not json", ""):
            with self.subTest(broken=broken):
                with open(os.path.join(self.dir_of(12), "rounds.json"), "w", encoding="utf-8") as fh:
                    fh.write(broken)
                self.assertEqual(r.load_rounds(self.key, 12), [])
                self.assertIsNone(r.latest_round(self.key, 12))
        self.assertEqual(r.save_round(self.key, 12, r2)["round"], 1)
        self.assertEqual(r.load_rounds(self.key, 12), [numbered(r2, 1)])


if __name__ == "__main__":
    unittest.main()
