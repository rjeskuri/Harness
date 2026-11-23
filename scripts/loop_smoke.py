#!/usr/bin/env python3
"""The loop smoke run's deterministic half, against the permanent sandbox repository `harness-sandbox`.

Usage:
    python3 scripts/loop_smoke.py check --sandbox PATH --ticket N [--json]
    python3 scripts/loop_smoke.py reset --sandbox PATH [--json]

Guard, both commands: PATH must be a git clone whose `origin` URL's last path part, without `.git`, is
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

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

SANDBOX = "harness-sandbox"
TITLE = "Loop smoke"
COMMENT = "Closed by loop smoke reset"
TIMEOUT = 120
REFUSED = 2


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
    args = parser.parse_args()
    try:
        path, repo = guard(args.sandbox)
    except Refused as exc:
        print(f"loop_smoke: {exc}", file=sys.stderr)
        return REFUSED
    if args.command == "check":
        return check(path, repo, args.ticket, args.json)
    return reset(path, repo, args.json)


if __name__ == "__main__":
    sys.exit(main())
