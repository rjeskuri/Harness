# Research notes: visual customization

Gathered 2026-10-06 by an Opus research agent from the official Claude Code docs, the changelog and the installed binary. Quotes are the sources' words; judgments are the agent's and were read by the harness designer before use.

## Findings

## 0. Environment on this Mac (affects every answer below)

- **Only one install exists:** the VS Code extension's bundled binary `~/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude`, which reports `2.1.289 (Claude Code)`.
  - `zsh -lic 'which claude'` gives "claude not found". There is no `~/.claude/local` and no Claude app in `/Applications` (`mdfind` for the Desktop bundle id also returned nothing).
  - No Kitty, Ghostty, iTerm2 or WezTerm is installed. The machine runs macOS 26.5.2, and the shell env has `TERM_PROGRAM=Apple_Terminal` and `COLORTERM=truecolor`.
- **This session runs in the extension's graphical chat panel.** The process tree is `Visual Studio Code` → `Code Helper (Plugin)` → `native-binary/claude`.
- **Versions:** the newest changelog entry is `## 2.1.291`. Mods arrived in `## 2.1.287`: "Added Claude Mods: plugins may now modify deeper behavior".
- **What that means in the chat panel:**
  - **Mods draw nothing there.** The mods overview table row reads: "The VS Code extension's chat panel | Yes | No" (hooks run, drawings don't appear).
  - **The custom status line doesn't show there either.**
    - The VS Code page never mentions it.
    - In `webview/index.js`, the only `statusLine` strings are CSS class names (`statusLine:"statusLine_hONcXw"`).
    - `extension.js` mentions `statusLine` only in its settings/security key lists.
  - **`spinnerVerbs` IS honored there.** The webview contains `if($.mode==="replace")return $.verbs.length>0?$.verbs:aj1;return[...aj1,...$.verbs]`.
- **How to get a terminal where the visual features work:**
  - VS Code setting `claudeCode.useTerminal`: "Launch Claude in terminal mode instead of graphical panel".
  - Or install the standalone CLI. The docs say: "Installing the extension does not put `claude` on your shell PATH."

## 1. `statusLine` setting and `/statusline` command

**Verdicts**
- **CONFIRMED: shell command, JSON on stdin, prints stdout.** "runs any shell script you configure. It receives JSON session data on stdin and displays whatever your script prints".
  - The local schema's `statusLine` properties are `type`, `command`, `padding`, `refreshInterval`, `hideVimModeIndicator`.
- **CONFIRMED: `/statusline` writes the script for you.** "accepts natural language instructions… generates a script file in `~/.claude/` and updates your settings automatically". With no argument it will "auto-configure from your shell prompt" (commands page).
- **CONFIRMED: ANSI colors.** "use ANSI escape codes like `\033[32m` for green (terminal must support them)".
- **CONFIRMED: OSC 8 links.** "use OSC 8 escape sequences to make text clickable".
- **PARTIALLY: 24-bit color.** The docs only show 16-color codes and never mention truecolor.
  - Corroboration: ccstatusline, which the official page links, advertises "Basic (16), 256-color …, and truecolor (with hex codes) modes".
  - Correction: "Truecolor SGR (`\033[38;2;R;G;Bm`) is not documented for status lines but is used by community status lines; it needs a truecolor terminal. Test with mock input."
- **CONFIRMED: Unicode block and box characters.** The doc examples build bars from `▓`, `░`, `█` and use emoji.
- **CONFIRMED: multiple lines.** "each `echo` or `print` statement displays as a separate row".
  - **Binary nuance (2.1.289):** output is processed as `M.stdout.trim().split('\n').flatMap((V)=>V.trim()||[]).join('\n')`. Every line is trimmed and blank lines are dropped.
  - A Node test showed NBSP gets trimmed, while U+2800 (braille blank) and a leading `\x1b[0m` survive.
  - Correction for sprites: "Start each row with an escape code (for example `\033[0m`) or use U+2800 for transparent pixels, or leading spaces are trimmed and rows misalign."
- **CONFIRMED: refresh behavior.**
  - It runs at session start, on each new assistant message, after `/compact`, on permission-mode change, on vim toggle, and on rate-limit and cache expiry times.
  - "Claude Code debounces updates at 300ms". "If a new update triggers while your script is still running, Claude Code cancels the in-flight script."
  - `refreshInterval` "re-runs your command every N seconds… The minimum is `1`." So a status line animates at 1 frame per second at most.
- **CONFIRMED: sizing.** "Read the `COLUMNS` and `LINES` environment variables instead."
- **Caveats:**
  - "The status line runs locally and does not consume API tokens. It temporarily hides during certain UI interactions, including the help menu and permission prompts."
  - "Multi-line status lines with escape codes are more prone to rendering issues than single-line plain text".
  - "Outside fullscreen rendering, Claude Code shows notifications on the same row as your status line."
  - It needs workspace trust, and `disableAllHooks` disables it.
- **REFUTED / not supported: images in the status line.** The docs list only colors and OSC 8 links. No image protocol is documented.

**JSON on stdin** (quoted field names):
- `model.id`, `model.display_name`, `cwd`, `workspace.current_dir`, `workspace.project_dir`, `workspace.repo.{host,owner,name}`
- `session_id`, `session_name`, `cost.total_cost_usd`, `cost.total_duration_ms`, `cost.total_api_duration_ms`, `cost.total_lines_added/removed`
- `context_window.{total_input_tokens,total_output_tokens,context_window_size,used_percentage,remaining_percentage,current_usage}`, `exceeds_200k_tokens`
- `rate_limits.five_hour/seven_day.{used_percentage,resets_at}`, `prompt_cache.{warm,hit_ratio,expires_at,…}`
- `effort.level`, `thinking.enabled`, `fast_mode`, `output_style.name`, `vim.mode`, `agent.name`, `pr.*`, `worktree.*`, `version`, `transcript_path`

**Related setting:** `subagentStatusLine` takes one row per subagent, and its `content` "is rendered as-is, including ANSI colors and OSC 8 hyperlinks."

## 2. Mods (plugins whose code draws panes, bands, toasts, status text)

**Where they're documented:** https://code.claude.com/docs/en/plugins/mods/ (overview, create, interface, gallery, events, api, reference, test, troubleshoot, admin). The full types are on GitHub at `mods/types/claude-code.d.ts`; that copy is header-stamped "Written by Claude Code 2.1.277". I extracted the 2.1.289 copy from the local binary into the scratchpad.

**What a mod is, and where it runs**
- "A mod is a plugin that changes how Claude Code looks and behaves. It's made of JavaScript or TypeScript event handlers".
- "Mods are on by default. In the terminal, use Claude Code v2.1.287 or later… mods work there [Desktop] from v2.1.286."
- **Drawing appears** in: the terminal (including an editor's integrated terminal), and the Desktop Code tab "except elements … marked terminal-only".
- **Drawing does not appear** in: the VS Code chat panel, `claude -p`, the Agent SDK, or cloud sessions.

**Where a mod can draw**
- **Pane:** "a sidebar beside the transcript in a wide fullscreen terminal, or a framed region above the prompt otherwise."
- **Band:** "a strip directly above the prompt input. It's always there, and every mod shares it."
  - From the 2.1.289 types, its height is "capped at half the terminal's rows, the prompt's included."
- **Existing parts of the UI:** a mod can restyle or replace `Spinner`, `ToolUse`/`ToolResult`, `UserMessage`/`AssistantMessage`, `PromptHint` and more.
  - "The permission prompt isn't a render site".

**Elements** (reference table plus the 2.1.289 types)
- **`Text`:** "A `color` is a theme key or a color such as 'red'".
- **`Raster`** (terminal only)
  - "a grid of colored character cells… A color is a 24-bit RGB value". `columns` up to 512, `rows` up to 256.
  - "A `Raster` rounds each color to a smaller palette, so `0x2e7d32` draws as `#337733`". The types add: "its palette paints 1024 distinct color pairs at once and the rest as their nearest". Each cell is "one printable width-1 BMP character (blocks, box drawing, braille too)".
- **`Image`** (terminal only)
  - Takes "PNG or RGBA bytes up to 2 MiB, or a file path".
  - Types: "pixels over a box of cells where the terminal can (kitty, Ghostty), the `alt` elsewhere."
- **`Svg`** (Desktop only)
  - Takes "An SVG document, up to 131,072 characters". "In the terminal, a pane that returns only an `Svg` opens empty."
  - The 2.1.289 types add `isInteractive`: "`true` draws the SVG in a script-less sandboxed frame so hover, CSS `:hover`, SMIL animation and `<title>` tooltips work". This prop is not in the docs table.
- **`Client`** (terminal and Desktop)
  - "A region drawn by a second file of yours, for animation and pointer input". It has its own `every(ms, fn)` frame clock.
  - Types: `ClientElements = Omit<Elements['terminal'], 'Client' | 'Raster' | 'Image'>`.

**Animation**
- "To animate a `Raster` that's already on screen, call `$.ui.blit`… It repaints that one element without running your `ui.render` hook again."
- Types: blits are taken "up to 120 a second … some sixty shown", for example `$.clock.every(33, () => $.ui.blit({ requestId, key, cells: frame() }))`.
- Blit targets are "one of its panes' ids, a tool row's `tool_use_id`, the band's".
- Full redraws via `$.ui.invalidate` are "Throttled to 10 a second, or 30 in the terminal for the visible pane, the expanded band, and the hint line under the prompt."

**Text-only surfaces**
- `$.ui.status(text)`: "One line under the prompt… starts with [a warning sign] and the mod's name".
- `$.ui.toast(text)`: "A toast notification at the top right… disappears after a few seconds" (4 s by default; `timeoutMs` changes it).
- `$.ui.log(text)`: "A dim line in the transcript that Claude doesn't read".

**Sound:** `$.audio.play({ asset })`. Types: "`{ asset }` is the plugin's own file; `afplay` plays it on macOS". This fits retro sound effects.

**Hot reload**
- "Claude Code watches a directory loaded with `--plugin-dir` and hot-reloads the hooks module when a file in it changes."
- Mods Claude writes live in `~/.claude/dev-mods/<session-id>/`, and after you approve, they "reload at the end of each turn that changes them".
- Use `/reload-plugins` for installed mods, and `CLAUDE_CODE_PLUGIN_DIR_WATCH=1` for non-interactive sessions.
- `CLAUDE_CODE_PLUGIN_DIRS` loads mod folders "for apps you can't pass a flag to" (for example the Desktop app).

**Trust:** "Mods aren't sandboxed." Run `claude plugin validate <dir>` to list a mod's hooks and calls before installing it.

**Template to start from:** the playground sample `token-weather`. It draws a block-character context chart in the band and uses `$.session.usage()`, which its README describes as "free… it sends no token-count request".

## 3. Themes

- **CONFIRMED: `/theme` and `/config` (Theme row) pick a theme.** The `theme` setting accepts `auto`, `dark`, `light`, `*-daltonized`, `*-ansi`, and `"custom:<slug>"` or `"custom:<plugin-name>:<slug>"`.
  - "Claude Code does not control the terminal's own color scheme".
- **CONFIRMED: custom themes are JSON files.** "Each custom theme is a JSON file in `~/.claude/themes/`" with the fields `name`, `base` and `overrides`.
  - Accepted color values: `#rrggbb`, `#rgb`, `rgb(r,g,b)`, `ansi256(n)`, `ansi:<name>`.
  - Hot reload: "Claude Code watches `~/.claude/themes/` and reloads when a file is added or changed". You need one restart if the folder didn't exist at startup.
  - Plugins can ship themes as `themes/<slug>.json` (changelog 2.1.118).
- **Useful tokens:** `claude` ("Primary brand accent, used for the spinner"), `claudeShimmer`, `promptBorder`, `userMessageBackground`, `rate_limit_fill`/`rate_limit_empty`, `<color>_FOR_SUBAGENTS_ONLY`.
- **Per project:** the `theme` key's scope is "Any file", so a project's `.claude/settings.json` can set its own `custom:<slug>`.
- **Desktop app: UNVERIFIABLE in official docs.**
  - The docs only say `/config` there "opens Settings → Claude Code… `/config theme=dark` doesn't set the theme."
  - A user report (GitHub #76200, closed `not_planned`) says the theme "lives in Settings (Cmd+,) under Appearance". Custom background colors (#63020) were also closed `not_planned`.

## 4. Spinner and branding

- **CONFIRMED: `spinnerVerbs`.** An object with a `verbs` array and a `mode` of `"append"` or `"replace"`. Added in 2.1.23, and it also works in the VS Code panel.
- **PARTIALLY: there is no `spinnerTips` key** (it's absent from the local schema). Correction: "Use `spinnerTipsEnabled` (boolean) and `spinnerTipsOverride` (`tips`, `tipsFile`, `label`, `excludeDefault`)."
  - Tip text is up to 500 characters, and "Claude Code strips ANSI escapes and control characters". `label` is up to 40 characters.
  - From project and local settings, Claude Code reads plain-string tips only.
- **No setting changes the spinner glyph or animation.**
  - A mod can replace the `Spinner` render site. Since the spinner isn't one of the 30-per-second sites, it redraws at most 10 times a second.
  - `prefersReducedMotion` "reduce[s] or turn[s] off interface animations such as the spinner, shimmer, and flash effects".
- **Custom greeting: PARTIALLY.**
  - There is no greeting key. `companyAnnouncements` ("Show your organization's announcements to users at startup… picks one at random for each session", any settings file) is the documented way to show startup text.
  - A mod can also toast or log at `session.start`.
  - `CLAUDE_CODE_HIDE_CWD` only hides the cwd in the startup logo. No key replaces the logo or mascot.
- **Other identity knobs:**
  - `/color` sets the prompt-bar color for the session, from 8 named colors. It is per session, with no settings key.
  - `/rename` puts a name on the prompt bar.
  - A subagent's `color:` frontmatter accepts "`red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, or `cyan`".
  - Output styles: "A style's instructions add input tokens". Custom styles "leave out Claude Code's built-in software engineering instructions… unless `keep-coding-instructions` is set to `true`".

## 5. Notifications and toasts

- **Built-in alerts:** `preferredNotifChannel` accepts `auto`, `terminal_bell`, `iterm2`, `iterm2_with_bell`, `kitty`, `ghostty`, `notifications_disabled`.
  - With `"auto"`: "rings the bell in Terminal.app only when its audible bell is off".
  - The VS Code integrated terminal gets no desktop notification.
- **Settings hooks can't show rich content: REFUTED.**
  - "Notification hooks can't block or modify notifications. Claude Code discards their `systemMessage` and `continue` fields but still emits `terminalSequence`".
  - `terminalSequence` is "Restricted to OSC `0`/`1`/`2`/`9`/`99`/`777` and BEL". "CSI cursor and color sequences… OSC 8 hyperlinks, OSC 52… and OSC 1337, is rejected".
  - "Hooks run without a controlling terminal, so writing escape sequences directly to `/dev/tty` fails".
  - Claude Code "ignores the field" with `-p` and in the Agent SDK.
  - What a hook can still do: side effects such as `afplay` or `osascript` (text-only notifications).
- **Mods:** toasts and `$.ui.status` are plain text. Rich content is only possible in a pane or the band.

**Starter-kit checks**
- `user/hooks/notify.py` returns `{"terminalSequence": "\u0007"}` as its last resort. **PARTIALLY:** this is valid (BEL is allowlisted and is emitted for Notification events) in interactive terminal sessions.
  - It's most likely ignored in the VS Code panel and the Desktop app. Those host Claude Code "through the Agent SDK's `canUseTool` callback", and the field only works "while its interface is on screen". This is an inference.
  - Suggested correction for the docstring's step 3: "The terminal bell, emitted by Claude Code on this hook's behalf (interactive terminal sessions only)."
- `project/.claude/agents/reviewer.md` uses `color: orange`. **CONFIRMED** valid.

## 6. Can images or sprites be shown anywhere?

- **Terminal:** the mod `Image` element only.
  - The binary contains Kitty graphics protocol code ("kitty graphics: payload must be standard base64", `a=T,U=1,q=1`).
  - Zero hits for `1337;File=` or `sixel`, so iTerm2 inline images are not used.
  - Pixels draw in kitty and Ghostty; everywhere else (Terminal.app, VS Code terminal, iTerm2) you get the `alt` text. This is per the types; I haven't tested each terminal.
  - `Raster` colored cells work in any terminal.
- **Desktop Code tab:**
  - Mod `Svg`, either as a static image or animated with SMIL via `isInteractive`, plus `Client`. No `Raster` or `Image` ("The Desktop app has no `Raster`").
  - "The Browser pane can also open static HTML files, PDFs, images, and videos from your project."
- **Artifacts:** "The CSP blocks every external image… embeds images as data URIs". Canvas and inline JS are allowed.
- **VS Code extension panel:** no documented image or sprite surface, and mods don't draw there.

## 7. Artifacts as a pixel-art dashboard

- **CONFIRMED feasible.** "An artifact is a single HTML page, so anything you can express in HTML, CSS, and inline JavaScript is in scope."
- **Fonts:** Google Fonts is "the one external font source". I confirmed Press Start 2P, VT323, Silkscreen and Pixelify Sans all return HTTP 200.
- **Limits:**
  - "The rendered page must be 16 MiB or smaller."
  - "Generating an artifact uses output tokens… images embedded as data URIs are the main contributors".
- **Live data:** only through claude.ai connectors, and "Local MCP servers you configure in Claude Code… the published page can't call them."
- **Requirements:** a Pro, Max, Team or Enterprise plan, a `/login` claude.ai session, and the Anthropic API (not Bedrock, Vertex or Foundry).
- **Finding and opening them:** `/artifacts` lists, opens or attaches an artifact (v2.1.208+). "Press `Ctrl+]` at any time to reopen the session's most recent artifact". Set `CLAUDE_CODE_ARTIFACT_AUTO_OPEN=0` to stop the auto-open.
- Run `/artifact-capabilities` to see which extra runtime capabilities your account has.

## 8. Frank feasibility assessment

- **Colored block-character pixel art in the status line: YES, today, zero tokens.**
  - Use the half-block `▀` with foreground and background colors to get 2 pixels per cell. A 2 to 4 row mascot plus a context "HP bar" fits.
  - Caveats: 24-bit color isn't documented, rows get trimmed, it hides during permission prompts, it truncates on narrow terminals, and it doesn't show in the VS Code chat panel.
- **In a pane or band: YES via a mod `Raster`.** This works in the terminal only, with the palette quantized. In Desktop, draw the pixels as `Svg` `<rect>`s instead.
- **Animated frame-by-frame sprites:**
  - **Terminal:** YES via `$.ui.blit` (up to about 60 shown per second, and 8 to 12 fps is plenty).
  - **Desktop:** YES via SVG SMIL (`isInteractive`) or a `Client`.
  - **Status line:** no better than 1 fps.
  - **Spinner replaced by a mod:** at most 10 redraws per second. A `Raster` there is untested.
- **Per-project mascots: YES.**
  - The status-line script can branch on `workspace.project_dir`, `workspace.repo.name` or `session_name`.
  - Project `.claude/settings.json` can set `statusLine`, `theme` and `spinnerVerbs`.
  - A mod can read `$.session.cwd()` or `$.session.repo()`.
- **Real PNG sprites in the terminal:** only after installing Ghostty or Kitty, and only through a mod `Image`.
- **Cheapest path:**
  1. `spinnerVerbs` set to "replace" with retro verbs, plus a retro-palette custom theme JSON. Zero code and zero tokens; the verbs even work in the VS Code panel.
  2. A small bash or python status-line script with a per-project half-block mascot. Run it via `claudeCode.useTerminal` or the standalone CLI.
  3. When animation is wanted, fork `token-weather` into a band mod using `Raster` and `blit`.
  4. Keep artifacts for occasional dashboards, since each publish costs output tokens and can't read local state live.

Scratchpad copy of the 2.1.289 types: `/private/tmp/claude-501/-Users-ryaneskuri-portfolio-projects-Harness/3156a81b-9c44-4695-be43-fa1ce35e5f57/scratchpad/embedded-claude-code.d.ts`

## Reported as not existing

- A `spinnerTips` settings key (the real keys are `spinnerTipsEnabled` and `spinnerTipsOverride`)
- Any setting that changes the spinner glyph or its animation frames (only verbs, tips, theme colors such as `claude`/`claudeShimmer`, and `prefersReducedMotion`); you need a mod that replaces the `Spinner` render site
- Any setting that replaces the startup logo, Clawd mascot or welcome art (`CLAUDE_CODE_HIDE_CWD` only hides the cwd; `companyAnnouncements` only adds text)
- A dedicated custom-greeting setting (closest: `companyAnnouncements`)
- Image or sprite rendering in `statusLine` (only ANSI colors and OSC 8 links are documented), and sub-second status-line animation (`refreshInterval` minimum is 1 s)
- iTerm2 inline-image (OSC 1337) or sixel support anywhere in Claude Code: the mod `Image` element uses the Kitty graphics protocol (kitty, Ghostty), and hooks' `terminalSequence` rejects OSC 1337
- Rich content (images, colors) from settings hooks: `terminalSequence` is allowlisted to OSC 0/1/2/9/99/777 plus BEL, and Notification hooks' `systemMessage` is discarded
- Colored or ANSI text in spinner tips (Claude Code strips ANSI escapes)
- Non-text toasts or status lines from mods (`$.ui.toast`, `$.ui.status` and `$.ui.log` take plain text)
- Mod drawings or the custom `statusLine` in the VS Code extension's graphical chat panel
- `Raster` and `Image` in the Desktop app, and `Svg` in the terminal
- A settings key that persists the `/color` prompt-bar color per project (it is per session)
- Custom color themes for the Desktop app window beyond Light/Dark/Match System (GitHub #63020 closed not_planned; user-reported, not official docs)
- Artifacts calling local MCP servers or loading external images at view time

## Open questions

- Does 24-bit SGR (`\033[38;2;R;G;Bm`) from a statusLine script render correctly in macOS 26 Terminal.app (this env has COLORTERM=truecolor)? The docs don't say; test with mock input in a terminal session.
- Does the Desktop app's Code tab render the settings `statusLine`? It isn't documented, and the Desktop app isn't installed here to check.
- Do custom themes in `~/.claude/themes/` affect the Desktop Code tab, or only the terminal UI?
- Will a `Raster` be accepted at the `Spinner` render site, and can it be blitted there? The documented blit targets are panes, tool rows and the band.
- Does the mod `Image` element draw pixels in WezTerm or the VS Code integrated terminal? The 2.1.289 types name only kitty and Ghostty.
- Is the kit's `terminalSequence` BEL fallback in notify.py emitted when Claude Code runs in the VS Code chat panel? Docs say the field is ignored in the Agent SDK and only works while the terminal interface is on screen, so it is probably not emitted.
- Mods went GA in 2.1.287 and 2.1.288-2.1.290 contain many mod rendering fixes. The local install is 2.1.289 and the latest is 2.1.291. Should the kit pin a minimum version?
- Does the user want visuals in the VS Code panel (only spinnerVerbs works there), in a terminal (status line plus mods), or in the Desktop app (mods with Svg)? This decides which path to build.

## Sources

- https://code.claude.com/docs/en/statusline
- https://code.claude.com/docs/en/settings-reference
- https://code.claude.com/docs/en/terminal-config
- https://code.claude.com/docs/en/plugins/mods/overview
- https://code.claude.com/docs/en/plugins/mods/create
- https://code.claude.com/docs/en/plugins/mods/interface
- https://code.claude.com/docs/en/plugins/mods/gallery
- https://code.claude.com/docs/en/plugins/mods/reference
- https://code.claude.com/docs/en/plugins/mods/api
- https://code.claude.com/docs/en/plugins/components
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/hooks-guide
- https://code.claude.com/docs/en/artifacts
- https://code.claude.com/docs/en/commands
- https://code.claude.com/docs/en/desktop
- https://code.claude.com/docs/en/vs-code
- https://code.claude.com/docs/en/fullscreen
- https://code.claude.com/docs/en/output-styles
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/env-vars
- https://code.claude.com/docs/llms.txt
- https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
- https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts
- https://github.com/anthropics/claude-code-playground/tree/main/claude-code/mods/token-weather
- https://github.com/anthropics/claude-code/issues/76200
- https://github.com/anthropics/claude-code/issues/63020
- https://github.com/sirmalloc/ccstatusline
- https://fonts.googleapis.com/css2?family=Press+Start+2P
- local: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/resources/native-binary/claude (--version = 2.1.289; strings: kitty graphics, statusLine trim code)
- local: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/webview/index.js (spinnerVerbs handling)
- local: /Users/ryaneskuri/.vscode/extensions/anthropic.claude-code-2.1.289-darwin-arm64/claude-code-settings.schema.json
- local: /private/tmp/claude-501/-Users-ryaneskuri-portfolio-projects-Harness/3156a81b-9c44-4695-be43-fa1ce35e5f57/scratchpad/embedded-claude-code.d.ts (types extracted from the 2.1.289 binary)
- local: /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/user/hooks/notify.py
- local: /Users/ryaneskuri/portfolio_projects/Harness/claude-code-starter-macos/project/.claude/agents/reviewer.md
