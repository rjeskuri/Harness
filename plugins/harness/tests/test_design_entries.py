"""Tests for ticket #250 (epic #249): docs/design.md gives each harness_lib module and each
per-repository state entry its own line, so parallel tickets stop conflicting on one line.

- LibEntries: under a `### harness_lib modules` heading inside `## Layout`, one single-line
  bullet `` - `<module>.py`: ... `` per `*.py` in plugins/harness/lib/harness_lib/ except
  `__init__.py` (read from disk at test time), in alphabetical order; the intro paragraph and
  the Layout block's `lib/` line name no module file; every backticked term of the old line 41
  paragraph (embedded below, taken once on 2026-10-26 with re.findall(r'`([^`]+)`', line))
  is still in `## Layout`; each module the old paragraph described keeps its own terms in its
  own bullet (text moved, not rewritten).
- StateEntries: the `~/.claude/harness/repos/<repo-key>/` State bullet lists each entry as a
  nested bullet, sorted, with the old line 119's words.

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

# For each module the old paragraph described, terms from that module's own sentences (checked on
# 2026-10-26 against line 41): the text moves to the module's bullet, so they must be in it.
MODULE_TERMS = {
    "autoland.py": ["`DEFAULT_MANUAL_PATHS`", '`gate_settings(main, ref="origin/main")`',
                    "`evaluate(facts, settings)`", "`CONVERSATION`"],
    "cli.py": ["`Problem`", "`run_main(prog, main)`", "`warn(prog, message)`", "`table(rows)`"],
    "gh.py": ["`gh_exe()`", "`repo_slug(origin_url)`",
              "`GhBatch(gh, calls, cwd, deadline, *, budget_seconds, describe=describe_list_call)`"],
    "ghlimit.py": ["`call_kind(args)`", "`call_name(args)`",
                   "`log_call(args, *, cwd, exit, ms, waited_ms=0, refused=False)`", "`read_calls(since)`"],
    "hookio.py": ["`read_input()`", "`hook_cwd(data)`", "`edited_path(data)`", "`run_fail_open(name, main)`"],
    "notify.py": ['`notify(message, group, title="Harness", subtitle="") -> bool`', "`applescript_string`",
                  "`DEFAULT_SOUND`"],
    "paths.py": ["state-key helpers"],
    "redproof.py": ["`tests_hash(contents)`", "`load_red_files`",
                    "`record_current(root, record, record_path, timeout=30)`"],
    "registry.py": ["`registry_path()`", "`load_registry(strict=False, path=None)`", "`save_registry(projects)`",
                    "`find_project(projects, key)`"],
    "routing.py": ["`label_tier(kind, size, risk)`", "`load_routing(main)`", "`code_route(kind, size, risk, routing)`",
                   "`review_route(risk, routing)`"],
    "sessionstate.py": ["`SessionState.for_repo(root, data)`", "`porcelain_status(root, timeout)`",
                        "`status_paths(root, out)`"],
    "settings.py": ['`settings_env(root, names=("settings.json", "settings.local.json"))`',
                    '`committed_env(main, ref="origin/main")`', "`DEFAULT_TEST_PATTERNS`", "`glob_match`"],
    "shellwalk.py": ["`split_command`", "`command_words`", "`SED_IN_PLACE`", "`MAX_DEPTH`"],
    "text.py": ["`one_line`", "`first_line`", "`write_json(path, data, *, prefix)`", "`age_text(then, now)`"],
    "waves.py": ["`WAVE_ID`", "`create_wave(key, main, tickets, tier, effort, at=None)`",
                 "`wave_state(wave, statuses)`", "`has_wave_review(reviews, wave_id)`"],
}

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

    def test_described_modules_keep_their_own_terms(self):
        """Text moved, not rewritten: each module the old paragraph described carries its own terms."""
        body = self.module_section()
        entries = {}
        for ln in body:
            m = ENTRY.match(ln)
            if m:
                entries.setdefault(m.group(1), m.group(2))
        for name, terms in MODULE_TERMS.items():
            with self.subTest(module=name):
                self.assertIn(name, entries, f"no entry for {name}")
                for term in terms:
                    self.assertIn(term, entries[name], f"{name}'s entry lost {term}")

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
        self.assertEqual(got, [e for e, _ in REPO_ENTRIES])
        for (entry, said), ln in zip(REPO_ENTRIES, nested):
            with self.subTest(entry=entry):
                rest = words(ln.split(f"`{entry}`", 1)[1])
                want = words(said)
                found = any(rest[k:k + len(want)] == want for k in range(len(rest) - len(want) + 1))
                self.assertTrue(found, f"{entry}'s bullet lost today's words {said!r}: {ln.strip()!r}")


if __name__ == "__main__":
    unittest.main()
