"""Claude Code's transcripts for a repository, read for the cost metric: the session folders
(the main checkout's and every worktree's, removed ones included), the sessions and subagents
in them, and from one transcript each API response's tokens (deduped by message id), the time
the model worked on it, and the session's own token and API-time totals. Tokens and time only:
no dollar figure is read. See docs/design.md (Layout).

It reads and never writes; a missing file, an unparseable line or an entry without a timestamp
is skipped, never raised. The entry shapes are Claude Code's own (CLI 2.1.296 and 2.1.301) and
undocumented.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os

from harness_lib.text import session_folder

# message.usage field -> the name a message's usage uses
USAGE_FIELDS = (("input_tokens", "input"), ("output_tokens", "output"),
                ("cache_creation_input_tokens", "cache_write"), ("cache_read_input_tokens", "cache_read"))
# cost-state modelUsage field -> the name cost_state uses (costUSD is never read)
MODEL_FIELDS = (("inputTokens", "input"), ("outputTokens", "output"), ("thinkingTokens", "thinking"),
                ("cacheReadInputTokens", "cache_read"), ("cacheCreationInputTokens", "cache_write"))


# --------------------------------------------------------------- folders and sessions
def session_dirs(main: str) -> list[str]:
    """~/.claude/projects/<session_folder(main)> first, whether it exists or not, then every
    folder there named for a worktree of `main` (<main>/.claude/worktrees/<name>), by name; a
    removed worktree keeps its folder, so the folders are found by listing, not from git."""
    projects = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    dirs = [os.path.join(projects, session_folder(main))]
    prefix = session_folder(os.path.join(main, ".claude", "worktrees")) + "-"
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        return dirs
    for name in names:
        if len(name) > len(prefix) and name.startswith(prefix) and os.path.isdir(os.path.join(projects, name)):
            dirs.append(os.path.join(projects, name))
    return dirs


def _listdir(folder):
    try:
        return os.listdir(folder)
    except OSError:
        return []


def _meta(path):
    """The parsed meta file when it is a JSON object; {} otherwise."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            value = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def sessions(dirs: list[str]) -> list[dict]:
    """One {"id", "path", "agents"} per <dir>/<id>.jsonl, in the order of `dirs` then by id; each
    agent {"id", "path", "meta"} from <dir>/<id>/subagents/agent-<aid>.jsonl, by id, with its
    agent-<aid>.meta.json parsed ({} when missing, unreadable or not an object)."""
    found = []
    for folder in dirs:
        ids = sorted(name[:-len(".jsonl")] for name in _listdir(folder)
                     if name.endswith(".jsonl") and len(name) > len(".jsonl")
                     and os.path.isfile(os.path.join(folder, name)))
        for sid in ids:
            sub = os.path.join(folder, sid, "subagents")
            aids = sorted(name[len("agent-"):-len(".jsonl")] for name in _listdir(sub)
                          if name.startswith("agent-") and name.endswith(".jsonl")
                          and len(name) > len("agent-.jsonl") and os.path.isfile(os.path.join(sub, name)))
            agents = [{"id": aid, "path": os.path.join(sub, f"agent-{aid}.jsonl"),
                       "meta": _meta(os.path.join(sub, f"agent-{aid}.meta.json"))} for aid in aids]
            found.append({"id": sid, "path": os.path.join(folder, f"{sid}.jsonl"), "agents": agents})
    return found


# --------------------------------------------------------------- one transcript
def _entries(path):
    """Each line of the file that parses as a JSON object; nothing when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def _count(value):
    """A token count or duration: an int (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _timestamp(entry):
    value = entry.get("timestamp")
    return value if isinstance(value, str) and value else None


def _branch(entry):
    value = entry.get("gitBranch")
    return value if isinstance(value, str) and value else None


def _cost_state(entry):
    """api_ms and each model's tokens from a cost-state entry; nothing else of it is read."""
    usage = entry.get("modelUsage")
    models = {}
    if isinstance(usage, dict):
        for model, counts in usage.items():
            if isinstance(counts, dict):
                models[model] = {name: _count(counts.get(raw)) for raw, name in MODEL_FIELDS}
    return {"api_ms": _count(entry.get("totalAPIDuration")), "models": models}


def read_transcript(path: str) -> dict:
    """{"messages", "branches", "cost_state"} of one transcript.

    A message per assistant message.id, in order of first appearance: its model, branch and
    segment from its first entry, each usage field the largest among its entries, `start` the
    api-request entry its requestRef names (else the latest user entry before it, else its first
    entry) and `end` its last entry. A user entry from the coordinator starts a new segment.
    `branches` are the distinct gitBranch values in order; `cost_state` comes from the last
    cost-state entry, or is None."""
    messages, by_id, branches = [], {}, []
    requests, last_user, segment, cost = {}, None, 1, None
    for entry in _entries(path):
        kind = entry.get("type")
        if kind == "cost-state":                     # the last line of an ended session, no timestamp
            cost = _cost_state(entry)
            continue
        ts = _timestamp(entry)
        if ts is None:
            continue
        branch = _branch(entry)
        if branch is not None and branch not in branches:
            branches.append(branch)
        if kind == "api-request":
            if isinstance(entry.get("id"), str):
                requests[entry["id"]] = ts
        elif kind == "user":
            last_user = ts
            origin = entry.get("origin")
            if isinstance(origin, dict) and origin.get("kind") == "coordinator":
                segment += 1
        elif kind == "assistant":
            message = entry.get("message")
            mid = message.get("id") if isinstance(message, dict) else None
            if not isinstance(mid, str) or not mid:
                continue
            m = by_id.get(mid)
            if m is None:
                ref = entry.get("requestRef")
                start = requests.get(ref) if isinstance(ref, str) else None
                model = message.get("model")
                m = by_id[mid] = {"id": mid, "model": model if isinstance(model, str) else None,
                                  "start": start or last_user or ts, "end": ts, "branch": branch,
                                  "segment": segment, "usage": {name: 0 for _, name in USAGE_FIELDS}}
                messages.append(m)
            m["end"] = ts
            usage = message.get("usage")
            if isinstance(usage, dict):
                for raw, name in USAGE_FIELDS:
                    m["usage"][name] = max(m["usage"][name], _count(usage.get(raw)))
    return {"messages": messages, "branches": branches, "cost_state": cost}


def first_branch(path: str) -> str | None:
    """The gitBranch of the first entry that has one, reading no further; None when none has.
    The same entries count as for read_transcript's branches (a timestamp, not cost-state)."""
    for entry in _entries(path):
        if entry.get("type") != "cost-state" and _timestamp(entry) is not None:
            branch = _branch(entry)
            if branch is not None:
                return branch
    return None
