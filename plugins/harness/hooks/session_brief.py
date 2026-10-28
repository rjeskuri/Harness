#!/usr/bin/env python3
"""SessionStart hook: a short brief so a new, resumed or compacted session starts oriented.

Matcher (hooks.json): startup|resume|clear|compact|fork. First, when CLAUDE_ENV_FILE and
CLAUDE_PROJECT_DIR are both set and not blank, it appends one line
`export HARNESS_SESSION_PROJECT=<CLAUDE_PROJECT_DIR, shell-quoted>` to that file (on a line of
its own, never truncating it), so the session's Bash calls, which Claude Code gives no
CLAUDE_PROJECT_DIR, know the session's project (harness_lib.settings.other_project); a file
that cannot be written is skipped silently, and the export happens even where the brief does
not. Adds at most 40 lines of context:
  ## Session brief: <repo> on <branch>, <n> uncommitted, last commit <age>: <subject>
  - on a ticket branch (t/<N>-...): Ticket #N: <title> (<status>, tier <tier>), up to six
    definition-of-done lines and Red run: <kind> at <time> (or none), from the ticket
    cache and red record in ~/.claude/harness/repos/<key>/ (no network);
  - otherwise, when the repo's origin remote is on github.com and gh (HARNESS_GH, else gh
    on PATH) answers within 3 s: Board: <r> ready, <p> in progress, <v> awaiting your
    review, <q> needs input (open issues by status label, epics not counted as ready;
    open non-draft pull requests not opened by a bot);
  - Budget: <name> exhausted until <time> (<reason>), for each budget that
    ~/.claude/harness/budgets.json marks exhausted;
  - the Next steps section of docs/agent/handoff.md (first 12 lines), else the file's
    first 20 lines; or, without a handoff, the lines starting with "Next" in the last
    `## ` block of docs/PROGRESS.md (at most 6, none from inside a code fence);
  - Latest spec: the newest file in docs/agent/specs/;
  - the first three `"passes": false` items of docs/plans/feature_list.json;
  - Hours this month: the sum of this month's rows in worklog/worklog.csv.
On startup it also names the session after the repo (sessionTitle).

When the project's .claude/settings.json declares its own SessionStart hook, it prints
no brief, so a project with a bespoke brief is not briefed twice; only when harness_lib
cannot be loaded does it still print the one harness_lib warning line there. Outside a
git repo the heading carries no git facts, and the sections are read from the directory
itself. Fails open: an error means a shorter brief or none, never a blocked session.
Without harness_lib it writes one line to stderr, puts one Warning: line naming
harness_lib in the brief, and leaves out the ticket state.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

_LIB = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
try:
    from harness_lib.gh import GH_ENV, gh_binary
    from harness_lib.ghlimit import log_call, ms_since
    from harness_lib.hookio import emit, hook_cwd, read_input, run_fail_open
    from harness_lib.paths import adopt_old_state, canonical_path, path_key, repo_key, same_entry
    from harness_lib.repo import main_checkout
    from harness_lib.settings import claude_project_dir
    from harness_lib.text import one_line, read_json

    LIB_PROBLEM = ""
except Exception as exc:  # fail open: the brief goes on without ticket state and says why
    LIB_PROBLEM = f"cannot load harness_lib from {_LIB}: {' '.join(str(exc).split())}"
    sys.stderr.write(f"session_brief.py: {LIB_PROBLEM}\n")

    # The brief must still reach the session without the lib (the guards are off then),
    # so only here does this hook keep its own copy of the hook I/O, text helpers and gh lookup it needs.
    GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
    log_call = None  # the gh call log is the lib's: without it the board's gh calls go unlogged

    def gh_binary() -> str | None:
        gh = os.environ.get("HARNESS_GH", "gh")
        if os.sep in gh:
            return gh if os.path.isfile(gh) and os.access(gh, os.X_OK) else None
        return shutil.which(gh)

    def read_json(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def one_line(text) -> str:
        return " ".join(str(text).split())

    def read_input() -> dict:
        try:
            data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def hook_cwd(data: dict) -> str:
        return str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())

    def claude_project_dir():
        return os.environ.get("CLAUDE_PROJECT_DIR") or None

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()

    def run_fail_open(name: str, main) -> int:
        try:
            code = main()
        except Exception as exc:
            sys.stderr.write(f"{name}: {exc}\n")
            return 0
        return 0 if code is None else code

MAX_LINES = 40
MAX_LINE_CHARS = 300
READ_LIMIT = 512 * 1024  # bytes read from any one source file
# Three git calls at most (rev-parse, status, log): 4 + 8 + 4 s, inside the 20 s that
# hooks.json gives this hook. The ticket and board lines add one quick git call (and the
# board two gh calls run side by side for at most 3 s), only while the brief has used
# less than EXTRAS_DEADLINE seconds.
GIT_TIMEOUT = 4
STATUS_TIMEOUT = 8
EXTRA_GIT_TIMEOUT = 2
GH_TIMEOUT = 3
EXTRAS_DEADLINE = 14

TICKET_BRANCH = re.compile(r"^t/(\d+)-")
DOD_LINES = 6
CHECKBOX_RE = re.compile(r"^(?:[-*+]\s+)?(\[[ xX]\]\s+)?")

HANDOFF = "docs/agent/handoff.md"
PROGRESS = "docs/PROGRESS.md"
SPECS = "docs/agent/specs"
FEATURES = "docs/plans/feature_list.json"
WORKLOG = "worklog/worklog.csv"

HANDOFF_SECTION_LINES = 12
HANDOFF_HEAD_LINES = 20
PROGRESS_NEXT_LINES = 6
PENDING_FEATURES = 3

# Sections the /harness:handoff skill writes. In a file that uses bold labels
# ("**Decisions**: ...") instead of headings, one of these ends the Next steps section.
HANDOFF_SECTIONS = ("goal", "done", "in progress", "decisions", "gotchas", "key files")

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
LABEL_RE = re.compile(r"^(\s*)(?:[-*+]\s+)?\*\*([^*]+?)\*\*\s*:?\s*(.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
LIST_PREFIX_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def git(
    root: Path, *args: str, timeout: float = GIT_TIMEOUT
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def repo_root(start: Path) -> Path | None:
    """Git top-level for `start`, or None outside a repo."""
    try:
        out = git(start, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def read_text(path: Path) -> str:
    with path.open("rb") as fh:
        return fh.read(READ_LIMIT).decode("utf-8", "replace")


def clip(line: str) -> str:
    line = line.rstrip()
    return (
        line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "\u2026"
    )


def nonblank(lines: list[str]) -> list[str]:
    return [ln.rstrip() for ln in lines if ln.strip()]


def declares_session_start(root: Path) -> bool:
    """True when the project's .claude/settings.json has its own SessionStart hook."""
    try:
        data = json.loads(
            (root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return bool(entries)


# ---------- heading ----------


def heading(root: Path, name: str) -> tuple[str, str | None]:
    """The heading line, and the branch (None when detached or unknown)."""
    branch, count, unborn = None, None, False
    try:
        st = git(
            root,
            "--no-optional-locks",
            "status",
            "--porcelain=v2",
            "--branch",
            timeout=STATUS_TIMEOUT,
        )
        if st.returncode == 0:
            count = 0
            for line in st.stdout.splitlines():
                if line.startswith("# branch.head "):
                    head = line[len("# branch.head ") :].strip()
                    branch = "detached HEAD" if head == "(detached)" else head
                elif line.startswith("# branch.oid "):
                    unborn = line.endswith("(initial)")
                elif line and not line.startswith("#"):
                    count += 1
    except (OSError, subprocess.SubprocessError):
        pass

    last = None
    if unborn:
        last = "no commits yet"
    else:
        try:
            lg = git(root, "log", "-1", "--format=%cr%x1f%s")
            if lg.returncode == 0 and "\x1f" in lg.stdout:
                age, subject = lg.stdout.strip("\n").split("\x1f", 1)
                last = f"last commit {age.strip()}: {subject.strip()}"
        except (OSError, subprocess.SubprocessError):
            pass

    text = f"## Session brief: {name}"
    if branch:
        text += f" on {branch}"
    facts = ([f"{count} uncommitted"] if count is not None else []) + (
        [last] if last else []
    )
    if facts:
        text += ", " + ", ".join(facts)
    return text, (None if branch == "detached HEAD" else branch)


# ---------- handoff and progress notes ----------


def norm(label: str) -> str:
    return " ".join(re.sub(r"[^a-z]+", " ", label.lower()).split())


def is_next_steps(label: str) -> bool:
    return re.match(r"next steps?\b", norm(label)) is not None


def ends_label_section(line: str, indent: int) -> bool:
    """A bold label naming another handoff section, no deeper than the Next steps label."""
    other = LABEL_RE.match(line)
    if not other or len(other.group(1)) > indent:
        return False
    return norm(other.group(2)) in HANDOFF_SECTIONS


def next_steps_section(text: str) -> list[str] | None:
    """The body of the Next steps section (heading or bold label), or None if absent."""
    lines = text.splitlines()
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        head = HEADING_RE.match(line)
        label = None if head else LABEL_RE.match(line)
        if head and is_next_steps(head.group(2)):
            level, body, fenced = len(head.group(1)), [], False
            for nxt in lines[i + 1 :]:
                if FENCE_RE.match(nxt):
                    fenced = not fenced
                elif not fenced:
                    other = HEADING_RE.match(nxt)
                    if other and len(other.group(1)) <= level:
                        break
                body.append(nxt)
            return body
        if label and is_next_steps(label.group(2)):
            indent = len(label.group(1))
            body = [label.group(3)] if label.group(3).strip() else []
            fenced = False
            for nxt in lines[i + 1 :]:
                if FENCE_RE.match(nxt):
                    fenced = not fenced
                elif not fenced and (
                    HEADING_RE.match(nxt) or ends_label_section(nxt, indent)
                ):
                    break
                body.append(nxt)
            return body
    return None


def handoff_part(root: Path) -> list[str] | None:
    """Lines from docs/agent/handoff.md, or None when the file does not exist."""
    path = root / HANDOFF
    if not path.is_file():
        return None
    text = read_text(path)
    section = next_steps_section(text)
    if section is not None:
        body = nonblank(section)[:HANDOFF_SECTION_LINES]
        return [f"Next steps ({HANDOFF}):", *body] if body else []
    head = nonblank(text.splitlines())[:HANDOFF_HEAD_LINES]
    return (
        [f"{HANDOFF} (no Next steps section; its first lines):", *head] if head else []
    )


def starts_with_next(line: str) -> bool:
    core = LIST_PREFIX_RE.sub("", line.strip(), count=1).lstrip("*_ ")
    return core.startswith("Next")


def progress_part(root: Path) -> list[str]:
    """Next lines of the last `## ` block of docs/PROGRESS.md, outside code fences."""
    path = root / PROGRESS
    if not path.is_file():
        return []
    title, picked, in_fence = None, [], False
    for line in read_text(path).splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif line.startswith("## "):
            title, picked = line[3:].strip(), []
        elif title is not None and starts_with_next(line):
            picked.append(line.strip())
    if not picked:
        return []
    return [f"{PROGRESS} ({title}):", *picked[:PROGRESS_NEXT_LINES]]


# ---------- specs, feature list, hours ----------


def latest_spec(root: Path) -> list[str]:
    folder = root / SPECS
    try:
        files = [
            p for p in folder.iterdir() if not p.name.startswith(".") and p.is_file()
        ]
    except OSError:
        return []
    if not files:
        return []

    def newest(p: Path) -> tuple[float, str]:
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:
            return (0.0, p.name)

    return [f"Latest spec: {SPECS}/{max(files, key=newest).name}"]


def describe(item: dict) -> str:
    for key in ("description", "name", "title", "feature", "summary", "id"):
        value = item.get(key)
        if (
            isinstance(value, (str, int))
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            return one_line(value)
    rest = {k: v for k, v in item.items() if k != "passes"}
    return json.dumps(rest, ensure_ascii=False)


def pending_features(root: Path) -> list[str]:
    path = root / FEATURES
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 4 * READ_LIMIT:
            return []
        items = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    if not any(isinstance(it, dict) and "passes" in it for it in items):
        return []
    pending = [it for it in items if isinstance(it, dict) and it.get("passes") is False]
    if not pending:
        return []
    shown = pending[:PENDING_FEATURES]
    return [
        f"Not passing yet ({FEATURES}, {len(pending)} of {len(items)}):",
        *(f"- {describe(it)}" for it in shown),
    ]


def hours_this_month(root: Path) -> list[str]:
    path = root / WORKLOG
    if not path.is_file():
        return []
    month = dt.date.today().strftime("%Y-%m-")
    total = 0.0
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if (row.get("date") or "").strip().startswith(month):
                try:
                    total += float((row.get("hours") or "0").strip() or 0)
                except ValueError:
                    pass
    return [f"Hours this month: {total:.2f}"]


def sections(root: Path) -> list[str]:
    lines: list[str] = []

    def add(build) -> list[str] | None:
        try:
            part = build(root)
        except Exception:  # one unreadable source never costs the rest of the brief
            return []
        if part:
            lines.extend(part)
        return part

    if add(handoff_part) is None:
        add(progress_part)
    add(latest_spec)
    add(pending_features)
    add(hours_this_month)
    return lines


# ---------- ticket, board and budgets ----------


def harness_home() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "harness"


def repo_state_dir(root: Path, cwd: Path) -> Path | None:
    """~/.claude/harness/repos/<key>, keyed by the canonical path of the main working
    tree (main_checkout); state under the key of the spelling git or `cwd` uses is moved
    there first. None without the lib: the key is harness_lib's alone."""
    if LIB_PROBLEM:
        return None
    main = main_checkout(str(root), timeout=EXTRA_GIT_TIMEOUT)
    if main is None:
        return None
    repos = harness_home() / "repos"
    return repos / adopt_old_state(str(repos), main, str(cwd))


def dod_line(item) -> str:
    """A definition-of-done item as `- [ ] text` (checkbox kept) or `- text`."""
    text = one_line(item)
    box = CHECKBOX_RE.match(text)
    rest = text[box.end() :]
    return f"- {box.group(1) or ''}{rest}"


def ticket_part(root: Path, branch: str, cwd: Path) -> list[str]:
    """Ticket and red-run lines for a t/<N>-... branch, from the cache files only."""
    number = str(int(TICKET_BRANCH.match(branch).group(1)))
    state = repo_state_dir(root, cwd)
    if state is None:
        return []
    ticket = read_json(state / "tickets" / f"{number}.json")
    lines = []
    if isinstance(ticket, dict):
        title = one_line(ticket.get("title") or "(no title)")
        status = one_line(ticket.get("status") or "status unknown")
        tier = one_line(ticket.get("tier") or "unknown")
        lines.append(f"Ticket #{number}: {title} ({status}, tier {tier})")
        sections_ = ticket.get("sections")
        dod = (
            sections_.get("definition_of_done") if isinstance(sections_, dict) else None
        )
        if isinstance(dod, list):
            lines.extend(dod_line(d) for d in dod[:DOD_LINES] if str(d).strip())
    else:
        lines.append(
            f"Ticket #{number}: not cached; harness-board show {number} fetches it."
        )
    record = read_json(state / "red" / f"{branch.replace('/', '-')}.json")
    if isinstance(record, dict) and record.get("kind"):
        when = one_line(record.get("at") or "an unknown time")
        lines.append(f"Red run: {one_line(record['kind'])} at {when}")
    else:
        lines.append("Red run: none")
    return lines


def run_side_by_side(
    cmds: list[list[str]], cwd: Path, timeout: float
) -> list[str] | None:
    """stdout of each command, run at the same time; None when one fails or the time runs
    out (then every one still running is stopped). With the lib, each command is logged
    once as a gh call (ghlimit), with exit null when it was stopped."""
    env = dict(os.environ, **GH_ENV)
    procs, started = [], []
    try:
        for cmd in cmds:
            started.append(time.monotonic())
            procs.append(
                subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + timeout
        outputs = []
        for proc in procs:
            out, _ = proc.communicate(timeout=max(0.0, deadline - time.monotonic()))
            if proc.returncode != 0:
                return None
            outputs.append(out.decode("utf-8", "replace"))
        return outputs
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        for i, proc in enumerate(procs):
            code = proc.poll()
            if code is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.communicate(timeout=1)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
            if log_call is not None:
                log_call(cmds[i][1:], cwd=cwd, exit=code, ms=ms_since(started[i]))


def label_names(issue) -> set[str]:
    labels = issue.get("labels") if isinstance(issue, dict) else None
    if not isinstance(labels, list):
        return set()
    return {str(lb.get("name")) if isinstance(lb, dict) else str(lb) for lb in labels}


def board_part(root: Path) -> list[str]:
    """Board counts from gh, when the repo's origin is on github.com and gh answers in time."""
    gh = gh_binary()
    if gh is None:
        return []
    remote = git(root, "remote", "get-url", "origin", timeout=EXTRA_GIT_TIMEOUT)
    if remote.returncode != 0 or "github.com" not in remote.stdout.lower():
        return []  # no origin, or one gh cannot read (a local path, another host)
    outputs = run_side_by_side(
        [
            [gh, "issue", "list", "--json", "labels", "--limit", "200"],
            [gh, "pr", "list", "--json", "isDraft,author", "--limit", "50"],
        ],
        root,
        GH_TIMEOUT,
    )
    if outputs is None:
        return []
    try:
        issues, prs = json.loads(outputs[0]), json.loads(outputs[1])
    except ValueError:
        return []
    if not isinstance(issues, list) or not isinstance(prs, list):
        return []
    counts = {"ready": 0, "in-progress": 0, "needs-input": 0}
    for issue in issues:
        names = label_names(issue)
        for status in counts:
            if f"status:{status}" in names and not (
                status == "ready" and "epic" in names
            ):
                counts[status] += 1
    review = 0
    for pr in prs:
        author = pr.get("author") if isinstance(pr, dict) else None
        bot = isinstance(author, dict) and bool(author.get("is_bot"))
        if isinstance(pr, dict) and pr.get("isDraft") is False and not bot:
            review += 1
    return [
        f"Board: {counts['ready']} ready, {counts['in-progress']} in progress, "
        f"{review} awaiting your review, {counts['needs-input']} needs input"
    ]


def parse_time(text: str) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()  # naive means local time


def budget_part() -> list[str]:
    """One line per budget that budgets.json marks exhausted until a time still ahead."""
    budgets = read_json(harness_home() / "budgets.json")
    if not isinstance(budgets, dict):
        return []
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    for name, entry in budgets.items():
        until = entry.get("exhausted_until") if isinstance(entry, dict) else None
        when = parse_time(until) if isinstance(until, str) else None
        if when is None or when <= now:
            continue
        reason = one_line(entry.get("reason") or "")
        line = f"Budget: {name} exhausted until {until.strip()}"
        lines.append(f"{line} ({reason})" if reason else line)
    return lines


def extras(
    git_root: Path | None, branch: str | None, started: float, cwd: Path
) -> list[str]:
    """The ticket or board lines, then the budget lines; each part fails on its own."""
    lines: list[str] = []
    late = time.monotonic() - started >= EXTRAS_DEADLINE
    try:
        if git_root is None or late:
            pass
        elif branch and TICKET_BRANCH.match(branch):
            lines.extend(ticket_part(git_root, branch, cwd))
        else:
            lines.extend(board_part(git_root))
    except Exception:  # one unreadable source never costs the rest of the brief
        pass
    try:
        lines.extend(budget_part())
    except Exception:
        pass
    return lines


# ---------- main ----------


def export_session_project() -> None:
    """Append `export HARNESS_SESSION_PROJECT=<CLAUDE_PROJECT_DIR>` to CLAUDE_ENV_FILE, which each
    later Bash call of the session sources first. Nothing when either is unset or blank or the
    file cannot be written; a last line without a newline gets one first."""
    target = os.environ.get("CLAUDE_ENV_FILE") or ""
    project = claude_project_dir() or ""
    if not target.strip() or not project.strip():
        return
    line = os.fsencode(f"export HARNESS_SESSION_PROJECT={shlex.quote(project)}\n")
    try:
        with open(target, "ab+") as fh:  # appends; makes the file, never its folder
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
            fh.write(line)
    except Exception:  # fail open: no export rather than a broken brief
        pass


def main() -> int:
    started = time.monotonic()
    export_session_project()
    data = read_input()
    cwd = Path(hook_cwd(data))
    git_root = repo_root(cwd)
    root = git_root or cwd
    warning = []
    if LIB_PROBLEM:  # the guards fail open without the lib, so the session hears of it
        warning.append(
            f"Warning: {LIB_PROBLEM}; the guard hooks allow every call"
            " and this brief has no ticket state."
        )
    if declares_session_start(root):
        # The project's own hook replaces the brief and its title, but not the warning.
        if not warning:
            return 0
        out = {"hookEventName": "SessionStart", "additionalContext": clip(warning[0])}
        emit({"hookSpecificOutput": out})
        return 0
    name = root.name or str(root)
    # Outside a git repo the heading has no git facts; the sections come from the cwd.
    head, branch = f"## Session brief: {name}", None
    if git_root is not None:
        head, branch = heading(git_root, name)
    lines = [head, *warning, *extras(git_root, branch, started, cwd), *sections(root)]
    brief = "\n".join(clip(ln) for ln in lines[:MAX_LINES])

    out = {"hookEventName": "SessionStart", "additionalContext": brief}
    if data.get("source") == "startup":
        out["sessionTitle"] = name
    emit({"hookSpecificOutput": out})
    return 0


if __name__ == "__main__":
    # fail open: no brief rather than a broken session start
    sys.exit(run_fail_open("session_brief.py", main))
