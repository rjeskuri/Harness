#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash; #452, epic #439): only the owner's own `/harness:land P` lands a `manual-land`
pull request.

`harness-pr land --loop` refuses a manual-land PR (#451), but --loop is a flag the caller sets itself, and any
session may run `harness-pr land P` (`Bash(harness-pr:*)` is allowed). So this hook walks the command with
branch_guard.py's walker (each simple command in the directory it runs in, after cd and pushd, and inside sh -c,
eval, a heredoc a shell reads and substitutions) and judges each part whose command word lands a pull request:
  - `harness-pr land P`, its options anywhere;
  - `gh pr merge [P | URL | branch]`, none naming the current branch's PR in the part's directory, -R/--repo kept;
  - `gh api ... pulls/P/merge` with -X PUT or --method PUT (any other method merges nothing), a literal
    repos/OWNER/REPO/ in the path read as -R OWNER/REPO.
For each it reads `gh pr view <selector> --json number,labels,closingIssuesReferences` in that directory and, as
harness-pr's manual_land does (gh's closingIssuesReferences carries no labels), `gh issue view N --json labels` for
each ticket the PR closes until one has `manual-land`. A PR with the label on it or on such a ticket gets through
(no decision) only when the event has no agent_id (the main session, not a subagent) and the newest transcript entry
whose origin is {"kind": "human"} is the /harness:land command whose <command-args> start with P (the first
whitespace-separated word, read as harness-pr reads P, so `570` is not 57); else it is denied, the reason naming
`/harness:land P`. A PR without the label gets no decision.

A part that starts a session (`claude` with --bg, -p or --print) whose prompt starts with /harness:land is denied,
on any PR and in any session: dispatch only ever sends /harness:work N. Any of its arguments counts as the prompt,
and so does what a heredoc, a here-string or the command piped into it puts on its stdin.

A part whose command word is anything else (git commit -m "...", echo, gh pr create --body "...") lands nothing,
whatever its text holds. A command naming none of harness-pr, gh and claude is not walked, and gh runs only for a
landing part.

When the labels or the transcript cannot be read, a `harness-pr land ... --loop` (which refuses a manual-land PR
anyway) is denied and anything else gets no decision, with one stderr line saying so, as the other hooks fail open.
A deny is hookio's PreToolUse deny, exit 0; letting a call through is no decision, never allow or ask, so the
permission rules still apply. See docs/design.md (Hooks).
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.cli import Problem
    from harness_lib.gh import gh_json
    from harness_lib.hookio import deny, hook_cwd, read_input, run_fail_open
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"land_owner.py: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
    sys.exit(0)

MANUAL_LAND = "manual-land"
LAND_COMMAND = "/harness:land"
LANDING_WORD = re.compile(r"(?<![\w-])(?:harness-pr|gh|claude)(?![\w-])")  # else nothing in it can land
BUDGET = 25.0  # seconds for every gh call together; hooks.json gives the hook 30
GH_TIMEOUT = 10.0
PR_FIELDS = "number,labels,closingIssuesReferences"
LAND_VALUES = {"--rounds", "--timeout"}  # harness-pr land's options that take a value
MERGE_VALUES = set("-b --body -F --body-file -t --subject -A --author-email --match-head-commit -R --repo".split())
API_VALUES = set(
    "-X --method -f --raw-field -F --field -H --header --input -q --jq -t --template --cache -p --preview "
    "--hostname".split()
)
LAUNCH_FLAGS = {"--bg", "-p", "--print"}
PR_NUMBER = re.compile(r"#?(\d+)$")  # harness-pr's P: 57 or #57
MERGE_PATH = re.compile(r"(?:^|/)pulls/(\d+)/merge/?(?:[?#]|$)")
REPO_PATH = re.compile(r"/?repos/([^/{}]+)/([^/{}]+)/pulls/")
COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
LAUNCH_REASON = (
    "A session started with a /harness:land prompt would land a pull request without the owner: only the owner's "
    "own /harness:land P, typed in the main session, lands a manual-land PR, and dispatch launches /harness:work N "
    "only. Leave the PR in review for the owner."
)


class Unread(Exception):
    """The labels or the transcript could not be read, so the land cannot be judged."""


def branch_guard():
    """branch_guard.py from this directory: its walker and argument helpers."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import branch_guard as guard

    return guard


# ---------- which parts land a pull request ----------


def api_method(args: list[str]) -> str:
    """gh api's method as given (-X PUT, -XPUT, --method PUT, --method=PUT), upper-cased; "" when none is."""
    method = ""
    for k, a in enumerate(args):
        if a == "--":
            break
        if a in ("-X", "--method") and k + 1 < len(args):
            method = args[k + 1]
        elif a.startswith("--method="):
            method = a[len("--method=") :]
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
    return method.upper()


def landing(guard, name: str, args: list[str]):
    """(selector, repo, loop) when the part lands a pull request, else None: the selector is the PR's number,
    gh pr merge's URL or branch as given, or "" for the current branch's PR; repo a -R value or None; loop
    whether it is `harness-pr land --loop`."""
    if name == "harness-pr":
        words = guard.plain_args(args, LAND_VALUES)
        number = PR_NUMBER.match(words[1]) if words[:1] == ["land"] and len(words) > 1 else None
        return (number.group(1), None, "--loop" in args) if number else None
    if name != "gh":
        return None
    if args[:1] == ["api"]:
        words = guard.plain_args(args[1:], API_VALUES)
        merge = MERGE_PATH.search(words[0]) if words else None
        if merge is None or api_method(args[1:]) != "PUT":
            return None
        repo = REPO_PATH.match(words[0])
        return merge.group(1), f"{repo.group(1)}/{repo.group(2)}" if repo else None, False
    words = guard.plain_args(args, MERGE_VALUES)
    if words[:2] != ["pr", "merge"]:
        return None
    selector = words[2] if len(words) > 2 else ""
    number = PR_NUMBER.match(selector)
    return number.group(1) if number else selector, guard.option_value(args, ("-R", "--repo")), False


def starts_land(text: str) -> bool:
    return text.split()[:1] == [LAND_COMMAND]


def land_launch(args: list[str], part, before) -> bool:
    """Whether a `claude` part starts a session (--bg, -p or --print) whose prompt starts with /harness:land: one of
    its arguments, its heredocs or here-strings, or the words of the command piped into it."""
    if not LAUNCH_FLAGS.intersection(args):
        return False
    texts = args + part.heredocs + part.inputs
    if before is not None and before.sep == "|":
        texts += before.words[1:] + before.heredocs
    return any(starts_land(text) for text in texts)


# ---------- the labels ----------


def gh_object(args: list[str], cwd: str, deadline: float) -> dict:
    left = deadline - time.monotonic()
    if left <= 0:
        raise Unread(f"no time left for gh {' '.join(args[:3])}")
    try:
        data = gh_json(args, cwd, timeout=min(GH_TIMEOUT, left))
    except Problem as err:
        raise Unread(str(err)) from None
    if not isinstance(data, dict):
        raise Unread(f"gh {' '.join(args[:3])} printed no object")
    return data


def labelled(labels) -> bool:
    return isinstance(labels, list) and any(isinstance(x, dict) and x.get("name") == MANUAL_LAND for x in labels)


def gated(selector: str, repo, cwd: str, deadline: float) -> tuple[int, bool]:
    """(the PR's number, whether `manual-land` is on it or on a ticket it closes), read from gh in `cwd`; Unread
    when gh cannot tell."""
    where = ["-R", repo] if repo else []
    view = gh_object(["pr", "view", *([selector] if selector else []), "--json", PR_FIELDS, *where], cwd, deadline)
    number = int(selector) if selector.isdigit() else view.get("number")
    if not isinstance(number, int):
        raise Unread(f"gh pr view {selector or '(the current branch)'} named no pull request")
    if labelled(view.get("labels")):
        return number, True
    refs = view.get("closingIssuesReferences")
    for ref in refs if isinstance(refs, list) else []:
        ticket = ref.get("number") if isinstance(ref, dict) else None
        if isinstance(ticket, int):
            issue = gh_object(["issue", "view", str(ticket), "--json", "labels", *where], cwd, deadline)
            if labelled(issue.get("labels")):
                return number, True
    return number, False


# ---------- the owner's prompt ----------


def entry_text(message) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def newest_human(path) -> str:
    """The text of the newest transcript entry whose origin is {"kind": "human"}, "" when there is none; Unread
    when the file cannot be read. A line that is not a JSON object (half-written, malformed) is skipped."""
    if not isinstance(path, str) or not path:
        raise Unread("the event names no transcript")
    text = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"human"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                origin = entry.get("origin") if isinstance(entry, dict) else None
                if isinstance(origin, dict) and origin.get("kind") == "human":
                    text = entry_text(entry.get("message"))
    except OSError as err:
        raise Unread(f"cannot read the transcript: {err.strerror or err}") from None
    return text


def lands(text: str, number: int) -> bool:
    """Whether `text` is the /harness:land command whose arguments start with PR `number`."""
    name, args = COMMAND_NAME.search(text), COMMAND_ARGS.search(text)
    if name is None or name.group(1) != LAND_COMMAND or args is None:
        return False
    first = args.group(1).split()[:1]
    given = PR_NUMBER.match(first[0]) if first else None
    return given is not None and int(given.group(1)) == number


# ---------- the decision ----------


def refused(number: int, why: str) -> str:
    return (
        f"PR #{number} is manual-land: only the owner's own /harness:land {number}, typed in the main session, "
        f"lands it, and {why}. Leave it ready in review and report that it waits for /harness:land {number}."
    )


def refusal(data: dict, command: str, notes: list) -> str | None:
    """The deny reason for `command`, or None; a land let through unjudged adds its line to `notes`."""
    guard = branch_guard()
    cwd = hook_cwd(data)
    state = {"cwd": cwd}
    deadline = time.monotonic() + BUDGET
    prompt, before = [], None  # prompt: the newest human entry's text, once read
    for part, name, args in guard.walk(state, command):
        here = state["cwd"] if state["cwd"] and os.path.isdir(state["cwd"]) else cwd
        if name == "claude" and land_launch(args, part, before):
            return LAUNCH_REASON
        before = part
        found = landing(guard, name, args)
        if found is None:
            continue
        selector, repo, loop = found
        what = f"PR #{selector}" if selector.isdigit() else f"the PR `{guard.shown(part.text)}` merges"
        try:
            number, manual = gated(selector, repo, here, deadline)
            if not manual:
                continue
            what = f"PR #{number}"
            if data.get("agent_id"):
                return refused(number, f"this call comes from a subagent ({data.get('agent_type') or 'unnamed'})")
            if not prompt:
                prompt.append(newest_human(data.get("transcript_path")))
        except Unread as err:
            if loop:
                return (
                    f"Cannot tell whether {what} is manual-land and the owner asked for it ({err}), so this --loop "
                    f"land is refused: harness-pr land --loop refuses a manual-land PR anyway. Retry it when gh "
                    f"answers, or leave the PR for the owner's /harness:land {selector}."
                )
            notes.append(f"cannot tell whether {what} is manual-land and the owner asked for it ({err}); "
                         "the land was let through unchecked")
            continue
        if not lands(prompt[0], number):
            return refused(number, f"the newest prompt the owner typed is not /harness:land {number}")
    return None


def main() -> int:
    data = read_input()
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") != "Bash" or not isinstance(command, str) or not LANDING_WORD.search(command):
        return 0
    notes: list = []
    reason = refusal(data, command, notes)
    if reason:
        deny(reason)
        return 0
    for note in notes:
        sys.stderr.write(f"land_owner.py: {note}\n")
    return 0


if __name__ == "__main__":
    # fail open: a broken hook must never trap the agent
    sys.exit(run_fail_open("land_owner.py", main))
