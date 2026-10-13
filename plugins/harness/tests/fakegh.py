#!/usr/bin/env python3
"""A stand-in for `gh` used by the harness tests.

Put this file on PATH as `gh` (copy or symlink) or point HARNESS_GH at it. Behaviour is
driven by a scenario JSON file named by FAKEGH_SCENARIO:

    {"calls": [
        {"match": ["issue", "view", "12"], "stdout": "...", "exit": 0},
        {"match": ["api", "-X", "POST"], "responses": [{"stderr": "boom", "exit": 1}, {"stdout": "{}"}]},
        {"contains": ["label", "create"], "stdout_json": {"ok": true}}
     ],
     "default": {"stdout": "", "exit": 0}}

Matching rules, first hit wins:
- "match": the argv after `gh` starts with these tokens.
- "contains": every token appears somewhere in argv (order free), for commands with interleaved options.
- "stdin_contains": the text must also appear in stdin.
A call entry gives one response ("stdout" or "stdout_json", "stderr", "exit") or a list under
"responses" consumed in order across invocations, repeating the last one. Consumption state is
kept in <scenario>.state.json. Without a match and without "default" the fake prints
`fakegh: no match for: gh ...` on stderr and exits 1.

Every invocation appends one JSON line {"argv": [...], "stdin": "..."} to the file named by
FAKEGH_LOG, so tests can assert on the exact calls made.
"""
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    stdin = read_stdin()
    log = os.environ.get("FAKEGH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
    scenario_path = os.environ.get("FAKEGH_SCENARIO")
    scenario = {"calls": []}
    if scenario_path and os.path.exists(scenario_path):
        with open(scenario_path, encoding="utf-8") as fh:
            scenario = json.load(fh)
    state_path = (scenario_path or "fakegh") + ".state.json"
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    for index, call in enumerate(scenario.get("calls", [])):
        prefix = call.get("match")
        if prefix is not None and argv[: len(prefix)] != list(prefix):
            continue
        needed = call.get("contains")
        if needed is not None and not all(tok in argv for tok in needed):
            continue
        piece = call.get("stdin_contains")
        if piece is not None and piece not in stdin:
            continue
        responses = call.get("responses")
        if responses:
            n = int(state.get(str(index), 0))
            response = responses[min(n, len(responses) - 1)]
            state[str(index)] = n + 1
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        else:
            response = call
        return respond(response)
    if "default" in scenario:
        return respond(scenario["default"])
    sys.stderr.write("fakegh: no match for: gh " + " ".join(argv) + "\n")
    return 1


def read_stdin() -> str:
    """Read stdin only when something is actually there, so an open pipe never blocks the fake."""
    try:
        if sys.stdin.isatty():
            return ""
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        if not ready:
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def respond(response: dict) -> int:
    if "stdout_json" in response:
        sys.stdout.write(json.dumps(response["stdout_json"]))
    else:
        sys.stdout.write(response.get("stdout", ""))
    if response.get("stderr"):
        sys.stderr.write(response["stderr"])
    sys.stdout.flush()
    return int(response.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
