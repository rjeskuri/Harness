#!/usr/bin/env python3
"""The loop smoke run, against the permanent sandbox repository `harness-sandbox`: `run` drives one ticket through
a headless `/harness:work`, and `check` and `reset` are its deterministic half.

Usage:
    python3 scripts/loop_smoke.py run --sandbox PATH [--plugin DIR] [--timeout MINUTES]
    python3 scripts/loop_smoke.py check --sandbox PATH --ticket N [--json]
    python3 scripts/loop_smoke.py reset --sandbox PATH [--json]

Guard, every command: PATH must be a git clone whose `origin` URL's last path part, without `.git`, is
`harness-sandbox`. Anything else prints one line and exits 2, calling no gh and changing nothing.
gh is HARNESS_GH, else `gh`, run with PATH as its working directory, with `-R OWNER/REPO` when origin is a
github.com URL (gh then works on the repository the guard checked; a local origin gets no -R).

check: three assertions, one stdout line each, starting `ok ` or `FAIL `: issue N is CLOSED; a pull request
whose head branch starts `t/N-` is MERGED; no worktree of `t/N-*` remains (none registered in
`git worktree list`, no folder PATH/.claude/worktrees/t-N-*). Exit 0 when all three hold, else 1.
--json prints {"ticket": N, "ok": bool, "checks": [{"name", "ok", "detail"}]} instead.

reset: `git switch main`; then for each OPEN issue whose title starts `Loop smoke`, remove its local worktrees
(--force), close its open pull requests from `t/<n>-*` (deleting their branches), delete its local branches
and close the issue with the comment `Closed by loop smoke reset`; then `git pull --ff-only`. One line per
action; exit 0, or 1 when any action failed (the remaining actions still run). It never pushes and never
touches an issue whose title does not start `Loop smoke`. --json prints {"ok": bool, "actions": [...]}.

run: (a) reset, its lines printed (a failed reset stops here, exit 1); (b) `DIR/bin/harness-board task create
--repo PATH --title "Loop smoke <UTC stamp>" --body-file - --size S --risk low` (status:ready), the body asking that
`stamp()` in src/harness_sandbox/stamp.py return the stamp; (c) `claude -p "/harness:work N" --permission-mode auto
--output-format json` (HARNESS_CLAUDE, else claude), plus with --plugin `--plugin-dir DIR --settings
'{"enabledPlugins": {"harness@ryan": false}}'`, in PATH, standard input closed, in a session of its own, with DIR/bin
first on PATH (DIR defaults to this checkout's plugins/harness) and its output in ~/.claude/harness/smoke/<stamp>.log;
(d) wait up to --timeout minutes (decimals allowed, default 45), then kill the session's process group; (e) check N,
its lines printed, then the log path and the elapsed time. Exit: check's. A failed ticket create, a failed launch
or a timeout prints one `FAIL ...` line naming it and exits 1 (a timeout runs no check). It never pushes and never
registers the sandbox in projects.json; it runs on the claude login (no apiKeyHelper), the subscription.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

SANDBOX = "harness-sandbox"
TITLE = "Loop smoke"
COMMENT = "Closed by loop smoke reset"
TIMEOUT = 120
REFUSED = 2
DEFAULT_PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "harness")
DISABLE_INSTALLED = json.dumps({"enabledPlugins": {"harness@ryan": False}})
KILL_GRACE = 10


class Refused(Exception):
    pass


def run(argv: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def git(path: str, *args: str) -> tuple[int, str, str]:
    return run(["git", *args], path)


def gh(path: str, *args: str, repo: str | None = None) -> tuple[int, str, str]:
    pin = ["-R", repo] if repo else []
    return run([os.environ.get("HARNESS_GH", "gh"), *args, *pin], path)


def gh_json(path: str, *args: str, repo: str | None = None):
    code, out, err = gh(path, *args, repo=repo)
    if code != 0:
        raise RuntimeError(one_line(err or out) or f"gh exited {code}")
    try:
        return json.loads(out)
    except ValueError as exc:
        raise RuntimeError(f"gh printed no JSON: {exc}") from None


def one_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def github_repo(url: str) -> str | None:
    """OWNER/REPO of a github.com remote URL (https, ssh or scp form), else None."""
    parts = url.rstrip("/").replace(":", "/").split("/")
    if len(parts) < 3 or not any(p == "github.com" or p.endswith("@github.com") for p in parts[:-2]):
        return None
    name = parts[-1][: -len(".git")] if parts[-1].endswith(".git") else parts[-1]
    return f"{parts[-2]}/{name}" if parts[-2] and name else None


def guard(path: str) -> tuple[str, str | None]:
    """The sandbox clone's absolute path and its github.com OWNER/REPO (None for another origin), or Refused."""
    if not os.path.isdir(path):
        raise Refused(f"{path} is not a folder")
    path = os.path.abspath(path)
    code, out, err = git(path, "remote", "get-url", "origin")
    url = out.strip()
    if code != 0 or not url:
        raise Refused(f"{path} is not a git clone with an origin: {one_line(err) or 'no origin'}")
    last = url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    if last.endswith(".git"):
        last = last[: -len(".git")]
    if last != SANDBOX:
        raise Refused(f"refused: origin of {path} is {url}, not {SANDBOX}")
    return path, github_repo(url)


def worktrees(path: str) -> list[tuple[str, str]]:
    """(folder, branch) for every worktree with a branch checked out."""
    code, out, err = git(path, "worktree", "list", "--porcelain")
    if code != 0:
        raise RuntimeError(one_line(err) or "git worktree list failed")
    found, folder = [], ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            folder = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            found.append((folder, line[len("branch refs/heads/"):]))
    return found


# ---------------------------------------------------------------------------------------------- check

def check_issue(path: str, n: int, repo: str | None) -> tuple[bool, str]:
    try:
        state = str(gh_json(path, "issue", "view", str(n), "--json", "state", repo=repo).get("state", ""))
    except (RuntimeError, AttributeError) as exc:
        return False, f"issue #{n}: {exc}"
    return state == "CLOSED", f"issue #{n} is {state or 'of no state'}"


def check_pr(path: str, n: int, repo: str | None) -> tuple[bool, str]:
    try:
        prs = gh_json(path, "pr", "list", "--state", "all", "--json", "number,state,headRefName", "--limit", "100",
                      repo=repo)
        ours = [pr for pr in prs if str(pr.get("headRefName", "")).startswith(f"t/{n}-")]
    except (RuntimeError, AttributeError, TypeError) as exc:
        return False, f"pull requests: {exc}"
    merged = [pr for pr in ours if pr.get("state") == "MERGED"]
    if merged:
        return True, f"PR #{merged[0]['number']} from {merged[0]['headRefName']} is MERGED"
    if not ours:
        return False, f"no pull request from t/{n}-*"
    seen = ", ".join(f"#{pr.get('number')} {pr.get('state')}" for pr in ours)
    return False, f"no merged pull request from t/{n}-* ({seen})"


def check_worktree(path: str, n: int, repo: str | None) -> tuple[bool, str]:
    try:
        trees = [(folder, branch) for folder, branch in worktrees(path) if branch.startswith(f"t/{n}-")]
    except RuntimeError as exc:
        return False, f"worktrees: {exc}"
    registered = {os.path.realpath(folder) for folder, _ in trees}
    left = [f"{folder} ({branch})" for folder, branch in trees]
    left += [folder for folder in sorted(glob.glob(os.path.join(path, ".claude", "worktrees", f"t-{n}-*")))
             if os.path.realpath(folder) not in registered]
    if left:
        return False, f"worktree of t/{n}-* left: {', '.join(left)}"
    return True, f"no worktree of t/{n}-* left"


def check(path: str, repo: str | None, n: int, as_json: bool) -> int:
    checks = []
    for name, fn in (("issue", check_issue), ("pr", check_pr), ("worktree", check_worktree)):
        ok, detail = fn(path, n, repo)
        checks.append({"name": name, "ok": ok, "detail": detail})
    all_ok = all(c["ok"] for c in checks)
    if as_json:
        print(json.dumps({"ticket": n, "ok": all_ok, "checks": checks}))
    else:
        for c in checks:
            print(f"{'ok' if c['ok'] else 'FAIL'} {c['detail']}")
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------------------------- reset

class Actions:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self.done: list[dict] = []

    def record(self, action: str, ok: bool, detail: str) -> bool:
        self.done.append({"action": action, "ok": ok, "detail": detail})
        if not self.as_json:
            print(f"{'ok' if ok else 'FAIL'} {detail}", flush=True)
        return ok

    def command(self, action: str, result: tuple[int, str, str], detail: str) -> bool:
        code, out, err = result
        if code == 0:
            return self.record(action, True, detail)
        return self.record(action, False, f"{detail}: {one_line(err or out) or f'exit {code}'}")

    @property
    def ok(self) -> bool:
        return all(a["ok"] for a in self.done)


def reset(path: str, repo: str | None, as_json: bool) -> int:
    acts = Actions(as_json)
    # first, so the clone itself has no t/<n>-* branch checked out when those branches are deleted
    on_main = acts.command("switch main", git(path, "switch", "main"), "switched to main")
    issues, prs = [], []
    try:
        found = gh_json(path, "issue", "list", "--state", "open", "--search", f"{TITLE} in:title",
                        "--json", "number,title", "--limit", "100", repo=repo)
        issues = [i for i in found if str(i.get("title", "")).startswith(TITLE)]
        acts.record("list issues", True, f"{len(issues)} open {TITLE} issue(s)"
                    + (": " + ", ".join(f"#{i['number']}" for i in issues) if issues else ""))
    except (RuntimeError, AttributeError, TypeError) as exc:
        acts.record("list issues", False, f"list open {TITLE} issues: {exc}")
    if issues:
        try:
            prs = gh_json(path, "pr", "list", "--state", "open", "--json", "number,state,headRefName",
                          "--limit", "100", repo=repo)
            prs = [pr for pr in prs if pr.get("state", "OPEN") == "OPEN"]
        except (RuntimeError, AttributeError, TypeError) as exc:
            acts.record("list prs", False, f"list open pull requests: {exc}")
            prs = []

    for issue in issues:
        n = issue["number"]
        prefix = f"t/{n}-"
        try:
            trees = [(folder, branch) for folder, branch in worktrees(path) if branch.startswith(prefix)]
        except RuntimeError as exc:
            acts.record("remove worktree", False, f"list worktrees of {prefix}*: {exc}")
            trees = []
        for folder, branch in trees:
            if os.path.realpath(folder) == os.path.realpath(path):
                continue  # the clone itself is still on that branch: `git switch main` above failed
            acts.command("remove worktree", git(path, "worktree", "remove", "--force", folder),
                         f"removed worktree {folder} ({branch})")
        # after the worktrees are gone: without -R, gh also deletes the local branch, which git refuses while a
        # worktree has it checked out, and gh then stops before deleting the remote branch
        for pr in prs:
            head = str(pr.get("headRefName", ""))
            if head.startswith(prefix):
                p = str(pr["number"])
                acts.command("close pr", gh(path, "pr", "close", p, "--delete-branch", repo=repo),
                             f"closed PR #{p} ({head}) and deleted its branch")
        code, out, err = git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        if code != 0:
            acts.record("delete branch", False, f"list local branches of {prefix}*: {one_line(err)}")
        for branch in out.split() if code == 0 else []:
            if branch.startswith(prefix):
                acts.command("delete branch", git(path, "branch", "-D", branch), f"deleted local branch {branch}")
        acts.command("close issue", gh(path, "issue", "close", str(n), "--comment", COMMENT, repo=repo),
                     f"closed issue #{n} ({issue.get('title', '')})")

    if on_main:
        acts.command("pull", git(path, "pull", "--ff-only"), "fast-forwarded main")
    else:
        acts.record("pull", False, "did not pull: not on main")
    if as_json:
        print(json.dumps({"ok": acts.ok, "actions": acts.done}))
    return 0 if acts.ok else 1


# ---------------------------------------------------------------------------------------------- run

def ticket_body(stamp: str) -> str:
    return f"""## Request
Loop smoke run {stamp}: make `stamp()` in `src/harness_sandbox/stamp.py` return exactly the string `{stamp}`.

## Outline
1. Change `stamp()` in `src/harness_sandbox/stamp.py` to return `"{stamp}"`.
2. Pin that value in `tests/test_stamp.py`.

## Definition of done
- [ ] `stamp()` returns exactly `"{stamp}"`.
- [ ] `tests/test_stamp.py` asserts that value.

## Tests that must pass
- `tests/test_stamp.py`: `stamp()` returns exactly `"{stamp}"`.
The existing suite (`scripts/verify.sh`) stays green.

## Context
- `src/harness_sandbox/stamp.py`: the one function this ticket changes.
- A loop smoke ticket, created by Harness's `scripts/loop_smoke.py run`; `reset` closes it if it is left open.
- Must not change: anything else.
"""


def session_env(plugin: str) -> dict:
    """This process's environment with the plugin's bin/ first on PATH."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(p for p in (os.path.join(plugin, "bin"), env.get("PATH", "")) if p)
    return env


def create_ticket(path: str, plugin: str, stamp: str) -> tuple[int | None, str]:
    """(the new issue's number, its URL), or (None, why not)."""
    argv = [os.path.join(plugin, "bin", "harness-board"), "task", "create", "--repo", path,
            "--title", f"{TITLE} {stamp}", "--body-file", "-", "--size", "S", "--risk", "low", "--json"]
    try:
        p = subprocess.run(argv, cwd=path, env=session_env(plugin), input=ticket_body(stamp),
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if p.returncode != 0:
        return None, one_line(p.stderr or p.stdout) or f"harness-board exited {p.returncode}"
    try:
        found = json.loads(p.stdout)
        return int(found["number"]), str(found.get("url") or "")
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"no issue number in harness-board's output: {exc}"


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the session's process group, then, after a grace period, SIGKILL whatever is left of it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def since(start: float) -> str:
    seconds = int(time.monotonic() - start)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_smoke(path: str, repo: str | None, plugin: str, plugin_dir: bool, minutes: float) -> int:
    start = time.monotonic()
    if reset(path, repo, False) != 0:
        print("loop_smoke: reset failed, so no ticket was created", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n, detail = create_ticket(path, plugin, stamp)
    if n is None:
        print(f"FAIL could not create the {TITLE} ticket: {detail}")
        return 1
    print(f"ok created #{n} {detail}".rstrip(), flush=True)

    folder = os.path.join(os.path.expanduser("~"), ".claude", "harness", "smoke")
    os.makedirs(folder, exist_ok=True)
    log = os.path.join(folder, f"{stamp}.log")
    claude = os.environ.get("HARNESS_CLAUDE") or "claude"
    argv = [claude, "-p", f"/harness:work {n}", "--permission-mode", "auto", "--output-format", "json"]
    if plugin_dir:
        argv += ["--plugin-dir", plugin, "--settings", DISABLE_INSTALLED]
    failed = None
    with open(log, "w", encoding="utf-8") as out:
        try:
            # a session of its own, so a timeout kills its whole process group and never this process
            proc = subprocess.Popen(argv, cwd=path, env=session_env(plugin), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            failed = f"FAIL could not launch {claude}: {exc}"
        else:
            print(f"launched /harness:work {n} (pid {proc.pid}), up to {minutes:g} min", flush=True)
            try:
                code = proc.wait(timeout=minutes * 60)
            except subprocess.TimeoutExpired:
                kill_group(proc)
                failed = f"FAIL session timed out after {minutes:g} min; killed its process group"
    if failed:
        print(failed)
        result = 1
    else:
        print(f"session exited {code}", flush=True)
        result = check(path, repo, n, False)
    print(f"log {log}")
    print(f"elapsed {since(start)}")
    return result


def minutes_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of minutes: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"not more than 0 minutes: {text!r}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(prog="loop_smoke.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="assert ticket N's issue closed, PR merged and worktree gone")
    p_check.add_argument("--sandbox", required=True, help="the local harness-sandbox clone")
    p_check.add_argument("--ticket", required=True, type=int, help="the issue number")
    p_check.add_argument("--json", action="store_true")
    p_reset = sub.add_parser("reset", help="close the open Loop smoke issues, their PRs and worktrees")
    p_reset.add_argument("--sandbox", required=True, help="the local harness-sandbox clone")
    p_reset.add_argument("--json", action="store_true")
    p_run = sub.add_parser("run", help="reset, create a Loop smoke ticket, run /harness:work on it headless, check")
    p_run.add_argument("--sandbox", required=True, help="the local harness-sandbox clone")
    p_run.add_argument("--plugin", metavar="DIR", help="load this plugin folder (a branch's plugins/harness) in "
                       "place of the installed harness")
    p_run.add_argument("--timeout", type=minutes_arg, default=45.0, metavar="MINUTES",
                       help="kill the session after this many minutes (decimals allowed, default 45)")
    args = parser.parse_args()
    try:
        path, repo = guard(args.sandbox)
    except Refused as exc:
        print(f"loop_smoke: {exc}", file=sys.stderr)
        return REFUSED
    if args.command == "check":
        return check(path, repo, args.ticket, args.json)
    if args.command == "run":
        plugin = os.path.abspath(os.path.expanduser(args.plugin)) if args.plugin else DEFAULT_PLUGIN
        return run_smoke(path, repo, plugin, bool(args.plugin), args.timeout)
    return reset(path, repo, args.json)


if __name__ == "__main__":
    sys.exit(main())
