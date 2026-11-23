# Loading a branch's plugin for one session (`--plugin-dir`)

Checked 2026-11-23 on this Mac, Claude Code 2.1.330, for the loop smoke run (#427).

## Run

From the sandbox clone (`~/portfolio_projects/harness-sandbox`), with `P` = a Harness worktree's `plugins/harness`:

```
PATH="$P/bin:$PATH" claude -p "<run: command -v harness-board harness-pr; echo PATHHEAD=${PATH%%:*}>" \
  --permission-mode auto --output-format stream-json --verbose --max-turns 3 \
  --plugin-dir "$P" --settings '{"enabledPlugins": {"harness@ryan": false}}' < /dev/null
```

## Findings

- The `system/init` event listed one harness plugin: `{"name": "harness", "path": "<worktree>/plugins/harness", "source": "harness@inline", "version": "0.2.0"}`. The installed `harness@ryan` was absent, so with it disabled the branch's copy is the only one loaded, with no name clash.
- Its skills (`harness:work`, `harness:land`, ... 10) and agents (`harness:coder`, `harness:reviewer`, ... 7) were listed, and three `SessionStart:startup` hooks ran, one returning the harness session brief.
- `command -v harness-board harness-pr` resolved both to `<worktree>/plugins/harness/bin/`, which was PATH's first entry: the `PATH` the launcher sets reaches the session's Bash tool. (The run set it explicitly, so this does not show whether `--plugin-dir` alone puts `bin/` on PATH; `loop_smoke.py run` puts it first either way.)
- `apiKeySource` was `none`, with no `ANTHROPIC_API_KEY` in the environment and no `apiKeyHelper` in the settings: `claude -p` ran on the claude.ai login, the subscription. The harness's own headless dispatch (`harness-work dispatch --venue headless`) adds `{"apiKeyHelper": HARNESS_API_KEY_CMD}`; `loop_smoke.py run` does not.
- stderr: `Ignoring 24 permissions.allow entries from .claude/settings.json: this workspace has not been trusted.` A `claude -p` session in a folder that has never had the interactive trust dialog accepted runs without the project's allow rules, so under `--permission-mode auto` every call they would cover (`harness-pr land` among them, which the classifier has denied as `[Merge Without Review]`) goes to the classifier. The sandbox's one-time setup therefore includes running `claude` there once interactively and accepting the trust dialog.

## For `loop_smoke.py run`

`--plugin DIR` passes `--plugin-dir DIR --settings '{"enabledPlugins": {"harness@ryan": false}}'` and puts `DIR/bin` first on PATH, as the ticket's Outline 3(c) says; no adjustment was needed.
