"""Review rounds: one ticket's harness reviews, recorded by `harness-pr findings` and read by
`harness-work stage`. One folder per ticket at ~/.claude/harness/repos/<key>/reviews/<ticket>/,
holding `rounds.json` (the list of rounds, oldest first) and `rulings.json` (the `Ruling:` lines
gathered so far). A round is

    {"round": 1, "at": "2026-10-26T10:00:00Z", "head": "<sha>", "base": "<sha>",
     "blocking": [...], "low": [...], "outside": [...], "pr": null, "review_id": null}

and each finding is {"path", "line", "severity", "body"} as harness-pr's load_findings normalises
it. See docs/design.md (State).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os

from harness_lib.text import harness_home, read_json, write_json

PREFIX = ".reviews-"


# --------------------------------------------------------------- paths
def reviews_dir(key, ticket):
    """~/.claude/harness/repos/<key>/reviews/<ticket>."""
    return harness_home("repos", key, "reviews", str(ticket))


def rounds_path(key, ticket):
    return os.path.join(reviews_dir(key, ticket), "rounds.json")


def rulings_path(key, ticket):
    return os.path.join(reviews_dir(key, ticket), "rulings.json")


# --------------------------------------------------------------- rounds
def load_rounds(key, ticket):
    """The ticket's rounds, oldest first; [] when rounds.json is missing, unreadable or not a list."""
    rounds = read_json(rounds_path(key, ticket))
    return [r for r in rounds if isinstance(r, dict)] if isinstance(rounds, list) else []


def _number(rnd, fallback):
    k = rnd.get("round")
    return k if isinstance(k, int) and not isinstance(k, bool) else fallback


def save_round(key, ticket, rnd):
    """Record `rnd` and return it numbered: the latest round's number plus one, or, when its `head`
    equals the latest round's, in place of that round with its number. Any `round` in `rnd` is
    ignored."""
    rounds = load_rounds(key, ticket)
    latest = rounds[-1] if rounds else None
    if latest is not None and latest.get("head") == rnd.get("head"):
        k = _number(latest, len(rounds))
        rounds.pop()
    else:
        k = _number(latest, len(rounds)) + 1 if latest is not None else 1
    saved = {"round": k}
    saved.update((name, value) for name, value in rnd.items() if name != "round")
    rounds.append(saved)
    write_json(rounds_path(key, ticket), rounds, prefix=PREFIX)
    return saved


def latest_round(key, ticket):
    """The ticket's latest round, or None when none is recorded."""
    rounds = load_rounds(key, ticket)
    return rounds[-1] if rounds else None


def blocking(rnd):
    """The round's blocking findings (high or medium, on a file the ticket changed)."""
    found = rnd.get("blocking") if isinstance(rnd, dict) else None
    return [f for f in found if isinstance(f, dict)] if isinstance(found, list) else []


# --------------------------------------------------------------- rulings
def load_rulings(key, ticket):
    """The `Ruling:` lines recorded for the ticket, in first-seen order; [] when none or broken."""
    rulings = read_json(rulings_path(key, ticket))
    return [r for r in rulings if isinstance(r, str)] if isinstance(rulings, list) else []


def add_rulings(key, ticket, lines):
    """Add the `lines` not yet recorded (by text, first-seen order kept) and return all of them.
    The file is written only when something new arrives."""
    rulings = load_rulings(key, ticket)
    new = [line for i, line in enumerate(lines) if line not in rulings and line not in lines[:i]]
    if new:
        rulings += new
        write_json(rulings_path(key, ticket), rulings, prefix=PREFIX)
    return rulings
