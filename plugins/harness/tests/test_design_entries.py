"""Tests for ticket #250 (epic #249): docs/design.md gives each harness_lib module and each
per-repository state entry its own line, so parallel tickets stop conflicting on one line.

- LibEntries: under a `### harness_lib modules` heading inside `## Layout`, one single-line
  bullet `` - `<module>.py`: ... `` per `*.py` in plugins/harness/lib/harness_lib/ except
  `__init__.py` (read from disk at test time), in alphabetical order; the intro paragraph and
  the Layout block's `lib/` line name no module file, and the `lib/` line points at the entries;
  every backticked term of the old line 41 paragraph (embedded below, taken once on 2026-10-26
  with re.findall(r'`([^`]+)`', line)) is still in `## Layout`. The old paragraph is also embedded
  cut into sentences, each with its owner: each module's sentences move to its bullet (terms,
  closing words, word count: moved, not rewritten), the opening ones stay in the intro, the
  `Fail open:` ones become their own paragraph, and no other Layout prose names a module file.
  Nothing is borrowed: no entry, nor the intro or the Fail open paragraph, ends a sentence
  another owner owns. There is no length cap, so a later ticket may add a sentence to any of
  them. The modules this ticket first describes (cost.py, repo.py, transcripts.py) get a line in
  their docstring's words naming one of their own public names (none of the other two's) and an
  importer.
- StateEntries: the `~/.claude/harness/repos/<repo-key>/` State bullet lists each entry as a
  nested bullet, sorted, and today's six keep the old line 119's words; a later entry (reviews/,
  verify/) may join in alphabetical place.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_design_entries.py" -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
ROOT = PLUGIN.parent.parent
DESIGN = ROOT / "docs" / "design.md"
LIB_PKG = PLUGIN / "lib" / "harness_lib"

HEADING = "### harness_lib modules"
INTRO_START = "Code shared between files lives in `plugins/harness/lib/harness_lib`"
ENTRY = re.compile(r"^- `([A-Za-z0-9_]+\.py)`: (\S.*)$")
STATE_LEAD = "- `~/.claude/harness/repos/<repo-key>/`"

# Every backticked term of docs/design.md line 41 as of 2026-10-26 (the harness_lib paragraph,
# before the split), deduplicated in first-seen order. CI cannot read history, so it is here.
OLD_TERMS = [
    'plugins/harness/lib/harness_lib', 'from __future__ import annotations', 'os.path.realpath(__file__)',
    '../lib', 'sys.path', 'from harness_lib.paths import canonical_path, ...', 'CLAUDE_PLUGIN_ROOT',
    'paths.py', 'shellwalk.py', 'Part', 'closing', 'substitution', 'split_command', 'command_words',
    'stdin_program', 'SHELLS', 'WRAPPERS', 'KEYWORDS', 'GIT_VALUE_OPTIONS', 'ASSIGNMENT', 'NUMBER',
    'PYTHON', 'SED_IN_PLACE', 'REDIRECT', 'REDIRECTS', 'MAX_DEPTH', 'hookio.py', 'lib/harness_lib/hookio.py',
    'read_input()', '{}', 'hook_cwd(data)', 'cwd', 'CLAUDE_PROJECT_DIR', 'edited_path(data)', 'tool_input',
    'file_path', 'notebook_path', 'deny(reason)', 'emit(obj)', 'json.dumps', 'run_fail_open(name, main)',
    '__main__', 'main()', '<name>: <error>', 'sessionstate.py', 'SessionState.for_repo(root, data)', '.dir',
    '$TMPDIR/claude-harness/<first 12 hex of the sha1 of the resolved root>', '.session', 'session_id',
    '[A-Za-z0-9._-]', '_', 'nosession', '.dirty', '.verify_file', '.stamp(name)', 'dirty-<session>',
    'verify-<session>.json', '<name>-<session>', 'ensure_dir()', 'record(paths)', 'dirty_files()',
    'clear_dirty()', 'porcelain_status(root, timeout)', 'git status --porcelain=v1 -z --untracked-files=all',
    'b""', 'status_paths(root, out)', 'settings.py',
    'settings_env(root, names=("settings.json", "settings.local.json"))', 'env', '.claude',
    'committed_env(main, ref="origin/main")', '.claude/settings.json', 'ref',
    'git -C <main> show <ref>:.claude/settings.json', 'settings.local.json', 'setting', 'patterns',
    'DEFAULT_TEST_PATTERNS', 'glob_match', '**', 'fnmatchcase', '/', 'DEFAULT_SOURCE_EXEMPT',
    'harness-work cost', 'settings_env(main, ("settings.json",))', 'TEST_FILE_PATTERNS', 'notify.py',
    'notify(message, group, title="Harness", subtitle="") -> bool', '-title', '-subtitle', '-message',
    '-group', '-activate', '-sound', 'env_setting', 'succeeded', 'applescript_string', 'DEFAULT_APP',
    'DEFAULT_SOUND', 'cli.py', 'text.py', 'Parser', 'run_main', 'session_folder', 'read_json', 'one_line',
    'Problem', 'error', '<prog>: error: <message>', 'run_main(prog, main)', '<prog>: <one line>',
    '<prog>: <TypeName>: <one line>', '| head', 'SystemExit', 'warn(prog, message)',
    '<prog>: warning: <one line>', 'emit(as_json, data, text)', 'table(rows)', 'issue_number', 'type=', '#',
    'first_line', 'last_line', '""', 'as_list', '{"nodes": [...]}', 'read_text(path, what)', '~', 'what',
    'write_json(path, data, *, prefix)', 'write_text(path, text, *, prefix)', 'prefix', 'home',
    'harness_home', '~/...', '~/.claude/harness/...', 'same_path', '-', 'now_iso', '2026-10-16T12:34:56Z',
    'parse_iso', 'Z', 'parse_iso_seconds', 'age_text(then, now)', 'just now', '5m ago', '3h ago', '2d ago',
    '4mo ago', '2y ago', 'registry.py', '~/.claude/harness/projects.json',
    'Project(path, name, billable=False, client="", notes="", extra={})', 'from_entry', 'extra',
    'to_entry()', 'registry_path()', 'load_registry(strict=False, path=None)', 'path', '[]',
    'is not valid JSON ... fix it or remove it', 'cannot read <path>: <error>',
    'expected {"version": 1, "projects": [...]}', 'is version ...; this tool reads version 1', 'strict',
    '<path>: project N has no path', 'save_registry(projects)', 'write_json', 'find_project(projects, key)',
    'gh.py', 'gh_exe()', 'HARNESS_GH', 'gh', 'gh_binary()', 'GH_ENV', 'GH_PROMPT_DISABLED',
    'GH_NO_UPDATE_NOTIFIER', 'NO_COLOR', '1', 'describe_list_call(args)', 'repo_slug(origin_url)',
    'GhBatch(gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call)', 'budget_seconds',
    'GH_BUDGET', 'describe(args)', 'gh api ', 'api', 'graphql', 'gh ',
    'gh_run(args, cwd, stdin=None, timeout=60, *, errors="replace", env=None, describe=describe, texts=None)',
    'stdin', 'GH_PROMPT_DISABLED=1', 'gh(args, cwd, ..., pick=first_line, ..., run=None)', 'exit N', 'run',
    'run(args, cwd, stdin, timeout)', 'gh_json(args, cwd, *, pick=first_line, empty_ok=False, via=None, **kw)',
    'via', 'via(args, cwd)', 'pick(output)', 'GH_TEXTS', 'texts', 'gh not found (<exe>); install it with ...',
    'cannot run gh (<exe>): ...', '<call> timed out after N s', '<call> failed: ...',
    '<call> printed something other than JSON: <excerpt>', 'describe', 'gh_run', 'gh_json',
    'gh(repo, args, stdin=None)', 'gh(args, cwd, stdin=None, timeout=GH_TIMEOUT)', 'pick',
    'gh_json(repo, args)', 'repo.main', 'gh_binary', 'run_side_by_side', 'waves.py', 'waves/<id>.json',
    'WAVE_ID', 'w[0-9]{14}(-[0-9]+)?', 'DONE', 'in-review', 'closed', 'waves_dir(key)',
    'wave_path(key, wave_id)', 'create_wave(key, main, tickets, tier, effort, at=None)', 'at', 'load_wave',
    'load_waves', '<wave id>.json', 'created_at', 'save_wave', 'wave_of(key, ticket)',
    'drop_ticket(key, wave_id, ticket)', 'dropped', 'ticket_statuses(key, tickets)',
    'progress(wave, statuses)', 'wave_state(wave, statuses)', 'wave_marker(wave_id)',
    'has_wave_review(reviews, wave_id)', 'ghlimit.py', '~/.claude/harness/gh/calls-<UTC date>.jsonl',
    'gh_dir()', 'harness_home("gh")', 'call_kind(args)', 'rest', 'label', 'release', 'workflow',
    'call_name(args)', 'api ', '-X', '--method', 'repos/<owner>/<repo>/', 'repos/:owner/:repo/', ':n',
    ':sha', 'ticket_of(cwd)', '/.claude/worktrees/t-N-', 'PWD', 'repo_of(cwd)', '/.claude/worktrees/',
    'log_call(args, *, cwd, exit, ms, waited_ms=0, refused=False)',
    '{"at", "tool", "cmd", "kind", "repo", "ticket", "exit", "ms", "waited_ms", "refused"}', 'now_iso()',
    'tool', 'sys.argv[0]', 'O_APPEND', 'read_calls(since)', 'since', 'GhBatch', 'results()', 'close()',
    'session_brief.py', 'autoland.py', 'DEFAULT_MANUAL_PATHS', 'gate_settings(main, ref="origin/main")',
    'committed_env', 'evaluate(facts, settings)', 'gather_facts(main, slug, pr)', 'check_entries',
    'overall', 'CONVERSATION', 'redproof.py', 'tests_hash(contents)', 'test_hash', 'read_files(root, files)',
    'red_files_path', 'save_red_files', 'load_red_files', 'record_current(root, record, record_path, timeout=30)',
    'check', 'status', 'routing.py', 'TIERS', 'EFFORTS', 'KEYS', 'WAVE_EFFORT', 'label_tier(kind, size, risk)',
    'review_label_tier(risk)', 'load_routing(main)',
    '{code_tier, code_effort, review_tier, ultrareview, wave_tier, wave_effort, problems}', 'problems',
    'code_route(kind, size, risk, routing)', 'review_route(risk, routing)', '{tier, effort, from, labels_tier}',
    'from', 'labels', 'load_routing', 'ultra', '<hook>.py: cannot load harness_lib from <lib>: <error>',
    '{"terminalSequence": "\\u0007"}', 'notify', 'Warning:', '<tool>: cannot load harness_lib from <lib>: <error>',
]

# docs/design.md line 41 as of 2026-10-26, cut into its sentences (the paths.py/shellwalk.py one at
# its semicolon), each with the owner(s) its text moves to: "intro" is the paragraph that still
# starts "Code shared between files lives in", "failopen" the closing `Fail open: ...` paragraph, a
# module name that module's entry. A piece with two owners (the cli.py/text.py sentence, and the
# "Only session_brief keeps a fallback copy" one that names neither module) may be split between
# them or repeated. Joined with single spaces the pieces give the old line back, and their
# backticked terms are exactly OLD_TERMS (checked in test_old_pieces_are_the_old_paragraph).
OLD_PIECES = [
    (('intro',),
     'Code shared between files lives in `plugins/harness/lib/harness_lib` (python3 standard library, '
     '`from __future__ import annotations`), loaded in place from the checkout with no install step.'),
    (('intro',),
     'A file finds it relative to its own real path: a seven-line prologue at module top, after the '
     "standard-library imports, puts `os.path.realpath(__file__)`'s `../lib` first on `sys.path` (so "
     'a symlink still reaches the checkout, and neither a hooks/-local file nor a project file can '
     'shadow it; a file copied out of the checkout does not find it) and binds the names it uses in '
     'its own namespace (`from harness_lib.paths import canonical_path, ...`), never through '
     '`CLAUDE_PLUGIN_ROOT`.'),
    (('paths.py',),
     '`paths.py` holds the state-key helpers (repo-key, below);'),
    (('shellwalk.py',),
     '`shellwalk.py` holds the shell walker both Bash guards split commands with: `Part`, `closing`, '
     '`substitution`, `split_command`, `command_words`, `stdin_program`, the tables behind them '
     '(`SHELLS`, `WRAPPERS`, `KEYWORDS`, `GIT_VALUE_OPTIONS`, `ASSIGNMENT`, `NUMBER`, `PYTHON`, '
     '`SED_IN_PLACE`, `REDIRECT`, `REDIRECTS`) and the depth cap `MAX_DEPTH`, defined once there and '
     'never in a hook.'),
    (('paths.py',),
     'The hooks branch_guard, tests_first and session_brief import `paths.py` today, and so do all '
     'six bin tools that use the state-key helpers (harness-board, harness-pr, harness-work, '
     'harness-red, harness-standup and harness-digest); none carries a copy.'),
    (('hookio.py',),
     'The hooks branch_guard, tests_first, readonly_bash, session_brief, verify_on_stop, after_edit '
     'and notify also import `hookio.py`.'),
    (('hookio.py',),
     '`lib/harness_lib/hookio.py` holds the hook I/O those seven share: `read_input()` (the event on '
     'stdin, `{}` for empty, invalid or non-object input), `hook_cwd(data)` (`cwd`, else '
     "`CLAUDE_PROJECT_DIR`, else the process's directory), `edited_path(data)` (`tool_input`'s "
     '`file_path`, else `notebook_path`), `deny(reason)` (the PreToolUse deny JSON), `emit(obj)` '
     '(`json.dumps` to stdout, no newline) and `run_fail_open(name, main)`, the `__main__` tail: '
     "`main()`'s status (0 for None), or on an exception one stderr line `<name>: <error>` and 0."),
    (('sessionstate.py',),
     '`sessionstate.py` holds the per-session state after_edit and verify_on_stop share: '
     '`SessionState.for_repo(root, data)` (`.dir`, `$TMPDIR/claude-harness/<first 12 hex of the sha1 '
     'of the resolved root>`, made on the first write, never by naming a path; `.session`, the '
     '`session_id` with anything outside `[A-Za-z0-9._-]` turned into `_`, `nosession` when absent; '
     '`.dirty`, `.verify_file` and `.stamp(name)`, the files `dirty-<session>`, '
     '`verify-<session>.json` and `<name>-<session>` in it; `ensure_dir()`, `record(paths)`, '
     '`dirty_files()` and `clear_dirty()`), `porcelain_status(root, timeout)` (`git status '
     '--porcelain=v1 -z --untracked-files=all` without the index lock: `b""` for no root, None when '
     "git fails or times out) and `status_paths(root, out)` (the paths in that output, a rename's or "
     "copy's source skipped)."),
    (('hookio.py', 'sessionstate.py'),
     'Only session_brief keeps a fallback copy of the four it uses, inside the except branch of its '
     'lib import, so the brief still reaches the session without the lib.'),
    (('settings.py',),
     '`settings.py` holds `settings_env(root, names=("settings.json", "settings.local.json"))` (the '
     '`env` object of each named `.claude` file, a later file winning key by key; a missing or broken '
     'file counts as none), `committed_env(main, ref="origin/main")` (the `env` object of the '
     '`.claude/settings.json` committed at `ref`, through `git -C <main> show '
     '<ref>:.claude/settings.json`, so neither the working tree nor `settings.local.json` counts; '
     '`{}` when the file or ref is missing, git fails, or the file is not JSON or not an object), '
     '`setting` (the environment first when not blank, then a string or number from that `env`), '
     '`patterns` (a comma-separated list, stripped, empties dropped), `DEFAULT_TEST_PATTERNS` (the '
     'HARNESS_TEST_PATTERNS default tests_first, harness-red and the auto-land gate share) and '
     '`glob_match` (repo-relative, `**` takes zero or more segments, other segments `fnmatchcase`, a '
     'pattern without `/` matches the basename); branch_guard, tests_first, verify_on_stop, '
     'harness-red and harness-work import them and keep no copy.'),
    (('settings.py',),
     'It also holds `DEFAULT_TEST_PATTERNS` and `DEFAULT_SOURCE_EXEMPT`, the defaults of '
     "HARNESS_TEST_PATTERNS and HARNESS_SOURCE_EXEMPT with harness-red's strings, which `harness-work "
     'cost` reads; harness-red and tests_first import both and keep no copy.'),
    (('settings.py',),
     'tests_first classifies with `settings_env(main, ("settings.json",))` only, the main checkout\'s '
     'tracked file.'),
    (('settings.py',),
     'harness-scaffold keeps its own `TEST_FILE_PATTERNS`.'),
    (('notify.py',),
     'hooks/notify.py, harness-board, harness-pr and harness-digest import `notify.py`, the one macOS '
     'notification (`notify(message, group, title="Harness", subtitle="") -> bool`: terminal-notifier '
     'with `-title`, `-subtitle` only when given, `-message`, `-group`, `-activate`, `-sound`, else '
     'osascript; it answers whether either showed it and never raises), with `env_setting`, '
     '`succeeded`, `applescript_string`, `DEFAULT_APP` and `DEFAULT_SOUND`; no other file keeps a '
     'copy.'),
    (('cli.py', 'text.py'),
     "`cli.py` and `text.py` hold the bin tools' command-line scaffolding and their text, file and "
     'time helpers; harness-board, harness-pr, harness-standup and harness-digest import them and '
     'keep no copy, harness-worklog imports `Parser`, `run_main` and `session_folder`, and the hooks '
     'session_brief and tests_first import `read_json` (session_brief also `one_line`, with a '
     "fallback copy of both only in its lib import's except branch); harness-projects imports "
     '`Problem` but keeps its own `Parser`, and harness-work, harness-red and harness-scaffold still '
     'carry their own `Parser`, `Problem` and some of the helpers below, until they move over.'),
    (('cli.py',),
     '`cli.py` holds `Problem` (a bad invocation, a failed precondition or a failed call: one line, '
     'exit 1), `Parser` (argparse whose `error` is `<prog>: error: <message>` on stderr and exit 1), '
     '`run_main(prog, main)` (the `__main__` tail: a `Problem` is `<prog>: <one line>`, any other '
     'exception `<prog>: <TypeName>: <one line>`, both exit 1 and never a traceback; Ctrl-C exits '
     '130; a closed stdout (`| head`) exits 1 quietly; `SystemExit` passes through; otherwise 0), '
     '`warn(prog, message)` (`<prog>: warning: <one line>` on stderr), `emit(as_json, data, text)` '
     '(indented JSON with non-ASCII kept, else the text), `table(rows)` (left-aligned columns two '
     'spaces apart) and `issue_number` (an argparse `type=`: a positive number, `#` allowed).'),
    (('text.py',),
     '`text.py` holds `one_line` (whitespace runs collapsed), `first_line` and `last_line` (the first '
     'or last non-blank line, stripped, `""` for none), `as_list` (a gh list given as a JSON list or '
     '`{"nodes": [...]}`, its objects only), `read_text(path, what)` (`~` expanded, undecodable bytes '
     'replaced, a `Problem` naming `what` and the path when unreadable), `read_json` (None when '
     'unreadable or invalid), `write_json(path, data, *, prefix)` and `write_text(path, text, *, '
     "prefix)` (written to a `prefix`ed temporary file in the target's folder and renamed over the "
     'target, so a reader never sees half a file; the folder is made, the temporary file removed on '
     'failure), `home` and `harness_home` (`~/...` and `~/.claude/harness/...`), `same_path` (whether '
     "two paths name the same real path, `~` expanded), `session_folder` (Claude Code's folder name "
     'for a project: every non-alphanumeric turned into `-`), `now_iso` (the current UTC second, '
     '`2026-10-16T12:34:56Z`), `parse_iso` (an aware datetime, a `Z` or a naive time meaning UTC, '
     'else None), `parse_iso_seconds` (epoch seconds, a naive time meaning local time, else None) and '
     '`age_text(then, now)` (`just now`, `5m ago`, `3h ago`, `2d ago`, `4mo ago`, `2y ago`).'),
    (('registry.py',),
     '`registry.py` is the one reader and writer of `~/.claude/harness/projects.json` for '
     'harness-projects, harness-board, harness-standup and harness-digest: `Project(path, name, '
     'billable=False, client="", notes="", extra={})` (`from_entry` fills in the schema\'s fields, the '
     "name defaulting to the folder's, and keeps unknown keys in `extra`; `to_entry()` writes them "
     'back), `registry_path()`, `load_registry(strict=False, path=None)` (`path` defaults to '
     '`registry_path()`; `[]` for a missing file; Problem with the one wording of `is not valid JSON '
     '... fix it or remove it`, `cannot read <path>: <error>`, `expected {"version": 1, "projects": '
     '[...]}` and `is version ...; this tool reads version 1`; an entry without a path is skipped, or '
     'with `strict`, as harness-projects reads, `<path>: project N has no path`), '
     "`save_registry(projects)` (through text.py's `write_json`) and `find_project(projects, key)` "
     '(exact name, then any case, then path; Problem for none or several).'),
    (('registry.py',),
     'No tool keeps a copy; harness-pr keeps its own reader, which turns a malformed registry into an '
     'empty table.'),
    (('gh.py',),
     '`gh.py` holds the gh helpers: `gh_exe()` (`HARNESS_GH`, else `gh`), `gh_binary()` (that name as '
     'a path when it holds a separator and is executable, else looked up on PATH; None when not '
     'found), `GH_ENV` (`GH_PROMPT_DISABLED`, `GH_NO_UPDATE_NOTIFIER`, `NO_COLOR`, each `1`), '
     '`describe_list_call(args)`, `repo_slug(origin_url)` and `GhBatch(gh, calls, cwd, deadline, *, '
     'budget_seconds, describe=describe_list_call)`, the gh calls run side by side whose failures are '
     "cli.py's `Problem` and whose timeout names the `budget_seconds` it was given; harness-standup "
     'and harness-digest import them and pass their own `GH_BUDGET`.'),
    (('gh.py',),
     'It also holds the sequential wrappers: `describe(args)` (`gh api ` plus the first argument '
     'after `api` that is `graphql` or a path, holding `/` and not starting with `-`; otherwise `gh ` '
     'plus the words before the first flag, three of them at most), `gh_run(args, cwd, stdin=None, '
     'timeout=60, *, errors="replace", env=None, describe=describe, texts=None)` (gh_exe() in `cwd`, '
     'stdin from `stdin` or /dev/null, os.environ plus `env`, by default only `GH_PROMPT_DISABLED=1`; '
     'the CompletedProcess whatever the exit, a `Problem` when gh is missing, cannot start or times '
     'out), `gh(args, cwd, ..., pick=first_line, ..., run=None)` (its stdout, a `Problem` naming the '
     'call and pick(stderr) or `exit N` on a failed exit; with `run`, the call is `run(args, cwd, '
     'stdin, timeout)` in place of gh_run) and `gh_json(args, cwd, *, pick=first_line, '
     "empty_ok=False, via=None, **kw)` (with `via`, the output is `via(args, cwd)` in place of gh's; "
     'the excerpt of output that is not JSON is `pick(output)`), with `GH_TEXTS`, the five messages '
     "as templates a caller's `texts` replace key by key (`gh not found (<exe>); install it with "
     '...`, `cannot run gh (<exe>): ...`, `<call> timed out after N s`, `<call> failed: ...`, `<call> '
     'printed something other than JSON: <excerpt>`).'),
    (('gh.py',),
     'harness-board imports `describe` and harness-pr `describe` and `gh_run`; each keeps its own '
     '`gh` and `gh_json` (board repo-first, `gh(repo, args, stdin=None)` in the toplevel, None '
     'meaning the cwd; pr `gh(args, cwd, stdin=None, timeout=GH_TIMEOUT)`) only as one-statement '
     "adapters over the lib's, passing its own `first_line`, `gh_run` and `gh` as `pick`, `run` and "
     "`via` so they resolve through the tool's namespace, and harness-work keeps a one-statement "
     '`gh_json(repo, args)` over them that keeps its own wording, the last stderr line, '
     'surrogateescape decoding, `repo.main` as cwd and empty output as None.'),
    (('gh.py',),
     "session_brief imports `gh_binary` and `GH_ENV` (a fallback copy only in its lib import's except "
     'branch) and keeps its own `run_side_by_side`.'),
    (('waves.py',),
     '`waves.py` is the one writer and reader of wave records (`waves/<id>.json`, under State), so '
     'harness-work, which writes them, and harness-pr and autoland, which read them, agree: `WAVE_ID` '
     '(`w[0-9]{14}(-[0-9]+)?`), `DONE` (`in-review`, `closed`), `waves_dir(key)`, `wave_path(key, '
     'wave_id)`, `create_wave(key, main, tickets, tier, effort, at=None)` (the id is reserved by '
     'creating its file with O_CREAT|O_EXCL, so two waves never share one; a naive `at` means UTC), '
     '`load_wave` (None for a missing, broken or non-object file, or one holding another id), '
     "`load_waves` (every readable `<wave id>.json`, oldest first by `created_at`, then the id's "
     "suffix), `save_wave` (through text.py's `write_json`), `wave_of(key, ticket)` (the newest wave "
     'listing the ticket, dropped tickets not counted), `drop_ticket(key, wave_id, ticket)` (moves it '
     'to `dropped`; None and no file for a missing wave), `ticket_statuses(key, tickets)` (from the '
     'ticket caches), `progress(wave, statuses)`, `wave_state(wave, statuses)`, '
     '`wave_marker(wave_id)` and `has_wave_review(reviews, wave_id)`.'),
    (('ghlimit.py',),
     '`ghlimit.py` holds the gh call log, `~/.claude/harness/gh/calls-<UTC date>.jsonl` (State): '
     '`gh_dir()` (`harness_home("gh")`, read at call time), `call_kind(args)` (`rest` for `api` on '
     'any path but `graphql` and for `run`, `label`, `release` and `workflow`; `graphql` for '
     "everything else, the command's main bucket rather than an exact request count), "
     '`call_name(args)` (for `api`: `api `, the method when `-X`/`--method` gives one other than GET, '
     'then the path, the first argument that is `graphql` or holds `/` and is not a flag, else the '
     'first that is not a flag, without its query string, `repos/<owner>/<repo>/` as '
     '`repos/:owner/:repo/`, an all-digit segment as `:n` and a 7-40 character hex one as `:sha`; '
     'otherwise the first two words), `ticket_of(cwd)` (N of `/.claude/worktrees/t-N-` in `PWD`, else '
     'in `cwd`; else None), `repo_of(cwd)` (the folder name of the main checkout: the part of cwd '
     "before `/.claude/worktrees/`, else cwd's own name; cwd None is the current directory), "
     '`log_call(args, *, cwd, exit, ms, waited_ms=0, refused=False)` (one line `{"at", "tool", "cmd", '
     '"kind", "repo", "ticket", "exit", "ms", "waited_ms", "refused"}`, `at` from `now_iso()` and '
     '`tool` the basename of `sys.argv[0]`, appended with a single write to the day file opened '
     "`O_APPEND`; the call that creates a day's file deletes the day files dated more than 14 days "
     "before it; any OSError is skipped silently, so logging never changes a command's output, exit "
     'or gh argv) and `read_calls(since)` (every line with `at` at or after the aware datetime '
     '`since`, from the day files that can hold it, bad lines skipped).'),
    (('ghlimit.py',),
     '`gh_run` logs each call when it returns or times out (exit null); `GhBatch` logs each call when '
     '`results()` reads it and `close()` every call not yet logged (a finished one with its exit, a '
     "stopped one with exit null), one line per call; session_brief's `run_side_by_side` logs each of "
     'its gh calls once when the lib loaded (tool `session_brief.py`) and nothing in the fallback.'),
    (('ghlimit.py',),
     "harness-scaffold's own gh calls are not logged."),
    (('autoland.py',),
     '`autoland.py` holds the auto-land gate (The auto-land gate, under bin tools): '
     '`DEFAULT_MANUAL_PATHS`, `gate_settings(main, ref="origin/main")` (from settings.py\'s '
     '`committed_env` only), `evaluate(facts, settings)` (the ten rules, matching paths with '
     "settings.py's `glob_match`), `gather_facts(main, slug, pr)` (the facts, from gh and git), "
     '`check_entries` and `overall` (a statusCheckRollup read as harness-pr checks and land read it; '
     'harness-pr imports them, its `check_entries` a one-statement adapter through its own `as_list`) '
     'and the GraphQL `CONVERSATION` query harness-pr comments also runs; it imports only the '
     'standard library and harness_lib.'),
    (('redproof.py',),
     '`redproof.py` holds whether a red record is current (Red record, below): `tests_hash(contents)` '
     '(the `test_hash` digest of a path → bytes map), `read_files(root, files)`, `red_files_path`, '
     '`save_red_files` and `load_red_files` (the side file of red-time bytes) and '
     '`record_current(root, record, record_path, timeout=30)` (the budget for all its git calls '
     'together); harness-red (`check` and `status`) and tests_first import them and keep no copy.'),
    (('routing.py',),
     '`routing.py` holds the model routing: `TIERS`, `EFFORTS`, `KEYS` and `WAVE_EFFORT`, '
     '`label_tier(kind, size, risk)` and `review_label_tier(risk)` (the tier rubric below), '
     '`load_routing(main)` (the five routing settings of the Settings table as `{code_tier, '
     'code_effort, review_tier, ultrareview, wave_tier, wave_effort, problems}`: defaults when unset, '
     'an invalid value ignored with one line in `problems`), and `code_route(kind, size, risk, '
     'routing)` and `review_route(risk, routing)` (`{tier, effort, from, labels_tier}`, `from` being '
     "`labels` or the overriding setting's name)."),
    (('routing.py',),
     'harness-pr imports `load_routing` (for `ultra`); no other tool does yet.'),
    (('failopen',),
     'Fail open: a hook that cannot load the lib (missing, a syntax error, an import that raises) '
     'writes `<hook>.py: cannot load harness_lib from <lib>: <error>` to stderr in one line and exits '
     '0, so the call is allowed; notify.py also prints the bell (`{"terminalSequence": "\\u0007"}`, as '
     "when `notify` answers False), because a Notification hook's stderr reaches no one; "
     'session_brief instead goes on without ticket state and puts one `Warning:` line naming '
     "harness_lib in the brief, because the guards are then off and a SessionStart hook's stderr "
     'reaches no one; in a project that declares its own SessionStart hook, where the brief is '
     'otherwise silent, that warning line is its whole output.'),
    (('failopen',),
     'The bin tools do not fail open: a bin tool that imports the lib (every one but '
     'harness-scaffold) prints `<tool>: cannot load harness_lib from <lib>: <error>` in one line and '
     'exits 1 before it parses an argument, because it must not guess a state key.'),
]
OLD_LINE_LENGTH = 17050  # characters in the old line 41, without its newline

END_WORDS = 6  # the last words of each piece still close it, in order, wherever it moved
KEEP_SHARE = 0.9  # a module's entry keeps at least this share of its single-owner pieces' words
# The modules the old paragraph never described, which this ticket gives their first line. Named,
# not derived, so a later module (reviews.py) is held only to the checks every entry meets.
FRESH_MODULES = ("cost.py", "repo.py", "transcripts.py")
DOC_WORDS = 3  # a FRESH_MODULES line shares at least this many words with its docstring's first paragraph
FAIL_OPEN_START = "Fail open: "

# The entries of the State `repos/<repo-key>/` bullet, sorted, each with the words that followed it
# on docs/design.md line 119 as of 2026-10-26.
REPO_ENTRIES = [
    ("cost/<issue>.json", "what the ticket cost in tokens and model time, written by harness-work cost; "
                          "schema under harness-work"),
    ("red/<branch-slug>.json", "red proofs"),
    ("scratch/<branch-slug>/", "the adversary's working copy"),
    ("tickets/<issue>.json", "ticket cache"),
    ("ultra/<pr>.json", ""),
    ("waves/<id>.json", "one dispatch batch, below"),
]


def design_lines() -> list:
    text = DESIGN.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)  # a comment documents nothing
    return text.splitlines()


def headings(lines: list) -> list:
    """(index, level, title) of each Markdown heading outside fenced code blocks."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"^(#{1,6}) (.*\S)\s*$", line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def section(lines: list, title_line: str, stop_level: int) -> tuple:
    """(start, end) line indexes of the section under the one heading `title_line` (exact text),
    from the line after it up to the next heading of level <= stop_level (or the end)."""
    hs = headings(lines)
    found = [(i, lvl) for i, lvl, t in hs if lines[i].rstrip() == title_line]
    if len(found) != 1:
        raise AssertionError(f"want exactly one heading {title_line!r} in docs/design.md, found {len(found)}")
    start = found[0][0]
    end = next((i for i, lvl, _ in hs if i > start and lvl <= stop_level), len(lines))
    return start + 1, end


def lib_modules() -> list:
    names = sorted(p.name for p in LIB_PKG.glob("*.py") if p.name != "__init__.py")
    if len(names) < 5:
        raise AssertionError(f"expected the harness_lib modules on disk, found {names}")
    return names


def module_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))


def words(text: str) -> list:
    return re.findall(r"[A-Za-z0-9']+", text.replace("’", "'"))


def backticked(text: str) -> list:
    out = []
    for t in re.findall(r"`([^`]+)`", text):
        if t not in out:
            out.append(t)
    return out


def contains_run(haystack: list, run: list) -> bool:
    return any(haystack[k:k + len(run)] == run for k in range(len(haystack) - len(run) + 1))


def owned_pieces(owner: str) -> list:
    """The old pieces whose only owner is `owner`."""
    return [text for owners, text in OLD_PIECES if owners == (owner,)]


def all_pieces(owner: str) -> list:
    """The old pieces `owner` owns, alone or with another owner."""
    return [text for owners, text in OLD_PIECES if owner in owners]


def foreign_tails(owner: str) -> list:
    """The closing END_WORDS words of every old piece `owner` does not own, except a run that also
    occurs in one of its own pieces (two pieces end `import them and keep no copy`)."""
    own = [words(text) for text in all_pieces(owner)]
    out = []
    for owners, text in OLD_PIECES:
        tail = words(text)[-END_WORDS:]
        if owner in owners or any(contains_run(w, tail) for w in own):
            continue
        out.append((owners, tail))
    return out


def docstring_head(module: Path) -> str:
    """The first paragraph of a module's docstring."""
    import ast
    doc = ast.get_docstring(ast.parse(module.read_text(encoding="utf-8"))) or ""
    return doc.split("\n\n", 1)[0]


def fenced_ranges(lines: list, start: int, end: int) -> set:
    """Indexes of the lines in [start, end) that sit in a fenced code block, fences included."""
    out, fenced = set(), False
    for i in range(start, end):
        if lines[i].lstrip().startswith("```"):
            out.add(i)
            fenced = not fenced
        elif fenced:
            out.add(i)
    return out


def public_names(module: Path) -> list:
    """Top-level public functions, classes and UPPER_CASE constants of a harness_lib module."""
    import ast
    tree = ast.parse(module.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.extend(t.id for t in targets if isinstance(t, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]+", t.id))
    return [n for n in names if not n.startswith("_")]


def importers(stem: str) -> list:
    """Names (bin tool, hook or lib module stem) of the plugin files that import harness_lib.<stem>."""
    pat = re.compile(r"harness_lib\.%s\b|from harness_lib import [^\n#]*\b%s\b" % (stem, stem))
    files = [p for p in (PLUGIN / "bin").iterdir() if p.is_file()]
    files += sorted((PLUGIN / "hooks").glob("*.py")) + sorted(LIB_PKG.glob("*.py"))
    out = []
    for p in sorted(files):
        if p.stem == stem:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pat.search(text):
            out.append(p.stem)
    return out


class LibEntries(unittest.TestCase):
    def setUp(self):
        self.lines = design_lines()

    def module_section(self) -> list:
        layout = section(self.lines, "## Layout", 2)
        start, end = section(self.lines, HEADING, 6)
        self.assertTrue(layout[0] <= start - 1 < layout[1],
                        f"{HEADING!r} must sit inside the ## Layout section")
        return self.lines[start:end]

    def bullet_lines(self, body: list) -> list:
        """Every top-level bullet line of the section; each must be a module entry."""
        bullets = [ln for ln in body if re.match(r"^[-*+] ", ln)]
        for ln in bullets:
            self.assertRegex(ln, ENTRY, f"not a `` - `<module>.py`: ... `` entry: {ln[:120]!r}")
        return bullets

    def test_each_lib_module_has_exactly_one_entry(self):
        body = self.module_section()
        names = [ENTRY.match(ln).group(1) for ln in self.bullet_lines(body)]
        on_disk = lib_modules()
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], f"modules with more than one entry: {dupes}")
        missing = sorted(set(on_disk) - set(names))
        extra = sorted(set(names) - set(on_disk))
        self.assertEqual(missing, [], f"modules on disk with no entry under {HEADING!r}")
        self.assertEqual(extra, [], f"entries naming no module in {LIB_PKG}")
        self.assertEqual(sorted(names), on_disk)

    def test_entries_are_alphabetical_single_lines(self):
        body = self.module_section()
        idx = [i for i, ln in enumerate(body) if ENTRY.match(ln)]
        self.assertTrue(idx, f"no entries under {HEADING!r}")
        names = [ENTRY.match(body[i]).group(1) for i in idx]
        self.assertEqual(names, sorted(names), "entries are not in alphabetical order of module name")
        # one block of consecutive single-line bullets: nothing (a wrapped line, an indented
        # continuation, a blank) between two entries, and no continuation after the last
        self.assertEqual(idx, list(range(idx[0], idx[-1] + 1)),
                         "something other than an entry line sits between two entries (a wrapped entry?)")
        after = body[idx[-1] + 1] if idx[-1] + 1 < len(body) else ""
        self.assertEqual(after.strip(), "", f"the last entry continues on the next line: {after[:120]!r}")
        for i in idx:
            self.assertTrue(len(words(ENTRY.match(body[i]).group(2))) >= 6,
                            f"entry too thin to describe its module: {body[i]!r}")

    def entries(self) -> dict:
        """module name -> its whole entry line (the first one, if there are several)."""
        out = {}
        for ln in self.module_section():
            m = ENTRY.match(ln)
            if m:
                out.setdefault(m.group(1), ln)
        return out

    def assert_old_pieces_are_the_old_paragraph(self):
        joined = " ".join(text for _, text in OLD_PIECES)
        self.assertEqual(len(joined), OLD_LINE_LENGTH, "OLD_PIECES no longer joins back into the old line 41")
        self.assertEqual(backticked(joined), OLD_TERMS, "OLD_PIECES and OLD_TERMS disagree")

    def assert_piece_kept(self, piece: str, homes: dict, where: str):
        """Every backticked term of `piece` is in one of `homes` (name -> text), and the piece's last
        END_WORDS words close it, in order, inside one of them."""
        joined = "\n".join(homes.values())
        lost = [t for t in backticked(piece) if f"`{t}`" not in joined]
        self.assertEqual(lost, [], f"{where} lost these terms of {piece[:80]!r}")
        tail = words(piece)[-END_WORDS:]
        self.assertTrue(any(contains_run(words(text), tail) for text in homes.values()),
                        f"{where} lost the words that end {piece[:80]!r}: {' '.join(tail)!r}")

    def test_described_modules_keep_their_own_text(self):
        """Text moved, not rewritten: each module the old paragraph described carries all of its own
        sentences in its entry: every backticked term, the closing words of each sentence, and at
        least KEEP_SHARE of the words. A sentence about two modules may be split or repeated."""
        self.assert_old_pieces_are_the_old_paragraph()
        entries = self.entries()
        for owners, piece in OLD_PIECES:
            if owners in (("intro",), ("failopen",)):
                continue
            with self.subTest(owners=owners, piece=piece[:60]):
                homes = {o: entries[o] for o in owners if o in entries}
                self.assertTrue(homes, f"no entry for {owners}")
                self.assert_piece_kept(piece, homes, f"the entry of {' or '.join(owners)}")
        modules = sorted({o for owners, _ in OLD_PIECES for o in owners if o.endswith(".py")})
        for name in modules:
            with self.subTest(module=name, check="word share"):
                self.assertIn(name, entries, f"no entry for {name}")
                want = sum(len(words(text)) for text in owned_pieces(name))
                got = len(words(entries[name]))
                self.assertGreaterEqual(got, KEEP_SHARE * want,
                                        f"{name}'s entry has {got} words; its old sentences had {want}")

    def assert_borrows_nothing(self, owner: str, text: str, where: str):
        """`text` ends no old sentence that another owner owns (its closing END_WORDS words, in order)."""
        got = words(text)
        borrowed = [f"{'/'.join(owners)}: {' '.join(tail)!r}" for owners, tail in foreign_tails(owner)
                    if contains_run(got, tail)]
        self.assertEqual(borrowed, [], f"{where} carries sentences other owners own")

    def test_entries_carry_only_their_own_text(self):
        """Each entry stands for its own module only: it does not end any old sentence another
        module (or the intro, or Fail open) owns. No length cap: a later ticket may add a sentence
        about its own module."""
        self.assert_old_pieces_are_the_old_paragraph()
        for name, line in sorted(self.entries().items()):
            with self.subTest(module=name):
                self.assert_borrows_nothing(name, line, f"{name}'s entry")

    def test_intro_and_fail_open_keep_their_text(self):
        """The intro keeps the old opening sentences (standard library, prologue, never
        CLAUDE_PLUGIN_ROOT); the closing `Fail open: ...` sentences are a paragraph of their own in
        ## Layout, outside the module entries."""
        self.assert_old_pieces_are_the_old_paragraph()
        intro = [ln for ln in self.lines if ln.startswith(INTRO_START)]
        self.assertEqual(len(intro), 1, f"want one line starting {INTRO_START!r}")
        for piece in owned_pieces("intro"):
            self.assert_piece_kept(piece, {"intro": intro[0]}, "the intro paragraph")
        self.assert_borrows_nothing("intro", intro[0], "the intro paragraph")
        start, end = section(self.lines, "## Layout", 2)
        mstart, mend = section(self.lines, HEADING, 6)
        fenced = fenced_ranges(self.lines, start, end)
        fail_open = [i for i in range(start, end)
                     if i not in fenced and self.lines[i].startswith(FAIL_OPEN_START)]
        self.assertEqual(len(fail_open), 1, f"want one ## Layout paragraph starting {FAIL_OPEN_START!r}")
        self.assertFalse(mstart <= fail_open[0] < mend, f"the Fail open paragraph sits under {HEADING!r}")
        self.assertFalse(self.lines[fail_open[0]].startswith(INTRO_START))
        for piece in owned_pieces("failopen"):
            self.assert_piece_kept(piece, {"failopen": self.lines[fail_open[0]]}, "the Fail open paragraph")
        self.assert_borrows_nothing("failopen", self.lines[fail_open[0]], "the Fail open paragraph")

    def test_no_layout_prose_outside_the_entries_names_a_module(self):
        """The module text lives only in the entries: no other prose line of ## Layout (the code block
        aside) names a module file, except that the Fail open paragraph keeps the old mention of
        notify.py (the hook)."""
        modules = lib_modules()
        start, end = section(self.lines, "## Layout", 2)
        mstart, mend = section(self.lines, HEADING, 6)
        fenced = fenced_ranges(self.lines, start, end)
        fail_open_text = " ".join(owned_pieces("failopen"))
        allowed_fail_open = [m for m in modules if module_pattern(m).search(fail_open_text)]
        for i in range(start, end):
            ln = self.lines[i]
            if i in fenced or (mstart <= i < mend and ENTRY.match(ln)):
                continue
            allowed = allowed_fail_open if ln.startswith(FAIL_OPEN_START) else []
            named = [m for m in modules if module_pattern(m).search(ln) and m not in allowed]
            self.assertEqual(named, [], f"## Layout line {i + 1} outside the entries names module files: "
                                        f"{ln[:120]!r}")

    def test_new_modules_name_what_they_hold_and_who_imports_them(self):
        """Each module the old paragraph never described that this ticket gives its first line
        (FRESH_MODULES) gets a line from its module docstring: it names something the module holds
        (a backticked public name parsed from the module) and no backticked public name of the
        other FRESH_MODULES, says what it holds in the docstring's words (at least DOC_WORDS words
        of the docstring's first paragraph outside the backticked names and the importers), and,
        when any plugin file imports it, names one of those importers."""
        entries = self.entries()
        described = {o for owners, _ in OLD_PIECES for o in owners}
        fresh = list(FRESH_MODULES)
        self.assertEqual([m for m in fresh if m in described], [], "a FRESH_MODULES module has old sentences")
        self.assertTrue(set(fresh) <= set(lib_modules()), f"FRESH_MODULES not all on disk: {fresh}")
        for name in fresh:
            with self.subTest(module=name):
                self.assertIn(name, entries, f"no entry for {name}")
                line = entries[name]
                names = public_names(LIB_PKG / name)
                self.assertTrue(names, f"{name} has no public names to describe it by")
                held = [n for n in names if re.search(r"`%s[`(.]" % re.escape(n), line)]
                self.assertTrue(held, f"{name}'s entry names none of {names} (backticked)")
                quoted = backticked(line)
                others = sorted({n for m in fresh if m != name for n in public_names(LIB_PKG / m)
                                 if n not in names and any(re.match(r"%s(\(|$)" % re.escape(n), q) for q in quoted)})
                self.assertEqual(others, [], f"{name}'s entry names what other modules hold")
                users = importers(name[:-3])
                if users:
                    hit = [u for u in users
                           if re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(u), line)]
                    self.assertTrue(hit, f"{name}'s entry names none of its importers {users}")
                prose = re.sub(r"`[^`]*`", " ", line.split(":", 1)[1])
                for u in users:
                    prose = re.sub(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(u), " ", prose)
                doc = {w.lower() for w in words(docstring_head(LIB_PKG / name)) if len(w) >= 5}
                shared = sorted({w.lower() for w in words(prose)} & doc - {"harness", name[:-3]})
                self.assertGreaterEqual(len(shared), DOC_WORDS,
                                        f"{name}'s entry does not say what the module holds in its "
                                        f"docstring's words (shares only {shared})")

    def test_intro_and_layout_line_name_no_module(self):
        modules = lib_modules()
        intro = [ln for ln in self.lines if ln.startswith("Code shared between files lives in")]
        self.assertEqual(len(intro), 1, "want one intro line starting 'Code shared between files lives in'")
        self.assertTrue(intro[0].startswith(INTRO_START), intro[0][:120])
        # the Layout code block's lib/ line
        start, end = section(self.lines, "## Layout", 2)
        block, fenced = [], False
        for ln in self.lines[start:end]:
            if ln.lstrip().startswith("```"):
                if fenced:
                    break
                fenced = True
                continue
            if fenced:
                block.append(ln)
        lib_lines = [ln for ln in block if ln.strip().startswith("lib/")]
        self.assertEqual(len(lib_lines), 1, f"want one lib/ line in the Layout block, found {lib_lines}")
        self.assertIn("harness_lib", lib_lines[0])
        self.assertIn("harness_lib modules", lib_lines[0], "the lib/ line points at the entries by heading name")
        for what, line in (("intro paragraph", intro[0]), ("Layout lib/ line", lib_lines[0])):
            named = [m for m in modules if module_pattern(m).search(line)]
            self.assertEqual(named, [], f"the {what} still names module files")

    def test_no_term_of_the_old_paragraph_is_dropped(self):
        start, end = section(self.lines, "## Layout", 2)
        text = "\n".join(self.lines[start:end])
        dropped = [t for t in OLD_TERMS if f"`{t}`" not in text]
        self.assertEqual(dropped, [], "backticked terms of the old harness_lib paragraph missing from ## Layout")


class StateEntries(unittest.TestCase):
    def test_repo_state_entries_are_nested_bullets_in_order(self):
        lines = design_lines()
        start, end = section(lines, "## State", 2)
        leads = [i for i in range(start, end) if lines[i].startswith(STATE_LEAD)]
        self.assertEqual(len(leads), 1, f"want one State bullet starting {STATE_LEAD!r}")
        lead = lines[leads[0]]
        self.assertIn("durable per-repository state", lead)
        for entry, _ in REPO_ENTRIES:
            self.assertNotIn(f"`{entry}`", lead, f"{entry} is still listed inline on the lead line")
        nested = []
        for ln in lines[leads[0] + 1:end]:
            if not ln.strip() or not ln[0].isspace():
                break
            nested.append(ln)
        for ln in nested:
            self.assertRegex(ln, r"^\s+- `", f"not a nested entry bullet (a wrapped line?): {ln[:120]!r}")
        got = [re.match(r"^\s+- `([^`]+)`", ln).group(1) for ln in nested]
        self.assertEqual(got, sorted(got), "the nested entries are not in alphabetical order")
        dupes = sorted({e for e in got if got.count(e) > 1})
        self.assertEqual(dupes, [], f"entries with more than one nested bullet: {dupes}")
        # today's six are there, each with today's words; a later ticket may add more in between
        missing = [e for e, _ in REPO_ENTRIES if e not in got]
        self.assertEqual(missing, [], "today's entries missing from the nested bullets")
        for entry, said in REPO_ENTRIES:
            with self.subTest(entry=entry):
                ln = nested[got.index(entry)]
                rest = words(ln.split(f"`{entry}`", 1)[1])
                want = words(said)
                found = any(rest[k:k + len(want)] == want for k in range(len(rest) - len(want) + 1))
                self.assertTrue(found, f"{entry}'s bullet lost today's words {said!r}: {ln.strip()!r}")


if __name__ == "__main__":
    unittest.main()
