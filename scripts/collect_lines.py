#!/usr/bin/env python3
"""Collect which lines of Harness run under the suite or a loop smoke, subprocesses included (#474).

    python3 scripts/collect_lines.py suite
    python3 scripts/collect_lines.py loop-smoke SANDBOX     # lands a PR on the sandbox: never run by default

A bare venv, ~/.claude/harness/deadcode/venv (`python3 -m venv --without-pip`, made once from the python3 running
this script and reused), holds one `.pth` line that imports a small starter. While the flag file
~/.claude/harness/deadcode/collecting.json exists, every process the venv's python3 starts traces itself with
sys.settrace and threading.settrace, keeps the lines it runs of the checkout's plugins/harness and scripts
(plugins/harness/tests left out) and writes them at exit or SIGTERM to one JSON per process. The wrapped runner
(scripts/run_suite.py, or scripts/loop_smoke.py run) runs on the venv's python3 with the venv's bin first on PATH.
Afterwards the per-process files are merged, each line mapped to its statement with ast, into
~/.claude/harness/deadcode/<repo-key>/<run>/lines.json: {"files": {path: {"executed_lines": [...]}}, "real_run",
"returncode"}, returncode the wrapped command's exit (130 for a Ctrl-C), so only 0 is a complete run.
The flag is removed whatever happens. Standard library only, 3.9-compatible.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "harness", "lib"))
from harness_lib.paths import repo_key  # noqa: E402
from harness_lib.repo import main_checkout  # noqa: E402

STATE = os.path.join(os.path.expanduser("~"), ".claude", "harness", "deadcode")
VENV = os.path.join(STATE, "venv")
FLAG = os.path.join(STATE, "collecting.json")
STARTER = "harness_deadcode_start"

STARTER_SOURCE = '''"""Harness dead-code line collection, written by scripts/collect_lines.py (#474). Off unless FLAG exists."""
FLAG = %r


def _start():
    import json
    import os
    try:
        with open(FLAG, encoding="utf-8") as fh:
            conf = json.load(fh)
        root, out = conf["root"], conf["out"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    import atexit
    import signal
    import sys
    import threading
    import time

    keep = (os.path.join(root, "plugins", "harness", ""), os.path.join(root, "scripts", ""))
    drop = os.path.join(root, "plugins", "harness", "tests", "")
    data, tracers = {}, {}

    def line_tracer(add):
        def trace(frame, event, arg):
            if event == "line":
                add(frame.f_lineno)
            return trace
        return trace

    def global_trace(frame, event, arg):
        name = frame.f_code.co_filename
        try:
            return tracers[name]
        except KeyError:
            pass
        found = None
        if not name.startswith("<"):
            real = os.path.realpath(name)
            if real.startswith(keep) and not real.startswith(drop):
                found = line_tracer(data.setdefault(real, set()).add)
        tracers[name] = found
        return found

    done = []

    def dump():
        if done:
            return
        done.append(True)
        sys.settrace(None)
        threading.settrace(None)
        try:
            payload = {name: sorted(lines) for name, lines in list(data.items())}
            os.makedirs(out, exist_ok=True)
            path = os.path.join(out, "%%d-%%d.json" %% (os.getpid(), time.time_ns()))
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(path + ".tmp", path)
        except Exception:
            pass

    def on_term(signum, frame):
        dump()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    atexit.register(dump)
    try:
        if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):
        pass
    threading.settrace(global_trace)
    sys.settrace(global_trace)


try:
    _start()
except Exception:
    pass
'''


def venv_python() -> str:
    """The venv's python3, made from the python3 running this script when it is missing."""
    python = os.path.join(VENV, "bin", "python3")
    if not os.path.exists(python):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", VENV], check=True)
    return python


def install_starter(python: str) -> None:
    """Write the starter and its `.pth` line into the venv's site-packages (only there)."""
    site = subprocess.run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                          capture_output=True, text=True, check=True).stdout.strip()
    with open(os.path.join(site, STARTER + ".py"), "w", encoding="utf-8") as fh:
        fh.write(STARTER_SOURCE % FLAG)
    with open(os.path.join(site, "harness_deadcode.pth"), "w", encoding="utf-8") as fh:
        fh.write("import " + STARTER + "\n")


def statement_lines(path: str) -> dict[int, int]:
    """Line -> the lineno of the innermost statement whose span (decorators included) holds it."""
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read(), path)
    owner: dict[int, int] = {}
    for node in ast.walk(tree):  # breadth first: an inner statement comes after the one holding it
        if isinstance(node, ast.stmt):
            first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            for n in range(first, (node.end_lineno or node.lineno) + 1):
                owner[n] = node.lineno
    return owner


def merge(procs: str, real_run: bool) -> dict:
    executed: dict[str, set[int]] = {}
    for name in sorted(os.listdir(procs)) if os.path.isdir(procs) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(procs, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for path, lines in data.items():
            executed.setdefault(path, set()).update(lines)
    files = {}
    for path, lines in sorted(executed.items()):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(os.pardir):
            continue
        try:
            owner = statement_lines(path)
            lines = {owner[n] for n in lines if n in owner}
        except (OSError, SyntaxError, ValueError):
            pass  # not parseable by this interpreter: the raw lines stay
        if lines:
            files[rel.replace(os.sep, "/")] = {"executed_lines": sorted(lines)}
    return {"files": files, "real_run": real_run}


def collect(argv: list[str], kind: str, real_run: bool) -> int:
    started = time.monotonic()
    python = venv_python()
    install_starter(python)
    main = main_checkout(ROOT) or ROOT
    run = os.path.join(STATE, repo_key(main), "%s-%s-%d" % (kind, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                                                              os.getpid()))
    procs = os.path.join(run, "procs")
    os.makedirs(procs)
    env = dict(os.environ, PATH=os.path.join(VENV, "bin") + os.pathsep + os.environ.get("PATH", ""))
    env.pop("PYTHONHOME", None)
    code = 1
    try:
        with open(FLAG, "w", encoding="utf-8") as fh:
            json.dump({"root": ROOT, "out": procs}, fh)
        code = subprocess.run([python, *argv], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            os.remove(FLAG)
        except OSError:
            pass
    count = len([n for n in os.listdir(procs) if n.endswith(".json")])
    out = os.path.join(run, "lines.json")
    data = merge(procs, real_run)
    data["returncode"] = code  # a reader treats only 0 as a complete run
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    shutil.rmtree(procs, ignore_errors=True)
    print("collect_lines: %s exit %d, %.0f s, %d processes, %s" % (kind, code, time.monotonic() - started, count, out))
    return 0 if code == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="collect_lines.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("suite", help="collect under scripts/run_suite.py")
    loop = sub.add_parser("loop-smoke", help="collect under one loop smoke run (lands a PR on the sandbox)")
    loop.add_argument("sandbox", help="the harness-sandbox clone")
    args = parser.parse_args()
    if args.command == "suite":
        return collect([os.path.join(ROOT, "scripts", "run_suite.py")], "suite", False)
    if args.command == "loop-smoke":
        return collect([os.path.join(ROOT, "scripts", "loop_smoke.py"), "run", "--sandbox",
                        os.path.abspath(args.sandbox), "--plugin", os.path.join(ROOT, "plugins", "harness")],
                       "loop-smoke", True)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
