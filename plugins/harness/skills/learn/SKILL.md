---
name: learn
description: Turn lessons from the current session into durable fixes in CLAUDE.md, .claude/rules/, skills, hooks or permission rules, proposed as diffs for approval. Use when the user runs /harness:learn, or at the end of a session that needed repeated corrections.
disable-model-invocation: true
---
# Compound what this session taught us

Each correction the user had to make is a chance to make the next session better.

First, in a repository with a GitHub origin, run `harness-pr learn --json` (it lists the PRs merged and the tickets parked since the last learn run, or the last 7 days). Without a GitHub origin, skip it and the closing `--mark` below, and say so. Its PR findings (`prs[].findings`), PR comments (`prs[].comments`: threads, reviews, comments) and Parked comments (`parked`) are sources next to this conversation. Group the repeats across all of them: the same kind of mistake, or the same file area, and say how often each was seen, with the PR and ticket numbers. A single occurrence is not a proposal on its own unless this conversation shows it too.

Then review this conversation and list:

- **Corrections**: places the user redirected you or rejected an approach.
- **Repeats**: instructions the user gave more than once.
- **Surprises**: facts about the code, data, tooling or environment you had to discover the hard way.
- **Friction**: commands that failed, permission prompts that kept recurring, slow feedback loops.

For each finding, choose the lightest fix that would have prevented it:

1. A one-line addition or edit to the project's `CLAUDE.md`, only if it matters in almost every session. Something true across all projects belongs in `~/.claude/CLAUDE.md` instead.
2. A line in a path-scoped `.claude/rules/*.md` file, if it only matters for certain files.
3. A skill, if it is a multi-step procedure the user will want again. A procedure useful in every project belongs in the harness plugin.
4. A hook or permission rule, if it must happen (or must never happen) every time, whatever the judgment call.
5. A test or lint rule, if the mistake lives in the code itself.

Also flag lines in CLAUDE.md or the rules that this session showed to be stale, wrong or ignored, and propose deleting or tightening them.

Present the proposals as a short numbered list of diffs, each with one line of reasoning (for a repeat, how often and where it was seen). For the ones the user approves, file a `chore` ticket each with `harness-board task create --type chore --no-tests --size S --risk low --proposed --title ... --body-file ...` (body: the proposed diff in a fenced block and the reasoning; `--no-tests` only when the change is docs or config), so the change waits for the user's `harness-board promote` and then goes through a branch and a pull request like any other. Nothing is edited on the spot. Without a board (no remote), show the diffs and let the user apply them. Prefer deleting and tightening over adding: these files stay effective only while they stay short.

When the user has answered the proposals (approved, declined or none found), run `harness-pr learn --mark` as the last step, so the next run starts from now.
