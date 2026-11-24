"""Tests for the gh cache check of `harness-work budget` (ticket #470, epic #446).

gh caches some feature-detection GraphQL replies for 24 h, refusals included (cli/cli#12812), so a cached
`"type":"RATE_LIMIT"` reply can be replayed as a refusal long after the quota reset. The contract:
- gh's cache dir is $XDG_CACHE_HOME/gh when XDG_CACHE_HOME is set and not empty, else $HOME/.cache/gh.
- A cache entry is an HTTP response dump (headers, a blank line, the body), at any depth under that dir. It is a
  refusal when its body holds `"type":"RATE_LIMIT"`, with or without whitespace around the colon.
- `harness-work budget status` prints one line naming the path of each such file (and no other cache file); with
  none it prints nothing about the cache. It never removes anything.
- `harness-work budget clear-gh-cache` removes exactly those files, prints their paths, exits 0; with none it
  removes nothing and exits 0. Other files, and the directories, stay.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_work_budget_cache.py" -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
TOOL = os.path.join(PLUGIN, "bin", "harness-work")

HEADERS = ("HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
           "X-Ghcli-Cache-Ttl: 24h0m0s\r\nX-Ratelimit-Used: {used}\r\n\r\n")
REFUSAL_COMPACT = '{"data":{"repository":null},"errors":[{"type":"RATE_LIMIT","code":"graphql_rate_limit",' \
                  '"message":"API rate limit already exceeded for user ID 1."}]}'
REFUSAL_PRETTY = '{\n  "errors": [\n    {\n      "type" : "RATE_LIMIT",\n      "code": "graphql_rate_limit"\n' \
                 '    }\n  ]\n}'
NORMAL = '{"data":{"repository":{"hasIssuesEnabled":true,"id":"R_1"}}}'
OTHER_ERROR = '{"errors":[{"type":"NOT_FOUND","message":"Could not resolve to a Repository"}]}'


def entry(body, used=5000):
    return HEADERS.format(used=used) + body


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-budget-cache-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        self.cwd = os.path.join(self.tmp, "elsewhere")
        for d in (self.home, self.xdg, self.cwd):
            os.makedirs(d)

    def env(self, xdg=True):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "HARNESS_", "GIT_", "XDG_", "GH_")) and k != "PWD"}
        env.update(HOME=self.home, PATH="/usr/bin:/bin")
        if xdg:
            env["XDG_CACHE_HOME"] = self.xdg
        return env

    def put(self, root, rel, text):
        path = os.path.join(root, "gh", rel) if root == self.xdg else os.path.join(root, ".cache", "gh", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def work(self, *args, xdg=True):
        p = subprocess.run([sys.executable, TOOL, *args], cwd=self.cwd, env=self.env(xdg),
                           capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def seed(self, root):
        self.bad = [
            self.put(root, "1b/aa11", entry(REFUSAL_COMPACT)),
            self.put(root, "7f/deep/er/bb22", entry(REFUSAL_PRETTY)),
        ]
        self.good = [
            self.put(root, "1b/cc33", entry(NORMAL, used=12)),
            self.put(root, "2c/dd44", entry(OTHER_ERROR, used=12)),
            self.put(root, "9e/nested/ee55", entry(NORMAL, used=4999)),   # old headers say nothing
        ]


class StatusTest(CacheTest):
    def test_status_names_only_the_refusals(self):
        self.seed(self.xdg)
        out = self.work("budget", "status")
        for path in self.bad:
            self.assertIn(path, out)
        for path in self.good:
            self.assertNotIn(path, out)
        for path in self.bad + self.good:        # status reports, it does not remove
            self.assertTrue(os.path.exists(path), path)

    def test_status_follows_home_when_no_xdg_cache_home(self):
        self.seed(self.home)
        out = self.work("budget", "status", xdg=False)
        for path in self.bad:
            self.assertIn(path, out)
        for path in self.good:
            self.assertNotIn(path, out)

    def test_status_reads_xdg_cache_home_not_home_when_both_exist(self):
        elsewhere = self.put(self.home, "1b/zz99", entry(REFUSAL_COMPACT))   # $HOME/.cache/gh is not gh's dir here
        self.seed(self.xdg)
        out = self.work("budget", "status")
        self.assertNotIn(elsewhere, out)
        self.assertIn(self.bad[0], out)

    def test_status_with_no_refusal_reports_nothing_about_the_cache(self):
        self.put(self.xdg, "1b/cc33", entry(NORMAL))
        with_cache = self.work("budget", "status")
        shutil.rmtree(os.path.join(self.xdg, "gh"))                # no cache dir at all
        without_cache = self.work("budget", "status")
        self.assertEqual(with_cache, without_cache)
        self.assertNotIn(self.xdg, with_cache)


class ClearTest(CacheTest):
    def test_clear_removes_only_the_refusals(self):
        self.seed(self.xdg)
        out = self.work("budget", "clear-gh-cache")
        for path in self.bad:
            self.assertFalse(os.path.exists(path), path)
            self.assertIn(path, out)
        for path in self.good:
            self.assertTrue(os.path.exists(path), path)
            self.assertNotIn(path, out)
        self.assertNotIn(self.bad[0], self.work("budget", "status"))   # and status no longer names them

    def test_clear_follows_home_when_no_xdg_cache_home(self):
        self.seed(self.home)
        self.work("budget", "clear-gh-cache", xdg=False)
        self.assertEqual([os.path.exists(p) for p in self.bad], [False, False])
        self.assertEqual([os.path.exists(p) for p in self.good], [True, True, True])

    def test_clear_with_nothing_to_clear_removes_nothing(self):
        for rooted in (False, True):
            with self.subTest(cache_dir_exists=rooted):
                if rooted:
                    paths = [self.put(self.xdg, "1b/cc33", entry(NORMAL)),
                             self.put(self.xdg, "2c/dd44", entry(OTHER_ERROR))]
                else:
                    paths = []
                self.work("budget", "clear-gh-cache")
                self.assertTrue(all(os.path.exists(p) for p in paths))
                self.assertEqual(sorted(os.listdir(self.xdg)), ["gh"] if rooted else [])


if __name__ == "__main__":
    unittest.main()
