# Proposal: one shared module for the harness plugin

Ticket #55. Written 2026-10-16 against the code on main at ec9008a, from the area reviews (bin/harness-board and harness-projects; harness-work; harness-pr; harness-red and harness-scaffold; the hooks; standup, digest and worklog; the test fixtures), then verified against the worktree: every line number below was opened, and every "identical" claim was re-checked by hashing each function's AST (docstrings and positions stripped) across the files that carry it. The loader was run as an experiment outside the repo, by every route the tools and hooks are reached (below).

## Summary

`plugins/harness/bin/` holds 9,286 lines in nine tools and `plugins/harness/hooks/` 4,342 lines in seven hooks: 13,628 lines in all. About 2,400 of them are copies: the same function pasted into two to nine files because docs/design.md:39 says every script is standalone. Five of those functions are identical in up to nine files; most of the rest differ in one or two named details that a shared version must keep as parameters.

The ten refactors below move the copies into `plugins/harness/lib/harness_lib/`, a stdlib-only package every tool and hook finds relative to its own real path. Expected result: bin/ and hooks/ lose about 2,400 lines and gain about 190 lines of import prologue, lib/ gains about 950 lines, for a net reduction of about 1,300 lines (10 percent) and, more to the point, one definition of each helper instead of up to nine.

The top three by lines removed against risk:

1. The state-key helpers (`canonical_path`, `same_entry`, `path_key`, `repo_key`, `adopt_old_state`), identical in nine files: about 380 net lines, low risk, and the ticket that establishes the loader.
2. CLI and text boilerplate (`Problem`, `Parser`, the `__main__` tail, `table`, `emit`, the line pickers, `parse_iso`, `read_json`, the atomic writers, `home`): about 240 net lines, low risk once the named differences are kept explicit.
3. readonly_bash.py's 197-line older copy of branch_guard.py's shell walker: about 200 net lines, medium risk because the newer walker emits marker parts the old one does not.

Everything else is 35 to 100 lines each. The whole list is 16 small tickets, one epic.

## How a shared module is loaded

### Where the code sits

```
plugins/harness/
  bin/harness-board            (unchanged location, shebang, mode 755, no .py suffix)
  hooks/branch_guard.py        (unchanged; hooks.json keeps `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/x.py`)
  lib/harness_lib/__init__.py  (empty but for `from __future__ import annotations`)
  lib/harness_lib/paths.py     (and cli.py, text.py, notify.py, ... one module per refactor)
```

The package name is `harness_lib`, not `lib` or `paths`: a tool runs with the project's cwd, an in-process test loader runs with `''` at `sys.path[0]`, and a project may well have a `paths.py` or `settings.py` of its own. Nothing under lib/ is named `test_*`, so `python3 -m unittest discover -s plugins/harness/tests` never sees it (discover starts in tests/, not lib/, anyway).

### The prologue

Every bin tool and every hook carries the same seven lines, after its stdlib imports and before the first use of a shared name:

```python
_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.paths import canonical_path, same_entry, path_key, repo_key, adopt_old_state
except Exception as exc:                        # a missing or broken lib: one line, never a traceback
    sys.exit(f"harness-board: cannot load harness_lib from {_LIB}: {exc}")
```

Why each piece is the way it is, checked against the files:

- `os.path.realpath(__file__)`, not `abspath`, `sys.argv[0]` or `sys.path[0]`. The bin tools are reached four ways: by absolute path (the tests, `[sys.executable, TOOL]`), via PATH (the plugin mechanism puts `bin/` on the Bash tool's PATH while the plugin is enabled, docs/research/claude-code-platform-v2.md:32; a user may also symlink one into `~/.local/bin`), as a sibling (`harness-work:252-260` and `harness-pr:471-479` find harness-board on PATH, else next to themselves via `HERE = os.path.dirname(os.path.realpath(__file__))` at `harness-work:70` and `Path(__file__).resolve().parent` at `harness-pr:45`), and as a module loaded by path (`tests/test_repo_key.py:776-783` and `tests/test_tools.py:36-43` use `SourceFileLoader`; `tests/test_hooks.py:151-157` and `tests/test_guards.py:158-164` `exec` the source into a bare module with only `__file__` set). `realpath` follows a symlink back to the checkout; `__file__` is the one thing all four routes set. harness-digest already does exactly this at `bin/harness-digest:620` to write its LaunchAgent path.
- Hooks are run as `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/<name>.py` (hooks/hooks.json) with the project as cwd, so `__file__` is the plugin's own path and the same expression finds `hooks/../lib`. No hook reads `CLAUDE_PLUGIN_ROOT`, and it stays that way: design.md:39 is right that the variable is not set for Bash, so a bin tool could not use it anyway.
- The plugin loads in place from the marketplace directory, `/Users/ryaneskuri/portfolio_projects/harness` (the lowercase spelling of the checkout, `~/.claude/plugins/known_marketplaces.json`); the copy under `~/.claude/plugins/cache/ryan/harness/0.1.0` is stale and is not what runs (design.md:41, docs/research/claude-code-platform-v2.md:33). `realpath` keeps whatever spelling it is given and `lib/` sits inside `plugins/harness/`, so the lib is found under either spelling, and would be found from a cache copy too if one ever ran.
- `sys.path.insert(0, ...)` ahead of the script's own directory, so neither a hooks/-local file nor a project file can shadow a lib module. hooks/tests_first.py:469-476 already does this (with `abspath`) to `import branch_guard`; the prologue generalises the one cross-file import the plugin has.
- The names are imported into the file's own namespace (`from harness_lib.paths import canonical_path, ...`), never reached as `harness_lib.paths.canonical_path`. `tests/test_repo_key.py:771-773` lists harness-board, -digest, -pr, -red, -standup, -work, session_brief.py and tests_first.py, loads each as a module and at 824-829 asserts `mod.canonical_path(given)` and `mod.repo_key(given)`; `tests/test_red.py` reads `red.glob_match`, `red.globs`, `red.TEST_PATTERNS`, `red.SOURCE_EXEMPT`, `red.Problem`, `red.settings_env` and `red.setting` the same way (728-760). A `from` import keeps every one of those attributes.
- The prologue runs at module top, not inside `main()`, for the same reason: the in-process loaders never call `main()`.

### What the experiment showed

A throwaway plugin tree under /private/tmp (a `bin/harness-x` with the tool prologue, a `hooks/h.py` with the hook prologue, a two-function `lib/harness_lib/paths.py`), run with a bare environment (`env -i PATH=... HOME=...`): the lib loads and `sys.path[0]` is the lib directory when the tool is run by absolute path from `/`, by name from another cwd with `bin/` on PATH, through a symlink in another directory on PATH, through a symlink to the whole plugin directory, and when the hook is run as `python3 <abs>/hooks/h.py` from a project directory; `SourceFileLoader` binds `canonical_path` on the module without running `main()`; `exec` into a bare module with only `__file__` set, from a cwd holding a shadow `harness_lib/paths.py` and with `''` at `sys.path[0]`, still gets the lib's function. A tool copied out of the tree exits 1 with `harness-x: cannot load harness_lib from <copy>/../lib: No module named 'harness_lib'`; with `lib/` renamed away the hook prints its one line and exits 0 and the tool prints its one line and exits 1; a syntax error in `paths.py` gives `cannot load harness_lib from <lib>: invalid syntax (paths.py, line 1)`. Run on the python3 this Mac has (3.10.9); 3.9 was not available here.

### What happens when the import fails

A bin tool: `sys.exit("<tool>: cannot load harness_lib from <path>: <error>")`, which is the tool's existing contract (one line on stderr, exit 1, never a traceback; design.md:39). `except Exception`, not `except ImportError`, so a syntax error in a lib module is reported the same way.

A hook: the same `try`, but the handler writes `<hook>.py: cannot load harness_lib from <path>: <error>` to stderr and exits 0:

```python
except Exception as exc:                        # fail open, like the __main__ wrapper below
    sys.stderr.write(f"branch_guard.py: cannot load harness_lib from {_LIB}: {exc}\n")
    sys.exit(0)
```

Today every hook's `__main__` block (branch_guard.py:1480-1485, readonly_bash.py:553-558, tests_first.py:674-679, session_brief.py:717-722, verify_on_stop.py:436-440, notify.py:133-138; after_edit.py:314-320 returns `main()`'s code and maps an exception to 0) catches `Exception` from `main()`, prints one line and exits 0. A module-level import sits outside that block, so without its own handler a missing lib would be a traceback and exit 1 rather than the one line and exit 0 the hooks promise. The handler keeps the policy exact: one line, exit 0, the call allowed. The cost is the one all the reviewers named: a lib that goes missing silently disables the guards. The suite runs every hook as a subprocess from the checkout, so a broken lib fails the suite; the loader ticket's test-author may add one additive test (run a hook with `lib/` renamed away, assert the one stderr line and exit 0) to pin the fail-open shape. Nothing in the existing suite changes.

### Cost at run time

Each lib module imports only the stdlib and defines functions; importing one costs about a millisecond after the first run compiles it. The hooks run under 10 s (guards), 20 s (session_brief), 30 s and 420 s timeouts (hooks/hooks.json); the budget is not touched. The compile writes `plugins/harness/lib/harness_lib/__pycache__/` into the checkout on first run (seen in the experiment), as `import branch_guard` from tests_first already writes `hooks/__pycache__/` in production today; the repo's .gitignore has `__pycache__/` and `*.pyc`. The hook tests route caches away with `PYTHONPYCACHEPREFIX` (tests/test_hooks.py:176, test_guards.py:182, test_session_brief_v2.py:97; the experiment confirmed the prefix catches the lib's bytecode too); the bin tests that load tools in-process set `sys.dont_write_bytecode` (test_repo_key.py:41, test_red.py:28, test_tools.py:26 and five others), and their subprocess runs will leave a gitignored `lib/harness_lib/__pycache__/`, which `tests/test_red.py:194` does not check (it walks the fixture worktree only). No `sys.dont_write_bytecode = True` in the prologue: it would make every hook recompile the lib on every tool call for no gain.

### Why the tests are unaffected

- Subprocess tests invoke tools and hooks by absolute path inside the checkout; `realpath(__file__)` finds `lib/` next to them. Their environments strip `PYTHONPATH` (test_red.py:108, test_repo_key.py:227, 912) or are built from scratch (test_hooks.py:171-180, test_guards.py:177-186); the lib is not found through `PYTHONPATH`, `PATH` or `HOME`, so none of that matters.
- The stand-ins the tests put on PATH in place of a sibling tool (test_work.py's `NOISY_BOARD` at 98, test_pr.py's `STUB` at 42) are their own scripts and import nothing.
- In-process loads set `__file__`, the prologue runs at import, and the `from` imports bind the names each test reads by attribute. Eight files loaded in one process share one `harness_lib` in `sys.modules`; that is fine for pure functions and is why a module-level constant a test monkeypatches on the tool (`tool.GH_BUDGET = 1`, tests/test_digest.py:449-453) must stay on the tool, named under refactor 7.
- Every bin file keeps its shebang, mode 755 and no suffix; only lib modules are `.py`. 3.9 holds by construction: `from __future__ import annotations` in every lib module, no `X | Y` at run time, no `match`, no `slots=True` on dataclasses. (Not run on 3.9 here; the Mac's python3 is 3.10.9.)

### The contract change

docs/design.md is the contract and two sentences would read as violated. Both change in the loader ticket:

- design.md:39, "Every bin script is standalone (no imports between scripts): argparse, standard library, finds nothing through `CLAUDE_PLUGIN_ROOT`", becomes: "Every bin script is standalone except for `plugins/harness/lib/harness_lib`, which it finds relative to its own real path (`os.path.realpath(__file__)`, so a symlink on PATH still reaches the checkout; a script copied out of the checkout does not run). No imports between scripts, nothing through `CLAUDE_PLUGIN_ROOT`. A missing lib is one line and exit 1 for a tool, one line and an allowed call for a hook."
- design.md:95, "The six tools and the hooks session_brief and tests_first carry byte-identical copies of `canonical_path`, `path_key` and `repo_key` ... branch_guard carries `canonical_path` and its `same_entry` helper", becomes: "The six tools and the three hooks import `canonical_path`, `same_entry`, `path_key`, `repo_key` and `adopt_old_state` from `lib/harness_lib/paths.py`; each binds the names it uses in its own namespace."

scripts/install.sh needs nothing: it registers the marketplace and installs the plugin (install.sh:14-30), copies only user/CLAUDE.md (install.sh:39), and merely prints where bin/ is (install.sh:126); the plugin itself loads in place.

## Ranked refactors

Lines: removed from bin/ and hooks/, then added to lib/, then net for the whole plugin. Both the removal and the lib figures are from counting the current copies (function bodies plus the blank lines between them); treat them as ±15 percent.

| # | Refactor | Files | Evidence | Removed / lib / net | Risk | Why |
|---|---|---|---|---|---|---|
| 1 | State-key helpers to `harness_lib/paths.py`, plus the loader itself | lib, 6 bin tools, 3 hooks, design.md | board:201-267, pr:310-376, work:277-343, red:186-252, standup:606-646, digest:82-122, session_brief.py:398-464, tests_first.py:252-318, branch_guard.py:259-289 | 525 / 80 / about 380 (after 63 lines of prologue) | low | Every copy is AST-identical (verified); nothing to reconcile. The only moving part is the loader. |
| 2 | CLI and text boilerplate to `harness_lib/cli.py` and `text.py` | lib, all 9 bin tools, 3 hooks | Problem in 8 files (board:118-119 ... work:127-128; worklog has none), Parser in 9 (board:122-126 ... worklog:725-729); `__main__` tails in 9 (board:1391-1400 ...); table ×4, parse_iso ×5, read_json ×5, atomic writers ×5, line pickers ×10, home ×5, age ×3 | 450 / 150 / about 240 | low | Parser, table, the tail and the key helpers are single variants. The named differences (below) stay explicit. |
| 3 | branch_guard's shell walker to `harness_lib/shellwalk.py`; readonly_bash drops its copy | lib, branch_guard.py, readonly_bash.py | readonly_bash.py:139-335 vs branch_guard.py:382-586; ten shared tables (about 23 lines) inside readonly_bash.py:57-117 and branch_guard.py:83-154 | 440 (230 moved, 210 deleted) / 235 / about 200 | medium | closing, substitution, command_words, stdin_program identical; Part and split_command differ (markers, `&&`/`\|\|`). PY_WRITERS differ on purpose and stay per hook. |
| 4 | macOS notification block to `harness_lib/notify.py` | lib, board, pr, digest, notify.py | board:745-782, pr:527-564 (identical), digest:526-564 (fixed group), notify.py:52-105 (adds subtitle) | 165 / 60 / about 100 | low | The three tool copies are one variant but for the group; the hook adds `-subtitle`, inserted only when given. |
| 5 | Hook I/O and the after_edit/verify_on_stop session state to `harness_lib/hookio.py` and `sessionstate.py` | lib, 7 hooks | read_input ×7 (identical), the deny JSON ×3, cwd fallback ×5, edited path ×3, fail-open tail ×7; after_edit.py:118-150, 200-229 vs verify_on_stop.py:97-108, 186-228 | 205 / 90 / about 100 | low | JSON strings and file names are asserted verbatim; the helpers reproduce them byte for byte. One mkdir-timing difference, reconciled by a lazy mkdir. |
| 6 | Project settings and test-pattern matching to `harness_lib/settings.py` | lib, work, red, branch_guard.py, verify_on_stop.py, tests_first.py | settings_env/setting: work:413-431, red:97-121, branch_guard.py:202-224, verify_on_stop.py:139-161, tests_first.py:188-204; glob_match red:78-94 vs tests_first.py:149-165; declares_* hooks | 170 / 70 / about 90 | low | Four settings_env variants give the same dict (differences named below); the five `setting` bodies behave identically (verified). glob_match: fnmatch vs fnmatchcase, identical on macOS, named. |
| 7 | The gh batch runner (GhBatch, gh_binary, describe, repo_slug) to `harness_lib/gh.py` | lib, standup, digest, session_brief.py | standup:387-473 vs digest:204-290 (identical); session_brief.py:536-541 gh_command (same body as gh_binary) | 150 / 80 / about 70 | medium | Identical between standup and digest. test_digest patches `GH_BUDGET` on the tool module, so the budget must be an argument, not a lib global. session_brief keeps its own silent runner. |
| 8 | Repo core and the `git worktree list --porcelain` parser to `harness_lib/repo.py` | lib, board, pr, work, red, digest, standup | Repo: board:270-306, pr:379-412, work:346-368, red:255-323 (open_repo board:309-310, pr:415-425, work:371-372); worktrees: work:470-488, pr:1347-1359, digest:305-320, standup:487-502 | 120 / 60 / about 50 | medium | Three constructors share 15 lines verbatim but for the line picker; red's differ in three (named below). `open_repo` order stays per tool. The parser is pure. |
| 9 | Registry readers to `harness_lib/registry.py` | lib, projects, board, standup, digest | projects:70-101, board:1129-1146, standup:106-127, digest:165-183 (same messages, four return shapes); standup:130-139 pick | 110 / 65 / about 40 | low | The four messages are word-for-word the same; one strict flag covers harness-projects raising where the others skip. harness-pr keeps its own reader (its policy differs; see below). |
| 10 | Sequential gh wrappers (describe, gh_run, gh, gh_json) onto `harness_lib/gh.py` | lib, board, pr, work | board:314-351, pr:429-468, work:402-410 | 90 / 45 / about 35 | medium | Three error-text conventions that tests assert; the line picker, cwd and the not-found hint become arguments. Small gain, do last. |

## 1. State-key helpers and the loader

**What moves where.** `canonical_path`, `same_entry`, `path_key`, `repo_key` and `adopt_old_state` move, bodies unchanged, from the nine files into `lib/harness_lib/paths.py`. The loader (`lib/harness_lib/__init__.py`, the prologue, the two design.md sentences) lands with them, because the state key is the one lift with nothing to reconcile and so the cleanest proof that loading works from every route.

**Interface.** `canonical_path(path: str) -> str`, `same_entry(a, b) -> bool`, `path_key(path) -> str`, `repo_key(path) -> str`, `adopt_old_state(repos: str, main: str, *spelled: str) -> str`. Each file imports only what it used: branch_guard `canonical_path, same_entry`; standup and digest the four without `adopt_old_state`; the rest all five.

**Behavior differences.** None. Hashing each function's AST across the nine files gives one variant per function (canonical_path 24 lines ×9, same_entry 5 ×9, path_key 3 ×8, repo_key 3 ×8, adopt_old_state 24 ×6). The risks are structural: `abspath` instead of `realpath` in the prologue would break a symlinked tool; reaching the names through the module instead of a `from` import would break `tests/test_repo_key.py:824-829`; leaving design.md:39 and :95 unchanged would leave the contract false.

**Guard.** tests/test_repo_key.py (every file, in-process and as subprocesses, keys, migration, spellings), tests/test_guards.py and test_hooks.py (branch_guard, tests_first, session_brief as subprocesses), tests/test_board.py, test_pr.py, test_work.py, test_red.py, test_standup_v2.py, test_digest.py (each tool end to end).

## 2. CLI and text boilerplate

**What moves where.** `lib/harness_lib/cli.py`: `class Problem(Exception)`; `class Parser(argparse.ArgumentParser)` with the one-line `error()`; `run_main(prog: str, main: Callable[[], object]) -> NoReturn` holding the nine-line `__main__` tail (KeyboardInterrupt → 130, BrokenPipeError → dup2 devnull and exit 1, any Exception → `prog: TypeName: one line`, exit 1); `emit(as_json: bool, data, text: str)`; `table(rows) -> str`; `issue_number(text) -> int` for `type=`; `warn(prog, message)` printing `prog: warning: message`. `lib/harness_lib/text.py`: `one_line`, `first_line`, `last_line`, `as_list`, `read_text(path, what)` raising Problem, `read_json(path) -> Any | None`, `write_json(path, data, prefix)` and `write_text(path, text, prefix)` with the mkstemp/`os.replace`/unlink body, raising OSError; `home(*parts)` (`~/<parts>`), `harness_home(*parts)` (`~/.claude/harness/<parts>`), `now_iso()`, `parse_iso(text) -> datetime` (aware; naive means UTC), `parse_iso_seconds(text) -> float | None` (the standup/digest rule: naive means local time), `age_text(then, now)` (the 14-line `age` identical in standup:80, digest:125 and projects:196; harness-pr's `age(created)` at pr:1168 is a different function and stays), `same_path(a, b)` (standup = digest; projects spells it through an `absolute()` helper, same result), `session_folder(path)` (the Claude Code `-` encoding, standup:63-65 = projects:52-54 = worklog:114-116). Each tool's tail becomes `if __name__ == "__main__": run_main("harness-board", main)`.

**Behavior differences that stay explicit.**
- `parse_iso` is two families: board/pr/work (12 lines, aware datetime, naive = UTC) and standup/digest (9 lines, epoch float, naive = local). Two names, both kept; nothing switches family.
- The line pickers are one behavior in several spellings: `one_line` identical in board, pr and work and typed in session_brief:492-493; `first_line` as a loop in board and pr (5 lines) and as a `next()` in standup and digest (2 lines); `last_line` in pr and work differing only in a variable name. `one_line` in harness-scaffold:149-152 is a different function (last non-empty line, collapsed). It is renamed `last_line_collapsed` in scaffold, not replaced. harness-worklog has no `one_line` and no `Problem`: its tail (worklog:923) collapses whitespace inline; it takes `run_main` and `Parser` only.
- `home`: identical in board, pr, standup and digest (`~/<parts>`); harness-work's `home` (work:151-152) is `~/.claude/harness/<parts>`, which is the lib's `harness_home` (digest already has both names, digest:61-65). harness-work's call sites are renamed, not re-pointed.
- `read_json`: work, standup and digest share one body (a str path, `open`); session_brief:485-489 takes a `Path` and returns any JSON value; tests_first:138-143 takes a `Path` and returns only a dict, else None. The lib's returns any value; tests_first's callers keep the dict check at the call site.
- `read_text`: board and pr share `read_text(path, what)` raising Problem; harness-red:129-134 returns `""` on OSError and session_brief:117-119 reads at most `READ_LIMIT` bytes. Those two are different functions and stay.
- `warn`: board and pr print `<tool>: warning: ...`, harness-work prints `harness-work: ...` without "warning:", and harness-work:375 and harness-pr:496 pass harness-board's stderr through by the literal prefix `harness-board: warning:`. `warn` takes the prog and keeps each tool's spelling; harness-work either keeps its own two-line `warn` or passes its exact prefix.
- The atomic writers are five bodies under four names: board `write_json` (169-184), work `write_json` (194-212, wraps OSError in `Problem("cannot write ...")`), pr `write_file` (184-198, text), red `save_record` (368-385) and projects `save` (104-120). board, red, pr and projects let OSError reach the `__main__` tail; the lib raises OSError and harness-work keeps a three-line wrapper. harness-projects writes without `ensure_ascii=False`, so a non-ASCII project name is escaped on disk today; adopting the shared writer changes those bytes (JSON-equivalent). Name it in the PR; the tests compare parsed JSON.
- `emit`: board reads `args.json`, work `getattr(args, "json", False)` (not every harness-work subcommand has `--json`), projects omits `ensure_ascii=False`. The lib takes a bool; work's call sites pass the `getattr`.
- `Problem` becomes one class. Each tool's `except Problem` catches the lib's class only if the tool imports it from the lib, so this refactor must land before any lib function that raises Problem is adopted (refactors 7 to 10). Problem's docstring differs per tool (text-only variants, one AST); the shared one gets one docstring.
- The mkstemp prefixes (`.ticket-`, `.harness-work-`, `.harness-pr-`, `.red-`, `.projects-`) are invisible unless a test lists the folder mid-write; none does. The lib takes `prefix` anyway.

**Guard.** Every tool test asserts its one-line error text and exit code through subprocesses (`assertOneLineError` defined at test_board.py:262, test_pr.py:233, test_work.py:271, test_red.py:152, test_tools.py:114, test_scaffold.py:144, test_digest.py:276), which is exactly what `run_main` and `Parser` preserve; test_work's budget tests pin `parse_iso`; test_standup_v2 and test_digest pin ages and `parse_iso_seconds`; test_tools pins harness-projects' JSON and messages.

## 3. The shell walker

**What moves where.** From branch_guard.py to `lib/harness_lib/shellwalk.py`: `Part` (the newer shape with `sep` and `marker`), `closing`, `substitution`, `split_command`, `command_words`, `stdin_program`, and the ten tables both hooks define identically: `SHELLS`, `WRAPPERS`, `KEYWORDS`, `GIT_VALUE_OPTIONS`, `ASSIGNMENT`, `NUMBER`, `PYTHON`, `SED_IN_PLACE`, `REDIRECT`, `REDIRECTS` (about 23 lines in each file; the rest of readonly_bash.py:57-117 and branch_guard.py:83-154 is hook-specific and stays). branch_guard imports them (its `walk`, `writes`, `git_*` and `python_writes` stay in branch_guard; tests_first keeps importing branch_guard for those). readonly_bash.py deletes lines 139-335 and those ten tables and imports the same names.

**Behavior differences.** `closing`, `substitution`, `command_words` and `stdin_program` are AST-identical. `Part` in branch_guard.py:382-394 carries `sep` and `marker`; readonly_bash.py:139-147 has neither. `split_command` in branch_guard.py:436-556 appends marker parts around subshells and substitutions and records `&&`/`||` as separators; readonly_bash.py:189-302 does neither (at `&&` it ends the part at the first `&` and drops the empty part the second `&` would make: both versions' `end_part` keep a part only when it has words or targets, so the parts that reach the caller carry the same stripped text). For readonly_bash this is behavior-preserving: a marker part has no words and no targets, so `part_refusal` (readonly_bash.py) returns None for it and `refusal` moves on to the next part, and the real parts come in the same order with the same text. That claim is the risk and must be checked against every readonly_bash case in the suite, not assumed. `OPEN_CALL` and `WRITE_MODE` are identical in both hooks (readonly_bash.py:118-119, branch_guard.py:157-158) and may move; `PY_WRITERS` differs on purpose (readonly_bash.py:120-125 also refuses `.mkdir(`, `os.mkdir(` and `os.makedirs(`; branch_guard.py:162-167 allows mkdir as it allows the command) and branch_guard alone has `PATH_OPEN` (159-161): those stay in each hook and are not unified. After this change readonly_bash imports something for the first time, so a broken lib fails open through the prologue handler and silently disables the reviewer guard; the suite is the only guard against that.

**Guard.** tests/test_guards.py (every branch_guard and readonly_bash case: the walker is exercised through both hooks as subprocesses and in-process via `load_hook`), tests/test_hooks.py.

## 4. The notification block

**What moves where.** `lib/harness_lib/notify.py`: `env_setting(name, default)`, `succeeded(cmd)`, `applescript_string(text)`, `DEFAULT_APP`, `DEFAULT_SOUND`, and `notify(message, group, title="Harness", subtitle="") -> bool` building the terminal-notifier argv in the order board:766-768 uses (`-title`, then `-subtitle` only when non-empty, `-message`, `-group`, `-activate`, `-sound`) and falling back to osascript as notify.py:92-105 does. board, pr and digest delete their 38-line blocks; digest passes `group="harness-digest"`; hooks/notify.py keeps `read_input`, `clean` and `main` and calls `notify()`.

**Behavior differences.** The board and pr copies are AST-identical; digest's differs only in the fixed group (`notify(message)`, digest:540-564); the hook's `succeeded` and `env_setting` are typed variants of the same bodies and the hook adds the subtitle. Inserting `-subtitle` only when given keeps the argv the tool tests record (tests/test_board.py:243-247 reads the fake notifier's log; test_digest.py:302 asserts `-group harness-digest -activate com.microsoft.VSCode -sound Glass`; test_pr.py:269 reads its notify.log).

**Guard.** tests/test_board.py, test_pr.py, test_digest.py (fake terminal-notifier recording argv), tests/test_hooks.py (notify.py).

## 5. Hook I/O and session state

**What moves where.** `lib/harness_lib/hookio.py`: `read_input() -> dict` (the identical six lines in seven hooks), `hook_cwd(data) -> str` (`cwd`, else `CLAUDE_PROJECT_DIR`, else `os.getcwd()`: the same expression at branch_guard.py:1471, after_edit.py:288, verify_on_stop.py:305, tests_first.py:652, session_brief.py:695), `edited_path(data) -> str | None` (`file_path` or `notebook_path`: branch_guard.py:366, after_edit.py:295, tests_first.py:660), `deny(reason)` writing the exact PreToolUse JSON (a function in branch_guard.py:185-192 and tests_first.py:128-135, inlined in readonly_bash.py:545-546), `emit(obj)`, and `run_fail_open(name, main)` holding the try/except/exit-0 tail and returning `main()`'s int when it gives one (after_edit's exit 2 on a confirmed secret, after_edit.py:282, 311). `lib/harness_lib/sessionstate.py`: `SessionState.for_repo(root, data)` with `.dir` (the `$TMPDIR/claude-harness/<sha1[:12]>` folder, created lazily on first write), `.session`, `.dirty`, `.verify_file`, `.stamp(name)`, `record(paths)`, `dirty_files()`, `clear_dirty()`; `porcelain_status(root, timeout) -> bytes | None` and `status_paths(root, out)` (identical 15 lines in after_edit.py:215-229 and verify_on_stop.py:214-228).

**Behavior differences.** after_edit creates the state directory eagerly in `state_dir()` (after_edit.py:118-127), verify_on_stop only in `record()` (verify_on_stop.py:186-193); a lazy mkdir on first write satisfies both since after_edit always writes after computing it. verify_on_stop's `worktree_status` returns `b""` for a None root (verify_on_stop.py:196-211) where after_edit's `git_status` has no such case (after_edit.py:200-212); the shared one keeps the None case. The truncation helpers (`MAX_SHOWN = 160` in branch_guard.py:65 and readonly_bash.py:54; 300 with different whitespace rules in session_brief.py:48 and notify.py:41) stay local: the tests assert the exact text. notify.py gains the import only through refactor 4; readonly_bash through refactor 3.

**Guard.** tests/test_hooks.py (after_edit and verify_on_stop: dirty lists, stamps, verify state at the asserted paths, lines 630, 773, 988, 1130, 1148; the deny JSON), tests/test_guards.py (deny text; `stderr == ""` at 241), tests/test_session_brief_v2.py.

## 6. Settings and test-pattern matching

**What moves where.** `lib/harness_lib/settings.py`: `settings_env(root, names=("settings.json", "settings.local.json")) -> dict` (red's variant with `names`, accepting str or Path), `setting(name, file_env) -> str | None`, `user_settings_env() -> dict` (the two lines of harness-work's `api_key_cmd` that read `~/.claude/settings.json`, work:436-437), `patterns(text) -> list[str]`, `glob_match(pattern, path) -> bool`, `DEFAULT_TEST_PATTERNS`, `DEFAULT_SOURCE_EXEMPT`, `declares_hook(root, event) -> bool`. tests_first's `classification_env(main)` (188-193) becomes `settings_env(main, ("settings.json",))`, spelled out at the call site because reading only the main checkout's tracked settings.json is a security property (tests_first.py:43-46, design.md); harness-red already spells it that way (red:286).

**Behavior differences.** The five `setting` bodies behave identically (environment wins when non-blank; str/int/float but not bool from the file; blank means unset): branch_guard, verify_on_stop and tests_first are AST-identical, red adds a docstring, and work:424-431 writes `return str(value).strip() or None` where the others use two lines; the int/float handling one reviewer thought differed does not. `settings_env` has four spellings that produce the same dict: red (the `names` parameter, str paths), branch_guard = verify_on_stop (a `Path`, two fixed names), work:413-421 (reads each file through its `read_json`, which swallows the same OSError/ValueError), and tests_first's one-file `classification_env`. `glob_match`: red:78-86 strips the pattern, returns False for an empty one and uses `fnmatch.fnmatch` (so does its `_segments_match`); tests_first.py:149-155 uses `fnmatchcase` and relies on `patterns()` to drop empties. Identical on macOS (`fnmatch` differs from `fnmatchcase` only through `os.path.normcase`, the identity on POSIX); the shared one uses `fnmatchcase` and keeps the empty guard. `declares_stop_hook` (verify_on_stop.py:126-136) requires a non-empty list, `declares_session_start` (session_brief.py:133-143) only truthiness; the strict form is adopted, which changes session_brief only for a `hooks.SessionStart` that is a truthy non-list, which Claude Code rejects anyway. harness-scaffold's `TEST_FILE_PATTERNS` (scaffold:102) is a hand-copied subset missing `*.spec.js` and friends; it stays as it is, because deriving it from the shared default would change `own_layout()` detection, which is a behavior change for another ticket. harness-red re-exports `glob_match`, `settings_env`, `setting`, `globs`, `TEST_PATTERNS`, `SOURCE_EXEMPT` because test_red reads them by attribute.

**Guard.** tests/test_red.py (classification, settings, globs, in-process attributes), tests/test_guards.py (tests_first classification from the main checkout only, branch_guard's allow-main setting), tests/test_hooks.py (verify_on_stop's command and timeout settings), tests/test_work.py (HARNESS_* settings, api_key_cmd).

## 7. The gh batch runner

**What moves where.** `lib/harness_lib/gh.py`: `gh_exe() -> str` (`HARNESS_GH` else `gh`), `gh_binary() -> str | None` (the which() form of standup:387-392 = digest:221-226 = session_brief.py:536-541, where it is named `gh_command`), `GH_ENV` (`GH_PROMPT_DISABLED`, `GH_NO_UPDATE_NOTIFIER`, `NO_COLOR`: standup:425, digest:242, session_brief.py:550), `describe_list_call(args)` (the two-words-plus-`--label` form, standup:412-417 = digest:229-234), `repo_slug(origin_url)` (standup:395-409 = digest:204-218), and `GhBatch(gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call)` moved whole from standup:420-473 / digest:237-290 (identical). Problem comes from cli.py.

**Behavior differences.** `GH_BUDGET` is read at call time inside `GhBatch` today (standup:442, digest:259) and tests/test_digest.py:449-453 sets `tool.GH_BUDGET = 1` on the tool module and expects `gh pr list timed out after 1 s`; the lib must take the budget as an argument the tool passes from its own constant, or the monkeypatch misses and the message changes. standup's git runner raises `TimeoutError` and digest's `Problem` for the same deadline, and standup:188 catches `TimeoutError` specifically: those git runners stay in their tools (see "not recommended"). session_brief's `run_side_by_side` (544-586) discards stderr and returns None instead of raising; it shares `gh_binary` and `GH_ENV` only and keeps its runner.

**Guard.** tests/test_standup_v2.py, tests/test_digest.py (timeouts, messages, parallel calls through fakegh), tests/test_session_brief_v2.py.

## 8. Repo core and the worktree parser

**What moves where.** `lib/harness_lib/repo.py`: `repo_facts(path, git, pick_line) -> (root, common_dir, main, key)` holding the 15 lines the constructors share (abspath and isdir check, rev-parse `--show-toplevel`, `--git-common-dir`, `realpath(dirname(common))`, `canonical_path`, `adopt_old_state(repos, main, reported, start)`), with the tool's own `git` callable and its line picker passed in so each tool's error text is built exactly as today; `main_checkout(path)` for worklog:72-83 and the hooks' `repo_state_dir`/`repo_facts` (session_brief.py:472-482, tests_first.py:233-249), returning rather than raising. `Worktree(path, branch, head, bare, locked, prunable)` and `worktree_entries(main, git) -> list[Worktree]` from work:470-488 (the only copy that records the flags), used by pr's `worktree_for` (pr:1347-1359, which only needs the path for a branch), digest:305-320 and standup:487-502 (identical to each other; path, branch, head). Each tool keeps its `Repo` class and its `open_repo`; the class calls `repo_facts` and adds what is its own (board's remote check and `slug()`, pr's `state()` and `slug()`, work's `common_dir` and `worktrees_dir`, red's branch/record_path/scratch/settings).

**Behavior differences.** board, pr and work share the 15 lines verbatim but for the picker and work's `home("repos")` spelling; red's constructor (258-288) differs in three lines: `not inside a git working tree:` where the others say `not a git repository:`, `check=False` on the first rev-parse and its own `check=True` on the second, so red's `git` (169-180) raises a full-argv `git rev-parse --git-common-dir failed: <last line>` before the others' message would apply. harness-board refuses a repository with no remote inside its constructor (board:289-292; test_board covers it); pr, work and red do not; the check stays in board's class. The common-dir failure message uses `first_line` in board and pr, `last_line` in work (work:359 vs board:283, pr:393): hence the `git` and `pick_line` parameters. `open_repo` order differs (board and work: `--repo`, `CLAUDE_PROJECT_DIR`, cwd; pr: `--worktree`, cwd, then `CLAUDE_PROJECT_DIR`, documented at pr:21) and stays per tool. worklog's `main_checkout` returns the given path on any git failure (worklog:79-80) and must keep returning, or `harness-worklog export` outside a repo starts failing. The hooks apply `canonical_path` to the main tree (tests_first.py:245-246) and the tools apply it inside `Repo`; the shared function returns the canonical spelling and says so. Not in scope: the ticket-worktree removal (work:1381-1406 `cmd_worktree_remove` vs pr:1646-1680 inside `cmd_land`), where pr's land deliberately continues and reports (`kept <wt>`, `branch kept`) and work raises.

**Guard.** tests/test_board.py (no-remote refusal, repo precedence), test_pr.py (cwd-first precedence, land's worktree lookup), test_work.py (start, worktree list, remove), test_red.py, test_repo_key.py (keys and migration through every tool), test_standup_v2.py and test_digest.py (stale worktrees), test_worklog.py.

## 9. The registry readers

**What moves where.** `lib/harness_lib/registry.py`: `@dataclass Project(path, name, billable=False, client="", notes="", extra: dict)` with `from_entry` (projects:70-77 `entry`) and `to_entry()`; `registry_path()` (projects:43-44); `load_registry(strict=False) -> list[Project]` raising Problem with the exact messages the four copies share (`is not valid JSON ... fix it or remove it`, `cannot read <path>: <err>`, `expected {"version": 1, ...}`, `is version ...`); `save_registry(projects)` over text.py's `write_json`; `find_project(projects, key)` (standup:130-139 `pick`). harness-projects calls it with `strict=True` to keep raising `project N has no path` (projects:98-99) where board:1129-1146, standup:106-127 and digest:165-183 skip such an entry; board takes `[p.path for p in ...]`; standup and digest take the fields they use (digest name and path; standup also billable and client).

**Behavior differences.** harness-pr's `registry_projects` (pr:1199-1213) expands `~`, does not check the version, returns `[]` only on a missing file and says `cannot read <path>: <err>` on bad JSON where the others say `is not valid JSON` (test_tools.py:187 asserts the latter for harness-projects). Adopting the strict reader would turn `harness-pr awaiting --all-projects` on a malformed registry from an empty table into exit 1: a behavior change, so harness-pr keeps its reader and is not in this ticket. standup's `report()` does `dict(project, ...)` at standup:297-298, which becomes `dataclasses.asdict` or the `--json` projects shape changes. harness-projects' `emit` omits `ensure_ascii=False` (projects:248-249); keep it there (its `--json` bytes differ from board's today and the tests parse JSON) or switch it in refactor 2 and say so.

**Guard.** tests/test_tools.py (harness-projects, every message), test_board.py (project sync over the registry), test_standup_v2.py, test_digest.py (attribution by key, partial entries).

## 10. The sequential gh wrappers

**What moves where.** Onto `lib/harness_lib/gh.py`: `describe(args)`, `gh_run(args, cwd, stdin=None, timeout=60, errors="replace") -> CompletedProcess` raising Problem on a missing or timed-out gh (`gh not found (<exe>); install it with brew install gh, then gh auth login`, board:336 = pr:448), `gh(args, cwd, *, pick=first_line, **kw) -> str`, `gh_json(args, cwd, *, pick=first_line, empty_ok=False, **kw)`. board and pr adopt them directly (same text, same picker; board's `gh_json` and pr's differ only in argument order). harness-work keeps a two-line adapter passing `cwd=repo.main`, `pick=last_line`, `errors="surrogateescape"` and `empty_ok=True` (`json.loads(stdout or "null")`), because test_work asserts its messages.

**Behavior differences.** `describe` is two variants and neither is a superset: pr:429-438 names `api graphql` (`"/" in a or a == "graphql"`), board:314-323 skips flag-shaped arguments in the `api` branch (`"/" in a and not a.startswith("-")`); the non-`api` branch (three words, stop at the first flag) is the same. The union, `a == "graphql" or ("/" in a and not a.startswith("-"))`, describes every current call site (board:583, 840, 844, 998; pr:852, 968, 1082, 1373, 1466) exactly as each tool does today, because in each the path or `graphql` is the first non-flag argument; the lib takes the union and the PR says so. harness-work reports the last stderr line and decodes with surrogateescape, board and pr the first line with replace (with fakegh's one-line stderr the two coincide; with real gh they do not); work's message is `gh <two words> failed:` from its generic `run` (`gh not found` without the install hint), board's and pr's `describe(args) failed:`; work runs gh with `cwd=repo.main`, board with `repo.root`, pr passes it per call; work sets only `GH_PROMPT_DISABLED` where the batch runner also sets `GH_NO_UPDATE_NOTIFIER` and `NO_COLOR` (adding them is harmless but is a change: pass `GH_ENV` explicitly); timeouts are 60 in board, per call in pr (120 for `run view` and `merge`). Each is an argument. This is the smallest gain against the most asserted text; do it last or leave it.

**Guard.** tests/test_board.py:291-293 (`HTTP 403: Resource not accessible` from a two-line stderr, i.e. the first line), test_pr.py (describe wording `gh issue view 12`, api paths), test_work.py (last-line messages, `gh not found`).

## Tickets

All are `type:refactor`, behavior-preserving, under the size cap (design.md:135: S one sitting and one or two files, M, L about 5 files or 300 lines). Every one shares the same "Tests that must pass": the existing suite, `python3 -m unittest discover -s plugins/harness/tests`, unchanged, with the modules named under each refactor's Guard as the ones that exercise the change. Dependencies name ticket numbers in this list.

### T1. The shared-module loader, `harness_lib/paths.py`, and the three hooks

size:M, risk:low, depends on nothing.

Request: give the plugin one place for code that today is pasted into every tool and hook, loaded in place from the checkout with no install step, and prove it with the state-key helpers, which are identical in nine files.

Outline: (1) create `plugins/harness/lib/harness_lib/__init__.py` and `paths.py` holding `canonical_path`, `same_entry`, `path_key`, `repo_key`, `adopt_old_state` with their current bodies; (2) add the seven-line prologue (realpath, `sys.path.insert(0, ...)`, `from harness_lib.paths import ...`, one-line fail-open handler) to hooks/session_brief.py, tests_first.py and branch_guard.py and delete their copies; (3) reword docs/design.md:39 and :95 as above; (4) run the suite and show that `tests/test_repo_key.py` still finds `canonical_path` and `repo_key` on each hook's module.

Definition of done: the three hooks carry no copy of the five functions and import them from the lib; a hook run with `lib/` absent prints one line to stderr and exits 0; design.md describes the lib and the fail-open rule; the suite is green.

Tests: the existing suite; test_repo_key.py, test_guards.py, test_hooks.py, test_session_brief_v2.py are the ones that matter. The test-author may add one additive test for the fail-open line.

### T2. harness-board, harness-pr and harness-work import the state-key helpers

size:S, risk:low, depends on T1.

Request: replace the state-key block in the three tools that open `repos/<key>/` most with the lib import.

Outline: (1) add the prologue to each; (2) delete board:201-267, pr:310-376, work:277-343; (3) run the suite.

Definition of done: the three files bind the five names from `harness_lib.paths` and carry no copy; test_repo_key, test_board, test_pr, test_work green.

### T3. harness-red, harness-standup and harness-digest import the state-key helpers

size:S, risk:low, depends on T1.

Request: the same for the remaining three tools (standup and digest import four names, no `adopt_old_state`).

Outline and definition of done as T2 for red:186-252, standup:606-646, digest:82-122; test_red, test_standup_v2, test_digest, test_repo_key green.

### T4. `harness_lib/cli.py` and `text.py`, adopted by harness-board and harness-pr

size:M, risk:low, depends on T1.

Request: one `Problem`, one `Parser`, one `run_main`, and the text, time and file helpers, adopted first by the two largest tools.

Outline: (1) write cli.py and text.py with the interfaces in refactor 2, including both `parse_iso` families and `warn(prog, message)`; (2) board and pr replace their copies (Problem, Parser, one_line, first_line, last_line, home, now_iso, parse_iso, as_list, read_text, write_json/write_file, table, emit, issue_number, the `__main__` tail); (3) keep board's `harness-board: warning:` spelling byte for byte; (4) suite.

Definition of done: board and pr define none of the moved helpers; every `assertOneLineError` in test_board and test_pr passes unchanged; the suite is green.

### T5. harness-work, harness-red, harness-scaffold and harness-projects adopt cli.py and text.py

size:M, risk:low, depends on T4.

Request: the same adoption for four more tools, keeping the named differences.

Outline: (1) harness-work keeps a three-line `write_json` wrapper turning OSError into `Problem("cannot write ...")`, its own `warn` prefix, and renames its `home` calls to `harness_home`; (2) harness-scaffold renames its `one_line` to `last_line_collapsed` before importing the lib's; (3) harness-projects keeps `ensure_ascii` as today unless the PR says it changes; (4) harness-red keeps `load_record`'s validation over the shared writer, keeps its own `read_text`, and re-exports `Problem`; (5) suite.

Definition of done: the four tools define none of the moved helpers except the named wrappers; test_work, test_red, test_scaffold, test_tools green.

### T6. harness-standup, harness-digest, harness-worklog and the hooks adopt cli.py and text.py

size:M, risk:low, depends on T4.

Request: finish the adoption: standup and digest take `parse_iso_seconds`, `age_text`, `same_path`, `session_folder`, `read_json`, `first_line`, Problem/Parser/run_main; worklog takes Parser and run_main and gains `from __future__ import annotations` (it has none today); session_brief and tests_first take `read_json` (tests_first keeping its dict check at the call sites) and `one_line`.

Definition of done: no copies remain in bin/ or hooks/ of the helpers cli.py and text.py export; test_standup_v2, test_digest, test_worklog, test_session_brief_v2, test_guards green.

### T7. `harness_lib/shellwalk.py` from branch_guard; readonly_bash drops its copy

size:M, risk:medium, depends on T1.

Request: one shell walker for the two Bash guards.

Outline: (1) move Part, closing, substitution, split_command, command_words, stdin_program and the ten shared tables to shellwalk.py, branch_guard importing them; (2) readonly_bash deletes its 197-line copy and its ten duplicated tables, keeps PY_WRITERS and `code_refusal` (OPEN_CALL and WRITE_MODE may move or stay; they are identical); (3) show, case by case against the readonly_bash tests, that marker parts fall through `part_refusal` as None; (4) suite.

Definition of done: readonly_bash.py contains no walker code; branch_guard's `walk`, `writes`, `git_*` unchanged; every test in test_guards passes unchanged.

### T8. `harness_lib/notify.py`

size:M, risk:low, depends on T1.

Request: one notification function for board, pr, digest and hooks/notify.py.

Outline: (1) notify.py in the lib with `notify(message, group, title, subtitle)` and the argv order board uses, `-subtitle` only when given; (2) the three tools delete their blocks, digest passes its group; (3) hooks/notify.py keeps read_input, clean and main; (4) suite.

Definition of done: one copy of env_setting, succeeded, applescript_string and the argv builder; the fake-notifier logs in test_board, test_pr, test_digest and test_hooks unchanged.

### T9. `harness_lib/hookio.py` for the guards and the brief

size:M, risk:low, depends on T1.

Request: one read_input, deny, emit, hook_cwd, edited_path and run_fail_open for branch_guard, tests_first, readonly_bash, session_brief and verify_on_stop.

Definition of done: the deny JSON and stderr are byte-identical to today (test_guards.py:241 asserts an empty stderr); the five hooks carry no copies; test_guards, test_hooks, test_session_brief_v2 green.

### T10. `harness_lib/sessionstate.py`; after_edit and notify.py adopt hookio

size:S, risk:low, depends on T9.

Request: name the per-session state after_edit and verify_on_stop share (state dir, session key, dirty list, stamps, porcelain status) once; after_edit keeps its exit-2 path.

Definition of done: the asserted paths in test_hooks (630, 773, 988, 1130, 1148) unchanged; `status_paths` exists once; the suite green.

### T11. `harness_lib/settings.py` with the glob matcher

size:M, risk:low, depends on T1.

Request: one settings_env/setting/patterns/glob_match for harness-work, harness-red, branch_guard, verify_on_stop and tests_first.

Outline: (1) settings.py with red's `names` parameter and `fnmatchcase`; (2) tests_first's `classification_env` becomes an explicit `settings_env(main, ("settings.json",))` with the security comment kept; (3) harness-red re-exports the names test_red reads; (4) suite.

Definition of done: one copy of each; tests_first still reads only the main checkout's tracked settings.json; harness-scaffold's `TEST_FILE_PATTERNS` untouched; test_red, test_guards, test_hooks, test_work green.

### T12. `harness_lib/gh.py`: the batch runner

size:M, risk:medium, depends on T4.

Request: one GhBatch, gh_binary, describe_list_call and repo_slug for harness-standup and harness-digest; session_brief shares gh_binary and GH_ENV.

Definition of done: GhBatch exists once and takes the budget as an argument from the tool's `GH_BUDGET`; `tool.GH_BUDGET = 1` in test_digest still yields `gh pr list timed out after 1 s`; test_standup_v2, test_digest, test_session_brief_v2 green.

### T13. `harness_lib/registry.py`

size:M, risk:low, depends on T4.

Request: one reader and writer for `~/.claude/harness/projects.json` for harness-projects (strict), harness-board, harness-standup and harness-digest; harness-pr keeps its reader.

Definition of done: the four messages exist once; harness-projects still raises on an entry without a path; the standup's `--json` projects shape unchanged; test_tools, test_board, test_standup_v2, test_digest green.

### T14. `harness_lib/repo.py`: the Repo core

size:M, risk:medium, depends on T4.

Request: the 15 constructor lines board, pr and work share, and red's near-copy, become `repo_facts(path, git, pick_line)`; each tool's Repo class and open_repo keep their own checks, messages and precedence; the hooks and worklog call `main_checkout`.

Definition of done: board still refuses a remote-less repo in its constructor; work's common-dir error still uses the last line and board's the first; red's `not inside a git working tree` text unchanged; worklog still returns rather than raises; test_board, test_pr, test_work, test_red, test_repo_key, test_worklog, test_guards green.

### T15. The `git worktree list --porcelain` parser

size:S, risk:low, depends on T14.

Request: one `worktree_entries(main, git) -> list[Worktree]` for harness-work, harness-pr's `worktree_for`, harness-digest and harness-standup.

Definition of done: four copies become one; the land, remove, stale-worktree outputs in test_pr, test_work, test_digest, test_standup_v2 unchanged.

### T16. The sequential gh wrappers

size:M, risk:medium, depends on T12.

Request: board and pr use the lib's describe, gh_run, gh and gh_json; harness-work keeps a two-line adapter with its own picker, cwd and decoding.

Definition of done: test_board:291-293 still sees the first stderr line, test_work still sees the last; `gh not found` texts unchanged; every `gh api` call in board and pr is described as today; the suite green.

## Considered and not recommended

- **One `git()` wrapper for the plugin.** Eleven variants (board 30 s and locale decoding; pr 60 s replace; work surrogateescape and `GIT_TERMINAL_PROMPT=0` through its generic `run`; red `check=True` with a full-argv message; standup and digest `--no-optional-locks`, `LC_ALL=C`, deadline-based, raising TimeoutError in one and Problem in the other; scaffold a two-line wrapper; four hooks with 3, 3, 4 and 20 s timeouts sized to their hooks.json budgets, branch_guard's and tests_first's identical). Each difference is a tuned, observable choice, several asserted by tests. Unifying saves about 60 lines against the widest behavior surface in the plugin. Refactor 8 passes each tool's `git` into the shared Repo core instead.
- **Ticket-worktree removal shared between `harness-work worktree remove` and `harness-pr land`.** pr's cleanup continues and reports (`kept <wt>`, `branch kept`) where work raises; a shared function would have to return outcomes and both callers reformat them. Behavior-preserving in principle, not worth the risk for about 30 lines.
- **Typed records for the five state files (ticket cache, red record, run record, budgets, Labels/classify).** About 20 to 60 lines each and medium risk each: the files are shared state read raw by other tools, hooks and tests, and the readers tolerate different missing keys on purpose (tests_first unions flags and labels, session_brief reads neither; pr reads `run_files` then `test_files`). Unifying run liveness would change the standup's answer for a recycled or foreign pid (standup:662-671 vs work:741-770) and `Budgets.exhaust` would change what harness-pr writes as the reason (raw vs `one_line`). Revisit after T1 to T16, when every reader at least shares `read_json`, as a design ticket rather than a refactor.
- **The Projects-board parse and argv builder inside harness-board, and the `cmd_project_sync` split.** No lib needed and about 20 lines; fold it into the next harness-board ticket that touches `project sync`.
- **Restructures that remove nothing:** `cmd_land` as a `Landing` object (pr:1608-1751), `cmd_dispatch` as a `Plan` (work:1183-1270), `VerifyState` in verify_on_stop, NamedTuples in branch_guard's `writes()`/`part_refusal` and in harness-worklog. Each is a readability change inside one file with a net of 0 to 15 lines. Do them when those files are next changed for a feature, under that ticket's tests, not as refactor tickets of their own.
- **`harness-pr` adopting the shared registry reader** (refactor 9) and **harness-scaffold's `TEST_FILE_PATTERNS` deriving from the shared default** (refactor 6): both change behavior (an empty table becomes exit 1; `own_layout()` detection widens). Each is a one-line feature ticket if wanted, never part of a refactor.
- **A `tests/support.py` and fakegh helpers** (about 260 lines of fixture duplication across 13 test modules, plus 15 hand-built environments with different hermeticity). Real, but the suite is the guard for every ticket above; refactoring the guard while the plugin moves removes the independent check, and the unified environment is stricter than three modules are today (test_board and test_scaffold keep most `HARNESS_*` and `CLAUDE_*` variables). Do it after T16 as a test-author ticket, and let that ticket also set `PYTHONDONTWRITEBYTECODE=1` in every subprocess env so the suite stops leaving `lib/harness_lib/__pycache__/` in the checkout.
- **`importlib.util.spec_from_file_location` instead of `sys.path`.** It avoids widening `sys.path` at the cost of three more lines per file and a different idiom from the `import branch_guard` the hooks already use. Not worth it: `sys.path.insert(0, lib)` with a distinctive package name has the same effect, and the experiment showed it beats a same-named package in the cwd.
- **A single `harness_lib.py` module instead of a package.** Simpler to load, but the ten refactors would put 900 lines of unrelated helpers in one file; a package with one module per concern keeps each import cheap (hooks import only `paths` and `hookio`) and each ticket's diff local.

## Decision

Ryan's decision, 2026-10-16: do all sixteen tickets, after the audit's cost metric (docs/research/harness-audit.md section 7, item 1). They are epic #78, T1 to T16 as #79 to #94, each blocked as the list above says, and T1 blocked by #55. Two changes from the list: T1 also makes a lib that fails to import visible (a warning line at session start), because the hooks fail open and a broken lib would otherwise switch off every guard silently; and T7 is risk:high, because it changes both Bash guards. The proposal's medium-risk items are labeled risk:low, since the board's risk scale is only low and high. No separate review of this proposal: the Fable workflow that wrote it was the review.
