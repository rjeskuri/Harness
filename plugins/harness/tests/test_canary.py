"""Tests for the Harness canary: scripts/smoke.sh (which runs scripts/canary.py).

    python3 -m unittest discover -s plugins/harness/tests -v

The canary runs as a subprocess, the way the auto-land runs it: `sh scripts/smoke.sh [--plugin DIR]`, from an
empty working directory, with a fresh empty HOME and TMPDIR per run. Each failure case runs it on a copy of
plugins/harness in a temporary directory with one or more files changed; the real checkout is never edited.

The contract these tests read from the ticket (#154):
  - exit 0 when every check passes, 1 on a big failure, 2 when only a reporting tool is broken; a big failure
    wins over a degraded one; a --plugin that does not exist is a big failure;
  - one line per check on stdout; a passing check's line starts with "ok", and names what it checked (the hook
    file or the tool); a failing check's line does not start with "ok" and names the file, hook, case or tool;
  - the hooks and tools are run, not compared with anything: a copy with harmless edits passes, a lib module
    that breaks at import fails the tools that load it, and a hook that exits non-zero, prints a traceback,
    times out or answers wrongly fails its case while the other cases of the same hook still pass;
  - it writes nothing outside its temporary directories: the plugin it checks, HOME and the working directory
    are unchanged, and so is `git status` of this checkout.

Reading files cannot stand in for running them. Each run of this module writes its breaks afresh: every name a
break uses (the module, the function, the word it compares) is spelled by an expression with a random encoding
and key, exit codes and sleeps are random, and several breaks leave the hook or tool file untouched (hookio.py's
deny becomes a no-op, gh.py exits when run as a harness-* tool, readonly_bash's guarded agent set loses the
reviewer). The copy with harmless edits carries the same shapes with the opposite outcome (the same guard behind
a condition that is true, the same exit behind one that is false, a 6-7 s sleep inside the 10 s a hook gets) and
the literal text of earlier fixtures in dead code, so matching text fails that copy. A hook that sleeps 11-12 s
must time out: with the 6-7 s sleep passing, the limit is 10 s, not 5 or 15. When /usr/bin/python3 is older than
3.10, a lib module with a `match` statement must fail the compile (the 3.9 floor), although the python3 on PATH
may accept it.

A hook's answer is read as Claude Code reads it: stdout parsed as JSON, a PreToolUse decision at
hookSpecificOutput.permissionDecision (a deny printed inside a line of text is not JSON, so it is no decision and
the call is allowed), the brief at hookSpecificOutput.additionalContext of a SessionStart answer, the bell at
terminalSequence. The harmless copy gives right answers in other bytes: branch_guard's deny and notify's bell with
compact separators and a trailing newline, a brief of session_brief's own, and from tests_first on main an allow
whose reason holds the words permissionDecision and deny. A copy whose hooks exit 0 with the wrong answer fails
each case: tests_first denies on main, notify's terminalSequence is not the bell (elsewhere, the bell under another
key), session_brief's SessionStart JSON has no additionalContext, and branch_guard's refusals are the deny JSON
inside a line of text, so they have become allows while its allow cases still pass.

Which check a line belongs to is read only from the names on it. A check that fails takes its ok line away, so
the tests compare how many ok lines name a hook or tool with the same count on a copy with harmless edits; that
way the tests do not care whether the compile step prints one line per file or one for all of them.

A canary run costs tens of seconds on macOS (each hook and tool is a fresh python3), so every scenario runs once
for the module: the real checkout alone first (its time is the ticket's 120 s budget), then the plugin copies
in parallel. Each test asserts on its scenario's result.

The temp HOME and TMPDIR the ticket gives the hooks show in one place: after_edit writes its session state under
$TMPDIR/claude-harness/, so that directory must not appear at the top of the TMPDIR the canary itself was given.
The canary's own temporary directory goes under that TMPDIR too, and it removes it: the TMPDIR is empty again
after every run.

Files the Mac leaves in a plugin are not part of it: a copy with a Finder .DS_Store and AppleDouble ._ files
(binary, null bytes, which python3 refuses to compile or run) in bin/, hooks/ and lib/ passes the same checks as
the checkout.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(PLUGIN))
SMOKE = os.path.join(REPO, "scripts", "smoke.sh")
BIN_TOOLS = sorted(name for name in os.listdir(os.path.join(PLUGIN, "bin")) if name.startswith("harness-"))
HOOKS = sorted(name for name in os.listdir(os.path.join(PLUGIN, "hooks")) if name.endswith(".py"))
CORE_TOOLS = ("harness-pr", "harness-board", "harness-work", "harness-red")
# The hook cases the ticket lists: branch_guard 4, readonly_bash, tests_first, session_brief, notify, after_edit,
# verify_on_stop 1 each.
HOOK_CASES = 10
RUN_TIMEOUT = 600
BUDGET = 120

FUTURE = "from __future__ import annotations\n"
MAIN = "def main() -> int:\n"
DUNDER_MAIN = 'if __name__ == "__main__":\n'
SYNTAX_ERROR = "\ndef broken(:\n    pass\n"
COMMENT = "\n# canary test: a comment that changes nothing\n"
HOOK_TIMEOUT = 10
SYSTEM_PYTHON = "/usr/bin/python3"

_RNG = random.SystemRandom()


def token():
    return "".join(_RNG.choice(string.ascii_lowercase) for _ in range(8))


def ident():
    return "_k" + token()


def spell(word):
    """A Python expression whose value is the string `word` and whose text does not contain it: one of three
    encodings, picked at random, with a random key."""
    var, how = ident(), _RNG.randrange(3)
    if how == 0:
        key = _RNG.randint(1, 40)
        codes = ", ".join(str(ord(c) + key) for c in word)
        return f'"".join(chr({var} - {key}) for {var} in ({codes},))'
    if how == 1:
        key = _RNG.randint(1, 127)
        codes = ", ".join(str(ord(c) ^ key) for c in word)
        return f'"".join(chr({var} ^ {key}) for {var} in ({codes},))'
    return f'bytes.fromhex("{word[::-1].encode().hex()}").decode()[::-1]'


def truthy():
    word = token()
    return f"{spell(word)} == {spell(word)}"


def falsy():
    first, second = token(), token()
    while second == first:
        second = token()
    return f"{spell(first)} == {spell(second)}"


def call(module, func, *args):
    """The statement `module.func(*args)`, with neither name in its text."""
    return f"getattr(__import__({spell(module)}), {spell(func)})({', '.join(repr(a) for a in args)})"


def exit_code():
    return _RNG.randint(3, 120)


def bad_json():
    return "{" + token()


def exit_when_run_as(*names):
    """Module-level code that exits, with a random code and no message, when the program's file name is exactly
    one of `names`."""
    mod = ident()
    spelled = "".join(f"{spell(name)}, " for name in names)
    return (
        f"\n{mod} = __import__({spell('sys')})\n"
        f"if (getattr({mod}, {spell('argv')}) or [''])[0].rsplit('/', 1)[-1] in ({spelled}):\n"
        f"    getattr({mod}, {spell('exit')})({exit_code()})\n"
    )


def noop_global(name):
    """Module-level code that rebinds the global `name` to a function that does nothing."""
    return f"\nglobals()[{spell(name)}] = lambda *_a, **_k: None\n"


def without_agent(agent):
    """readonly_bash.py code that drops `agent` from GUARDED, the set of agents it holds to reads."""
    return f"GUARDED = {{_g for _g in GUARDED if _g != {spell(agent)}}}\n"


BELL = "\u0007"


def dict_expr(pairs):
    """A Python dict display whose keys are spelled words and whose values are expressions."""
    return "{" + ", ".join(f"{spell(key)}: {value}" for key, value in pairs) + "}"


def dumps(expr, compact=False):
    """The expression json.dumps(expr), neither name in its text; `compact` drops the spaces after , and :."""
    tail = ', separators=(",", ":")' if compact else ""
    return f"getattr(__import__({spell('json')}), {spell('dumps')})({expr}{tail})"


def specific_output(event, pairs):
    """The dict expression {"hookSpecificOutput": {"hookEventName": event, **pairs}}, every key spelled."""
    return dict_expr([("hookSpecificOutput", dict_expr([("hookEventName", spell(event))] + list(pairs)))])


def printed(expr, compact=False, newline=True):
    """A statement writing json.dumps(expr) to stdout, with or without a trailing newline."""
    end = "" if newline else ', end=""'
    return f"print({dumps(expr, compact)}{end})"


def answer_and_return(expr, compact=False, newline=True):
    """Code for the top of main(): print the JSON `expr` and exit 0 without doing anything else."""
    return f"    {printed(expr, compact, newline)}\n    return None\n"


def deny_as(printer):
    """Module-level code rebinding a hook's `deny` to a lambda of one argument, the reason, whose body is
    `printer(reason variable)`, so every refusal the hook makes is printed that way instead."""
    reason = ident()
    return f"\nglobals()[{spell('deny')}] = lambda {reason}: {printer(reason)}\n"


def deny_json(reason):
    """The deny decision as hookio.py builds it, keys spelled, `reason` an expression."""
    return specific_output(
        "PreToolUse", [("permissionDecision", spell("deny")), ("permissionDecisionReason", reason)]
    )


# The literal text of earlier fixtures, in code that never runs: a copy carrying it must still pass.
LITERAL_ALLOW_ALL = MAIN + "    return 0\n"
DEAD_LITERALS = (
    "def _canary_unused():\n"
    "    if False:\n"
    "        raise SystemExit(3)\n"
    "    time.sleep(60)\n"
    "    traceback.print_exc()\n"
    '    raise RuntimeError("canary test: never raised")\n'
    "\n\n"
)
DEAD_DENY = 'def _canary_never_called():\n    deny("never called")\n    return 0\n\n\n'
DEAD_MAIN_TEXT = '_CANARY_TEXT = """\n' + LITERAL_ALLOW_ALL + '"""\n\n\n'


def first_in_main(code):
    """An edit that runs `code` (indented four spaces) at the top of main()."""
    return (MAIN, MAIN + code)


def before_dunder_main(code):
    """An edit that runs `code` at module level, after every definition and before the __main__ block."""
    return (DUNDER_MAIN, code + DUNDER_MAIN)


_RESULTS: dict = {}
_TEMP: list = []


class Run:
    """One canary run: its exit code, output, time, and the state of what it must not write to."""

    def __init__(self, code, stdout, stderr, seconds, untouched, tmp):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.seconds = seconds
        self.untouched = untouched  # {what: (before, after)}
        self.tmp = tmp  # the TMPDIR the canary was given

    @property
    def output(self):
        return self.stdout + "\n" + self.stderr

    def ok_lines(self):
        return [line.strip() for line in self.stdout.splitlines() if re.match(r"\s*ok\b", line, re.IGNORECASE)]

    def problem_lines(self):
        """Every line of stdout and stderr that is not an ok line."""
        return [
            line.strip()
            for line in self.output.splitlines()
            if line.strip() and not re.match(r"\s*ok\b", line, re.IGNORECASE)
        ]

    def failing_lines(self):
        """The lines on stdout, where the check lines go, that are not ok lines. A hook's own stderr (a traceback
        names the hook's file) cannot stand in for the canary saying the hook failed."""
        return [
            line.strip()
            for line in self.stdout.splitlines()
            if line.strip() and not re.match(r"\s*ok\b", line, re.IGNORECASE)
        ]

    def ok_count(self, name):
        return sum(1 for line in self.ok_lines() if names(line, name))

    def describe(self):
        return f"exit {self.code} in {self.seconds:.1f}s\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def names(text, name):
    """`name` appears in `text` as a whole name: harness-pr does not match inside harness-projects."""
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def stem(hook):
    return hook[: -len(".py")]


def snapshot(root):
    """Every directory and file under root, with each file's mode and content hash."""
    state = {}
    for folder, dirs, files in os.walk(root):
        rel = os.path.relpath(folder, root)
        state[rel + "/"] = "dir"
        for name in files:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                state[os.path.join(rel, name)] = "link:" + os.readlink(path)
                continue
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            state[os.path.join(rel, name)] = (os.stat(path).st_mode, digest)
    return state


def git_status():
    return subprocess.run(
        ["git", "-C", REPO, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def scratch(prefix):
    path = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
    _TEMP.append(path)
    return path


def canary_env(home, tmp):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HARNESS_", "CLAUDE_", "GIT_")) and key not in ("PYTHONDONTWRITEBYTECODE",)
    }
    env["HOME"] = home
    env["TMPDIR"] = tmp
    return env


def run_canary(plugin=None, watch_repo=False):
    """Run scripts/smoke.sh, on `plugin` when given, from an empty directory with an empty HOME."""
    home, cwd, tmp = scratch("canary-home-"), scratch("canary-cwd-"), scratch("canary-tmp-")
    watched = {"HOME": home, "working directory": cwd}
    if plugin is not None:
        watched["the plugin copy"] = plugin
    before = {what: snapshot(path) for what, path in watched.items()}
    if watch_repo:
        before["git status of this checkout"] = git_status()
    args = ["sh", SMOKE] + (["--plugin", plugin] if plugin is not None else [])
    start = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            env=canary_env(home, tmp),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr = "timeout", str(exc.stdout or ""), str(exc.stderr or "")
    seconds = time.monotonic() - start
    after = {what: snapshot(path) for what, path in watched.items()}
    if watch_repo:
        after["git status of this checkout"] = git_status()
    return Run(code, stdout, stderr, seconds, {what: (before[what], after[what]) for what in before}, tmp)


def plugin_copy(edits):
    """A copy of plugins/harness with `edits` applied: {relative path: edit or [edits] or bytes}, where an edit
    is (old text, new text) replacing the one occurrence of old, or ("", text) appending text, and bytes are the
    whole content of a new file."""
    root = os.path.join(scratch("canary-plugin-"), "harness")
    shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), symlinks=True)
    for rel, change in edits.items():
        path = os.path.join(root, rel)
        if isinstance(change, bytes):
            if os.path.exists(path):
                raise AssertionError(f"test fixture: {rel} already exists")
            with open(path, "wb") as fh:
                fh.write(change)
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for old, new in change if isinstance(change, list) else [change]:
            if old:
                if text.count(old) != 1:
                    raise AssertionError(f"test fixture: {rel} no longer holds exactly one {old!r}")
                text = text.replace(old, new)
            else:
                text += new
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def broken_tool(name):
    """The tool exits at load with a random code and no message."""
    return {os.path.join("bin", name): (FUTURE, FUTURE + call("sys", "exit", exit_code()) + "\n")}


def system_python_below_310():
    if not os.access(SYSTEM_PYTHON, os.X_OK):
        return False
    proc = subprocess.run(
        [SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] < (3, 10))"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "True"


FLOOR_APPLIES = system_python_below_310()


def finder_file(magic):
    """Binary content as the Finder writes it: its magic header, null bytes, random bytes."""
    return magic + b"\x00" * _RNG.randint(8, 64) + os.urandom(_RNG.randint(64, 512)) + b"\x00\x00"


DS_STORE = b"\x00\x00\x00\x01Bud1"
APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        "
# A .DS_Store in each folder the canary walks, and the AppleDouble ._ companion of a tool, a hook and a lib
# module (the hook's and the module's names end in .py).
FINDER_FILES = {
    "bin/.DS_Store": finder_file(DS_STORE),
    "bin/._harness-pr": finder_file(APPLE_DOUBLE),
    "hooks/.DS_Store": finder_file(DS_STORE),
    "hooks/._branch_guard.py": finder_file(APPLE_DOUBLE),
    "lib/.DS_Store": finder_file(DS_STORE),
    "lib/harness_lib/.DS_Store": finder_file(DS_STORE),
    "lib/harness_lib/._cli.py": finder_file(APPLE_DOUBLE),
}
EDIT_CHECK = "        check_edit(data, cwd)\n"
BASH_BRANCH = '    elif tool == "Bash":\n'

# Hooks broken while running, each a different way, in one copy; each hook has one case here except
# branch_guard, whose Bash branch is behind a false condition so it allows `git commit` but still refuses the
# Edit of a source file.
RUNTIME_BREAKS = {
    # a non-zero exit (SystemExit is not caught by the fail-open wrapper), random code, no message
    "tests_first": first_in_main(f"    {call('sys', 'exit', exit_code())}\n"),
    # exits 0 with no output
    "session_brief": first_in_main(f"    if {truthy()}:\n        return None\n"),
    # exits 0 without the bell JSON
    "notify": first_in_main(f"    if {truthy()}:\n        return None\n"),
    # does its job and exits 0, but prints a traceback first
    "verify_on_stop": first_in_main(
        f"    try:\n        {call('json', 'loads', bad_json())}\n"
        f"    except ValueError:\n        {call('traceback', 'print_exc')}\n"
    ),
    # outlives the 10 s every hook gets, by one or two seconds only
    "after_edit": first_in_main(f"    {call('time', 'sleep', round(_RNG.uniform(11, 12), 1))}\n"),
    # a module-level error outside the fail-open wrapper: a traceback and exit 1
    "readonly_bash": before_dunder_main(call("json", "loads", bad_json()) + "\n"),
}

# Hooks that exit 0 with the wrong answer on stdout, one case each; branch_guard joins them in the scenario with
# its deny rebound to a line of text around the deny JSON (not JSON: Claude Code reads no decision from it).
WRONG_ANSWERS = {
    # denies on main, by a spelled name
    "tests_first": first_in_main(f"    globals()[{spell('deny')}]({spell(token())})\n    return None\n"),
    # JSON with the right key and a sequence that is not the bell
    "notify": first_in_main(answer_and_return(dict_expr([("terminalSequence", spell(token()))]), newline=False)),
    # SessionStart JSON with a title and no additionalContext: no brief reaches the session
    "session_brief": first_in_main(
        answer_and_return(specific_output("SessionStart", [("sessionTitle", spell(token()))]), newline=False)
    ),
}


def text_deny(reason):
    return f"print({spell(token())}, {dumps(deny_json(reason))})"


def compact_deny(reason):
    return printed(deny_json(reason), compact=True)


# An allow decision whose reason holds the words a text match for a deny would find.
ALLOW_MENTIONING_DENY = specific_output(
    "PreToolUse",
    [
        ("permissionDecision", spell("allow")),
        ("permissionDecisionReason", spell(f"{token()} permissionDecision deny {token()}")),
    ],
)

SCENARIOS = {
    # Byte-for-byte different from the checkout, the same behaviour: each break's shape with the opposite
    # outcome, and the literal text of earlier fixtures in code that never runs.
    "harmless edits": {
        "hooks/branch_guard.py": [
            ("", COMMENT),
            first_in_main(
                "    _canary = len('no-op')  # noqa\n"
                f"    if {falsy()}:\n"
                f"        globals()[{spell('deny')}]({spell(token())})\n"
                "        return None\n"
            ),
            (EDIT_CHECK, f"        if {truthy()}:\n    {EDIT_CHECK}"),
            (BASH_BRANCH, f'    elif tool == "Bash" and {truthy()}:\n'),
            before_dunder_main(DEAD_LITERALS + DEAD_DENY + DEAD_MAIN_TEXT),
            # the same deny decision in other bytes: compact separators and a trailing newline
            before_dunder_main(deny_as(compact_deny)),
        ],
        "hooks/notify.py": [
            ("", COMMENT),
            first_in_main(f"    if {falsy()}:\n        return None\n"),
            # the bell itself, by this hook's own hand: compact and with a trailing newline
            first_in_main(
                f"    if {truthy()}:\n"
                f"        {printed(dict_expr([('terminalSequence', repr(BELL))]), compact=True)}\n"
                "        return None\n"
            ),
        ],
        "hooks/session_brief.py": [
            ("", COMMENT),
            first_in_main(f"    if {falsy()}:\n        return None\n"),
            # a brief of its own: SessionStart JSON with a non-empty additionalContext, nothing else
            first_in_main(
                answer_and_return(
                    specific_output("SessionStart", [("additionalContext", spell("## " + token()))]), newline=False
                )
            ),
        ],
        "hooks/tests_first.py": [
            first_in_main(
                "    _canary = len('no-op')  # noqa\n"
                "    if False:\n        raise SystemExit(3)\n"
                f"    if {falsy()}:\n        {call('sys', 'exit', exit_code())}\n"
                f"    if {falsy()}:\n        globals()[{spell('deny')}]({spell(token())})\n"
                # on main nothing else is printed: one allow decision whose reason mentions a deny
                f"    {printed(ALLOW_MENTIONING_DENY, newline=False)}\n"
            ),
            before_dunder_main(DEAD_LITERALS + DEAD_MAIN_TEXT),
        ],
        "hooks/after_edit.py": [
            # inside the 10 s a hook gets, and over a 5 s limit
            first_in_main(f"    {call('time', 'sleep', round(_RNG.uniform(6, 7), 1))}\n"),
            before_dunder_main(DEAD_LITERALS),
        ],
        "hooks/verify_on_stop.py": [
            first_in_main(
                f"    try:\n        {call('json', 'loads', '{}')}\n"
                f"    except ValueError:\n        {call('traceback', 'print_exc')}\n"
            ),
            before_dunder_main(DEAD_LITERALS),
        ],
        "hooks/readonly_bash.py": [
            (FUTURE, FUTURE + COMMENT),
            before_dunder_main(
                call("json", "loads", "{}") + "\n" + without_agent("harness:" + token()) + "\n\n"
                + DEAD_LITERALS + DEAD_DENY + DEAD_MAIN_TEXT
            ),
        ],
        "lib/harness_lib/hookio.py": ("", noop_global("_canary_" + token())),
        "lib/harness_lib/gh.py": ("", exit_when_run_as("not-harness-")),
        "bin/harness-digest": ("", COMMENT + '_CANARY_NOTE = "canary test: this tool is broken"\n'),
        "bin/harness-pr": (FUTURE, FUTURE + f"if {falsy()}:\n    {call('sys', 'exit', exit_code())}\n"),
        "lib/harness_lib/cli.py": ("", COMMENT + "_CANARY = 'not broken at import'\n"),
    },
    # Files the Mac leaves behind, binary with null bytes: not part of the plugin, nothing changes.
    "Finder files": dict(FINDER_FILES),
    "syntax error in a hook and a lib module": {
        "hooks/after_edit.py": ("", SYNTAX_ERROR),
        "lib/harness_lib/registry.py": ("", SYNTAX_ERROR),
    },
    "guard allows everything": {
        "hooks/branch_guard.py": first_in_main(
            f"    if {truthy()}:\n        return {_RNG.choice(['0', 'None'])}\n"
        )
    },
    "broken reporting tools": dict(broken_tool("harness-digest"), **broken_tool("harness-standup")),
    "broken harness-pr": broken_tool("harness-pr"),
    "syntax error in a reporting tool": {"bin/harness-projects": ("", SYNTAX_ERROR)},
    # Through data the hooks read: readonly_bash no longer holds the reviewer to reads, branch_guard refuses
    # everything through its own deny, called by a spelled name, and notify rings the bell under another key.
    "many failures": dict(
        broken_tool("harness-red"),
        **broken_tool("harness-worklog"),
        **{
            "hooks/branch_guard.py": first_in_main(f"    globals()[{spell('deny')}]({spell(token())})\n    return None\n"),
            "hooks/readonly_bash.py": before_dunder_main(without_agent("harness:reviewer") + "\n\n"),
            "hooks/notify.py": first_in_main(answer_and_return(dict_expr([(token(), repr(BELL))]), newline=False)),
        },
    ),
    # Exit 0 everywhere, the wrong answer on stdout: see WRONG_ANSWERS; branch_guard's deny cases become allows.
    "hooks answer wrongly": dict(
        {f"hooks/{name}.py": edit for name, edit in WRONG_ANSWERS.items()},
        **{"hooks/branch_guard.py": before_dunder_main(deny_as(text_deny))},
    ),
    # cli.py still compiles; every tool imports it, so every tool's --help fails. branch_guard (which does not
    # load cli.py) skips its Edit check behind a false condition but still refuses `git commit`.
    "lib module broken at import": {
        "lib/harness_lib/cli.py": ("", "\n" + call("json", "loads", bad_json()) + "\n"),
        "hooks/branch_guard.py": (EDIT_CHECK, f"        if {falsy()}:\n    {EDIT_CHECK}"),
    },
    # Every hook file unchanged: hookio.py's deny does nothing, so every refusal becomes an allow.
    "hookio broken at runtime": {"lib/harness_lib/hookio.py": ("", noop_global("deny"))},
    # Every tool file unchanged: gh.py exits, no message, when loaded by harness-digest or harness-standup.
    # harness-board, harness-pr and harness-work load gh.py too and must be unaffected, as must session_brief.py,
    # which loads it as a hook.
    "reporting lib broken for its tools": {
        "lib/harness_lib/gh.py": ("", exit_when_run_as("harness-digest", "harness-standup"))
    },
    "hooks broken at runtime": dict(
        {f"hooks/{name}.py": edit for name, edit in RUNTIME_BREAKS.items()},
        **{"hooks/branch_guard.py": (BASH_BRANCH, f'    elif tool == "Bash" and {falsy()}:\n')},
    ),
}
if FLOOR_APPLIES:
    # Valid for the python3 on PATH when it is 3.10 or newer, a syntax error for the Mac's 3.9.
    _shape = ident()
    SCENARIOS["3.10 syntax in a lib module"] = {
        "lib/harness_lib/text.py": (
            "",
            f"\n\ndef {_shape}(value):\n    match value:\n        case _:\n            return value\n",
        )
    }


def setUpModule():
    _RESULTS["checkout"] = run_canary(watch_repo=True)
    copies = {name: plugin_copy(edits) for name, edits in SCENARIOS.items()}
    copies["missing plugin"] = os.path.join(scratch("canary-missing-"), "harness")
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = {name: pool.submit(run_canary, path) for name, path in copies.items()}
        for name, future in futures.items():
            _RESULTS[name] = future.result()
    _RESULTS["missing plugin"].plugin = copies["missing plugin"]


def tearDownModule():
    for path in _TEMP:
        shutil.rmtree(path, ignore_errors=True)


class Canary(unittest.TestCase):
    maxDiff = None

    def result(self, name):
        return _RESULTS[name]

    def assert_exit(self, run, code):
        self.assertEqual(run.code, code, run.describe())

    def assert_named_as_problem(self, run, *needles):
        problems = "\n".join(run.problem_lines())
        for needle in needles:
            self.assertTrue(names(problems, needle), f"no failing line names {needle!r}\n{run.describe()}")

    def assert_fewer_ok(self, run, name, failed):
        """`failed` of the checks naming `name` fail in `run`: that many fewer ok lines name it than on the copy
        with harmless edits."""
        base = self.result("harmless edits")
        self.assertEqual(
            run.ok_count(name),
            base.ok_count(name) - failed,
            f"ok lines naming {name}: expected {failed} fewer than the harmless copy's\n"
            f"--- harmless copy\n{base.stdout}\n--- this run\n{run.describe()}",
        )

    def assert_guard_cases(self, run, failing, passing):
        """branch_guard's cases, each told by text on its line: `failing` on a failing line, `passing` on an ok
        line; an Edit case is the source-file one when its line does not name docs/agent."""

        def edit_of_source(line):
            return "Edit" in line and "docs/agent" not in line

        def has(lines, case):
            lines = [line for line in lines if "branch_guard" in line]
            return any(edit_of_source(line) if case == "Edit" else case in line for line in lines)

        for case in failing:
            self.assertTrue(has(run.failing_lines(), case), f"branch_guard {case} should fail\n{run.describe()}")
            self.assertFalse(has(run.ok_lines(), case), f"branch_guard {case} passed\n{run.describe()}")
        for case in passing:
            self.assertTrue(has(run.ok_lines(), case), f"branch_guard {case} should pass\n{run.describe()}")
            self.assertFalse(has(run.failing_lines(), case), f"branch_guard {case} failed\n{run.describe()}")
        self.assert_fewer_ok(run, "branch_guard", len(failing))

    def assert_passes_everything(self, run):
        self.assert_exit(run, 0)
        ok = run.ok_lines()
        self.assertEqual(len(ok), len(set(ok)), f"an ok line repeats\n{run.describe()}")
        # one per hook case, one per tool's --help, and at least one compile check
        self.assertGreaterEqual(len(ok), HOOK_CASES + len(BIN_TOOLS) + 1, run.describe())
        joined = "\n".join(ok)
        for tool in BIN_TOOLS:
            self.assertTrue(names(joined, tool), f"no ok line names {tool}\n{run.describe()}")
        for hook in HOOKS:
            self.assertTrue(re.search(re.escape(stem(hook)), joined), f"no ok line names {hook}\n{run.describe()}")
        guard_lines = [line for line in ok if "branch_guard" in line]
        self.assertGreaterEqual(len(guard_lines), 4, f"branch_guard has four cases\n{run.describe()}")
        self.assertNotIn("Traceback", run.output, run.describe())
        self.assertFalse(run.failing_lines(), f"a check failed\n{run.describe()}")

    # ---------- broad: the real checkout and a copy that differs from it without changing behaviour ----------

    def test_passes_on_this_checkout(self):
        run = self.result("checkout")
        self.assert_passes_everything(run)
        self.assertLess(run.seconds, BUDGET, f"over the {BUDGET} s budget\n{run.describe()}")

    def test_a_copy_with_harmless_edits_passes(self):
        # Comments and no-op lines in hooks, tools and a lib module: the canary runs them, it does not compare
        # them with the checkout, so this copy passes every check, the same checks as the checkout.
        run = self.result("harmless edits")
        self.assert_passes_everything(run)
        self.assertEqual(len(run.ok_lines()), len(self.result("checkout").ok_lines()), run.describe())

    def test_files_the_finder_leaves_are_not_checked(self):
        # A .DS_Store and AppleDouble ._ files, null bytes in each, in bin/, hooks/ and lib/: not plugin files, so
        # nothing compiles or runs them, and the copy passes the same checks as the checkout.
        run = self.result("Finder files")
        self.assert_passes_everything(run)
        self.assertEqual(len(run.ok_lines()), len(self.result("checkout").ok_lines()), run.describe())
        for rel in FINDER_FILES:
            with self.subTest(file=rel):
                self.assertNotIn(os.path.basename(rel), run.output, f"the canary looked at {rel}\n{run.describe()}")

    def test_writes_nothing_outside_its_temp_directories(self):
        for name, run in _RESULTS.items():
            # only a run that got as far as its checks proves anything about what it writes
            if name != "missing plugin":
                with self.subTest(scenario=name, ran=True):
                    self.assertIn(run.code, (0, 1, 2), run.describe())
                    self.assertTrue(run.ok_lines(), f"no check ran\n{run.describe()}")
            for what, (before, after) in run.untouched.items():
                with self.subTest(scenario=name, unchanged=what):
                    self.assertEqual(before, after, f"the canary changed {what}\n{run.describe()}")
            # after_edit writes $TMPDIR/claude-harness/<repo hash>/ on an Edit or Bash event: in the hooks' own
            # temp TMPDIR, not at the top of the one the canary was given
            with self.subTest(scenario=name, unchanged="TMPDIR"):
                self.assertFalse(
                    os.path.exists(os.path.join(run.tmp, "claude-harness")),
                    f"the hooks wrote into the canary's TMPDIR: they did not get a temp TMPDIR\n{run.describe()}",
                )
            # the canary's own temporary directory lives under the TMPDIR it was given, and is removed
            with self.subTest(scenario=name, emptied="TMPDIR"):
                self.assertEqual(
                    sorted(os.listdir(run.tmp)), [], f"the canary left files in its TMPDIR\n{run.describe()}"
                )

    def test_a_missing_plugin_is_a_big_failure(self):
        # Exit 1, not 2: the auto-land reads 2 as "only a reporting tool is broken".
        run = self.result("missing plugin")
        self.assert_exit(run, 1)
        self.assertFalse(run.ok_lines(), f"nothing to check, yet a check passed\n{run.describe()}")
        self.assertIn(run.plugin, run.output, f"the missing path is not named\n{run.describe()}")
        self.assertNotIn("Traceback", run.output, run.describe())

    # ---------- big failures: exit 1, naming what failed ----------

    def test_syntax_error_in_a_hook_is_a_big_failure(self):
        run = self.result("syntax error in a hook and a lib module")
        self.assert_exit(run, 1)
        self.assert_named_as_problem(run, "after_edit.py", "registry.py")
        self.assertFalse(
            any(names(line, "after_edit.py") for line in run.ok_lines() if "compil" in line.lower()),
            f"after_edit.py passed a compile check\n{run.describe()}",
        )

    def test_syntax_error_in_a_reporting_tool_is_a_big_failure(self):
        # A file that does not compile is a big failure even for a tool whose --help failing is only degraded.
        run = self.result("syntax error in a reporting tool")
        self.assert_exit(run, 1)
        self.assert_named_as_problem(run, "harness-projects")

    def test_guard_that_allows_everything_is_a_big_failure(self):
        run = self.result("guard allows everything")
        self.assert_exit(run, 1)
        self.assert_guard_cases(run, failing=("git commit", "Edit"), passing=("git status", "docs/agent"))
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")

    def test_guard_cut_in_one_branch_fails_only_those_cases(self):
        # The Bash branch cut: `git commit` is allowed, the Edit of a source file is still refused.
        self.assert_guard_cases(
            self.result("hooks broken at runtime"),
            failing=("git commit",),
            passing=("Edit", "git status", "docs/agent"),
        )
        # The Edit branch cut: the Edit of a source file is allowed, `git commit` is still refused.
        self.assert_guard_cases(
            self.result("lib module broken at import"),
            failing=("Edit",),
            passing=("git commit", "git status", "docs/agent"),
        )

    def test_hooks_are_run_and_each_runtime_failure_is_named(self):
        # A non-zero exit, no output, no bell, a logged traceback, a timeout and a load-time error: each fails
        # its hook's one case, named on a check line, and no other check of the run.
        run = self.result("hooks broken at runtime")
        self.assert_exit(run, 1)
        failing = "\n".join(run.failing_lines())
        for hook in RUNTIME_BREAKS:
            with self.subTest(hook=hook):
                self.assertTrue(names(failing, hook) or names(failing, hook + ".py"), run.describe())
                self.assert_fewer_ok(run, hook, 1)
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")

    def test_a_lib_change_fails_hooks_whose_files_are_unchanged(self):
        # hookio.py's deny does nothing: branch_guard and readonly_bash, unchanged, now allow what they must
        # refuse; the hooks and cases that allow, and every tool, still pass.
        run = self.result("hookio broken at runtime")
        self.assert_exit(run, 1)
        self.assert_guard_cases(run, failing=("git commit", "Edit"), passing=("git status", "docs/agent"))
        failing = "\n".join(run.failing_lines())
        self.assertTrue(names(failing, "readonly_bash") or names(failing, "readonly_bash.py"), run.describe())
        self.assert_fewer_ok(run, "readonly_bash", 1)
        for hook in ("tests_first", "session_brief", "notify", "after_edit", "verify_on_stop"):
            with self.subTest(hook=hook):
                self.assert_fewer_ok(run, hook, 0)
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")

    def test_hook_timeout_is_the_ten_seconds_hooks_get(self):
        # after_edit sleeping 11-12 s fails its case (hooks broken at runtime); sleeping 6-7 s passes (harmless
        # edits, which pass every check): a limit of 5 s fails the second, one of 15 s passes the first. The
        # timed-out run still ends well inside the module's own limit.
        self.assert_fewer_ok(self.result("hooks broken at runtime"), "after_edit", 1)
        harmless, checkout = self.result("harmless edits"), self.result("checkout")
        self.assertEqual(harmless.ok_count("after_edit"), checkout.ok_count("after_edit"), harmless.describe())
        self.assertGreater(harmless.ok_count("after_edit"), 0, harmless.describe())
        self.assertLess(self.result("hooks broken at runtime").seconds, BUDGET + HOOK_TIMEOUT)

    @unittest.skipUnless(FLOOR_APPLIES, f"needs {SYSTEM_PYTHON} older than 3.10")
    def test_compiles_with_the_mac_python_floor(self):
        # A `match` statement compiles under the python3 on PATH (3.10+) but not under /usr/bin/python3: the
        # compile step uses the Mac's python3, the floor the hooks must run on.
        run = self.result("3.10 syntax in a lib module")
        self.assert_exit(run, 1)
        self.assertTrue(names("\n".join(run.failing_lines()), "text.py"), f"text.py not named\n{run.describe()}")
        self.assertFalse(
            any(names(line, "text.py") for line in run.ok_lines() if "compil" in line.lower()),
            f"text.py passed a compile check\n{run.describe()}",
        )

    def test_reporting_lib_broken_for_its_tools_is_degraded(self):
        # gh.py exits with no message when harness-digest or harness-standup loads it; neither tool file changed.
        # session_brief.py loads gh.py too, as a hook, and still passes: the exit is 2.
        run = self.result("reporting lib broken for its tools")
        self.assert_exit(run, 2)
        self.assert_named_as_problem(run, "harness-digest", "harness-standup")
        for tool in ("harness-digest", "harness-standup"):
            self.assert_fewer_ok(run, tool, 1)
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            if tool not in ("harness-digest", "harness-standup"):
                self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")
        for hook in HOOKS:
            with self.subTest(hook=hook):
                self.assert_fewer_ok(run, stem(hook), 0)

    def test_broken_core_tool_is_a_big_failure(self):
        run = self.result("broken harness-pr")
        self.assert_exit(run, 1)
        self.assert_named_as_problem(run, "harness-pr")
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            if tool != "harness-pr":
                self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")
        self.assert_fewer_ok(run, "harness-pr", 1)

    def test_lib_module_broken_at_import_fails_every_tool(self):
        # Every tool loads harness_lib.cli; it compiles, so only running each tool's --help finds it.
        run = self.result("lib module broken at import")
        self.assert_exit(run, 1)
        self.assert_named_as_problem(run, *BIN_TOOLS)
        for tool in BIN_TOOLS:
            with self.subTest(tool=tool):
                self.assert_fewer_ok(run, tool, 1)

    def test_every_failure_is_named_and_the_worst_wins(self):
        # harness-red is core, harness-worklog is reporting; readonly_bash lets a reviewer write, branch_guard
        # refuses what it should allow, and notify prints the bell character under a key that is not
        # terminalSequence. Every one is named, and the exit is the big failure's.
        run = self.result("many failures")
        self.assert_exit(run, 1)
        self.assert_named_as_problem(run, "harness-red", "harness-worklog")
        failing = "\n".join(run.failing_lines())
        self.assertTrue(names(failing, "readonly_bash") or names(failing, "readonly_bash.py"), run.describe())
        self.assert_fewer_ok(run, "readonly_bash", 1)
        self.assertTrue(names(failing, "notify") or names(failing, "notify.py"), run.describe())
        self.assert_fewer_ok(run, "notify", 1)
        self.assert_guard_cases(run, failing=("git status", "docs/agent"), passing=("git commit", "Edit"))

    def test_a_wrong_answer_with_exit_0_fails_its_case(self):
        # Exit 0 everywhere, the answer on stdout read as Claude Code reads it: tests_first denies on main;
        # notify's terminalSequence is not the bell; session_brief's SessionStart JSON carries no
        # additionalContext; branch_guard prints its deny JSON inside a line of text, which is not a decision, so
        # its two refusals have become allows. Each fails its case and names it; every allow case, the hooks
        # left alone and every tool still pass.
        run = self.result("hooks answer wrongly")
        self.assert_exit(run, 1)
        failing = "\n".join(run.failing_lines())
        for hook in WRONG_ANSWERS:
            with self.subTest(hook=hook):
                self.assertTrue(names(failing, hook) or names(failing, hook + ".py"), run.describe())
                self.assert_fewer_ok(run, hook, 1)
        on_main = [line for line in run.failing_lines() if "tests_first" in line]
        self.assertTrue(
            any("main" in line for line in on_main), f"tests_first's case is 'allows on main'\n{run.describe()}"
        )
        self.assert_guard_cases(run, failing=("git commit", "Edit"), passing=("git status", "docs/agent"))
        for hook in ("readonly_bash", "after_edit", "verify_on_stop"):
            with self.subTest(hook=hook):
                self.assert_fewer_ok(run, hook, 0)
        ok = "\n".join(run.ok_lines())
        for tool in BIN_TOOLS:
            self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")

    # ---------- degraded: exit 2 ----------

    def test_broken_reporting_tool_is_degraded(self):
        run = self.result("broken reporting tools")
        self.assert_exit(run, 2)
        self.assert_named_as_problem(run, "harness-digest", "harness-standup")
        ok = "\n".join(run.ok_lines())
        for tool in CORE_TOOLS:
            self.assertTrue(names(ok, tool), f"{tool} should still pass\n{run.describe()}")
        for hook in HOOKS:
            self.assertIn(stem(hook), ok, f"{hook} should still pass\n{run.describe()}")
        for tool in ("harness-digest", "harness-standup"):
            self.assert_fewer_ok(run, tool, 1)


if __name__ == "__main__":
    unittest.main()
