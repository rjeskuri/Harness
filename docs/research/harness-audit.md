# Research notes: harness audit against outside practice, its own wording and its process cost

Gathered 2026-10-14 for ticket #76. Outside practice: a `harness:researcher` agent read primary sources only (Anthropic's Claude Code docs, engineering posts and model prompting guides, and published harnesses and posts by named developers); every page was fetched 2026-10-14, and the code.claude.com and platform.claude.com pages show no "last updated" date. Process cost: an Opus agent read the session transcripts, the harness attempt records, git and the GitHub PRs of seven landed tickets (#50, #57, #64, #65, #66, #67, #69). Constraint audit and size: an Opus coder agent read every prompt file in this repository at commit 6634f15 and ran `claude plugin details harness`. Quotes are the sources' words; verdicts and the shortlist are the agents' judgment, to be read by Ryan before use.

## 1. Summary

The harness's deterministic core is what the sources recommend, and the data backs it: hooks that deny instead of prose that asks, a test-edit block for the coder, a red proof before source changes, a reviewer in a fresh context, a worktree per ticket. The wording is not shouty: no prompt file uses IMPORTANT, CRITICAL, MUST, NEVER or ALWAYS in capitals, and the always-on load is about 3,600 tokens. Where the harness constrains the model more than it needs to is elsewhere: the same rule stated in four to six places, procedures written into prose that a tool should carry, a review policy that makes every low finding mandatory, and effort settings above the model's default that were never measured. The process cost data says the money goes to the orchestrator's accumulated context and to fix rounds, not to the test-first chain itself. The largest gap is measurement: there is no cost-per-ticket number, no eval of the harness, and no loop from review findings back into the prompts.

Top five:

1. **One ticket per session.** The orchestrator is 30% of ticket cost ($16.96 of $55.90): about 292k tokens of cached context are re-read on every call, because twelve tickets ran in one 8.6-hour session.
2. **Fix what matters, not every finding.** Fix rounds cost $10.41 (19%), 1.8 times the first reviews, for 26 findings of which none was high and 19 were low. #65 spent $3.11 (30% of the ticket) fixing three low findings. The reviewer's own `VERDICT: ready` (no high or medium) is overridden by the work skill's "whatever its severity".
3. **Say each rule once, where it applies, and let the tool carry the procedure.** Reviewer routing is written four times, yet the recorded review tier differed from the model that ran on three of seven tickets; "never loosen a test" appears six times; the work skill is 1,936 words, with a 284-word paragraph of `gh api` steps.
4. **Keep the deterministic parts and the breaker.** The breaker loop is 19% of cost but found real code cheats on three of six tickets (#50, #64, #69). What it should stop doing is chasing regex wording checks on prose (#57, #67).
5. **Add measurement before more rules.** The token-cost metric (already picked by Ryan), then `claude plugin eval` cases drawn from real failures, a learning loop from review findings, measured effort levels, and a re-check on each model release.

## 2. Outside practice

### Rule text and emphasis

- Length: "Keep it concise. For each line, ask: 'Would removing this cause Claude to make mistakes?' If not, cut it. Bloated CLAUDE.md files cause Claude to ignore your actual instructions!" ([best-practices](https://code.claude.com/docs/en/best-practices), fetched 2026-10-14). "target under 200 lines per CLAUDE.md file. Longer files consume more context and reduce adherence." ([memory](https://code.claude.com/docs/en/memory), fetched 2026-10-14). HumanLayer's root CLAUDE.md "is less than sixty lines" ([Writing a good CLAUDE.md](https://www.humanlayer.dev/blog/writing-a-good-claude-md), Nov 25, 2025).
- Emphasis: "If Claude keeps skipping one instruction, add emphasis such as 'IMPORTANT' to that line alone. If you emphasize many lines, none of them stands out." ([best-practices](https://code.claude.com/docs/en/best-practices)). "Where you might have said 'CRITICAL: You MUST use this tool when...', you can use more normal prompting like 'Use this tool when...'." ([Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices), fetched 2026-10-14). "If you find yourself writing ALWAYS or NEVER in all caps, or using super rigid structures, that's a yellow flag — if possible, reframe and explain the reasoning." ([skill-creator SKILL.md](https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/SKILL.md), last commit 2026-03-06).
- Emphasis on purpose is still used for the one rule that matters: Anthropic's long-running harness used "strongly-worded instructions like 'It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality.'" ([Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), Nov 26, 2025). Anthropic's code-review plugin uses capitals on two lines ("**CRITICAL: We only want HIGH SIGNAL issues.**") (anthropics/claude-code/plugins code-review, last commit 2026-03-12).
- Explain why, say what to do: "Providing context or motivation behind your instructions ... Claude is smart enough to generalize from the explanation." "Tell Claude what to do instead of what not to do." "Prefer general instructions over prescriptive steps ... Claude's reasoning frequently exceeds what a human would prescribe." ([Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)). The skills page leans the other way on length: "every line is a recurring token cost. State what to do rather than narrating how or why." ([skills](https://code.claude.com/docs/en/skills), fetched 2026-10-14). The two reconcile as: one short reason per rule, no narration.
- Altitude: "At one extreme, we see engineers hardcoding complex, brittle logic in their prompts." "teams will often stuff a laundry list of edge cases into a prompt ... We do not recommend this." ([Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), Sep 29, 2025). "Match the level of specificity to the task's fragility and variability", low freedom when "A specific sequence must be followed" ([Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices), fetched 2026-10-14).
- Contradiction and duplication: "if two instructions contradict each other, Claude may pick one arbitrarily." Between user and project files: "Claude may follow either one, so keep the two consistent." Procedures belong elsewhere: "If an entry is a multi-step procedure or only matters for one part of the codebase, move it to a skill or a path-scoped rule instead." ([memory](https://code.claude.com/docs/en/memory)).
- Older-model wording: `/doctor prompt-audit` "looks for problems such as instructions written for older models, references to files or commands that don't exist, and files that contradict each other", across "rules, skills, commands, subagents, and output styles" ([memory](https://code.claude.com/docs/en/memory)).

### Hooks versus prose

- "Unlike CLAUDE.md instructions which are advisory, hooks are deterministic and guarantee the action happens." "If Claude already does something correctly without the instruction, delete it or convert it to a hook." ([best-practices](https://code.claude.com/docs/en/best-practices)).
- "An instruction like 'never edit `.env`' in CLAUDE.md or a skill is a request, not a guarantee ... If a rule must hold every time, make it a hook rather than a prompt instruction." Hook context cost: "Zero unless the hook returns output." ([features-overview](https://code.claude.com/docs/en/features-overview), fetched 2026-10-14).
- "A real guardrail needs to be deterministic." ([Steering Claude Code](https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more), Michael Segner, June 18, 2026). "The skill makes violations rare and the hook makes them close to impossible." ([The AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook), Louis Claxton, August 21, 2026).
- Tools over prompts: "My custom tools are normal scripts that it just runs." ([Agentic Coding Recommendations](https://lucumr.pocoo.org/2025/6/12/agentic-coding/), Armin Ronacher, June 12, 2025). "Never send an LLM to do a linter's job." ([HumanLayer](https://www.humanlayer.dev/blog/writing-a-good-claude-md), Nov 25, 2025).

### Verification and review chains

- Verification is the top lever, with four gating levels, two of which are "As a deterministic gate: a Stop hook" and "By a second opinion: a verification subagent ... so the agent doing the work isn't the one grading it." ([best-practices](https://code.claude.com/docs/en/best-practices)).
- Self-verification on current models: "Claude Opus 5 verifies its own work without being told to. If your prompt contains explicit verification instructions ... remove them: instructions like these cause over-verification ... The same applies to legacy harness scaffolding that adds separate verification steps." "Avoid instructing re-checks it already performs." ([Prompting Claude Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5), fetched 2026-10-14; the Opus 5.5 page says its patterns "remain a reasonable starting point").
- Separate reviewer: "A fresh context improves code review since Claude won't be biased toward code it just wrote." ([best-practices](https://code.claude.com/docs/en/best-practices)). "Separating the agent doing the work from the agent judging it proves to be a strong lever." ([Harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps), Mar 24, 2026). "Separation of duties is preserved, because the agent that wrote the code has no way to approve it." ([AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook), Aug 21, 2026).
- Over-review: "A reviewer prompted to find gaps will usually report some, even when the work is sound ... Chasing every finding leads to over-engineering ... Tell the reviewer to flag only gaps that affect correctness or the stated requirements, and treat the rest as optional." ([best-practices](https://code.claude.com/docs/en/best-practices)). But do not filter in the reviewer's prompt: "If your review prompt says 'only report high-severity issues' ... the model may follow that instruction literally and report less; ask it to report everything and filter in a separate pass instead." ([Prompting Claude Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)). The playbook's review policy: "Report at most five nits per review"; "Do not report ... anything CI already enforces." ([AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook)). Anthropic's code-review plugin "Filters out issues below 80 confidence threshold" and lists "Pedantic nitpicks" as false positives (code-review plugin, 2026-03-12).

### Test-first development

- "have one Claude write tests, then another write code to pass them." ([best-practices](https://code.claude.com/docs/en/best-practices)). "write the failing test first ... confirm it fails for the reason you expect. Commit that test. Only then ask Claude to make it pass without editing the test, with the test-file hook ... enforcing the restriction." "an agent fixing code must not be able to weaken the check on that code." ([AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook)).
- Test gaming: Claude may "focus too heavily on making tests pass at the expense of more general solutions"; the suggested prompt: "Tests are there to verify correctness, not to define the solution", and report "if any of the tests are incorrect" ([Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)).
- Volume: Fable "may ... commit more test files than the change warrants"; fix prompt "Commit tests only where the task asks for them" ([Prompting Claude Fable 5.1](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1), fetched 2026-10-14).
- Practitioners: obra/superpowers' TDD skill says "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST" (capitals; last commit 2026-09-19, https://github.com/obra/superpowers). Claude Code's own common-workflows page has no TDD recipe ([common-workflows](https://code.claude.com/docs/en/common-workflows)); Anthropic's feature-dev plugin has no test-first phase (2025-10-17).

### Delegation and cost

- "agents typically use about 4× more tokens than chat interactions, and multi-agent systems use about 15× more tokens than chats." "token usage by itself explains 80% of the variance." "most coding tasks involve fewer truly parallelizable tasks than research" ([Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), Jun 13, 2025).
- "Delegation pays off on genuinely independent, sizeable tracks of work, but it multiplies cost and time when applied to small tasks." Example instruction: "do not use subagents to verify or double-check your own work" ([Prompting Claude Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)).
- "each subagent spends tokens of its own while it runs"; "a fork costs less than a fresh subagent for tasks that need the same context"; subagents load CLAUDE.md "unless you set `omitClaudeMd`" ([sub-agents](https://code.claude.com/docs/en/sub-agents), fetched 2026-10-14).
- Effort: "Start at `medium`, the default on Claude Opus 5.5 ... test several levels against your own evals." "Reserve `xhigh` and `max` for work where you've measured a quality gain." ([Prompting Claude Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5), fetched 2026-10-14). "Sonnet handles most coding tasks well and costs less than Opus." ([costs](https://code.claude.com/docs/en/costs), fetched 2026-10-14).
- A full harness can be worth it: solo agent 20 min and $9 against the full harness at 6 hours and $200, "over 20x more expensive", with clearly better output ([Harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps), Mar 24, 2026).
- Simplest first: "we recommend finding the simplest solution possible, and only increasing complexity when needed." ([Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), Dec 19, 2024). "If you could describe the diff in one sentence, skip the plan." ([best-practices](https://code.claude.com/docs/en/best-practices)).

### Context

- "Claude's context window fills up fast, and performance degrades as it fills." "After two failed corrections, `/clear` and write a better initial prompt." ([best-practices](https://code.claude.com/docs/en/best-practices)). HumanLayer keeps "utilization in the 40%-60% range" and says "Subagents are about context control" ([advanced context engineering](https://github.com/humanlayer/advanced-context-engineering-for-coding-agents/blob/main/ace-fca.md), no date, refers to Aug 2025). A subagent "returns only a condensed, distilled summary of its work (often 1,000-2,000 tokens)" ([Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), Sep 29, 2025).

### Evals

- "When we evaluate 'an agent,' we're evaluating the harness and the model working together." "20-50 simple tasks drawn from real failures is a great start." Step checks make "overly brittle tests ... it's often better to grade what the agent produced, not the path it took." "You won't know if your graders are working well unless you read the transcripts." ([Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), Jan 9, 2026).
- "The `claude plugin eval` shell command runs your plugin against a suite of test cases and scores the results." Each case runs three times; "If a case scores 1.0 both with and without the plugin, the plugin isn't what made it pass." Graders `regex`, `tool_used`, `tool_order`, `file_exists` "cost nothing"; `llm` and `baseline` call a judge. "Pin it in CI so a model rollout isn't mistaken for a plugin regression." "Every eval run and every judge grader is a real model call on your account." ([plugin-evals](https://code.claude.com/docs/en/plugin-evals), fetched 2026-10-14).
- "The suite runs non-interactively in CI on a schedule and on any change to CLAUDE.md, skills or hooks ... Gate configuration changes on the results." "Each production incident gets an eval." ([AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook)). "If you didn't watch an agent fail without the skill, you don't know if the skill teaches the right thing." (obra/superpowers writing-skills, 2026-09-19). "Create evaluations BEFORE writing extensive documentation." ([Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)).

### Learning loops

- "When Claude makes a mistake twice, the correction goes into CLAUDE.md." "When a review flags a mistake for the second time, the correction goes into CLAUDE.md as part of that review." ([AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook)). "A repeated mistake or a recurring review comment is a CLAUDE.md edit, not a one-off correction in chat." ([features-overview](https://code.claude.com/docs/en/features-overview)). "Update CLAUDE.md based on recurring review patterns." (code-review plugin, 2026-03-12).
- "anytime you find an agent makes a mistake, you take the time to engineer a solution such that the agent never makes that mistake again." ([My AI Adoption Journey](https://mitchellh.com/writing/my-ai-adoption-journey), Mitchell Hashimoto, Feb 5, 2026). Every's loop is "plan, work, review, compound", with learnings "built up in a mostly automated fashion" ([compound engineering](https://every.to/chain-of-thought/compound-engineering-how-every-codes-with-agents), Dec 11, 2025).
- Re-check on model release: "every component in a harness encodes an assumption about what the model can't do on its own." "when a new model lands ... re-examine a harness, stripping away pieces that are no longer load-bearing." The sprint construct was removed on Opus 4.6 and context resets on Opus 4.5 ([Harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps), Mar 24, 2026).
- `/insights` reports "friction points such as misunderstood requests or buggy code" ([costs](https://code.claude.com/docs/en/costs)); `/skill-doctor` "flags skills that are in Claude's skill listing but have never been invoked" ([plugins/measure](https://code.claude.com/docs/en/plugins/measure), fetched 2026-10-14).

### Asking versus deciding

- obra/superpowers: "A running plan does not wait on a human ... Record every decision ... `Ruling: <what you decided> — <why> — <what it costs if wrong>` ... a session parked on a question costs their whole day and buys nothing." It stops only for irreversible, security-sensitive or outside side effects, or a plan so broken every path is a guess (last commit 2026-09-19).
- Opus 5 "performs best when given the complete task specification up front and left to run." ([Prompting Claude Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)). Anthropic's feature-dev plugin asks up front and "Waits for your answers before proceeding" in its Phase 3 (2025-10-17). Harper Reed: "Ask me one question at a time" while writing the spec ([My LLM codegen workflow](https://harper.blog/2025/02/16/my-llm-codegen-workflow-atm/), 2025-02-16).
- Unattended runs: treat a text-only end of turn "as a report rather than as proof the task is done" ([Prompting Claude Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)).

### What the sources confirm is right here

| Harness piece | Source that backs it |
|---|---|
| Hooks deny (branch guard, tests-first, read-only guard) instead of prose asking | best-practices, features-overview, Steering Claude Code, AI-native SDLC playbook (all above) |
| Coder cannot edit tests; red proof before source | AI-native SDLC playbook ("the test-file hook ... enforcing the restriction"); best-practices (one Claude writes tests, another code) |
| Reviewer in a fresh context, read-only | best-practices; Harness design for long-running apps; playbook's separation of duties |
| Stop hook running the verify command | best-practices ("As a deterministic gate: a Stop hook") |
| A worktree and branch per ticket, one feature at a time | Effective harnesses for long-running agents ("work on only one feature at a time. This incremental approach turned out to be critical.") |
| `harness-work runs --sweep` marking `early` runs and redispatching | Prompting Claude Opus 5.5 (a text-only end of turn is a report, not proof) |
| The `learn` skill's "Prefer deleting and tightening over adding" | best-practices ("Ruthlessly prune"); skill-creator ("Keep the prompt lean") |
| The reviewer's "What does not" list | best-practices on chasing findings; code-review plugin's false-positive list |
| Hook deny reasons that name the next action | Prompting best practices ("Tell Claude what to do instead") |

## 3. Constraint audit

Verdicts: **redundant** (a hook or tool already enforces it; named), **keep** (a guardrail worth its tokens), **over-constraining** (absolute wording, a script where a goal would do, a rule fighting model defaults, duplication across files, or a contradiction). Lines not listed were read and nothing is flagged.

### Size

`claude plugin details harness` (run 2026-10-14) reports always-on **~1,527 tokens** for the plugin (skill and agent descriptions; hooks have no model context cost). Token figures for CLAUDE.md files are bytes / 4.

| File | Lines | Words | Bytes | Tokens always-on | Tokens on invoke |
|---|---|---|---|---|---|
| `user/CLAUDE.md` (= `~/.claude/CLAUDE.md`, identical) | 44 | 824 | 5,127 | ~1,280 | |
| `CLAUDE.md` (project; HTML comment stripped) | 39 | 374 | 2,477 | ~550 | |
| auto memory `MEMORY.md` (outside the repo) | 5 | | 1,054 | ~260 | |
| `agents/coder.md` | 27 | 471 | 2,708 | ~120 | ~770 |
| `agents/planner.md` | 31 | 503 | 3,209 | ~120 | ~920 |
| `agents/quick.md` | 9 | 117 | 666 | ~70 | ~130 |
| `agents/researcher.md` | 21 | 243 | 1,447 | ~90 | ~360 |
| `agents/reviewer.md` | 26 | 410 | 2,524 | ~110 | ~710 |
| `agents/test-author.md` | 28 | 504 | 2,971 | ~100 | ~880 |
| `agents/test-breaker.md` | 23 | 328 | 1,840 | ~130 | ~470 |
| `skills/handoff` | 22 | 218 | 1,387 | ~80 | ~370 |
| `skills/land` | 26 | 280 | 1,816 | ~100 | ~440 |
| `skills/learn` | 25 | 372 | 2,256 | ~80 | ~660 |
| `skills/new-project` | 14 | 248 | 1,696 | ~110 | ~430 |
| `skills/plan` | 23 | 426 | 2,729 | ~110 | ~780 |
| `skills/standup` | 16 | 250 | 1,681 | ~100 | ~400 |
| `skills/work` | 47 | **1,936** | 12,036 | ~100 | **~3,900** |
| `skills/worklog` | 19 | 271 | 1,773 | ~110 | ~420 |
| Hooks (7 scripts) | | | | 0 | only the text they return |

Always-on per main session: about 3,600 tokens (both CLAUDE.md files, MEMORY.md, plugin descriptions). Every custom subagent also loads both CLAUDE.md files, about 1,830 tokens, of which the routing, budget and across-projects sections (`user/CLAUDE.md:25-40`, about 650 tokens) only matter to the orchestrator. All of this is far under the 200-line guidance and a rounding error in dollars (the orchestrator re-reads ~292k tokens per call). The case for cutting is adherence and contradiction, not size. The one large file is the work skill: 1,936 words in 47 lines, with single lines of 284 (line 22), 181 (14), 167 (26) and 156 (17, 27) words.

Capitalized emphasis (IMPORTANT, CRITICAL, MUST, NEVER, ALWAYS) appears **zero** times across the CLAUDE.md files, agents and skills. Lowercase "never" appears 32 times; the work skill has 23 absolutes (never, must, always, only, exactly, whatever), `user/CLAUDE.md` 12.

Rules stated in more than one place:

| Rule | Where | Enforced by |
|---|---|---|
| Reviewer model and the `--tier` on its attempt | `user/CLAUDE.md:27`, `:29`; `skills/work:20`, `:26`; `docs/design.md` | nothing: prose only; recorded tier differed from the model on 3 of 7 tickets |
| Never delete, skip or loosen a test | `user/CLAUDE.md:23`; `CLAUDE.md:30`; `templates/CLAUDE.md:29`; `agents/coder.md:15`; `agents/quick.md:9`; `skills/work:33`; `verify_on_stop.py:60` | `tests_first.py` for coder and breaker; nothing for the main session |
| Fix every finding whatever its severity | `skills/work:14`, `:26`, `:27`, `:29`, `:47`; `docs/design.md` | `harness-pr ready` and `land` gate (severity-blind) |
| Do not guess product behavior, ask | `user/CLAUDE.md:12`; `agents/coder.md:19`; `agents/planner.md:20`; `skills/work:33` | nothing (judgment) |
| Never on main | `user/CLAUDE.md:15-17`; `CLAUDE.md:28`; `agents/coder.md:10`, `:17`; `skills/work:20`, `:33` | `branch_guard.py` |
| End states of a work run | `skills/work:14` and `:38-47` | `harness-work runs --sweep` (`early`) |

### `user/CLAUDE.md`

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 3-5 | "macOS ships BSD `sed`, `grep` and `date`: no `grep -P`, `sed -i` needs `''` ..." | keep | Facts Claude gets wrong and cannot infer; exactly what features-overview says CLAUDE.md is for ("gets a convention or command wrong twice"). |
| 6 | "show the evidence (the command you ran and its result) and say plainly what you could not verify" | keep | A reporting rule, not a re-verification instruction; Opus 5's warning is about telling the model to re-check, which this does not do. |
| 7 | "Ask before adding dependencies or doing anything else hard to undo ... Never put a secret on a command line" | keep | Not hook-enforced (`after_edit.py` scans files after the write, not command lines). Worth its tokens. |
| 11 | "Every piece of work is a GitHub issue on the project's board" | over-constraining (mild) | Absolute; contradicts the branch guard's own advice ("or git switch -c t/<n>-<slug> for untracked work", `branch_guard.py:71-72`). best-practices: "If you could describe the diff in one sentence, skip the plan." Say "Ticketed work ..." and name the light path. |
| 12 | "becomes `harness-board needs-input N --question ...`, never a guess" | keep, soften | Right for product behavior. For reversible implementation choices superpowers logs a `Ruling:` instead of parking the run. Data: 0 needs-input across the seven tickets, so it costs nothing yet; low priority. |
| 17 | "A hook denies edits, commits and pushes on main; do not look for a way around it." | redundant | `branch_guard.py` enforces it and its deny text gives the way forward. The fact is worth half a line; the admonition is not. |
| 18 | "Only I merge, push main, force-push or delete a branch" | keep | Merge is not hook-blocked (`branch_guard.py` lets `gh` run on main); this line and `land`'s `disable-model-invocation` are the guard. |
| 22 | "tests are written before code by `harness:test-author`, proven red by `harness-red check`, challenged by `harness:test-breaker`, and only then implemented by `harness:coder`. A hook enforces the roles" | redundant | The chain is the work skill's job and `tests_first.py` enforces the roles. Keep one clause ("a hook enforces test-first roles on ticket branches"). |
| 23 | "Never delete, skip or loosen a test to get green." | keep | The only guard for the main session: `tests_first.py`'s role rules key on `agent_type`, which the main session lacks. Anthropic's own harness worded this rule strongly on purpose (Effective harnesses, Nov 26, 2025). Keep it here, once. |
| 27 | "Default: Opus 5.5 at high effort, for the main session and every agent." | over-constraining | Fights the model default without a measurement: "Start at `medium`, the default on Claude Opus 5.5 ... test several levels against your own evals" (Prompting Claude Opus 5.5). |
| 27, 29 | "The reviewer's model comes from `harness-board tier N --stage review` ..." and "Standard reviews run on Opus 5.5. `harness:reviewer` runs on Fable when ..." | over-constraining (duplication) | The same rule twice in this file and twice more in `skills/work:20`, `:26`. It still failed: #66 and #67 recorded `tier: sonnet` for Opus reviews after the rule existed; #57 (before the rule) recorded `opus` for Fable reviews. Prose repetition does not make a procedure reliable; a tool that derives the tier does (memory: "move it to a skill"; best-practices: "convert it to a hook"). |
| 29 | "`harness-work attempt N review pass\|fail --tier <the output of harness-board tier N --stage review>`" | redundant once fixed | A command line the tool could compute itself (`attempt` knows the stage). |
| 30 | "A Workflow is yours to launch when it pays off ... Say what you launched and why." | keep | Goal wording with a reason, the form the prompting guide recommends. |
| 34 | "While the API budget is exhausted, `--api` and `--venue headless` are refused." | redundant | `harness-work dispatch` refuses; the line describes the tool. "report it, do not fight it" is the useful half. |
| 39-40 | "Write `docs/agent/handoff.md` with `/harness:handoff` ..." and "run `/harness:worklog` at the end of a working session" | over-constraining (contradiction) | Both skills set `disable-model-invocation: true`, which is for workflows "you want to trigger manually" (`docs/research/best-practices-efficiency.md:83`). Claude cannot run them; the lines should say "remind me to run". |
| 44 | Compact Instructions | keep | A documented feature, short. |

The auto memory's "Model routing rule" entry restates line 27 a fifth time (outside the repo).

### `CLAUDE.md` (project) and `templates/CLAUDE.md`

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 9, 12, 15-22 | purpose line, verify command, where things live, 3.9 compatibility | keep | Facts not inferable from the code. |
| 23-24 | "Ask before adding a dependency ..."; "Secrets come from the environment ..." | redundant | Repeats `user/CLAUDE.md:7`; both always load for Ryan. Fine in the template for a project whose owner lacks the user file. |
| 26-31 | "Tests first: write the tests the ticket names, prove them red with `harness-red check`, then change the source. Never loosen a test to make it pass." | over-constraining (duplication, soft contradiction) | Describes a single agent doing both steps, while `user/CLAUDE.md:22` and the work skill split them across roles. memory: "keep the two consistent." The text comes from `templates/CLAUDE.md:25-30`, so every scaffolded project carries it. |
| 34 | "`python3 -m unittest discover -s plugins/harness/tests` passes. Show the command and its output." | redundant | `verify_on_stop.py` runs it at every stop with edits, and `user/CLAUDE.md:6` already asks for evidence. |
| 38-39 | Compact Instructions | redundant | A subset of `user/CLAUDE.md:44`. Harmless. |

### Agents

**`coder.md`**

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 7 | "a hook denies you any edit to a test file. The design is settled in the ticket; a reviewer checks your work" | keep | Tells the agent what is enforced and why; cheaper than discovering it by a deny. |
| 10 | "(work only there, never in the main checkout)" | redundant | `branch_guard.py` denies edits on main. |
| 14 | "No speculative generality, no drive-by refactors, no new dependencies." | keep | Matches the Fable 5.1 guide's warning that the model "may fix nearby code, extend behavior the task didn't mention". |
| 15 | "Never delete, skip, loosen or special-case a test." | partly redundant | Delete, skip and loosen are denied by `tests_first.py` (including bulk git rewrites). "Special-case" is not visible to a hook and is worth keeping, better as the guide's own wording: "Tests are there to verify correctness, not to define the solution." |
| 16 | "Size cap: if the change needs more than about 5 files or 300 lines, stop" | keep | No tool enforces it; it routes to a split. |
| 17 | "Commit only: do not push, do not open a pull request, do not touch main." | partly redundant | "do not touch main" is `branch_guard.py`. "do not push" is a role rule hooks do not enforce (t/* pushes are allowed): keep. The commit format overlaps Claude Code's built-in git instructions; see `includeGitInstructions` in section 6. |
| 19 | "Do not guess product behavior." | redundant (duplication) | The subagent also loads `user/CLAUDE.md:12`. |

**`planner.md`**

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| frontmatter | `effort: xhigh` | over-constraining | "Reserve `xhigh` and `max` for work where you've measured a quality gain" (Prompting Claude Opus 5.5). Not measured here. |
| 16 | "Every task body uses the task template headings exactly" | keep | Load-bearing: `harness-board` parses `## ` headings into sections (`bin/harness-board:517`). |
| 17 | "the Context names the public functions, commands or endpoints the tests will call, with signatures" | keep | The test-author has only the ticket; this is what makes test-first work. |
| 18 | label rules | keep | They drive `harness-board tier`. Worth adding: a change to prompt or doc text is `no-tests` (see section 4, item 3). |
| 20 | "Do not guess at product behavior." | redundant (duplication) | As above. |

**`quick.md`**: line 9, "Never delete or weaken tests, never touch secrets or files named as protected, and never run anything hard to undo." Keep: quick runs outside the ticket loop, where the role hooks do not apply. Three "never"s in one sentence could be one ("stop before anything a hook would not catch: tests, secrets, protected files, irreversible commands"), but nothing is wrong.

**`researcher.md`**: nothing flagged. Line 8 "never from memory alone" is the agent's job. Line 14 restates `readonly_bash.py` and the tools list, and says so itself.

**`reviewer.md`**

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 8 | "You review code you did not write. You see the diff and the ticket, not the reasoning" | keep | The fresh-context reviewer every source endorses. |
| 14-17 | what counts as a finding | keep | Correctness and requirements, as best-practices asks. |
| 20 | "Style, naming, formatting, speculative refactors ... Leave them out; chasing them makes code worse." | keep | Mirrors best-practices on chasing findings. |
| 26 | "At most 10 findings." | keep | A cap, not a severity filter; the playbook caps nits at five. |
| 26 | "`VERDICT: ready` (no high or medium) or `VERDICT: fix`" | over-constraining (contradiction) | The work skill fixes every finding "whatever its severity" (`skills/work:14`, `:26`, `:27`, `:47`) and `harness-pr ready` refuses any finding without a newer commit. The reviewer's ready verdict is ignored. Opus 5's guide wants the reviewer to report everything and a separate pass to filter; best-practices says treat non-correctness gaps as optional. The filter should be severity, applied by the orchestrator and the gate. |

**`test-author.md`**

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 7 | "A hook denies you any edit outside test files; that is the point." | keep | Explains why. |
| 14, 16 | "a table or loop of varied inputs so a hard-coded answer cannot pass"; "include for each input dimension at least one case whose expected value changes with it. A test that would pass for `return 42` is not finished." | keep, add proportion | Right goal, and the breaker still found real cheats on three of six tickets. But nothing says "as many tests as the definition of done needs": tests outnumbered code 7.1 to 1 (2,769 to 388 lines), 16.6 to 1 on #57. The Fable guide warns of "more test files than the change warrants". |
| 18 | "They must fail for the right reason" | keep | `harness-red check` enforces red and records the kind; "the right reason" is the judgment it cannot make. |
| 19 | "Write no implementation, stubs or placeholder modules." | redundant | `tests_first.py` denies the author any source edit (`AUTHOR_ON_SOURCE`). |
| (missing) | | addition | Nothing tells the author that prose cannot be tested by regex. The handoff's lesson: "Regex wording checks on skill/doc text are gameable every time (#57, #67)". |

**`test-breaker.md`**: nothing over-constraining. Line 14, "Spend at most about ten tool calls. A cheat that needs real logic is not a cheat; stop there.", is a good budget: breaker active time was 1 to 6 minutes per ticket.

### Skills

All eight skills set `disable-model-invocation: true`, yet their descriptions carry model triggers ("or when the context window is getting full", "or asks to merge a PR", "at the end of a working session"). Claude cannot act on these; they cost about 10 to 30 tokens each and mislead nothing, so this is cosmetic. The `/skill-doctor` report ([plugins/measure](https://code.claude.com/docs/en/plugins/measure)) would show which are never used.

- **`handoff`**: nothing flagged.
- **`land`**: line 26, "Never merge without the confirmation, never force, never bypass red checks." "never force" is redundant (`branch_guard.py` denies force pushes on every branch; `harness-pr land` has no force path); "never bypass red checks" is redundant (`harness-pr land` refuses red checks and the review gate). The confirmation is the guard worth keeping.
- **`learn`**: nothing over-constraining; line 25, "Prefer deleting and tightening over adding: these files stay effective only while they stay short", is exactly the sources' advice. Its gap: it reviews only the current conversation, not the review findings already on landed PRs (section 6).
- **`new-project`**: nothing flagged. Line 13, "Confirm the irreversible part explicitly", is the right kind of rule.
- **`plan`**: nothing over-constraining. Line 16 delegating wide reads to an Explore agent matches "Subagents are about context control" (HumanLayer).
- **`standup`**: nothing flagged.
- **`worklog`**: line 16, "Never change the hours of an auto row." Keep: billing data.
- **`work`**:

| Line | Quote | Verdict | Reason and source |
|---|---|---|---|
| 14 | "a dispatched run ... has no next turn: whatever is unfinished when the turn ends stays unfinished. Run every agent in the foreground" | keep | A real platform constraint, stated with its reason. |
| 14 | "Every review finding on the files the ticket touched is fixed before `harness-pr ready`, whatever its severity" | over-constraining | Stated five times in the file. Contradicts `reviewer.md:26`. best-practices: "Chasing every finding leads to over-engineering". Data: fix rounds $10.41 (19%), 0 high of 26 findings; #65 spent $3.11 on three low findings. |
| 14, 38-47 | the end states, then "Closing checklist ... Otherwise the turn is not over" | over-constraining (duplication) | The same end states twice, the second as an explicit self-check, which the Opus 5 guide asks to remove ("cause over-verification"). The tool-side backstop exists: `harness-work runs --sweep` marks `early` and redispatches. Keep one list. |
| 17 | dispatch paragraph (156 words) | redundant | Narrates `harness-work dispatch`'s own behavior (`--api` alias, budget refusal), which the tool enforces and prints. "Run `harness-work dispatch $ARGUMENTS`, show its plan or launches" is enough. |
| 20, 26 | tier, effort and `--tier` instructions | over-constraining (duplication) | As in `user/CLAUDE.md:27-29`. The tool should print `model` and `effort` and record the review tier itself. |
| 22 | resume from a draft PR: "from `gh api --paginate repos/<owner>/<repo>/pulls/<P>/reviews`, the review with the newest `submitted_at` whose body carries the `<!-- harness-review -->` marker ... with `<id>` the numeric `id`, not the `gh pr view` node id" | over-constraining (script in prose) | 284 words of API procedure in a prompt: "hardcoding complex, brittle logic in their prompts" (Effective context engineering). `harness-pr` already finds the latest harness review for its gate; `harness-work start --json` could return its findings. |
| 24 | "at most two rounds, then continue and note it for the PR" | keep | A cap. On S/low tickets one round would do (section 4). |
| 27 | "At most two fix rounds; findings still open after them fail the stage." | keep | A cap. |
| 33 | "Never loosen a test, never edit tests yourself, never push to main, never merge" | partly redundant | "never push to main" is `branch_guard.py`. "never edit tests yourself" and "never merge" are not hook-enforced for the main session: keep. |

### Hook prompts

Hooks cost "Zero unless the hook returns output" ([features-overview](https://code.claude.com/docs/en/features-overview)), and every text they return is short, says why, and names the next action. This is the strongest part of the harness's wording.

| Hook | Text returned to the model | Verdict |
|---|---|---|
| `branch_guard.py:70-81` | `ADVICE`, `FORCE_ADVICE`, `PUSH_ADVICE`: "main and master change only through a pull request: push the ticket branch and open one (harness-pr open <issue#>)." | keep |
| `tests_first.py:92-117`, `:399-418` | role refusals ("Tests are fixed for this ticket. If a test is wrong, stop and report it; the orchestrator sends it back to the test-author."), red-run refusals, `INLINE` | keep |
| `readonly_bash.py:538-542` | "The {role} only reads, searches, diffs and runs tests ... describe the change instead of making it." | keep |
| `verify_on_stop.py:59-62` | "Fix the root cause and re-run. Don't delete, skip or loosen tests. If a failure predates your change or is out of scope, say so explicitly." | keep: the test rule at the moment of temptation is the best place for it |
| `after_edit.py:188-191` | "SECRET pattern in ...: remove it before continuing; read secrets from the environment or a file that is gitignored." | keep (a label in capitals, not emphasis) |
| `session_brief.py:709` | `additionalContext` of at most 40 lines: facts only (branch, ticket, red run, board counts, budgets, next steps) | keep |
| `notify.py` | none (macOS notification only) | nothing to flag |

`docs/design.md` (413 lines, 11,390 words) is not loaded into any context; it is the contract the hooks and tools implement, and it is why so many prose rules are redundant.

## 4. Process cost

Seven landed tickets, all run on 2026-10-14 in one interactive main-checkout session (`4b318caa`, 00:07 to 08:46 UTC, which also worked on #43/#51, #54/#61 and #58). None was dispatched. Nothing was API-billed: they ran on the Max subscription, so dollars are API-equivalent at list price.

### Per ticket

| | #50 | #57 | #64 | #65 | #66 | #67 | #69 |
|---|---|---|---|---|---|---|---|
| Labels | bug S low | task M low | task M low | task M low | chore S low no-tests | task S low | bug S low |
| Tier | opus | opus | opus | opus | sonnet | sonnet | opus |
| Start to PR / ready / merge (min) | 43 / 49 / 54 | 50 / 89 / 132 | 55 / 64 / 117 | 53 / 81 / 81 | 35 / 44 / 45 | 54 / 71 / 98 | 40 / 46 / 46 |
| Agents | TA, TB, C, R(fable) | TA×2, TB, C, R(fable)×2 | TA, TB, C, R | TA, TB, C, R | C(sonnet), R | TA, TB, C (sonnet), R×2 | TA, TB, C, R |
| SendMessage continuations | 3 | 7 | 4 | 3 | 1 | 5 | 1 |
| Breaker rounds / result | 2 / real cheats | 2 / wording only | 2 / real cheat | 1 / none | n/a | 1 / wording only | 1 / real scope gap |
| Review rounds: findings (H/M/L) | 1: 0 | 2: 7 (0/2/5), 1 (0/0/1) | 1: 3 (0/1/2) | 1: 3 (0/0/3) | 1: 5 (0/1/4) | 2: 6 (0/3/3), 1 (0/0/1) | 1: 0 |
| Fix commits after review | 0 | 3 | 1 | 2 | 1 | 3 | 0 |
| CI runs (failed) | 1 (0) | 3 (0) | 2 (0) | 2 (0) | 2 (0) | 4 (0) | 1 (0) |
| Test / code / .md lines added | 309 / 18 / 4 | 746 / 45 / 24 | 491 / 73 / 3 | 803 / 216 / 2 | 0 / 0 / 23 | 227 / 13 / 18 | 193 / 23 / 1 |
| Cache read / write / output tokens | 10.8M / 0.44M / 41.7k | 27.5M / 1.33M / 72.9k | 18.9M / 1.00M / 37.0k | 20.9M / 1.03M / 49.9k | 7.6M / 0.40M / 14.6k | 16.4M / 0.69M / 29.3k | 12.6M / 0.51M / 18.7k |
| $ lower bound / scaled | 6.06 / 8.75 | 15.19 / 21.30 | 9.67 / 12.39 | 10.43 / 13.36 | 3.19 / 3.89 | 5.87 / 7.03 | 5.50 / 7.05 |

TA test-author, TB test-breaker, C coder, R reviewer. Agents ran on the ticket's tier unless shown. 0 escalations, 0 needs-input, 0 CI failures across the seven.

### By stage (transcript-priced lower bound, $)

| | Tests (incl. red) | TA re-rounds after breaker | Breaker | Coder | Review | Fix rounds | Orchestrator | Total |
|---|---|---|---|---|---|---|---|---|
| #50 | 0.91 | 0.42 | 0.53 | 0.91 | 1.27 (fable) | 0 | 2.01 | 6.06 |
| #57 | 1.79 | 0.87 | 0.98 | 1.67 | 1.43 (fable) | 4.58 | 3.87 | 15.19 |
| #64 | 0.92 | 1.70 | 0.60 | 1.41 | 0.60 | 1.53 | 2.90 | 9.67 |
| #65 | 1.38 | 0.37 | 0.25 | 2.18 | 0.67 | 3.11 | 2.46 | 10.43 |
| #66 | n/a | n/a | n/a | 0.67 (sonnet) | 1.13 | 0.36 | 1.04 | 3.19 |
| #67 | 0.88 (sonnet) | 0.40 | 0.16 | 0.47 | 0.33 | 0.83 | 2.80 | 5.87 |
| #69 | 1.22 | 0.95 | 0.22 | 0.89 | 0.34 | 0 | 1.88 | 5.50 |
| **All** | 7.11 (13%) | 4.70 (8%) | 2.74 (5%) | 8.21 (15%) | 5.76 (10%) | 10.41 (19%) | 16.96 (30%) | **55.90** |

The red check costs no tokens of its own; it runs inside the test-author's turn, two to four times per ticket. By model: Opus $48.72, Fable $3.64 (#50, #57), Sonnet $3.56 (#66, #67).

### Totals and medians

| Metric | Total | Median per ticket |
|---|---|---|
| Start to ready / merge (min) | overlapped | 64 / 81 |
| Agents launched | 29 (TA 7, TB 6, C 7, R 9) | 4 |
| SendMessage continuations | 24 | 3 |
| Review rounds / findings | 9 / 26 (0 high, 7 medium, 19 low) | 1 / 3 |
| Fix commits after review | 10 | 1 |
| CI runs | 15 (0 failed, 2 cancelled, about 5 min each) | 2 |
| Lines added, test / code / .md | 2,769 / 388 / 75 | |
| $ lower bound / scaled | 55.90 / 73.76 | 6.06 / 8.75 |

By size: the four S tickets cost $3.19 to $6.06 (median $5.69); the three M tickets $9.67 to $15.19 (median $10.43).

### Where ceremony outweighed risk

1. **The orchestrator's context is the biggest single cost**: $16.96, 30%, and 48% of #67. It averaged about 292k cached tokens per call (89.4M cache reads over 306 calls), because one session ran about twelve tickets over 8.6 hours. That is context, not ceremony, and it is the cheapest to fix.
2. **Fix rounds cost 1.8 times the first review** ($10.41 against $5.76) for findings that were never high. #65's fix round ($3.11, 30% of the ticket) answered three low findings; under the reviewer's own verdict rule the ticket was ready.
3. **Regex tests of prose are expensive and weak.** #57 was the costliest ticket ($15.19): 746 test lines for 45 code lines, and both breaker rounds beat the wording checks with false text. #67: 227 test lines for 13 code and 18 .md lines, five agents, two review rounds, four CI runs, $5.87. Its Resolution: "Regex checks do not prove meaning; the review read the text."
4. **The breaker loop costs about 19%** ($4.70 author re-rounds + $2.74 breaker + $3.31 orchestrator = $10.75), about $3.58 per real cheat found. It found real code cheats on #50, #64 and #69, two of them S/low bugs, so the data does not support dropping it on small tickets; it supports one round on S/low and no wording checks.
5. **The tier is a smaller lever than it looks.** S/low bugs go to Opus; #69 (23 code lines, 0 findings) cost $5.50, while #67 on Sonnet cost $5.87, because the orchestrator and the Opus review dominate. Sonnet stages were cheap (#67 review $0.33) and nothing failed on Sonnet.
6. **CI never caught anything**: 15 runs, 0 failures, about 5 minutes of waiting per fix round. It is the only check in a clean checkout, so it earns its place, but nothing here argues for more of it.
7. **Merge latency is not the bottleneck**: ready to merge was 0 minutes for #65, #66 and #69, 5 to 53 minutes otherwise.
8. **For contrast**, #54 (outside this sample) failed review three times and escalated to a Fable coder: eight subagents, seven on Fable, $30.43 in subagents alone, more than the subagents of #57, #64 and #65 combined ($25.10). Escalation is where costs run away; the escalation ladder is worth watching once the metric exists.

### Caveats

- **Transcripts undercount.** Priced transcripts came to $99.78 against Claude Code's `cost-state` of $149.55 for the session (Haiku excluded): 78% of Opus, 49% of Fable, 92% of Sonnet captured. "Scaled" assumes the missing share falls evenly. Thinking tokens are only available session-wide.
- **List price on a subscription.** Prices from the bundled claude-api skill (cached 2026-10-06), per MTok: Opus 5.5 $4 in / $20 out / $0.20 cache read; Fable 5.1 $10 / $50 / $0.25; Sonnet 5.5 $2 / $10 / $0.20; cache writes 1.25 times input (5 min) or 2 times (1 h). Fable matched `cost-state` exactly; Sonnet only fits if its cache reads are $0.10. On Max the real cost is rate-limit share, not dollars.
- **Heuristic attribution.** Subagents map to tickets by `#N` in their `meta.json` description (29 of 43); stages by agent type and timestamps against attempt records. Orchestrator calls map by ticket, PR and branch mentions, split evenly; $8.54 of orchestrator cost was unattributable. #64/#67 and #65/#69 ran in parallel, so wall times overlap. Human time and CI minutes were not measured.
- **The attempt store hides fix rounds**: `review pass` is written before the fixes (#57 has two review passes and no fail), and the recorded review tier differed from the model that ran on #57, #66 and #67 (#57 predates the `--tier` rule).

### Re-running the numbers

The scripts are outside the repo in `~/.claude/jobs/b83b1db3/tmp/`: `fetch.py` and `gh_summary.py` (GitHub PRs, reviews, CI), `tx.py`, `calls.py`, `agg.py` and `stages.py` (transcripts and attempts to `stages.json`). They read `~/.claude/projects/-Users-ryaneskuri-portfolio-projects-Harness/4b318caa-....jsonl`, its `subagents/agent-*.jsonl` and `*.meta.json`, and `~/.claude/harness/repos/953ec26da542/tickets/<N>.json`. Claude Code deletes a session after about 30 days (`bin/harness-worklog`, `KEEP_DAYS`), so these transcripts are gone by mid-November; the proposed `harness-work cost` (section 5) replaces the scripts.

## 5. Token cost per ticket (picked by Ryan, 2026-10-14)

Ryan on #76: "A metric of token cost per ticket broken out by model/agent/review/build etc will be amazing and great for documenting/tweaking going forward."

### Data sources

| Source | What it gives | Subscription | API key |
|---|---|---|---|
| Session transcripts, `~/.claude/projects/<project>/<session>.jsonl` ([sessions](https://code.claude.com/docs/en/sessions)) and `<session>/subagents/agent-<id>.jsonl` ([claude-directory](https://code.claude.com/docs/en/claude-directory), [sub-agents](https://code.claude.com/docs/en/sub-agents)) | Per assistant message, observed on CLI 2.1.299 (the schema is undocumented): `message.model`, `message.id`, `requestId`, `effort`, `gitBranch`, `sessionId`, `timestamp`, `message.usage` (input, cache creation 5m/1h, cache read, output, thinking). Subagent entries add `agentId`, `isSidechain`, `attributionAgent` (`harness:coder`), `requestedModel`. No dollars. | yes, local, free | yes |
| Subagent `*.meta.json` | the Agent call's `description` and `agentType` | yes | yes |
| SubagentStop hook input | "`stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path`, and `last_assistant_message`" ([hooks](https://code.claude.com/docs/en/hooks)) | yes | yes |
| OpenTelemetry ([monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)) | metrics `claude_code.cost.usage` and `claude_code.token.usage` "Incremented after each API request", with `model`, `query_source` (`main`, `subagent`, `auxiliary`), `effort`, `agent.name`, `skill.name`; event `claude_code.api_request` with `cost_usd`, token counts, `agent.name`, `request_id`, `message.uuid` matching the transcript; spans with `agent_id`, `parent_agent_id`, `workflow.name`. `OTEL_RESOURCE_ATTRIBUTES` values are attached "on every metric datapoint and event record". Names are redacted unless `OTEL_LOG_TOOL_DETAILS=1`. "Cost metrics are approximations." | yes; needs an OTLP receiver | yes |
| Headless JSON ([headless](https://code.claude.com/docs/en/headless)) | "`total_cost_usd` and a per-model cost breakdown"; a resumed run reports the whole conversation; "client-side estimates" | only for `claude -p` runs | yes: `harness-work dispatch --venue headless` already logs it to `runs/<id>.log` |
| Agent SDK `modelUsage` ([agent-sdk/cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)) | whole-tree tokens and cost "broken down by model"; result-level `usage` "Counts only the top-level agent loop"; parallel tool uses share a message `id` | not used by the harness | not used |
| `/usage` (`/cost`, `/stats`) ([costs](https://code.claude.com/docs/en/costs)) | on Max: "recent usage attributed to skills, subagents, plugins, and individual MCP servers, each shown as a percentage"; the session cost "isn't relevant for billing purposes"; `~/.claude/stats-cache.json` aggregates | yes, interactive, not per ticket | yes |
| Status line ([statusline](https://code.claude.com/docs/en/statusline)) | `cost.total_cost_usd` at list price (resets on `/clear`), durations, lines added/removed, `rate_limits.five_hour/seven_day.used_percentage` "only for claude.ai Pro and Max subscribers"; "does not consume API tokens" | yes: the only source of rate-limit share, the subscription's real currency | cost only |
| Workflows ([workflows](https://code.claude.com/docs/en/workflows)) | "The `/workflows` view shows each agent's token usage"; events and spans carry `workflow.name` | yes | yes |
| `cost-state` (local, undocumented) | the session's total by model; matched Fable exactly in the sample | yes | yes |

### Attribution

- **Ticket.** Exact when a session is the ticket's own: a dispatched `--bg` or headless run works in the worktree, so its whole transcript and every subagent belong to N (`gitBranch` is `t/N-...`). In a shared interactive session, subagents map by the `#N` in their Agent description (29 of 43 in the sample; make it a convention: `#N <stage>`), and the orchestrator is heuristic (mentions and time windows; $8.54 unattributable in the sample). Running one ticket per session (shortlist item 2) makes ticket attribution exact.
- **Stage.** Subagents by `attributionAgent` (test-author, test-breaker, coder, reviewer) and by order: a test-author after the breaker is a re-round; a coder or reviewer after the first review is a fix round. Orchestrator calls by the attempt windows. Red runs cost no tokens of their own.
- **Model.** From each message's `message.model`, never from the recorded tier.
- **Pitfalls found in the sample.** One `message.id` appears once per content block with identical usage: dedupe by `message.id` (Agent SDK says the same of parallel tool uses). The attempt store writes `review pass` before the fixes, so fix rounds are invisible in it: add a `fix` stage record. The recorded review tier differed from the model that ran on three of seven tickets: record the model that ran. Transcripts undercount against `cost-state` (78% Opus, 49% Fable): label transcript figures as a lower bound, or use OTel for exact numbers. A continued (SendMessage) agent keeps one transcript across rounds: split by time windows.

### Proposed design

- **Where.** A `cost` subcommand of `harness-work`, which already owns the attempt records and run records. It reuses `harness-worklog`'s way of finding a repository's transcript directories, worktrees and removed worktrees included.
- **What it records.** `harness-work cost N [--json]` reads the transcripts, subagent transcripts and metas, the ticket's attempts and its run records; dedupes by `message.id`; writes `~/.claude/harness/repos/<key>/cost/<N>.json` with rows `{stage, agent, model, round, input, output, cache_write_5m, cache_write_1h, cache_read, usd_list}`, an `unattributed` row, the sources used, and a `coverage` note (transcript lower bound; headless `total_cost_usd` when present as a cross-check). One price table in the tool, labelled list-price and dated.
- **Two small changes in the loop.** The work skill names every Agent call `#N <stage>` (stage tests, breaker, code, review, fix); `harness-work attempt` gains a `fix` stage and derives the review tier itself (the same change as shortlist item 4).
- **When it runs.** `harness-pr land` runs it after the merge and adds a `Cost` block to the ticket's Resolution comment: a stage × model table with tokens and list-price dollars, and the total. `harness-pr ready` can add a provisional block to the PR body. It can be run by hand any time.
- **Roll-up.** `harness-work cost --all [--since 30d]` prints medians and stage shares by size, risk and type, and writes one CSV row per ticket, stage and model, so changes to the loop can be judged by before and after. The standup or the daily digest can show the last week's median.
- **Later, optional.** OpenTelemetry for exact, real-time figures: `harness-work dispatch` sets `OTEL_RESOURCE_ATTRIBUTES=ticket=N` (one label value per ticket; the docs warn that "high-cardinality values increase storage cost") and `OTEL_LOG_TOOL_DETAILS=1` so `agent.name` carries the real names. It needs an OTLP receiver, which is a dependency for Ryan to decide on. A status-line script that logs `rate_limits` percentages per session would give the subscription's real cost.
- **Size.** M: the reader and its tests are near the 300-line cap, so plan two tasks: (a) `harness-work cost` with synthetic transcript fixtures; (b) the `#N <stage>` convention, the `fix` stage, the derived review tier and the Resolution block.

### First sample

The seven tickets above are the baseline: $55.90 lower bound ($73.76 scaled) across 29 agents, with the orchestrator at 30%, fix rounds 19%, coder 15%, tests 13%, review 10%, breaker re-rounds 8% and the breaker 5%. The script output is in `~/.claude/jobs/b83b1db3/tmp/stages.json` until the transcripts expire.

## 6. Gaps in the AI development cycle

1. **No evals of the harness itself.** The unittest suite proves the hooks and tools deterministic; nothing measures whether the prompts make the model do better. "When we evaluate 'an agent,' we're evaluating the harness and the model working together" (Demystifying evals, Jan 9, 2026). `claude plugin eval` exists for this, with free graders (`tool_used`, `tool_order`, `file_exists`, `regex`) and a no-plugin baseline. The cases should come from real failures: #54's three failed reviews, the early-ending runs `runs --sweep` caught, the breaker's cheats, the regex-gameable tests, the mis-recorded tiers. Run it on changes to `agents/`, `skills/` and `hooks/`, pinned to a model so "a model rollout isn't mistaken for a plugin regression" (plugin-evals). Every run is a real model call, so keep the suite small (the evals post suggests 20 to 50 tasks) and use `--max-cost-usd`.
2. **No outcome metrics.** The board has the raw data (start, PR, ready, merge times; review rounds and severities; fix commits; CI runs; bugs filed against landed tickets) but nothing turns it into cycle time, rework and escaped defects per ticket, which the playbook pairs with each practice as leading and lagging indicators. The cost metric (section 5) is the first of these.
3. **No learning loop from review findings.** Findings are posted inline on PRs and then forgotten. The playbook rule is "When a review flags a mistake for the second time, the correction goes into CLAUDE.md as part of that review"; the code-review plugin says "Update CLAUDE.md based on recurring review patterns". The `learn` skill only reads the current conversation. Feeding it the findings of recently landed PRs (`harness-pr comments`) would close the loop, with each repeat becoming a CLAUDE.md line, a hook, or an eval case.
4. **Context management is left to habit.** One session ran about twelve tickets, so the orchestrator re-read about 292k tokens per call. `harness-work dispatch` already gives each ticket its own session; for in-session work, start each ticket fresh (or `/clear` between tickets) and keep agent reports short, as the agents already ask. HumanLayer's target is 40 to 60% utilization.
5. **No re-check when a model lands.** "when a new model lands ... re-examine a harness, stripping away pieces that are no longer load-bearing" (Harness design, Mar 24, 2026). `/doctor prompt-audit` looks for "instructions written for older models" and contradictions (memory). Neither is part of the routine; a model release could open a chore ticket that runs both and the evals.
6. **Effort is set, not measured.** The harness pins Opus at high and the planner at xhigh; the Opus 5.5 guide says to start at medium and reserve xhigh for measured gains. Once the cost metric and a few evals exist, a medium-against-high comparison on a handful of tickets settles it.
7. **Asking versus deciding.** Every unanswerable question parks the ticket. Nothing parked in the sample, so this costs nothing yet; when it starts to, logging a `Ruling:` for reversible implementation choices (superpowers) keeps dispatched runs moving and leaves the user a record to overrule.
8. **Built-in git instructions.** The coder sets its own commit format and `harness-pr` writes PR bodies; "If your CLAUDE.md sets commit or pull request rules, turn off the built-in ones with `includeGitInstructions`" (memory). Not checked here whether the two conflict.
9. **Verification is stacked.** The coder runs the verify command, the stop hook runs it again when files changed, the orchestrator passes the log, the reviewer may run the tests, and CI runs it once more. Each is cheap in tokens, and the deterministic gates are what the sources recommend; the cost is wall time (about 5 minutes of CI per round). Worth watching in the cost metric rather than cutting now.

## 7. Ranked shortlist

Cuts and additions in one list, ranked by expected value against cost and by strength of evidence. Sizes: S (one sitting, one or two files), M (the normal ticket), L (at the cap).

| # | Change | Files it changes | Why it beats what exists | Evidence | Size |
|---|---|---|---|---|---|
| 1 | **Token cost per ticket** (picked by Ryan): `harness-work cost`, a `fix` stage, `#N <stage>` Agent descriptions, a Cost block on the Resolution, a roll-up | `bin/harness-work`, `bin/harness-pr`, `skills/work/SKILL.md`, tests | Today the only numbers come from a one-off script and expire with the transcripts; every other item here is judged by this metric | Section 5; sample of seven tickets, $55.90; attempt store hides fix rounds | M (two tasks) |
| 2 | **One ticket per session**: the work skill and `user/CLAUDE.md` say to run each in-session ticket in a fresh session (or `/clear` first) and prefer `harness-work dispatch` for several | `skills/work/SKILL.md`, `user/CLAUDE.md` | The orchestrator re-reads its whole history on every call; a fresh session starts from its system prompt and about 4k tokens of instructions instead of about 292k of history | Orchestrator $16.96, 30% (48% on #67); best-practices on context; HumanLayer 40-60% | S |
| 3 | **Severity-based fix policy**: fix high and medium; post lows, let the coder fix them in the same commit when trivial, otherwise list them in the PR notes; `harness-pr ready` and `land` gate on high and medium only; the reviewer still reports everything | `skills/work/SKILL.md` (lines 14, 26, 27, 29, 47), `bin/harness-pr`, `agents/reviewer.md`, `docs/design.md`, tests | Removes the contradiction with the reviewer's own verdict and stops paying for low findings | Fix rounds $10.41 (19%), 1.8× the first review; 0 high of 26; #65 $3.11 on three lows; best-practices "treat the rest as optional"; Opus 5 "report everything and filter in a separate pass" | M |
| 4 | **Tools carry the routing**: `harness-board tier N --stage S` prints model and effort; `harness-work attempt` derives the review tier itself; `user/CLAUDE.md:27-29` shrinks to one line; the work skill says "pass what `harness-board tier` prints" | `bin/harness-board`, `bin/harness-work`, `user/CLAUDE.md`, `skills/work/SKILL.md`, tests | Four prose copies did not prevent mis-records; a tool cannot forget | Recorded tier wrong on #66 and #67 after the rule existed, and on #57; best-practices "convert it to a hook"; memory on contradictions | S |
| 5 | **No regex tests for prose**: a change to prompt or doc text is `no-tests` and is checked by the reviewer (and, later, an eval); the test-author is told that wording checks do not prove meaning, and to write as many tests as the definition of done needs | `agents/planner.md`, `agents/test-author.md` | Wording checks were beaten every time and drove the costliest ticket | #57 $15.19, 746 test lines for 45 code; #67 17:1; handoff lesson; Fable guide on test volume | S |
| 6 | **Shrink the work skill**: move the resume-from-draft-PR procedure into `harness-work start --json` (it returns the latest harness review findings and the step to resume at); keep one end-state list and drop the closing self-check; cut the dispatch paragraph to a line | `skills/work/SKILL.md`, `bin/harness-work`, tests | 1,936 words with a 284-word API script; the tools already know these facts | Effective context engineering ("hardcoding complex, brittle logic"); Opus 5 on over-verification; `runs --sweep` already catches early ends | M |
| 7 | **Say each rule once**: drop the "Board and branches" and "Done means" duplicates from `CLAUDE.md` and `templates/CLAUDE.md`; drop hook-enforced clauses from `coder.md:10`, `:15`, `:17`, `test-author.md:19`, `land:26`, `work:33`, `user/CLAUDE.md:17`, `:22`, `:34`; fix `user/CLAUDE.md:39-40` to "remind me to run"; reword `coder.md:15` as "tests verify correctness, not define the solution"; soften `user/CLAUDE.md:11` | `user/CLAUDE.md`, `CLAUDE.md`, `templates/CLAUDE.md`, `agents/coder.md`, `agents/test-author.md`, `skills/land/SKILL.md` | Fewer copies to drift and contradict; each rule sits where it applies, with its reason | Memory ("keep the two consistent"; "Claude may pick one arbitrarily"); best-practices ("Ruthlessly prune"); section 3 duplication table | S |
| 8 | **Breaker scaled by risk**: one round on S/low tickets, two on M and above | `skills/work/SKILL.md` | Keeps the cheat check that paid off on small bugs while capping the re-round cost | Breaker loop $10.75 (19%); real cheats on #50, #64 (in round 1), #69; wording-only on #57, #67 | S |
| 9 | **Harness evals**: a `claude plugin eval` suite of 10 to 20 cases from real failures, run by hand and on changes to agents, skills and hooks, pinned model, cost cap | new `plugins/harness/evals/`, `scripts/verify.sh` or a separate CI job | The unittest suite proves the hooks; nothing proves the prompts help, or that a cut (items 3 to 8) did not hurt | Demystifying evals (Jan 9, 2026); plugin-evals; playbook "on any change to CLAUDE.md, skills or hooks"; superpowers "watch an agent fail without the skill" | L (start with M) |
| 10 | **Learning loop from findings**: `learn` also reads the findings on PRs landed since its last run and proposes, for each repeat, a CLAUDE.md line, a hook or an eval case | `skills/learn/SKILL.md`, possibly `bin/harness-pr` | Findings are paid for and then forgotten; the loop turns them into fewer future findings | Playbook "second time ... goes into CLAUDE.md"; code-review plugin; Hashimoto; 26 findings in the sample | S |
| 11 | **Measure effort**: run three to five tickets at medium against high with the cost metric, and drop the planner's `xhigh` unless it shows a gain | `user/CLAUDE.md`, `agents/planner.md`, `docs/design.md` (after the measurement) | Today's settings are above the model's default without evidence | Opus 5.5 guide ("Start at `medium`"; "Reserve `xhigh` ... where you've measured a quality gain") | S (needs 1) |
| 12 | **Model-release re-check**: a chore ticket template for each model release: run `/doctor prompt-audit`, the evals, and a review of which pieces are still load-bearing | `templates/` or `docs/design.md`, and the digest | The harness encodes assumptions about the model that age | Harness design (Mar 24, 2026): sprints and resets removed on newer models; memory on `/doctor prompt-audit` | S |
| 13 | **Rulings for reversible choices**: agents decide reversible implementation questions, log `Ruling: <decision> — <why> — <cost if wrong>` in the PR notes, and keep needs-input for product behavior, irreversible or security-sensitive questions | `user/CLAUDE.md:12`, `agents/coder.md:19`, `skills/work/SKILL.md` | Keeps dispatched runs moving; Ryan overrules on the PR instead of unparking a ticket | superpowers rulings; Opus 5 "left to run". Counter-evidence: 0 needs-input in the sample, so low urgency | S |
| 14 | **Check `includeGitInstructions`** against the harness's own commit and PR rules | `user/settings` snippet, `docs/design.md` | Removes a possible conflict between built-in and harness git rules | memory page | S |

Considered and not recommended: dropping CI (it is the only clean-checkout check, and its cost is wall time, not tokens); dropping the breaker on small tickets (it found real cheats on two S bugs); moving S/low bugs to Sonnet on cost grounds alone (the tier saved little against the orchestrator and review); adding capitals for emphasis (no rule here is being skipped, which is the only case best-practices gives for it).

## 8. Next step

Ryan picks from the shortlist on #76. Only picked items become tickets, through `/harness:plan`; the picks are recorded on #76. Item 1 is already picked.
