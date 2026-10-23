"""The auto-land gate harness-pr gate and harness-pr autoland apply: the gate's settings, read only
from the .claude/settings.json committed at a ref (gate_settings), and its ten rules over the facts
gathered about one pull request (evaluate), the first failing rule deciding. See docs/design.md
(The auto-land gate).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import re

from harness_lib.settings import committed_env, glob_match, patterns

# Paths whose change always lands by hand; a project's HARNESS_MANUAL_PATHS adds to these.
DEFAULT_MANUAL_PATHS = (
    ".github/**", ".claude/**", "scripts/deploy.sh", "scripts/smoke.sh", "scripts/deploy_guard.py",
    "scripts/verify.sh", "**/credentials/**", "**/.env*", "**/*.pem",
    "pyproject.toml", "package.json", "**/conftest.py", "pytest.ini",
)
DEFAULT_SIZES = ["S", "M"]
SIZES = ("S", "M", "L")
MAX_NONTEST_FILES = 8  # above either, rule 5 answers too large whatever the size label says
MAX_NONTEST_LINES = 400

TICKET_BRANCH = re.compile(r"t/(\d+)-")


def _committed(name, env):
    """settings.setting() for a committed value only: a string or number (never a bool),
    stripped, None when unset or blank."""
    value = env.get(name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def gate_settings(main, ref="origin/main"):
    """{autoland, sizes, manual_paths, smoke_local} from the `env` of the .claude/settings.json
    committed at `ref` in the repository at `main`; the process environment, the working tree and
    settings.local.json never count. HARNESS_AUTOLAND and HARNESS_SMOKE_LOCAL are on only at `1`;
    HARNESS_AUTOLAND_SIZES holding anything but S, M or L gives the default S,M;
    HARNESS_MANUAL_PATHS adds to DEFAULT_MANUAL_PATHS (defaults first, de-duplicated)."""
    env = committed_env(main, ref)
    sizes = list(dict.fromkeys(patterns(_committed("HARNESS_AUTOLAND_SIZES", env) or "")))
    if not sizes or any(size not in SIZES for size in sizes):
        sizes = list(DEFAULT_SIZES)
    project = patterns(_committed("HARNESS_MANUAL_PATHS", env) or "")
    return {
        "autoland": _committed("HARNESS_AUTOLAND", env) == "1",
        "sizes": sizes,
        "manual_paths": list(dict.fromkeys([*DEFAULT_MANUAL_PATHS, *project])),
        "smoke_local": _committed("HARNESS_SMOKE_LOCAL", env) == "1",
    }


# Each rule answers None when it passes, else (verdict, code, reason) for its first failing condition.
def _opted_in(facts, settings):
    if not settings.get("autoland"):
        return "manual", "off", "autoland off"
    return None


def _pull_request(facts, settings):
    number = (facts.get("ticket") or {}).get("number")
    state = facts.get("state")
    if state != "OPEN":
        return "manual", "pr", f"PR state is {state or 'empty'}, not OPEN"
    if facts.get("draft"):
        return "manual", "pr", "draft"
    head = facts.get("head_ref") or ""
    m = TICKET_BRANCH.match(head)
    if not m or int(m.group(1)) != number:
        return "manual", "pr", f"head {head or '(none)'} is not a ticket branch t/{number}-..."
    author, login = facts.get("author"), facts.get("login")
    if author != login:
        return "manual", "pr", f"author {author} is not {login}"
    closing = list(facts.get("closing") or [])
    if closing != [number]:
        closes = ", ".join(f"#{n}" for n in closing) or "nothing"
        return "manual", "pr", f"does not close exactly #{number} (closes {closes})"
    return None


def _main_healthy(facts, settings):
    incidents = facts.get("incidents") or []
    if incidents:
        numbers = ", ".join(f"#{i.get('number')}" for i in incidents)
        return "wait", "incident", f"incident {numbers} open"
    deploy = facts.get("deploy")
    if deploy is not None:
        if deploy.get("status") != "completed":
            return "wait", "deploy", f"deploy of main in flight ({deploy.get('status')})"
        if deploy.get("conclusion") != "success":
            return "wait", "deploy", f"last deploy of main failed ({deploy.get('conclusion') or 'no conclusion'})"
    return None


def _ticket(facts, settings):
    ticket = facts.get("ticket") or {}
    labels = list(ticket.get("labels") or [])
    if ticket.get("state") != "OPEN":
        return "manual", "ticket", f"ticket #{ticket.get('number')} is {ticket.get('state') or 'not open'}"
    risks = [label for label in labels if label.startswith("risk:")]
    if risks != ["risk:low"]:
        return "manual", "risk", f"risk {', '.join(risks)}, not risk:low" if risks else "no risk label"
    sizes = [label[len("size:"):] for label in labels if label.startswith("size:")]
    allowed = settings.get("sizes") or DEFAULT_SIZES
    if not sizes:
        return "manual", "size", "no size label"
    if any(size not in allowed for size in sizes):
        return "manual", "size", f"size {', '.join(sizes)} not in {','.join(allowed)}"
    if "manual-land" in (facts.get("pr_labels") or []) or "manual-land" in labels:
        return "manual", "label", "manual-land label"
    return None


def _paths(facts, settings):
    manual = settings.get("manual_paths") or DEFAULT_MANUAL_PATHS
    for path in facts.get("files") or []:
        if any(glob_match(p, path) for p in manual):
            return "manual", "path", f"protected path {path}"
    files, lines = facts.get("nontest_files") or 0, facts.get("nontest_lines") or 0
    if files > MAX_NONTEST_FILES or lines > MAX_NONTEST_LINES:
        return "manual", "too large", (f"too large: {files} non-test files, {lines} non-test lines "
                                       f"(at most {MAX_NONTEST_FILES} and {MAX_NONTEST_LINES})")
    return None


def _review(facts, settings):
    review = facts.get("review")
    if review is None:
        return "manual", "review", "no harness review"
    if not review.get("at_head"):
        return "wait", "re-check", f"review of {(review.get('commit_id') or '')[:7]}, head {facts['head_sha'][:7]}"
    found = [f"{review.get(sev) or 0} {sev}" for sev in ("high", "medium") if review.get(sev)]
    if found:
        return "fix", "findings", f"{', '.join(found)} at head"
    return None


def _comments(facts, settings):
    threads, comments = facts.get("unresolved_threads") or 0, facts.get("owner_comments") or 0
    if threads > 0 or comments > 0:
        return "manual", "comment", f"{threads} unresolved threads, {comments} owner comments"
    return None


def _mergeable(facts, settings):
    if facts.get("mergeable") == "CONFLICTING":
        return "fix", "conflict", "conflicts with main"
    behind = facts.get("behind_by") or 0
    if behind > 0:
        return "wait", "behind", f"behind main by {behind}"
    return None


def _checks(facts, settings):
    checks = facts.get("checks")
    if checks == "success":
        return None
    if checks == "pending":
        names = ", ".join(facts.get("pending_checks") or [])
        return "wait", "ci", f"pending: {names}" if names else "checks pending"
    if checks == "failure":
        names = ", ".join(facts.get("failed_checks") or [])
        return "fix", "ci", f"failed: {names}" if names else "checks failed"
    return "manual", "checks", "no checks" if checks == "none" else f"checks state {checks!r}"


RULES = (_opted_in, _pull_request, _main_healthy, _ticket, _paths, _review, _comments, _mergeable, _checks)


def evaluate(facts, settings):
    """{verdict, code, reason, rule}: rules 1 to 9 in order, the first failing one deciding
    (verdict wait, fix or manual); all passing gives land/land, rule 10, at the head's sha7."""
    for rule, check in enumerate(RULES, 1):
        failed = check(facts, settings)
        if failed:
            verdict, code, reason = failed
            return {"verdict": verdict, "code": code, "reason": reason, "rule": rule}
    return {"verdict": "land", "code": "land", "reason": f"gate passed at {facts['head_sha'][:7]}", "rule": 10}
