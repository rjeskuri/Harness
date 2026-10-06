# Research notes: best practices efficiency

Gathered 2026-10-06 by an Opus research agent from the official Claude Code docs, the changelog and the installed binary. Quotes are the sources' words; judgments are the agent's and were read by the harness designer before use.

## Findings

# Official Claude Code guidance for a solo-dev harness, checked 2026-10-06

**How this was checked:**
- Official docs at code.claude.com, downloaded as raw markdown on 2026-10-06.
- The official CHANGELOG. Its newest entry is `## 2.1.291`.
- The local binary at `~/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude`. `--version` prints `2.1.289 (Claude Code)`. I ran its `--help` and subcommand `--help` and searched the binary's strings. Nothing on disk was changed.

## Verdict summary

| # | Claim | Verdict | Correction for the kit |
|---|---|---|---|
| 1 | Anthropic's best-practices guide is code.claude.com/docs/en/best-practices; the anthropic.com engineering post is the same guide | **confirmed** | – |
| 2 | Kit README: "It follows the Agentic Coding Playbook" | **unverifiable**: no official document has that title | `It follows Anthropic's official guidance in [Best practices for Claude Code](https://code.claude.com/docs/en/best-practices) and [The AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook):` |
| 3 | CLAUDE.md should be short; the official number is under 200 lines | **confirmed** | – |
| 4 | Kit `project/CLAUDE.md` maintainer note: "Keep this under ~60 lines" | **partially**: stricter than the official number, which is fine, but not Anthropic's figure | `- Keep this short. Anthropic's guidance is "target under 200 lines per CLAUDE.md file"; this kit aims for about 60. Only write what Claude can't infer from the code.` |
| 5 | `.claude/rules/*.md` with `paths:` load only when a matching file is touched; `~/.claude/rules/` works for every project | **confirmed** | – |
| 6 | Skills replace legacy `.claude/commands/` | **confirmed**: commands still work, skills are preferred for new work | – |
| 7 | A plugin can package a whole personal harness (agents, skills, hooks, settings) | **partially**: settings, CLAUDE.md and rules can't go in a plugin | Keep CLAUDE.md, rules, permissions, sandbox, statusLine and env in `~/.claude/` |
| 8 | Plugin hooks go in `hooks/hooks.json`, same shape as settings | **confirmed** | – |
| 9 | Claude Code uses a 1-hour prompt cache TTL | **partially**: only for the main conversation, on a subscription, within plan usage | – |
| 10 | `/cost`, `/usage`, `/context` exist | **confirmed**: `/cost` is now an alias of `/usage` | – |
| 11 | `/compact <focus>` and configurable auto-compaction | **confirmed** | – |
| 12 | A `+500k` token-budget directive exists | **unverifiable in docs and changelog**: only internal strings in the binary | Don't design around it |
| 13 | `--max-turns` flag | **confirmed with caveats**: print mode only, and not shown in `claude --help` | – |
| 14 | Effort levels and model aliases | **confirmed** | – |
| 15 | Auto mode is the default; project settings can't turn it on | **confirmed** | – |
| 16 | The sandbox is the efficient way to fewer prompts | **confirmed** | – |
| 17 | Kit README sandbox bullet: "prompts you, or retries outside the sandbox with your approval" | **partially**: wrong in auto mode, which is now the default | See the full replacement text in §5 |
| 18 | Kit hook mechanics: `args` exec form, `statusMessage`, Notification matchers, `terminalSequence`, 8-block Stop cap, `background_tasks`, `disableAllHooks` | **confirmed** | – |
| 19 | Kit references: `/skills`, `/fewer-permission-prompts`, `claude doctor`, `/goal … or stop after N turns`, `pyright-lsp@claude-plugins-official` | **confirmed** | – |

## 0. This Mac (read-only checks)

**Claude Code isn't on PATH.**
- `zsh -lic 'which claude'` printed `claude not found`.
- A native install exists at `~/.local/bin/claude`, a symlink to `~/.local/share/claude/versions/2.1.285`. Its modification time is today at 19:35. But `~/.local/bin` is not on the login-shell PATH.
- So `claude doctor`, `claude plugin …` and `claude agents` won't run from Terminal until PATH is fixed.

**The Desktop app isn't installed.** Nothing matched in `/Applications`, and Spotlight found no `Claude.app`. The user's "manage projects from the desktop" goal needs it.

**Current user settings (`~/.claude/settings.json`):**
- `"model": "claude-fable-5-1"`, `"effortLevel": "xhigh"`.
- `modelSettings` sets xhigh for `claude-fable-5-1`, `claude-opus-5` and `claude-opus-5-5`.
- So every session runs Fable at xhigh. That conflicts with both the user's routing rule and the "efficient" goal.
- There is no `~/.claude/CLAUDE.md`, `agents/` or `rules/`.
- The session summary lists an `ultracode` key, but it isn't in `~/.claude/settings.json` or `~/.claude/settings.local.json`.

**Security note.** `~/.claude/settings.local.json` acts as project-local settings when Claude is launched from `~`. It holds about 60 one-off allow rules, and many of them embed a plaintext third-party API key inside `curl` URLs. The user should rotate the key, move it to an environment variable, and prune those rules.

## 1. Best practices and other official guides

**Location.** `curl -sIL https://www.anthropic.com/engineering/claude-code-best-practices` returns `HTTP/2 308` with `location: https://code.claude.com/docs/en/best-practices`.

**Core premise:**
> "Most best practices are based on one constraint: Claude's context window fills up fast, and performance degrades as it fills."

**Verification (the first section of the page):**
- "Give Claude a check it can run: tests, a build, a screenshot to compare. It's the difference between a session you watch and one you walk away from."
- It lists four ways to make the check gate the stop:
  - "In one prompt"
  - "Across a session: set the check as a `/goal` condition"
  - "As a deterministic gate: a Stop hook runs your check as a script and blocks the turn from ending until it passes"
  - "By a second opinion: a verification subagent"
- "Have Claude show evidence rather than asserting success."
- The older wording "single highest-leverage thing you can do" no longer appears anywhere in the current docs.

**CLAUDE.md:**
- "CLAUDE.md is loaded every session, so only include things that apply broadly. For domain knowledge or workflows that are only relevant sometimes, use skills instead."
- "For each line, ask: 'Would removing this cause Claude to make mistakes?' If not, cut it. Bloated CLAUDE.md files cause Claude to ignore your actual instructions!"
- The include/exclude table excludes "File-by-file descriptions of the codebase" and "Anything Claude can figure out by reading code".
- "If you emphasize many lines, none of them stands out."

**Hooks:**
- "Use hooks for actions that must happen every time with zero exceptions."
- "Unlike CLAUDE.md instructions which are advisory, hooks are deterministic and guarantee the action happens."

**Skills:** "Use `disable-model-invocation: true` for workflows with side effects that you want to trigger manually."

**Subagents:** "Subagents run in their own context with their own set of allowed tools." The page's example agent uses `model: opus`.

**Plugins:** "Plugins bundle skills, hooks, subagents, and MCP servers into a single installable unit."

**Parallel work:**
- "Worktrees: run separate CLI sessions in isolated git checkouts so edits don't collide."
- "Desktop app: manage multiple local sessions visually, optionally each in its own worktree."

**Planning and specs:**
- "If you could describe the diff in one sentence, skip the plan."
- "For larger features, have Claude interview you first… using the `AskUserQuestion` tool."
- "Once the spec is complete, start a fresh session to execute it."

**Reviewer caution:** "A reviewer prompted to find gaps will usually report some, even when the work is sound… Tell the reviewer to flag only gaps that affect correctness or the stated requirements."

**Failure patterns:** the kitchen-sink session, correcting over and over ("After two failed corrections, `/clear`"), and the over-specified CLAUDE.md.

**Skills vs legacy commands** (https://code.claude.com/docs/en/skills):
- "**Custom commands have been merged into skills.** A file at `.claude/commands/deploy.md` and a skill at `.claude/skills/deploy/SKILL.md` both create `/deploy` and work the same way. Your existing `.claude/commands/` files keep working."
- "Prefer a skill for new work, since skills also support supporting files."

**A good novice roadmap** (https://code.claude.com/docs/en/features-overview, "Build your setup over time"):
- "Claude gets a convention or command wrong twice → Add it to CLAUDE.md"
- "You paste the same playbook… for the third time → Capture it as a skill"
- "You want something to happen every time without asking → Write a hook"
- "A second repository needs the same setup → Package it as a plugin"
- Also: "**Put guardrails in hooks.** An instruction like 'never edit `.env`' in CLAUDE.md or a skill is a request, not a guarantee."

**Other official guides (2026):**
- "The AI-native SDLC playbook" (claude.com/blog, Aug 21 2026):
  - "Keep it under a page, because Claude reads all of it at the start of a session"
  - "When Claude makes a mistake twice, the correction goes into `CLAUDE.md`"
  - The phrase "Agentic Coding Playbook" does not appear in it.
- "Steering Claude Code…" (claude.com/blog, Jun 18 2026): "Keep CLAUDE.md under 200 lines, give it an owner, and review changes to it like code."
- "Choosing a Claude model and effort level in Claude Code" (claude.com/blog, Jul 7 2026):
  - "For most tasks you should use the model's default effort level."
  - "Pick a smaller model when the work is routine…"
  - "Pick a larger model when the problem is genuinely hard… architecture decisions."

## 2. Memory (https://code.claude.com/docs/en/memory)

**Locations, in load order:**
- Managed policy (`/Library/Application Support/ClaudeCode/CLAUDE.md`)
- User (`~/.claude/CLAUDE.md`)
- Project (`./CLAUDE.md` or `./.claude/CLAUDE.md`)
- Local (`./CLAUDE.local.md`, gitignored)

**When files load:**
- "CLAUDE.md and CLAUDE.local.md files in the directory hierarchy above the working directory are loaded at launch. Files in subdirectories load on demand."
- "All discovered files are concatenated into context rather than overriding each other."

**Size:**
- "target under 200 lines per CLAUDE.md file. Longer files consume more context and reduce adherence."
- "Imports help you organize a long file but don't reduce its context cost."

**Imports:**
- "using `@path/to/import` syntax… Relative paths resolve relative to the file containing the import… maximum depth of four hops."
- External imports trigger "an approval dialog" in project files. User-scope files load their imports without it.

**HTML comments:** "Block-level HTML comments (`<!-- maintainer notes -->`) in CLAUDE.md files are stripped before the content is injected into Claude's context." This confirms the kit's maintainer-note claim.

**Rules:**
- "Rules without `paths` frontmatter are loaded at launch with the same priority as `.claude/CLAUDE.md`."
- "Path-scoped rules trigger when Claude uses the Read, Write, or Edit tool on a file matching the pattern."
- "`paths` is the only field Claude Code reads from a rule."
- "Personal rules in `~/.claude/rules/` apply to every project on your machine."
- From claude-directory: user-level "Rules with `paths:` load when a matching file enters context."

**Auto memory:**
- "The first 200 lines of `MEMORY.md`, or the first 25KB, whichever comes first, are loaded at the start of every conversation."
- Stored at `~/.claude/projects/<project>/memory/`.
- Toggle with `/memory`, `autoMemoryEnabled`, or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

**Extra directories:** "The `--add-dir` flag… By default, CLAUDE.md files from these directories are not loaded." Set `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` to load them.

**Mid-session edits** (prompt-caching page): CLAUDE.md "edit also doesn't apply… The new content loads on the next `/clear`, `/compact`, or restart."

**After compaction** (context-window page):
- "Project-root CLAUDE.md and unscoped rules | Re-injected from disk"
- Path-scoped rules "reload… on demand"
- "If a rule must persist across compaction, drop the `paths:` frontmatter."

**Audit:** `/doctor prompt-audit` checks instruction files for stale or conflicting content. It needs v2.1.283 or later.

## 3. Plugins as a packaged personal harness

**When to use one** (https://code.claude.com/docs/en/plugins/create): "Keep that standalone setup while it serves one project or only you. Make a plugin when you want to share the setup with teammates, install it in several projects, or publish versioned releases."

**Layout:** `.claude-plugin/plugin.json`, `skills/`, `agents/`, `hooks/hooks.json`, `.mcp.json`. "Only `plugin.json` goes inside `.claude-plugin/`."

**Hooks:**
- "`hooks/hooks.json` | Hook configuration: a top-level `"hooks"` key whose value has the same shape as `hooks` in a settings file."
- Use `${CLAUDE_PLUGIN_ROOT}` for script paths.
- "a hook that is in both your settings file and `hooks/hooks.json` runs twice."

**Limits** (https://code.claude.com/docs/en/plugins/components):
- "Only two keys take effect from a plugin, `agent` and `subagentStatusLine`; every other key is dropped."
- "Claude Code doesn't load a `CLAUDE.md` at the plugin root."
- Plugins have no rules component.
- "plugin subagents don't support the `hooks`, `mcpServers`, or `permissionMode` frontmatter fields."
- Plugin skills are namespaced, for example `/my-plugin:review`.

**Easiest route for a solo dev (no marketplace):**
- "Claude Code loads any folder there [`~/.claude/skills/`] that contains a `.claude-plugin/plugin.json` as a plugin in every session, with no flag and no install step."
- `claude plugin init <name>` scaffolds one. Local help: "Scaffold a new plugin at ~/.claude/skills/<name>/ (auto-loads next session as <name>@skills-dir)".

**Local marketplace:**
- `claude plugin validate ./my-marketplace`
- `claude plugin marketplace add ./my-marketplace`. A path source "Reads the directory in place."
- `claude plugin install <plugin>@<marketplace>`. `--scope` takes `user`, `project` or `local`; "Defaults to `user`."
- "Your edits take effect at the next session start or when you run `/reload-plugins`… with no change to the plugin's `version`."
- `--plugin-dir` loads a plugin for one session.
- Binary 2.1.289 `claude plugin --help` lists configure, details, disable, enable, eval, init, install, list, marketplace, prune, tag, test, uninstall, update and validate.

**Converting the kit:** the doc's "Convert an existing `.claude/` setup" steps say to copy `agents/`, `skills/` and `commands/`, and to move the `hooks` object into `hooks/hooks.json`.

**Cache cost:** "Claude Code never invalidates the cache for a plugin's skills, commands, agents, hooks, monitors, or themes."

## 4. Cost and efficiency

**Tracking usage** (https://code.claude.com/docs/en/commands):
- "`/cost` | Alias for `/usage`"
- "`/usage` | Show session cost, plan usage limits, and activity stats… `/cost` and `/stats` are aliases"
- "`/context [all]` | Visualize current context usage as a colored grid"
- "`/compact [instructions]`… Optionally pass focus instructions for the summary"
- `/usage` also shows a `Prompt cache (main)` line, for example "91% of input tokens from cache… warm (1h TTL…)" (v2.1.251 and later).

**Prompt-cache lifetime (TTL)** (https://code.claude.com/docs/en/prompt-caching):
- "Claude Code requests the one-hour TTL only on a Claude subscription within your plan's included usage."
- The table gives: main conversation, one hour on a subscription and five minutes on usage credits, API key or cloud. "Everything else" (subagents, workflows, compaction) gets five minutes.
- "If you sign in with an API key or use a cloud provider, set `promptCacheTtl` to `1h`." There is also `subagentPromptCacheTtl`.
- Subagents "get five minutes even on a subscription."

**What breaks the cache:**
- "Each model has its own cache. Switching with `/model` means the next request reads the entire conversation history with no cache hits."
- A skill whose frontmatter `model` differs from the session's "is also a model switch."
- "`opusplan`… each plan-mode toggle is a model switch."
- Effort changes are an exception: "On Opus 5.5, Sonnet 5.5, and Fable 5.1 with an API key or a Claude subscription, changing effort keeps the cache."
- Advisor toggles are another: "Enabling or disabling the advisor mid-session does not invalidate your main model's prompt cache."

**Compaction:**
- "If you don't set an auto-compact window, Claude Code compacts when the conversation reaches the model's context limit." Native 1M-window models compact "at about 967K tokens by default" (model-config).
- Set the window with `/autocompact 500k`, `autoCompactWindow`, `--autocompact`, or `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. The range is 100K to 1M.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` "can't raise the threshold." `DISABLE_AUTO_COMPACT=1` turns automatic compaction off.
- "`/compact` reads the conversation it summarizes… When you want a fresh start instead of continuity, `/clear` costs nothing" (costs page).

**Costs page tips** (https://code.claude.com/docs/en/costs):
- "Sonnet handles most coding tasks well and costs less than Opus. Reserve Opus for complex architectural decisions or multi-step reasoning."
- "For simple subagent tasks, specify `model: haiku`."
- "Aim to keep CLAUDE.md under 200 lines."
- "Delegate verbose operations to subagents."
- CLI tools "are still more context-efficient than MCP servers."
- "You can't turn off thinking on Opus 5.5, Sonnet 5.5, or the Fable models."

**Effort levels** (https://code.claude.com/docs/en/model-config):
- Levels are `low`, `medium`, `high`, `xhigh`, `max`.
- "Opus 5.5 and Sonnet 5.5 default to `medium`."
- "In Anthropic's testing, Opus 5.5 at `medium` matches or exceeds Opus 5 at `high`."
- `max` "is prone to overthinking" and is session-only.
- Ultracode is a separate toggle: "each request uses more tokens and takes longer… reaches a session or weekly limit sooner."

**Model aliases:**
- `default`, `best`, `fable` (resolves to Fable 5.1), `sonnet` (Sonnet 5.5 on the Anthropic API), `opus` (Opus 5.5), `haiku`, `sonnet[1m]`, `opus[1m]`, `opusplan`.
- `default` is Opus 5.5 on Pro, Max, Team, Enterprise and the API.
- "Fable usage can bill to usage credits instead of drawing on your plan's included limits."

**`--max-turns`:**
- CLI reference: "Limit the number of agentic turns (print mode only)… No limit by default."
- Not in 2.1.289 `claude --help`, though the binary contains `max-turns`.
- Changelog fixes in 2.1.205, 2.1.281 and 2.1.285. Also `CLAUDE_CODE_MAX_TURNS` and subagent `maxTurns`.
- `--max-budget-usd` exists, also print mode only.

**`+500k` directive:**
- Not found in any doc page or the changelog.
- The binary's workflow-script text says: "the turn's token target from the user's "+500k"-style directive… The target is a HARD ceiling".
- SDK fields `budget_tokens` and `budget_limit` are marked `@internal`.
- Treat it as undocumented and possibly flag-gated.

**Background agents:**
- `claude --bg`, `claude agents` (agent view, "research preview"), `/tasks`.
- Subagents run in the background by default where fork mode is on: "as it is by default in an interactive session."
- "Running several sessions or subagents at once multiplies token usage" (agents page).

## 5. Auto mode and the sandbox

**What auto mode is** (https://code.claude.com/docs/en/permission-modes): "A separate classifier model reviews actions before they run, blocking anything that escalates beyond your request, targets unrecognized infrastructure, or appears driven by hostile content."

**How the modes differ:**

| Mode | Runs without asking |
|---|---|
| `acceptEdits` | "Reads, file edits, and common filesystem commands" |
| `auto` | "Everything, with background safety checks" |
| `bypassPermissions` | "Everything", for "Isolated containers and VMs only" |

**It's the default:**
- "With Claude Code v2.1.283 or later, auto mode is the built-in starting permission mode for interactive terminal and VS Code sessions on every plan and provider."
- Changelog 2.1.284 says the same for every plan and provider.

**Enabling it:**
- `claude --permission-mode auto`, or `defaultMode` in `~/.claude/settings.json`.
- "If you set `"auto"` in `.claude/settings.json` or `.claude/settings.local.json`, the value doesn't take effect." This confirms the kit's claim.
- Desktop: "A mode you pick in the selector is remembered per folder."
- VS Code: `claudeCode.initialPermissionMode` "doesn't accept `auto`."

**Caveats:**
- "Auto mode reduces permission prompts but does not guarantee safety."
- Haiku is "not supported on any provider" in auto mode.
- After "3 times in a row or 20 times total" blocks, auto mode pauses.
- The classifier "runs on Claude Sonnet 5 by default."
- Its calls count toward token usage on Enterprise and API accounts, unless the server does the review.

**The sandbox** (https://code.claude.com/docs/en/sandboxing):
- "The sandbox is off by default."
- "Because the operating system applies the limits while a command runs, Claude Code can run sandboxed commands without asking you."
- "Auto-allow mode works independently of your permission mode setting."
- `autoAllowBashIfSandboxed` defaults to `true`, so the kit setting it is redundant but harmless.
- "command hooks… run with your full access", meaning hooks run outside the sandbox.
- Best practices: "pre-approve the tools you trust with `/permissions` and let sandboxed commands run without asking with `/sandbox`."

**Fix for kit verdict #17:**
- Docs on hosts outside the allowlist: "Auto mode | Refused unless the command listed the host and the classifier approved the list."
- On unsandboxed retries: "Auto mode: a separate classifier model evaluates the underlying command."
- And: "if an allow rule such as `Bash(curl *)` matches the command, it also approves the retry."
- Replacement text for the bullet: `A command that needs more (another host, a write elsewhere) is blocked, and the error names what was blocked. In Manual or Accept-edits mode you then get a prompt, or an "unsandboxed" retry prompt. In auto mode (the default) a new host is refused unless Claude names it on the command and the classifier approves, and an unsandboxed retry is approved or refused by the classifier, not by you. A matching allow rule also approves the retry. To be asked about every unsandboxed retry, add the ask rule Bash(dangerouslyDisableSandbox:true).`

## 6. Multiple projects for a solo developer

**Settings scopes** (https://code.claude.com/docs/en/settings):

| Scope | File | Applies to |
|---|---|---|
| User | `~/.claude/settings.json` | "You, in every project on this machine" |
| Project | `.claude/settings.json` | The project, shared |
| Local | `.claude/settings.local.json` | You, in this one project |

- Precedence: Managed > CLI > Local > Project > User. "Lists merge instead of overriding."

**Sharing config across projects** (https://code.claude.com/docs/en/permissions): "**User-level configuration**: place files in `~/.claude/agents/`, `~/.claude/output-styles/`, or `~/.claude/settings.json`… **Plugins**… **Launch from the config directory**."

**User-level pieces:**
- `~/.claude/skills/` loads in "All your projects on this machine, but not Cowork or cloud sessions."
- `~/.claude/agents/` is "available in all your projects."
- `~/.claude/rules/` works as described in §2.

**What `--add-dir` loads:**
- Skills: yes. Commands: yes. Agents: yes.
- Settings: only `enabledPlugins` and `extraKnownMarketplaces`.
- CLAUDE.md and rules: only with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`.
- The `permissions.additionalDirectories` setting grants file access only.

**Monorepo vs separate repos:**
- https://code.claude.com/docs/en/large-codebases covers monorepos: where you start Claude, per-directory CLAUDE.md, `claudeMdExcludes`, sparse worktrees, and plugins "when layering stops scaling."
- No official doc tells a solo dev to choose one over the other.

**Desktop app** (https://code.claude.com/docs/en/desktop):
- "filter sessions by status, project, or environment, and… group sessions by project."
- Each session can use a worktree.
- "use Desktop when you want to manage parallel sessions in one window."
- Desktop and CLI "read the same configuration files."
- Cloud "Projects" are a public beta on Pro and Max (https://code.claude.com/docs/en/claude-projects).

**Retro pixel-art customization:**
- The status line supports multi-line output and ANSI colors.
- Plugins can ship "color themes and output styles."
- A mod can "replace or restyle parts Claude Code draws itself, such as… the spinner" and draw panes.
- Mods draw only "in the terminal and the Desktop app", not in the VS Code chat panel (https://code.claude.com/docs/en/plugins/mods/overview).

## Mapping the user's model rule (Fable / Opus 5.5 / Sonnet)

**Mechanisms that exist:**
- Subagent `model`: "`sonnet`, `opus`, `haiku`, `fable`, a full model ID such as `claude-opus-5-5`, or `inherit`".
- Skill `model` and `effort` frontmatter.
- `CLAUDE_CODE_SUBAGENT_MODEL`, plus `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` to override everything.
- `opusplan` (Opus during plan mode, Sonnet during execution).
- The advisor (https://code.claude.com/docs/en/advisor).
  - "Opus 5 or Opus 5.5 | Fable, Opus 5 or later" is an accepted pairing.
  - "Claude calls the advisor at decision points rather than on every turn, so pairing a faster main model with a stronger advisor typically costs less."
  - Limits: it is "experimental and requires the Anthropic API." "There is no setting to cap or force advisor calls." A Fable advisor bills to usage credits on some plans.

**Traps when Fable is the main model:**
- Forks use the "Same as main session" model.
- The general-purpose subagent inherits the main model unless `CLAUDE_CODE_SUBAGENT_MODEL` is set.
- Explore runs on Opus when the main model is Fable (subscription).
- Every `/model` switch rebuilds the cache.
- **Implication:** keep Opus 5.5 as the main model, use `/advisor fable` for big decisions, use a `model: fable` subagent for final review, and use `model: sonnet` subagents for small tasks.

**Tension with official guidance:**
- The costs page treats Sonnet, not Opus, as the everyday model.
- The Fable docs support the user's design use: "Hand it ambiguous problems: root-cause investigations, outage debugging, and architecture decisions."
- They also say: "Skip the verification reminders."

## Reported as not existing

- No official Anthropic document titled 'Agentic Coding Playbook'. The closest official sources are 'Best practices for Claude Code', 'The AI-native SDLC playbook' (claude.com blog, Aug 21 2026), and the docs 'Champion kit' (an internal-adoption playbook).
- Plugins cannot ship CLAUDE.md (the plugin-root CLAUDE.md is not loaded) or .claude/rules.
- A plugin's settings.json keeps only `agent` and `subagentStatusLine`, so permissions, sandbox, env, model and statusLine can't come from a plugin.
- Plugin subagents ignore the `hooks`, `mcpServers` and `permissionMode` frontmatter fields.
- No documented `+500k` token-budget directive in the docs or changelog. It appears only as internal workflow-budget strings in the binary.
- `--max-turns` is not listed in `claude --help` (2.1.289), although the CLI reference documents it for print mode. `--advisor` is also hidden from `--help`.
- `defaultMode: "auto"` in a project's `.claude/settings.json` or `.claude/settings.local.json` has no effect.
- No setting caps or forces advisor calls, and there is no per-subagent thinking setting.
- No built-in rule-based model router. Model routing is done through subagent or skill `model` fields, `CLAUDE_CODE_SUBAGENT_MODEL`, `opusplan`, or the advisor.
- Haiku is not a supported model for auto mode on any provider.
- Thinking cannot be turned off on Opus 5.5, Sonnet 5.5 or the Fable models.
- No official recommendation to choose a monorepo over separate repos for a solo developer.
- No mention of the status line in the Desktop app docs (unverified whether it shows there). Mods draw in the terminal and Desktop, but not in the VS Code chat panel.

## Open questions

- Which plan is the user on (Pro, Max, Team, or API key)? It decides whether the 1-hour cache applies, whether Fable and a Fable advisor bill to usage credits, and whether auto-mode classifier calls cost tokens.
- Where is `ultracode` set? The session summary lists it, but it isn't in ~/.claude/settings.json or ~/.claude/settings.local.json. If it is on, every substantive task becomes a multi-agent workflow.
- Does the user want Fable as the main model (expensive; forks and the general-purpose subagent inherit it), or Opus 5.5 as the main model with `/advisor fable` plus a Fable final-review subagent? The current settings pin `claude-fable-5-1` at xhigh effort for every session.
- Will the user work mainly in the Desktop app (not installed), the VS Code extension (installed, 2.1.289), or Terminal? `~/.local/bin` is not on PATH, and this affects where mods and status-line art can show.
- Are all of the user's projects git repos? Worktrees and Desktop session isolation need git.
- Who created `~/.local/bin/claude` (2.1.285, modified today at 19:35)? Was it the user or another process? PATH needs fixing either way.
- Should the plaintext API key in ~/.claude/settings.local.json allow rules be rotated and the one-off rules pruned?
- Does the undocumented `+500k` per-turn token target work in the user's build and session, or is it gated?
- Not documented: whether path-scoped globs in user-level `~/.claude/rules/` resolve against each project's root.

## Sources

- https://code.claude.com/docs/en/best-practices
- https://www.anthropic.com/engineering/claude-code-best-practices (308 redirect to the docs page)
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/features-overview
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/context-window
- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/advisor
- https://code.claude.com/docs/en/commands
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/env-vars
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/settings-reference
- https://code.claude.com/docs/en/permissions
- https://code.claude.com/docs/en/permission-modes
- https://code.claude.com/docs/en/auto-mode-classifier-billing
- https://code.claude.com/docs/en/sandboxing
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/goal
- https://code.claude.com/docs/en/workflows
- https://code.claude.com/docs/en/agents
- https://code.claude.com/docs/en/agent-view
- https://code.claude.com/docs/en/worktrees
- https://code.claude.com/docs/en/large-codebases
- https://code.claude.com/docs/en/desktop
- https://code.claude.com/docs/en/claude-projects
- https://code.claude.com/docs/en/claude-directory
- https://code.claude.com/docs/en/debug-your-config
- https://code.claude.com/docs/en/statusline
- https://code.claude.com/docs/en/champion-kit
- https://code.claude.com/docs/en/plugins/create
- https://code.claude.com/docs/en/plugins/components
- https://code.claude.com/docs/en/plugins/create-marketplace
- https://code.claude.com/docs/en/plugins/cli-reference
- https://code.claude.com/docs/en/plugins/loading
- https://code.claude.com/docs/en/plugins/code-intelligence
- https://code.claude.com/docs/en/plugins/mods/overview
- https://code.claude.com/docs/llms.txt
- https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
- https://claude.com/blog/the-ai-native-sdlc-playbook
- https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more
- https://claude.com/blog/claude-model-and-effort-level-in-claude-code
- Local binary: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude (--version, --help, plugin --help, string search)
- Local: /Users/ryaneskuri/.local/bin/claude -> ~/.local/share/claude/versions/2.1.285
- Local: /Users/ryaneskuri/.claude/settings.json and /Users/ryaneskuri/.claude/settings.local.json (read only)
- Kit: /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/README.md, project/CLAUDE.md, project/.claude/*, user/*
