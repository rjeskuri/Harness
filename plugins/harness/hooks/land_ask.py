#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): ask before a session lands a risk:high ticket's PR.

/harness:work lands its own pull request once the gate passes, except for a risk:high ticket,
whose PR waits for Ryan (#325, #330). This hook makes that stop deterministic: it turns
`harness-pr land P` into a permission prompt (permissionDecision "ask") unless every ticket the
PR closes is labeled risk:low.

A command whose text does not hold "harness-pr" exits at once, printing nothing. Otherwise the
command is walked as branch_guard.py walks it (split on ;, &&, ||, |, & and newlines, inside
(...), $(...), sh -c, eval and a heredoc a shell reads), with the directory each part runs in
(the event's cwd, moved by cd and pushd, restored after a subshell or a child shell, unknown after
a cd whose target cannot be resolved), and every part `harness-pr land P [flags]` (harness-pr by
its basename, so a path to it counts) is judged, in the order the shell runs them:
  - `--accept-findings` or `--discard` on any land part: ask, naming the flag, before any gh call;
  - P not a number (a leading # allowed): ask; `harness-pr land` with no number or with --help,
    and every other harness-pr command: no decision;
  - the directory unknown: ask, no gh call; the directory outside a git repository: ask, no gh call;
  - else, in the main checkout of the part's directory (the parent of `git rev-parse
    --git-common-dir`, resolved, so a linked worktree or a subdirectory maps to its main
    checkout), `gh pr view P --json closingIssuesReferences,headRefName` names the tickets (the
    closing issues, else N of a t/<N>- head when nothing closes), then `gh issue view N --json
    labels` for each: a ticket not labeled risk:low (risk:high, or no risk label), no ticket, or a
    gh that fails, is missing, is too slow or runs out of the budget asks with its reason.
Every ticket risk:low: no decision at all (no stdout, no stderr, exit 0).

gh runs through harness_lib's gh_run (HARNESS_GH honored), one call at a time, each bounded by
the lesser of 3 s and what is left of one 8 s budget for the whole run (hooks.json gives the hook
10 s; a hook killed there makes no decision, and the land runs unasked); the git lookups count
against the same budget. Out of time, the hook asks, naming gh (or git) and the seconds in force.
HARNESS_LAND_ASK_TIMESCALE, for tests only, is a factor s in (0, 1] on both (3*s a call, 8*s for
the run); any other value (unset, empty, over 1, 0, negative, nan, not a number) leaves 3 s and 8 s
and prints nothing. It never lengthens a deadline.

An ask prints {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
"permissionDecisionReason": "..."}} and exits 0. Fails open: without its lib, or on any error of
its own, one stderr line, exit 0, no decision.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

NAME = "land_ask.py"
TOOL = "harness-pr"
BUDGET = 8.0  # seconds of gh and git for the whole run, inside hooks.json's 10 s
CALL_TIMEOUT = 3.0  # at most, for each gh or git call
TIMESCALE = "HARNESS_LAND_ASK_TIMESCALE"  # tests only: a factor in (0, 1] on both deadlines
OVERRIDES = ("--accept-findings", "--discard")
TICKET_BRANCH = re.compile(r"^t/(\d+)-")
RYAN_LANDS = ": Ryan lands it."
_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))


def read_event() -> dict:
    """The PreToolUse event on stdin, {} when it is not a JSON object (read here, before the lib, so a
    Bash call without harness-pr costs no import)."""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def command_of(data: dict) -> str:
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return command if isinstance(command, str) else ""


def timescale() -> float:
    """HARNESS_LAND_ASK_TIMESCALE when it is a number in (0, 1], else 1: the knob only ever shortens the
    deadlines, since past hooks.json's 10 s Claude Code kills the hook with no decision and the land runs."""
    try:
        scale = float(os.environ.get(TIMESCALE, ""))
    except ValueError:
        return 1.0
    return scale if 0 < scale <= 1 else 1.0


def seconds(value: float) -> str:
    """`value` seconds as a reason names them: 8, 0.75, 2.4."""
    return f"{round(value, 2):g}"


def ask(reason: str) -> None:
    """Turn the call into a permission prompt with `reason`, which Ryan sees (the lib is loaded by now)."""
    from harness_lib.hookio import emit

    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                 "permissionDecisionReason": reason}})


class Land:
    """One `harness-pr land` part: its text, the directory it runs in (None when unknown), its PR
    number as written, and the override flags it carries."""

    def __init__(self, text: str, cwd: str | None, args: list) -> None:
        self.text = text
        self.cwd = cwd
        self.flags = [a for a in OVERRIDES if a in args]
        plain = [a for a in args if not a.startswith("-") or a == "-"]
        self.given = plain[0] if plain else None

    @property
    def number(self) -> int | None:
        """The PR number, None when what was given is not one."""
        digits = (self.given or "").lstrip("#")
        return int(digits) if digits.isdigit() and int(digits) > 0 else None


def lands(guard, cwd: str, command: str) -> list:
    """The `harness-pr land P` parts of `command` with a number given and no --help, in the order the
    shell runs them, each with the directory it runs in."""
    found = []
    state = {"cwd": cwd}
    for part, name, args in guard.walk(state, command):
        if name != TOOL or not args or args[0] != "land":
            continue
        rest = args[1:]
        if "--help" in rest or "-h" in rest:
            continue
        land = Land(part.text, state["cwd"], rest)
        if land.given is not None:
            found.append(land)
    return found


class Judge:
    """The gh and git calls of one run, under one deadline: `budget` seconds from `started`, each call at most
    `call` seconds of it (BUDGET and CALL_TIMEOUT, scaled by the test knob)."""

    def __init__(self, started: float, budget: float, call: float, gh_run, problem) -> None:
        self.deadline = started + budget
        self.budget = budget
        self.call = call
        self.gh_run = gh_run
        self.problem = problem
        self.mains: dict = {}

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def out_of_time(self) -> str:
        return f"{seconds(self.budget)} s for the whole command"

    def cut_off(self, left: float) -> str:
        """What a call given `left` seconds of the run was cut off by: its own limit, or the last of the run's."""
        if left < self.call:
            return f", the last of the {self.out_of_time()}"
        return ""

    def main_checkout(self, directory: str) -> tuple:
        """(main checkout, None) of the repository holding `directory`, or (None, why)."""
        if directory in self.mains:
            return self.mains[directory]
        left = self.left()
        if left <= 0:
            return None, f"git ran out of time finding its main checkout ({self.out_of_time()})"
        timeout = min(self.call, left)
        try:
            out = subprocess.run(["git", "-C", directory, "rev-parse", "--git-common-dir"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout)
        except subprocess.TimeoutExpired:
            limit = f"{seconds(timeout)} s{self.cut_off(left)}"
            return None, f"git was too slow finding its main checkout (cut off after {limit})"
        except OSError as err:
            return None, f"git cannot run ({err.strerror or err})"
        common = out.stdout.strip()
        if out.returncode != 0 or not common:
            found = (None, f"no git repository found at {directory}")
        else:
            found = (os.path.realpath(os.path.dirname(os.path.normpath(os.path.join(directory, common)))), None)
        self.mains[directory] = found
        return found

    def gh_json(self, args: list, cwd: str) -> tuple:
        """(gh's JSON answer, None), or (None, why) naming gh."""
        left = self.left()
        if left <= 0:
            return None, f"gh ran out of time ({self.out_of_time()})"
        texts = {"timed_out": "{call} timed out after {timeout} s" + self.cut_off(left)}
        try:
            done = self.gh_run(args, cwd, timeout=round(min(self.call, left), 2), texts=texts)
        except self.problem as exc:
            return None, str(exc)
        call = "gh " + " ".join(args[:3])
        if done.returncode != 0:
            why = next((ln.strip() for ln in done.stderr.splitlines() if ln.strip()), f"exit {done.returncode}")
            return None, f"{call} failed: {why}"
        try:
            data = json.loads(done.stdout)
        except ValueError:
            excerpt = next((ln.strip() for ln in done.stdout.splitlines() if ln.strip()), "")[:80]
            return None, f"{call} printed something other than JSON: {excerpt}"
        return (data if isinstance(data, dict) else {}), None

    def reason(self, land: Land) -> str | None:
        """Why `land` must be asked about, or None when every ticket its PR closes is risk:low."""
        number = land.number
        pr = f"PR #{number}"
        if land.cwd is None:
            return (f"{pr}: cannot tell which directory `{land.text}` runs in (a cd the hook cannot resolve), so "
                    f"which repository it lands in{RYAN_LANDS}")
        main, why = self.main_checkout(land.cwd)
        if main is None:
            return f"{pr}: {why}, so its ticket's risk cannot be checked{RYAN_LANDS}"
        view, why = self.gh_json(["pr", "view", str(number), "--json", "closingIssuesReferences,headRefName"], main)
        if view is None:
            return f"{pr}: {why}, so its ticket's risk cannot be checked{RYAN_LANDS}"
        refs = view.get("closingIssuesReferences")
        tickets = [ref["number"] for ref in (refs if isinstance(refs, list) else [])
                   if isinstance(ref, dict) and isinstance(ref.get("number"), int)]
        head = view.get("headRefName") if isinstance(view.get("headRefName"), str) else ""
        if not tickets:
            m = TICKET_BRANCH.match(head)
            if not m:
                return f"{pr} closes no issue and its head {head or '(unknown)'} names no ticket{RYAN_LANDS}"
            tickets = [int(m.group(1))]
        for ticket in tickets:
            issue, why = self.gh_json(["issue", "view", str(ticket), "--json", "labels"], main)
            if issue is None:
                return f"{pr} closes #{ticket}: {why}, so its risk cannot be checked{RYAN_LANDS}"
            labels = issue.get("labels")
            names = [lb.get("name") for lb in (labels if isinstance(labels, list) else []) if isinstance(lb, dict)]
            if "risk:low" in names:
                continue
            risk = next((n for n in names if isinstance(n, str) and n.startswith("risk:")), None)
            if risk:
                return f"{pr} closes #{ticket}, a {risk} ticket{RYAN_LANDS}"
            return f"{pr} closes #{ticket}, which has no risk label{RYAN_LANDS}"
        return None


def main() -> int:
    started = time.monotonic()
    data = read_event()
    command = command_of(data)
    if TOOL not in command or data.get("tool_name", "Bash") != "Bash":
        return 0
    try:
        from harness_lib.cli import Problem
        from harness_lib.gh import gh_run
        from harness_lib.hookio import hook_cwd
    except Exception as exc:  # fail open: one line, exit 0, no decision
        sys.stderr.write(f"{NAME}: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
        return 0
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import branch_guard as guard

    found = lands(guard, hook_cwd(data), command)
    if not found:
        return 0
    for land in found:  # an override flag anywhere asks before any gh call
        if land.flags:
            ask(f"`{land.text}` overrides the land's checks with {' and '.join(land.flags)}{RYAN_LANDS}")
            return 0
    for land in found:
        if land.number is None:
            ask(f"`{land.text}`: {land.given} is not a PR number{RYAN_LANDS}")
            return 0
    scale = timescale()
    judge = Judge(started, BUDGET * scale, CALL_TIMEOUT * scale, gh_run, Problem)
    for land in found:
        reason = judge.reason(land)
        if reason:
            ask(reason)
            return 0
    return 0


def run_fail_open() -> int:
    """main()'s status; an exception from it is one stderr line, "land_ask.py: <error>", and 0."""
    try:
        return main() or 0
    except Exception as exc:
        sys.stderr.write(f"{NAME}: {' '.join(str(exc).split())}\n")
        return 0


if __name__ == "__main__":
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    # fail open: a broken hook must never trap the agent, and never let a land through unjudged in silence
    sys.exit(run_fail_open())
