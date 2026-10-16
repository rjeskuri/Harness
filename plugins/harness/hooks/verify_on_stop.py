#!/usr/bin/env python3
"""Stop hook: before Claude finishes a turn in which it edited files, run the project's
verify command. If it fails, block the stop and hand Claude the failure, so it fixes it
now instead of reporting that it is done.

Opt-in per project: set CLAUDE_VERIFY_CMD, for example in the "env" block of the
project's .claude/settings.json:
    "env": { "CLAUDE_VERIFY_CMD": "uv run pytest -q -x", "CLAUDE_VERIFY_TIMEOUT": "180" }
Both are read from the environment first, then from `env` in .claude/settings.local.json,
then .claude/settings.json, so they work whether or not settings `env` reaches hooks.

When it runs
  Only when files changed in this session, no background task is still in flight, and
  the code changed since the last green run. Files changed means after_edit.py recorded
  an edit (<state>/dirty-<session_id>), or the working tree that `git status` lists
  differs from the one this hook saw at the previous stop, which catches changes made
  through Bash or by other processes; the hook then adds the files git lists to the dirty
  list, so they count as an edit until a green run. A session's first stop only records
  the tree, so changes that predate the session never trigger a run. The fingerprint is
  HEAD plus the contents of the files in the dirty list and of every file git status
  lists. Question-and-answer turns cost nothing, after a failure too: a fresh stop with
  the same fingerprint and command as the last run that failed, timed out or could not
  start says so in one line instead of running it again (the in-turn retries that a block
  forces still run). It also stays silent when the project's .claude/settings.json
  declares a Stop hook of its own, so a project with its own verify-on-stop is not
  checked twice.

What it does
  Runs the command through the shell from the repo root, with CLAUDE_VERIFY_TIMEOUT
  seconds (default 300, capped at 400 to stay inside the hook's own 420 s budget).
  Pass: records the fingerprint, clears the dirty list, and says so in one line.
  Fail: blocks with the last 60 non-empty lines of output, at most 2 times in a row;
  after that it lets Claude stop and warns you instead.
  Timeout, or a command that cannot start (shell status 126 or 127): a one-line warning,
  never a block.

Fails open: an error inside this script never blocks Claude.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.hookio import emit, hook_cwd, read_input, run_fail_open
    from harness_lib.settings import setting, settings_env
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"verify_on_stop.py: cannot load harness_lib from {_LIB}: {exc}\n")
    sys.exit(0)

MAX_CONSECUTIVE_BLOCKS = 2
TAIL_LINES = 60
DEFAULT_TIMEOUT = 300.0
MAX_TIMEOUT = 400.0  # hooks.json gives this hook 420 s
CANNOT_START = (126, 127)  # the shell's "not executable" and "command not found"
STATUS_TIMEOUT = 8
GUIDANCE = (
    "Fix the root cause and re-run. Don't delete, skip or loosen tests. If a failure "
    "predates your change or is out of scope, say so explicitly."
)

_child: subprocess.Popen | None = None


def git(root: Path, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def repo_root(start: Path) -> Path | None:
    """Git top-level for `start`, or None outside a repo."""
    try:
        out = git(start, "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def state_dir(root: Path) -> Path:
    """Per-repo scratch directory shared with after_edit.py, which creates it."""
    key = os.path.normcase(str(root.resolve())).encode("utf-8", "surrogateescape")
    return (
        Path(tempfile.gettempdir())
        / "claude-harness"
        / hashlib.sha1(key).hexdigest()[:12]
    )


def session_key(data: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(data.get("session_id") or "nosession"))


def tail(text: str, n: int = TAIL_LINES) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join([f"... ({len(lines) - n} earlier lines omitted)", *lines[-n:]])


# ---------- settings ----------


def declares_stop_hook(root: Path) -> bool:
    """True when the project's .claude/settings.json has a Stop hook of its own."""
    try:
        data = json.loads(
            (root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    entries = hooks.get("Stop") if isinstance(hooks, dict) else None
    return isinstance(entries, list) and bool(entries)


def verify_timeout(file_env: dict) -> float:
    raw = setting("CLAUDE_VERIFY_TIMEOUT", file_env)
    try:
        value = float(raw) if raw is not None else DEFAULT_TIMEOUT
    except ValueError:
        value = DEFAULT_TIMEOUT
    if not value > 0:  # also catches nan
        value = DEFAULT_TIMEOUT
    return min(value, MAX_TIMEOUT)


# ---------- what changed ----------


def dirty_files(flag: Path) -> list[Path]:
    try:
        lines = flag.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return []
    return sorted({Path(p) for p in lines if p.strip()})


def record(flag: Path, files: list[Path]) -> None:
    """Add `files` to the dirty list, as after_edit.py does for an edit."""
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        with flag.open("a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write("".join(f"{f}\n" for f in files if "\n" not in str(f)))
    except OSError:
        pass


def worktree_status(git_root: Path | None) -> bytes | None:
    """`git status --porcelain=v1 -z --untracked-files=all` output: empty outside git,
    None when git fails."""
    if git_root is None:
        return b""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(git_root), "status"]
            + ["--porcelain=v1", "-z", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=STATUS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def status_paths(root: Path, out: bytes) -> list[Path]:
    """The paths in `git status --porcelain=v1 -z` output: one "XY path" field per entry,
    and for a rename or copy (R or C in X or Y) a second field, its source, skipped here."""
    paths = []
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(root / os.fsdecode(entry[3:]))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            i += 1
    return paths


def fingerprint(git_root: Path | None, files: list[Path]) -> str:
    """HEAD plus the contents of `files`."""
    head = ""
    if git_root is not None:
        try:
            out = git(git_root, "rev-parse", "--verify", "-q", "HEAD")
            head = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            head = ""
    h = hashlib.sha256(head.encode("utf-8"))
    for f in files:
        h.update(b"\0" + str(f).encode("utf-8", "surrogateescape") + b"\0")
        try:
            with f.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            h.update(b"\0missing\0")
    return h.hexdigest()


# ---------- running the command ----------


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def on_signal(signum, frame) -> None:
    """Claude Code cancelled the hook: take the verify command down with it."""
    if _child is not None and _child.poll() is None:
        kill_group(_child)
    os._exit(0)


def run_command(cmd: str, root: Path, timeout: float) -> tuple[int, str]:
    """(exit status, combined output). Raises TimeoutExpired or OSError."""
    global _child
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    # A session of its own, so a timeout can stop the shell and everything it started.
    _child = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        out, _ = _child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(_child)
        try:
            _child.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        raise
    return _child.returncode, out.decode("utf-8", "replace")


# ---------- main ----------


def main() -> int:
    data = read_input()
    cwd = Path(hook_cwd(data))
    git_root = repo_root(cwd)
    root = git_root or cwd
    if declares_stop_hook(root) or data.get("background_tasks"):
        return 0
    file_env = settings_env(root)
    cmd = setting("CLAUDE_VERIFY_CMD", file_env)
    if not cmd:
        return 0

    sdir = state_dir(root)
    session = session_key(data)
    dirty = sdir / f"dirty-{session}"
    state_file = sdir / f"verify-{session}.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("blocks"), int):
        state["blocks"] = 0

    def save() -> None:
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    # The working tree as git lists it, kept as `tree` at the end of every stop, so files
    # changed through Bash count even when after_edit.py did not record them. When git
    # fails the last known tree stands.
    status = worktree_status(git_root)
    listed = status_paths(git_root, status) if git_root is not None and status else []
    seen = state.get("tree")
    tree = hashlib.sha256(status).hexdigest() if status is not None else None
    if tree is not None:
        state["tree"] = tree
    if not dirty.is_file():
        if tree is None or seen is None or tree == seen:
            save()  # a session's first stop only records the tree
            return 0
        record(dirty, listed)  # changed since the last stop: an edit until a green run

    fresh = not data.get("stop_hook_active")  # not a continuation a Stop hook forced
    if fresh:
        state["blocks"] = 0
        save()

    fp = fingerprint(git_root, sorted(set(dirty_files(dirty)) | set(listed)))
    if fp == state.get("green"):
        save()
        return 0
    if fresh and fp == state.get("attempted") and cmd == state.get("attempted_cmd"):
        emit(
            {
                "systemMessage": f"verify_on_stop: {cmd} still failing and nothing "
                "changed since the last check; fix it or run it by hand."
            }
        )
        return 0

    def remember_failure() -> None:
        """Failed, timed out or could not start: an unchanged fresh stop skips it."""
        state.update(attempted=fp, attempted_cmd=cmd)
        save()

    timeout = verify_timeout(file_env)
    try:
        code, output = run_command(cmd, root, timeout)
    except subprocess.TimeoutExpired:
        remember_failure()
        emit(
            {
                "systemMessage": f"verify_on_stop: {cmd} timed out after {timeout:g}s and was "
                "stopped; nothing was checked. Raise CLAUDE_VERIFY_TIMEOUT or point "
                "CLAUDE_VERIFY_CMD at a faster check."
            }
        )
        return 0
    except OSError as exc:
        remember_failure()
        emit({"systemMessage": f"verify_on_stop: could not start {cmd} ({exc})."})
        return 0

    if code in CANNOT_START:
        remember_failure()
        said = [ln.strip() for ln in output.splitlines() if ln.strip()]
        last = said[-1] if said else f"exit status {code}"
        emit({"systemMessage": f"verify_on_stop: could not start {cmd}: {last}"})
        return 0

    if code == 0:
        state.update(green=fp, blocks=0)
        state.pop("attempted", None)
        state.pop("attempted_cmd", None)
        save()
        try:
            dirty.unlink()
        except OSError:
            pass
        emit({"systemMessage": f"verify_on_stop: {cmd} passed."})
        return 0

    remember_failure()
    if state["blocks"] >= MAX_CONSECUTIVE_BLOCKS:
        state["blocks"] = 0
        save()
        emit(
            {
                "systemMessage": "verify_on_stop: still failing after "
                f"{MAX_CONSECUTIVE_BLOCKS} automatic retries; review before trusting this turn."
            }
        )
        return 0

    state["blocks"] += 1
    save()
    details = tail(output) or f"(no output; exit status {code})"
    emit({"decision": "block", "reason": f"{cmd} failed:\n{details}\n\n{GUIDANCE}"})
    return 0


if __name__ == "__main__":
    for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(_sig, on_signal)
        except (OSError, ValueError):
            pass
    # fail open: a broken hook must never trap the session
    sys.exit(run_fail_open("verify_on_stop.py", main))
