# Research notes: where the GraphQL quota went (#448)

Gathered 2026-11-24 for ticket #448, after the 03:00:16–04:00:16Z hour on 2026-11-24 hit `GraphQL: API rate limit already exceeded for user ID 126895455.` with about 580 logged GraphQL calls. Measured read-only between 04:05Z and 04:20Z by the researcher. More readings came from the orchestrating session (harness-fe) at 03:22Z and 04:18–04:24Z. Sources: docs.github.com (REST "Rate limits for the REST API", "REST API endpoints for rate limits", GraphQL "Rate limits and query limits for the GraphQL API"), the local gh 2.99.0 (2026-09-01) at `/opt/homebrew/bin/gh`, its `~/.cache/gh`, the harness call log `~/.claude/harness/gh/calls-*.jsonl`, the session transcripts `~/.claude/projects/**/*.jsonl` (subagents included) and the VS Code logs. Each point is marked **verified** (quoted or measured) or **inferred**. It builds on `github-rate-limits.md` (#186), which has the limits in full and gh's requests per command.

Login: `rjeskuri`, an OAuth app token (`gho_` prefix, keyring; no token printed), scopes gist, project, read:org, repo, workflow; `GH_TOKEN` and `GITHUB_TOKEN` unset.

## Headline

The gap between GitHub's 5,000 points and the ~580 logged calls is mostly **not** unlogged calls. The log counts calls, and a call can cost far more than one point. One logged call, the board mirror's `gh project item-edit --url --field --value`, costs about 104 points (measured). About 60 of them ran between 03:00Z and the first refusal at 03:15:26Z, most from `harness-board promote <epic>` fan-outs. That is roughly 6,000 points, more than the whole hour's quota. The unlogged calls found add up to about 60–90 points.

## 1. Which limits apply to a gh OAuth token, and whether other apps' tokens share them

- **Verified: one budget per user, shared across credentials.**
  - REST: "Additionally, you can authorize a GitHub App or OAuth app, which can then make API requests on your behalf." / "All of these requests count towards your personal rate limit of 5,000 requests per hour." https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
  - GraphQL: "5,000 points per hour per user", including "requests made by a GitHub App or OAuth app on behalf of a user that authorized the app." https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
  - REST `/rate_limit` reference: "Requests authenticated as a user share the user's rate limit across credentials." https://docs.github.com/en/rest/rate-limit/rate-limit
  - So VS Code's PR and Actions extensions, Claude's GitHub integration and any other OAuth app acting as rjeskuri draw on the same 5,000 points as gh.
- **Verified: the exceptions have budgets of their own.** A GitHub App installation token gets 5,000 per installation ("cannot increase beyond 12,500"). An Actions `GITHUB_TOKEN` gets "1,000 points per hour per repository". Same GraphQL page.
- **Verified: what a call costs.** "Divide the number by 100 and round the result to the nearest whole number", assuming every `first`/`last` limit is reached; "The minimum point value of a call to the GraphQL API is 1." Mutations cost no more on the primary limit. The "5 points" for mutations is in the secondary-limit table only.
- **Verified: measured per command** (gh 2.99.0, `X-Ratelimit-Used` read step by step inside one `GH_DEBUG=api` run):

| Command | Requests | Points |
|---|---|---|
| `pr view 438` | 2 | 2 (1 each) |
| `pr view --json …` | 1 | 1 |
| `pr list` | 1 | 1 |
| `issue view --json number,title,url,body,labels,state,parent,blockedBy,comments` | 1 | 1 |
| `pr checks 438` | 4 | 2 (two are feature-detection queries from gh's 24 h cache, `X-Gh-Cache-Ttl: 24h0m0s`) |
| `project item-edit --url --field --value` | 4 | ~104 |

- **Verified: why `project item-edit` costs ~104.** Its `UserProjectWithFields` query asks for `items(first:100){fieldValues(first:100){…labels(first:10)…}}`. With `rateLimit(dryRun:true){cost}` it returned `{"cost":101}`. The live header went 174 → 275 inside one process. Project 3 has 712 items. That query plus the other three requests (`github-rate-limits.md`, the item-edit row) is about 104 per call. **Inferred:** the 75 fitted below (section 4) may mean the item-by-URL query costs less than assumed. One dry-run settles it (Needs the owner).
- **Verified: watch intervals** (installed binary's `--help`). `gh pr checks -i`: "Refresh interval in seconds in watch mode (default 10)", so 6 a minute and ~360 points an hour per watcher. `gh run watch -i`: "(default 3)", mostly REST.

## 2. What the refusal text means when quota remains

- **Verified: the text seen is the primary (hourly) limit.** The transcripts show `GraphQL: API rate limit already exceeded for user ID 126895455.` The cached body in `~/.cache/gh` holds `"type":"RATE_LIMIT","code":"graphql_rate_limit"` with `X-Ratelimit-Used: 5000`. The docs give no exact strings, only the shape: a GraphQL primary refusal is HTTP 200 with `x-ratelimit-remaining` 0, and a secondary one is 200 or 403.
- **Verified: the secondary limits**, on both the REST and GraphQL pages: "No more than 100 concurrent requests are allowed" (shared); "no more than 2,000 points per minute are allowed for the GraphQL API endpoint" (900 for REST); "No more than 90 seconds of CPU time per 60 seconds of real time", of which at most 60 s may be GraphQL; "no more than 80 content-generating requests per minute" and 500 an hour; 2,000 OAuth token requests an hour. "There is not a way to check the status of your secondary rate limit."
- **Inferred: telling the two apart.** "already exceeded … for user ID" is the per-user hourly bucket. "secondary rate limit" is the burst, concurrency or content-creation limit, which `rate_limit` cannot show. The REST content-creation text, "You have exceeded a secondary rate limit and have been temporarily blocked from content creation. Please retry your request again later.", is sourced only to peter-evans/create-pull-request#2062. No GraphQL secondary text could be verified.
- **Verified, and the usual answer to "refused while quota remains": the reading came from another counter, or the refusal came from cache.** A refusal while `rate_limit` shows points left means one of two things. Either the reading came from a lagging counter (section 3), or the refusal is a cached reply that gh replays (section 7).

## 3. Whether `rate_limit` is per token or per user, and why the headers disagreed

- **Verified: per user across credentials** (the quotes in section 1). REST `/rate_limit` is free: "Accessing this endpoint does not count against your REST API rate limit." **Not stated:** neither page says whether `rate_limit`/`rateLimit` reads a token-, app- or user-level view.
- **Verified: GitHub says the numbers can disagree, and the headers win.** "Because GitHub processes API requests in multiple regions, rate limit values can vary from one response to the next … based on the location handling the request." and "The x-ratelimit-* headers are the authoritative source for your current rate limit status … If the two disagree, rely on the response headers." https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- **Verified, measured: there are at least two GraphQL counters** for rjeskuri with the same reset (05:00:30Z). Separate gh processes land on one or the other.
  - Header samples, 04:09–04:13Z: 785, 169, 785, 785, 785, 785, 170, 786, then 901 four times. `rate_limit` read 168–170 throughout, later 279.
  - The high counter moves in steps. It held 785 for about 2 minutes, then rose by 111.
  - At 04:05:16Z `gh api -i graphql -f query='query{rateLimit{limit used remaining resetAt cost}}'` gave header 505 and body 505; `gh api rate_limit --jq .resources.graphql` right after gave 506.
- **Verified, measured: the high counter matches the log once item-edit is weighted by points.**
  - Logged calls since 04:00:30Z, including 6 item-edits by 04:05:49Z, predict ~782 at ~103 per item-edit; the counter read 785.
  - In the refused hour, `rate_limit` read 1,903 at 03:13:34Z and 2,016 at 03:15:19Z, against 544 logged calls.
- **Verified: the header and the body of one response disagreed at saturation, then agreed later.**
  - At 03:22Z (harness-fe), `gh api -i graphql -f query='{viewer{login} rateLimit{used remaining resetAt}}'` returned header Used 5000 / Remaining 0 while the body's `rateLimit.used` said 2032, and calls were being refused.
  - The researcher's ~20 samples after 04:05Z agreed header and body within each response.
  - At 04:18:33Z the header read 1067. One read at 04:23:25Z gave 314, an outlier. Three reads in a row at 04:24Z agreed, header and body: 1487, 1488, 1489, reset 05:00:30Z.
- **Verified: cached replies carry stale headers.** In `gh pr checks`, the cached feature-detection replies showed `X-Ratelimit-Used` 559/560 while the live counter was 903.
- **Inferred: why they disagree.** The high counter is the global, authoritative one that refuses. The low one is a regional or lagging view, which `/rate_limit` reads and which can also fill the body's `rateLimit`. That fits "multiple regions", and they diverge most at saturation. The step updates explain why enforcement trails the point where the log crosses 5,000.
- **Rule:** one read can be wrong, header against body or read against read. Sample several times and take the value that agrees, and keep raw `GH_DEBUG=api` output when it matters.

## 4. Attribution, 03:00Z to the first refusal at 03:15:26Z

The log, `calls-2026-11-24.jsonl`, starts at 03:00:07Z. The previous file ends at 23:30:08Z, and the log and transcripts show no gh calls 02:20–03:00Z (21 tool uses, none gh). The quota hour ran ~03:00:16–04:00:16Z (the cached refusal's reset, 1795492816). From 03:00 to 03:20Z the log holds 577 GraphQL and 91 REST calls. GraphQL by tool and command:

- harness-work `issue view`: 210
- harness-board `issue view`: 151
- harness-board `project item-edit`: 61
- `issue edit`: 60
- `issue list`: 24
- `issue comment`: 21
- harness-pr `pr view`: 14
- harness-work `pr list`: 7
- `repo view`: 6 + 5
- everything else: 2 or fewer each

By repository: Harness 214, FantasyGolf 128, Assistant 114, HorseTrackBetting 78, jotform 43. The first refusal came at 03:15:26Z, and 147 calls were refused through 04:00:11Z.

Ranked by estimated points (item-edit at ~104; **inferred** from the measured costs above):

| # | Source | Calls | Points, est. |
|---|---|---|---|
| 1 | HorseTrackBetting `harness-board promote 3 && harness-board promote 4` (03:09:01–03:11:40) | 23 item-edits | ~2,400 (~1,750 at a fitted 75) |
| 2 | FantasyGolf `harness-board promote 1` (03:06:44–03:10:18) | 21 item-edits | ~2,200 |
| 3 | Harness `close 430`, `promote 433/439/443` | 10 item-edits | ~1,000 |
| 4 | Assistant status and close | 6 item-edits | ~620 |
| 5 | jotform `task create` (board mirror) | 3 item-edits | ~310 |
| 6 | All other logged GraphQL | ~486 | ~500–700 |
| 7 | Unlogged agent gh in Bash, plus the jotform poller | ~30 tool uses + ~14 | ~60–90 |
| 8 | VS Code extensions | 0 | 0 |

The path, in `plugins/harness/bin/harness-board`: `cmd_promote` → `set_status` for each open proposed sub-issue → `mirror()` → `gh project item-edit … --url --field Status --value`. Promoting an epic with n proposed tasks costs about n × 104 points.

**Verified, measured: fitting the cost to the refusal time.** Weighted at 75 per item-edit, the log crosses 5,000 at exactly 03:15:26Z, the first refusal. Weighted at 103, it crosses at 03:10:22Z. Item-edit's true cost lies between those, and the counter's step updates (section 3) can account for the lag.

## 5. The unlogged sources, and what they cost

**Verified** from the transcripts (02:20–03:20Z: 609 tool uses, 415 Bash, 7 Monitor; 38 with a direct gh, all after 03:00Z):

- By project: Assistant 12, Harness 9, FantasyGolf t-3 6, HorseTrackBetting 5, jotform 3, Harness t-440 2, jotform t-262 1.
- None used `gh pr checks --watch` or `gh run watch`. No harness skill or agent tells an agent to poll gh.
- The biggest were:
  - HorseTrackBetting `gh issue list --limit 200` ×2 (~4)
  - a jotform `gh api graphql` parent loop (~4)
  - an Assistant subagent's `gh pr checks 61` ×2 (~4)
- Loops:
  - jotform-glow-sync, running since 03:02Z, makes `gh pr list` + `gh issue view 262` every 120 s, ~1 point a minute, unlogged.
  - Harness `harness-work runs --sweep` every 300 s is logged, 3 GraphQL per tick.
  - An Assistant `docker restart` loop made 12 REST `actions/runners` calls.
- After the refusal, outside the window:
  - a Harness `gh project item-list 3 --limit 300` at 03:16:09Z, ~300 points (3 pages × 101)
  - HorseTrackBetting `rateLimit` / `issue view` pollers started at 03:15:37Z and 03:19:56Z
- Claude's GitHub integration is server-side and was not measured.

Total before the refusal: about 60–90 points, 1–2 % of the hour.

## 6. VS Code: ruled out

**Verified** from its logs:
- Installed: github.vscode-pull-request-github 0.180.0 and github.vscode-github-actions 0.32.3.
- No VS Code process was running; `main.log` shows the extension host exited at 03:23:31Z.
- `GitHub Authentication.log`: every entry is "Got 0 sessions for …".
- The PR extension's log says "Skipping poll: window is not focused" and shows no API activity on 11-24 before it quit.

VS Code had no GitHub session and spent none of the quota. There is no need to close it for a measurement.

## 7. Hazard: a cached refusal replayed for up to 24 h

**Verified:** `~/.cache/gh` holds a refusal written at 03:52:22Z (`RATE_LIMIT`, `X-Ratelimit-Used: 5000`). gh replays it for up to 24 h to any command that sends the same feature-detection request, whatever the quota is now. This is cli/cli#12812, open, reproduced on 2.105.0 on 2026-11-20: https://github.com/cli/cli/issues/12812. A refusal while every live read shows points left should be checked against this cache first.

**Which commands (#470).** **Verified** only for `gh pr checks` (section 1: two of its four requests are feature-detection queries served from the 24 h cache, `X-Gh-Cache-Ttl: 24h0m0s`). **Not confirmed:** the full list of commands that send the cached query and a replayed refusal end to end; that needs `GH_DEBUG=api` runs, which spend quota, so it was not done. The detection is therefore by content, not by command: any file under gh's cache dir whose body holds `"type":"RATE_LIMIT"`.

**Detect and clear (#470).** `harness-work budget status` names each such file (`$XDG_CACHE_HOME/gh`, else `~/.cache/gh`, any depth) and `harness-work budget clear-gh-cache` removes exactly those files and nothing else; a plain `gh config clear-cache` would also drop the valid entries. The entries are regenerable, so clearing costs a few points at most, but it is an explicit command, not run by status or dispatch.

## 8. How the shim and `budget calls` close the logging gap

- **The gh shim** (`plugins/harness/shim/gh`, design.md "The gh shim"). Each session's SessionStart line in `CLAUDE_ENV_FILE` puts it first on PATH. It runs the real gh unchanged and logs one `tool: "agent"` line per call. The harness runners mark their gh children `HARNESS_GH_LOGGED=1`, so a harness call through the shim is logged once. That covers section 5's agent gh in Bash. It does not cover the jotform poller, which runs outside a Claude session, nor VS Code or other apps.
- **`harness-work budget calls`** now makes one `gh api -i graphql -f query=query{rateLimit{limit used remaining resetAt}}` after reading the log. `--json` gains `github`: the header's used, limit, remaining and reset, the window start (reset − 1 h), the body's `rateLimit.used`, the logged GraphQL calls in that window, and `gap` = used − logged. The text adds one line with the same figures.
- **Read the gap in points, not calls.** The log counts calls, so a gap of a few thousand with item-edits in the window is section 4's cost, not missing lines. One item-edit is worth ~104 logged calls.
- **`budget calls`' own rate-limit call is not logged.** It was thought to cost about 1 point a run (three consecutive reads went 1487 → 1488 → 1489); section 9 measured about 0 over 198 reads. A line stamped with the real clock would prune a log read under a pinned `HARNESS_NOW`, and one stamped with the pinned clock would count in the next report. Taken at 1, each run would add 1 to the next run's gap.
- **Single readings.** Section 3 applies to `budget calls`: one reading can come from the lagging counter. Run it two or three times and take the agreeing figure.

## 9. An hour with the shim on (#468)

Measured 2026-11-24 by the orchestrating session, with the shim first on PATH (`command -v gh` = `plugins/harness/shim/gh`). The sampler wrote `~/.claude/harness/gh/quota-samples-2026-11-24.jsonl` from 10:52:20Z to 12:04:22Z. Each minute it made three `gh api -i graphql rateLimit` reads (header `X-Ratelimit-Used`, header reset, body `rateLimit.used`) and one free REST `gh api rate_limit`. It called `/opt/homebrew/bin/gh` directly, so its reads are not in the call log.

**The hour.** GitHub's counters rolled over at 11:03:22Z and again by 12:03:22Z, so the hour is 11:03:22–12:03:22Z. The two counters of section 3 both persisted all hour. A read's reset header told them apart: the high counter read 12:03:13Z, the low one 12:03:28Z. 66 sampler ticks fall in the window: 198 reads, 142 on the high counter and 56 on the low one. The high counter's value is the max of a tick's reads that share its reset, and the reads agreed.

| | Value | |
|---|---|---|
| Header, high counter, last read 12:02:22Z | **351** | **Verified**; the next tick, 12:03:22Z, was already in the new window |
| Header, low counter, same tick | 106 | **Verified**, 30 % of the high one; REST `rate_limit` mostly read this one |
| Logged GraphQL calls, 11:03:22–12:02:22Z | 359 | **Verified**; 363 to 12:03:22Z, 363 (to 12:02:22Z) if the window starts at the high counter's own reset, 11:03:13Z |
| Logged REST calls, whole hour | 3,190 | **Verified**; no GraphQL points. harness-board 2,145, harness-work 807, harness-pr 198, unit tests 24, agent 16 |
| Points-weighted log | 359 (363 to 12:03:22Z) | **Verified**: no `project item-edit` and no `project item-list` ran, so every weight is 1 |
| Sampler points | 198 reads, about 0 points | **Verified**, see below |
| Raw remainder, header minus log | 351 − 359 = **−8** (−2.3 %) | the log is over, not short; −12 (−3.4 %) against the 363 |

Logged GraphQL by tool (to 12:03:22Z, 363): harness-pr 287, land_owner.py 26, python3 -m unittest 16, agent 11, harness-work 10, harness-deadcode 8, harness-board 3, session_brief.py 2. Top commands: `pr view` 214, `repo view` 34, `pr list` 26, `issue view` 17, `pr merge` 12, `pr diff` 11, `pr create` 11, `pr ready` 10, `issue list` 9, `pr review` 7. By repo: HorseTrackBetting 218, Harness 127, proj 16, Assistant 2. **Verified:** no line is refused or waited (`refused` false, `waited_ms` 0). Two failed (`pr merge` exit 1, `pr checks` exit 8); both still sent their request, so both count.

**What the log adds up to.** The high counter tracked the log call for call: its rise per minute equals the logged GraphQL calls of that minute, within a few. The hour ends within 8 points of the log, 2.3 % of the header count, the log being over. Two named items and the counter's lag explain it:

- **The log is over by 16: unit-test lines that never reached GitHub. Inferred.** At 11:15:03–11:15:05Z the log holds 16 lines from `python3 -m unittest`, repo `proj`, ticket 476, 30–40 ms each, in four repeats of `pr view`, `issue list`, `issue view`, `api graphql`. That is a test suite driving a fake `gh` through the shim. In that interval the log rose by 32 and the counter by 13. Without them the log is 343, 8 *below* the header.
- **The log is short by 5: direct `gh` in the orchestrating Harness session. Inferred.** Its transcript has five GraphQL calls before 12:02:22Z that are not in the log: `issue view 489` and `pr list` at 11:07:07Z, `issue view 455` and `pr list` at 11:32:51Z, `pr view 495` at 11:32:57Z. Every other direct `gh` of the hour in the transcripts matches a log line, so that session most likely started before the shim was on its PATH. Two more (12:02:33Z) fall after the last read. With both items the log is 348 against 351: a remainder of +3, under 1 %.
- **The counter lags.** It moves in steps (section 3). Early in the hour it ran 9–18 ahead of the log (11:03–11:14Z); the 32-call burst at 11:15Z closed that. The last read also has up to a minute of uncertainty: 4 logged and 2 direct calls fall between 12:02:22Z and the reset.
- **Not found: `harness-work budget calls`.** No transcript ran it in the window. The jotform-glow-sync poller of section 5 (about 60 points an hour) does not show either: the remainder leaves no room for it.

**The sampler's reads cost nothing.** If each of the 198 reads cost 1 point, the high counter would read about 500 (359 calls plus the 142 reads that landed on it). It read 351. Six reads in a row at 11:40:58–11:41:08Z moved neither counter. Section 8's "about 1 point a run" was therefore other calls landing between its reads, and `budget calls`' own unlogged read adds about 0 to the next gap. **Not checked:** what the GraphQL docs say about a `rateLimit`-only query.

**The previous window, for the "take the agreeing value" rule.** `harness-work budget calls --hours 1 --json` at about 10:51Z reported header used 383 (a low-counter read) against `logged_graphql` 1,383, a gap of −1,000 from the lagging counter alone. The sampler's reads from 10:52Z show both counters in that window: the high at 1,508–1,545 and the low at 384–403. The high counter's 1,545 at 11:02:28Z against the points-weighted log of 1,535 (1,432 calls, one `project item-edit` at 10:07:59Z worth 104) leaves +10, 0.6 %.

**Result.** The header count is 351, the log 359 calls and 359 points, and the remainder is −8 (−2.3 %): under 10 %, and it is the log that is over. Named: 16 test lines that never reached GitHub, 5 calls the log missed because one session ran without the shim, and the counter's lag. For the sessions that had the shim on PATH, it logged what GitHub counted.

**Observed, not settled.** The low counter rose 1 → 106 against the high one's 10 → 351, and 28 % of the sampler's reads landed on it, close to the 30 % ratio. Which counter refuses is not answered: nothing was refused, and the high one peaked at 351 of 5,000. The REST core counter was not sampled. The 3,190 logged REST calls are 64 % of its 5,000 an hour if they all count against it, which is worth one sample.

## Needs the owner's measurement

- **Done (#468): an hour of real runs with the shim on**, in section 9: header 351, log 359 calls and points, remainder -8 (-2.3 %), named.
- One real `gh project item-edit` with a dry-run cost of its item-by-URL query, to settle 75 against 101 + 3.
- Whether the two counters converge and which one refuses. Section 9's hour shows both persisting for an hour, the low one at about 30 % of the high one, and nothing refused. Samples from a saturated hour would settle it.
- https://github.com/settings/applications (owner only): any other OAuth or GitHub App acting as rjeskuri.
- Which gh commands send the cached feature-detection query, beyond `pr checks` (section 7, not confirmed).
