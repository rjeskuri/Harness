"""The gh helpers harness-standup and harness-digest share (and session_brief's gh lookup and
environment): which gh to run, the environment it runs in, an origin URL as a `gh -R` slug, and
GhBatch, the gh calls started side by side under one deadline.

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


class GhBatch:
    """gh calls started side by side; results() waits for each until the shared deadline.
    `budget_seconds` is only named in a timeout's message (the caller's own budget)."""

    def __init__(self, gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call):
        self.calls, self.deadline, self.procs = calls, deadline, []
        self.budget_seconds, self.describe = budget_seconds, describe
        env = dict(os.environ, **GH_ENV)
        try:
            for args in calls:
                self.procs.append(subprocess.Popen([gh] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                                   start_new_session=True))
        except OSError as err:
            self.close()
            raise Problem(f"cannot run gh ({gh}): {err.strerror or err}") from None

    def results(self):
        """One list of objects per call, in order; Problem naming the first call that failed."""
        out = []
        for args, proc in zip(self.calls, self.procs):
            try:
                stdout, stderr = proc.communicate(timeout=max(0.1, self.deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Problem(f"{self.describe(args)} timed out after {self.budget_seconds} s") from None
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

    def close(self):
        """Stop every call still running and release the pipes."""
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
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
