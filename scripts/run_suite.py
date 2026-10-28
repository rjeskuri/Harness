#!/usr/bin/env python3
"""Run the harness test suite with one unittest process per test module, several at a time.

    python3 scripts/run_suite.py [-j N] [-v] [MODULE ...]

Each test module under plugins/harness/tests runs as `python3 -m unittest <module>` from that
directory (the same imports `unittest discover -s plugins/harness/tests` gives it), the largest
modules first so the last worker is not left with the biggest one. The output of a module that
fails is printed whole; a passing one gets one line. The summary line has unittest's shape,
`Ran N tests in S s` then `OK` or `FAILED (failures=F, errors=E)`, and the exit is 1 when any
module failed, errored or could not run. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TESTS = os.path.join(ROOT, "plugins", "harness", "tests")
RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
TALLY = re.compile(r"\b(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")


def modules(names: list[str]) -> list[str]:
    if names:
        return [n[:-3] if n.endswith(".py") else os.path.basename(n) for n in names]
    found = [f[:-3] for f in os.listdir(TESTS) if f.startswith("test_") and f.endswith(".py")]
    # Largest file first: the best cheap guess at the longest run.
    return sorted(found, key=lambda m: -os.path.getsize(os.path.join(TESTS, m + ".py")))


def run_module(module: str, verbose: bool) -> dict:
    started = time.monotonic()
    argv = [sys.executable, "-m", "unittest"] + (["-v"] if verbose else []) + [module]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(argv, cwd=TESTS, capture_output=True, text=True, errors="replace", env=env)
        output, code = proc.stdout + proc.stderr, proc.returncode
    except OSError as exc:
        output, code = f"{module}: could not run: {exc}\n", 1
    ran = RAN.search(output)
    tally = {k: int(v) for k, v in TALLY.findall(output)}
    ok = code == 0 and ran is not None
    return {"module": module, "ok": ok, "output": output, "seconds": time.monotonic() - started,
            "tests": int(ran.group(1)) if ran else 0, "tally": tally}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modules", nargs="*", help="test modules to run (default: every test_*.py)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2, help="modules at a time")
    parser.add_argument("-v", "--verbose", action="store_true", help="pass -v to unittest")
    args = parser.parse_args()
    todo = modules(args.modules)
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for result in pool.map(lambda m: run_module(m, args.verbose), todo):
            results.append(result)
            if result["ok"]:
                skipped = result["tally"].get("skipped", 0)
                extra = f", {skipped} skipped" if skipped else ""
                print(f"{result['module']:44s} {result['tests']:5d} tests {result['seconds']:7.1f}s  ok{extra}", flush=True)
            else:
                print(f"{result['module']:44s} {result['tests']:5d} tests {result['seconds']:7.1f}s  FAILED", flush=True)
                print(result["output"].rstrip(), flush=True)
                print("-" * 70, flush=True)
    total = sum(r["tests"] for r in results)
    counts: dict[str, int] = {}
    for r in results:
        for k, v in r["tally"].items():
            counts[k] = counts.get(k, 0) + v
    failed = [r["module"] for r in results if not r["ok"]]
    print()
    print(f"Ran {total} tests in {time.monotonic() - started:.1f}s across {len(results)} modules, {args.jobs} at a time")
    if failed:
        parts = [f"{k}={v}" for k, v in counts.items() if k in ("failures", "errors") and v]
        broken = [m for m in failed if not any(k in ("failures", "errors") for k in next(r for r in results if r["module"] == m)["tally"])]
        if broken:
            parts.append("could not run=" + ",".join(broken))
        print(f"FAILED ({', '.join(parts)})")
        return 1
    skipped = counts.get("skipped", 0)
    print("OK" + (f" (skipped={skipped})" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
