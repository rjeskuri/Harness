"""The owner's dismissals of review findings (#515): a standalone line `dismiss <path>:<line> <why>` in the owner's own
comment on a ticket or its PR (owner_comments' rules: the authenticated login casefolded, the harness's own comments
dropped) selects the finding at that place in the newest harness review posted before the comment, and that finding
stops blocking by its finding_key ([path, body]) wherever it is raised again, whatever its line or severity. A
selector that names no finding, or several, or a line without a reason, dismisses nothing and is reported.

A request is {"path", "line", "why", "at", "url", "source"}, `at` and `url` the comment's createdAt and url as given;
a snapshot is {"at", "findings"}, one harness review's findings ({"path", "line", "severity", "body"}) and its
submitted_at. A harness review is one by the authenticated account (anyone can type the marker) whose body ends with
the trailer harness-pr appends. Times compare as parsed datetimes, a snapshot strictly older than the comment; a time
that does not parse matches nothing. parse, resolve, apply and review_findings are pure; read and github_snapshots
call gh through harness_lib.gh and harness_lib.issues and raise Problem on a failed read.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import re

from harness_lib import owner_comments as oc
from harness_lib.autoland import NO_FINDINGS, REVIEW_MARKER
from harness_lib.cli import Problem
from harness_lib.gh import gh
from harness_lib.issues import PAGE, REPO, Rest, rows
from harness_lib.reviews import finding_key
from harness_lib.text import first_line, parse_iso

# `dismiss` first on its line, then path:line (in backticks or not, a leading ./ dropped), then the reason
REQUEST = re.compile(r"[ \t]*dismiss[ \t]+(`?)(?:\./)?([^\s`]+):([0-9]+)\1(?:\s+(.*))?", re.I)
DIGITS = re.compile(r"[0-9]+")                 # ASCII only: str.isdigit() also passes digits int() refuses
LISTED = "Not on a changed line:"            # review_body's heading over the findings in the body
ENTRY = re.compile(r"- (`([^`]*)`|\(no path\)) (.*)")
SEVERITY = re.compile(r"\[(\w+)\] ?(.*)", re.S)
SEVERITIES = ("high", "medium", "low")
CONTINUED = "  "                             # harness-pr's indented(): a body's further lines
INLINE_TRAILER = "\n\n" + REVIEW_MARKER       # what post_review appends to an inline comment's `[sev] body`
# a harness review body's end: the marker paragraph, then the GPT receipt (harness-pr's `harness-gpt-{uuid4()}`) or not
TRAILER = re.compile(re.escape(INLINE_TRAILER)
                     + r"(?:\n\n<!-- harness-gpt-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12} -->)?\Z")


def parse(comments, login, source):
    """One request per standalone `dismiss <path>:<line> <why>` line of the owner's own comments, in comment then
    line order; `why` is "" when the line gives none."""
    out = []
    for c in oc.owner_comments(comments, login):
        for line in (c.get("body") or "").splitlines():
            m = REQUEST.fullmatch(line)
            if m:
                out.append({"path": m.group(2), "line": int(m.group(3)), "why": (m.group(4) or "").strip(),
                            "at": c.get("createdAt"), "url": c.get("url"), "source": source})
    return out


def _line(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _at(item):
    """An order key on `at`: parsed times first, oldest first; one that does not parse last."""
    when = parse_iso(item.get("at"))
    return (0, when) if when is not None else (1, 0)


def resolve(requests, snapshots):
    """(dismissed, unmatched): each request, earliest first, matched in the newest snapshot strictly older than its
    comment that holds a finding at its path and line. One finding_key there dismisses it ({"key", "path", "line",
    "why", "at", "url", "source"}; a key already dismissed by an earlier request is not repeated); otherwise it is
    unmatched ({"selector", "reason", "at", "url", "source"}): no reason given, no finding at the place before the
    comment, or ambiguous, the findings at the place counted."""
    timed = []
    for s in snapshots:
        when = parse_iso(s.get("at")) if isinstance(s, dict) else None
        if when is not None:
            timed.append((when, [f for f in s.get("findings") or [] if isinstance(f, dict)]))
    timed.sort(key=lambda pair: pair[0], reverse=True)
    dismissed, unmatched, keys = [], [], set()
    for req in sorted((r for r in requests if isinstance(r, dict)), key=_at):
        path, line = req.get("path"), _line(req.get("line"))
        selector = f"{path}:{req.get('line')}"
        why = (req.get("why") or "").strip()
        when = parse_iso(req.get("at"))
        found = []
        if why and when is not None and line is not None:
            for at, findings in timed:
                if at < when:
                    found = [f for f in findings if f.get("path") == path and _line(f.get("line")) == line]
                    if found:
                        break
        distinct = {tuple(finding_key(f)) for f in found}
        if len(distinct) == 1:
            key = distinct.pop()
            if key not in keys:
                keys.add(key)
                dismissed.append({"key": list(key), "path": path, "line": line, "why": req.get("why"),
                                  "at": req.get("at"), "url": req.get("url"), "source": req.get("source")})
            continue
        if not why:
            reason = "no reason given"
        elif found:
            reason = f"ambiguous: {len(found)} findings at {selector}"
        else:
            reason = f"no finding at {selector} before the comment"
        unmatched.append({"selector": selector, "reason": reason, "at": req.get("at"), "url": req.get("url"),
                          "source": req.get("source")})
    return dismissed, unmatched


def apply(findings, dismissed):
    """(kept, dropped): the findings whose finding_key a dismissal holds are dropped, each with that dismissal's
    `why` and `url` added; the rest kept, both in the order given."""
    by_key = {}
    for d in dismissed:
        key = d.get("key") if isinstance(d, dict) else None
        if isinstance(key, (list, tuple)) and len(key) == 2 and all(isinstance(k, str) for k in key):
            by_key.setdefault(tuple(key), d)
    kept, dropped = [], []
    for f in findings:
        d = by_key.get(tuple(finding_key(f))) if isinstance(f, dict) else None
        if d is None:
            kept.append(f)
        else:
            dropped.append(dict(f, why=d.get("why"), url=d.get("url")))
    return kept, dropped


def _finding(path, line, text):
    """A finding from `[severity] body` as harness-pr writes it; a missing or unknown severity reads as high."""
    m = SEVERITY.fullmatch(text)
    severity = m.group(1).lower() if m else ""
    return {"path": path, "line": line, "severity": severity if severity in SEVERITIES else "high",
            "body": (m.group(2) if m else text).strip()}


def review_findings(review, comments):
    """The findings of a harness review as harness-pr posted them: the ``- `path:line` [sev] body`` lines under the
    body's `Not on a changed line:` (their two-space continuation lines restored) and the inline `comments` (its
    `.../reviews/<id>/comments`, `[sev] body` and the trailer post_review appends, which alone is stripped; where
    it was posted, original_line, else line). [] for a review that found nothing."""
    body = (review or {}).get("body") or ""
    if body.startswith(NO_FINDINGS):
        return []
    found, current, listing = [], None, False
    for line in body.splitlines():
        if not listing:
            listing = line.strip() == LISTED
            continue
        m = ENTRY.fullmatch(line)
        if m:
            place, path, line_no = m.group(2), "", None
            if place is not None:
                path, _, tail = place.rpartition(":")
                if path and DIGITS.fullmatch(tail):
                    line_no = int(tail)
                else:
                    path = place
            current = [path, line_no, [m.group(3)]]
            found.append(current)
        elif current is not None and line.startswith(CONTINUED):
            current[2].append(line[len(CONTINUED):])
        else:
            break
    out = [_finding(path, line_no, "\n".join(text)) for path, line_no, text in found]
    for c in comments or []:
        if isinstance(c, dict):
            text = c.get("body") or ""
            if text.endswith(INLINE_TRAILER):
                text = text[:-len(INLINE_TRAILER)]
            posted = _line(c.get("original_line"))
            out.append(_finding(c.get("path") or "", posted if posted is not None else _line(c.get("line")),
                                text.strip()))
    return out


def _list(rest, path):
    """Every row of a REST list under repos/{owner}/{repo}/."""
    args = ["api", f"{REPO}/{path}?per_page={PAGE}", "--paginate", "--slurp"]
    return rows(rest.json(args, args))


def _login(cwd):
    """The authenticated account's login casefolded, read as owner_comments.fetch reads it. Problem on a failed read
    or an empty login."""
    login = gh(["api", "user", "--jq", ".login"], cwd, pick=first_line).strip()
    if not login:
        raise Problem("gh api user printed no login")
    return login.casefold()


def _harness_review(r, login):
    """Is `r` a harness review: by `login` (casefolded), its body ending with TRAILER, its submitted_at parsing?"""
    if not isinstance(r, dict):
        return False
    user = r.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    body = r.get("body")
    return (isinstance(author, str) and author.casefold() == login and isinstance(body, str)
            and TRAILER.search(body) is not None and parse_iso(r.get("submitted_at")) is not None)


def github_snapshots(cwd, pr, reviews=None):
    """One {"at", "findings"} per harness review on PR `pr`, oldest first by submitted_at: by the authenticated
    account, its body ending with the marker (then the GPT receipt or not), any other review left out: `reviews`
    when given, else the PR's, each one's findings read with its comments. Problem on a failed read, the login's
    included."""
    login = _login(cwd)
    rest = Rest(cwd, pick=first_line)
    if reviews is None:
        reviews = _list(rest, f"pulls/{pr}/reviews")
    marked = [r for r in reviews if _harness_review(r, login)]
    marked.sort(key=lambda r: parse_iso(r.get("submitted_at")))
    out = []
    for r in marked:
        comments = []
        if not (r.get("body") or "").startswith(NO_FINDINGS):
            rid = r.get("id")
            if not isinstance(rid, int) or isinstance(rid, bool):
                raise Problem(f"PR #{pr}: a harness review without an id")
            comments = _list(rest, f"pulls/{pr}/reviews/{rid}/comments")
        out.append({"at": r.get("submitted_at"), "findings": review_findings(r, comments)})
    return out


def read(cwd, tickets, pr):
    """The owner's requests on `tickets` and PR `pr`, ordered by `at`, sourced `ticket #N` or `PR #P`. Problem on a
    failed read."""
    login, comments = oc.fetch(cwd, list(tickets) + [pr])
    requests = []
    for n in tickets:
        requests += parse(comments[int(n)], login, f"ticket #{n}")
    requests += parse(comments[int(pr)], login, f"PR #{pr}")
    return sorted(requests, key=_at)
