"""The per-session state after_edit.py and verify_on_stop.py share, named once: the state
directory, the session key, the dirty list, the stamps and verify state beside it, and the
`git status` listing both hooks read. See docs/design.md (Layout).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path


class SessionState:
    """One session's files in the per-repo scratch directory, outside the repo:
    $TMPDIR/claude-harness/<first 12 hex of the sha1 of the resolved root>. Naming a path
    creates nothing; the first write (record, or ensure_dir before a caller's own write)
    makes the directory."""

    def __init__(self, dir: Path, session: str) -> None:
        self.dir = dir
        self.session = session

    @classmethod
    def for_repo(cls, root: Path, data: dict) -> SessionState:
        """The state of the hook event `data`'s session for the repo at `root`."""
        key = os.path.normcase(str(root.resolve())).encode("utf-8", "surrogateescape")
        dir = Path(tempfile.gettempdir()) / "claude-harness" / hashlib.sha1(key).hexdigest()[:12]
        session = re.sub(r"[^A-Za-z0-9._-]", "_", str(data.get("session_id") or "nosession"))
        return cls(dir, session)

    @property
    def dirty(self) -> Path:
        """The list of files edited in this session, one absolute path per line."""
        return self.dir / f"dirty-{self.session}"

    @property
    def verify_file(self) -> Path:
        """The Stop hook's JSON state for this session."""
        return self.dir / f"verify-{self.session}.json"

    def stamp(self, name: str) -> Path:
        """A per-session file of the caller's own, such as the last Bash scan's start time."""
        return self.dir / f"{name}-{self.session}"

    def ensure_dir(self) -> Path:
        """Create the state directory if needed (OSError if it cannot be) and return it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def record(self, paths: list[Path]) -> None:
        """Append `paths` to the dirty list, skipping any with a newline in it; nothing is
        written, and no directory made, when none are left. An OSError is swallowed."""
        lines = "".join(f"{p}\n" for p in paths if "\n" not in str(p))
        if not lines:
            return
        try:
            self.ensure_dir()
            with self.dirty.open("a", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(lines)
        except OSError:
            pass

    def dirty_files(self) -> list[Path]:
        """The dirty list's paths, sorted and unique; [] when there is none."""
        try:
            lines = self.dirty.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        except OSError:
            return []
        return sorted({Path(p) for p in lines if p.strip()})

    def clear_dirty(self) -> None:
        """Delete the dirty list (the other state files stay); no error when it is absent."""
        try:
            self.dirty.unlink()
        except OSError:
            pass


def porcelain_status(root: Path | None, timeout: float) -> bytes | None:
    """`git status --porcelain=v1 -z --untracked-files=all` output for the repo at `root`
    (without taking the index lock): b"" for a None root, None when git fails or times out."""
    if root is None:
        return b""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "status"]
            + ["--porcelain=v1", "-z", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def status_paths(root: Path, out: bytes) -> list[Path]:
    """The paths in `git status --porcelain=v1 -z` output: one "XY path" field per entry,
    and for a rename or copy (R or C in X or Y) a second field, its source, skipped here."""
    paths = []
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(root / os.fsdecode(entry[3:]))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            i += 1
    return paths
