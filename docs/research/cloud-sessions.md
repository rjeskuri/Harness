# Research notes: Claude cloud sessions

Gathered 2026-10-14 and re-checked 2026-10-26 (Claude Code 2.1.296, then 2.1.310) for #58, from the docs at code.claude.com (`claude-code-on-the-web`, `cloud-environments`, `headless`, `routines`), the changelog, the local binary's strings and cached feature flags. The two needs-input comments on #58 hold the full findings; this note keeps what `harness-work cloud` relies on. No cloud session or routine was created for the research.

## Verified

- **Starting a new session needs a terminal.** The 2.1.310 binary: "Without an interactive terminal, --cloud can only send the prompt to an existing cloud session ... To start a new cloud session, run from a TTY." The `-p` path for new sessions is compiled off (cloudPrintEnabled returns false). `harness-work cloud` therefore gives `claude --cloud` a pseudo-terminal for its stdout.
- **`--on-branch <branch>`** exists in the 2.1.310 binary, hidden from `--help`: the session starts on that branch, which must already be pushed.
- **What a successful launch prints:** `Created cloud session: <title>` on stdout (binary strings), then the command exits.
- **Follow-ups without a terminal:** `claude -p "msg" --cloud <session_id> --output-format json` works for an existing session only; it returns `{ok, session_id, url}` without waiting for a reply.
- **No plugins.** `cloud-environments`: plugins from the repository's or the user's settings are not loaded ("No"). The repository's CLAUDE.md, `.claude/settings.json` hooks and `.claude/skills|agents|commands` are. The harness lives under `plugins/harness/`, so `/harness:work`, harness-board, harness-pr, harness-red and the role hooks are all missing there. A flag-gated, undocumented `/cloud-plugins` forwards only github, git and url marketplaces, not the directory marketplace `harness@ryan`.
- **GitHub from the cloud:** the GitHub proxy returns 403 for GraphQL, so `gh issue` and `gh pr` fail; `gh api` REST works.
- **Billing:** the subscription (and, until 2026-11-05, Ryan's separate cloud-session credit); API keys are refused.
- **Tracking:** `claude agents --json` does not list cloud sessions; there is no public status API for scripts. The run's outcome comes back as marker comments on the ticket.
- **Seed bundle (seen 2026-10-26 on six hand-launched runs):** a session created before the Claude GitHub integration was connected for the repository is "Cloned from seed bundle": it has no `origin`, and the git proxy answers 403 "not in this session's authorized repository set" to a push, so it can never push. Connecting the integration later does not fix a session that is already running. Check the session page for "Cloned from seed bundle" before trusting a run; the launch prompt makes the session check `git remote get-url origin` and `git push --dry-run` first.

## Not verified

- The exact launch output after `Created cloud session:`. `harness-work cloud` takes the session id from the first `https://claude.ai/code/<id>` URL in it (the form of the session pages and of the `url` a follow-up returns) and treats output without one as a failed launch.

- The cloud environment settings on claude.ai have a setup script field described as "Runs when a new session is started, before Claude Code launches. The result is cached so future sessions start quickly." It is a candidate way to clone the public Harness repository and put its `bin/` on PATH in the cloud; the launch does not rely on it.
