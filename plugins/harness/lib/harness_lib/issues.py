"""Issues and a branch's pull requests through GitHub's REST API (#447): `gh api repos/{owner}/{repo}/...`, which
spends the core rate-limit bucket, in place of `gh issue view|list|edit|comment|close` and `gh pr list --head`, which
spend the account's 5,000 GraphQL points an hour that every tool and session shares. Each read answers in the shape the
tools read from `gh issue view --json`, `gh issue list --json` and `gh pr list --json`, so REST's names live here
only: html_url is url, user.login is author.login, created_at is createdAt, state and state_reason are upper case
(OPEN, CLOSED, COMPLETED), sub_issues_summary is subIssuesSummary, and a pull request with merged_at is MERGED.

Rest(cwd, ...) runs the calls in `cwd` (gh fills {owner} and {repo} from that checkout), each failure worded as the gh
command it stands for (`gh issue view 12 failed: <why>`) with the caller's own pick, describe and texts. Issues(rest)
reads each issue, its comments, parent and blockers once (its cache, one command's) and keeps that cache in line with
its own writes; drop(n) forgets one issue, for a caller whose child process changed it.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from harness_lib.gh import describe as gh_describe, gh, gh_json, gh_run
from harness_lib.text import first_line

REPO = "repos/{owner}/{repo}"
PAGE = 100                               # rows per REST page, GitHub's most
NOT_FOUND = re.compile(r"\b404\b|Not Found")
BASE = ("number", "title", "body", "state", "stateReason", "url", "labels", "subIssuesSummary")


def upper(value):
    return str(value).upper() if value else None


def labels_of(raw):
    return [{"name": x["name"]} for x in raw.get("labels") or [] if isinstance(x, dict) and isinstance(x.get("name"), str)]


def issue_shape(raw):
    """A REST issue as `gh issue view --json` and `gh issue list --json` give its fields."""
    summary = raw.get("sub_issues_summary") if isinstance(raw.get("sub_issues_summary"), dict) else {}
    return {"number": raw.get("number"), "title": raw.get("title") or "", "body": raw.get("body") or "",
            "state": upper(raw.get("state")) or "OPEN", "stateReason": upper(raw.get("state_reason")),
            "url": raw.get("html_url"), "labels": labels_of(raw),
            "subIssuesSummary": {"total": int(summary.get("total") or 0),
                                 "completed": int(summary.get("completed") or 0),
                                 "percentCompleted": int(summary.get("percent_completed") or 0)}}


def comment_shape(raw):
    user = raw.get("user") if isinstance(raw.get("user"), dict) else None
    return {"author": {"login": user.get("login")} if user and user.get("login") else None,
            "body": raw.get("body") or "", "createdAt": raw.get("created_at"), "url": raw.get("html_url")}


def pull_shape(raw):
    """A REST pull request as `gh pr list --json` gives it: state OPEN, CLOSED or MERGED."""
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    merged = raw.get("merged_at") or None
    return {"number": raw.get("number"), "state": "MERGED" if merged else upper(raw.get("state")) or "OPEN",
            "isDraft": bool(raw.get("draft")), "mergedAt": merged, "url": raw.get("html_url"),
            "title": raw.get("title"), "createdAt": raw.get("created_at"), "headRefName": head.get("ref"),
            "headRefOid": head.get("sha")}


def rows(data):
    """The rows of a `--paginate --slurp` answer (a list of pages), or of one page."""
    if isinstance(data, list) and all(isinstance(page, list) for page in data):
        return [x for page in data for x in page if isinstance(x, dict)]
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


class Rest:
    """gh api calls in `cwd`, a failure worded for the gh command each stands for."""

    def __init__(self, cwd, *, pick=first_line, describe=gh_describe, texts=None, errors="replace", timeout=60):
        self.cwd, self.pick, self.describe, self.texts = cwd, pick, describe, texts
        self.errors, self.timeout = errors, timeout

    def call(self, args, stands_for, missing_ok=False):
        """gh `args`: its stdout; None when `missing_ok` and GitHub answered 404; else Problem worded for
        `stands_for`."""
        def words(_args):
            return self.describe(stands_for)
        done = gh_run(args, self.cwd, None, self.timeout, errors=self.errors, describe=words, texts=self.texts)
        if done.returncode != 0 and missing_ok and NOT_FOUND.search(f"{done.stderr}\n{done.stdout}"):
            return None
        return gh(args, self.cwd, pick=self.pick, describe=words, texts=self.texts, run=lambda *_: done)

    def json(self, args, stands_for, missing_ok=False):
        out = self.call(args, stands_for, missing_ok)
        if out is None:
            return None
        return gh_json(args, self.cwd, pick=self.pick, empty_ok=True, via=lambda *_: out,
                       describe=lambda _args: self.describe(stands_for), texts=self.texts)


class Issues:
    """One command's issue reads and writes through `rest` (a Rest), each issue read once."""

    def __init__(self, rest):
        self.rest, self.seen = rest, {}

    def drop(self, n):
        self.seen.pop(int(n), None)

    def _once(self, n, key, fetch):
        entry = self.seen.setdefault(int(n), {})
        if key not in entry:
            entry[key] = fetch()
        return entry[key]

    def _list(self, path, stands_for, query=None):
        q = urlencode(dict(query or {}, per_page=PAGE))
        return rows(self.rest.json(["api", f"{REPO}/{path}?{q}", "--paginate", "--slurp"], stands_for))

    def raw(self, n):
        """GET issues/N, the REST issue object."""
        return self._once(n, "issue", lambda: self.rest.json(["api", f"{REPO}/issues/{n}"], view_cmd(n)))

    def view(self, n, fields):
        """Issue n as `gh issue view n --json <fields>` (a comma list) prints it, None when GitHub answered no issue:
        the issue first, then comments, parent and blockedBy when asked, a call each only where the issue says there
        is any."""
        wanted = [f.strip() for f in fields.split(",")] if isinstance(fields, str) else list(fields)
        raw = self.raw(n)
        if not isinstance(raw, dict):
            return None
        shape = issue_shape(raw)
        out = {k: shape[k] for k in wanted if k in BASE}
        if "comments" in wanted:
            out["comments"] = self.comments(n)
        if "parent" in wanted:
            out["parent"] = self.parent(n)
        if "blockedBy" in wanted:
            out["blockedBy"] = self.blocked_by(n)
        return out

    def comments(self, n, fresh=False):
        """[{author {login}, body, createdAt, url}], oldest first; `fresh` reads them again (a wait for a new one)."""
        entry = self.seen.setdefault(int(n), {})
        if fresh:
            entry.pop("comments", None)
        elif isinstance(entry.get("issue"), dict) and entry["issue"].get("comments") == 0:
            entry.setdefault("comments", [])
        return self._once(n, "comments", lambda: [comment_shape(c) for c in
                                                  self._list(f"issues/{n}/comments", view_cmd(n))])

    def parent(self, n):
        """{number, title, body} of issue n's parent, or None: none when the issue's parent_issue_url says so."""
        raw = self.raw(n)
        if isinstance(raw, dict) and "parent_issue_url" in raw and not raw["parent_issue_url"]:
            return None

        def fetch():
            found = self.rest.json(["api", f"{REPO}/issues/{n}/parent"], view_cmd(n), missing_ok=True)
            return {k: issue_shape(found)[k] for k in ("number", "title", "body")} if isinstance(found, dict) else None
        return self._once(n, "parent", fetch)

    def blocked_by(self, n, raw=None):
        """[{number, state}] of the issues blocking n; no call when its dependencies summary counts none."""
        raw = self.raw(n) if raw is None else raw
        summary = raw.get("issue_dependencies_summary") if isinstance(raw, dict) else None
        if isinstance(summary, dict) and summary.get("total_blocked_by") == 0:
            return []
        return self._once(n, "blockedBy", lambda: [{"number": b.get("number"), "state": upper(b.get("state"))}
                                                   for b in self._list(f"issues/{n}/dependencies/blocked_by",
                                                                       view_cmd(n))])

    def sub_issues(self, n):
        """Issue n's sub-issues, each as issue_shape gives it."""
        return [issue_shape(s) for s in self._list(f"issues/{n}/sub_issues", ["api", f"{REPO}/issues/{n}/sub_issues"])]

    def list(self, labels=(), state="open", blocked=False):
        """The issues (pull requests left out) with every one of `labels` in `state`, newest first, as
        `gh issue list --json` gives them, with blockedBy when `blocked`."""
        query = {"state": state}
        if labels:
            query["labels"] = ",".join(labels)
        out = []
        for raw in self._list("issues", ["issue", "list"], query):
            if raw.get("pull_request"):
                continue
            item = issue_shape(raw)
            if blocked:
                item["blockedBy"] = self.blocked_by(raw.get("number"), raw)
            out.append(item)
        return out

    # ---------------------------------------------------- writes
    def edit_labels(self, n, add=(), remove=()):
        """Add `add` (one POST), then remove each of `remove` (a DELETE each; one already gone is no error)."""
        cmd = ["issue", "edit", str(n)]
        if add:
            fields = [x for name in add for x in ("-f", f"labels[]={name}")]
            self.rest.call(["api", "-X", "POST", f"{REPO}/issues/{n}/labels"] + fields, cmd)
        for name in remove:
            self.rest.call(["api", "-X", "DELETE", f"{REPO}/issues/{n}/labels/{quote(name, safe='')}"], cmd,
                           missing_ok=True)
        raw = self.seen.get(int(n), {}).get("issue")
        if isinstance(raw, dict):
            names = [x["name"] for x in labels_of(raw) if x["name"] not in remove]
            raw["labels"] = [{"name": x} for x in names + [x for x in add if x not in names]]

    def comment(self, n, body):
        """Post `body` on issue n; the comment's URL, or "" when GitHub named none."""
        found = self.rest.json(["api", "-X", "POST", f"{REPO}/issues/{n}/comments", "-f", f"body={body}"],
                               ["issue", "comment", str(n)])
        found = found if isinstance(found, dict) else {}
        entry = self.seen.get(int(n), {})
        if isinstance(entry.get("comments"), list):
            entry["comments"].append(comment_shape(dict(found, body=body)))
        return found.get("html_url") or ""

    def close(self, n, comment=None):
        """Comment `comment` (when given), then close issue n as completed."""
        if comment:
            self.comment(n, comment)
        self.rest.call(["api", "-X", "PATCH", f"{REPO}/issues/{n}", "-f", "state=closed"], ["issue", "close", str(n)])
        raw = self.seen.get(int(n), {}).get("issue")
        if isinstance(raw, dict):
            raw["state"] = "closed"

    def pulls(self, branch, state="all", limit=PAGE):
        """The pull requests whose head is `branch` in `state` (open, closed or all), newest first, at most `limit`,
        as `gh pr list --head` gives them; what GitHub answered as it is when that is not a list."""
        q = f"head={{owner}}%3A{quote(str(branch), safe='/')}&state={state}&per_page={min(int(limit), PAGE)}"
        found = self.rest.json(["api", f"{REPO}/pulls?{q}"], ["pr", "list", "--head", str(branch)])
        return [pull_shape(r) for r in found if isinstance(r, dict)][:limit] if isinstance(found, list) else found


def view_cmd(n):
    return ["issue", "view", str(n)]
