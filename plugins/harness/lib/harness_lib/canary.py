"""The project's canary, run by harness-pr land in the main checkout once a land has pulled it: that checkout's
scripts/smoke.sh, under a time limit. The process launch lives here, not in harness-pr, which launches nothing
but git, its sibling tools and claude (as gh's launches live in gh.py). See docs/design.md (harness_lib, land).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile

SMOKE = "scripts/smoke.sh"   # the project's canary, run in the main checkout after a land


def run_canary(main, limit):
    """Run the main checkout's scripts/smoke.sh there when it is an executable file: stdin closed, stdout and stderr
    together into a temporary file (so a child left holding them cannot keep the land waiting), in a session of its
    own so that past `limit` seconds its whole process group, children too, is killed. (verdict, what it did, its
    output): `healthy` for exit 0, `degraded` for exit 2 or the limit, `failed` for any other exit or a script that
    cannot be started. None when there is no executable script."""
    path = os.path.join(main, SMOKE)
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen([path], cwd=main, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as err:
            return "failed", f"could not be started ({err.strerror or err})", ""
        try:
            code = proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            code = None
        out.seek(0)
        output = out.read().decode("utf-8", "replace")
    if code is None:
        return "degraded", f"ran past its {limit:g} s time limit and was stopped", output
    said = f"exited {code}" if code >= 0 else f"was killed by signal {-code}"
    return ("healthy" if code == 0 else "degraded" if code == 2 else "failed"), said, output
