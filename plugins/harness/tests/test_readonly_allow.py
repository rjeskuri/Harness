"""Tests for plugins/harness/hooks/readonly_allow.py (ticket #392, epic #383): allow read-only Bash without the
auto-mode classifier.

On 2026-11-16 about 55 Bash calls were refused with `classifier:auto-mode:unavailable`. The hook (PreToolUse,
matcher Bash, last in hooks.json's list, timeout 10) answers allow, reason `read-only`, when every part of the
command (branch_guard's walk(), each part in the directory it runs in) is read-only, and no decision (nothing on
stdout, exit 0) otherwise. It never answers deny or ask, so a deny from another hook still wins.

- Allowed parts: cd, pwd, true, echo, printf, test, [; the readers cat, head, wc, ls, grep, egrep, fgrep, cut, tr,
  tail (not -f or -F), sort (not -o), uniq (at most one operand) and sed -n whose only script is <n>p, <n>,<m>p or
  <n>,$p; git status, log, diff, show, rev-parse, rev-list, merge-base, ls-files, ls-tree, blame, grep, stash list,
  worktree list and a branch listing, with no -c, --git-dir, --work-tree or assignment in front and no --output,
  -o or --ext-diff; the nine harness bin tools.
- The whole command: output redirections only to /dev/null or a descriptor; no heredoc, sh -c, eval, xargs or
  assignment prefix; no $ or backtick in a reader's or git's operands (a harness tool's "$PWD" is fine).
- A reader's file operand and a git pathspec resolve inside the session's project (the main checkout of
  CLAUDE_PROJECT_DIR, else of the event's cwd, and its worktrees), ~/.claude/harness/ or a temp directory ($TMPDIR,
  /tmp, /private/tmp), and are neither secret-shaped (.env, .env.*, *.pem, *.key, credentials/) nor matched by
  HARNESS_SENSITIVE_PATHS (the main checkout's .claude/settings.json).
- No decision in plan mode or with HARNESS_READONLY_ALLOW=off (the environment first, then the main checkout's
  .claude settings). Without its lib, or on its own error, it fails open: exit 0, nothing on stdout.

Fixtures (guards_support.GuardTestCase): a main checkout on main, a ticket worktree under its .claude/worktrees/ and
a second worktree beside the main checkout's folder; the sensitive folder and extension are random per run and set
only in the main checkout's .claude/settings.json (on disk, so the worktrees do not carry it). HOME is a folder that
does not exist, outside every temp directory: on Linux CI tempfile puts the fixtures under /tmp, and ~/.ssh/id_rsa
must stay outside the allowed places there too. Table rows run in this process (gitrepo_support.run_in_process,
with tempfile's cached temp directory cleared so the hook reads the row's TMPDIR as its own process would); only
test_hook_process_contract starts a process, the way hooks.json runs the hook.

Rulings (test-author, reversible):
- A reader's operand need not exist (`tail -40 /tmp/t364-own-tests.log`, `cat ~/.claude/harness/...` under a HOME
  that does not exist): reading a missing file reads nothing.
- `sed -n '3,$p'` is allowed: that $ is sed's last-line address (the outline lists `<n>,$p`), not an expansion.
- `git -C <worktree>` is allowed: the outline bars only -c, --git-dir, --work-tree and assignments in front.
- A path is judged secret or sensitive relative to its own working tree (a worktree's file read from the main
  checkout too), as backup_gate judges a removal's target.

Run: python3 -m unittest discover -s plugins/harness/tests -p test_readonly_allow.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from guards_support import HOOKS, GuardTestCase, setUpModule, tearDownModule  # noqa: F401

PLUGIN = HOOKS.parent
HOOK_NAME = "readonly_allow.py"
HOOK = HOOKS / HOOK_NAME
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "read-only",
    }
}
MODES_BUT_PLAN = ("default", "auto", "acceptEdits", "bypassPermissions")

# The commands refused on 2026-11-16 while the classifier was down. {wt} is the ticket worktree.
OUTAGE_SHAPES = (
    "cd {wt} && git status --short && git diff HEAD -- plugins/harness/tests | head -250",
    "grep -n x plugins/harness/bin/harness-red | head -40",
    "sed -n 80,200p plugins/harness/bin/harness-red",
    'harness-work attempt 365 code pass && harness-red run --worktree "$PWD" 2>&1 | tail -5',
    "tail -40 /tmp/t364-own-tests.log",
)

# (command, where it runs): main checkout, ticket worktree, or the worktree beside the main checkout's folder.
ALLOWED = (
    # the shell's own
    ("pwd", "wt"),
    ("true", "main"),
    ("echo checking {tok}", "wt"),
    ("printf '%s\\n' {tok}", "main"),
    ("test -f README.md", "wt"),
    ("[ -d src ]", "main"),
    ("cd src && pwd", "main"),
    # readers
    ("cat README.md", "main"),
    ("head -n 5 src/app.py", "wt"),
    ("wc -l README.md src/app.py", "main"),
    ("ls", "wt"),
    ("ls -la src", "main"),
    ("grep -rn login src", "wt"),
    ("egrep -n 'login|return' src/app.py", "main"),
    ("fgrep -n hello README.md", "wt"),
    ("cut -d: -f1 README.md", "main"),
    ("cat README.md | tr a-z A-Z", "wt"),
    ("tail -n 3 README.md", "main"),
    ("sort README.md", "wt"),
    ("sort -r src/app.py | uniq", "main"),
    ("uniq -c README.md", "wt"),
    ("sed -n 2p src/app.py", "main"),
    ("sed -n 1,2p README.md", "wt"),
    ("sed -n '3,$p' plugins/harness/bin/harness-red", "main"),
    ("cd src && cat app.py", "wt"),
    ("cat README.md", "side"),
    # git
    ("git status", "main"),
    ("git log --oneline -5 -- src/app.py", "wt"),
    ("git diff", "main"),
    ("git diff HEAD -- src", "wt"),
    ("git show --stat HEAD", "main"),
    ("git rev-parse HEAD", "wt"),
    ("git rev-list --count HEAD", "main"),
    ("git merge-base HEAD main", "wt"),
    ("git ls-files src", "main"),
    ("git ls-tree HEAD", "wt"),
    ("git blame README.md", "main"),
    ("git grep -n login", "wt"),
    ("git stash list", "main"),
    ("git worktree list", "wt"),
    ("git branch", "main"),
    ("git branch -a", "wt"),
    ("git branch --list 't/*'", "main"),
    ("git -C {side} log --oneline -3", "main"),
    ("git status --short", "side"),
    # the nine harness tools
    ("harness-board show 392 --markdown", "main"),
    ("harness-digest", "main"),
    ("harness-pr status 57", "wt"),
    ("harness-projects", "main"),
    ("harness-red run --worktree {wt}", "main"),
    ("harness-scaffold --help", "wt"),
    ("harness-standup", "main"),
    ("harness-work budget status", "wt"),
    ("harness-worklog --help", "main"),
    # several parts, and redirects to /dev/null or a descriptor
    ("git log --oneline | head -3", "wt"),
    ("ls src && cat README.md", "main"),
    ("grep -c login src/app.py || true", "wt"),
    ("cat README.md; git status", "main"),
    ("cat README.md 2>/dev/null", "wt"),
    ("ls >/dev/null 2>&1", "main"),
    ("git status --short 2>&1 | head -20", "wt"),
    ("cat README.md 1>&2", "main"),
    # where a read may go: the project's worktrees, the temp directories, ~/.claude/harness/
    ("cat {side}/README.md", "main"),
    ("cat {wt}/src/app.py", "main"),
    ("cat {tmpdir}/run-{tok}.log", "wt"),
    ("tail -20 /private/tmp/harness-{tok}.log", "main"),
    ("cat ~/.claude/harness/runs/{tok}.json", "wt"),
    ("ls ~/.claude/harness/", "main"),
)

NO_DECISION = (
    # writes and unknown programs
    ("rm README.md", "wt"),
    ("rm -rf build", "main"),
    ("python3 x.py", "wt"),
    ("python3 -m unittest discover -s tests", "main"),
    ("curl -s https://example.com", "wt"),
    ("sed -i '' 's/hello/bye/' README.md", "wt"),
    ("sed -i.bak 's/hello/bye/' README.md", "main"),
    ("sed 's/hello/bye/' README.md", "wt"),
    ("sed -n 's/hello/bye/w out.txt' README.md", "main"),
    ("sort -o sorted.txt README.md", "wt"),
    ("cat README.md | tee copy.txt", "main"),
    ("uniq README.md out.txt", "wt"),
    ("tail -f README.md", "main"),
    ("tail -F README.md", "wt"),
    ("touch x.txt", "main"),
    ("cp README.md copy.md", "wt"),
    ("find . -name '*.py'", "main"),
    ("npm test", "wt"),
    ("make", "main"),
    ("harness-deploy now", "wt"),
    ("cat README.md && rm src/app.py", "wt"),
    ("echo $(rm -rf build)", "main"),
    # redirects to a file
    ("cat README.md > copy.txt", "wt"),
    ("echo x >> README.md", "main"),
    ("ls 2> err.log", "wt"),
    ("git status > status.txt", "main"),
    ("harness-board show 392 > board.txt", "wt"),
    # git: options in front, writing options, and subcommands that change things
    ("git -c core.pager=cat log --oneline", "wt"),
    ("git -c user.name=x status", "main"),
    ("git --git-dir=.git log", "wt"),
    ("git --work-tree=. status", "main"),
    ("git diff --output=diff.txt", "wt"),
    ("git log -p --ext-diff", "main"),
    # git grep -O<pager> runs the pager on the matches: refused in any spelling, its value stuck or in a cluster
    ("git grep -O'touch /tmp/x' login", "wt"),
    ("git grep -nO./x.sh -e login", "main"),
    ("git push", "wt"),
    ("git push origin main", "main"),
    ("git commit -m x", "wt"),
    ("git checkout -- .", "wt"),
    ("git branch new-{tok}", "main"),
    ("git branch -D t/366-side", "main"),
    ("git stash", "wt"),
    ("git worktree add ../w2", "main"),
    ("git fetch", "wt"),
    # heredocs, child shells, eval, xargs, assignment prefixes
    ("cat <<EOF\nhi {tok}\nEOF", "wt"),
    ("sh -c 'cat README.md'", "main"),
    ("bash -c ls", "wt"),
    ("eval ls", "main"),
    ("ls | xargs cat", "wt"),
    ("LC_ALL=C sort README.md", "main"),
    ("GIT_PAGER=cat git log", "wt"),
)

SECRET_SENSITIVE_OUTSIDE = (
    # secret-shaped
    ("cat .env", "main"),
    ("cat .env", "wt"),
    ("cat .env.local", "main"),
    ("cat credentials/x", "main"),
    ("grep -rn token credentials", "main"),
    ("cat certs/server.pem", "wt"),
    ("head keys/deploy.key", "main"),
    ("cat README.md .env", "main"),
    ("cat README.md && cat .env", "wt"),
    ("git log -p -- .env", "main"),
    # HARNESS_SENSITIVE_PATHS from the main checkout's settings: {vault}/**, *.{ext}
    ("cat {vault}/notes.txt", "main"),
    ("cat {vault}/notes.txt", "wt"),
    ("grep -rn x {vault}", "main"),
    ("head data/cache.{ext}", "wt"),
    # a recursive grep whose walk reaches a file matched by *.{ext} (src beside it holds none: allowed above)
    ("grep -rn rows data", "wt"),
    ("cat {wt}/{vault}/notes.txt", "main"),
    ("git diff HEAD -- {vault}/notes.txt", "wt"),
    # outside the project, ~/.claude/harness/ and the temp directories
    ("cat ~/.ssh/id_rsa", "main"),
    ("cat ~/.claude/settings.json", "wt"),
    ("cat /etc/hosts", "main"),
    ("ls /etc", "wt"),
    ("cd /etc && cat hosts", "main"),
    # $ or a backtick in a reader's or git's operands
    ("cat $F", "main"),
    ('cat "$HOME/.ssh/id_rsa"', "wt"),
    ("grep -n x $FILE", "main"),
    ("cat `echo README.md`", "wt"),
    ("git log -- $X", "main"),
    ("git diff $(git merge-base HEAD main)", "wt"),
    # a substitution in a harness tool's words reads too, in the folder the tool runs in (not after a later cd)
    ('harness-board show 1 "$(< .env)"', "main"),
    ('harness-board show 1 "`cat {vault}/notes.txt`"; cd src', "main"),
)

# The same shapes on paths that are none of those: allowed, so the rows above turn on the path alone.
PATH_CONTROLS = (
    ("cat {vault}-old/notes.txt", "main"),
    ("head data/cache.txt", "wt"),
    ("cat certs/README.md", "main"),
    ("git diff HEAD -- src/app.py", "wt"),
    ("cd src && cat app.py", "main"),
)


# Pinned by mutation triage (#477, epic #439): each row is a command a mutant of readonly_allow.py allowed while the
# hook gives no decision. (Rows that depend on the environment or a special fixture are the tests below the class.)
TRIAGE_NO_DECISION = (
    # a word with a glob or a leading = names no known file, wherever a reader or git takes one
    ("cat src/*.py", "main"),
    ("ls *.md", "wt"),
    ("cat =ls", "main"),
    ("wc --lines src/*.py", "wt"),
    ("grep -n hello src/*.py", "wt"),
    ("sed -n 2p src/*.py", "main"),
    ("cat < src/*.py", "main"),
    ("cd * && pwd", "main"),
    ("grep -f .en* README.md", "main"),
    ("git grep -f '.en*' login", "main"),
    ("git ls-files '*.py'", "main"),
    ("git log -- 'src/*.py'", "main"),
    ("git -C '*' status", "main"),
    ("git grep -e login -- 'src/*.py'", "main"),
    ("git grep --regexp=login -- 'src/*.py'", "main"),
    ("git grep --file=README.md -- 'src/*.py'", "main"),
    ("cat ~+/README.md", "main"),
    # options that write, run a program or list what git would not
    ("sort --out=sorted.txt README.md", "wt"),
    ("tail --fol README.md", "wt"),
    ("git grep --open-files=sh login", "main"),
    ("git grep --untracked login", "main"),
    ("git grep --no-exclude-standard login", "main"),
    ("git ls-files -o", "main"),
    ("git", "main"),
    ("git worktree prune", "main"),
    ("git -C /etc status", "main"),
    ("cat README.md | uniq - out.txt", "wt"),
    ("sed 2p README.md", "main"),
    ("sed -n -e 1p -e 3p README.md", "main"),
    ("sed -n 2p $HOME/.ssh/id_rsa", "main"),
    # cd that does not move the shell where the walker puts it
    ("cd src | cat app.py", "main"),
    ("cat README.md | cd src", "main"),
    ("cd src data && cat app.py", "main"),
    ("cd nosuchdir && cat README.md", "main"),
    # a program by path, nothing to run, a bare grep (it searches its own folder) and grep -r under a pipe
    ("/bin/cat README.md", "main"),
    ("./cat README.md", "main"),
    ("# only a note", "main"),
    ("grep TOKEN", "main"),
    ("cat README.md | grep -rn hello", "main"),
    ("cat README.md | grep --recursive hello", "main"),
    ("cat README.md | grep --dereference-recursive hello", "main"),
    ("cat README.md | grep -d recurse hello", "main"),
    ("cat README.md | grep --directories=recurse hello", "main"),
    ("cat README.md | grep --directories recurse hello", "main"),
    # a redirection that only redirects, which split_command leaves out
    ("pwd; < README.md", "main"),
    ("(cat) < README.md", "main"),
)

# The same refusals on secret-shaped, sensitive or outside paths: the file an option or an operand names is judged.
TRIAGE_SECRET = (
    ("head --lines=2 .env", "main"),
    ("head -n5 .env", "main"),
    ("cut -d: -f1 .env", "main"),
    ("grep -f .env README.md", "main"),
    ("grep --file=.env README.md", "main"),
    ("grep --exclude-from=.env pat README.md", "main"),
    ("grep --include-from=.env pat README.md", "main"),
    ("git diff .env", "main"),
    ("git blame .env", "main"),
    ("git show HEAD:.env", "main"),
    ("git show HEAD:{vault}/notes.txt", "main"),
    ("git blame --contents=.env README.md", "main"),
    ("git log -L1,5:.env", "main"),
    ("pwd; < .env", "main"),
    ("(cat) < .env", "main"),
    ("cd {vault} && ls", "main"),
)

# What the rows above are not: the same commands with nothing wrong in them.
TRIAGE_CONTROLS = (
    ("cat README.md | grep hello", "main"),
    ("grep -n hello src/app.py", "wt"),
    ("grep -f README.md src/app.py", "main"),
    ("head --lines=2 README.md", "main"),
    ("head -n5 README.md", "wt"),
    ("cut -d: -f1 README.md", "wt"),
    ("sed -n 2p README.md", "wt"),
    ("sed -n -e 2p README.md", "main"),
    ("git diff README.md", "main"),
    ("git ls-files -- src", "main"),
    ("git show HEAD:src/app.py", "main"),
    ("git log -L1,5:src/app.py", "main"),
    ("git blame --contents=README.md README.md", "main"),
    ("git worktree list --porcelain", "main"),
    ("git grep -e login -- src", "main"),
    ("git -C {wt} status", "main"),
    ("cd src && ls", "main"),
)


class ReadonlyAllowTests(GuardTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.tok = uuid.uuid4().hex[:8]
        # Never created, and outside the project and every temp directory, on macOS and on Linux alike.
        self.env["HOME"] = f"/nonexistent/harness-readonly-allow-{self.tok}"
        self.vault = f"vault-{self.tok}"
        self.ext = f"x{self.tok[:4]}"
        self.main = self.build_project()
        self.wt = self.main / ".claude" / "worktrees" / "t-365-guard"
        self.git(self.main, "worktree", "add", "-q", "-b", "t/365-guard", str(self.wt))
        self.side = self.tmp / f"side-{self.tok}"
        self.git(self.main, "worktree", "add", "-q", "-b", "t/366-side", str(self.side))
        self.tmpdir = Path(self.env["TMPDIR"])
        self.write(self.tmpdir / f"run-{self.tok}.log", "ok\n")
        self.places = {"main": self.main, "wt": self.wt, "side": self.side}
        self.names = {
            "wt": str(self.wt),
            "side": str(self.side),
            "tok": self.tok,
            "vault": self.vault,
            "ext": self.ext,
            "tmpdir": str(self.tmpdir),
        }
        self.addCleanup(self.forget_copies)

    def build_project(self) -> Path:
        main = self.tmp / "proj"
        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True, capture_output=True, env=self.env)
        files = {
            "README.md": f"hello {self.tok}\n",
            "src/app.py": "def login():\n    return 'ok'\n",
            "data/cache.txt": "cache\n",
            f"data/cache.{self.ext}": "rows\n",
            "certs/README.md": "certificates\n",
            "certs/server.pem": "-----BEGIN CERTIFICATE-----\n",
            f"{self.vault}/notes.txt": "private\n",
            f"{self.vault}-old/notes.txt": "public\n",
            "plugins/harness/bin/harness-red": "".join(f"line {i} x\n" for i in range(250)),
            "plugins/harness/tests/test_red.py": "def test_red():\n    pass\n",
            ".gitignore": ".claude/worktrees/\n.claude/settings.json\n.env\n.env.*\ncredentials/\n*.key\n",
        }
        for rel, text in files.items():
            self.write(main / rel, text)
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "Initial commit")
        # Untracked, so only the main checkout has them on disk.
        self.write(main / ".env", f"TOKEN={self.tok}\n")
        self.write(main / "credentials" / "x", "secret\n")
        self.write(main / "keys" / "deploy.key", "key\n")
        self.write_settings(main, {"HARNESS_SENSITIVE_PATHS": f"{self.vault}/**, *.{self.ext}"})
        return main

    def write_settings(self, repo: Path, env: dict, name: str = "settings.json") -> None:
        self.write(repo / ".claude" / name, json.dumps({"env": env}) + "\n")

    def forget_copies(self) -> None:
        """Drop modules imported from the plugin copies under this test's folder (branch_guard among them), so
        no later run in this process picks them up instead of the checkout's."""
        root = str(self.tmp) + os.sep
        for name, module in list(sys.modules.items()):
            path = getattr(module, "__file__", None)
            if isinstance(path, str) and os.path.abspath(path).startswith(root):
                del sys.modules[name]

    # ---- running the hook

    def run_allow(self, command: str, cwd: str = "main", mode: str = "default", env: dict | None = None,
                  project: str | None = "main", script: Path | None = None):
        """The hook on a Bash `command` whose event cwd is `cwd`, run in this process; CLAUDE_PROJECT_DIR is the
        place `project` names (None: unset)."""
        script = script or HOOK
        self.assertTrue(script.is_file(), f"{script} does not exist")
        data = self.payload("Bash", {"command": command}, self.places[cwd])
        data["permission_mode"] = mode
        extra = {} if project is None else {"CLAUDE_PROJECT_DIR": str(self.places[project])}
        extra.update(env or {})
        with mock.patch.object(tempfile, "tempdir", None):
            return self.run_in_process(script, json.dumps(data), env=extra)

    def assert_allow(self, r, label: str) -> None:
        self.assertEqual((r.returncode, r.stderr), (0, ""), label)
        self.assertNotEqual(r.stdout.strip(), "", f"{label}: no decision; want allow")
        self.assertEqual(json.loads(r.stdout), ALLOW, label)

    def assert_no_decision(self, r, label: str, quiet: bool = True) -> None:
        """Exit 0 and nothing on stdout; never a deny or an ask. `quiet`: nothing on stderr either."""
        self.assertEqual(r.returncode, 0, f"{label}: exit {r.returncode}; stderr {r.stderr!r}")
        decision = None
        if r.stdout.strip():
            try:
                decision = json.loads(r.stdout).get("hookSpecificOutput", {}).get("permissionDecision")
            except (ValueError, AttributeError):
                decision = None
        self.assertNotIn(decision, ("deny", "ask"), f"{label}: answered {decision}; the hook never denies or asks")
        self.assertEqual(r.stdout, "", f"{label}: want no decision (nothing on stdout)")
        if quiet:
            self.assertEqual(r.stderr, "", label)

    def check(self, rows, allow: bool, **kwargs) -> None:
        for command, cwd in rows:
            command = command.format(**self.names)
            label = f"{command!r} in {cwd}"
            with self.subTest(command=command, cwd=cwd, **{k: str(v) for k, v in kwargs.items()}):
                r = self.run_allow(command, cwd, **kwargs)
                if allow:
                    self.assert_allow(r, label)
                else:
                    self.assert_no_decision(r, label)

    # ---- the tests

    def test_allows_read_only_commands(self):
        outage = [(c, where) for c in OUTAGE_SHAPES for where in ("main", "wt")]
        self.check(outage + list(ALLOWED), allow=True)
        # CLAUDE_PROJECT_DIR unset: the event's cwd names the project, from a worktree too.
        unset = outage + [("cat README.md && cat {wt}/src/app.py && git -C {wt} status --short", "side")]
        self.check(unset, allow=True, project=None)

    def test_no_decision_for_writes_unknowns_and_redirects(self):
        self.check(NO_DECISION, allow=False)

    def test_no_decision_for_secret_sensitive_or_outside_paths(self):
        self.check(SECRET_SENSITIVE_OUTSIDE, allow=False)
        self.check(PATH_CONTROLS, allow=True)
        # A grep over ~/.claude/harness (HOME the fixture's real home folder here) must not walk into the global
        # backup store, whose snapshots keep each file as files/<k>: a backed-up .env is not secret by its name.
        self.write(self.home / ".claude/harness/backups" / f"20261118T101500Z-{self.tok[:6]}" / "files" / "0",
                   f"TOKEN={self.tok}\n")
        self.check([("grep -rn TOKEN ~/.claude/harness", "main")], allow=False, env={"HOME": str(self.home)})

    def test_no_decision_for_spellings_a_mutant_would_allow(self):
        self.check(TRIAGE_NO_DECISION, allow=False)
        self.check(TRIAGE_SECRET, allow=False)
        self.check(TRIAGE_CONTROLS, allow=True)

    def test_cdpath_makes_a_relative_cd_unplaceable(self):
        # With CDPATH set, `cd src` may land in a folder of CDPATH: the walker could not follow it.
        env = {"CDPATH": str(self.tmp)}
        self.check([("cd src && cat app.py", "main"), ("cd src; ls", "wt")], allow=False, env=env)
        self.check([("cd ./src && cat app.py", "main"), (f"cd {self.wt} && pwd", "main")], allow=True, env=env)
        self.check([("cd src && cat app.py", "main")], allow=True)

    def test_no_decision_outside_a_repository(self):
        # No main checkout to judge paths against (the event's cwd is no repository, CLAUDE_PROJECT_DIR unset).
        bare = self.tmp / f"bare-{self.tok}"
        self.write(bare / "notes.txt", "hi\n")
        self.places["bare"] = bare
        rows = [("cat notes.txt", "bare"), ("pwd", "bare"), ("cat /etc/hosts", "bare")]
        self.check(rows, allow=False, project=None)

    def test_folder_grep_refuses_symlinks_and_huge_folders(self):
        self.write(self.main / "links" / "real.txt", "x\n")
        os.symlink("../README.md", self.main / "links" / "pointer")
        many = self.main / "many"
        for k in range(21):
            (many / f"d{k}").mkdir(parents=True)
            for n in range(1000):
                (many / f"d{k}" / f"f{n}").touch()
        # 21,000 entries: past the 20,000 a folder grep walks; a symlink anywhere in the folder.
        self.check([("grep -rn x links", "main"), ("grep -rn x many", "main"), ("grep x many", "main")], allow=False)
        self.check([("grep -rn login src", "main"), ("grep -n x links/real.txt", "main")], allow=True)

    def test_off_plan_mode_and_fail_open(self):
        shapes = [(c, "wt") for c in OUTAGE_SHAPES]
        for mode in MODES_BUT_PLAN:
            self.check(shapes[:2], allow=True, mode=mode)
        self.check(shapes, allow=False, mode="plan")
        self.check(shapes, allow=False, env={"HARNESS_READONLY_ALLOW": "off"})

        # Off in the main checkout's settings, read from a worktree too; the environment comes first.
        command = "git status --short && cat README.md"
        sensitive = {"HARNESS_SENSITIVE_PATHS": f"{self.vault}/**, *.{self.ext}"}
        for name in ("settings.json", "settings.local.json"):
            self.write_settings(self.main, dict(sensitive, HARNESS_READONLY_ALLOW="off"), name)
            for cwd in ("main", "wt"):
                with self.subTest(settings=name, cwd=cwd):
                    self.assert_no_decision(self.run_allow(command, cwd), f"off in {name}, from {cwd}")
            with self.subTest(settings=name, env="HARNESS_READONLY_ALLOW=on"):
                r = self.run_allow(command, "wt", env={"HARNESS_READONLY_ALLOW": "on"})
                self.assert_allow(r, f"off in {name}, on in the environment")
            (self.main / ".claude" / name).unlink()
        self.write_settings(self.main, sensitive)

        # Its own error: every json.loads raises; the hook exits 0 with nothing on stdout.
        with self.subTest(fail_open="an error of its own"):
            with mock.patch("json.loads", side_effect=RuntimeError(f"readonly-allow-test-{self.tok}")):
                r = self.run_allow(OUTAGE_SHAPES[1], "wt")
            self.assert_no_decision(r, "an error of its own", quiet=False)

        # Without its lib: a copy of hooks/ with no lib/ beside it, the checkout's lib off sys.path.
        bare = self.tmp / "plugin-without-lib"
        whole = self.tmp / "plugin-with-lib"
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(HOOKS, bare / "hooks", ignore=ignore)
        shutil.copytree(HOOKS, whole / "hooks", ignore=ignore)
        shutil.copytree(PLUGIN / "lib", whole / "lib", ignore=ignore)
        lib_dirs = {p for p in sys.path if os.path.isdir(os.path.join(p or os.curdir, "harness_lib"))}
        with self.subTest(fail_open="without its lib"):
            with mock.patch.object(sys, "path", [p for p in sys.path if p not in lib_dirs]):
                r = self.run_allow(OUTAGE_SHAPES[1], "wt", script=bare / "hooks" / HOOK_NAME)
            self.assert_no_decision(r, "without its lib", quiet=False)
        with self.subTest(fail_open="the same copy with its lib"):
            r = self.run_allow(OUTAGE_SHAPES[1], "wt", script=whole / "hooks" / HOOK_NAME)
            self.assert_allow(r, "the same copy with its lib")

    def test_hook_process_contract(self):
        data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        wired = [
            (event, entry.get("matcher"), k, len(entry.get("hooks", [])), hook)
            for event, entries in data["hooks"].items()
            for entry in entries
            for k, hook in enumerate(entry.get("hooks", []))
            if any(str(w).endswith("/" + HOOK_NAME) for w in [hook.get("command", "")] + list(hook.get("args", [])))
        ]
        self.assertEqual(len(wired), 1, f"hooks.json wires {HOOK_NAME} {len(wired)} times: {wired}")
        event, matcher, k, count, hook = wired[0]
        self.assertEqual((event, matcher, k), ("PreToolUse", "Bash", count - 1), "wired last for PreToolUse Bash")
        self.assertEqual(
            [hook.get("type"), hook.get("command"), hook.get("args"), hook.get("timeout")],
            ["command", "python3", [f"{PLUGIN_ROOT}/hooks/{HOOK_NAME}"], 10],
        )
        # Run as hooks.json runs it, within its timeout.
        argv = [sys.executable] + [a.replace(PLUGIN_ROOT, str(PLUGIN)) for a in hook["args"]]
        event_data = self.payload("Bash", {"command": OUTAGE_SHAPES[0].format(**self.names)}, self.main)
        event_data["permission_mode"] = "auto"
        r = subprocess.run(
            argv,
            input=json.dumps(event_data),
            capture_output=True,
            text=True,
            env=dict(self.env, CLAUDE_PROJECT_DIR=str(self.main), CLAUDE_PLUGIN_ROOT=str(PLUGIN)),
            cwd=str(self.tmp),
            timeout=hook["timeout"],
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(json.loads(r.stdout), ALLOW)


if __name__ == "__main__":
    unittest.main()
