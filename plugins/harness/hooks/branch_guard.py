#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit|Bash): never on main.

GitHub Free does not protect branches on private repos, so this hook is the only block
against work landing on main or master outside a pull request.

Edit, Write, NotebookEdit: the repo is the one holding the file (its nearest existing
directory). When that repo is on main or master and has a commit, the edit is refused,
except for the main checkout's own notes: a path under docs/agent/ (the handoff note and
specs the session brief reads) or a path git ignores (`git check-ignore`: .env,
.claude/settings.local.json, scratch files).

Bash: the command is split on ;, &&, ||, |, & and newlines (and inside (...), $(...),
sh -c, eval, a heredoc a shell reads and a git !alias), and each part is judged in the
directory it runs in: the hook's cwd, moved by cd and pushd (an empty target, or one that
is not a directory, leaves it where it was, unless && follows, since a failed cd stops
the chain), restored after a subshell or a child shell, and moved for git by -C,
--git-dir and --work-tree. Then:
  - every file a part writes is judged by the repo holding it, as an Edit is: a
    redirection or tee to it, cp, mv, install, ln, rsync or dd into it, mv, rm, touch,
    truncate or chmod of it, and in-place edits (sed or gsed -i, perl -i, awk -i inplace,
    ed, patch). A path under <top>/.claude/worktrees/ or in a linked worktree belongs to
    that worktree, and a path outside any repo is never refused. A named path the Edit
    rule exempts (under docs/agent/, or one git ignores) is allowed for every form, rm,
    truncate and both sides of mv included; a directory holding a tracked file is not
    ignored. The exemption judges what the write reaches: never for a word with an
    unquoted glob (*, ?, [) or brace expansion, nor for a path whose first component in
    the repo is .git, nor for a spelling that differs in case from a tracked file it
    names (git ls-files with :(icase) says); a symlink is exempt only when the file it
    resolves to is exempt or lies outside every protected main working tree. The
    refusal names the first target that is not exempt;
  - when the repo git runs in is on main or master with a commit: git commit, merge
    (except --ff-only), rebase, cherry-pick, revert, am, apply, reset, rm, mv, clean,
    restore, stash pop|apply, checkout of paths or -B main, switch -C main, branch -f, -m
    or -C onto main, -D main, update-ref and symbolic-ref writes to HEAD or main, and a
    push with no refspec, of HEAD, or deleting a remote branch. Aliases given with
    -c alias.<name>= are expanded first;
  - when the part runs in such a repo: python -c, node -e, or a program either reads from
    a heredoc, that writes files, unless it has a path literal (a quoted string starting
    with /, ~/, ./ or ../, holding a /, or a bare name with an extension) and every one,
    resolved against the part's directory with ~ expanded, lies outside every protected
    main working tree. A literal joined to a value known only at run time does not
    count as outside: after + or += with a non-literal before it, an argument of
    os.path.join or path.join with a non-literal before it, or an f-string or template
    literal with an interpolation before a /;
  - in every repo, on every branch: a force push (--force, -f, --force-with-lease or a
    +refspec), and a push that names main or master as the destination (main,
    HEAD:main, refs/heads/main, :main), --mirror, --all, or a matching (:) or wildcard
    refspec that reaches main.
Reading, mkdir, git fetch, pull, switch, checkout <branch>, worktree, gh, harness-* and
the project's CLAUDE_VERIFY_CMD run.

Allowed always: a linked worktree, a detached HEAD, a repo with no commits, a path outside
any repo, and any repo whose HARNESS_ALLOW_MAIN is 1 (environment first, then `env` in the
repo's .claude/settings.json, then .claude/settings.local.json).

The shell walker (Part, split_command, command_words, stdin_program and their tables) comes
from harness_lib.shellwalk, shared with readonly_bash.py. tests_first.py imports this
module for walk(), writes(), git_split(), git_paths(), code_writes() and the helpers and
writer tables behind them, the walker's names included; importing it has no side effects.

A refusal prints {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": "..."}} and exits 0. Allowing
prints nothing. Fails open: any error of its own goes to stderr in one line, exit 0.
Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.hookio import deny, edited_path, hook_cwd, read_input, run_fail_open
    from harness_lib.paths import canonical_path, same_entry
    from harness_lib.shellwalk import (
        ASSIGNMENT,
        GIT_VALUE_OPTIONS,
        MAX_DEPTH,
        PYTHON,
        SED_IN_PLACE,
        SHELLS,
        Part,
        closing,
        command_words,
        split_command,
        stdin_program,
    )
    from harness_lib.settings import setting, settings_env
except Exception as exc:  # fail open, like the __main__ wrapper below: one line, exit 0
    sys.stderr.write(f"branch_guard.py: cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}\n")
    sys.exit(0)

MAX_SHOWN = 160
GIT_TIMEOUT = 3
PROTECTED = {"main", "master"}
WORKTREES = ".claude/worktrees/"
ADVICE = (
    "Work happens on a ticket branch: harness-work start <issue#> for a ticket, or "
    "git switch -c t/<n>-<slug> for untracked work."
)
FORCE_ADVICE = (
    "Force pushes are refused on every branch; push new commits instead, or ask the user "
    "to rewrite history by hand."
)
PUSH_ADVICE = (
    "main and master change only through a pull request: push the ticket branch and "
    "open one (harness-pr open <issue#>)."
)

NODES = {"node", "nodejs", "bun"}
NODE_VALUE_OPTIONS = set(
    "-r --require --import --loader --input-type -C --conditions".split()
)
PUSH_VALUE_OPTIONS = set("-o --push-option --repo --receive-pack --exec".split())
PUSH_EVERYTHING = {"--mirror", "--all", "--branches"}
CHECKOUT_VALUE_OPTIONS = {"-b", "-B", "--orphan"}
TOUCH_VALUE_OPTIONS = {"-t", "-d", "-r", "-A"}
COPY_VALUE_OPTIONS = {"-t", "-S", "--target-directory", "--suffix"}
INSTALL_VALUE_OPTIONS = set(
    "-m -o -g -t -S -B -f -M --mode --owner --group --target-directory --suffix".split()
)
TRUNCATE_VALUE_OPTIONS = {"-s", "-r", "--size", "--reference"}
RSYNC_VALUE_OPTIONS = set(
    "-e -f -T -B -M --rsh --filter --exclude --include --exclude-from --include-from "
    "--files-from --temp-dir --partial-dir --backup-dir --suffix --chmod --chown "
    "--log-file --password-file --compare-dest --copy-dest --link-dest --timeout "
    "--port --remote-option --rsync-path".split()
)
# patch (GNU and BSD): the short options that take a value, every long option (getopt
# accepts any unambiguous prefix of one) and the long options that take a value.
PATCH_SHORT_VALUES = set("BDFVYdgioprxz")
PATCH_LONG_OPTIONS = (
    "--backup --backup-if-mismatch --basename-prefix --batch --binary --check "
    "--context --debug --directory --dry-run --ed --follow-symlinks --force --forward "
    "--fuzz --get --help --ifdef --ignore-whitespace --input --merge "
    "--no-backup-if-mismatch --normal --output --posix --prefix --quiet --quoting-style "
    "--read-only --reject-file --reject-format --remove-empty-files --reverse "
    "--set-time --set-utc --silent --strip --suffix --unified --verbose --version "
    "--version-control".split()
)
PATCH_LONG_VALUES = set(
    "--basename-prefix --debug --directory --fuzz --get --ifdef --input --output "
    "--prefix --quoting-style --read-only --reject-file --reject-format --strip "
    "--suffix --version-control".split()
)
STASH_VALUE_OPTIONS = {"-m", "--message", "--pathspec-from-file"}
AWK_VALUE_OPTIONS = set(
    "-i -f -v -F -l -e -E --include --file --assign --field-separator --load --source "
    "--exec".split()
)
AWK_PROGRAM_OPTIONS = {"-f", "-e", "-E", "--file", "--source", "--exec"}
CHMOD_OPTION_LETTERS = set("fhvRHLPcECNiI")
# Commands whose files, when xargs supplies them, are unknown: the directory they run
# in is what they change.
XARGS_WRITERS = set("rm touch chmod truncate tee ed".split())

SHORT_CLUSTER = re.compile(r"-[A-Za-z0-9]+$")
REMOTE_PATH = re.compile(r"[^/]*:")
# Inline programs that write files (as in readonly_bash.py; creating a directory is
# allowed, as mkdir is).
OPEN_CALL = re.compile(r"\bopen\s*\(")
WRITE_MODE = re.compile(r"""(?:,|\bmode\s*=)\s*(["'])([rwxabt+]{1,4})\1""")
PATH_OPEN = re.compile(
    r"""\.open\s*\(\s*(?:mode\s*=\s*)?(["'])[rbt]*[wax+][rwxabt+]*\1"""
)
PY_WRITERS = re.compile(
    r"\.(write_text|write_bytes|unlink|rmdir|touch|symlink_to)\s*\("
    r"|\b(os|shutil)\.(remove|unlink|rmdir|removedirs|rename|renames|replace"
    r"|truncate|chmod|chown|symlink|link|rmtree|move|copy|copy2|copyfile"
    r"|copytree)\s*\("
)
JS_WRITERS = re.compile(
    r"\b(writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream"
    r"|copyFile|copyFileSync|cpSync)\s*\("
    r"|\.(cp|rename|renameSync|rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync|truncate"
    r"|truncateSync|symlink|symlinkSync)\s*\("
    r"|\bBun\.write\s*\("
)
# Path literals in an inline program: a quoted string (backslash escapes kept) that
# starts with /, ~/, ./ or ../, holds a /, or is a bare file name with an extension.
STRING_LITERAL = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\\\n])*)\1""")
BARE_FILE = re.compile(r"[A-Za-z0-9_.-]+[.][A-Za-z0-9]{1,8}$")
# A path literal joined at run time: after + or +=, an argument of os.path.join or
# path.join, or an f-string or template literal with an interpolation before a /.
STRING_PREFIX = re.compile(r"(?<![A-Za-z0-9_])[rRbBuUfF]{1,2}$")
JOIN_CALL = re.compile(r"\b(?:os\.)?path\.join\s*\(")
INTERPOLATION = re.compile(r"\{[^{}]*\}")
INTERPOLATION_BEFORE_SLASH = re.compile(r"\{[^{}]*\}.*/")
# What the shell expands in an unquoted word: a glob or a brace expansion. In the
# unquoted spelling of a word, each quoted or escaped character is a NUL.
SHELL_EXPANSION = re.compile(r"[*?[]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
WORD_BREAKS = " \t\r\n;|&()<>"


def shown(text: str) -> str:
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "…"


# ---------- settings ----------


def allow_main(root: Path | None) -> bool:
    """HARNESS_ALLOW_MAIN is 1 in the environment or in `root`'s project settings."""
    file_env = settings_env(root) if root is not None else {}
    return setting("HARNESS_ALLOW_MAIN", file_env) == "1"


# ---------- the repo at a directory ----------


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT,
    )


def existing_dir(path: str) -> str:
    """`path` itself when it is a directory, else its nearest existing ancestor."""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def toplevel(directory: str | None) -> Path | None:
    """The top-level of the working tree holding `directory`, or None outside a repo."""
    if not directory or not os.path.isdir(directory):
        return None
    out = git(directory, "rev-parse", "--show-toplevel")
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def protected_repo(directory: str) -> tuple[Path, str] | None:
    """(top-level, branch) when `directory` is in the main working tree of a repo that is
    on main or master and has a commit; None otherwise (not a repo, a linked worktree,
    detached, another branch, no commits yet)."""
    out = git(
        directory,
        "rev-parse",
        "--show-toplevel",
        "--absolute-git-dir",
        "--git-common-dir",
    )
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    if out.returncode != 0 or len(lines) < 3 or not all(lines[:3]):
        return None
    top, git_dir, common = lines[:3]
    common_dir = os.path.join(directory, common)  # unchanged when already absolute
    # git_dir comes in the on-disk spelling, common_dir in the spelling of `directory`
    if canonical_path(git_dir) != canonical_path(common_dir):
        return None  # a linked worktree: its branch is never main
    head = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    branch = head.stdout.strip()
    if head.returncode != 0 or branch not in PROTECTED:
        return None  # detached, or another branch
    if git(directory, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return None  # no commits yet: the first commit has to land somewhere
    return Path(top), branch


# ---------- Edit, Write, NotebookEdit ----------

NOTES_DIR = "docs/agent/"


def resolved(path: str) -> str:
    """The absolute `path` with its nearest existing directory canonical (symlinks
    resolved, each component spelled as on disk); the rest stays as spelled."""
    base = existing_dir(os.path.dirname(os.path.abspath(path)))
    tail = os.path.relpath(os.path.abspath(path), base)
    return os.path.normpath(os.path.join(canonical_path(base), tail))


def repo_relative(path: str, top: Path) -> str | None:
    """`path` relative to the repo root `top`, with / separators; None when outside it.
    Both sides are canonical, so symlinked and other-case spellings still match."""
    rel = os.path.relpath(resolved(path), canonical_path(str(top)))
    if rel in (".", "..") or rel.startswith("../"):
        return None
    return rel.replace(os.sep, "/")


def notes_or_ignored(path: str, top: Path) -> bool:
    """True for a path under docs/agent/, or one git ignores (untracked and matched by
    an ignore rule)."""
    rel = repo_relative(path, top)
    if rel is None:
        return False
    if rel.startswith(NOTES_DIR):
        return True
    return git(str(top), "check-ignore", "-q", "--", rel).returncode == 0


def tracked_otherwise_spelled(path: str, top: Path, rel: str) -> bool:
    """True when git tracks `path` (or a file under it) by a spelling of `rel` that
    differs in case and names the same file: another spelling of a tracked file on a
    case-insensitive file system, which check-ignore may report as ignored."""
    listed = git(str(top), "ls-files", "-z", "--", f":(icase,literal){rel}").stdout
    for entry in listed.split("\0"):
        spelling = entry[: len(rel)]
        if entry and spelling != rel and same_entry(os.path.join(str(top), spelling), path):
            return True
    return False


def check_edit(data: dict, cwd: str) -> None:
    raw = edited_path(data)
    if not isinstance(raw, str) or not raw.strip():
        return
    path = os.path.join(cwd, os.path.expanduser(raw))
    found = protected_repo(existing_dir(os.path.dirname(path)))
    if found is None:
        return
    top, branch = found
    if allow_main(top) or notes_or_ignored(path, top):
        return
    deny(f"On {branch} in {top.name}. {ADVICE}")


# ---------- where each part runs ----------


def resolve_dir(cwd: str | None, target: str) -> str | None:
    """The directory `git -C target` (or --work-tree) names: `cwd` for an empty target,
    as git treats it; None when unknown (unexpanded variables)."""
    if cwd is None:
        return None
    target = os.path.expandvars(os.path.expanduser(target))
    if "$" in target or "`" in target:
        return None
    if not target:
        return cwd
    return os.path.normpath(os.path.join(cwd, target))


def cd_target(cwd: str | None, target: str, sep: str) -> str | None:
    """Where `cd target` leaves the shell. An empty target is a no-op, and one that is not
    a directory (yet) makes cd fail, which leaves the shell where it was, unless && follows:
    then nothing after a failed cd runs, and the commands that do run are in `target`
    (made earlier in the command). None when unknown (`cd -`, unexpanded variables)."""
    if target == "-":
        return None
    path = resolve_dir(cwd, target)
    if path is None or path == cwd or os.path.isdir(path):
        return path
    return path if sep == "&&" else cwd


def shell_script(name: str, args: list[str], part: Part) -> str | None:
    """The program a shell, or eval, runs: its -c string or the heredoc it reads."""
    if name == "eval":
        return " ".join(args)
    if name not in SHELLS:
        return None
    flags = [
        k
        for k, a in enumerate(args)
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]
    ]
    if flags:
        return args[flags[0] + 1] if flags[0] + 1 < len(args) else None
    if stdin_program(args) and part.heredocs:
        return "\n".join(part.heredocs)
    return None


def walk(state: dict, command: str, depth: int = 0):
    """Yields (part, name, args) for each simple command in `command` in the order the
    shell runs it, with state["cwd"] set to the directory it runs in (None when unknown).
    cd and pushd move it; a subshell, a substitution and a child shell (sh -c, eval, a
    heredoc a shell reads, a git !alias) put it back when they end."""
    if depth > MAX_DEPTH:
        return
    saved: list = []
    for part in split_command(command):
        if part.marker == "(":
            saved.append(state["cwd"])
            continue
        if part.marker == ")":
            if saved:
                state["cwd"] = saved.pop()
            continue
        words = command_words(part.words)
        name = os.path.basename(words[0]) if words else ""
        args = words[1:]
        yield part, name, args
        if name in ("cd", "pushd"):
            plain = plain_args(args)
            target = plain[0] if plain else "~"
            state["cwd"] = cd_target(state["cwd"], target, part.sep)
            continue
        script = shell_script(name, args, part)
        if script is None and name == "git":
            _, sub, rest = git_split(args, state["cwd"])
            if sub.startswith("!"):
                script = " ".join([sub[1:]] + rest)
        if script is not None:
            before = state["cwd"]
            yield from walk(state, script, depth + 1)
            state["cwd"] = before  # a child shell's cd stays in the child


# ---------- arguments ----------


def plain_args(
    args: list[str], value_options: frozenset | set = frozenset()
) -> list[str]:
    """The non-option arguments; everything after `--` counts."""
    found, k = [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            found.extend(args[k:])
            break
        if a in value_options:
            k += 1
        elif not a.startswith("-") or a == "-":
            found.append(a)
    return found


def short_flag(args: list[str], letter: str) -> bool:
    return any(
        SHORT_CLUSTER.match(a) and not a.startswith("--") and letter in a[1:]
        for a in args
    )


def option_value(args: list[str], names: tuple) -> str | None:
    """The value given to the first of the options `names` (`-B x`, `--long=x`)."""
    for k, a in enumerate(args):
        if a == "--":
            return None
        if a in names:
            return args[k + 1] if k + 1 < len(args) else ""
        for name in names:
            if name.startswith("--") and a.startswith(name + "="):
                return a[len(name) + 1 :]
    return None


def absolute(path: str, cwd: str | None) -> str | None:
    """`path` made absolute against `cwd` and normalized; None when it cannot be known."""
    if not path:
        return None
    path = os.path.expandvars(os.path.expanduser(path))
    if "$" in path or "`" in path:
        return None  # unexpanded: not established
    if not os.path.isabs(path):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


# ---------- git ----------


def git_split(args: list[str], cwd: str | None) -> tuple[str | None, str, list[str]]:
    """(directory git runs in, subcommand, its arguments). -C, --work-tree and --git-dir
    move the directory; an alias given with -c alias.<name>=<value> is expanded (a !alias
    comes back as the subcommand "!<command>"). The directory is None when unknown."""
    k = 0
    git_dir = work_tree = None
    aliases: dict = {}
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a in GIT_VALUE_OPTIONS:
            value = args[k + 1] if k + 1 < len(args) else ""
            k += 2
        elif a.startswith(("--git-dir=", "--work-tree=")):
            a, value = a.split("=", 1)
            k += 1
        else:
            k += 1
            continue
        if a == "-C":
            cwd = resolve_dir(cwd, value)
        elif a == "--git-dir":
            git_dir = resolve_dir(cwd, value)
        elif a == "--work-tree":
            work_tree = resolve_dir(cwd, value)
        elif a == "-c":
            key, _, expansion = value.partition("=")
            if key.lower().startswith("alias."):
                aliases[key[len("alias.") :].lower()] = expansion
    where = cwd
    if work_tree is not None:
        where = work_tree
    elif git_dir is not None:
        where = (
            os.path.dirname(git_dir) if os.path.basename(git_dir) == ".git" else git_dir
        )
    if k >= len(args):
        return where, "", []
    sub, rest = args[k], args[k + 1 :]
    expansion = aliases.get(sub.lower())
    if expansion is not None:
        if expansion.startswith("!"):
            return where, expansion, rest
        try:
            words = shlex.split(expansion)
        except ValueError:
            words = expansion.split()
        if words:
            sub, rest = words[0], words[1:] + rest
    return where, sub, rest


def branch_name(ref: str) -> str:
    """The branch a ref names: refs/heads/main and heads/main are main."""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def push_target_refusal(rest: list[str]) -> str | None:
    """Why `git push <rest>` would update or delete main or master, from any branch."""
    for a in rest:
        option = a.split("=", 1)[0]
        if option in PUSH_EVERYTHING:
            return f"git push {option} pushes every branch, main included"
    deleting = "--delete" in rest or short_flag(rest, "d")
    for spec in plain_args(rest, PUSH_VALUE_OPTIONS)[1:]:  # the first is the remote
        spec = spec.lstrip("+")
        if spec == ":":
            return "git push : pushes every matching branch, main included"
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        name = branch_name(dst)
        if name in PROTECTED:
            verb = "deletes" if deleting or spec.startswith(":") else "updates"
            return f"git push {spec} {verb} {name}"
        if "*" in dst and any(
            fnmatch.fnmatchcase(ref, dst)
            for ref in ("main", "master", "refs/heads/main", "refs/heads/master")
        ):
            return f"git push {spec} reaches main"
    return None


def push_refusal(rest: list[str], branch: str) -> str | None:
    """Why `git push <rest>` would update or delete what it must not, on `branch`."""
    why = push_target_refusal(rest)
    if why:
        return why
    if "--delete" in rest or short_flag(rest, "d"):
        return "git push --delete deletes a remote branch"
    refspecs = plain_args(rest, PUSH_VALUE_OPTIONS)[1:]
    if not refspecs:
        return f"git push without a refspec pushes {branch}"
    for spec in refspecs:
        spec = spec.lstrip("+")
        if spec.startswith(":"):
            return "a git push refspec starting with : deletes a remote branch"
        if spec in ("HEAD", "@"):
            return f"git push {spec} updates {branch}"
    return None


def is_force_push(sub: str, rest: list[str]) -> bool:
    if sub != "push":
        return False
    for a in rest:
        if a in ("--force", "--force-with-lease") or a.startswith(
            "--force-with-lease="
        ):
            return True
    if short_flag(rest, "f"):
        return True
    return any(a.startswith("+") for a in plain_args(rest, PUSH_VALUE_OPTIONS)[1:])


def dry_run(rest: list[str]) -> bool:
    return "--dry-run" in rest or short_flag(rest, "n")


def index_only(rest: list[str]) -> bool:
    """git restore that touches only the index (--staged without --worktree)."""
    staged = "--staged" in rest or short_flag(rest, "S")
    return staged and not ("--worktree" in rest or short_flag(rest, "W"))


def checkout_paths(rest: list[str], where: str | None) -> list[str]:
    """The paths `git checkout <rest>` restores: after --, after the tree-ish, or a lone
    argument that names an existing file or directory."""
    if "--" in rest:
        return rest[rest.index("--") + 1 :]
    plain = plain_args(rest, CHECKOUT_VALUE_OPTIONS)
    if len(plain) >= 2:
        return plain[1:]
    if (
        len(plain) == 1
        and plain[0] != "-"
        and where is not None
        and os.path.exists(os.path.join(where, plain[0]))
    ):
        return plain
    return []


def stash_action(rest: list[str]) -> str:
    """The git stash subcommand `rest` runs: push when it starts with an option or is
    empty."""
    return rest[0] if rest and not rest[0].startswith("-") else "push"


def git_paths(sub: str, rest: list[str], where: str | None) -> list[str]:
    """The working-tree paths git `sub` `rest` names to write, relative to `where`:
    checkout and restore of paths, rm, mv, stash push and clean of paths (dry runs and
    index-only forms write none)."""
    if sub == "checkout":
        return checkout_paths(rest, where)
    if sub == "restore" and not index_only(rest):
        return plain_args(rest, {"-s", "--source"})
    if sub == "rm" and not dry_run(rest) and "--cached" not in rest:
        return plain_args(rest)
    if sub == "mv" and not dry_run(rest):
        return plain_args(rest)
    if sub == "stash" and stash_action(rest) == "push":
        given = rest[1:] if rest[:1] == ["push"] else rest
        return plain_args(given, STASH_VALUE_OPTIONS)
    if sub == "clean" and not dry_run(rest):
        return plain_args(rest, {"-e", "--exclude"})
    return []


def branch_refusal(rest: list[str]) -> str | None:
    plain = plain_args(rest)
    if "--delete" in rest or short_flag(rest, "d") or short_flag(rest, "D"):
        hit = [b for b in plain if b in PROTECTED]
        return f"git branch -D {hit[0]} deletes {hit[0]}" if hit else None
    if "--move" in rest or short_flag(rest, "m") or short_flag(rest, "M"):
        hit = [b for b in plain[:2] if b in PROTECTED]
        if hit:
            return f"git branch -m renames or overwrites {hit[0]}"
    if "--copy" in rest or short_flag(rest, "c") or short_flag(rest, "C"):
        if plain and plain[-1] in PROTECTED:
            return f"git branch -C overwrites {plain[-1]}"
    if ("--force" in rest or short_flag(rest, "f")) and plain[:1]:
        if plain[0] in PROTECTED:
            return f"git branch -f {plain[0]} moves {plain[0]}"
    return None


def checkout_refusal(rest: list[str], branch: str, where: str | None) -> str | None:
    reset = option_value(rest, ("-B",))
    if reset is not None and branch_name(reset) in PROTECTED:
        return f"git checkout -B {reset} resets {reset}"
    if "-p" in rest or "--patch" in rest or checkout_paths(rest, where):
        return f"git checkout of paths changes files on {branch}"
    return None


def touches_head(ref: str) -> bool:
    return ref == "HEAD" or branch_name(ref) in PROTECTED


def git_refusal(
    sub: str, rest: list[str], branch: str, where: str | None
) -> str | None:
    """Why git `sub` `rest` would change `branch` (main or master), or None."""
    plain = plain_args(rest)
    if sub in ("commit", "rebase", "cherry-pick", "revert", "am", "reset"):
        return f"git {sub} changes {branch}"
    if sub == "merge" and "--ff-only" not in rest:
        return f"git merge without --ff-only changes {branch}"
    if sub == "stash" and plain[:1] in (["pop"], ["apply"]):
        return f"git stash {plain[0]} changes files on {branch}"
    if sub == "push":
        return push_refusal(rest, branch)
    if sub == "branch":
        return branch_refusal(rest)
    if sub == "apply":
        reads = {"--check", "--stat", "--numstat", "--summary"}
        if not (reads & set(rest)) or "--apply" in rest:
            return f"git apply changes files on {branch}"
    if sub in ("rm", "mv", "clean") and not dry_run(rest):
        if not (sub == "rm" and "--cached" in rest):
            return f"git {sub} changes files on {branch}"
    if sub == "restore" and not index_only(rest):
        return f"git restore changes files on {branch}"
    if sub == "checkout":
        return checkout_refusal(rest, branch, where)
    if sub == "switch":
        reset = option_value(rest, ("-C", "--force-create"))
        if reset is not None and branch_name(reset) in PROTECTED:
            return f"git switch -C {reset} resets {reset}"
    if sub == "update-ref":
        refs = plain_args(rest, {"-m"})
        if "--stdin" in rest:
            return f"git update-ref --stdin can move {branch}"
        if refs and touches_head(refs[0]):
            return f"git update-ref {refs[0]} moves {branch}"
    if sub == "symbolic-ref":
        refs = plain_args(rest, {"-m"})
        writes = len(refs) >= 2 or "-d" in rest or "--delete" in rest
        if writes and refs and touches_head(refs[0]):
            return f"git symbolic-ref {refs[0]} repoints {branch}"
    return None


# ---------- files a part writes ----------


def sed_files(args: list[str]) -> list[str]:
    """The files `sed args` reads (and, with -i, rewrites): the plain arguments after the
    script, which is the first plain argument unless -e or -f gives it. A lone -i
    followed by '' or .ext takes that word as BSD's backup suffix."""
    files: list[str] = []
    script, k = False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files.extend(args[k:])
            break
        if a in ("-e", "--expression", "-f", "--file"):
            script, k = True, k + 1
        elif a.startswith(("--expression=", "--file=")):
            script = True
        elif a in ("-l", "--line-length"):
            k += 1
        elif a.startswith("-") and len(a) > 1 and not a.startswith("--"):
            letters = a[1:]
            for j, ch in enumerate(letters):
                last = j == len(letters) - 1
                if ch in "ef":
                    script = True
                    k += 1 if last else 0
                    break
                if ch == "i":
                    if last and k < len(args) and (
                        args[k] == "" or args[k].startswith(".")
                    ):
                        k += 1
                    break
                if ch == "l":
                    k += 1 if last else 0
                    break
        elif not a.startswith("--") and a != "-":
            files.append(a)
    return files if script else files[1:]


def perl_files(args: list[str]) -> tuple[bool, list[str]]:
    """(whether `perl args` edits in place, the files it is given)."""
    in_place = code = False
    files: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files = args[k:]
            break
        if not a.startswith("-") or a == "-":
            files = args[k - 1 :]
            break
        letters, j = a[1:], 0
        while j < len(letters):
            ch = letters[j]
            last = j == len(letters) - 1
            if ch == "i":
                in_place = True  # the rest of the word is the backup suffix
                break
            if ch in "eE":
                code = True
                k += 1 if last else 0
                break
            if ch in "IMm":
                k += 1 if last else 0
                break
            if ch in "xdDCFV":
                break  # the rest of the word is its value
            j += 1
            if ch in "0l":
                while j < len(letters) and letters[j].isdigit():
                    j += 1
    return in_place, (files if code else files[1:])


def awk_files(args: list[str]) -> tuple[bool, list[str]]:
    """(whether `awk args` loads gawk's inplace extension, the files it is given)."""
    in_place = program = False
    plain: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            plain.extend(args[k:])
            break
        if a in AWK_VALUE_OPTIONS:
            value = args[k] if k < len(args) else ""
            k += 1
            if a in ("-i", "--include") and value in ("inplace", "inplace.awk"):
                in_place = True
            program = program or a in AWK_PROGRAM_OPTIONS
        elif a in ("-iinplace", "-iinplace.awk", "--include=inplace"):
            in_place = True
        elif a.startswith(("--file=", "--source=", "--exec=")) or (
            len(a) > 2 and a[:2] in ("-f", "-e", "-E")
        ):
            program = True
        elif not a.startswith("-") or a == "-":
            plain.append(a)
    files = plain if program else plain[1:]
    return in_place, [f for f in files if not ASSIGNMENT.match(f)]


def chmod_files(args: list[str]) -> list[str]:
    """The files `chmod args` changes: the plain arguments after the mode (which may look
    like an option, as in chmod -x)."""
    files: list[str] = []
    mode = False
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            rest = args[k:]
            files.extend(rest if mode else rest[1:])
            break
        if a == "--reference":
            mode, k = True, k + 1
        elif a.startswith("--"):
            mode = mode or a.startswith("--reference=")
        elif a.startswith("-") and len(a) > 1 and set(a[1:]) <= CHMOD_OPTION_LETTERS:
            continue
        elif not mode:
            mode = True
        else:
            files.append(a)
    return files


def long_option(word: str, names) -> str:
    """The long option in `names` that `word` (no =value) spells out or abbreviates
    unambiguously; `word` itself otherwise."""
    if word in names:
        return word
    found = [n for n in names if n.startswith(word)]
    return found[0] if len(found) == 1 else word


def patch_args(args: list[str]) -> tuple[str | None, str | None, list[str], bool]:
    """(the -d directory, the -o file, the file arguments, whether it is a dry run) for
    `patch args`. Short options cluster (-Rp1) and a value letter takes the rest of the
    word or the next one; long options may be abbreviated and take a value after = or
    as the next word; everything after -- is a file argument."""
    directory = output = None
    files: list[str] = []
    dry = False
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            files.extend(args[k:])
            break
        if a.startswith("--"):
            word, eq, value = a.partition("=")
            word = long_option(word, PATCH_LONG_OPTIONS)
            if word in PATCH_LONG_VALUES and not eq:
                value = args[k] if k < len(args) else ""
                k += 1
            if word == "--directory":
                directory = value
            elif word == "--output":
                output = value
            elif word in ("--dry-run", "--check"):
                dry = True
        elif a.startswith("-") and a != "-":
            letters = a[1:]
            for j, ch in enumerate(letters):
                if ch == "C":  # BSD's --check
                    dry = True
                if ch in PATCH_SHORT_VALUES:
                    value = letters[j + 1 :]
                    if not value:
                        value = args[k] if k < len(args) else ""
                        k += 1
                    if ch == "d":
                        directory = value
                    elif ch == "o":
                        output = value
                    break
        else:
            files.append(a)
    return directory, output, files, dry


def copy_writes(name: str, args: list[str], cwd: str | None, options: set) -> list:
    """(destination as written, path written) for cp, install, ln and mv: the
    destination, or the name each source gets inside it when it is a directory."""
    target = option_value(args, ("-t", "--target-directory"))
    plain = plain_args(args, options)
    if target is not None:
        dest, sources, into = target, plain, True
    elif name == "ln" and len(plain) == 1:
        dest, sources, into = ".", plain, True
    elif len(plain) >= 2:
        dest, sources = plain[-1], plain[:-1]
        full = absolute(dest, cwd)
        into = (
            dest.endswith("/")
            or len(sources) > 1
            or (full is not None and os.path.isdir(full))
        )
    else:
        return []
    if not into:
        return [(dest, dest)]
    return [
        (dest, os.path.join(dest, os.path.basename(s.rstrip("/")) or s))
        for s in sources
    ]


def rsync_dest(args: list[str]) -> list[str]:
    plain: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            plain.extend(args[k:])
            break
        if a in RSYNC_VALUE_OPTIONS or (
            SHORT_CLUSTER.match(a) and not a.startswith("--") and a.endswith("e")
        ):
            k += 1
        elif not a.startswith("-"):
            plain.append(a)
    if len(plain) < 2:
        return []
    if REMOTE_PATH.match(plain[-1]) and not plain[-1].startswith("/"):
        return []  # host:path
    return plain[-1:]


def edits_in_place(name: str, args: list[str]) -> bool:
    if name in ("sed", "gsed"):
        return any(SED_IN_PLACE.match(a) for a in args)
    if name == "perl":
        return perl_files(args)[0]
    if name in ("awk", "gawk"):
        return awk_files(args)[0]
    return False


def file_writes(name: str, args: list[str], cwd: str | None) -> list:
    """(as written, path, why, exact) for each file `name args` creates, changes or
    removes; `exact` is False when the path is only the directory the files are in."""

    def changes(paths: list[str]) -> list:
        return [(p, p, f"{name} changes {p} inside the repo", True) for p in paths]

    if name in ("sed", "gsed"):
        if not edits_in_place(name, args):
            return []
        why = f"{name} -i edits files in place"
        return [(f, f, why, True) for f in sed_files(args)]
    if name == "perl":
        in_place, files = perl_files(args)
        why = "perl -i edits files in place"
        return [(f, f, why, True) for f in files] if in_place else []
    if name in ("awk", "gawk"):
        in_place, files = awk_files(args)
        why = f"{name} -i inplace edits files in place"
        return [(f, f, why, True) for f in files] if in_place else []
    if name in ("ed", "red"):
        return changes(plain_args(args, {"-p"}))
    if name in ("tee", "rm"):
        return changes(plain_args(args))
    if name == "touch":
        return changes(plain_args(args, TOUCH_VALUE_OPTIONS))
    if name == "truncate":
        return changes(plain_args(args, TRUNCATE_VALUE_OPTIONS))
    if name == "chmod":
        return changes(chmod_files(args))
    if name == "dd":
        return changes([a[3:] for a in args if a.startswith("of=")])
    if name == "rsync":
        return changes(rsync_dest(args))
    if name in ("cp", "mv", "ln", "install"):
        if name == "install" and short_flag(args, "d"):
            return []  # creates directories, as mkdir does
        options = INSTALL_VALUE_OPTIONS if name == "install" else COPY_VALUE_OPTIONS
        found = []
        if name == "mv":  # the sources go away
            plain = plain_args(args, options)
            target = option_value(args, ("-t", "--target-directory"))
            moved = plain if target is not None else plain[:-1]
            found.extend(changes(moved))
        for dest, path in copy_writes(name, args, cwd, options):
            found.append((dest, path, f"{name} changes {dest} inside the repo", True))
        return found
    if name == "patch":
        directory, out, files, dry = patch_args(args)
        if dry or out == "-":  # -o - writes to standard output
            return []
        # the output file, else the file to patch (a second one is the diff it reads);
        # patch changes into the -d directory before anything else
        named = [out] if out is not None else files[:1]
        if named:
            path = os.path.join(directory or "", named[0])
            return [(named[0], path, f"patch changes {named[0]} inside the repo", True)]
        where = directory or "."
        return [(where, where, f"patch changes files in {place(where)}", False)]
    return []


def place(directory: str) -> str:
    return "the current directory" if directory in (".", "./") else directory


def writes(part: Part, name: str, args: list[str], cwd: str | None) -> list:
    """(as written, absolute path or None, why, exact) for everything `part` writes:
    its redirections and the files its command changes."""
    found = [
        (t, t, f"it writes to {t} through a redirection", True) for t in part.targets
    ]
    if name:
        found.extend(file_writes(name, args, cwd))
        wrappers = part.words[: max(len(part.words) - len(args) - 1, 0)]
        if any(os.path.basename(w) == "xargs" for w in wrappers) and (
            name in XARGS_WRITERS or edits_in_place(name, args)
        ):
            why = f"xargs {name} changes files in {place('.')}"
            found.append((".", ".", why, False))
    return [(text, absolute(p, cwd), why, exact) for text, p, why, exact in found]


def python_writes(code: str) -> str | None:
    """Why a python program writes files, or None."""
    for m in OPEN_CALL.finditer(code):
        call = code[m.end() - 1 : closing(code, m.end() - 1) + 1]
        for mode in WRITE_MODE.finditer(call):
            if set(mode.group(2)) & set("wax+"):
                return "python opens a file for writing"
    if PATH_OPEN.search(code):
        return "python opens a file for writing"
    if PY_WRITERS.search(code):
        return "python writes, moves or deletes files"
    return None


def inline_programs(name: str, args: list[str], part: Part) -> list[str]:
    """The programs python or node runs from the command line (-c, -e) or a heredoc."""
    programs: list[str] = []
    python = bool(PYTHON.match(name))
    if not python and name not in NODES:
        return programs
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if python and a in ("-W", "-X"):
            k += 1
        elif not python and a in NODE_VALUE_OPTIONS:
            k += 1
        elif python and a.startswith("-c"):
            programs.append(a[2:] or (args[k] if k < len(args) else ""))
            break
        elif not python and a in ("-e", "--eval", "-p", "--print"):
            programs.append(args[k] if k < len(args) else "")
            break
        elif not python and a.startswith(("--eval=", "--print=")):
            programs.append(a.split("=", 1)[1])
            break
        elif a == "-m" or a == "-" or not a.startswith("-"):
            break
    if not programs and stdin_program(args):
        programs.extend(part.heredocs)
    return programs


def program_writes(name: str, code: str) -> str | None:
    """Why the python or node program `code` writes files, or None."""
    if PYTHON.match(name):
        return python_writes(code)
    if JS_WRITERS.search(code):
        return "node writes, moves or deletes files"
    return None


def code_writes(name: str, args: list[str], part: Part) -> str | None:
    """Why the python or node program `part` runs inline writes files, or None."""
    for code in inline_programs(name, args, part):
        why = program_writes(name, code)
        if why:
            return why
    return None


# ---------- Bash ----------


def repo_at(state: dict, directory: str | None) -> tuple[Path, str] | None:
    """protected_repo(directory), cached per command; None for an unknown directory."""
    if directory is None or not os.path.isdir(directory):
        return None
    repos = state["repos"]
    if directory not in repos:
        repos[directory] = protected_repo(directory)
    return repos[directory]


def holder(state: dict, path: str | None) -> tuple[Path, str] | None:
    """The protected repo whose main working tree holds the absolute `path`; None when
    no such repo does: outside any repo, in a linked worktree, under
    <top>/.claude/worktrees/ (ticket worktrees, judged by their own branch)."""
    if path is None or path == "/dev" or path.startswith("/dev/"):
        return None
    directory = path if os.path.isdir(path) else existing_dir(os.path.dirname(path))
    names = resolved(path).split(os.sep)
    if ".git" in names:  # git's own files belong to the working tree around them
        directory = os.sep.join(names[: names.index(".git")]) or os.sep
    found = repo_at(state, directory)
    if found is None:
        return None
    rel = os.path.relpath(resolved(path), canonical_path(str(found[0])))
    if rel == ".." or rel.startswith("../"):
        return None
    if rel.replace(os.sep, "/").startswith(WORKTREES):
        return None
    return found


def globbing_words(text: str) -> set[str]:
    """The words of the shell text `text`, quotes removed, that hold an unquoted glob
    (*, ?, [) or brace expansion ({a,b}, {a..b}): the shell expands them, so they do not
    name the files a command writes."""
    found: set[str] = set()
    word = raw = quote = ""
    started = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if (quote == "'" and c != "'") or (quote == '"' and c not in '"\\'):
            word, raw = word + c, raw + "\0"
        elif quote and c == quote:
            quote = ""
        elif quote == '"':  # a backslash inside double quotes
            if text[i + 1 : i + 2] in ('"', "\\", "$", "`"):
                i += 1
            word, raw = word + text[i : i + 1], raw + "\0"
        elif c in WORD_BREAKS:
            if started and SHELL_EXPANSION.search(raw):
                found.add(word)
            word = raw = ""
            started = False
        elif c == "\\":
            i += 1
            word, raw, started = word + text[i : i + 1], raw + "\0", True
        elif c in "'\"":
            quote, started = c, True
        else:
            word, raw, started = word + c, raw + c, True
        i += 1
    if started and SHELL_EXPANSION.search(raw):
        found.add(word)
    return found


def expanded(text: str, globbing: set[str]) -> bool:
    """True when the write target `text` comes from one of the `globbing` words (the
    word itself, or its end, as in dd's of=)."""
    return bool(text) and any(word.endswith(text) for word in globbing)


def written_exempt(state: dict, path: str, top: Path) -> bool:
    """True when a Bash write to the absolute `path` in the protected repo `top` gets the
    Edit exemption: the name is under docs/agent/ or ignored, is not under .git and is
    not another case spelling of a tracked file; and when it is a symlink, the file it
    reaches is exempt as well or lies outside every protected main working tree."""
    if not name_exempt(path, top):
        return False
    if not os.path.islink(path):
        return True
    target = os.path.realpath(path)
    found = holder(state, target)
    if found is None:
        return True
    return not os.path.islink(target) and name_exempt(target, found[0])


def name_exempt(path: str, top: Path) -> bool:
    """The name checks of written_exempt for the absolute `path` in the repo `top`."""
    rel = repo_relative(path, top)
    if rel is None or rel.split("/")[0] == ".git" or not notes_or_ignored(path, top):
        return False
    return not tracked_otherwise_spelled(path, top, rel)


def literal_spans(code: str) -> list[tuple[int, int, str, bool]]:
    """(start, end, text, template) for each quoted string in `code`: start includes a
    string prefix such as f or r, and template is True for an f-string or a backtick
    template literal."""
    spans = []
    for m in STRING_LITERAL.finditer(code):
        prefix = STRING_PREFIX.search(code, 0, m.start())
        start = prefix.start() if prefix else m.start()
        template = m.group(1) == "`" or bool(prefix and "f" in prefix.group(0).lower())
        spans.append((start, m.end(), m.group(2), template))
    return spans


def is_path_literal(text: str) -> bool:
    """A string that looks like a path: starting with /, ~/, ./ or ../, holding a /
    anywhere (which covers those), or a bare file name with an extension."""
    return "/" in text or bool(BARE_FILE.match(text))


def fixed(span: tuple[int, int, str, bool]) -> bool:
    """True for a string whose value is in the program: not a template that interpolates."""
    return not (span[3] and INTERPOLATION.search(span[2]))


def joined_at_run_time(code: str, spans: list, k: int) -> bool:
    """True when the string spans[k] of `code` is joined to a value known only at run
    time: + or += with a non-literal before it, os.path.join or path.join with a
    non-literal argument before it, or a template interpolation before a /."""
    start, _, text, template = spans[k]
    if template and INTERPOLATION_BEFORE_SLASH.search(text):
        return True
    ending = {span[1]: span for span in spans}
    before = code[:start].rstrip()
    while before.endswith(("+", "+=")):
        if before.endswith(("+=", "++")):
            return True
        before = before[:-1].rstrip()
        span = ending.get(len(before))
        if span is None or not fixed(span):
            return True
        before = code[: span[0]].rstrip()
    for m in JOIN_CALL.finditer(code):
        opened = m.end() - 1
        if not opened < start < closing(code, opened):
            continue
        rest, j = "", opened + 1
        for span in spans:
            if opened < span[0] < start:
                if not fixed(span):
                    return True
                rest += code[j : span[0]]
                j = span[1]
        rest += code[j:start]
        if rest.replace(",", "").strip():
            return True
    return False


def outside_only(state: dict, code: str, cwd: str) -> bool:
    """True when `code` has a path literal, none is joined to a value known only at run
    time, and every one, resolved against `cwd` (~ expanded), lies outside every
    protected main working tree."""
    spans = literal_spans(code)
    literals = [k for k, span in enumerate(spans) if is_path_literal(span[2])]
    if not literals:
        return False
    for k in literals:
        if joined_at_run_time(code, spans, k):
            return False
        text = spans[k][2]
        path = os.path.expanduser(text) if text.startswith("~/") else text
        if holder(state, os.path.normpath(os.path.join(cwd, path))) is not None:
            return False
    return True


def program_refusal(
    state: dict, name: str, args: list[str], part: Part, cwd: str
) -> str | None:
    """Why an inline python or node program `part` runs writes files, unless every path
    literal in it lies outside the protected repos; None when none must be refused."""
    for code in inline_programs(name, args, part):
        why = program_writes(name, code)
        if why and not outside_only(state, code, cwd):
            return why
    return None


def part_refusal(state: dict, part: Part, name: str, args: list[str]) -> tuple | None:
    """(text, why, top, branch, advice) when `part` must not run; branch is None for the
    rules that hold on every branch."""
    cwd = state["cwd"]
    where, sub, rest = git_split(args, cwd) if name == "git" else (None, "", [])
    if name == "git" and is_force_push(sub, rest):
        why = "a force push rewrites history"
        return part.text, why, toplevel(where), None, FORCE_ADVICE
    globbing = None
    for text, path, why, exact in writes(part, name, args, cwd):
        found = holder(state, path)
        if found is None:
            continue
        if exact and globbing is None:
            globbing = globbing_words(part.text)
        if not (
            exact
            and not expanded(text, globbing)
            and written_exempt(state, path, found[0])
        ):
            return part.text, why, found[0], found[1], ADVICE
    if name == "git":
        there = repo_at(state, where)
        if there is not None:
            why = git_refusal(sub, rest, there[1], where)
            if why:
                return part.text, why, there[0], there[1], ADVICE
        elif sub == "push":
            why = push_target_refusal(rest)
            if why:
                return part.text, why, toplevel(where), None, PUSH_ADVICE
    here = repo_at(state, cwd)
    if here is not None:
        why = program_refusal(state, name, args, part, cwd)
        if why:
            return part.text, why, here[0], here[1], ADVICE
    return None


def refusal(state: dict, command: str) -> tuple | None:
    """The first part of `command` to refuse, walking parts with the directory each runs
    in; None when every part may run."""
    for part, name, args in walk(state, command):
        found = part_refusal(state, part, name, args)
        if found:
            return found
    return None


def verify_command(top: Path) -> str | None:
    return setting("CLAUDE_VERIFY_CMD", settings_env(top))


def check_bash(data: dict, cwd: str) -> None:
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return
    found = refusal({"cwd": cwd, "repos": {}}, command)
    if found is None:
        return
    text, why, top, branch, advice = found
    if allow_main(top):
        return
    if branch is None:
        deny(f'Refused "{shown(text)}": {why}. {advice}')
        return
    verify = verify_command(top) if top is not None else None
    if verify and command.strip() == verify:
        return
    name = top.name if top is not None else "this repo"
    deny(f'On {branch} in {name}. Refused "{shown(text)}": {why}. {advice}')


# ---------- main ----------


def main() -> int:
    if os.environ.get("HARNESS_ALLOW_MAIN", "").strip() == "1":
        return 0
    data = read_input()
    cwd = hook_cwd(data)
    tool = data.get("tool_name")
    if tool in ("Edit", "Write", "NotebookEdit"):
        check_edit(data, cwd)
    elif tool == "Bash":
        check_bash(data, cwd)
    return 0


if __name__ == "__main__":
    # fail open: a broken guard must never trap the agent
    sys.exit(run_fail_open("branch_guard.py", main))
