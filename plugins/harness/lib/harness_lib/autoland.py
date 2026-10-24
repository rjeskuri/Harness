"""The auto-land gate harness-pr gate and harness-pr autoland apply: the gate's settings, read only
from the .claude/settings.json committed at a ref (gate_settings), the facts about one pull request
gathered from gh and git in the main checkout (gather_facts), and its ten rules over those facts
(evaluate), the first failing rule deciding. Also the statusCheckRollup reading harness-pr's checks
and land share (check_entries, overall). See docs/design.md (The auto-land gate).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import json
import re
import subprocess

from harness_lib.cli import Problem
from harness_lib.gh import describe, gh, gh_json, gh_run
from harness_lib.settings import DEFAULT_TEST_PATTERNS, committed_env, glob_match, patterns
from harness_lib.text import as_list, first_line, parse_iso

# Paths whose change always lands by hand; a project's HARNESS_MANUAL_PATHS adds to these.
DEFAULT_MANUAL_PATHS = (
    ".github/**", ".claude/**", "scripts/deploy.sh", "scripts/smoke.sh", "scripts/deploy_guard.py",
    "scripts/verify.sh", "**/credentials/**", "**/.env*", "**/*.pem",
    "pyproject.toml", "package.json", "**/conftest.py", "pytest.ini",
)
DEFAULT_SIZES = ["S", "M"]
SIZES = ("S", "M", "L")
MAX_NONTEST_FILES = 8  # above either, rule 5 answers too large whatever the size label says
MAX_NONTEST_LINES = 400

TICKET_BRANCH = re.compile(r"t/(\d+)-")
REVIEW_MARKER = "<!-- harness-review -->"
BOARD_MARKER = "<!-- harness -->"
REVIEW_COUNTS = re.compile(r"Harness review \([^)]*\): (\d+) findings?, (\d+) high, (\d+) medium, (\d+) low\.")
NO_FINDINGS = "Harness review: no findings."
NO_DEPLOY_WORKFLOW = re.compile(r"could not find any workflows? named", re.I)
FAILED = ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR", "ACTION_REQUIRED", "STARTUP_FAILURE")
PASSED = ("SUCCESS", "SKIPPED", "NEUTRAL")
PR_FIELDS = ("number,state,isDraft,headRefName,headRefOid,author,closingIssuesReferences,labels,mergeable,"
             "statusCheckRollup")
GIT_TIMEOUT = 120  # seconds any one git call of the gate may take
# Review threads, reviews and PR comments; harness-pr comments reads it too. __typename tells a bot (GraphQL
# gives a bot's login without the [bot] suffix).
CONVERSATION = """query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) { nodes { isResolved
        comments(first: 50) { nodes { author { login __typename } body path line createdAt url } } } }
      reviews(first: 50) { nodes { author { login __typename } body state createdAt url } }
      comments(first: 100) { nodes { author { login __typename } body createdAt url } }
    }
  }
}"""


def _committed(name, env):
    """settings.setting() for a committed value only: a string or number (never a bool),
    stripped, None when unset or blank."""
    value = env.get(name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def gate_settings(main, ref="origin/main"):
    """{autoland, sizes, manual_paths, smoke_local} from the `env` of the .claude/settings.json
    committed at `ref` in the repository at `main`; the process environment, the working tree and
    settings.local.json never count. HARNESS_AUTOLAND and HARNESS_SMOKE_LOCAL are on only at `1`;
    HARNESS_AUTOLAND_SIZES holding anything but S, M or L gives the default S,M;
    HARNESS_MANUAL_PATHS adds to DEFAULT_MANUAL_PATHS (defaults first, de-duplicated)."""
    env = committed_env(main, ref)
    sizes = list(dict.fromkeys(patterns(_committed("HARNESS_AUTOLAND_SIZES", env) or "")))
    if not sizes or any(size not in SIZES for size in sizes):
        sizes = list(DEFAULT_SIZES)
    project = patterns(_committed("HARNESS_MANUAL_PATHS", env) or "")
    return {
        "autoland": _committed("HARNESS_AUTOLAND", env) == "1",
        "sizes": sizes,
        "manual_paths": list(dict.fromkeys([*DEFAULT_MANUAL_PATHS, *project])),
        "smoke_local": _committed("HARNESS_SMOKE_LOCAL", env) == "1",
    }


# Each rule answers None when it passes, else (verdict, code, reason) for its first failing condition.
def _opted_in(facts, settings):
    if not settings.get("autoland"):
        return "manual", "off", "autoland off"
    return None


def _pull_request(facts, settings):
    number = (facts.get("ticket") or {}).get("number")
    state = facts.get("state")
    if state != "OPEN":
        return "manual", "pr", f"PR state is {state or 'empty'}, not OPEN"
    if facts.get("draft"):
        return "manual", "pr", "draft"
    head = facts.get("head_ref") or ""
    m = TICKET_BRANCH.match(head)
    if not m or int(m.group(1)) != number:
        return "manual", "pr", f"head {head or '(none)'} is not a ticket branch t/{number}-..."
    author, login = facts.get("author"), facts.get("login")
    if author != login:
        return "manual", "pr", f"author {author} is not {login}"
    closing = list(facts.get("closing") or [])
    if closing != [number]:
        closes = ", ".join(f"#{n}" for n in closing) or "nothing"
        return "manual", "pr", f"does not close exactly #{number} (closes {closes})"
    return None


def _main_healthy(facts, settings):
    incidents = facts.get("incidents") or []
    if incidents:
        numbers = ", ".join(f"#{i.get('number')}" for i in incidents)
        return "wait", "incident", f"incident {numbers} open"
    deploy = facts.get("deploy")
    if deploy is not None:
        if deploy.get("status") != "completed":
            return "wait", "deploy", f"deploy of main in flight ({deploy.get('status')})"
        if deploy.get("conclusion") != "success":
            return "wait", "deploy", f"last deploy of main failed ({deploy.get('conclusion') or 'no conclusion'})"
    return None


def _ticket(facts, settings):
    ticket = facts.get("ticket") or {}
    labels = list(ticket.get("labels") or [])
    if ticket.get("state") != "OPEN":
        return "manual", "ticket", f"ticket #{ticket.get('number')} is {ticket.get('state') or 'not open'}"
    risks = [label for label in labels if label.startswith("risk:")]
    if risks != ["risk:low"]:
        return "manual", "risk", f"risk {', '.join(risks)}, not risk:low" if risks else "no risk label"
    sizes = [label[len("size:"):] for label in labels if label.startswith("size:")]
    allowed = settings.get("sizes") or DEFAULT_SIZES
    if not sizes:
        return "manual", "size", "no size label"
    if any(size not in allowed for size in sizes):
        return "manual", "size", f"size {', '.join(sizes)} not in {','.join(allowed)}"
    if "manual-land" in (facts.get("pr_labels") or []) or "manual-land" in labels:
        return "manual", "label", "manual-land label"
    return None


def _paths(facts, settings):
    manual = settings.get("manual_paths") or DEFAULT_MANUAL_PATHS
    for path in facts.get("files") or []:
        if any(glob_match(p, path) for p in manual):
            return "manual", "path", f"protected path {path}"
    files, lines = facts.get("nontest_files") or 0, facts.get("nontest_lines") or 0
    if files > MAX_NONTEST_FILES or lines > MAX_NONTEST_LINES:
        return "manual", "too large", (f"too large: {files} non-test files, {lines} non-test lines "
                                       f"(at most {MAX_NONTEST_FILES} and {MAX_NONTEST_LINES})")
    return None


def _review(facts, settings):
    review = facts.get("review")
    if review is None:
        return "manual", "review", "no harness review"
    if review.get("unreadable"):
        return "manual", "review", "unreadable harness review"
    if not review.get("at_head"):
        return "wait", "re-check", f"review of {(review.get('commit_id') or '')[:7]}, head {facts['head_sha'][:7]}"
    found = [f"{review.get(sev) or 0} {sev}" for sev in ("high", "medium") if review.get(sev)]
    if found:
        return "fix", "findings", f"{', '.join(found)} at head"
    return None


def _comments(facts, settings):
    threads, comments = facts.get("unresolved_threads") or 0, facts.get("owner_comments") or 0
    if threads > 0 or comments > 0:
        return "manual", "comment", f"{threads} unresolved threads, {comments} owner comments"
    return None


def _mergeable(facts, settings):
    mergeable = facts.get("mergeable")
    if mergeable == "CONFLICTING":
        return "fix", "conflict", "conflicts with main"
    if mergeable != "MERGEABLE":                   # UNKNOWN: GitHub has not computed it yet
        return "wait", "mergeable", f"mergeable is {mergeable or 'empty'}"
    behind = facts.get("behind_by") or 0
    if behind > 0:
        return "wait", "behind", f"behind main by {behind}"
    return None


def _checks(facts, settings):
    checks = facts.get("checks")
    if checks == "success":
        return None
    if checks == "pending":
        names = ", ".join(facts.get("pending_checks") or [])
        return "wait", "ci", f"pending: {names}" if names else "checks pending"
    if checks == "failure":
        names = ", ".join(facts.get("failed_checks") or [])
        return "fix", "ci", f"failed: {names}" if names else "checks failed"
    return "manual", "checks", "no checks" if checks == "none" else f"checks state {checks!r}"


RULES = (_opted_in, _pull_request, _main_healthy, _ticket, _paths, _review, _comments, _mergeable, _checks)


def evaluate(facts, settings):
    """{verdict, code, reason, rule}: rules 1 to 9 in order, the first failing one deciding
    (verdict wait, fix or manual); all passing gives land/land, rule 10, at the head's sha7."""
    for rule, check in enumerate(RULES, 1):
        failed = check(facts, settings)
        if failed:
            verdict, code, reason = failed
            return {"verdict": verdict, "code": code, "reason": reason, "rule": rule}
    return {"verdict": "land", "code": "land", "reason": f"gate passed at {facts['head_sha'][:7]}", "rule": 10}


# --------------------------------------------------------------- checks (harness-pr checks and land read them too)
def check_entries(rollup):
    """[{"name", "state", "bucket": pass|fail|pending, "url"}] from a statusCheckRollup (CheckRun or StatusContext)."""
    out = []
    for e in as_list(rollup):
        kind = e.get("__typename")
        if kind == "StatusContext" or (kind != "CheckRun" and "state" in e and "status" not in e):
            state = str(e.get("state") or "").upper()
            bucket = ("pending" if state in ("PENDING", "EXPECTED") else "fail" if state in FAILED
                      else "pass" if state in PASSED else "pending")
            name, url = e.get("context") or e.get("name") or "status", e.get("targetUrl") or e.get("detailsUrl") or ""
        else:
            status = str(e.get("status") or "").upper()
            conclusion = str(e.get("conclusion") or "").upper()
            if status != "COMPLETED":
                state, bucket = status or "PENDING", "pending"
            else:
                state = conclusion
                bucket = "fail" if conclusion in FAILED else "pass" if conclusion in PASSED else "pending"
            name, url = e.get("name") or e.get("context") or "check", e.get("detailsUrl") or e.get("targetUrl") or ""
        out.append({"name": str(name), "state": state, "bucket": bucket, "url": str(url)})
    return out


def overall(entries):
    """none, failure, pending or success: a failure wins over pending (gh pr checks does the same)."""
    if not entries:
        return "none"
    if any(e["bucket"] == "fail" for e in entries):
        return "failure"
    if any(e["bucket"] == "pending" for e in entries):
        return "pending"
    return "success"


# --------------------------------------------------------------- gathering the facts
class GatherError(Problem):
    """A gh or git call the gate needs failed (`<call> failed: <first stderr line>`); `facts` holds what was
    gathered before it."""

    def __init__(self, message, facts):
        super().__init__(message)
        self.facts = facts


def git_call(main, *args, ok=(0,)):
    """git -C `main` `args`; the CompletedProcess when its exit is in `ok`, else Problem naming the call (its
    words without flags) and git's first stderr line."""
    call = "git " + " ".join(a for a in args if not a.startswith("-"))
    try:
        done = subprocess.run(["git", "-C", main] + list(args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise Problem("git not found on PATH") from None
    except OSError as err:
        raise Problem(f"cannot run git: {err.strerror or err}") from None
    except subprocess.TimeoutExpired:
        raise Problem(f"{call} timed out after {GIT_TIMEOUT} s") from None
    if done.returncode not in ok:
        raise Problem(f"{call} failed: {first_line(done.stderr) or f'exit {done.returncode}'}")
    return done


def fetch_origin(main, ref=None):
    """git fetch --quiet origin [ref] in the main checkout; Problem when it fails."""
    git_call(main, "fetch", "--quiet", "origin", *([ref] if ref else []))


def committed_test_patterns(main, ref="origin/main"):
    """The committed HARNESS_TEST_PATTERNS at `ref`, else settings.py's DEFAULT_TEST_PATTERNS."""
    return patterns(_committed("HARNESS_TEST_PATTERNS", committed_env(main, ref)) or DEFAULT_TEST_PATTERNS)


def _pages(args, cwd):
    """Every item of a paginated REST list (`gh api --paginate` prints one JSON array per page)."""
    text, decoder, items, at = gh(args, cwd).strip(), json.JSONDecoder(), [], 0
    while at < len(text):
        try:
            page, at = decoder.raw_decode(text, at)
        except ValueError:
            raise Problem(f"{describe(args)} printed something other than JSON: {first_line(text[at:])[:80]}") \
                from None
        if not isinstance(page, list):
            raise Problem(f"{describe(args)} printed something other than a list")
        items += [item for item in page if isinstance(item, dict)]
        while at < len(text) and text[at].isspace():
            at += 1
    return items


def _names(labels):
    return [label["name"] for label in as_list(labels) if isinstance(label.get("name"), str)]


def _deploy(main):
    """The latest deploy.yml run on main ({status, conclusion, headSha}), or None when there is none or the
    repository has no deploy workflow."""
    args = ["run", "list", "--workflow", "deploy.yml", "--branch", "main", "--limit", "1", "--json",
            "status,conclusion,headSha"]
    done = gh_run(args, main)
    if done.returncode != 0:
        if NO_DEPLOY_WORKFLOW.search(done.stderr or ""):
            return None
        raise Problem(f"{describe(args)} failed: {first_line(done.stderr) or f'exit {done.returncode}'}")
    try:
        runs = json.loads(done.stdout) if done.stdout.strip() else []
    except ValueError:
        raise Problem(f"{describe(args)} printed something other than JSON: {first_line(done.stdout)[:80]}") \
            from None
    runs = as_list(runs)
    return runs[0] if runs else None


def _files(main, slug, pr, tests):
    """(paths with a rename's old path, non-test files, non-test lines) from the paginated files endpoint; a file
    counts once, by its new path, and its lines are additions plus deletions."""
    endpoint = f"repos/{slug}/pulls/{pr}/files?per_page=100"
    pages = gh_json(["api", endpoint, "--paginate", "--slurp"], main)
    if not isinstance(pages, list):
        raise Problem(f"gh api {endpoint} printed no list of files")
    paths, files, lines = [], 0, 0
    for page in pages:
        for f in (page if isinstance(page, list) else [page]):
            name = f.get("filename") if isinstance(f, dict) else None
            if not isinstance(name, str) or not name:
                continue
            paths.append(name)
            old = f.get("previous_filename")
            if isinstance(old, str) and old and old not in paths:
                paths.append(old)
            if not any(glob_match(p, name) for p in tests):
                files += 1
                lines += sum(n for n in (f.get("additions"), f.get("deletions")) if isinstance(n, int))
    return paths, files, lines


def latest_review(reviews):
    """The review with the newest submitted_at whose body carries REVIEW_MARKER, or None."""
    found = [(parse_iso(r.get("submitted_at")), r) for r in reviews
             if REVIEW_MARKER in (r.get("body") or "") and parse_iso(r.get("submitted_at"))]
    return max(found, key=lambda pair: pair[0])[1] if found else None


def review_counts(body):
    """{high, medium, low} from a harness review's first line, or None when it is neither the counts line nor
    `Harness review: no findings.`"""
    line = first_line(body)
    if line.startswith(NO_FINDINGS):
        return {"high": 0, "medium": 0, "low": 0}
    m = REVIEW_COUNTS.fullmatch(line)
    if not m:
        return None
    return {"high": int(m.group(2)), "medium": int(m.group(3)), "low": int(m.group(4))}


def _clean_merge_of_main(main, sha, first, second):
    """Is `sha` (parents `first`, `second`) a merge of main that changed nothing else: `second` an ancestor of
    origin/main and the tree what git merge-tree makes of the two, without conflicts?"""
    if git_call(main, "merge-base", "--is-ancestor", second, "origin/main", ok=(0, 1)).returncode != 0:
        return False
    merged = git_call(main, "merge-tree", "--write-tree", first, second, ok=(0, 1))
    if merged.returncode != 0:                      # conflicts: the merge on the branch resolved them by hand
        return False
    return first_line(merged.stdout) == git_call(main, "rev-parse", f"{sha}^{{tree}}").stdout.strip()


def at_head(main, reviewed, head, head_ref, commits):
    """Does the review of `reviewed` cover `head`: the same commit, or, walking first parents back from the
    head through the PR's `commits`, only clean merges of main until the reviewed commit, which must be one of
    them? Fetches origin `head_ref` before reading trees."""
    if not reviewed:
        return False
    if reviewed == head:
        return True
    parents = {c["sha"]: [p.get("sha") for p in as_list(c.get("parents"))] for c in commits
               if isinstance(c.get("sha"), str)}
    if reviewed not in parents or head not in parents:
        return False
    fetch_origin(main, head_ref)
    sha, seen = head, set()
    while sha != reviewed:
        pair = parents.get(sha)
        if sha in seen or pair is None or len(pair) != 2 or not all(isinstance(p, str) for p in pair):
            return False
        if not _clean_merge_of_main(main, sha, pair[0], pair[1]):
            return False
        seen.add(sha)
        sha = pair[0]
    return True


def _is_bot(node):
    author = node.get("author")
    if not isinstance(author, dict):
        return False
    return author.get("__typename") == "Bot" or str(author.get("login") or "").endswith("[bot]")


def owner_said(node):
    """A comment or review by a person (not a bot) with a body, and not the harness's own (neither marker)."""
    body = node.get("body") or ""
    return bool(body.strip()) and REVIEW_MARKER not in body and BOARD_MARKER not in body and not _is_bot(node)


def _conversation(main, slug, pr):
    """(unresolved threads holding an owner comment, owner comments and reviews) from the GraphQL conversation."""
    owner, name = slug.split("/", 1)
    data = gh_json(["api", "graphql", "-f", f"query={CONVERSATION}", "-f", f"owner={owner}", "-f", f"repo={name}",
                    "-F", f"number={pr}"], main)
    node = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise Problem(f"gh api graphql found no PR #{pr} in {slug}")
    threads = sum(1 for t in as_list(node.get("reviewThreads"))
                  if not t.get("isResolved") and any(owner_said(c) for c in as_list(t.get("comments"))))
    said = sum(1 for n in as_list(node.get("comments")) + as_list(node.get("reviews")) if owner_said(n))
    return threads, said


def _behind(main, slug, head):
    args = ["api", f"repos/{slug}/compare/main...{head}", "--jq", ".behind_by"]
    out = gh(args, main).strip()
    if not out.isdigit():
        raise Problem(f"{describe(args)} printed no behind_by: {out[:80]}")
    return int(out)


def gather_facts(main, slug, pr):
    """The facts evaluate() reads about PR `pr` of `slug`, from gh and git run in the main checkout `main` (origin
    already fetched). GatherError naming the first call that fails, with the facts gathered before it."""
    facts = {"pr": pr}
    try:
        _gather(main, slug, pr, facts)
    except Problem as err:
        raise GatherError(str(err), facts) from None
    return facts


def _gather(main, slug, pr, facts):
    facts["login"] = gh(["api", "user", "--jq", ".login"], main).strip()
    view = gh_json(["pr", "view", str(pr), "--json", PR_FIELDS], main)
    if not isinstance(view, dict):
        raise Problem(f"gh pr view {pr} printed no pull request")
    head_ref, head = str(view.get("headRefName") or ""), str(view.get("headRefOid") or "")
    m = TICKET_BRANCH.match(head_ref)
    number = int(m.group(1)) if m else None
    author = view.get("author") if isinstance(view.get("author"), dict) else {}
    entries = check_entries(view.get("statusCheckRollup"))
    facts.update(
        state=view.get("state"), draft=bool(view.get("isDraft")), head_ref=head_ref, head_sha=head,
        author=author.get("login"), pr_labels=_names(view.get("labels")), mergeable=view.get("mergeable"),
        closing=list(dict.fromkeys(r["number"] for r in as_list(view.get("closingIssuesReferences"))
                                   if isinstance(r.get("number"), int))),
        checks=overall(entries), failed_checks=[e["name"] for e in entries if e["bucket"] == "fail"],
        pending_checks=[e["name"] for e in entries if e["bucket"] == "pending"],
        ticket={"number": number, "state": None, "labels": []})
    incidents = gh_json(["issue", "list", "--label", "incident", "--state", "open", "--json", "number,title"], main)
    facts["incidents"] = as_list(incidents)
    facts["deploy"] = _deploy(main)
    if number is not None:
        issue = gh_json(["issue", "view", str(number), "--json", "state,labels"], main)
        issue = issue if isinstance(issue, dict) else {}
        facts["ticket"] = {"number": number, "state": issue.get("state"), "labels": _names(issue.get("labels"))}
    tests = committed_test_patterns(main)
    facts["files"], facts["nontest_files"], facts["nontest_lines"] = _files(main, slug, pr, tests)
    review = latest_review(_pages(["api", "--paginate", f"repos/{slug}/pulls/{pr}/reviews"], main))
    commits = _pages(["api", "--paginate", f"repos/{slug}/pulls/{pr}/commits"], main)
    facts["unresolved_threads"], facts["owner_comments"] = _conversation(main, slug, pr)
    facts["behind_by"] = _behind(main, slug, head)
    if review is None:
        facts["review"] = None
    else:
        counts = review_counts(review.get("body"))
        commit_id = review.get("commit_id") if isinstance(review.get("commit_id"), str) else ""
        facts["review"] = dict(counts or {"high": 0, "medium": 0, "low": 0}, commit_id=commit_id,
                               unreadable=counts is None,
                               at_head=counts is not None and at_head(main, commit_id, head, head_ref, commits))
    return facts
