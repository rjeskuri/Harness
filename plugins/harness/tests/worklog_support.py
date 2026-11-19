"""Fixtures for the per-ticket hours tests of harness-worklog (ticket #59): a stateful fake GitHub, client
projects that are real git repositories with an origin, and Claude Code session transcripts.

Not a test module (no `test_` prefix). Run as a script it IS the fake `gh` (the module writes a one-line
wrapper once per test, see `WorklogCase.setUp`): unlike the scripted fakegh.py, it keeps GitHub's issues and
their comments in a JSON state file (WORKLOG_FAKEGH_STATE), so a test asserts on what the tickets carry
after a run (one hours comment, its rows), never on the sequence of calls that put it there.

What the fake understands (anything else exits 1 with `fakegh: unsupported ...` on stderr):
- `gh api [-X M] [-f|-F|--raw-field|--field k=v] [--input FILE|-] [--paginate [--slurp]] [-H ..] ENDPOINT`
  for `user`, `repos/O/R`, `repos/O/R/issues/N`, `repos/O/R/issues/N/comments` (GET, POST),
  `repos/O/R/issues/comments/ID` (GET, PATCH, DELETE), `repos/O/R/issues/N/parent` and `.../sub_issues`;
  `{owner}`/`{repo}` placeholders resolve from the origin of gh's working directory, as gh does. No --jq.
- `gh issue view N --json FIELDS`, `gh issue list --json FIELDS [--state S] [--limit N] [--label L]`,
  `gh issue comment N (--body T | -b T | --body-file F | -F F) [--edit-last [--create-if-none]]`,
  `gh repo view --json nameWithOwner`, `gh auth status`; each with `-R/--repo OWNER/REPO`, else the origin of
  gh's working directory (exit 1 outside a repository, as gh does).
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

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
TOOL = os.path.join(BIN, "harness-worklog")
UTC = dt.timezone.utc
LOGIN = "ryan"
HOURS_MARKER = "<!-- harness-hours -->"
BOARD_MARKER = "<!-- harness -->"

# =====================================================================================================
# the fake gh (run as a script)
# =====================================================================================================


class _Fail(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def _slug_of(url):
    url = (url or "").strip()
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]*@)?github\.com(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", url) or \
        re.match(r"^(?:[^@/:]+@)?github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _cwd_slug():
    try:
        p = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        p = None
    slug = _slug_of(p.stdout) if p is not None and p.returncode == 0 else None
    if not slug:
        raise _Fail("failed to determine base repo: not a git repository with a GitHub origin "
                    f"(cwd {os.getcwd()})")
    return slug


class _State:
    def __init__(self, path):
        self.path = path
        self.lock = open(path + ".lock", "a+")
        import fcntl
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        with open(path, encoding="utf-8") as fh:
            self.data = json.load(fh)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)

    def close(self):
        self.lock.close()

    def issue(self, slug, number):
        repo = self.data["repos"].get(slug)
        issue = repo and repo["issues"].get(str(int(number)))
        if not issue:
            raise _Fail(f"gh: Not Found (HTTP 404) - {slug}#{number}")
        return issue

    def find_comment(self, slug, cid):
        for n, issue in (self.data["repos"].get(slug) or {"issues": {}})["issues"].items():
            for c in issue["comments"]:
                if c["id"] == int(cid):
                    return n, issue, c
        raise _Fail(f"gh: Not Found (HTTP 404) - comment {cid}")

    def now(self):
        self.data["clock"] = self.data.get("clock", 0) + 1
        return (dt.datetime(2026, 11, 1, tzinfo=UTC) + dt.timedelta(seconds=self.data["clock"])) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")

    def create(self, slug, number, body):
        issue = self.issue(slug, number)
        self.data["next_id"] += 1
        when = self.now()
        c = {"id": self.data["next_id"], "user": self.data["login"], "body": body, "created_at": when,
             "updated_at": when}
        issue["comments"].append(c)
        self.data["writes"].append({"op": "create", "repo": slug, "issue": int(number), "id": c["id"]})
        return c

    def edit(self, slug, number, c, body):
        if c["user"] != self.data["login"]:
            raise _Fail("gh: Must have admin rights to Repository. (HTTP 403)")
        c["body"] = body
        c["updated_at"] = self.now()
        self.data["writes"].append({"op": "edit", "repo": slug, "issue": int(number), "id": c["id"]})


def _rest_comment(slug, number, c):
    return {"id": c["id"], "node_id": f"IC_kwDOfake{c['id']}", "body": c["body"],
            "user": {"login": c["user"], "type": "User"}, "author_association": "OWNER",
            "html_url": f"https://github.com/{slug}/issues/{number}#issuecomment-{c['id']}",
            "issue_url": f"https://api.github.com/repos/{slug}/issues/{number}",
            "created_at": c["created_at"], "updated_at": c["updated_at"]}


def _rest_issue(slug, number, issue):
    return {"number": int(number), "title": issue["title"], "state": issue["state"].lower(),
            "body": issue.get("body", ""), "labels": [{"name": x} for x in issue.get("labels", [])],
            "html_url": f"https://github.com/{slug}/issues/{number}", "comments": len(issue["comments"]),
            "user": {"login": LOGIN}}


def _gql_comment(slug, number, c, login):
    return {"id": f"IC_kwDOfake{c['id']}", "author": {"login": c["user"]}, "authorAssociation": "OWNER",
            "body": c["body"], "createdAt": c["created_at"], "includesCreatedEdit": c["updated_at"] != c["created_at"],
            "isMinimized": False, "minimizedReason": "", "reactionGroups": [],
            "url": f"https://github.com/{slug}/issues/{number}#issuecomment-{c['id']}",
            "viewerDidAuthor": c["user"] == login}


def _gql_issue(st, slug, number, issue, fields):
    parent = issue.get("parent")
    full = {"number": int(number), "title": issue["title"], "state": issue["state"], "body": issue.get("body", ""),
            "url": f"https://github.com/{slug}/issues/{number}",
            "labels": [{"name": x} for x in issue.get("labels", [])],
            "comments": [_gql_comment(slug, number, c, st.data["login"]) for c in issue["comments"]],
            "parent": None, "closed": issue["state"] == "CLOSED", "author": {"login": LOGIN},
            "createdAt": "2026-08-01T00:00:00Z"}
    if parent:
        p = st.issue(slug, parent)
        full["parent"] = {"number": int(parent), "title": p["title"], "state": p["state"],
                          "url": f"https://github.com/{slug}/issues/{parent}"}
    out = {}
    for f in fields:
        if f not in full:
            raise _Fail(f"Unknown JSON field: {f!r}")
        out[f] = full[f]
    return out


def _read_value(value, stdin):
    if value.startswith("@"):
        if value == "@-":
            return stdin
        with open(value[1:], encoding="utf-8") as fh:
            return fh.read()
    return value


def _api(st, args, stdin):
    method, fields, source, paginate, slurp, endpoint = None, {}, None, False, False, None
    i = 0
    while i < len(args):
        a = args[i]
        key, eq, inline = a.partition("=") if a.startswith("--") else (a, "", "")
        if key in ("-X", "--method"):
            method = inline if eq else args[i + 1]
            i += 0 if eq else 1
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
        elif key in ("-f", "-F", "--raw-field", "--field"):
            pair = inline if eq else args[i + 1]
            i += 0 if eq else 1
            k, _, v = pair.partition("=")
            fields[k] = _read_value(v, stdin) if key in ("-F", "--field") else v
        elif key == "--input":
            source = inline if eq else args[i + 1]
            i += 0 if eq else 1
        elif a == "--paginate":
            paginate = True
        elif a == "--slurp":
            slurp = True
        elif key in ("-H", "--header", "--hostname", "--cache", "-p", "--preview"):
            i += 0 if eq else 1
        elif a == "--silent":
            pass
        elif a.startswith("-"):
            raise _Fail(f"fakegh: unsupported api flag {a}")
        elif endpoint is None:
            endpoint = a
        else:
            raise _Fail(f"fakegh: unsupported extra argument {a}")
        i += 1
    if endpoint is None:
        raise _Fail("fakegh: api without an endpoint")
    body = None
    if source is not None:
        text = stdin if source == "-" else open(source, encoding="utf-8").read()
        payload = json.loads(text or "{}")
        body = payload.get("body")
    elif "body" in fields:
        body = fields["body"]
    method = (method or ("POST" if fields or source is not None else "GET")).upper()
    path = endpoint.lstrip("/").split("?", 1)[0]
    if "{owner}" in path or "{repo}" in path:
        owner, repo = _cwd_slug().split("/")
        path = path.replace("{owner}", owner).replace("{repo}", repo)
    if path == "user" and method == "GET":
        return {"login": st.data["login"], "id": 1}, paginate and slurp
    m = re.fullmatch(r"repos/([^/]+/[^/]+)(?:/(.*))?", path)
    if not m:
        raise _Fail(f"fakegh: unsupported endpoint {endpoint}")
    slug, rest = m.group(1), m.group(2) or ""
    if slug not in st.data["repos"]:
        raise _Fail(f"gh: Not Found (HTTP 404) - {slug}")
    wrap = paginate and slurp
    if rest == "" and method == "GET":
        owner, name = slug.split("/")
        return {"full_name": slug, "name": name, "owner": {"login": owner},
                "html_url": f"https://github.com/{slug}"}, wrap
    m = re.fullmatch(r"issues/(\d+)/comments", rest)
    if m:
        n = m.group(1)
        if method == "GET":
            issue = st.issue(slug, n)
            return [_rest_comment(slug, n, c) for c in issue["comments"]], wrap
        if method == "POST":
            if not body:
                raise _Fail("gh: Validation Failed: body is missing (HTTP 422)")
            c = st.create(slug, n, body)
            st.save()
            return _rest_comment(slug, n, c), False
    m = re.fullmatch(r"issues/comments/(\d+)", rest)
    if m:
        n, issue, c = st.find_comment(slug, m.group(1))
        if method == "GET":
            return _rest_comment(slug, n, c), False
        if method == "PATCH":
            if not body:
                raise _Fail("gh: Validation Failed: body is missing (HTTP 422)")
            st.edit(slug, n, c, body)
            st.save()
            return _rest_comment(slug, n, c), False
        if method == "DELETE":
            issue["comments"].remove(c)
            st.data["writes"].append({"op": "delete", "repo": slug, "issue": int(n), "id": c["id"]})
            st.save()
            return None, False
    m = re.fullmatch(r"issues/(\d+)", rest)
    if m and method == "GET":
        return _rest_issue(slug, m.group(1), st.issue(slug, m.group(1))), False
    m = re.fullmatch(r"issues/(\d+)/parent", rest)
    if m and method == "GET":
        parent = st.issue(slug, m.group(1)).get("parent")
        if not parent:
            raise _Fail("gh: Not Found (HTTP 404)")
        return _rest_issue(slug, parent, st.issue(slug, parent)), False
    m = re.fullmatch(r"issues/(\d+)/sub_issues", rest)
    if m and method == "GET":
        st.issue(slug, m.group(1))
        subs = [(k, v) for k, v in st.data["repos"][slug]["issues"].items() if v.get("parent") == int(m.group(1))]
        return [_rest_issue(slug, k, v) for k, v in subs], wrap
    raise _Fail(f"fakegh: unsupported {method} {endpoint}")


def _split_repo(args):
    out, slug, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo"):
            slug = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            slug = a.split("=", 1)[1]
        else:
            out.append(a)
        i += 1
    return out, slug


def _opt(args, *names):
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def _issue_cmd(st, args, slug, stdin):
    sub = args[0] if args else ""
    if sub == "view":
        n = next((a for a in args[1:] if a.lstrip("#").isdigit()), None)
        fields = _opt(args, "--json")
        if n is None or fields is None:
            raise _Fail("fakegh: unsupported issue view (give a number and --json)")
        n = n.lstrip("#")
        return _gql_issue(st, slug, n, st.issue(slug, n), [f for f in fields.split(",") if f])
    if sub == "list":
        fields = _opt(args, "--json")
        if fields is None:
            raise _Fail("fakegh: unsupported issue list without --json")
        state = (_opt(args, "--state", "-s") or "open").upper()
        label = _opt(args, "--label", "-l")
        limit = int(_opt(args, "--limit", "-L") or 30)
        out = []
        for n, issue in sorted(st.data["repos"].get(slug, {"issues": {}})["issues"].items(), key=lambda x: -int(x[0])):
            if state != "ALL" and issue["state"] != state:
                continue
            if label and label not in issue.get("labels", []):
                continue
            out.append(_gql_issue(st, slug, n, issue, [f for f in fields.split(",") if f]))
        return out[:limit]
    if sub == "comment":
        n = next((a for a in args[1:] if a.lstrip("#").isdigit()), None)
        if n is None:
            raise _Fail("fakegh: issue comment without a number")
        n = n.lstrip("#")
        body = _opt(args, "--body", "-b")
        body_file = _opt(args, "--body-file", "-F")
        if body is None and body_file is not None:
            body = _read_value("@" + body_file, stdin)
        if not body:
            raise _Fail("fakegh: unsupported issue comment without --body or --body-file")
        issue = st.issue(slug, n)
        if "--edit-last" in args:
            mine = [c for c in issue["comments"] if c["user"] == st.data["login"]]
            if mine:
                st.edit(slug, n, mine[-1], body)
                st.save()
                return f"https://github.com/{slug}/issues/{n}#issuecomment-{mine[-1]['id']}\n"
            if "--create-if-none" not in args:
                raise _Fail("no comments found for current user")
        c = st.create(slug, n, body)
        st.save()
        return f"https://github.com/{slug}/issues/{n}#issuecomment-{c['id']}\n"
    raise _Fail(f"fakegh: unsupported issue {sub}")


def fake_gh_main(argv):
    stdin = ""
    try:
        if not sys.stdin.isatty():
            import select
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if ready:
                stdin = sys.stdin.read()
    except Exception:
        stdin = ""
    st = _State(os.environ["WORKLOG_FAKEGH_STATE"])
    try:
        st.data.setdefault("calls", []).append({"argv": argv, "cwd": os.getcwd()})
        st.save()
        if argv[:2] == ["auth", "status"]:
            sys.stdout.write(f"github.com\n  Logged in to github.com account {st.data['login']}\n")
            return 0
        if argv and argv[0] == "api":
            out, wrap = _api(st, argv[1:], stdin)
            if out is not None:
                sys.stdout.write(json.dumps([out] if wrap else out))
            return 0
        args, slug = _split_repo(argv)
        if args and args[0] in ("issue", "repo"):
            slug = slug or _cwd_slug()
            if args[0] == "repo":
                if args[1:2] != ["view"]:
                    raise _Fail(f"fakegh: unsupported repo {' '.join(args[1:2])}")
                owner, name = slug.split("/")
                full = {"nameWithOwner": slug, "name": name, "owner": {"login": owner},
                        "url": f"https://github.com/{slug}"}
                fields = (_opt(args, "--json") or "nameWithOwner").split(",")
                sys.stdout.write(json.dumps({f: full[f] for f in fields if f in full}))
                return 0
            out = _issue_cmd(st, args[1:], slug, stdin)
            sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
            return 0
        raise _Fail("fakegh: unsupported command gh " + " ".join(argv))
    except _Fail as err:
        sys.stderr.write(str(err) + "\n")
        return err.code
    finally:
        st.close()


# =====================================================================================================
# the test side
# =====================================================================================================


def encode(path):
    """Claude Code's session folder name for a project path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def mac_zone():
    """The Mac's own zone, read here (not through the tool) as `harness-worklog init` records it."""
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(os.readlink("/etc/localtime").split("zoneinfo/", 1)[1])
    except (OSError, IndexError, KeyError, ValueError):
        return dt.datetime.now().astimezone().tzinfo


def stamp(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(\d+\.\d{2})\s*\|", re.M)


def hours_rows(body):
    """{date: 'H.HH'} from an hours comment's table rows (`| 2026-09-08 | 0.75 |...`); a date written twice is an
    AssertionError, since a day's hours must be one row."""
    rows = ROW.findall(body)
    dates = [d for d, _ in rows]
    if len(dates) != len(set(dates)):
        raise AssertionError(f"a day is listed twice in the hours comment:\n{body}")
    return dict(rows)


class WorklogCase(unittest.TestCase):
    """A temporary HOME, a fake gh with an empty GitHub, and helpers to make projects and sessions."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-worklog-tickets-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.tz = mac_zone()
        self.today = dt.datetime.now(UTC).astimezone(self.tz).date()
        fakebin = self.mk("fakebin")
        self.gh = os.path.join(fakebin, "gh")
        with open(self.gh, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.abspath(__file__)}" "$@"\n')
        os.chmod(self.gh, 0o755)
        self.state = os.path.join(self.tmp, "github.json")
        self.save({"login": LOGIN, "next_id": 5000, "repos": {}, "writes": [], "calls": []})
        from gitrepo_support import GIT
        self.git_bin = GIT
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "HARNESS_SESSION_PROJECT", "GIT_DIR", "GIT_WORK_TREE",
                            "GIT_INDEX_FILE", "GIT_COMMON_DIR", "HARNESS_GH", "GH_TOKEN", "GITHUB_TOKEN",
                            "GH_REPO", "GH_HOST")}
        env.update(HOME=self.home, PATH=os.pathsep.join([fakebin, os.path.dirname(GIT), "/usr/bin", "/bin"]),
                   HARNESS_GH=self.gh, WORKLOG_FAKEGH_STATE=self.state, GIT_CEILING_DIRECTORIES=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
                   PYTHONDONTWRITEBYTECODE="1")
        self.env = env
        self.runs = 0

    # ---------------------------------------------------------------- the fake GitHub
    def load(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data):
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)

    def add_issue(self, slug, number, title, state="OPEN", parent=None, comments=()):
        """Issue `number` on `slug`; `comments` are (author login, body) in order."""
        data = self.load()
        issues = data["repos"].setdefault(slug, {"issues": {}})["issues"]
        issue = {"title": title, "state": state, "parent": parent, "labels": [], "body": "", "comments": []}
        issues[str(number)] = issue
        self.save(data)
        for user, body in comments:
            self.add_comment(slug, number, body, user)

    def add_comment(self, slug, number, body, user=LOGIN):
        data = self.load()
        data["next_id"] += 1
        data["repos"][slug]["issues"][str(number)]["comments"].append(
            {"id": data["next_id"], "user": user, "body": body, "created_at": "2026-08-01T00:00:00Z",
             "updated_at": "2026-08-01T00:00:00Z"})
        self.save(data)

    def comments(self, slug, number):
        return self.load()["repos"][slug]["issues"][str(number)]["comments"]

    def hours_comments(self, slug, number):
        return [c for c in self.comments(slug, number) if HOURS_MARKER in c["body"]]

    def ticket_hours(self, slug):
        """{issue number: {date: 'H.HH'}} for every issue of `slug` carrying an hours comment; an issue with two
        hours comments fails the test."""
        out = {}
        for n in self.load()["repos"][slug]["issues"]:
            found = self.hours_comments(slug, n)
            self.assertLessEqual(len(found), 1, f"#{n} carries {len(found)} hours comments: {found}")
            if found:
                self.assertEqual(found[0]["user"], LOGIN)
                out[int(n)] = hours_rows(found[0]["body"])
        return out

    def calls_text(self):
        return "\n".join("gh " + " ".join(c["argv"]) for c in self.load().get("calls", []))

    # ---------------------------------------------------------------- projects and sessions
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def git(self, cwd, *args):
        env = dict(self.env, GIT_AUTHOR_DATE="2020-01-01T12:00:00+00:00", GIT_COMMITTER_DATE="2020-01-01T12:00:00+00:00")
        p = subprocess.run([self.git_bin, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")

    def project(self, name, origin):
        """A client project at <tmp>/<name>: a git repository on main, one commit dated long before the zone
        timeline (no evidence of the Mac's zone), `origin` as its remote."""
        path = self.mk(name)
        self.git(path, "-c", "init.defaultBranch=main", "init", "-q")
        with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("client\n")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-q", "-m", "Initial commit")
        self.git(path, "remote", "add", "origin", origin)
        return path

    def folder(self, path):
        """The fake HOME's transcript folder for a project or worktree path."""
        return os.path.join(self.home, ".claude", "projects", encode(path))

    def worktree(self, repo, name):
        return os.path.join(repo, ".claude", "worktrees", name)

    def at(self, day, hhmm):
        hh, mm = (int(x) for x in hhmm.split(":"))
        return dt.datetime.combine(day, dt.time(hh, mm), self.tz)

    def transcript(self, folder, sid, entries, cwd=None, branch=None):
        """<folder>/<sid>.jsonl; entries are (kind 'user'|'assistant', aware datetime, user text or None).
        `cwd` and `branch` are written on every entry as Claude Code does (cwd, gitBranch), when given."""
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sid + ".jsonl"), "a", encoding="utf-8") as fh:
            for kind, when, text in entries:
                entry = {"type": kind, "sessionId": sid, "timestamp": stamp(when)}
                if cwd is not None:
                    entry["cwd"] = cwd
                if branch is not None:
                    entry["gitBranch"] = branch
                if kind == "user":
                    entry["message"] = {"role": "user", "content": text or "work on it"}
                else:
                    entry["message"] = {"role": "assistant", "content": [{"type": "text", "text": "On it."}]}
                fh.write(json.dumps(entry) + "\n")

    def worked(self, folder, sid, day, start, end, cwd=None, branch=None, text="work on it"):
        """A session in `folder`: the user's message at `start`, the agent every 10 minutes until `end`."""
        a, b = self.at(day, start), self.at(day, end)
        entries = [("user", a, text)]
        t = a + dt.timedelta(minutes=10)
        while t < b:
            entries.append(("assistant", t, None))
            t += dt.timedelta(minutes=10)
        entries.append(("assistant", b, None))
        self.transcript(folder, sid, entries, cwd=cwd, branch=branch)

    # ---------------------------------------------------------------- the tool
    def run_wl(self, repo, *args, ok=True):
        """harness-worklog --repo REPO ARGS from <tmp> (never from inside the project): this test's first run
        is a real process, the further ones run in this process (gitrepo_support.run_in_process)."""
        argv = ["--repo", repo, *args]
        if self.runs == 0:
            p = subprocess.run([sys.executable, TOOL, *argv], cwd=self.tmp, env=self.env, capture_output=True,
                               text=True, timeout=120)
        else:
            from gitrepo_support import run_in_process
            p = run_in_process(TOOL, argv, cwd=self.tmp, env=self.env)
        self.runs += 1
        if ok:
            self.assertEqual(p.returncode, 0, f"harness-worklog {' '.join(args)}:\n{p.stdout}{p.stderr}\n"
                                              f"gh calls:\n{self.calls_text()}")
        return p

    def init(self, repo, since):
        self.run_wl(repo, "init", "--since", since.isoformat())

    def log_rows(self, repo):
        path = os.path.join(repo, "worklog", "worklog.csv")
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))


if __name__ == "__main__":
    sys.exit(fake_gh_main(sys.argv[1:]))
