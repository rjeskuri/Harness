"""The owner's comments on a ticket (#513): which comments are the harness's own, which are the owner's answers and
which ask a needs-input question, as `harness-board answers` reads them, plus every question round of a ticket's
history for a reader that needs the whole of it.

A comment is in harness_lib.issues.Issues.comments' shape, {"author": {"login"} | None, "body", "createdAt", "url"},
and a list of them is oldest first. `login` is the authenticated user's (the owner's; the harness posts under it),
compared casefolded. A question is a timestamped comment whose body, after leading whitespace, starts
`**Needs your input**`: the owner's own, or the deploy guard's (github-actions, as REST or GraphQL names it) when it
also carries MARKER. The harness's own comments end with MARKER; older ones start with one of HARNESS_PREFIXES.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import re

from harness_lib.cli import Problem
from harness_lib.gh import gh
from harness_lib.issues import Issues, Rest
from harness_lib.text import first_line, parse_iso

NEEDS_INPUT = "**Needs your input**"
MARKER = "<!-- harness -->"     # the last line of every comment harness-board posts
BOT_LOGINS = ("github-actions[bot]", "github-actions")   # the deploy guard's author (REST, GraphQL), casefolded
# the harness's own comments from before the marker, as their bodies start
HARNESS_PREFIXES = (NEEDS_INPUT, "Started on ", "PR #", "Landed in ")
REPLY_LINE = "Reply in a comment;"   # how needs_input_body's last paragraph starts
FENCE = re.compile(r"(`{3,})")


def by_harness(body):
    """Was this comment posted by the harness? The marker, or (older comments) a body the harness writes."""
    body = body or ""
    return MARKER in body or body.lstrip().startswith(HARNESS_PREFIXES)


def author_login(c):
    """The comment's author login casefolded, or ""."""
    author = c.get("author") if isinstance(c.get("author"), dict) else {}
    return (author.get("login") or "").casefold()


def owner_comments(comments, login):
    """The owner's own comments, the harness's dropped, in the order given."""
    owner = (login or "").casefold()
    return [c for c in comments if owner and author_login(c) == owner and not by_harness(c.get("body"))]


def is_question(c, login):
    """Does this comment ask a needs-input question: timestamped, starting NEEDS_INPUT, by the owner, or by the
    deploy guard with MARKER?"""
    body = c.get("body") or ""
    if parse_iso(c.get("createdAt")) is None or not body.lstrip().startswith(NEEDS_INPUT):
        return False
    author = author_login(c)
    return bool(author) and (author == (login or "").casefold() or (author in BOT_LOGINS and MARKER in body))


def question_times(comments, login):
    """When each needs-input question was asked (datetimes), in the order given."""
    return [parse_iso(c.get("createdAt")) for c in comments if is_question(c, login)]


def replies_since(comments, login, since):
    """owner_comments created strictly after `since` (a datetime; None: no bound); one without a time only when
    unbounded."""
    if since is None:
        return owner_comments(comments, login)
    out = []
    for c in owner_comments(comments, login):
        when = parse_iso(c.get("createdAt"))
        if when is not None and when > since:
            out.append(c)
    return out


def question_text(body):
    """The question alone: needs_input_body's NEEDS_INPUT line, fenced evidence block, reply line and marker
    removed."""
    lines = [line.rstrip() for line in (body or "").strip().splitlines() if line.strip() != MARKER]
    if lines and lines[0].strip() == NEEDS_INPUT:
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].lstrip().startswith(REPLY_LINE):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    start = evidence_start(lines)
    if start is not None:
        lines = lines[:start]
    return "\n".join(lines).strip()


def evidence_start(lines):
    """The index of the fenced block's opening line when the lines end with one (the evidence), else None. A fence
    closes on a line of backticks alone at least as long as the one that opened it."""
    i, last = 0, None
    while i < len(lines):
        opened = FENCE.match(lines[i].strip())
        if not opened:
            i += 1
            continue
        width, j = len(opened.group(1)), i + 1
        while j < len(lines):
            text = lines[j].strip()
            if text and set(text) == {"`"} and len(text) >= width:
                break
            j += 1
        if j >= len(lines):
            return None                 # an unclosed fence is not an evidence block
        last = (i, j)
        i = j + 1
    return last[0] if last and last[1] == len(lines) - 1 else None


def answer_rounds(comments, login):
    """Each needs-input question, oldest first, with the owner's replies after it and before the next:
    [{"question", "asked_at", "answers": [{"at", "body"}]}]; replies before the first question are in no round."""
    owners = {id(c) for c in owner_comments(comments, login)}
    rounds = []
    for c in comments:
        if is_question(c, login):
            rounds.append({"question": question_text(c.get("body")), "asked_at": c.get("createdAt"), "answers": []})
        elif rounds and id(c) in owners:
            rounds[-1]["answers"].append({"at": c.get("createdAt"), "body": c.get("body") or ""})
    return rounds


def fetch(cwd, numbers):
    """(login, {n: comments}): `gh api user --jq .login`, then each issue's comments through REST, run in `cwd`.
    Problem on a failed read or an empty login."""
    login = gh(["api", "user", "--jq", ".login"], cwd, pick=first_line).strip()
    if not login:
        raise Problem("gh api user printed no login")
    issues = Issues(Rest(cwd, pick=first_line))
    return login, {int(n): issues.comments(int(n)) for n in numbers}
