# Research notes: Uncle Bob's ub-mode mutation testing for the harness

Gathered 2026-11-24 for #442 (epic #439) on the MacBook Pro (Apple M1 Pro, 8 cores, macOS 26.5.2). Sources: the four repositories below, cloned into empty directories under the job's scratch directory and read before anything ran; a scratch clone of Harness at main `8cbcb78` (put on a local branch `scratch/mutation-spike`); and the Harness files named in each section. The tools were installed only into two new venvs in the scratch directory (one from Homebrew's python3.14 for mutator and crapper, one from /usr/bin/python3 3.9 for coverage.py). Nothing was installed into /usr/bin/python3, Anaconda or any other existing interpreter. Nothing in this repository changed but this note. One thing landed outside the scratch directory, tree-sitter-language-pack's grammar cache (see Verified). It was moved into the scratch directory after the runs.

## Verified

### The repositories (read 2026-11-24)

| Repository | Commit read (`git rev-parse HEAD`) | Branch | Last commit | License |
|---|---|---|---|---|
| https://github.com/unclebob/ub-mode | `3da62bf3fcfd2034de41f30d920d2739e566171f` | master | 2026-11-02 | MIT ("Copyright (c) 2026 Robert C. Martin") |
| https://github.com/unclebob/mutator | `29afb427129b6bf03699286966fc55ec8a4830d7` | main | 2026-11-18, 13 commits since 2026-09-30 | 0BSD ("BSD Zero Clause License") |
| https://github.com/unclebob/crapper | `c757ec79a6c15dc815238bbd9c2f4601eb54d3cc` | master | 2026-11-10, 10 commits | 0BSD |
| https://github.com/unclebob/dryer | `66ff6d21a42c04afcad89c78a80066176d1294b0` | master | 2026-10-03 | 0BSD |

- **ub-mode is a prompt, not a tool.** `README.md`: "`ub-mode` is an agent skill that enforces disciplined software engineering principles when writing or changing code." Its "Then Make it Right" step names "[Mutator](https://github.com/unclebob/mutator) (cover the uncovered, kill the survivors, delete dead code)". `SKILL.md`, section `## Mutator`, in full: "Use the mutator tool / Cover the uncovered. / Kill the survivors. / Delete dead code." The skill holds no code; the mutation testing is all in mutator.
- **What mutator does** (`mutator/README.md`): "A mutant is killed when the tests fail or time out. It survives when the tests still pass. A site on a line the coverage report does not hit is uncovered and is not run." Python mutations: "the Java set, spelled `and`/`or`, `not`, and `True`/`False`", the Java set being "`true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, delete `!` and unary `-`, `0`/`1`". A mutant's timeout "is ten times that baseline, and at least two seconds". Exit codes: "`0` when every executed mutant was killed. Exit `2` when the baseline tests fail. Exit `3` when a mutant survives."
- **Which files it treats as Python:** by suffix only. `crapper/src/crapper/discover.py`: `EXTENSIONS = {... ".py": "python"}` and `language_of` returns `EXTENSIONS.get(file_path.suffix)`; `is_test_file` drops `test_*.py`, `*_test.py`, `conftest.py` and anything under a `tests` directory. mutator picks its files through these (`cli.py`, `_is_source`). So Harness's hooks (`plugins/harness/hooks/*.py`) and `lib/harness_lib/*.py` are seen; the ten bin tools (`plugins/harness/bin/harness-*`, no suffix) are not. Paths are resolved before the suffix check (`_positionals`: `candidate.resolve()`), so a `.py` symlink to a bin tool does not help.
- **Python version:** the tool itself needs 3.11 or newer. `pyproject.toml` of mutator, crapper and dryer: `requires-python = ">=3.11"`; `mutator/src/mutator/runner.py` line 14: `Command = str | list[str]` at module level, which fails before 3.10. Run as the README says (`./mutator`, whose first run does `python3 -m venv "$ROOT/.venv"` and `pip install -e "$ROOT"`), with Anaconda's 3.10 first on PATH: `ERROR: Package 'mutator' requires a different Python: 3.10.9 not in '>=3.11'`. /usr/bin/python3 (3.9.6) fails the same way. It ran here from a venv of Homebrew's `/opt/homebrew/bin/python3.14` (3.14.10). The code under test can still run on 3.9: the test command is any command (`--test-command`), and every run below used /usr/bin/python3 3.9.6 for the tests.
- **unittest:** the default Python test command is "the project's `.venv` or `venv` Python running `pytest`, or `unittest discover`" (README), from the nearest `pyproject.toml`, `pytest.ini` or `setup.cfg` (`runner.py`, `_python_plan`). Harness has none of these and no `.venv`, so the default is mutator's own 3.14 running `unittest discover` at the root, which finds nothing. Measured: `Baseline failed for plugins/harness/lib/harness_lib/routing.py: .../venv-mutator/bin/python3.14 -m unittest discover`, "Ran 0 tests", "NO TESTS RAN", exit 2. `--test-command "<cmd>"` runs any shell string ("Run this command for the baseline and every mutant"), so unittest works through it.
- **What it installs, and where:**
  - mutator and crapper: `./mutator` and `./crapper` create `.venv` inside their own checkout and `pip install -e` there. Their dependencies are `tree-sitter>=0.24` and `tree-sitter-language-pack>=0.7` (resolved here to 0.27.0 and 1.25.2, MIT). mutator imports crapper "from the environment or the sibling checkout" (`crapper_link.py`: `project.parent / "crapper" / "src"`, else "Clone github.com/unclebob/crapper next to mutator."), so crapper must sit at `../crapper` or be installed in the same venv (done here).
  - At run time `crapper/src/crapper/languages/treesitter.py` calls `tree_sitter_language_pack.download([language])`. The first scan downloaded a 26 MB bundle into `~/Library/Caches/tree-sitter-language-pack/v1.25.2/` (outside every venv and outside the scratch directory). So a run needs the network once per version.
  - Coverage: crapper's README says "`coverage` and `pytest` are installed into the project's interpreter when they are missing." The code (`runners.py`, `_python_executable`): the project's `.venv/bin/python` or `venv/bin/python`, else `"python3"`, then `_ensure_python_module` runs `[py, "-m", "pip", "install", "--disable-pip-version-check", module]`. In Harness, which has no `.venv`, `python3` is Anaconda's, so a default run would pip-install coverage into Anaconda. No run here used that default: each passed `--coverage-command` (the scratch 3.9 venv's coverage.py 7.10.7) or `--no-coverage`, so that code never ran.
- **What it writes into the tree:** `.metrics/mutate/<namespace>.edn` (the snapshot), `target/mutation-workers/run-<uuid>/worker-N/` (one symlink overlay per worker, removed when the file finishes), `target/mutator-backup/` (restored after an interrupted run), and, through crapper's coverage, `target/coverage/python/lcov.info`. mutator's own `.gitignore` lists `.metrics/` and `target/`; Harness's `.gitignore` lists neither, so a run in a worktree leaves untracked files that `git status`, the hooks and the pre-commit hook see.
- **Workers** (`workers.py`): "Each worker is a directory under target/mutation-workers. Project files are linked in. The file under test is a private copy." Only the directories on the mutated file's path are real; every sibling is a symlink to the real tree (`_overlay`, `_link_children`). Worker count: `max(1, min(site_count, processors, limit))`, so 8 on this Mac.
- **Coverage misses code the tests reach through a tool.** coverage.py, run in the test process as crapper runs it, does not follow subprocesses. test_cost.py and test_pr_ultra.py reach `cost.py` through `bin/harness-work` and `bin/harness-pr` run as processes, so the report has no entry for it. The README: "A file missing from the report is treated as uncovered". So mutator marked all 164 sites uncovered, ran none and exited 0. Measured: `Total 0 0 164 164 n/a`, exit 0, 159 s. Only `--no-coverage` made it run them.
- **The overlay breaks Harness's way of finding its code.** Harness's tests find `lib/` through `TESTS = Path(__file__).resolve().parent` (`test_harness_lib_routing.py` line 43, `test_harness_lib_waves.py` line 62, `guards_support.py` line 30), and bin tools and hooks through `os.path.realpath(__file__)` (docs/design.md, Layout; `bin/harness-runner` line 29). In a worker `plugins/harness/tests` and `plugins/harness/bin` are symlinks, so these resolve to the real tree and import the unmutated file, and every mutant survives. Measured below: 0 of 15 killed on routing.py, 0 of 27 on runners.py. The fix used here is a test command that replaces those links with copies before running the tests (`cp -R` of `tests/`, and of `bin/` and `hooks/` for lib modules a tool reaches). It is about ten lines of shell, kept in the scratch directory. A sanity check: with it, sites in the two routing.py functions the module's tests never call (`cached_code_route`, `next_route`) survive, so the command passes in a worker and the kills are real test failures.

### Harness's own side

- docs/design.md, Principle 9: "**Standard library only.** `python3` 3.9 or newer, `git`, `gh`. Nothing to install."
- Principle 2 and the Agents table: the test-breaker runs "only on a `risk:high` ticket that changes a hook's decision path or a tool that destroys or rewrites data", "for one round", and writes "the cheapest implementation that could pass the new tests (constants, special-casing the fixtures)"; its gaps go to the coder as `Requirement:` lines and to the reviewer as check items. Failure policy: "`breaker` runs one round (#375) ... never sends the tests back for a second round and never answers `park`".
- `plugins/harness/agents/test-breaker.md`: "Do not chase unlikely inputs: chained or nested symlinks, exotic shell expansion and quoting, hostile file names, races between separate sessions, platforms the project does not run on." The reviewer row: "'No test pins X' is no finding when X is an adversarial or extreme case a real session would not hit."
- The tests-first proof (Principle 2, harness-red): a new test must fail against the unchanged code. A test written to kill a surviving mutant passes against the real code by construction (the code is right; only the mutant is wrong), so `harness-red check` would record it `green`, which "proves nothing" (`tests_first.py`, `source_refusal`).
- Ticket #426's Cost block (`harness-board show 426`): its one tests round was 1.9M tokens, 3 min model time, 4 min agent time; start to land 36 min.
- Recent squash commits on main (`git show --stat`): #426 changed `bin/harness-pr`, `bin/harness-work` and `lib/harness_lib/cost.py`; #422 only `bin/harness-work`; #428 only docs and skills; #420 `bin/harness-runner` (350 lines), three other bin tools (digest, scaffold, standup) and `lib/harness_lib/runners.py` (141 lines, new). mutator sees 2 of those 8 source files.

## Not verified

- **The full survivor lists were not triaged.** The 4 waves.py, 5 runners.py and 7 cost.py-sample survivors were read one by one (below). Of readonly_allow.py's 119, only the 21 in `grep_reads` were sampled. Which of them widen what the hook allows, rather than make it stricter, is not known.
- **Wall times are from one run each**, on this Mac with the owner's sessions running beside them. Per-mutant times grew under 8 workers: waves' tests take 1.3 s alone and the 33 mutants took 25 s, about 5 s per mutant per worker.
- **A whole run over hooks/, lib/ and bin/ was not made.** Its cost below is extrapolated from the measured runs.
- **mutmut and cosmic-ray were not tried.** The outline asks for them only if mutator cannot run Harness's Python; it can, with the overlay fix.
- **GitHub Actions for a periodic run:** docs/research/off-mac-loop.md quotes "The use of standard GitHub-hosted runners is free: * In public repositories" and "a GitHub-hosted job runs at most 6 hours", and Harness's CI runs on `ubuntu-latest` (`.github/workflows/ci.yml`). No mutation job was run there.
- **A standard-library mutator** was not prototyped. That Python 3.9's `ast` gives every node `lineno`, `col_offset`, `end_lineno` and `end_col_offset` (since 3.8) is from the standard library documentation, not from a run.

## Measurements

All runs: mutator `29afb42` and crapper `c757ec7` from the scratch 3.14 venv; tests on /usr/bin/python3 3.9.6; coverage through `--coverage-command` (coverage.py 7.10.7, the scratch 3.9 venv, `--source=plugins/harness`), except the row marked `--no-coverage`; the scratch clone of Harness at `8cbcb78`; 8 workers (the default, one per core). Wall time is `/usr/bin/time -p` real for the whole mutator call, the coverage run and the baseline included. "Tests alone" is the test modules' own time on 3.9, run once before.

| File | Test modules (tests alone) | Sites | Killed | Survived | Uncovered | Score | Wall | Note |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `lib/harness_lib/routing.py` | test_harness_lib_routing.py (0.2 s) | 19 | 0 | 15 | 4 | 0 % | 3.0 s | default overlay: every mutant survives |
| `lib/harness_lib/routing.py` | the same | 19 | 15 | 0 | 4 | 100 % | 2.2 s | `tests/` copied into each worker |
| `lib/harness_lib/waves.py` | test_harness_lib_waves.py (1.3 s) | 34 | 29 | 4 | 1 | 87.9 % | 25 s | |
| `hooks/readonly_allow.py` | test_readonly_allow.py (9.1 s) | 330 | 172 | 119 | 39 | 59.1 % | 541 s | |
| `lib/harness_lib/runners.py` (#420) | test_runner.py (7.6 s) | 29 | 0 | 27 | 2 | 0 % | 166 s | `tests/` copied only: the tool still loads the real lib |
| `lib/harness_lib/runners.py` (#420) | the same | 29 | 22 | 5 | 2 | 81.5 % | 109 s | `tests/`, `bin/`, `hooks/` copied |
| `lib/harness_lib/cost.py`, lines #426 changed | test_cost.py, test_pr_ultra.py (37 s + 86 s) | 0 | - | - | - | - | - | `--scan --lines`: no site on the 9 changed lines |
| `lib/harness_lib/cost.py`, `--lines` 104-127 (14 sites) | the same | 164 | 0 | 0 | 164 | n/a | 159 s | with coverage.py: the whole file reported uncovered, nothing run, exit 0 |
| `lib/harness_lib/cost.py`, the same 14 sites | the same | 14 | 7 | 7 | 0 | 50 % | 419 s | `--no-coverage`; `tests/`, `bin/`, `hooks/` copied |

What the survivors are:
- **waves.py (4):** `if not isinstance(wave_id, str) or not WAVE_ID.fullmatch(wave_id)` with `or -> and`, three times (lines 83, 101, 117): a non-string or malformed wave id, which no caller passes. And `_suffix`'s default `1 -> 0` for an id without a suffix (line 42).
- **runners.py (5):** two are equivalent mutants, which no test can kill: `slug.split("/")[-1]` to `[1]` on a slug already checked to hold exactly one `/` (line 86), and `max(1, min(8, len(paths)))` to `max(0, ...)` after `if docker is None or not paths: return` (line 123). Three are defensive guards: an empty slug (42), a non-dict runner entry (88), a missing docker (99).
- **cost.py sample (7 of 14):** mixed.
  - `agent.rsplit(":", 1)[-1]`, mutated twice (line 125): changes which stage an agent without a `#N <stage>` description counts under (`harness:coder` would count as `other`). That is real behavior, if secondary, since the loop always sets the description.
  - `bool(branches) and all(...)` to `or` (line 108): counts a session with no branch as the ticket's own.
  - Line 124 is a non-string `agentType` guard, line 113 a flag on the orchestrator unit, and line 111 (two sites) the orchestrator's message filter.
- **readonly_allow.py (119):** by function, `git_reads` 37 of 73 sites survive, `grep_reads` 21 of 31, `sed_reads` 12 of 37, `scan` 9 of 23, `cd_moves` 8 of 18, `Places.git` 7, `Places.tree` 6, `reader_reads` 5, `decide` 3, and 11 more with 1 or 2. This is a hook's decision path, the case the breaker exists for. The sample of `grep_reads` holds stricter mutants (pattern `operands[:1]` to `[:0]`, line 259, which only checks one more word) and others whose effect needs reading the code, such as dropping the strict flag on grep's file operands (line 264, `True -> False`, which skips `unread()`'s glob and expansion check for them).

Cost per ticket, on this Mac:
- **Sites:** about 1 per 2-4 lines of Python (routing.py 19 sites in 153 lines, waves.py 34 in 205, readonly_allow.py 330 in 543, cost.py 164 in 549, runners.py 29 in 141).
- **Per mutant:** the ticket's own test modules once each, 8 at a time, and slower when they run together. Measured (wall divided by waves of 8): about 5 s per mutant for 1.3 s tests (waves.py), about 13 s for 9.1 s tests (readonly_allow.py), about 25 s for 7.6 s tests (runners.py), and about 150 s for #426's 123 s of tests (the cost.py sample: 419 s for the baseline and 14 mutants in two waves of 8).
- **#420** (runners.py, new): 109 s.
- **#426** (cost.py): 0 s for the lines it changed, which hold no site. The changed logic was in `bin/harness-pr` and `bin/harness-work`, which mutator cannot see. The whole of cost.py would be about 55 minutes: the baseline, then 164 sites in 21 waves of 8 at about 150 s each, each mutant's timeout 10 × 123 s.
- **#422 and #428:** nothing to mutate (bin tool and docs only).
- So on the four latest tickets a stage would have run from 0 s to about 2 minutes for most, and up to about an hour when a ticket touches a module whose tests take near 100 s and its whole file is mutated. Any survivor sent back costs a tests round besides: #426's was 1.9M tokens and 4 minutes.
- **A periodic run:**
  - hooks/ is 5,803 lines, lib/harness_lib 5,266 and bin/ 12,469, so about 8,000 sites at one per 3 lines.
  - The hooks alone, at readonly_allow.py's 1.6 s of wall per site, take about an hour on this Mac. The guards' modules are among the ~100 s ones, which makes it longer.
  - All of it, at an average of 30 s per mutant per worker, is about 8 hours on 8 cores, and more on a 4-core hosted runner.
  - So one pass is a set of shards (hooks one month, lib the next, bin tools over several), each within a 6-hour Actions job or a night on this Mac. It does not fit inside a ticket.

## Overlap with the test-breaker

| | test-breaker | mutation testing |
|---|---|---|
| When | after the red run, before any code | after green, against the real code |
| Which tickets | `risk:high` and a hook's decision path or a data-destroying tool | any file it can see (here: `.py` only) |
| What it asks | can a cheat (a constant, special-cased fixtures) pass these tests? | does each small change of the real code fail some test? |
| Finds | tests that check one fixed output, an input never varied, a definition-of-done item with no test | operators and constants no test depends on: off-by-one, `and`/`or`, a guard no test reaches, dead branches, equivalent code |
| Misses | gaps in how the real code is written (it never sees that code); a test that is broad but blind to one branch | a missing feature (there is nothing to mutate); tests that pass for the wrong reason when the code is also wrong; bin tools here |
| Output | `Requirement:` lines for the coder, check items for the reviewer, one round | a list of survivors, many of them equivalent or defensive (5 of 5 in runners.py, 4 of 4 in waves.py; some of the cost.py sample's 7) |
| Cost | one agent round, about ten tool calls | minutes to hours of test runs, no tokens until someone reads the survivors |

Neither replaces the other. The breaker judges the tests against the definition of done before code exists, so it catches a missing behavior, which mutation cannot see. Mutation judges the tests against the code as written, so it catches a branch that no test reaches, which the breaker cannot see. The breaker is also bounded (one round, likely gaps only), while mutation returns everything it found, equivalent mutants included. Much of what it returned here is what the breaker's own rules and epic #374 say not to pin.

Spike 2 overlaps here: mutator's "uncovered" sites and "delete dead code" are the dead-code question. #441 (`docs/research/dead-code-agent.md`) and its proposed scanner #453 cover suite coverage and dead code. A mutation report should hand its uncovered sites to that scan rather than file its own tickets for them.

## Recommendation

**(c) A periodic, report-only mutation run on main, over hooks/, lib/harness_lib and bin/, with a standard-library mutator kept outside the plugin (which, unlike mutator, sees the bin tools).**

Why not the others:
- **(a) A work-loop stage after green: no.**
  - It sees only 2 of the 8 source files the four latest tickets changed.
  - On #426 the lines the ticket changed held no site at all. On a whole file whose tests take 100 s, it costs about an hour.
  - Its survivors would go to the test-author, and a test that kills a survivor passes against the real code, so `harness-red check` cannot prove it red. The stage would need its own exemption from the tests-first proof.
  - 9 of the 16 survivors read here (all of waves.py's and runners.py's) were equivalent mutants or defensive guards, the extreme-case tests epic #374 stopped writing. The 7 in the cost.py sample are more mixed (Measurements), and they sit in code #426 did not change, so a stage limited to changed lines would not have shown them.
  - A survivor round adds about 1.9M tokens and 4 minutes (a tests round), and a second loop stage with its own cap and failure policy.
- **(b) Reviewer input: no.** It has the same run time as (a) on every ticket. The reviewer must already drop "no test pins X" for extreme cases, which is most of the list, so it would spend review tokens throwing survivors away.
- **(d) None: not quite.** readonly_allow.py's 59 % score on its own tests, with 21 of 31 sites in `grep_reads` surviving, is a lead worth a look on a hook that decides what runs without a prompt. A run over the hooks every month or so finds such leads at no token cost until someone reads the report.

How (c) works:
- Monthly, one shard at a time, and on demand, on main. Where it runs ties to spike 1 (docs/research/off-mac-loop.md): Harness is public and its suite already passes on `ubuntu-latest`, so a scheduled GitHub Actions job runs it free, with no secret and no laptop, one shard per run within the 6-hour job limit. The report is a workflow artifact. On this Mac, or on the Mac mini that note recommends, the same command runs from a LaunchAgent at night.
- The report lists survivors by file and function. A function where no mutant was killed is listed apart, for the dead-code scan (#453), not as a test gap.
- A person, or a session the owner starts, reads it. Only a survivor that changes what a hook allows or denies, or what a data-destroying tool deletes, becomes a `status:proposed` ticket. Equivalent mutants and defensive guards are dismissed. Nothing goes to the test-author automatically, and nothing blocks a ticket.

Dev-only tool or the standard library alone: **the standard library alone, kept outside the plugin** (`scripts/`, like `scripts/run_suite.py`).
- Principle 9 binds the plugin, which every project loads. A dev-only tool outside it could in principle need an install.
- mutator, though, needs a second interpreter (3.11+), two compiled dependencies and a 26 MB download at run time. Its defaults would pip-install coverage into Anaconda. It cannot see the ten bin tools. It needs a shell wrapper, or every mutant silently survives. And its coverage step silently runs nothing for code the tests reach through a tool (cost.py: 164 sites skipped, exit 0). It is also a 13-commit, eight-week-old project.
- A mutator in the standard library avoids all of that:
  - it runs on 3.9;
  - it parses any file whose first line is a python3 shebang, bin tools included;
  - it uses mutator's operator set (the Python column above);
  - it mutates a full copy of the tree (`cp -c` clones a 7 MB checkout on APFS), so `realpath` finds the mutated file;
  - it runs each file's own test modules through `HARNESS_TEST_MAP` or the `test_<name>.py` convention.
- `ast` gives the source positions it needs. It is about the size of the dead-code prototype (#453 says about 300 lines).
- It needs no coverage step: coverage.py is not in the standard library, and the measured one hid cost.py entirely. Every site runs, and one no test reaches survives and is reported. The dead-code work's coverage JSON (#453) can narrow a run later.

Tradeoffs: building the tool is a ticket of its own (size M), where mutator is free. A monthly report finds a gap weeks after the ticket that made it landed, not before. And someone has to read the report; a report nobody reads costs Actions minutes, which are free here, and nothing else.

## Proposed follow-ups

All are `status:proposed` under #439 and block nothing.

- #475 Add a standard-library mutation runner for Harness (`scripts/mutate.py`), size M:
  - Python 3.9, `.py` files and python3-shebang files;
  - mutator's Python operator set;
  - full-copy workers and per-file test modules;
  - a JSON and a text report.
  - Nothing in the plugin changes.
- #476 Run a monthly, report-only mutation job on main, size S:
  - a scheduled and manual GitHub Actions workflow, one shard per run, the report as an artifact;
  - the triage rule written down: only survivors that change a hook's or a data-destroying tool's decision become proposed tickets.
  - It needs #475 first.
- #477 Triage readonly_allow.py's 119 surviving mutants, size S, a tests-only refactor. Pin only the ones that widen what the hook allows. The survivors come from the measured run above and can be reproduced with the recipe in Verified, or with #475.
- #478 After three monthly reports, decide whether other projects get the same run, size S, docs only. It is one decision for all of them, not one ticket per project.
