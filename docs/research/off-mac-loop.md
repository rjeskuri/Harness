# Research notes: running the harness loop while this laptop is off

Gathered 2026-11-24 for #440 (epic #439) on the MacBook Pro (M1 Pro, 8 cores, 16 GB, macOS 26.5.2), with Claude Code 2.1.331 and gh 2.99.0. Sources: this Mac, read only (the LaunchAgents, `~/.claude/harness/`, the run records, the transcripts under `~/.claude/projects/`, `ps`, `pmset`, `claude auth status`, `gh auth status`, `gh auth login --help`, `gh help environment`, `harness-runner list`, `harness-projects list`, the 2.1.331 binary, the plugin source); two `gh search issues` calls; and the price pages and docs quoted below, all fetched 2026-11-24. The counting scripts lived in the job's scratch directory, outside the repository. No account, trial, VPS, cloud session, scheduled task (routine) or LaunchAgent was created, nothing was bought, and no secret was moved, created or printed. This note names credentials but holds none of their values, and no account ids.

## Verified

### What the loop needs from this Mac (local, read only)

- **LaunchAgents:** `ls ~/Library/LaunchAgents | grep harness` lists only `com.ryaneskuri.harness-digest.plist`. The hourly `com.ryaneskuri.harness-dispatch.plist` (`harness-work dispatch --install-launchd`) is not installed, so today every dispatch starts from one of the owner's sessions.
- **State:** `~/.claude/harness/` (86 MB) holds `backups budgets.json digest gh projects.json repos runner runs smoke`. That is 922 files in `runs/` (463 run records and their logs) and 13 folders in `repos/`. `repo-key` is the sha1 of the main checkout's canonical path (docs/design.md, State), so a machine with other paths gets other keys.
- **Transcripts:** `~/.claude/projects/` is 2.5 GB in 253 folders, 239 of them worktree folders. `harness-worklog` reads a repository's own folder plus its worktree folders, named from the absolute path (docs/design.md, harness-worklog), and `harness-work cost` reads the same transcripts.
- **Logins:**
  - `gh auth status`: "Logged in to github.com account <login> (keyring)", with scopes `gist, project, read:org, repo, workflow`. harness-board needs `project`: it calls `gh project item-add`, `item-edit`, `field-list` and `item-list` (`plugins/harness/bin/harness-board`).
  - Where gh keeps that token (`gh auth login --help`, gh 2.99.0, read 2026-11-24): "an authentication token will be stored securely in the system credential store. If a credential store is not found or there is an issue using it gh will fallback to writing the token to a plain text file. See `gh auth status` for its stored location." `--insecure-storage` saves it "in plain text instead of credential store". "To use gh in GitHub Actions, add `GH_TOKEN: ${{ github.token }}` to `env`."
  - `gh help environment` (same version and date): `GH_TOKEN`, `GITHUB_TOKEN` "takes precedence over previously stored credentials"; `GH_CONFIG_DIR` defaults to `$XDG_CONFIG_HOME/gh` or `$HOME/.config/gh`.
  - `claude auth status --json` (only these keys were printed): `{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}`.
- **API key:** `HARNESS_API_KEY_CMD` is unset (no `env` entry in `~/.claude/settings.json` but `HARNESS_PROJECT`), so the default applies: `security find-generic-password -s harness-anthropic -w` (`plugins/harness/bin/harness-work`, `DEFAULT_KEY_CMD`). `security find-generic-password -s harness-anthropic >/dev/null; echo $?` printed 0, so the item exists. It was not read. It matters only for headless dispatch, and `budgets.json` has `api` (and `cloud`) "exhausted_until": "2027-11-20T04:45:57Z" ("Ryan, 2026-11-20: ... off for all repos until he resets it"). `subscription` is not exhausted.
- **CI runners (#420):** `harness-runner list` lists five private repositories: accessible-iceland, kalshi-markets, edu, a-chance-for-glory and Assistant. The first four are `exited`, Assistant's is `running`, and all five are `offline` on GitHub, "PRs queue for up to 24 h".
- **macOS-only parts of the plugin** (a grep of `plugins/harness` outside `tests/` for `osascript|terminal-notifier|security|launchctl|darwin|cp -c`):
  - `lib/harness_lib/notify.py`: "A macOS notification: terminal-notifier when on PATH, else osascript." Its callers are harness-board `needs-input` and `park`, harness-pr `ready` and revert, harness-digest, and `hooks/notify.py`. Elsewhere `notify()` returns False and `hooks/notify.py` rings the terminal bell.
  - `bin/harness-work` and `bin/harness-digest`, `--install-launchd`: they write `~/Library/LaunchAgents/*.plist` and run `launchctl bootstrap gui/<uid>`.
  - `bin/harness-work`: `DEFAULT_KEY_CMD`, the keychain through `security`.
  - `lib/harness_lib/backups.py`: `cp -c` clones only when `sys.platform == "darwin"` and otherwise copies, so it is portable.
  - The rest (hooks, harness-board, -pr, -red, -runner) is python3 standard library plus git, gh and docker.
- **Projects:** `harness-projects list` shows 13 registered projects. Two are billable: jotform-glow-sync ("Global G.L.O.W.") and okinawa-dream-reservation ("Okinawa Dream").
- **needs-local and ops tickets:**
  - `gh search issues --owner <owner> --label needs-local`, open by repository: HorseTrackBetting 36, jotform-glow-sync 22 (60 closed), Assistant 9, okinawa-dream-reservation 7, FantasyGolf 4, Harness 3, mcp-art-lab 3, HKTrack 2, kalshi-markets 1. That is 161 open and closed.
  - The same search with `--label ops`, open: HorseTrackBetting 4, jotform-glow-sync 1, okinawa-dream-reservation 1, project-remora 1.
- **Credentials behind needs-local (names only):**
  - jotform-glow-sync's `.env` keys are `JOTFORM_API_KEY`, `GLOW_SHEET_ID` and `GLOW_SA_KEY`.
  - Its `credentials/` holds `config.json` and `service-account.json`.
  - `.worktreeinclude` copies `.env`, `.env.*` and `credentials/` into every worktree.
  - `~/.sf/` holds the Salesforce CLI's caches and logs.
  - The Salesforce connectors this session sees are claude.ai connectors, so they follow the claude.ai login, not the Mac.
- **Bg dispatch does not skip needs-local:** in `bin/harness-work` only `cloud N` refuses "a needs-local or ops ticket". `dispatch` plans from `harness-board ready` whatever the labels.
- **Staying awake today:**
  - `pmset -g`: "sleep 0 (sleep prevented by caffeinate, caffeinate, ...)", "powernap 1", "womp 1", on "AC Power".
  - `ps` shows `caffeinate -i -t 300` children of live claude sessions. `man caffeinate`: "-i Create an assertion to prevent the system from idle sleeping."
  - The 2.1.331 binary starts `caffeinate` on macOS and `systemd-inhibit` on Linux ("Started ${n} to prevent sleep", `linuxInhibitor`). That covers idle sleep only, not a closed lid.
  - `man pmset`: "pmset repeat wakeorpoweron T 12:00:00 sleep MTWRFSU 20:00:00" schedules a repeating wake.

### Dispatch volume (counted 2026-11-24 from `~/.claude/harness/runs/*.json`)

How it was counted: every record was parsed (463, none unreadable). "Month" is the UTC month of `started_at`. A record's length is `ended_at` minus `started_at`. For bg runs the record's `ended_at` is when a later sweep closed it, which can be hours after the session went quiet, so bg runs also get an **active span**: `started_at` to the last timestamped entry of the main transcript at or before `ended_at`. The transcript is the job's `linkScanPath`, else `~/.claude/projects/*/<bg_id>*.jsonl`. 228 bg runs have a transcript; 123, mostly from October, do not (jobs pruned, project moved). Peak concurrency is a sweep line over [start, end), with ends sorted before starts in the same second.

| | Oct 13-31 | Nov 1-24 |
|---|---|---|
| bg runs (this Mac) | 146 | 210 |
| headless runs (this Mac, API key) | 49 | 0 |
| cloud runs | 11 | 47 |
| bg active run-hours (transcript spans; Oct covers 23 runs only) | 266 | 239 |
| wall-clock hours with at least one local run open (records) | 308 | 166 |

- **Run length:**
  - bg active span: median 44 min, p90 283 min, max 2,474 min (n = 228).
  - bg by record: median 84 min, p90 1,470 min. This is inflated by late sweeps.
  - headless: median 22 min, p90 94 min.
  - cloud records: median 3 min, p90 52 min (the record is about the launch, and the work runs elsewhere).
- **Peak live runs:**
  - Active spans peak at 12 bg runs at once, on 2026-10-24 09:56Z and again on 2026-11-19 13:50Z, both before the cross-project cap of 8 (`dispatch --all-projects --max`, `ALL_PROJECTS_MAX`; per-repository dispatch defaults to 10, `DISPATCH_MAX`, and interactive launches have no cap). The October figure covers only runs whose transcripts survive, so a later recount may differ.
  - By record it is 24 on 2026-10-24, from records closed late.
  - Hours at each level of concurrency (active spans): 1: 45 h, 2: 23 h, 3: 17 h, 4-8: 24 h, 9-12: 23 h. Most of the time the count is 0.
- **Rhythm:** runs started on 22 of the 43 days, with a median of 14.5 and a maximum of 59 runs on an active day. Start hours (Asia/Tokyo) spread from 10:00 to 02:00 and peak at 16:00 and 18:00.
- **By repository (all venues):** Harness 164, OkinawaDream 111, jotform-glow-sync 110, edu 43, Assistant 17, KalshiMarkets 12, McpArtLab 5, FantasyGolf 1.
- **One session's footprint:**
  - Sampled 2026-11-24 with `claude agents --json` (13 sessions: 7 background busy, 6 interactive) and `ps -axo pid,ppid,rss,%cpu` over each session's process tree. A bg session's tree is 366-453 MB RSS and 0.8-10 % of one core at sample time.
  - The tree includes the user-level MCP servers (playwright, cmux-cua) and `caffeinate`.
  - One test module, `/usr/bin/time -l python3 -m unittest ... -p test_digest.py`, used 23.9 MB maximum RSS and 6 s.
  - A Harness worktree is 7.2 MB.
  - So 8 sessions fit in about 4 GB plus the test runs. CPU is mostly idle because the sessions wait on the model. Sizing: 4 vCPU / 8 GB for the loop alone, 8 vCPU / 16 GB with the five runner containers (each capped at 2 GB, 2 CPUs).

### (a) Hosted

- **MacStadium** (https://www.macstadium.com/pricing, fetched 2026-11-24):
  - "M2.S Mac mini / M2 8 Core / 8 GB / 256 GB SSD / $109"
  - "Individual MacStadium bare metal subscriptions are available on a monthly basis with pre-payment on the first day of the calendar month."
- **AWS EC2 Mac** (https://aws.amazon.com/ec2/instance-types/mac/; hourly rates from the price feed those pages load, https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/dedicatedhost-ondemand.json; both fetched 2026-11-24):
  - "Billing for EC2 Mac instances is per second with a 24-hour minimum allocation period."
  - The rate is $0.650/h for mac2.metal (M1, 16 GB) in us-east-1, which is $474.50 for 730 h, before EBS.
- **Scaleway Apple silicon** (the web pages answered 403; the public catalog https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=apple_silicon, fetched 2026-11-24):
  - "Mac Mini M1 - M - month - fr-par-1 (75€ per month)" (8 GiB). M2-M (16 GiB) is €115 a month.
  - From the docs repository: "The minimum lease period for Apple silicon is 24 hours due to licensing restrictions."
- **GitHub Codespaces** (fetched 2026-11-24):
  - https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle: "A codespace has a maximum lifetime of 12 hours, regardless of your idle timeout policy or settings."
  - https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-codespaces/about-billing-for-github-codespaces: "2 core 1 hour ... $0.18"
- **Daytona** (fetched 2026-11-24):
  - https://www.daytona.io/pricing: "vCPU $0.0504/h Memory GiB $0.0162/h". 1 vCPU / 4 GiB for 730 h is about $84.
  - https://www.daytona.io/docs/en/sandboxes.md: "The auto-stop triggers even if there are internal processes running in the sandbox." The default is 15 minutes, and 0 turns it off.
- **Ona (formerly Gitpod):** https://ona.com/pricing (fetched 2026-11-24) shows no public plan price.

### (b) Self-hosted, always on

- **Hetzner Cloud** (https://www.hetzner.com/cloud/cost-optimized and https://www.hetzner.com/cloud/regular-performance, fetched 2026-11-24):
  - CX33, 4 vCPU / 8 GB / 80 GB, €8.99, and CX43, 8 / 16 / 160, €16.49, "per month, excl. VAT", "Price incl. IPv4", in the EU only.
  - The Singapore CPX32 (4 / 8 / 160) is "€ 49.49 per month, excl. VAT".
  - "Your server's bill will never exceed its monthly price cap."
  - Every Create button said "This product is currently unavailable. Please check back later."
- **DigitalOcean** (https://www.digitalocean.com/pricing/droplets, fetched 2026-11-24): Basic, 8 GiB / 4 vCPU / 160 GiB, is $48.00/mo, and 16 GiB / 8 vCPU is $96.00/mo. "For Bundled Plans, a public IPv4 is included at no extra cost."
- **Akamai (Linode)** (https://www.akamai.com/cloud/pricing/cpu, fetched 2026-11-24): "Linode 8 GB", 4 vCPU / 8 GB / 160 GB, is $48.00/mo. The first plan with 8 vCPU is "Linode 32 GB" at $192.00/mo.
- **Mac mini at home:**
  - https://www.apple.com/jp/shop/buy-mac/mac-mini (fetched 2026-11-24): the base M6, 16 GB / 256 GB, is "149,800円" including tax. The US page, https://www.apple.com/shop/buy-mac/mac-mini, says "$899.00".
  - Power, https://support.apple.com/en-us/103253 (fetched 2026-11-24): "Mac mini (M6) ... Idle ... 4 W", max 70 W.
- **Electricity** (https://www.tepco.co.jp/ep/private/plan/old01.html, fetched 2026-11-24): 従量電灯B from 2026-11-01, third tier "40円54銭" per kWh including tax. The fuel adjustment and the renewables levy are extra.
- **Raspberry Pi 5:** the 8 GB board alone is "$175" (official product brief, https://pip-assets.raspberrypi.com/categories/892-raspberry-pi-5/documents/RP-008348-DS-10-raspberry-pi-5-product-brief.pdf, fetched 2026-11-24).
- **Exchange rate** (ECB, 23 November 2026, fetched 2026-11-24): "USD 1.0939", "JPY 174.01", so 1 USD ≈ 159.07 JPY.
- **Claude Code on Linux** (fetched 2026-11-24):
  - https://code.claude.com/docs/en/setup: "Ubuntu 20.04+ / Debian 10+ / Alpine Linux 3.19+".
  - https://code.claude.com/docs/en/agent-view: "on macOS and Linux, your sessions stop unless a new supervisor connects within about a minute", and "background sessions consume your subscription usage the same as interactive sessions".
  - https://code.claude.com/docs/en/hooks: "`sh -c` on macOS and Linux".
  - The plugin docs (`plugin-marketplaces`, `cli-reference --plugin-dir`) state no OS limit.
  - https://code.claude.com/docs/en/authentication: "On Linux, credentials are stored in `~/.claude/.credentials.json` with file mode `0600`."
  - The same page says of the headless login: "generate a one-year OAuth token with `claude setup-token`" ... "It can only make model requests, so it can't establish Remote Control sessions or fetch claude.ai connectors."

### (c) The rest

- **Cloud sessions** (fetched 2026-11-24):
  - https://code.claude.com/docs/en/cloud-environments: "Plugins enabled only in your user settings | No", and "Marketplaces declared in your repo's `.claude/settings.json` | No, in an Anthropic-hosted environment".
  - https://code.claude.com/docs/en/plugins/install: "To use a plugin in a cloud session, add it to your claude.ai account at claude.ai/customize/plugins, then start a new session."
  - GraphQL, from cloud-environments: "the proxy rejects requests to GitHub's GraphQL endpoint with a 403 ... `gh` subcommands that use GraphQL, such as `gh pr` and `gh issue`, get the same 403."
  - Billing, https://code.claude.com/docs/en/claude-code-on-the-web: "It counts toward your plan's usage limits ... there's no separate charge for the cloud VM."
  - 2.1.331 binary: "Without an interactive terminal, --cloud can only send the prompt to an existing cloud session ... To start a new cloud session, run in an interactive terminal."
- **Scheduled tasks, formerly routines** (https://code.claude.com/docs/en/routines, fetched 2026-11-24):
  - "Scheduled tasks draw down subscription usage the same way interactive sessions do."
  - "Without usage credits, additional runs are rejected until your usage window resets."
  - "Expressions that run more often than once per hour are rejected."
  - "they keep working when your laptop is closed"
  - https://code.claude.com/docs/en/desktop-scheduled-tasks: "Tasks only run while the desktop app is running and your computer is awake."
- **GitHub Actions** (fetched 2026-11-24):
  - https://docs.github.com/en/billing/concepts/product-billing/github-actions: GitHub Free includes "$12 USD" of compute a month; "Linux 2-core (x64) ... $0.006" per minute; "The use of standard GitHub-hosted runners is free: * In public repositories"; "Self-hosted runners" stay free; without a payment method "usage is blocked once you consume the included compute usage."
  - https://docs.github.com/en/actions/reference/limits: "A job can be in the queue for 24 hours before it is automatically cancelled", and a GitHub-hosted job runs at most 6 hours.
  - https://code.claude.com/docs/en/github-actions: "If you authenticate with an OAuth token, runs use your Claude subscription instead of API billing."
- **Notifications from GitHub** (https://docs.github.com/en/subscriptions-and-notifications/get-started/configuring-notifications, fetched 2026-11-24):
  - The email options include "Your own updates, such as when you open, comment on, or close an issue or pull request".
  - GitHub Mobile pushes "Direct mentions * Assignments to issues or pull requests * Requests to review a pull request * Requests to approve a deployment".

### Terms: may the Max login run there

- Consumer Terms, effective Oct 8, 2025 (https://www.anthropic.com/legal/consumer-terms, fetched 2026-11-24), prohibit, "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise." They also say "You may not share your Account login information ... with anyone else."
- https://code.claude.com/docs/en/legal-and-compliance (fetched 2026-11-24):
  - "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage of Claude Code and the Agent SDK."
  - "OAuth authentication ... is designed to support ordinary use of Claude Code and other native Anthropic applications."
  - "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code".
- `claude setup-token` is documented "for CI pipelines, scripts, or other environments where interactive browser login isn't available" (authentication) and "requires a Pro, Max, Ultra, Team, or Enterprise plan".
- Support article https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan (fetched 2026-11-24): "These limits are shared across Claude and Claude Code". So a second machine adds no capacity; it shares the one limit.

## Not verified

- No page addresses an always-on server or a cron-driven Max login by name. The reading that a home Mac logged in by browser is "ordinary use of Claude Code", as this laptop is, and that a VPS on a `setup-token` token sits closer to the "automated or non-human means" line, is a judgment, not a quote.
- That the harness plugin, its hooks and `claude --bg` actually run on Linux: nothing was installed. The docs name no macOS-only part, and the grep above found only notifications, launchd and the keychain default.
- Prices not fetched or not shown: Vultr (403), MacinCloud (403/404), Ona, Coder, AWS Savings Plans and EBS, DigitalOcean Premium and regional prices, and whether Scaleway's catalog includes VAT. Also how Japanese consumption tax applies to a Hetzner bill, and the TEPCO fuel adjustment. Hetzner's AP overage unit is printed "per hour".
- The Mac mini model line: the Apple page names an M6 base model at 149,800円. Power for a Pi 5 has no official idle figure.
- Whether GitHub pushes or emails a self-mention, or a comment by the account that posted it, once "Your own updates" is turned on.
- Whether a plugin added at claude.ai/customize/plugins can come from this repository's GitHub marketplace, and whether its hooks run in a cloud session.
- The cost of each GitHub Actions option below assumes every active run-hour becomes 2-core Linux minutes. No job was run.
- gh on a headless Linux box: that it finds no credential store there and so writes the token to a plain-text file (said to be `hosts.yml` under `~/.config/gh`), rather than to a Secret Service keyring. The help text above names only "a plain text file"; nothing was installed to check.
- Whether a workflow's `GITHUB_TOKEN` can edit a user-owned Projects board. If it cannot, the Actions option needs a personal access token with `project` scope as a repository secret. Also which GitHub token a Codespace or a Daytona sandbox would carry.
- Which GitHub credential a cloud session's proxy uses; none of the quotes above involves the owner's gh token.

## Comparison at the measured volume

Volume: about 300 active run-hours a month (239 h from Nov 1 to 24, scaled), with up to 8 at once, on 166 to 308 wall-clock hours. Prices are converted at the ECB rate above and exclude VAT where the page does.

| Option | Monthly cost | What moves off the MacBook | Billing | Within ~$50? |
|---|---|---|---|---|
| Keep the MacBook awake (baseline) | $0 | nothing; the claude and gh logins stay in this Mac's keychain | nothing new | yes, but it runs only while the laptop is open on power at home |
| **Mac mini (M6, 16 GB) at home, macOS** | **≈ $27** (¥149,800 / 36 = ¥4,161, plus 4 W × 730 h × ¥40.54 = ¥118) | the bg loop for non-local tickets, the hourly dispatch and digest LaunchAgents, the five runner containers, `~/.claude/harness` (new on the mini), transcripts of its runs; a new claude browser login and a new `gh auth login` (same scopes), each in the mini's keychain | Apple, once; TEPCO; Claude on the same Max login | **yes: recommended** |
| Linux VPS, Hetzner CX33 / CX43 (EU) | ≈ $10 / $18 + VAT | as above, plus porting: launchd to systemd timers or cron, keychain to `~/.claude/.credentials.json` or a `setup-token` token, a gh token in a plain-text file (or `GH_TOKEN`) on the provider's disk, no notifications | Hetzner monthly, with a cap; Max login | yes on price; not recommended (unavailable on 2026-11-24, porting, the terms are less clear, a token on a third-party host) |
| Linux VPS, DigitalOcean or Linode 4 vCPU / 8 GB | $48 (loop only); $96 / $192 with runners | as above, gh token included | DO or Akamai monthly | loop only, at the edge; not recommended |
| Hosted Mac: Scaleway M1-M 8 GiB / M2-M 16 GiB | ≈ $82 / $126 | as for the Mac mini, with the claude and gh logins in a keychain on the provider's machine | Scaleway, 24 h minimum | no |
| Hosted Mac: MacStadium M2.S | $109 | as for the Mac mini, both logins on the provider's machine | MacStadium monthly prepaid | no |
| Hosted Mac: AWS mac2.metal | ≈ $475 + EBS | as for the Mac mini, both logins on the provider's machine | AWS, 24 h minimum | no |
| Daytona sandbox, 1 vCPU / 4 GiB, always on | ≈ $84 | the loop, ported to Linux, plus a claude token and a gh token inside the sandbox | Daytona per second | no |
| GitHub Codespaces | not possible (12 h lifetime) | (would be: the loop, ported, and a gh token in the codespace) | GitHub | no |
| Cloud sessions / scheduled tasks | $0 extra (plan usage) | the work only; no gh login moves (GitHub goes through the hosted proxy); the harness plugin, `gh issue`/`gh pr` and the tests-first gate do not come along | Max subscription; `cloud` budget marked off by the owner | yes on price; not recommended (no harness there) |
| GitHub Actions running Claude Code per ticket | $0 for Harness (public); about $96 if all ~300 h ran in private repos ($0.006 × 18,000 min − $12) | each run's state and transcript (ephemeral unless uploaded), a `CLAUDE_CODE_OAUTH_TOKEN` secret per repository; gh on the job's `GITHUB_TOKEN` (`GH_TOKEN: ${{ github.token }}`), plus a personal access token secret if the board needs one | GitHub minutes plus the Max subscription | Harness only; not recommended (no `~/.claude/harness` state, a 6 h job cap, private minutes, hosted jobs refused on 2026-11-20) |

### Each option, the five questions

- **Mac mini at home:**
  - Login: the same unmodified binary, logged in by browser like this laptop. gh: a fresh `gh auth login` on the mini with the scopes above, so its token sits in the mini's keychain; nothing is copied from this Mac.
  - Notifications: the tools notify the mini's own screen, which nobody watches.
  - Needs-input: answers stay ticket comments.
  - Private CI: moves there and stays online.
  - State and transcripts: on the mini, under the same home path, if the projects are cloned to the same paths.
  - Stays on the MacBook: needs-local, ops, interactive work.
- **Linux VPS:**
  - Login: a credentials file or a one-year `setup-token` token on a third-party host. The token "can't ... fetch claude.ai connectors". gh: a `gh auth login` there falls back to a plain-text token file when it finds no credential store (see Not verified for Linux), or a `GH_TOKEN` in the service's environment; either way a token with `repo` and `workflow` scope on a disk the provider holds.
  - Notifications: none (the bell only).
  - Needs-input: comments, as now.
  - Private CI: the runners can move (the image is Linux).
  - State and transcripts: on the VPS, and identical paths need `/Users/<name>` to be created there.
  - Stays on the MacBook: the same as for the Mac mini.
- **Hosted Mac:** the same as the Mac mini, at a monthly price, with credentials on a provider's machine: the claude login and a fresh `gh auth login`, both in that Mac's keychain.
- **Daytona (and Codespaces, were it possible):** as for the Linux VPS, with the claude token and a gh token inside the sandbox.
- **Cloud sessions and scheduled tasks:**
  - Login: native and on the subscription. gh: no login of the owner's moves; GitHub calls go through the hosted proxy, which refuses GraphQL.
  - Notifications: marker comments only.
  - Private CI: unchanged.
  - State: none of `~/.claude/harness`, and the transcripts stay at claude.ai, so cost and worklog miss them.
  - Stays on the MacBook: everything that needs the plugin.
- **GitHub Actions:**
  - Login: the documented subscription token, as a repository secret. gh: the job's `GITHUB_TOKEN`, passed as `GH_TOKEN`, scoped to that repository and job; the board's `gh project` calls may need a personal access token with `project` scope as a further secret (not verified).
  - State and transcripts: lost after the job unless uploaded.
- **Keeping the MacBook awake:** nothing changes and both logins stay in this Mac's keychain, but nothing runs while the lid is closed or the laptop travels.

### Credentials: moving needs-local and ops tickets

To move them, the always-on machine would need:
- jotform-glow-sync's `.env` (`JOTFORM_API_KEY`, `GLOW_SHEET_ID`, `GLOW_SA_KEY`) and `credentials/` (`config.json`, `service-account.json`, a Google service-account key).
- A fresh Salesforce CLI login there (`~/.sf`), rather than a copy of its tokens.
- The keychain item `harness-anthropic`, only if headless dispatch is ever turned back on.
- The other projects' local files behind their needs-local tickets (HorseTrackBetting's 36 are the most).
- The claude.ai connectors (Salesforce) follow the browser login, but not a `setup-token` token.

The risks:
- A second copy of a client's credentials doubles what can leak.
- On a VPS or a hosted Mac a third party holds the disk.
- Ops tickets write live records, so their before-values (`~/.claude/harness/ops/`) would live on the machine that wrote them.
- jotform-glow-sync is billable, so its hours would need the transcripts brought back.

The recommendation keeps all of it on the MacBook. The mini runs only tickets without `needs-local` or `ops`.

## Recommendation

Put an always-on Mac mini (base M6, 16 GB) at home as a second dispatch machine. It costs about $27 a month over 36 months, power included. It is the only option under about $50 that runs the harness unchanged, because it is the same OS, so launchd, the keychain, notifications, Docker Desktop and the plugin all work as they do now. The login is the browser login, as on this laptop, which reads as the ordinary use the legal page describes (a judgment, not a quote; see Not verified). gh gets its own `gh auth login` into the mini's keychain. Its hourly `harness-work dispatch --all-projects --max 8` LaunchAgent takes the tickets that need nothing local. The MacBook keeps needs-local and ops tickets and interactive work.

Tradeoffs:
- **Cost:** ¥149,800 is paid up front, where a Hetzner VPS costs a third as much per month. The VPS was unavailable on 2026-11-24, needs Linux porting, and would hold a long-lived subscription token on a third-party host.
- **No new capacity:** the one Max usage limit is shared, so a second machine adds hours in the day, not tokens. The usage-limit hold (#164) applies on each machine separately, through its own `budgets.json`.
- **Two machines on one board:** the MacBook does not see the mini's run records, so `harness-work runs`, standup and digest on the MacBook miss them. Both machines can dispatch the same ready ticket within the same minute; `start` marks it in progress at once, which narrows but does not close the race.
- **Bg dispatch takes needs-local tickets today**, so the mini needs a way to skip them before its LaunchAgent is installed (follow-up #444).

What changes under it:
- **Notifications:** `notify()` fires on the mini, where nobody sees it. Two choices, which are the owner's to make:
  - Turn on GitHub's email option "Your own updates", so the needs-input, park and ready comments the harness posts as the owner arrive by email. It costs nothing, needs no new service and uses GitHub's own channel, but it is unverified for comments made through the API.
  - Add a push channel. That is an outside message, which the owner approves first, so no ticket is filed for it.
  - Until then, `/harness:standup` and the digest from the MacBook show needs-input and PRs to review across the board.
- **Needs-input answers:** still a comment on the ticket. The mini's hourly dispatch does not resume an answered ticket (by design since #367: only `dispatch --only N` does), so an answer is picked up when the owner runs `harness-work dispatch --only N` on the mini (over ssh) or `/harness:work N` on the MacBook.
- **Private CI:** the five runner containers move to the mini (`harness-runner remove` here, `add` there; each `add` asks GitHub for its own registration token, so no secret is copied). Pull requests stop queueing while the laptop is off.
- **Cost and worklog counts:**
  - Clone each project on the mini at the same absolute path (the same user name), so `repo-key` and the `~/.claude/projects/` folder names match.
  - Before `harness-worklog update`, `/harness:invoice` or `harness-work cost N` on the MacBook, copy the mini's `~/.claude/projects/` into the MacBook's, adding files only and deleting nothing. Session files are named by session id, so the folders merge.
  - Or run cost and worklog on the mini for the tickets it ran.
  - Billable projects, jotform-glow-sync above all, keep their needs-local tickets on the MacBook, so most billable hours stay where they are counted today.

Not recommended:
- Every hosted Mac ($82 to $475).
- Daytona ($84).
- Codespaces (impossible).
- DigitalOcean and Linode with the runners ($96+).
- Cloud sessions and scheduled tasks, as the loop's home: they carry no harness. They remain fine for `harness-work cloud N` workers, and per the current docs they count toward the plan limit with "no separate charge for the cloud VM". The `cloud` budget the owner marked off on 2026-11-20 is his to reset.
- GitHub Actions.

If the owner does not want to buy a machine, the zero-cost baseline is the MacBook on power with the lid open, or closed with an external display, and the hourly dispatch LaunchAgent installed.

## Proposed follow-ups

- #444 Let bg dispatch skip needs-local and ops tickets on a machine that cannot run them (needed before any second dispatch machine).
- #445 Write the second-machine runbook: the same paths, the login, the LaunchAgents, moving the runners, bringing transcripts back for worklog and cost (docs only; to promote only if the owner decides to buy).
