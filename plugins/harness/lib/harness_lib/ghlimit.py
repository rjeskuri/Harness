"""The gh call log: one JSON line per gh call a harness tool or hook makes, in
~/.claude/harness/gh/calls-<UTC date>.jsonl, so calls per tool, command and ticket can be counted.
A line: at, tool, cmd (call_name), kind (call_kind), repo, ticket, exit (null: timed out or stopped), ms,
waited_ms and refused. Logging never fails a call: a log that cannot be written is skipped.

It also holds the machine-wide pause, ~/.claude/harness/gh/pause.json: a call GitHub refuses for a rate limit
(refused) records one (record_refusal), and every harness process waits for it before its next gh call
(wait_for_clear), at most HARNESS_GH_MAX_WAIT seconds per process in all; a longer pause is GhPaused, exit 75.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import re
import sys
import time

from harness_lib.cli import Problem
from harness_lib.text import first_line, harness_home, now_iso, parse_iso, read_json, write_json

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


# --------------------------------------------------------------- the pause
REFUSAL = re.compile(r"(?i)rate limit|abuse detection|submitted too quickly|\bHTTP 429\b|too many requests")
KINDS = ("primary", "secondary", "manual")
PRIMARY_SLACK = 5          # seconds after the reset GitHub gives
WAIT_STEP = 5              # the longest sleep before pause.json is read again
REASON_CHARS = 200


def refused(text):
    """Whether gh's output (its stderr) says GitHub refused the call for a rate limit."""
    return bool(text) and bool(REFUSAL.search(text))


def _seconds(name, default, minimum=0.0):
    """The environment variable `name` as seconds (a float at or above `minimum`), else `default`."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= minimum else default


def max_wait():
    """HARNESS_GH_MAX_WAIT: the seconds one process may wait for pauses in all (default 90; 0 allowed)."""
    return _seconds("HARNESS_GH_MAX_WAIT", 90.0)


def backoff():
    """HARNESS_GH_BACKOFF: the first secondary pause in seconds (default 60; more than 0)."""
    return _seconds("HARNESS_GH_BACKOFF", 60.0, minimum=1e-9)


def backoff_max():
    """HARNESS_GH_BACKOFF_MAX: the longest secondary pause in seconds (default 900; more than 0)."""
    return _seconds("HARNESS_GH_BACKOFF_MAX", 900.0, minimum=1e-9)


def pause_path():
    return os.path.join(gh_dir(), "pause.json")


def iso_at(seconds):
    """Epoch seconds as 2026-10-22T14:05:00Z, with milliseconds (…:00.250Z) when not a whole second."""
    when = dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
    if when.microsecond == 0:
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def pause_info():
    """The recorded pause, {until, kind, reason, strikes, at} plus `until_s` (epoch seconds), read without the
    lock; None when pause.json is missing, unreadable or corrupt. A pause that already ended is still returned."""
    data = read_json(pause_path())
    if not isinstance(data, dict):
        return None
    until = parse_iso(data.get("until"))
    if until is None:
        return None
    strikes = data.get("strikes")
    return {"until": data["until"], "kind": data.get("kind") if data.get("kind") in KINDS else "secondary",
            "reason": str(data.get("reason") or ""), "at": data.get("at"),
            "strikes": strikes if isinstance(strikes, int) and not isinstance(strikes, bool) and strikes > 0 else 0,
            "until_s": until.timestamp()}


def _exhausted_reset(limits):
    """The latest reset (epoch seconds) of the graphql or core bucket with remaining 0, else None."""
    resets = []
    for bucket in ("graphql", "core"):
        part = (limits or {}).get(bucket) if isinstance(limits, dict) else None
        if isinstance(part, dict) and part.get("remaining") == 0 and isinstance(part.get("reset"), int):
            resets.append(part["reset"])
    return max(resets) if resets else None


def _next_pause(previous, reason, limits, now):
    """The pause a refusal at `now` records over `previous` (pause_info() or None); never shorter than it."""
    if previous is not None and previous["until_s"] > now:
        strikes = max(previous["strikes"], 1)          # a call that started before this pause: no further strike
    elif previous is not None and now - previous["until_s"] < backoff_max():
        strikes = previous["strikes"] + 1
    else:
        strikes = 1
    reset = _exhausted_reset(limits)
    if reset is not None and reset + PRIMARY_SLACK > now:
        kind, until = "primary", float(reset + PRIMARY_SLACK)
    else:
        kind, until = "secondary", now + min(backoff() * 2 ** (strikes - 1), backoff_max())
    if previous is not None and previous["until_s"] >= until:
        return dict(previous, strikes=strikes)
    return {"until": iso_at(until), "kind": kind, "reason": first_line(reason)[:REASON_CHARS], "strikes": strikes,
            "at": now_iso(), "until_s": until}


def record_refusal(reason, limits=None):
    """Record the pause for a refusal whose stderr is `reason` (its first line, at most 200 characters, is kept),
    given rate_limits()' `limits` (None: not probed), in pause.json while holding pause.lock; return the pause.
    When it cannot be written, the pause returned is HARNESS_GH_BACKOFF seconds from now, kept by the caller only."""
    try:
        os.makedirs(gh_dir(), exist_ok=True)
        fd = os.open(os.path.join(gh_dir(), "pause.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            pause = _next_pause(pause_info(), reason, limits, time.time())
            write_json(pause_path(), {k: v for k, v in pause.items() if k != "until_s"}, prefix=".pause-")
            return pause
        finally:
            os.close(fd)
    except OSError:
        until = time.time() + backoff()
        return {"until": iso_at(until), "kind": "secondary", "reason": first_line(reason)[:REASON_CHARS],
                "strikes": 1, "at": now_iso(), "until_s": until}


class GhPaused(Problem):
    """gh calls are paused past what this process may still wait: one line, exit 75 (EX_TEMPFAIL)."""

    exit_code = 75

    def __init__(self, until, kind, reason):
        self.until, self.kind, self.reason = until, kind, reason
        super().__init__(f"GitHub rate limit: gh calls are paused until {until} ({kind}: {reason}); wait until then "
                         "(harness-work budget wait) and run this again")


class WaitBudget:
    """The seconds a process has waited for pauses; left() is HARNESS_GH_MAX_WAIT, read at call time, less that."""

    def __init__(self):
        self.spent = 0.0

    def left(self):
        return max(0.0, max_wait() - self.spent)


BUDGET = WaitBudget()


def wait_for_clear(budget=None, at_least=None):
    """Wait while a pause is ahead: the recorded one, re-read before each sleep of at most WAIT_STEP seconds, or
    the pause `at_least` (a pause dict that could not be written). The seconds waited, charged to `budget`
    (None: this process's BUDGET). GhPaused, without waiting, once the pause ends past the budget left."""
    budget = BUDGET if budget is None else budget
    waited = 0.0
    while True:
        pause = pause_info()
        if at_least is not None and (pause is None or pause["until_s"] < at_least["until_s"]):
            pause = at_least
        now = time.time()
        ahead = pause["until_s"] - now if pause is not None else 0
        if ahead <= 0:
            return waited
        if ahead > budget.left():
            raise GhPaused(pause["until"], pause["kind"], pause["reason"])
        step = min(WAIT_STEP, ahead, budget.left())
        started = time.monotonic()
        time.sleep(step)
        slept = time.monotonic() - started
        budget.spent += slept
        waited += slept
