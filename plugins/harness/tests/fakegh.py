#!/usr/bin/env python3
"""A stand-in for `gh` used by the harness tests.

Put this file on PATH as `gh` (copy or symlink) or point HARNESS_GH at it. Behaviour is
driven by a scenario JSON file named by FAKEGH_SCENARIO:

    {"calls": [
        {"match": ["issue", "view", "12"], "stdout": "...", "exit": 0},
        {"match": ["api", "-X", "POST"], "responses": [{"stderr": "boom", "exit": 1}, {"stdout": "{}"}]},
        {"contains": ["label", "create"], "stdout_json": {"ok": true}}
     ],
     "default": {"stdout": "", "exit": 0}}

Matching rules, first hit wins:
- "match": the argv after `gh` starts with these tokens.
- "contains": every token appears somewhere in argv (order free), for commands with interleaved options.
- "stdin_contains": the text must also appear in stdin.
A call entry gives one response ("stdout" or "stdout_json", "stderr", "exit") or a list under
"responses" consumed in order across invocations, repeating the last one. Consumption state is
kept in <scenario>.state.json. Without a match and without "default" the fake prints
`fakegh: no match for: gh ...` on stderr and exits 1.

The REST layer (#447). After the "calls" entries and before "default", `gh api` calls on GitHub's issue and pull
request REST routes are answered from the scenario's world, and `gh project` calls from its board:
- "issues": {"12": {...}} and, in "calls", entries {"issue": 12, "data": {...}} (issue_entry): each issue in the
  shape `gh issue view --json` prints (number, title, body, state OPEN|CLOSED, stateReason, url, labels as
  [{"name"}] or names, comments as [{"author": {"login"}, "body", "createdAt", "url"}], parent as a number or
  {"number", "title"}, blockedBy as [{"number", "state"}], subIssuesSummary) plus sub_issues (numbers or rows) and
  pull_request (true: the row is a pull request). Entries for one number merge, the first value of a field winning.
- "issue_list" (or "calls" entries {"issue_list": [...]}, list_entry): the rows `GET repos/{o}/{r}/issues` lists;
  without it the list is the issues above. Rows are filtered by the labels and state query and paged.
- "pulls" (or "calls" entries {"pulls": [...]}, pulls_entry): pull requests in `gh pr list --json` shape (number,
  state OPEN|CLOSED|MERGED, isDraft, mergedAt, url, body, title, headRefName, headRefOid, createdAt); a row without
  headRefName belongs to every branch.
- "project": a board, or a list of them (board()): {"owner", "number", "id", "fields": [{"id", "name", "options":
  [{"id", "name"}]}], "items": {url: id}}, for `gh project view|field-list|item-add|item-list N --owner O` and
  `gh project item-edit` by IDs; "refuse": true refuses every item-edit.
- "rest" entries in "calls" (rest_entry): {"rest": "issues/12/labels", "method": "POST"} answers any call on that
  route (or below it, unless "exact"; a `*` segment matches any one) with that method (any when absent), and for
  "branch" only a pulls list whose head names that branch, with the entry's response(s), before the world does.
- "api_fallback" entries: what a `{"match": ["api"]}` answer became (as_rest): any gh api call the world does not route.
- as_rest(calls, pulls=False) moves a list of gh-command fixtures (`issue view|list|edit|comment|close`, with
  `pulls` also `pr list`) to these entries, the data as it was, and adds {"world": true}.
Routes: a path (leading "/" or not) under repos/{owner}/{repo}/ or repos/<owner>/<name>/, the query string or -f/-F
fields (GET) giving the parameters; --paginate, --slurp, -X/--method, -f/--raw-field, -F/--field (@file, @- for
stdin, true/false/null and numbers typed), --input FILE|-, -H and a small --jq (.a.b, .[], .[].a) in any order.
GET: issues/N, issues/N/comments, issues/N/sub_issues, issues/N/parent (404 without one),
issues/N/dependencies/blocked_by, issues?labels=a,b&state=open|closed|all, pulls?head=OWNER:BRANCH&state=...
(a head without OWNER: is refused); lists page at per_page (default 30) unless --paginate. Writes succeed and
leave the world as it was (each read answers the scenario): POST issues/N/labels, PUT issues/N/labels,
DELETE issues/N/labels/NAME (404 when the issue lacks it), POST issues/N/comments (html_url
<issue url>#issuecomment-1), PATCH issues/N, POST issues/N/sub_issues, POST issues/N/dependencies/blocked_by.
gh project item-edit takes --id, --project-id, --field-id and --single-select-option-id; an ID the board does not
hold is refused (`GraphQL: Could not resolve to a node with the global id of '<id>'`, exit 1); the value set is kept
in <scenario>.state.json (project_values). By name (--field/--value) it is refused.

Every invocation appends one JSON line {"argv": [...], "stdin": "..."} to the file named by
FAKEGH_LOG (plus "exit" for a call the REST layer answered), so tests can assert on the exact calls made.
gh_view(entries, scenario_path) reads them back with the REST issue writes as the gh commands they stand for (issue
edit --add-label/--remove-label, issue comment --body, issue close [--comment] for a comment then a PATCH, issue
reopen), so a test can compare what changed on GitHub whichever route made it; issue_gets, list_queries,
pull_queries and board_writes read the reads, the list and pulls queries and the board's changes back.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zlib
from urllib.parse import parse_qs, unquote

DEFAULT_SLUG = "o/r"
DEFAULT_LOGIN = "rjeskuri"
PER_PAGE = 30
WORLD_KEYS = ("issue", "issue_list", "pulls", "rest", "world", "api_fallback")
WORLD = {"world": True}     # a "calls" entry that turns the REST layer on with nothing in it (as_rest adds it)


def main() -> int:
    argv = sys.argv[1:]
    stdin = read_stdin()
    answer = answer_for(argv, stdin)
    log = os.environ.get("FAKEGH_LOG")
    if log:
        line = {"argv": argv, "stdin": stdin}
        if answer.get("world"):            # answered by the REST layer: its exit, so gh_view skips refused writes
            line["exit"] = int(answer.get("exit", 0))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    if answer.get("none"):
        sys.stderr.write("fakegh: no match for: gh " + " ".join(argv) + "\n")
        return 1
    return respond(answer)


def answer_for(argv, stdin):
    """The response to give: a "calls" entry's, the world's (marked "world"), "default", or {"none": True}."""
    scenario_path = os.environ.get("FAKEGH_SCENARIO")
    scenario = {"calls": []}
    if scenario_path and os.path.exists(scenario_path):
        with open(scenario_path, encoding="utf-8") as fh:
            scenario = json.load(fh)
    state_path = (scenario_path or "fakegh") + ".state.json"
    state = load_state(state_path)
    for index, call in enumerate(scenario.get("calls", [])):
        if any(key in call for key in WORLD_KEYS):
            continue
        prefix = call.get("match")
        if prefix is not None and argv[: len(prefix)] != list(prefix):
            continue
        needed = call.get("contains")
        if needed is not None and not all(tok in argv for tok in needed):
            continue
        piece = call.get("stdin_contains")
        if piece is not None and piece not in stdin:
            continue
        return pick(call, index, state, state_path)
    world = World(scenario)
    if world.active:
        answer = world.serve(argv, stdin, state, state_path)
        if answer is not None:
            return dict(answer, world=True)
    if "default" in scenario:
        return scenario["default"]
    return {"none": True}


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def pick(call, index, state, state_path):
    """The call's response: its own, or the next of its "responses" (the last one repeating)."""
    responses = call.get("responses")
    if not responses:
        return call
    n = int(state.get(str(index), 0))
    state[str(index)] = n + 1
    save_state(state_path, state)
    return responses[min(n, len(responses) - 1)]


def read_stdin() -> str:
    """Read stdin only when something is actually there, so an open pipe never blocks the fake."""
    try:
        if sys.stdin.isatty():
            return ""
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        if not ready:
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def respond(response: dict) -> int:
    if "stdout_json" in response:
        sys.stdout.write(json.dumps(response["stdout_json"]))
    else:
        sys.stdout.write(response.get("stdout", ""))
    if response.get("stderr"):
        sys.stderr.write(response["stderr"])
    sys.stdout.flush()
    return int(response.get("exit", 0))


# --------------------------------------------------------------- scenario helpers for the tests
def issue_entry(number, data=None, **fields):
    """A "calls" entry that puts issue `number` in the world: `data` (gh issue view --json's shape) plus `fields`."""
    return {"issue": int(number), "data": dict(data or {}, **fields)}


def list_entry(rows):
    """A "calls" entry with the rows GET repos/{o}/{r}/issues lists (filtered by its labels and state)."""
    return {"issue_list": list(rows)}


def pulls_entry(rows):
    """A "calls" entry with pull requests (gh pr list --json's shape) GET repos/{o}/{r}/pulls lists."""
    return {"pulls": list(rows)}


def rest_entry(route, method=None, exact=False, **response):
    """A "calls" entry answering `method` (any when None) on `route` (after repos/{o}/{r}/; a `*` segment matches
    any one) and, unless `exact`, below it."""
    entry = {"rest": route.strip("/"), **response}
    if method:
        entry["method"] = method.upper()
    if exact:
        entry["exact"] = True
    return entry


def board(project, fields, items=(), generation=1):
    """A Projects board for the "project" key: OWNER/NUMBER, {field name: [option names]}, the issue URLs already
    on it, and the generation of its field and option IDs (a field made again gets new ones; the board's own ID and
    its items' stay)."""
    owner, number = project.split("/")
    tag = re.sub(r"\W", "_", project)

    def ident(*parts):
        return "_".join([tag] + [re.sub(r"\W", "_", p.lower()) for p in parts] + [f"g{generation}"])

    return {"owner": owner, "number": int(number), "id": f"PVT_{tag}",
            "fields": [{"id": "PVTSSF_" + ident(name), "name": name,
                        "options": [{"id": "OPT_" + ident(name, o), "name": o} for o in opts]}
                       for name, opts in fields.items()],
            "items": {url: f"PVTI_PVT_{tag}_{url.rstrip('/').rsplit('/', 1)[-1]}" for url in items}}


ISSUE_WRITES = {"edit": ("labels", None), "close": ("", "PATCH"), "reopen": ("", "PATCH"),
                "comment": ("comments", "POST")}
RESPONSE_KEYS = ("stdout", "stdout_json", "stderr", "exit", "responses")


def as_rest(calls, pulls=False):
    """The "calls" with each gh issue fixture moved to the REST layer, its data as it was:
    `issue view N` answers become issue N (issue_entry; a failing one a GET failure on issues/N, or on every issue
    without N), `issue list` answers the listed rows (list_entry; a --label the fixture was matched on is added to
    each row, so the labels query finds it; failing: a GET failure on the list), `issue close|reopen` answers are
    dropped and `issue edit` ones answer only issue creation's `--add-blocked-by` (the REST writes succeed) unless
    they fail (a failure of that write on N, or on every issue), `issue
    comment` answers become the POST comments answer {"html_url": the URL printed}, and REST sub-issue and comment
    answers (`api repos/o/r/issues/N/sub_issues`, `.../comments`) become issue N's sub_issues and comments. With
    `pulls`, `pr list` answers become pulls (pulls_entry). An `issue edit --add-blocked-by` (issue creation's) and
    everything else is kept as it is, so the gh issue commands themselves are answered by nothing but `default`."""
    out = []
    for call in calls:
        new = rest_form(call, pulls)
        if new is None:
            out.append(call)
        elif new != "drop":
            out.append(new)
    return out + [WORLD]


def rest_form(call, pulls):
    if not isinstance(call, dict) or "responses" in call and not call.get("responses"):
        return None
    response = {k: call[k] for k in RESPONSE_KEYS if k in call}
    if call.get("match") == ["api"] and "contains" not in call and "stdin_contains" not in call:
        # every gh api call: now only those the REST layer does not route
        return dict({"api_fallback": True}, **response)
    toks = call.get("match") if call.get("match") is not None else call.get("contains")
    if not isinstance(toks, list) or len(toks) < 2:
        return None
    failing = int(call.get("exit", 0)) != 0 or "responses" in call
    number = next((t for t in toks[2:] if str(t).isdigit()), None) if call.get("match") is not None else \
        next((t for t in toks if str(t).isdigit()), None)
    head = toks[:2]
    if toks[:1] == ["api"] and len(toks) >= 2 and "stdout_json" in call and not failing:
        m = re.match(r"/?repos/[^/]+/[^/]+/issues/(\d+)/(sub_issues|comments)(?:\?.*)?$", str(toks[1] if
                                                                                         call.get("match") else toks[0]))
        if m:
            key = "sub_issues" if m.group(2) == "sub_issues" else "comments"
            return issue_entry(m.group(1), {key: call["stdout_json"]})
        return None
    if call.get("contains") is not None:
        api = next((t for t in toks if re.match(r"/?repos/[^/]+/[^/]+/issues/\d+/(sub_issues|comments)", str(t))),
                   None)
        if api and any(t in ("POST", "PATCH", "DELETE", "PUT", "-X", "--method", "-f", "-F") for t in toks):
            return None                          # a write's answer: kept as it is
        if api and "stdout_json" in call and not failing:
            m = re.match(r"/?repos/[^/]+/[^/]+/issues/(\d+)/(sub_issues|comments)", api)
            return issue_entry(m.group(1), {m.group(2): call["stdout_json"]})
        if "pr" in toks and "list" in toks:
            head = ["pr", "list"]
        elif "issue" in toks:
            head = ["issue", next((t for t in toks if t in ("view", "list", "edit", "close", "reopen", "comment")),
                                  None)]
        else:
            return None
    rest = [t for t in (call.get("match") or [])[2:] + [t for t in call.get("contains") or []
                                                        if t not in ("pr", "list")]]
    branch = next((rest[i + 1] for i, t in enumerate(rest[:-1]) if t == "--head"), None) or \
        next((t for t in rest if not str(t).startswith("-") and t not in ("all", "open", "closed", "merged")
              and not str(t).isdigit()), None)
    if head[0] == "pr" and head[1] == "list" and pulls and failing:
        entry = rest_entry("pulls", "GET", exact=True, **response)
        if branch:
            entry["branch"] = branch
        return entry
    if head[0] == "pr" and head[1] == "list" and pulls and isinstance(call.get("stdout_json"), list) \
            and not failing:
        rows = [dict(r, headRefName=r.get("headRefName") or branch) if branch and isinstance(r, dict) else r
                for r in call["stdout_json"]]
        return pulls_entry(rows)
    if head[0] != "issue":
        return None
    verb = head[1]
    if verb == "view":
        if not failing and isinstance(call.get("stdout_json"), dict) and number is not None:
            return issue_entry(number, call["stdout_json"])
        if failing:
            return rest_entry(f"issues/{number}" if number else "issues/*", "GET", **response)
        return None
    if verb == "list":
        label = next((toks[i + 1] for i, t in enumerate(toks[:-1]) if t == "--label"), None) or \
            next((t[len("--label="):] for t in toks if str(t).startswith("--label=")), None)
        if failing:
            return rest_entry("issues", "GET", exact=True, **response)
        rows = call.get("stdout_json")
        if not isinstance(rows, list):
            return None
        if label:
            rows = [dict(r, labels=list(r.get("labels") or []) + ([] if label in label_names(r.get("labels"))
                                                                   else [{"name": label}]))
                    if isinstance(r, dict) else r for r in rows]
        return list_entry(rows)
    if verb in ISSUE_WRITES:
        if "--add-blocked-by" in toks:
            return None
        if not failing and verb == "edit":       # still answers issue creation's --add-blocked-by, nothing else
            return dict({"contains": ["issue", "edit", "--add-blocked-by"] + ([str(number)] if number else [])},
                        **response)
        if not failing and verb != "comment":
            return "drop"
        sub, method = ISSUE_WRITES[verb]
        route = "issues/" + (str(number) if number else "*") + (f"/{sub}" if sub else "")
        if verb == "comment" and not failing:
            url = str(call.get("stdout") or "").strip().splitlines()
            return rest_entry(route, "POST", stdout_json={"id": 1, "html_url": url[-1] if url else None})
        return rest_entry(route, method, exact=not sub, **response)
    return None


# --------------------------------------------------------------- gh api's arguments
VALUE_FLAGS = ("-H", "--header", "-t", "--template", "--hostname", "--cache", "-p", "--preview")


def parse_api(argv, stdin=""):
    """A `gh api` call as {"method", "endpoint", "slug" (None for {owner}/{repo}), "route" (the path after
    repos/O/R/, or the whole path), "query" ({name: [values]}), "body" (the -f/-F fields and --input JSON),
    "paginate", "slurp", "jq"}; None when argv is not gh api."""
    if not argv or argv[0] != "api":
        return None
    method, endpoint, fields, raw_input, jq = None, None, [], None, None
    paginate = slurp = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        value = argv[i + 1] if i + 1 < len(argv) else ""
        name, eq, inline = arg.partition("=")
        if arg in ("-X", "--method"):
            method, i = value, i + 2
            continue
        if eq and name in ("--method",):
            method = inline
        elif arg in ("-f", "--raw-field"):
            fields.append(("raw", value))
            i += 2
            continue
        elif eq and name == "--raw-field":
            fields.append(("raw", inline))
        elif arg in ("-F", "--field"):
            fields.append(("typed", value))
            i += 2
            continue
        elif eq and name == "--field":
            fields.append(("typed", inline))
        elif arg == "--input":
            raw_input, i = value, i + 2
            continue
        elif eq and name == "--input":
            raw_input = inline
        elif arg in ("-q", "--jq"):
            jq, i = value, i + 2
            continue
        elif eq and name == "--jq":
            jq = inline
        elif arg == "--paginate":
            paginate = True
        elif arg == "--slurp":
            slurp = True
        elif arg in VALUE_FLAGS:
            i += 2
            continue
        elif not arg.startswith("-") and endpoint is None:
            endpoint = arg
        i += 1
    body = {}
    if raw_input is not None:
        text = stdin if raw_input == "-" else read_file(raw_input)
        try:
            loaded = json.loads(text or "{}")
        except ValueError:
            loaded = {}
        if isinstance(loaded, dict):
            body.update(loaded)
    for kind, item in fields:
        key, _, val = item.partition("=")
        add_field(body, key, typed(val, stdin) if kind == "typed" else val)
    if method is None:
        method = "POST" if (fields or raw_input is not None) else "GET"
    method = method.upper()
    path, _, qs = (endpoint or "").lstrip("/").partition("?")
    query = parse_qs(qs, keep_blank_values=True)
    if method == "GET":
        for key, val in body.items():
            query.setdefault(key, []).extend(val if isinstance(val, list) else [str(val)])
        body = {}
    parts = [unquote(p) for p in path.split("/")]
    slug, route = None, "/".join(parts)
    if len(parts) >= 3 and parts[0] == "repos":
        if not (parts[1].startswith("{") or parts[2].startswith("{")):
            slug = f"{parts[1]}/{parts[2]}"
        route = "/".join(parts[3:])
    return {"method": method, "endpoint": endpoint or "", "slug": slug, "route": route, "query": query,
            "body": body, "paginate": paginate, "slurp": slurp, "jq": jq}


def read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def typed(value, stdin):
    """-F's value: @- (stdin) or @file read, true/false/null and integers typed, else the text."""
    if value.startswith("@"):
        return stdin if value == "@-" else read_file(value[1:])
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def add_field(body, key, value):
    """key=value into the body: key[]=v appends to a list, key[sub]=v sets a nested key."""
    m = re.fullmatch(r"([^\[\]]+)\[([^\[\]]*)\]", key)
    if not m:
        body[key] = value
    elif m.group(2) == "":
        body.setdefault(m.group(1), [])
        if isinstance(body[m.group(1)], list):
            body[m.group(1)].append(value)
    else:
        body.setdefault(m.group(1), {})
        if isinstance(body[m.group(1)], dict):
            body[m.group(1)][m.group(2)] = value


def first(query, name, default=None):
    values = query.get(name)
    return values[0] if values else default


def apply_jq(data, expr):
    """The small --jq this fake knows: `.`, `.a.b`, `.[]`, `.[].a` and `.a[]`; strings print raw, one per line.
    None for an expression it does not know."""
    expr = expr.strip()
    if expr != "." and not re.fullmatch(r"(?:\.?(?:[A-Za-z_][A-Za-z0-9_]*|\[\]))+", expr) or not expr.startswith("."):
        return None
    values = [data]
    for token in re.findall(r"\[\]|[A-Za-z_][A-Za-z0-9_]*", expr):
        out = []
        for value in values:
            if token == "[]":
                out.extend(value if isinstance(value, list) else list(value.values()) if isinstance(value, dict)
                           else [])
            else:
                out.append(value.get(token) if isinstance(value, dict) else None)
        values = out
    lines = []
    for value in values:
        lines.append(value if isinstance(value, str) else json.dumps(value))
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------- the world
def label_names(labels):
    out = []
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name:
            out.append(name)
    return out


def not_found(message="Not Found"):
    return {"stdout_json": {"message": message, "documentation_url": "https://docs.github.com/rest",
                            "status": "404"},
            "stderr": f"gh: {message} (HTTP 404)\n", "exit": 1}


def unprocessable(message):
    return {"stdout_json": {"message": message, "status": "422"}, "stderr": f"gh: {message} (HTTP 422)\n", "exit": 1}


class World:
    """The issues, lists, pull requests, board and REST overrides a scenario declares."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.issues = {}
        self.issue_list = None
        self.pulls = None
        self.rest = []
        self.fallback = []
        for key, data in (scenario.get("issues") or {}).items():
            self.merge(key, data)
        for index, call in enumerate(scenario.get("calls", [])):
            if "issue" in call and isinstance(call.get("data"), dict):
                self.merge(call["issue"], call["data"])
            if isinstance(call.get("issue_list"), list):
                self.issue_list = (self.issue_list or []) + call["issue_list"]
            if isinstance(call.get("pulls"), list):
                self.pulls = (self.pulls or []) + call["pulls"]
            if "rest" in call:
                self.rest.append((index, call))
            if call.get("api_fallback"):
                self.fallback.append((index, call))
        if isinstance(scenario.get("issue_list"), list):
            self.issue_list = (self.issue_list or []) + scenario["issue_list"]
        if isinstance(scenario.get("pulls"), list):
            self.pulls = (self.pulls or []) + scenario["pulls"]
        if self.pulls is not None:                  # one row per pull request and branch, the first one given
            seen, rows = set(), []
            for pr in self.pulls:
                key = (pr.get("number"), pr.get("headRefName")) if isinstance(pr, dict) else id(pr)
                if key not in seen:
                    seen.add(key)
                    rows.append(pr)
            self.pulls = rows
        if self.issue_list is not None:             # one row per issue, the first one given
            numbers, rows = set(), []
            for row in self.issue_list:
                key = row.get("number") if isinstance(row, dict) else id(row)
                if key not in numbers:
                    numbers.add(key)
                    rows.append(row)
            self.issue_list = rows
        for row in self.issue_list or []:           # a listed row is an issue too, under any entry of its own
            if isinstance(row, dict) and str(row.get("number", "")).isdigit():
                self.merge(row["number"], row)
        self.project = scenario.get("project") if isinstance(scenario.get("project"), (dict, list)) else None
        self.login = scenario.get("login") or DEFAULT_LOGIN
        self.active = bool(self.issues or self.issue_list is not None or self.pulls is not None or self.rest
                           or self.project or any(c.get("world") for c in scenario.get("calls", [])
                                                  if isinstance(c, dict)))

    def merge(self, key, data):
        n = int(key)
        record = self.issues.setdefault(n, {"number": n})
        for field, value in data.items():
            record.setdefault(field, value)

    # ---------------------------------------------------- dispatch
    def serve(self, argv, stdin, state, state_path):
        if argv[:1] == ["project"]:
            return self.serve_project(argv, state, state_path) if self.project else None
        req = parse_api(argv, stdin)
        if req is None:
            return None
        for index, call in self.rest:
            if call.get("method") and call["method"] != req["method"]:
                continue
            if call.get("branch") is not None and \
                    (first(req["query"], "head") or "").split(":", 1)[-1] != call["branch"]:
                continue
            if route_matches(call["rest"], req["route"], call.get("exact")):
                return pick(call, index, state, state_path)
        answer = self.route(req)
        if answer is None and self.fallback:
            index, call = self.fallback[0]
            return pick(call, index, state, state_path)
        if answer is None:
            return None
        if answer.get("exit", 0) == 0 and "stdout_json" in answer:
            data = answer["stdout_json"]
            if req["slurp"] and req["paginate"]:
                data = [data]
            if req["jq"]:
                text = apply_jq(data, req["jq"])
                if text is None:
                    return {"stderr": f"fakegh: --jq {req['jq']!r} is not one this fake knows\n", "exit": 1}
                return {"stdout": text}
            return {"stdout_json": data}
        return answer

    def route(self, req):
        method, route, query = req["method"], req["route"], req["query"]
        parts = route.split("/")
        slug = req["slug"]
        if parts[0] == "issues" and len(parts) == 1:
            return self.page(req, self.listed(query, slug)) if method == "GET" else None
        if parts[0] == "pulls" and len(parts) == 1 and method == "GET":
            return self.pull_list(req, slug)
        if parts[0] != "issues" or len(parts) < 2 or not parts[1].isdigit():
            return None
        n, rest = int(parts[1]), parts[2:]
        issue = self.issues.get(n)
        if method == "GET":
            if issue is None:
                return not_found()
            if not rest:
                return {"stdout_json": self.rest_issue(issue, slug)}
            if rest == ["comments"]:
                return self.page(req, [self.rest_comment(c, issue, slug, k)
                                       for k, c in enumerate(issue.get("comments") or [], 1) if isinstance(c, dict)])
            if rest == ["sub_issues"]:
                return self.page(req, [self.rest_issue(self.row(s), slug) for s in issue.get("sub_issues") or []])
            if rest == ["parent"]:
                parent = issue.get("parent")
                if parent in (None, {}, 0):
                    return not_found()
                return {"stdout_json": self.rest_issue(self.row(parent), slug)}
            if rest == ["dependencies", "blocked_by"]:
                return self.page(req, [self.rest_issue(self.row(b), slug) for b in issue.get("blockedBy") or []])
            if rest == ["labels"]:
                return self.page(req, [label_object(x) for x in label_names(issue.get("labels"))])
            return None
        record = issue or {"number": n}
        body = req["body"]
        have = label_names(record.get("labels"))
        if rest == ["labels"] and method in ("POST", "PUT"):
            given = body.get("labels")
            given = [given] if isinstance(given, str) else given
            if not isinstance(given, list):
                return unprocessable("Invalid request: labels is not an array")
            names = [str(x.get("name") if isinstance(x, dict) else x) for x in given]
            result = names if method == "PUT" else have + [x for x in names if x not in have]
            return {"stdout_json": [label_object(x) for x in result]}
        if rest == ["labels"] and method == "DELETE":
            return {"stdout": "", "exit": 0}
        if len(rest) == 2 and rest[0] == "labels" and method == "DELETE":
            if rest[1] not in have:
                return not_found("Label does not exist")
            return {"stdout_json": [label_object(x) for x in have if x != rest[1]]}
        if rest == ["comments"] and method == "POST":
            text = body.get("body")
            if not isinstance(text, str) or not text:
                return unprocessable("Invalid request: body is missing")
            return {"stdout_json": {"id": 1, "html_url": f"{self.html_url(record, slug)}#issuecomment-1",
                                    "body": text, "user": {"login": self.login},
                                    "created_at": "2026-11-24T00:00:00Z"}}
        if not rest and method == "PATCH":
            out = self.rest_issue(record, slug)
            if isinstance(body.get("state"), str):
                out["state"] = body["state"]
            if "state_reason" in body:
                out["state_reason"] = body["state_reason"]
            if isinstance(body.get("labels"), list):
                out["labels"] = [label_object(x.get("name") if isinstance(x, dict) else x) for x in body["labels"]]
            return {"stdout_json": out}
        if rest == ["sub_issues"] and method == "POST":
            return {"stdout_json": self.rest_issue(record, slug)}
        if rest == ["dependencies", "blocked_by"] and method == "POST":
            return {"stdout_json": self.rest_issue(record, slug)}
        return None

    # ---------------------------------------------------- shapes
    def row(self, ref):
        """An issue from a reference: a number (its world entry), or a row merged over its world entry."""
        if isinstance(ref, dict):
            n = ref.get("number")
            base = self.issues.get(int(n)) if isinstance(n, int) or (isinstance(n, str) and n.isdigit()) else None
            return dict(base or {}, **ref)
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return self.issues.get(int(ref)) or {"number": int(ref)}
        return {}

    def slug_of(self, issue, slug):
        url = issue.get("url") or issue.get("html_url")
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/(?:issues|pull)/\d+", url or "")
        return m.group(1) if m else slug or self.scenario.get("slug") or DEFAULT_SLUG

    def html_url(self, issue, slug):
        return issue.get("url") or issue.get("html_url") or \
            f"https://github.com/{self.slug_of(issue, slug)}/issues/{issue.get('number')}"

    def rest_issue(self, issue, slug):
        n = issue.get("number")
        state = str(issue.get("state") or "open").lower()
        reason = issue.get("stateReason", issue.get("state_reason"))
        subs = [self.row(s) for s in issue.get("sub_issues") or []]
        summary = issue.get("subIssuesSummary")
        if not isinstance(summary, dict):
            done = sum(1 for s in subs if str(s.get("state") or "").upper() == "CLOSED")
            summary = {"total": len(subs), "completed": done}
        total, completed = int(summary.get("total") or 0), int(summary.get("completed") or 0)
        blockers = [self.row(b) for b in issue.get("blockedBy") or []]
        open_blockers = sum(1 for b in blockers if str(b.get("state") or "open").upper() != "CLOSED")
        own_slug = self.slug_of(issue, slug)
        parent = issue.get("parent")
        parent = parent.get("number") if isinstance(parent, dict) else parent
        parent_url = f"https://api.github.com/repos/{own_slug}/issues/{parent}" if parent else None
        out = {"id": issue.get("id") if isinstance(issue.get("id"), int) else 1000000 + int(n or 0),
               "node_id": issue.get("node_id") or f"I_kw{n}",
               "url": f"https://api.github.com/repos/{own_slug}/issues/{n}",
               "html_url": issue.get("url") or issue.get("html_url"),
               "number": n, "title": issue.get("title"), "body": issue.get("body"),
               "state": state, "state_reason": str(reason).lower() if reason else None,
               "labels": [label_object(x) for x in label_names(issue.get("labels"))],
               "comments": len(issue.get("comments") or []),
               "user": {"login": self.login},
               "parent_issue_url": parent_url,      # as GitHub's REST issue carries it: null with no parent
               "sub_issues_summary": {"total": total, "completed": completed,
                                      "percent_completed": int(100 * completed / total) if total else 0},
               "issue_dependencies_summary": {"blocked_by": open_blockers, "total_blocked_by": len(blockers),
                                              "blocking": 0, "total_blocking": 0}}
        if issue.get("pull_request"):
            out["pull_request"] = {"url": f"https://api.github.com/repos/{own_slug}/pulls/{n}",
                                   "html_url": f"https://github.com/{own_slug}/pull/{n}"}
        return out

    def rest_comment(self, comment, issue, slug, k):
        author = comment.get("author") if isinstance(comment.get("author"), dict) else comment.get("user")
        login = author.get("login") if isinstance(author, dict) else None
        return {"id": comment.get("id") if isinstance(comment.get("id"), int) else k,
                "html_url": comment.get("url") or comment.get("html_url"),
                "user": {"login": login} if login is not None else None,
                "body": comment.get("body"), "created_at": comment.get("createdAt", comment.get("created_at"))}

    def listed(self, query, slug):
        rows = self.issue_list if self.issue_list is not None else list(self.issues.values())
        state = first(query, "state", "open").lower()
        wanted = [x for x in first(query, "labels", "").split(",") if x]
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            issue = self.row(row) if self.issue_list is None else dict(row)
            closed = str(issue.get("state") or "open").upper() == "CLOSED"
            if (state == "open" and closed) or (state == "closed" and not closed):
                continue
            if not all(x in label_names(issue.get("labels")) for x in wanted):
                continue
            out.append(self.rest_issue(issue, slug))
        return out

    def pull_list(self, req, slug):
        query = req["query"]
        head = first(query, "head")
        if head is not None and ":" not in head:
            return unprocessable(f"fakegh: head must be OWNER:BRANCH, not {head!r}")
        branch = head.split(":", 1)[1] if head is not None else None
        state = first(query, "state", "open").lower()
        out = []
        named = any(isinstance(pr, dict) and pr.get("headRefName") == branch for pr in self.pulls or [])
        for pr in self.pulls or []:
            if not isinstance(pr, dict):
                continue
            # a branch's own rows when it has any, else the rows that name no branch
            if branch is not None and pr.get("headRefName") != (branch if named else None):
                continue
            # a row that names no state is merged when it has a merge time (gh pr list's mergedAt), else open
            kind = str(pr.get("state") or ("MERGED" if pr.get("mergedAt") else "OPEN")).upper()
            if (state == "open" and kind != "OPEN") or (state == "closed" and kind == "OPEN"):
                continue
            n = pr.get("number")
            own = self.slug_of({"url": pr.get("url")}, slug)
            merged = pr.get("mergedAt") or ("2026-01-01T00:00:00Z" if kind == "MERGED" else None)
            out.append({"number": n, "state": "open" if kind == "OPEN" else "closed",
                        "draft": bool(pr.get("isDraft")), "merged_at": merged if kind == "MERGED" else None,
                        "html_url": pr.get("url") or f"https://github.com/{own}/pull/{n}",
                        "title": pr.get("title"), "body": pr.get("body"), "created_at": pr.get("createdAt"),
                        "head": {"ref": pr.get("headRefName") or branch, "sha": pr.get("headRefOid")}})
        return self.page(req, out)

    def page(self, req, rows):
        if req["paginate"]:
            return {"stdout_json": rows}
        try:
            per_page = max(1, min(100, int(first(req["query"], "per_page", PER_PAGE))))
            page = max(1, int(first(req["query"], "page", 1)))
        except ValueError:
            per_page, page = PER_PAGE, 1
        return {"stdout_json": rows[(page - 1) * per_page: page * per_page]}

    # ---------------------------------------------------- the Projects board
    def serve_project(self, argv, state, state_path):
        verb = argv[1] if len(argv) > 1 else ""
        opts, positional = options(argv[2:])
        number = positional[0] if positional else None
        wants_json = opts.get("--format") == "json"
        added = state.setdefault("project", {}).setdefault("items", {})
        values = state["project"].setdefault("values", {})
        if verb == "item-edit":
            if "--field" in opts or "--value" in opts or "--url" in opts:
                return {"stderr": "fakegh: item-edit by name is not served by the board; pass --id, --project-id, "
                                  "--field-id and --single-select-option-id\n", "exit": 1}
            item, project_id = opts.get("--id"), opts.get("--project-id")
            field_id, option_id = opts.get("--field-id"), opts.get("--single-select-option-id")
            board = next((b for b in self.boards() if b.get("id") == project_id and not b.get("refuse")), None)
            items = board_items(board, added) if board else {}
            field = next((f for f in (board or {}).get("fields") or [] if f.get("id") == field_id), None)
            option = next((o for o in (field or {}).get("options") or [] if o.get("id") == option_id), None)
            for given, known in ((project_id, board is not None), (item, item in items.values()),
                                 (field_id, field is not None), (option_id, option is not None)):
                if not known:
                    return {"stderr": f"GraphQL: Could not resolve to a node with the global id of '{given}'. "
                                      "(updateProjectV2ItemFieldValue)\n", "exit": 1}
            values.setdefault(item, {})[field["name"]] = option["name"]
            save_state(state_path, state)
            return {"stdout_json": {"id": item}} if wants_json else {"stdout": ""}
        if verb not in ("view", "field-list", "item-add", "item-list"):
            return None
        board = next((b for b in self.boards() if number == str(b.get("number"))
                      and opts.get("--owner") == b.get("owner")), None)
        if board is None:
            return {"stderr": f"Could not resolve to a ProjectV2 with the number {number}.\n", "exit": 1}
        items = board_items(board, added)
        if verb == "view":
            return {"stdout_json": {"id": board.get("id"), "number": board.get("number"),
                                    "title": board.get("title", "Board"), "owner": {"login": board.get("owner")}}}
        if verb == "field-list":
            fields = [dict(f, type=f.get("type") or ("ProjectV2SingleSelectField" if f.get("options") is not None
                                                     else "ProjectV2Field")) for f in board.get("fields") or []]
            return {"stdout_json": {"fields": fields, "totalCount": len(fields)}}
        if verb == "item-add":
            url = opts.get("--url")
            if not url:
                return {"stderr": "required flag(s) \"url\" not set\n", "exit": 1}
            item = items.get(url)
            if item is None:
                m = re.search(r"/(\d+)$", url)
                item = f"PVTI_{board.get('id')}_{m.group(1) if m else len(items) + 1}"
                added.setdefault(str(board.get("id")), {})[url] = item
                save_state(state_path, state)
            return {"stdout_json": {"id": item, "type": "Issue", "url": url}} if wants_json else {"stdout": ""}
        rows = [dict({"id": i, "content": {"url": u, "type": "Issue"}},
                     **{k.lower(): v for k, v in values.get(i, {}).items()}) for u, i in items.items()]
        return {"stdout_json": {"items": rows, "totalCount": len(rows)}}

    def boards(self):
        project = self.project
        return [b for b in (project if isinstance(project, list) else [project]) if isinstance(b, dict)]


def board_items(board, added):
    """{url: item id} on `board`: its fixture's items and the ones item-add put there."""
    return dict(board.get("items") or {}, **added.get(str(board.get("id")), {}))


def route_matches(pattern, route, exact=False):
    """Whether `route` is `pattern` or (unless `exact`) below it; a `*` segment of the pattern matches any one."""
    want, have = pattern.strip("/").split("/"), route.split("/")
    if exact and len(have) != len(want):
        return False
    return len(have) >= len(want) and all(w in ("*", h) for w, h in zip(want, have))


def label_object(name):
    return {"id": zlib.crc32(name.encode("utf-8")) % 100000, "name": name, "color": "ededed", "description": None}


def options(args):
    """({--flag: value}, [positional]) of a gh command's arguments (--flag=value too)."""
    opts, positional, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            key, _, value = arg.partition("=")
            opts[key] = value
        elif arg.startswith("-") and i + 1 < len(args):
            opts[arg] = args[i + 1]
            i += 1
        elif arg.startswith("-"):
            opts[arg] = ""
        else:
            positional.append(arg)
        i += 1
    return opts, positional


# --------------------------------------------------------------- reading the log back
def log_entries(path):
    """The FAKEGH_LOG lines, each {"argv", "stdin"}."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def scenario_labels(scenario_path, number):
    """The labels issue `number` has in the scenario's world (what a PUT or PATCH replaces)."""
    if not scenario_path or not os.path.exists(scenario_path):
        return []
    with open(scenario_path, encoding="utf-8") as fh:
        world = World(json.load(fh))
    return label_names(world.issues.get(int(number), {}).get("labels"))


def issue_write(req):
    """(kind, number, detail) of a REST issue write, else None. kind: add, remove, set, comment, close, reopen."""
    if req is None or req["method"] == "GET":
        return None
    parts = req["route"].split("/")
    if len(parts) < 2 or parts[0] != "issues" or not parts[1].isdigit():
        return None
    n, rest, body = parts[1], parts[2:], req["body"]
    given = body.get("labels")
    given = [given] if isinstance(given, str) else given if isinstance(given, list) else None
    names = [str(x.get("name") if isinstance(x, dict) else x) for x in given] if given is not None else None
    if rest == ["labels"] and req["method"] == "POST":
        return "add", n, names or []
    if rest == ["labels"] and req["method"] == "PUT":
        return "set", n, names or []
    if rest == ["labels"] and req["method"] == "DELETE":
        return "set", n, []
    if len(rest) == 2 and rest[0] == "labels" and req["method"] == "DELETE":
        return "remove", n, [rest[1]]
    if rest == ["comments"] and req["method"] == "POST":
        return "comment", n, body.get("body")
    if not rest and req["method"] == "PATCH":
        state = str(body.get("state") or "").lower()
        if names is not None:
            return "set", n, names
        if state == "closed":
            return "close", n, None
        if state == "open":
            return "reopen", n, None
    return None


def gh_view(entries, scenario_path=None):
    """Each logged call's argv, with the REST issue writes as the gh commands they stand for: the label writes on one
    issue in a row (no other write between) as one `issue edit N --add-label A... --remove-label R...`, a comment as
    `issue comment N --body B`, a close as `issue close N` (`--comment B` when a comment on N is next to it) and a
    reopen as `issue reopen N`. A legacy `issue comment N --body-file -` reads as `--body <its stdin>`. Reads and
    every other call keep their argv."""
    out, edit = [], None          # edit: (index in out, number, added, removed)
    for entry in entries:
        argv = list(entry.get("argv") or [])
        stdin = entry.get("stdin") or ""
        req = parse_api(argv, stdin)
        write = issue_write(req) if entry.get("exit", 0) == 0 else None
        if write is None and issue_write(req) is not None:     # a write the REST layer refused: nothing changed
            continue
        if write is None:
            if argv[:2] == ["issue", "comment"] and "--body-file" in argv and \
                    argv[argv.index("--body-file") + 1:][:1] == ["-"]:
                argv = argv[:3] + ["--body", stdin]
            if req is None or req["method"] != "GET":
                edit = None
            out.append(argv)
            continue
        kind, n, detail = write
        if kind in ("add", "remove", "set"):
            if kind == "set":
                before = scenario_labels(scenario_path, n)
                added = [x for x in detail if x not in before]
                removed = [x for x in before if x not in detail]
            else:
                added, removed = (detail, []) if kind == "add" else ([], detail)
            if edit is not None and edit[1] == n:
                index, _, have_added, have_removed = edit
                have_added += [x for x in added if x not in have_added]
                have_removed += [x for x in removed if x not in have_removed]
            else:
                out.append(None)
                edit = (len(out) - 1, n, list(added), list(removed))
            index, _, have_added, have_removed = edit
            out[index] = ["issue", "edit", n] + [t for x in have_added for t in ("--add-label", x)] + \
                [t for x in have_removed for t in ("--remove-label", x)]
            continue
        edit = None
        if kind == "comment":
            out.append(["issue", "comment", n, "--body", detail])
        elif kind == "close":
            prev = next((k for k in range(len(out) - 1, -1, -1) if out[k] is not None and not is_world_read(out[k])),
                        None)
            if prev is not None and out[prev][:3] == ["issue", "comment", n]:
                out[prev] = ["issue", "close", n, "--comment", out[prev][4]]
            else:
                out.append(["issue", "close", n])
        else:
            out.append(["issue", "reopen", n])
    return [argv for argv in out if argv is not None]


def is_world_read(argv):
    """Whether argv is a `gh api` GET of an issue or pull request route (what gh_view keeps as it was)."""
    req = parse_api(argv)
    return req is not None and req["method"] == "GET" and req["route"].split("/")[0] in ("issues", "pulls")


def pull_queries(entries):
    """[(branch, state)] of each GET repos/{o}/{r}/pulls?head=OWNER:BRANCH call (state None when not given)."""
    out = []
    for e in entries:
        req = parse_api(e.get("argv") or [], e.get("stdin") or "")
        if req is not None and req["method"] == "GET" and req["route"] == "pulls":
            head = first(req["query"], "head")
            out.append((head.split(":", 1)[-1] if head else None, first(req["query"], "state")))
    return out


def list_queries(entries):
    """[(sorted labels, state)] of each GET repos/{o}/{r}/issues list call (state None when not given)."""
    out = []
    for e in entries:
        req = parse_api(e.get("argv") or [], e.get("stdin") or "")
        if req is not None and req["method"] == "GET" and req["route"] == "issues":
            labels = sorted(x for x in first(req["query"], "labels", "").split(",") if x)
            out.append((labels, first(req["query"], "state")))
    return out


def issue_gets(entries, number):
    """How many times issue `number` itself was read (GET repos/{o}/{r}/issues/N)."""
    n = str(number)
    return sum(1 for e in entries
               for req in [parse_api(e.get("argv") or [], e.get("stdin") or "")]
               if req is not None and req["method"] == "GET" and req["route"] == f"issues/{n}")


def board_writes(entries, scenario_path):
    """What the gh project calls the board answered changed, in order: ("item-add", "OWNER/NUMBER", url) for an
    issue added that was not on the board, ("item-edit", "OWNER/NUMBER", url, field, option) for a value set, by
    names, whether the call named IDs or (a "calls" answer aside) names. Reads, resolving an item already there
    and refused calls change nothing."""
    with open(scenario_path, encoding="utf-8") as fh:
        world = World(json.load(fh))
    on_board = {str(b.get("id")): dict(b.get("items") or {}) for b in world.boards()}
    out = []
    for entry in entries:
        argv = entry.get("argv") or []
        if argv[:1] != ["project"] or entry.get("exit", 0) != 0 or "exit" not in entry:
            continue
        opts, pos = options(argv[2:])
        if "--project-id" in opts:
            board = next((b for b in world.boards() if b.get("id") == opts["--project-id"]), None)
        else:
            board = next((b for b in world.boards() if pos[:1] == [str(b.get("number"))]
                          and opts.get("--owner") == b.get("owner")), None)
        if board is None:
            continue
        name, items = f"{board.get('owner')}/{board.get('number')}", on_board[str(board.get("id"))]
        if argv[1] == "item-add" and opts.get("--url") and opts["--url"] not in items:
            m = re.search(r"/(\d+)$", opts["--url"])
            items[opts["--url"]] = f"PVTI_{board.get('id')}_{m.group(1) if m else len(items) + 1}"
            out.append(("item-add", name, opts["--url"]))
        elif argv[1] == "item-edit" and "--field-id" in opts:
            url = next((u for u, i in items.items() if i == opts.get("--id")), None)
            field = next((f for f in board.get("fields") or [] if f.get("id") == opts["--field-id"]), {})
            option = next((o for o in field.get("options") or []
                           if o.get("id") == opts.get("--single-select-option-id")), {})
            out.append(("item-edit", name, url, field.get("name"), option.get("name")))
    return out


def project_values(scenario_path):
    """{item id: {field name: option name}} the board holds after the item-edit calls, and {url: item id} added."""
    state = load_state((scenario_path or "fakegh") + ".state.json")
    project = state.get("project") or {}
    return project.get("values") or {}


if __name__ == "__main__":
    sys.exit(main())
