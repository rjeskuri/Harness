"""harness-pr learn: the PRs merged and the tickets parked since the last recorded learn run (#424).

`learn` reads the window's start from --since, else `last_run` in ~/.claude/harness/repos/<key>/learn.json, else
7 days ago; lists each PR merged since then (gh pr list --state merged, filtered by mergedAt) with its ticket (from
the t/N- branch), the findings of that ticket's review rounds and the PR's conversation without the harness's own;
and every issue comment since then whose first line is `**Parked**`. `learn --mark` records now as `last_run` and
calls no gh. The fixtures are in pr_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_pr_learn.py" -v
"""

from __future__ import annotations

import time
import unittest
import urllib.parse

from gitrepo_support import run_in_process
from pr_support import *  # noqa: F401,F403

LAST = "2026-11-16T09:00:00Z"
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
EMPTY = {"threads": [], "reviews": [], "comments": []}


def when(text):
    """Seconds since the epoch of an ISO-8601 time (Z or an offset)."""
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def iso(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merged(number, title, at, branch):
    return {"number": number, "title": title, "url": f"https://github.com/o/r/pull/{number}", "mergedAt": at,
            "headRefName": branch}


def issue_comment(cid, ticket, created, body, updated=None):
    """An issue comment as the REST API lists it (repos/o/r/issues/comments)."""
    return {"id": cid, "html_url": f"https://github.com/o/r/issues/{ticket}#issuecomment-{cid}",
            "issue_url": f"https://api.github.com/repos/o/r/issues/{ticket}", "body": body, "created_at": created,
            "updated_at": updated or created, "user": {"login": "ryanje"}}


def parked_body(reason):
    """A park comment as harness-board's park_body writes it: the header, a blank line, the reason, the rest."""
    return (f"**Parked**\n\n{reason}\n\n```\nFAIL: test_widget\n```\n\nFollow-up: #500\n"
            "Retry #1: `harness-board status 1 ready`\n\n<!-- harness -->")


def finding(path, line, severity, body):
    return {"path": path, "line": line, "severity": severity, "body": body}


# The rounds of ticket 416: round 1 blocks on one finding and has one outside; round 2 leaves one low.
ROUNDS_416 = [
    {"round": 1, "at": "2026-11-19T10:00:00Z", "head": "a" * 40, "base": "b" * 40,
     "blocking": [finding("bin/prune", 40, "high", "Deletes the live branch.")], "low": [],
     "outside": [finding("README.md", None, "medium", "The docs drift.")], "pr": 419, "review_id": 1},
    {"round": 2, "at": "2026-11-20T10:00:00Z", "head": "c" * 40, "base": "b" * 40,
     "blocking": [], "low": [finding("bin/prune", 12, "low", "Name the remote.")], "outside": [], "pr": 419,
     "review_id": 2},
]
# Ticket 267's PR merged before the window: its round must not show.
ROUNDS_267 = [{"round": 1, "at": "2026-11-15T10:00:00Z", "head": "d" * 40, "base": "b" * 40,
               "blocking": [], "low": [finding("x.py", 1, "low", "Too old to list.")], "outside": []}]

CONVERSATION_419 = graphql(
    threads=[thread(False, gql_comment("pal", "Why prune the remote branches too?", "2026-11-19T11:00:00Z",
                                       path="bin/prune", line=40)),
             thread(False, gql_comment("rjeskuri", f"[high] Deletes the live branch.\n\n{MARKER}",
                                       "2026-11-19T10:00:00Z", path="bin/prune", line=40))],
    reviews=[{"author": {"login": "rjeskuri"}, "body": f"Harness review (opus): 1 finding\n\n{MARKER}",
              "state": "COMMENTED", "createdAt": "2026-11-19T10:00:00Z", "url": "https://github.com/o/r/pull/419#r1"},
             {"author": {"login": "pal"}, "body": "Ship it once the canary passes.", "state": "APPROVED",
              "createdAt": "2026-11-20T09:00:00Z", "url": "https://github.com/o/r/pull/419#r2"}],
    comments=[{"author": {"login": "rjeskuri"}, "body": f"Harness review: no findings. Checked: x\n\n{MARKER}",
               "createdAt": "2026-11-20T12:00:00Z", "url": "https://github.com/o/r/pull/419#c1"},
              # older than the window, on a PR merged inside it: the whole conversation is listed
              {"author": {"login": "rjeskuri"}, "body": "Started before the last learn run.",
               "createdAt": "2026-11-14T08:00:00Z", "url": "https://github.com/o/r/pull/419#c2"}])


class Learn(PrTest):
    """A test's first `harness-pr` run is a process of its own; its later runs run the tool in this process
    (gitrepo_support.run_in_process) with the same argv, cwd and environment."""

    ran_a_process = False

    def pr(self, *args, cwd=None, stubs=True, **env):
        if not self.ran_a_process:
            self.ran_a_process = True
            return super().pr(*args, cwd=cwd, stubs=stubs, **env)
        return run_in_process(TOOL, args=args, cwd=cwd or self.main, env=self.env(stubs=stubs, **env))

    # ----------------------------------------------------------- helpers
    def record(self, last_run):
        write(self.state_path("learn.json"), json.dumps({"last_run": last_run}))

    def recorded(self):
        with open(self.state_path("learn.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def seed_rounds(self, ticket, rounds):
        write(self.state_path("reviews", str(ticket), "rounds.json"), json.dumps(rounds))

    def learn_scenario(self, prs, pages, conversations=None):
        calls = [REPO_VIEW]
        for number, convo in (conversations or {}).items():
            calls.append({"contains": ["graphql", f"number={number}"], "stdout_json": convo})
        calls += [{"match": ["api", "graphql"], "stdout_json": graphql()},
                  {"match": ["pr", "list"], "stdout_json": prs},
                  {"match": ["api"], "stdout_json": pages}]       # issues/comments, --paginate --slurp: a list of pages
        self.scenario(calls)

    def learn_json(self, *args):
        return json.loads(self.ok("learn", "--json", *args).stdout)

    def search_dates(self):
        """The `merged:>=` values of every gh pr list call."""
        return [a.split("merged:>=", 1)[1].split()[0] for argv in self.gh_argv() if argv[:2] == ["pr", "list"]
                for a in argv if "merged:>=" in a]

    def comments_sinces(self):
        """The `since=` values of every issues/comments call, decoded."""
        out = []
        for argv in self.gh_argv():
            for a in argv:
                if "issues/comments" in a:
                    query = urllib.parse.urlsplit(urllib.parse.unquote(a)).query
                    out += urllib.parse.parse_qs(query).get("since", [])
        return out

    def by_number(self, data):
        return {p["number"]: p for p in data["prs"]}

    def parks(self, data):
        return sorted(({k: p.get(k) for k in ("ticket", "url", "created_at", "reason")} for p in data["parked"]),
                      key=lambda p: p["created_at"])

    # ----------------------------------------------------------- tests
    def test_learn_lists_prs_merged_and_parks_since_the_last_run_with_their_findings(self):
        self.record(LAST)
        self.seed_rounds(416, ROUNDS_416)
        self.seed_rounds(267, ROUNDS_267)
        prs = [merged(419, "Prune stale ticket branches", "2026-11-20T15:00:00Z", "t/416-prune"),
               merged(418, "Dispatch hourly", LAST, "t/164-dispatch"),                    # at `since`: in
               merged(417, "Fix a typo", "2026-11-18T08:00:00Z", "fix-typo"),               # no ticket branch
               merged(415, "Guard the state directory", "2026-11-16T08:59:59Z", "t/267-guard")]   # before: out
        pages = [[issue_comment(1, 420, "2026-11-21T10:00:00Z", parked_body("Tests still red after 3 rounds.")),
                  issue_comment(2, 421, "2026-11-22T10:00:00Z",
                                "**Needs your input**\n\nShould it be **Parked**?\n\n<!-- harness -->"),
                  issue_comment(3, 422, "2026-11-22T11:00:00Z", "See the note below.\n**Parked**\nNot a park."),
                  # created before the window, edited inside it (the API's since filters by update): out
                  issue_comment(4, 405, "2026-11-15T10:00:00Z", parked_body("Stale park."),
                                updated="2026-11-17T10:00:00Z")],
                 [issue_comment(5, 411, LAST, "**Parked**\nCI minutes exhausted.")]]          # at `since`: in
        self.learn_scenario(prs, pages, {419: CONVERSATION_419, 418: graphql(), 417: graphql(),
                                         415: graphql(comments=[{"author": {"login": "pal"}, "body": "Out of range.",
                                                                 "createdAt": "2026-11-16T08:00:00Z", "url": "u"}])})

        data = self.learn_json()
        self.assertEqual(data["since"], LAST)
        self.assertEqual(data["last_run"], LAST)
        found = self.by_number(data)
        self.assertEqual(sorted(found), [417, 418, 419])
        self.assertEqual({n: (p["title"], p["url"], p["merged_at"], p["ticket"]) for n, p in found.items()}, {
            419: ("Prune stale ticket branches", "https://github.com/o/r/pull/419", "2026-11-20T15:00:00Z", 416),
            418: ("Dispatch hourly", "https://github.com/o/r/pull/418", LAST, 164),
            417: ("Fix a typo", "https://github.com/o/r/pull/417", "2026-11-18T08:00:00Z", None)})
        self.assertEqual(sorted(found[419]["findings"], key=lambda f: (f["round"], f["body"])), [
            {"round": 1, "severity": "high", "path": "bin/prune", "line": 40, "body": "Deletes the live branch."},
            {"round": 1, "severity": "medium", "path": "README.md", "line": None, "body": "The docs drift."},
            {"round": 2, "severity": "low", "path": "bin/prune", "line": 12, "body": "Name the remote."}])
        self.assertEqual(found[418]["findings"], [])                  # a ticket without rounds
        self.assertEqual(found[417]["findings"], [])                  # no ticket
        talk = found[419]["comments"]
        self.assertEqual([[c["body"] for c in t["comments"]] for t in talk["threads"]],
                         [["Why prune the remote branches too?"]])
        self.assertEqual(talk["threads"][0]["path"], "bin/prune")
        self.assertEqual([r["body"] for r in talk["reviews"]], ["Ship it once the canary passes."])
        self.assertEqual([c["body"] for c in talk["comments"]], ["Started before the last learn run."])
        self.assertEqual(found[418]["comments"], EMPTY)
        self.assertEqual(self.parks(data), [
            {"ticket": 411, "url": "https://github.com/o/r/issues/411#issuecomment-5", "created_at": LAST,
             "reason": "CI minutes exhausted."},
            {"ticket": 420, "url": "https://github.com/o/r/issues/420#issuecomment-1",
             "created_at": "2026-11-21T10:00:00Z", "reason": "Tests still red after 3 rounds."}])
        self.assertEqual(self.search_dates(), ["2026-11-16"])
        self.assertTrue(self.comments_sinces(), self.gh_argv())
        self.assertEqual({when(s) for s in self.comments_sinces()}, {when(LAST)})
        self.assertEqual(self.recorded(), {"last_run": LAST})          # listing records nothing

        text = self.ok("learn").stdout
        for piece in (LAST, "419", "Prune stale ticket branches", "Deletes the live branch.", "Name the remote.",
                      "Why prune the remote branches too?", "Tests still red after 3 rounds.",
                      "CI minutes exhausted."):
            self.assertIn(piece, text)
        for absent in ("Guard the state directory", "Too old to list.", "Stale park.", "Not a park.", MARKER):
            self.assertNotIn(absent, text)

    def test_without_a_record_the_window_is_seven_days_and_since_overrides_it(self):
        now = time.time()
        day = 86400
        prs = [merged(51, "Six days ago", iso(now - 6 * day), "t/41-six"),
               merged(50, "Eight days ago", iso(now - 8 * day), "t/40-eight"),
               merged(49, "Eleven days ago", iso(now - 11 * day), "t/39-eleven")]
        pages = [[issue_comment(11, 61, iso(now - 6 * day), parked_body("Six-day park.")),
                  issue_comment(12, 62, iso(now - 8 * day), parked_body("Eight-day park.")),
                  issue_comment(13, 63, iso(now - 11 * day), parked_body("Eleven-day park."))]]
        rows = [
            # (what is recorded, --since, the expected since, the PRs listed, the parks' reasons)
            (None, None, now - 7 * day, [51], ["Six-day park."]),
            (iso(now - 2 * day), iso(now - 10 * day), now - 10 * day, [50, 51], ["Eight-day park.", "Six-day park."]),
            (iso(now - 2 * day), None, now - 2 * day, [], []),
            (iso(now - 9 * day), None, now - 9 * day, [50, 51], ["Eight-day park.", "Six-day park."]),
        ]
        for recorded, since, expected, numbers, reasons in rows:
            with self.subTest(recorded=recorded, since=since):
                path = self.state_path("learn.json")
                if recorded is None and os.path.exists(path):
                    os.unlink(path)
                elif recorded is not None:
                    self.record(recorded)
                self.learn_scenario(prs, pages)
                data = self.learn_json(*(["--since", since] if since else []))
                self.assertLess(abs(when(data["since"]) - expected), 120, data["since"])
                self.assertEqual(data["last_run"], recorded)
                self.assertEqual(sorted(p["number"] for p in data["prs"]), numbers)
                self.assertEqual([p["reason"] for p in self.parks(data)], reasons)
                self.assertEqual(self.search_dates(), [iso(when(data["since"]))[:10]])

    def test_mark_records_the_time_and_calls_no_gh(self):
        self.scenario([])                                     # any gh call fails, and is logged
        before = int(time.time())
        p = self.ok("learn", "--mark")
        after = time.time()
        self.assertEqual(self.gh_calls(), [])
        stamp = self.recorded()["last_run"]
        self.assertRegex(stamp, STAMP)
        self.assertGreaterEqual(when(stamp), before)
        self.assertLessEqual(when(stamp), after + 1)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1, p.stdout)
        self.assertIn(stamp, p.stdout)

        self.learn_scenario([merged(60, "Merged long ago", "2026-01-01T00:00:00Z", "t/59-old")],
                            [[issue_comment(21, 70, "2026-01-01T00:00:00Z", parked_body("Old park."))]])
        data = self.learn_json()
        self.assertEqual(data["last_run"], stamp)
        self.assertEqual(data["since"], stamp)
        self.assertEqual(data["prs"], [])
        self.assertEqual(data["parked"], [])
        self.assertEqual(self.search_dates(), [stamp[:10]])


if __name__ == "__main__":
    unittest.main()
