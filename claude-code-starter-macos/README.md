# Claude Code starter kit for macOS: Python, Jupyter and SQL

A small, opinionated `.claude/` setup to drop into a repo. It follows the Agentic Coding Playbook: a lean CLAUDE.md, path-scoped rules, hooks for the things that must always happen, skills for workflows you repeat, one fresh-context reviewer, and Claude Code's macOS sandbox. Checked against Claude Code 2.1.283 (September 2026).

```
project/                          -> copy into your repo root
  CLAUDE.md                       lean project memory with <placeholders>; about 40 lines
  .worktreeinclude                gitignored files (.env) copied into each worktree Claude creates
  gitignore-additions.txt         lines to append to .gitignore
  .claude/
    settings.json                 permissions (allow / ask / deny) and hook wiring
    hooks/
      notebook_guard.py           PreToolUse: redirects edits of paired .ipynb files to the jupytext .py
      format_python.py            PostToolUse: ruff format and safe fixes on each edited .py file
      verify_on_stop.py           Stop: ruff and pytest on changed code; sends failures back to Claude
    rules/                        load only when Claude touches matching files
      python.md  notebooks.md  sql.md  tests.md
    skills/
      spec/        /spec <idea>   interview, then write docs/agent/specs/<date>-<slug>.md
      handoff/     /handoff       write docs/agent/handoff.md before /clear
      ship/        /ship [pr]     verify, fresh-context review, commit, optional PR
      learn/       /learn         turn this session's corrections into CLAUDE.md, rule or hook diffs
      verify-data/ (automatic)    data-change checklist Claude loads when relevant
    agents/
      reviewer.md                 read-only diff reviewer that reports correctness findings only
user/                             -> merge into ~/.claude/
  CLAUDE.md                       personal preferences for every project, including macOS quirks
  settings.json                   the macOS sandbox, plus a notification when Claude waits on you
  hooks/notify.py
```

## Prerequisites

- Xcode command line tools: `xcode-select --install`. They provide `git` and the `python3` the hooks run on.
- [Homebrew](https://brew.sh), then `brew install uv gh`.
- In each project, ruff and pytest: `uv add --dev ruff pytest`. For a global ruff instead, `uv tool install ruff`.
- Recommended:
  - `jupytext` in projects with notebooks.
  - `brew install terminal-notifier` for notifications you can click to bring VS Code forward.
  - The Pyright code-intelligence plugin: `/plugin install pyright-lsp@claude-plugins-official`. It needs `pyright-langserver` on PATH (`brew install pyright` or `npm i -g pyright`) and gives Claude type errors right after each edit.

## Install

1. Copy everything in `project/` into your repo root. If you already have a `.claude/`, merge folder by folder; for `settings.json`, merge the keys.
2. Append `gitignore-additions.txt` to your `.gitignore`.
3. Edit `CLAUDE.md`: fill in the placeholders and delete what doesn't apply (the data-access line, the notebook lines if you don't use jupytext). Adjust `.claude/rules/sql.md` to your SQL conventions.
4. Install the personal layer:
   ```sh
   mkdir -p ~/.claude/hooks
   cp user/hooks/notify.py ~/.claude/hooks/
   cp user/CLAUDE.md ~/.claude/CLAUDE.md   # merge by hand instead if you already have one
   ```
   Then merge `user/settings.json` into `~/.claude/settings.json`.
5. Without terminal-notifier, allow AppleScript notifications once: run `osascript -e 'display notification "test"'`, then turn on Script Editor under System Settings > Notifications.
6. Check it:
   - `claude doctor` in the repo validates every settings file and names any invalid rule or hook.
   - Start `claude` in the repo once and accept the workspace-trust prompt. Project hooks don't run until the folder is trusted.
   - In a session: `/hooks` should list PreToolUse, PostToolUse, Stop and Notification; `/skills` should list spec, handoff, ship, learn and verify-data; `/sandbox` shows the sandbox status; `/context` shows what loads at startup.
7. Smoke test: ask Claude for a small change that breaks a test on purpose. Before it finishes, the Stop hook should hand it the failure; once fixed, you'll see `verify_on_stop: ruff and pytest passed.`

## The sandbox

`user/settings.json` turns on Claude Code's sandbox. On macOS it uses the built-in Seatbelt framework, so there's nothing to install.

- Bash commands can write only to the project, the temp directory and uv's cache (`~/.cache/uv`), and reach only PyPI and GitHub.
- In auto-allow mode, commands that fit inside those limits run without a permission prompt.
- A command that needs more (another host, a write elsewhere) prompts you, or retries outside the sandbox with your approval. The error names what was blocked.
- Ask rules still prompt (`git push` does), and deny rules still hold.
- Hooks run outside the sandbox, so the kit's hooks are unaffected.

Add hosts to `sandbox.network.allowedDomains` and paths to `sandbox.filesystem.allowWrite` as your projects need them, or delete the `sandbox` block to turn it off.

## How the hooks behave

| Hook | Runs | Does | On its own errors |
|---|---|---|---|
| notebook_guard | before Edit, Write, NotebookEdit | Denies edits to an `.ipynb` that has a jupytext-paired `.py` and tells Claude to edit the `.py` and sync. No pair means no effect. | exits 0 |
| format_python | after Edit, Write | `ruff format` plus safe `ruff check --fix` on that one file. Unused-import removal is off, so imports added before their first use survive. It never blocks, and it records the edit for the Stop check. | exits 0 |
| verify_on_stop | when Claude tries to end a turn | Only if Claude edited Python this session and the code changed since the last green run: `ruff check` on the files Claude edited (not your other work in progress), then fast pytest. Failures go back to Claude as a block reason. After 2 consecutive blocks it lets Claude stop and warns you. On success it prints a one-line pass message. | exits 0 |
| notify (user) | Claude waits on a permission prompt or goes idle | A macOS notification naming the repo that's waiting: terminal-notifier if installed, else AppleScript, else the terminal bell. Edit `SOUND` and `ACTIVATE_APP` at the top of the script. | exits 0 |

The project hooks run as `python3` in exec form, without a shell, and use only the standard library, so Xcode's `/usr/bin/python3` or Homebrew's both work. To run them on uv's interpreter instead, set `"command": "uv"` and `"args": ["run", "--no-project", "python", "${CLAUDE_PROJECT_DIR}/.claude/hooks/<script>.py"]`.

Tune `verify_on_stop` with environment variables in the `env` block of `.claude/settings.json`, or of `.claude/settings.local.json` for yourself only:

```json
"env": {
  "CLAUDE_VERIFY_CMD": "uv run pytest -q -x tests/unit",
  "CLAUDE_VERIFY_TIMEOUT": "180",
  "CLAUDE_VERIFY_LINT": "1"
}
```

## Permissions

- **allow**: routine, low-risk commands (uv, pytest, ruff, pyright, git add and commit, read-only gh). Read-only commands such as `git status` and `git diff` are already auto-approved, so they aren't listed.
- **ask**: anything visible to others or hard to undo: `git push`, `git reset --hard`, `git clean`, PR creation and merge, dependency changes, `brew install`. Ask rules prompt even in auto mode and inside the sandbox.
- **deny**: reading or editing `.env` and `secrets/`, hand-editing `uv.lock`, `data/raw/` and `tests/golden/`, and force pushes. Deny rules hold in every mode.

After a week of real use, run `/fewer-permission-prompts`. It reads your transcripts and proposes an allowlist of the safe commands you actually run.

The kit doesn't set a `defaultMode`. Recent Claude Code versions start interactive sessions in auto mode, and a project settings file can't switch you into auto mode anyway; that's a personal choice for `~/.claude/settings.json`.

## Troubleshooting

- **Hooks never fire**: trust the folder (start `claude` there interactively once), then check `/hooks`. Start with `claude --debug hooks` to see each run with its exit code and output.
- **Hooks can't find `uv` or `ruff`**: they search the PATH Claude Code was started with. If VS Code was opened from the Dock, open it from a terminal with `code .` and compare, or rely on the project's `.venv`, which the hooks check first.
- **No notifications**: allow Script Editor (or terminal-notifier) under System Settings > Notifications, and check that a Focus mode isn't hiding them.
- **A command fails inside the sandbox**: the error names the blocked host or path. Add it to `allowedDomains` or `filesystem.allowWrite`, or approve the retry outside the sandbox.
- **The Stop check is too slow**: point `CLAUDE_VERIFY_CMD` at a fast subset, and keep the full suite for `/ship` or CI.
- **Pause every hook for a session**: add `"disableAllHooks": true` to `.claude/settings.local.json`, and remove it afterwards.
