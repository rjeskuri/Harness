# Research notes: model routing

Gathered 2026-10-06 by an Opus research agent from the official Claude Code docs, the changelog and the installed binary. Quotes are the sources' words; judgments are the agent's and were read by the harness designer before use.

## Findings

# Which model runs which work in Claude Code (checked 2026-10-06)

I pulled the raw docs with curl and copies are in `/private/tmp/claude-501/-Users-ryaneskuri-portfolio-projects-Harness/3156a81b-9c44-4695-be43-fa1ce35e5f57/scratchpad/docs/`. Markdown links are stripped from the quotes below.

## 0. Local environment (read-only checks)
- **Only Claude Code install found:** the binary inside the VS Code extension, `~/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude`. Running `--version` prints `2.1.289 (Claude Code)`.
  - `zsh -lic 'which claude'` prints `claude not found`.
  - There is no `~/.claude/local`, and no Claude app in `/Applications` or `~/Applications`.
  - The GitHub CHANGELOG's newest entry is `## 2.1.291`.
  - 2.1.289 supports all three target models: Opus 5.5 needs ≥2.1.280, Sonnet 5.5 ≥2.1.284, Fable 5.1 ≥2.1.257.
- **There is no `cli.js`.** The bundle is a Mach-O arm64 binary with JavaScript embedded in it, so I grepped the binary itself.
- **Current `~/.claude/settings.json`:**
  - `"model": "claude-fable-5-1"`
  - top-level `"effortLevel": "xhigh"`
  - `modelSettings` sets `xhigh` for `claude-fable-5-1`, `claude-opus-5` and `claude-opus-5-5`
  - no `env` block, and no `~/.claude/agents/`
- **What that means today:**
  - The main session runs Fable 5.1 at `xhigh`.
  - Every subagent without its own `model` inherits Fable 5.1.
  - Sonnet 5.5 has no saved level, so it runs at its default, `medium`.
  - Nothing enforces "Opus 5.5 is the workhorse."
- **Kit:** `project/.claude/agents/reviewer.md` has `model: inherit`, so it runs on whatever model the session runs. No kit skill sets `model` or `effort`.

## 1. Main-session model: `model` setting, `/model`, `--model`, `ANTHROPIC_MODEL`
- **Aliases** (model-config): `default` ("Not itself a model alias"), `best`, `fable`, `sonnet`, `opus`, `haiku`, `sonnet[1m]`, `opus[1m]`, `opusplan`.
  - `fable`: "Uses the Fable model for your provider for your hardest and longest-running tasks".
  - "Unless you set `ANTHROPIC_DEFAULT_FABLE_MODEL`, the `fable` alias resolves to Fable 5.1".
  - On the Anthropic API, `opus` is Opus 5.5 and `sonnet` is Sonnet 5.5.
  - "Aliases point to the recommended version for your provider and update over time. To pin to a specific version, use the full model name, for example `claude-opus-5-5`".
- **Priority order** (model-config):
  1. "During session: use `/model <alias|name>`"
  2. "At startup: launch with `claude --model <alias|name>`"
  3. "Environment variable: set `ANTHROPIC_MODEL=<alias|name>`"
  4. "Settings: ... the `model` field"
  5. "Default for new sessions: set `ANTHROPIC_DEFAULT_MODEL`"
- **`/model` saves your choice:** "`/model` saves your choice as the default for new sessions by writing the `model` field in your user settings." In the picker, "`s`: switch model for this session only".
- **`ANTHROPIC_MODEL` still wins on the next launch:** "With `ANTHROPIC_MODEL` set instead, Claude Code returns to that variable's model on the next launch, whatever you saved with `/model`."
- **Per-terminal models:** "The `--model` flag and `ANTHROPIC_MODEL` environment variable apply only to the session you launch with them. To run different models in different terminals at the same time, launch each one with its own `--model` flag".
- **Settings key** (settings-reference `model`): "Type: string, a model alias or full model ID". File precedence (settings) is managed > command line > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`. Also: "`ANTHROPIC_MODEL` exported in your shell applies over the `model` key from any file". So a project file can set a per-project default model.
- **`/model` reaches subagents:** "If you switch models with `/model`, the switch also reaches subagents that inherit the main conversation's model ... To keep a custom subagent on a smaller model, set `model` in its definition."
- **Desktop app:** "Model: pick a model from the dropdown next to the send button" (Cmd+Shift+I opens the model menu, Cmd+Shift+E the effort menu). "Desktop and CLI read the same configuration files". You can add `env` vars to `~/.claude/settings.json`, "though these reach Claude sessions only and not dev servers."
- **`opusplan`:** "In plan mode: uses `opus` ... In execution mode: automatically switches to `sonnet`". There is no Fable equivalent (binary alias list in §9).
- **Fable billing:**
  - "Depending on your plan and seat tier, Fable usage can bill to usage credits instead of drawing on your plan's included limits."
  - "In non-interactive mode with the `-p` flag ... When a Fable request there would bill to usage credits, Claude Code bills it without asking."

## 2. Effort: `effortLevel`, `modelSettings`, `/effort`, `--effort`, frontmatter
- **Levels by model** (model-config):
  - Fable 5.1 and Fable 5: `low`, `medium`, `high`, `xhigh`, `max`
  - Opus 5.5, Sonnet 5.5, Opus 5, Sonnet 5, Opus 4.8 and Opus 4.7: `low`, `medium`, `high`, `xhigh`, `max`
  - Haiku 4.5: effort "Not supported" (models overview)
- **Defaults in Claude Code:** "`high` on every model that supports effort, except that Opus 5.5 and Sonnet 5.5 default to `medium`". So Fable 5.1 is `high`, and Opus 5.5 and Sonnet 5.5 are `medium`.
  - Note: the API default for Sonnet 5.5 is `high` ("`high` is the default on the Claude API"), so Claude Code and the raw API differ.
- **What effort changes:**
  - "Lower effort is faster and cheaper for straightforward tasks, while higher effort provides deeper reasoning".
  - From the platform effort page: "The effort parameter affects all tokens in the response, including: Text responses and explanations, Tool calls and function arguments, Thinking".
  - Per level: `xhigh` is "Deeper reasoning at higher token spend". `max` "may show diminishing returns and is prone to overthinking".
  - `low` on the API: "Simpler tasks that need the best speed and lowest costs, such as subagents".
  - "The effort scale is calibrated per model".
  - "Opus 5.5 at `medium` matches or exceeds Opus 5 at `high`".
  - "You can't turn thinking off on Opus 5.5, Sonnet 5.5, or the Fable models", and "You are charged for all thinking tokens".
- **Resolution order:**
  1. `CLAUDE_CODE_EFFORT_LEVEL`, `--effort` or `/effort`
  2. Settings: a per-model `modelSettings` level or `effortLevel`
  3. The model's default
  - Frontmatter: "Frontmatter effort applies when that skill or subagent is active, overriding the session level but not the environment variable."
- **`modelSettings`** (settings-reference): "Save an effort level for each model you use. Requires Claude Code v2.1.251 or later."
  - Fields: `effortLevel`, `maxEffortLevel`, `autoCompactWindow` (v2.1.288+).
  - "Claude Code writes each entry under the model's canonical name, such as `claude-opus-5-5`".
- **Gotcha with top-level `effortLevel`** in user settings: it "keeps applying where it applied before, on Opus 5, Fable 5.1, and earlier models. Opus 5.5 and models released after it ignore it". In project, local or managed settings it applies to every model.
- **`max` is not a saved setting:** "`max` isn't accepted as a level in either key". The bundled schema confirms the `effortLevel` enum is `["low","medium","high","xhigh"]`.
  - "Unless you set it through the `CLAUDE_CODE_EFFORT_LEVEL` environment variable, Claude Code applies `max` to the current session only."
  - Subagent and skill `effort` frontmatter does accept `max`.
- **Cache:** "On Opus 5.5, Sonnet 5.5, and Fable 5.1 with an API key or a Claude subscription, changing effort keeps the cache".
- **Official guidance** (blog, July 7, 2026, via WebFetch): "for most tasks you should use the model's default effort level."

## 3. Fast mode (`/fast`)
- "Fast mode is a high-speed configuration for Claude Opus, making the model up to 2.5x faster at a higher cost per token." "Fast mode is not a different model."
- "Fast mode is supported on Opus 5.5, Opus 5, and Opus 4.8. It is not available on Sonnet, Haiku, or other models." Turning it on from Fable switches the session to Opus.
- **Price:** "\$8/\$40 on Opus 5.5", which is 2x Opus 5.5's standard \$4/\$20. claude.com/pricing: "up to 2.5x faster speeds with fast mode for Opus 5.5 at 2x standard pricing."
- **On subscriptions:** "fast mode is available via usage credits only and not included in the subscription rate limits."
- **First use is expensive:** "The first time you enable fast mode in a conversation, you pay the full fast mode uncached input token price for the entire conversation context."
- It is a research preview.

## 4. Subagents (`.claude/agents`, plugins, `--agents`, the Agent tool)
- **Frontmatter `model`** (sub-agents): "`sonnet`, `opus`, `haiku`, `fable`, a full model ID such as `claude-opus-5-5`, or `inherit`. When you omit it, Claude Code picks the model in the subagent model order".
  - Full IDs "Accepts the same values as the `--model` flag".
  - **`fable` is accepted.**
- **Order when picking a subagent's model:**
  1. "The per-invocation `model` parameter"
  2. "The subagent definition's `model` frontmatter, where `inherit` selects the main conversation's model"
  3. "`CLAUDE_CODE_SUBAGENT_MODEL` ... when you set it"
  4. "The main conversation's model"
  - So if you omit `model` and don't set the variable, the subagent effectively inherits.
- **Family-alias rule:** a family alias "resolves to the main conversation's model instead of the version the alias points to" when "The main conversation's model belongs to that family".
- **Agent tool `model` parameter (binary):** `model:j(["sonnet","opus","haiku","fable"]).optional().describe("Optional model override for this agent. Takes precedence over the agent definition's model frontmatter and the configured default subagent model. ... Ignored for subagent_type: \"fork\" — forks always inherit the parent model.")`.
  - The hooks reference agrees: "`model` | string | `"sonnet"` | Optional model alias to override the default".
  - **It accepts aliases only, not full IDs.**
  - It persists when the subagent resumes (v2.1.211+). The changelog has "Restored the `model` parameter on the Agent tool" at 2.1.72.
- **`effort` frontmatter:** "Overrides the session effort level. Default: inherits from session."
  - The binary applies `effort` only when the frontmatter sets it.
  - For a subagent on a different model, it isn't documented whether the inherited level re-resolves against that model (see open questions).
- **Built-in subagents:**
  - Explore: when the main session is Fable on a subscription or Console account, "Explore runs on the Opus model that the `opus` alias resolves to".
  - Plan: "inherits from the main conversation".
  - General-purpose: `CLAUDE_CODE_SUBAGENT_MODEL` if set, otherwise the main model.
  - statusline-setup: Sonnet. claude-code-guide: Haiku.
  - "Setting `CLAUDE_CODE_SUBAGENT_MODEL` by itself doesn't change the model the built-in Explore and Plan subagents run on."
- **Forks:** "Model | Same as main session" for a fork. "Claude Code turns fork mode on by default in interactive sessions."
  - To block forks: "deny the `fork` subagent type with an `Agent(fork)` rule."
  - **Gotcha:** forks spawned by a Fable orchestrator run on Fable.
- **`CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`** (v2.1.257+): "Claude Code ignores the `model` field in subagent definitions, and Claude can't pass a model". It forces one model on everything, so it cannot express three tiers.
- **Plugin agents:** "Supported fields: `name`, `description`, `model`, `effort`, ..." Plugin agents ignore `hooks`, `mcpServers` and `permissionMode`. `--agents` JSON also accepts `model` and `effort`.
- **Teammates** use spawn-prompt model > definition `model` > `CLAUDE_CODE_SUBAGENT_MODEL` > the lead's model. Workflow agents follow the subagent order: "A model the script names for a stage counts as the per-invocation model".
- **Checking:** `/tasks` shows each subagent's model. PostToolUse on `Agent` reports `resolvedModel`.
- **Main-thread agent:** `--agent <name>` or the `agent` setting runs the main thread with that agent's model and tools. But "a custom subagent's system prompt replaces the default Claude Code system prompt entirely". The `tools: Agent(worker, researcher)` allowlist only works in that mode.

## 5. Skills can pin a model
- Skills frontmatter `model`: "Model to use when this skill is active. The override applies for the rest of the current turn and isn't saved to settings. The session model resumes when you send your next prompt. Accepts the same values as `/model`, or `inherit` ... With `context: fork`, the value sets the forked subagent's model instead."
- A skill's `effort` frontmatter is supported too.
- `agent:` picks the subagent type for `context: fork`: "If omitted, uses `general-purpose`."
- **Cost gotcha** (prompt-caching): "When a skill or command's frontmatter names a `model` other than the session's current model, that turn is also a model switch: the next request reads the entire conversation history with no cache hits."
- **Implication for the kit (my inference):** a multi-turn interview skill like `/spec` cannot stay on Fable just by setting `model: fable`. It reverts after the first turn. Run that session on Fable instead.

## 6. Environment variables (env-vars and model-config)
- `ANTHROPIC_MODEL`: "Name of the model setting to use".
- `ANTHROPIC_DEFAULT_MODEL` (v2.1.236+): "Model that new sessions start on by default".
- `ANTHROPIC_DEFAULT_FABLE_MODEL`, `_OPUS_MODEL`, `_SONNET_MODEL`, `_HAIKU_MODEL`: the model each alias resolves to.
  - Haiku is "also used for background functionality".
  - The binary maps `{sonnet:ANTHROPIC_DEFAULT_SONNET_MODEL, opus:..._OPUS_..., haiku:..._HAIKU_..., fable:..._FABLE_...}`.
- `CLAUDE_CODE_SUBAGENT_MODEL`: "The default model for subagents, agent team teammates, and workflow agents that aren't assigned a model another way. Accepts an alias such as `haiku` or a full model name. Two sources take precedence over it: a model Claude passes when it spawns the agent, and a `model` field in the agent's definition, including `inherit`."
- `CLAUDE_CODE_EFFORT_LEVEL`: "Takes precedence over `--effort`, `/effort`, and the `modelSettings` and `effortLevel` settings."
- **`ANTHROPIC_SMALL_FAST_MODEL` is deprecated:** "`ANTHROPIC_SMALL_FAST_MODEL` is deprecated in favor of `ANTHROPIC_DEFAULT_HAIKU_MODEL`."
- **The `[1m]` suffix:**
  - "On the Anthropic API, Fable 5.1, Fable 5, Sonnet 5 and later, and Opus 4.7 and later run with the 1M window on every plan, including Pro. You don't select a `[1m]` variant".
  - `sonnet[1m]` has "No effect when `sonnet` already resolves to Sonnet 5.5".
  - "Claude Code strips the suffix before sending the model ID to your provider."
  - "The 1M context window uses standard model pricing with no premium for tokens beyond 200K."
  - The suffix matters for Opus 4.6, Sonnet 4.6 and third-party pins only.

## 7. Advisor tool: an option for "Fable makes the decisions"
- "The advisor tool lets Claude consult a second, typically stronger model at key moments during a task, such as before committing to an approach, when stuck on a recurring error, or before declaring a task complete."
- Accepted pairing: "Opus 5 or Opus 5.5 | Fable, Opus 5 or later".
- Set it with `/advisor fable`, `"advisorModel": "fable"`, or `--advisor`.
- "Claude calls the advisor at decision points rather than on every turn, so pairing a faster main model with a stronger advisor typically costs less than running the stronger model throughout."
- Limits:
  - It is experimental and needs the Anthropic API plus feature-flag fetching.
  - "There is no setting to cap or force advisor calls".
  - A Fable advisor needs the one-time `/model fable` usage-credits consent on some plans.
  - "Claude Code doesn't apply an Opus or Sonnet advisor to a Fable main model".

## 8. Pricing (platform.claude.com/docs/en/about-claude/pricing; docs.claude.com redirects there)

| Model (ID) | Input | 5m cache write | 1h cache write | Cache read | Output |
|---|---|---|---|---|---|
| Fable 5.1 (`claude-fable-5-1`) | \$10 | \$12.50 | \$20 | \$0.25 | \$50 |
| Opus 5.5 (`claude-opus-5-5`) | \$4 | \$5 | \$8 | \$0.20 | \$20 |
| Sonnet 5.5 (`claude-sonnet-5-5`) | \$2 | \$2.50 | \$4 | \$0.20 | \$10 |
| Haiku 4.5 (`claude-haiku-4-5`) | \$1 | \$1.25 | \$2 | \$0.10 | \$5 |

All prices are per million tokens.
- **Footnotes:** "Cache hits ... on Claude Fable 5.1 ... 0.025x" and "on Claude Opus 5.5 ... 0.05x".
- **Confirmed elsewhere:** claude.com/pricing raw HTML and the changelog ("Added Claude Opus 5.5 ... \$4/\$20 per Mtok with \$0.20/Mtok cache reads").
- **My inference:** Opus 5.5 and Sonnet 5.5 cost the same per cached-read token. Routing work to Sonnet mainly saves on output and uncached input, at 2x. Fable is 2.5x Opus 5.5 on input and output.
- **Haiku 4.5 retirement:** "Not sooner than October 15, 2026".
- **Plans** (claude.com/pricing raw HTML):
  - Pro: "\$17 Per month with annual subscription discount ... \$20 if billed monthly".
  - Max: "From \$100 Per month", "Choose 5x or 20x more usage than Pro".
  - "Claude Code is included in all paid plans. It shares the same usage limits as the rest of your plan".
  - Fable row: Pro is "Usage credits"; Max 5x and Max 20x are "50% of weekly limits*".
  - Support article (via WebFetch): Pro "aren't included in your plan's usage limits. You can use them with usage credits"; Max "up to 50% of your weekly usage limits on Fable models at no extra cost ... use them faster than other Claude models."
- **Subscription limits** (costs): there is a session limit "shared across all models", plus model-specific "You've hit your Opus limit" and "You've hit your Sonnet limit" messages.
- **Subagents use your limits:** subagent "requests ... count toward the same usage limits as your main conversation".
- **Cache lifetime:** "Subagents ... get five minutes even on a subscription". Switching models: "Each model has its own cache."

## 9. Model aliases inside the installed binary (2.1.289)
- `RW=["sonnet","opus","haiku","fable","best","sonnet[1m]","opus[1m]","fable[1m]","opusplan"],xW=["sonnet","opus","haiku","fable"]`, plus `function sh(e){return RW.includes(e)}`.
  - `fable[1m]` is accepted even though the docs' alias table omits it.
  - There is no `fableplan`: grep count 0.
- `--help`: "--model <model> Model for the current session. Provide an alias for the latest model (e.g. 'fable', 'opus', or 'sonnet') or a model's full name."
- `--effort`: "(low, medium, high, xhigh, max)".
- `default` and `inherit` are handled as special values, not aliases.

## 10. Official tiering guidance and how to phrase routing rules
- **Models overview:** "start with Claude Opus 5.5 for most workloads. Use Claude Fable 5.1 for demanding reasoning and long-horizon agentic work, or when your evals on Claude Opus 5.5 at higher effort still fall short."
- **Fable advice** (model-config): "Hand it ambiguous problems: root-cause investigations, outage debugging, and architecture decisions are where the extra investigation and verification pay off."
- **Costs page:** "Sonnet handles most coding tasks well and costs less than Opus. Reserve Opus for complex architectural decisions or multi-step reasoning." The docs default is more cost-aggressive than the user's Opus-workhorse rule.
- **Blog:**
  - "Pick a smaller model when the work is routine. For example, edits you can describe precisely, mechanical changes, or questions about code that's already in context."
  - "Pick a larger model when the problem is genuinely hard ... architecture decisions."
- **Subagent descriptions** (sub-agents):
  - "Claude uses each subagent's description to decide when to delegate tasks ... write a clear description".
  - "To encourage proactive delegation, include phrases like "use proactively" in your subagent's description field."
  - Keep descriptions short: there is a 15,000-token warning.
  - "@-mention ... ensures that specific subagent runs rather than leaving the choice to Claude".
- **When to stay in the main conversation:** use it for "a quick, targeted change" and when "Latency matters. A subagent that isn't a fork starts fresh". So a Sonnet subagent for a tiny edit isn't automatically cheaper or faster.
- **CLAUDE.md is advisory** (memory): "Claude treats CLAUDE.md files as context, not enforced configuration ... Write instructions that are concrete enough to verify". Also: "if two instructions contradict each other, Claude may pick one arbitrarily."
- **Final review** (best-practices): "have a subagent review the diff in a fresh context".

## 11. How these map onto the user's rule (a proposal for the interview; nothing applied)
Enforcement, from strongest to weakest:
1. **Pin `model` (and `effort`) in each agent's frontmatter:**
   - `implementer` (Opus 5.5): `model: claude-opus-5-5`, `effort: medium` or `high`
   - `quick-task` (Sonnet 5.5): `model: claude-sonnet-5-5`, `effort: low`
   - `reviewer` (Fable): change `model: inherit` to `model: claude-fable-5-1` (or `fable`)
   - Full IDs pin exact versions; aliases follow new releases.
2. **Set `"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5-5"}`** in `~/.claude/settings.json`. Unpinned delegated work (general-purpose, workflow agents, teammates) then runs on Opus 5.5 instead of inheriting Fable. Explore and Plan are unaffected.
3. **Stop orchestrator overrides.** The orchestrator's per-invocation `model` beats frontmatter. Either tell it in CLAUDE.md not to pass `model` to these agents, or use a PreToolUse hook on `Agent` (untested):
   - `updatedInput` "Replaces the entire input object" and must be combined with `allow` or `ask`.
   - Alternatively, deny the call with a reason so Claude retries without `model`.
4. **Forks run on the session model.** Add `permissions.deny: ["Agent(fork)"]`, or instruct the orchestrator not to fork implementation work.
5. **Main session choice:**
   - (A) Fable orchestrator, which is the current setup (`model: claude-fable-5-1`). Every main-thread turn costs Fable rates or limits, and Plan subagents and forks run on Fable.
   - (B) Opus 5.5 as the main session, with Fable pinned to architect and reviewer agents, optional `advisorModel: fable`, and `/model fable` or `claude --model fable` for dedicated design sessions. This matches the official "Opus 5.5 for most workloads" guidance and costs less.
6. **CLAUDE.md routing block.** Name the agents, give concrete triggers (for example, "single-file mechanical edit" goes to quick-task), and add "don't pass a model parameter" and "don't fork implementation work".
7. **Check routing** with `/tasks`, or by reading `resolvedModel` from a PostToolUse hook.
- **Effort note:** the user currently runs Opus 5.5 at `xhigh`, two levels above its documented default. The docs suggest defaults for most tasks.

## 12. Questionable claims I checked
| Claim | Verdict | Evidence | Correction |
|---|---|---|---|
| WebFetch summary: "/model ... taking precedence over environment variables on next launch" | refuted | "With `ANTHROPIC_MODEL` set instead, Claude Code returns to that variable's model on the next launch, whatever you saved with `/model`." | `ANTHROPIC_MODEL` (and `--model`) outrank the saved `/model` choice. |
| WebFetch summary ranked skill/subagent `effort` frontmatter below settings | refuted | "Frontmatter effort applies when that skill or subagent is active, overriding the session level but not the environment variable." | Frontmatter overrides the session and settings level; only `CLAUDE_CODE_EFFORT_LEVEL` and caps beat it. |
| WebSearch summary: Fable 5.1 needs v2.1.250 | refuted | model-config: "Fable 5.1 requires Claude Code v2.1.257 or later"; changelog 2.1.257: "Added Claude Fable 5.1". The support article says 2.1.255. | Use v2.1.257. |
| Top-level `effortLevel` applies to all models | partially | settings-reference `effortLevel` entry | In user settings it is ignored by Opus 5.5 and later. Use `modelSettings.<id>.effortLevel`. |
| Subagent `model` default when omitted is `inherit` | partially | sub-agents order list | Per-invocation `model`, then `CLAUDE_CODE_SUBAGENT_MODEL` if set, then the main model. |
| Agent tool `model` takes any model ID | refuted | binary enum `["sonnet","opus","haiku","fable"]` | Aliases only; ignored for forks. |
| `[1m]` needed for 1M context on these models | refuted | model-config Extended context section | Fable 5.1, Opus 5.5 and Sonnet 5.5 are natively 1M on the Anthropic API. |
| `ANTHROPIC_SMALL_FAST_MODEL` is current | partially | "deprecated in favor of `ANTHROPIC_DEFAULT_HAIKU_MODEL`" | Use `ANTHROPIC_DEFAULT_HAIKU_MODEL`. |
| "grep the bundled cli.js" | refuted (not applicable) | the bundle is a Mach-O binary; `resources/native-binary/claude` | Grep the native binary instead. |

**Side observation (outside this task):** `~/.claude/settings.local.json` has a third-party API key written in plain text inside its `permissions.allow` rules. I have not repeated it here. It should be removed and rotated before any settings are copied into the kit.

## Reported as not existing

- A `fableplan` alias, or any built-in 'Fable plans, Opus executes' hybrid. Only `opusplan` (Opus in plan mode, Sonnet in execution) exists. The binary alias list is [sonnet, opus, haiku, fable, best, sonnet[1m], opus[1m], fable[1m], opusplan] and 'fableplan' appears 0 times.
- An automatic router that picks a model per task based on complexity. The main session runs one model at a time. Routing only happens through delegation (subagent, skill, workflow or teammate `model`), the advisor tool, or manual `/model` or `--model` switches.
- Full model IDs in the Agent tool's per-invocation `model` parameter. It is an enum of only sonnet/opus/haiku/fable (binary schema; hooks reference says 'Optional model alias'). Full IDs must go in agent frontmatter or CLAUDE_CODE_SUBAGENT_MODEL.
- A per-invocation model override for forks: 'Ignored for subagent_type: "fork" — forks always inherit the parent model.'
- `max` in the `effortLevel` or `modelSettings.<model>.effortLevel` settings. The schema enum is low/medium/high/xhigh, and the docs say "`max` isn't accepted as a level in either key". `max` only comes from /effort (session-only), --effort, CLAUDE_CODE_EFFORT_LEVEL, or skill/subagent `effort` frontmatter.
- A way to make a skill's `model:` override last beyond the current turn: 'The session model resumes when you send your next prompt.'
- A way to use CLAUDE_CODE_SUBAGENT_MODEL_FORCE for a multi-tier rule. It forces one model on every subagent, teammate and workflow agent, and ignores definition and per-spawn models.
- A per-subagent thinking toggle ('There is no per-subagent thinking setting'), and any way to turn thinking off on Fable 5.1, Opus 5.5 or Sonnet 5.5.
- Fast mode on Fable or Sonnet. It runs only on Opus 5.5, Opus 5 and Opus 4.8.
- Effort support on Haiku 4.5.
- A setting to cap or force advisor calls ('There is no setting to cap or force advisor calls').
- The `teammateDefaultModel` setting (removed in v2.1.234).
- A `cli.js` in the installed Claude Code bundle. The VS Code extension ships a native Mach-O binary at resources/native-binary/claude.
- A standalone `claude` CLI on PATH, or the Claude Desktop app, on this Mac. Only the VS Code extension's bundled 2.1.289 binary is present.

## Open questions

- Which plan is the user on (Pro, Max 5x, Max 20x, or a Console API key)? It decides Fable billing: Pro bills Fable to usage credits from the first token, Max covers it up to 50% of weekly limits then usage credits, and API keys pay list price. On a Pro plan, a Fable-orchestrator setup could be expensive.
- Should the main session itself be Fable (literal rule; the current settings already have model: claude-fable-5-1)? Or should Opus 5.5 be the main session, with Fable pinned to architect and reviewer subagents and optionally the advisor? The second matches the official 'start with Opus 5.5 for most workloads' guidance and costs less.
- If a subagent omits `effort`, the docs say 'Default: inherits from session'. It isn't documented whether a subagent on a different model (for example Sonnet under a Fable xhigh session) re-resolves to its own modelSettings level. Set `effort` explicitly in each agent, or test it with a PreToolUse hook that logs the `effort` field of its input.
- Should Claude Code's own background Haiku usage (summaries, the claude-code-guide agent) stay as is? The user's rule doesn't mention Haiku. Redirecting ANTHROPIC_DEFAULT_HAIKU_MODEL to Sonnet would raise background cost.
- Should routing be enforced, or only advised? The enforced version would be a PreToolUse hook on `Agent` that strips or denies a per-invocation `model` for the pinned agents, plus permissions.deny Agent(fork). Neither was tested here.
- Should agent definitions pin exact versions (claude-opus-5-5, claude-sonnet-5-5, claude-fable-5-1) or use aliases (opus, sonnet, fable) that change with Claude Code releases?
- Should the user keep `xhigh` for Fable 5.1 and Opus 5.5 (current settings) or move to the documented defaults (Fable `high`, Opus 5.5 `medium`)? The blog says 'for most tasks you should use the model's default effort level'.
- How does the user plan to manage multiple projects 'from the desktop'? The Claude Desktop app and a standalone CLI aren't installed; only the VS Code extension's bundled CLI (2.1.289; latest is 2.1.291) is present.
- Security: ~/.claude/settings.local.json has a plain-text third-party API key in its permissions.allow rules. Should it be removed and rotated before any settings are copied into a shareable kit?

## Sources

- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/settings-reference
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/env-vars
- https://code.claude.com/docs/en/fast-mode
- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/advisor
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/agent-teams
- https://code.claude.com/docs/en/workflows
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/commands
- https://code.claude.com/docs/en/best-practices
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/desktop
- https://code.claude.com/docs/en/plugins/components
- https://code.claude.com/docs/en/tools-reference
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/about-claude/models/overview
- https://platform.claude.com/docs/en/build-with-claude/effort
- https://platform.claude.com/docs/en/about-claude/model-deprecations
- https://claude.com/pricing
- https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan
- https://claude.com/blog/claude-model-and-effort-level-in-claude-code
- https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
- local: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude (--version, --help, embedded JS grep)
- local: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/claude-code-settings.schema.json
- local (read-only): /Users/ryaneskuri/.claude/settings.json
- local (read-only): /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/project/.claude/agents/reviewer.md
- local raw doc copies: /private/tmp/claude-501/-Users-ryaneskuri-portfolio-projects-Harness/3156a81b-9c44-4695-be43-fa1ce35e5f57/scratchpad/docs/
