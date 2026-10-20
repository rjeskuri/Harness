"""The project registry, ~/.claude/harness/projects.json: {"version": 1, "projects": [{"path": "/abs/path",
"name": "HKTrack", "billable": false, "client": "", "notes": ""}]}, in the order the standup shows them.
One reader and writer for harness-projects (strict), harness-board, harness-standup and harness-digest;
harness-pr keeps its own reader.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import dataclasses
import json
import os

from harness_lib.cli import Problem
from harness_lib.text import harness_home, same_path, write_json

VERSION = 1
KNOWN = ("path", "name", "billable", "client", "notes")


@dataclasses.dataclass
class Project:
    """One registered project; `extra` keeps the entry's unknown keys through a write."""

    path: str
    name: str
    billable: bool = False
    client: str = ""
    notes: str = ""
    extra: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def from_entry(cls, p):
        """The Project a registry entry with a path stands for, every field of the schema filled in."""
        return cls(path=p["path"], name=str(p.get("name") or os.path.basename(p["path"].rstrip("/")) or p["path"]),
                   billable=bool(p.get("billable", False)), client=str(p.get("client") or ""),
                   notes=str(p.get("notes") or ""), extra={k: v for k, v in p.items() if k not in KNOWN})

    def to_entry(self):
        """The registry entry: the schema's fields, then the unknown keys."""
        out = {"path": self.path, "name": self.name, "billable": self.billable, "client": self.client,
               "notes": self.notes}
        out.update((k, v) for k, v in self.extra.items() if k not in out)
        return out


def registry_path():
    """~/.claude/harness/projects.json."""
    return harness_home("projects.json")


def has_path(p):
    return isinstance(p, dict) and isinstance(p.get("path"), str) and bool(p["path"].strip())


def load_registry(strict=False, path=None):
    """The registered projects in registry order, [] when the file does not exist; Problem when it cannot
    be read. An entry without a path is skipped, or with `strict` a Problem naming it (counted from 1).
    `path` defaults to registry_path()."""
    path = path or registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except ValueError as err:
        raise Problem(f"{path} is not valid JSON ({err}); fix it or remove it") from None
    except OSError as err:
        raise Problem(f"cannot read {path}: {err.strerror or err}") from None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise Problem(f'{path}: expected {{"version": 1, "projects": [...]}}')
    if data.get("version", VERSION) != VERSION:
        raise Problem(f"{path} is version {data.get('version')!r}; this tool reads version 1")
    projects = []
    for n, p in enumerate(data["projects"], start=1):
        if has_path(p):
            projects.append(Project.from_entry(p))
        elif strict:
            raise Problem(f"{path}: project {n} has no path")
    return projects


def save_registry(projects):
    """Write the registry: a temporary file in the same folder, renamed over the old one."""
    write_json(registry_path(), {"version": VERSION, "projects": [p.to_entry() for p in projects]},
               prefix=".projects-")


def find_project(projects, key):
    """The project named `key` exactly, else in any case, else registered at the path `key`; Problem when
    none matches or several do."""
    for match in (lambda p: p.name == key, lambda p: p.name.casefold() == key.casefold(),
                  lambda p: same_path(p.path, key)):
        hits = [p for p in projects if match(p)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise Problem(f"{key!r} matches {len(hits)} projects; give its path instead")
    raise Problem(f"no registered project named or at {key!r} (harness-projects list shows them)")
