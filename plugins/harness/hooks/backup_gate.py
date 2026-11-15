#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): back up, then allow, rm in the project.

A command whose parts run rm, unlink or rmdir (branch_guard.py's walker: each part in the
directory it runs in) gets what they remove snapshotted with harness_lib/backups.py's create,
then allow, so a removal that can be undone runs without a prompt. What cannot be backed up
gets ask (over the size cap) or no decision at all, which leaves it to Claude Code's own rules.

No decision and no snapshot: plan mode; HARNESS_BACKUP=off (the environment first, then `env`
in the main checkout's .claude/settings.json and settings.local.json); a command branch_guard
refuses (its own decision, bash_refusal; unlink and rmdir count as rm of the same word from the
same directory); an unknown target: a word with $, a backtick, a brace expansion, **, ^, #, an
inner ~, a leading =, a bracket form fnmatch reads otherwise ([^, [[:), a word running on into
a parenthesis, xargs, an unknown directory (below), a glob matching nothing; a target outside
the session's project (the main checkout of CLAUDE_PROJECT_DIR, else of the event's cwd, and its
worktrees: the working tree git names from the target's folder must be one of them); /, $HOME
or a folder holding it, the main checkout, a worktree, any repository root or a folder holding a
project root, a .git directory or anything in one, and anything under .claude/, .githooks/ or
.github/ relative to its own working tree.

The directory is the walker's after a cd or pushd only when that cd surely runs, in this shell,
before what follows, and lands where the walker says: first in the command or after ;, & or a
newline, followed by ;, &&, a newline or the end of its subshell, outside if, loops, case and
{ }, behind no keyword, assignment or wrapper but builtin or command; cd's options at most -L
and -P, pushd's none; one operand, a word with nothing the shell expands but a leading ~ or ~/;
CDPATH unset or not consulted; an existing folder it may enter, its logical and physical paths
the same, that no removal in the command removes (nor a folder above it). Otherwise, and after
popd, the directory is unknown until the subshell or child shell holding the cd ends. Also
unknown: a removal inside a loop, one whose directory is not an existing folder, and every
removal when the command holds both a command or process substitution (the walker runs those
last) and a cd, pushd or popd.

Each existing target, by its path relative to its own working tree: secret-shaped (.env,
.env.*, *.pem, *.key, credentials/ and under) or sensitive (a HARNESS_SENSITIVE_PATHS match, or
a folder holding one) is copied; else a path with a component named node_modules, .venv, venv,
dist, build, __pycache__, .pytest_cache, .mypy_cache, .ruff_cache or .tox is rebuildable, with
only the secret-shaped paths inside it copied; everything else is copied. Inside a copied
folder each folder with such a name is judged on its own: holding a sensitive match, it is
copied (its own named folders judged in turn); holding secret-shaped paths, only they are kept;
otherwise it is left out of the copy and of the size.

The size (the regular files' st_size the copy keeps, links not followed) over
HARNESS_BACKUP_CAP_MB (default 200, MB being 10^6 bytes; the environment, then the settings as
for HARNESS_BACKUP) gets ask and no snapshot. Otherwise one snapshot of what is copied, then
allow when every other part is harmless (cd, pushd, popd, pwd, true, :, ls, echo, printf, test,
[ or a bare shell keyword, and no part redirects but to /dev/null or a descriptor), the reason
naming the snapshot id, also as additionalContext since an allow's reason never reaches the
model; with any other part the snapshot stays and there is no decision. Nothing to copy
(rebuildable or missing): allow on the same rule, no snapshot.

One deadline, 25 s from the start (hooks.json gives 30): out of time, a partial snapshot is
removed and there is no decision. Fails open: an error of its own (the lib missing, a store
that cannot be written) is one stderr line, exit 0, no decision.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import fnmatch
import itertools
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import unicodedata

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib import backups
    from harness_lib.hookio import emit, hook_cwd, read_input, run_fail_open
    from harness_lib.paths import canonical_path
    from harness_lib.repo import main_checkout, worktree_entries
    from harness_lib.settings import claude_project_dir, glob_match, setting, settings_env
    from harness_lib.shellwalk import KEYWORDS
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"backup_gate.py: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
    sys.exit(0)

BUDGET = 25.0  # seconds from the start; hooks.json gives the hook 30
DEADLINE = time.monotonic() + BUDGET
GIT_TIMEOUT = 5
DEFAULT_CAP_MB = 200
MB = 1_000_000
REMOVERS = {"rm", "unlink", "rmdir"}
HARMLESS = {"cd", "pushd", "popd", "pwd", "true", ":", "ls", "echo", "printf", "test", "["}
REBUILDABLE = frozenset(
    "node_modules .venv venv dist build __pycache__ .pytest_cache .mypy_cache .ruff_cache .tox".split()
)
GUARDED = {".claude", ".githooks", ".github"}
OPENERS = {"if", "while", "until", "for", "select", "case", "{"}
CLOSERS = {"fi", "done", "esac", "}"}
LOOPS = {"while", "until", "for", "select"}
RESERVED = KEYWORDS | OPENERS | CLOSERS
RM_WORD = re.compile(r"\b(?:rm|unlink|rmdir)\b")
GLOB = re.compile(r"[*?[]")
UNREAD = re.compile(r"[$`^#]|\*\*|\[\^|\[\[[:=.]|(?<=.)~(?=.)|\A=|\{[^{}]*(?:,|\.\.)[^{}]*\}")
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
QUIET = re.compile(r"/dev/(?:null|stdout|stderr|fd/\d+)\Z")


class OutOfTime(Exception):
    """The deadline passed: create removes its partial snapshot, and there is no decision."""


def out_of_time(_signum, _frame):
    raise OutOfTime(f"out of time after {BUDGET:g} s; no backup taken, no decision")


def branch_guard():
    """branch_guard.py from this directory: its walker and its own Bash decision."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import branch_guard as guard

    return guard


def git(cwd, *args):
    """git's stripped stdout, or None when it fails or does not answer in time."""
    try:
        done = subprocess.run(["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# ---------- the command ----------


def steps(guard, command, cwd, moves):
    """(part, name, args, its directory or None, run by xargs) for each simple command, in the walker's order. After
    a cd or pushd the walker's directory is kept only where `landing` gives it (each such folder goes into `moves`);
    otherwise, and after popd, it is unknown until the subshell or child shell holding the cd ends. A removal inside
    a loop is unknown too: a later pass may run it elsewhere."""
    state = {"cwd": cwd, "repos": {}}
    before, opened = "", []  # the operator before this part; the compound commands open around it
    for part, name, args in guard.walk(state, command):
        for word in itertools.takewhile(lambda w: w in RESERVED, part.words):
            if word in OPENERS:
                opened.append(word)
            elif word in CLOSERS and opened:
                opened.pop()
        here = None if name in REMOVERS and LOOPS.intersection(opened) else state["cwd"]
        wrappers = part.words[: max(len(part.words) - len(args) - 1, 0)]
        yield part, name, args, here, any(os.path.basename(w) == "xargs" for w in wrappers)
        if name in ("cd", "pushd", "popd"):
            folder = landing(part, name, args, here, before, opened, wrappers)
            if folder is None:
                state["cwd"] = None  # the walker's cd_target(None, ...) keeps it None
            else:
                moves.append(folder)
        before = part.sep


def landing(part, name, args, here, before, opened, wrappers):
    """The folder a cd or pushd surely leaves this shell in, the walker's own; None unless it runs whenever what
    follows it does (first, or after ;, & or a newline; then ;, &&, a newline or the subshell's end; outside every
    if, loop, case and { }; behind no keyword or wrapper but builtin or command) and cannot fail or land elsewhere
    (cd's options at most -L and -P, pushd's none; one operand, a plain word, ~ or ~/... alone expanded; CDPATH
    unset or not used; an existing folder it may enter, as the logical and the physical path name it)."""
    surely = not opened and before in ("", ";", "&") and part.sep in ("", ";", "&&", ")")
    if here is None or name == "popd" or not surely or any(w not in ("builtin", "command") for w in wrappers):
        return None
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        k += 1
        if args[k - 1] == "--":
            break
        if name == "pushd" or args[k - 1] not in ("-L", "-P"):
            return None
    rest = args[k:] or (["~"] if name == "cd" else [])
    if len(rest) != 1:
        return None
    word = rest[0]
    if not word or word[0] in "+-" or UNREAD.search(word) or GLOB.search(word):
        return None  # +N and -N name the directory stack in zsh
    if word.startswith("~"):
        if word != "~" and not word.startswith("~/"):
            return None  # ~+, ~-, ~0, ~user and zsh's named directories
        word = os.path.expanduser(word)
    if os.environ.get("CDPATH") and not word.startswith("/") and word.split("/")[0] not in (".", ".."):
        return None
    path = os.path.normpath(os.path.join(here, word))
    if not (os.path.isdir(path) and os.access(path, os.X_OK)):
        return None
    return path if os.path.realpath(path) == os.path.realpath(os.path.join(here, word)) else None


def lineage(folder):
    """(st_dev, st_ino) of `folder` and of each folder above it, as spelled (a link on the way counted itself) and
    resolved: a removal of any of them makes a cd into `folder` fail."""
    found = set()
    for path in (folder, os.path.realpath(folder)):
        while True:
            try:
                st = os.lstat(path)
                found.add((st.st_dev, st.st_ino))
            except OSError:
                pass
            if os.path.dirname(path) == path:
                break
            path = os.path.dirname(path)
    return found


def operands(name, args):
    """The words `name args` removes: those after its options (from the first operand on, every word, as BSD's
    getopt reads them; one that names nothing is nothing to copy), and for rmdir -p each word's parents too."""
    words, parents = [], False
    for k, a in enumerate(args):
        if a == "--":
            words = args[k + 1 :]
            break
        if not a.startswith("-") or a == "-":
            words = args[k:]
            break
        parents = parents or a == "--parents" or (not a.startswith("--") and "p" in a[1:])
    if name == "rmdir" and parents:
        for word in list(words):
            bits = word.rstrip("/").split("/")
            words += [up for up in ("/".join(bits[:i]) for i in range(len(bits) - 1, 0, -1)) if up]
    return words


def expand(word, here):
    """The existing paths the glob `word` matches from `here`, hidden names included (a superset of what any
    shell's options make of it), each with the word's trailing / kept."""
    found = [os.sep] if word.startswith("/") else [here]
    for comp in word.split("/"):
        if comp in ("", "."):
            continue
        if not GLOB.search(comp):
            found = [os.path.join(folder, comp) for folder in found]
            continue
        matched = []
        for folder in found:
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            matched += [os.path.join(folder, n) for n in names if fnmatch.fnmatchcase(n, comp)]
        found = matched
    tail = "/" if word.endswith("/") else ""
    return [path + tail for path in found if os.path.lexists(path)]


def named_paths(word, here):
    """The absolute paths an operand names from `here`; [] for an empty word, None when they cannot be known."""
    if not word:
        return []
    if UNREAD.search(word):
        return None
    if word.startswith("~"):
        word = os.path.expanduser(word)
    path = os.path.join(here, word)
    if not GLOB.search(word):
        return [path]
    found = expand(word, here)
    if os.path.lexists(path.rstrip("/") or os.sep) and path not in found:  # a quoted name spelled like a glob
        found.append(path)
    return found or None


def harmless(part, name):
    """A part whose only effect is to move or to print: one of HARMLESS (or a removal, or a bare keyword such as fi)
    redirecting to nothing but /dev/null or a descriptor."""
    if not all(QUIET.match(target) for target in part.targets):
        return False
    if name:
        return name in HARMLESS or name in REMOVERS
    return all(word in KEYWORDS for word in part.words)


# ---------- where a target is ----------


def disk_name(folder, name):
    """`name` as `folder` lists it: another case or Unicode form of it names the same entry on a case-insensitive
    disk."""
    try:
        names = os.listdir(folder)
        if name in names:
            return name
        st = os.lstat(os.path.join(folder, name))
    except OSError:
        return name
    fold = unicodedata.normalize("NFC", name).casefold()
    for other in names:
        if unicodedata.normalize("NFC", other).casefold() != fold:
            continue
        try:
            found = os.lstat(os.path.join(folder, other))
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (st.st_dev, st.st_ino):
            return other
    return name


def locate(path):
    """The entry the absolute `path` names, never followed itself: its folder resolved (links, ..) and each
    component spelled as on disk. None for a symlink given with a trailing /, which rm would reach through."""
    trailing = path.endswith("/")
    path = path.rstrip("/") or os.sep
    name = os.path.basename(path)
    if name in ("", ".", ".."):
        return canonical_path(path)
    folder = canonical_path(os.path.dirname(path))
    loc = os.path.join(folder, disk_name(folder, name))
    return None if trailing and os.path.islink(loc) else loc


def project_roots(main):
    """The main checkout and every worktree of it, canonical."""
    roots = {main}
    for entry in worktree_entries(main, lambda *args: git(*args) or ""):
        if os.path.isdir(entry.path):
            roots.add(canonical_path(entry.path))
    return roots


def own_top(loc, tops):
    """The working tree holding `loc`, as git names it from the nearest existing folder above; None outside one."""
    probe = os.path.dirname(loc)
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if probe not in tops:
        top = git(probe, "rev-parse", "--show-toplevel")
        tops[probe] = canonical_path(top) if top else None
    return tops[probe]


def place(loc, roots, home, tops):
    """`loc` relative to its own working tree (/ separators) when that tree is one of `roots` and the hook may back
    it up; None otherwise."""
    if loc == os.sep or ".git" in loc.split(os.sep):
        return None
    if any(r == loc or r.startswith(loc + os.sep) for r in roots | {home}):
        return None  # the main checkout, a worktree, $HOME, or a folder holding one
    if os.path.isdir(loc) and not os.path.islink(loc) and os.path.lexists(os.path.join(loc, ".git")):
        return None  # the root of another repository
    top = own_top(loc, tops)
    if top not in roots:
        return None
    rel = os.path.relpath(loc, top).replace(os.sep, "/")
    if rel in (".", "..") or rel.startswith("../") or rel.split("/")[0] in GUARDED:
        return None
    return rel


# ---------- what to copy ----------


def secret(rel):
    """Secret-shaped, in any case: .env, .env.*, *.pem, *.key, or credentials/ and under."""
    parts = rel.lower().split("/")
    name = parts[-1]
    return "credentials" in parts or name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".key"))


def sensitive(rel, pats):
    return any(glob_match(p, rel) for p in pats)


class Folder:
    """A folder in a target: the regular files' bytes directly in it (own) and under it (total), whether it holds a
    secret-shaped or a sensitive path, whether it is one itself (protected), and its folders by path."""

    __slots__ = ("path", "rel", "name", "own", "total", "secret", "sensitive", "protected", "folders")

    def __init__(self, path, rel):
        self.path, self.rel, self.name = path, rel, rel.rsplit("/", 1)[-1]
        self.own = self.total = 0
        self.secret = self.sensitive = self.protected = False
        self.folders = {}


def file_size(entry):
    return entry.stat(follow_symlinks=False).st_size if entry.is_file(follow_symlinks=False) else 0


def scan(path, rel, pats):
    node = Folder(path, rel)
    with os.scandir(path) as entries:
        for entry in entries:
            sub = rel + "/" + entry.name
            is_secret, is_sensitive = secret(sub), sensitive(sub, pats)
            if entry.is_dir(follow_symlinks=False):
                child = node.folders[entry.path] = scan(entry.path, sub, pats)
                child.protected = is_secret or is_sensitive
                node.total += child.total
                is_secret, is_sensitive = is_secret or child.secret, is_sensitive or child.sensitive
            else:
                size = file_size(entry)
                node.own += size
                node.total += size
            node.secret = node.secret or is_secret
            node.sensitive = node.sensitive or is_sensitive
    return node


def kept(node, leave):
    """The bytes a copy of the folder `node` keeps; what it leaves out of each named folder in it goes into
    `leave`: the folder whole, or all but its secret-shaped paths, or nothing when it holds a sensitive path."""
    total = node.own
    for child in node.folders.values():
        if child.name not in REBUILDABLE or child.protected or child.sensitive:
            total += kept(child, leave)
        elif child.secret:
            total += secrets(child, None, leave)
        else:
            leave.append(child.path)
    return total


def secrets(node, keep, leave):
    """The bytes of the secret-shaped paths in the folder `node`: each goes into `keep` (unless None), every other
    entry but the folders on the way to one into `leave` (unless None)."""
    total = 0
    with os.scandir(node.path) as entries:
        for entry in entries:
            child = node.folders.get(entry.path)
            if secret(node.rel + "/" + entry.name):
                total += child.total if child is not None else file_size(entry)
                if keep is not None:
                    keep.append(entry.path)
            elif child is not None and child.secret:
                total += secrets(child, keep, leave)
            elif leave is not None:
                leave.append(entry.path)
    return total


def judge(loc, rel, pats, items, leave):
    """The bytes copied of the target `loc`; what is copied goes into `items`, what is left out of it into `leave`."""
    try:
        st = os.lstat(loc)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    protected = secret(rel) or sensitive(rel, pats)
    rebuildable = any(part in REBUILDABLE for part in rel.split("/"))
    if not stat.S_ISDIR(st.st_mode):
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)) or (rebuildable and not protected):
            return 0
        items.append(loc)
        return st.st_size if stat.S_ISREG(st.st_mode) else 0
    node = scan(loc, rel, pats)
    if protected or node.sensitive or not rebuildable:
        items.append(loc)
        return kept(node, leave)
    return secrets(node, items, None) if node.secret else 0


# ---------- the answer ----------


def cap_mb(file_env):
    value = setting("HARNESS_BACKUP_CAP_MB", file_env)
    try:
        cap = float(value) if value is not None else DEFAULT_CAP_MB
    except ValueError:
        return DEFAULT_CAP_MB
    return cap if math.isfinite(cap) and cap >= 0 else DEFAULT_CAP_MB


def size_text(count):
    """Bytes in decimal units, as harness-work backup list shows them: 512 B, 3.4 kB, 1.2 MB."""
    if count < 1000:
        return f"{count} B"
    for unit in ("kB", "MB", "GB", "TB"):
        count /= 1000
        if count < 999.95 or unit == "TB":
            return f"{count:.1f} {unit}"
    return ""


def answer(kind, reason):
    out = {"hookEventName": "PreToolUse", "permissionDecision": kind, "permissionDecisionReason": reason}
    if kind == "allow":
        out["additionalContext"] = reason  # an allow's reason goes to the debug log only
    return {"hookSpecificOutput": out}


def decide(data, command):
    """The hook's answer to the Bash `command`, or None for no decision."""
    cwd = hook_cwd(data)
    if not os.path.isdir(cwd):
        return None
    main = main_checkout(claude_project_dir() or cwd, timeout=GIT_TIMEOUT)
    if main is None:
        return None
    file_env = settings_env(main)
    switch = setting("HARNESS_BACKUP", file_env)
    if switch is not None and switch.lower() == "off":
        return None
    guard = branch_guard()
    moves = []
    parts = list(steps(guard, command, cwd, moves))
    paths, removals = [], [step for step in parts if step[1] in REMOVERS]
    if not removals:
        return None
    if SUBSTITUTION.search(command) and any(step[1] in ("cd", "pushd", "popd") for step in parts):
        return None  # the walker runs what a substitution holds after the whole command, where its cds left it
    for part, name, args, here, xargs in removals:
        # ( after the words: a zsh glob qualifier or an extglob
        if here is None or not os.path.isdir(here) or xargs or part.sep == "(":
            return None
        words = operands(name, args)
        for word in words:
            found = named_paths(word, here)
            if found is None:
                return None
            paths += found
        # unlink and rmdir count as rm: where branch_guard refuses rm of the word, they get nothing either
        if name != "rm" and any(guard.bash_refusal("rm -- " + shlex.quote(w), here) for w in words if w):
            return None
    if guard.bash_refusal(command, cwd):
        return None
    roots, home, tops, places = project_roots(main), canonical_path(os.path.expanduser("~")), {}, {}
    for path in paths:
        loc = locate(path)
        rel = place(loc, roots, home, tops) if loc is not None else None
        if rel is None:
            return None
        places[loc] = rel
    entered = set().union(*map(lineage, moves))
    for loc in places:  # removing a folder a cd enters, or one above it, makes that cd fail
        try:
            st = os.lstat(loc)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in entered:
            return None
    folders = [loc for loc in places if os.path.isdir(loc) and not os.path.islink(loc)]
    pats, items, leave, total = backups.sensitive_patterns(main), [], [], 0
    for loc, rel in places.items():
        if not any(loc.startswith(folder + os.sep) for folder in folders):  # else judged with its folder
            total += judge(loc, rel, pats, items, leave)
    sid = None
    if items:
        cap = cap_mb(file_env)
        if total > cap * MB:
            return answer("ask", f"Not backed up: what this removes holds {size_text(total)} ({total:,} bytes) to "
                                 f"copy, over the {cap:g} MB backup cap (HARNESS_BACKUP_CAP_MB); approve it only if "
                                 "it need not be undone.")
        session = data.get("session_id")
        manifest = backups.create(items, main=main, cwd=cwd, command=command, leave_out=leave,
                                  session=session if isinstance(session, str) else None)
        sid, count = manifest["id"], len(manifest["items"])
    if not all(harmless(part, name) for part, name, _args, _here, _xargs in parts):
        return None
    if sid is None:
        return answer("allow", "Nothing to back up: what this removes is build output or not there.")
    return answer("allow", f"Backed up {count} path{'' if count == 1 else 's'} as {sid}; "
                           f"harness-work backup restore {sid} undoes it.")


def main() -> int:
    data = read_input()
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") != "Bash" or not isinstance(command, str) or not RM_WORD.search(command):
        return 0
    if data.get("permission_mode") == "plan":
        return 0
    timer = hasattr(signal, "setitimer")
    if timer:
        signal.signal(signal.SIGALRM, out_of_time)
        signal.setitimer(signal.ITIMER_REAL, max(DEADLINE - time.monotonic(), 0.01))
    try:
        found = decide(data, command)
    finally:
        if timer:
            signal.setitimer(signal.ITIMER_REAL, 0)
    if found:
        emit(found)
    return 0


if __name__ == "__main__":
    # fail open: a broken hook must never trap the agent
    sys.exit(run_fail_open("backup_gate.py", main))
