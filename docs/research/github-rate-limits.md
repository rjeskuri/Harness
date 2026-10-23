# Research notes: GitHub rate limits and the API traffic of gh

Gathered 2026-10-23 for ticket #186 (epic #185: call gh less, log every call, one machine-wide pause). Sources: docs.github.com (REST "Rate limits for the REST API", "Best practices for using the REST API", "Troubleshooting the REST API", "REST API endpoints for rate limits", GraphQL "Rate limits and query limits for the GraphQL API"), the cli/cli source at tag v2.99.0 (the local `gh version 2.99.0 (2026-09-01)`), its pinned dependencies go-gh v2.13.0 (`go.mod:21`) and cli/shurcooL-graphql v0.0.4 (`go.mod:94`), and two local commands: `gh --version` and `gh api rate_limit` (with `GH_DEBUG=api`). No other API call was made. Nothing here was measured by sending a real refusal: GitHub's refusal message texts come from public reports, and the gh format strings from the source, and the note says which is which.

gh links below are `github.com/cli/cli/blob/v2.99.0/<path>#L<line>`; the go-gh ones are `github.com/cli/go-gh/blob/v2.13.0/pkg/api/<file>#L<line>`.

## The epic's two assumptions

- `GET /rate_limit` does not count against the primary limit: **right**. Docs: "Accessing this endpoint does not count against your REST API rate limit." (https://docs.github.com/en/rest/rate-limit/rate-limit). Measured: 18 consecutive `gh api rate_limit` calls left REST `core.used` and `graphql.used` unchanged (8 calls at 51 and 537, then 10 at 58 and 583; other sessions moved the numbers by 7 and 46 in the 12 minutes between the two runs). **But** it can count against the secondary limit: "Calling this endpoint does not count against your primary rate limit, but it can count against your secondary rate limit." (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). Use it after a refusal, not as a poll.
- gh shows no response headers for its subcommands: **right by default, wrong with `GH_DEBUG=api`**. No subcommand prints headers (only `gh api -i` and `gh api --verbose` do). But `GH_DEBUG=api` (or `DEBUG=api`) turns on the full HTTP log for every subcommand: `api/http_client.go:43-52`, `internal/ghcmd/cmd.go:55`, `utils/utils.go:10-29`. Request and response headers and JSON bodies (up to 100000 bytes) go to stderr. See "The local probe".

## GitHub's limits

Primary, for a user token (a gh login is an OAuth token):
- REST: "All of these requests count towards your personal rate limit of 5,000 requests per hour." Search REST has its own bucket (30 a minute on this account, `rate_limit` output below). https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- GraphQL: "For users: 5,000 points per hour per user. This includes requests made with a personal access token as well as requests made by a GitHub App or OAuth app on behalf of a user that authorized the app." REST and GraphQL are separate buckets. https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
- Points: "Add up the number of requests needed to fulfill each unique connection in the call. Assume every request will reach the first or last argument limits. Divide the number by 100 and round the result to the nearest whole number"; "The minimum point value of a call to the GraphQL API is 1." The docs' example: 5,101 requests, score 51. `rateLimit { cost }` returns the real cost; **not measured here** (it needs a GraphQL call), so the per-command costs below are the docs' formula, not readings.
- Timeouts: "If a timeout occurs for any of your API requests, additional points will be deducted from your primary rate limit for the next hour."

Secondary (same list on the REST and GraphQL pages):
- "No more than 100 concurrent requests are allowed. This limit is shared across the REST API and GraphQL API."
- "No more than 900 points per minute are allowed for REST API endpoints, and no more than 2,000 points per minute are allowed for the GraphQL API endpoint."
- Points: GraphQL without mutations 1, with mutations 5; "Most REST API GET, HEAD, and OPTIONS requests" 1; "Most REST API POST, PATCH, PUT, or DELETE requests" 5; "Some REST API endpoints have a different point cost that is not shared publicly."
- CPU: "No more than 90 seconds of CPU time per 60 seconds of real time is allowed. No more than 60 seconds of this CPU time may be for the GraphQL API."
- Content creation: "In general, no more than 80 content-generating requests per minute and no more than 500 content-generating requests per hour are allowed. Some endpoints have lower content creation limits. Content creation limits include actions taken on the GitHub web interface as well as via the REST API and GraphQL API."
- Also: "too many requests that consume excessive compute resources in a short period of time" and 2,000 OAuth token requests an hour. "These secondary rate limits are subject to change without notice."
- "There is not a way to check the status of your secondary rate limit."

What a refusal carries and the waits asked for:
- REST primary: "you will receive a 403 or 429 response, and the x-ratelimit-remaining header will be 0. You should not retry your request until after the time specified by the x-ratelimit-reset header." (UTC epoch seconds.)
- REST secondary: "a 403 or 429 response and an error message that indicates that you exceeded a secondary rate limit. If the retry-after response header is present, you should not retry your request until after that many seconds has elapsed. If the x-ratelimit-remaining header is 0, you should not retry your request until after the time, in UTC epoch seconds, specified by the x-ratelimit-reset header. Otherwise, wait for at least one minute before retrying. If your request continues to fail due to a secondary rate limit, wait for an exponentially increasing amount of time between retries, and throw an error after a specific number of retries."
- GraphQL primary: "the response status will still be 200, but you will receive an error message, and the value of the x-ratelimit-remaining header will be 0." GraphQL secondary: "the response status will be 200 or 403". Same retry rules.
- "Continuing to make requests while you are rate limited may result in the banning of your integration." Mutations: "wait at least one second between each request" (best practices), serial rather than concurrent ("you should make requests serially instead of concurrently").
- The docs give no message texts. Observed ones: GraphQL primary, `"type": "RATE_LIMITED"` in the `errors` array with the message "API rate limit exceeded for installation ID 12345678." (an installation, https://github.com/github/docs/issues/11071; "always returns HTTP 200"); for a user, the title and body of https://github.com/orgs/community/discussions/184351: "GraphQL: API rate limit already exceeded for user ID" with `graphql` at `used: 5004` of 5000 and `core` at 592 (no GitHub staff reply). REST secondary: "You have exceeded a secondary rate limit and have been temporarily blocked from content creation." and "Please retry your request again later." (https://github.com/peter-evans/create-pull-request/issues/2062, where a maintainer doubts a `Retry-After` header is sent). The REST primary text is not sourced here.
- `x-ratelimit-resource` names the bucket a request counted against (`core`, `graphql`, `search`, ...). Note from the docs: "x-ratelimit-remaining may be higher on a later response than on an earlier one within the same rate limit window" and the headers "may differ from values reported by the GET /rate_limit endpoint. If the two disagree, rely on the response headers."

304 and conditional requests:
- "Making a conditional request does not count against your primary rate limit if a 304 response is returned and the request was made while correctly authorized with an Authorization header." (https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api). Whether a 304 counts against the secondary limit: **not stated** in the docs read.
- "Conditional requests for unsafe methods, such as POST, PUT, PATCH, and DELETE are not supported unless otherwise noted". GraphQL is POST, so every gh subcommand that uses GraphQL (most of them) can never get a 304. Only `gh api` on a REST GET can.
- "If a response includes an x-poll-interval header, wait at least that many seconds before you poll the same endpoint again."

## The 2026-10-22 incident

"API rate limit already exceeded for user ID ..." while `gh api rate_limit` shows GraphQL points left.

- Best-supported answer: the **GraphQL primary limit** (5,000 points per hour). It is the only place this wording is reported for a user (community #184351, `graphql` 5004 of 5000, everything else healthy). gh adds the `GraphQL: ` prefix only to a GraphQL error (go-gh `errors.go:59-69`); a REST refusal would print `HTTP 403: ... (https://api.github.com/...)`. The incident text as given to this ticket has no prefix, so even this is not certain. A REST `core` refusal and a secondary limit cannot be excluded from the text alone.
- Why points could be left afterwards, all consistent with the sources: the window is hourly (`x-ratelimit-reset`) so it may have reset before the check; `rate_limit` may lag the response headers (docs quote above); and, from the gh source, **a refused GraphQL answer can be replayed from disk**. Feature detection (`internal/featuredetection/feature_detection.go:260,263,468`: `PullRequest_fields`, `PullRequest_fields2`, `SearchType_enumValues`) runs through `api.NewCachedHTTPClient(httpClient, time.Hour*24)` (for example `pkg/cmd/issue/list/list.go:151`). The go-gh cache stores any answer where `res.StatusCode < 500 && res.StatusCode != 403` (`cache.go:51-53`) and a GraphQL refusal is HTTP 200, so a refusal that lands on one of these introspection queries is written to `~/.cache/gh` and returned for up to 24 hours, with no network call, to every `gh issue list --label`, `gh pr list --author`, `gh pr merge` and `gh pr checks` that needs it. A 429 would be cached too.
- Not determinable: which of these happened. The incident's `rate_limit` output, the response headers and the cache were not kept. The cache on this Mac today holds 31 stored responses and none contains an `errors` array or the words "rate limit", so any stored refusal has expired or never existed. The pause (task 4) should therefore compare a refusal against a fresh `gh api rate_limit` before pausing for an hour.

## How gh reports a refusal (v2.99.0)

No gh code recognises a rate limit except `pkg/cmd/skills/search/search.go:690-731` and the attachments uploader; every other command prints GitHub's message through one of four formatters. All exit **1** (`internal/ghcmd/cmd.go:46,226,248`; `gh api` returns `cmdutil.SilentError`, exit 1 at `cmd.go:200-201`). The exit codes that differ are 2 (cancel), 4 (auth), 8 (`pr checks` pending, `cmd.go:44-50`). A 401 adds `Try authenticating with:  gh auth login` (`cmd.go:234-240`). `printError` writes `fmt.Fprintln(out, err)` to stderr (`cmd.go:293`). Response headers are kept in the Go error (`HTTPError.Headers`) but never printed, so `retry-after` and `x-ratelimit-reset` never reach stderr.

Which formatter applies depends on the call (`api/client.go`):
- `Client.GraphQL` (`client.go:57`; used by the lookups of `issue view|close|edit|create`, `pr view|list|create|edit|merge|ready|review|diff|checks`, `repo view`, `run`): status outside 200-299 goes to `HandleHTTPError` (go-gh `graphql_client.go:76-79`); a 200 with `errors` becomes a GraphQL error (`graphql_client.go:96-98`).
- `Client.Query` / `Client.Mutate` (`client.go:69,81`; comments, close, merge, review, ready mutations, feature detection, all `gh project` commands, preloads): a non-200 is **not** an `HTTPError`. cli/shurcooL-graphql `graphql.go:88-91`: `return fmt.Errorf("non-200 OK status code: %v body: %q", resp.Status, body)`. A 200 with `errors` becomes a GraphQL error (go-gh `graphql_client.go:114-131`).
- `Client.REST` (`client.go:104`; `label create`, `pr diff`, `pr merge` branch delete, `run view|watch`): go-gh `HandleHTTPError`.
- `gh api`: its own printer, `pkg/cmd/api/api.go:549-562`.

The formats (go-gh `errors.go`):
- REST and `Client.GraphQL` non-2xx: `HTTP %d: %s (%s)` with the status code, GitHub's JSON `message`, and the request URL (`errors.go:35`); with a multi-line message, `HTTP %d: %s (%s)\n%s` (the first line, the URL, then the rest, `errors.go:33`); with no JSON body, the message is the HTTP status text (`errors.go:110-113`); with none at all, `HTTP %d (%s)` (`errors.go:37`). Array `errors` items are appended to the message (`errors.go:129-153`).
- GraphQL `errors` array: `GraphQL: %s`, items joined by `, `, each item as `message (path.joined.by.dots)` when it has a path (`errors.go:59-69`).
- `Query`/`Mutate` non-200: `non-200 OK status code: <status> body: "<JSON body, Go-quoted>"`. `<status>` is Go's `resp.Status`, for example `403 Forbidden` (HTTP/2 prints it without the version).
- `gh api` (`api.go:488-496,549-553,651-722`): `gh: <message> (HTTP <code>)` when the JSON body has a `message`; `gh: <message-1>\n<message-2>` joined for an `errors` array (a GraphQL error through `gh api graphql` therefore has no `GraphQL: ` prefix and no path); `gh: HTTP <code>` for any status over 299 with no parsed message. The body is still printed to stdout first.
- Commands wrap some of these: `pull request create failed: <error>` (`pr/create/create.go:1146-1150`), `could not find pull request diff: <error>` (`pr/diff/diff.go:162`), `failed to get run: <error>` (`run/view/view.go:259-260`).

The four kinds the pause must recognise (GitHub's part in `<>`; gh's part exact; the user id is shown as `<id>`):

| Kind | Where it surfaces | stderr (one line unless noted) | Exit |
|---|---|---|---|
| GraphQL primary limit (HTTP 200) | any GraphQL gh command | `GraphQL: API rate limit already exceeded for user ID <id>.` (message from community #184351; the trailing period is from the installation variant in docs#11071) | 1 |
| same, through `gh api graphql` | `gh api graphql ...` | `gh: API rate limit already exceeded for user ID <id>.` | 1 |
| 403 secondary limit, REST | `label create`, `pr diff`, branch delete, `gh api` | `HTTP 403: <You have exceeded a secondary rate limit and have been temporarily blocked from content creation. Please retry your request again later.> (https://api.github.com/<path>)`; `gh api`: `gh: <same message> (HTTP 403)` | 1 |
| 403 on the GraphQL endpoint | `Client.GraphQL` / `Query`/`Mutate` | `HTTP 403: <message> (https://api.github.com/graphql)` / `non-200 OK status code: 403 Forbidden body: "{\"message\":\"<message>\",\"documentation_url\":\"<url>\"}"` | 1 |
| 429 | any | `HTTP 429: <message> (<url>)` / `non-200 OK status code: 429 Too Many Requests body: "<body>"` / `gh: <message> (HTTP 429)` or `gh: HTTP 429` | 1 |
| "was submitted too quickly" | `issue create`, `pr create`, comments | GitHub's part is the error `{"type": "UNPROCESSABLE", "path": ["createIssue"], "message": "was submitted too quickly"}` (https://github.com/cli/cli/issues/4801, a script posting to GraphQL). By the formatter above, gh 2.99.0 would print `GraphQL: was submitted too quickly (createIssue)` (`pr create`: `pull request create failed: GraphQL: ...`; `gh api graphql`: `gh: was submitted too quickly`). **Derived from the source, never observed**; the 2021-2023 reports show older gh formats. | 1 |

Texts GitHub sends for 429 are not sourced anywhere read. A 403 is also what a missing permission returns (`github-gh-v2.md` lists a 403 "Upgrade to GitHub Pro" for branch protection on this account), so a match on `HTTP 403` alone is wrong: match the message (`rate limit`, case-insensitive).

`gh api` and 304 (`pkg/cmd/api/api.go:473-481,549-562`): gh never sends `If-None-Match` itself; pass `-H 'If-None-Match: "<etag>"'`. A 304 has no body and no JSON type, so `serverError` stays empty and line 549-550 sets it to `HTTP 304`: stderr `gh: HTTP 304`, **exit 1**, stdout empty. With `-i` stdout first gets `HTTP/2.0 304 Not Modified` (Go's `resp.Proto` and `resp.Status`, line 475), then the headers sorted by name, each as `Name: value\r\n` (`printHeaders`, `api.go:613-630`), then a blank `\r\n`. `--cache <duration>` (line 301) stores the answer under `~/.cache/gh` and makes no request while it is fresh.

Two behaviours that hide a refusal:
- `run watch` and `run view` (annotations): `shared.GetAnnotations` turns **every 403 into `ErrMissingAnnotationsPermissions`** (`pkg/cmd/run/shared/shared.go:301-302`). A secondary-limit 403 there is not reported as one: the output reads "requesting annotations returned 403 Forbidden as the token does not have sufficient permissions..." (`watch.go:274`) and, in `watch`, the loop goes on.
- Any 5xx or network failure inside `pr checks --watch` ends the loop and returns the error (`checks.go:227-230`); there is no retry in any gh command (go-gh has no retry code).

## Requests per command, mechanics shared by all

- Base repo (`-R O/R`, `GH_REPO`, or the git remotes): no API call. `pr`, `issue`, `repo`, `run`, `label`, `api` use `SmartBaseRepoFunc` (`pkg/cmd/root/root.go:164`), which calls the API only when it can prompt and no remote has a `resolved` config (`context/context.go:83-88`, `pkg/cmd/factory/default.go:152-176`). Harness calls are not terminals, so none. `gh project` uses the plain resolver.
- Update check: only when stdout and stderr are terminals and `GH_NO_UPDATE_NOTIFIER` is unset (`internal/update/update.go:81-88`); `GH_ENV` in `harness_lib/gh.py` already sets it.
- Telemetry: `gh config get telemetry` reads `enabled` on this Mac; events are posted by a `gh send-telemetry` process to `https://cafe.github.com` (`pkg/cmd/send-telemetry/send_telemetry.go:21`), a different host from api.github.com. Not counted below; whether it carries the token was not determined.
- Feature detection on github.com: `IssueFeatures`, `ProjectsV1`, `ProjectFeatures` return constants (`feature_detection.go:189-194,335-338,350-353`). `PullRequestFeatures` (`:225-264`) and `SearchFeatures` (`:399-472`) run 2 and 1 GraphQL introspection queries, cached on disk for 24 hours, so about 0 in steady state and 2 or 1 after a cold cache.
- Polling: `gh pr checks --watch` sleeps `--interval`, default 10 seconds (`checks.go:21,226`), one GraphQL query per tick (`checks.go:281`; +1 per further page of 100 contexts): 360 an hour. `gh run watch` sleeps `--interval`, default 3 seconds (`run/watch/watch.go:21,186`); each tick is `GetRun` (1 REST), `GetJobs` (1 REST per page) and `GetAnnotations` (1 REST **per job** still running; finished jobs are cached, `watch.go:220-253`): with 3 running jobs 5 requests every 3 seconds, 6000 an hour, which alone exceeds the 5,000 REST limit. Start: `GetRun` (1 REST) and `PullRequestForRun` (1 GraphQL, `watch.go:133,148`).

## The local probe: `GH_DEBUG=api gh api rate_limit`

Run 2026-10-23. gh prints the request and response **headers and the JSON body to stderr** (stdout gets the body once more). The token is redacted: the line is `> Authorization: token ` followed by 20 block characters, whatever the token length. The shape, with ids and per-account values replaced:

```
* Request at <timestamp> m=+0.2
* Request to https://api.github.com/rate_limit
> GET /rate_limit HTTP/1.1
> Host: api.github.com
> Accept: */*
> Authorization: token ████████████████████
> Content-Type: application/json; charset=utf-8
> Time-Zone: <tz>
> User-Agent: GitHub CLI 2.99.0 Agent/<agent>
> X-Github-Api-Version: 2022-11-28

< HTTP/2.0 200 OK
< Access-Control-Expose-Headers: ETag, Link, Location, Retry-After, X-GitHub-OTP, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Used, X-RateLimit-Resource, X-RateLimit-Reset, X-OAuth-Scopes, X-Accepted-OAuth-Scopes, X-Poll-Interval, ...
< Cache-Control: no-cache
< Content-Type: application/json; charset=utf-8
< X-Github-Request-Id: <request id>
< X-Oauth-Client-Id: <client id>
< X-Oauth-Scopes: <scopes>
< X-Ratelimit-Limit: 5000
< X-Ratelimit-Remaining: <n>
< X-Ratelimit-Reset: <epoch seconds>
< X-Ratelimit-Resource: core
< X-Ratelimit-Used: <n>
{ "resources": { "core": {limit, used, remaining, reset}, "search": {...}, "graphql": {...}, ... }, "rate": {...} }
```

- The response to `GET /rate_limit` itself carries `X-Ratelimit-Resource: core` and the core counters; its body also lists `graphql` (limit 5000), `search` (limit 30) and a dozen other resources (`integration_manifest`, `source_import`, `code_scanning_autofix`, `actions_runner_registration`, `scim`, `dependency_snapshots`, `dependency_sbom`, `audit_log`, `audit_log_streaming`, `code_search`, `copilot_usage_records`, `enterprise_token_inventory`), plus the legacy `rate` object ("The rate object is closing down", use `core`). Reset epochs differ per bucket (`core` and `graphql` reset at different moments).
- The debug stream has no `Retry-After` on a 200, as expected. A parser of the stream for the pause would read `< X-Ratelimit-Remaining: 0` and `< Retry-After: <s>` from a refused response. That works for every subcommand, at the cost of a large stderr (bodies included). A fresh `gh api rate_limit --jq` is the lighter source of `reset`.

## REST fields the volume tasks rely on

- `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` (https://docs.github.com/en/rest/checks/runs): query `status` "Can be one of: queued, in_progress, completed"; `filter` "Filters check runs by their completed_at timestamp. latest returns the most recent check runs. Default: latest. Can be one of: latest, all"; `per_page` "(max 100)", default 30; `page`; `check_name`; `app_id`. Response `{ "total_count": N, "check_runs": [...] }`, paginated with the `Link` header. A run's `status` can also be `waiting`, `requested` or `pending` ("reserved for GitHub Actions check runs", create-run parameter list); `conclusion` is `null` until completed, then one of "action_required, cancelled, failure, neutral, success, skipped, stale, timed_out". Cap: "If there are more than 1000 check suites on a single git reference, this endpoint will limit check runs to the 1000 most recent check suites." Needs the "Checks" read permission, or the `repo` scope for a private repo.
- `GET /repos/{owner}/{repo}/commits/{ref}/status` (https://docs.github.com/en/rest/commits/statuses): "a combined state is returned. The state is one of: failure if any of the contexts report as error or failure; pending if there are no statuses or a context is pending; success if the latest status for all contexts is success". Each status `state` is one of "error, failure, pending, success"; paginated (`per_page` max 100, default 30). It reports commit statuses only, not check runs.
- REST issue objects (the schema on https://docs.github.com/en/rest/issues/issues): `sub_issues_summary` `{ total, completed, percent_completed }` (integers) and `issue_dependencies_summary` `{ blocked_by, blocking, total_blocked_by, total_blocking }` (integers). Counts only: the blocking issues themselves come from `GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` and `.../blocking` (https://docs.github.com/en/rest/issues/issue-dependencies), and the children from `.../sub_issues`.

## Implications for the harness

Refusal detection (task 4's pause):
- Match on stderr, not exit code: every refusal is exit 1, and exit 8 from `gh pr checks` is "pending".
- Patterns, case-insensitive: `rate limit` (covers `GraphQL: API rate limit already exceeded...`, `HTTP 403: ... secondary rate limit ...`, `gh: ... rate limit ...`); `was submitted too quickly`; `HTTP 429` and `status code: 429` and `(HTTP 429)`. Do not match a bare `HTTP 403`.
- `run watch|view` can hide a 403 refusal as an annotations-permission message; do not call them in volume.
- A refusal can be replayed from `~/.cache/gh`. Before a long pause, call `gh api rate_limit` once (free on primary; "can count against your secondary rate limit", so once per pause, not per waiter): if `core` and `graphql` both have `remaining` above 0, the refusal is a secondary limit or a stale cached answer.

Backoff:
- Primary (`remaining` 0 in `rate_limit`): wait until that bucket's `reset` epoch (REST `core` and `graphql` reset separately and the pause can be per bucket: GraphQL commands and `gh api` REST calls are different buckets). The window is an hour, so a refusal at the start can mean a long pause.
- Secondary or unclassified (no bucket at 0): the docs' rule, "wait for at least one minute", then exponential, "throw an error after a specific number of retries". Honour `retry-after` or `x-ratelimit-reset` only when `GH_DEBUG=api` output was captured. There is no way to read the secondary state.
- Content creation: 80 a minute and 500 an hour at most, "Some endpoints have lower content creation limits"; a cli maintainer's advice for the undocumented "submitted too quickly" limit was to stop for an hour (cli/cli#4801, 2021) or put 3 seconds between creations; unconfirmed by GitHub docs. Serialise writes across processes and keep at least 1 second between them.
- Never poll with gh in a loop: a GraphQL poll cannot be conditional, and `pr checks --watch` / `run watch` multiply the cost (see above). Use `x-poll-interval` where a REST endpoint sends it; `gh api -H 'If-None-Match: ...'` on a REST GET makes unchanged polls free on the primary limit.

Requests per command, as the harness calls them (common case on github.com, non-terminal, `-R` or git remote; "intro" is the cached feature-detection query, 0 when warm). GraphQL points are about 1 per query by the docs' formula for ordinary field sets; list queries with nested connections (`pr list` with `statusCheckRollup`, `project item-list`) may cost a few; unmeasured.

| Command | API | Requests | Adds requests when | Source (v2.99.0) |
|---|---|---|---|---|
| `issue view N --json F` | GraphQL | 1 | +1 per further 100 comments if `comments` in F; +1 if `projectItems` in F; no `--json`: +1 (`projectItems`) | `issue/view/view.go:152-158`, `issue/shared/lookup.go:176,203` |
| `issue list --state S --json F` | GraphQL | 1 per page (default `--limit` 30; 200 is 2) | `--label`/`--search`: the GraphQL `search`, same 1 per page, +1 intro; `--assignee/--author @me` without search: +1 viewer lookup | `issue/list/list.go:235-262`, `list/http.go:91,128,193` |
| `issue create --title --body-file` | GraphQL | 2 (repo info, `createIssue`) | +1 per kind of `--label/--assignee/--project/--milestone` (labels: 1 per 100); `--parent/--blocked-by`: +2 each (id lookup, mutation); `--type`: +2 | `issue/create/create.go:287,457,478`, `pr/shared/params.go:60` |
| `issue edit N` (title/body) | GraphQL | 2 (lookup, `updateIssue`) | `--add-label`/`--remove-label`: +1 label list, +1 mutation each; relationships `--add-blocked-by` etc.: +2 each; projects: more | `issue/edit/edit.go:358,390,491,501`, `pr/shared/editable_http.go:17-36,61` |
| `issue comment N --body` | GraphQL | 2 (lookup, `addComment`) | `--edit-last`: lookup carries comments, then 1 mutation | `issue/comment/comment.go:86-97`, `api/queries_comments.go:74` |
| `issue close N` | GraphQL | 2 (lookup, `closeIssue`) | `--comment`: +1 mutation | `issue/close/close.go:112,158,164,207` |
| `pr view N --json F` | GraphQL | 1 | +1 per overflow page of reviews, comments, closing issues, status contexts (>100); `projectItems` in F: +1; `isInMergeQueue`/`isMergeQueueEnabled` in F: +2 intro | `pr/shared/finder.go:212-226,250,256,264-292` |
| `pr list --json F` | GraphQL | 1 per page | `--author/--assignee/--label/--search/--draft`: `search`, +1 intro | `pr/list/http.go:15-30,37,102`, `pr/shared/lister.go:118` |
| `pr create --draft --base --head --title --body-file` | GraphQL | 3 (repo info, existing-PR lookup, `createPullRequest`) | metadata flags: +1 per kind; reviewers/projects: +1 follow-up mutation; in a terminal with several remotes and no default: network lookup | `pr/create/create.go:794,512,1140`, `api/queries_pr.go:490-546` |
| `pr edit N --body-file -` | GraphQL | 3 (lookup, project items, `updatePullRequest`) | label or reviewer edits: as `issue edit`, +1 for reviewers | `pr/edit/edit.go:300,320,442`, `pr/shared/finder.go:290` |
| `pr ready N` | GraphQL | 2 (lookup, `markPullRequestReadyForReview`) | none | `pr/ready/ready.go:73,106`, `api/queries_pr.go:807-820` |
| `pr review N --comment -b` | GraphQL | 2 (lookup, `addPullRequestReview`) | none | `pr/review/review.go:153,181`, `api/queries_pr_review.go:273` |
| `pr merge N -R O/R --squash --delete-branch` | 2 GraphQL, 1 REST | 3 (lookup, `mergePullRequest`, `DELETE /repos/O/R/git/refs/heads/B`) | +2 intro (the lookup asks for `isInMergeQueue`) once a day; none for the branch delete on a cross-repo PR or auto-merge | `pr/merge/merge.go:571-573,525`, `pr/merge/http.go:103`, `api/queries_pr.go:872-877` |
| `pr diff N` | 1 GraphQL, 1 REST | 2 (lookup, `GET /repos/O/R/pulls/N` with the diff `Accept`) | none | `pr/diff/diff.go:138,160,213-239` |
| `repo view --json nameWithOwner` | GraphQL | 1 | without `--json`: +1 README; a bare name argument: +1 viewer | `repo/view/view.go:120`, `api/queries_repo.go:300` |
| `project item-add N --owner O --url U` | GraphQL | 4 (owner id, project, issue id, mutation) | none | `project/item-add/item_add.go:86-104` |
| `project item-edit N --owner O --url U --field F --value V` | GraphQL | 4 (owner id, project with fields, item by URL, mutation) | +1 per further 100 fields | `project/item-edit/item_edit.go:265,270,289,487` |
| `project item-list N --owner O --limit L` | GraphQL | 1 + pages (owner id, then 1 per 100 items; `--limit 1000` is up to 11) | the project is not asked for first when the number is given | `project/item-list/item_list.go:145,159`, `queries/queries.go:928-952` |
| `project field-list N --owner O --limit 100` | GraphQL | 2 (owner id, project with fields) | +1 per further 100 fields | `project/field-list/field_list.go:76,91` |
| `label create NAME --force` | REST | 1 for a new label (POST), 2 for an existing one (POST answers 422, then PATCH) | none | `label/create.go:144,156,182` |
| `run view ID --log-failed` | REST | 3 (run, jobs, log zip, which redirects to a storage host) | +1 per job log missing from the zip, at most 25; 0 for the zip on a repeat (disk cache); +1 jobs page per 100 jobs | `run/view/view.go:257,304,324,332,494-537`, `run/view/logs.go:81,152` |
| `api PATH` | REST or GraphQL | 1 | `--paginate`: 1 per page of 100 (REST, `Link` next) or per cursor (GraphQL); `{owner}`/`{repo}` placeholders: 0; `--cache`: 0 while fresh | `pkg/cmd/api/api.go:432-471,571-611` |
| `api rate_limit` | REST | 1, not against the primary limit | may count against the secondary | docs above, local probe |
| `pr checks N --watch` (reference) | GraphQL | lookup 1 + 2 intro, then 1 poll per 10 s | +1 per 100 contexts | `pr/checks/checks.go:159,178,226,281` |
| `run watch ID` (reference) | REST + GraphQL | 1 REST + 1 GraphQL, then 2 + running jobs REST per 3 s | see "Polling" | `run/watch/watch.go:133,148,186,220-253` |

Cross-checks for the volume tasks: one `issue view` or `pr view` per ticket is 1 GraphQL; mirroring one new issue to the Projects board is `item-add` (4) plus one `item-edit` per field (4 each), up to 12 GraphQL for Status and Priority, so the board mirror is the most expensive call per change; `project item-list --limit 1000` per sync is up to 11.
