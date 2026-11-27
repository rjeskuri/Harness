#!/usr/bin/env python3
"""Mutation testing for Harness with the standard library alone: dev-only, outside the plugin, not run by the loop.

    python3 scripts/mutate.py run [PATH ...] [--changed REF] [--lines N,N] [--tests PATTERN ...] [--workers N] [--json]

Run from the repository root. Each PATH is a file or a directory walked (default: the plugin's hooks, lib and bin);
the files mutated are `.py` files and files whose first line is a python3 shebang, never a test file (under
`tests/`, `test_*.py`, `*_support.py`). A site is one operator or literal from mutator's Python set, found with
`ast`. `--changed REF` keeps only the sites on lines new or changed in `git diff REF` (the working tree against REF);
with `--lines` too, a site must be on both. Each mutant runs in a full copy of the tree in a temporary directory (`cp -c` on macOS), so a test that finds
its code through its own real path imports the mutant; the checkout is never written and every copy is removed.

A file's tests: `--tests` (test module file names in plugins/harness/tests, or repo-relative globs), else the
`HARNESS_TEST_MAP` entries its path matches plus, for `lib/harness_lib/<name>.py`, `hooks/<name>.py` and
`bin/harness-<name>`, the `test_*.py` beside it that harness-red's name rule gives (a subject that is a `_`-token
suffix of the path or a directory name: `test_<name>.py`, `test_harness_lib_<name>.py`, `test_harness_lib.py`,
`test_hooks.py`) and the split suites `test_<suffix>_*.py` of those suffixes. Each test set runs once unmutated
first; a failing baseline stops the run with exit 2. A mutant whose tests fail, or run past 10 times the baseline
(at least 2 s), is killed. Exit 0 when nothing survived, 3 when a mutant did, 1 on a bad invocation.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import fnmatch
import glob
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PATHS = ["plugins/harness/hooks", "plugins/harness/lib", "plugins/harness/bin"]
DEFAULT_TESTS = "plugins/harness/tests"
NOT_COPIED = {".git", ".claude/worktrees"}
NOT_WALKED = {".git", "__pycache__", "node_modules"}
OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
       ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.And: "and", ast.Or: "or"}
SWAP = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "+": "-", "-": "+", "*": "/",
        "and": "or", "or": "and"}
CONVENTION = re.compile(r"^(?P<base>(?:.*/)?)(?:lib/harness_lib/(?P<lib>\w+)\.py|hooks/(?P<hook>\w+)\.py"
                        r"|bin/harness-(?P<tool>[\w-]+))$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# Each write in a copy gets a fresh mtime, so a cached .pyc of the same size never stands in for the new source.
STAMPS = itertools.count(1_000_000_000)


def is_test(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or (name.startswith("test_") and name.endswith(".py")) or name.endswith("_support.py")


def is_python(path: str) -> bool:
    if path.endswith(".py"):
        return True
    try:
        with open(path, "rb") as handle:
            first = handle.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python3" in first


def source_files(root: str, paths: list[str]) -> list[str]:
    found = set()
    for path in paths:
        full = os.path.join(root, path)
        if os.path.isfile(full):
            candidates = [full]
        elif os.path.isdir(full):
            candidates = []
            for here, dirs, files in os.walk(full):
                prefix = os.path.relpath(here, root).replace(os.sep, "/") + "/"
                prefix = "" if prefix == "./" else prefix
                dirs[:] = sorted(d for d in dirs if d not in NOT_WALKED and prefix + d not in NOT_COPIED)
                candidates += [os.path.join(here, name) for name in files]
        else:
            sys.exit(f"mutate: no such file or directory: {path}")
        for cand in candidates:
            rel = os.path.relpath(cand, root).replace(os.sep, "/")
            if not is_test(rel) and is_python(cand):
                found.add(rel)
    return sorted(found)


class Sites(ast.NodeVisitor):
    """Every mutation site of one file: (start, stop, replacement, change, line, function), offsets in bytes."""

    def __init__(self, data: bytes):
        self.data = data
        self.starts = list(itertools.accumulate([0] + [len(line) for line in data.splitlines(keepends=True)]))
        self.func = ["<module>"]
        self.found: list[tuple] = []

    def pos(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col

    def add(self, start: int, stop: int, repl: bytes, change: str) -> None:
        line = bisect.bisect_right(self.starts, start)
        self.found.append((start, stop, repl, change, line, self.func[-1]))

    def token(self, left: ast.AST, right: ast.AST, text: str, repl: str) -> None:
        """The operator between two operands: skip blanks, parentheses, continuations and comments to reach it."""
        i, stop = self.pos(left.end_lineno, left.end_col_offset), self.pos(right.lineno, right.col_offset)
        while i < stop:
            char = self.data[i:i + 1]
            if char in b" \t\r\n\x0c\\()":
                i += 1
            elif char == b"#":
                i = self.data.find(b"\n", i) if self.data.find(b"\n", i) != -1 else stop
            else:
                break
        if self.data.startswith(text.encode(), i):
            self.add(i, i + len(text), repl.encode(), f"{text} -> {repl}")

    def span(self, node: ast.AST, repl: bytes, change: str) -> None:
        self.add(self.pos(node.lineno, node.col_offset), self.pos(node.end_lineno, node.end_col_offset), repl, change)

    def visit_FunctionDef(self, node: ast.AST) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)  # the defaults; annotations and `returns` are never visited
        self.func.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.func.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_arguments(self, node: ast.arguments) -> None:
        for default in node.defaults + [d for d in node.kw_defaults if d is not None]:
            self.visit(default)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_JoinedStr(self, node: ast.AST) -> None:
        pass  # inside an f-string: a string, and 3.9's positions there are unreliable

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_Compare(self, node: ast.Compare) -> None:
        for left, op, right in zip([node.left] + node.comparators, node.ops, node.comparators):
            if type(op) in OPS:
                self.token(left, right, OPS[type(op)], SWAP[OPS[type(op)]])
        self.generic_visit(node)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_BinOp(self, node: ast.BinOp) -> None:
        if type(node.op) in (ast.Add, ast.Sub, ast.Mult):
            self.token(node.left, node.right, OPS[type(node.op)], SWAP[OPS[type(node.op)]])
        self.generic_visit(node)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if type(node.op) in (ast.Add, ast.Sub, ast.Mult):
            text = OPS[type(node.op)]
            self.token(node.target, node.value, text + "=", SWAP[text] + "=")
        self.generic_visit(node)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        text = OPS[type(node.op)]
        for left, right in zip(node.values, node.values[1:]):
            self.token(left, right, text, SWAP[text])
        self.generic_visit(node)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, (ast.Not, ast.USub)):
            operand = self.data[self.pos(node.operand.lineno, node.operand.col_offset):
                                self.pos(node.operand.end_lineno, node.operand.end_col_offset)]
            self.span(node, b"(" + operand + b")", ("not" if isinstance(node.op, ast.Not) else "-") + " -> (removed)")
        self.generic_visit(node)

    # deadcode: keep (ast.NodeVisitor dispatches visit_<Node> by name)
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            self.span(node, str(not node.value).encode(), f"{node.value} -> {not node.value}")
        elif type(node.value) is int and node.value in (0, 1):
            self.span(node, str(1 - node.value).encode(), f"{node.value} -> {1 - node.value}")


def mutants(root: str, rel: str, lines: set[int] | None) -> list[dict]:
    with open(os.path.join(root, rel), "rb") as handle:
        data = handle.read()
    try:
        sites = Sites(data)
        sites.visit(ast.parse(data, rel))
    except (SyntaxError, ValueError) as exc:
        print(f"mutate: skipped {rel}: {exc}", file=sys.stderr)
        return []
    found = []
    for start, stop, repl, change, line, function in sorted(set(sites.found)):
        mutated = data[:start] + repl + data[stop:]
        if lines is not None and line not in lines:
            continue
        try:
            compile(mutated, rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            continue
        found.append(dict(path=rel, line=line, change=change, function=function, source=data, mutated=mutated))
    return found


def changed_lines(root: str, ref: str) -> dict[str, set[int]]:
    """The lines new or changed in each file of `git diff REF` (the working tree against REF), read from the hunk
    headers of a diff without context; a file whose diff only deletes lines maps to an empty set."""
    diff = subprocess.run(["git", "-C", root, "diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                           "--dst-prefix=b/", ref, "--"], capture_output=True, text=True)
    if diff.returncode:
        sys.exit(f"mutate: git diff {ref} failed: {diff.stderr.strip()}")
    found: dict[str, set[int]] = {}
    path = None
    for text in diff.stdout.splitlines():
        if text.startswith("+++ "):
            path = text[len("+++ b/"):] if text.startswith("+++ b/") else None  # +++ /dev/null: a deleted file
            if path is not None:
                found.setdefault(path, set())
        elif path is not None and text.startswith("@@ "):
            hunk = HUNK.match(text)
            if not hunk:
                continue
            start, count = int(hunk.group(1)), 1 if hunk.group(2) is None else int(hunk.group(2))
            found[path].update(range(start, start + count))
    return found


def test_map(root: str) -> list[str]:
    raw = os.environ.get("HARNESS_TEST_MAP")
    if raw is None:
        try:
            with open(os.path.join(root, ".claude", "settings.json")) as handle:
                raw = json.load(handle).get("env", {}).get("HARNESS_TEST_MAP", "")
        except (OSError, ValueError, AttributeError):
            raw = ""
    return [entry for entry in raw.split(",") if "=" in entry]


def find_tests(root: str, pattern: str) -> set[str]:
    full = os.path.join(root, pattern if "/" in pattern else os.path.join(DEFAULT_TESTS, pattern))
    return {os.path.relpath(p, root).replace(os.sep, "/") for p in glob.glob(full, recursive=True) if p.endswith(".py")}


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def name_keys(rel: str) -> tuple[set[str], set[str]]:
    """harness-red's name rule, mirrored: the test subjects that name rel (each `_`-token suffix of its normalised
    path without the extension, and each directory's normalised name), and the suffixes alone, whose split suites
    `test_<suffix>_*.py` name it too."""
    *dirs, base = rel.split("/")
    tokens = normalise("/".join(dirs + [os.path.splitext(base)[0]])).split("_")
    suffixes = {"_".join(tokens[i:]) for i in range(len(tokens))} - {""}
    return suffixes | ({normalise(d) for d in dirs} - {""}), suffixes


def tests_for(root: str, rel: str, patterns: list[str] | None) -> tuple:
    if patterns:
        return tuple(sorted(set().union(*(find_tests(root, p) for p in patterns))))
    found = set()
    for entry in test_map(root):
        source, _, tests = entry.partition("=")
        if fnmatch.fnmatch(rel, source.strip()):
            found |= find_tests(root, tests.strip())
    match = CONVENTION.match(rel)
    if match:
        names, suffixes = name_keys(rel)
        for test in find_tests(root, f"{match.group('base')}tests/test_*.py"):
            subject = normalise(test.rsplit("/", 1)[-1][len("test_"):-len(".py")])
            if subject in names or any(subject.startswith(key + "_") for key in suffixes):
                found.add(test)
    return tuple(sorted(found))


def copy_tree(src: str, dst: str, rel: str = "") -> None:
    """Copy src into dst but NOT_COPIED: clones with `cp -c` on macOS, a plain copy elsewhere or where cloning fails."""
    os.makedirs(dst, exist_ok=True)
    batch = []
    for name in sorted(os.listdir(src)):
        sub, full = rel + name, os.path.join(src, name)
        if sub in NOT_COPIED:
            continue
        if any(skip.startswith(sub + "/") for skip in NOT_COPIED) and os.path.isdir(full) and not os.path.islink(full):
            copy_tree(full, os.path.join(dst, name), sub + "/")
        else:
            batch.append(full)
    if not batch:
        return
    if sys.platform == "darwin" and subprocess.run(["cp", "-cR", *batch, dst], capture_output=True).returncode == 0:
        return
    for full in batch:
        target = os.path.join(dst, os.path.basename(full))
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.copytree(full, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(full, target, follow_symlinks=False)


def write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
    stamp = next(STAMPS)
    os.utime(path, (stamp, stamp))


def run_tests(work: str, tree: str, tests: tuple, timeout: float | None) -> tuple[str, str]:
    """('passed' | 'failed' | 'timeout', output of a failed run); one unittest process per tests directory."""
    groups: dict[str, list[str]] = {}
    for test in tests:
        groups.setdefault(os.path.dirname(test), []).append(os.path.basename(test)[:-3])
    deadline = None if timeout is None else time.monotonic() + timeout
    log = os.path.join(work, "log")
    for directory, modules in sorted(groups.items()):
        with open(log, "w+b") as out:
            proc = subprocess.Popen([sys.executable, "-m", "unittest", "-q", "-f", *modules], stdout=out,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=os.path.join(tree, directory), start_new_session=True)
            try:
                proc.wait(None if deadline is None else max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return "timeout", ""
            if proc.returncode:
                out.seek(0)
                return "failed", out.read().decode(errors="replace")
    return "passed", ""


def counts() -> dict:
    return {"killed": 0, "survived": 0, "timed_out": 0}


def report(results: list[tuple[dict, str]]) -> dict:
    total, files, survivors = counts(), {}, []
    for mutant, outcome in results:
        entry = files.setdefault(mutant["path"], dict(path=mutant["path"], **counts(), functions={}))
        function = entry["functions"].setdefault(mutant["function"], counts())
        for tally in (total, entry, function):
            tally["survived" if outcome == "passed" else "killed"] += 1
            tally["timed_out"] += outcome == "timeout"
        if outcome == "passed":
            survivors.append({key: mutant[key] for key in ("path", "line", "change", "function")})
    survivors.sort(key=lambda s: (s["path"], s["line"], s["change"]))
    return dict(total, files=[files[path] for path in sorted(files)], survivors=survivors)


def tally_text(tally: dict) -> str:
    return f"{tally['killed']} killed ({tally['timed_out']} timed out), {tally['survived']} survived"


def run(args: argparse.Namespace) -> int:
    root = os.path.realpath(os.getcwd())
    files = source_files(root, args.paths or DEFAULT_PATHS)
    if args.tests and not tests_for(root, "", args.tests):
        sys.exit(f"mutate: no test module matches {' '.join(args.tests)}")
    lines = {int(n) for n in args.lines.split(",") if n.strip()} if args.lines else None
    keep = {rel: lines for rel in files}
    if args.changed:
        changed = changed_lines(root, args.changed)
        keep = {rel: changed[rel] if lines is None else changed[rel] & lines for rel in files if changed.get(rel)}
    jobs = []
    for rel in sorted(keep):
        found = mutants(root, rel, keep[rel])
        tests = tests_for(root, rel, args.tests)
        if found and not tests:
            print(f"mutate: skipped {rel}: no test module found", file=sys.stderr)
            continue
        jobs += [dict(m, tests=tests) for m in found]
    print(f"mutate: {len(jobs)} mutants in {len({j['path'] for j in jobs})} files", file=sys.stderr)
    baselines = sorted({job["tests"] for job in jobs})
    workers = max(1, min(args.workers or os.cpu_count() or 1, max(len(jobs), 1)))
    free: queue.Queue = queue.Queue()
    made = []
    try:
        for _ in range(workers if jobs else 0):
            work = tempfile.mkdtemp(prefix="mutate-")
            made.append(work)
            tree = os.path.join(work, os.path.basename(root))
            copy_tree(root, tree)
            free.put((work, tree))

        def in_copy(job: dict | None, tests: tuple, timeout: float | None) -> tuple[str, str, float]:
            work, tree = free.get()
            try:
                began = time.monotonic()
                if job is None:
                    return (*run_tests(work, tree, tests, timeout), time.monotonic() - began)
                target = os.path.join(tree, job["path"])
                write(target, job["mutated"])
                try:
                    return (*run_tests(work, tree, tests, timeout), 0.0)
                finally:
                    write(target, job["source"])
            finally:
                free.put((work, tree))

        with ThreadPoolExecutor(workers) as pool:
            base = dict(zip(baselines, pool.map(lambda tests: in_copy(None, tests, None), baselines)))
            failed = [(tests, out) for tests, (outcome, out, _) in base.items() if outcome != "passed"]
            if failed:
                for tests, out in failed:
                    print(f"mutate: the baseline fails for {' '.join(tests)}:\n{out[-3000:]}", file=sys.stderr)
                return 2
            limits = {tests: max(2.0, 10 * seconds) for tests, (_, _, seconds) in base.items()}
            outcomes = list(pool.map(lambda job: in_copy(job, job["tests"], limits[job["tests"]])[0], jobs))
    finally:
        for work in made:
            shutil.rmtree(work, ignore_errors=True)
    result = report(list(zip(jobs, outcomes)))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for entry in result["files"]:
            print(f"{entry['path']}: {tally_text(entry)}")
            for name, tally in sorted(entry["functions"].items()):
                print(f"  {name}: {tally_text(tally)}")
        for s in result["survivors"]:
            print(f"survived: {s['path']}:{s['line']} in {s['function']}: {s['change']}")
        print(f"total: {tally_text(result)}")
    return 3 if result["survived"] else 0


class Parser(argparse.ArgumentParser):
    """A usage error exits 1 like every other bad invocation, not argparse's 2 (a failing baseline)."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="mutate.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("run", help="mutate the files and run their tests against each mutant")
    cmd.add_argument("paths", nargs="*", metavar="PATH")
    cmd.add_argument("--changed", metavar="REF", help="only the sites on lines new or changed since REF")
    cmd.add_argument("--lines", metavar="N,N", help="only the sites on these lines")
    cmd.add_argument("--tests", nargs="+", metavar="PATTERN", help="the test modules to run for every file")
    cmd.add_argument("--workers", type=int, help="copies run at once (default: the cores)")
    cmd.add_argument("--json", action="store_true", help="print the report as one JSON object")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
