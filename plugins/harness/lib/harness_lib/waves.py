"""Wave records: the tickets one `harness-work dispatch` launched, reviewed together once all of
them are in review. One JSON file per wave at ~/.claude/harness/repos/<key>/waves/<id>.json:

    {"id": "w20261022140859", "repo": "<main>", "created_at": "2026-10-22T14:08:59Z",
     "tickets": [12, 13], "dropped": [], "tier": "fable", "effort": "xhigh", "review": null}

`review` is null until the wave review is launched, then {"launched_at": ISO, "run": "<run id>",
"reviewed_at": ISO or null}. harness-work writes waves; harness-pr and autoland read them. See
docs/design.md (State).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import datetime as dt
import errno
import os
import re

from harness_lib.text import harness_home, read_json, write_json

WAVE_ID = re.compile(r"w[0-9]{14}(?:-([0-9]+))?")
DONE = ("in-review", "closed")
PREFIX = ".wave-"


# --------------------------------------------------------------- paths
def waves_dir(key):
    """~/.claude/harness/repos/<key>/waves."""
    return harness_home("repos", key, "waves")


def wave_path(key, wave_id):
    """The record file of wave `wave_id`."""
    return os.path.join(waves_dir(key), wave_id + ".json")


def _suffix(wave_id):
    """The numeric suffix of an id (`w<stamp>-N`), 1 for none."""
    found = WAVE_ID.fullmatch(wave_id) if isinstance(wave_id, str) else None
    return int(found.group(1)) if found and found.group(1) else 1


# --------------------------------------------------------------- records
def create_wave(key, main, tickets, tier, effort, at=None):
    """Write and return a new wave of `tickets` (sorted, unique). The id is `w` plus the UTC
    yyyymmddHHMMSS of `at` (default now; a naive `at` means UTC); a second wave in the same second
    is `w<stamp>-2`, then `-3`, .... The id is reserved by creating its file with O_CREAT|O_EXCL,
    so two writers, in one process or several, never share one and no wave is overwritten."""
    when = at if at is not None else dt.datetime.now(dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc) if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    base = "w" + when.strftime("%Y%m%d%H%M%S")
    os.makedirs(waves_dir(key), exist_ok=True)
    n = 1
    while True:
        wave_id = base if n == 1 else f"{base}-{n}"
        path = wave_path(key, wave_id)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except OSError as err:
            if err.errno != errno.EEXIST:
                raise
            n += 1
            continue
        break
    wave = {"id": wave_id, "repo": main, "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tickets": sorted(set(tickets)), "dropped": [], "tier": tier, "effort": effort, "review": None}
    try:
        write_json(path, wave, prefix=PREFIX)
    except BaseException:
        try:
            os.unlink(path)         # give the reserved id back rather than leave an empty file
        except OSError:
            pass
        raise
    return wave


def load_wave(key, wave_id):
    """The wave's record, or None when the id is not a wave id, or the file is missing, unreadable,
    not a JSON object or holds another wave's id."""
    if not isinstance(wave_id, str) or not WAVE_ID.fullmatch(wave_id):
        return None
    wave = read_json(wave_path(key, wave_id))
    if not isinstance(wave, dict) or wave.get("id") != wave_id:
        return None
    return wave


def load_waves(key):
    """Every readable wave of the repository, oldest first: by created_at, then the id's numeric
    suffix (none = 1). Only files named `<wave id>.json` are read; broken ones are skipped."""
    try:
        names = os.listdir(waves_dir(key))
    except OSError:
        return []
    waves = []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext != ".json" or not WAVE_ID.fullmatch(stem):
            continue
        wave = load_wave(key, stem)
        if wave is not None:
            waves.append(wave)

    def order(wave):
        created = wave.get("created_at")
        return (created if isinstance(created, str) else "", _suffix(wave["id"]), wave["id"])

    return sorted(waves, key=order)


def save_wave(key, wave):
    """Write `wave` back to its file (through text.py's write_json)."""
    wave_id = wave.get("id")
    if not isinstance(wave_id, str) or not WAVE_ID.fullmatch(wave_id):
        raise ValueError(f"not a wave id: {wave_id!r}")
    write_json(wave_path(key, wave_id), wave, prefix=PREFIX)


def _tickets(wave, field="tickets"):
    value = wave.get(field)
    return [n for n in value if isinstance(n, int)] if isinstance(value, list) else []


def wave_of(key, ticket):
    """The newest wave whose `tickets` lists `ticket` (a dropped ticket does not count), else None."""
    for wave in reversed(load_waves(key)):
        if ticket in _tickets(wave):
            return wave
    return None


def drop_ticket(key, wave_id, ticket):
    """Move `ticket` from the wave's `tickets` to `dropped` (sorted, unique) and save it; return the
    record. A ticket the wave does not list leaves it unchanged and unwritten. None, and no file
    written, when the wave does not exist or cannot be read."""
    wave = load_wave(key, wave_id)
    if wave is None:
        return None
    tickets = _tickets(wave)
    if ticket not in tickets:
        return wave
    wave["tickets"] = [n for n in tickets if n != ticket]
    wave["dropped"] = sorted(set(_tickets(wave, "dropped")) | {ticket})
    save_wave(key, wave)
    return wave


# --------------------------------------------------------------- progress
def ticket_statuses(key, tickets):
    """{n: status or None} from each ticket's cache repos/<key>/tickets/<n>.json; None when the
    cache is missing, broken or has no status."""
    statuses = {}
    for n in tickets:
        cache = read_json(harness_home("repos", key, "tickets", f"{n}.json"))
        status = cache.get("status") if isinstance(cache, dict) else None
        statuses[n] = status if isinstance(status, str) else None
    return statuses


def progress(wave, statuses):
    """{"done": [n], "waiting": [{"number": n, "status": s}], "complete": bool} over the wave's
    tickets (dropped ones aside): in-review and closed are done, anything else, a ticket missing
    from `statuses` included, waits. Complete when the wave has tickets and none waits."""
    done, waiting = [], []
    for n in sorted(set(_tickets(wave))):
        status = statuses.get(n)
        if status in DONE:
            done.append(n)
        else:
            waiting.append({"number": n, "status": status})
    return {"done": done, "waiting": waiting, "complete": bool(done) and not waiting}


def wave_state(wave, statuses):
    """`empty` (no tickets), `working` (not complete), `due` (complete, no review launched),
    `reviewing` (the review has `launched_at` and no `reviewed_at`) or `reviewed` (`reviewed_at`
    set). Completeness comes first: a waiting ticket is `working` whatever the review says."""
    if not _tickets(wave):
        return "empty"
    if not progress(wave, statuses)["complete"]:
        return "working"
    review = wave.get("review")
    if not isinstance(review, dict):
        return "due"
    if review.get("reviewed_at"):
        return "reviewed"
    if review.get("launched_at"):
        return "reviewing"
    return "due"


# --------------------------------------------------------------- the review marker
def wave_marker(wave_id):
    """The HTML comment a wave review's PR comment carries: <!-- harness-wave-review <id> -->."""
    return f"<!-- harness-wave-review {wave_id} -->"


def has_wave_review(reviews, wave_id):
    """Whether any review dict's `body` contains the wave's marker."""
    marker = wave_marker(wave_id)
    return any(isinstance(r, dict) and isinstance(r.get("body"), str) and marker in r["body"]
               for r in reviews or ())
