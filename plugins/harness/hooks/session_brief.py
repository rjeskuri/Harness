#!/usr/bin/env python3
"""SessionStart hook: a short brief so a new, resumed or compacted session starts oriented.

Matcher (hooks.json): startup|resume|compact. Adds at most 40 lines of context:
  ## Session brief: <repo> on <branch>, <n> uncommitted, last commit <age>: <subject>
  - the Next steps section of docs/agent/handoff.md (first 12 lines), else the file's
    first 20 lines; or, without a handoff, the lines starting with "Next" in the last
    `## ` block of docs/PROGRESS.md (at most 6, none from inside a code fence);
  - Latest spec: the newest file in docs/agent/specs/;
  - the first three `"passes": false` items of docs/plans/feature_list.json;
  - Hours this month: the sum of this month's rows in worklog/worklog.csv.
On startup it also names the session after the repo (sessionTitle).

It prints nothing when the project's .claude/settings.json declares its own SessionStart
hook, so a project with a bespoke brief is not briefed twice. Outside a git repo the
heading carries no git facts, and the sections are read from the directory itself. Fails
open: an error means a shorter brief or none, never a blocked session.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_LINES = 40
MAX_LINE_CHARS = 300
READ_LIMIT = 512 * 1024  # bytes read from any one source file
# Three git calls at most (rev-parse, status, log): 4 + 8 + 4 s, inside the 20 s that
# hooks.json gives this hook.
GIT_TIMEOUT = 4
STATUS_TIMEOUT = 8

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


def read_input() -> dict:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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


def heading(root: Path, name: str) -> str:
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
    return text


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
            return " ".join(str(value).split())
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


# ---------- main ----------


def main() -> int:
    data = read_input()
    cwd = Path(
        str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    )
    git_root = repo_root(cwd)
    root = git_root or cwd
    if declares_session_start(root):
        return 0
    name = root.name or str(root)
    # Outside a git repo the heading has no git facts; the sections come from the cwd.
    head = f"## Session brief: {name}" if git_root is None else heading(git_root, name)
    lines = [head, *sections(root)]
    brief = "\n".join(clip(ln) for ln in lines[:MAX_LINES])

    out = {"hookEventName": "SessionStart", "additionalContext": brief}
    if data.get("source") == "startup":
        out["sessionTitle"] = name
    sys.stdout.write(json.dumps({"hookSpecificOutput": out}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open: no brief rather than a broken session start
        sys.stderr.write(f"session_brief.py: {exc}\n")
    sys.exit(0)
