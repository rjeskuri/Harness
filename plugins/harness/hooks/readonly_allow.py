#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash, last in hooks.json's Bash list): allow read-only commands without the auto-mode
classifier (#392, epic #383), so a classifier outage does not stall a run.

A hook's allow runs the call with `source: hook` and no classifier call; it does not get past an ask rule, the
built-in rm check or plan mode, and a deny from another hook (branch_guard, tests_first) beats it
(docs/research/hook-permission-decisions.md). This hook never denies or asks: it answers allow, reason `read-only`,
when every part of the command (branch_guard's walk(), each part in the directory it runs in) is one of these, by
its bare name, with no assignment, keyword or wrapper in front:
  - cd (first in the command or its subshell, or after ;, & or a newline; then ;, && or the end; at most -L or -P
    and one operand naming an existing folder, CDPATH unset for a relative one), pwd, true, test, [, and echo or
    printf with no $ or backtick in their words;
  - a reader: cat, head, wc, ls, grep, egrep, fgrep, cut, tr; tail but not -f, -F or --follow; sort but not -o,
    --output, --compress-program, --files0-from or --random-source; uniq with at most one operand; sed -n whose
    one script is <n>p, <n>,<m>p or <n>,$p. grep's file operands are found after its options (-e and -f name the
    pattern), and ugrep's --filter, --pager, --view, --format-open, --config, --save-config, --query and -Q get
    nothing;
  - git with nothing in front but -C <dir>, --no-pager, -P or --no-optional-locks, running status, log, diff, show,
    rev-parse, rev-list, merge-base, ls-files, ls-tree, blame, grep, stash list, worktree list or a branch listing,
    never with --output, -o, --ext-diff or --no-index (git grep: nor -O, --open-files-in-pager, --untracked or
    --no-exclude-standard), in the main checkout or one of its worktrees;
  - one of the nine harness bin tools (any words: permissions.allow already allows them one by one).
The whole command: no heredoc and no (( )) or $(( )); every output redirection to /dev/null, /dev/stdout,
/dev/stderr, /dev/fd/N or a descriptor; no $ or backtick in a reader's or git's words (but sed's <n>,$p).

What is read must lie in the session's project (the main checkout of CLAUDE_PROJECT_DIR, else of the event's cwd,
and its worktrees), ~/.claude/harness/ (not its backups/) or a temp directory ($TMPDIR, /tmp, /private/tmp), never
in a backup store (harness-backups), after links and .. are resolved, and be neither secret-shaped (backups.secret)
nor matched by HARNESS_SENSITIVE_PATHS (backups.sensitive_patterns), both judged relative to its own working tree
(by its full path outside one) and by its spelling as well as its real path: each reader's file operand (ls with
none: its folder), each input redirection, grep's -f file, each git operand (a pathspec, or a revision that could be
one) relative to the folder git runs in, and each path in a rev:path word relative to the working tree. A word
holding a glob, a brace expansion or a leading = names no known file: no decision. A folder grep reads (any file
operand that is a folder, as ugrep recurses into one, and with -r, -R or -d recurse and no file, the folder it runs
in) holds no symlink and nothing secret-shaped or matched by HARNESS_SENSITIVE_PATHS (each entry judged as an
operand is), within 20,000 entries. git grep is judged by its operands and pathspecs alone.

No decision (nothing on stdout, exit 0) otherwise, in plan mode, and with HARNESS_READONLY_ALLOW=off (the
environment first, then `env` in the main checkout's .claude/settings.json and settings.local.json). Fails open:
without its lib, or on an error of its own, one stderr line, exit 0, no decision.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
_LIB = os.path.normpath(os.path.join(_HERE, os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.backups import PROJECT_STORE, secret, sensitive_patterns
    from harness_lib.hookio import emit, hook_cwd, read_input, run_fail_open
    from harness_lib.paths import canonical_path
    from harness_lib.repo import main_checkout, worktree_entries
    from harness_lib.settings import claude_project_dir, glob_match, setting, settings_env

    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import branch_guard as guard
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"readonly_allow.py: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
    sys.exit(0)

BUDGET = 8.0  # seconds from the start; hooks.json gives the hook 10
DEADLINE = time.monotonic() + BUDGET
GIT_TIMEOUT = 3
WALK_LIMIT = 20000  # entries a folder grep reads whole may hold
ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                "permissionDecisionReason": "read-only"}}
HARNESS_TOOLS = {"harness-board", "harness-digest", "harness-pr", "harness-projects", "harness-red",
                 "harness-scaffold", "harness-standup", "harness-work", "harness-worklog"}
SHELL_OWN = {"pwd", "true", "test", "["}
# reader: (short options taking a value, long ones taking a value, long ones refused, short ones refused)
GREP_REFUSED = ("--filter", "--pager", "--view", "--format-open", "--config", "--save-config", "--query", "---")
READERS = {
    "cat": ("", (), (), ""),
    "head": ("nc", ("--lines", "--bytes"), (), ""),
    "tail": ("bcns", ("--lines", "--bytes", "--sleep-interval", "--pid", "--max-unchanged-stats"), ("--follow",),
             "fF"),
    "wc": ("", (), ("--files0-from",), ""),
    "ls": ("", (), (), ""),
    "cut": ("bcdf", ("--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"), (), ""),
    "tr": ("", (), (), ""),
    "sort": ("kotST", ("--key", "--field-separator", "--output", "--buffer-size", "--temporary-directory",
                       "--compress-program", "--random-source", "--files0-from", "--parallel", "--batch-size",
                       "--sort"),
             ("--output", "--compress-program", "--files0-from", "--random-source"), "o"),
    "uniq": ("fsw", ("--skip-fields", "--skip-chars", "--check-chars"), (), ""),
    "grep": ("efmABCdD", ("--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
                          "--directories", "--devices", "--label", "--include", "--exclude", "--exclude-dir",
                          "--exclude-from", "--binary-files"), GREP_REFUSED, "Q"),
}
GIT_READERS = {"status", "log", "diff", "show", "rev-parse", "rev-list", "merge-base", "ls-files", "ls-tree",
               "blame", "grep"}
GIT_FRONT = {"--no-pager", "-P", "--no-optional-locks"}
GIT_REFUSED = ("--output", "--ext-diff", "--no-index")
GIT_GREP_REFUSED = ("--open-files-in-pager", "--untracked", "--no-exclude-standard")
GIT_GREP_VALUES = ("--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
                   "--max-depth", "--threads")
WORKTREE_LIST = {"--porcelain", "-v", "--verbose", "-z"}
BRANCH_FLAG = re.compile(r"-[arvli]+\Z|--(?:all|remotes|verbose|list|show-current|ignore-case|color|no-color|column"
                         r"|no-column|omit-empty)\Z|--(?:color|column|sort|format|abbrev|contains|no-contains|merged"
                         r"|no-merged|points-at)=")
BRANCH_VALUES = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
EXPANSION = re.compile(r"[$`]")
QUIET = re.compile(r"/dev/(?:null|stdout|stderr|fd/\d+)\Z")
SED_SCRIPT = re.compile(r"\d+(?:,(?:\d+|\$))?p\Z")
SED_FLAGS = re.compile(r"-([nEr]*)(?:e(.*))?\Z", re.S)


def left() -> float:
    return max(0.1, min(GIT_TIMEOUT, DEADLINE - time.monotonic()))


def unread(word: str) -> bool:
    """A word that names no known file: an expansion, a glob, a brace expansion, zsh's leading =."""
    return bool(EXPANSION.search(word) or guard.SHELL_EXPANSION.search(word) or word.startswith("="))


def inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def long_is(name: str, full: str) -> bool:
    """`name` is the long option `full` or an abbreviation getopt could read as it."""
    return len(name) > 2 and full.startswith(name)


def refused_long(name: str, refused) -> bool:
    return any(long_is(name, r) or name.startswith(r) for r in refused)


def scan(args, shorts: str = "", longs=()):
    """(options, operands) as getopt_long reads `args`, options anywhere: options as (name, value or None), "-x"
    or "--name" as written. A short cluster ends at a letter of `shorts`, the rest of the word or the next word its
    value; a long option takes =value, or the next word when it is (an abbreviation of) one of `longs`. After --
    every word is an operand, and - is one."""
    options, operands, k = [], [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            operands += args[k:]
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if not eq and k < len(args) and any(long_is(name, full) for full in longs):
                value, eq, k = args[k], "=", k + 1
            options.append((name, value if eq else None))
        elif a.startswith("-") and len(a) > 1:
            for i in range(1, len(a)):
                if a[i] in shorts:
                    value = a[i + 1:]
                    if not value and k < len(args):
                        value, k = args[k], k + 1
                    options.append(("-" + a[i], value))
                    break
                options.append(("-" + a[i], None))
        else:
            operands.append(a)
    return options, operands


# ---------- the parts ----------


def cd_moves(part, args, here) -> bool:
    """A cd that surely runs in this shell and lands where the walker says."""
    lead = part.lead.rsplit("(", 1)[-1].lstrip(")")
    if lead not in ("", ";", "&") or part.sep not in ("", ";", "&&"):
        return False
    words = [a for a in args if a not in ("-L", "-P")]
    if len(words) > 1 or any(w.startswith("-") for w in words):
        return False
    target = words[0] if words else "~"
    if unread(target):
        return False
    relative = not os.path.isabs(os.path.expanduser(target)) and target not in (".", "..")
    if os.environ.get("CDPATH") and relative and not target.startswith(("./", "../")):
        return False
    path = guard.resolve_dir(here, target)
    return path is not None and os.path.isdir(path)


def sed_reads(args, here, need) -> bool:
    """sed -n with one script, <n>p, <n>,<m>p or <n>,$p, and its files."""
    quiet, scripts, files, k = False, [], [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files += args[k:]
            break
        if a in ("--quiet", "--silent"):
            quiet = True
        elif a == "--regexp-extended":
            continue
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a == "--expression":
            scripts.append(args[k] if k < len(args) else "")
            k += 1
        elif a.startswith("-") and len(a) > 1:
            m = SED_FLAGS.match(a)
            if m is None:
                return False
            quiet = quiet or "n" in m.group(1)
            if m.group(2) is not None:
                script = m.group(2)
                if not script and k < len(args):
                    script, k = args[k], k + 1
                scripts.append(script)
        else:
            files.append(a)
    if not scripts and files:
        scripts.append(files.pop(0))
    if not quiet or len(scripts) != 1 or not SED_SCRIPT.match(scripts[0]):
        return False
    if any(EXPANSION.search(f) for f in files):
        return False
    need.extend(("read", f, here, True) for f in files)
    return True


def grep_reads(part, options, operands, here, need) -> None:
    """grep's pattern, files and folders: a -e or -f pattern makes every operand a file."""
    patterned = recursive = False
    for opt, value in options:
        if opt in ("-e", "-f") or long_is(opt, "--regexp") or long_is(opt, "--file"):
            patterned = True
        if opt in ("-r", "-R") or long_is(opt, "--recursive") or long_is(opt, "--dereference-recursive") or (
                (opt == "-d" or long_is(opt, "--directories")) and "recurse" in (value or "")):
            recursive = True
        if value is None or opt in ("-e",) or long_is(opt, "--regexp"):
            continue
        if opt == "-f" or long_is(opt, "--file") or long_is(opt, "--exclude-from"):
            need.append(("read", value, here, True))
        elif opt.startswith("--") and not any(long_is(opt, full) for full in READERS["grep"][1]):
            need.append(("read", value, here, False))
    pattern, files = ([], operands) if patterned else (operands[:1], operands[1:])
    need.extend(("read", w, here, False) for w in pattern)
    if not files and (recursive or not (part.lead.endswith("|") or part.inputs)):
        files = ["."]  # grep -r with no file searches its folder; without input it reads no stdin of its own
    for word in files:
        need.append(("read", word, here, True))
        need.append(("tree", word, here))


def reader_reads(part, name, args, here, need) -> bool:
    if name == "sed":
        return sed_reads(args, here, need)
    if any(EXPANSION.search(a) for a in args):
        return False
    kind = "grep" if name in ("egrep", "fgrep") else name
    shorts, longs, refused, refused_short = READERS[kind]
    options, operands = scan(args, shorts, longs)
    for opt, _value in options:
        refused_here = refused_long(opt, refused) if opt.startswith("--") else opt[1:] in refused_short
        if refused_here:
            return False
    if kind == "uniq" and len(operands) > 1:
        return False  # the second operand is uniq's output file
    if kind == "tr":
        return True  # its operands are sets; it reads stdin only
    if kind == "grep":
        grep_reads(part, options, operands, here, need)
        return True
    for opt, value in options:
        if value is not None and opt.startswith("--") and not any(long_is(opt, full) for full in longs):
            need.append(("read", value, here, False))
    need.extend(("read", w, here, True) for w in operands or (["."] if kind == "ls" else []))
    return True


def branch_listing(rest) -> bool:
    """git branch's listing forms: only listing options, and patterns only with --list or -l."""
    listing = any(a == "--list" or re.fullmatch(r"-[arvi]*l[arvli]*", a) for a in rest)
    k = 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a == "--":
            return listing or k >= len(rest)
        if a in BRANCH_VALUES:
            k += 1
        elif a.startswith("-"):
            if not BRANCH_FLAG.match(a):
                return False
        elif not listing:
            return False
    return True


def git_reads(args, here, need) -> bool:
    if any(EXPANSION.search(a) for a in args):
        return False
    where, k = here, 0
    while k < len(args) and args[k].startswith("-"):
        if args[k] == "-C" and k + 1 < len(args) and not unread(args[k + 1]):
            where = guard.resolve_dir(where, args[k + 1])
            k += 2
        elif args[k] in GIT_FRONT:
            k += 1
        else:
            return False  # -c, --git-dir, --work-tree and every other option in front
    if where is None or k >= len(args):
        return False
    sub, rest = args[k], args[k + 1:]
    if sub == "worktree":
        ok = rest[:1] == ["list"] and all(a in WORKTREE_LIST for a in rest[1:])
        need.append(("git", where, [], [], []))
        return ok
    if sub == "branch":
        need.append(("git", where, [], [], []))
        return branch_listing(rest)
    if sub == "stash":
        if rest[:1] != ["list"]:
            return False
        rest = rest[1:]
    elif sub not in GIT_READERS:
        return False
    refused = GIT_REFUSED + (GIT_GREP_REFUSED if sub == "grep" else ())
    for a in rest:
        if a == "--":
            break
        if a.startswith("--") and refused_long(a.split("=", 1)[0], refused):
            return False
        if guard.SHORT_CLUSTER.match(a) and ("o" in a[1:] or sub == "grep" and "O" in a[1:]):
            return False
    if sub == "grep":
        options, operands = scan(rest, "efABCm", GIT_GREP_VALUES)
        patterned = any(o in ("-e", "-f") or long_is(o, "--regexp") or long_is(o, "--file") for o, _v in options)
        files = [v for o, v in options if v is not None and (o == "-f" or long_is(o, "--file"))]
        values = [v for o, v in options if v is not None and v not in files and o != "-e"
                  and not long_is(o, "--regexp")]
        values += [] if patterned else operands[:1]
        operands = operands if patterned else operands[1:]
    else:
        operands, values, files = [], [], []
        for i, a in enumerate(rest):
            if a == "--":
                operands += rest[i + 1:]
                break
            if a.startswith("--") and "=" in a:
                values.append(a.split("=", 1)[1])
            elif a.startswith("-") and not a.startswith("--") and len(a) > 2:
                values.append(a[2:])  # a value given in the word: -L1,5:path
            elif not a.startswith("-") or a == "-":
                operands.append(a)
    need.append(("git", where, operands, values, files))
    return True


def part_reads(part, name, args, here, need) -> bool:
    """Whether the part may run, its reads added to `need`."""
    words = part.words
    if part.heredocs or not words or words[0] != name or len(words) != len(args) + 1:
        return False  # a heredoc, a path to the program, an assignment, keyword or wrapper in front
    if not all(QUIET.match(t) for t in part.targets):
        return False
    need.extend(("read", w, here, True) for w in part.inputs)
    if name in HARNESS_TOOLS or name in SHELL_OWN:
        return True
    if name in ("echo", "printf"):
        return not any(EXPANSION.search(a) for a in args)
    if name == "cd":
        return cd_moves(part, args, here)
    if name == "git":
        return git_reads(args, here, need)
    if name in READERS or name in ("egrep", "fgrep", "sed"):
        return reader_reads(part, name, args, here, need)
    return False


# ---------- where a read may go ----------


def project_roots(main):
    """The main checkout and every worktree of it, canonical, the deepest first."""

    def listing(cwd, *args):
        try:
            done = subprocess.run(["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="surrogateescape", timeout=left())
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""

    roots = {main}
    for entry in worktree_entries(main, listing):
        if os.path.isdir(entry.path):
            roots.add(canonical_path(entry.path))
    return sorted(roots, key=len, reverse=True)


class Places:
    def __init__(self, main) -> None:
        self.roots = project_roots(main)
        self.harness = canonical_path(os.path.join(os.path.expanduser("~"), ".claude", "harness"))
        temps = [os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp", "/private/tmp"]
        self.bases = self.roots + [self.harness] + [canonical_path(t) for t in temps if t]
        self.pats = sensitive_patterns(main)

    def allowed(self, real) -> bool:
        return (any(inside(real, base) for base in self.bases)
                and not inside(real, os.path.join(self.harness, "backups"))
                and PROJECT_STORE not in real.split(os.sep))

    def rels(self, path):
        """`path` relative to its working tree, or every tail of it outside one."""
        root = next((r for r in self.roots if inside(path, r)), None)
        if root is not None:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            return [] if rel == "." else [rel]
        parts = [p for p in path.split(os.sep) if p]
        return ["/".join(parts[i:]) for i in range(len(parts))]

    def clean(self, path) -> bool:
        return not any(secret(rel) or any(glob_match(p, rel) for p in self.pats) for rel in self.rels(path))

    def absolute(self, word, base):
        path = os.path.expanduser(word)
        if path.startswith("~"):
            return None
        if not os.path.isabs(path):
            if base is None:
                return None
            path = os.path.join(base, path)
        return path

    def reads(self, word, base, strict) -> bool:
        """May `word`, read from the folder `base`, be read? `strict`: a word that names no known file cannot."""
        if word == "-":
            return True
        if strict and unread(word):
            return False
        path = self.absolute(word, base)
        if path is None:
            return False
        real = canonical_path(path)
        return self.allowed(real) and self.clean(os.path.normpath(path)) and self.clean(real)

    def tree(self, word, base) -> bool:
        """A folder grep reads whole: no symlink, nothing secret-shaped or sensitive and no backup store in it."""
        real = canonical_path(self.absolute(word, base))
        if not os.path.isdir(real):
            return True
        count = 0
        for folder, dirs, files in os.walk(real):
            for name in dirs + files:
                count += 1
                entry = os.path.join(folder, name)
                if count > WALK_LIMIT or time.monotonic() > DEADLINE or os.path.islink(entry):
                    return False
                if name == PROJECT_STORE or not self.clean(entry):
                    return False
        return True

    def git(self, where, operands, values, files) -> bool:
        top = next((r for r in self.roots if inside(canonical_path(where), r)), None)
        if top is None:
            return False  # git runs only in the project's working trees
        if not (all(self.reads(w, where, True) for w in operands + files)
                and all(self.reads(w, where, False) for w in values)):
            return False
        for word in operands + values + files:  # rev:path, :path, :<n>:path
            for tail in [word[i + 1:] for i, ch in enumerate(word) if ch == ":" and word[i + 1:]]:
                base = where if tail in (".", "..") or tail.startswith(("./", "../")) else top
                if not self.reads(tail, base, False):
                    return False
        return True

    def check(self, item) -> bool:
        kind, rest = item[0], item[1:]
        if kind == "read":
            return self.reads(*rest)
        if kind == "tree":
            return self.tree(*rest)
        return self.git(*rest)


# ---------- the answer ----------


def decide(data, command) -> bool:
    cwd = hook_cwd(data)
    if "((" in command or not os.path.isdir(cwd):
        return False  # an arithmetic command or expansion runs what the walker cannot see
    state, need, parts = {"cwd": cwd}, [], 0
    for part, name, args in guard.walk(state, command):
        parts += 1
        if not part_reads(part, name, args, state["cwd"], need):
            return False
    if not parts:
        return False
    main = main_checkout(claude_project_dir() or cwd, timeout=2 * GIT_TIMEOUT)
    if main is None:
        return False
    switch = setting("HARNESS_READONLY_ALLOW", settings_env(main))
    if switch is not None and switch.lower() == "off":
        return False
    places = Places(main)
    return all(places.check(item) for item in need)


def main() -> int:
    data = read_input()
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") != "Bash" or not isinstance(command, str):
        return 0
    if data.get("permission_mode") == "plan":
        return 0
    if decide(data, command):
        emit(ALLOW)
    return 0


if __name__ == "__main__":
    # fail open: a broken hook must never trap the agent
    sys.exit(run_fail_open("readonly_allow.py", main))
