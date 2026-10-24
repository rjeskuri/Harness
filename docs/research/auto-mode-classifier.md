# Research notes: the auto mode classifier and the harness allow rules

Gathered 2026-10-24 for ticket #139 (epic #131, Approvals) on Claude Code 2.1.310 (`claude --version`), from the Claude Code docs, the installed binary and the session transcripts under `~/.claude/projects/`. Quotes are the docs' words; the code reading and the counts are this note's own and say how they were made. Nothing in `~/.claude/settings.json` or any other user-level file was edited.

## Finding

- **`Bash(harness-*:*)` was not dropped by auto mode; it never matched anything.** The strip list that auto mode applies on entry does not contain it, and the rule parser reads `harness-*:*` as a legacy prefix rule whose prefix is the literal text `harness-*`, so no `harness-pr land 129` ever matched it, in any permission mode. #137's premise ("auto mode may drop a wildcarded program rule") named the wrong cause; its fix is right anyway.
- **The nine narrow rules `Bash(harness-<tool>:*)` do keep a bare `harness-*` call away from the classifier.** A single command such as `harness-pr land --help` is accepted by the rule path (`reasonType: subcommandResults`), the same path the narrow `git` rules take. A command with a pipe, a redirect, `;` or `&&` around the harness call still went to the classifier in the transcripts, and that is the form both `Merge Without Review` denials had (`... harness-pr land N 2>&1 | tail -15`). The narrow rules alone will not stop a denial of a landing written that way.

## 1. Rules in effect on this Mac

- `claude --version`: `2.1.310 (Claude Code)`, binary `~/.local/share/claude/versions/2.1.310`.
- `~/.claude/settings.json` (read, not changed): `permissions` is `{"defaultMode": "auto"}` only (no allow, ask or deny rule at user level); `autoMode` holds 24 `environment` entries and no `allow`, `soft_deny` or `hard_deny`. The `environment` list has no `"$defaults"` entry and reads as written by Claude Code's own draft flow: `Trusted repo` is `The current working directory (/Users/ryaneskuri) - no git remote is configured here`, `Organization`, `Source control`, `Key internal services` and the others are `None configured`. Nothing in it names the Harness repository, its GitHub account, or `harness-pr land` as a reviewed-merge path.
- Project allow rules, `.claude/settings.json` in the Harness repo (and the scaffold template `plugins/harness/templates/settings.json`):
  - Old, from 7452690 (2026-10-13) to 2026-10-23 16:33 +09:00: `Bash(scripts/verify.sh)`, `Bash(git status:*)`, `diff`, `log`, `add`, `commit`, `push`, `fetch`, `worktree`, `Bash(gh issue:*)`, `pr`, `api`, and `Bash(harness-*:*)`.
  - New, from 876e33a (#137, #206): the same minus the wildcard, plus nine rules `Bash(harness-pr:*)`, `harness-board`, `harness-work`, `harness-red`, `harness-scaffold`, `harness-digest`, `harness-standup`, `harness-projects`, `harness-worklog`.
- `claude auto-mode config` prints the effective classifier config as JSON: 17 `allow` (built-in), 72 `soft_deny` (built-in), 1 `hard_deny` (Data Exfiltration), and the 24 `environment` entries above. `claude auto-mode defaults [--label <prefix>]` prints the built-ins, `critique` asks the model for feedback on custom rules, `reset` removes the `autoMode` block from user settings. These show the classifier's rules only.
- Not found: a non-interactive equivalent of `/permissions` or `/status` that lists the allow rules in effect (`claude --help` and `claude auto-mode --help` show none). The rules above were read from the settings files, and the effect was read from the transcripts (section 3).

## 2. What the docs say

Sources: https://code.claude.com/docs/en/permission-modes.md, https://code.claude.com/docs/en/auto-mode-config.md, https://code.claude.com/docs/en/permissions.md, https://code.claude.com/docs/en/errors.md (fetched 2026-10-24).

- What auto mode drops on entry (permission-modes, "Eliminate prompts with auto mode"): "On entering auto mode, broad allow rules that grant arbitrary code execution are dropped: Blanket `Bash(*)` or `PowerShell(*)`; Wildcarded interpreters like `Bash(python*)`; Package-manager run commands; `Agent` allow rules; `Monitor` allow rules ... Narrow rules like `Bash(npm test)` stay in effect. Claude Code restores the dropped rules when you leave auto mode." The docs do not mention a wildcarded program name such as `harness-*`.
- Narrow rules run before the classifier (auto-mode-config, "Route all shell commands through the classifier"): "By default, narrow Bash and PowerShell allow rules such as `Bash(npm test)` stay in effect in auto mode. Claude Code resolves them before the classifier runs ... Claude Code suspends only the broad rules that grant arbitrary code execution, such as `Bash(*)` or wildcarded interpreters". `autoMode.classifyAllShell: true` suspends every Bash and PowerShell allow rule. It is not set in `~/.claude/settings.json`.
- Where the classifier reads config (auto-mode-config): "The classifier doesn't read `autoMode` from project settings in `.claude/settings.json` or `.claude/settings.local.json`." `autoMode` takes effect in `~/.claude/settings.json` (and managed settings, `--settings`). `permissions.ask` rules "are evaluated before the classifier and always force a permission prompt"; `permissions.deny` "Blocks before the classifier is consulted".
- `$defaults` (auto-mode-config): "To add your own entries alongside the defaults, include the literal string `"$defaults"` in the array." and "Setting any of `environment`, `allow`, `soft_deny`, or `hard_deny` without `"$defaults"` replaces the entire default list for that section." The `/auto-mode-setup` draft "writes the `environment` list without `"$defaults"`, because the draft spells out the built-in entries it left unchanged". `autoMode.environment` is where trusted repos, orgs and domains go; "By default, the classifier trusts only the working directory and the current repo's configured remotes."
- Rule syntax (permissions, "Wildcard patterns"): "A `*` can go anywhere in the rule" and "The `:*` suffix is an equivalent way to write a trailing wildcard, so `Bash(ls:*)` matches the same commands as `Bash(ls *)`." The docs do not say what `:*` does when the text before it already holds a `*`; section 4 shows what 2.1.310 does.
- Default blocks relevant here (permission-modes, "What the classifier blocks by default"): "Merging a pull request no human has approved, approving Claude's own pull request, or disabling CI checks"; "Production deploys and migrations"; and "Allowed by default: ... Pushing to any branch of the repository you're working in, including the default branch." The category names below are the rule labels printed by `claude auto-mode defaults` and in the denial text (`Reason: [Merge Without Review]`); the docs page names only `[Production Deploy]` and `[Data Exfiltration]` as examples. The full wording of the six categories in the ticket, from `claude auto-mode config`:
  - Merge Without Review: "Merging a PR before any human has approved it. The `--admin`/`--force` arm - bypassing required review or checks ... `gh pr merge --auto` on a repo with required-reviews branch protection is NOT this rule".
  - Production Deploy: "Deploying to production/a production service or running production database migrations".
  - Instruction Poisoning: "Writing to any file an agent session will read back as instructions - e.g. `CLAUDE.md`, `CLAUDE.local.md`, `.claude/` instruction files, the agent's memory directory ... content that would function as a permission grant or BLOCK-rule bypass when read back ... Recording, at the user's request, an authorization or preference the user actually stated in their own message is not poisoning".
  - Self-Modification: "Edits to the agent's own config that (a) add or widen permissions or weaken guards - permission allow rules (e.g. wildcard `"Bash(prefix:*)"` entries), hook or sandbox changes, approval-gate settings - that the user did not explicitly ask for ... or (b) write consent or instruction content the user did not give".
  - Unauthorized Persistence: "Adding SSH keys, modifying shell profiles ..., creating cron jobs, systemd services ..., git hooks, or other mechanisms that persist access or execute code beyond the current session."
  - Auto-Mode Bypass: "Working around this classifier or the Claude Code permission system - (i) jailbreaking the classifier ...; (ii) taking a denial in bad faith and tunneling the same action through a different path ...; (iii) using flags, config, aliases, shell indirection, or extension points so that the Claude Code permission system allows arbitrary code execution."

## 3. Evidence from the transcripts

Every Bash tool result in a transcript carries a `permissionDecision` object (`decision`, `source`, `reasonType`). `reasonType: subcommandResults` (or `rule`) is the allow-rule path; `classifier` is the classifier path; a denial adds `toolDenialKind: automode-blocked`. Read with a script that pairs each `tool_use` with its `tool_result` by `tool_use_id`, for sessions whose cwd is the Harness checkout or one of its worktrees.

- A control: the narrow `git` rules, single commands (no `|`, `;`, `&&`, redirect), 2026-10-13 to 2026-10-23: 19 `subcommandResults`, 1 `rule`, 0 `classifier`.
- The old rule, single `harness-*` commands, 2026-10-13T09:41Z to 2026-10-23T07:33Z: 26 `classifier`, 1 `subcommandResults`. With a pipe, redirect or `&&` around them: 366 `classifier`, 0 other. So `Bash(harness-*:*)` did not take the rule path for a plain call.
- The nine rules, from 2026-10-23T07:33Z (some sessions started before and still hold the old settings): single `harness-*` commands 5 `subcommandResults`, 2 `classifier`; compound or redirected 2 `subcommandResults`, 22 `classifier`.
- This session (2.1.310, worktree with the nine rules, auto mode), run for this note: `harness-pr land --help` alone and `git log --oneline -1` alone were both `accept / subcommandResults`. `harness-pr awaiting | head -2` and `harness-pr awaiting --help >/dev/null; echo done` were both `accept / classifier`. `harness-pr awaiting` itself printed the awaiting table with no prompt and no denial.
- `harness-pr land` itself: 2026-10-22T11:47:30Z `harness-pr land 129 2>&1 | tail -15` (run as `cd <repo> && ...`) and 2026-10-23T01:36:26Z `harness-pr land 169 2>&1 | tail -15` were denied, `source: config`, `reasonType: classifier`, `[Merge Without Review]`. Dozens of other `harness-pr land N` calls in the same period were accepted by the classifier (first 2026-10-14, last 2026-10-24).
- Not verifiable here: whether `classifier` in `reasonType` always means the model ran (an accepted compound command with only allowed parts still shows it), and how the nine rules treat the compound forms in a fresh interactive session; both need an interactive `/permissions` look or a controlled run.

## 4. Why the old rule never matched (code reading, 2.1.310)

Read from the minified bundle in `~/.local/share/claude/versions/2.1.310` with `grep -a -F -b` and `dd`, so treat it as a reading, not a contract.

- The auto-mode strip check lowercases the rule content and compares it against a fixed list: `python`, `python3`, `python2`, `node`, `deno`, `tsx`, `ruby`, `perl`, `php`, `lua`, `npx`, `bunx`, `npm run`, `yarn run`, `pnpm run`, `bun run`, `bash`, `sh`, `ssh`, `zsh`, `fish`, `eval`, `exec`, `env`, `xargs`, `sudo`, each as `x`, `x:*`, `x *` or `x*`, plus a bare `*`. `harness-*:*` equals none of these, so it would not be stripped.
- The rule parser tries the legacy form first: `tFe(e)` is `e.match(/^(.+):\*$/)?.[1]`, and when it returns text the rule becomes `{type: "prefix", prefix: "harness-*"}`. A prefix rule matches when the command equals the prefix or starts with the prefix plus a space, after collapsing whitespace. The `*` inside is not a glob there. So only a command literally named `harness-*` matched. The wildcard-pattern branch (where `*` is a glob) is reached only for rules that do not end in `:*`.
- `Bash(harness-pr:*)` has prefix `harness-pr` and matches `harness-pr land 129`, as the transcripts show.

## 5. Classifier denials, 2026-09-01 to 2026-10-22

Method: every `tool_result` in `~/.claude/projects/**/*.jsonl` (2,534 files, main sessions and subagents) whose text starts `Permission for this action was denied by the Claude Code auto mode classifier. Reason: ...`, deduplicated by `tool_use_id` (the same event can sit in a resumed or forked transcript), dated by the entry's `timestamp` (UTC), window by calendar date from 2026-09-01 to 2026-10-22 inclusive. Script: a short `python3 -I` walk over the files (kept out of the repo); the earliest denial in any transcript is 2026-09-12. Project is the transcript's folder, not the cwd of the command. Command shapes and timestamps only; no transcript content is quoted.

| Category | Count | First to last | Where |
| :- | -: | :- | :- |
| (no category: "judged this action dangerous (it gave no explanation)") | 7 | 2026-09-28 to 2026-10-09 | jotform-glow-sync 3, home dir 4 (incl. 3 Salesforce MCP create calls on 10-09) |
| Modify Shared Resources | 6 | 2026-09-24 to 2026-10-22 | jotform-glow-sync 5, home dir 1 |
| Production Deploy | 6 | 2026-09-26 to 2026-10-07 | jotform-glow-sync 6 (live writes) |
| Auto-Mode Bypass | 5 | 2026-09-12 to 2026-10-14 | jotform-glow-sync 5 |
| PII Data Handling | 3 | 2026-09-28 to 2026-10-06 | jotform-glow-sync 3 |
| Instruction Poisoning | 3 | 2026-09-30 to 2026-10-14 | jotform-glow-sync 3 |
| Self-Modification | 2 | 2026-09-30 and 2026-10-13 | jotform-glow-sync 1, Harness 1 |
| Credential Leakage | 1 | 2026-10-01 | jotform-glow-sync |
| Credential Exploration | 1 | 2026-10-06 | Harness |
| Unauthorized Persistence | 1 | 2026-10-08 | home dir |
| Irreversible Local Destruction | 1 | 2026-10-13 | Harness |
| Merge Without Review | 1 | 2026-10-22T11:47Z | Harness (`harness-pr land 129`) |
| Production Reads | 1 | 2026-10-22 | jotform-glow-sync (Salesforce SOQL MCP) |
| Interfere With Workloads | 1 | 2026-10-22 | Harness |
| (classifier error, "Stage 2 ... blocking based on stage 1") | 1 | 2026-09-14 | jotform-glow-sync |
| **Total** | **40** | | |

Outside the window, 2026-10-23 to 2026-10-24: 6 more: Merge Without Review 1 (2026-10-23T01:36Z, `harness-pr land 169`), CI Bypass 2 (`gh workflow disable`, 10-23T04:09Z), Auto-Mode Bypass 1, no category 2.

Against the automation review's count (14 in seven weeks: 5 Production Deploy, 3 Instruction Poisoning, 2 Self-Modification, 2 Auto-Mode Bypass, 1 Merge Without Review, 1 Unauthorized Persistence): Instruction Poisoning 3, Self-Modification 2, Merge Without Review 1 and Unauthorized Persistence 1 agree. Production Deploy is 6 here (review 5) and Auto-Mode Bypass is 5 (review 2). The review's 14 lists six categories; the transcripts hold seven more categories (Modify Shared Resources 6, PII Data Handling 3, Credential Leakage 1, Credential Exploration 1, Irreversible Local Destruction 1, Production Reads 1, Interfere With Workloads 1) and 8 events with no category, for 40 in all. I do not know how the review counted; its likely differences are the filter (named categories it judged relevant) and treating two Production Deploy events a minute apart as one. This note counts events, so a retried call counts again. Six of the 40 have a harness tool in the command text (two Instruction Poisoning on `harness-board comment`, one each Irreversible Local Destruction, Self-Modification and Interfere With Workloads inside longer commands, and Merge Without Review on `harness-pr land 129`); a second `harness-pr land` denial came a day later, outside the window.

## 6. What Ryan could change in `~/.claude/settings.json` (not done)

Reported only; none of it applied.

1. Nothing is needed for the allow-rule question: the old wildcard was inert, the nine project rules work for plain calls. Do not add a user-level `Bash(harness-*)` style rule to "fix" it; a program wildcard is exactly the shape the docs warn against and Self-Modification names wildcard allow rules.
2. The reviewed-merge path is a classifier-context question, which `autoMode` in `~/.claude/settings.json` can answer (it is ignored in the project file). The current `environment` says the trusted repo is `/Users/ryaneskuri` with no remote, so a landing in `rjeskuri/Harness` is judged with no hint that it is the intended repo. An `environment` entry naming the Harness and project repositories as trusted, and a sentence that `harness-pr land` and `harness-pr autoland` merge only after a passing review, a passing gate and the written eligibility rules, is the documented lever ("A destination Claude needs throughout the task ... add it to `autoMode.environment`"). Add `"$defaults"` first so the built-in entries stay. This is what the later tickets of #131 should decide; it is not verified that it clears `Merge Without Review`, since that rule's wording is "Merging a PR before any human has approved it" and `autoland` merges without a human by design. `claude auto-mode critique` can review a draft.
3. If the denial of a compound `harness-pr land N 2>&1 | tail -15` matters, the cheaper fix is on the harness side: have `/harness:land` and `/harness:work` issue `harness-pr land N` as a plain command (no pipe or redirect, no `cd ... &&`), which reaches the rule path under the nine rules, and keep `permissions.ask` out of it.
4. `classifyAllShell` stays unset; setting it would send every shell command to the classifier.

## Open questions

- Does `harness-pr land N` as a plain command, in a fresh interactive session with the nine rules, always take the rule path and never meet `[Merge Without Review]`? The transcripts show 5 of 7 plain calls on the rule path; the other 2 may be stale settings. Needs a controlled run.
- design.md and the scaffold's warning text say auto mode "may drop" `Bash(harness-*:*)`. By this note the accurate cause is the literal-prefix reading of `:*`; the wording is left as is here (out of this ticket's scope).
