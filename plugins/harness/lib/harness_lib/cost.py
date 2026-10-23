"""What one ticket cost in tokens and model time, from a repository's Claude Code transcripts
(read by transcripts.py): rows per stage, round, agent and model, their totals and per-model
sums, Claude Code's own totals for the ticket's own sessions, and the sessions that
contributed. Tokens and time only: no dollar figure. See docs/design.md (Layout).

Attribution: a session whose main transcript is on t/N-... (HEAD aside) is N's own and every
message in it counts for N, except a subagent whose description names another ticket. Elsewhere
an orchestrator message counts when its own branch is t/N-..., and a subagent when its
description starts `#N`, or names no ticket and its first branch is t/N-.... One message id
found in several files counts once.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import re

from harness_lib import transcripts
from harness_lib.text import parse_iso

# stage order of the rows; the first five are the words of the `#N <stage>` convention
STAGES = ("tests", "breaker", "code", "review", "fix", "other", "orchestrator")
STAGE_WORDS = STAGES[:5]
# agent type (the part after its last `:`) -> stage, when the description gives no stage word
TYPE_STAGES = {"test-author": "tests", "test-breaker": "breaker", "coder": "code", "reviewer": "review"}
TOKENS = ("input", "output", "cache_write", "cache_read")
TOTAL_KEYS = TOKENS + ("total", "messages", "model_ms")
SESSION_TOKENS = ("input", "output", "thinking", "cache_read", "cache_write")

DESCRIPTION = re.compile(r"#(\d+)\b\W*(\w*)")
BRANCH = re.compile(r"t/(\d+)-")


def _millis(start, end):
    """Whole milliseconds from start to end, never negative; 0 when either is unknown."""
    if start is None or end is None:
        return 0
    d = end - start
    return max(0, (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000)


def _description(meta):
    """(ticket, word) from a meta description `#N <word>` (leading spaces ignored); (None, "")
    when it names no ticket."""
    text = meta.get("description")
    found = DESCRIPTION.match(text.lstrip()) if isinstance(text, str) else None
    return (int(found.group(1)), found.group(2)) if found else (None, "")


def _branch_ticket(branch):
    found = BRANCH.match(branch) if isinstance(branch, str) else None
    return int(found.group(1)) if found else None


def _mentions(path, needle):
    """Whether the file's bytes contain `needle`; False when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return needle in fh.read()
    except OSError:
        return False


def _message(m):
    """A transcript message with its times parsed: {"id", "model", "segment", "start", "end",
    "usage"}."""
    return {"id": m["id"], "model": m["model"] or "unknown", "segment": m["segment"],
            "start": parse_iso(m["start"]), "end": parse_iso(m["end"]), "usage": dict(m["usage"])}


def _merge(kept, other):
    """One message id seen again: per usage field the largest, the earliest start, the latest end."""
    for name in TOKENS:
        kept["usage"][name] = max(kept["usage"][name], other["usage"][name])
    starts = [t for t in (kept["start"], other["start"]) if t is not None]
    ends = [t for t in (kept["end"], other["end"]) if t is not None]
    kept["start"] = min(starts) if starts else None
    kept["end"] = max(ends) if ends else None


def _attribute(number, found):
    """The ticket, stage and round decision, in one place: which of `found`'s sessions (from
    transcripts.sessions) are N's own, which orchestrator messages and subagents count for N,
    each one's stage and round. A continued subagent is split at the coordinator's entries into
    segments and each segment is a round of its own (see fix and rounds below). Returns
    (entries, sessions): entries [(stage, round, agent, message)] with each message id once, and
    {session id: {"own", "cost_state", "cost"}} for each session that is N's own or holds a
    message counted for N (cost: an own session's cost-state, else None).

    Speed: a main transcript whose text has no `t/N-` is not parsed (it can be neither N's own
    nor hold N's orchestrator messages), and a subagent is parsed only when it counts for N."""
    pattern = re.compile(rf"t/{number}-")
    units = []         # [session id, key, stage, agent, messages, named]; key None: orchestrator
    info = {}
    for s in found:
        own, cost, orchestrator, before = False, None, [], len(units)
        if _mentions(s["path"], f"t/{number}-".encode()):
            main = transcripts.read_transcript(s["path"])
            branches = [b for b in main["branches"] if b != "HEAD"]
            own = bool(branches) and all(pattern.match(b) for b in branches)
            cost = main["cost_state"]
            orchestrator = [_message(m) for m in main["messages"]
                            if own or (m["branch"] is not None and pattern.match(m["branch"]))]
        if orchestrator:
            units.append([s["id"], None, "orchestrator", "main", orchestrator, False])
        for a in s["agents"]:
            ticket, word = _description(a["meta"])
            if ticket is None:
                ticket = number if own else _branch_ticket(transcripts.first_branch(a["path"]))
            if ticket != number:
                continue
            messages = [_message(m) for m in transcripts.read_transcript(a["path"])["messages"]]
            if not messages:
                continue
            agent = a["meta"].get("agentType")
            agent = agent if isinstance(agent, str) and agent else "unknown"
            stage = word if word in STAGE_WORDS else TYPE_STAGES.get(agent.rsplit(":", 1)[-1], "other")
            units.append([s["id"], a["path"], stage, agent, messages, word in STAGE_WORDS])
        if own or len(units) > before:
            known = info.setdefault(s["id"], {"own": False, "cost_state": None, "cost": None})
            if own and not known["own"]:      # a session id in two folders is listed once
                known.update(own=True, cost_state=cost is not None, cost=cost)

    # one message id once: the first unit to hold it keeps it, merged with every later copy
    seen = {}
    for unit in units:
        kept = []
        for m in unit[4]:
            if m["id"] in seen:
                _merge(seen[m["id"]], m)
            else:
                seen[m["id"]] = m
                kept.append(m)
        unit[4] = kept
    units = [u for u in units if u[4]]

    # segments: a subagent's messages split at the coordinator's entries, one part each:
    # [session id, key, stage, agent, messages, named, segment number, agent's first segment]
    parts = []
    for u in units:
        if u[1] is None:
            parts.append(u + [0, True])
            continue
        by_segment = {}
        for m in u[4]:
            by_segment.setdefault(m["segment"], []).append(m)
        for seg in sorted(by_segment):
            parts.append(u[:4] + [by_segment[seg], u[5], seg, seg == 1])

    def first_start(part):
        starts = [m["start"] for m in part[4] if m["start"] is not None]
        return (min(starts).timestamp() if starts else float("inf"), part[0], part[1] or "", part[6])

    # fix: the ticket's first review is its earliest-starting review segment, in any session.
    # Every code or review segment starting strictly after that review's last end is a fix
    # round, except the first segment of an agent whose description names its stage. Tests,
    # breaker and other segments are never fix, and without a review nothing is.
    reviews = sorted((p for p in parts if p[2] == "review"), key=first_start)
    ends = [m["end"] for m in reviews[0][4] if m["end"] is not None] if reviews else []
    for part in parts:
        starts = [m["start"] for m in part[4] if m["start"] is not None]
        if (part[2] in ("code", "review") and starts and ends and min(starts) > max(ends)
                and not (part[5] and part[7])):
            part[2] = "fix"

    # rounds: one per segment, numbered within its stage by first start (fix: one sequence across
    # coder and reviewer); the orchestrator's is 1
    rounds = {}
    for stage in STAGES:
        segments = sorted((p for p in parts if p[2] == stage and p[1] is not None), key=first_start)
        for n, p in enumerate(segments, 1):
            rounds[(p[1], p[6])] = n
    entries = [(p[2], rounds.get((p[1], p[6]), 1), p[3], m) for p in parts for m in p[4]]
    return entries, info


def _zero():
    return {key: 0 for key in TOTAL_KEYS}


def ticket_usage(main: str, number: int, dirs: list[str] | None = None) -> dict:
    """{"rows", "totals", "by_model", "session_totals", "sessions"} for ticket `number` from the
    transcripts in `dirs` (default: transcripts.session_dirs(main)).

    A row per stage, round, agent and model: input, output, cache_write, cache_read, total (their
    sum), messages, model_ms (each message's end minus start, summed) and wall_ms (the row's
    latest end minus its earliest start; None on orchestrator rows, whose agent is `main`),
    sorted by stage (STAGES), round, agent, model. `totals` and `by_model` sum the rows.
    `session_totals` sums the cost-state of N's own sessions that have one ({"models", "api_ms",
    "sessions"}), None when none has. `sessions` lists each contributing session once as
    {"id", "own", "cost_state"} (cost_state None on sessions that are not N's own)."""
    found = transcripts.sessions(transcripts.session_dirs(main) if dirs is None else dirs)
    entries, info = _attribute(number, found)

    groups = {}
    for stage, rnd, agent, m in entries:
        groups.setdefault((stage, rnd, agent, m["model"]), []).append(m)
    rows = []
    for (stage, rnd, agent, model), msgs in groups.items():
        x = {"stage": stage, "round": rnd, "agent": agent, "model": model}
        for name in TOKENS:
            x[name] = sum(m["usage"][name] for m in msgs)
        x["total"] = sum(x[name] for name in TOKENS)
        x["messages"] = len(msgs)
        x["model_ms"] = sum(_millis(m["start"], m["end"]) for m in msgs)
        x["wall_ms"] = None
        if stage != "orchestrator":
            starts = [m["start"] for m in msgs if m["start"] is not None]
            ends = [m["end"] for m in msgs if m["end"] is not None]
            x["wall_ms"] = _millis(min(starts), max(ends)) if starts and ends else 0
        rows.append(x)
    rows.sort(key=lambda x: (STAGES.index(x["stage"]), x["round"], x["agent"], x["model"]))

    totals, by_model = _zero(), {}
    for x in rows:
        per = by_model.setdefault(x["model"], _zero())
        for key in TOTAL_KEYS:
            totals[key] += x[key]
            per[key] += x[key]

    session_totals = None
    for known in info.values():
        cost = known["cost"]
        if cost is None:
            continue
        session_totals = session_totals or {"models": {}, "api_ms": 0, "sessions": 0}
        session_totals["api_ms"] += cost["api_ms"]
        session_totals["sessions"] += 1
        for model, counts in cost["models"].items():
            per = session_totals["models"].setdefault(model, {key: 0 for key in SESSION_TOKENS})
            for key in SESSION_TOKENS:
                per[key] += counts.get(key, 0)

    sessions = [{"id": sid, "own": known["own"], "cost_state": known["cost_state"]}
                for sid, known in info.items()]
    return {"rows": rows, "totals": totals, "by_model": by_model,
            "session_totals": session_totals, "sessions": sessions}
