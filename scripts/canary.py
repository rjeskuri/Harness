#!/usr/bin/env python3
"""The Harness canary, run by scripts/smoke.sh: is the plugin every project loads still working?

Usage:
    sh scripts/smoke.sh [--plugin DIR]      # DIR defaults to <repo>/plugins/harness

Three steps, one line per check on stdout; a passing check's line starts with "ok":
  1. Compile, with /usr/bin/python3 when it exists (the Mac's 3.9, the floor the plugin keeps) else python3,
     every .py under hooks/ and lib/ and every file in bin/. compile() only: no bytecode is written.
  2. Run each hook as Claude Code runs it, `python3 <hook>` with the event on stdin and 10 s, in a temporary
     git repository on main with one commit, and read its answer as Claude Code does: stdout parsed as JSON,
     a PreToolUse decision at hookSpecificOutput.permissionDecision (output that is not JSON is no decision).
     branch_guard denies an Edit of a source file and `git commit -m x` and allows an Edit under docs/agent/
     and `git status`; readonly_bash denies `echo x > f` for harness:reviewer; tests_first allows on main;
     session_brief answers SessionStart with an additionalContext; notify, with no notifier on PATH, prints
     the bell; after_edit and verify_on_stop exit 0 without blocking. A case fails on a timeout, a non-zero
     exit, a traceback, an error line on stderr (a hook failing open) or a wrong answer.
  3. `<tool> --help` for every file in bin/: exit 0 with usage on stdout.

Exit 0 when every check passes; 1 on a big failure (a file that does not compile, any hook case, the --help
of harness-pr, harness-board, harness-work or harness-red, a --plugin that is not there); 2 when only the
--help of a reporting tool failed. A big failure wins.

Every process it starts gets a temporary HOME, TMPDIR and working directory, and none of the caller's
HARNESS_*, CLAUDE_*, GIT_* or PYTHON* settings (HARNESS_ALLOW_MAIN must not fake a deny into an allow);
the temporary directory is removed at the end, so it writes nothing anywhere else.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PLUGIN = os.path.join(os.path.dirname(HERE), "plugins", "harness")
SYSTEM_PYTHON = "/usr/bin/python3"
HOOK_TIMEOUT = 10  # seconds: what hooks.json gives the guards
TOOL_TIMEOUT = 20
COMPILE_TIMEOUT = 60
CORE_TOOLS = ("harness-pr", "harness-board", "harness-work", "harness-red")
NOTIFIERS = ("osascript", "terminal-notifier")
SCRUBBED = ("HARNESS_", "CLAUDE_", "GIT_", "PYTHON", "XDG_CONFIG_HOME")
SOURCE = os.path.join("src", "app.py")
NOTES = os.path.join("docs", "agent", "handoff.md")
BELL = "\u0007"
SHOWN = 160  # characters of a hook's or tool's own message on a failing line

OK, BIG, DEGRADED = 0, 1, 2

# Run by the compiling python3, which may be 3.9: the files on argv, a JSON report on stdout.
COMPILE = r"""
import json, sys
errors = []
for path in sys.argv[1:]:
    try:
        with open(path, "rb") as fh:
            compile(fh.read(), path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        errors.append([path, "%s: %s (line %s)" % (type(exc).__name__, exc.msg, exc.lineno)])
    except (ValueError, OSError) as exc:
        errors.append([path, "%s: %s" % (type(exc).__name__, exc)])
print(json.dumps({"version": "%d.%d.%d" % sys.version_info[:3], "errors": errors}))
"""


class Problem(Exception):
    """The canary itself cannot go on: one line, exit 1."""


class Report:
    """The check lines and the exit code: a big failure wins over a degraded one."""

    def __init__(self) -> None:
        self.code = OK

    def ok(self, text: str) -> None:
        print(f"ok {text}", flush=True)

    def fail(self, text: str, big: bool = True) -> None:
        print(f"{'FAIL' if big else 'degraded'} {text}", flush=True)
        self.code = BIG if big or self.code == BIG else DEGRADED


def one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= SHOWN else line[: SHOWN - 1] + "…"


def first_line(text: str) -> str:
    return next((one_line(ln) for ln in text.splitlines() if ln.strip()), "")


def last_line(text: str) -> str:
    return next((one_line(ln) for ln in reversed(text.splitlines()) if ln.strip()), "")


def run(cmd: list, cwd: str, env: dict, timeout: float, stdin: str = "") -> tuple:
    """(exit code, stdout, stderr); the code is None on a timeout, when the whole process group is killed."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, "", f"cannot start {cmd[0]}: {exc}"
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        code = None
    return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class Sandbox:
    """The temporary HOME, TMPDIR and working directory every process gets, and the fixture repository."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.home = os.path.join(root, "home")
        self.tmp = os.path.join(root, "tmp")
        self.work = os.path.join(root, "work")
        self.repo = os.path.join(root, "repo")
        for path in (self.home, self.tmp, self.work):
            os.makedirs(path)
        self.python = shutil.which("python3") or sys.executable

    def env(self, path: str | None = None) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith(SCRUBBED)}
        env.update(HOME=self.home, TMPDIR=self.tmp, PYTHONDONTWRITEBYTECODE="1")
        if path is not None:
            env["PATH"] = path
        return env

    def make_repo(self) -> None:
        """A git repository on main with one commit holding a source file."""
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, SOURCE), "w", encoding="utf-8") as fh:
            fh.write('print("canary")\n')
        identity = ["-c", "user.name=canary", "-c", "user.email=canary@localhost", "-c", "commit.gpgsign=false"]
        for args in (
            ["init", "-q"],
            ["symbolic-ref", "HEAD", "refs/heads/main"],
            ["add", "-A"],
            identity + ["commit", "-q", "-m", "canary fixture"],
        ):
            code, out, err = run(["git"] + args, self.repo, self.env(), 30)
            if code != 0:
                raise Problem(f"cannot make the fixture repository: git {' '.join(args)}: {first_line(err or out)}")


def no_notifier_path() -> str:
    """The caller's PATH without the directories that hold osascript or terminal-notifier."""
    kept = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and not any(os.access(os.path.join(folder, name), os.X_OK) for name in NOTIFIERS):
            kept.append(folder)
    return os.pathsep.join(kept)


# ---------- reading a hook's answer as Claude Code does ----------


def answer(stdout: str) -> dict | None:
    """stdout as a JSON object; None for no output or output that is not one."""
    try:
        value = json.loads(stdout.strip() or "null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def specific(stdout: str, event: str) -> dict | None:
    data = answer(stdout)
    out = data.get("hookSpecificOutput") if data else None
    return out if isinstance(out, dict) and out.get("hookEventName") == event else None


def decision(stdout: str):
    out = specific(stdout, "PreToolUse")
    return out.get("permissionDecision") if out else None


def expect_deny(stdout: str) -> str | None:
    found = decision(stdout)
    return None if found == "deny" else f"allowed it (decision: {found or 'none'})"


def expect_allow(stdout: str) -> str | None:
    found = decision(stdout)
    return None if found in (None, "allow") else f"answered {found!r}, not allow"


def expect_brief(stdout: str) -> str | None:
    out = specific(stdout, "SessionStart")
    context = out.get("additionalContext") if out else None
    if isinstance(context, str) and context.strip():
        return None
    return "no SessionStart additionalContext on stdout"


def expect_bell(stdout: str) -> str | None:
    data = answer(stdout)
    return None if data and data.get("terminalSequence") == BELL else "no terminal bell on stdout"


def expect_no_block(stdout: str) -> str | None:
    data = answer(stdout)
    return "blocked" if data and data.get("decision") == "block" else None


def hook_cases(repo: str) -> list:
    """(hook file, case, event, check, PATH without notifiers) for each case the canary runs."""
    source, notes = os.path.join(repo, SOURCE), os.path.join(repo, NOTES)

    def event(name: str, **fields) -> dict:
        return dict({"session_id": "canary", "cwd": repo, "hook_event_name": name}, **fields)

    def pre(tool: str, tool_input: dict, **fields) -> dict:
        return event("PreToolUse", tool_name=tool, tool_input=tool_input, **fields)

    edit = {"file_path": source, "old_string": "canary", "new_string": "canary2"}
    return [
        ("branch_guard.py", f"denies an Edit of {SOURCE} on main", pre("Edit", edit), expect_deny, False),
        ("branch_guard.py", "denies Bash `git commit -m x` on main", pre("Bash", {"command": "git commit -m x"}),
         expect_deny, False),
        ("branch_guard.py", f"allows an Edit of {NOTES} on main",
         pre("Edit", {"file_path": notes, "old_string": "a", "new_string": "b"}), expect_allow, False),
        ("branch_guard.py", "allows Bash `git status` on main", pre("Bash", {"command": "git status"}),
         expect_allow, False),
        ("readonly_bash.py", "denies `echo x > f` for harness:reviewer",
         pre("Bash", {"command": "echo x > f"}, agent_type="harness:reviewer"), expect_deny, False),
        ("tests_first.py", f"allows an Edit of {SOURCE} on main", pre("Edit", edit), expect_allow, False),
        ("session_brief.py", "briefs a session at startup", event("SessionStart", source="startup"),
         expect_brief, False),
        ("notify.py", "rings the bell with no notifier on PATH",
         event("Notification", message="canary", title="Claude Code", notification_type="idle_prompt"),
         expect_bell, True),
        ("after_edit.py", f"exits 0 after an Edit of {SOURCE}",
         event("PostToolUse", tool_name="Edit", tool_input=edit, tool_response={"filePath": source}),
         expect_no_block, False),
        ("verify_on_stop.py", "exits 0 at a stop", event("Stop", stop_hook_active=False), expect_no_block, False),
    ]


# ---------- the checks ----------


def bin_tools(plugin: str) -> list:
    """The regular files in bin/; a dotfile (a Finder .DS_Store, an AppleDouble ._ file) is not a tool."""
    bin_dir = os.path.join(plugin, "bin")
    return [os.path.join(bin_dir, name) for name in sorted(os.listdir(bin_dir))
            if not name.startswith(".") and os.path.isfile(os.path.join(bin_dir, name))]


def plugin_files(plugin: str) -> list:
    """Every .py under hooks/ and lib/ and every file in bin/, relative to the plugin, dotfiles skipped."""
    found = []
    for top in ("hooks", "lib"):
        for folder, dirs, files in os.walk(os.path.join(plugin, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            found += [os.path.join(folder, name) for name in sorted(files)
                      if name.endswith(".py") and not name.startswith(".")]
    found += bin_tools(plugin)
    return [os.path.relpath(path, plugin) for path in found]


def check_compile(report: Report, box: Sandbox, plugin: str) -> None:
    python = SYSTEM_PYTHON if os.access(SYSTEM_PYTHON, os.X_OK) else box.python
    files = plugin_files(plugin)
    code, out, err = run([python, "-I", "-B", "-c", COMPILE] + files, plugin, box.env(), COMPILE_TIMEOUT)
    data = answer(out) if code == 0 else None
    if data is None or not isinstance(data.get("errors"), list):
        why = "timed out" if code is None else f"exit {code}: {first_line(err or out)}"
        report.fail(f"compile with {python}: {why}")
        return
    for rel, error in data["errors"]:
        report.fail(f"compile {rel} with {python} {data.get('version')}: {one_line(str(error))}")
    if not data["errors"]:
        report.ok(f"compile: {len(files)} files with {python} {data.get('version')}")


def check_hooks(report: Report, box: Sandbox, plugin: str) -> None:
    try:
        box.make_repo()
    except Problem as exc:
        report.fail(f"hooks: {exc}")
        return
    quiet_path = no_notifier_path()
    for hook, case, event, check, no_notifier in hook_cases(box.repo):
        env = box.env(quiet_path if no_notifier else None)
        path = os.path.join(plugin, "hooks", hook)
        code, out, err = run([box.python, path], box.repo, env, HOOK_TIMEOUT, json.dumps(event))
        if code is None:
            why = f"timed out after {HOOK_TIMEOUT} s"
        elif code != 0:
            why = f"exit {code}" + (f": {last_line(err)}" if err.strip() else "")
        elif "Traceback (most recent call last)" in err + out:
            why = f"printed a traceback: {last_line(err or out)}"
        elif err.strip():
            why = f"error on stderr: {first_line(err)}"
        else:
            why = check(out)
        if why is None:
            report.ok(f"{hook}: {case}")
        else:
            report.fail(f"{hook}: {case}: {why}")


def check_tools(report: Report, box: Sandbox, plugin: str) -> None:
    for path in bin_tools(plugin):
        name = os.path.basename(path)
        code, out, err = run([box.python, path, "--help"], box.work, box.env(), TOOL_TIMEOUT)
        if code is None:
            why = f"timed out after {TOOL_TIMEOUT} s"
        elif code != 0:
            why = f"exit {code}" + (f": {last_line(err)}" if err.strip() else "")
        elif "Traceback (most recent call last)" in err + out:
            why = f"printed a traceback: {last_line(err or out)}"
        elif not out.strip():
            why = "printed no usage"
        else:
            why = None
        if why is None:
            report.ok(f"{name} --help")
        else:
            report.fail(f"{name} --help: {why}", big=name in CORE_TOOLS)


def main() -> int:
    parser = argparse.ArgumentParser(prog="smoke.sh", description=__doc__.splitlines()[0])
    parser.add_argument("--plugin", default=DEFAULT_PLUGIN, help="the plugin to check (default: %(default)s)")
    args = parser.parse_args()
    plugin = os.path.abspath(args.plugin)
    report = Report()
    for path in (plugin, os.path.join(plugin, "hooks"), os.path.join(plugin, "bin")):
        if not os.path.isdir(path):
            report.fail(f"plugin: no such directory: {path}")
            return report.code
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-canary-"))
    try:
        box = Sandbox(root)
        check_compile(report, box, plugin)
        check_hooks(report, box, plugin)
        check_tools(report, box, plugin)
    except (OSError, Problem) as exc:
        report.fail(f"canary: {one_line(str(exc))}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return report.code


if __name__ == "__main__":
    sys.exit(main())
