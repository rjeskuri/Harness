"""Observed ticket milestones. Independent per-commit files survive retries and worktree removal."""

from __future__ import annotations

from pathlib import Path

from harness_lib.text import harness_home, parse_iso, read_json, write_json


def remember(key, number, event, sha, at):
    path = Path(harness_home("repos", key, "timeline", str(number))) / f"{event}-{sha}.json"
    if parse_iso(at) and not path.exists():
        write_json(str(path), {"event": event, "at": at, "id": sha}, prefix=".event-")


def collect(key, number, elapsed, reviews, ci_runs, workers):
    events = []
    start = parse_iso(elapsed.get("started_at"))
    end = parse_iso(elapsed.get("landed_at"))

    def add(event, at, identity=None):
        when = parse_iso(at)
        if when and (not start or when >= start) and (not end or when <= end):
            events.append((when, {"event": event, "at": at, "id": identity}))

    add("ticket started", elapsed.get("started_at"))
    folder = Path(harness_home("repos", key, "timeline", str(number)))
    for path in sorted(folder.glob("*.json")):
        row = read_json(path)
        if isinstance(row, dict) and isinstance(row.get("event"), str):
            add(row["event"], row.get("at"), row.get("id"))
    add("PR opened", elapsed.get("pr_opened_at"))
    if reviews:
        add("first review completed", reviews[0].get("at"), reviews[0].get("head"))
    for run in ci_runs:
        run_id = run.get("databaseId")
        add("CI queued", run.get("createdAt"), run_id)
        add("CI started", run.get("startedAt"), run_id)
        if run.get("status") == "completed":
            add("CI finished", run.get("updatedAt"), run_id)
    for index, run in enumerate(sorted(workers, key=lambda r: r.get("started_at") or "")):
        add("worker resumed" if index else "worker launched", run.get("started_at"), run.get("id"))
        add("worker exited", run.get("ended_at"), run.get("id"))
    add("PR ready", elapsed.get("ready_at"))
    add("merged", elapsed.get("landed_at"))
    events.sort(key=lambda pair: pair[0])
    gaps = [{"from": left[1], "to": right[1], "seconds": int((right[0] - left[0]).total_seconds())}
            for left, right in zip(events, events[1:])]
    return {"events": [row for _, row in events],
            "longest_gap": max(gaps, key=lambda gap: gap["seconds"], default=None)}
