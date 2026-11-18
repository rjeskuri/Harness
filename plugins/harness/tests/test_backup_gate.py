"""Tests for plugins/harness/hooks/backup_gate.py (ticket #327, epic #322): back up, then allow, rm in the project.

The hook (PreToolUse, matcher Bash) snapshots what an rm, unlink or rmdir in the session's project would destroy,
with harness_lib/backups.py's create, and then answers allow; what it cannot back up gets ask (over the size cap)
or no decision at all (Claude Code's default). It never weakens branch_guard: a command branch_guard denies gets
no decision and no snapshot.

- No decision and no snapshot: plan mode; HARNESS_BACKUP=off (the environment, then the project's settings); a
  command branch_guard denies (HARNESS_ALLOW_MAIN honored); an unknown target ($, a backtick, xargs, an unknown
  directory, a glob matching nothing); a target outside the session's project (the main checkout of
  CLAUDE_PROJECT_DIR, else of the event's cwd, its worktrees included); the main checkout, a worktree root, .git
  or anything in it, $HOME, and anything under .claude/, .githooks/ or .github/ relative to its own working tree.
- Rebuildable (no copy): a path component named node_modules, .venv, venv, dist, build, __pycache__,
  .pytest_cache, .mypy_cache, .ruff_cache or .tox, unless the path is sensitive (HARNESS_SENSITIVE_PATHS, or a
  folder holding a path that matches) or secret-shaped (.env, .env.*, *.pem, *.key, credentials/ and under).
  Rebuildable folders inside a copied target are left out of the copy and of the size.
- Everything else is copied. Over HARNESS_BACKUP_CAP_MB (default 200): ask, no snapshot, the reason naming the
  size and the cap. Otherwise one snapshot, then allow when every other part is harmless (cd, pushd, popd, pwd,
  true, :, ls, echo, printf, test, [, with no redirect but to /dev/null or a descriptor), the reason naming the
  snapshot id; with any other part the snapshot stays and there is no decision. Nothing to copy (rebuildable or
  missing): allow on the same rule, no snapshot.
- A command without rm, unlink, rmdir or one of the git forms below prints nothing. Without the lib, or on its own
  error: one stderr line naming the hook, exit 0, no decision.
- hooks.json runs it on PreToolUse Bash after tests_first.py, with a 30 s timeout.

Git forms (ticket #328). A git part, in the repository it runs in (branch_guard's git_split after the walker's cd),
that destroys what git can keep is backed up as refs, in the same snapshot as any copied files: `reset --hard` and
`--merge` keep HEAD as `head` and `git stash create`'s commit (tracked and staged changes; none on a clean tree) as
`changes`; checkout of paths (`--`, `.`, a path) and `checkout -f`, `switch -f` or `--discard-changes`, `restore` of
the worktree (not --staged alone) and `git rm` (not --cached) keep `changes`; `branch -D`, `-d` or `--delete
[--force]` keeps each named branch's tip as `branch/<name>`; `stash drop [<stash>]` and `stash clear` keep each
dropped entry as `stash/<n>`. Each is a ref refs/harness-backup/<id>/<what>[/<name>] in that repository and an entry
{"ref", "sha", "what", "name", "worktree"} in the manifest's `refs`. `clean -f` (not -n) copies the paths `git clean
-n` lists with the same options, through the file rules above (rebuildable skipped, sensitive kept in the project,
the cap). `status`, `log`, `diff`, `show`, `rev-parse`, `stash list` and a `branch` listing join the harmless parts.
`harness-work backup restore <id>` never touches a working tree for a ref: a branch comes back at its sha (refused
when it exists), `changes` and `stash` go into the stash list (`git stash store`), `head` becomes the branch
harness-restore/<id>. A push is never backed up and never harmless. A git form branch_guard denies gets nothing.

Fixtures: a temporary HOME per test (and one for the whole process, so nothing here can reach the real ~/.claude),
real git repositories (`git init -b main`, one commit holding README.md, src/, docs/, docs/agent/, .gitignore,
.claude/settings.json, .claude/agents/, .githooks/ and .github/workflows/; each name, branch and settings built once
per module with git's background maintenance off and copied into each test) and linked worktrees made with
`git worktree add`, random names and contents. GIT_CEILING_DIRECTORIES keeps git from finding a repository above
the test's folder. Each test's first hook run is as hooks.json runs it: `python3 <hook>` with the event JSON on
stdin and CLAUDE_PROJECT_DIR in its environment, from a folder outside every fixture repository, exit 0 and nothing
on stderr; its later runs execute the same script as __main__ in this process, with the same stdin, environment,
cwd and argv, and are held to the same exit status and empty stderr. Its answer is read as Claude Code reads it
(stdout parsed as JSON, hookSpecificOutput.permissionDecision; nothing on stdout is no decision). A snapshot is any
new folder in the global store (<HOME>/.claude/harness/backups/) or in a fixture repository's project store
(<git common dir>/harness-backups/); the round trip removes the target itself, then runs `harness-work backup
restore <id>` (a test's first restore as its own process, later ones in this process as the hook's are, exit 0
either way) and compares every entry (bytes, modes, link targets) with what was there.
backup_gate is run alone: tests_first.py, which judges rm on a ticket branch as a source edit, is another hook.
Git state is read through git itself (`git for-each-ref refs/harness-backup/`, `git stash list`, `git rev-parse`,
`git show <sha>:<path>`), and the hook must leave a worktree's files, `git status`, index, HEAD, branches and stash
list as they were; a git round trip runs the real git command, then the restore, then `git stash apply` where the
restore stored an entry, and compares the files with what was there.

Choices the ticket leaves open, fixed here:
- Ruling 2 (Ryan, 2026-11-05, epic #322) supersedes the outline's "or `git check-ignore` reporting it" and the
  DoD's "rm of a git-ignored scratch file: allow, no snapshot": only a path component among the named build-output
  folders is rebuildable; any other git-ignored path (a scratch file, data/, .env) is copied under the cap.
- A word with a glob character (*, ? or [) is expanded from the directory its part runs in, as the shell does;
  each match is a target, and a glob matching nothing makes the command's targets unknown.
- A missing literal target is nothing to copy; an empty folder (rmdir) is an existing target and is copied.
- unlink and rmdir count as rm for branch_guard's main-branch check (orchestrator's ruling on #327): where
  branch_guard would deny rm of the same path from the same directory (a main checkout on main or master,
  HARNESS_ALLOW_MAIN unset), the command gets no decision and no snapshot, although branch_guard itself lets
  unlink and rmdir through; with HARNESS_ALLOW_MAIN=1, or for a git-ignored path, they are backed up and allowed.
- `.git` by name: a target named .git (a worktree's .git file too) or with a .git component gets nothing. The
  folders .claude, .githooks and .github themselves get nothing, like the paths under them.
- A command whose parts run no rm, unlink or rmdir (the word only as an argument: `echo rm -rf build`) gets no
  decision, even when every part is harmless.
- One snapshot per command, holding every copied target. The size is the apparent size (the sum of the regular
  files' st_size, links not followed, as the manifest's bytes), over all copied targets of the command; the sizes
  used are over or under the cap whether MB means 10^6 or 2^20 bytes. A reason names the size when it shows a
  number with a unit within 25% of the byte count (decimal or binary units), and the cap when it shows its number.
- HARNESS_BACKUP is read from the environment first, then `env` in the main checkout's .claude/settings.json as it
  is on disk (settings_env, as backups.sensitive_patterns, branch_guard and tests_first read their settings; only
  autoland reads the committed file); an environment value other than off (on) wins over a settings off. Every
  permission_mode but plan (default, auto, acceptEdits, bypassPermissions) gets the same answer.
- branch_guard's decision is its own, on the whole command: any part it refuses (a write into a main checkout on
  main, git commit there, a force push, a push to main) leaves the command with no decision and no snapshot, even
  when the rm itself is in a worktree; HARNESS_ALLOW_MAIN counts wherever branch_guard reads it (the environment,
  the repository's .claude/settings.json, then its .claude/settings.local.json).
- Every named folder (not sensitive, not secret-shaped) inside a copied target is left out of the copy and the
  size, at any depth; in a project with sensitive patterns a named folder holding no matching path stays
  rebuildable, and one holding a matching path (node_modules/pkg/x.db under *.db) is copied whole.
- The session's project is the main checkout of CLAUDE_PROJECT_DIR (a worktree's names its main checkout), else
  of the event's cwd; a worktree outside the main checkout's folder is inside the project.
- A store the snapshot cannot be written to is the hook's own error: one stderr line, exit 0, no decision.
- The directory a removal runs in (review finding on #327): an unconditional cd or pushd into an existing folder
  moves it, whatever separator follows. It is unknown, so no decision and no snapshot, after a cd that can fail (a
  missing folder, a regular file) when the removal runs anyway (after ;, || or a newline); after a cd to a word the
  shell expands and the walker cannot (~+, ~0, ~+/sub, a glob); and after a cd that may not run (after && or ||,
  inside if), whichever way its condition goes. A missing folder joined to the removal by && alone is left open.
  Each such case is also run by a real shell (zsh, else bash) in the same worktree, to show what it removes.
  Also unknown (review round 2): a cd behind `command` (zsh runs the external cd, bash the builtin) or spelled as
  a path (a child process in every shell), and a cd joined by && or || to an arithmetic command or to a part that
  only redirects input. A cd behind `builtin`, or bare after ; or a newline, still moves the directory.
  A line continuation alone on its line holds nothing (#357): a cd after `&& \`, `|| \` or `| \` followed by blank
  lines, lines of only spaces and tabs, a comment or another continuation is as conditional as one right after the
  operator (unknown, whichever way the condition goes, after a command, an arithmetic or a bare redirect); after
  `; \`, `& \` or a newline, or with a continuation inside its own line, a cd still moves the directory.

Choices #328 leaves open, fixed here:
- One snapshot per command holds every copy and every ref it keeps; a command that destroys only what refs keep has
  `items: []`. Over the cap: ask, and neither a snapshot nor a ref (`git reset --hard && git clean -fd` too).
  Nothing to keep (a path form on a clean tracked tree, a clean that lists only rebuildable paths or nothing):
  allow on the same rule, no snapshot.
- A `refs` entry has exactly the keys ref, sha, what, name, worktree. `name` is null for head and changes, the
  branch for branch, and for stash the entry's index n (stash@{n} when the hook ran; a string or a number);
  `ref` is refs/harness-backup/<id>/<what>, plus /<name> when there is one; `sha` is what git holds at that ref;
  `worktree` is the top level of the working tree git runs in (after cd and -C), whatever the cwd.
- `head` is HEAD before the command, not the reset's target, kept for reset alone and on a clean tree too.
  `changes` is kept for reset and the path forms only (never for clean, a branch delete or a stash drop): a commit
  whose first parent is HEAD, holding the worktree's tracked edits (a staged new file too, untracked files not) in
  its tree and the staged ones in its second parent's.
- `restore --staged` (-S) alone, `rm --cached`, `clean -n` (`--dry-run`, `-fdn`), and `switch` or `checkout` of a
  branch without -f keep nothing: with no other destructive part, no decision and no snapshot.
- Branch deletes: `-D`, `-d`, `--delete --force`, `-d -f`, several names in one part; in a main checkout on main a
  branch other than main is backed up and allowed (branch_guard refuses only main and master), its worktree then
  the main checkout. Stash drops: `drop`, `drop stash@{n}`, `drop -q 'stash@{n}'`, `drop n`; and `clear`.
- `git clean`: the items are exactly the paths `git clean -n` lists with the command's own options and pathspecs,
  from the folder git runs in (-d, -x, -X, -e, a pathspec, a subfolder cwd, -C): an untracked folder is one item,
  but one holding an ignored file lists its other files one by one; a listed rebuildable folder is left out, and so
  is a named folder inside a copied one (#327).
- The read-only parts are harmless with any options and no redirect: `git status`, `log`, `diff`, `show`,
  `rev-parse`, `stash list`, and `branch` alone or with -a, -v or --list [pattern] (with a name and no delete option
  it creates a branch: not harmless). A command of read-only parts alone gets no decision (nothing destroys).
  #327's `cd sub && rm -rf x && git status` therefore moves from no decision to allow.
- Restore of refs: every ref is checked before anything is written, as items are: a `branch` whose branch exists,
  or a `head` whose harness-restore/<id> exists, refuses the whole restore in one line naming it, exit 1, nothing
  created or stored. A stored entry's subject is `harness-backup <id>`, and the output names `git stash apply`.
- branch_guard: each form it denies in a main checkout on main (reset, clean, restore, checkout of paths, rm,
  branch -D main), there or through -C from a worktree, gets no decision and no ref, also when another part of the
  command runs in a worktree; with HARNESS_ALLOW_MAIN=1 the same forms there are backed up and allowed.
- Pushes: alone (force, +refspec, --delete, -d, :branch, or a plain push), no decision and no snapshot; after a
  backed-up part branch_guard lets through, the snapshot stays and there is no decision; with a force push
  branch_guard refuses the whole command, so nothing.
- Outside the project (the git counterpart of an rm target outside it): a git part whose repository is not the
  main checkout or one of its worktrees gets no decision, no snapshot and no ref in either repository, however git
  gets there (-C, a cd, the cwd), and also for a repository nested in the main checkout's or a worktree's folder.
  What counts is where git runs: from a cwd in an unrelated repository, `git -C <worktree>` is backed up. A `$`, a
  backtick or a substitution in the -C directory or in a branch name makes the target unknown: nothing.
- Plan mode and HARNESS_BACKUP=off (the environment, or `env` in the main checkout's .claude/settings.json) hold
  for the git forms as for rm: no decision and no ref; an environment `on` beats a settings `off`; every other
  permission_mode gets the allow.
- Ruling (review of PR #361, finding 1): `git stash create` holds no untracked or ignored file, so a command that
  overwrites one gets no decision and no snapshot or ref (the review's cheaper form; the colliding files are not
  copied through the file rules). That is `reset --hard <commit>`, `checkout -f <branch>`, `switch -f` or
  `--discard-changes <branch>`, `checkout <commit> -- <path>` and `restore --source=<commit> <path>`, given a
  branch name or a sha, when an untracked or ignored path of the worktree is a path the target tracks, lies under
  one (a folder's file where it tracks a file) or over one (a file where it tracks a folder). The colliding path is
  always inside the pathspec of the path forms, so whether they check the whole target tree is not pinned. Without
  such a path the same command is backed up and allowed as before, untracked and ignored files elsewhere included.
- Ruling (finding 1): a git clean that follows a reset, checkout -f or switch to another commit in the same worktree
  in one command gets no decision and no snapshot (its dry run reads the ignore rules the reset replaces); a clean
  before the reset, or after a reset with no target, is backed up as before.
- Ruling (fix round 2): a git clean after an earlier destructive part of the same command, in the same worktree,
  that rewrites a .gitignore gets no decision and no snapshot or ref. Any .gitignore counts (the root one, a
  nested one, one deleted from disk, a new one only staged) when what is on disk differs from what the form writes
  there: HEAD for a reset (with no target or HEAD), the index for checkout or restore of paths without a tree-ish,
  the tree-ish for `checkout HEAD -- p`; `restore --staged --worktree` writes HEAD's, so a change only staged
  counts for it. Pinned here: `reset --hard && clean -fd` with the root .gitignore edited on disk, and
  `restore --staged --worktree . && clean -fd` with it edited and staged, get nothing; `checkout -- src && clean -fd`
  (its pathspec leaves the edited .gitignore out), and both pairs when no .gitignore differs, are backed up as before.
- Ruling (fix round 2, low): the overwrite check reads the target in every spelling (`restore -s X`, `-WsX`,
  `--so X`, `checkout X p` without `--`, `switch --detach -f X`, `reset --merge X` among them). Pinned here:
  `reset --hard X`, `checkout -f X`, `checkout X -- p` and `restore --source=X p`.
- Ruling (finding 2): a branch name that is not a plain branch of refs/heads/ (`@{-1}`, `@{-2}`, quoted or not) or
  names none (a missing branch) gets no decision and no ref, for every branch of the part: `git branch -D <real>
  @{-1}` and `git branch -D <real> <missing>` keep nothing either (git deletes the real one before it fails).

Ticket #377 (epic #374) collapsed the cd cases, the fix-round clean cases and the outside-repository cases into
table tests, cut the git forms' spelling lists to one row per decision, and dropped what no real session types. The
hook still follows the rulings above; this module no longer pins a cd behind `command` or `builtin`, spelled as a
path, inside if, after an arithmetic or a bare redirect, or to ~+ or ~0; the line-continuation spellings but #357's
own; a repository unrelated to the project or nested in the main checkout, and a $, a backtick or a substitution in
-C or a branch name; a staged restore over what git stash create lacks; branch names that are not plain branches.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_backup_gate.py -v
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from gitrepo_support import GIT, copy_repo  # noqa: E402  (GIT: the git binary itself, not macOS's xcrun shim)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
HOOKS_DIR = os.path.join(PLUGIN, "hooks")
HOOK = os.path.join(HOOKS_DIR, "backup_gate.py")
GUARD = os.path.join(HOOKS_DIR, "branch_guard.py")
HOOKS_JSON = os.path.join(HOOKS_DIR, "hooks.json")
TOOL = os.path.join(PLUGIN, "bin", "harness-work")
PATH = os.pathsep.join([os.path.dirname(GIT), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
SHELL = shutil.which("zsh", path=PATH) or shutil.which("bash", path=PATH) or "/bin/bash"   # as the Bash tool runs
ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
REBUILDABLE = ("node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache",
               ".ruff_cache", ".tox")
GITIGNORE = (".claude/worktrees/\n.claude/settings.local.json\nnode_modules/\n*.tmp\ndata/\n.env\n.env.*\n*.pem\n"
             "*.key\ncredentials/\n")
FILE_MODES = (0o600, 0o640, 0o644, 0o664, 0o755)
REF_KEYS = {"ref", "sha", "what", "name", "worktree"}

# An internal error, as test_hookio.py makes one: every json.loads raises, then the hook runs as __main__.
CRASH_DRIVER = r'''
import json, os, runpy, sys
token = os.environ["BACKUP_GATE_TEST_TOKEN"]

def loads(*args, **kwargs):
    raise RuntimeError(token)

json.loads = loads
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
'''

_SAVED = {}
_TEMPLATES = {}     # (name, branch, settings) -> a project repository built once per module, copied by each test
_CODE = {}          # a hook's path -> its compiled code, for the runs in this process


def hook_code(path):
    """The hook script at `path` compiled once per process, without this module's __future__ flags."""
    if path not in _CODE:
        with open(path, encoding="utf-8") as fh:
            _CODE[path] = compile(fh.read(), path, "exec", dont_inherit=True)
    return _CODE[path]


def setUpModule():
    """One umask and a module HOME for the whole process: nothing below can reach the real ~/.claude."""
    _SAVED["umask"] = os.umask(0o022)
    _SAVED["home"] = os.environ.get("HOME")
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-gate-module-"))
    _SAVED["root"] = root
    os.makedirs(os.path.join(root, "home"))
    os.environ["HOME"] = os.path.join(root, "home")


def tearDownModule():
    os.umask(_SAVED.get("umask", 0o022))
    if _SAVED.get("home") is not None:
        os.environ["HOME"] = _SAVED["home"]
    shutil.rmtree(_SAVED.get("root", ""), True)


# ================================================================ helpers
def tree(path):
    """Every entry at and under `path`, links never followed: relative path -> ("dir", mode),
    ("file", sha256, size, mode) or ("link", target)."""
    out = {}

    def walk(p, rel):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            out[rel] = ("link", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            for name in sorted(os.listdir(p)):
                walk(os.path.join(p, name), name if rel == "." else rel + "/" + name)
        else:
            with open(p, "rb") as fh:
                data = fh.read()
            out[rel] = ("file", hashlib.sha256(data).hexdigest(), len(data), stat.S_IMODE(st.st_mode))

    walk(path, ".")
    return out


def holds(root, data):
    """Whether any regular file at or under `root` (links not followed) contains `data`."""
    if not os.path.lexists(root):
        return False
    if os.path.isfile(root) and not os.path.islink(root):
        with open(root, "rb") as fh:
            return data in fh.read()
    for folder, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            p = os.path.join(folder, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                if data in fh.read():
                    return True
    return False


def real(path):
    return os.path.realpath(str(path))


def entries(root):
    """Every path under `root`, relative to it, links not followed."""
    out = set()
    for folder, dirs, files in os.walk(root):
        out.update(os.path.relpath(os.path.join(folder, n), root) for n in dirs + files)
    return out


def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


# Sizes in a reason: a number, thousands separators allowed, and its unit.
AMOUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(bytes?\b|[kmg]i?b\b|[kmg]\b|b\b)?", re.I)
SCALES = {"b": (1,), "byte": (1,), "bytes": (1,), "kb": (1e3, 2 ** 10), "kib": (2 ** 10,), "k": (1e3, 2 ** 10),
          "mb": (1e6, 2 ** 20), "mib": (2 ** 20,), "m": (1e6, 2 ** 20), "gb": (1e9, 2 ** 30), "gib": (2 ** 30,),
          "g": (1e9, 2 ** 30)}


def amounts(text):
    return [(float(num.replace(",", "")), (unit or "").lower()) for num, unit in AMOUNT.findall(text)]


def names_size(text, size):
    """Does `text` show `size` bytes: a number with a unit within 25% of it, or the exact byte count?"""
    for value, unit in amounts(text):
        if not unit and value == size:
            return True
        if any(0.75 * size <= value * scale <= 1.25 * size for scale in SCALES.get(unit, ())):
            return True
    return False


def names_cap(text, cap_mb):
    """Does `text` show a cap of `cap_mb` MB: the number itself (bare or in MB), or its bytes in some unit?"""
    for value, unit in amounts(text):
        if unit in ("", "mb", "mib", "m") and value == cap_mb:
            return True
        for scale in SCALES.get(unit, ()):
            if any(abs(value * scale - cap_mb * mb) <= 0.01 * cap_mb * mb for mb in (1e6, 2 ** 20)):
                return True
    return False


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mk("home")
        self.neutral = self.mk("neutral")       # the hook process's own cwd: in no repository
        seed = int.from_bytes(os.urandom(8), "big")
        self.rng = random.Random(seed)
        self.seed = seed
        self.mains = []
        self.ran_a_process = False          # the test's first hook run is a real process, the rest run in process
        self.ran_a_restore = False          # and so is its first harness-work backup restore

    # ----------------------------------------------------------- files and repositories
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def word(self):
        return "%08x" % self.rng.getrandbits(32)

    def put(self, path, size=None, mode=0o644, data=None):
        """A file of `data` (else `size` random bytes, else 1 to 5000) with `mode`; returns its bytes."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if data is None:
            data = os.urandom(self.rng.randint(1, 5000) if size is None else size)
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, mode)
        return data

    def fill(self, folder, files=3, total=None):
        """`folder` holding `files` random files at random depths and modes and one empty subfolder; with `total`,
        the files' sizes add up to it. Returns the folder."""
        os.makedirs(folder, exist_ok=True)
        sizes = [None] * files
        if total is not None:
            sizes = [total // files] * files
            sizes[0] += total - sum(sizes)
        for k, size in enumerate(sizes):
            parts = [self.word() for _ in range(self.rng.randint(0, 2))]
            self.put(os.path.join(folder, *parts, f"f{k}-{self.word()}.bin"), size=size,
                     mode=self.rng.choice(FILE_MODES))
        os.makedirs(os.path.join(folder, "empty-" + self.word()), exist_ok=True)
        return folder

    def env(self, **extra):
        env = {"HOME": self.home, "PATH": PATH, "TMPDIR": self.mk("tmpdir"), "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
               "GIT_CEILING_DIRECTORIES": self.tmp, "GIT_AUTHOR_NAME": "Test",
               "GIT_AUTHOR_EMAIL": "test@test.invalid", "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "test@test.invalid"}
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def git(self, cwd, *args):
        p = subprocess.run([GIT, "-C", cwd, "-c", "commit.gpgsign=false"] + list(args), env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def make_project(self, name="proj", branch="main", settings=None):
        """A repository with one commit on main, then on `branch`; `settings` is the `env` of its tracked
        .claude/settings.json. Each (name, branch, settings) is built once per module (build_project) and copied
        here, so two projects of one test still differ in their files and commits."""
        key = (name, branch, json.dumps(settings or {}, sort_keys=True))
        if key not in _TEMPLATES:
            template = os.path.join(_SAVED["root"], "templates", str(len(_TEMPLATES)))
            self.build_project(template, name, branch, settings)
            _TEMPLATES[key] = template
        main = os.path.join(self.tmp, name)
        copy_repo(_TEMPLATES[key], main)
        self.mains.append(main)
        return main

    def build_project(self, main, name, branch, settings):
        """make_project's repository built at `main`, git's background maintenance and auto gc off for every call
        (a copy must never catch one mid-run, #365)."""
        os.makedirs(main)
        quiet = ("-c", "maintenance.auto=false", "-c", "gc.auto=0")
        self.git(main, *quiet, "init", "-q", "-b", "main")
        files = {
            "README.md": f"# {name} {self.word()}\n",
            "src/app.py": f"def login():\n    return {self.word()!r}\n",
            "src/util.py": f"VALUE = {self.word()!r}\n",
            "docs/a.md": f"a {self.word()}\n",
            "docs/b.md": f"b {self.word()}\n",
            "docs/agent/old.md": f"note {self.word()}\n",
            ".gitignore": GITIGNORE,
            ".claude/settings.json": json.dumps({"env": settings or {}}) + "\n",
            ".claude/agents/helper.md": f"helper {self.word()}\n",
            ".githooks/pre-commit": "#!/bin/sh\nexit 0\n",
            ".github/workflows/ci.yml": "on: pull_request\n",
        }
        for rel, text in files.items():
            self.put(os.path.join(main, rel), data=text.encode(), mode=0o755 if rel.startswith(".githooks") else 0o644)
        self.git(main, *quiet, "add", "-A")
        self.git(main, *quiet, "commit", "-q", "-m", "First")
        if branch != "main":
            self.git(main, *quiet, "switch", "-q", "-c", branch)

    def commit_settings(self, main, settings):
        self.put(os.path.join(main, ".claude", "settings.json"), data=(json.dumps({"env": settings}) + "\n").encode())
        self.git(main, "add", ".claude/settings.json")
        self.git(main, "commit", "-q", "-m", "Settings")

    def add_worktree(self, main, name, branch, path=None):
        path = path or os.path.join(main, ".claude", "worktrees", name)
        self.git(main, "worktree", "add", "-q", "-b", branch, path)
        return real(path)

    def ignored(self, repo, rel):
        p = subprocess.run([GIT, "-C", repo, "check-ignore", "-q", "--", rel], env=self.env(),
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode == 0

    def tracked(self, repo, rel):
        p = subprocess.run([GIT, "-C", repo, "ls-files", "--error-unmatch", "--", rel], env=self.env(),
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode == 0

    # ----------------------------------------------------------- the hook
    def event(self, command, cwd, mode="auto"):
        return {"session_id": "s1", "transcript_path": "/dev/null", "cwd": cwd, "permission_mode": mode,
                "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                "tool_use_id": "toolu_01"}

    def run_raw(self, stdin, project, env=None, hook=None, argv=None):
        """The hook run as hooks.json runs it: `python3 <hook>`, `stdin` on its stdin, CLAUDE_PROJECT_DIR set to
        `project` (None: unset), from a folder in no repository."""
        return subprocess.run(argv or [sys.executable, hook or HOOK], input=stdin, capture_output=True, text=True,
                              env=self.env(CLAUDE_PROJECT_DIR=project, **(env or {})), cwd=self.neutral,
                              timeout=90)

    def in_process(self, stdin, project, env=None, hook=None, args=(), cwd=None):
        """run_raw's run in this process: the script's code (default the hook) run as __main__ with `stdin` on its
        stdin, os.environ replaced by the same environment, the same cwd (default the neutral folder) and argv
        (the script, then `args`); harness_lib stays imported between runs. A CompletedProcess of its exit status
        and what it wrote."""
        path = hook or HOOK
        out, err = io.StringIO(), io.StringIO()
        status, here, alarm = 0, os.getcwd(), signal.getsignal(signal.SIGALRM)
        stdin_file = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, self.env(CLAUDE_PROJECT_DIR=project, **(env or {})),
                                                    clear=True))
                stack.enter_context(mock.patch.object(sys, "stdin", stdin_file))
                stack.enter_context(mock.patch.object(sys, "argv", [path, *args]))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                os.chdir(cwd or self.neutral)
                try:
                    exec(hook_code(path), {"__name__": "__main__", "__file__": path})
                except SystemExit as exc:
                    if exc.code is None or isinstance(exc.code, int):
                        status = exc.code or 0
                    else:                                           # sys.exit("text"): the text on stderr, 1
                        err.write(f"{exc.code}\n")
                        status = 1
        finally:
            os.chdir(here)
            if hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, alarm)
        return subprocess.CompletedProcess([sys.executable, path, *args], status, out.getvalue(), err.getvalue())

    def hook_run(self, stdin, project, env=None):
        """backup_gate.py on `stdin`: the test's first run as hooks.json runs it (run_raw, a real process: its exit
        status and stderr are the process's own), every later one in this process (in_process)."""
        if self.ran_a_process:
            return self.in_process(stdin, project, env)
        self.ran_a_process = True
        return self.run_raw(stdin, project, env)

    def run_gate(self, command, cwd, project, mode="auto", env=None):
        """The hook's run on a Bash `command` in `cwd`; exit 0 and nothing on stderr."""
        p = self.hook_run(json.dumps(self.event(command, cwd, mode)), project, env)
        last = p.stderr.strip().splitlines()[-1:] or [""]
        self.assertEqual((p.returncode, p.stderr), (0, ""),
                         f"backup_gate.py on {command!r} exited {p.returncode}: {last[0]} (seed {self.seed})")
        return p

    def answer(self, p):
        """(decision, reason) of the hook's stdout; (None, None) when it printed nothing."""
        if not p.stdout.strip():
            return None, None
        out = json.loads(p.stdout)
        self.assertEqual(list(out), ["hookSpecificOutput"], p.stdout)
        d = out["hookSpecificOutput"]
        self.assertEqual(d.get("hookEventName"), "PreToolUse", p.stdout)
        self.assertIn(d.get("permissionDecision"), ("allow", "ask"), p.stdout)
        reason = d.get("permissionDecisionReason")
        self.assertIsInstance(reason, str, p.stdout)
        self.assertTrue(reason.strip(), p.stdout)
        return d["permissionDecision"], reason

    # ----------------------------------------------------------- the stores
    def global_store(self, home=None):
        return os.path.join(home or self.home, ".claude", "harness", "backups")

    def project_store(self, main):
        return os.path.join(main, ".git", "harness-backups")

    def snapshots(self):
        """Every folder in the global store and in each fixture repository's project store."""
        found = set()
        for store in [self.global_store()] + [self.project_store(m) for m in self.mains]:
            if os.path.isdir(store):
                found.update(os.path.join(store, n) for n in os.listdir(store)
                             if os.path.isdir(os.path.join(store, n)))
        return found

    def snapshot_made(self, before, store, paths, command):
        """The id of the one snapshot made since `before`, in `store`; its items are `paths` (unless None)."""
        new = self.snapshots() - before
        self.assertEqual(len(new), 1, f"{command!r}: snapshots made {sorted(new)} (seed {self.seed})")
        folder = new.pop()
        self.assertEqual(os.path.dirname(folder), store, f"{command!r}: snapshot in the wrong store")
        sid = os.path.basename(folder)
        self.assertRegex(sid, r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        if paths is not None:
            self.assertEqual({real(i["path"]) for i in manifest["items"]}, {real(p) for p in paths},
                             f"{command!r}: what the snapshot holds (seed {self.seed})")
        return sid

    def expect_backup(self, p, before, store, paths, command):
        """Allow, one new snapshot in `store` holding `paths`, its id in the reason; returns the id."""
        decision, reason = self.answer(p)
        self.assertEqual(decision, "allow", f"{command!r}: {p.stdout!r} (seed {self.seed})")
        sid = self.snapshot_made(before, store, paths, command)
        self.assertEqual(set(ID_RE.findall(reason)), {sid}, f"{command!r}: the reason names another id: {reason}")
        return sid

    def expect_snapshot_only(self, p, before, store, paths, command):
        """No decision, but one new snapshot in `store` holding `paths`; returns its id."""
        self.assertEqual(p.stdout.strip(), "", f"{command!r}: expected no decision (seed {self.seed})")
        return self.snapshot_made(before, store, paths, command)

    def expect_allow_without_copy(self, p, before, command):
        decision, _reason = self.answer(p)
        self.assertEqual(decision, "allow", f"{command!r}: {p.stdout!r} (seed {self.seed})")
        self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made (seed {self.seed})")

    def expect_nothing(self, p, before, command):
        self.assertEqual(p.stdout.strip(), "", f"{command!r}: expected no decision (seed {self.seed})")
        self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made (seed {self.seed})")

    def restore(self, sid, cwd):
        """`harness-work backup restore <sid>` from `cwd`, exit 0: the test's first restore as its own process, every
        later one in this process (in_process), with the same environment, cwd and argv."""
        if self.ran_a_restore:
            p = self.in_process("", None, hook=TOOL, args=("backup", "restore", sid), cwd=cwd)
        else:
            self.ran_a_restore = True
            p = subprocess.run([sys.executable, TOOL, "backup", "restore", sid], cwd=cwd, env=self.env(),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(p.returncode, 0, f"harness-work backup restore {sid}: {p.stdout}{p.stderr}")
        return p

    def round_trip(self, sid, targets, was, cwd):
        """Remove `targets` as the command would, restore `sid` and compare with `was`."""
        for target in targets:
            remove(target)
            self.assertFalse(os.path.lexists(target))
        self.restore(sid, cwd)
        self.assertEqual({t: tree(t) for t in targets}, was, f"restored from {sid} (seed {self.seed})")


# ================================================================ backed up and allowed
class BackedUp(GateTest):
    def test_rm_of_an_untracked_dir_is_backed_up_allowed_and_restorable(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        sub = os.path.join(wt, "sub")
        a = self.fill(os.path.join(wt, "build-cache-" + self.word()))
        b = self.fill(os.path.join(sub, "out-" + self.word()), files=4)
        c1 = self.fill(os.path.join(wt, "gen-" + self.word()), files=2)
        c2 = self.fill(os.path.join(wt, "tmpdata-" + self.word()))
        stem = "c" + self.word()
        g1, g2 = (self.fill(os.path.join(wt, f"{stem}-{k}"), files=2) for k in "ab")
        empty = os.path.join(wt, "empty-" + self.word())
        os.mkdir(empty)
        os.chmod(empty, 0o750)
        cases = (
            (f"rm -rf {os.path.basename(a)}", wt, main, [a]),
            (f"rm -r -f ./{os.path.basename(b)}/", sub, wt, [b]),                     # project: a worktree
            (f"/bin/rm -Rf -- {c1} {os.path.basename(c2)}", wt, main, [c1, c2]),
            (f"rm -rf {stem}-*", wt, main, [g1, g2]),                                  # a glob, expanded
            (f"rmdir {os.path.basename(empty)}", wt, main, [empty]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command):
                self.assertFalse(any(self.ignored(wt, os.path.relpath(t, wt)) for t in targets))
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_rm_of_a_tracked_file_on_a_ticket_branch_is_backed_up(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        other = self.make_project("proj2", branch="t/2-y")      # a main checkout on a ticket branch
        cases = (
            ("rm src/app.py", wt, main, [os.path.join(wt, "src", "app.py")]),
            ("rm -f a.md ../src/util.py", os.path.join(wt, "docs"), wt,
             [os.path.join(wt, "docs", "a.md"), os.path.join(wt, "src", "util.py")]),
            ("unlink docs/b.md", wt, main, [os.path.join(wt, "docs", "b.md")]),
            ("rm -r src", wt, main, [os.path.join(wt, "src")]),
            ("rm README.md", other, other, [os.path.join(other, "README.md")]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command, cwd=cwd):
                for t in targets:
                    top = other if t.startswith(other + os.sep) else wt
                    self.assertTrue(self.tracked(top, os.path.relpath(t, top)), t)
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_rebuildable_targets_are_allowed_without_a_copy(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.global_store()
        nm_marker = os.urandom(48)
        self.fill(os.path.join(wt, "node_modules"))
        self.put(os.path.join(wt, "node_modules", "left-pad", "marker.bin"), data=nm_marker)
        self.fill(os.path.join(wt, "dist"))                                     # not git-ignored: the name counts
        self.assertFalse(self.ignored(wt, "dist"))
        nothing_to_copy = ["rm -rf node_modules dist"]
        for k, name in enumerate(REBUILDABLE):
            rel = name if k % 2 == 0 else f"lib/{self.word()}/{name}"
            self.fill(os.path.join(wt, rel), files=2)
            nothing_to_copy.append(f"rm -rf {rel}")
        self.put(os.path.join(wt, "pkg", "__pycache__", "mod.cpython-39.pyc"))
        self.fill(os.path.join(wt, "web", "node_modules", ".cache"), files=2)
        nothing_to_copy += [
            "rm -f pkg/__pycache__/mod.cpython-39.pyc",                         # a file under a named folder
            "rm -rf web/node_modules/.cache",
            f"rm -f missing-{self.word()}.txt",                                 # missing: nothing to copy
            f"rm -rf node_modules missing-{self.word()}",
        ]
        for command in nothing_to_copy:
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_allow_without_copy(self.run_gate(command, wt, main), before, command)

        # Ruling 2: a git-ignored path that is not under a named folder is copied.
        scratch = os.path.join(wt, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        self.assertTrue(self.ignored(wt, os.path.basename(scratch)))
        # Names that only contain a rebuildable name are copied.
        lookalikes = [self.fill(os.path.join(wt, n), files=1)
                      for n in ("distro", "builds", "prebuild", "node_modules.bak", "venv2", "my_dist")]
        notes = self.fill(os.path.join(wt, "notes-" + self.word()))
        copied = (
            (f"rm -f {os.path.basename(scratch)}", [scratch], [scratch]),
            ("rm -rf " + " ".join(os.path.basename(p) for p in lookalikes), lookalikes, lookalikes),
            (f"rm -rf node_modules {os.path.basename(notes)}", [notes], [notes]),     # only notes is copied
        )
        for command, targets, held in copied:
            with self.subTest(command=command):
                before, was = self.snapshots(), {t: tree(t) for t in held}
                sid = self.expect_backup(self.run_gate(command, wt, main), before, store, targets, command)
                self.assertFalse(holds(store, nm_marker), f"{command!r}: node_modules went into the snapshot")
                self.round_trip(sid, held, was, wt)

        # Every named folder inside a copied target is left out of the copy and of the size: 2.5 MB of it
        # under a 1 MB cap is no reason to ask. Each sits at the top of the target or one folder down.
        for name in REBUILDABLE:
            app = os.path.join(wt, "app-" + self.word())
            self.put(os.path.join(app, "index.js"))
            self.put(os.path.join(app, "src", "view.js"), mode=0o640)
            self.put(os.path.join(app, "sub", "keep.js"))
            inner = self.rng.choice([name, "sub/" + name])
            big = self.put(os.path.join(app, inner, "pkg", "big.bin"), size=2_500_000)
            command = f"rm -rf {os.path.basename(app)}"
            with self.subTest(command=command, inner=inner, seed=self.seed):
                before = self.snapshots()
                want = {k: v for k, v in tree(app).items() if k != inner and not k.startswith(inner + "/")}
                p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": "1"})
                sid = self.expect_backup(p, before, store, None, command)
                self.assertFalse(holds(os.path.join(store, sid), big[:4096]), f"{inner} went into the snapshot")
                remove(app)
                self.restore(sid, wt)
                self.assertEqual(tree(app), want, f"restored from {sid} (seed {self.seed})")

    def test_sensitive_path_snapshot_stays_in_the_project(self):
        main = self.make_project("vault", settings={"HARNESS_SENSITIVE_PATHS": "data/**, *.db"})
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.project_store(main)
        wt_data = self.fill(os.path.join(wt, "data"), files=4)
        main_data = self.fill(os.path.join(main, "data"))
        self.assertTrue(self.ignored(wt, "data") and self.ignored(main, "data"))
        data_build = self.fill(os.path.join(wt, "data", "build"), files=2)       # sensitive beats rebuildable
        dist = self.fill(os.path.join(wt, "dist"), files=2)
        self.put(os.path.join(dist, "cache.db"))                                 # dist holds a sensitive path
        notes = self.fill(os.path.join(wt, "notes-" + self.word()), files=2)    # matches nothing: same store
        cases = (
            ("rm -rf data/build", wt, wt, [data_build]),
            ("rm -rf data", wt, main, [wt_data]),
            ("rm -rf data", main, main, [main_data]),                           # on main: git-ignored, so allowed
            ("rm -rf dist", wt, main, [dist]),
            (f"rm -rf {os.path.basename(notes)}", wt, main, [notes]),
        )
        for command, cwd, project, targets in cases:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, store, targets, command)
                self.round_trip(sid, targets, was, cwd)
        # Named folders holding nothing that matches a pattern stay rebuildable in a project with patterns.
        plain = ["build", ".venv", f"lib/{self.word()}/__pycache__", "site/dist", "node_modules"]
        for rel in plain:
            self.fill(os.path.join(wt, rel), files=2)
        for rel in plain:
            command = f"rm -rf {rel}"
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_allow_without_copy(self.run_gate(command, wt, main), before, command)
        modules = os.path.join(wt, "node_modules")
        self.put(os.path.join(modules, "pkg", "x.db"))                         # now it holds a sensitive path
        with self.subTest(command="rm -rf node_modules", holds="x.db"):
            before, was = self.snapshots(), {modules: tree(modules)}
            p = self.run_gate("rm -rf node_modules", wt, main)
            sid = self.expect_backup(p, before, store, [modules], "rm -rf node_modules")
            self.round_trip(sid, [modules], was, wt)
        glob = self.global_store()
        self.assertFalse(os.path.lexists(glob) and not os.path.isdir(glob), glob)
        self.assertEqual(os.listdir(glob) if os.path.isdir(glob) else [], [], f"written under {glob}")

    def test_secret_shaped_ignored_file_is_copied(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.global_store()

        def at(rel, mode=0o600):
            path = os.path.join(wt, rel)
            self.put(path, mode=mode)
            return path

        env, env_local, env_prod = at(".env"), at(".env.local"), at("config/.env.production")
        pem, key = at("certs/server.pem"), at("keys/deploy.key")
        creds = self.fill(os.path.join(wt, "credentials"), files=2)
        token = at("credentials/token.json")
        build_env, nm_pem = at("build/.env"), at("node_modules/pkg/client.pem")
        dist_creds = self.fill(os.path.join(wt, "dist", "credentials"), files=1)
        for rel in (".env", ".env.local", "config/.env.production", "certs/server.pem", "keys/deploy.key",
                    "credentials", "build/.env", "node_modules/pkg/client.pem"):
            self.assertTrue(self.ignored(wt, rel), rel)
        cases = (
            ("rm .env", [env]),
            ("rm -f .env.local config/.env.production", [env_local, env_prod]),
            ("rm -f certs/server.pem keys/deploy.key", [pem, key]),
            ("rm -f credentials/token.json", [token]),
            ("rm -rf credentials", [creds]),
            ("rm -f build/.env", [build_env]),                                   # secret beats rebuildable
            ("rm -f node_modules/pkg/client.pem", [nm_pem]),
            ("rm -rf dist/credentials", [dist_creds]),
        )
        for command, targets in cases:
            with self.subTest(command=command):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                sid = self.expect_backup(self.run_gate(command, wt, main), before, store, targets, command)
                self.round_trip(sid, targets, was, wt)
        at("build/notes.txt")
        at("node_modules/pkg/index.js")
        for command in ("rm -f build/notes.txt", "rm -f node_modules/pkg/index.js"):
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_allow_without_copy(self.run_gate(command, wt, main), before, command)


# ================================================================ the cap
class Cap(GateTest):
    def test_over_the_cap_asks_without_a_snapshot(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        r = self.rng.randint

        def folder(prefix, total):
            return self.fill(os.path.join(wt, f"{prefix}-{self.word()}"), files=3, total=total)

        big1 = folder("big", r(2_300_000, 2_900_000))
        big3 = folder("huge", r(6_600_000, 8_200_000))
        data = self.fill(os.path.join(wt, "data"), files=3, total=r(1_600_000, 2_000_000))   # git-ignored
        self.assertTrue(self.ignored(wt, "data"))
        pair = [folder("half", r(580_000, 650_000)) for _ in range(2)]              # each under, both over
        sparse = os.path.join(wt, "disk-" + self.word() + ".img")
        with open(sparse, "wb") as fh:
            fh.truncate(215_000_000)                                                  # over 200 MB and 200 MiB

        def size(paths):
            """The regular files' st_size at and under `paths`, links not followed (never read: one is sparse)."""
            total = 0
            for p in paths:
                if not os.path.isdir(p):
                    total += os.lstat(p).st_size
                    continue
                for folder, _dirs, files in os.walk(p):
                    total += sum(os.lstat(os.path.join(folder, n)).st_size for n in files
                                 if stat.S_ISREG(os.lstat(os.path.join(folder, n)).st_mode))
            return total

        asks = (
            ("1", [big1]),
            ("3", [big3]),
            ("1", [data]),
            ("1", pair),
            (None, [sparse]),                                                         # the default cap
        )
        for cap, targets in asks:
            command = "rm -rf " + " ".join(os.path.basename(t) for t in targets)
            with self.subTest(command=command, cap=cap):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": cap})
                decision, reason = self.answer(p)
                self.assertEqual(decision, "ask", f"{command!r} cap {cap}: {p.stdout!r}")
                self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made")
                total = size(targets)
                self.assertTrue(names_size(reason, total), f"the reason does not name {total} bytes: {reason}")
                self.assertTrue(names_cap(reason, int(cap or 200)), f"the reason does not name the cap: {reason}")
        remove(sparse)

        under = folder("mid", r(1_800_000, 2_200_000))
        nm = self.fill(os.path.join(wt, "node_modules"), files=3, total=r(2_300_000, 2_900_000))
        plain = folder("plain", r(2_300_000, 2_900_000))
        for cap, target, copied in (("3", under, True), ("1", nm, False), (None, plain, True)):
            command = f"rm -rf {os.path.basename(target)}"
            with self.subTest(command=command, cap=cap):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, env={"HARNESS_BACKUP_CAP_MB": cap})
                if copied:
                    self.expect_backup(p, before, self.global_store(), [target], command)
                else:
                    self.expect_allow_without_copy(p, before, command)


# ================================================================ the other parts of a command
class Parts(GateTest):
    def test_harmless_parts_allow_other_parts_give_no_decision(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        os.makedirs(os.path.join(wt, "sub"))
        allowed = (
            ("cd sub && rm -rf {d}", "sub"),
            ("pushd sub && rm -rf {d} && popd", "sub"),
            ("(cd sub && rm -rf {d}); pwd", "sub"),
            ("pwd; rm -rf {d}; ls -la", ""),
            ("echo removing && rm -rf {d} && echo done", ""),
            ("test -d {d} && rm -rf {d}", ""),
            ("[ -d {d} ] && rm -rf {d}", ""),
            ("rm -rf {d} || true", ""),
            (": ; rm -rf {d}", ""),
            ("printf 'clean\\n'; rm -rf {d} 2>/dev/null", ""),
            ("rm -rf {d} >/dev/null 2>&1; echo ok 2>&1", ""),
            ("ls {d} > /dev/null && rm -rf {d}", ""),
            ("cd sub && rm -rf {d} && git status", "sub"),          # a read-only git part is harmless (#328)
        )
        undecided = (
            ("rm -rf {d} && python3 build.py", ""),
            ("rm -rf {d}; make", ""),
            ("rm -rf {d} && npm install", ""),
            ("echo hi > log-{w}.txt; rm -rf {d}", ""),
            ("ls > files-{w}.txt && rm -rf {d}", ""),
            ("cat {d}/keep.bin && rm -rf {d}", ""),
            ("cd sub && rm -rf {d} && git add -A", "sub"),
            ("rm -rf {d} && echo built >> out-{w}.log", ""),
        )
        store = self.global_store()
        for k, (shape, where) in enumerate(allowed + undecided):
            name = "x-" + self.word()
            target = self.fill(os.path.join(wt, where, name), files=2)
            self.put(os.path.join(target, "keep.bin"))
            command = shape.format(d=name, w=self.word())
            with self.subTest(command=command):
                before, was = self.snapshots(), {target: tree(target)}
                p = self.run_gate(command, wt, main)
                if k < len(allowed):
                    sid = self.expect_backup(p, before, store, [target], command)
                else:
                    sid = self.expect_snapshot_only(p, before, store, [target], command)
                if k in (0, len(allowed)):
                    self.round_trip(sid, [target], was, wt)


# ================================================================ what gets nothing
class Nothing(GateTest):
    def test_unknown_or_outside_targets_get_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        victim = self.fill(os.path.join(wt, "victim-" + self.word()))
        v = os.path.basename(victim)
        self.put(os.path.join(wt, "list.txt"), data=(v + "\n").encode())
        outside = self.fill(self.mk("outside", "dir-" + self.word()))
        in_home = self.fill(os.path.join(self.home, "scratch-" + self.word()))
        other = self.make_project("other", branch="dev")
        stuff = self.fill(os.path.join(other, "stuff-" + self.word()))
        # A $ in a target makes it unknown, whatever the hook's own environment holds: these name the real target.
        hook_env = {"DIR": victim, "TARGET": victim, "VICTIM": v}
        nothing = (
            ("rm -rf $DIR", wt, main),
            ('rm -rf "$TARGET"', wt, main),
            ("rm -rf ${VICTIM}", wt, main),
            (f"rm -rf $(echo {v})", wt, main),
            (f"rm -rf `echo {v}`", wt, main),
            (f"printf '%s\\n' {v} | xargs rm -rf", wt, main),
            ("xargs rm -rf < list.txt", wt, main),
            (f"ls -d {v} | xargs -0 rm -r", wt, main),
            (f"cd - && rm -rf {v}", wt, main),
            (f"cd $D && rm -rf {v}", wt, main),
            (f"rm -rf nomatch-{self.word()}-*", wt, main),
            (f"rm -rf {v} $DIR", wt, main),
            (f"find . -name {v} -exec rm -rf {{}} +", wt, main),
            (f"rm -rf {outside}", wt, main),
            (f"rm -rf {os.path.relpath(outside, wt)}", wt, main),
            (f"rm -rf ~/{os.path.basename(in_home)}", wt, main),
            (f"rm -rf {stuff}", wt, main),
            (f"rm -rf {os.path.basename(stuff)}", other, main),               # another repository's cwd
            (f"rm -rf {v} {outside}", wt, main),
        )
        for command, cwd, project in nothing:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, project, env=hook_env), before, command)

        stuff2 = self.fill(os.path.join(other, "stuff-" + self.word()))
        victim2 = self.fill(os.path.join(wt, "victim-" + self.word()))
        scratch = os.path.join(main, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        backed = (
            (f"rm -rf {os.path.basename(stuff2)}", other, None, [stuff2]),     # no CLAUDE_PROJECT_DIR: the cwd's
            (f"rm -rf {victim2}", os.path.dirname(outside), main, [victim2]),  # cwd in no repo, target inside
            (f"rm -f {scratch}", wt, wt, [scratch]),                           # the worktree's main checkout
        )
        for command, cwd, project, targets in backed:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project, env=hook_env)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, os.path.dirname(targets[0]))

    def test_roots_git_dirs_and_guard_files_get_nothing(self):
        main = self.make_project(branch="t/2-y")       # off main: branch_guard refuses nothing in this repository
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        wt2 = self.add_worktree(main, "t-3-z", "t/3-z", path=os.path.join(self.tmp, "proj-wt2"))
        self.fill(os.path.join(wt, ".githooks", "extra"), files=1)
        plain = self.fill(os.path.join(wt, "plain-" + self.word()), files=1)
        homedir = self.fill(os.path.join(main, "homedir-" + self.word()), files=1)
        nothing = (
            ("rm -rf .", main, main, None),
            (f"rm -rf {main}/", wt, main, None),
            ("rm -rf ../../..", wt, main, None),
            ("rm -rf .claude/worktrees/t-1-x", main, main, None),
            ("cd .. && rm -rf t-1-x", wt, main, None),
            (f"rm -rf {wt2}", main, main, None),
            ("rm -rf .", wt2, wt2, None),
            ("rm -rf .git", main, main, None),
            ("rm -rf .git/hooks", main, main, None),
            (f"rm -f {main}/.git/index", wt, main, None),
            ("rm -f .git", wt, main, None),
            ("rm .claude/settings.json", wt, main, None),
            ("cd .claude && rm -f settings.json", wt, main, None),
            ("rm -rf .claude", wt2, main, None),
            ("rm -rf .claude/agents", main, main, None),
            ("rm -rf .githooks", wt, main, None),
            ("rm .github/workflows/ci.yml", wt, main, None),
            (f"rm -rf {os.path.basename(plain)} .github", wt, main, None),
            ("rm -rf ~", main, main, {"HOME": homedir}),                       # a HOME inside the project
        )
        for command, cwd, project, env in nothing:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, project, env=env), before, command)
        self.assertFalse(os.path.lexists(os.path.join(homedir, ".claude")))

        in_wt = self.fill(os.path.join(wt, "cache-" + self.word()), files=2)
        lookalikes = [self.fill(os.path.join(wt, n + self.word()), files=1) for n in (".claude-old-", ".github-")]
        in_wt2 = self.fill(os.path.join(wt2, "cache-" + self.word()), files=2)
        backed = (
            (f"rm -rf .claude/worktrees/t-1-x/{os.path.basename(in_wt)}", main, main, [in_wt]),
            ("rm -rf " + " ".join(os.path.basename(p) for p in lookalikes), wt, main, lookalikes),
            (f"rm -rf {in_wt2}", main, main, [in_wt2]),
        )
        for command, cwd, project, targets in backed:
            with self.subTest(command=command, cwd=cwd):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, project)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)

    def test_branch_guard_denials_stay_denied_with_no_snapshot(self):
        main = self.make_project()                     # the main checkout on main
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        notes = self.fill(os.path.join(main, "notes-" + self.word()), files=1)    # untracked, not ignored
        scratch = os.path.join(main, f"scratch-{self.word()}.tmp")                 # git-ignored
        self.put(scratch)
        s = os.path.basename(scratch)
        victim = self.fill(os.path.join(wt, "victim-" + self.word()), files=1)
        v = os.path.basename(victim)
        denied = (
            ("rm src/app.py", main),
            ("rm -rf src", main),
            ("cd src && rm -f app.py", main),
            (f"rm -rf {os.path.basename(notes)}", main),
            (f"rm -f {s} && rm docs/a.md", main),
            (f"rm -f {main}/README.md", wt),
            # The rm is fine (a worktree); branch_guard refuses another part of the command.
            (f"rm -rf {v} && git push --force origin t/1-x", wt),
            (f"rm -rf {v} && git push -f origin t/1-x", wt),
            (f"rm -rf {v} && git push origin +t/1-x", wt),
            (f"rm -rf {v} && git push origin main", wt),
            (f"rm -rf {v} && echo x > {main}/src/new.py", wt),
            (f"rm -rf {v} && cp a {main}/src/app.py", wt),
            (f"rm -rf {v} && git -C {main} commit -m x", wt),
        )
        for command, cwd in denied:
            with self.subTest(command=command, cwd=cwd):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, main), before, command)
        command = f"rm -rf {v} && git push origin t/1-x"            # a push branch_guard lets through
        with self.subTest(command=command):
            before = self.snapshots()
            self.expect_snapshot_only(self.run_gate(command, wt, main), before, self.global_store(), [victim], command)

        # HARNESS_ALLOW_MAIN=1 in the main checkout's settings.local.json alone (git-ignored), as branch_guard reads it.
        local = self.make_project("proj-local")
        self.put(os.path.join(local, ".claude", "settings.local.json"),
                 data=(json.dumps({"env": {"HARNESS_ALLOW_MAIN": "1"}}) + "\n").encode())
        self.assertTrue(self.ignored(local, ".claude/settings.local.json"))
        target = os.path.join(local, "src", "app.py")
        with self.subTest(command="rm src/app.py", settings_local="HARNESS_ALLOW_MAIN=1"):
            before, was = self.snapshots(), {target: tree(target)}
            p = self.run_gate("rm src/app.py", local, local)
            sid = self.expect_backup(p, before, self.global_store(), [target], "rm src/app.py")
            self.round_trip(sid, [target], was, local)

        backed = (
            (f"rm -f {s}", None, [scratch]),                                    # branch_guard's ignored exemption
            ("rm -f docs/agent/old.md", None, [os.path.join(main, "docs", "agent", "old.md")]),
            ("rm src/app.py", {"HARNESS_ALLOW_MAIN": "1"}, [os.path.join(main, "src", "app.py")]),
        )
        for command, env, targets in backed:
            with self.subTest(command=command, env=env):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, main, main, env=env)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, main)

        # unlink and rmdir count as rm for branch_guard's main-branch check (ruling on #327), although
        # branch_guard itself lets them through: where rm of the path would be denied, nothing.
        empty = os.path.join(main, "empty-" + self.word())                         # untracked, not ignored
        os.mkdir(empty)
        self.assertFalse(self.ignored(main, os.path.basename(empty)))
        util = os.path.join(main, "src", "util.py")
        shapes = (
            ("unlink src/util.py", main, [util]),
            (f"rmdir {os.path.basename(empty)}", main, [empty]),
            ("cd src && unlink util.py", main, [util]),
            (f"unlink {main}/README.md", wt, [os.path.join(main, "README.md")]),
        )
        for command, cwd, _targets in shapes:
            with self.subTest(command=command, cwd=cwd, allow_main=None):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, main), before, command)
        ignored = os.path.join(main, f"scratch-{self.word()}.tmp")
        self.put(ignored)
        allowed = [(c, w, t, {"HARNESS_ALLOW_MAIN": "1"}) for c, w, t in shapes]
        allowed.append((f"unlink {os.path.basename(ignored)}", main, [ignored], None))   # rm of it is not denied
        for command, cwd, targets, env in allowed:
            with self.subTest(command=command, cwd=cwd, env=env):
                before, was = self.snapshots(), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, main, env=env)
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.round_trip(sid, targets, was, cwd)
        self.commit_settings(main, {"HARNESS_ALLOW_MAIN": "1"})
        target = os.path.join(main, "docs", "b.md")
        with self.subTest(command="rm -f docs/b.md", settings="HARNESS_ALLOW_MAIN=1"):
            before, was = self.snapshots(), {target: tree(target)}
            p = self.run_gate("rm -f docs/b.md", main, main)
            sid = self.expect_backup(p, before, self.global_store(), [target], "rm -f docs/b.md")
            self.round_trip(sid, [target], was, main)

    def test_plan_mode_and_backup_off_get_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")

        def victim(where):
            return self.fill(os.path.join(where, "victim-" + self.word()), files=2)

        for mode in ("default", "auto", "acceptEdits", "bypassPermissions"):
            target = victim(wt)
            command = f"rm -rf {os.path.basename(target)}"
            with self.subTest(mode=mode):
                before = self.snapshots()
                p = self.run_gate(command, wt, main, mode=mode)
                self.expect_backup(p, before, self.global_store(), [target], command)
        target = victim(wt)
        command = f"rm -rf {os.path.basename(target)}"
        for mode, env in (("plan", None), ("auto", {"HARNESS_BACKUP": "off"}), ("default", {"HARNESS_BACKUP": "off"})):
            with self.subTest(mode=mode, env=env):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, wt, main, mode=mode, env=env), before, command)

        off = self.make_project("proj-off", settings={"HARNESS_BACKUP": "off"})
        wt_off = self.add_worktree(off, "t-4-off", "t/4-off")
        target = victim(wt_off)
        scratch = os.path.join(off, f"scratch-{self.word()}.tmp")
        self.put(scratch)
        for command, cwd in ((f"rm -rf {os.path.basename(target)}", wt_off), (f"rm -f {os.path.basename(scratch)}", off)):
            with self.subTest(command=command, settings="HARNESS_BACKUP=off"):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, cwd, off), before, command)
        command = f"rm -rf {os.path.basename(target)}"
        with self.subTest(command=command, settings="HARNESS_BACKUP=off", env="HARNESS_BACKUP=on"):
            before = self.snapshots()
            p = self.run_gate(command, wt_off, off, env={"HARNESS_BACKUP": "on"})
            self.expect_backup(p, before, self.global_store(), [target], command)

    def test_commands_without_rm_print_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        os.makedirs(os.path.join(wt, "sub"))
        self.fill(os.path.join(wt, "build"), files=1)
        commands = (
            "ls -la", "pwd", "cd sub && ls", "true", "echo hi", "git status", "python3 build.py", "make clean",
            "mv a.txt b.txt && cp b.txt c.txt", "cat rm.txt", "echo firm > rm.log", "npm run rmdir-helper",
            "rmx a", "trm a", "farm --charm", "unlinked x", "rmdirs y", "echo rm -rf build", "ls -l rmdir",
            "printf '%s\\n' unlink", "grep -rn 'rm -rf' docs",
        )
        for command in commands:
            with self.subTest(command=command):
                before = self.snapshots()
                self.expect_nothing(self.run_gate(command, wt, main), before, command)
        edit = dict(self.event("", wt), tool_name="Edit",
                    tool_input={"file_path": os.path.join(wt, "src", "app.py"), "old_string": "a", "new_string": "b"})
        read = dict(self.event("", wt), tool_name="Read", tool_input={"file_path": os.path.join(wt, "README.md")})
        raw = (
            json.dumps(edit), json.dumps(read), json.dumps(self.event("", wt)), json.dumps(self.event("   ", wt)),
            json.dumps(dict(self.event("", wt), tool_input="rm -rf build")),
            json.dumps({k: v for k, v in self.event("", wt).items() if k != "tool_input"}),
            "not json at all, rm -rf build", "", "[]",
        )
        for stdin in raw:
            with self.subTest(stdin=stdin[:60]):
                before = self.snapshots()
                p = self.hook_run(stdin, main)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), stdin[:60])
                self.assertEqual(self.snapshots() - before, set())


# ================================================================ the directory a removal runs in
class Directory(GateTest):
    """Where a removal runs after a cd. Each case runs the hook, then a real shell on the same worktree: an allow
    must never come with something the shell removes and the snapshot lacks."""

    def scene(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        names = {"sub": "sub-" + self.word(), "nope": "nope-" + self.word(), "x": f"x-{self.word()}.txt",
                 "y": f"y-{self.word()}.txt", "d": "d-" + self.word()}
        return main, wt, names

    def refresh(self, wt, n):
        """x, y and the folder d both in the worktree and in sub/, and docs/a.md, src/util.py and README.md (tracked),
        all with fresh contents."""
        for where in ("", n["sub"]):
            for key in ("x", "y"):
                self.put(os.path.join(wt, where, n[key]))
            remove(os.path.join(wt, where, n["d"]))
            self.fill(os.path.join(wt, where, n["d"]), files=2)
        for rel in ("docs/a.md", "src/util.py", "README.md"):
            self.put(os.path.join(wt, rel))

    def shell_removes(self, command, cwd, wt):
        """What a real shell running `command` in `cwd` removes from `wt`: the topmost removed paths, relative."""
        before = entries(wt)
        argv = [SHELL, "-f", "-c", command] if os.path.basename(SHELL) == "zsh" else [SHELL, "-c", command]
        subprocess.run(argv, cwd=cwd, env=self.env(), capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        gone = before - entries(wt)
        return {g for g in gone if os.path.dirname(g) not in gone}

    def test_where_a_removal_runs_after_a_cd(self):
        """A cd that can fail, to a word the shell expands, or that may not run (also across a line continuation,
        #357) leaves the directory unknown: no decision, no snapshot. An unconditional cd into an existing folder
        moves it, whatever separator follows and whatever part stands before it: allow, with one snapshot holding
        exactly what the shell removes, and a restore that brings it back."""
        cases = (
            # (command, cwd, what the shell removes, backed up)
            ("cd {nope} && true; rm -f docs/a.md", "", ["docs/a.md"], False),            # a cd that can fail
            ("cd sub-* && rm -f {x}", "", ["{sub}/{x}"], False),                          # a glob
            ("test -d {sub} && cd {sub}; rm -f {x}", "", ["{sub}/{x}"], False),           # a cd that may not run
            ("test -d {nope} && \\\n\ncd {sub}; rm -f {x}", "", ["{x}"], False),          # the same, #357
            ("cd {sub} && rm -rf {d}", "", ["{sub}/{d}"], True),
            ("cd {sub}; rm -rf {d}", "", ["{sub}/{d}"], True),
            ("cd {wt}/{sub}; rm -rf {d}", "", ["{sub}/{d}"], True),                       # an absolute path
            ("cd .. && rm -rf {d}", "{sub}", ["{d}"], True),                               # up from a subfolder
            ("pwd\ncd {sub} && rm -rf {d}", "", ["{sub}/{d}"], True),                    # a part before the cd
        )
        main, wt, n = self.scene()
        for shape, where, removed, backed_up in cases:
            command = shape.format(wt=wt, **n)
            cwd = os.path.join(wt, where.format(**n)) if where else wt
            with self.subTest(command=command, cwd=cwd):
                self.refresh(wt, n)
                want = {r.format(**n) for r in removed}
                targets = [os.path.join(wt, r) for r in sorted(want)]
                before, there, was = self.snapshots(), entries(wt), {t: tree(t) for t in targets}
                p = self.run_gate(command, cwd, main)
                self.assertEqual(entries(wt), there, f"{command!r}: the hook itself changed the worktree")
                self.assertEqual(self.shell_removes(command, cwd, wt), want, f"{command!r}: what {SHELL} removes")
                if not backed_up:
                    self.expect_nothing(p, before, command)
                    continue
                sid = self.expect_backup(p, before, self.global_store(), targets, command)
                self.restore(sid, cwd)
                self.assertEqual({t: tree(t) for t in targets}, was, f"restored from {sid} (seed {self.seed})")


# ================================================================ git forms (#328)
def apparent_size(paths):
    """The regular files' st_size at and under `paths`, links not followed."""
    total = 0
    for p in paths:
        if not os.path.isdir(p) or os.path.islink(p):
            total += os.lstat(p).st_size if stat.S_ISREG(os.lstat(p).st_mode) else 0
            continue
        for folder, _dirs, files in os.walk(p):
            for n in files:
                st = os.lstat(os.path.join(folder, n))
                total += st.st_size if stat.S_ISREG(st.st_mode) else 0
    return total


class GitGate(GateTest):
    """Helpers for the git forms: git state read through git, refs checked against the manifest."""

    def text(self):
        """A few lines of random text: the bytes of an edit."""
        return "".join(f"{self.word()} {self.word()}\n" for _ in range(self.rng.randint(1, 6))).encode()

    def edit(self, repo, rel, stage=False):
        data = self.put(os.path.join(repo, rel), data=self.text())
        if stage:
            self.git(repo, "add", "--", rel)
        return data

    def commit(self, repo, message="Work"):
        """A commit of a new random file on `repo`'s branch; returns its sha."""
        self.edit(repo, f"src/w_{self.word()}.py", stage=True)
        self.git(repo, "commit", "-q", "-m", message)
        return self.rev(repo)

    def rev(self, repo, rev="HEAD"):
        return self.git(repo, "rev-parse", "--verify", "-q", rev + "^{commit}")

    def branch_at(self, repo, name):
        """The sha refs/heads/<name> holds, None when there is no such branch."""
        p = subprocess.run([GIT, "-C", repo, "rev-parse", "--verify", "-q", "refs/heads/" + name], env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        return p.stdout.strip() if p.returncode == 0 else None

    def blob(self, repo, rev, rel):
        p = subprocess.run([GIT, "-C", repo, "show", f"{rev}:{rel}"], env=self.env(), capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, f"git show {rev}:{rel}: {p.stderr!r}")
        return p.stdout

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def backup_refs(self, repo):
        """refname -> sha of every ref under refs/harness-backup/, as git lists them."""
        out = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/harness-backup/")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def stash(self, repo):
        """[(sha, subject)] of the stash list, newest first."""
        out = self.git(repo, "stash", "list", "--format=%H%x09%gs")
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if line.strip()]

    def files_state(self, repo):
        """What a ref's restore must leave alone: the files (.git aside), git status, the index and HEAD."""
        files = {k: v for k, v in tree(repo).items() if k != ".git" and not k.startswith(".git/")}
        return {"files": files, "status": self.git(repo, "status", "--porcelain=v1", "-uall", "--ignored"),
                "index": self.git(repo, "ls-files", "-s"), "head": self.git(repo, "rev-parse", "HEAD"),
                "branch": self.git(repo, "rev-parse", "--symbolic-full-name", "HEAD")}

    def state(self, repo):
        """What the hook must leave alone: files_state, the branches and the stash list."""
        return dict(self.files_state(repo), stash=self.stash(repo),
                    branches=self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/"))

    def manifest(self, sid):
        for store in [self.global_store()] + [self.project_store(m) for m in self.mains]:
            path = os.path.join(store, sid, "manifest.json")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
        self.fail(f"no manifest for {sid}")

    def changes_of(self, repo, head, worktree, index=None):
        """A check that a `changes` sha is git stash create's commit of these edits: `head` its first parent,
        `worktree` ({rel: bytes}) in its tree, `index` ({rel: bytes}) in its second parent's."""
        def check(sha):
            self.assertEqual(self.git(repo, "cat-file", "-t", sha), "commit", f"changes {sha} is not a commit")
            self.assertEqual(self.rev(repo, sha + "^1"), head, "the changes commit's first parent is not HEAD")
            for rel, data in worktree.items():
                self.assertEqual(self.blob(repo, sha, rel), data, f"{rel} in the changes commit (seed {self.seed})")
            for rel, data in (index or {}).items():
                self.assertEqual(self.blob(repo, sha + "^2", rel), data, f"{rel} in the changes commit's index")
        return check

    def check_refs(self, sid, command, repo, top, want):
        """The manifest's refs are exactly `want` ({(what, name): sha, or a check(sha)}), each entry
        {ref, sha, what, name, worktree} with ref refs/harness-backup/<id>/<what>[/<name>] and worktree `top`, and
        git holds exactly those refs under refs/harness-backup/<id>/ in `repo`. Returns {(what, name): sha}."""
        refs = self.manifest(sid).get("refs")
        self.assertIsInstance(refs, list, f"{command!r}: the manifest's refs")
        found = {}
        for entry in refs:
            self.assertIsInstance(entry, dict, f"{command!r}: {entry!r}")
            self.assertEqual(set(entry), REF_KEYS, f"{command!r}: {entry}")
            what, name = entry["what"], entry["name"]
            key = (what, None if name is None else str(name))
            self.assertNotIn(key, found, f"{command!r}: two refs for {key}")
            found[key] = entry["sha"]
            self.assertEqual(entry["ref"], f"refs/harness-backup/{sid}/{what}" + ("" if name is None else f"/{name}"),
                             f"{command!r}: {entry}")
            self.assertEqual(real(entry["worktree"]), real(top), f"{command!r}: the worktree of {key}")
        self.assertEqual(set(found), set(want), f"{command!r}: the refs kept (seed {self.seed})")
        for key, expected in want.items():
            if callable(expected):
                with self.subTest(ref=key):
                    expected(found[key])
            else:
                self.assertEqual(found[key], expected, f"{command!r}: the sha of {key}")
        held = {r: s for r, s in self.backup_refs(repo).items() if r.startswith(f"refs/harness-backup/{sid}/")}
        self.assertEqual(held, {e["ref"]: e["sha"] for e in refs}, f"{command!r}: the refs git holds for {sid}")
        return found

    def expect_refs(self, p, before, command, repo, top, want, paths=(), store=None):
        """Allow, one new snapshot (in `store`, default the global one) holding `paths` and the refs `want`;
        returns (id, {(what, name): sha})."""
        sid = self.expect_backup(p, before, store or self.global_store(), list(paths), command)
        return sid, self.check_refs(sid, command, repo, top, want)

    def expect_none(self, p, before, refs_before, command, repo):
        """No decision, no snapshot and no new ref."""
        self.expect_nothing(p, before, command)
        self.assertEqual(self.backup_refs(repo), refs_before, f"{command!r}: a ref was written (seed {self.seed})")

    def guard_denies(self, command, cwd, project, env=None):
        p = self.in_process(json.dumps(self.event(command, cwd)), project, env=env, hook=GUARD)
        out = json.loads(p.stdout) if p.stdout.strip() else {}
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def restore_refused(self, sid, cwd, *names):
        """`harness-work backup restore` refused: exit 1, one stderr line naming one of `names`."""
        p = subprocess.run([sys.executable, TOOL, "backup", "restore", sid], cwd=cwd, env=self.env(),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(p.returncode, 1, f"restore {sid}: {p.stdout}{p.stderr}")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertTrue(any(n in lines[0] for n in names), f"the refusal names none of {names}: {lines[0]}")
        return p

    def taken(self, sids, key):
        """The snapshot id the case `key` recorded above; a failure, not an error, when it took none."""
        self.assertIn(key, sids, f"{key!r} took no snapshot to restore (seed {self.seed})")
        return sids[key]

    def stored(self, repo, sid):
        """The shas the stash list holds under the subject `harness-backup <id>`."""
        return [sha for sha, subject in self.stash(repo) if subject == f"harness-backup {sid}"]

    def apply(self, repo, sha):
        """git stash apply of the stash entry holding `sha`."""
        shas = [s for s, _subject in self.stash(repo)]
        self.assertIn(sha, shas, "no stash entry holds it")
        self.git(repo, "stash", "apply", "-q", f"stash@{{{shas.index(sha)}}}")


class GitResets(GitGate):
    def test_reset_hard_keeps_head_and_changes_and_restores_them(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        self.commit(wt, "Feature")                                       # HEAD is the ticket's own commit
        self.commit(wt, "More")
        head = self.rev(wt)
        new = f"src/new_{self.word()}.py"
        edits = {"src/app.py": self.edit(wt, "src/app.py"),                       # unstaged
                 "docs/a.md": self.edit(wt, "docs/a.md", stage=True),             # staged
                 new: self.edit(wt, new, stage=True)}                             # a staged new file
        self.put(os.path.join(wt, f"notes-{self.word()}.txt"))                    # untracked: no part of changes
        staged = {rel: edits[rel] for rel in ("docs/a.md", new)}
        changes = self.changes_of(wt, head, edits, staged)
        cases = (
            ("git reset --hard", wt, main),
            ("git reset --hard HEAD", wt, main),
            ("git reset -q --hard HEAD~1", wt, main),                    # head is HEAD before, not the target
            ("git reset --hard HEAD~2", wt, wt),
            ("cd src && git reset --hard", wt, main),
            (f"git -C {wt} reset --hard", main, main),                   # git runs in the worktree
            ("git reset --merge", wt, main),
        )
        sids = {}
        for command, cwd, project in cases:
            with self.subTest(command=command, cwd=cwd):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, cwd, project)
                sid, _found = self.expect_refs(p, before, command, wt, wt, {("head", None): head,
                                                                            ("changes", None): changes})
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
                sids[command, cwd] = sid

        # The round trip: the real reset drops the edits; the restore stores them; git stash apply brings them back.
        sid = self.taken(sids, (cases[0][0], cases[0][1]))
        kept = self.check_refs(sid, cases[0][0], wt, wt, {("head", None): head, ("changes", None): changes})
        self.git(wt, "reset", "-q", "--hard")
        self.assertEqual(self.read(os.path.join(wt, "src", "app.py")), self.blob(wt, "HEAD", "src/app.py"))
        self.assertFalse(os.path.lexists(os.path.join(wt, new)))
        there = self.files_state(wt)
        p = self.restore(sid, wt)
        self.assertIn("git stash apply", p.stdout, "the restore does not name the next step")
        self.assertEqual(self.files_state(wt), there, "the restore of refs touched the worktree")
        self.assertEqual(self.stored(wt, sid), [kept[("changes", None)]])
        self.assertEqual(self.branch_at(wt, f"harness-restore/{sid}"), head)
        self.assertEqual(self.rev(wt), head, "the restore moved the branch")
        self.apply(wt, kept[("changes", None)])
        self.assertEqual({rel: self.read(os.path.join(wt, rel)) for rel in edits}, edits, f"seed {self.seed}")

    def test_clean_tree_reset_records_only_head(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        wt2 = self.add_worktree(main, "t-2-y", "t/2-y")
        base = self.rev(wt)
        self.commit(wt, "One")
        tip = self.commit(wt, "Two")
        # Untracked and ignored files are no tracked change: no `changes`.
        for where in (wt, wt2):
            self.put(os.path.join(where, f"notes-{self.word()}.txt"))
            self.put(os.path.join(where, f"scratch-{self.word()}.tmp"))
        self.assertTrue(self.git(wt, "status", "--porcelain"))
        cases = (
            ("git reset --hard", wt, wt, tip),
            ("git reset --hard HEAD~2", wt, wt, tip),
            ("git reset --merge", wt, wt, tip),
            ("cd docs && git reset --hard HEAD~1", wt, wt, tip),
            ("git reset --hard", wt2, wt2, base),                        # the other worktree's own HEAD
            (f"git -C {wt2} reset --hard {tip}", wt, wt2, base),
        )
        sids = {}
        for command, cwd, top, head in cases:
            with self.subTest(command=command, cwd=cwd):
                before, there = self.snapshots(), self.state(top)
                p = self.run_gate(command, cwd, main)
                sid, _found = self.expect_refs(p, before, command, top, top, {("head", None): head})
                self.assertEqual(self.state(top), there, f"{command!r}: the hook changed the worktree")
                sids[command] = sid

        # The real reset leaves the two commits behind; the restore makes harness-restore/<id> of the old HEAD.
        sid = self.taken(sids, "git reset --hard HEAD~2")
        self.git(wt, "reset", "-q", "--hard", "HEAD~2")
        self.assertEqual(self.rev(wt), base)
        there, stash = self.files_state(wt), self.stash(wt)
        self.restore(sid, wt)
        self.assertEqual(self.branch_at(wt, f"harness-restore/{sid}"), tip)
        self.assertEqual(self.files_state(wt), there, "the restore of a head ref touched the worktree")
        self.assertEqual(self.branch_at(wt, "t/1-x"), base, "the restore moved the branch")
        self.assertEqual(self.stash(wt), stash, "a head ref went into the stash list")


class GitPaths(GitGate):
    def test_checkout_and_restore_of_paths_keep_changes(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        self.commit(wt)
        head = self.rev(wt)
        other = f"t/{self.rng.randint(10, 99)}-{self.word()}"
        self.git(wt, "branch", other)
        edits = {"src/app.py": self.edit(wt, "src/app.py"), "src/util.py": self.edit(wt, "src/util.py"),
                 "README.md": self.edit(wt, "README.md", stage=True)}
        changes = self.changes_of(wt, head, edits, {"README.md": edits["README.md"]})
        kept = (
            ("git checkout -- src/app.py", wt),
            ("git checkout src/app.py", wt),
            ("git checkout HEAD -- src/util.py", wt),
            ("git checkout .", wt),
            ("git checkout -- src", wt),
            ("git checkout -f", wt),
            (f"git checkout -f {other}", wt),
            ("git restore src/app.py", wt),
            ("git restore --worktree src/util.py", wt),
            ("git restore -W -S README.md", wt),
            ("git restore --source=HEAD src", wt),
            ("git restore .", wt),
            ("git restore app.py util.py", os.path.join(wt, "src")),
            (f"git switch -f {other}", wt),
            (f"git switch --discard-changes {other}", wt),
            ("git rm -f src/util.py", wt),
            ("git rm -r -f src", wt),
            (f"git -C {wt} restore src/app.py", main),
        )
        for command, cwd in kept:
            with self.subTest(command=command, cwd=cwd):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, cwd, main)
                self.expect_refs(p, before, command, wt, wt, {("changes", None): changes})
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
        for command in ("git restore --staged README.md", "git restore -S README.md", "git rm --cached README.md",
                        f"git switch {other}", f"git checkout {other}"):
            with self.subTest(command=command):
                before, refs = self.snapshots(), self.backup_refs(wt)
                self.expect_none(self.run_gate(command, wt, main), before, refs, command, wt)

        # A clean tracked tree has nothing to keep: allow, no snapshot, no ref.
        wt2 = self.add_worktree(main, "t-2-y", "t/2-y")
        self.put(os.path.join(wt2, f"notes-{self.word()}.txt"))
        for command in ("git checkout -- src/app.py", "git restore .", "git checkout -f", "git rm -f docs/a.md"):
            with self.subTest(command=command, tree="clean"):
                before, refs = self.snapshots(), self.backup_refs(wt2)
                self.expect_allow_without_copy(self.run_gate(command, wt2, main), before, command)
                self.assertEqual(self.backup_refs(wt2), refs, f"{command!r}: a ref was written")

        # The round trip: git restore drops the edit, the restore stores it, git stash apply brings it back.
        mine = {"src/app.py": self.edit(wt2, "src/app.py")}
        command = "git restore src/app.py"
        before = self.snapshots()
        sid, found = self.expect_refs(self.run_gate(command, wt2, main), before, command, wt2, wt2,
                                      {("changes", None): self.changes_of(wt2, self.rev(wt2), mine)})
        self.git(wt2, "restore", "src/app.py")
        self.assertEqual(self.read(os.path.join(wt2, "src", "app.py")), self.blob(wt2, "HEAD", "src/app.py"))
        there = self.files_state(wt2)
        p = self.restore(sid, wt2)
        self.assertIn("git stash apply", p.stdout)
        self.assertEqual(self.files_state(wt2), there, "the restore of refs touched the worktree")
        self.assertEqual(self.stored(wt2, sid), [found[("changes", None)]])
        self.apply(wt2, found[("changes", None)])
        self.assertEqual(self.read(os.path.join(wt2, "src", "app.py")), mine["src/app.py"])


class GitBranches(GitGate):
    def test_branch_delete_keeps_the_tip_and_restore_recreates_it(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")

        def branch(name, commits=1):
            self.git(wt, "switch", "-q", "-c", name)
            for _ in range(commits):
                self.commit(wt)
            tip = self.rev(wt)
            self.git(wt, "switch", "-q", "t/1-x")
            return tip

        old = "t/9-old"
        a, b = f"t/{self.rng.randint(10, 99)}-{self.word()}", f"feature/{self.word()}"
        merged = f"t/{self.rng.randint(100, 999)}-merged-{self.word()}"
        tips = {old: branch(old), a: branch(a, 2), b: branch(b), merged: branch(merged, 0)}
        self.assertEqual(len(set(tips[n] for n in (old, a, b))), 3)
        cases = (
            (f"git branch -D {old}", wt, wt, [old]),
            (f"git branch -D {a} {b}", wt, wt, [a, b]),
            (f"git branch -d {merged}", wt, wt, [merged]),
            (f"git branch --delete --force {b}", wt, wt, [b]),
            (f"git branch -d -f {a}", os.path.join(wt, "src"), wt, [a]),
            (f"git branch -D {old}", main, main, [old]),          # on main, branch_guard refuses only main and master
            (f"git -C {main} branch -D {b}", wt, main, [b]),
        )
        sids = {}
        for command, cwd, top, names in cases:
            with self.subTest(command=command, cwd=cwd):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, cwd, main)
                sid, _found = self.expect_refs(p, before, command, wt, top, {("branch", n): tips[n] for n in names})
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the repository")
                sids[command, cwd] = sid

        # The round trip: the branches go, the restore recreates each at its tip and touches no worktree.
        for command, names in ((f"git branch -D {old}", [old]), (f"git branch -D {a} {b}", [a, b])):
            with self.subTest(round_trip=command):
                sid = self.taken(sids, (command, wt))
                self.git(wt, "branch", "-q", "-D", *names)
                self.assertEqual([self.branch_at(wt, n) for n in names], [None] * len(names))
                there = self.files_state(wt)
                self.restore(sid, wt)
                self.assertEqual({n: self.branch_at(wt, n) for n in names}, {n: tips[n] for n in names})
                self.assertEqual(self.files_state(wt), there, "the restore of a branch touched the worktree")

        # A branch that exists again refuses the restore, and is left where it is.
        self.git(wt, "branch", "-q", "-f", old, "HEAD")
        moved = self.branch_at(wt, old)
        self.assertNotEqual(moved, tips[old])
        self.restore_refused(self.taken(sids, (f"git branch -D {old}", wt)), wt, old)
        self.assertEqual(self.branch_at(wt, old), moved)


class GitStash(GitGate):
    def test_stash_drop_and_clear_keep_each_stash(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        content = {}
        for k in range(3):
            data = self.edit(wt, self.rng.choice(["src/app.py", "src/util.py", "docs/b.md"]))
            self.git(wt, "stash", "push", "-q", "-m", f"wip {k} {self.word()}")
            content[self.rev(wt, "stash@{0}")] = data
        shas = [sha for sha, _subject in self.stash(wt)]
        self.assertEqual(len(set(shas)), 3)
        cases = (
            ("git stash drop", {("stash", "0"): shas[0]}),
            ("git stash drop stash@{2}", {("stash", "2"): shas[2]}),
            ("git stash drop -q 'stash@{1}'", {("stash", "1"): shas[1]}),
            ("git stash drop 1", {("stash", "1"): shas[1]}),
            ("git stash clear", {("stash", str(n)): sha for n, sha in enumerate(shas)}),
        )
        sids = {}
        for command, want in cases:
            with self.subTest(command=command):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, wt, main)
                sid, _found = self.expect_refs(p, before, command, wt, wt, want)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the stash list or worktree")
                sids[command] = sid

        # The round trips: the real drop and clear, then the restore stores each entry back.
        sid = self.taken(sids, "git stash drop stash@{2}")
        self.git(wt, "stash", "drop", "-q", "stash@{2}")
        self.assertNotIn(shas[2], [s for s, _ in self.stash(wt)])
        there = self.files_state(wt)
        p = self.restore(sid, wt)
        self.assertIn("git stash apply", p.stdout)
        self.assertEqual(self.stored(wt, sid), [shas[2]])
        self.assertEqual(self.files_state(wt), there, "the restore of a stash touched the worktree")

        sid = self.taken(sids, "git stash clear")
        self.git(wt, "stash", "clear")
        self.assertEqual(self.stash(wt), [])
        self.restore(sid, wt)
        self.assertEqual(sorted(self.stored(wt, sid)), sorted(shas))
        self.assertEqual(len(self.stash(wt)), 3)
        self.assertEqual(self.files_state(wt), there, "the restore of a stash touched the worktree")
        pick = self.rng.choice(shas)
        self.apply(wt, pick)
        self.assertIn(content[pick], [self.read(os.path.join(wt, rel)) for rel in ("src/app.py", "src/util.py",
                                                                                      "docs/b.md")])


class GitClean(GitGate):
    def test_git_clean_copies_what_it_would_remove(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.global_store()
        app = self.edit(wt, "src/app.py")                         # a tracked edit: clean keeps no `changes` for it
        gen = self.fill(os.path.join(wt, "gen-" + self.word()))
        notes = os.path.join(wt, f"notes-{self.word()}.txt")
        self.put(notes)
        new = os.path.join(wt, "src", f"new_{self.word()}.py")
        self.put(new)
        drafts = self.fill(os.path.join(wt, "docs", "drafts-" + self.word()), files=2)
        mixed = os.path.join(wt, "mixed-" + self.word())
        keep = os.path.join(mixed, f"keep-{self.word()}.txt")
        self.put(keep)
        self.put(os.path.join(mixed, f"skip-{self.word()}.tmp"))    # ignored: git clean -d lists keep alone
        pkg = os.path.join(wt, "pkg-" + self.word())
        self.put(os.path.join(pkg, "mod.py"))
        cached = self.put(os.path.join(pkg, "__pycache__", "mod.cpython-39.pyc"), size=4096)
        dist = self.fill(os.path.join(wt, "dist"), files=2)           # listed, rebuildable: no copy
        built = self.put(os.path.join(dist, "bundle.js"), size=4096)
        self.put(os.path.join(wt, f"scratch-{self.word()}.tmp"))      # ignored: not without -x
        self.fill(os.path.join(wt, "data"), files=2)
        self.assertFalse(self.ignored(wt, "dist"))
        everything = [gen, notes, new, drafts, keep, pkg]
        cases = (
            ("git clean -fd", wt, main, everything),
            ("git clean -f", wt, main, [notes, new]),                    # without -d no untracked folder goes
            ("git clean -f -d src docs", wt, main, [new, drafts]),
            ("git clean -fd -e 'notes-*'", wt, main, [p for p in everything if p != notes]),
            ("git clean -fd", os.path.join(wt, "src"), main, [new]),      # from a subfolder, what is under it
            (f"git clean --force -d -- {os.path.basename(gen)} {os.path.relpath(keep, wt)}", wt, main, [gen, keep]),
            (f"git -C {wt} clean -fd docs", main, main, [drafts]),
        )
        sids = {}
        for command, cwd, project, targets in cases:
            with self.subTest(command=command, cwd=cwd):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, cwd, project)
                sid, _found = self.expect_refs(p, before, command, wt, wt, {}, paths=targets)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
                for marker, what in ((built, "dist"), (cached, "pkg's __pycache__")):
                    self.assertFalse(holds(os.path.join(store, sid), marker), f"{command!r}: {what} was copied")
                sids[command, cwd] = sid
        for command in ("git clean -n -d", "git clean -fdn", "git clean --dry-run -f -d"):
            with self.subTest(command=command):
                before, refs = self.snapshots(), self.backup_refs(wt)
                self.expect_none(self.run_gate(command, wt, main), before, refs, command, wt)
        command = "git reset --hard && git clean -fd"                      # one snapshot: the refs and the copies
        with self.subTest(command=command):
            before = self.snapshots()
            self.expect_refs(self.run_gate(command, wt, main), before, command, wt, wt,
                             {("head", None): self.rev(wt),
                              ("changes", None): self.changes_of(wt, self.rev(wt), {"src/app.py": app})},
                             paths=everything)

        # The round trip: what the real git clean -fd removes is the snapshot plus dist; the restore brings it back.
        sid = self.taken(sids, ("git clean -fd", wt))
        was = {t: tree(t) for t in everything}
        was[pkg] = {k: v for k, v in was[pkg].items() if k != "__pycache__" and not k.startswith("__pycache__/")}
        listed = entries(wt)
        self.git(wt, "clean", "-q", "-f", "-d")
        gone = listed - entries(wt)
        self.assertEqual({g for g in gone if os.path.dirname(g) not in gone},
                         {os.path.relpath(t, wt) for t in everything} | {"dist"}, "what git clean -fd removed")
        self.restore(sid, wt)
        self.assertEqual({t: tree(t) for t in everything}, was, f"restored (seed {self.seed})")
        self.assertFalse(os.path.lexists(dist))

        # Only rebuildable paths, or nothing, to remove: allow, no snapshot.
        wt2 = self.add_worktree(main, "t-2-y", "t/2-y")
        self.fill(os.path.join(wt2, "dist"), files=2)
        self.fill(os.path.join(wt2, "lib", "__pycache__"), files=1)
        for command in ("git clean -fd", "git clean -f"):
            with self.subTest(command=command, tree="rebuildable only"):
                before, refs = self.snapshots(), self.backup_refs(wt2)
                self.expect_allow_without_copy(self.run_gate(command, wt2, main), before, command)
                self.assertEqual(self.backup_refs(wt2), refs)

        # Over the cap: ask, and neither a snapshot nor a ref.
        wt3 = self.add_worktree(main, "t-3-cap", "t/3-cap")
        big = self.fill(os.path.join(wt3, "big-" + self.word()), files=3, total=self.rng.randint(2_300_000, 2_900_000))
        small = os.path.join(wt3, f"small-{self.word()}.txt")
        self.put(small)
        self.edit(wt3, "src/util.py")
        total = apparent_size([big, small])
        for command in ("git clean -fd", "git reset --hard && git clean -fd"):
            with self.subTest(command=command, cap=1):
                before, refs = self.snapshots(), self.backup_refs(wt3)
                p = self.run_gate(command, wt3, main, env={"HARNESS_BACKUP_CAP_MB": "1"})
                decision, reason = self.answer(p)
                self.assertEqual(decision, "ask", f"{command!r}: {p.stdout!r}")
                self.assertEqual(self.snapshots() - before, set(), f"{command!r}: a snapshot was made")
                self.assertEqual(self.backup_refs(wt3), refs, f"{command!r}: a ref was written")
                self.assertTrue(names_size(reason, total), f"the reason does not name {total} bytes: {reason}")
                self.assertTrue(names_cap(reason, 1), f"the reason does not name the cap: {reason}")
        with self.subTest(command="git clean -fd", cap=5):
            before = self.snapshots()
            p = self.run_gate("git clean -fd", wt3, main, env={"HARNESS_BACKUP_CAP_MB": "5"})
            self.expect_refs(p, before, "git clean -fd", wt3, wt3, {}, paths=[big, small])

    def test_git_clean_x_keeps_sensitive_paths_in_the_project(self):
        main = self.make_project("vault", settings={"HARNESS_SENSITIVE_PATHS": "data/**"})
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        store = self.project_store(main)
        data = self.fill(os.path.join(wt, "data"), files=3)                   # ignored and sensitive
        marker = self.put(os.path.join(data, f"rows-{self.word()}.db"), size=3000)
        env = os.path.join(wt, ".env")                                         # ignored, secret-shaped
        self.put(env, mode=0o600)
        scratch = os.path.join(wt, f"scratch-{self.word()}.tmp")               # ignored: copied (ruling 2)
        self.put(scratch)
        modules = self.fill(os.path.join(wt, "node_modules"), files=2)         # ignored, rebuildable
        venv = self.fill(os.path.join(wt, ".venv"), files=2)                   # untracked, rebuildable
        notes = os.path.join(wt, f"notes-{self.word()}.txt")
        self.put(notes)
        for rel in ("data", ".env", os.path.basename(scratch), "node_modules"):
            self.assertTrue(self.ignored(wt, rel), rel)
        cases = (
            ("git clean -fdx", [data, env, scratch, notes]),
            ("git clean -fdX", [data, env, scratch]),
            ("git clean -fd", [notes]),
            ("git clean -f -x -d data", [data]),
        )
        sids = {}
        for command, targets in cases:
            with self.subTest(command=command):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, wt, main)
                sid, _found = self.expect_refs(p, before, command, wt, wt, {}, paths=targets, store=store)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
                self.assertEqual(holds(os.path.join(store, sid), marker), data in targets, command)
                sids[command] = sid
        glob = self.global_store()
        self.assertEqual(os.listdir(glob) if os.path.isdir(glob) else [], [], f"written under {glob}")
        self.assertFalse(holds(self.home, marker), "a sensitive path's copy left the project")

        # The round trip: the real git clean -fdx, then the restore from the project's store.
        sid = self.taken(sids, "git clean -fdx")
        targets = [data, env, scratch, notes]
        was = {t: tree(t) for t in targets}
        listed = entries(wt)
        self.git(wt, "clean", "-q", "-f", "-d", "-x")
        gone = listed - entries(wt)
        self.assertEqual({g for g in gone if os.path.dirname(g) not in gone},
                         {os.path.relpath(t, wt) for t in targets + [modules, venv]}, "what git clean -fdx removed")
        self.restore(sid, wt)
        self.assertEqual({t: tree(t) for t in targets}, was, f"restored (seed {self.seed})")
        self.assertFalse(os.path.lexists(modules) or os.path.lexists(venv))


class GitGuard(GitGate):
    def test_git_forms_branch_guard_denies_on_main_get_nothing(self):
        main = self.make_project()                                          # the main checkout on main
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        old = f"t/{self.rng.randint(10, 99)}-old-{self.word()}"
        self.git(main, "branch", old)
        readme = self.edit(main, "README.md")
        self.edit(main, "src/app.py")
        notes = os.path.join(main, f"notes-{self.word()}.txt")
        self.put(notes)
        self.edit(wt, "src/util.py")
        denied = (
            ("git reset --hard", main),
            ("git clean -fd", main),
            ("git checkout -- README.md", main),
            ("git branch -D main", main),
            (f"git -C {main} reset --hard", wt),
        )
        for command, cwd in denied:
            with self.subTest(command=command, cwd=cwd):
                self.assertTrue(self.guard_denies(command, cwd, main), f"branch_guard lets {command!r} through")
                before, refs, there = self.snapshots(), self.backup_refs(main), (self.state(main), self.state(wt))
                self.expect_none(self.run_gate(command, cwd, main), before, refs, command, main)
                self.assertEqual((self.state(main), self.state(wt)), there, f"{command!r}: the hook changed a tree")

        # What branch_guard lets through there is backed up and allowed: another branch, or HARNESS_ALLOW_MAIN=1.
        head = self.rev(main)
        allow_main = {"HARNESS_ALLOW_MAIN": "1"}
        allowed = (
            (f"git branch -D {old}", None, {("branch", old): head}, []),
            ("git reset --hard", allow_main,
             {("head", None): head, ("changes", None): self.changes_of(main, head, {"README.md": readme})}, []),
            ("git clean -fd", allow_main, {}, [notes]),
        )
        for command, env, want, paths in allowed:
            with self.subTest(command=command, env=env):
                self.assertFalse(self.guard_denies(command, main, main, env=env))
                before = self.snapshots()
                p = self.run_gate(command, main, main, env=env)
                self.expect_refs(p, before, command, main, main, want, paths=paths)

    def test_plan_mode_and_backup_off_get_nothing_for_git(self):
        def scene(main, name, branch):
            """A worktree with an edit, an untracked file and a branch of its own; its cases (command, refs, paths)."""
            wt = self.add_worktree(main, name, branch)
            x = f"t/{self.rng.randint(10, 99)}-{self.word()}"
            self.git(wt, "switch", "-q", "-c", x)
            tip = self.commit(wt)
            self.git(wt, "switch", "-q", branch)
            head = self.rev(wt)
            edits = {"src/app.py": self.edit(wt, "src/app.py")}
            notes = os.path.join(wt, f"notes-{self.word()}.txt")
            self.put(notes)
            return wt, (
                ("git reset --hard", {("head", None): head, ("changes", None): self.changes_of(wt, head, edits)}, []),
                (f"git branch -D {x}", {("branch", x): tip}, []),
                ("git clean -fd", {}, [notes]),
            )

        off = {"HARNESS_BACKUP": "off"}
        main = self.make_project()
        wt, cases = scene(main, "t-1-x", "t/1-x")
        for command, want, paths in cases:
            for mode, env in (("plan", None), ("auto", off)):
                with self.subTest(command=command, mode=mode, env=env):
                    before, refs = self.snapshots(), self.backup_refs(wt)
                    self.expect_none(self.run_gate(command, wt, main, mode=mode, env=env), before, refs, command, wt)
            with self.subTest(command=command, mode="auto"):
                before = self.snapshots()
                self.expect_refs(self.run_gate(command, wt, main, mode="auto"), before, command, wt, wt, want,
                                 paths=paths)

        project = self.make_project("proj-off", settings=off)
        wt_off, cases = scene(project, "t-4-off", "t/4-off")
        for command, want, paths in cases:
            with self.subTest(command=command, settings="HARNESS_BACKUP=off"):
                before, refs = self.snapshots(), self.backup_refs(wt_off)
                self.expect_none(self.run_gate(command, wt_off, project), before, refs, command, wt_off)
            with self.subTest(command=command, settings="HARNESS_BACKUP=off", env="HARNESS_BACKUP=on"):
                before = self.snapshots()
                p = self.run_gate(command, wt_off, project, env={"HARNESS_BACKUP": "on"})
                self.expect_refs(p, before, command, wt_off, wt_off, want, paths=paths)


class GitOutside(GitGate):
    def outside_state(self, repo):
        """state(repo) without anything under a .git, its own or a nested repository's."""
        found = self.state(repo)
        found["files"] = {k: v for k, v in found["files"].items() if ".git" not in k.split("/")}
        return found

    def foreign(self, name):
        """A repository on dev (branch_guard refuses nothing there) with a branch of its own commit, a stash entry, an
        edit, a staged edit and an untracked file; (its path, the branch)."""
        repo = real(self.make_project(name, branch="dev"))
        x = f"t/{self.rng.randint(10, 99)}-{self.word()}"
        self.git(repo, "switch", "-q", "-c", x)
        self.commit(repo)
        self.git(repo, "switch", "-q", "dev")
        self.edit(repo, "docs/b.md")
        self.git(repo, "stash", "push", "-q", "-m", f"wip {self.word()}")
        self.edit(repo, "README.md")
        self.edit(repo, "src/app.py", stage=True)
        self.put(os.path.join(repo, f"notes-{self.word()}.txt"))
        return repo, x

    def test_git_in_a_repository_outside_the_project_gets_nothing(self):
        """A repository nested in the worktree's folder (a submodule's checkout, a cloned dependency) is not the
        project: git run there, by -C or after a cd, gets nothing in either repository. What counts is where git
        runs: from a cwd in that repository, `git -C <worktree>` is backed up."""
        main = self.make_project()                                     # the session's project, on main
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        head = self.rev(wt)
        mine = {"src/app.py": self.edit(wt, "src/app.py")}
        repo, _branch = self.foreign(os.path.relpath(os.path.join(wt, "third_party", "dep-" + self.word()), self.tmp))
        for command in (f"git -C {repo} reset --hard", f"cd {os.path.relpath(repo, wt)} && git clean -fd"):
            with self.subTest(command=command):
                self.assertFalse(self.guard_denies(command, wt, main), f"branch_guard denies {command!r}")
                before = self.snapshots()
                refs = (self.backup_refs(main), self.backup_refs(repo))
                there = (self.outside_state(repo), self.outside_state(wt))
                self.expect_nothing(self.run_gate(command, wt, main), before, command)
                self.assertEqual((self.backup_refs(main), self.backup_refs(repo)), refs,
                                 f"{command!r}: a ref was written (seed {self.seed})")
                self.assertEqual((self.outside_state(repo), self.outside_state(wt)), there,
                                 f"{command!r}: the hook changed a tree")
        command = f"git -C {wt} reset --hard"
        with self.subTest(command=command, cwd=repo, inside=True):
            before = self.snapshots()
            self.expect_refs(self.run_gate(command, repo, main), before, command, wt, wt,
                             {("head", None): head, ("changes", None): self.changes_of(wt, head, mine)})


class GitParts(GitGate):
    def test_pushes_get_no_decision(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        old = f"t/{self.rng.randint(10, 99)}-{self.word()}"
        self.git(wt, "switch", "-q", "-c", old)
        tip = self.commit(wt)
        self.git(wt, "switch", "-q", "t/1-x")
        head = self.rev(wt)
        mine = {"src/app.py": self.edit(wt, "src/app.py")}
        nothing = (
            "git push --force origin t/1-x",
            "git push -f origin t/1-x",
            "git push --force-with-lease origin t/1-x",
            "git push origin +t/1-x",
            f"git push origin --delete {old}",
            f"git push --delete origin {old}",
            f"git push origin -d {old}",
            f"git push origin :{old}",
            "git push origin t/1-x",
            "git push -u origin HEAD",
            "git reset --hard && git push --force origin t/1-x",              # branch_guard refuses the command
            f"git branch -D {old}; git push -f origin t/1-x",
            "git stash clear && git push origin +t/1-x",
        )
        for command in nothing:
            with self.subTest(command=command):
                before, refs = self.snapshots(), self.backup_refs(wt)
                self.expect_none(self.run_gate(command, wt, main), before, refs, command, wt)
        command = f"git branch -D {old}"                                        # alone: backed up and allowed
        with self.subTest(command=command):
            before = self.snapshots()
            self.expect_refs(self.run_gate(command, wt, main), before, command, wt, wt, {("branch", old): tip})
        kept = (
            (f"git branch -D {old} && git push origin --delete {old}", {("branch", old): tip}),
            ("git reset --hard && git push origin t/1-x",
             {("head", None): head, ("changes", None): self.changes_of(wt, head, mine)}),
        )
        for command, want in kept:                                              # a push is never harmless
            with self.subTest(command=command):
                before = self.snapshots()
                sid = self.expect_snapshot_only(self.run_gate(command, wt, main), before, self.global_store(), [],
                                                command)
                self.check_refs(sid, command, wt, wt, want)

    def test_read_only_git_parts_are_harmless_other_parts_give_no_decision(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        os.makedirs(os.path.join(wt, "sub"))
        b = f"t/{self.rng.randint(10, 99)}-{self.word()}"
        self.git(wt, "branch", b)
        for k in range(2):
            self.edit(wt, "docs/b.md")
            self.git(wt, "stash", "push", "-q", "-m", f"wip {k}")
        self.edit(wt, "src/app.py")
        self.edit(wt, "src/util.py")
        fields = {"b": b, "new": f"t/{self.rng.randint(100, 999)}-{self.word()}"}
        allowed = (
            "git status && git reset --hard",
            "git log --oneline -3 && git reset --hard",
            "git diff --stat; git restore src/app.py",
            "git stash list && git stash clear",
            "git branch && git branch -D {b}",
        )
        undecided = (
            "git reset --hard && git pull",
            "git branch -D {b} && git branch {new}",
            "git diff > {w}.patch && git checkout .",
        )
        store = self.global_store()
        for k, shape in enumerate(allowed + undecided):
            command = shape.format(w=self.word(), **fields)
            with self.subTest(command=command):
                before, there = self.snapshots(), self.state(wt)
                p = self.run_gate(command, wt, main)
                if k < len(allowed):
                    self.expect_backup(p, before, store, [], command)
                else:
                    self.expect_snapshot_only(p, before, store, [], command)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
        for command in ("git status", "git branch", "git status && git log -1 && git diff --stat"):
            with self.subTest(command=command, alone=True):
                before, refs = self.snapshots(), self.backup_refs(wt)
                self.expect_none(self.run_gate(command, wt, main), before, refs, command, wt)


class GitTargets(GitGate):
    """Review of PR #361, finding 1: a move to another commit, or paths taken from one, overwrite the untracked and
    ignored files where that commit tracks a path, and git stash create holds none of them."""

    def target(self, wt, branch):
        """A branch made from `wt`'s HEAD whose one commit tracks tests/test_<w>.py and data/seed-<w>.csv (forced in
        under the ignored data/); `wt` is back on `branch` without them. Returns (its name, its sha, {kind: the
        local path that collides with it, also the pathspec naming it})."""
        name = f"t/{self.rng.randint(10, 99)}-target-{self.word()}"
        w = self.word()
        tests, seed = f"tests/test_{w}.py", f"data/seed-{w}.csv"
        self.git(wt, "switch", "-q", "-c", name)
        for rel in (tests, seed):
            self.put(os.path.join(wt, rel), data=self.text())
        self.git(wt, "add", "-f", "--", seed)
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-q", "-m", "Target")
        sha = self.rev(wt)
        self.git(wt, "switch", "-q", branch)
        for rel in (tests, seed):
            self.assertFalse(os.path.lexists(os.path.join(wt, rel)), rel)
        return name, sha, {"untracked": tests, "ignored": seed}

    def test_untracked_paths_the_target_tracks_get_nothing(self):
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        tgt, tc, kinds = self.target(wt, "t/1-x")
        head = self.rev(wt)
        mine = {"src/app.py": self.edit(wt, "src/app.py")}
        self.put(os.path.join(wt, f"notes-{self.word()}.txt"))                    # untracked, where the target has none
        self.put(os.path.join(wt, f"scratch-{self.word()}.tmp"))                  # ignored, likewise
        both = {("head", None): head, ("changes", None): self.changes_of(wt, head, mine)}
        changes = {("changes", None): self.changes_of(wt, head, mine)}
        forms = (
            (f"git reset --hard {tgt}", both),
            (f"git checkout -f {tgt}", changes),
            ("git checkout {tc} -- {spec}", changes),
            ("git restore --source={tc} {spec}", changes),
        )
        # An untracked file where the target tracks one, for every form; an ignored one, for the reset.
        for kind, shapes in (("untracked", forms), ("ignored", forms[:1])):
            local = kinds[kind]
            path = os.path.join(wt, local)
            data = self.put(path, data=self.text())
            self.assertEqual(self.ignored(wt, local), kind == "ignored", local)
            self.assertFalse(self.tracked(wt, local), local)
            for shape, _want in shapes:
                command = shape.format(tc=tc, spec=local)
                with self.subTest(command=command, collides=kind):
                    before, refs, there = self.snapshots(), self.backup_refs(wt), self.state(wt)
                    self.expect_none(self.run_gate(command, wt, main), before, refs, command, wt)
                    self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
            self.assertEqual(self.read(path), data)
            remove(os.path.join(wt, local.split("/")[0]))

        # No untracked or ignored path where the target tracks one: backed up and allowed, as before.
        for shape, want in forms:
            command = shape.format(tc=tc, spec=kinds["untracked"])
            with self.subTest(command=command, collides=None):
                before, there = self.snapshots(), self.state(wt)
                self.expect_refs(self.run_gate(command, wt, main), before, command, wt, wt, want)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")

        # What the review found: the real reset overwrites the untracked file, and nothing holds it.
        local = os.path.join(wt, kinds["untracked"])
        data = self.put(local, data=self.text())
        self.git(wt, "reset", "-q", "--hard", tgt)
        self.assertNotEqual(self.read(local), data)

    def test_a_clean_after_a_form_that_changes_the_ignore_rules_gets_nothing(self):
        """Review of PR #361, finding 1 and fix rounds 2 and 3: a git clean after a reset to another commit, or after a
        form that puts back a .gitignore the worktree has changed (edited on disk; for restore --staged --worktree,
        which takes HEAD's, staged as well), removes what its dry run saw ignored: no decision, no snapshot, no ref.
        The pair with nothing changed, a clean before the form, and a path form whose pathspec leaves the .gitignore
        out are backed up and allowed as before. Each refused command, run by a real shell, removes that path."""
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        log = f"run-{self.word()}.log"
        gitignore = os.path.join(wt, ".gitignore")
        self.put(gitignore, data=self.read(gitignore) + (log + "\n").encode())
        self.git(wt, "commit", "-q", "-am", "Ignore the run log")                 # HEAD ignores it, HEAD~1 does not
        head = self.rev(wt)
        at_head = self.read(gitignore)

        def fresh(change):
            """The worktree at HEAD with nothing untracked, then a source edit, an untracked file, an ignored one and
            the run log; with `change` ("edited", or "staged" as well), the root .gitignore also ignoring reports/, a
            folder on disk. Returns (the refs a reset keeps, those a path form keeps, the untracked file, the path
            the clean after a form that changes the ignore rules removes)."""
            self.git(wt, "reset", "-q", "--hard", head)
            self.git(wt, "clean", "-q", "-f", "-d", "-x")
            edits, index, lost = {"src/app.py": self.edit(wt, "src/app.py")}, {}, log
            notes = os.path.join(wt, f"notes-{self.word()}.txt")
            self.put(notes)
            self.put(os.path.join(wt, f"scratch-{self.word()}.tmp"))
            self.put(os.path.join(wt, log), data=self.text())
            if change:
                lost = "reports-" + self.word()
                edits[".gitignore"] = self.put(gitignore, data=at_head + (lost + "/\n").encode())
                self.fill(os.path.join(wt, lost), files=2)
                if change == "staged":
                    self.git(wt, "add", "--", ".gitignore")
                    index[".gitignore"] = edits[".gitignore"]
            changes = {("changes", None): self.changes_of(wt, head, edits, index)}
            return {**changes, ("head", None): head}, changes, notes, lost

        cases = (
            # (the .gitignore change, command, the refs kept: "reset", "path", or None for nothing)
            (None, "git reset --hard && git clean -fd", "reset"),
            (None, "git reset --hard HEAD~1 && git clean -fd", None),                  # HEAD~1 does not ignore the log
            (None, "git clean -fd && git reset --hard HEAD~1", "reset"),               # the clean runs first
            (None, "git restore --staged --worktree . && git clean -fd", "path"),
            ("edited", "git reset --hard && git clean -fd", None),                     # HEAD's .gitignore comes back
            ("edited", "git checkout -- src && git clean -fd", "path"),                # the pathspec leaves it out
            ("staged", "git restore --staged --worktree . && git clean -fd", None),    # HEAD's, not the index's
        )
        for change, command, kept in cases:
            with self.subTest(command=command, gitignore=change):
                reset, path, notes, lost = fresh(change)
                before, refs, there = self.snapshots(), self.backup_refs(wt), self.state(wt)
                p = self.run_gate(command, wt, main)
                self.assertEqual(self.state(wt), there, f"{command!r}: the hook changed the worktree")
                if kept:
                    self.expect_refs(p, before, command, wt, wt, reset if kept == "reset" else path, paths=[notes])
                    continue
                self.expect_none(p, before, refs, command, wt)
                # What the review found: the clean's dry run sees `lost` ignored, and the command removes it.
                folder = os.path.join(wt, lost)
                files = [os.path.join(lost, n) for n in os.listdir(folder)] if os.path.isdir(folder) else [lost]
                self.assertTrue(files and all(self.ignored(wt, f) for f in files), f"{lost} is not ignored now")
                argv = [SHELL, "-f", "-c", command] if os.path.basename(SHELL) == "zsh" else [SHELL, "-c", command]
                subprocess.run(argv, cwd=wt, env=self.env(), capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
                self.assertFalse(os.path.lexists(folder), f"{command!r} left {lost} (seed {self.seed})")


# ================================================================ failing open
class FailOpen(GateTest):
    def plugin(self, lib):
        """hooks/ copied from the checkout next to a lib/ that is missing, raises at import, or is the real one."""
        root = os.path.join(self.tmp, f"plugin-{lib}")
        shutil.copytree(HOOKS_DIR, os.path.join(root, "hooks"), ignore=shutil.ignore_patterns("__pycache__"))
        if lib == "raises":
            pkg = os.path.join(root, "lib", "harness_lib")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write(f"raise RuntimeError({self.token!r})\n")
        return os.path.join(root, "hooks", "backup_gate.py")

    def one_line(self, p, *needles):
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "", "a decision")
        self.assertNotIn("Traceback", p.stderr)
        lines = p.stderr.splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertTrue(lines[0].startswith("backup_gate.py:"), lines[0])
        for needle in needles:
            self.assertIn(needle, lines[0])

    def test_fails_open_without_the_lib(self):
        self.token = "gate-" + self.word()
        main = self.make_project()
        wt = self.add_worktree(main, "t-1-x", "t/1-x")
        target = self.fill(os.path.join(wt, "victim-" + self.word()))
        stdin = json.dumps(self.event(f"rm -rf {os.path.basename(target)}", wt))
        for lib, needles in (("missing", ("harness_lib",)), ("raises", ("harness_lib", self.token))):
            with self.subTest(lib=lib):
                before = self.snapshots()
                self.one_line(self.run_raw(stdin, main, hook=self.plugin(lib)), *needles)
                self.assertEqual(self.snapshots() - before, set())
        with self.subTest(error="an internal crash"):
            before = self.snapshots()
            p = self.run_raw(stdin, main, env={"BACKUP_GATE_TEST_TOKEN": self.token},
                             argv=[sys.executable, "-B", "-c", CRASH_DRIVER, HOOK])
            self.one_line(p, self.token)
            self.assertEqual(self.snapshots() - before, set())
        with self.subTest(error="a store that cannot be written"):
            store = self.global_store()
            self.put(store, data=b"not a folder\n")
            before = self.snapshots()
            self.one_line(self.run_raw(stdin, main))
            self.assertEqual(self.snapshots() - before, set())
            with open(store, "rb") as fh:
                self.assertEqual(fh.read(), b"not a folder\n")
            self.assertTrue(os.path.isdir(target))


# ================================================================ registration
class Registration(unittest.TestCase):
    def test_hooks_json_registers_it_with_a_30s_timeout(self):
        self.assertTrue(os.path.isfile(HOOK), HOOK)
        with open(HOOKS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        found = []
        for event, entries in data["hooks"].items():
            for entry in entries:
                for k, hook in enumerate(entry["hooks"]):
                    words = [str(hook.get("command", ""))] + [str(a) for a in hook.get("args", [])]
                    if any("backup_gate.py" in w for w in words):
                        found.append((event, entry.get("matcher"), k, hook, entry))
        self.assertEqual(len(found), 1, found)
        event, matcher, k, hook, entry = found[0]
        self.assertEqual((event, matcher), ("PreToolUse", "Bash"))
        self.assertEqual([hook.get("type"), hook.get("command"), hook.get("args"), hook.get("timeout")],
                         ["command", "python3", ["${CLAUDE_PLUGIN_ROOT}/hooks/backup_gate.py"], 30])
        names = [os.path.basename(str((h.get("args") or [""])[0])) for h in entry["hooks"]]
        self.assertIn("tests_first.py", names)
        self.assertGreater(k, names.index("tests_first.py"), names)


if __name__ == "__main__":
    unittest.main()
