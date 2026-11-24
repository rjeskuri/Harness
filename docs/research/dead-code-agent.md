# Research notes: a daily dead-code agent with a human-review land gate

Gathered 2026-11-24 for #441 (epic #439) on the MacBook Pro (M1 Pro, 8 cores, macOS 26.5.2), with Claude Code 2.1.331 and gh 2.99.0, against the plugin source at 8cbcb78. Sources: the plugin and docs/design.md; the run records under `~/.claude/harness/runs/` and the transcripts under `~/.claude/projects/` (read only); `harness-projects list`; each registered project's top-level files (read only); a few gh calls (`gh api rate_limit`, one `gh search issues` for the `manual-land` label, one `gh issue list` of the sandbox) besides filing the follow-ups; one loop smoke run on harness-sandbox. The prototypes, two uv venvs on /usr/bin/python3 3.9.6 (vulture 2.16 and coverage.py 7.10.7), the stdlib tracer and all line data lived in the job's scratch directory, outside the repository. Nothing was removed from Harness, no label was created, no LaunchAgent was installed, and nothing was installed into an existing interpreter.

## Verified

### How Harness code is reached (outline step 1)

Each way below was checked in the source; the scanner has to count each one as a use.

- **bin tools by name.** The ten files in `plugins/harness/bin/` have no `.py` suffix and start `#!/usr/bin/env python3`. They are run by name from skills (`!` lines and `allowed-tools`, for example `Bash(harness-pr:*)` in `skills/land/SKILL.md`), from agents, from `user/settings.snippet.json` (allow rules), from `docs/guide.md`, and from each other: harness-pr runs `harness-board` through `board(repo, "close", ...)` and `board(repo, "epic-check", ...)` (`bin/harness-pr` 1751 and 1780), `harness-work cost` and `harness-work dispatch --after N`. That last form passes the subcommand as a separate string constant, so a text search for `harness-board close` misses it.
- **LaunchAgents.** `harness-digest` (installed: `com.ryaneskuri.harness-digest.plist`, `StartCalendarInterval` 08:30) and `harness-work dispatch --install-launchd` (hourly, minute 0, not installed) write plists whose `ProgramArguments` are the tool's real path.
- **hooks.json.** It wires nine hook files in eleven entries over five events: notify.py, session_brief.py, readonly_bash.py, branch_guard.py (twice), tests_first.py (twice), backup_gate.py, readonly_allow.py, after_edit.py and verify_on_stop.py. `land_ask.py` is not wired; docs/design.md (Hooks) keeps it as a silent stub on purpose, for sessions that started before #385.
- **harness_lib** modules are imported by name after each tool's `sys.path` prologue (`bin/harness-pr` 100-101), so an import statement is the use.
- **argparse surface.** Subcommands and flags are reached from skills, agents and other tools, and many only by the owner typing them. docs/design.md lists them under each tool's heading without the tool's name (`- \`close N [--comment TEXT]\``, `- \`upgrade\``), so `harness-runner upgrade` never appears as one string.
- **Templates** (`templates/deploy_guard.py`, `templates/runner/`, the CI and hook templates) are copied into other projects by harness-scaffold and harness-runner; their functions run there, not here. `templates/test_smoke.py` is not valid Python until scaffolded (`import {{pkg}}`), and vulture reports it as a syntax error.
- **scripts/** are named by CLAUDE.md, scripts/verify.sh and the CI workflow.
- **session_brief's fail-open fallback copies** (`hooks/session_brief.py` 80-100) are defined inside the `except` of the harness_lib import and run only when the lib cannot load.
- **Test seams.** `HARNESS_GH`, `HARNESS_NOW`, `HARNESS_LAUNCHCTL` and `HARNESS_CLAUDE` are read by the tools and set by the tests; `fakegh.py` and the `*_support.py` modules are imported only by tests.

### Facts the review gate is designed against

- The `manual-land` label exists: harness-board's label table, `("manual-land", "D93F0B", "Land by hand: autoland skips it")` (`bin/harness-board` 101), and docs/design.md's board labels. Its only reader is `autoland._ticket`, rule 4: `if "manual-land" in (facts.get("pr_labels") or []) or "manual-land" in labels: return "manual", "label", "manual-land label"` (`lib/harness_lib/autoland.py` 148-149), reached only through `harness-pr gate`, which the work loop does not run.
- `gh search issues --owner <owner> --label manual-land` returned 0: no issue on any of the owner's repositories carries it today, so changing what it means changes nothing in flight.
- `harness-pr land` reads no label. `cmd_land` (`bin/harness-pr` 2073) runs `merge_preconditions` and `land_gate` (1817), which check behind-main, the harness review at the head and its findings.
- `--loop` only changes the Cost block: its help says "the /harness:work loop is landing it: the Cost block records `Landed by: loop`, else hand", and the caller sets it itself.
- `/harness:land` (`skills/land/SKILL.md`) runs `harness-pr land P --catch-up` without `--loop` and has `disable-model-invocation: true`, as do all ten skills. The allow rule `Bash(harness-pr:*)` is in `user/settings.snippet.json` and in the user settings, so any session may run `harness-pr land` itself; `land_ask.py` no longer asks (#385). No hook refuses `gh pr merge`: branch_guard.py guards `git merge` only (its line 746), as docs/research/harness-audit.md row 18 already says.
- The settings snippet's auto-mode text says "Every pull request, risk:high included, lands through that command after the harness review, CI and its gates, without asking me", next to user/CLAUDE.md's standing rule.
- **What a transcript records about who typed a prompt** (`~/.claude/projects/*/*.jsonl`, read 2026-11-24):
  - The owner's `/harness:land 217` on 2026-10-25 is a `"type":"user"` entry whose content is `<command-message>harness:land</command-message>\n<command-name>/harness:land</command-name>\n<command-args>217</command-args>`, with `"origin":{"kind":"human"}` and `"turnOrigin":"human"`.
  - Of 40 transcripts that start with `/harness:work`, 25 record `"origin":{"kind":"human"}` and `"turnOrigin":"human"` (entrypoint `cli`), 4 record `"turnOrigin":"sdk"` (entrypoint `sdk-cli`, the `claude -p` runs), and 11 printed neither within the matched span.
  - The first user entry of the five newest bg runs dispatched by `harness-work dispatch` (`~/.claude/harness/runs/*.json`, their `bg_id` transcripts) also records `"origin":{"kind":"human"}`, `"turnOrigin":"human"`. So `origin` alone does not tell a typed prompt from a dispatched one; the prompt's text does, because dispatch only ever sends `/harness:work N`.
  - docs/design.md (Platform facts) and docs/research/claude-code-platform-v2.md: PreToolUse input carries `session_id`, `transcript_path`, `agent_id` and `agent_type`.
- **Dispatch and sweep (#422).** `ask_before_landing` (`bin/harness-work` 1777) already reads the ticket issue's labels (`"status:in-review" not in labels`) before it lists the branch's pull requests, so a label check there costs no extra gh call. `ticket_finished` treats an in-review ticket as finished unless `before_landing` finds its ready PR open; `redispatch_candidates` dispatches such a ticket again, up to `REDISPATCH_MAX = 3` ended runs in `REDISPATCH_WINDOW` (7 days).
- **Tests first on a removal.** `tests_first.py` lets any source edit through on a `refactor` ticket once any red record exists, green included (its line 387); `harness-red check` writes the record even on green (docs/design.md, harness-red step 5). But `test_changes` (`bin/harness-red` 503-511) keeps only the changed test files that still exist and otherwise refuses "No test changes on this branch; write the tests first.", so a branch whose only test change is deleting whole test files gets no record.

### Measurements (outline steps 2 to 4)

All on 2026-11-24 in the scratch directory. Other sessions on this Mac were running their own test suites at the same time: the 1-minute load average read between 13 and 220 on 8 cores during these runs, so every wall time below is an upper bound, and the ratios matter more than the seconds.

**Static, stdlib prototype** (`ast` plus a text search; about 300 lines, never committed). Run as `/usr/bin/python3 -I - <checkout> --json` with the program on a heredoc: the tests-first hook refuses the coder role's run of a program file outside the worktree, and an inline program it can read is allowed when it writes no file.
- It reads 134 Python files (bin/, hooks/, harness_lib, scripts/, tests/ as references only, templates/ as entry points) and 66 text files (skills, agents, hooks.json, docs/guide.md, user/, CLAUDE.md, scripts/*.sh, .githooks/, .github/, docs/design.md as contract), and 1,948 definitions in 48 parsed non-test files.
- Wall time: 9.6 s for the final version (15.6 s for a first version that searched every file once per flag), both at a load average near 130.
- **36 candidates:** 4 definitions, 1 flag parsed and never read, 17 flags with no caller, 14 subcommands with no caller. No constant branch: the 7 constant tests it found first were all `while True:` loops, which the rule now skips.

**Static, vulture 2.16** (`vulture plugins/harness/bin/* plugins/harness/hooks plugins/harness/lib plugins/harness/templates scripts [plugins/harness/tests] --min-confidence 60`; bin files must be named one by one, since vulture reads only `*.py` in a directory):
- Without tests/: 0.80 s at a load near 19 (2.13 s near 130), **18 candidates** (plus the syntax error in templates/test_smoke.py).
- With tests/ as users: 4.99 s near 19 (10.39 s near 130), 277 lines, of which 274 are in tests/ (220 `unittest.TestCase` classes it cannot see used, 48 attributes) and **3** in the plugin.
- vulture reads no skill, hooks.json or docs, so it cannot judge a bin tool, a hook file, a subcommand or a flag at all.

**Dynamic, coverage.py 7.10.7** over `python3 scripts/run_suite.py`, with the scratch venv's bin first on PATH and a `.pth` in the venv that calls `coverage.process_startup()` whatever the environment (some tests rebuild it: test_backup_gate.py keeps only HOME, PATH, TMPDIR and LANG). rcfile: `parallel`, `sigterm`, sources plugins/harness and scripts, tests omitted.
- Baseline, the same venv interpreter without coverage: 2,301 tests in **739 s**. 3 modules fail with this interpreter, coverage or not: test_deploy_guard and test_harness_lib_repo (their standard-library-only checks name `signal` as not standard library under the CLT 3.9.6 build) and test_canary (that python writes bytecode under `~/Library/Caches/com.apple.python`, which the canary's HOME check sees). CI runs on ubuntu-latest and does not hit them.
- With coverage: **1,361 s (1.84x)**, 9,829 data files (499 MB, one unreadable, from a process killed while writing), `coverage combine` 10 s, `coverage json` 2 s. Two timing tests in test_harness_lib_gh fail only under coverage (`test_calls_run_side_by_side`, `test_the_first_slow_call_in_order_is_the_one_named`).
- **90 % of 16,459 statements ran** (1,604 missed). Lowest: harness_lib/programs.py 56 %, hooks/land_ask.py 80 % (the canary runs it), harness_lib/runners.py and bin/harness-runner 82 %.
- Of 1,328 functions with statements, **17 never ran** (94 statements). All 17 are referenced in code, so none is dead: 8 are session_brief's fail-open copies, 2 are signal or timeout handlers (`verify_on_stop.on_signal`, `backup_gate.out_of_time`), 5 are programs.py's wrapper paths (`preload_word`, `wrapper_subcommand`, `wrapper_programs`, `deno_programs`, `bun_programs`: 51 statements), and 2 are `backup_gate.secrets` and harness-worklog's `first_day`. They are untested, not dead.
- The bodies of `store_dir` and `has_wave_review` ran under the suite, only because their own tests call them, and the two dead constants run at import like every constant. So suite coverage cannot find test-only code; it can only veto a static candidate that production code reaches in a way the static pass missed, and it found none.

**Sample, coverage.py against stdlib `sys.settrace`** (4 modules, `run_suite.py -j 4 test_pr_land test_hooks test_work_runs test_red`, a second scratch venv whose `.pth` starts either coverage.py or a 40-line `sys.settrace` tracer that keeps only plugin and scripts lines):
- Plain: 250 s (load 17) and 219 s (load 13). coverage.py: 560 s (load 184) and **442 s (load 22)**. settrace: **257 s (load 23)**. So at comparable load coverage.py cost about 1.9 times the plain run and settrace about 1.1 times. Each collecting run wrote 1,258 per-process data files, one per measured Python process, most of them short tool and hook runs, and coverage.py's start-up in each one is where its time goes.
- Reach: settrace recorded 4,662 of the 4,664 lines coverage.py did, in the same 31 files with executed lines. Its 286 extra lines are all continuation lines inside multi-line statements, which coverage.py folds into the statement's first line. Data: 6.3 MB of trace JSON against 64 MB of coverage data files.

**Loop smoke, run once with collection** (`PATH=<scratch venv>/bin:$PATH python3 scripts/loop_smoke.py run --sandbox <harness-sandbox clone> --plugin <this worktree>/plugins/harness --timeout 60`, with the flag file naming a second rcfile and data folder):
- Nothing had to be installed into an existing interpreter. `loop_smoke.py` starts `claude -p` with the inherited PATH (plus the plugin's bin), so the session's hooks (`python3 ${CLAUDE_PLUGIN_ROOT}/hooks/...`), the bin tools (`#!/usr/bin/env python3`) and the agents' Bash calls all found the scratch venv's python3 and its `.pth`. The sandbox's own tests run under uv's project venv (`uv run pytest`) and were not measured.
- Result: ticket #9 closed, PR #10 merged, no worktree left: `exit 0`, **4 min 13 s** in all. The session took 239 s, against 164 to 194 s for the four smoke runs of 2026-11-23 without collection (their logs' `duration_ms`), so collection added roughly a quarter to a run that mostly waits on the model.
- 142 data files; 36 source files ran, 5,754 lines. harness-digest, -projects, -runner, -scaffold, -standup and -worklog did not run at all.
- **29 lines ran only in the real run, never in the suite:**
  - 19 in harness_lib/programs.py: the `uv run` wrapper path that tests_first follows for the sandbox's `HARNESS_TEST_CMD` (`uv run pytest -q {files}`): `wrapper_subcommand`, `wrapper_programs`, and the `PYTHONPATH=` scan. Two of the 17 functions the suite never ran are therefore live.
  - 7 in hooks/verify_on_stop.py: the Stop hook after the land had removed the session's own worktree (hashing missing files, then "could not start scripts/verify.sh").
  - harness-work 644 (`cmd = "uv sync"`, the worktree setup of a uv project), harness-pr 529 (`return "No findings."`), tests_first.py 679.
- The bodies of the dead definitions did not run (only their `def` and assignment lines, at import).

**The candidates, read one by one:**

| Candidate | Found by | Read | Verdict |
|---|---|---|---|
| `harness_lib/programs.py` 80 `FILE_RUNNERS` | both | Referenced nowhere, tests included; added in #363, and the `Spec` table at line 182 does the job | **dead** |
| `harness_lib/backups.py` 73 `store_dir(main)` | both | No caller in the plugin; `_store` does the work; test_backups.py uses it as an oracle (724, 840, 841); docs/design.md names it | **dead, test-only + contract** |
| `harness_lib/routing.py` 19 `KEYS` | both | No reader in the plugin; test_harness_lib_routing.py 813 pins it; docs/design.md names it | **dead, test-only + contract** |
| `harness_lib/waves.py` 201 `has_wave_review` | both | No caller in the plugin; test_harness_lib_waves.py pins it (its `PUBLIC` list and one test); docs/design.md names it | **dead, test-only + contract** |
| `bin/harness-pr` 2615 `land --yes` | prototype | Parsed, never read ("accepted and ignored (the land skill confirms)"); since #2, and no skill passes it now; test_pr_land.py passes it in 5 tests; docs/design.md lists it | **dead, test + contract** |
| 14 subcommands and 17 flags with no caller | prototype | `harness-board close` and `epic-check` are called by harness-pr as `board(repo, "close", ...)`; the rest are listed in docs/design.md or are `--help`-only owner flags (harness-worklog `update --gap-minutes`, `--drop-stale`, `--rewrite-old`, `--ignore-commit`, `zone --evidence`) | false positive: other-tool call form, and the owner's typed surface |
| `sub.required = True` (10) | vulture | argparse reads the attribute | false positive: library protocol |
| `frame` in two signal-handler lambdas | vulture | the signature is signal's | false positive: library protocol |
| `Worktree.locked`, `.prunable` | vulture | dataclass fields parsed from `git worktree list --porcelain`; only tests read them | false positive for removal: a record's shape (docs/design.md describes the flags) |

So of **36 prototype candidates, 5 are dead** (4 definitions and 1 flag, about 19 lines of code, 4 of them named in docs/design.md) and 31 are false positives of two classes. Of **18 vulture candidates, 4 are dead** and 14 are false positives of two classes; vulture misses the flag.

## Not verified

- **Wall times** were taken while other sessions ran their own suites (load 16 to 220 on 8 cores), so the ratios are rough. The coverage-against-trace sample in particular moved with the load.
- **That collection reached every process.** One data file was unreadable (a process killed while writing), and a process ended by SIGKILL or `os._exit` writes nothing. Tests that start a tool by `python3` on a PATH without the venv (guards_support.py's `SYSTEM_PATH`) would go unmeasured; the guard tests run hooks with `sys.executable`, so they were measured, but not every test module was audited for this.
- **The rules were written after reading these candidates**, so "5 dead, no false positive under the full rule" is a fit to today's tree, not a forecast. New code will bring new classes.
- **The gate hook (#452) was not prototyped.** That the transcript already holds the prompt's user entry when PreToolUse runs, and that `origin`/`turnOrigin` stay as read here, were not checked: they are an undocumented transcript format of Claude Code 2.1.331.
- **GitHub self-approval:** that GitHub does not let a PR's author approve it (so a required review cannot be the gate on a solo repository) was not fetched today.
- **Whole-file test deletion:** that `harness-red check` refuses a refactor branch whose only test change deletes files was read from `test_changes`, not run.
- The daily job's rejected-fingerprint memory, its batch rule and the LaunchAgent are designs only; nothing was installed.
- **The other projects:** each line in "Beyond Harness" comes from a listing of the project's top-level files, its pyproject.toml or package.json, and its CLAUDE.md, not from a scan.
- **The loop smoke reached one path of the loop** (a size S, risk:low task with no findings): no fix round, no park, no needs-input, no breaker. Lines only those paths run are not in its data.

## False positives found on Harness, and the rule that excludes each

Counted on today's tree with the prototype, by turning each rule off in turn (an ablation run over the same 1,948 definitions) and from vulture's output. "Cost" is how many false positives the class gave on Harness without its rule.

| Class | Cost on Harness without the rule | Rule that excludes it |
|---|---|---|
| **Entry points: bin tools run by name** | 10 (every file in bin/ has no import or call from code; skills, agents, docs and settings name them) | Every file in bin/ is an entry point; it is a candidate only when no skill, agent, setting, plist, doc or other tool names it, and even then it is reported, not removed. `main` and code under `if __name__ == "__main__"` are entry points. |
| **hooks.json** | 7 hook files referenced only there (after_edit, backup_gate, readonly_allow, readonly_bash, session_brief, tests_first, verify_on_stop); branch_guard.py is also imported by tests_first.py, and notify.py is hidden by the lib module of the same name | A hook file is used when hooks.json names it. A file hooks.json does not name (land_ask.py) is used when scripts/canary.py or a `# deadcode: keep (<reason>)` comment keeps it; docs/design.md keeps land_ask.py on purpose. |
| **Skills, agents and other tools calling bin tools** | 14 subcommands with no `<tool> ... <sub>` string anywhere outside their tool, and 17 flags no skill, agent or other tool passes: 2 of the subcommands are called as `board(repo, "close", ...)` and `board(repo, "epic-check", ...)` from harness-pr, the rest the owner's typed surface | A subcommand or flag is used when a skill, agent, doc, setting or other tool names it, a string constant equal to it appears in another tool's call, or docs/design.md lists it under the tool's heading. The owner's typed surface (listed in docs/design.md, or only in `--help`, like harness-worklog's `--rewrite-old`) is never a removal candidate; the scanner reports it as "no caller" for the owner to judge. A flag parsed and never read (`--yes`) is a candidate. |
| **Test-only code** | 3 of the 4 dead definitions are kept alive only by their tests (`store_dir`, `routing.KEYS`, `has_wave_review`); vulture without the tests adds 2 dataclass fields, `Worktree.locked` and `.prunable` | A reference from tests/ does not count as a use, but the candidate is marked `refs: test`, its test files are listed, and it goes to the test-author first (Design). `*_support.py`, `fakegh.py` and the `HARNESS_*` seams are test infrastructure: the scanner never proposes test files, and an environment seam read by a tool stays while docs/design.md names it. |
| **docs/design.md as contract** | 4 of the 5 true candidates are named there | A name in docs/design.md does not keep code alive, but it marks the candidate `contract`, and the batch's PR updates those lines (project rule). |
| **Docstrings and comments** | 1 false negative: `store_dir` is named in `restore`'s docstring (backups.py 599), so counting docstrings hides it | Docstrings do not count as references; other string constants do (dispatch tables, `getattr`, argv lists). Turning string constants off added no candidate today, so the rule costs nothing. |
| **Library protocols** (vulture) | 12: `sub.required = True` on argparse subparsers (10), and the `frame` parameter of signal handlers (2) | An attribute set on an argparse object, a parameter whose signature a library fixes (signal handlers, `__exit__`), dunder methods, and dataclass or NamedTuple fields are not candidates. |
| **Templates** | 0 today (their functions are called inside the template) | templates/ are entry points of other projects; their definitions are never candidates here. |
| **Fail-open fallbacks** | 0 static; a dynamic-only class (below) | Code inside the `except` of the harness_lib import (session_brief's copies) is kept: it runs only when the lib is broken. |
| **Name collisions** | 0 found; a false negative risk (hooks/notify.py and harness_lib/notify.py share a name) | Modules are matched by path (an import of `harness_lib.notify` is not a use of hooks/notify.py); definitions stay matched by name, which errs toward keeping. |

## Recommendation

**The rule (outline step 4).** A candidate is a definition (function, class, method, module constant), a harness_lib module or a parsed-but-unread argparse flag that:
1. has no reference from plugin code, scripts/, skills, agents, hooks.json, settings, plists, docs/guide.md or user/, after the exclusion rules above (tests and docs/design.md do not count as uses, but mark it `test` and `contract`);
2. did not run in any real loop run on file (the loop smoke's or another collected real run's data), which vetoes a use the static pass cannot see; and
3. carries no `# deadcode: keep (<reason>)` comment.

Suite coverage does not make or veto candidates. It lists the tests that reach a test-only candidate, so the ticket can name them, and it reports never-run functions (17 today) as untested, which goes to the owner, not to removal.

Measured on Harness today: **5 candidates, all 5 dead when read, no false positive.** They are `FILE_RUNNERS`, `store_dir`, `routing.KEYS`, `has_wave_review` and harness-pr `land --yes`, about 19 lines of code plus their docs/design.md lines and the test cases that pin them. Beside them the scanner reports, but never proposes, 31 subcommands and flags that no tool or skill calls: the owner's typed surface. None of the 29 real-run-only lines touches a candidate.

**Static: the standard library, not vulture.**
- The two found the same four dead definitions.
- Every false positive on Harness that matters comes from references outside Python (skills, hooks.json, docs/design.md, the `board(repo, "close")` call form, the owner's typed surface), and vulture reads none of them, so a harness tool needs its own pass anyway.
- vulture added 14 false positives of its own (library protocols, record fields) that would need a whitelist, and it misses the unread flag.
- The plugin ships standard library only (CLAUDE.md, Conventions), and the prototype ran in seconds.
- Cost: about 300 lines of scanner to own and test (#453). vulture stays a fine one-off cross-check from a scratch venv.

**Dynamic: the standard library (`sys.settrace` started from a `.pth`), with coverage.py only as a one-off cross-check.**
- On the sample the stdlib tracer reached the same code as coverage.py (all but 2 of its lines) at about 1.1 times the plain run, where coverage.py cost about 1.9 times (the full suite: 739 s plain, 1,361 s under coverage.py). The tests start over a thousand short Python processes, and coverage.py pays its start-up in each one.
- What the tracer must add that coverage.py gives for free: merging the per-process files, mapping lines to statements with `ast` (the 286 continuation lines), a SIGTERM flush and a report. That is a script of perhaps 150 lines to own and test (#474). Neither tool sees a process killed by SIGKILL or ended by `os._exit`.
- Both need a hook in every child process, including the tests that rebuild their environment, so both need a `.pth` in a venv's site-packages rather than a `sitecustomize` on `PYTHONPATH`. For the tracer the venv is a bare `python3 -m venv` with no package in it. Either way nothing goes into an existing interpreter.
- The tradeoff: coverage.py is mature and already handles the edge cases, but it is a third-party package in a standard-library-only project, and it nearly doubles the collection run. The tracer keeps the convention and the cost low, at the price of a small script the harness owns. On 3.12 and later `sys.monitoring` would make a stdlib tracer cheaper still, but the floor is 3.9 (/usr/bin/python3) and the PATH's python3 is Anaconda's 3.10.
- Collect the suite weekly, and a loop smoke whenever one runs anyway (each canary run after a skill or agent change). Scan daily against the newest data. Dead code changes daily; what real runs reach changes slowly. Collection with coverage.py added about a quarter to the loop smoke's session time (above), which mostly waits on the model.
- In the other projects the tradeoff flips: they already run pytest under uv with a dev dependency group, so vulture and pytest-cov cost one line each there.

**Where the agent fits.** The scan, rules, batch and memory are a deterministic tool. The judgment (is this really dead, what does the contract say, which tests go) is the normal `/harness:work` loop on a refactor ticket, with the owner's review as the last word at the land.

## Design

### The daily run

- **Where:** a Mac LaunchAgent, `~/Library/LaunchAgents/com.ryaneskuri.harness-deadcode.plist`, written by `harness-deadcode daily --install-launchd` the way `harness-digest --install-launchd` writes its own: `ProgramArguments` the python3 its own PATH finds and the tool's real path, `StartCalendarInterval` at 03:30 (launchd runs a calendar job missed asleep once on wake, as `install_launchd` in harness-work notes), `RunAtLoad` false, PATH as harness-digest's, a log under `~/.claude/harness/deadcode/`. Once a day is enough: the candidates change only when main changes, and a day's batch is one PR.
- **Under spike 1's options** (docs/research/off-mac-loop.md):
  - MacBook only (the zero-cost baseline): the job runs when the Mac wakes, and the ticket it files waits for a session or the hourly dispatch LaunchAgent, which is not installed today.
  - Mac mini at home (the recommendation): the plist moves unchanged, since it is the same OS, the same paths and the same launchd. The mini's hourly `dispatch --all-projects --max 8` runs the ticket, because a dead-code ticket needs nothing local (#444, proposed, would let dispatch there skip the ones that do). The owner still lands from either machine with `/harness:land`, which needs only gh.
  - A Linux VPS would need the plist as a systemd timer; it is not recommended there anyway.
- **What is a tool and what is an agent:**
  - Deterministic (`harness-deadcode`, #453 and #455): the scan, the exclusion rules, the rejected fingerprints, the batch, and the ticket body with each candidate's evidence (its references by kind, the test cases that reach it, the docs/design.md lines that name it). It makes no gh call beyond the board and the closed-PR list, and no model call.
  - Agent (`/harness:work N` on that ticket, the normal loop): the test-author removes the named test cases, the coder removes the code and updates docs/design.md, and the reviewer checks each removal against the candidate's evidence. Any role that finds a real use drops that candidate and says why, and the dropped fingerprint goes to the rejected list.
- **How it fits the board:** a `refactor` ticket, not a PR without a ticket. A ticket gets the tests-first roles, the red record (a refactor needs one of any kind), the Cost block, worklog attribution and `Closes #N`; a bare PR would draw `harness-pr land`'s "closes no issue" warning and skip all of it. The job files the ticket `status:ready` with `manual-land` under a standing epic. Ready rather than proposed is the owner's call: proposed would add `harness-board promote` as a second human step on top of the land.
- **Batch size:** at most 5 candidates and at most 150 removed non-test lines a ticket, which keeps it S or M under the size cap and well inside autoland's backstop (`MAX_NONTEST_FILES = 8`, `MAX_NONTEST_LINES = 400`). Today's whole list (five candidates, about 19 lines of code) fits in one M ticket. Test-only candidates go last in a batch, because they cost a test-author round.
- **Removing what docs/design.md specifies:** four of the five true candidates are named in docs/design.md (`store_dir` under backups.py, `KEYS` under routing.py, `has_wave_review` under waves.py, `--yes` under `land`). The project rule (a change to a tool updates its contract in the same PR) means the batch's PR edits those lines too. The scanner puts the line numbers in the ticket body, and the reviewer checks them.
- **When the owner closes the PR unmerged:** the next daily run lists closed, unmerged PRs labelled `manual-land` from dead-code branches, reads the fingerprints their ticket listed, and adds them to `~/.claude/harness/deadcode/<repo-key>/rejected.json`. A fingerprint is the path, kind, name and the sha256 of the definition's source, so the same candidate is not proposed again until its code changes. A candidate the owner wants kept for good gets a `# deadcode: keep (<reason>)` comment in the code instead, which survives renames.

### Code reached only by its own tests

The rule is "never delete, skip or loosen a test to get green", and the coder may not edit tests. The removal fits inside both, because the deletion is the ticket's stated scope and the test-author makes it before any source changes:

1. The daily job writes the ticket's **Tests that must pass** as the test cases that reach only the removed code (from the scanner's `refs: test` evidence), marked "removed with the code", plus the modules that stay green.
2. **Test-author stage:** it deletes exactly those test cases. Nothing fails, because the code is still there, so `harness-red check` records `green`. On a `refactor` ticket that is enough: tests_first allows the coder's source edits once any record exists (Verified). When the deletion takes a whole test file and nothing else, `check` refuses "No test changes on this branch", so #454 gives that case a `removed` record.
3. **Coder stage:** it removes the code and the docs/design.md lines, and runs `harness-red run`.
4. **Reviewer:** each deleted test case pinned only removed symbols, and each kept test still passes. The owner's review at the land is the second check.

The tests are not deleted to get green: they are green before the deletion and after it. They go because the behaviour they pin goes, by a ticket the owner reads.

### The review gate

- **Label: the existing `manual-land`.** Its description, "Land by hand: autoland skips it", already says a person lands it. autoland's rule 4 already reads it on the PR or the ticket, and no issue carries it today (Verified), so one label means one thing everywhere. A new `human-review` would need adding to every project's label table (`harness-board labels ensure`), and it would describe the review rather than the land, which is what the gate controls. The gate follow-up (#451) changes the description to "Land by hand: only /harness:land lands it".
- **How `harness-pr land` refuses the loop (#451):** `cmd_land` reads the PR's labels and its closing tickets' labels. With `--loop` and `manual-land`, it refuses before anything merges, with its own exit code (9) and `PR #P is manual-land: the owner lands it with /harness:land P`. Without `--loop`, which is how `/harness:land` calls it, it lands as today.
- **How `/harness:land` gets through and the loop does not (#452):** `--loop` is a flag the caller sets itself, so on its own it only stops a loop that follows its skill. The owner check is a PreToolUse hook on Bash. For a part that lands PR P (`harness-pr land P`, `gh pr merge P`, `gh api .../pulls/P/merge`) on a `manual-land` PR, it allows the call only when the event has no `agent_id` (the main session, not a subagent) and the newest transcript entry with `origin.kind: human` is the `/harness:land` command whose args start with P. It denies any `claude --bg` or `claude -p` launch whose prompt starts with `/harness:land`. `disable-model-invocation: true` keeps the model from invoking the skill through the Skill tool, so that transcript entry comes only from a prompt someone typed or a session someone launched.
- **How strong it is:**
  - It stops the loop following its skill (the label refusal), a subagent, a main session landing on its own initiative after any other prompt, `gh pr merge`, and a session starting another session with the land prompt.
  - It does not stop a session that writes a forged human entry into its own transcript file, merges through the GitHub web API by some path the walker does not parse, or a person who runs `gh pr merge` in a terminal. Each of those is a deliberate act, visible in the transcript, and the review convention forbids it; harness-pr's revert undoes a land.
  - GitHub cannot enforce it: on a personal repository the owner is the PR's author through gh, and GitHub does not let an author approve their own pull request, so a required review would block the owner too.
  - So it is a strong guard against the loop and against accidents, and a weak one against a session set on bypassing it. That is the level the rest of the harness keeps, since `harness-pr land`'s own gates are of the same kind.
- **`/harness:work` (#451):** step 9 runs `harness-pr ready` as today, which sets `status:in-review` and notifies the owner. Step 10 reads the ticket's labels. On `manual-land` it ends the turn and reports "PR #P waits for /harness:land P". This is not a park: no `harness-board park`, no blocked label, no follow-up. An exit 9 from the land, should the skill reach it, ends the turn the same way.
- **`harness-work dispatch` and `runs --sweep` (#451):** `ask_before_landing` returns None for a ticket labelled `manual-land`, using the labels it already reads. `ticket_finished` then counts the in-review ticket finished, so the sweep stops its bg session and closes its record, and `redispatch_candidates` does not pick it up. Without this change, #422's rule would redispatch the gated ticket after every hourly tick until `REDISPATCH_MAX` (3) runs had ended within 7 days.
- **The standing rule:** user/CLAUDE.md says the loop lands every PR with `harness-pr land --catch-up` and a session merges no other way, and the settings snippet's review convention says every PR lands without asking. #451 writes the exception into docs/design.md, user/CLAUDE.md and user/settings.snippet.json: a `manual-land` PR is landed only by the owner's `/harness:land`.

### Beyond Harness

Harness first. One proposed follow-up per other registered project (`harness-projects list`, 12 of them), each to decide whether the scan is worth it there and to write its rules:

| Project | Language and what the scan needs | Follow-up |
|---|---|---|
| jotform-glow-sync | Python plus Apps Script (.gs) and JavaScript; entry points are scripts/, the Makefile and Apps Script functions named only by triggers, menus and `google.script.run`; vulture and coverage.py as dev dependencies; billable, so the owner decides first | #456 |
| okinawa-dream-reservation | TypeScript on Astro and a Cloudflare Worker, plus Apps Script; entry points are src/worker.ts, src/cron/, the file-based routes and migrations; knip and vitest's v8 coverage; billable | #457 |
| Assistant | Python on uv plus Apps Script; the console script and the pushed Apps Script functions; vulture and pytest-cov | #458 |
| edu | Python on uv; the console script and the local server's decorator-registered routes; vulture with a decorator whitelist, pytest-cov | #459 |
| Kalshi | Python on uv; the console script and the hand-run fixture recorders in scripts/; vulture and pytest-cov | #460 |
| FantasyGolf | Python on uv; the console script; vulture and pytest-cov; small | #461 |
| mcp-art-lab | Python on uv; the console script and code run inside Blender through the MCP, which no suite reaches | #462 |
| kalshi-markets | Python on uv, about 8 files; small | #463 |
| HorseTrackBetting | Python on uv, 3 files today | #464 |
| a-chance-for-glory | scaffold only; nothing to scan yet | #465 |
| accessible-iceland | JavaScript with npm test, a few files; knip and node's coverage | #466 |
| PilatesAttendance | no code and no git repository yet | #467 |

The Python projects run pytest under uv with a dev dependency group, so for them a dev-only vulture and coverage.py cost one line in pyproject.toml, unlike Harness, which ships standard library only.

### Mutation testing (spike 3)

Spike 3 (#442, `docs/research/mutation-testing.md`, unclebob/mutator) has not landed. Its tool also flags uncovered code for deletion, and it uses coverage.py. If it is adopted, its coverage.py run of the suite can feed this scanner as well (coverage.py's `executed_lines` is the shape #474's data takes), so the suite is not collected twice. Whichever note lands second points to the other. When this was written #442's note did not exist, so this note names its planned path, and #442's note points here if this one lands first.

## Proposed follow-ups

All are `status:proposed` sub-issues of #439, filed without `--blocked-by`; the order below is the order to promote them in.

- #451 Refuse the loop's land of a manual-land PR and keep it in review (`harness-pr land --loop` exit 9, the work skill's stop at ready, dispatch and sweep, and the exception in docs/design.md, user/CLAUDE.md and the settings snippet). M, risk:high.
- #452 Let only the owner's /harness:land land a manual-land PR (the PreToolUse owner check). M, risk:high.
- #453 Add harness-deadcode scan, a standard-library dead-code scanner. M.
- #474 Collect suite and loop-smoke line data for the dead-code scanner with a stdlib tracer. M, needs-local.
- #455 Run the dead-code scan daily and file a manual-land refactor ticket (the LaunchAgent, the batch, the rejected fingerprints). M, needs-local.
- #454 Give a refactor branch whose only test change deletes test files a red record. S, risk:high.
- Rollout, one per other registered project, each S and no-tests: #456 jotform-glow-sync, #457 okinawa-dream-reservation, #458 Assistant, #459 edu, #460 Kalshi, #461 FantasyGolf, #462 mcp-art-lab, #463 kalshi-markets, #464 HorseTrackBetting, #465 a-chance-for-glory, #466 accessible-iceland, #467 PilatesAttendance.

The first batch the daily job would file is today's five candidates; no separate ticket is filed for them, so they are not removed twice.
