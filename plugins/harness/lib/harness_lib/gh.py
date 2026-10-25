"""The gh helpers the bin tools share (and session_brief's gh lookup and environment): which gh to
run, the environment it runs in, an origin URL as a `gh -R` slug, the sequential wrappers
harness-board, harness-pr and harness-work run gh through (describe, gh_run, gh, gh_json), and
GhBatch, the gh calls harness-standup and harness-digest start side by side under one deadline.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time

from harness_lib.cli import Problem
from harness_lib.ghlimit import log_call, ms_since
from harness_lib.text import first_line

GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}


def gh_exe() -> str:
    """The gh named by HARNESS_GH, else `gh`, as given (found or not)."""
    return os.environ.get("HARNESS_GH", "gh")


def gh_binary() -> str | None:
    """The gh to run (HARNESS_GH, else gh on PATH), or None when it cannot be found."""
    gh = gh_exe()
    if os.sep in gh:
        return gh if os.path.isfile(gh) and os.access(gh, os.X_OK) else None
    return shutil.which(gh)


def repo_slug(origin_url):
    """OWNER/REPO (HOST/OWNER/REPO off github.com) for gh -R, from an origin URL; None for a local path."""
    url = origin_url.strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d*)?/(.+)$", url)
    if not m or url.lower().startswith("file:"):
        m = re.match(r"^(?:[^@/:]+@)?([^/:]+):(?!/)(.+)$", url)         # scp-like: git@host:owner/repo
    if not m:
        return None
    host, parts = m.group(1), [p for p in m.group(2).split("/") if p]
    if len(parts) != 2:
        return None
    owner, name = parts[0], parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not owner or not name:
        return None
    return f"{owner}/{name}" if host.lower() in ("github.com", "www.github.com") else f"{host}/{owner}/{name}"


def describe_list_call(args):
    """`gh pr list`, `gh issue list --label status:needs-input`: the call, for an error line."""
    words = "gh " + " ".join(args[:2])
    if "--label" in args[:-1]:
        words += " --label " + args[args.index("--label") + 1]
    return words


# --------------------------------------------------------------- one gh call at a time
# What a failed call reports, as str.format templates. Fields: exe (gh_exe()), call (describe(args)), args (the
# list), timeout, why (the OS error, or the picked stderr line or `exit N`) and excerpt (the output's picked line,
# by default its first, cut at 80). A caller's `texts` replace only the keys it gives.
GH_TEXTS = {
    "not_found": "gh not found ({exe}); install it with `brew install gh`, then `gh auth login`",
    "cannot_run": "cannot run gh ({exe}): {why}",
    "timed_out": "{call} timed out after {timeout} s",
    "failed": "{call} failed: {why}",
    "not_json": "{call} printed something other than JSON: {excerpt}",
}


def describe(args):
    """`gh issue view 12`, `gh api repos/o/r/issues/3/sub_issues`, `gh api graphql`: the call, for an error line.
    For api the first argument that is graphql or a path (holds "/", not a flag); else up to three words before
    the first flag."""
    if args and args[0] == "api":
        return "gh api " + next((a for a in args[1:] if a == "graphql" or ("/" in a and not a.startswith("-"))), "")
    words = []
    for arg in args:
        if arg.startswith("-") or len(words) == 3:
            break
        words.append(arg)
    return "gh " + " ".join(words)


def _text(texts, key, args, describe, exe, **fields):
    """The `key` text for the call: the caller's template if it gave one, else GH_TEXTS'."""
    template = (texts or {}).get(key, GH_TEXTS[key])
    values = dict({"timeout": "", "why": "", "excerpt": ""}, **fields)
    return template.format(exe=exe, call=describe(args), args=args, **values)


def gh_run(args, cwd, stdin=None, timeout=60, *, errors="replace", env=None, describe=describe, texts=None):
    """Run gh_exe() with `args` in `cwd` (None: the current directory), `stdin` as its input (None: /dev/null), in
    os.environ plus `env` (None: prompts disabled). The CompletedProcess, output decoded as utf-8 with `errors`,
    whatever the exit; Problem when gh is missing, cannot start or runs past `timeout` seconds. A call that ran
    is logged (ghlimit), with exit null when it timed out."""
    exe = gh_exe()
    io = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    extra = env if env is not None else {"GH_PROMPT_DISABLED": "1"}
    started = time.monotonic()
    try:
        done = subprocess.run([exe] + list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors=errors, timeout=timeout, env=dict(os.environ, **extra), **io)
    except FileNotFoundError:
        raise Problem(_text(texts, "not_found", args, describe, exe)) from None
    except OSError as err:
        raise Problem(_text(texts, "cannot_run", args, describe, exe, why=err.strerror or err)) from None
    except subprocess.TimeoutExpired:
        log_call(args, cwd=cwd, exit=None, ms=ms_since(started))
        raise Problem(_text(texts, "timed_out", args, describe, exe, timeout=timeout)) from None
    log_call(args, cwd=cwd, exit=done.returncode, ms=ms_since(started))
    return done


def gh(args, cwd, stdin=None, timeout=60, *, pick=first_line, errors="replace", env=None, describe=describe,
       texts=None, run=None):
    """gh_run's stdout (or, with `run`, that of run(args, cwd, stdin, timeout), so a tool's adapter can name its
    own gh_run); on a non-zero exit Problem naming the call and pick(stderr), else `exit N`."""
    if run is None:
        done = gh_run(args, cwd, stdin, timeout, errors=errors, env=env, describe=describe, texts=texts)
    else:
        done = run(args, cwd, stdin, timeout)
    if done.returncode != 0:
        why = pick(done.stderr) or f"exit {done.returncode}"
        raise Problem(_text(texts, "failed", args, describe, gh_exe(), why=why))
    return done.stdout


def gh_json(args, cwd, *, pick=first_line, empty_ok=False, via=None, **kw):
    """gh's stdout (or, with `via`, via(args, cwd), so a tool's adapter can name its own gh) decoded as JSON; None
    for empty output when `empty_ok`; `kw` reaches gh. Problem for output that is not JSON, its excerpt picked."""
    out = gh(args, cwd, pick=pick, **kw) if via is None else via(args, cwd)
    if empty_ok and not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        raise Problem(_text(kw.get("texts"), "not_json", args, kw.get("describe", describe), gh_exe(),
                            excerpt=pick(out)[:80])) from None


class GhBatch:
    """gh calls started side by side; results() waits for each until the shared deadline.
    `budget_seconds` is only named in a timeout's message (the caller's own budget). Each call is logged
    (ghlimit) once: when results() reads it, else when close() releases it (exit null when it was stopped)."""

    def __init__(self, gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call):
        self.calls, self.deadline, self.procs = calls, deadline, []
        self.budget_seconds, self.describe = budget_seconds, describe
        self.cwd, self.started, self.logged = cwd, [], set()
        env = dict(os.environ, **GH_ENV)
        try:
            for args in calls:
                self.started.append(time.monotonic())
                self.procs.append(subprocess.Popen([gh] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                                   start_new_session=True))
        except OSError as err:
            self.close()
            raise Problem(f"cannot run gh ({gh}): {err.strerror or err}") from None

    def results(self):
        """One list of objects per call, in order; Problem naming the first call that failed."""
        out = []
        for i, (args, proc) in enumerate(zip(self.calls, self.procs)):
            try:
                stdout, stderr = proc.communicate(timeout=max(0.1, self.deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Problem(f"{self.describe(args)} timed out after {self.budget_seconds} s") from None
            self._log(i, proc.returncode)
            text = stdout.decode("utf-8", "replace")
            if proc.returncode != 0:
                why = first_line(stderr.decode("utf-8", "replace")) or f"exit {proc.returncode}"
                raise Problem(f"{self.describe(args)} failed: {why}")
            try:
                data = json.loads(text)
            except ValueError:
                raise Problem(f"{self.describe(args)} printed something other than JSON: {first_line(text)[:80]}") from None
            if not isinstance(data, list):
                raise Problem(f"{self.describe(args)} did not print a JSON list")
            out.append([x for x in data if isinstance(x, dict)])
        return out

    def _log(self, i, code):
        """Log call i with exit `code`, unless it already was."""
        if i not in self.logged:
            self.logged.add(i)
            log_call(self.calls[i], cwd=self.cwd, exit=code, ms=ms_since(self.started[i]))

    def close(self):
        """Stop every call still running and release the pipes; log each call results() did not read."""
        for i, proc in enumerate(self.procs):
            code = proc.poll()
            if code is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            self._log(i, code)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
