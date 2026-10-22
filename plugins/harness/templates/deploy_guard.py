#!/usr/bin/env python3
"""
The deploy guard: after a merge to main deploys and the live smoke check fails, revert the merge, redeploy,
check again and tell the owner.

Runs in the project checkout (the cwd: on main, full history), from the deploy workflow:

    python3 scripts/deploy_guard.py revert --sha "$GITHUB_SHA" [--smoke-log smoke.log]

1. Revert: unless origin/main already has a commit saying `This reverts commit SHA`, `git revert --no-edit SHA` on
   top of origin/main as github-actions[bot] and `git push origin HEAD:main`. Then scripts/deploy.sh (when present)
   and scripts/smoke.sh run again, the smoke bounded by DEPLOY_GUARD_SMOKE_TIMEOUT seconds (default 600).
2. Incident: unless an open `incident` issue's body has the line `Merge commit: SHA`, open
   `Incident: <PR title> reverted` (no PR: the commit's subject), labelled `incident` and assigned to the repository
   owner, naming the merge commit, PR, ticket and revert commit, with the last 60 lines of the --smoke-log fenced.
3. Ticket (the issue the merge's pull request closes): reopen it, replace its status:* labels with
   status:needs-input and ask in a harness needs-input comment, unless that identical comment is already there.
A conflicting revert, a rejected push, a failed redeploy or a failing second smoke freezes deploys: the incident is
`Incident: <PR title>: revert failed, deploys frozen`, also labelled `deploy-frozen`, and the exit is 1.

Standard library and gh only. gh is HARNESS_GH, else `gh` (GH_TOKEN authenticates it); the repository is
GITHUB_REPOSITORY, else `gh repo view`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from typing import NamedTuple

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SMOKE_TIMEOUT = 600.0
TAIL_LINES = 60
INCIDENT, FROZEN, NEEDS_INPUT_LABEL = "incident", "deploy-frozen", "status:needs-input"
NEEDS_INPUT = "**Needs your input**"
MARKER = "<!-- harness -->"     # the last line of every harness comment


class GhError(Exception):
    pass


class PullRequest(NamedTuple):
    number: int
    title: str
    url: str
    ticket: int | None


def say(message):
    print(f"deploy_guard: {message}", flush=True)


def warn(message):
    print(f"deploy_guard: {message}", file=sys.stderr, flush=True)


def one_line(text, limit=300):
    return " ".join((text or "").split())[:limit]


def run(argv, stdin=None, env=None):
    """(exit, stdout, stderr); stdin is closed unless text is given."""
    try:
        p = subprocess.run(argv, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, env=env)
    except OSError as exc:
        return 127, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def git(*args, env=None):
    return run(["git", *args], env=env)


def gh(*args, stdin=None):
    code, out, err = run([os.environ.get("HARNESS_GH") or "gh", *args], stdin)
    if code:
        raise GhError(f"gh {' '.join(args[:2])} failed: {one_line(err or out)}")
    return out


def gh_json(*args):
    out = gh(*args)
    try:
        return json.loads(out)
    except ValueError:
        raise GhError(f"gh {' '.join(args[:2])} printed no JSON: {one_line(out)}") from None


# --------------------------------------------------------------- revert, redeploy, smoke
def full_sha(sha):
    code, out, _ = git("rev-parse", "--verify", "--quiet", sha + "^{commit}")
    return out.strip() if code == 0 and out.strip() else sha


def existing_revert(sha):
    """The newest commit on origin/main saying `This reverts commit SHA`, or None."""
    code, out, _ = git("log", "--format=%H", "--fixed-strings", f"--grep=This reverts commit {sha}", "origin/main")
    lines = out.split() if code == 0 else []
    return lines[0] if lines else None


def revert_on_main(sha):
    """Revert sha on top of origin/main and push it, once: (revert sha, None) or (None, why it failed)."""
    code, _, err = git("fetch", "--quiet", "origin", "main")
    if code:
        return None, f"git fetch origin main failed: {one_line(err)}"
    found = existing_revert(sha)
    if found:
        say(f"origin/main already reverts {sha[:7]} in {found[:7]}; not reverting again")
        return found, None
    code, _, err = git("checkout", "--quiet", "--detach", "origin/main")
    if code:
        return None, f"cannot check out origin/main: {one_line(err)}"
    bot = dict(os.environ, GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL, GIT_COMMITTER_NAME=BOT_NAME,
               GIT_COMMITTER_EMAIL=BOT_EMAIL)
    code, out, err = git("-c", "commit.gpgsign=false", "revert", "--no-edit", sha, env=bot)
    if code:
        git("revert", "--abort")
        return None, f"git revert {sha[:7]} did not apply: {one_line(err or out)}"
    revert_sha = git("rev-parse", "HEAD")[1].strip()
    code, _, err = git("push", "--quiet", "origin", "HEAD:main")
    if code:
        return None, f"git push origin HEAD:main was rejected: {one_line(err)}"
    say(f"reverted {sha[:7]} in {revert_sha[:7]} and pushed it to main")
    return revert_sha, None


def run_script(path, timeout=None):
    """Run a project script with our stdout and stderr; its exit, or None past the timeout (its group killed)."""
    argv = [os.path.join(".", path)] if os.access(path, os.X_OK) else ["sh", path]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        warn(f"cannot run {path}: {exc}")
        return 127
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return None


def smoke_timeout():
    raw = os.environ.get("DEPLOY_GUARD_SMOKE_TIMEOUT", "").strip()
    if not raw:
        return SMOKE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value > 0:
        return value
    warn(f"DEPLOY_GUARD_SMOKE_TIMEOUT={raw!r} is not a positive number of seconds; using {SMOKE_TIMEOUT:g}")
    return SMOKE_TIMEOUT


def redeploy():
    """scripts/deploy.sh when present, then scripts/smoke.sh: None, or why it failed."""
    deploy, smoke = os.path.join("scripts", "deploy.sh"), os.path.join("scripts", "smoke.sh")
    if os.path.isfile(deploy):
        say("redeploying with scripts/deploy.sh")
        code = run_script(deploy)
        if code:
            return f"scripts/deploy.sh exited {code} after the revert"
    if not os.path.isfile(smoke):
        return "scripts/smoke.sh is missing, so the revert cannot be checked"
    timeout = smoke_timeout()
    say("checking the revert with scripts/smoke.sh")
    code = run_script(smoke, timeout)
    if code is None:
        return f"scripts/smoke.sh ran past {timeout:g} s after the revert"
    if code:
        return f"scripts/smoke.sh exited {code} after the revert"
    return None


# --------------------------------------------------------------- GitHub: the PR, the incident, the ticket
def repository():
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug:
        data = gh_json("repo", "view", "--json", "nameWithOwner")
        slug = (data.get("nameWithOwner") or "") if isinstance(data, dict) else ""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        raise GhError(f"cannot tell the repository (owner/name): {slug!r}")
    return slug


def closing_ticket(refs, slug):
    """The first issue in this repository that the pull request closes."""
    owner, name = slug.lower().split("/")
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or not ref.get("number"):
            continue
        repo = ref.get("repository") or {}
        if repo and (((repo.get("owner") or {}).get("login") or "").lower(), (repo.get("name") or "").lower()) != (
                owner, name):
            continue
        return int(ref["number"])
    return None


def pull_request(slug, sha):
    """The pull request that merged sha (its merge commit, else a merged one containing it), or None."""
    pulls = gh_json("api", f"repos/{slug}/commits/{sha}/pulls")
    pulls = [p for p in pulls if isinstance(p, dict) and p.get("number")] if isinstance(pulls, list) else []
    pr = next((p for p in pulls if p.get("merge_commit_sha") == sha), None) or next(
        (p for p in pulls if p.get("merged_at")), None)
    if pr is None:
        return None
    number, title, url, ticket = int(pr["number"]), pr.get("title") or "", pr.get("html_url") or "", None
    try:
        view = gh_json("pr", "view", str(number), "--repo", slug, "--json", "title,url,closingIssuesReferences")
        title, url = view.get("title") or title, view.get("url") or url
        ticket = closing_ticket(view.get("closingIssuesReferences"), slug)
    except GhError as exc:
        warn(f"{exc}; naming no ticket")
    return PullRequest(number, title, url, ticket)


def open_incident(slug, sha):
    """The open incident whose body has the line `Merge commit: SHA`, or None."""
    issues = gh_json("issue", "list", "--repo", slug, "--label", INCIDENT, "--state", "open", "--limit", "200",
                     "--json", "number,title,body,labels,state")
    line = f"Merge commit: {sha}"
    for issue in issues if isinstance(issues, list) else []:
        if isinstance(issue, dict) and any(row.strip() == line for row in (issue.get("body") or "").splitlines()):
            return int(issue["number"])
    return None


def smoke_tail(path):
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-TAIL_LINES:]
    except OSError as exc:
        warn(f"cannot read the smoke log: {exc}")
        return []


def incident_body(sha, pr, revert_sha, failure, smoke_log):
    lines = [f"Merge commit: {sha}"]
    if pr:
        lines.append(f"PR: #{pr.number} {pr.url}".rstrip())
        if pr.ticket:
            lines.append(f"Ticket: #{pr.ticket}")
    if revert_sha:
        lines.append(f"Revert commit: {revert_sha}")
    if failure:
        lines += ["", f"Deploys are frozen: {failure}."]
    tail = smoke_tail(smoke_log)
    if tail:
        longest = max((len(run) for run in re.findall(r"`{3,}", "\n".join(tail))), default=0)
        fence = "`" * max(3, longest + 1)
        lines += ["", f"The live smoke check that failed (last {len(tail)} lines):", "", fence, *tail, fence]
    return "\n".join(lines) + "\n"


def create_incident(slug, title, body, frozen):
    labels = f"{INCIDENT},{FROZEN}" if frozen else INCIDENT
    out = gh("issue", "create", "--repo", slug, "--title", title, "--label", labels, "--assignee",
             slug.split("/")[0], "--body-file", "-", stdin=body)
    found = re.search(r"/issues/(\d+)", out)
    if not found:
        raise GhError(f"gh issue create printed no issue URL: {one_line(out)}")
    return int(found.group(1))


def ask_on_ticket(slug, number, revert_sha, incident):
    """Reopen the ticket as needs-input with the question, unless that identical comment is already there."""
    question = (f"{NEEDS_INPUT}\n\nReverted in {revert_sha[:7]} after the live smoke check failed: incident "
                f"#{incident}.\n\nReply in a comment; /harness:work {number} picks it up.\n\n{MARKER}")
    issue = gh_json("issue", "view", str(number), "--repo", slug, "--json", "state,labels,comments")
    issue = issue if isinstance(issue, dict) else {}
    if (issue.get("state") or "").upper() != "OPEN":
        gh("issue", "reopen", str(number), "--repo", slug)
    labels = [label.get("name") or "" for label in issue.get("labels") or [] if isinstance(label, dict)]
    stale = [name for name in labels if name.startswith("status:") and name != NEEDS_INPUT_LABEL]
    edit = ([] if NEEDS_INPUT_LABEL in labels else ["--add-label", NEEDS_INPUT_LABEL]) + (
        ["--remove-label", ",".join(stale)] if stale else [])
    if edit:
        gh("issue", "edit", str(number), "--repo", slug, *edit)
    comments = [c for c in issue.get("comments") or [] if isinstance(c, dict)]
    if any((c.get("body") or "").strip() == question for c in comments):
        say(f"#{number} already has the question")
        return
    gh("issue", "comment", str(number), "--repo", slug, "--body-file", "-", stdin=question)
    say(f"reopened #{number} as needs-input")


def report(sha, revert_sha, failure, smoke_log):
    """The incident and the ticket's question; False when GitHub could not be told."""
    try:
        slug = repository()
    except GhError as exc:
        warn(f"{exc}; no incident opened")
        return False
    pr = None
    try:
        pr = pull_request(slug, sha)
    except GhError as exc:
        warn(f"{exc}; treating {sha[:7]} as a commit without a pull request")
    title = (pr.title if pr else "") or git("log", "-1", "--format=%s", sha)[1].strip() or sha[:7]
    try:
        incident = open_incident(slug, sha)
        if incident:
            say(f"incident #{incident} is already open for {sha[:7]}")
            if failure:
                gh("issue", "edit", str(incident), "--repo", slug, "--add-label", FROZEN)
        else:
            heading = f"Incident: {title}: revert failed, deploys frozen" if failure else f"Incident: {title} reverted"
            incident = create_incident(slug, heading, incident_body(sha, pr, revert_sha, failure, smoke_log),
                                       bool(failure))
            say(f"opened incident #{incident}")
        if revert_sha and pr and pr.ticket:
            ask_on_ticket(slug, pr.ticket, revert_sha, incident)
    except GhError as exc:
        warn(str(exc))
        return False
    return True


def cmd_revert(args):
    sha = full_sha(args.sha)
    revert_sha, failure = revert_on_main(sha)
    if not failure:
        failure = redeploy()
    if failure:
        warn(f"deploys frozen: {failure}")
    reported = report(sha, revert_sha, failure, args.smoke_log)
    return 1 if failure or not reported else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="deploy_guard.py", description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    revert = sub.add_parser("revert", help="revert a merge whose live smoke check failed")
    revert.add_argument("--sha", required=True, help="the merge commit on main")
    revert.add_argument("--smoke-log", help="the failing smoke check's output, for the incident")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.sha.strip()):
        parser.error(f"--sha: not a commit sha: {args.sha!r}")
    args.sha = args.sha.strip()
    return cmd_revert(args)


if __name__ == "__main__":
    sys.exit(main())
