"""The gh call log: one JSON line per gh call a harness tool or hook makes, in
~/.claude/harness/gh/calls-<UTC date>.jsonl, so calls per tool, command and ticket can be counted.
A line: at, tool, cmd (call_name), kind (call_kind), repo, ticket, exit (null: timed out or stopped), ms,
waited_ms and refused. Logging never fails a call: a log that cannot be written is skipped.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time

from harness_lib.text import harness_home, now_iso, parse_iso

KEEP_DAYS = 14
REST_COMMANDS = ("run", "label", "release", "workflow")
DAY_FILE = re.compile(r"^calls-(\d{4}-\d\d-\d\d)\.jsonl$")
TICKET_DIR = re.compile(r"/\.claude/worktrees/t-(\d+)-")
WORKTREES = "/.claude/worktrees/"
HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")


def gh_dir():
    """~/.claude/harness/gh, read at call time (HOME may change)."""
    return harness_home("gh")


def _api_parts(args):
    """(method or None, path) of a `gh api` call: the path is the first argument that is graphql or holds "/"
    and is not a flag, else the first that is not a flag; -X/--method and its value are not candidates."""
    method, candidates = None, []
    rest = list(args[1:])
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-X", "--method") and i + 1 < len(rest):
            method = rest[i + 1]
            i += 2
            continue
        if not arg.startswith("-"):
            candidates.append(arg)
        i += 1
    path = next((a for a in candidates if a == "graphql" or "/" in a), candidates[0] if candidates else "")
    return method, path


def call_kind(args):
    """`rest` for gh api on any path but graphql and for run, label, release and workflow; else `graphql`."""
    if not args:
        return "graphql"
    if args[0] == "api":
        return "graphql" if _api_parts(args)[1] == "graphql" else "rest"
    return "rest" if args[0] in REST_COMMANDS else "graphql"


def call_name(args):
    """`issue view`, `api repos/:owner/:repo/issues/:n/sub_issues`, `api POST graphql`: the call without its
    numbers, shas, owner, repository and query string."""
    if not args or args[0] != "api":
        return " ".join(args[:2])
    method, path = _api_parts(args)
    parts = path.split("?", 1)[0].split("/")
    if len(parts) >= 4 and parts[0] == "repos":
        parts[1:3] = [":owner", ":repo"]
    parts = [":n" if p.isdigit() else ":sha" if HEX.match(p) else p for p in parts]
    words = ["api"] + ([method] if method and method.upper() != "GET" else []) + ["/".join(parts)]
    return " ".join(words)


def ticket_of(cwd):
    """N of the ticket worktree .claude/worktrees/t-N-... in PWD, else in `cwd` (None: the current directory);
    else None."""
    for where in (os.environ.get("PWD") or "", cwd if cwd is not None else os.getcwd()):
        m = TICKET_DIR.search(str(where))
        if m:
            return int(m.group(1))
    return None


def repo_of(cwd):
    """The folder name of the main checkout: the part of `cwd` (None: the current directory) before
    /.claude/worktrees/ when there is one, else cwd's own name."""
    path = str(cwd if cwd is not None else os.getcwd())
    if WORKTREES in path:
        path = path.split(WORKTREES, 1)[0]
    return os.path.basename(path.rstrip("/"))


def log_call(args, *, cwd, exit, ms, waited_ms=0, refused=False):
    """Append the call's line to today's file; the first line of a day drops the files over KEEP_DAYS days
    old. Any OSError is skipped silently."""
    try:
        at = now_iso()
        line = {"at": at, "tool": os.path.basename(sys.argv[0]) if sys.argv else "", "cmd": call_name(args),
                "kind": call_kind(args), "repo": repo_of(cwd), "ticket": ticket_of(cwd), "exit": exit,
                "ms": int(ms), "waited_ms": int(waited_ms), "refused": bool(refused)}
        folder = gh_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"calls-{at[:10]}.jsonl")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        try:
            fd, created = os.open(path, flags | os.O_EXCL, 0o644), True
        except FileExistsError:
            fd, created = os.open(path, flags, 0o644), False
        try:
            os.write(fd, (json.dumps(line) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        if created:
            _prune(folder, dt.date.fromisoformat(at[:10]))
    except OSError:
        pass


def ms_since(started):
    """Whole milliseconds since time.monotonic() gave `started`."""
    return int((time.monotonic() - started) * 1000)


def _prune(folder, today):
    """Delete the day files dated more than KEEP_DAYS days before `today`."""
    oldest = today - dt.timedelta(days=KEEP_DAYS)
    for name in os.listdir(folder):
        m = DAY_FILE.match(name)
        if not m:
            continue
        try:
            if dt.date.fromisoformat(m.group(1)) < oldest:
                os.unlink(os.path.join(folder, name))
        except (OSError, ValueError):
            pass


def read_calls(since):
    """Every logged call (a dict) with `at` at or after `since` (an aware datetime), oldest file first; lines
    that are not a JSON object with a readable `at` are skipped."""
    since = since.astimezone(dt.timezone.utc)
    folder = gh_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        m = DAY_FILE.match(name)
        if not m or m.group(1) < since.date().isoformat():
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
        except OSError:
            continue
        for raw in raw_lines:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            at = parse_iso(line.get("at")) if isinstance(line, dict) else None
            if at is not None and at >= since:
                out.append(line)
    return out
