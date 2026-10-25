"""What one ticket cost in tokens and model time, from a repository's Claude Code transcripts
(read by transcripts.py): rows per stage, round, agent and model, their totals and per-model
sums, Claude Code's own totals for the ticket's own sessions, and the sessions that
contributed. Tokens and time only: no dollar figure. See docs/design.md (Layout).

Attribution: a session whose main transcript is on t/N-... (HEAD aside) is N's own and every
message in it counts for N, except a subagent whose description names another ticket. Elsewhere
an orchestrator message counts when its own branch is t/N-..., and a subagent when its
description starts `#N`, or names no ticket and its first branch is t/N-.... One message id
found in several files counts once.

The record `harness-work cost N` saves (build_record, with the attempt store replayed by rounds) and its
text and markdown renderings live here too, with the number formats, so every caller shows the same.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import re
import statistics

from harness_lib import transcripts
from harness_lib.cli import table
from harness_lib.settings import glob_match
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


# --------------------------------------------------------------- the record and its renderings
# The record's coverage note, printed under the table: what the figures leave out.
COVERAGE = ("A lower bound: only the transcripts on this Mac are read, and the orchestrator turns of shared "
            "sessions are left out")
HEADER = "**Cost** (tokens and model time from the transcripts, a lower bound)"
TABLE_HEAD = ("Stage", "Model", "Rounds", "Tokens", "Output", "Model time", "Agent time")
ROUND_KEYS = {"retry": "retry", "escalate": "escalate", "needs-input": "needs_input"}


def attempt_action(stage, result, failures):
    """What `harness-work attempt` answers for a stage's attempt, `failures` being that stage's run of
    consecutive fails ending with it: done on a pass, then retry, escalate, needs-input (fix: retry, then
    needs-input; it never escalates). The tier of an escalation is the caller's."""
    if result == "pass":
        return "done"
    if failures == 1:
        return "retry"
    if stage != "fix" and failures == 2:
        return "escalate"
    return "needs-input"


def rounds(attempts):
    """The attempt store replayed: {"stages": {stage: {"runs", "fails", "retry", "escalate", "needs_input"}},
    "breaker_rerounds", "fix_rounds"}, each stage counted on its own as `harness-work attempt` answered;
    stages never attempted are absent. Breaker re-rounds are breaker fails, fix rounds fix attempts."""
    stages, run = {}, {}
    for entry in attempts or []:
        stage = entry.get("stage") if isinstance(entry, dict) else None
        if not isinstance(stage, str):
            continue
        counts = stages.setdefault(stage, {"runs": 0, "fails": 0, "retry": 0, "escalate": 0, "needs_input": 0})
        counts["runs"] += 1
        failed = entry.get("result") == "fail"
        counts["fails"] += failed
        run[stage] = run.get(stage, 0) + 1 if failed else 0
        key = ROUND_KEYS.get(attempt_action(stage, entry.get("result"), run[stage]))
        if key:
            counts[key] += 1
    return {"stages": stages, "breaker_rerounds": stages.get("breaker", {}).get("fails", 0),
            "fix_rounds": stages.get("fix", {}).get("runs", 0)}


def build_record(number: int, main: str, usage: dict, cache: dict | None, pr: int | None,
                 computed_at: str) -> dict:
    """The cost/<N>.json record: ticket_usage's `usage`, the ticket cache's title, labels, tier and rounds
    (all None without a cache), `pr` and the coverage note. needs_input, lines, elapsed, ci and landed_at
    stay None here."""
    cache = cache if isinstance(cache, dict) else None
    field = (lambda key: cache.get(key)) if cache else (lambda key: None)
    attempts = cache.get("attempts") if cache and isinstance(cache.get("attempts"), list) else []
    return {"ticket": number, "title": field("title"), "repo": main, "computed_at": computed_at,
            "type": field("type"), "size": field("size"), "risk": field("risk"), "tier": field("tier"), "pr": pr,
            "rows": usage["rows"], "totals": usage["totals"], "by_model": usage["by_model"],
            "session_totals": usage["session_totals"], "sessions": usage["sessions"],
            "rounds": rounds(attempts) if cache else None,
            "needs_input": None, "lines": None, "elapsed": None, "ci": None, "landed_at": None,
            "coverage": COVERAGE}


# --------------------------------------------------------------- the gh-backed parts (pure; harness-work asks gh)
LABEL_TYPES = ("epic", "bug", "refactor", "chore", "task")   # harness-board's order: the first present wins
FAILED_RUNS = ("failure", "timed_out", "startup_failure")
NEEDS_INPUT = "**Needs your input**"
STARTED = "Started on "


def pick_pr(prs):
    """The branch's pull request from `gh pr list`: the latest merged one, else the newest; None for none."""
    prs = [p for p in prs or [] if isinstance(p, dict) and isinstance(p.get("number"), int)]
    merged = [p for p in prs if parse_iso(p.get("mergedAt"))]
    pool = merged or prs
    if not pool:
        return None
    key = "mergedAt" if merged else "createdAt"
    epoch = parse_iso("1970-01-01T00:00:00Z")
    return max(pool, key=lambda p: (parse_iso(p.get(key)) or epoch, p["number"]))


def file_kind(path, test_globs, exempt_globs):
    """test when the path matches a test pattern, else other when it matches an exempt one, else source."""
    if any(glob_match(g, path) for g in test_globs):
        return "test"
    if any(glob_match(g, path) for g in exempt_globs):
        return "other"
    return "source"


def line_counts(pages, test_globs, exempt_globs):
    """{"test", "source", "other": {"added", "removed"}} summed over the files endpoint's pages (--slurp: an array
    of the page arrays)."""
    out = {kind: {"added": 0, "removed": 0} for kind in ("test", "source", "other")}
    for page in pages if isinstance(pages, list) else []:
        for f in page if isinstance(page, list) else [page]:
            name = f.get("filename") if isinstance(f, dict) else None
            if not isinstance(name, str) or not name:
                continue
            part = out[file_kind(name, test_globs, exempt_globs)]
            for key, field in (("added", "additions"), ("removed", "deletions")):
                value = f.get(field)
                part[key] += value if isinstance(value, int) and not isinstance(value, bool) else 0
    return out


def _body(comment):
    body = comment.get("body") if isinstance(comment, dict) else None
    return body.lstrip() if isinstance(body, str) else ""


def _earliest(comments, test):
    """The earliest createdAt, as given, of the comments whose body (leading space aside) passes `test`; or None."""
    times = [c.get("createdAt") for c in comments or [] if test(_body(c)) and parse_iso(c.get("createdAt"))]
    return min(times, key=parse_iso) if times else None


def needs_input_count(comments):
    """The ticket's needs-input questions: the comments starting `**Needs your input**`."""
    return sum(1 for c in comments or [] if _body(c).startswith(NEEDS_INPUT))


def started_from_comments(comments):
    """The earliest `Started on ` comment's time, or None."""
    return _earliest(comments, lambda body: body.startswith(STARTED))


def ready_from_comments(comments, pr):
    """The earliest `PR #<pr> ready for review` comment's time, or None."""
    mark = f"PR #{pr} ready for review"
    return _earliest(comments, lambda body: body.startswith(mark))


def label_fields(names):
    """type, size and risk from issue label names (harness-board's reading), each None when absent."""
    present = set(names)
    size = next((s for s in ("L", "M", "S") if "size:" + s in present), None)
    risk = "high" if "risk:high" in present else "low" if "risk:low" in present else None
    return {"type": next((t for t in LABEL_TYPES if t in present), None), "size": size, "risk": risk}


def _seconds(start, end):
    a, b = parse_iso(start), parse_iso(end)
    return int((b - a).total_seconds()) if a and b else None


def elapsed(started_at, pr_opened_at, ready_at, landed_at):
    """{"started_at", "pr_opened_at", "ready_at", "landed_at", "start_to_ready_s", "start_to_land_s"}, a span null
    when either end is missing; None when none of them is known."""
    part = {"started_at": started_at, "pr_opened_at": pr_opened_at, "ready_at": ready_at, "landed_at": landed_at,
            "start_to_ready_s": _seconds(started_at, ready_at), "start_to_land_s": _seconds(started_at, landed_at)}
    return part if any(v is not None for v in part.values()) else None


def ci_summary(runs):
    """{"runs", "failed", "cancelled", "total_s", "median_s"} over the completed runs of `gh run list`: a run lasts
    updatedAt minus startedAt (createdAt without one); median_s in whole seconds, None without a run."""
    done = [r for r in runs or [] if isinstance(r, dict) and r.get("status") == "completed"]
    spans = [max(0, _seconds(r.get("startedAt") or r.get("createdAt"), r.get("updatedAt")) or 0) for r in done]
    return {"runs": len(done), "failed": sum(1 for r in done if r.get("conclusion") in FAILED_RUNS),
            "cancelled": sum(1 for r in done if r.get("conclusion") == "cancelled"), "total_s": sum(spans),
            "median_s": int(statistics.median(spans)) if spans else None}


def tokens_text(count):
    """999, 12.3k, 12.3M."""
    if count < 1000:
        return str(count)
    if round(count / 1000, 1) < 1000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1000000:.1f}M"


def duration_text(millis):
    """45s, 12m, 1h05m (whole units, rounded down)."""
    seconds = max(0, int(millis)) // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def model_text(name):
    """A model name without its leading `claude-`."""
    return name[len("claude-"):] if name.startswith("claude-") else name


def stage_rows(rows):
    """The rows aggregated by stage and model, in the rows' order: (stage, model, rounds, tokens, output,
    model time, agent time) as text; agent time is empty where no row has one (the orchestrator)."""
    groups = {}
    for x in rows:
        g = groups.setdefault((x["stage"], x["model"]), {"rounds": set(), "total": 0, "output": 0, "model_ms": 0,
                                                         "wall_ms": None})
        g["rounds"].add(x["round"])
        for key in ("total", "output", "model_ms"):
            g[key] += x[key]
        if x["wall_ms"] is not None:
            g["wall_ms"] = (g["wall_ms"] or 0) + x["wall_ms"]
    return [(stage, model_text(model), str(len(g["rounds"])), tokens_text(g["total"]), tokens_text(g["output"]),
             duration_text(g["model_ms"]), "" if g["wall_ms"] is None else duration_text(g["wall_ms"]))
            for (stage, model), g in groups.items()]


def total_row(record):
    totals = record["totals"]
    return ("total", "", "", tokens_text(totals["total"]), tokens_text(totals["output"]),
            duration_text(totals["model_ms"]), "")


def notes(record):
    """The lines under the table that have data: the session total, the rounds, the extras (lines, elapsed, CI,
    needs input), the shared sessions left out."""
    lines = []
    st = record["session_totals"]
    if st:
        models = st["models"].values()
        tokens = sum(m.get(k, 0) for m in models for k in ("input", "output", "cache_read", "cache_write"))
        lines.append(f"Session total (Claude Code's count): {tokens_text(tokens)} tokens, "
                     f"{tokens_text(sum(m.get('output', 0) for m in models))} output, "
                     f"{tokens_text(sum(m.get('thinking', 0) for m in models))} thinking, "
                     f"{duration_text(st['api_ms'])} API time")
    r = record["rounds"]
    if r and r["stages"]:
        tally = {key: sum(s[key] for s in r["stages"].values()) for key in ROUND_KEYS.values()}
        lines.append(f"Rounds: breaker re-rounds {r['breaker_rerounds']}, fix rounds {r['fix_rounds']}; "
                     f"retries {tally['retry']}, escalations {tally['escalate']}, "
                     f"needs-input {tally['needs_input']}")
    lines += extras(record)
    shared = [s["id"][:8] for s in record["sessions"] if not s["own"]]
    if shared:
        what = "the orchestrator of shared session" if len(shared) == 1 else "the orchestrators of shared sessions"
        lines.append(f"Not counted: {what} {', '.join(shared)}")
    return lines


def extras(record):
    """The Lines, Elapsed, CI and Needs input lines whose parts have data; Elapsed names only the known spans."""
    out = []
    lines = record["lines"]
    if lines:
        out.append("Lines: " + ", ".join(f"{word} +{lines[kind]['added']} -{lines[kind]['removed']}"
                                         for word, kind in (("tests", "test"), ("source", "source"),
                                                            ("other", "other"))))
    e = record["elapsed"] or {}
    spans = [f"{word} {duration_text(e[key] * 1000)}"
             for word, key in (("start to ready", "start_to_ready_s"), ("start to land", "start_to_land_s"))
             if e.get(key) is not None]
    if spans:
        out.append("Elapsed: " + ", ".join(spans))
    ci = record["ci"]
    if ci:
        pieces = [f"{ci['runs']} runs ({ci['failed']} failed, {ci['cancelled']} cancelled)",
                  f"{duration_text(ci['total_s'] * 1000)} total"]
        if ci["median_s"] is not None:
            pieces.append(f"{duration_text(ci['median_s'] * 1000)} median")
        out.append("CI: " + ", ".join(pieces))
    if record["needs_input"] is not None:
        out.append(f"Needs input: {record['needs_input']}")
    return out


def render_text(record: dict) -> str:
    """The command's text output: the ticket, the stage x model table and its total (or that no transcripts
    were found), the notes and the coverage note."""
    n = record["ticket"]
    labels = [record["type"]] + [f"{k} {record[k]}" for k in ("size", "risk", "tier") if record[k]]
    head = f"#{n}" + (f" {record['title']}" if record["title"] else "")
    if record["type"]:
        head += f" ({', '.join(labels)})"
    out = [head, ""]
    if record["rows"]:
        out.append(table([tuple(h.upper() for h in TABLE_HEAD)] + stage_rows(record["rows"]) + [total_row(record)]))
    else:
        out.append(f"No transcripts of #{n} were found.")
    out += [""] + notes(record) + [record["coverage"]]
    return "\n".join(out)


def render_markdown(record: dict) -> str:
    """The Cost block of a ticket's Resolution: the stage x model table, its total and the notes with data."""
    def line(cells):
        return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"

    out = [HEADER, ""]
    if record["rows"]:
        out += [line(TABLE_HEAD), "|---|---|---:|---:|---:|---:|---:|"]
        out += [line(r) for r in stage_rows(record["rows"])] + [line(total_row(record))]
    else:
        out.append("No transcripts of this ticket were found.")
    extra = notes(record)
    return "\n".join(out + ([""] + extra if extra else []))
